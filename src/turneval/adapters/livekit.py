"""LiveKit adapter.

Reads the JSONL trace that ``agents/booking_agent.py`` writes during a call and
normalizes it into :class:`~turneval.adapters.base.RawSession`.

Field names below were verified against the installed SDK rather than the docs::

    uv run python -c "from livekit.agents import metrics; print(metrics.EOUMetrics.model_fields.keys())"

against **livekit-agents 1.7.0**. Two things had already moved by then and are
worth knowing if you are reading an older writeup:

* ``EOUMetrics`` carries ``speech_id``, not ``sequence_id``, and adds
  ``on_user_turn_completed_delay``.
* ``InterruptionMetrics`` now exists and ships ``num_interruptions``,
  ``num_backchannels``, and ``detection_delay``. Interruption counting used to
  be something you had to derive yourself.

This module deliberately does not import ``livekit`` at all. It reads the trace
file the agent produced, so scoring runs on a machine with no SDK, no
credentials, and no network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from turneval.adapters.base import RawSession, register
from turneval.schema import Tokens, ToolCall, Turn, TurnLatency


def _ms(seconds: Any) -> float | None:
    """LiveKit reports durations in seconds as floats. The schema is in ms."""
    if seconds is None:
        return None
    try:
        return float(seconds) * 1000.0
    except (TypeError, ValueError):
        return None


def _latency_from(event: dict[str, Any]) -> TurnLatency:
    """Fold the per-turn metric events into the four-number breakdown.

    The agent groups metric events by ``speech_id`` before writing them, so one
    trace record already holds the EOU, LLM, and TTS numbers for a single turn.
    """
    eou = event.get("eou") or {}
    llm = event.get("llm") or {}
    tts = event.get("tts") or {}
    return TurnLatency(
        end_of_utterance_delay_ms=_ms(eou.get("end_of_utterance_delay")),
        transcription_delay_ms=_ms(eou.get("transcription_delay")),
        llm_ttft_ms=_ms(llm.get("ttft")),
        tts_ttfb_ms=_ms(tts.get("ttfb")),
    )


def _tokens_from(event: dict[str, Any]) -> Tokens:
    llm = event.get("llm") or {}
    return Tokens(
        input=int(llm.get("prompt_tokens") or 0),
        output=int(llm.get("completion_tokens") or 0),
        cached_input=int(llm.get("prompt_cached_tokens") or 0),
    )


def _tool_calls_from(event: dict[str, Any]) -> list[ToolCall]:
    out: list[ToolCall] = []
    for call in event.get("tool_calls") or []:
        out.append(
            ToolCall(
                name=call.get("name", ""),
                arguments=call.get("arguments") or {},
                ok=bool(call.get("ok")),
                duration_ms=_ms(call.get("duration")),
                error=call.get("error"),
                result=call.get("result"),
            )
        )
    return out


class LiveKitAdapter:
    name = "livekit"

    def load(self, source: str | Path) -> list[RawSession]:
        path = Path(source)
        files = sorted(path.glob("*.jsonl")) if path.is_dir() else [path]
        return [s for f in files for s in self._load_file(f)]

    def _load_file(self, path: Path) -> list[RawSession]:
        records = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if not records:
            return []

        header = next((r for r in records if r.get("type") == "session_start"), {})
        footer = next((r for r in records if r.get("type") == "session_end"), {})
        turn_events = [r for r in records if r.get("type") == "turn"]

        turns: list[Turn] = []
        tokens = Tokens()
        audio_seconds = 0.0

        for i, event in enumerate(turn_events):
            turn_tokens = _tokens_from(event)
            tokens = tokens + turn_tokens
            audio = float(event.get("audio_duration") or 0.0)
            audio_seconds += audio

            turns.append(
                Turn(
                    index=i,
                    role=event.get("role", "agent"),
                    text=event.get("text", ""),
                    started_at=event.get("started_at"),
                    ended_at=event.get("ended_at"),
                    latency=_latency_from(event),
                    tool_calls=_tool_calls_from(event),
                    tokens=turn_tokens,
                    audio_seconds=audio or None,
                    interrupted_by_user=bool(event.get("interrupted_by_user")),
                    talked_over_user=bool(event.get("talked_over_user")),
                    silence_before_ms=_ms(event.get("silence_before")),
                )
            )

        return [
            RawSession(
                provider_session_id=header.get("session_id") or path.stem,
                adapter=self.name,
                started_at=header.get("timestamp", ""),
                ended_at=footer.get("timestamp", ""),
                turns=turns,
                tokens=tokens,
                audio_seconds=audio_seconds,
                wall_clock_ms=_ms(footer.get("duration")) or 0.0,
                recording_ref=footer.get("recording_ref"),
                scenario_id=header.get("scenario_id"),
                ended_reason=footer.get("ended_reason"),
                raw={"header": header, "footer": footer, "source_file": str(path)},
            )
        ]


register(LiveKitAdapter())
