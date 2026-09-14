# Report

A computer-use automation system for legacy bank back-office apps with no API.
An LLM discovers a flow once, the run is frozen into a typed versioned artifact,
and production replays that artifact with **no model in the decision loop** — with
a human able to take over the same live session when it gets stuck.

`README.md` has the setup and the exact demo commands. This is the argument.

---

## 1. Architecture

Four ideas carry the system, and everything else is consequence.

**One seam: `Surface`.** Perceiving and acting live behind a protocol
(`surfaces/base.py`). Above it — artifacts, locators, the condition DSL, replay,
policy, escalation — nothing knows what Playwright is. `desktop_stub.py`
implements the protocol without a body so the seam can be inspected rather than
described.

**One contract: the capability artifact.** `artifact/schema.py` is the interface
between three audiences at once: a calling agent that needs typed I/O and a
result it can branch on, a human reviewer who needs to judge whether this may run
unattended, and the replay engine that needs to execute with no model available.

**One choke point: `Surface.act()`.** Allowlist, risk tier and the control lease
are all enforced there. Registration is part of the protocol, so policy can only
ever be attached to the choke point — a caller that checked a rule itself would
be creating a second enforcement point the next caller forgets, which is how a
guardrail becomes a convention.

**One result union.** `success | business_outcome | escalated | failure`, never
collapsed.

```
   discovery/          →   artifact/         →   replay/
   the only package        the frozen            no anthropic import,
   importing anthropic     capability            enforced by a test
        │                       │                     │
        └── perception/ ── locators/ ── conditions/ ──┘
                    all shared, none model-aware

   policy/  session/  escalation/     observability/
   enforced inside act()              journal + evidence pack
```

**Perception is AX-shaped, not DOM-shaped.** An injected extractor produces
`UiNode`s — role, accessible name, value, frame path, anchors — the intersection
of what a web page and a desktop accessibility tree can both offer. That choice
is what makes the desktop story a new adapter rather than a new system.

**The model never writes a selector.** It picks a numbered node from an
`Observation`; the *recorder* derives the `LocatorBundle`. A selector written by a
model is a selector nobody reviewed.

---

## 2. Artifact schema

A capability is a `CapabilityMeta` + `Binding` + typed `inputs`/`outputs` +
ordered `steps` + `extractions` + `outcomes` + `recoveries` + a `success`
condition, sealed with a content hash.

Three decisions are load-bearing.

**`outcomes[]` — declared business outcomes with detection conditions.** "No such
member" is an answer the caller asked for, not a crash. Because outcomes are
declared data evaluated *before* the step's checkpoint, it is structurally
impossible for one to surface as a failure. This is the trap the brief names in
its own glossary, and the schema is where it is closed.

**`binding` binds to a vendor product, not a tenant.** One recording serves every
institution running that product; per-tenant difference is an overlay.
Re-recording per tenant does not scale to hundreds of institutions, so the schema
refuses to encode a tenant as identity — `recorded_against` is provenance, and
saying which of those two a field is turned out to matter (see §7).

**Locators are bundles, not selectors.** An ordered candidate ladder —
role+name, label-for, anchor-relative ("the Balance cell of the Savings row"),
scoped ordinal, normalised-text pattern — each with recorded reasoning and a
stability score, and a `unique_required` match policy. Ambiguity escalates. There
is no positional tie-breaking and no first-match-wins, because on a screen whose
element ids change on every render, "the first one" is not a thing.

`ValueSource` is `param | literal | credential`, exactly one. The guarantee that a
recorded run does not bake in one member's data cannot live in the schema —
nothing in a type can tell a constant from a leaked parameter — so it lives at
record time (R-M4-1) and is tested there.

The content hash covers the executable flow and excludes `approval_state`,
`created_at` and `provenance`: approving a capability must not look like tampering
with one. Telemetry lives in a sidecar for the same reason — recording a replay
must not invalidate the artifact.

---

## 3. Determinism & error handling

**Replay imports no model client, and that is a test, not a claim.**
`test_replay_has_no_llm.py` walks the import graph of every package reachable from
`replay/` — 38 modules across 11 packages — and also spawns a subprocess to catch
a transitive import arriving through a package `__init__`. It caught one: an
annotation helper living in `discovery/` had to move to `observability/` when the
operator console started rendering screens.

**The evaluation ladder, after every action, in this order:**

```
recoveries  →  business outcomes  →  hard failures  →  checkpoint
```

The order is the design. Recoveries first, because a marketing interstitial
covering the screen is not a failed checkpoint — it is a known obstruction with a
bounded fix. Outcomes before failures, because "already exists" is the
institution answering, not the automation breaking. Checkpoint last, because it
is the weakest signal: it says the screen looks right, which is not the same as
being right.

**Four variants, never collapsed**, and the CLI exit code *is* the variant so a
caller branches without parsing: `0` success, `1` failure, `2` business outcome,
`3` escalated.

**Recovery is a step policy, not a result.** A dismissed interstitial or a retried
transient load is journaled and moved past; it becomes a failure only when the
attempt budget is exhausted.

**Session recovery rewinds to a checkpoint that is still true.** `resume_from:
last_checkpoint` does not mean the last checkpoint that passed — re-authentication
lands on the app's post-login screen, so the recorded checkpoint is usually stale.
Checkpoints are re-evaluated deepest-first against the live screen and the flow
re-enters after the first that still holds. A step with no checkpoint is never a
resume point: it verified nothing.

`evidence/runs/run_4dc66cf83e8c` shows this working — seven step traces for a
four-step flow, `s1 s2 s3 s1 s2 s3 s4`, and a `success` at the end.

**An irreversible step is never replayed by a resume** (R-M6-1). If the rewind
window contains one, the run escalates with `IRREVERSIBLE_INTERRUPTED` rather than
guess. Where an artifact can declare a `completion_witness` — an observable
consequence of the write — the engine observes instead of guessing, and continues
unattended. Two honest limits, both measured rather than assumed:

- The witness is evaluated at the resume point **and polled forward before every
  replayed step** until its own step is reached, because re-authentication in this
  application lands on the dashboard, where the plan's own example witness reads
  false for the wrong reason. The screens that can answer a witness are the ones
  the flow replays *through*.
- A witness makes *skipping* the step safe. It does not make the rest of the flow
  possible: if the screen the write produced carried data later steps needed,
  those steps fail, and that failure is correct.

**Preflight.** Every condition in the artifact and the profile is checked against
the surface's declared capabilities before the first action. Discovering that a
URL checkpoint is unevaluable *after* an irreversible submit is the failure mode
this prevents.

---

## 4. Heterogeneity & multi-tenant

**Surface extension.** Legacy web needed no special work — the fixture *is* the
hostile case. A desktop app is a new `Surface` adapter emitting the same
`UiNode`s; artifact, resolver, DSL, replay, policy and console are unchanged.
Candidates 1–5 of the locator ladder all have desktop analogues; only `url`
conditions degrade, which is why `SurfaceCapabilities.has_url` exists and the
evaluator refuses rather than silently passing.

**Multi-tenant is an overlay, capped at depth two.** A product profile declares
what is true of the *application* — session-expiry detector, login recipe, generic
error banner, marketing interstitial, which regions hold regulated data, drift
canaries. A tenant overlay supplies `base_url`, `label_overrides`,
`locator_overrides`, `param_defaults`, `disabled_recoveries`. Resolution merges
product → tenant → effective artifact at load time and **journals the
resolution**, so specialization is traceable rather than invisible.

Keyed lists merge by id: same id replaces, new id appends. `locator_overrides`
replace a whole bundle rather than patching candidates, so an override is
reviewable as one unit and cannot half-apply.

**Demonstrated, not described.** `evidence/runs/run_35f71d735c9a` is the artifact
recorded from a real Opus run against `demo-cu`, replaying on `valley-cu` — which
renames the nav item and the member-id field and runs a different consent modal —
through two locator overrides and no re-recording.

Getting that to work turned up the most interesting bug in the project, and it is
in §7.

**Drift.** Two signals: per-tenant replay telemetry (checkpoint failure rate, and
*which* candidate resolved — a flow silently degrading to weaker candidates is the
early warning), and a `surface_fingerprint` canary over the sorted role+name set
of key screens. Divergence demotes the binding out of `approved`, which means
unattended replay stops and a human looks.

---

## 5. Escalation & handoff

**The control lease is enforced, not advisory.**

```
AUTOMATION_OWNED → PAUSED_PENDING_HUMAN → HUMAN_OWNED → RESUMING → AUTOMATION_OWNED
                                                      → ABANDONED (terminal)
```

Checked inside `act()`. A non-holder physically cannot act on the session. Actor
identity is **ambient** — a `ContextVar` set by `lease.acting_as(actor)`, per
asyncio task — rather than a field on `Action`, because an actor field would make
identity a caller-supplied claim, which is exactly what the risk model stopped
doing. *Honest limit:* this is enforcement against callers who are wrong, not
against callers who are hostile; hostile needs the lease behind a boundary the
caller cannot reach.

**Seven stuck triggers route to escalation.** The rule for whether the console
offers a steering wheel is mechanical rather than per-trigger: `takeover=True`
**iff the run is still alive and parked**. A trigger that ends the run files an
intervention with `takeover=False` — an operator sees it, but the console does not
advertise a wheel attached to nothing.

**The run parks in place.** `Escalated` is a terminal variant, and the tempting
implementation is to return it and let a later command re-attach. That loses the
only property that makes takeover interesting: it is the *same live session*. A
new process gets a new browser on a new login, and "the human continues where the
robot stopped" has quietly become "the human starts over". So the engine files an
intervention and awaits an `asyncio.Event`; the console, embedded in the same
process on the same loop, resolves it.

**The console forwards semantic node picks, never coordinates.** A coordinate has
no node, so its risk cannot be re-derived and it can never become a recorded step
— it is precisely the bypass the choke point exists to prevent. Locators for human
picks reuse the *recorder's* `derive_bundle`, which is what makes "promote a
human's fix into the artifact" a copy rather than a translation.

**Handback is verified, never trusted** (R-M5-1). The operator saying the screen
is ready is not evidence: they were fixing a broken flow under time pressure, on a
screen the automation already misread once. The engine re-checks before reclaiming
the lease, bounded to {same step, next step} — the target step's precondition, else
the previous step's checkpoint, else the flow's success checkpoint — and fails
loudly rather than hunting for a step that happens to match. It tolerates a
settling screen, because the operator's last click and their release are unordered
events, and the bound is what keeps a genuine desync failing.

**A declared bad screen outranks a passing checkpoint** (R-M6-3). Found by
running it: `member.lookup_balance`'s success checkpoint is URL-only, and this
application's authorization wall is served at the *same URL* as the member record.
An operator who took control, did nothing, and pressed "I completed this step" got
`handback.verified` on a permission error, and the run failed a step later at
extraction pointing at the wrong thing. The fix is precedence, not a longer
checkpoint: a profile-declared stuck pattern is *positive evidence of the wrong
state*, a passing condition is only the *absence of evidence* of it, and the two
are not equal weight.

**Direct-window driving is detected, not silently trusted** (R-M6-4). The plan
offers the operator the headed browser as well as the console, and only the
console goes through `act()`. A click in the window reaches the page through the
OS: no journal entry, no derived tier, no lease check. Found the way such things
are found — an operator drove the window, fixed the screen, and the run reported
zero human actions. It stays, because it is what an operator uses when the action
vocabulary cannot express the fix and removing it would make the console the
ceiling on what a person may repair. But the broker fingerprints the screen at
take and at release: changed screen with nothing forwarded is journaled as
`handback.unsanctioned_change` and flagged on the intervention. This detects
**that** something happened off-channel, never **what** — the guarantee is evidence
completeness, not containment.

---

## 6. Safety

**Policy lives in `act()`.** Not in a prompt. A guardrail expressed as an
instruction to a model is a request.

**The allowlist** (`config/policy.yaml`) declares allowed path patterns, denied
path patterns, permitted action types and a navigation budget. Origins are
anchored to the tenant's own resolved `base_url` rather than listed in the file: a
committed policy cannot name every institution's host, and one that tried would be
widened to a wildcard by the third onboarding. Per-capability blocks **narrow
only** — `default AND capability` — because an overlay that can widen makes the
default advisory.

It is enforced twice, and the second time is the honest half: the pre-action guard
sees the navigations automation *asks* for, but a click is a click and the app
decides where it lands, so the engine re-checks the URL after every step. What
remains uncovered is a redirect chain that returns before the next observation —
a statement about what this design can see, not a gap it forgot.

**Five risk tiers**, derived inside `act()` **after the locator resolves**
(R-M6-2). A pre-resolution guard has a bundle, not a node, so the only tier
available to it is the one the caller wrote down — and a choke point that reads
the caller's own description of how dangerous its action is has not checked
anything. "The button in this row" is neither reversible nor irreversible until
you know which button it is. `action.risk` survives as a *declaration*: journaled
beside the derived tier with any mismatch flagged, and never the thing policy
trusts.

**An irreversible capability replays only when it is `approved` AND the caller
passed `confirm_irreversible`.** Either alone is insufficient: an approved artifact
should not fire because a caller invoked it by mistake, and an emphatic caller
should not be able to run a flow nobody reviewed. One boolean, and it is the
difference between an agent that decided to post a transaction and one that was
told to. Both refusals are in `evidence/` (`run_92e276942bdc`), as is the run that
was allowed (`run_7a30a00f0a27`).

During discovery, irreversible actions are blocked outright unless the run passes
`--allow-irreversible`, and the refusal doubles as a genuine escalation trigger.
In a bank, an unattended agent posting a transaction on a first exploratory run is
the nightmare scenario.

**A human lease holder may go further than the agent.** Takeover exists so a
person can do what automation may not; the irreversible guard stands down for
them, journaled as `policy.human_override` with their name on it. They do not
leave the allowlist — that guard is not lease-aware and never should be.

**Redaction at every egress.** Two mechanisms, in order of trustworthiness: node
rules from the app profile (the institution's own screens declare which fields
hold what, so masking does not depend on a value happening to look recognisable),
and regex detectors as a net for values that surface somewhere nobody declared.
Artifacts store value **shapes**, never values. Profiles hold credential
*references*, never values, and that is validated on load because profiles are
committed to git.

Two distinctions took work to get right:

- **Persistence, not the return value.** An agent that asked for an account number
  needs the account number. `OutputSpec.redact` governs the copy that lands in the
  journal and in `result.json`, so a `Success` carries the value twice: whole for
  the caller, rendered for disk.
- **The live view and the archive are different audiences.** An operator holding
  the lease needs to read an account number off the record in front of them; a PNG
  in an evidence pack outlives the incident and travels. Persisted screenshots mask
  every classified region regardless of what the per-rule mask list says.

*Honest limit:* regex redaction is best-effort by construction. The real guarantee
comes from the profile declaring sensitive regions and from never persisting raw
DOM. The raw discovery transcript is deliberately not stored at all — it contains
observations of regulated data — which is why `discovery.json` carries decisions
rather than screens.

---

## 7. Cuts

**Deliberate, and I would make them again.**

- **The operator console is minimal** — a screenshot poll and forwarded input, not
  WebRTC co-browsing. The brief puts that out of scope. The *mechanism* is real
  (same session, same actuator, enforced lease, verified handback); the UI is a
  mock.
- **Desktop adapter: protocol only.** `desktop_stub.py` implements the seam
  without a body. Building a second real adapter would demonstrate less than the
  seam already does and cost more than everything else combined.
- **No queues, services, clusters or multi-tenant plumbing.** Explicitly not
  rewarded. The intervention store is a directory of JSON files; in production it
  is a queue message into whatever routes work to operators, and the *shape of the
  request* is the real interface. The seam is documented where the mock lives.
- **No auth/SSO.** The fixture's session is a cookie. Session-timeout *recovery* is
  implemented anyway, because that is an error-handling requirement rather than an
  auth one.
- **Five of six stretch goals deferred; one was built.** Cross-tenant overrides and
  the multi-run telemetry fields were already part of the core, so what remained
  genuinely optional was confidence scoring, richer drift detection, a second
  surface adapter, artifact diffing — and the agent-facing capability interface.
  I built the last one, for the reason in the next block. The others are additions
  to a story that already runs end to end.

**The one stretch goal I did build: the agent-facing capability interface.**

The project's own thesis is *"the model discovers, the artifact becomes a reusable
capability, deterministic replay is how an AI agent invokes it in production."* Every
clause but the last had an executable demonstration. `cua catalog` could *print*
tool definitions and nothing anywhere turned a tool **call** back into a replay, so
the catalog was a menu with no kitchen. This is the only stretch goal that closes
the argument rather than extending it, which is why it was worth one milestone.

`src/cua/catalog/tools.py` — the module the architecture named and nobody built:

- `build_catalog()` decides what a calling agent is offered;
- `ToolCatalog.resolve()` maps a tool name from a model's `tool_use` block back to
  the artifact it was generated from;
- `tool_result_block()` renders a `ReplayResult` as something the model reads next
  turn.

There is deliberately no browser, profile resolution or engine construction here.
Dispatch belongs to whoever owns the session — otherwise a run an agent started
could not be handed to a human, because the session would belong to a library.

**The approval decision (R-M7-1): a catalogued capability is an invocable one.**
`cua catalog` now lists only `approved` capabilities. `--include-drafts` shows the
rest, marked, and a draft is **never** emitted into the `--json` tools payload.

A tool definition is an *offer*. Listing a draft tells a model "you may call this",
and the call then meets a gate the definition never mentioned. The two ways that
goes wrong are not symmetrical. A `writes_irreversible` draft fails loudly as
`IRREVERSIBLE_NOT_AUTHORIZED` — annoying, and the model can do nothing about it,
because approval is a human act it cannot perform. A `read_only` draft **succeeds**,
which is worse: unreviewed automation runs against the back-office application and
the only evidence the review gate was skipped is a field nobody read. So the gate
moves to where disagreement is cheap, and `approval_state` stops being a label. It
is a narrowing of what an *agent* is told exists, not a new refusal — `cua replay`
still runs a read-only draft when asked directly, which is the reviewer's own path.

**The four variants survive the trip.** `is_error` on a tool result is set for
`failed` only. A `business_outcome` returned as an error is the brief's own trap
arriving one layer later than usual, and it reads to the model as "you did something
wrong" — which for "that member does not exist" is a lie. Each payload also carries
a `guidance` line naming the variant, because a model handed a bare JSON blob will
guess. `Success` renders from `evidence_outputs` rather than `outputs`: a tool result
**is** a prompt on the caller's next turn, so the redaction rule applies to it.

**The round trip, with nothing staged** — `uv run python scripts/watch_agent_call.py`
shells out to `cua catalog --json`, sends the result as `tools` on a real Messages
API call, dispatches the model's `tool_use` to the M3 engine, and returns the result
as a `tool_result`. Two Sonnet calls, ~$0.01; Sonnet rather than Opus because this
demonstrates tool selection, not agentic discovery. On a real run the model asked
*"What's the savings balance for member 12345?"*, chose `member_lookup_balance`, and
got `{"savings_balance": "4210.75"}` back through the engine in a single call. With
`--bad-argument`
it declines before calling — the `^\d{5}$` is in the schema it was handed — and the
forged call it would have had to make comes back as `PARAM_INVALID` in 34 ms with no
browser navigation. Defence in depth, and the second layer does not depend on the
model's judgement.

The script also re-walks the replay path's import graph **with `anthropic` loaded in
the same process**. The suite's clean-interpreter test proves the modules do not
import a client; it cannot prove the more interesting thing, which is that a model
choosing the capability does not put a model inside it.

**Not chosen — rejected, with reasons.**

- **Idempotency keys** for the irreversible-replay problem. They require the target
  to honour them, and the premise is an application with no API: there is no field
  to carry a key and no server logic to deduplicate against it. The one workaround
  — stamping a token into a writable free-text field — depends on such a field
  existing and writes synthetic data into a customer record to serve the
  automation's bookkeeping.
- **A "did the write land" check that does not consult the target.** Impossible in
  principle, not merely hard: the application is the only authority. This is the
  two-generals problem. What local bookkeeping *can* do is record that an
  irreversible action was dispatched and its checkpoint never observed, which
  converts an unknown-unknown into a known-unknown — and a known-unknown is exactly
  what an escalation carries to a person.

**Known limitations, stated rather than hidden.**

- **The capability description was the discovery goal, verbatim — found by the
  agent interface, and fixed.** This is the one defect the stretch goal earned its
  place by catching, so it is worth stating in full. One of my discovery goals
  contained an exploratory instruction ("search for member 99999, which does not
  exist, so you can see how it reports that"), and the recorder used the goal as
  the capability's description. That string is what a production model reads when
  it decides whether and how to call the capability. On **every** round-trip run
  the model followed it: it called the tool twice, spending an entire replay
  looking up member 99999 before answering the question actually asked. On one run
  it named the text explicitly as an embedded instruction in tool metadata that it
  declined to obey — the correct instinct, and still a wasted turn.

  A discovery goal is written to steer the model that will **explore**. A
  description is read by the agent that will **invoke**. The goal was already
  preserved correctly in `provenance.discovery_goal`, so the description was a
  second, lossy copy of it serving the wrong audience. Fixed at four points, none
  of them a warning in a doc:

  1. the recorder writes `description=""` — it knows what was asked of the
     explorer, not what the capability is *for*;
  2. the authoring reviewer's `title` and `description` are now **applied**. They
     were declared on `AuthoringReview`, filled by the reviewer every run, and
     discarded — a declared-but-unconsumed field my own M4 review missed;
  3. `cua describe` authors the prose by **minting a new version**. The
     description is part of the contract a caller depends on, so changing it
     changes the content hash; editing the file by hand would look exactly like
     the tampering the hash exists to reveal. The flow is untouched, and the new
     version starts as a draft, because a new contract is a new review;
  4. `cua approve` **refuses** a capability with no description or one that
     restates its goal. Together with R-M7-1 — only approved capabilities are
     offered — a goal-as-description can no longer reach a calling agent at all.

  The guard behind (2) and (4) targets **explorer-directed phrasing** rather than
  similarity to the goal. My first cut compared word sets at 0.75 and rejected a
  perfectly good hand-written description of `member.open_subaccount`, whose goal
  happened to be well phrased: it was measuring subject matter and being read as
  authorship. A good goal and a good description of the same operation *should*
  share their content words.

  Both capabilities ship at **1.1.0**. The recording — steps, locators,
  checkpoints, outcomes, provenance — is exactly what the Opus runs produced;
  `1.0.0` is still on disk. **Before:** two tool calls and a wasted replay.
  **After:** one call, `{"member_id": "12345"}`, straight to the answer.
- **`member.open_subaccount` ships `verified_by_replay: false`**, on purpose:
  verifying an irreversible capability means performing it a second time, the
  application correctly refuses the duplicate, and the artifact then looks broken
  when the recording was fine. Observed on a real run.
- **Two committed artifacts were corrected in place after recording.** Both were
  pinned to the tenant they were learned from — see below. The flow, locators,
  checkpoints and outcomes are exactly as recorded; one recorder bug was fixed and
  the same canonicalization was applied to the bytes it had already produced,
  because the discovery transcript is a redacted summary by design and cannot be
  replayed through the fixed recorder without spending another real run.

**The bug worth reporting.** `binding` said these artifacts bound to a vendor
product and not a tenant. The schema enforced it. Both artifacts were pinned to
`demo-cu` anyway, through a door nobody was watching: the recorder built URL
checkpoints from the observed address, and the observed address begins
`/t/demo-cu`. Eleven checkpoints across two capabilities quietly contradicted the
binding directly above them.

Nothing caught it because two half-covered axes read as one covered one — every
test that replayed a *recorded* artifact replayed it on the tenant it came from,
and every test that replayed cross-tenant used the *hand-authored* artifact, whose
conditions were written by hand and were correct. The action side of the recorder
had always stripped the deployment (`_templatize`); the condition side had not.
`tests/test_capability_portability.py` now crosses the axes on purpose: the
artifacts that ship, on the tenant they did not come from.

That is the general shape of what this project taught me. Every defect worth
reporting here — the inert sensitivity rule that matched nothing, the handback
that verified itself on an error page, the operator actions that went unrecorded,
the account number in `result.json` — looked correct in the code and was only
visible by following one real value, or one real operator, all the way through.
