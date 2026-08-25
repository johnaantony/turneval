"""Projection into ForwardEval's ``RunResult`` shape.

ForwardEval already has a dashboard that renders Overview, Run Comparison, and
Token ROI off a single ``results/<run-id>.json`` file. Rather than build a
second dashboard, a voice run is projected into that file format, so someone
can drop a TurnEval run next to their coding runs and compare them in one view.

The correspondence that makes this legitimate, rather than a format hack:

======================  ==========================  =========================
concept                 ForwardEval (coding)        TurnEval (voice)
======================  ==========================  =========================
verifiable outcome      hidden test suite exits 0   booking record matches
layer 2                 rework loops, first-try     reprompts, one-turn
layer 3 headline        passed-but-rejected         completed-but-abandoned
ROI denominator         cost per solve              cost per successful call
======================  ==========================  =========================

**What is lossy, stated plainly.** The projection is one-way and drops the
voice payload: per-turn latency breakdowns, dead air, overlap, and audio
duration have no home in the coding schema. Read the native
``turneval`` run file for those. Two further mismatches worth knowing:

* ForwardEval's transcript union has no ``user`` variant, so caller turns are
  emitted as ``system`` items prefixed ``CALLER:``. They render; they are not
  natively typed.
* ``FailureTagName`` in ForwardEval is a closed union of coding tags. Voice
  tags pass through as strings and display correctly, but a strict TypeScript
  consumer would need that union widened.

Neither is worth distorting the voice schema to avoid.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from turneval.schema import Run, SessionResult, Turn

FORWARDEVAL_SCHEMA_VERSION = 1


def project(run: Run) -> dict[str, Any]:
    """Return a ForwardEval-compatible ``RunResult`` dict."""
    sessions = run.sessions
    passed = [s for s in sessions if s.passed]

    return {
        "schemaVersion": FORWARDEVAL_SCHEMA_VERSION,
        "runId": run.run_id,
        "startedAt": run.started_at,
        "finishedAt": run.finished_at,
        "config": _config(run),
        "summary": _summary(run, passed),
        "tasks": [_task(s) for s in sessions],
    }


def write(run: Run, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(project(run), indent=2) + "\n", encoding="utf-8")
    return p


# --------------------------------------------------------------------------


def _config(run: Run) -> dict[str, Any]:
    pricing = run.config.pricing
    fe_pricing = None
    if pricing and pricing.llm_input_per_mtok is not None:
        fe_pricing = {
            "inputPerMTok": pricing.llm_input_per_mtok,
            "outputPerMTok": pricing.llm_output_per_mtok or 0.0,
        }

    return {
        "model": run.config.llm_model,
        "temperature": 0,
        # A voice call has no turn cap, and inventing one would show up in the
        # dashboard as a limit that was never enforced.
        "maxTurns": 0,
        "attempts": 1,
        "pricing": fe_pricing,
        "label": run.config.label,
        "harnessVersion": f"turneval/{run.config.harness_version}",
        "verification": run.config.verification.value,
        "sandboxTimeoutMs": 0,
        "testMode": "human",
        "verdictSource": "human",
    }


def _summary(run: Run, passed: list[SessionResult]) -> dict[str, Any]:
    s = run.summary
    total = s.total_sessions
    n_passed = len(passed)

    by_category: dict[str, dict[str, float]] = {}
    by_difficulty: dict[str, dict[str, float]] = {}
    for session in run.sessions:
        for bucket, key in ((by_category, session.category.value), (by_difficulty, session.difficulty.value)):
            cell = bucket.setdefault(key, {"total": 0, "passed": 0, "rate": 0.0})
            cell["total"] += 1
            cell["passed"] += int(session.passed)
            cell["rate"] = cell["passed"] / cell["total"]

    turns_to_resolve = [
        session.behavior.turns_to_resolution
        for session in passed
        if session.behavior.turns_to_resolution is not None
    ]

    return {
        "totalTasks": total,
        "passedAt1": n_passed,
        "passAt1Rate": (n_passed / total) if total else 0.0,
        "passedAtK": n_passed,
        "passAtKRate": (n_passed / total) if total else 0.0,
        "byCategory": by_category,
        "byDifficulty": by_difficulty,
        "failureDistribution": s.failure_distribution,
        "tokens": {
            "input": s.tokens.input,
            "output": s.tokens.output,
            "total": s.tokens.total,
        },
        "cost": s.cost,
        "avgTurnsToSolve": (sum(turns_to_resolve) / len(turns_to_resolve)) if turns_to_resolve else 0.0,
        "tokensPerSolve": s.tokens_per_success or 0.0,
        "costPerSolve": s.cost_per_success,
        "wastedTokens": s.wasted_tokens,
        "wastedCost": s.wasted_cost,
        "totalWallClockMs": s.total_wall_clock_ms,
        "testAuthorship": None,
        "metricLayers": _metric_layers(run),
    }


def _metric_layers(run: Run) -> dict[str, Any]:
    layers = run.summary.metric_layers
    behavior = None
    if layers.behavior:
        b = layers.behavior
        behavior = {
            "tasksWithData": b.sessions_with_data,
            "avgToolCalls": _avg(s.behavior.tool_errors + _tool_call_count(s) for s in run.sessions),
            "avgReworkLoops": _avg(s.behavior.reprompts for s in run.sessions),
            "firstTryPassRate": b.one_turn_resolution_rate,
        }

    outcome = None
    if layers.outcome:
        o = layers.outcome
        outcome = {
            "tasksWithOutcome": o.sessions_with_outcome,
            "passedAccepted": o.passed_accepted,
            # The line this entire projection exists for.
            "passedButRejected": o.passed_but_abandoned,
            "failedAccepted": o.failed_accepted,
            "failedRejected": o.failed_rejected,
        }

    return {"behavior": behavior, "outcome": outcome}


def _task(session: SessionResult) -> dict[str, Any]:
    checks = session.deterministic.checks
    n_failed = sum(1 for c in checks if not c.ok)
    test_output = {
        "stdout": "\n".join(
            f"{'ok' if c.ok else 'FAIL'} {c.name}"
            + (f"  expected={c.expected!r} actual={c.actual!r}" if not c.ok else "")
            for c in checks
        ),
        "stderr": session.deterministic.error or "",
        "exitCode": 0 if session.passed else 1,
        "passed": len(checks) - n_failed,
        "failed": n_failed,
        "total": len(checks),
        "timedOut": False,
        "durationMs": session.wall_clock_ms,
    }

    behavior = {
        "toolCalls": _tool_call_count(session),
        "testRuns": 1,
        "writes": sum(1 for t in session.turns for c in t.tool_calls if c.ok),
        "reworkLoops": session.behavior.reprompts,
        "firstTryPass": session.behavior.resolved_in_one_turn,
        "turnsUsed": session.behavior.turns,
    }

    outcome = None
    if session.outcome:
        outcome = {
            "accepted": session.outcome.accepted,
            "reworkRequired": session.outcome.escalated_to_human,
            "note": session.outcome.note,
            "recordedAt": session.outcome.recorded_at,
        }

    attempt = {
        "attempt": 0,
        "passed": session.passed,
        "turnsUsed": session.behavior.turns,
        "stopReason": "call_ended",
        "tokens": {
            "input": session.tokens.input,
            "output": session.tokens.output,
            "total": session.tokens.total,
        },
        "cost": session.cost,
        "wallClockMs": session.wall_clock_ms,
        "finalTestOutput": test_output,
        "failureTags": [
            {"tag": t.tag.value, "justification": t.justification} for t in session.failure_tags
        ],
        "transcript": [item for turn in session.turns for item in _transcript_items(turn)],
        "finalCode": _final_artifact(session),
        "behavior": behavior,
    }

    return {
        "id": session.id,
        "title": session.title,
        "category": session.category.value,
        "difficulty": session.difficulty.value,
        "prompt": session.goal,
        "entryFile": f"scenarios/{session.scenario_id}",
        "testCommand": "turneval score",
        "stubCode": "\n".join(t.text for t in session.turns if t.role == "user"),
        "passAt1": session.passed,
        "passAtK": session.passed,
        "representativeAttempt": 0,
        "passed": session.passed,
        "turnsUsed": session.behavior.turns,
        "tokens": attempt["tokens"],
        "cost": session.cost,
        "wallClockMs": session.wall_clock_ms,
        "failureTags": attempt["failureTags"],
        "finalTestOutput": test_output,
        "finalCode": attempt["finalCode"],
        "behavior": behavior,
        "outcome": outcome,
        "provenance": {"source": "authored"},
        "attempts": [attempt],
    }


def _transcript_items(turn: Turn) -> list[dict[str, Any]]:
    if turn.role == "user":
        return [{"type": "system", "text": f"CALLER: {turn.text}"}]

    items: list[dict[str, Any]] = []
    if turn.text:
        items.append({"type": "assistant_text", "text": turn.text})
    for i, call in enumerate(turn.tool_calls):
        tool_use_id = f"{turn.index}-{i}"
        items.append(
            {
                "type": "tool_use",
                "tool": call.name,
                "input": call.arguments,
                "toolUseId": tool_use_id,
            }
        )
        items.append(
            {
                "type": "tool_result",
                "toolUseId": tool_use_id,
                "tool": call.name,
                "content": json.dumps(call.result) if call.ok else (call.error or "error"),
                "isError": not call.ok,
            }
        )
    return items


def _final_artifact(session: SessionResult) -> str:
    """The thing the agent produced, as the coding schema's ``finalCode``.

    For a voice run that is the record it wrote, which is genuinely the
    equivalent artifact: the durable side effect the outcome check reads.
    """
    for check in session.deterministic.checks:
        if check.name == "record_written" and check.actual is not None:
            return json.dumps(check.actual, indent=2)
    return ""


def _tool_call_count(session: SessionResult) -> int:
    return sum(len(t.tool_calls) for t in session.turns)


def _avg(values: Any) -> float:
    items = list(values)
    return (sum(items) / len(items)) if items else 0.0
