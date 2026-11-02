"""End-to-end test over a synthetic trace.

Runs the real adapter, the real scorer, and the real projection. Nothing is
mocked except the call itself: the trace is written in the same JSONL format
the agent's TraceRecorder produces, so a change that breaks the agent-to-scorer
contract fails here.

    uv run python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from turneval.export import forwardeval
from turneval.runner import run_from_disk
from turneval.schema import Pricing
from turneval.scoring.layers import DEAD_AIR_MS, RESPONSE_BUDGET_MS

SCENARIOS = Path(__file__).resolve().parent.parent / "scenarios" / "scenarios.yaml"


def _turn(role: str, text: str, **kw) -> dict:
    rec = {"type": "turn", "role": role, "text": text, "silence_before": kw.pop("silence", 0.4)}
    rec.update(kw)
    return rec


def _agent_turn(text: str, ttft: float = 0.35, tool_calls=None, **kw) -> dict:
    return _turn(
        "agent",
        text,
        eou={"end_of_utterance_delay": 0.12, "transcription_delay": 0.08},
        llm={"ttft": ttft, "prompt_tokens": 300, "completion_tokens": 40},
        tts={"ttfb": 0.21},
        audio_duration=2.5,
        tool_calls=tool_calls or [],
        **kw,
    )


BOOKED = {"date": "2026-12-01", "time": "15:00", "name": "Dana Whitfield"}

TOOL_OK = [{
    "name": "book_appointment",
    "arguments": {"date": "2026-12-01", "time": "15:00", "name": "Dana Whitfield"},
    "ok": True, "duration": 0.012, "result": BOOKED, "error": None,
}]


def write_trace(path: Path, session_id: str, scenario_id: str, turns: list[dict]) -> None:
    records = [
        {"type": "session_start", "session_id": session_id,
         "scenario_id": scenario_id, "timestamp": "2026-08-25T18:00:00+00:00"},
        *turns,
        {"type": "session_end", "timestamp": "2026-08-25T18:01:10+00:00",
         "duration": 70.0, "ended_reason": "closed", "num_backchannels": 0,
         "escalated": False},
    ]
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")


class PipelineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.traces = root / "traces"
        self.bookings = root / "bookings"
        self.outcomes = root / "outcomes.json"
        self.traces.mkdir()
        self.bookings.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _score(self, pricing: Pricing | None = None):
        return run_from_disk(
            traces=self.traces, scenarios_file=SCENARIOS, bookings_dir=self.bookings,
            outcomes_file=self.outcomes, label="test", pricing=pricing,
        )

    # ---- layer 1 ----

    def test_happy_path_passes(self) -> None:
        write_trace(self.traces / "s1.jsonl", "s1", "happy-path", [
            _turn("user", "I'd like an appointment."),
            _agent_turn("What date?"),
            _turn("user", "December first at 3pm, Dana Whitfield."),
            _agent_turn("Booked.", tool_calls=TOOL_OK),
        ])
        (self.bookings / "s1.json").write_text(json.dumps(BOOKED), encoding="utf-8")

        run = self._score()
        self.assertEqual(len(run.sessions), 1)
        self.assertTrue(run.sessions[0].passed)
        self.assertEqual(run.summary.pass_rate, 1.0)

    def test_wrong_field_fails_and_is_tagged(self) -> None:
        write_trace(self.traces / "s2.jsonl", "s2", "mind-change", [
            _turn("user", "Actually make it 4pm."),
            _agent_turn("Booked.", tool_calls=TOOL_OK),
        ])
        # Scenario expects 16:00; the agent wrote 15:00.
        (self.bookings / "s2.json").write_text(json.dumps(BOOKED), encoding="utf-8")

        run = self._score()
        session = run.sessions[0]
        self.assertFalse(session.passed)
        self.assertIn("wrong_arguments", [t.tag.value for t in session.failure_tags])

    def test_expect_null_requires_no_booking(self) -> None:
        """off-topic passes only when the agent booked nothing."""
        write_trace(self.traces / "s3.jsonl", "s3", "off-topic", [
            _turn("user", "Do you do whitening?"),
            _agent_turn("We do, but I can only book appointments."),
        ])
        run = self._score()
        self.assertTrue(run.sessions[0].passed)

        # Now the agent invents a booking nobody asked for.
        (self.bookings / "s3.json").write_text(json.dumps(BOOKED), encoding="utf-8")
        run = self._score()
        self.assertFalse(run.sessions[0].passed)

    def test_tool_failure_scenario_passes_when_nothing_written(self) -> None:
        write_trace(self.traces / "s4.jsonl", "s4", "tool-failure", [
            _turn("user", "Book me December first at 3pm, Dana Whitfield."),
            _agent_turn("I couldn't complete that booking.", tool_calls=[{
                "name": "book_appointment", "arguments": {}, "ok": False,
                "duration": 0.01, "result": None, "error": "backend unavailable",
            }]),
        ])
        run = self._score()
        session = run.sessions[0]
        self.assertTrue(session.passed, "declining honestly is the correct outcome")
        self.assertEqual(session.behavior.tool_errors, 1)

    # ---- layer 2 ----

    def test_dead_air_and_latency_tags(self) -> None:
        slow = (RESPONSE_BUDGET_MS / 1000.0) + 1.0
        write_trace(self.traces / "s5.jsonl", "s5", "silence-after-question", [
            _turn("user", "I'd like to book something."),
            _agent_turn("What date works?"),
            _turn("user", "Sorry. December first, 3pm, Dana Whitfield.",
                  silence=(DEAD_AIR_MS / 1000.0) + 1.2),
            _agent_turn("Booked.", ttft=slow, tool_calls=TOOL_OK),
        ])
        (self.bookings / "s5.json").write_text(json.dumps(BOOKED), encoding="utf-8")

        run = self._score()
        session = run.sessions[0]
        tags = [t.tag.value for t in session.failure_tags]
        self.assertEqual(session.behavior.dead_air_events, 1)
        self.assertIn("dead_air", tags)
        self.assertIn("latency_budget_exceeded", tags)
        # A slow, awkward call that still booked correctly stays a layer-1 pass.
        self.assertTrue(session.passed)

    def test_resolution_turn_counts_agent_turns_only(self) -> None:
        write_trace(self.traces / "s6.jsonl", "s6", "happy-path", [
            _turn("user", "Hi."),
            _agent_turn("What date?"),
            _turn("user", "December first, 3pm, Dana Whitfield."),
            _agent_turn("Booked.", tool_calls=TOOL_OK),
        ])
        (self.bookings / "s6.json").write_text(json.dumps(BOOKED), encoding="utf-8")
        run = self._score()
        self.assertEqual(run.sessions[0].behavior.turns_to_resolution, 2)

    # ---- layer 3 ----

    def test_completed_but_abandoned_is_the_headline(self) -> None:
        write_trace(self.traces / "s7.jsonl", "s7", "abandonment-hangup", [
            _turn("user", "December first, 3pm, Dana Whitfield."),
            _agent_turn("Booked.", tool_calls=TOOL_OK),
        ])
        (self.bookings / "s7.json").write_text(json.dumps(BOOKED), encoding="utf-8")

        # Without a human outcome there is no matrix at all, by design.
        run = self._score()
        self.assertIsNone(run.summary.metric_layers.outcome)
        self.assertFalse(run.sessions[0].completed_but_abandoned)

        self.outcomes.write_text(json.dumps({
            "s7": {"accepted": False, "caller_hung_up_mid_turn": True,
                   "note": "hung up on confirmation"}
        }), encoding="utf-8")

        run = self._score()
        session = run.sessions[0]
        self.assertTrue(session.passed, "layer 1 still records a success")
        self.assertTrue(session.completed_but_abandoned)
        matrix = run.summary.metric_layers.outcome
        self.assertIsNotNone(matrix)
        assert matrix is not None
        self.assertEqual(matrix.passed_but_abandoned, 1)
        self.assertEqual(matrix.abandonment_rate, 1.0)
        self.assertIn("abandoned_by_caller", [t.tag.value for t in session.failure_tags])

    # ---- cost ----

    def test_cost_is_none_without_pricing(self) -> None:
        write_trace(self.traces / "s8.jsonl", "s8", "happy-path", [_agent_turn("Booked.", tool_calls=TOOL_OK)])
        (self.bookings / "s8.json").write_text(json.dumps(BOOKED), encoding="utf-8")

        self.assertIsNone(self._score().summary.cost)

        run = self._score(pricing=Pricing(llm_input_per_mtok=3.0, llm_output_per_mtok=15.0,
                                          stt_per_audio_minute=0.006))
        self.assertIsNotNone(run.summary.cost)
        self.assertIsNotNone(run.summary.cost_per_success)

    # ---- projection ----

    def test_forwardeval_projection_shape(self) -> None:
        write_trace(self.traces / "s9.jsonl", "s9", "abandonment-hangup", [
            _turn("user", "December first, 3pm, Dana Whitfield."),
            _agent_turn("Booked.", tool_calls=TOOL_OK),
        ])
        (self.bookings / "s9.json").write_text(json.dumps(BOOKED), encoding="utf-8")
        self.outcomes.write_text(json.dumps({"s9": {"accepted": False}}), encoding="utf-8")

        fe = forwardeval.project(self._score())

        self.assertEqual(fe["schemaVersion"], 1)
        for key in ("runId", "startedAt", "finishedAt", "config", "summary", "tasks"):
            self.assertIn(key, fe)
        # The line the whole projection exists for.
        self.assertEqual(fe["summary"]["metricLayers"]["outcome"]["passedButRejected"], 1)

        task = fe["tasks"][0]
        for key in ("id", "title", "category", "difficulty", "prompt", "entryFile",
                    "testCommand", "stubCode", "passAt1", "passed", "tokens",
                    "finalTestOutput", "behavior", "attempts"):
            self.assertIn(key, task)
        self.assertEqual(task["finalTestOutput"]["exitCode"], 0)
        # Caller turns survive as labelled system items.
        transcript = task["attempts"][0]["transcript"]
        self.assertTrue(any(i["type"] == "system" and i["text"].startswith("CALLER:")
                            for i in transcript))
        self.assertTrue(any(i["type"] == "tool_use" for i in transcript))
        json.dumps(fe)  # must be serializable

    def test_unmatched_scenario_is_skipped_not_crashed(self) -> None:
        write_trace(self.traces / "s10.jsonl", "s10", "no-such-scenario", [_agent_turn("Hi.")])
        run = self._score()
        self.assertEqual(len(run.sessions), 0)
        self.assertIn("skipped_unmatched_traces", run.config.notes)


if __name__ == "__main__":
    unittest.main()
