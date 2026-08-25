"""Command line interface.

    turneval scenarios              list the scenario set
    turneval score                  score traces into results/<run-id>.json
    turneval outcome <id> --reject  record the layer-3 human ratification
    turneval export <run.json>      project a run into ForwardEval's format
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from turneval.export import forwardeval
from turneval.runner import run_from_disk
from turneval.scenarios import load_scenarios
from turneval.schema import Pricing, Run

ROOT = Path.cwd()
DEFAULT_TRACES = ROOT / "data" / "traces"
DEFAULT_BOOKINGS = ROOT / "data" / "bookings"
DEFAULT_OUTCOMES = ROOT / "data" / "outcomes.json"
DEFAULT_SCENARIOS = ROOT / "scenarios" / "scenarios.yaml"
DEFAULT_RESULTS = ROOT / "results"


def _fmt(value: float | None, suffix: str = "", digits: int = 0) -> str:
    if value is None:
        return "n/a"
    return f"{value:.{digits}f}{suffix}"


def cmd_scenarios(args: argparse.Namespace) -> int:
    scenarios = load_scenarios(args.scenarios)
    print(f"{len(scenarios)} scenarios in {args.scenarios}\n")
    for s in scenarios:
        expects = "no booking" if s.expect is None else json.dumps(s.expect)
        print(f"  {s.id:<24} {s.category.value:<14} {s.difficulty.value:<7} expects {expects}")
        if s.fault:
            print(f"  {'':<24} fault injected: {s.fault}")
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    if not Path(args.traces).exists():
        print(f"no traces at {args.traces}. Run a call first.", file=sys.stderr)
        return 1

    pricing = None
    if args.pricing:
        pricing = Pricing(**json.loads(Path(args.pricing).read_text(encoding="utf-8")))

    run = run_from_disk(
        traces=Path(args.traces),
        scenarios_file=Path(args.scenarios),
        bookings_dir=Path(args.bookings),
        outcomes_file=Path(args.outcomes),
        label=args.label,
        pricing=pricing,
    )

    if not run.sessions:
        print("no traces matched a scenario id. Is TURNEVAL_SCENARIO_ID set when you run the agent?", file=sys.stderr)
        return 1

    out = Path(args.out or DEFAULT_RESULTS / f"{run.run_id}.json")
    run.write(out)
    _print_table(run)
    print(f"\nrun written to {out}")

    if args.export_forwardeval:
        fe = forwardeval.write(run, args.export_forwardeval)
        print(f"forwardeval projection written to {fe}")
    return 0


def _print_table(run: Run) -> None:
    s = run.summary
    print(f"\n{run.config.label}  ({s.total_sessions} sessions)\n")
    header = f"{'scenario':<24} {'L1':<5} {'turns':>5} {'dead':>5} {'p95 ms':>8}  tags"
    print(header)
    print("-" * len(header))

    for session in run.sessions:
        delays = [
            t.latency.response_delay_ms
            for t in session.turns
            if t.latency and t.latency.response_delay_ms is not None
        ]
        worst = max(delays) if delays else None
        tags = ",".join(t.tag.value for t in session.failure_tags) or "-"
        print(
            f"{session.scenario_id:<24} "
            f"{'pass' if session.passed else 'FAIL':<5} "
            f"{session.behavior.turns:>5} "
            f"{session.behavior.dead_air_events:>5} "
            f"{_fmt(worst):>8}  {tags}"
        )

    print(f"\npass rate         {s.passed}/{s.total_sessions}  ({s.pass_rate:.0%})")
    print(f"response p50/p95  {_fmt(s.latency.p50_ms)}ms / {_fmt(s.latency.p95_ms)}ms")
    print(f"within budget     {_fmt((s.latency.within_budget_rate or 0) * 100, '%')}")
    print(f"tokens            {s.tokens.total:,}")
    if s.cost is not None:
        print(f"cost per success  {_fmt(s.cost_per_success, digits=4)}")

    outcome = s.metric_layers.outcome
    if outcome is None:
        print(
            "\nlayer 3           not recorded. "
            "Run `turneval outcome <session-id> --reject` after listening back."
        )
    else:
        print(
            f"\ncompleted-but-abandoned  {outcome.passed_but_abandoned}"
            f"/{outcome.passed_accepted + outcome.passed_but_abandoned}"
            f"  ({_fmt((outcome.abandonment_rate or 0) * 100, '%')})"
        )


def cmd_outcome(args: argparse.Namespace) -> int:
    """Record the human ratification for one session.

    Deliberately a separate command run by a person after listening back.
    Layer 3 is the one number in this harness that cannot be derived from the
    trace, and wiring it into scoring would quietly turn it into a guess.
    """
    path = Path(args.outcomes)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    data[args.session_id] = {
        "accepted": not args.reject,
        "caller_hung_up_mid_turn": args.hung_up,
        "called_back_within_minutes": args.called_back,
        "escalated_to_human": args.escalated,
        "note": args.note,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    verdict = "rejected" if args.reject else "accepted"
    print(f"recorded {args.session_id} as {verdict} in {path}")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    raw = json.loads(Path(args.run).read_text(encoding="utf-8"))
    print(
        "note: re-projecting from JSON is lossy for computed fields; "
        "prefer `turneval score --export-forwardeval`.",
        file=sys.stderr,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="turneval", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scenarios", help="list the scenario set")
    p.add_argument("--scenarios", default=str(DEFAULT_SCENARIOS))
    p.set_defaults(func=cmd_scenarios)

    p = sub.add_parser("score", help="score traces into a run file")
    p.add_argument("--traces", default=str(DEFAULT_TRACES))
    p.add_argument("--scenarios", default=str(DEFAULT_SCENARIOS))
    p.add_argument("--bookings", default=str(DEFAULT_BOOKINGS))
    p.add_argument("--outcomes", default=str(DEFAULT_OUTCOMES))
    p.add_argument("--label", default="local")
    p.add_argument("--pricing", help="JSON file of Pricing fields")
    p.add_argument("--out", help="where to write the run file")
    p.add_argument("--export-forwardeval", help="also write a ForwardEval-format run file")
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("outcome", help="record a human ratification (layer 3)")
    p.add_argument("session_id")
    p.add_argument("--reject", action="store_true", help="the caller did not get what they wanted")
    p.add_argument("--hung-up", action="store_true", help="caller left mid agent turn")
    p.add_argument("--called-back", type=int, help="minutes until the caller called back")
    p.add_argument("--escalated", action="store_true", help="handed off to a human")
    p.add_argument("--note")
    p.add_argument("--outcomes", default=str(DEFAULT_OUTCOMES))
    p.set_defaults(func=cmd_outcome)

    p = sub.add_parser("export", help="copy a run file into ForwardEval's results dir")
    p.add_argument("run")
    p.add_argument("--out", required=True)
    p.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
