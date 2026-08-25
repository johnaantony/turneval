"""The three scoring layers.

    Layer 1  deterministic   did the world change the way it was supposed to
    Layer 2  behavioral      what shape was the trajectory that got there
    Layer 3  human           did the caller actually get what they came for

Layers 1 and 2 are pure functions of the trace and the world. No model is
called anywhere in this module. That is the point: the moment your grader is
the same kind of thing you are grading, your numbers stop meaning anything.

Layer 3 cannot be computed. It is *recorded*, by a person, sampled, and read
from ``outcomes.json``. A session with no recorded outcome reports ``None``
rather than defaulting to accepted, because assuming success for unreviewed
calls is exactly how an abandonment rate gets quietly rounded to zero.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from turneval.adapters.base import RawSession
from turneval.scenarios import Scenario
from turneval.schema import (
    AssignedBy,
    BehaviorStats,
    DeterministicResult,
    HumanOutcome,
    OutcomeCheck,
    TaggedFailure,
)
from turneval.taxonomy import FailureTag

#: A turn slower than this reads as "did it hang up?" to a human caller.
#: Humans turn-take at roughly 200ms gaps in natural conversation, so this is
#: already generous. It is a constant rather than a config knob on purpose:
#: a budget you can tune per run is a budget you will tune until you pass.
RESPONSE_BUDGET_MS = 1500.0

#: Silence with neither party speaking, past which the call has stalled.
DEAD_AIR_MS = 3000.0


# --------------------------------------------------------------------------
# Layer 1
# --------------------------------------------------------------------------


def score_deterministic(
    scenario: Scenario,
    session: RawSession,
    world_state: dict[str, Any] | None,
) -> DeterministicResult:
    """Compare the world after the call against what the scenario expected.

    ``world_state`` is whatever the task tool wrote, read back off disk. For
    the booking agent that is the record in ``bookings.json`` keyed by this
    session, or ``None`` if the agent never wrote one.
    """
    checks: list[OutcomeCheck] = []

    if scenario.expect is None:
        # Scenarios where writing nothing is the correct behavior, e.g. the
        # caller went off topic and never actually asked to book.
        wrote_nothing = world_state is None
        checks.append(
            OutcomeCheck(
                name="no_record_written",
                ok=wrote_nothing,
                expected=None,
                actual=world_state,
                detail="agent should not have booked anything",
            )
        )
        return DeterministicResult(passed=wrote_nothing, checks=checks)

    if world_state is None:
        checks.append(
            OutcomeCheck(
                name="record_written",
                ok=False,
                expected=scenario.expect,
                actual=None,
                detail="agent never completed the booking",
            )
        )
        return DeterministicResult(passed=False, checks=checks)

    checks.append(
        OutcomeCheck(name="record_written", ok=True, expected=scenario.expect, actual=world_state)
    )
    for key, expected in scenario.expect.items():
        actual = world_state.get(key)
        checks.append(
            OutcomeCheck(
                name=f"field:{key}",
                ok=_matches(expected, actual),
                expected=expected,
                actual=actual,
            )
        )

    return DeterministicResult(passed=all(c.ok for c in checks), checks=checks)


def _matches(expected: Any, actual: Any) -> bool:
    """Exact match, with strings compared case- and whitespace-insensitively.

    Deliberately strict beyond that. Fuzzy field matching is how a booking for
    the wrong Tuesday quietly counts as a pass.
    """
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.strip().casefold() == actual.strip().casefold()
    return expected == actual


# --------------------------------------------------------------------------
# Layer 2
# --------------------------------------------------------------------------


def score_behavior(session: RawSession, resolved_turn: int | None = None) -> BehaviorStats:
    """Count the shape of the call. Every value comes from the trace."""
    turns = session.turns
    agent_turns = [t for t in turns if t.role == "agent"]

    dead_air = [
        t.silence_before_ms
        for t in turns
        if t.silence_before_ms is not None and t.silence_before_ms > DEAD_AIR_MS
    ]
    tool_errors = sum(1 for t in turns for c in t.tool_calls if not c.ok)

    return BehaviorStats(
        turns=len(turns),
        turns_to_resolution=resolved_turn,
        user_interruptions=sum(1 for t in turns if t.interrupted_by_user),
        agent_overlaps=sum(1 for t in turns if t.talked_over_user),
        backchannels=int(session.raw.get("footer", {}).get("num_backchannels") or 0),
        reprompts=_count_reprompts(agent_turns),
        dead_air_events=len(dead_air),
        max_dead_air_ms=max(dead_air) if dead_air else None,
        tool_errors=tool_errors,
        escalated_to_human=bool(session.raw.get("footer", {}).get("escalated")),
        resolved_in_one_turn=resolved_turn == 1,
    )


def _count_reprompts(agent_turns: list[Any]) -> int:
    """Agent turns that repeat a question it has already asked.

    Normalized exact repetition only. A paraphrase detector here would need a
    model, and a model in layer 2 is a model in the verdict path.
    """
    seen: set[str] = set()
    repeats = 0
    for turn in agent_turns:
        text = " ".join(turn.text.split()).casefold()
        if not text.endswith("?"):
            continue
        if text in seen:
            repeats += 1
        seen.add(text)
    return repeats


# --------------------------------------------------------------------------
# Layer 3
# --------------------------------------------------------------------------


def load_outcomes(path: str | Path) -> dict[str, HumanOutcome]:
    """Read human ratifications. Missing file means no outcomes recorded yet."""
    p = Path(path)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8"))
    return {
        session_id: HumanOutcome(
            accepted=bool(entry["accepted"]),
            caller_hung_up_mid_turn=bool(entry.get("caller_hung_up_mid_turn")),
            called_back_within_minutes=entry.get("called_back_within_minutes"),
            escalated_to_human=bool(entry.get("escalated_to_human")),
            note=entry.get("note"),
            recorded_at=entry.get("recorded_at"),
        )
        for session_id, entry in raw.items()
    }


# --------------------------------------------------------------------------
# Tagging
# --------------------------------------------------------------------------


def tag_failures(
    session: RawSession,
    deterministic: DeterministicResult,
    behavior: BehaviorStats,
    outcome: HumanOutcome | None,
) -> list[TaggedFailure]:
    """Assign failure tags from the trace. Code only, fixed taxonomy only."""
    tags: list[TaggedFailure] = []

    def add(tag: FailureTag, why: str, by: AssignedBy = AssignedBy.CODE) -> None:
        tags.append(TaggedFailure(tag=tag, justification=why, assigned_by=by))

    if not deterministic.passed:
        failed = [c for c in deterministic.checks if not c.ok]
        wrote_nothing = any(c.name == "record_written" and not c.ok for c in failed)
        if behavior.tool_errors:
            add(
                FailureTag.TOOL_CALL_FAILED,
                f"{behavior.tool_errors} tool call(s) returned an error",
            )
        elif wrote_nothing:
            add(FailureTag.TOOL_CALL_FAILED, "the task tool was never called successfully")
        else:
            bad = ", ".join(c.name for c in failed if c.name.startswith("field:"))
            if bad:
                add(FailureTag.WRONG_ARGUMENTS, f"booked with mismatched fields: {bad}")

    slow = [
        t
        for t in session.turns
        if t.latency
        and t.latency.response_delay_ms is not None
        and t.latency.response_delay_ms > RESPONSE_BUDGET_MS
    ]
    if slow:
        worst = max(t.latency.response_delay_ms for t in slow if t.latency)
        add(
            FailureTag.LATENCY_BUDGET_EXCEEDED,
            f"{len(slow)} turn(s) over {RESPONSE_BUDGET_MS:.0f}ms, worst {worst:.0f}ms",
        )

    if behavior.dead_air_events:
        add(
            FailureTag.DEAD_AIR,
            f"{behavior.dead_air_events} silence(s) over {DEAD_AIR_MS:.0f}ms",
        )

    if behavior.agent_overlaps:
        add(FailureTag.TALKED_OVER_USER, f"agent overlapped the caller {behavior.agent_overlaps}x")

    if behavior.reprompts:
        add(FailureTag.LOOPED, f"agent re-asked {behavior.reprompts} answered question(s)")

    # Layer 3. The tag that only a human pass can produce.
    if outcome is not None and deterministic.passed and not outcome.accepted:
        why = outcome.note or "caller did not accept a call the checks scored as a success"
        add(FailureTag.ABANDONED_BY_CALLER, why, by=AssignedBy.HUMAN)

    return tags
