"""VoiceTurnObserver: captures the live transcript and accumulates bot
speech, flushing it (grounded + judge-scheduled) once a reply is done.

A pipecat BaseObserver, constructed once per call in bot.py and passed into
PipelineWorker(..., observers=[...]) -- sees every frame flowing through the
pipeline without being wired into the pipeline's own processor chain, so it
can't affect the actual conversation. Same capture pattern as
kyc-voice-agent's instrumentation.py's InstrumentationObserver (including the
same frame.id dedup -- BaseObserver.on_push_frame fires once per
processor-to-processor hop, not once per logical frame).

TWO real bugs were found testing this live, in order:

1. Finalizing on every BotStoppedSpeakingFrame is wrong. A single logical
   reply to one user question can span SEVERAL speak/stop cycles -- e.g. the
   model says a filler ("I'll check that for you right away.") BEFORE
   calling ask_calculation_engine, THEN speaks the real, numbers-bearing
   answer in a second cycle after the tool returns. Finalizing on the first
   stop grounds the filler instead of the real answer and never checks the
   actual numbers at all.

2. The fix for #1 was to flush on the pipeline's own on_user_turn_started
   event instead (bot.py calls VoiceTurnObserver.flush() there) -- correct
   for real spoken turns, where VAD reliably detects when the user starts
   talking again. But that event is driven by the SileroVADAnalyzer-based
   user aggregator, and does NOT fire for text-injected turns (e.g. a
   client's sendText() call used for testing without a microphone) -- so a
   flush mechanism that ONLY depends on it can silently never fire at all,
   leaving entries stuck at their optimistic default status forever.

The fix for both: a debounce timer is the PRIMARY mechanism -- every new
TTSTextFrame chunk reschedules a short delay; once no new chunk has arrived
for FLUSH_DEBOUNCE_SECONDS, the accumulated buffer (spanning however many
speak/stop cycles the reply took) is flushed. This works regardless of how
the turn was initiated (real speech, VAD, or injected text) because it
never depends on a turn-taking event firing at all. bot.py's
on_user_turn_started and on_client_disconnected also call flush() directly,
as a faster path when they DO fire -- flush() is idempotent on an empty
buffer, so having both is safe.

3. A THIRD bug, found from a real conversation's query log: the debounce
   above is only 2.5s, but the gap between the pre-tool-call filler's
   TTSTextFrame and the REAL answer's first TTSTextFrame chunk can be much
   longer than that. The first instinct was "the tool call must be slow" --
   it isn't (ask_calculation_engine's own latency_ms is consistently under
   half a second, confirmed by tracing FunctionCallsStartedFrame ->
   FunctionCallResultFrame directly: that round-trip finishes in ~0.4s).
   The real bottleneck is what happens AFTER the tool result is available:
   the LLM needs a SECOND completion call (context + tool result -> the
   actual narration), and that call alone can take 8-10+ seconds for a
   longer Hindi/Hinglish answer -- and it produces ZERO frames of any kind
   while it's thinking. So "no tool call in flight" is NOT the same as "the
   reply is done"; there's a long silent gap in between where the debounce
   would still fire on the filler alone if nothing suppressed it, grounding
   +logging just that placeholder against a trace that already had the real
   numbers. Then the real answer's TTSTextFrame chunks would arrive several
   seconds later and get flushed as their OWN turn -- but by then
   session.pending_log_ids had already been cleared by the first flush, so
   the real, numerically-correct answer the user actually heard was
   silently dropped from query_log entirely: never grounded, never judged,
   invisible in admin/query-log.

   Fixed by tracking "a function call happened this turn and its narration
   hasn't started yet" as its own state (_awaiting_post_tool_narration),
   independent of whether the call itself has resolved. The first attempt
   at this cleared the flag on the next TTSTextFrame chunk seen after a
   tool result -- but since the tool call resolves in under half a second,
   the FILLER's own trailing TTS chunks (still being synthesized/paced out)
   routinely arrive AFTER that result too, clearing the flag on nothing but
   more filler. The reliable signal turned out to be LLMFullResponseStartFrame:
   the LLM's post-tool-result narration is a SEPARATE completion call, and
   pipecat marks the start of every completion call with one of these --
   only a start-frame seen after a tool result really means "the real
   narration is beginning now", regardless of how the filler's own audio is
   still pacing out. No flush (debounce or direct) is allowed to fire while
   the flag is set, however long the LLM's second completion call takes to
   produce it.
"""
from __future__ import annotations

import asyncio
import time

from loguru import logger
from pipecat.frames.frames import (
    Frame, FunctionCallResultFrame, FunctionCallsStartedFrame, LLMFullResponseStartFrame,
    TranscriptionFrame, TTSTextFrame,
)
from pipecat.observers.base_observer import BaseObserver, FramePushed

import grounding
import transcripts
from session_store import get_session, update_session

FLUSH_DEBOUNCE_SECONDS = 2.5


class VoiceTurnObserver(BaseObserver):
    def __init__(self, session_id: str):
        super().__init__()
        self._session_id = session_id
        self._seen_frame_ids: set[int] = set()
        self._bot_text_buffer: list[str] = []
        self._flush_timer_task: asyncio.Task | None = None
        # True from the moment a function call starts until real narration
        # text is confirmed flowing again afterward -- see bug #3 above.
        # While True, no flush (debounce OR a direct call() from bot.py) is
        # allowed to fire, no matter how long the LLM's post-tool-call
        # completion takes to produce its first token.
        self._awaiting_post_tool_narration = False
        self._any_tool_result_seen = False

    def _first_time_seeing(self, frame: Frame) -> bool:
        if frame.id in self._seen_frame_ids:
            return False
        self._seen_frame_ids.add(frame.id)
        return True

    async def on_push_frame(self, data: FramePushed) -> None:
        frame = data.frame

        if isinstance(frame, TranscriptionFrame):
            if self._first_time_seeing(frame):
                self._append_transcript("user", frame.text)
        elif isinstance(frame, TTSTextFrame):
            if self._first_time_seeing(frame):
                self._append_transcript("bot", frame.text)
                self._bot_text_buffer.append(frame.text)
                if not self._awaiting_post_tool_narration:
                    self._reschedule_debounced_flush()
        elif isinstance(frame, FunctionCallsStartedFrame):
            if self._first_time_seeing(frame):
                self._awaiting_post_tool_narration = True
                self._any_tool_result_seen = False
                # Whatever's in the buffer so far is at most a pre-call
                # filler, never the final reply -- cancel outright so a
                # slow filler-TTS window can't let the timer creep in ahead
                # of the real answer.
                if self._flush_timer_task is not None and not self._flush_timer_task.done():
                    self._flush_timer_task.cancel()
        elif isinstance(frame, FunctionCallResultFrame):
            if self._first_time_seeing(frame):
                self._any_tool_result_seen = True
        elif isinstance(frame, LLMFullResponseStartFrame):
            if self._first_time_seeing(frame):
                # A new completion call starting once we've seen a tool
                # result is the LLM beginning the REAL post-tool narration
                # -- not just another trailing chunk of the filler's own
                # (separate, earlier) completion call, which can still be
                # mid-flight here since the tool itself resolves so fast.
                if self._awaiting_post_tool_narration and self._any_tool_result_seen:
                    self._awaiting_post_tool_narration = False

    def _reschedule_debounced_flush(self) -> None:
        if self._flush_timer_task is not None and not self._flush_timer_task.done():
            self._flush_timer_task.cancel()
        self._flush_timer_task = asyncio.create_task(self._debounced_flush())

    async def _debounced_flush(self) -> None:
        try:
            await asyncio.sleep(FLUSH_DEBOUNCE_SECONDS)
        except asyncio.CancelledError:
            return
        self.flush()

    def flush(self, *, force: bool = False) -> None:
        """Call once the bot's reply is done -- bot.py's on_user_turn_started
        (fast path when VAD fires it) and on_client_disconnected both call
        this directly; the debounce timer above is the guaranteed fallback.
        Idempotent: a no-op if nothing has accumulated since the last flush.

        These direct callers don't know about tool-call state, so this is
        the single place that guards against grounding a turn while still
        waiting on post-tool-call narration (bug #3 in the module
        docstring) -- not just the debounce path. Pass force=True only when
        there's no more chance to wait for the real answer
        (on_client_disconnected): capture whatever's there even if the
        narration never resumed, rather than lose it silently.
        """
        if self._flush_timer_task is not None and not self._flush_timer_task.done():
            self._flush_timer_task.cancel()
        if not force and self._awaiting_post_tool_narration:
            return
        if not self._bot_text_buffer:
            return
        narration = " ".join(self._bot_text_buffer).strip()
        self._bot_text_buffer = []
        self._awaiting_post_tool_narration = False
        self._any_tool_result_seen = False
        logger.debug(f"session_id={self._session_id}: flushing turn narration: {narration!r}")
        grounding.finalize_turn(self._session_id, narration)
        # Fire-and-forget, off-thread: persist_new_entries() does a blocking
        # Postgres write, which must never sit in this frame-processing hot
        # path (the exact kind of added latency that caused the choppy-audio
        # bug fixed earlier this session, just from a different source).
        asyncio.create_task(asyncio.to_thread(transcripts.persist_new_entries, self._session_id))

    def _append_transcript(self, role: str, text: str) -> None:
        if not text:
            return
        session = get_session(self._session_id)
        entry = {"role": role, "text": text, "at": time.time()}
        update_session(self._session_id, transcript=session.transcript + [entry])
