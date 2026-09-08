# session_store.py
"""In-memory per-session state, same pattern as kyc-voice-agent's
session_store.py: access only through get_session/update_session/
list_sessions, so this can be swapped for Redis later without touching call
sites.

query_cache is what satisfies "within a session, a repeated ask should not
re-trigger the calculation engine" -- keyed by QueryIntent.cache_key(), same
lifetime as the rest of session state (cleared when the session/call ends).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass
class CachedAnswer:
    result: dict
    trace: dict  # StepTrace.to_dict(): {tables_queried, computations, steps}
    computed_at: float = field(default_factory=time.time)


@dataclass
class SessionData:
    query_cache: dict[str, CachedAnswer] = field(default_factory=dict)
    connected_at: float | None = None
    disconnected_at: float | None = None
    # [{"role": "user"|"bot", "text": str, "at": float}] -- same shape as
    # kyc-voice-agent's transcript capture.
    transcript: list = field(default_factory=list)
    # log_ids produced by ask_calculation_engine calls since the last turn
    # was finalized (grounding.py's finalize_turn) -- appended to by
    # tools/ask_calculation_engine.py's handle() regardless of transport, so
    # both the text /ask path (dev_llm_client.py) and the voice path
    # (bot.py's turn-completion observer) can find "what tool calls happened
    # this turn" the same way, without threading state through closures.
    pending_log_ids: list = field(default_factory=list)
    # Updated by POST /session/{id}/typing, pinged by the client while the
    # user has text in the type-to-ask box (see voiceClient.js's
    # sendTypingPing). bot.py's on_user_turn_idle handler checks how recent
    # this is before treating silence as a dropped call/mic problem -- the
    # user is just composing, not gone.
    last_typing_at: float | None = None
    # Snapshot of the LLMContext's full message list (system prompt +
    # conversation so far), saved by bot.py's on_client_disconnected. A
    # reconnect within RESUME_WINDOW_SECS (see server.py's /start-session)
    # reuses this same session_id and restores these messages into the new
    # pipeline's LLMContext, so a brief disconnect doesn't reset the
    # conversation -- the bot picks up where it left off instead of
    # re-greeting as if it were a brand new call.
    saved_llm_messages: list | None = None
    # "<sub_metric>:<period>" for every gross_profit/gross_margin/
    # revenue_vs_profit/margin_by_category call made THIS SESSION (not just
    # this turn -- unlike pending_log_ids, which clears every turn).
    # ask_calculation_engine.py's _maybe_add_trend_hint reads this to detect
    # a profit/margin TREND question being answered by casting around among
    # these single-total tools instead of calling profit_by_month -- a
    # pattern confirmed spanning both repeats of the SAME sub_metric with a
    # different period, and switches to a DIFFERENT sub_metric entirely
    # (gross_profit, then two turns later revenue_vs_profit, still never
    # profit_by_month) -- and confirmed spanning separate turns too (an
    # initial ask, then an explicit "yes, month by month" follow-up), so a
    # per-turn-only or single-sub_metric-only check would miss it.
    non_trend_profit_calls_made: set = field(default_factory=set)


_sessions: dict[str, SessionData] = {}


def list_sessions() -> dict[str, SessionData]:
    return _sessions


def session_exists(session_id: str) -> bool:
    return session_id in _sessions


def get_session(session_id: str) -> SessionData:
    if session_id not in _sessions:
        _sessions[session_id] = SessionData()
    return _sessions[session_id]


def set_session(session_id: str, data: SessionData) -> None:
    _sessions[session_id] = data


def update_session(session_id: str, **fields) -> SessionData:
    session = get_session(session_id)
    for key, value in fields.items():
        setattr(session, key, value)
    return session
