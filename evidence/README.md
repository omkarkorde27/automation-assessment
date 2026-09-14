# Evidence

What actually happened, written as the runs proceeded rather than assembled
afterwards — so a run that crashes still leaves its evidence.

Every run directory here has been through redaction — with one documented
exception, `agent_round_trip.txt`, which quotes the fixture member's name twice
on purpose to show the caller/model split it is demonstrating (the file says so
where it does it, and the value is fabricated). Observations are stored
post-`apply_sensitivity`; screenshots are masked before the bytes reach disk, and
a **persisted** screenshot is masked more aggressively than the live console view
(an operator holding the lease needs to read an account number off the record; a
file in an evidence pack outlives the incident and travels). Extracted values
appear here through their declared `OutputSpec.redact`, falling back to the
profile's `output_redaction_defaults` — the caller receives them whole.

## Layout of one run

```
evidence/runs/<run_id>/
  journal.jsonl              every decision, in order, with the reason behind it
  result.json                the typed result a caller receives (values redacted)
  steps/NNN_observation.json the AX snapshot each step was resolved against
  steps/NNN_<step>_post.png  the failing step, and the one before it
  failure/observation.json   what the locators were actually looking at
  failure/screen.png         full page, masked
  artifact.json              (discovery runs) what was recorded
  discovery.json             (discovery runs) the pruned transcript, redacted
```

Screenshots follow a policy: **every step for discovery**, where the pictures are
the evidence; **the failing step ±1 for replay**, because a replay that
photographs fifty screens nobody looks at is storage somebody has to justify.
The "−1" is why one frame is held in memory during a replay — you cannot go back
and photograph the screen a step started from.

## Discovery — the genuine LLM runs

Real Opus 5 runs against the live fixture. `discovery.json` is the pruned
transcript with the model's own stated reason on every tool call; `artifact.json`
is what the recorder derived from it.

| run | goal |
|---|---|
| `disc_cccbcae3f06c` | `member.lookup_balance` — **the shipped artifact was recorded from this run** (`provenance.recorded_from_run`), 11 steps observed, 7 pruned to the successful path |
| `disc_e199911014d8` | `member.open_subaccount` — **the shipped write artifact**, run with `--allow-irreversible`. `verified_by_replay: false`, deliberately: verifying an irreversible capability means performing it a second time, the app correctly refuses the duplicate, and the artifact then looks broken when the recording was fine |
| `disc_e8f25db672dc`, `disc_730f30655195` | other `lookup_balance` runs that finished |
| `disc_deb070ce99ed` | ended `stuck`. Kept on purpose: a loop that gave up is evidence too, and it is the run the dead-end detector was written for |

Both shipped artifacts name their source run in `provenance`, so a reviewer can
read the transcript that produced any recorded step.

**These runs were re-redacted after the fact, and it is worth knowing why.** They
were captured in M4, when the profile's `account_number` node rule matched
nothing (it anchored on a column header, and that relation matches a grid by its
panel header) and there was no rule for the member's name at all. So the stored
observations carried both. The *current* redactor was re-run over those files
once the rules were fixed: the decisions, the model's stated reasons, the step
order and the recorded artifacts are untouched, and only the pass that was wrong
was run again. `tests/test_redaction.py` now asserts the rules actually match
something, which is the check that was missing.

The raw transcript is deliberately **not** persisted: it contains observations of
regulated data. `discovery.json` carries the decisions, not the screens.

## Replay — the four result variants, plus the paths that are not variants

Regenerate any of these with the commands in the root `README.md`.

| run | result | what it shows |
|---|---|---|
| `run_82617c55b4bb` | `success` | the ordinary path, demo-cu |
| `run_68b670b3c48a` | `business_outcome` `MEMBER_NOT_FOUND` | **not a failure.** A declared, detected, typed answer the caller asked for |
| `run_482df3966f5c` | `failure` `SURFACE_ERROR` | the app's 500 page mid-flow, matched against a profile-declared hard failure. Carries the `failure/` pack |
| `run_d2c1d434f225` | `escalated` `PERMISSION_REQUIRED` | a state no capability anticipated → a person is needed. `stuck.detected` names the pattern (`permission_wall`), and the result carries `reason_class`, `at_step` and a `human_message` written for the person who picks it up — plus a `failure/` pack: masked screenshot, observation, Playwright trace. No intervention was *filed*: `cua replay` has no console attached, so `intervention_id` is null. Filing and parking is the takeover demo |
| `run_4dc66cf83e8c` | `success` | **silently recovered.** The session dropped at `s3`; the engine re-authenticated, rewound to the last checkpoint still true of the screen, and finished. seven step traces for a four-step flow — `s1 s2 s3 s1 s2 s3 s4` — is the rewind, visible |
| `run_35f71d735c9a` | `success` | **cross-tenant.** The same artifact, recorded against demo-cu, replayed on valley-cu through two locator overrides |
| `run_92e276942bdc` | `escalated` `IRREVERSIBLE_NOT_AUTHORIZED` | the write flow refused: the artifact is a draft and nobody passed explicit intent |
| `run_7a30a00f0a27` | `success` | the same write flow, once approved and explicitly confirmed. `new_account_number` is `*****0001` here and whole in the caller's hands |

## The agent-facing interface — a real model invoking a capability

`agent_round_trip.txt` is the transcript of
`uv run python scripts/watch_agent_call.py`: `cua catalog --json` shelled out for
real, sent as `tools` to claude-sonnet-5, and the model's `tool_use` dispatched to
the M3 replay engine. Two Sonnet calls, about a cent. It carries both halves — a
`success` and a `business_outcome` returned as tool results the model reasons over,
and (below the separator) an argument the artifact refuses, coming back as
`PARAM_INVALID` in 34 ms with no browser navigation.

The run uses `member.lookup_balance@1.1.0`. Version 1.0.0 shipped the discovery
goal as its description, and on every run the model read *"Search for member
99999, which does not exist…"* and spent an entire replay on it before answering
the question asked. R-M7-2 fixed that and the transcript shows the result: one
tool call instead of two. Both 1.0.0 artifacts are still on disk; the recording
in 1.1.0 is byte-for-byte what the Opus run produced, and only the caller-facing
prose changed.

One thing in it is still uncomfortable and stated rather than hidden:
`member_name` reaches the model as `<redacted>` while the engine read the real
name. A tool result is a prompt on the caller's next turn, so it is rendered
through the declared redaction. See `REPORT.md` section 7.

## Reading a journal

```bash
jq -r '"\(.kind)\t\(.step // .intervention // "")"' evidence/runs/<id>/journal.jsonl
```

The ordering is load-bearing. After every action the engine evaluates, in this
order: **recoveries → business outcomes → hard failures → checkpoint.** A journal
is how you check it actually ran in that order, which is the only way to know.
