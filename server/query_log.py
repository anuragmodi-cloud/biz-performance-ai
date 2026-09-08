"""Structured per-ask logging -- what the admin dashboard's Query Log /
scoring tab is built from.

Every single ask (cache hit or miss, successful or not) appends exactly one
QueryLogEntry here. Backed by Supabase/Postgres (db.py's shared connection
pool) rather than local SQLite -- the whole point of the admin eval-trace
UI is to look back at what happened, and a container with no persistent
disk (Render's free tier) was wiping SQLite on every restart/redeploy,
which defeated that. Same get/append/list function signatures as before, so
no caller (admin.py, grounding.py, tools/ask_calculation_engine.py,
dev_llm_client.py) needed to change.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field

from psycopg.rows import dict_row

from db import get_pool

STATUS_ANSWERED = "answered"
STATUS_HALLUCINATION = "possible_hallucination"
STATUS_UNFULFILLED = "unfulfilled"
# The LLM produced a final narration without ever calling
# ask_calculation_engine in this exchange -- there is no trace to ground it
# against at all, so this is worse than possible_hallucination (which at
# least has a real computed result to check numbers against) and is always
# an automatic fail, never eligible for admin_verdict="pass".
STATUS_NO_ENGINE_CALL = "answered_without_engine"
# The engine found more than one plausible entity match for a caller-named
# customer/product/supplier and refused to silently guess one (see
# engine/formulas.py's resolve_entity) -- there's no single result to ground
# narration numbers against, same as STATUS_UNFULFILLED, but distinguished
# so the admin dashboard and judge can tell "ambiguous, asked for
# clarification" apart from "couldn't answer at all."
STATUS_AMBIGUOUS_ENTITY = "ambiguous_entity"
# An unexpected exception inside ask_calculation_engine.handle() -- NOT one
# of the two anticipated failure shapes above (a bad question / no matching
# data), but a genuine bug the engine hit. Distinguished so it's trivial to
# filter admin's Query Log for "the tool actually crashed" specifically,
# with the full traceback in `error`. Before this existed, an exception here
# had nowhere safe to go: bot.py's voice tool-call adapter had no try/except
# of its own, so it silently killed the whole turn -- the bot said its
# "let me calculate" filler line and then never responded again, since the
# LLM was left waiting on a tool result that would never arrive.
STATUS_TOOL_CRASH = "tool_crash"

# Columns that hold a dict/list in QueryLogEntry and need JSON en/decoding
# to round-trip through a Postgres TEXT column.
_JSON_FIELDS = {"resolved_intent", "trace", "result"}


@dataclass
class QueryLogEntry:
    log_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    session_id: str = ""
    ts: float = field(default_factory=time.time)
    question_text: str = ""
    resolved_intent: dict | None = None
    cache_hit: bool = False
    trace: dict = field(default_factory=dict)  # StepTrace.to_dict(): {tables_queried, computations, steps}
    result: dict | None = None
    narrated_text: str = ""
    status: str = STATUS_UNFULFILLED
    error: str | None = None
    latency_ms: float = 0.0
    # Admin manual override (mirrors kyc-voice-agent's confirm/dispute
    # review pattern) -- for judgment-call asks the automatic check can't
    # resolve on its own (diagnostic/action categories, mainly).
    admin_reviewed: bool = False
    admin_verdict: str | None = None  # "pass" | "fail"
    admin_note: str | None = None
    # Judge LLM (judge.py) -- an independent, typically higher-capability
    # model that audits the full evidence trail after the fact and assigns
    # a strict pass/fail with the earliest pipeline stage that went wrong.
    # None until judged; judge_error is set instead of the rest if the
    # judge call itself failed (bad key, provider error, etc.).
    judge_verdict: str | None = None  # "pass" | "fail"
    judge_failed_stage: str | None = None  # "intent_resolution" | "computation" | "narration" | "none"
    judge_reason: str | None = None
    judge_downstream_impact: str | None = None
    judge_model: str | None = None
    judge_ran_at: float | None = None
    judge_error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------
# Postgres plumbing -- one shared pool (db.py), a fresh connection borrowed
# per call. This app's write volume (one row per ask, occasional judge/
# review updates) is far below where pool contention would matter.
# ---------------------------------------------------------------------

def _init_db() -> None:
    with get_pool().connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS query_log (
                log_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL DEFAULT '',
                ts DOUBLE PRECISION NOT NULL,
                question_text TEXT NOT NULL DEFAULT '',
                resolved_intent TEXT,
                cache_hit BOOLEAN NOT NULL DEFAULT FALSE,
                trace TEXT,
                result TEXT,
                narrated_text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'unfulfilled',
                error TEXT,
                latency_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
                admin_reviewed BOOLEAN NOT NULL DEFAULT FALSE,
                admin_verdict TEXT,
                admin_note TEXT,
                judge_verdict TEXT,
                judge_failed_stage TEXT,
                judge_reason TEXT,
                judge_downstream_impact TEXT,
                judge_model TEXT,
                judge_ran_at DOUBLE PRECISION,
                judge_error TEXT
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_query_log_session_id ON query_log(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_query_log_status ON query_log(status)")


_init_db()


def _entry_to_row(entry: QueryLogEntry) -> dict:
    row = asdict(entry)
    for field_name in _JSON_FIELDS:
        row[field_name] = json.dumps(row[field_name]) if row[field_name] is not None else None
    return row


def _row_to_entry(row: dict) -> QueryLogEntry:
    kwargs = dict(row)
    for field_name in _JSON_FIELDS:
        kwargs[field_name] = json.loads(kwargs[field_name]) if kwargs[field_name] is not None else (
            {} if field_name == "trace" else None
        )
    return QueryLogEntry(**kwargs)


def append(entry: QueryLogEntry) -> QueryLogEntry:
    row = _entry_to_row(entry)
    columns = list(row.keys())
    placeholders = ", ".join(f"%({c})s" for c in columns)
    with get_pool().connection() as conn:
        conn.execute(f"INSERT INTO query_log ({', '.join(columns)}) VALUES ({placeholders})", row)
    return entry


def list_entries(session_id: str | None = None, status: str | None = None) -> list[QueryLogEntry]:
    query = "SELECT * FROM query_log WHERE 1=1"
    params: list = []
    if session_id is not None:
        query += " AND session_id = %s"
        params.append(session_id)
    if status is not None:
        query += " AND status = %s"
        params.append(status)
    query += " ORDER BY ts DESC"
    with get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
    return [_row_to_entry(r) for r in rows]


def get_entry(log_id: str) -> QueryLogEntry | None:
    with get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute("SELECT * FROM query_log WHERE log_id = %s", (log_id,))
            row = cur.fetchone()
    return _row_to_entry(row) if row else None


def _update(log_id: str, **fields) -> QueryLogEntry:
    row = {}
    for key, value in fields.items():
        row[key] = json.dumps(value) if key in _JSON_FIELDS and value is not None else value
    set_clause = ", ".join(f"{k} = %({k})s" for k in row)
    row["log_id"] = log_id
    with get_pool().connection() as conn:
        conn.execute(f"UPDATE query_log SET {set_clause} WHERE log_id = %(log_id)s", row)
    entry = get_entry(log_id)
    if entry is None:
        raise KeyError(f"No query log entry {log_id!r}")
    return entry


def finalize_log_entry(log_id: str, narrated_text: str) -> QueryLogEntry:
    """Single-entry version of finalize_log_entries, for the common case of
    one tool call per exchange. See finalize_log_entries's docstring for why
    multi-call exchanges need the union-of-traces version instead.
    """
    return finalize_log_entries([log_id], narrated_text)[0]


def finalize_log_entries(log_ids: list[str], narrated_text: str) -> list[QueryLogEntry]:
    """Called once the LLM's spoken narration for an exchange is known.

    When an exchange makes SEVERAL tool calls (e.g. the LLM asks for
    revenue, overdue receivables, and margin in one turn and narrates all
    three together), a single log entry's own trace only has that one
    call's numbers -- checking the shared narration against just that trace
    would flag the other two calls' numbers as unmatched even though they
    ARE grounded, just in a sibling entry from the same exchange. So this
    grounds against the UNION of every given entry's trace, and applies the
    resulting verdict to all of them (they share one narration, so they
    share one verdict). Entries that already ended in `unfulfilled` or
    `answered_without_engine` are left alone -- there's no result to
    narrate against for those.
    """
    from hallucination_check import check_narration, extract_numbers, numbers_from_serialized_trace

    entries = []
    for log_id in log_ids:
        entry = get_entry(log_id)
        if entry is None:
            raise KeyError(f"No query log entry {log_id!r}")
        entry.narrated_text = narrated_text
        entries.append(entry)
    _update_narrated_text_only(log_ids, narrated_text)

    groundable = [e for e in entries if e.status not in (STATUS_UNFULFILLED, STATUS_NO_ENGINE_CALL, STATUS_AMBIGUOUS_ENTITY)]
    if not groundable:
        return entries

    grounded_pool: list[float] = []
    for e in groundable:
        grounded_pool.extend(numbers_from_serialized_trace(e.trace))
        grounded_pool.extend(extract_numbers(e.result))
    check = check_narration(narrated_text, grounded_pool)
    verdict = STATUS_ANSWERED if check["grounded"] else STATUS_HALLUCINATION
    for e in groundable:
        e.status = verdict
        _update(e.log_id, status=verdict)
    return entries


def _update_narrated_text_only(log_ids: list[str], narrated_text: str) -> None:
    with get_pool().connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE query_log SET narrated_text = %s WHERE log_id = %s",
                [(narrated_text, log_id) for log_id in log_ids],
            )


def apply_judge_verdict(log_id: str, verdict: dict) -> QueryLogEntry:
    """Records a judge.judge_entry() result onto its log entry. Called after
    the judge LLM call returns -- see dev_llm_client.py's fire-and-forget
    scheduling and admin.py's on-demand trigger, both of which call this.
    """
    return _update(
        log_id,
        judge_verdict=verdict.get("verdict"),
        judge_failed_stage=verdict.get("failed_stage"),
        judge_reason=verdict.get("reason"),
        judge_downstream_impact=verdict.get("downstream_impact") or None,
        judge_model=verdict.get("judge_model"),
        judge_ran_at=time.time(),
        judge_error=None,
    )


def record_judge_error(log_id: str, error: str) -> QueryLogEntry:
    return _update(log_id, judge_error=error, judge_ran_at=time.time())


def review_entry(log_id: str, verdict: str, note: str | None = None) -> QueryLogEntry:
    if verdict not in ("pass", "fail"):
        raise ValueError(f"Unknown verdict: {verdict!r}")
    return _update(log_id, admin_reviewed=True, admin_verdict=verdict, admin_note=note)


def summary_stats() -> dict:
    entries = list_entries()
    total = len(entries)
    by_status: dict[str, int] = {}
    for e in entries:
        by_status[e.status] = by_status.get(e.status, 0) + 1
    cache_hits = sum(1 for e in entries if e.cache_hit)
    reviewed = [e for e in entries if e.admin_reviewed]
    admin_fails = sum(1 for e in reviewed if e.admin_verdict == "fail")
    judged = [e for e in entries if e.judge_verdict is not None]
    judge_fails = sum(1 for e in judged if e.judge_verdict == "fail")
    judge_failed_stages: dict[str, int] = {}
    for e in judged:
        if e.judge_verdict == "fail" and e.judge_failed_stage:
            judge_failed_stages[e.judge_failed_stage] = judge_failed_stages.get(e.judge_failed_stage, 0) + 1
    return {
        "total_asks": total,
        "by_status": by_status,
        "cache_hit_rate_pct": round(cache_hits / total * 100, 2) if total else None,
        "admin_reviewed": len(reviewed),
        "admin_fail_rate_pct": round(admin_fails / len(reviewed) * 100, 2) if reviewed else None,
        "judge_reviewed": len(judged),
        "judge_fail_rate_pct": round(judge_fails / len(judged) * 100, 2) if judged else None,
        "judge_failed_stages": judge_failed_stages,
    }
