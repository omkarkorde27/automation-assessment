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
3. **Implement only the current milestone.** Then stop, summarize (every file created/changed + real test output), and **wait for the user to say "continue."** Approval of the summary is not permission to start the next one.

## STATUS

| | |
|---|---|
| **Current milestone** | **M2 — artifact schema + store, `AppProfile` schema + product→tenant merge + fingerprinting** (not started; awaiting "continue") |
| Completed | **M0** — uv scaffold, `.env`, `mockbank` fixture (both flows, 2 tenants, 8 faults, churned ids), CLI.<br>**M1** — `Surface` seam, JS observation extractor, `UiNode`/`Observation`, `LocatorBundle` + resolver, Playwright + desktop-stub adapters. 69 tests pass. |
| Next, on "continue" | M2 |

**Schema note (M1):** `AnchorRelative` gained a `same_column` relation and an optional `scope`, extending plan §3.2.1. A grid read ("the Balance cell of the Savings row") is a 2-D lookup the original candidate set could not express without falling back to a positional ordinal.

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
