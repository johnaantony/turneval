"""A voice agent with exactly one job you can verify.

Run it::

    uv run python agents/booking_agent.py console      # talk from the terminal
    uv run python agents/booking_agent.py dev          # join a LiveKit room

The agent books an appointment. That is the whole product surface, and the
narrowness is deliberate: an agent that just chats gives the scorer nothing to
check, because "was that a good conversation" has no ground truth. One tool
with a durable side effect turns layer 1 from an opinion into a file read.

While the call runs, :class:`TraceRecorder` writes a JSONL trace that
``turneval.adapters.livekit`` reads back. Scoring never touches this process,
the SDK, or the network.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
    metrics,
)

load_dotenv()

logger = logging.getLogger("booking-agent")

ROOT = Path(__file__).resolve().parent.parent
TRACE_DIR = Path(os.getenv("TURNEVAL_TRACE_DIR", ROOT / "data" / "traces"))
BOOKING_DIR = Path(os.getenv("TURNEVAL_BOOKING_DIR", ROOT / "data" / "bookings"))

#: Scenario under test, set by the harness so the trace is self-identifying.
SCENARIO_ID = os.getenv("TURNEVAL_SCENARIO_ID")

#: Set by a fault-injection scenario to make the booking tool fail on purpose.
#: Scenario ``tool-failure`` needs a tool that genuinely errors, and faking the
#: failure inside the scorer instead would not exercise the agent's recovery.
FAULT = os.getenv("TURNEVAL_FAULT")

INSTRUCTIONS = """\
You are a receptionist for a dental office, taking appointment bookings by phone.

Collect three things: the date, the time, and the caller's name. Once you have
all three, call book_appointment. Confirm the booking back to the caller in one
short sentence.

Rules:
- Keep every reply to one or two sentences. You are on a phone call, not writing.
- Today is {today}. Resolve relative or yearless dates to the next future
  occurrence and never book a date in the past.
- Never guess a date or a time. If the caller is vague ("next Tuesday-ish"),
  ask one clarifying question and offer a specific date.
- If the caller changes their mind, use the latest values they gave you.
- If book_appointment returns an error, tell the caller plainly that you could
  not complete the booking and offer to take a callback number. Do not claim
  the appointment was made.
- If the caller wants something you cannot do, say so and offer a human.
"""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------
# The agent
# --------------------------------------------------------------------------


class BookingAgent(Agent):
    def __init__(self, session_id: str) -> None:
        today = datetime.now().strftime("%A, %B %d, %Y")
        super().__init__(instructions=INSTRUCTIONS.format(today=today))
        self._session_id = session_id
        self._recorder: TraceRecorder | None = None

    def bind(self, recorder: TraceRecorder) -> None:
        self._recorder = recorder

    @function_tool
    async def book_appointment(
        self,
        context: RunContext,
        date: str,
        time_of_day: str,
        name: str,
    ) -> str:
        """Book an appointment.

        Args:
            date: ISO date, YYYY-MM-DD.
            time_of_day: 24-hour time, HH:MM.
            name: The caller's name.
        """
        started = time.monotonic()
        record = {"date": date, "time": time_of_day, "name": name, "booked_at": _utc_now()}

        try:
            if FAULT == "tool_error":
                raise RuntimeError("scheduling backend unavailable")

            BOOKING_DIR.mkdir(parents=True, exist_ok=True)
            path = BOOKING_DIR / f"{self._session_id}.json"
            path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - the agent must see the failure
            if self._recorder:
                self._recorder.note_tool_call(
                    name="book_appointment",
                    arguments={"date": date, "time": time_of_day, "name": name},
                    ok=False,
                    duration=time.monotonic() - started,
                    error=str(exc),
                )
            # Returned, not raised, so the model gets a chance to recover. Whether
            # it actually recovers is what the no_recovery tag measures.
            return f"ERROR: could not book the appointment ({exc})"

        if self._recorder:
            self._recorder.note_tool_call(
                name="book_appointment",
                arguments={"date": date, "time": time_of_day, "name": name},
                ok=True,
                duration=time.monotonic() - started,
                result=record,
            )
        return f"Booked {date} at {time_of_day} for {name}."


# --------------------------------------------------------------------------
# Trace recording
# --------------------------------------------------------------------------


@dataclass
class _PendingTurn:
    """Metrics for one agent turn, accumulated until the turn is emitted.

    LiveKit reports EOU, LLM, and TTS timings as separate events keyed by
    ``speech_id``, so the four latency numbers arrive independently and have to
    be regrouped before they mean anything as a response delay.
    """

    eou: dict[str, Any] = field(default_factory=dict)
    llm: dict[str, Any] = field(default_factory=dict)
    tts: dict[str, Any] = field(default_factory=dict)
    audio_duration: float = 0.0


class TraceRecorder:
    """Subscribes to session events and writes the JSONL the adapter reads.

    Fidelity note: turn boundaries here are derived from
    ``conversation_item_added``, which fires when an item is committed rather
    than at the acoustic instant speech started. Latency numbers come from the
    SDK's own metrics and are exact; wall-clock turn timestamps are within a
    few hundred milliseconds. Nothing in scoring depends on the latter.
    """

    def __init__(self, session_id: str, scenario_id: str | None, path: Path) -> None:
        self.session_id = session_id
        self.scenario_id = scenario_id
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("w", encoding="utf-8")
        self._started = time.monotonic()
        self._last_activity = time.monotonic()

        self._pending: dict[str, _PendingTurn] = {}
        self._seen_user_items: set[str] = set()
        self._tool_calls: list[dict[str, Any]] = []
        self._interrupted_next = False
        self._talked_over_next = False
        self._backchannels = 0
        self._ended_reason: str | None = None

        self._write({
            "type": "session_start",
            "session_id": session_id,
            "scenario_id": scenario_id,
            "timestamp": _utc_now(),
        })

    # ---- public hooks ----

    def note_tool_call(
        self,
        name: str,
        arguments: dict[str, Any],
        ok: bool,
        duration: float,
        result: Any = None,
        error: str | None = None,
    ) -> None:
        self._tool_calls.append({
            "name": name,
            "arguments": arguments,
            "ok": ok,
            "duration": duration,
            "result": result,
            "error": error,
        })

    def attach(self, session: AgentSession) -> None:
        session.on("metrics_collected", self._on_metrics)
        session.on("user_input_transcribed", self._on_user_transcript)
        session.on("conversation_item_added", self._on_item)
        session.on("overlapping_speech", self._on_overlap)
        session.on("close", self._on_close)

    def close(self) -> None:
        if self._fh.closed:
            return
        self._write({
            "type": "session_end",
            "timestamp": _utc_now(),
            "duration": time.monotonic() - self._started,
            "ended_reason": self._ended_reason,
            "num_backchannels": self._backchannels,
            "escalated": False,
        })
        self._fh.close()

    # ---- event handlers ----

    def _on_metrics(self, event: Any) -> None:
        m = event.metrics
        speech_id = getattr(m, "speech_id", None) or "_"
        pending = self._pending.setdefault(speech_id, _PendingTurn())

        if isinstance(m, metrics.EOUMetrics):
            pending.eou = {
                "end_of_utterance_delay": m.end_of_utterance_delay,
                "transcription_delay": m.transcription_delay,
            }
        elif isinstance(m, metrics.LLMMetrics):
            pending.llm = {
                "ttft": m.ttft,
                "prompt_tokens": m.prompt_tokens,
                "completion_tokens": m.completion_tokens,
                "prompt_cached_tokens": getattr(m, "prompt_cached_tokens", 0),
            }
        elif isinstance(m, metrics.TTSMetrics):
            pending.tts = {"ttfb": m.ttfb}
            pending.audio_duration += m.audio_duration or 0.0
        elif isinstance(m, metrics.InterruptionMetrics):
            # Ships since SDK 1.7. Interruption counting used to be something
            # you derived yourself from overlapping speech.
            self._backchannels += getattr(m, "num_backchannels", 0) or 0

    def _on_user_transcript(self, event: Any) -> None:
        if not event.is_final:
            return
        # The SDK can emit more than one final transcript for the same chat item
        # (a formatted and a raw variant), which doubled every user turn on the
        # first real call. Keep the first final per item.
        item_id = getattr(event, "item_id", None)
        if item_id is not None:
            if item_id in self._seen_user_items:
                return
            self._seen_user_items.add(item_id)
        self._emit_turn(role="user", text=event.transcript, speech_id=None)

    def _on_item(self, event: Any) -> None:
        item = event.item
        role = getattr(item, "role", None)
        if role != "assistant":
            return  # user turns come from the transcription event, with timing
        text = getattr(item, "text_content", None) or ""
        self._emit_turn(role="agent", text=text, speech_id=getattr(item, "id", None))

    def _on_overlap(self, event: Any) -> None:
        if getattr(event, "is_interruption", False):
            self._interrupted_next = True
        # agent_ended False means the agent kept the floor through the overlap.
        if not getattr(event, "agent_ended", True):
            self._talked_over_next = True

    def _on_close(self, event: Any) -> None:
        self._ended_reason = getattr(event, "reason", None) or "closed"
        self.close()

    # ---- writing ----

    def _emit_turn(self, role: str, text: str, speech_id: str | None) -> None:
        now = time.monotonic()
        silence = now - self._last_activity
        self._last_activity = now

        pending = self._pending.pop(speech_id, None) if speech_id else None
        if pending is None and role == "agent" and self._pending:
            # conversation_item_added carries the chat item id while metrics are
            # keyed by speech_id, so a direct match normally misses. The metrics
            # for this turn arrived before the item did: take the oldest entry.
            # (Found on the first real call: every turn's latency and tokens
            # were silently dropped.)
            pending = self._pending.pop(next(iter(self._pending)))
        pending = pending or _PendingTurn()

        record: dict[str, Any] = {
            "type": "turn",
            "role": role,
            "text": text,
            "started_at": _utc_now(),
            "ended_at": _utc_now(),
            "silence_before": silence,
            "interrupted_by_user": self._interrupted_next,
            "talked_over_user": self._talked_over_next,
        }
        if role == "agent":
            record["eou"] = pending.eou
            record["llm"] = pending.llm
            record["tts"] = pending.tts
            record["audio_duration"] = pending.audio_duration
            record["tool_calls"] = self._tool_calls
            self._tool_calls = []

        self._interrupted_next = False
        self._talked_over_next = False
        self._write(record)

    def _write(self, record: dict[str, Any]) -> None:
        if self._fh.closed:
            return
        self._fh.write(json.dumps(record) + "\n")
        self._fh.flush()  # a hung-up call must still leave a readable trace


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------


async def entrypoint(ctx: JobContext) -> None:
    session_id = os.getenv("TURNEVAL_SESSION_ID") or f"sess_{uuid.uuid4().hex[:10]}"
    recorder = TraceRecorder(
        session_id=session_id,
        scenario_id=SCENARIO_ID,
        path=TRACE_DIR / f"{session_id}.jsonl",
    )

    await ctx.connect()

    # Model strings resolve through LiveKit Inference, their managed gateway,
    # so one set of credentials covers STT, LLM, and TTS. Swap any of these for
    # a plugin instance to use your own provider keys instead.
    session = AgentSession(
        stt=os.getenv("TURNEVAL_STT", "assemblyai/universal-streaming:en"),
        llm=os.getenv("TURNEVAL_LLM", "openai/gpt-4o-mini"),
        tts=os.getenv("TURNEVAL_TTS", "cartesia/sonic-2:9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"),
    )

    agent = BookingAgent(session_id=session_id)
    agent.bind(recorder)
    recorder.attach(session)

    logger.info("session %s starting (scenario=%s)", session_id, SCENARIO_ID)

    # session.start() returns once the session is LIVE, not when the call ends,
    # so closing the recorder in a finally here would freeze the trace at zero
    # turns. The recorder closes itself on the session's close event; atexit is
    # the backup for console mode, where the process can exit without one.
    atexit.register(recorder.close)
    await session.start(agent=agent, room=ctx.room)
    logger.info("session live; tracing to %s", recorder.path)


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
