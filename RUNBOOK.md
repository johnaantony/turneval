# Runbook

**From a fresh clone to a ship decision you can defend, in eight steps.**

Written for forward-deployed engineers, who run the steps, and forward-deployed
PMs, who make the calls the steps lead to. Each step says why it matters, what
to run, and what counts as done. A step is done when its evidence is on disk,
never because someone ticked a box, which is the same rule the verdicts follow.

> [!TIP]
> **Where am I?** After step 1, run `make runbook` any time. It reads your files
> and prints which steps are done and the exact command to run next.

New to the idea? [GUIDE.md](GUIDE.md) is the five-minute, no-code version.

## The eight steps

| # | Step | Who | Time | Done when |
|---|---|---|---|---|
| 1 | [Connect](#1-connect) | FDE | 15 min | LiveKit keys are in `.env` and `make test` is green |
| 2 | [Define done](#2-define-done) | FDPM decides, FDE writes | 5 min to read the shipped set | Every scenario names the record a good call leaves behind |
| 3 | [Run](#3-run) | FDE, as the caller | 3 min a call | Each scenario has a real call on disk |
| 4 | [Read the verdict](#4-read-the-verdict) | Both | 5 min | Every call is in a scored run |
| 5 | [Explain the failures](#5-explain-the-failures) | FDE | 10 min | Every failure carries a reason from a fixed list |
| 6 | [Record the human verdict](#6-record-the-human-verdict) | FDPM, or whoever would accept the call | 2 min a call | Every scored call has an accept or a reject |
| 7 | [Compare before you ship](#7-compare-before-you-ship) | FDE runs, FDPM decides | 30 min | Two scored runs of the same scenarios, one change between them |
| 8 | [Make it routine](#8-make-it-routine) | Both | Ongoing | A call in the last 7 days |

The same eight steps run in
[ForwardEval](https://github.com/johnaantony/forwardeval/blob/main/RUNBOOK.md)
for coding agents. Learn a step in one and you know it in the other.

**FDPMs: your moments are steps 2, 6, and 7.** Decide what done means for the
customer, say whether you would have accepted each call, and make the ship call
from the comparison. Each is a judgment, not an engineering task, and the FDE
can run the commands with you.

---

## 1. Connect

**Why:** prove the plumbing before spending a minute on calls.

You need Python 3.12, [uv](https://docs.astral.sh/uv/), and a free LiveKit
Cloud project.

```bash
make sync                 # install dependencies
cp .env.example .env      # paste the three keys from your LiveKit project's Settings > Keys page
make test                 # runs in under a second, no network
```

Speech-to-text, the LLM, and text-to-speech all default to LiveKit Inference,
so the LiveKit keys are the only account you need.

**Done when:** the keys are set and the tests pass. `make runbook` checks the
keys; `make test` checks the harness.

## 2. Define done

**Why:** a check needs a truth to check against. "Did the call go well?" has no
ground truth. "Is there a booking for Dana Whitfield on December 1st at 3 PM?"
does.

Each scenario in `scenarios/scenarios.yaml` has a caller script and an
`expect`: the record that must exist after the call. `expect: null` means the
right outcome is that nothing gets written, which catches the agent that books
something nobody asked for.

```bash
make scenarios            # the eight scenarios, and what each one expects
```

Seven of the eight are built to break the agent: an interruption, a long
silence, a vague date, a failing backend, a changed mind, an off-topic caller,
and a hang-up the moment the agent says "booked".

**FDPM:** this is where you decide what success means for the customer. Write
it as a record you could look up afterwards, not as a feeling about the call.

**Done when:** every scenario has an `expect` and its dates are still in the
future. The shipped scenarios use 2026-12-01 and the agent refuses to book in
the past, so once that date passes, move the dates forward in both the caller
script and the `expect`.

## 3. Run

**Why:** the verdict comes from what the agent did on a real call, written down
as it happened.

```bash
make console SCENARIO=happy-path
```

You are the caller. Talk through your microphone, follow the scenario's script,
and press Ctrl+C after the agent confirms. Every turn, timing, and tool call is
written to `data/traces/` while the call runs, and whatever the agent booked
lands in `data/bookings/`. `make runbook` names the next scenario to call.

**Done when:** each scenario has a call on disk.

## 4. Read the verdict

**Why:** pass or fail comes from re-reading the record the call changed. Not
from the transcript, and never from a model.

```bash
make score LABEL=first-run
```

```
scenario                 L1    turns  dead   p95 ms  tags
---------------------------------------------------------
happy-path               FAIL      8     5      n/a  wrong_arguments,dead_air
```

That row is the first real call this harness ever scored. The agent said
"booked for September 1st" and wrote 2023-09-01; the
[README](README.md#what-the-first-real-call-caught) has the whole story. The
run file in `results/` holds every check with its expected and actual value.

**Done when:** every call is in a scored run.

## 5. Explain the failures

**Why:** a reason on every failure, from a fixed list, means this week's
numbers mean the same thing as last week's.

Tags are assigned by code from the trace and the record, each with a one-line
justification in the run file:

| Tag | What happened |
|---|---|
| `wrong_arguments` | The tool ran with values that do not match what the caller asked for |
| `tool_call_failed` | The booking tool raised, timed out, or was never called |
| `latency_budget_exceeded` | A reply took longer than the 1.5 second budget |
| `dead_air` | A silence longer than 3 seconds, with neither side speaking |
| `talked_over_user` | The agent kept talking while the caller was speaking |
| `looped` | The agent asked again for something it had already been told |
| `abandoned_by_caller` | The caller left, called back, or escalated. Comes from step 6 |

The taxonomy also reserves `premature_close`, `misheard`, and `no_recovery`,
which the scorer does not assign yet.

**Done when:** every failed call has a tag.

## 6. Record the human verdict

**Why:** the checks say whether the record is right. Only a person can say
whether the caller got what they came for. A call that passed every check and
was still rejected is the number this harness exists to show you: **completed
but abandoned**.

Listen back, or read the trace, then record what you would have decided as the
caller:

```bash
PYTHONPATH=src .venv/bin/python -m turneval.cli outcome <session-id> --note "what you heard"
```

Add `--reject` if you would not have accepted the call, `--hung-up` if the
caller left mid-sentence, `--escalated` if it went to a human, and
`--called-back <minutes>` if they called again. `make runbook` prints this
command with the session id filled in. Verdicts are merged when you score, so
score again:

```bash
make score LABEL=first-run
```

**Done when:** every scored call has a verdict. A verdict is never filled in
for you: a call without one is left out of the matrix, not counted as accepted.

## 7. Compare before you ship

**Why:** a new model can lift the average and still break calls that used to
work. Hold the agent constant, change one thing, and read exactly which calls
flipped.

```bash
TURNEVAL_LLM=<provider/model> make console SCENARIO=happy-path    # repeat for each scenario
make score LABEL=model-b
```

`TURNEVAL_STT` and `TURNEVAL_TTS` swap the other two stages the same way. Read
the new table against the old one: which calls went from pass to fail, which
went the other way, and what happened to response time.

**FDPM:** this is the ship call. A flat average can hide new failures, so read
the calls, not the rate.

**Done when:** two scored runs of the same scenarios exist with one change
between them.

**Not built yet:** a `turneval compare` command that prints the flip list with
per-stage latency and cost, and fails a build on a regression. Today you read
the two tables side by side, which is why `make runbook` never marks this step
done.

## 8. Make it routine

**Why:** an eval you ran once is a demo. One you run after every change is a
release gate.

Run the scenario set after every change to the prompt, the model, or a tool.
While a call runs, you can watch its trace being written from a second
terminal:

```bash
tail -f "$(ls -t data/traces/*.jsonl | head -1)"
```

**Done when:** there is a call from the last 7 days.

**Not built yet:** a live monitor that shows the call, every detail the caller
gave, and each tool call checked against the record as it happens. Today the
trace is the live view.

---

## Not built yet, and what to do today

| Not built | Today |
|---|---|
| `turneval compare`: the flip list, per-stage latency and cost, a failing exit code | Score both runs with different labels and read the tables side by side |
| Live call monitor | `tail -f` the trace while the call runs |
| Scenario dates relative to the day of the call | Move the fixture dates forward before they pass |
| A runbook view in the shared dashboard | `make runbook` |

## With ForwardEval

A scored run projects into ForwardEval's run format, so voice and coding
results render in one dashboard under one rule: checks decide, models explain.

```bash
PYTHONPATH=src .venv/bin/python -m turneval.cli score --label first-run \
  --export-forwardeval ../forwardeval/results/voice-first-run.json
```
