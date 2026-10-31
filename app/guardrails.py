"""
guardrails.py - the last line of defence between an LLM-written query and the database.

The model is asked to write read-only SQL, but we never rely on that. Every query is
parsed with sqlglot and must pass ALL of these checks before it is executed:

  1. exactly one statement
  2. the statement is a SELECT (or a UNION/INTERSECT/EXCEPT of SELECTs), CTEs allowed
  3. no write/DDL/admin node anywhere in the tree (catches "WITH x AS (DELETE ...)")
  4. no SELECT ... INTO, no FOR UPDATE / FOR SHARE locks
  5. only whitelisted tables (CTE names excluded); no table-valued functions
  6. no dangerous functions (pg_*, lo_*, dblink*, set_config, ...)
  7. a LIMIT is always present and never larger than max_rows

The database role should ALSO be read-only (see schema.sql: poc_readonly). Two independent
layers is the point.
"""
from dataclasses import dataclass

import sqlglot
from sqlglot import exp


class GuardrailError(ValueError):
    """The SQL was rejected. The message is safe to show to the user / send back to the model."""


# Node types that must never appear anywhere in the parsed tree. Built with getattr so the
# module keeps working across sqlglot versions that rename a class.
_FORBIDDEN_NAMES = [
    "Insert", "Update", "Delete", "Drop", "Create", "Alter", "AlterTable", "Merge", "Command",
    "TruncateTable", "Copy", "Grant", "Revoke", "Set", "Use", "Pragma", "Transaction",
    "Commit", "Rollback", "LoadData", "Kill", "Analyze",
]
FORBIDDEN_NODES = tuple(getattr(exp, n) for n in _FORBIDDEN_NAMES if hasattr(exp, n))

# Set operations (UNION / INTERSECT / EXCEPT). Newer sqlglot: SetOperation; older: Union.
_SET_OP = getattr(exp, "SetOperation", exp.Union)

BLOCKED_FUNCTION_PREFIXES = (
    "pg_", "lo_", "dblink", "set_config", "query_to_xml", "table_to_xml",
    "cursor_to_xml", "database_to_xml", "schema_to_xml", "xpath",
)


@dataclass
class Validated:
    sql: str                 # normalised SQL in the target dialect, with LIMIT enforced
    tables: list             # base tables the query reads


def validate_sql(sql: str, dialect: str, allowed_tables, max_rows: int) -> Validated:
    """Return normalised, limit-capped SQL or raise GuardrailError."""
    allowed = {t.lower() for t in allowed_tables}

    try:
        statements = [s for s in sqlglot.parse(sql, read=dialect) if s is not None]
    except sqlglot.errors.ParseError as e:
        raise GuardrailError(f"SQL could not be parsed: {str(e).splitlines()[0]}")
    if len(statements) != 1:
        raise GuardrailError("Exactly one SQL statement is allowed.")
    tree = statements[0]

    if not isinstance(tree, (exp.Select, _SET_OP)):
        raise GuardrailError("Only SELECT queries are allowed.")

    bad = next(tree.find_all(*FORBIDDEN_NODES), None) if FORBIDDEN_NODES else None
    if bad is not None:
        raise GuardrailError(f"Statement type not allowed: {type(bad).__name__.upper()}.")

    for node in tree.find_all(exp.Select):
        if node.args.get("into"):
            raise GuardrailError("SELECT ... INTO is not allowed.")
        if node.args.get("locks"):
            raise GuardrailError("Row-locking clauses (FOR UPDATE / FOR SHARE) are not allowed.")

    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    tables = []
    for t in tree.find_all(exp.Table):
        if isinstance(t.this, exp.Func):
            raise GuardrailError("Table-valued functions are not allowed.")
        name = t.name.lower()
        if not t.db and name in cte_names:
            continue
        if t.db and t.db.lower() not in ("public",):
            raise GuardrailError(f"Schema-qualified table '{t.db}.{t.name}' is not allowed.")
        if name not in allowed:
            raise GuardrailError(
                f"Table '{t.name}' is not available. Use only: {', '.join(sorted(allowed))}.")
        tables.append(name)

    for fn in tree.find_all(exp.Anonymous):
        if fn.name.lower().startswith(BLOCKED_FUNCTION_PREFIXES):
            raise GuardrailError(f"Function '{fn.name}' is not allowed.")

    # Enforce a row cap: add a LIMIT if missing, clamp it if too big or not a plain number.
    limit = tree.args.get("limit")
    if limit is None:
        tree = tree.limit(max_rows)
    else:
        try:
            n = int(limit.expression.name)
        except (ValueError, AttributeError, TypeError):
            n = None
        if n is None or n > max_rows:
            tree = tree.limit(max_rows)

    return Validated(sql=tree.sql(dialect=dialect), tables=sorted(set(tables)))
