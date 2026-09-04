"""Phase-1-only: a minimal chat + tool-call loop over the raw provider SDKs,
used by server.py's POST /ask so the tool/engine/cache/logging path can be
fully exercised over plain HTTP without a Pipecat pipeline or voice
API keys.

Phase 2's bot.py runs the real Pipecat pipeline (llm_factory.py + Sarvam
STT/TTS) instead of this file -- everything both call into
(tools/ask_calculation_engine.py, engine/, session_store.py, query_log.py,
grounding.py) is shared, unchanged by which transport is driving a turn.

Each /ask call is one complete request/response cycle: the LLM decides
whether to call the tool, we execute it if so, feed the result back, and
return the final narration. Conversation continuity across calls within a
session is reconstructed from session.transcript (a plain list of
{role, text}), not from provider-specific message objects -- simple, and
sufficient for a dev/test entrypoint.
"""
from __future__ import annotations

import json
import os
import time

from loguru import logger

import grounding
from prompts.system_prompt_hi import SYSTEM_PROMPT
from query_log import QueryLogEntry, STATUS_NO_ENGINE_CALL, append as log_append
from session_store import get_session, update_session
from tools.ask_calculation_engine import TOOL_DESCRIPTION, TOOL_NAME, TOOL_PARAMETERS
from tools.ask_calculation_engine import handle as handle_tool

MAX_TOOL_ROUNDS = 3

# Injected when the model produces a final answer without ever having
# called ask_calculation_engine in this exchange -- the system prompt
# already says to always call it, but that's a preference the model can
# ignore; this is the code-level enforcement, same philosophy as
# kyc-voice-agent's tools/submit_user_details.py refusing to trust the LLM
# alone for its locked-fields rule.
_NUDGE_MESSAGE = (
    "You answered without calling ask_calculation_engine. Every business-performance "
    "question MUST go through that tool -- you are not allowed to state, estimate, or "
    "infer any number yourself. Call ask_calculation_engine now for the user's question."
)
_FALLBACK_NARRATION = (
    "Arre, yeh wala main abhi verify nahi kar paayi — koi baat nahi! Aap ise thoda alag tarike se "
    "poochiye, ya kisi aur specific business metric ke baare mein poochiye, main zaroor madad karungi."
)


def _log_no_engine_call(session_id: str, question: str) -> str:
    entry = log_append(QueryLogEntry(
        session_id=session_id, question_text=question, status=STATUS_NO_ENGINE_CALL,
        error="LLM produced a final answer without ever calling ask_calculation_engine.",
    ))
    session = get_session(session_id)
    update_session(session_id, pending_log_ids=session.pending_log_ids + [entry.log_id])
    logger.warning(f"session_id={session_id}: LLM never called the tool for {question!r} -- logged as failure")
    return entry.log_id


async def ask(session_id: str, question: str) -> dict:
    provider = (os.getenv("LLM_PROVIDER") or "deepseek").lower()
    session = get_session(session_id)
    history = [{"role": t["role"], "text": t["text"]} for t in session.transcript]

    if provider in ("deepseek", "openai"):
        narration, log_ids = await _run_openai_compatible(provider, session_id, history, question)
    elif provider == "anthropic":
        narration, log_ids = await _run_anthropic(session_id, history, question)
    else:
        raise ValueError(f"Unknown LLM_PROVIDER: {provider}")

    now = time.time()
    update_session(
        session_id,
        transcript=session.transcript + [
            {"role": "user", "text": question, "at": now},
            {"role": "bot", "text": narration, "at": now},
        ],
    )
    grounding.finalize_turn(session_id, narration)

    return {"narration": narration, "log_ids": log_ids}


def _tool_openai_schema() -> dict:
    return {
        "type": "function",
        "function": {"name": TOOL_NAME, "description": TOOL_DESCRIPTION, "parameters": TOOL_PARAMETERS},
    }


async def _run_openai_compatible(provider: str, session_id: str, history: list[dict], question: str):
    from openai import AsyncOpenAI

    if provider == "deepseek":
        # Same optional CostGuard gateway routing as llm_factory.py: if
        # DEEPSEEK_API_KEY holds a cgmk_... monitoring key rather than a
        # real DeepSeek key, DEEPSEEK_BASE_URL points at the gateway and
        # DEEPSEEK_GATEWAY_PROVIDER_ID identifies which upstream provider
        # the gateway should forward to.
        client_kwargs = {
            "api_key": os.getenv("DEEPSEEK_API_KEY"),
            "base_url": os.getenv("DEEPSEEK_BASE_URL") or "https://api.deepseek.com",
        }
        gateway_provider_id = os.getenv("DEEPSEEK_GATEWAY_PROVIDER_ID")
        if gateway_provider_id:
            client_kwargs["default_headers"] = {"X-CostGuard-Provider": gateway_provider_id}
        client = AsyncOpenAI(**client_kwargs)
        model = os.getenv("LLM_MODEL") or "deepseek-chat"
    else:
        client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        model = os.getenv("LLM_MODEL") or "gpt-4o"

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for turn in history:
        messages.append({"role": "user" if turn["role"] == "user" else "assistant", "content": turn["text"]})
    messages.append({"role": "user", "content": question})

    log_ids: list[str] = []
    tool_called = False
    for round_i in range(MAX_TOOL_ROUNDS):
        resp = await client.chat.completions.create(
            model=model, messages=messages, tools=[_tool_openai_schema()],
        )
        msg = resp.choices[0].message

        if not msg.tool_calls:
            if tool_called:
                # It called the tool earlier this exchange and is now just
                # narrating -- trust it, grounding.finalize_turn checks the
                # narration against that trace afterward.
                return msg.content or "", log_ids
            if round_i < MAX_TOOL_ROUNDS - 1:
                messages.append({"role": "assistant", "content": msg.content})
                messages.append({"role": "user", "content": _NUDGE_MESSAGE})
                continue
            # Exhausted retries and it STILL never called the tool -- refuse
            # to return its ungrounded narration at all.
            return _FALLBACK_NARRATION, [_log_no_engine_call(session_id, question)]

        tool_called = True
        messages.append({"role": "assistant", "content": msg.content, "tool_calls": [
            {"id": tc.id, "type": "function",
             "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in msg.tool_calls
        ]})
        for tc in msg.tool_calls:
            args = json.loads(tc.function.arguments or "{}")
            result = await handle_tool(session_id, args)
            if "log_id" in result:
                log_ids.append(result["log_id"])
            messages.append({"role": "tool", "tool_call_id": tc.id, "content": json.dumps(result)})

    logger.warning(f"session_id={session_id}: hit MAX_TOOL_ROUNDS without a final narration")
    return "Arre, yeh sawaal thoda tricky nikla — main abhi jawab nahi de paayi. Ek aur baar poochiye?", log_ids


async def _run_anthropic(session_id: str, history: list[dict], question: str):
    from anthropic import AsyncAnthropic

    api_key = os.getenv("ANTHROPIC_API_KEY")
    # The anthropic SDK authenticates via the `x-api-key` header by default,
    # but CostGuard's gateway (ANTHROPIC_BASE_URL, when ANTHROPIC_API_KEY is
    # a cgmk_... monitoring key) only reads `Authorization: Bearer <key>` to
    # find the monitoring key (see extract_monitoring_key in the gateway's
    # own app/gateway/auth.py) -- without this, the gateway sees no
    # Authorization header at all and 401s with "Missing or malformed
    # Authorization header", even though a real Anthropic key would need
    # neither.
    client = AsyncAnthropic(api_key=api_key, default_headers={"Authorization": f"Bearer {api_key}"})
    model = os.getenv("LLM_MODEL") or "claude-sonnet-5"
    tool_schema = {"name": TOOL_NAME, "description": TOOL_DESCRIPTION, "input_schema": TOOL_PARAMETERS}

    messages = []
    for turn in history:
        messages.append({"role": "user" if turn["role"] == "user" else "assistant", "content": turn["text"]})
    messages.append({"role": "user", "content": question})

    log_ids: list[str] = []
    tool_called = False
    for round_i in range(MAX_TOOL_ROUNDS):
        resp = await client.messages.create(
            model=model, system=SYSTEM_PROMPT, messages=messages, tools=[tool_schema], max_tokens=1024,
        )
        tool_uses = [b for b in resp.content if b.type == "tool_use"]
        text_blocks = [b.text for b in resp.content if b.type == "text"]

        if not tool_uses:
            if tool_called:
                return "\n".join(text_blocks), log_ids
            if round_i < MAX_TOOL_ROUNDS - 1:
                messages.append({"role": "assistant", "content": resp.content})
                messages.append({"role": "user", "content": _NUDGE_MESSAGE})
                continue
            return _FALLBACK_NARRATION, [_log_no_engine_call(session_id, question)]

        tool_called = True
        messages.append({"role": "assistant", "content": resp.content})
        tool_results = []
        for tu in tool_uses:
            result = await handle_tool(session_id, tu.input)
            if "log_id" in result:
                log_ids.append(result["log_id"])
            tool_results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(result)})
        messages.append({"role": "user", "content": tool_results})

    logger.warning(f"session_id={session_id}: hit MAX_TOOL_ROUNDS without a final narration")
    return "Arre, yeh sawaal thoda tricky nikla — main abhi jawab nahi de paayi. Ek aur baar poochiye?", log_ids
