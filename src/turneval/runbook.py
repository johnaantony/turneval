"""Where you are in the runbook, read from your files.

RUNBOOK.md walks eight steps from a fresh clone to a ship decision. This module
answers the question a new user actually has, "which of those have I done, and
what do I run next?", without asking them to keep track: every step's state is
derived from evidence on disk (the keys in ``.env``, the traces, the run files,
the outcomes log). Nothing is ticked by hand, which is the same rule the
verdicts follow.

It only reads. It never writes a file, calls the network, or runs a test.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from turneval.scenarios import Scenario, load_scenarios

DONE = "done"
PARTIAL = "in progress"
TODO = "not started"

ENV_KEYS = ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
#: What .env.example ships with, so a copied but unfilled file reads as unset.
PLACEHOLDER_URL = "wss://your-project.livekit.cloud"
#: Step 8 counts as routine while the last call is at most this many days old.
ROUTINE_DAYS = 7

OUTCOME_CMD = "PYTHONPATH=src .venv/bin/python -m turneval.cli outcome"


@dataclass(slots=True)
class Step:
    number: int
    name: str
    state: str
    detail: str
    #: The command that moves this step forward, when there is one to give.
    next: str | None = None


def status(root: Path, today: date | None = None) -> list[Step]:
    """The eight steps, each with its state read from the files under ``root``."""
    today = today or date.today()
    scenarios_file = root / "scenarios" / "scenarios.yaml"
    scenarios = load_scenarios(scenarios_file) if scenarios_file.exists() else []
    ids = [s.id for s in scenarios]
    # The scorer skips traces whose scenario id it does not know, so they are
    # not evidence of anything here either.
    traces = [t for t in _traces(root / "data" / "traces") if t.get("scenario_id") in ids]
    runs = _runs(root / "results")
    latest = runs[-1] if runs else None
    outcomes = _read_json(root / "data" / "outcomes.json")
    label = today.isoformat()

    return [
        _connect(root / ".env"),
        _define(scenarios, today),
        _run(ids, traces),
        _verdict(traces, latest, label),
        _explain(latest),
        _human(latest, outcomes, label),
        _compare(runs),
        _routine(traces, today),
    ]


def render(steps: list[Step], today: date) -> str:
    lines = [f"TurnEval runbook, {today.isoformat()}  (RUNBOOK.md explains each step)", ""]
    for s in steps:
        lines.append(f"  {s.number}  {s.name:<25} {s.state:<12} {s.detail}")

    pending = [s for s in steps if s.state != DONE]
    if not pending:
        lines += ["", "Every step has its evidence. Run the set again after the next agent change."]
    else:
        first = pending[0]
        lines += ["", f"Next, step {first.number}, {first.name.lower()}:", f"  {first.next or first.detail}"]
    return "\n".join(lines)


# ---- the eight steps -------------------------------------------------------


def _connect(env_file: Path) -> Step:
    name = "Connect"
    if not env_file.exists():
        return Step(1, name, TODO, "no .env yet",
                    "cp .env.example .env, then paste your three LiveKit keys into it")
    env = _read_env(env_file)
    missing = [k for k in ENV_KEYS if env.get(k, "") in ("", PLACEHOLDER_URL)]
    if missing:
        return Step(1, name, PARTIAL, f"not set in .env: {', '.join(missing)}",
                    "copy them from your LiveKit project's Settings > Keys page into .env")
    return Step(1, name, DONE, "LiveKit keys set in .env")


def _define(scenarios: list[Scenario], today: date) -> Step:
    name = "Define done"
    if not scenarios:
        return Step(2, name, TODO, "no scenarios in scenarios/scenarios.yaml",
                    "add a scenario: a caller script and the record the call must leave behind")

    dates = {s.id: date.fromisoformat(s.expect["date"])
             for s in scenarios if s.expect and "date" in s.expect}
    past = [i for i, d in dates.items() if d < today]
    if past:
        # The agent refuses to book in the past, so an expired fixture fails
        # every call for a reason that has nothing to do with the agent.
        return Step(2, name, PARTIAL,
                    f"{len(past)} of {len(dates)} scenario dates are in the past, and the agent refuses past dates",
                    "move the dates in scenarios/scenarios.yaml forward, in the caller script and in expect")

    adversarial = sum(1 for s in scenarios if s.category.value != "happy_path")
    detail = f"{len(scenarios)} scenarios, {adversarial} built to break the agent"
    if dates:
        first = min(dates.values())
        detail += f"; dates valid until {first.isoformat()} ({(first - today).days} days)"
    return Step(2, name, DONE, detail)


def _run(scenario_ids: list[str], traces: list[dict[str, Any]]) -> Step:
    name = "Run"
    if not scenario_ids:
        return Step(3, name, TODO, "no scenarios to call yet")
    traced = {t["scenario_id"] for t in traces}
    missing = [i for i in scenario_ids if i not in traced]
    if len(missing) == len(scenario_ids):
        return Step(3, name, TODO, "no real calls yet", f"make console SCENARIO={missing[0]}")
    if missing:
        return Step(3, name, PARTIAL,
                    f"{len(scenario_ids) - len(missing)} of {len(scenario_ids)} scenarios have a real call",
                    f"make console SCENARIO={missing[0]}")
    return Step(3, name, DONE, f"all {len(scenario_ids)} scenarios have a real call")


def _verdict(traces: list[dict[str, Any]], latest: dict[str, Any] | None, label: str) -> Step:
    name = "Read the verdict"
    if not traces:
        return Step(4, name, TODO, "nothing to score until a call has run")
    if latest is None:
        return Step(4, name, TODO, f"{len(traces)} call(s) not scored yet", f"make score LABEL={label}")
    scored = {s["id"] for s in latest["sessions"]}
    unscored = [t for t in traces if t.get("session_id") not in scored]
    if unscored:
        return Step(4, name, PARTIAL, f"{len(unscored)} call(s) since the last score", f"make score LABEL={label}")
    s = latest["summary"]
    return Step(4, name, DONE, f"latest run {s['passed']}/{s['total_sessions']} passed ({latest['_file']})")


def _explain(latest: dict[str, Any] | None) -> Step:
    name = "Explain the failures"
    if latest is None:
        return Step(5, name, TODO, "nothing scored yet")
    failed = [s for s in latest["sessions"] if not s["passed"]]
    untagged = [s for s in failed if not s.get("failure_tags")]
    if untagged:
        return Step(5, name, PARTIAL, f"{len(untagged)} failure(s) with no reason tag")
    if not failed:
        return Step(5, name, DONE, "no layer-1 failures in the latest run")
    counts = latest["summary"].get("failure_distribution") or {}
    tags = ", ".join(f"{tag} {n}" for tag, n in sorted(counts.items(), key=lambda kv: -kv[1]))
    return Step(5, name, DONE, f"every failure has a reason: {tags}")


def _human(latest: dict[str, Any] | None, outcomes: dict[str, Any], label: str) -> Step:
    name = "Record the human verdict"
    if latest is None:
        return Step(6, name, TODO, "nothing scored yet")
    scored = [s["id"] for s in latest["sessions"]]
    pending = [i for i in scored if i not in outcomes]
    recorded = [i for i in scored if i in outcomes]
    record = f'{OUTCOME_CMD} {pending[0] if pending else "<session-id>"} --note "what you heard"'
    hint = "   (add --reject if you would not have accepted the call)"

    if not recorded:
        return Step(6, name, TODO, f"0 of {len(scored)} scored calls have a verdict", record + hint)
    if pending:
        return Step(6, name, PARTIAL, f"{len(recorded)} of {len(scored)} scored calls have a verdict", record + hint)
    # Verdicts are merged into the run at score time, so one recorded after the
    # last score is not in the matrix yet.
    in_run = {s["id"] for s in latest["sessions"] if s.get("outcome")}
    unmerged = [i for i in recorded if i not in in_run]
    if unmerged:
        return Step(6, name, PARTIAL, f"{len(unmerged)} verdict(s) recorded since the last score",
                    f"make score LABEL={label}")
    matrix = (latest["summary"].get("metric_layers") or {}).get("outcome") or {}
    return Step(6, name, DONE, f"all {len(scored)} scored calls have a verdict; "
                               f"completed-but-abandoned {matrix.get('passed_but_abandoned', 0)}")


def _compare(runs: list[dict[str, Any]]) -> Step:
    name = "Compare before you ship"
    labels = sorted({r["config"]["label"] for r in runs})
    if len(labels) < 2:
        return Step(7, name, TODO, "needs two scored runs of the same scenarios, one change between them",
                    "TURNEVAL_LLM=<provider/model> make console SCENARIO=happy-path, then make score LABEL=model-b")
    # Never done: two labels prove two runs exist, not that only one thing
    # changed between them, and the command that checks that is not built yet.
    return Step(7, name, PARTIAL, f"{len(labels)} labelled runs to read side by side; "
                                  "a compare command is not built yet")


def _routine(traces: list[dict[str, Any]], today: date) -> Step:
    name = "Make it routine"
    if not traces:
        return Step(8, name, TODO, "no calls yet")
    last = max(datetime.fromisoformat(t["timestamp"]).date() for t in traces)
    age = (today - last).days
    if age <= ROUTINE_DAYS:
        return Step(8, name, DONE, f"last call {age} day(s) ago")
    return Step(8, name, TODO, f"last call {age} days ago; run the set after every agent change")


# ---- reading the files -----------------------------------------------------


def _read_env(path: Path) -> dict[str, str]:
    """KEY=VALUE pairs from a dotenv file. Values are only ever tested for presence."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip().removeprefix("export ").strip()] = value.strip().strip("'\"")
    return values


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _traces(traces_dir: Path) -> list[dict[str, Any]]:
    """The session_start record of each trace. Non-recursive, so aborted/ is skipped."""
    starts = []
    for path in sorted(traces_dir.glob("*.jsonl")):
        with path.open(encoding="utf-8") as f:
            first = f.readline()
        try:
            record = json.loads(first)
        except json.JSONDecodeError:
            continue
        if record.get("type") == "session_start":
            starts.append(record)
    return starts


def _runs(results_dir: Path) -> list[dict[str, Any]]:
    """Scored voice runs, oldest first. Anything else in results/ is ignored."""
    runs = []
    for path in results_dir.glob("*.json"):
        data = _read_json(path)
        if data.get("domain") == "voice" and isinstance(data.get("sessions"), list):
            data["_file"] = path.name
            runs.append(data)
    return sorted(runs, key=lambda r: r.get("finished_at") or "")
