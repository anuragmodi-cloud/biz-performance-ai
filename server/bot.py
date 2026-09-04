#
# Copyright (c) 2024-2025, Daily
#
# SPDX-License-Identifier: BSD 2-Clause License
#

"""biz-performance-voice-agent - Pipecat Voice Agent (Phase 2)

Cascade pipeline: Speech-to-Text -> LLM -> Text-to-Speech. Same shape as
kyc-voice-agent's bot.py -- Sarvam STT/TTS in Hindi/Hinglish, llm_factory.py's
provider switch, SmallWebRTC transport -- with the ONE
ask_calculation_engine tool wired in instead of KYC's document tools, and
VoiceTurnObserver in place of InstrumentationObserver (transcript capture +
grounding/judge triggering instead of TTFB/TTFA/interruption metrics).

Run via server.py's /api/offer route (uvicorn server:app), not directly.
"""

import os
import time

from dotenv import load_dotenv
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.frames.frames import EndFrame, LLMRunFrame, TTSSpeakFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import (
    LLMContextAggregatorPair,
    LLMUserAggregatorParams,
)
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.sarvam.stt import SarvamSTTService
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

import pipeline_registry
from llm_factory import build_llm
from prompts.system_prompt_hi import SYSTEM_PROMPT
from session_store import get_session, update_session
from tools.ask_calculation_engine import build_pipecat_tool
from voice_observer import VoiceTurnObserver

load_dotenv(override=True)

# Same generous default as kyc-voice-agent -- a voice call has real thinking
# pauses; too jumpy a timeout interrupts normal silence, not just genuine
# audio problems.
USER_IDLE_TIMEOUT_SECS = float(os.getenv("USER_IDLE_TIMEOUT_SECS") or 12.0)
MAX_CONSECUTIVE_IDLE_CHECKS = 2

# How recent a /session/{id}/typing ping has to be for silence to still
# count as "user is composing a question in the text box", not "call went
# quiet". Comfortably above the client's ~3s ping throttle so a couple of
# missed/delayed pings don't false-trigger a check-in mid-sentence.
TYPING_GRACE_SECS = 8.0

NO_AUDIO_CLOSING_LINE = (
    "Mujhe lagta hai aapki awaaz mujhtak nahi pahunch rahi. Kripya apna microphone "
    "check karke thodi der baad dobara call karein. Dhanyawad."
)


async def run_bot(transport: BaseTransport, runner_args: RunnerArguments) -> None:
    logger.info("Starting bot")

    # Speech-to-Text -- `os.getenv(key, default)` only falls back to `default`
    # when the key is ABSENT, not present-but-empty (as with SARVAM_MODEL=
    # and nothing after the `=`) -- `or` catches that too.
    stt = SarvamSTTService(
        api_key=os.getenv("SARVAM_API_KEY"),
        settings=SarvamSTTService.Settings(
            model=os.getenv("SARVAM_MODEL") or "saaras:v3",
            language=Language.HI_IN,
        ),
    )

    # Text-to-Speech. "priya" on bulbul:v3 is the same voice/pace kyc-voice-agent
    # confirmed via a listening test -- warm, unhurried delivery (pace 0.85,
    # slightly under natural-speed default) reads as calmer/friendlier than
    # 1.0, not sluggish. enable_preprocessing normalizes numbers/currency
    # (₹ amounts, lakh/crore) into speakable form before synthesis.
    tts = SarvamTTSService(
        api_key=os.getenv("SARVAM_API_KEY"),
        settings=SarvamTTSService.Settings(
            model=os.getenv("SARVAM_TTS_MODEL") or "bulbul:v3",
            voice=os.getenv("SARVAM_VOICE_ID") or "priya",
            pace=float(os.getenv("SARVAM_TTS_PACE") or 0.85),
            enable_preprocessing=True,
            language=Language.HI_IN,
        ),
    )

    llm = build_llm()

    session_id = runner_args.session_id or "dev-session"
    ask_calculation_engine = build_pipecat_tool(session_id)

    context = LLMContext(
        messages=[{"role": "system", "content": SYSTEM_PROMPT}],
        tools=ToolsSchema(standard_tools=[ask_calculation_engine]),
    )
    user_aggregator, assistant_aggregator = LLMContextAggregatorPair(
        context,
        user_params=LLMUserAggregatorParams(
            vad_analyzer=SileroVADAnalyzer(),
            user_idle_timeout=USER_IDLE_TIMEOUT_SECS,
            # Pipecat's default end-of-turn detector is a local ONNX model
            # (LocalSmartTurnAnalyzerV3) run in-process on every frame -- real
            # CPU competition against STT/LLM/TTS/audio-codec work on Render's
            # free tier, which was causing audio to stutter roughly every few
            # seconds. Silero VAD (already configured above) is lightweight
            # and this swaps end-of-turn detection to a plain VAD-plus-timer
            # strategy instead of a second local model.
            user_turn_strategies=UserTurnStrategies(stop=[SpeechTimeoutUserTurnStopStrategy()]),
        ),
    )

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            user_aggregator,
            llm,
            tts,
            transport.output(),
            assistant_aggregator,
        ]
    )

    voice_turn_observer = VoiceTurnObserver(session_id)
    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        observers=[voice_turn_observer],
    )

    pipeline_registry.register_task(session_id, worker, context)

    # Same idempotency guard as kyc-voice-agent: RTVIProcessor's on_client_ready
    # has no guard of its own and can genuinely fire more than once for one
    # connection (e.g. a brief WebRTC/data-channel retry) -- without this,
    # each extra firing re-queues the greeting.
    conversation_started = False

    @worker.rtvi.event_handler("on_client_ready")
    async def on_client_ready(rtvi):
        nonlocal conversation_started
        if conversation_started:
            logger.info(f"session_id={session_id}: on_client_ready fired again -- ignoring")
            return
        conversation_started = True

        context.add_message(
            {
                "role": "developer",
                "content": (
                    "Start the conversation now: greet the user warmly and briefly "
                    "in ONE natural line, then in that same turn ask what they'd "
                    "like to know about their business today -- don't stop at just "
                    "the greeting and wait silently for the user to speak first."
                ),
            }
        )
        await worker.queue_frames([LLMRunFrame()])

    idle_check_count = 0

    @user_aggregator.event_handler("on_user_turn_started")
    async def on_user_turn_started(aggregator, strategy=None):
        nonlocal idle_check_count
        idle_check_count = 0
        # The user only starts a new turn once the bot's ENTIRE reply to the
        # previous one is done, however many speak/stop cycles that reply
        # took (filler + tool call + real answer, etc.) -- see
        # voice_observer.py's docstring for why this is the right boundary
        # and BotStoppedSpeakingFrame alone is not.
        voice_turn_observer.flush()

    @user_aggregator.event_handler("on_user_turn_idle")
    async def on_user_turn_idle(aggregator):
        nonlocal idle_check_count

        session = get_session(session_id)
        typing_at = session.last_typing_at
        if typing_at is not None and (time.time() - typing_at) < TYPING_GRACE_SECS:
            # User is composing a question in the text box, not silent --
            # don't burn the idle-check budget or talk over them. Reset so
            # they get the full budget once they actually go quiet.
            idle_check_count = 0
            logger.debug(f"session_id={session_id}: idle timeout fired but user is typing -- skipping check-in")
            return

        idle_check_count += 1
        logger.info(
            f"session_id={session_id}: user idle for {USER_IDLE_TIMEOUT_SECS:.0f}s "
            f"(check-in {idle_check_count}/{MAX_CONSECUTIVE_IDLE_CHECKS})"
        )

        if idle_check_count > MAX_CONSECUTIVE_IDLE_CHECKS:
            await worker.queue_frames(
                [TTSSpeakFrame(NO_AUDIO_CLOSING_LINE), EndFrame(reason="user_idle_no_response")]
            )
            return

        context.add_message(
            {
                "role": "developer",
                "content": (
                    "The user hasn't said anything since your last turn. Check in "
                    "with them in ONE short, warm line -- ask if they're still there "
                    "or if there's a mic problem. Don't repeat your introduction, "
                    "just this one check-in."
                ),
            }
        )
        await worker.queue_frames([LLMRunFrame()])

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected")
        update_session(session_id, connected_at=time.time(), disconnected_at=None)

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        # Flush any trailing bot speech that never got a following user turn
        # to trigger the flush above (e.g. the call ended right after the
        # bot's answer, or mid-reply). force=True: the call is over, so
        # capture whatever's buffered even if a tool call never resolved --
        # there's no more chance to wait for the real answer.
        voice_turn_observer.flush(force=True)
        update_session(session_id, disconnected_at=time.time())
        pipeline_registry.unregister_task(session_id)
        await worker.cancel()

    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    await runner.run()


async def bot(runner_args: RunnerArguments):
    """Main bot entry point (called by server.py's /api/offer route)."""
    transport_params = {
        "webrtc": lambda: TransportParams(audio_in_enabled=True, audio_out_enabled=True),
    }
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport, runner_args)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
