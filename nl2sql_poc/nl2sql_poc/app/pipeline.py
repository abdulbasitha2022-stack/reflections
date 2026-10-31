"""Pipeline: conversational planner, SQL generation, validation and read-only execution.

Two LLM stages, both stateless helpers (conversation memory lives in session.py):

  plan()          chat history + state  -> Plan (intent, reply, self-contained request, ...)
  generate_sql()  request (+ old SQL)   -> Result (validated SQL executed read-only)
"""
import getpass
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from .db import QueryError
from .guardrails import GuardrailError, validate_sql
from .prompt import build_planner_prompt, build_system_prompt

REPORT_INTENTS = ("new_report", "modify_report")
INTENTS = ("chat", "explain", "new_report", "modify_report", "change_chart", "clarify", "decline")
VISUAL_TYPES = ("auto", "clusteredColumnChart", "barChart", "lineChart", "pieChart", "donutChart", "tableEx")

_CHART_ALIASES = {
    "auto": "auto", "automatic": "auto", "": "auto",
    "column": "clusteredColumnChart", "columns": "clusteredColumnChart",
    "columnchart": "clusteredColumnChart", "clusteredcolumnchart": "clusteredColumnChart",
    "verticalbar": "clusteredColumnChart", "verticalbarchart": "clusteredColumnChart",
    "bar": "barChart", "barchart": "barChart", "horizontalbar": "barChart",
    "horizontalbarchart": "barChart",
    "line": "lineChart", "linechart": "lineChart", "trend": "lineChart",
    "pie": "pieChart", "piechart": "pieChart",
    "donut": "donutChart", "doughnut": "donutChart", "donutchart": "donutChart",
    "doughnutchart": "donutChart",
    "table": "tableEx", "tableex": "tableEx", "grid": "tableEx", "tableonly": "tableEx",
}


def normalize_chart_type(value):
    """Map a model/user chart name to a Power BI visual type; None when unrecognised."""
    if value is None:
        return "auto"
    key = re.sub(r"[^a-z]", "", str(value).lower())
    return _CHART_ALIASES.get(key)


# --------------------------------------------------------------------------- SQL reply parsing
@dataclass
class Parsed:
    kind: str  # "sql" | "clarify" | "invalid"
    sql: str = ""
    assumptions: list = field(default_factory=list)
    clarification: str = ""


_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.S | re.I)
_ASSUME = re.compile(r"^\s*ASSUMPTION:\s*(.+?)\s*$", re.M | re.I)
_CLARIFY = re.compile(r"^\s*CLARIFY:\s*(.+?)\s*$", re.M | re.I)


def parse_response(text: str) -> Parsed:
    fence = _FENCE.search(text)
    assumptions = [match.group(1) for match in _ASSUME.finditer(text)]
    if fence and fence.group(1).strip():
        return Parsed("sql", sql=fence.group(1).strip(), assumptions=assumptions)
    clarification = _CLARIFY.search(text)
    if clarification:
        return Parsed("clarify", clarification=clarification.group(1))
    if re.match(r"\s*(with|select)\b", text, re.I):
        return Parsed("sql", sql=text.strip(), assumptions=assumptions)
    return Parsed("invalid")


# --------------------------------------------------------------------------- planner parsing
@dataclass
class Plan:
    intent: str
    reply: str = ""
    request: str = ""
    title: str = ""
    chart_type: str = "auto"
    assumptions: list = field(default_factory=list)
    confidence: int = 0          # the readiness score (0-100) the model gave the request
    scored: bool = False         # False when the model gave no score at all
    sufficient: bool = True
    clarification: str = ""


def _extract_json_object(text: str):
    cleaned = (text or "").strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        first, last = cleaned.find("{"), cleaned.rfind("}")
        if first < 0 or last <= first:
            raise ValueError("The planner did not return a JSON object.")
        try:
            return json.loads(cleaned[first:last + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"The planner returned invalid JSON: {exc}") from exc


def parse_plan(text: str) -> Plan:
    """Parse the planner reply, tolerating fences, stray text, float scores and odd casing."""
    data = _extract_json_object(text)
    if not isinstance(data, dict):
        raise ValueError("The planner reply must be a JSON object.")

    def as_text(key):
        value = data.get(key)
        return value.strip() if isinstance(value, str) else ""

    reply, request, title = as_text("reply"), as_text("request"), as_text("title")
    intent = re.sub(r"[\s\-]+", "_", as_text("intent").lower())
    if intent not in INTENTS:
        if request:
            intent = "new_report"
        elif reply:
            intent = "chat"
        else:
            raise ValueError("The planner reply had no usable intent.")

    chart = normalize_chart_type(data.get("chart_type"))
    raw_assumptions = data.get("assumptions")
    assumptions = [str(a).strip() for a in raw_assumptions if str(a).strip()][:5] \
        if isinstance(raw_assumptions, list) else []

    raw_score = data.get("score", data.get("confidence"))
    scored, confidence = False, 0
    if raw_score is not None:
        try:
            confidence, scored = max(0, min(100, int(float(raw_score)))), True
        except (TypeError, ValueError):
            pass

    raw_sufficient = data.get("sufficient")
    if isinstance(raw_sufficient, str):
        sufficient = raw_sufficient.strip().lower() in ("true", "yes", "1")
    elif raw_sufficient is None:
        sufficient = intent != "clarify"
    else:
        sufficient = bool(raw_sufficient)
    return Plan(intent=intent, reply=reply, request=request, title=title,
                chart_type=chart or "auto", assumptions=assumptions, confidence=confidence,
                scored=scored, sufficient=sufficient, clarification=as_text("clarification"))


# --------------------------------------------------------------------------- results
@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0

    def add(self, response):
        self.input_tokens += response.input_tokens
        self.output_tokens += response.output_tokens
        self.cache_read_tokens += response.extra.get("cache_read_tokens", 0)


@dataclass
class Result:
    request_id: str
    question: str
    status: str = "failed"  # ok | clarify | chat | failed | error
    intent: str = ""
    sql: str = ""
    assumptions: list = field(default_factory=list)
    clarification: str = ""
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)
    truncated: bool = False
    attempts: int = 0
    error: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    latency_s: float = 0.0
    sql_runtime_ms: int = 0
    prompt_score: int = 0
    prompt_reason: str = ""


class PlannerError(RuntimeError):
    """The planner LLM call failed or kept returning unusable output."""


class Pipeline:
    def __init__(self, llm, db, cfg, base_dir="."):
        self.llm, self.db = llm, db
        self.cfg = cfg
        db_cfg = cfg["db"]
        self.allowed = db_cfg["allowed_tables"]
        self.max_rows = int(db_cfg.get("max_rows", 1000))
        pipeline_cfg = cfg.get("pipeline", {})
        self.max_retries = int(pipeline_cfg.get("max_retries", 2))
        self.log_path = Path(base_dir, pipeline_cfg["log_file"]) if pipeline_cfg.get("log_file") else None

        context_cfg = cfg.get("context", {})
        self._context_texts = [Path(base_dir, path).read_text(encoding="utf-8")
                               for path in context_cfg.get("files", [])]
        self._extra_rules = context_cfg.get("extra_rules", "")
        self._schema_text = db.describe(self.allowed)
        self._sql_system, self._sql_system_day = None, None

    # The prompts embed today's date. Rebuild when the day changes so a server that stays up
    # for days still resolves "last month" / "this year" correctly.
    @property
    def system(self):
        today = date.today()
        if self._sql_system_day != today:
            self._sql_system = build_system_prompt(
                dialect=self.db.dialect, schema_text=self._schema_text,
                context_texts=self._context_texts, extra_rules=self._extra_rules,
                max_rows=self.max_rows, today=today)
            self._sql_system_day = today
        return self._sql_system

    def planner_system(self, state: str = "") -> str:
        return build_planner_prompt(
            dialect=self.db.dialect, schema_text=self._schema_text,
            context_texts=self._context_texts, extra_rules=self._extra_rules, state=state)

    # ------------------------------------------------------------------ stage 1: plan
    def plan(self, messages: list, state: str = ""):
        """Return (Plan, Usage). Retries once when the reply is not valid JSON."""
        system = self.planner_system(state)
        usage, msgs, last_error = Usage(), list(messages), None
        for _ in range(2):
            try:
                response = self.llm.complete(system, msgs)
            except Exception as exc:
                raise PlannerError(f"LLM call failed: {exc}") from exc
            usage.add(response)
            try:
                return parse_plan(response.text), usage
            except ValueError as exc:
                last_error = exc
                msgs = msgs + [
                    {"role": "assistant", "content": response.text or "(empty reply)"},
                    {"role": "user", "content": "That was not valid. Reply with ONLY the JSON object "
                                                "described in the instructions."},
                ]
        raise PlannerError(str(last_error))

    # ------------------------------------------------------------------ stage 2: SQL
    def generate_sql(self, request_text: str, *, previous: dict | None = None,
                     assumptions: list | None = None) -> Result:
        started = time.perf_counter()
        result = Result(request_id=uuid.uuid4().hex[:8], question=request_text)
        content = f"Report request: {request_text}"
        if assumptions:
            content += "\nAgreed assumptions (stay consistent with them): " + "; ".join(assumptions)
        if previous and previous.get("sql"):
            content += (f"\n\nThe user is revising an earlier report.\nEarlier request: "
                        f"{previous.get('request', '')}\nEarlier SQL:\n```sql\n{previous['sql']}\n```\n"
                        "Update this query so it satisfies the new request.")
        messages = [{"role": "user", "content": content}]
        system = self.system

        for attempt in range(1, self.max_retries + 2):
            result.attempts = attempt
            try:
                response = self.llm.complete(system, messages)
            except Exception as exc:
                result.status, result.error = "error", f"LLM call failed: {exc}"
                break
            result.input_tokens += response.input_tokens
            result.output_tokens += response.output_tokens
            result.cache_read_tokens += response.extra.get("cache_read_tokens", 0)
            parsed = parse_response(response.text)

            if parsed.kind == "clarify":
                result.status, result.clarification, result.error = "clarify", parsed.clarification, ""
                break

            failure = None
            if parsed.kind != "sql":
                failure = "Reply did not follow the output protocol (expected a SQL block or CLARIFY line)."
                result.error = failure
            else:
                try:
                    validated = validate_sql(parsed.sql, self.db.dialect, self.allowed, self.max_rows)
                except GuardrailError as exc:
                    result.sql, failure = parsed.sql, str(exc)
                    result.error = failure
                else:
                    query_started = time.perf_counter()
                    try:
                        query_result = self.db.execute_readonly(validated.sql, self.max_rows)
                    except QueryError as exc:
                        result.sql_runtime_ms = round((time.perf_counter() - query_started) * 1000)
                        result.sql, failure = validated.sql, str(exc)
                        result.error = failure
                    else:
                        result.sql_runtime_ms = round((time.perf_counter() - query_started) * 1000)
                        result.status, result.error = "ok", ""
                        result.sql, result.assumptions = validated.sql, parsed.assumptions
                        result.columns, result.rows = query_result.columns, query_result.rows
                        result.truncated = query_result.truncated
                        break
            messages += [
                {"role": "assistant", "content": response.text or "(empty reply)"},
                {"role": "user", "content": f"That failed: {failure}\n"
                                            "Return a corrected reply following the same output protocol."},
            ]

        result.latency_s = round(time.perf_counter() - started, 2)
        self._log(result)
        return result

    # ------------------------------------------------------------------ CLI convenience
    def ask(self, question: str) -> Result:
        """Single-shot use (run.py): plan, then build SQL when the plan is a report."""
        started = time.perf_counter()
        try:
            plan, usage = self.plan([{"role": "user", "content": question}],
                                    "LAST REPORT: none yet. Nothing to explain, modify or re-chart.")
        except PlannerError as exc:
            return Result(request_id=uuid.uuid4().hex[:8], question=question, status="error",
                          error=f"Prompt evaluation failed: {exc}")
        if plan.intent in REPORT_INTENTS:
            result = self.generate_sql(plan.request or question, assumptions=plan.assumptions)
            result.question = question
        else:
            result = Result(request_id=uuid.uuid4().hex[:8], question=question,
                            status="clarify" if plan.intent == "clarify" else "chat",
                            clarification=plan.reply)
        result.intent = plan.intent
        result.prompt_score = plan.confidence
        result.input_tokens += usage.input_tokens
        result.output_tokens += usage.output_tokens
        result.cache_read_tokens += usage.cache_read_tokens
        result.latency_s = round(time.perf_counter() - started, 2)
        return result

    # ------------------------------------------------------------------ logging
    def _append_log(self, entry: dict):
        if not self.log_path:
            return
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.log_path, "a", encoding="utf-8") as log_file:
            log_file.write(json.dumps(entry, default=str) + "\n")

    def _base_entry(self) -> dict:
        return {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "user": getpass.getuser(), "provider": self.llm.name, "model": self.llm.model}

    def _log(self, result: Result):
        self._append_log({
            **self._base_entry(), "request_id": result.request_id, "question": result.question,
            "status": result.status, "sql": result.sql, "assumptions": result.assumptions,
            "attempts": result.attempts, "rows": len(result.rows), "latency_s": result.latency_s,
            "sql_runtime_ms": result.sql_runtime_ms, "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens, "cache_read_tokens": result.cache_read_tokens,
            "prompt_score": result.prompt_score, "prompt_reason": result.prompt_reason,
            "error": result.error})

    def log_turn(self, **fields):
        """Log a conversational turn that did not need SQL (chat, explain, clarify, ...)."""
        self._append_log({**self._base_entry(), **fields})
