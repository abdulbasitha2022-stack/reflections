import pytest

from app.guardrails import GuardrailError, validate_sql

ALLOWED = ["membership_sample", "ref_member_type_2"]
D = "postgres"


def ok(sql, max_rows=1000):
    return validate_sql(sql, D, ALLOWED, max_rows)


# ---------------------------------------------------------------- must PASS
@pytest.mark.parametrize("sql", [
    "SELECT count(*) FROM membership_sample",
    "SELECT count(*) AS n FROM membership_sample WHERE master_associate = '0';",
    "SELECT city, COUNT(*) FILTER (WHERE autobill_flag = 'Y') AS enrolled "
    "FROM membership_sample GROUP BY city ORDER BY enrolled DESC LIMIT 5",
    "WITH hh AS (SELECT * FROM membership_sample WHERE master_associate = '0') "
    "SELECT count(*) FROM hh",
    "SELECT COALESCE(payment_ts, billed_date) AS latest FROM membership_sample",
    "SELECT first_name FROM membership_sample WHERE first_name LIKE 'A%'",
    "SELECT expiration_date::date FROM membership_sample "
    "WHERE expiration_date < now() + interval '30 days'",
    "SELECT s.member_type, r.member_type_desc FROM membership_sample s "
    "JOIN ref_member_type_2 r ON r.member_type_code = s.member_type",
    "SELECT ui FROM membership_sample UNION SELECT primary_ui FROM membership_sample",
    "SELECT * FROM public.membership_sample",
])
def test_allowed(sql):
    assert ok(sql).sql.upper().startswith(("SELECT", "WITH"))


# ---------------------------------------------------------------- must FAIL
@pytest.mark.parametrize("sql,why", [
    ("DELETE FROM membership_sample", "not a select"),
    ("UPDATE membership_sample SET city = 'x'", "not a select"),
    ("DROP TABLE membership_sample", "not a select"),
    ("INSERT INTO membership_sample (ui) VALUES ('1')", "not a select"),
    ("TRUNCATE membership_sample", "not a select"),
    ("SELECT 1; DELETE FROM membership_sample", "two statements"),
    ("SELECT 1; SELECT 2", "two statements"),
    ("WITH d AS (DELETE FROM membership_sample RETURNING *) SELECT * FROM d", "write inside CTE"),
    ("SELECT * INTO backup FROM membership_sample", "select into"),
    ("SELECT * FROM membership_sample FOR UPDATE", "row lock"),
    ("SELECT * FROM pg_catalog.pg_user", "schema not allowed"),
    ("SELECT * FROM information_schema.tables", "schema not allowed"),
    ("SELECT * FROM some_other_table", "table not whitelisted"),
    ("SELECT * FROM membership_sample m JOIN secrets s ON s.id = m.ui", "join non-whitelisted"),
    ("SELECT pg_sleep(30)", "blocked function"),
    ("SELECT pg_read_file('/etc/passwd')", "blocked function"),
    ("SELECT * FROM generate_series(1, 1000000)", "table-valued function"),
    ("SELEC nonsense FROM", "unparseable"),
    ("", "empty"),
])
def test_rejected(sql, why):
    with pytest.raises(GuardrailError):
        ok(sql)


# ---------------------------------------------------------------- row cap
def test_limit_added_when_missing():
    assert "LIMIT 1000" in ok("SELECT ui FROM membership_sample").sql


def test_small_limit_kept():
    out = ok("SELECT ui FROM membership_sample LIMIT 5").sql
    assert "LIMIT 5" in out and "LIMIT 1000" not in out


def test_huge_limit_clamped():
    out = ok("SELECT ui FROM membership_sample LIMIT 999999").sql
    assert "LIMIT 1000" in out and "999999" not in out


def test_limit_added_on_union():
    assert "LIMIT 1000" in ok("SELECT ui FROM membership_sample UNION SELECT primary_ui "
                              "FROM membership_sample").sql


def test_cte_name_is_not_treated_as_a_table():
    r = ok("WITH hh AS (SELECT * FROM membership_sample) SELECT count(*) FROM hh")
    assert r.tables == ["membership_sample"]
