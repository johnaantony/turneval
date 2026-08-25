# The evaluation contract

A run file is the only artifact this project produces. Everything downstream
reads it: the CLI prints from it, a dashboard renders it, and the ForwardEval
projection rewrites it into a format an existing dashboard already understands.

This document is the field-by-field reference and, more importantly, the
argument for why the two harnesses share a shape at all.

## The envelope

```
Run
├── schema_version, domain, run_id, started_at, finished_at
├── config    RunConfig      what produced this run
├── summary   RunSummary     rollups, including the cross-layer matrix
└── sessions  SessionResult[]
    ├── deterministic   layer 1, from the world
    ├── behavior        layer 2, from the trace
    ├── outcome         layer 3, from a person
    ├── failure_tags    fixed taxonomy, with who assigned each one
    └── turns           the full turn-by-turn detail
```

## The three layers

| Layer | Question | Judge | Source |
|---|---|---|---|
| 1. Deterministic | Did the world change the way it was supposed to | Code, never a model | The booking record on disk vs `scenario.expect` |
| 2. Behavioral | What shape was the trajectory | Code plus a fixed taxonomy | The session trace |
| 3. Human acceptance | Did the caller get what they came for | A person, sampled | `data/outcomes.json` |

Layer 1 is a file read, not a transcript read. That distinction is the whole
design: `book_appointment` writes a record, and the check compares that record
to what the scenario said the caller wanted. No model participates in the
verdict. A model may be asked to *explain* a failure in prose; it never decides
whether the call succeeded.

Layer 3 cannot be computed and is not defaulted. A session with no recorded
outcome reports `None` and is excluded from the matrix, because assuming
success for unreviewed calls is exactly how an abandonment rate gets quietly
rounded to zero.

## The headline metric

```python
session.completed_but_abandoned == session.passed and not session.outcome.accepted
```

The tool call succeeded, every field matched, the record is on disk, and the
caller still did not get what they came for. Nothing in the latency breakdown,
the transcript, or the provider console can surface this. It appears only once
a human ratifies the outcome, and that gap is the argument for a third layer.

## Failure taxonomy

A tag may only come from `turneval.taxonomy.FailureTag`. Free-form failure
descriptions are how eval suites quietly stop being comparable across runs.

| Tag | Layer | Assigned by |
|---|---|---|
| `tool_call_failed` | 1 | code |
| `wrong_arguments` | 1 | code |
| `latency_budget_exceeded` | 1 | code |
| `dead_air` | 1 | code |
| `talked_over_user` | 1 | code |
| `premature_close` | 1 | code |
| `misheard` | 2 | code |
| `no_recovery` | 2 | code |
| `looped` | 2 | code |
| `abandoned_by_caller` | 3 | human |

`CODE_ASSIGNED_ONLY` is the subset a model may never propose, even as a
suggestion for a human to confirm.

## Latency

`TurnLatency` carries the four numbers LiveKit instruments separately:

```
response_delay_ms = end_of_utterance_delay
                  + transcription_delay
                  + llm_ttft
                  + tts_ttfb
```

It is a sum of components rather than one wall-clock measurement on purpose.
The point of instrumenting four numbers is attribution: a total you cannot
decompose tells an engineer nothing about who owns the regression. The property
returns `None` if any component is missing, because a partial sum would read as
a suspiciously fast turn.

`RESPONSE_BUDGET_MS` is 1500 and is a constant, not a config knob. Humans
turn-take at roughly 200ms gaps, so this is already generous, and a budget you
can tune per run is a budget you will tune until you pass.

Percentiles use nearest-rank, not interpolation. With the handful of turns a
run like this produces, interpolation would invent precision the sample size
cannot support.

## Cost

Voice spends in three currencies at once: LLM tokens, transcribed audio
minutes, and synthesized characters. `Pricing` has a slot for each plus a
platform per-minute rate.

**Cost figures are a floor.** The trace does not carry TTS character counts, so
TTS is excluded rather than estimated. `cost_per_success` is the number worth
reporting: cost per minute tells an operator nothing about whether the spend
produced anything.

Any cost field is `None` when pricing is unconfigured, which disables every
cost view downstream rather than showing a confident zero.

## Adapters

The scorer never imports a vendor SDK. It reads `RawSession`, which is plain
data. Everything that knows what LiveKit calls a field lives in
`turneval/adapters/livekit.py`.

Two consequences. Supporting another voice stack means writing one more
module, not touching the scoring layers. And a provider renaming a field
between SDK versions breaks exactly one file.

Field names in the LiveKit adapter were verified against the installed SDK, not
the docs:

```bash
.venv/bin/python -c "from livekit.agents import metrics; print(metrics.EOUMetrics.model_fields.keys())"
```

Against **livekit-agents 1.7.0**, two things had already moved relative to
mid-2026 writeups: `EOUMetrics` carries `speech_id` rather than `sequence_id`,
and `InterruptionMetrics` now exists with `num_interruptions`,
`num_backchannels`, and `detection_delay`. Interruption counting used to be
something you derived yourself.

## Projection to ForwardEval

`turneval.export.forwardeval.project()` returns a dict matching ForwardEval's
`RunResult`, so a voice run drops into that dashboard's `results/` directory
and renders in Overview, Run Comparison, and Token ROI with no new UI.

The correspondence that makes this legitimate rather than a format hack:

| Concept | ForwardEval (coding) | TurnEval (voice) |
|---|---|---|
| Verifiable outcome | Hidden test suite exits 0 | Booking record matches `expect` |
| Layer 2 | Rework loops, first-try pass | Reprompts, one-turn resolution |
| Layer 3 headline | `passedButRejected` | `passed_but_abandoned` |
| ROI denominator | Cost per solve | Cost per successful call |

Both headline cells mean the same thing: the system recorded success and the
human threw the work away.

### What the projection loses

One-way, and it drops the voice payload. Per-turn latency breakdowns, dead air,
overlap, and audio duration have no home in the coding schema. Read the native
run file for those.

Two smaller mismatches, stated plainly:

- ForwardEval's transcript union has no `user` variant, so caller turns are
  emitted as `system` items prefixed `CALLER:`. They render; they are not
  natively typed.
- `FailureTagName` in ForwardEval is a closed union of coding tags. Voice tags
  pass through as strings and display correctly, but a strict TypeScript
  consumer would need that union widened.

Neither is worth distorting the voice schema to avoid.
