"""Shared turn-finalization logic: numeric grounding check + judge
scheduling, used identically by the text path (dev_llm_client.py's /ask)
and the voice path (bot.py's turn-completion observer).

A "turn" is complete once the actor's full narration for this exchange is
known -- for text mode that's the moment the HTTP handler has the whole
string; for voice mode that's when BotStoppedSpeakingFrame fires and every
TTSTextFrame chunk since the last turn has been collected. Either way, the
same two things need to happen: ground the narration against whichever
ask_calculation_engine calls happened this turn (session.pending_log_ids,
appended to by tools/ask_calculation_engine.py's handle() regardless of
transport), and schedule the judge LLM per JUDGE_AUTO_RUN.
"""
from __future__ import annotations

import asyncio
import os

from loguru import logger

import judge
from query_log import (
    STATUS_ANSWERED, apply_judge_verdict, finalize_log_entries, get_entry, record_judge_error,
)
from session_store import get_session, update_session

_JUDGE_AUTO_RUN = (os.getenv("JUDGE_AUTO_RUN") or "flagged").lower()


async def _run_judge_background(log_id: str) -> None:
    entry = get_entry(log_id)
    if entry is None:
        return
    try:
        verdict = await judge.judge_entry(entry)
        apply_judge_verdict(log_id, verdict)
        logger.info(f"judge: log_id={log_id} verdict={verdict.get('verdict')} stage={verdict.get('failed_stage')}")
    except Exception as e:  # noqa: BLE001 -- a broken judge call must never affect the actor's response
        record_judge_error(log_id, f"{type(e).__name__}: {e}")
        logger.warning(f"judge: log_id={log_id} failed to run: {type(e).__name__}: {e}")


def schedule_judge(log_ids: list[str]) -> None:
    if _JUDGE_AUTO_RUN == "off":
        return
    for log_id in log_ids:
        if _JUDGE_AUTO_RUN == "flagged":
            entry = get_entry(log_id)
            if entry is None or entry.status == STATUS_ANSWERED:
                continue
        asyncio.create_task(_run_judge_background(log_id))


def finalize_turn(session_id: str, narration: str) -> list[str]:
    """Call once a turn's full narration text is known. Grounds it against
    every ask_calculation_engine call made this turn (session.pending_log_ids),
    schedules the judge per JUDGE_AUTO_RUN, and clears the pending list so
    the next turn starts fresh. Returns the log_ids that were finalized.
    """
    session = get_session(session_id)
    log_ids = list(session.pending_log_ids)
    if log_ids:
        finalize_log_entries(log_ids, narration)
        schedule_judge(log_ids)
    update_session(session_id, pending_log_ids=[])
    return log_ids
