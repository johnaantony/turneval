"""The fixed failure taxonomy.

Two rules, inherited from ForwardEval and non-negotiable here:

1. A tag may only come from this list. Free-form failure descriptions are how
   eval suites quietly stop being comparable across runs.
2. Layer-1 tags are assigned by code, never by a model. A model may be asked to
   *explain* a failure in prose, and it may propose a tag for a human to
   confirm, but it never decides whether the call succeeded.
"""

from __future__ import annotations

from enum import StrEnum


class FailureTag(StrEnum):
    """Why a voice session failed. Ordered roughly from plumbing to judgment."""

    # --- Layer 1: assigned by code from the session trace, never by a model ---
    TOOL_CALL_FAILED = "tool_call_failed"
    """The task tool raised, timed out, or was never called at all."""

    WRONG_ARGUMENTS = "wrong_arguments"
    """The tool ran, but with values that do not match the caller's request."""

    LATENCY_BUDGET_EXCEEDED = "latency_budget_exceeded"
    """A turn blew the perceptual budget. See scoring.layers.RESPONSE_BUDGET_MS."""

    DEAD_AIR = "dead_air"
    """Silence past the threshold with neither party speaking."""

    TALKED_OVER_USER = "talked_over_user"
    """The agent kept speaking while the user was speaking."""

    PREMATURE_CLOSE = "premature_close"
    """The agent confirmed or ended before the outcome check was satisfiable."""

    # --- Layer 2: assigned by code plus this fixed taxonomy ---
    MISHEARD = "misheard"
    """The final transcript diverged materially from the intended utterance."""

    NO_RECOVERY = "no_recovery"
    """The agent never recovered after an ambiguity, correction, or tool error."""

    LOOPED = "looped"
    """The agent re-asked a question it had already had answered."""

    # --- Layer 3: requires the human ratification pass ---
    ABANDONED_BY_CALLER = "abandoned_by_caller"
    """The caller left mid-turn, called back, or escalated. The headline tag."""


#: Tags a model is never permitted to assign, even as a suggestion.
CODE_ASSIGNED_ONLY: frozenset[FailureTag] = frozenset(
    {
        FailureTag.TOOL_CALL_FAILED,
        FailureTag.WRONG_ARGUMENTS,
        FailureTag.LATENCY_BUDGET_EXCEEDED,
        FailureTag.DEAD_AIR,
        FailureTag.TALKED_OVER_USER,
        FailureTag.ABANDONED_BY_CALLER,
    }
)


class ScenarioCategory(StrEnum):
    """What a scenario is designed to probe.

    These map to the seven deliberate failure cases in scenarios/scenarios.yaml.
    Keeping them as a closed set is what makes per-category pass rates mean
    something when you compare two runs.
    """

    HAPPY_PATH = "happy_path"
    INTERRUPTION = "interruption"
    SILENCE = "silence"
    AMBIGUITY = "ambiguity"
    TOOL_FAILURE = "tool_failure"
    CORRECTION = "correction"
    OFF_TOPIC = "off_topic"
    ABANDONMENT = "abandonment"


class Difficulty(StrEnum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"
