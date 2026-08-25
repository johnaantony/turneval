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

## The headline

```
scenario                 L1    turns  dead   p95 ms  tags
---------------------------------------------------------
happy-path               pass      4     0      760  -
abandonment-hangup       pass      2     0     2810  latency_budget_exceeded,abandoned_by_caller
off-topic                pass      2     0      760  -

pass rate         3/3  (100%)
response p50/p95  760ms / 2810ms
within budget     75%

completed-but-abandoned  1/1  (100%)
```

Every call passed. One of them was a failure. The agent booked the
appointment, wrote the record, matched every field, and the caller hung up
anyway. **Completed-but-abandoned** is the metric nobody instruments and
everybody feels.

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
make score LABEL=baseline
.venv/bin/python -m turneval.cli score --export-forwardeval ../forwardeval/results/voice-baseline.json
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
.venv/bin/python -m turneval.cli outcome <session-id> --reject --hung-up --note "left on confirmation"
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

## A macOS note

Every Make target runs with `PYTHONPATH=src` rather than relying on the
editable install. On macOS the `.pth` file uv writes into the venv gets the
`UF_HIDDEN` file flag, and CPython 3.12's `site.py` silently skips hidden
`.pth` files. The install reports success and the package then cannot be
imported, with no error anywhere. `make unhide` clears it, but the flag returns
on every reinstall, so the Makefile sidesteps it entirely.

## Status

The harness, scenarios, scoring, and projection are built and tested. No real
call has been run through it yet: that needs LiveKit credentials.
