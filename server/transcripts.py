"""Persists voice/text conversation transcripts to Supabase/Postgres
(db.py's shared pool) -- previously session.transcript (session_store.py)
was in-memory only and never written anywhere durable, so it was lost the
moment a session's process state went away.

Deliberately NOT written per audio chunk. voice_observer.py accumulates
transcript entries in-memory as they stream in (cheap, no I/O, safe on the
real-time frame-processing hot path) and calls persist_new_entries() only
once per finalized turn, at the same point it already knows the turn is
truly done (grounding.finalize_turn()) -- writing to a remote database on
every TTSTextFrame/TranscriptionFrame chunk would add real per-chunk
latency to that hot path, which is exactly the kind of thing that caused
the choppy-audio bug fixed earlier (a different cause, same lesson: don't
add blocking I/O to the frame pipeline).
"""
from __future__ import annotations

from loguru import logger

from db import get_pool
from session_store import get_session, update_session


def _init_db() -> None:
    with get_pool().connection() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS voice_transcripts (
                id BIGSERIAL PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                at DOUBLE PRECISION NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_voice_transcripts_session_id ON voice_transcripts(session_id)")


_init_db()


def persist_new_entries(session_id: str) -> None:
    """Persists whatever's in session.transcript since the last persist
    checkpoint (session.transcript_persisted_count), then advances the
    checkpoint. Best-effort: any failure is logged, never raised --
    transcript logging must never break the actual conversation, voice or
    text. Called directly (sync, briefly blocking -- acceptable, /ask is
    already a slow LLM-latency-bound call) from dev_llm_client.py, and
    wrapped in asyncio.to_thread by voice_observer.py so the real-time
    frame pipeline is never blocked on it.
    """
    session = get_session(session_id)
    new_entries = session.transcript[session.transcript_persisted_count:]
    if not new_entries:
        return
    update_session(session_id, transcript_persisted_count=len(session.transcript))
    try:
        with get_pool().connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    "INSERT INTO voice_transcripts (session_id, role, text, at) VALUES (%s, %s, %s, %s)",
                    [(session_id, e["role"], e["text"], e["at"]) for e in new_entries],
                )
    except Exception as e:  # noqa: BLE001 -- best-effort logging, never breaks the conversation
        logger.warning(f"session_id={session_id}: failed to persist {len(new_entries)} transcript entries: {type(e).__name__}: {e}")
