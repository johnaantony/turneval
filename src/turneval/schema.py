"""The shared evaluation contract.

A run file (``results/<run-id>.json``) is the single artifact this project
produces. Everything downstream reads it: the scorer writes it, a dashboard
renders it, and :mod:`turneval.export.forwardeval` projects it into the shape
ForwardEval's dashboard already understands.

Why a separate schema instead of reusing ForwardEval's ``RunResult`` directly:
a voice session is not a coding task, and pretending otherwise would mean
stuffing placeholder strings into ``entry_file`` and ``test_command`` forever.
What the two domains genuinely share is the *envelope*, not the payload:

    run -> config + summary + N items
    item -> deterministic verdict, behavioral counters, human acceptance
    summary -> the cross-layer matrix, tokens, and outcome-per-token

That envelope is what lives here, tagged with a ``domain`` discriminator and
carrying a domain-specific block. The projection to ForwardEval is then a real
mapping with one honest correspondence at its center:

    passed-but-rejected (coding)  ==  completed-but-abandoned (voice)

Both mean: the system recorded success and the human threw the work away.

See SCHEMA.md for the full field-by-field mapping.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from turneval.taxonomy import Difficulty, FailureTag, ScenarioCategory

SCHEMA_VERSION = 1
HARNESS_VERSION = "0.1.0"


class Domain(StrEnum):
    """Which kind of agent produced this run."""

    VOICE = "voice"
    CODING = "coding"


class Verification(StrEnum):
    """Whether the deterministic checks actually executed.

    ``UNCONFIRMED`` exists so a run from a degraded environment can still be
    published without silently claiming an authority it does not have.
    """

    CONFIRMED = "confirmed"
    UNCONFIRMED = "unconfirmed"


class AssignedBy(StrEnum):
    CODE = "code"
    MODEL = "model"
    HUMAN = "human"


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Tokens:
    input: int = 0
    output: int = 0
    cached_input: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output

    def __add__(self, other: Tokens) -> Tokens:
        return Tokens(
            input=self.input + other.input,
            output=self.output + other.output,
            cached_input=self.cached_input + other.cached_input,
        )


@dataclass(slots=True)
class Pricing:
    """USD rates. ``None`` anywhere disables the corresponding cost view.

    Voice costs money in three currencies at once: LLM tokens, transcribed
    audio, and synthesized characters. Reporting only token cost understates a
    voice run badly, which is the whole reason cost-per-successful-call is
    worth computing separately from cost-per-token.
    """

    llm_input_per_mtok: float | None = None
    llm_output_per_mtok: float | None = None
    stt_per_audio_minute: float | None = None
    tts_per_mchar: float | None = None
    platform_per_session_minute: float | None = None


@dataclass(slots=True)
class TaggedFailure:
    tag: FailureTag
    justification: str
    assigned_by: AssignedBy = AssignedBy.CODE


# --------------------------------------------------------------------------
# Turn-level detail (the voice payload)
# --------------------------------------------------------------------------


@dataclass(slots=True)
class TurnLatency:
    """The four numbers that sum to perceived response delay.

    Field names mirror ``livekit.agents.metrics`` as of SDK 1.7.0 so the
    adapter is a rename, not a reinterpretation. Every value is milliseconds.
    """

    end_of_utterance_delay_ms: float | None = None
    transcription_delay_ms: float | None = None
    llm_ttft_ms: float | None = None
    tts_ttfb_ms: float | None = None

    @property
    def response_delay_ms(self) -> float | None:
        """What the caller actually experiences as silence.

        Deliberately a sum of the components rather than a single wall-clock
        measurement: the point of instrumenting four numbers is attribution,
        and a total you cannot decompose tells an engineer nothing about who
        owns the regression. Returns ``None`` if any component is missing,
        because a partial sum here would read as a suspiciously fast turn.
        """
        parts = (
            self.end_of_utterance_delay_ms,
            self.transcription_delay_ms,
            self.llm_ttft_ms,
            self.tts_ttfb_ms,
        )
        if any(p is None for p in parts):
            return None
        return sum(p for p in parts if p is not None)


@dataclass(slots=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    ok: bool
    duration_ms: float | None = None
    error: str | None = None
    result: Any = None


@dataclass(slots=True)
class Turn:
    """One leg of the turn cycle.

    The unit of everything in voice. A session is a list of these plus an
    outcome.
    """

    index: int
    role: Literal["user", "agent"]
    text: str
    started_at: str | None = None  # ISO 8601
    ended_at: str | None = None
    latency: TurnLatency | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens: Tokens = field(default_factory=Tokens)
    audio_seconds: float | None = None
    #: User began speaking while the agent held the floor.
    interrupted_by_user: bool = False
    #: Agent kept speaking while the user held the floor. Not the same thing.
    talked_over_user: bool = False
    #: Silence preceding this turn, with neither party speaking.
    silence_before_ms: float | None = None


# --------------------------------------------------------------------------
# The three layers
# --------------------------------------------------------------------------


@dataclass(slots=True)
class OutcomeCheck:
    """One deterministic assertion about the world after the call.

    This is the voice equivalent of a hidden test. It reads the side effect the
    agent was supposed to produce (a row in bookings.json) and compares it to
    what the scenario said the caller wanted. No model is consulted.
    """

    name: str
    ok: bool
    expected: Any = None
    actual: Any = None
    detail: str | None = None


@dataclass(slots=True)
class DeterministicResult:
    """Layer 1. Facts you can check without an opinion."""

    passed: bool
    checks: list[OutcomeCheck] = field(default_factory=list)
    #: Present when the checks could not be executed at all.
    error: str | None = None


@dataclass(slots=True)
class BehaviorStats:
    """Layer 2. The shape of the trajectory, not whether it arrived.

    Counted from the trace by code against the fixed taxonomy. Two sessions can
    both pass layer 1 and be wildly different calls to sit through, and this is
    the layer that says so.
    """

    turns: int = 0
    #: Agent turns before the outcome check first became satisfiable.
    turns_to_resolution: int | None = None
    user_interruptions: int = 0
    agent_overlaps: int = 0
    backchannels: int = 0
    reprompts: int = 0
    dead_air_events: int = 0
    max_dead_air_ms: float | None = None
    tool_errors: int = 0
    escalated_to_human: bool = False
    resolved_in_one_turn: bool = False


@dataclass(slots=True)
class HumanOutcome:
    """Layer 3. Did it actually work for the caller.

    Recorded out of band by a person, sampled, never inferred by a model. The
    interesting cell is ``passed and not accepted``: the tool call succeeded,
    the record was written, and the caller still did not get what they came
    for. That is completed-but-abandoned, and nothing in layers 1 or 2 can see
    it.
    """

    accepted: bool
    caller_hung_up_mid_turn: bool = False
    called_back_within_minutes: int | None = None
    escalated_to_human: bool = False
    note: str | None = None
    recorded_at: str | None = None  # ISO 8601


# --------------------------------------------------------------------------
# Session and run
# --------------------------------------------------------------------------


@dataclass(slots=True)
class SessionResult:
    """One call, scored across all three layers."""

    id: str
    scenario_id: str
    title: str
    category: ScenarioCategory
    difficulty: Difficulty
    #: What the caller was trying to accomplish, in plain language.
    goal: str

    deterministic: DeterministicResult
    behavior: BehaviorStats
    outcome: HumanOutcome | None = None

    failure_tags: list[TaggedFailure] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)

    tokens: Tokens = field(default_factory=Tokens)
    cost: float | None = None
    wall_clock_ms: float = 0.0
    audio_seconds: float = 0.0

    #: Where the recording and provider-side trace live, for the drilldown.
    recording_ref: str | None = None
    provider_session_id: str | None = None
    adapter: str = "livekit"

    @property
    def passed(self) -> bool:
        return self.deterministic.passed

    @property
    def completed_but_abandoned(self) -> bool:
        """The headline. Layer 1 said yes, the human said no."""
        return bool(self.outcome is not None and self.passed and not self.outcome.accepted)


@dataclass(slots=True)
class LatencyPercentiles:
    """Latency is a distribution, and voice is judged at the tail.

    A mean response delay inside budget with a p95 at three seconds is a bad
    agent that looks fine on a dashboard, so the tail ships in the summary
    rather than being left for someone to compute.
    """

    p50_ms: float | None = None
    p90_ms: float | None = None
    p95_ms: float | None = None
    worst_ms: float | None = None
    within_budget_rate: float | None = None


@dataclass(slots=True)
class OutcomeMatrix:
    """The cross-layer matrix, and the reason both harnesses exist.

    ``passed_but_abandoned`` is the same cell ForwardEval calls
    ``passedButRejected``. ``failed_accepted`` is its mirror and flags checks
    that are too strict.
    """

    sessions_with_outcome: int = 0
    passed_accepted: int = 0
    passed_but_abandoned: int = 0
    failed_accepted: int = 0
    failed_rejected: int = 0

    @property
    def abandonment_rate(self) -> float | None:
        denom = self.passed_accepted + self.passed_but_abandoned
        return (self.passed_but_abandoned / denom) if denom else None


@dataclass(slots=True)
class BehaviorSummary:
    sessions_with_data: int = 0
    avg_turns: float = 0.0
    avg_turns_to_resolution: float | None = None
    interruption_rate: float = 0.0
    overlap_rate: float = 0.0
    dead_air_rate: float = 0.0
    one_turn_resolution_rate: float = 0.0


@dataclass(slots=True)
class MetricLayers:
    behavior: BehaviorSummary | None = None
    outcome: OutcomeMatrix | None = None


@dataclass(slots=True)
class RunConfig:
    label: str
    adapter: str = "livekit"
    llm_model: str = ""
    stt_model: str = ""
    tts_model: str = ""
    turn_detection: str = ""
    pricing: Pricing | None = None
    harness_version: str = HARNESS_VERSION
    verification: Verification = Verification.CONFIRMED
    #: Free-form provider/runtime notes, e.g. SDK version.
    notes: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class RunSummary:
    total_sessions: int = 0
    passed: int = 0
    pass_rate: float = 0.0
    by_category: dict[str, dict[str, float]] = field(default_factory=dict)
    failure_distribution: dict[str, int] = field(default_factory=dict)

    tokens: Tokens = field(default_factory=Tokens)
    cost: float | None = None
    audio_minutes: float = 0.0
    total_wall_clock_ms: float = 0.0

    latency: LatencyPercentiles = field(default_factory=LatencyPercentiles)
    metric_layers: MetricLayers = field(default_factory=MetricLayers)

    # --- outcome-per-unit block (the ROI lens, mirrored from ForwardEval) ---
    tokens_per_success: float | None = None
    cost_per_success: float | None = None
    #: Spend on calls that ultimately failed. The number an operator feels.
    wasted_cost: float | None = None
    wasted_tokens: int = 0


@dataclass(slots=True)
class Run:
    """The top-level artifact. One file per run."""

    run_id: str
    started_at: str
    finished_at: str
    config: RunConfig
    summary: RunSummary
    sessions: list[SessionResult] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION
    domain: Domain = Domain.VOICE

    # ---- serialization ----

    def to_dict(self) -> dict[str, Any]:
        return _asdict(self)

    def write(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return p


def _asdict(obj: Any) -> Any:
    """dataclasses.asdict, but it keeps computed properties and enum values.

    ``total``, ``response_delay_ms``, ``passed`` and friends are properties, so
    the stock asdict drops them and a consumer of the JSON has to recompute
    derived values it should simply be handed.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        out: dict[str, Any] = {}
        for f in dataclasses.fields(obj):
            out[f.name] = _asdict(getattr(obj, f.name))
        for name in _COMPUTED.get(type(obj), ()):
            out[name] = _asdict(getattr(obj, name))
        return out
    if isinstance(obj, StrEnum):
        return obj.value
    if isinstance(obj, dict):
        return {k: _asdict(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_asdict(v) for v in obj]
    return obj


#: Properties that must survive serialization, per type.
_COMPUTED: dict[type, tuple[str, ...]] = {
    Tokens: ("total",),
    TurnLatency: ("response_delay_ms",),
    SessionResult: ("passed", "completed_but_abandoned"),
    OutcomeMatrix: ("abandonment_rate",),
}
