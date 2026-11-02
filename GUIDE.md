# The field guide

**For the person who has to say "yes, ship it" to a customer.**

You do not need to be an engineer to read this. Commands live in one section near
the end; everything else is plain language.

---

## Who this is for

You are embedded with a customer. Maybe you are the engineer building their agent,
maybe you are the product manager who owns the outcome. Either way, the question
lands on you:

> "Is this agent good enough to put in front of our customers?"

And you have to answer it with something better than "the demo went well."

## The problem, honestly stated

An agent can sound perfect and still fail the customer. We know because it happened
to us on our very first real call.

Our own booking agent took a call. The caller asked for an appointment on
"Tuesday, September 1st." The agent answered, warmly and confidently:

> "Your appointment has been successfully booked for September 1st at 3 PM, Dana!"

Then it wrote the booking into the calendar **with the wrong year, three years in
the past**, a year in which September 1st was not even a Tuesday. The
conversation was flawless. The transcript would pass any review, human or AI.
The caller would have shown up to an appointment that did not exist.

That is the gap this tool closes. **Listening to the call tells you how it sounded.
It cannot tell you whether the work got done.**

## What it does

It answers three questions with evidence instead of opinion:

1. **Is this agent ready to go live?** After every test call, the tool ignores the
   conversation and goes looking at the thing the agent was supposed to change: the
   calendar entry, the updated record. If the record is right, the call passed. If
   the agent said "done" and the record is wrong or missing, the call failed, and
   the tool tells you exactly which detail was wrong. No AI grades another AI here.
   A check either passes or it does not.

2. **Should we switch to that new model?** New speech and language models ship
   constantly. Swap one in, run the same set of calls again, and get a plain
   answer: which calls got better, which got worse, what happened to speed, and
   what happened to cost. Not a leaderboard. A before-and-after for your agent on
   your calls. Today that means scoring both sets of calls and reading them side
   by side; a single command that lists every call that flipped is being built
   next.

3. **Was it worth the money?** Give it your rates and every call is priced: the
   model, the transcription, the voice. The number that matters is not cost per
   minute. It is **cost per call that actually resolved.** Cost per call that a
   person also signed off on is next.

## How it decides: three layers

| Layer | The question | Who answers it |
|---|---|---|
| 1 | Did the world change the way the caller asked? | A check that reads the record. Never an AI opinion. |
| 2 | What was the call like? Interruptions, silences, repeated questions, slow replies | Counters, straight from the call data |
| 3 | Did the caller actually walk away satisfied? | A person. Always a person. |

Layer 3 exists because layers 1 and 2 can both pass and the caller can still hang
up unhappy. A call that passed every check and was still rejected by a person is
the most important thing this tool can show you, because it means your checks are
measuring the wrong thing. The tool never fills in that judgment for you.

The same three layers also grade coding agents in the companion project,
**ForwardEval**, and both show up in one dashboard. If your team ships code with
agents and answers phones with agents, you read both on the same screen, scored by
the same rules: real checks decide, the AI only explains, every result is priced.

## What a result looks like

After a batch of calls you get one table. A real one, from our first day:

```
scenario                 L1    turns  dead   p95 ms  tags
happy-path               FAIL      8     5      n/a  wrong_arguments,dead_air
```

Reading it takes ten seconds: the booking call failed because a detail in the
record did not match what the caller asked for (`wrong_arguments`, that was the
wrong-year booking), and there were long silences (`dead_air`). Every failure gets
a tag from a short fixed list, so week over week the numbers mean the same thing.

## What this will not do

- It will not pretend an AI can grade a conversation. Where judgment is
  unavoidable (did the voice sound natural?), a person decides. An automated
  opinion earns a say only after it has been checked against human verdicts,
  which is how the coding half already treats its AI judges.
- It will not simulate a thousand fake callers. Your calls, your scenarios.
- It will not grade your agent's politeness and call that quality. The record on
  disk is quality. Politeness is layer 2.

## Try it

**Zero-install first look:** the coding half is live at
https://johnaantony.github.io/forwardeval/ . Open it, read the "Start here" panel,
and click into one failed task. That is the experience the voice half brings to
calls.

**Run a voice call yourself** (20 minutes, a few dollars; you need Python 3.12 and
uv, and a free LiveKit Cloud account, which is the platform this harness runs on):

```bash
make sync
cp .env.example .env    # paste your three LiveKit keys into it
make console SCENARIO=happy-path
```

Talk to the agent through your microphone. Book the appointment the scenario asks
for (`make scenarios` lists it: Tuesday, December 1st, 3 PM, Dana Whitfield). Press Ctrl+C
when it confirms. Then:

```bash
make score LABEL=my-first-run
```

That prints the table above for your call. Finally, record your own layer-3
verdict, because only you know whether you would have accepted that call:

```bash
PYTHONPATH=src .venv/bin/python -m turneval.cli outcome <session-id> --reject --note "why"
```

(Leave off `--reject` if you would have accepted it. The session id is printed in
the score table's run file and in `data/traces/`.)

## The eight scenarios

Seven of the eight are designed to break the agent on purpose: interruptions, long
silences, vague dates, a broken backend, a changed mind, an off-topic caller, and a
caller who hangs up the moment the agent says "booked." A suite of happy paths
tells you nothing. Every agent looks good until someone interrupts it.

## Words we use

| Word | Meaning |
|---|---|
| **Trace** | The call's written record: every turn, timing, and action, saved as it happens |
| **Oracle** | The check that reads the system the agent changed, instead of the transcript |
| **Turn** | One back-and-forth: the caller speaks, the agent answers |
| **Dead air** | Silence long enough that a caller wonders if the line dropped |
| **Flip list** | After a change: exactly which calls went from pass to fail, and the reverse |
| **Completed but abandoned** | The checks passed and the person still walked away. The headline metric. |
