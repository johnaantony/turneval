# TurnEval

Outcome evaluation for voice agents.

Voice observability answers "what happened." It does not answer "was it any
good," and for voice agents those are different questions with different
owners. Turn-by-turn playback and a latency breakdown tell you the mechanics of
a call. They do not tell you whether the caller got what they wanted. An agent
can hit every latency budget, produce a clean transcript, and still fail the
person on the phone.

TurnEval measures the second question. It runs a voice agent against a set of
scenarios that are designed to break it, checks the outcome against the world
rather than the transcript, and reports the one number a latency dashboard can
never surface.

Not an engineer? Start with [GUIDE.md](GUIDE.md), the plain-language version.

## What the first real call caught

The first real call ran on 2026-10-30: the `happy-path` scenario, a caller
booking Tuesday, September 1st at 3 PM for Dana Whitfield. From the trace:

```
caller  Hi, I'd like to book an appointment.
agent   Sure! May I have your name, please?
caller  Tuesday, September 1st, 3 in the afternoon.
agent   I just need your name to proceed with the appointment. What is your name?
caller  Dana Whitfield
agent   Your appointment has been successfully booked for September 1st at 3 PM, Dana!
```

Any transcript review passes that call. Then the scorer read the record the
agent wrote:

```json
{"date": "2023-09-01", "time": "15:00", "name": "Dana Whitfield"}
```

Right day, right time, right name, wrong year: three years in the past, and a
Friday, though the caller said Tuesday. The caller never said a year, so the
model supplied one. The score:

```
scenario                 L1    turns  dead   p95 ms  tags
---------------------------------------------------------
happy-path               FAIL      8     5      n/a  wrong_arguments,dead_air
```

And the one failing check, from the run file:

```json
{"name": "field:date", "ok": false, "expected": "2026-09-01", "actual": "2023-09-01"}
```

The fix was one line of context: the agent's instructions now carry today's
date and a rule against booking in the past. The point is the catch. This
failure is invisible in the transcript, and it shows up only when you check the
world.

The same call exposed two bugs in the trace recorder, both since fixed: caller
turns were recorded twice (8 turns for a 6-turn call), and every turn's latency
and token metrics were dropped (no p95). The scenario's own date had also gone
stale by the day of the call, so with past dates now refused, the fixtures
moved to 2026-12-01.

## The headline metric

**Completed-but-abandoned**: layer 1 passed, the record is right, and a person
still judged the call a failure. It is the metric nobody instruments and
everybody feels. The `abandonment-hangup` scenario exists to produce it: the
agent books correctly and the caller hangs up the instant it says "booked."
Nothing in the transcript, the latency breakdown, or the record shows that call
went badly. Only a recorded human verdict does, which is why layer 3 exists.

## How it works

Three layers, and which one owns a verdict is the entire design.

| Layer | Question | Judge |
|---|---|---|
| 1. Deterministic | Did the world change the way it was supposed to | Code, never a model |
| 2. Behavioral | What shape was the trajectory | Code plus a fixed taxonomy |
| 3. Human acceptance | Did the caller get what they came for | A person, sampled |

Layer 1 works because every scenario has a **verifiable outcome**. The agent
has one tool, `book_appointment`, that writes a record to disk. "Did the task
succeed" is then a file read, not an opinion, and not an LLM judging a
transcript. The moment your grader is the same kind of thing you are grading,
your numbers stop meaning anything.

Layer 3 is recorded by a person and is never defaulted. A session with no
recorded outcome is excluded from the matrix rather than counted as accepted.

See [SCHEMA.md](SCHEMA.md) for the full contract.

## Portability

The scorer never imports a vendor SDK. It reads a normalized session trace, so
the LiveKit-specific code is one file. Supporting another stack means writing
one more adapter, not touching the scoring layers.

Currently shipped: **LiveKit** (verified against livekit-agents 1.7.0).

## Works with ForwardEval

A run projects into [ForwardEval's](https://github.com/johnaantony/forwardeval)
`RunResult` format, so voice runs and coding-agent runs render in the same
dashboard and can be compared side by side:

```bash
PYTHONPATH=src .venv/bin/python -m turneval.cli score --label baseline \
  --export-forwardeval ../forwardeval/results/voice-baseline.json
```

The two harnesses share a headline metric under different names.
ForwardEval calls it `passedButRejected`: the tests went green and the human
threw the work away. TurnEval calls it `completed_but_abandoned`. Same cell,
same idea, different domain.

## Setup

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
make sync
cp .env.example .env      # then fill in your LiveKit credentials
make test
```

Model access defaults to LiveKit Inference, their managed gateway, so the
LiveKit credentials cover STT, LLM, and TTS with no other provider account.

## Running a call

```bash
make scenarios                              # see the scenario set
make console SCENARIO=ambiguous-date        # talk to it from your terminal
make score LABEL=baseline                   # score every trace
```

Then record the layer-3 verdict after listening back:

```bash
PYTHONPATH=src .venv/bin/python -m turneval.cli outcome <session-id> --reject --hung-up --note "left on confirmation"
```

## The scenarios

Seven of the eight are designed to break the agent in a specific way, because a
suite of happy paths tells you nothing. Every voice agent looks good until
someone interrupts it.

| Scenario | Probes |
|---|---|
| `happy-path` | The baseline |
| `interruption-midsentence` | Barge-in, and whether collected state survives it |
| `silence-after-question` | Dead air and re-prompt behavior |
| `ambiguous-date` | Whether it asks or guesses |
| `tool-failure` | Honesty when the backend fails |
| `mind-change` | State overwrite |
| `off-topic` | Scope discipline, checked inversely |
| `abandonment-hangup` | The headline: layer 1 passes, the caller left |

Scenario dates are fixed at 2026-12-01, and the agent refuses to book in the
past, so once that date passes, move the dates in `scenarios/scenarios.yaml`
forward. Dates relative to the day of the call are planned.

## A macOS note

Every Make target runs with `PYTHONPATH=src` rather than relying on the
editable install. On macOS the `.pth` file uv writes into the venv gets the
`UF_HIDDEN` file flag, and CPython 3.12's `site.py` silently skips hidden
`.pth` files. The install reports success and the package then cannot be
imported, with no error anywhere. `make unhide` clears it, but the flag returns
on every reinstall, so the Makefile sidesteps it entirely.

## Status

The first real call was scored on 2026-10-30 (above). Running the full scenario
set on real calls is next. Built and tested: the agent, the trace recorder, the
eight scenarios, three-layer scoring, the layer-3 outcome log, and the
ForwardEval projection. Planned after that: a comparison gate for model swaps
(hold the agent constant, change one stage, and list exactly which calls
flipped, with per-stage latency and cost), then a live view of each call as it
is scored.

## License

MIT. The agent runs on [LiveKit Agents](https://github.com/livekit/agents),
which is Apache-2.0.
