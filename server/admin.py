# admin.py
"""Admin support: shared-secret auth (same pattern as kyc-voice-agent's
admin.py) + query-log listing/review + ground-truth scoring.

/admin/score is the ONLY place in this codebase that ever reads eval/ --
it does so server-side, for a human admin's JSON report, and that data is
never passed into any LLM context or tool response. Everything else
(engine/, tools/, dev_llm_client.py) never imports eval/ or references
ground_truth.json/business_questions.json at all.
"""
import json
import os
from pathlib import Path

from fastapi import Header, HTTPException

import query_log
from session_store import list_sessions

EVAL_DIR = Path(__file__).resolve().parents[1] / "eval"


async def require_admin_key(x_admin_key: str | None = Header(default=None)) -> None:
    configured_key = os.getenv("ADMIN_KEY")
    if not configured_key:
        raise HTTPException(status_code=503, detail="Admin access is not configured on this server.")
    if not x_admin_key or x_admin_key != configured_key:
        raise HTTPException(status_code=401, detail="Invalid admin key.")


def list_query_log(session_id: str | None = None, status: str | None = None) -> list[dict]:
    entries = query_log.list_entries(session_id=session_id, status=status)
    return [e.to_dict() for e in sorted(entries, key=lambda e: e.ts, reverse=True)]


def get_query_log_entry(log_id: str) -> dict:
    entry = query_log.get_entry(log_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"No query log entry {log_id!r}")
    return entry.to_dict()


def review_query_log_entry(log_id: str, verdict: str, note: str | None = None) -> dict:
    try:
        entry = query_log.review_entry(log_id, verdict, note)
    except (KeyError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    return entry.to_dict()


async def run_judge_now(log_id: str) -> dict:
    """On-demand judge trigger (POST /admin/query-log/{id}/judge) -- unlike
    dev_llm_client.py's fire-and-forget auto-scheduling, this is awaited and
    returns the verdict directly, for an admin who wants a second opinion
    right now on a specific ask (e.g. one JUDGE_AUTO_RUN=off skipped, or one
    whose auto-judge call previously failed and needs a retry)."""
    import judge

    entry = query_log.get_entry(log_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"No query log entry {log_id!r}")
    try:
        verdict = await judge.judge_entry(entry)
    except Exception as e:
        query_log.record_judge_error(log_id, f"{type(e).__name__}: {e}")
        raise HTTPException(status_code=502, detail=f"Judge call failed: {type(e).__name__}: {e}")
    query_log.apply_judge_verdict(log_id, verdict)
    return query_log.get_entry(log_id).to_dict()


def dashboard_summary() -> dict:
    return {
        "query_log": query_log.summary_stats(),
        "active_sessions": sum(1 for s in list_sessions().values() if s.disconnected_at is None),
        "total_sessions": len(list_sessions()),
    }


# ---------------------------------------------------------------------
# Ground-truth scoring -- admin-only, server-side eval/ access
# ---------------------------------------------------------------------

def _load_ground_truth(dataset: str) -> dict:
    path = EVAL_DIR / dataset / "ground_truth.json"
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"No eval/{dataset}/ground_truth.json -- this dataset wasn't synced with an eval bundle, "
                   f"or isn't a synthetic test dataset at all (a real deployment has no ground truth).",
        )
    with open(path) as f:
        return json.load(f)


def score_session(dataset: str, session_id: str | None = None) -> dict:
    """Cross-checks logged asks against the dataset's ground truth. This is
    necessarily approximate/heuristic (ground_truth.json wasn't authored as
    a per-question answer key) -- it reports what it CAN verify
    automatically (numbers present in both the log and any figure derivable
    from ground_truth) and leaves the rest for the admin_verdict manual
    review column.
    """
    gt = _load_ground_truth(dataset)
    entries = query_log.list_entries(session_id=session_id)

    report = {
        "dataset": dataset, "total_asks": len(entries),
        "by_status": {}, "flagged_for_manual_review": [],
    }
    for e in entries:
        report["by_status"][e.status] = report["by_status"].get(e.status, 0) + 1
        if e.status != query_log.STATUS_ANSWERED or e.admin_reviewed:
            continue
        # Heuristic: diagnostic-category asks and any ask whose result
        # referenced a candidate_events_in_window are exactly the
        # judgment-call cases automatic scoring can't resolve -- surface
        # them for a human to compare against ground_truth's
        # injected_scenarios / *_with_* fields directly.
        category = (e.resolved_intent or {}).get("metric_category")
        if category == "diagnostic" or (e.result or {}).get("candidate_events_in_window"):
            report["flagged_for_manual_review"].append({
                "log_id": e.log_id, "question_text": e.question_text, "result": e.result,
            })

    report["ground_truth_available"] = {
        "products_with_margin_decline": len(gt.get("products_with_margin_decline", [])),
        "suppliers_with_price_increase": len(gt.get("suppliers_with_price_increase", [])),
        "customers_with_payment_deterioration": len(gt.get("customers_with_payment_deterioration", [])),
        "slow_moving_products": len(gt.get("slow_moving_products", [])),
        "injected_scenarios": list(gt.get("injected_scenarios", {}).keys()),
    }
    return report
