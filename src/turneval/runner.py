"""Assembles scored sessions into a :class:`~turneval.schema.Run`.

This is the glue: read traces through an adapter, read the world the agent
changed, score all three layers, and roll everything up. It is the only module
that knows about the filesystem layout of a run.
"""

from __future__ import annotations

import json
import statistics
from datetime import datetime, timezone
from pathlib import Path

from turneval.adapters import get_adapter
from turneval.adapters.base import RawSession
from turneval.scenarios import Scenario, load_scenarios
from turneval.schema import (
    HARNESS_VERSION,
    BehaviorSummary,
    Domain,
    HumanOutcome,
    LatencyPercentiles,
    MetricLayers,
    OutcomeMatrix,
    Pricing,
    Run,
    RunConfig,
    RunSummary,
    SessionResult,
    Tokens,
    Verification,
)
from turneval.scoring.layers import (
    RESPONSE_BUDGET_MS,
    load_outcomes,
    score_behavior,
    score_deterministic,
    tag_failures,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_world(bookings_dir: Path, session_id: str) -> dict | None:
    """Read back the side effect the agent was supposed to produce.

    Absence is meaningful, not an error: it is how a failed booking and a
    correctly-declined booking are both represented.
    """
    path = bookings_dir / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _resolution_turn(session: RawSession) -> int | None:
    """Which agent turn first satisfied the task, counting agent turns only."""
    agent_turns = 0
    for turn in session.turns:
        if turn.role != "agent":
            continue
        agent_turns += 1
        if any(call.ok for call in turn.tool_calls):
            return agent_turns
    return None


def _session_cost(session: RawSession, pricing: Pricing | None) -> float | None:
    """Cost in the three currencies a voice call actually spends.

    Returns ``None`` when no pricing is configured, which disables every cost
    view downstream rather than showing a confident zero.
    """
    if pricing is None:
        return None

    total = 0.0
    have_any = False

    if pricing.llm_input_per_mtok is not None:
        total += session.tokens.input / 1_000_000 * pricing.llm_input_per_mtok
        have_any = True
    if pricing.llm_output_per_mtok is not None:
        total += session.tokens.output / 1_000_000 * pricing.llm_output_per_mtok
        have_any = True

    minutes = session.audio_seconds / 60.0
    if pricing.stt_per_audio_minute is not None:
        total += minutes * pricing.stt_per_audio_minute
        have_any = True
    if pricing.platform_per_session_minute is not None:
        total += (session.wall_clock_ms / 60_000.0) * pricing.platform_per_session_minute
        have_any = True

    # TTS is billed per character and the trace does not carry character counts,
    # so it is deliberately excluded rather than estimated. Cost figures here
    # are a floor, and SCHEMA.md says so.
    return total if have_any else None


def score_session(
    scenario: Scenario,
    raw: RawSession,
    bookings_dir: Path,
    outcome: HumanOutcome | None,
    pricing: Pricing | None,
) -> SessionResult:
    world = _read_world(bookings_dir, raw.provider_session_id)
    deterministic = score_deterministic(scenario, raw, world)
    behavior = score_behavior(raw, resolved_turn=_resolution_turn(raw))
    tags = tag_failures(raw, deterministic, behavior, outcome)

    return SessionResult(
        id=raw.provider_session_id,
        scenario_id=scenario.id,
        title=scenario.title,
        category=scenario.category,
        difficulty=scenario.difficulty,
        goal=scenario.goal,
        deterministic=deterministic,
        behavior=behavior,
        outcome=outcome,
        failure_tags=tags,
        turns=raw.turns,
        tokens=raw.tokens,
        cost=_session_cost(raw, pricing),
        wall_clock_ms=raw.wall_clock_ms,
        audio_seconds=raw.audio_seconds,
        recording_ref=raw.recording_ref,
        provider_session_id=raw.provider_session_id,
        adapter=raw.adapter,
    )


def build_run(
    sessions: list[SessionResult],
    config: RunConfig,
    run_id: str,
    started_at: str,
    finished_at: str,
) -> Run:
    return Run(
        run_id=run_id,
        started_at=started_at,
        finished_at=finished_at,
        config=config,
        summary=summarize(sessions),
        sessions=sessions,
        domain=Domain.VOICE,
    )


def summarize(sessions: list[SessionResult]) -> RunSummary:
    total = len(sessions)
    passed = [s for s in sessions if s.passed]
    n_passed = len(passed)

    tokens = Tokens()
    cost_parts = [s.cost for s in sessions if s.cost is not None]
    for s in sessions:
        tokens = tokens + s.tokens

    failure_distribution: dict[str, int] = {}
    for s in sessions:
        for tag in s.failure_tags:
            failure_distribution[tag.tag.value] = failure_distribution.get(tag.tag.value, 0) + 1

    by_category: dict[str, dict[str, float]] = {}
    for s in sessions:
        cell = by_category.setdefault(s.category.value, {"total": 0, "passed": 0, "rate": 0.0})
        cell["total"] += 1
        cell["passed"] += int(s.passed)
        cell["rate"] = cell["passed"] / cell["total"]

    wasted_tokens = sum(s.tokens.total for s in sessions if not s.passed)
    wasted_cost_parts = [s.cost for s in sessions if not s.passed and s.cost is not None]
    total_cost = sum(cost_parts) if cost_parts else None

    return RunSummary(
        total_sessions=total,
        passed=n_passed,
        pass_rate=(n_passed / total) if total else 0.0,
        by_category=by_category,
        failure_distribution=failure_distribution,
        tokens=tokens,
        cost=total_cost,
        audio_minutes=sum(s.audio_seconds for s in sessions) / 60.0,
        total_wall_clock_ms=sum(s.wall_clock_ms for s in sessions),
        latency=_latency(sessions),
        metric_layers=MetricLayers(
            behavior=_behavior_summary(sessions),
            outcome=_outcome_matrix(sessions),
        ),
        tokens_per_success=(tokens.total / n_passed) if n_passed else None,
        cost_per_success=(total_cost / n_passed) if (total_cost is not None and n_passed) else None,
        wasted_tokens=wasted_tokens,
        wasted_cost=sum(wasted_cost_parts) if wasted_cost_parts else None,
    )


def _latency(sessions: list[SessionResult]) -> LatencyPercentiles:
    """Response delay across every agent turn in the run.

    Reported as a distribution because voice is judged at the tail: a mean
    inside budget with a p95 at three seconds is a bad agent that looks fine.
    """
    delays = [
        turn.latency.response_delay_ms
        for s in sessions
        for turn in s.turns
        if turn.latency and turn.latency.response_delay_ms is not None
    ]
    if not delays:
        return LatencyPercentiles()

    ordered = sorted(delays)

    def pct(p: float) -> float:
        # Nearest-rank. With the handful of turns a run like this produces,
        # interpolation would invent precision the sample size cannot support.
        k = max(0, min(len(ordered) - 1, int(round(p * len(ordered) + 0.5)) - 1))
        return ordered[k]

    within = sum(1 for d in delays if d <= RESPONSE_BUDGET_MS)
    return LatencyPercentiles(
        p50_ms=statistics.median(ordered),
        p90_ms=pct(0.90),
        p95_ms=pct(0.95),
        worst_ms=ordered[-1],
        within_budget_rate=within / len(delays),
    )


def _behavior_summary(sessions: list[SessionResult]) -> BehaviorSummary | None:
    if not sessions:
        return None
    n = len(sessions)
    resolutions = [
        s.behavior.turns_to_resolution
        for s in sessions
        if s.behavior.turns_to_resolution is not None
    ]
    return BehaviorSummary(
        sessions_with_data=n,
        avg_turns=sum(s.behavior.turns for s in sessions) / n,
        avg_turns_to_resolution=(sum(resolutions) / len(resolutions)) if resolutions else None,
        interruption_rate=sum(1 for s in sessions if s.behavior.user_interruptions) / n,
        overlap_rate=sum(1 for s in sessions if s.behavior.agent_overlaps) / n,
        dead_air_rate=sum(1 for s in sessions if s.behavior.dead_air_events) / n,
        one_turn_resolution_rate=sum(1 for s in sessions if s.behavior.resolved_in_one_turn) / n,
    )


def _outcome_matrix(sessions: list[SessionResult]) -> OutcomeMatrix | None:
    """The cross-layer matrix, over ratified sessions only.

    Sessions with no recorded human outcome are excluded rather than counted as
    accepted. Assuming success for unreviewed calls is exactly how an
    abandonment rate gets quietly rounded to zero.
    """
    rated = [s for s in sessions if s.outcome is not None]
    if not rated:
        return None

    matrix = OutcomeMatrix(sessions_with_outcome=len(rated))
    for s in rated:
        assert s.outcome is not None
        if s.passed and s.outcome.accepted:
            matrix.passed_accepted += 1
        elif s.passed and not s.outcome.accepted:
            matrix.passed_but_abandoned += 1
        elif not s.passed and s.outcome.accepted:
            matrix.failed_accepted += 1
        else:
            matrix.failed_rejected += 1
    return matrix


def run_from_disk(
    traces: Path,
    scenarios_file: Path,
    bookings_dir: Path,
    outcomes_file: Path,
    label: str,
    adapter_name: str = "livekit",
    pricing: Pricing | None = None,
) -> Run:
    """Score every trace in ``traces`` and return the assembled run."""
    scenarios = {s.id: s for s in load_scenarios(scenarios_file)}
    outcomes = load_outcomes(outcomes_file)
    adapter = get_adapter(adapter_name)
    raw_sessions = adapter.load(traces)

    scored: list[SessionResult] = []
    skipped: list[str] = []
    for raw in raw_sessions:
        scenario = scenarios.get(raw.scenario_id or "")
        if scenario is None:
            skipped.append(raw.provider_session_id)
            continue
        scored.append(
            score_session(
                scenario=scenario,
                raw=raw,
                bookings_dir=bookings_dir,
                outcome=outcomes.get(raw.provider_session_id),
                pricing=pricing,
            )
        )

    started = min((s.started_at for s in raw_sessions if s.started_at), default=_utc_now())
    finished = max((s.ended_at for s in raw_sessions if s.ended_at), default=_utc_now())

    config = RunConfig(
        label=label,
        adapter=adapter_name,
        pricing=pricing,
        harness_version=HARNESS_VERSION,
        verification=Verification.CONFIRMED,
        notes={"skipped_unmatched_traces": skipped} if skipped else {},
    )

    return build_run(
        sessions=scored,
        config=config,
        run_id=f"{label}-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}",
        started_at=started,
        finished_at=finished,
    )
