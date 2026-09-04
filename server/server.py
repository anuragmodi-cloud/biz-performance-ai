# server.py
"""FastAPI app: Phase 1's dev/test entrypoint (POST /ask) plus the admin API.

Run with:
    uvicorn server:app --port 8010 --reload

Phase 2 adds the real Pipecat/WebRTC voice bot (bot.py) alongside this same
app, the same way kyc-voice-agent's server.py hosts both the voice pipeline
and its REST endpoints in one process -- /ask stays as a text-only
debugging/testing entrypoint even after that lands.
"""
import os
import time
import uuid

from dotenv import load_dotenv

# Must run before importing any local module -- dev_llm_client (below) pulls
# in grounding.py, which reads JUDGE_AUTO_RUN from the environment at IMPORT
# TIME (a module-level constant, not re-read per call). Loading .env after
# that import would permanently bake in the pre-.env default ("flagged"),
# silently ignoring whatever JUDGE_AUTO_RUN is actually set to.
load_dotenv(override=True)

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import admin  # noqa: E402
import query_log  # noqa: E402
from dev_llm_client import ask as llm_ask  # noqa: E402
from session_store import SessionData, get_session, set_session, update_session  # noqa: E402

app = FastAPI(title="biz-performance-voice-agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
async def health():
    return {"status": "ok", "data_dir": os.getenv("DATA_DIR", "data/baseline_seed42")}


# ---------------------------------------------------------------------
# Phase 2 voice entrypoint (Pipecat / SmallWebRTC) -- lazily imported so a
# Phase-1-only install (no pipecat-ai) can still run /ask and /admin/*.
# ---------------------------------------------------------------------
try:
    from pipecat.runner.types import SmallWebRTCRunnerArguments
    from pipecat.transports.smallwebrtc.request_handler import (
        SmallWebRTCPatchRequest, SmallWebRTCRequest, SmallWebRTCRequestHandler,
    )

    import aioice_turn_patch
    from bot import bot as run_voice_bot
    from turn_credentials import fetch_ice_servers

    aioice_turn_patch.apply()
    _webrtc_handler = SmallWebRTCRequestHandler()
    _VOICE_AVAILABLE = True
except ImportError:
    _VOICE_AVAILABLE = False


if _VOICE_AVAILABLE:
    @app.on_event("startup")
    async def _configure_turn() -> None:
        # Fetched once at startup, not per-connection: the credential's 24h
        # TTL comfortably outlives any call, and Cloudflare's TURN
        # credentials aren't single-use, so there's no reason to pay a
        # per-request round trip (or risk a race between concurrent callers)
        # to fetch new ones for every /api/offer.
        ice_servers = await fetch_ice_servers()
        if ice_servers:
            _webrtc_handler.update_ice_servers(ice_servers)

    @app.post("/start-session")
    async def start_session():
        """Mint a session_id ahead of the WebRTC connect call -- same
        reasoning as kyc-voice-agent's /start-session: a plain REST call the
        frontend controls directly is a safer way to hand back a session_id
        than relying on undocumented extra fields in the SDP-answer payload.
        """
        session_id = str(uuid.uuid4())
        set_session(session_id, SessionData())
        return {"session_id": session_id}

    @app.post("/api/offer")
    async def offer(request: "SmallWebRTCRequest", background_tasks: BackgroundTasks, session_id: str | None = None):
        if session_id is None:
            session_id = str(uuid.uuid4())
            set_session(session_id, SessionData())

        async def webrtc_connection_callback(connection) -> None:
            runner_args = SmallWebRTCRunnerArguments(
                webrtc_connection=connection, body=request.request_data, session_id=session_id,
            )
            background_tasks.add_task(run_voice_bot, runner_args)

        return await _webrtc_handler.handle_web_request(
            request=request, webrtc_connection_callback=webrtc_connection_callback,
        )

    @app.patch("/api/offer")
    async def offer_ice_candidate(request: "SmallWebRTCPatchRequest"):
        await _webrtc_handler.handle_patch_request(request)
        return {"status": "success"}


@app.post("/session/{session_id}/typing")
async def session_typing(session_id: str):
    """Pinged by the client (throttled) while the user has text in the
    type-to-ask box -- lets bot.py's idle-check-in logic tell "user is
    composing a question" apart from "call went quiet / mic problem" so it
    doesn't talk over/interrupt someone who's just typing.
    """
    update_session(session_id, last_typing_at=time.time())
    return {"status": "ok"}


@app.get("/session/{session_id}/answers")
async def session_answers(session_id: str, limit: int = 20):
    """Public (no admin key) -- lets a connected caller's OWN client poll
    the structured results of their OWN session's asks, for the UI's output
    panel. Admin-only fields (judge verdicts, admin review) are left out;
    everything here is the caller's own business data from their own call.
    """
    entries = query_log.list_entries(session_id=session_id)
    entries = sorted(entries, key=lambda e: e.ts, reverse=True)[:limit]
    return [
        {
            "log_id": e.log_id, "ts": e.ts, "question_text": e.question_text,
            "result": e.result, "trace_summary": {
                "tables_queried": e.trace.get("tables_queried", []),
                "computations": e.trace.get("computations", []),
            } if e.trace else None,
            "narrated_text": e.narrated_text, "cache_hit": e.cache_hit, "status": e.status,
        }
        for e in entries
    ]


# ---------------------------------------------------------------------
# Phase 1 dev entrypoint
# ---------------------------------------------------------------------

class AskRequest(BaseModel):
    session_id: str | None = None
    question: str


class AskResponse(BaseModel):
    session_id: str
    narration: str
    log_ids: list[str]


@app.post("/ask", response_model=AskResponse)
async def ask(body: AskRequest):
    session_id = body.session_id or f"dev-{uuid.uuid4().hex[:8]}"
    get_session(session_id)  # ensure it exists even before any transcript entry
    try:
        result = await llm_ask(session_id, body.question)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")
    return AskResponse(session_id=session_id, narration=result["narration"], log_ids=result["log_ids"])


# ---------------------------------------------------------------------
# Admin API
# ---------------------------------------------------------------------

@app.get("/admin/summary", dependencies=[Depends(admin.require_admin_key)])
async def admin_summary():
    return admin.dashboard_summary()


@app.get("/admin/query-log", dependencies=[Depends(admin.require_admin_key)])
async def admin_query_log(session_id: str | None = None, status: str | None = None):
    return admin.list_query_log(session_id=session_id, status=status)


@app.get("/admin/query-log/{log_id}", dependencies=[Depends(admin.require_admin_key)])
async def admin_query_log_entry(log_id: str):
    return admin.get_query_log_entry(log_id)


class ReviewRequest(BaseModel):
    verdict: str  # "pass" | "fail"
    note: str | None = None


@app.post("/admin/query-log/{log_id}/review", dependencies=[Depends(admin.require_admin_key)])
async def admin_review(log_id: str, body: ReviewRequest):
    return admin.review_query_log_entry(log_id, body.verdict, body.note)


@app.get("/admin/score/{dataset}", dependencies=[Depends(admin.require_admin_key)])
async def admin_score(dataset: str, session_id: str | None = None):
    return admin.score_session(dataset, session_id=session_id)


@app.post("/admin/query-log/{log_id}/judge", dependencies=[Depends(admin.require_admin_key)])
async def admin_judge(log_id: str):
    return await admin.run_judge_now(log_id)
