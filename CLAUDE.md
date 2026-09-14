# CLAUDE.md

## What this is

The interface.ai take-home: a **computer-use automation system** for legacy bank back-office apps that have no API.

An LLM discovers how to do a task once → the run is frozen into a typed, versioned **capability artifact** → production **replays that artifact with no model in the decision loop** → a human can take over the *same live session* when it gets stuck.

> **The through-line (this is the grading rubric):**
> The model discovers. The artifact becomes a reusable capability. Deterministic replay is how the AI agent invokes it in production.

---

## Start here, every session

1. **Read `STATUS` below** — it names the current milestone and what is done.
2. **Read the approved plan:** `/Users/User/.claude/plans/precious-singing-rocket.md`. It is the source of truth for all design — schemas, error taxonomy, control model, milestones. **Do not redesign.** If something in it looks wrong, say so and ask; don't quietly deviate.
3. **Implement only the current milestone.** Then run the plan-diff gate below, stop, summarize (every file created/changed + real test output), and **wait for the user to say "continue."** Approval of the summary is not permission to start the next one.

### Plan-diff gate — mandatory before every milestone summary

Before writing a milestone summary, **open the plan section that milestone implements and diff it field by field against the code — the whole section, top to bottom.** Do not list deviations from memory; recall finds the ones you happened to think about and misses the rest, which is how a summary ends up claiming "the only deviation is X" when there were four.

Method, per milestone:
1. Re-read the plan section in full (Part 3.x / the milestone's row in Part 10).
2. Walk **every field, key, and enum value** in the plan's schema block against the implementation, in the plan's order.
3. Classify each difference: **structural** (shape/type/cardinality changed), **vocabulary** (names or enum members changed), **policy** (unspecified in the plan, decided during implementation — e.g. what the content hash covers), **additive** (new field, plan silent), or **declared-but-unconsumed** (below).
4. For every schema field carrying real semantics, **grep `src/` for a read site.** A field that is defined, serialized, and set in a committed profile is still unimplemented if nothing ever reads it. `grep -rn "field_name" src/` returning only the definition line is the signal.
5. Put the resulting table in the summary. "No deviations" is a valid result *only* after the walk.

**Declared-but-unconsumed** is its own category because it does not look like a deviation from any angle the other four cover: the plan says it, the schema has it, a profile sets it, the tests pass, and the behaviour is simply absent. `auth.reauth.resume_from` survived the entire M3 gate that way — the gate walked §3.4 and §3.5 for shape and vocabulary, found the mid-flow session handling present, and recorded it as *additive*. It was a gap. The engine re-ran the interrupted step instead of resuming from the last checkpoint, which worked only because the one test covering it armed the fault on a `navigate` step; every deeper failure point reported `LOCATOR_UNRESOLVED` for a healthy app. A schema is a claim about behaviour, and only a read site makes the claim true.

A deviation is not a problem — several have been improvements. Undisclosed deviation is the problem: it makes the plan stop describing the system, and every later milestone builds on a spec that quietly no longer matches.

## STATUS

| | |
|---|---|
| **Current milestone** | **M4 — complete**, awaiting "continue" before M5. |
| Completed | **M0** — uv scaffold, `.env`, `mockbank` fixture (both flows, 2 tenants, 8 faults, churned ids), CLI.<br>**M1** — `Surface` seam, JS observation extractor, `UiNode`/`Observation`, `LocatorBundle` + resolver, Playwright + desktop-stub adapters.<br>**M2** — `Condition` schema, `CapabilityArtifact` + `ArtifactStore`, `AppProfile` + product→tenant merge + `specialize()`, fingerprint/drift. Real profiles committed for both tenants.<br>**M3** — condition DSL + preflight, `ReplayEngine`, evaluation ladder, four-variant result contract, journal, `resume_from` session recovery. Hand-authored artifacts for both flows in `tests/factories.py`.<br>**M4** — discovery agent (manual Messages-API loop, 12 strict tools, image pruning, 4 loop-owned stopping conditions), recorder (locator derivation, R-M4-1, backtrack pruning, checkpoint synthesis), authoring review pass, redaction, irreversible guard, evidence pack, `cua discover`. **Both capabilities recorded from real Opus 5 runs.** **277 tests pass.** |
| Next, on "continue" | M5 — control lease, intervention store, operator console, takeover + resume |

**Before any real discovery run:** `uv run cua dry-run` exercises the loop's control logic (budgets, stopping conditions, dead-end detector, tool schemas, the guard being installed) against scripted observations — **0 API calls**. `discover` runs it first by default and exits 4 rather than spend a run on a broken loop. The dead-end bug that cost a real Opus run is caught by it in under a second.

**Model selection:** `CUA_MODEL` / `--model` for the discovery loop (default `claude-opus-5`; use a cheaper model while iterating). `CUA_REVIEWER_MODEL` / `--reviewer-model` for the authoring pass (default Haiku 4.5 — it fills a fixed schema from an already-successful transcript, and its output is validated against observed screens in code regardless). Every run prints measured tokens and an estimated cost, split by which model did what.

**Real LLM runs (M4):** `uv run cua discover --goal "..." --capability-id member.lookup_balance` (add `--allow-irreversible` for `member.open_subaccount`). Needs `ANTHROPIC_API_KEY`; everything else is offline. Recorded artifacts live in `capabilities/`, evidence in `evidence/runs/<id>/`.

**Visual inspection:** `uv run python scripts/watch_replay.py --tenant valley-cu` (M2 merge + cross-tenant replay, headed); `--inject error_500` / `--inject session_timeout` (M3 failure and recovery paths). `--arm-at-step` chooses where the fault lands; `--headless --no-pause --slow-mo 0` for a fast check.

**Named requirements carried into later milestones** (written into the plan, not just agreed in chat):
- **R-M3-1** — a `Condition` using an operator the surface cannot evaluate is rejected at **replay preflight** (`CAPABILITY_UNSUPPORTED`, before any action); the evaluator raises as a backstop and never returns a bool it cannot justify.
- **R-M3-2** — `ElementCondition.frame` unset = any frame; `UrlCondition.frame` unset = top-level page. Opposite on purpose; do not unify.
- **R-M4-1** — the recorder must never emit `{literal: V}` when `V` matches a declared goal input, and must refuse outright when `V` is data read off the screen.
- **R-M4-2 (evidence completeness)** — **every tool call the model makes, valid or invalid, must produce a journal entry.** Silent handling of any model output is never acceptable: §3.5's evidence guarantee is that somebody can reconstruct what happened without having been there, and a decision that was answered but not recorded breaks it. Journal once per call *before* dispatch — emitting per-branch means the branch nobody thought about is the one that goes unrecorded. This covers invalid node ids, unknown tools, missing required fields, turns that end without acting, and transport failures. Applies to M5's console (forwarded human actions) and anywhere else untrusted input is interpreted.
- **R-M6-2** — risk tier must be **re-derived inside `act()` after the locator resolves**, not taken from `action.risk`. Guards currently run pre-resolution, so the tier is trusted rather than enforced: a caller that mislabels an action walks past the choke point. One caller exists today and it is careful; M5 adds the console. Named tests in the plan.
- **R-M6-1** — a resumed flow must never silently repeat an irreversible step. **Default-deny ships with M3**: if the replay window `[resume_at, interrupted]` contains a `submit_irreversible` step, the run escalates with `IRREVERSIBLE_INTERRUPTED` instead of resuming. **M6 adds `Step.completion_witness`** — a declared `Condition` proving whether the write landed, so the engine can skip or re-run deterministically instead of paging. Idempotency keys were rejected (they need target-side support a no-API app cannot give); a target-independent "did it land" check was rejected as impossible in principle (the app is the only authority — two-generals).

**Schema notes (deviations from the plan):**
- **M1** — `AnchorRelative` gained a `same_column` relation and an optional `scope`. A grid read ("the Balance cell of the Savings row") is a 2-D lookup §3.2.1's candidates could only express as a positional ordinal.
- **M2** — telemetry moved to a **sidecar** (`capabilities/.telemetry/`) rather than living inside the artifact, so recording a replay never invalidates the content hash. `approval_state` stays in the artifact but is excluded from the hash: approving must not look like tampering.
- **M4** — R-M4-1 was extended beyond values to **locators, target ids, record-time snapshots and prose**. The plan only required it for `{literal: V}`, but a real run showed the same leak arriving through the back door: the result-row link is *named* "12345" and the cell beside it holds the customer's name, so the recorded locator pinned the capability to one customer and carried their name in it. Data-named controls are now matched by the value's SHAPE; anchors on data rows are dropped; extraction targets can never be identified by their own text.
- **M4** — verify-by-replay is **skipped for `writes_irreversible` capabilities**: replaying one to verify it means performing it again, the application correctly refuses the duplicate, and the artifact then looks broken when the recording was fine. Observed on a real run.
- **M4** — the authoring reviewer may not propose an outcome matching the **success** screen. The ladder checks outcomes before checkpoints, so such an outcome turns every successful run into a `BusinessOutcome`. The live model proposed exactly this.
- **M3** — `auth.reauth.resume_from: last_checkpoint` means the deepest recorded checkpoint **that is still true of the current screen**, not the last one that passed. Re-authentication lands on the app's post-login screen, so the recorded checkpoint is usually stale; checkpoints are re-evaluated deepest-first against the live screen and the flow re-enters after the first that holds, falling back to step 0. A step with **no** checkpoint is never a resume point — it verified nothing.

*Update this table at every milestone boundary.*

Milestones: **M0** scaffold+mockbank · **M1** surface+locators · **M2** artifact & profile schemas · **M3** replay engine+DSL · **M4** discovery agent+recorder (real LLM run) · **M5** escalation+console · **M6** safety hardening · **M7** evidence+README+REPORT.

---

## Invariants — never violate these without asking

1. **`replay/` never imports `anthropic`.** "No LLM in the decision loop" is enforced by an import-graph test, not by claim.
2. **The model never writes a selector.** It picks a numbered node from an `Observation`; the *recorder* derives the `LocatorBundle`.
3. **Locators resolve on a unique match only.** Ambiguity → escalate. Never positional tie-breaking, never "first match wins".
4. **A business outcome is not a failure.** `ReplayResult` has four variants — `success` / `business_outcome` / `escalated` / `failure` — and they are never collapsed. This is the trap the brief flags in its own glossary.
5. **Policy lives in `Surface.act()`**, the single choke point. Allowlist and risk-tier checks are never implemented as prompt instructions.
6. **Secrets and PII never reach artifacts, journals, screenshots, or prompts.** Profiles hold credential *references* (`env:…`), never values. Artifacts store value *shapes*, never values.
7. **Post-action evaluation ladder, in this order:** recoveries → business outcomes → hard failures → checkpoint. The order is load-bearing.
7b. **An irreversible step is never replayed by a resume.** If the rewind window contains one, escalate (R-M6-1). "We don't know whether we posted this" is an escalation, never a retry.
8. **The control lease is checked inside `act()`.** A non-holder physically cannot act on the session.
9. **Artifacts bind to a vendor product + version range, not a tenant.** Tenant specialization is an overlay, capped at depth 2.
10. **No queues, services, clusters, or multi-tenant plumbing.** The brief explicitly does not reward them. Document the seam instead.

---

## Locked decisions (settled — don't relitigate)

Python 3.11+ / `uv` · **async** Playwright · Pydantic v2 · `claude-opus-5` with adaptive thinking · **manual** Messages-API loop (not the beta Tool Runner) · JSON for artifacts, YAML for hand-edited config · single process, file-backed stores · local `mockbank` as the target surface.

Stretch goals are **deferred** until M7 lands.

## Layout

```
mockbank/            the target app — a FIXTURE, not part of the system under test
src/cua/             the system: surfaces, perception, locators, artifact, profiles,
                     conditions, discovery, replay, policy, session, escalation,
                     observability, catalog, cli
config/policy.yaml   allowlist + risk policy
profiles/            product profiles + tenant overlays
capabilities/        saved artifacts (.json)
evidence/runs/<id>/  journal.jsonl, screenshots, observations, result.json
tests/
```

## Commands

```bash
uv sync                      # install
uv run pytest                # full suite — must pass with NO API key
uv run cua serve-app         # mockbank on :8800
```

Only `cua discover` ever needs `ANTHROPIC_API_KEY` (in `.env`, gitignored). Everything else — tests, replay, console, both failure demos — runs offline. Playwright browsers: `uv run playwright install chromium` (needed from M1 on).

## Deliverables (paths fixed by the brief)

`/README.md` (setup + exact demo commands) · `/REPORT.md` (**seven prescribed headings, verbatim**: Architecture · Artifact schema · Determinism & error handling · Heterogeneity & multi-tenant · Escalation & handoff · Safety · Cuts) · `/evidence/` (artifact + discovery log + replay log + **one replay that hits an error state**).

Non-negotiable: at least one **genuine** LLM-driven discovery run against the live surface, with evidence in `/evidence/`.
