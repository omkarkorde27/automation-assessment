# Computer-use automation for legacy back-office apps

An LLM figures out how to do a task on a UI with no API. The run is frozen into a
typed, versioned **capability artifact**. Production **replays that artifact with
no model in the decision loop** — and when it gets stuck, a person takes over the
*same live browser session* and hands it back.

> The model discovers. The artifact becomes a reusable capability. Deterministic
> replay is how an AI agent invokes it in production.

The target is `mockbank`, a deliberately hostile fixture: a frameset, element ids
that churn on every render, three different labelling styles on one form, eight
injectable faults, and two tenants running the same vendor product with different
labels.

---

## Setup

```bash
uv sync                          # install
uv run playwright install chromium
cp .env.example .env             # fixture credentials (fake) + optional API key
```

`.env` is gitignored. Only `cua discover` needs `ANTHROPIC_API_KEY`; **everything
else in this README runs offline.**

```bash
uv run pytest                    # 459 tests, no API key required
```

---

## The demo, in the order the story goes

Start the target app and leave it running:

```bash
uv run cua serve-app             # mockbank on :8800
```

### 1. What a calling agent sees

```bash
uv run cua catalog               # on a fresh clone this lists NOTHING. That is the point.
uv run cua catalog --include-drafts
uv run cua catalog --json        # the Messages API `tools` payload
```

Typed inputs, typed outputs, declared business outcomes — generated from the
artifact, never hand-written, and no model involved at call time.

**Only `approved` capabilities are listed** (R-M7-1). Both committed artifacts ship
as drafts, so the default catalog is empty and says so. A tool definition is an
*offer*: offering a draft means a model calls something nobody reviewed, and for a
read-only capability nothing would stop it. `--include-drafts` shows them, marked,
for review; a draft never reaches the `--json` payload. `cua approve` is the gate.

**One version per capability** (R-M7-3) — the highest that is approved. `cua replay
id@version` still addresses any version directly.

```bash
uv run cua describe member.lookup_balance --description "..."   # mints a new version
```

The description is what a production model reads when it decides whether to call a
capability, so the recorder refuses to write it (R-M7-2): it knows what was asked of
the *explorer*, not what the capability is *for*. `cua approve` refuses a capability
described by its own discovery goal. This is not hypothetical — see §7 of
`REPORT.md` for the run where the model followed one into a wasted replay.

### 1b. …and a real model calling one

```bash
uv run python scripts/watch_agent_call.py              # needs ANTHROPIC_API_KEY
uv run python scripts/watch_agent_call.py --bad-argument
uv run python scripts/watch_agent_call.py --headed
```

The whole last mile, with nothing staged: `cua catalog --json` is shelled out for
real, sent as `tools` to Sonnet with *"What's the savings balance for member
12345?"*, and the model's `tool_use` is resolved back to its artifact and handed to
the same replay engine everything else uses. The result comes back as a
`tool_result` the model answers from.

Two model calls, roughly a cent — Sonnet, because this demonstrates **tool
selection**, not agentic discovery. The model chooses *which* capability; it is not
in the loop while the capability runs, and the script re-walks the replay path's
import graph with `anthropic` loaded in the same process to show it.

### 2. Deterministic replay, and the four result variants

The exit code *is* the variant, so a caller branches without parsing anything:
`0` success · `1` failure · `2` business outcome · `3` escalated.

```bash
# success
uv run cua replay member.lookup_balance --params '{"member_id":"12345"}'

# business outcome — NOT a failure. "No such member" is the answer you asked for.
uv run cua replay member.lookup_balance --params '{"member_id":"99999"}'

# a genuine application error, mid-flow
uv run cua replay member.lookup_balance --params '{"member_id":"12345"}' \
    --inject error_500 --arm-at-step s3

# a state no capability anticipated -> a human is needed
uv run cua replay member.lookup_balance --params '{"member_id":"12345"}' \
    --inject permission_denied --arm-at-step s3

# silently recovered: the session drops mid-flow, the engine re-authenticates,
# rewinds to the last checkpoint that is still true, and finishes
uv run cua replay member.lookup_balance --params '{"member_id":"12345"}' \
    --inject session_timeout --arm-at-step s3
```

Every run writes `evidence/runs/<run_id>/`.

### 3. The same recording on a different institution

```bash
uv run cua replay member.lookup_balance --params '{"member_id":"12345"}' --tenant valley-cu
```

Valley Credit Union renamed the nav item and the member-id field and runs a
different consent modal. The artifact was recorded against `demo-cu` and is not
re-recorded: a tenant overlay supplies two locator replacements, and the run
prints which ones it applied.

### 4. Safety: an irreversible capability will not just run

```bash
# refused twice over: the artifact is a draft, and nobody asked for the write
uv run cua replay member.open_subaccount \
    --params '{"member_id":"12345","product_code":"HSA","initial_deposit":"50.00"}'

uv run cua approve member.open_subaccount --reason "reviewed the recorded flow"

# still refused — approval is only half of it
uv run cua replay member.open_subaccount \
    --params '{"member_id":"12345","product_code":"HSA","initial_deposit":"50.00"}'

# runs
uv run cua replay member.open_subaccount \
    --params '{"member_id":"12345","product_code":"HSA","initial_deposit":"50.00"}' \
    --confirm-irreversible

# this one really did write. Put the fixture back, so §2 still works afterwards.
curl -X POST localhost:8800/t/demo-cu/__control/reset
```

That last line is not ceremony. The write appends a sub-account to member
12345 — as a *Savings* row, whatever the product code — so re-running §2 against
a mutated fixture finds two Savings rows and correctly refuses to guess between
them (`EXTRACTION_FAILED`). That is the locator layer doing its job, but it is a
confusing thing to meet on your second command, so the demo cleans up after
itself. Restarting `cua serve-app` does the same thing.

### 5. Human takeover of the same live session

```bash
uv run python scripts/watch_takeover.py
```

A headed browser opens, the run hits a real permission wall mid-flow and **parks**
— it does not exit. The operator console comes up on <http://localhost:8801>:

1. open the intervention, press **Take control** (the lease moves to you);
2. drive the session from the console's node list — clicks go through the same
   `act()` the engine uses, so they are journaled, lease-checked and risk-derived;
3. press **Release & resume**.

The engine then **re-verifies the screen** before taking the wheel back. If you
release it somewhere the flow does not expect, it refuses loudly rather than
carrying on.

> You can also click directly in the browser window — it is the same session and
> the changes are real — but nothing intercepts input to Chromium, so those actions
> are **not journaled, not risk-checked, and cannot be promoted into the artifact**.
> The release is recorded as an unsanctioned change: the evidence pack will say the
> screen moved and will not be able to say how.

`uv run cua serve-console` serves the same console read-only over `evidence/`.
There is no takeover there, and the reason is the point: takeover needs the browser
that is still open, and that lives in the run's own process.

### 6. The real discovery run (needs an API key)

```bash
uv run cua dry-run               # exercises the loop's control logic. 0 API calls.

uv run cua discover --goal "Look up member 12345 and read their current savings balance" \
                    --capability-id member.lookup_balance
```

`discover` runs `dry-run` first by default and exits `4` rather than spend a real
run on a broken loop. Recorded artifacts land in `capabilities/`, evidence in
`evidence/runs/<id>/`. Each run prints measured tokens and an estimated cost, split
by which model did what.

---

## Visual inspection

```bash
uv run python scripts/watch_replay.py --tenant valley-cu        # merge + cross-tenant, headed
uv run python scripts/watch_replay.py --inject session_timeout  # recovery path
uv run python scripts/watch_takeover.py --headless --wait 6     # takeover smoke check
```

---

## Layout

```
mockbank/          the target app — a FIXTURE, not part of the system under test
src/cua/
  surfaces/        the seam: perceive and act, Playwright behind a protocol
  perception/      injected extractor -> UiNode / Observation
  locators/        LocatorBundle: a candidate ladder, unique match or escalate
  artifact/        the capability contract, its store, and the recorder
  profiles/        product profile + tenant overlay, merge, fingerprint/drift
  conditions/      the condition DSL — checkpoints as data, not callbacks
  catalog/         artifact -> tool definition, tool call -> replay (R-M7-1)
  replay/          the engine, the evaluation ladder, the four-variant result
  discovery/       the LLM loop (the only thing that imports anthropic)
  policy/          allowlist, risk tiers, redaction
  session/         auth + the control lease
  escalation/      intervention store, broker, operator console
  observability/   journal, evidence pack, screenshot annotation
config/policy.yaml the allowlist, enforced inside act()
profiles/          product profile + two tenant overlays
capabilities/      recorded artifacts
evidence/runs/     journals, observations, screenshots, results
```

## Reading the code

Four files carry the argument, in this order:

1. **`src/cua/surfaces/base.py`** — the seam, and why `act()` is the only place
   policy can live.
2. **`src/cua/artifact/schema.py`** — the contract between the model, the reviewer
   and the engine.
3. **`src/cua/replay/engine.py`** — the evaluation ladder and the four variants.
4. **`src/cua/session/control.py`** — the lease, including an honest note on what
   it does not defend against.

`REPORT.md` is the write-up: architecture, schema, determinism, multi-tenant,
escalation, safety, and what was deliberately cut.
