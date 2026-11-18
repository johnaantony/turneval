"""The runbook reports state from files, so these tests build the files.

Each case writes what a real user would have on disk at that point (a .env, a
trace, a scored run, an outcomes entry) and checks that every step reports
exactly that, and nothing more. The runs are scored with the real runner, so a
change to the run-file format that the runbook depends on fails here.

    uv run python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path

from test_pipeline import BOOKED, TOOL_OK, _agent_turn, _turn, write_trace
from turneval import runbook
from turneval.runbook import DONE, PARTIAL, TODO
from turneval.runner import run_from_disk

SCENARIOS = Path(__file__).resolve().parent.parent / "scenarios" / "scenarios.yaml"
TODAY = date(2026, 11, 18)
KEYS = "LIVEKIT_URL=wss://test.livekit.cloud\nLIVEKIT_API_KEY=key\nLIVEKIT_API_SECRET=secret\n"


class RunbookTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "scenarios").mkdir()
        shutil.copy(SCENARIOS, self.root / "scenarios" / "scenarios.yaml")
        self.traces = self.root / "data" / "traces"
        self.bookings = self.root / "data" / "bookings"
        self.traces.mkdir(parents=True)
        self.bookings.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _steps(self, today: date = TODAY) -> dict[int, runbook.Step]:
        return {s.number: s for s in runbook.status(self.root, today)}

    def _call(self, session_id: str, scenario_id: str = "happy-path") -> None:
        write_trace(self.traces / f"{session_id}.jsonl", session_id, scenario_id, [
            _turn("user", "December first at 3pm, Dana Whitfield."),
            _agent_turn("Booked.", tool_calls=TOOL_OK),
        ])
        (self.bookings / f"{session_id}.json").write_text(json.dumps(BOOKED), encoding="utf-8")

    def _score(self, label: str = "test") -> None:
        run = run_from_disk(
            traces=self.traces, scenarios_file=self.root / "scenarios" / "scenarios.yaml",
            bookings_dir=self.bookings, outcomes_file=self.root / "data" / "outcomes.json", label=label,
        )
        run.write(self.root / "results" / f"{run.run_id}.json")

    def test_fresh_clone_points_at_step_one(self) -> None:
        steps = self._steps()
        self.assertEqual(steps[1].state, TODO)
        self.assertEqual(steps[3].state, TODO)
        self.assertEqual(steps[4].state, TODO)
        out = runbook.render(list(steps.values()), TODAY)
        self.assertIn("Next, step 1", out)
        self.assertIn("cp .env.example .env", out)

    def test_copied_but_unfilled_env_is_not_connected(self) -> None:
        shutil.copy(SCENARIOS.parent.parent / ".env.example", self.root / ".env")
        step = self._steps()[1]
        self.assertEqual(step.state, PARTIAL)
        for key in runbook.ENV_KEYS:
            self.assertIn(key, step.detail)

    def test_scored_call_without_a_verdict(self) -> None:
        (self.root / ".env").write_text(KEYS, encoding="utf-8")
        self._call("s1")
        self._score()
        steps = self._steps()
        self.assertEqual(steps[1].state, DONE)
        self.assertEqual(steps[2].state, DONE)
        self.assertEqual(steps[3].state, PARTIAL)
        self.assertIn("1 of 8", steps[3].detail)
        self.assertEqual(steps[4].state, DONE)
        self.assertEqual(steps[6].state, TODO)
        # The command is pre-filled with the session that still needs a verdict.
        self.assertIn("outcome s1", steps[6].next or "")

    def test_a_call_after_the_last_score_is_unscored(self) -> None:
        self._call("s1")
        self._score()
        self._call("s2", "mind-change")
        self.assertEqual(self._steps()[4].state, PARTIAL)

    def test_a_verdict_counts_only_after_the_next_score(self) -> None:
        self._call("s1")
        self._score()
        (self.root / "data" / "outcomes.json").write_text(
            json.dumps({"s1": {"accepted": False}}), encoding="utf-8")
        step = self._steps()[6]
        self.assertEqual(step.state, PARTIAL)
        self.assertIn("since the last score", step.detail)

        self._score()
        self.assertEqual(self._steps()[6].state, DONE)

    def test_expired_fixture_dates_reopen_step_two(self) -> None:
        step = self._steps(today=date(2026, 12, 2))[2]
        self.assertEqual(step.state, PARTIAL)
        self.assertIn("in the past", step.detail)

    def test_compare_is_never_claimed_done(self) -> None:
        self._call("s1")
        self._score(label="model-a")
        self.assertEqual(self._steps()[7].state, TODO)
        self._score(label="model-b")
        self.assertEqual(self._steps()[7].state, PARTIAL)

    def test_aborted_and_unmatched_traces_are_not_evidence(self) -> None:
        (self.traces / "aborted").mkdir()
        write_trace(self.traces / "aborted" / "s1.jsonl", "s1", "happy-path", [_turn("user", "Hi.")])
        write_trace(self.traces / "s2.jsonl", "s2", "no-such-scenario", [_turn("user", "Hi.")])
        self.assertEqual(self._steps()[3].state, TODO)

    def test_routine_goes_stale(self) -> None:
        self._call("s1")  # the shared trace helper stamps calls 2026-08-25
        self.assertEqual(self._steps(today=date(2026, 8, 30))[8].state, DONE)
        self.assertEqual(self._steps(today=TODAY)[8].state, TODO)


if __name__ == "__main__":
    unittest.main()
