"""Deterministic replay -- the production execution path.

Given an artifact and parameters, execute the flow with **no model in the
decision loop**. Everything that decides anything here is data from the artifact
or the resolved profile. There is no import path from this module to `anthropic`,
and a test asserts that, because "the LLM is not involved" is a claim worth
making structurally rather than in prose.

The load-bearing piece is the post-action evaluation ladder, and its ORDER:

    1. recoveries        a modal may be covering everything else, so dismissable
                         and retriable conditions are checked first. Bounded,
                         then re-observe and re-enter the ladder.
    2. business outcomes checked BEFORE checkpoints, so "no records found" is
                         reported as MEMBER_NOT_FOUND and can never surface as
                         CHECKPOINT_FAILED. This ordering is the entire defence
                         against the mistake the brief names in its glossary.
    3. hard failures     the app's own error page. Terminal, never retried.
    4. checkpoint        pass -> next step; fail -> bounded wait-and-re-observe,
                         then CHECKPOINT_FAILED.

Run that order the other way and a not-found result looks like a broken
locator, an operator gets paged for a working system, and the caller is told the
automation is down when the honest answer was "that member does not exist".
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field, replace
from typing import Any

from ..artifact.schema import (
    ApprovalState,
    CapabilityArtifact,
    Extraction,
    OutcomeSpec,
    ParamValidationError,
    ParseSpec,
    RecoverySpec,
    Step,
    StepAction,
)
from ..conditions.dsl import EvalContext, UnsupportedOperator, evaluate, unsupported
from ..conditions.model import Condition, describe
from ..locators.model import LocatorBundle
from ..locators.resolve import LocatorError, resolve_node
from ..escalation.requests import (
    Disposition,
    InterventionRequest,
    Resolution,
)
from ..observability.annotate import annotate
from ..observability.journal import Journal, MemoryJournal
from ..perception.model import Observation
# Policy is imported by the engine on purpose: the guards have to be REGISTERED
# on the surface by something that knows which capability is running, and the
# engine is that something. Neither module can reach a model -- the import-graph
# test scans both.
from ..policy.allowlist import AllowlistGuard, AllowlistRules
from ..policy.redaction import (
    apply_sensitivity, redact_output, redaction_for, screenshot_allowed,
    screenshot_masks, scrub_text,
)
from ..policy.risk import IrreversibleActionGuard
from ..profiles.resolve import ResolvedProfile
from ..session.control import AUTOMATION, ControlLease
from ..surfaces.base import Action, ActionType, PolicyDenied, RiskTier, Surface
from .result import (
    to_dict,
    BusinessOutcome,
    Escalated,
    Failure,
    FailureClass,
    ReplayResult,
    StepTrace,
    Success,
)


# Authentication lives in `session.auth`: the profile says how to sign in to a
# product, and replay is not the only thing that needs to. Re-exported here so
# every existing `from cua.replay.engine import CredentialResolver` keeps
# working -- there is one implementation, not two.
from ..session.auth import Authenticator, CredentialResolver  # noqa: F401


@dataclass
class _Ok:
    """Minimal ActionResult stand-in for actions the engine performs itself."""

    ok: bool
    error: str | None = None
    resolved: Any = None
    error_detail: dict = field(default_factory=dict)


@dataclass
class _LadderOutcome:
    """What the ladder decided after one action."""

    kind: str  # "continue" | "outcome" | "hard_failure" | "recovered"
    outcome: OutcomeSpec | None = None
    failure_code: str = ""
    detail: str = ""
    recoveries: tuple[str, ...] = ()


@dataclass
class _Resume:
    """Re-enter the flow at this step index, after re-authenticating.

    Not a `ReplayResult`: the run has not ended, it has been rewound. Returned
    up to `run()` because only the loop that owns the step index can rewind.
    """

    from_index: int


#: Failure classes that file an intervention without parking the run. These are
#: the three of Part 3.7's seven stuck triggers that resolve to a `failure`
#: rather than an `escalated`: the caller is told to debug it, AND an operator
#: sees it. Absent on purpose: PARAM_INVALID and CAPABILITY_UNSUPPORTED (nothing
#: was opened, so there is no session and no screen for a person to look at) and
#: INTERNAL (a bug in this code is not human work in a bank's back office).
INTERVENTION_WORTHY_FAILURES = frozenset({
    FailureClass.LOCATOR_UNRESOLVED,
    FailureClass.CHECKPOINT_FAILED,
    FailureClass.POLICY_DENIED,
})


class ReplayEngine:
    def __init__(
        self,
        surface: Surface,
        artifact: CapabilityArtifact,
        profile: ResolvedProfile,
        *,
        journal: Journal | None = None,
        credentials: CredentialResolver | None = None,
        tenant: str = "",
        max_recoveries_per_run: int = 8,
        broker=None,
        lease: ControlLease | None = None,
        goal: str = "",
        policy: AllowlistRules | None = None,
        confirm_irreversible: bool = False,
        evidence=None,
        screenshots: str = "failure",
    ) -> None:
        self.surface = surface
        self.artifact = artifact
        self.profile = profile
        self.journal = journal or MemoryJournal()
        self.credentials = credentials or CredentialResolver()
        self.tenant = tenant or profile.profile.profile.id
        self.max_recoveries_per_run = max_recoveries_per_run
        self.goal = goal

        self.broker = broker
        """Optional `escalation.InterventionBroker`. With one attached, an
        escalation parks the run in place and waits for a person; without one --
        plain `cua replay`, every offline test -- the engine returns the
        terminal `Escalated` it always has. Duck-typed so `replay` keeps not
        importing anything that could reach a model."""

        self.lease = lease
        """Optional `ControlLease`. Present when a human could take this session
        over. The engine holds it as `automation` for the whole run."""

        self.confirm_irreversible = confirm_irreversible
        """The caller's explicit intent to perform this capability's writes.

        One boolean, and it is the difference between an agent that decided on
        its own to post a transaction and one that was told to. Checked together
        with `approval_state`, because either alone is insufficient: an approved
        artifact still should not fire because a caller invoked it by mistake,
        and an emphatic caller should not be able to run a flow nobody
        reviewed."""

        # ---- policy, installed on the choke point --------------------------
        # Registered here rather than by the caller for the reason `add_guard`
        # exists: a guard the caller has to remember to attach is a guard some
        # caller forgets. The engine knows the capability ref and the tenant's
        # base_url, which is exactly what the allowlist needs to be specific.
        self.allowlist: AllowlistGuard | None = None
        if policy is not None:
            self.allowlist = AllowlistGuard(policy, base_url=profile.base_url,
                                            capability_ref=artifact.ref)
            surface.add_guard(self.allowlist)

        # The risk guard runs on EVERY run, gated or not: `_irreversible_gate`
        # below refuses a flow whose DECLARED risk is not authorised, and this
        # refuses an action whose DERIVED tier turns out to be irreversible even
        # though nothing declared it (R-M6-2). The first is cheap and early; the
        # second is the one that holds when the declaration was wrong.
        self.risk_guard = IrreversibleActionGuard(
            allow_irreversible=self._irreversible_authorized(), lease=lease)
        surface.add_guard(self.risk_guard)

        self.evidence = evidence
        """Optional `observability.EvidenceWriter`. Present for `cua replay` and
        the demo scripts; absent for the test suite, which asserts on the
        in-memory journal and should not litter the filesystem to do it."""

        self.screenshots = screenshots
        """`failure` (default), `all`, or `none`.

        Part 7 asks for "the failing step +/- 1" on replay rather than every
        step, and the minus-one is the half that costs something: you cannot go
        back and photograph the screen a step started from, so `failure` keeps
        exactly one frame in memory and writes it only if the next step fails.
        A discovery run captures everything -- there the pictures ARE the
        evidence -- but a replay that photographs fifty screens nobody looks at
        is noise somebody has to store."""

        self._last_frame: tuple[int, str, bytes] | None = None

        self.run_id = f"run_{uuid.uuid4().hex[:12]}"
        if evidence is not None:
            # One id, so the journal, the screenshots and the result all name
            # the same run. An evidence directory whose contents disagree about
            # which run they describe is worse than no directory.
            self.run_id = evidence.run_id
        self._traces: list[StepTrace] = []
        self._outputs: dict[str, Any] = {}
        self._secrets: set[str] = set()
        """Regulated values this run has already seen masked.

        Carried across observations because a value is regulated for what it is,
        not for which screen it is on -- and because this application embeds a
        member's name in a pane header that exists only as structural context,
        where no node rule can reach it. See `policy.redaction.apply_sensitivity`."""

        self._evidence_outputs: dict[str, Any] = {}
        """The same outputs as they may be written down. See
        `replay.result.Success.evidence_outputs`."""
        self._recovery_budget = max_recoveries_per_run
        self._session_recoveries = 0
        self._started = time.monotonic()

        # Index of the last step whose checkpoint PASSED -- the most recent
        # point at which the screen was verified rather than assumed. -1 means
        # nothing has been verified yet, so a resume starts from the beginning.
        # Only checkpointed steps qualify: a step with no checkpoint left behind
        # a state nobody confirmed, and resuming onto an unconfirmed state is
        # the bug this field exists to avoid.
        self._last_checkpoint_index = -1

        self._witnessed_complete: set[str] = set()
        """Irreversible steps a completion witness has proved already landed
        (R-M6-1). Consumed the next time the step loop reaches one."""

        self._pending_witness: dict[str, Condition] = {}
        """Witnesses still looking for their answer, by step id.

        A resume rewinds the flow, and the screen it rewinds ONTO is the app's
        post-login screen -- which is exactly where the consequence of a write
        is least likely to be visible. Measured, not assumed: re-authentication
        in this application lands on the dashboard, so a witness phrased over
        the member's record (the plan's own example) reads false there for the
        wrong reason.

        The screens where a consequence IS visible are the ones the flow replays
        THROUGH on its way back. So a witness is evaluated at the rewind and
        again before every replayed step until its own step is reached, and the
        first screen that answers it, answers it. One extra condition evaluation
        per step, only while a resume is in flight."""

    # ---- public ---------------------------------------------------------

    async def run(self, params: dict | None = None) -> ReplayResult:
        """Execute the flow. The lease, if there is one, is held for the whole run.

        Held via `acting_as` rather than by setting a field, because the guard
        reads an ambient actor: every `act()` the engine performs -- including
        the ones inside recoveries, waits and re-authentication -- happens inside
        this block, and every `act()` performed by anything else does not.
        """
        if self.lease is None:
            return await self._run_guarded(params)
        with self.lease.acting_as(AUTOMATION):
            return await self._run_guarded(params)

    async def _run_guarded(self, params: dict | None) -> ReplayResult:
        result = await self._run(params)
        result = await self._file_for_failure(result)
        await self._write_evidence(result)
        return result

    async def _run(self, params: dict | None = None) -> ReplayResult:
        self.journal.emit(
            "run.started", run_id=self.run_id, capability=self.artifact.ref,
            tenant=self.tenant, profile=list(self.profile.lineage),
            profile_hash=self.profile.hash,
        )

        # 1. Parameters, before anything opens. A caller error should not cost a
        #    browser session or leave a flow half-done.
        try:
            resolved_params = self.artifact.validate_params(params or {})
        except ParamValidationError as exc:
            return self._fail(FailureClass.PARAM_INVALID, detail={"errors": exc.errors},
                              observed=str(exc))

        # 2. Preflight (R-M3-1). Every condition in the artifact AND the profile
        #    is checked against this surface's capabilities before the first
        #    action -- discovering a URL checkpoint is unevaluable *after* an
        #    irreversible submit is the failure mode this prevents.
        if problem := self._preflight():
            return problem

        # 2b. Risk gating, before the browser (Part 3.6). A capability that
        #     commits something runs only when it has been reviewed AND the
        #     caller asked for it on purpose. Escalated rather than failed: the
        #     flow is not broken, it is unauthorised, and the answer is a
        #     person's approval rather than a debugging session.
        if gate := self._irreversible_gate():
            return gate

        try:
            if session_failure := await self._ensure_session():
                return session_failure

            # Indexed rather than a plain `for`, because a mid-flow session
            # expiry rewinds the flow (profiles/schema.py: auth.reauth.resume_from).
            index = 0
            while index < len(self.artifact.steps):
                step = self.artifact.steps[index]
                result = await self._run_step(step, resolved_params, index=index)
                await self._capture(index, step, bad=result is not None
                                    and not isinstance(result, _Resume))
                if isinstance(result, _Resume):
                    index = result.from_index
                    continue
                if result is not None:
                    return result
                index += 1

            if extraction_failure := await self._extract_remaining():
                return extraction_failure

            return await self._verify_success()

        except UnsupportedOperator as exc:
            return self._fail(FailureClass.CAPABILITY_UNSUPPORTED, detail=exc.as_detail(),
                              observed=str(exc))
        except Exception as exc:  # pragma: no cover - genuine bug path
            self.journal.emit("run.internal_error", error=f"{type(exc).__name__}: {exc}")
            return self._fail(FailureClass.INTERNAL, observed=f"{type(exc).__name__}: {exc}")

    # ---- risk gating (Part 3.6) -----------------------------------------

    def _irreversible_steps(self) -> list[Step]:
        return [s for s in self.artifact.steps if s.risk is RiskTier.SUBMIT_IRREVERSIBLE]

    def _irreversible_authorized(self) -> bool:
        """Both halves, or neither. See `confirm_irreversible`."""
        approved = self.artifact.capability.approval_state is ApprovalState.APPROVED
        return approved and self.confirm_irreversible

    def _irreversible_gate(self) -> Escalated | None:
        """Refuse an unauthorised write flow before anything is opened.

        Nothing is filed with the broker here, for the same reason PARAM_INVALID
        files nothing: there is no session, no screen and no screenshot, so an
        operator opening the console would find a request with nothing in it and
        no wheel attached. The caller is told, in the variant the caller can
        branch on, what it has to do differently.
        """
        steps = self._irreversible_steps()
        if not steps:
            return None

        state = self.artifact.capability.approval_state
        approved = state is ApprovalState.APPROVED
        if approved and self.confirm_irreversible:
            self.journal.emit("policy.irreversible_authorized",
                              capability=self.artifact.ref, approval=state.value,
                              steps=[st.id for st in steps])
            return None

        missing = []
        if not approved:
            missing.append(f"the capability is '{state.value}', not 'approved'")
        if not self.confirm_irreversible:
            missing.append("the caller did not pass confirm_irreversible=True")
        why = " and ".join(missing)

        self.journal.emit("policy.irreversible_refused", capability=self.artifact.ref,
                          approval=state.value, confirmed=self.confirm_irreversible,
                          steps=[st.id for st in steps])
        message = (
            f"{self.artifact.ref} performs {len(steps)} irreversible step(s) "
            f"({', '.join(st.id for st in steps)}) and {why}. An unattended agent "
            f"committing a transaction on a capability nobody reviewed, or on a call "
            f"nobody meant to make, is the failure this gate exists for. Approve the "
            f"artifact and re-invoke with explicit confirmation."
        )
        self.journal.emit("run.finished", status="escalated",
                          reason="IRREVERSIBLE_NOT_AUTHORIZED", step=steps[0].id)
        return Escalated(reason_class="IRREVERSIBLE_NOT_AUTHORIZED",
                         human_message=message, at_step=steps[0].id,
                         resume_token=self._resume_token(), **self._common())

    # ---- preflight ------------------------------------------------------

    def _all_conditions(self) -> list[tuple[str, Condition | None]]:
        """Every condition that could be evaluated during this run."""
        out: list[tuple[str, Condition | None]] = []
        for step in self.artifact.steps:
            out.append((f"step {step.id} precondition", step.preconditions))
            out.append((f"step {step.id} checkpoint", step.checkpoint))
        out.append(("success checkpoint", self.artifact.success.checkpoint))
        for outcome in self.artifact.outcomes:
            out.append((f"outcome {outcome.code}", outcome.detect))
        for recovery in self.artifact.recoveries:
            out.append((f"recovery {recovery.id}", recovery.trigger))

        prof = self.profile.profile
        for recovery in prof.recoveries:
            out.append((f"profile recovery {recovery.id}", recovery.trigger))
        for hard in prof.hard_failures:
            out.append((f"hard failure {hard.code}", hard.detect))
        for stuck in prof.stuck_patterns:
            out.append((f"stuck pattern {stuck.id}", stuck.detect))
        if prof.auth.session_expiry_detector:
            out.append(("session expiry detector", prof.auth.session_expiry_detector))
        if prof.auth.login_recipe and prof.auth.login_recipe.success:
            out.append(("login success", prof.auth.login_recipe.success))
        return [(w, c) for w, c in out if c is not None]

    def _preflight(self) -> Failure | None:
        problems = unsupported(self._all_conditions(), self.surface.capabilities)
        if not problems:
            self.journal.emit("preflight.ok", conditions=len(self._all_conditions()))
            return None

        first = problems[0]
        self.journal.emit(
            "preflight.unsupported",
            surface=self.surface.capabilities.kind.value,
            problems=[{"operator": p.operator, "where": p.where} for p in problems],
        )
        return self._fail(
            FailureClass.CAPABILITY_UNSUPPORTED,
            at_step=first.where,
            expected=first.condition,
            observed=(
                f"a {self.surface.capabilities.kind.value} surface cannot evaluate "
                f"'{first.operator}' ({len(problems)} condition(s) affected)"
            ),
            detail={"problems": [{"operator": p.operator, "where": p.where,
                                  "condition": p.condition} for p in problems]},
        )

    # ---- session --------------------------------------------------------

    async def _ensure_session(self) -> Failure | None:
        """Log in if the app says we are not logged in.

        Uses the profile's login recipe, executed by the same step machinery as
        any other step: one executor, one set of checkpoints, one place for this
        to be wrong.
        """
        auth = self.profile.profile.auth
        if not auth.login_recipe or not auth.session_expiry_detector:
            return None

        # Open the application if nothing is open yet. "Is the session valid?"
        # is unanswerable against about:blank, and every flow needs the app on
        # screen before its first step regardless.
        if self.surface.capabilities.can_navigate:
            current = await self.surface.current_url()
            if current in ("", "about:blank"):
                self.journal.emit("session.opening_app", url=self.profile.base_url)
                await self.surface.act(Action(
                    ActionType.NAVIGATE, url=f"{self.profile.base_url}/",
                    reason="open the application before checking the session",
                ))

        ctx = await self._context()
        if not evaluate(auth.session_expiry_detector, ctx):
            return None

        self.journal.emit("session.expired", detector=describe(auth.session_expiry_detector))
        return await self._reauthenticate()

    async def _reauthenticate(self) -> Failure | None:
        """Run the profile's login recipe. Shared by the pre-flight session
        check and by mid-flow expiry recovery -- one code path, so a session
        that drops halfway through a flow is handled exactly like one that was
        never valid."""
        auth = self.profile.profile.auth
        if not auth.reauth.enabled:
            return self._fail(FailureClass.SESSION_EXPIRED,
                              observed="session expired and re-authentication is disabled")

        for attempt in range(auth.reauth.max_attempts + 1):
            self.journal.emit("session.reauth_attempt", attempt=attempt + 1)
            for step in auth.login_recipe.steps:
                result = await self._perform(step.action, {}, reason=step.intent,
                                             timeout_ms=step.timeout_ms)
                if not result.ok:
                    self.journal.emit("session.reauth_step_failed", step=step.id,
                                      error=result.error)
                    break
            # Waited for, not checked once: signing in ends in a navigation, and
            # asserting the signed-in state the instant after the submit click
            # races the response. Same reason every step has a checkpoint.
            if auth.login_recipe.success:
                waited = await self._wait_for(
                    auth.login_recipe.success,
                    self.profile.profile.surface.timeouts.navigation_ms,
                )
                if waited.ok:
                    self.journal.emit("session.reauth_ok", attempt=attempt + 1)
                    return None

                # Signing in runs outside the step ladder, so it needs its own
                # hard-failure check. Without this, an application error during
                # login is retried blindly until the timeout -- reported as
                # "could not authenticate" when the truth is "the app is down",
                # which sends whoever is paged to the wrong system.
                ctx = await self._context()
                for hard in self.profile.profile.hard_failures:
                    if evaluate(hard.detect, ctx):
                        self.journal.emit("hard_failure.detected", step="login", code=hard.code)
                        return self._fail(
                            FailureClass.SURFACE_ERROR, at_step="login",
                            expected="the sign-in screen to accept the service account",
                            observed=f"{hard.code}: {describe(hard.detect)}",
                            detail={"hard_failure": hard.code, "during": "authentication"},
                        )

        return self._fail(
            FailureClass.SESSION_EXPIRED,
            expected=describe(auth.login_recipe.success) if auth.login_recipe.success else "",
            observed="re-authentication did not reach the signed-in state",
        )

    # ---- steps ----------------------------------------------------------

    async def _resume_index(self) -> int | None:
        """Where to re-enter the flow after re-authenticating.

        `resume_from` is an app fact, declared per product in the profile
        (§3.3.1), because "what happens to a half-finished flow when the session
        drops" is a property of the application, not of a capability.

          last_checkpoint  re-enter after the deepest checkpoint that is STILL
                           TRUE of the screen we are on now.
          start            re-run the whole flow from step 0.
          fail             do not resume; the run ends as SESSION_EXPIRED.

        The subtlety is in "still true". Signing in again lands on the app's
        post-login screen, not on the screen the flow was standing on, so the
        last checkpoint we *recorded* as passing is usually no longer the state
        in front of us. Resuming there anyway re-runs a step against the wrong
        screen, and the locator that cannot find its control reports
        LOCATOR_UNRESOLVED -- a broken-automation alarm for a working app.

        So the recorded checkpoints are re-evaluated, deepest first, against the
        live post-login screen. The first that still holds is genuinely where we
        are; everything after it is replayed to rebuild the state that was lost.
        Falling all the way through means nothing is verifiable and the flow
        restarts, which is the only honest answer when the app has put us
        somewhere the recording never described.
        """
        mode = self.profile.profile.auth.reauth.resume_from
        if mode == "fail":
            return None
        if mode == "start":
            return 0

        ctx = await self._context()
        for i in range(self._last_checkpoint_index, -1, -1):
            checkpoint = self.artifact.steps[i].checkpoint
            if checkpoint is not None and evaluate(checkpoint, ctx):
                return i + 1
        return 0

    async def _run_step(self, step: Step, params: dict, *,
                        index: int = 0) -> ReplayResult | _Resume | None:
        """Execute one step. Returns a terminal result, a rewind, or None."""
        started = time.monotonic()

        # R-M6-1. While a resume is in flight, look for the declared consequence
        # of every irreversible step still ahead of us. This runs before the
        # skip check below rather than inside it, because the screen that
        # answers a witness is usually several steps earlier than the step it
        # answers for.
        if self._pending_witness:
            await self._poll_witnesses(reached=step.id)

        # A resume already proved this write landed. Checked before
        # `step.started` is emitted, so the journal does not claim a step ran
        # that never did.
        if step.id in self._witnessed_complete:
            self._witnessed_complete.discard(step.id)
            self.journal.emit("step.skipped", step=step.id,
                              reason="completion witness satisfied: the write already landed")
            self._trace(step, ok=True, started=started,
                        note="skipped (completion witness satisfied)")
            return None

        self.journal.emit("step.started", step=step.id, intent=step.intent,
                          action=step.action.type.value, risk=step.risk.value)

        if step.requires_human:
            return await self._escalate(
                "REQUIRES_HUMAN", step.id,
                "The artifact marks this step as requiring a person.",
                index=index, expected=step.intent)

        # Preconditions. An optional step whose precondition fails is skipped --
        # that is how a tenant-specific extra screen is tolerated without
        # branching the recording.
        if step.preconditions is not None:
            ctx = await self._context()
            if not evaluate(step.preconditions, ctx):
                if step.optional:
                    self.journal.emit("step.skipped", step=step.id,
                                      reason="optional precondition not met")
                    self._trace(step, ok=True, note="skipped (optional)", started=started)
                    return None
                return self._fail(
                    FailureClass.PRECONDITION_FAILED, at_step=step.id,
                    expected=describe(step.preconditions),
                    observed=await self._observed_summary(),
                )

        action = await self._build_action(step, params)
        if action is None:
            return self._fail(FailureClass.INTERNAL, at_step=step.id,
                              observed=f"could not build an action for {step.action.type}")

        result = await self.surface.act(action)
        if not result.ok:
            return await self._action_failure(step, result, started, index=index)

        if offsite := await self._offsite():
            self.journal.emit("policy.offsite", step=step.id, reason=offsite)
            self._trace(step, ok=False, started=started, note="left the allowed space")
            return self._fail(
                FailureClass.POLICY_DENIED, at_step=step.id,
                expected="a url inside the allowlist",
                observed=offsite,
                detail={"denied_by": "allowlist", "when": "after the action"},
            )

        resolved_by = result.resolved.strategy if result.resolved else ""
        degraded = bool(result.resolved and result.resolved.degraded)
        if result.resolved:
            self.journal.emit("action.resolved", step=step.id,
                              resolved=result.resolved.describe(), degraded=degraded)

        ladder = await self._settle(step)

        if ladder.kind == "retry_step":
            # The session dropped mid-flow and was restored. Re-authenticating
            # lands on the app's post-login screen, NOT on the screen this step
            # assumed -- so re-running this step alone works only when the step
            # is self-sufficient (a navigate). Anything that depends on state
            # earlier steps built must be re-entered from the last screen we
            # actually verified.
            resume_at = await self._resume_index()
            if resume_at is None:
                self.journal.emit("session.resume_refused", step=step.id,
                                  policy="resume_from=fail")
                self._trace(step, ok=False, resolved_by=resolved_by, degraded=degraded,
                            recoveries=ladder.recoveries, started=started)
                return self._fail(
                    FailureClass.SESSION_EXPIRED, at_step=step.id,
                    expected="an authenticated session",
                    observed="the session expired and this app does not permit resuming",
                )

            # R-M6-1. Resuming replays [resume_at, index] forward, so every
            # irreversible step in that window would be posted a second time --
            # whether it is the step that was interrupted (did the write land
            # before the request was rejected?) or one that already completed
            # (its checkpoint is false on the post-login screen, so the rewind
            # walks straight back past it). Neither question is answerable from
            # outside the application, and "we do not know whether we posted
            # this" is an escalation, never a retry.
            window = self.artifact.steps[resume_at:index + 1]
            irreversible = [s for s in window if s.risk is RiskTier.SUBMIT_IRREVERSIBLE]

            # R-M6-1's upgrade. A witness turns the unanswerable question into
            # an observable one, per step, right here -- we are standing on the
            # screen re-authentication landed us on, which for an interrupted
            # navigation is the screen that was being loaded, and that is where
            # a consequence of the write is visible if it is visible anywhere.
            #
            # A witness that is FALSE is the more valuable answer of the two: it
            # says the write did not land, so replaying is not a duplicate, and
            # the run continues instead of paging somebody for nothing.
            if irreversible:
                irreversible = await self._filter_witnessed(irreversible, interrupted=step.id)

            if irreversible:
                named = ", ".join(s.id for s in irreversible)
                self.journal.emit(
                    "resume.blocked_irreversible", interrupted_at=step.id,
                    would_replay=[s.id for s in window], irreversible=[s.id for s in irreversible],
                )
                self._trace(step, ok=False, resolved_by=resolved_by, degraded=degraded,
                            recoveries=ladder.recoveries, started=started,
                            note="resume would repeat an irreversible step")
                return await self._escalate(
                    "IRREVERSIBLE_INTERRUPTED", step.id,
                    f"The session dropped while running {step.id}. Resuming would re-run "
                    f"irreversible step(s) {named} ({', '.join(s.intent for s in irreversible)}), "
                    f"and whether that write already took effect cannot be determined from "
                    f"outside the application. Check whether it landed, then either mark this "
                    f"run complete or allow it to re-run.",
                    index=index, expected=f"{step.intent} to have completed exactly once",
                    observed=await self._observed_summary(),
                    # "Resume" re-runs the write; "I completed this step" skips
                    # it. That is precisely the choice R-M6-1 says an operator
                    # must make, and it needs no vocabulary of its own.
                )

            self._trace(step, ok=False, resolved_by=resolved_by, degraded=degraded,
                        recoveries=ladder.recoveries, started=started,
                        note="interrupted by session expiry")
            self.journal.emit(
                "session.resuming", interrupted_at=step.id,
                resume_from=self.profile.profile.auth.reauth.resume_from,
                resume_at=self.artifact.steps[resume_at].id
                if resume_at < len(self.artifact.steps) else "(end of flow)",
                last_verified=self.artifact.steps[self._last_checkpoint_index].id
                if self._last_checkpoint_index >= 0 else "(nothing verified yet)",
            )
            return _Resume(resume_at)

        if ladder.kind == "session_failed":
            self._trace(step, ok=False, resolved_by=resolved_by, degraded=degraded,
                        recoveries=ladder.recoveries, started=started)
            return self._fail(
                FailureClass.SESSION_EXPIRED, at_step=step.id,
                expected="an authenticated session",
                observed="the session expired and could not be re-established",
            )

        if ladder.kind == "outcome":
            self._trace(step, ok=True, resolved_by=resolved_by, degraded=degraded,
                        recoveries=ladder.recoveries, started=started)
            return await self._business_outcome(ladder.outcome, step.id)
        if ladder.kind == "hard_failure":
            self._trace(step, ok=False, resolved_by=resolved_by, degraded=degraded,
                        recoveries=ladder.recoveries, started=started, note=ladder.failure_code)
            return self._fail(FailureClass.SURFACE_ERROR, at_step=step.id,
                              expected="the application to respond normally",
                              observed=ladder.detail,
                              detail={"hard_failure": ladder.failure_code})
        if ladder.kind == "escalate":
            self._trace(step, ok=False, resolved_by=resolved_by, degraded=degraded,
                        recoveries=ladder.recoveries, started=started)
            return await self._escalate(
                ladder.failure_code, step.id, ladder.detail, index=index,
                # A stuck pattern fires INSTEAD of the checkpoint, so what the
                # flow expected is what the checkpoint asserted. Filing the
                # request without it leaves the operator the observed screen and
                # nothing to compare it against.
                expected=describe(step.checkpoint) if step.checkpoint is not None
                else step.intent,
                observed=await self._observed_summary())

        if ladder.kind == "checkpoint_failed":
            self._trace(step, ok=False, resolved_by=resolved_by, degraded=degraded,
                        recoveries=ladder.recoveries, started=started)
            return self._fail(
                FailureClass.CHECKPOINT_FAILED, at_step=step.id,
                expected=describe(step.checkpoint),
                observed=await self._observed_summary(),
            )

        self._trace(step, ok=True, resolved_by=resolved_by, degraded=degraded,
                    recoveries=ladder.recoveries, started=started)

        # This screen is now verified, so it becomes the resume point. Guarded on
        # the checkpoint existing: a step that asserted nothing proves nothing.
        if step.checkpoint is not None:
            self._last_checkpoint_index = index

        return await self._extract_for_step(step.id)

    # ---- the ladder -----------------------------------------------------

    async def _filter_witnessed(self, irreversible: list[Step], *,
                                interrupted: str) -> list[Step]:
        """Split the window into "a witness will answer for this" and "escalate".

        Returns only the steps with no witness at all -- the ones default-deny
        still covers, and the reason the floor did not move. Everything else is
        registered in `_pending_witness` and settled during the replay forward.

        The asymmetry is deliberate. A witness that comes back FALSE is the more
        valuable of the two answers: it says the write is not there, so
        replaying is not a duplicate, and the run continues unattended -- which
        is the entire return on declaring one. A witness that comes back TRUE
        only buys the right to skip; see `Step.completion_witness` for why that
        is not the same as the flow being able to finish.
        """
        unanswered: list[Step] = []
        for step in irreversible:
            if step.completion_witness is None:
                unanswered.append(step)
                continue
            self._pending_witness[step.id] = step.completion_witness
            self.journal.emit("resume.witness_pending", step=step.id,
                              interrupted_at=interrupted,
                              witness=describe(step.completion_witness))
        if self._pending_witness:
            await self._poll_witnesses(reached=None)
        return unanswered

    async def _poll_witnesses(self, *, reached: str | None) -> None:
        """Evaluate the outstanding witnesses against the screen in front of us.

        `reached` is the step the loop is about to run, or None at the rewind
        itself. A witness whose own step has been reached without ever holding
        has run out of screens to be true on, and its step is replayed -- which
        the witness has, by saying nothing, said is safe.
        """
        ctx = await self._context()
        for step_id, witness in list(self._pending_witness.items()):
            if evaluate(witness, ctx):
                del self._pending_witness[step_id]
                self._witnessed_complete.add(step_id)
                self.journal.emit("resume.witness_satisfied", step=step_id,
                                  seen_at=reached or "(the resume point)",
                                  witness=describe(witness),
                                  effect="the write landed; the step will be skipped")
            elif step_id == reached:
                del self._pending_witness[step_id]
                self.journal.emit("resume.witness_absent", step=step_id,
                                  witness=describe(witness),
                                  effect="the write did not land; the step will be replayed")

    # ---- evidence (Part 7) ----------------------------------------------

    async def _capture(self, index: int, step: Step, *, bad: bool) -> None:
        """One frame per step, kept or written according to `screenshots`.

        The redacted observation is always written when evidence is attached --
        it is small, it is the AX snapshot Part 7 asks for, and it is the thing
        that actually explains a locator failure. Pixels are the expensive part,
        so they follow the policy.
        """
        if self.evidence is None or self.screenshots == "none":
            return
        try:
            # Observed once. The masks need the raw boxes and the file needs the
            # redacted nodes, but they are two views of one snapshot -- taking
            # two would risk masking boxes that describe a screen that has
            # already moved.
            raw = await self.surface.observe()
            observation = apply_sensitivity(raw, self.profile.profile, self._secrets)
            self.evidence.observation(index, observation)

            png = None
            if self.surface.capabilities.can_screenshot:
                url = observation.frame_urls.get("content") or observation.url
                if screenshot_allowed(url, self.profile.profile):
                    png = annotate(
                        await self.surface.screenshot(), observation,
                        masks=screenshot_masks(raw, self.profile.profile, persisted=True))
            if png is None:
                return

            if self.screenshots == "all" or bad:
                # The failing step, and the one before it -- which only exists
                # because it was held back for exactly this.
                if bad and self._last_frame is not None:
                    prev_index, prev_step, prev_png = self._last_frame
                    self.evidence.screenshot(prev_index, prev_png, phase=f"{prev_step}_pre")
                self.evidence.screenshot(index, png, phase=f"{step.id}_post")
                self._last_frame = None
            else:
                self._last_frame = (index, step.id, png)
        except Exception as exc:  # evidence is never what breaks a run
            self.journal.emit("evidence.capture_failed", step=step.id,
                              error=f"{type(exc).__name__}: {exc}")

    async def _write_evidence(self, result: ReplayResult) -> None:
        """`result.json`, and a failure pack when there is something to explain.

        Written here rather than by the caller so a run that ends badly still
        leaves its evidence -- the case the pack exists for is precisely the one
        where nobody is around to write it afterwards.
        """
        if self.evidence is None:
            return
        try:
            if not result.ok and self.surface.capabilities.can_screenshot:
                raw = await self.surface.observe()
                observation = apply_sensitivity(raw, self.profile.profile, self._secrets)
                url = observation.frame_urls.get("content") or observation.url
                pack = self.evidence.dir / "failure"
                pack.mkdir(parents=True, exist_ok=True)
                # The AX snapshot: this is what the locators were resolved
                # against, so it is the artifact that explains the failure.
                (pack / "observation.json").write_text(
                    observation.model_dump_json(indent=2))
                if screenshot_allowed(url, self.profile.profile):
                    shot = await self.surface.screenshot(full_page=True)
                    (pack / "screen.png").write_bytes(
                        annotate(shot, observation,
                                 masks=screenshot_masks(raw, self.profile.profile,
                                                        persisted=True)))
        except Exception as exc:
            self.journal.emit("evidence.capture_failed", phase="failure_pack",
                              error=f"{type(exc).__name__}: {exc}")

        self.evidence.write_json("result.json", to_dict(result))

    async def _offsite(self) -> str | None:
        """Has the application put us somewhere the allowlist does not permit?

        The pre-action guard sees navigations automation ASKS for; a click on a
        link is a click, and where it lands is the app's decision. So the URL is
        re-checked after every action. Without this the allowlist would be a
        rule about intent rather than about position, which is not what Part 3.6
        claims for it.

        Both the page URL and the content frame's are checked: in a frameset the
        shell stays put while the flow moves, so checking only the top document
        would be checking the one URL that cannot change.
        """
        if self.allowlist is None or not self.surface.capabilities.has_url:
            return None
        urls = [await self.surface.current_url()]
        if self.surface.capabilities.has_frames:
            observation = await self.surface.observe()
            urls += [u for u in observation.frame_urls.values() if u]
        for url in urls:
            if not url:
                continue
            if problem := self.allowlist.rejects(url):
                return f"{url}: {problem}"
        return None

    async def _settle(self, step: Step) -> _LadderOutcome:
        """The post-action evaluation ladder, run until the state settles.

        The whole ladder is inside the wait loop, not just the checkpoint. An
        action that changes screens takes time to land, and the states we are
        classifying -- a not-found notice, an error page, an interstitial --
        arrive on exactly the same delay as the checkpoint does. Evaluating the
        ladder once, immediately after the click, reads the PREVIOUS screen and
        silently misclassifies: a legitimate "no records found" looks like a
        broken locator on the following step.

        Order within each pass is fixed and load-bearing:
          1. recoveries        (a modal may be hiding everything else)
          2. business outcomes (before checkpoints -- the brief's named trap)
          3. hard failures     (the app's own error page)
          3b. stuck patterns   (only if no outcome claimed the state)
          4. checkpoint        (pass, or wait and re-observe)
        """
        applied: list[str] = []
        checkpoint_attempts = step.retries + 1
        passes = 0

        while passes < 24:  # absolute guard; recoveries have their own budget
            passes += 1
            ctx = await self._context()

            # 1a. session expiry -- an app-level recoverable condition, checked
            #     before everything else. A bounced-to-login page can otherwise
            #     satisfy a URL checkpoint by accident (the `next=` parameter
            #     carries the path we were heading to), so detecting expiry
            #     first is both the correct order and the safe one.
            expiry = auth_detector = self.profile.profile.auth.session_expiry_detector
            if expiry is not None and evaluate(expiry, ctx):
                if self._session_recoveries >= self.profile.profile.auth.reauth.max_attempts:
                    self.journal.emit("session.reauth_exhausted", step=step.id)
                    return _LadderOutcome("session_failed", recoveries=tuple(applied))
                self._session_recoveries += 1
                self.journal.emit("session.expired_mid_flow", step=step.id,
                                  detector=describe(expiry))
                failure = await self._reauthenticate()
                if failure is not None:
                    return _LadderOutcome("session_failed", recoveries=tuple(applied))
                applied.append("session_reauth")
                return _LadderOutcome("retry_step", recoveries=tuple(applied))

            # 1b. recoveries -- a successful one re-observes without consuming a
            #    checkpoint attempt, because dismissing a modal can reveal the
            #    state we were actually waiting for.
            fired = await self._try_recoveries(ctx, step)
            if fired:
                applied.append(fired)
                continue

            # 2. business outcomes, BEFORE the checkpoint
            for outcome in self._outcomes_for(step.id):
                if evaluate(outcome.detect, ctx):
                    self.journal.emit("outcome.detected", step=step.id, code=outcome.code,
                                      detector=describe(outcome.detect))
                    return _LadderOutcome("outcome", outcome=outcome, recoveries=tuple(applied))

            # 3. hard failures
            for hard in self.profile.profile.hard_failures:
                if evaluate(hard.detect, ctx):
                    self.journal.emit("hard_failure.detected", step=step.id, code=hard.code)
                    return _LadderOutcome("hard_failure", failure_code=hard.code,
                                          detail=f"{hard.code}: {describe(hard.detect)}",
                                          recoveries=tuple(applied))

            # 3b. stuck patterns -- consulted only after declared outcomes, so a
            #     capability that declares PERMISSION_DENIED wins over the
            #     profile's generic permission_wall and no human is paged.
            for stuck in self.profile.profile.stuck_patterns:
                if evaluate(stuck.detect, ctx):
                    self.journal.emit("stuck.detected", step=step.id, pattern=stuck.id,
                                      reason=stuck.reason_class)
                    return _LadderOutcome("escalate", failure_code=stuck.reason_class,
                                          detail=stuck.human_message, recoveries=tuple(applied))

            # 4. checkpoint
            if step.checkpoint is None:
                return _LadderOutcome("continue", recoveries=tuple(applied))
            if evaluate(step.checkpoint, ctx):
                self.journal.emit("checkpoint.passed", step=step.id, pass_no=passes)
                return _LadderOutcome("continue", recoveries=tuple(applied))

            checkpoint_attempts -= 1
            if checkpoint_attempts <= 0:
                self.journal.emit("checkpoint.failed", step=step.id,
                                  expected=describe(step.checkpoint))
                return _LadderOutcome("checkpoint_failed", recoveries=tuple(applied))

            self.journal.emit("checkpoint.retry", step=step.id,
                              remaining=checkpoint_attempts,
                              expected=describe(step.checkpoint))
            await self.surface.act(Action(ActionType.WAIT, timeout_ms=400,
                                          reason="wait before re-evaluating the ladder"))

        self.journal.emit("ladder.guard_tripped", step=step.id)
        return _LadderOutcome("checkpoint_failed", recoveries=tuple(applied))

    async def _try_recoveries(self, ctx: EvalContext, step: Step) -> str | None:
        """Fire at most one recovery, respecting its own and the run's budget."""
        for recovery in self._recoveries():
            if self._recovery_budget <= 0:
                self.journal.emit("recovery.budget_exhausted", step=step.id)
                return None
            if not evaluate(recovery.trigger, ctx):
                continue

            self.journal.emit("recovery.attempted", step=step.id, recovery=recovery.id,
                              trigger=describe(recovery.trigger))
            self._recovery_budget -= 1

            for raw in recovery.actions:
                outcome = await self._perform(
                    raw, {}, reason=f"recovery {recovery.id}",
                    timeout_ms=self.profile.profile.surface.timeouts.slow_load_grace_ms,
                )
                if not outcome.ok:
                    self.journal.emit("recovery.action_failed", recovery=recovery.id,
                                      error=outcome.error)

            after = await self._context()
            if not evaluate(recovery.trigger, after):
                self.journal.emit("recovery.succeeded", step=step.id, recovery=recovery.id)
                return recovery.id
            self.journal.emit("recovery.ineffective", step=step.id, recovery=recovery.id)
        return None

    def _recoveries(self) -> list[RecoverySpec]:
        """Capability recoveries first, then the app-wide ones inherited from
        the profile -- a capability may special-case a condition its app also
        handles generically."""
        out = list(self.artifact.recoveries)
        out.extend(
            RecoverySpec(id=r.id, trigger=r.trigger, actions=r.actions,
                         max_attempts=r.max_attempts, description=r.description)
            for r in self.profile.profile.recoveries
        )
        return out

    def _outcomes_for(self, step_id: str) -> list[OutcomeSpec]:
        """An outcome with no `after_step` is checked after every step -- a
        permission wall can appear anywhere."""
        return [o for o in self.artifact.outcomes
                if o.after_step is None or o.after_step == step_id]

    # ---- extraction -----------------------------------------------------

    async def _extract_for_step(self, step_id: str) -> ReplayResult | None:
        for extraction in self.artifact.extractions:
            if extraction.after_step == step_id:
                if failure := await self._extract(extraction):
                    return failure
        return None

    async def _extract_remaining(self) -> ReplayResult | None:
        for extraction in self.artifact.extractions:
            if extraction.after_step is None and extraction.output not in self._outputs:
                if failure := await self._extract(extraction):
                    return failure
        return None

    async def _extract(self, extraction: Extraction) -> Failure | None:
        observation = await self.surface.observe()
        try:
            node, resolved = resolve_node(extraction.target, observation)
        except LocatorError as exc:
            self.journal.emit("extraction.failed", output=extraction.output, error=exc.message)
            return self._fail(
                FailureClass.EXTRACTION_FAILED,
                expected=f"a node for declared output '{extraction.output}'",
                observed=exc.message, detail=exc.as_detail(),
            )

        raw = {"name": node.name, "value": node.value or "", "text": node.name}[extraction.source]
        try:
            parsed = _parse(raw, extraction.parse)
        except ValueError as exc:
            return self._fail(
                FailureClass.EXTRACTION_FAILED,
                expected=f"'{extraction.output}' parseable as {extraction.parse.type}",
                observed=f"{raw!r}: {exc}",
            )

        self._outputs[extraction.output] = parsed

        # `OutputSpec.redact` said how this value may appear in logs and
        # evidence, and until M6 nothing read it -- the field was declared,
        # documented, defaulted in a committed profile, and unconsumed. The
        # caller still gets `parsed` in full: the boundary is persistence, not
        # the return value, which is the distinction the field was written to
        # make.
        mode = redaction_for(extraction.output,
                             self.artifact.outputs.get(extraction.output),
                             self.profile.profile)
        shown = redact_output(parsed, mode)
        # Kept, not just logged: `result.json` needs the same rendering, and
        # recomputing it there would put the decision in two places.
        self._evidence_outputs[extraction.output] = shown
        self.journal.emit("extraction.ok", output=extraction.output,
                          value=shown, redaction=mode,
                          resolved=resolved.describe(), degraded=resolved.degraded)
        return None

    # ---- terminal results -----------------------------------------------

    async def _verify_success(self) -> ReplayResult:
        ctx = await self._context()
        if not evaluate(self.artifact.success.checkpoint, ctx):
            self.journal.emit("success.checkpoint_failed",
                              expected=describe(self.artifact.success.checkpoint))
            return self._fail(
                FailureClass.CHECKPOINT_FAILED, at_step="success",
                expected=describe(self.artifact.success.checkpoint),
                observed=await self._observed_summary(),
            )

        missing = [
            name for name, spec in self.artifact.outputs.items()
            if not spec.optional and name not in self._outputs
        ]
        if missing:
            return self._fail(FailureClass.EXTRACTION_FAILED,
                              expected=f"declared outputs {missing}",
                              observed="the flow finished without reading them")

        # Names only, never values: `extraction.ok` already carries each value at
        # its declared redaction, and a second unredacted copy here would undo
        # the first one.
        self.journal.emit("run.finished", status="success", outputs=sorted(self._outputs))
        return Success(outputs=dict(self._outputs),
                       evidence_outputs=dict(self._evidence_outputs),
                       **self._common())

    async def _business_outcome(self, outcome: OutcomeSpec, step_id: str) -> BusinessOutcome:
        message = outcome.description
        if outcome.message_from is not None:
            try:
                node, _ = resolve_node(outcome.message_from, await self.surface.observe())
                # The institution's own wording, scrubbed. An error banner
                # quoting an account number is precisely the free-text case the
                # detectors exist for, and this string is returned to the caller
                # AND journaled.
                message = scrub_text(node.name or message,
                                     self.profile.profile.sensitivity.text_detectors)
            except LocatorError:
                pass  # the app's wording is a nicety; the code is the contract

        self.journal.emit("run.finished", status="business_outcome", code=outcome.code)
        return BusinessOutcome(code=outcome.code, message=message, at_step=step_id,
                               **self._common())

    async def _escalate(self, reason_class: str, step_id: str, human_message: str,
                        *, index: int | None = None, expected: str = "",
                        observed: str = "") -> Escalated | _Resume:
        """A human is needed. File the request, and -- if one can arrive -- wait.

        Returns `_Resume` when an operator hands the flow back, so the caller's
        step loop re-enters. Returns `Escalated` when nobody is listening, when
        the operator abandons the run, or when the wait expires.
        """
        request = await self._file_intervention(
            reason_class, step_id, human_message,
            index=index, expected=expected, observed=observed, takeover=True,
        )

        if request is None or self.broker is None:
            self.journal.emit("run.finished", status="escalated",
                              reason=reason_class, step=step_id)
            return Escalated(reason_class=reason_class, human_message=human_message,
                             at_step=step_id, resume_token=self._resume_token(),
                             **self._common())

        resolution = await self.broker.wait(request)
        self.journal.emit("intervention.answered", intervention=request.id,
                          disposition=resolution.disposition.value,
                          operator=resolution.operator)

        if resolution.disposition is Disposition.ABANDON or index is None:
            self.journal.emit("run.finished", status="escalated",
                              reason=reason_class, step=step_id,
                              intervention=request.id)
            return Escalated(reason_class=reason_class, human_message=human_message,
                             at_step=step_id, intervention_id=request.id,
                             resume_token=self._resume_token(), **self._common())

        target = index if resolution.disposition is Disposition.RESUME else index + 1
        return await self._handback(target, request, resolution)

    # ---- handback -------------------------------------------------------

    async def _handback(self, target: int, request: InterventionRequest,
                        resolution: Resolution) -> _Resume | Failure:
        """Re-verify where we are standing before taking the wheel back.

        The operator says the screen is ready. Believing them is the easy
        implementation and the wrong one: they were fixing a broken flow under
        time pressure, on a screen the automation already misread once. So the
        engine checks the target step's own precondition against the live screen
        and only then reclaims the lease.

        Bounded to {same step, next step} by construction -- those are the only
        two values `target` can hold -- so a handback can resynchronise or fail,
        and can never go hunting for a step that happens to match.
        """
        verified, how, blocked_by = await self._verify_handback(target)
        self.journal.emit(
            "handback.verified" if verified else "handback.resync_failed",
            intervention=request.id, target_step=self._step_name(target),
            disposition=resolution.disposition.value, operator=resolution.operator,
            checked=how, blocked_by=blocked_by,
        )

        if not verified:
            if self.lease is not None:
                self.lease.abandon(actor=AUTOMATION,
                                   reason="handback left the session on an unexpected screen")
            return self._fail(
                FailureClass.PRECONDITION_FAILED, at_step=self._step_name(target),
                expected=how,
                observed=await self._observed_summary(),
                detail={"handback": resolution.disposition.value,
                        "intervention": request.id, "operator": resolution.operator,
                        **({"blocked_by": blocked_by} if blocked_by else {})},
            )

        if self.lease is not None:
            self.lease.resumed(reason=f"handback verified at {self._step_name(target)}")
        # Everything the human did happened on a screen we did not verify, so
        # nothing before this point is a safe resume point any more.
        self._last_checkpoint_index = min(self._last_checkpoint_index, target - 1)
        return _Resume(target)

    async def _verify_handback(self, target: int) -> tuple[bool, str, str]:
        """Is the live screen the one step `target` expects to start from?

        Returns `(verified, what was checked, what blocked it)`.

        **R-M6-3 runs first, ahead of every case below.** Before any condition
        is evaluated, the profile's declared `stuck_patterns` and
        `hard_failures` are checked against the live screen, and a match refuses
        the handback whatever the condition would have said. The asymmetry is
        the point: a declared bad screen is POSITIVE evidence of the wrong
        state, while a passing condition is only the ABSENCE of evidence of the
        wrong state, and the two are not equal weight.

        This was found by running it. `member.lookup_balance`'s success
        checkpoint is URL-only, and this application's authorization wall lives
        at the same URL as the member record -- so an operator who took control,
        did nothing, and pressed "I completed this step" got `handback.verified`
        on a permission error, and the run failed one step later at extraction
        with an error that pointed at the wrong thing. Tightening the checkpoint
        would mean enumerating every bad screen sharing that URL inside every
        checkpoint, which is what stuck patterns already are.

        Four cases, in descending order of how much they prove:
          1. the operator finished the LAST step, so there is no step to enter --
             what the flow claims is now true is its success checkpoint, and
             that is what gets checked;
          2. the step declares a precondition -- evaluate it, and that is that;
          3. it does not, but the step before it declared a checkpoint -- the
             screen that step left behind is the screen this one starts on;
          4. none of the above -- there is nothing to check. Say so in the
             journal rather than reporting a verification that did not happen.

        Case 1 is not a formality. "I completed that step myself" on the final
        step means the flow is over, and accepting it unchecked sends the run
        straight into extraction against whatever screen the operator left
        behind -- which surfaces as an unresolvable locator three frames later
        instead of as the handback problem it actually is.
        """
        if target >= len(self.artifact.steps):
            success = self.artifact.success.checkpoint
            if success is None:
                # Nothing to assert, but the bad-screen check still applies --
                # it needs no condition of its own, which is exactly why it is
                # able to cover the case where the artifact asserts nothing.
                return await self._settle_for(None, "(the flow declares no success checkpoint)")
            return await self._settle_for(
                success, f"the flow's success state: {describe(success)}")

        step = self.artifact.steps[target]

        if step.preconditions is not None:
            return await self._settle_for(step.preconditions, describe(step.preconditions))

        if target > 0:
            previous_step = self.artifact.steps[target - 1]
            if previous_step.checkpoint is not None:
                return await self._settle_for(
                    previous_step.checkpoint,
                    f"the state {previous_step.id} leaves behind: "
                    f"{describe(previous_step.checkpoint)}")

        return await self._settle_for(
            None, "(neither this step nor the one before it asserts anything)")

    def _bad_screen(self, ctx: EvalContext) -> tuple[str, str, str] | None:
        """A screen the profile has declared known-bad, if we are on one.

        Hard failures before stuck patterns, matching the evaluation ladder's
        own ordering, so the two places that ask "is this screen a declared
        problem" answer in the same order.
        """
        for hard in self.profile.profile.hard_failures:
            if evaluate(hard.detect, ctx):
                return ("hard_failure", hard.code,
                        hard.description or describe(hard.detect))
        for stuck in self.profile.profile.stuck_patterns:
            if evaluate(stuck.detect, ctx):
                return ("stuck_pattern", stuck.reason_class,
                        stuck.human_message or describe(stuck.detect))
        return None

    async def _settle_for(self, condition: Condition | None, described: str,
                          attempts: int = 6) -> tuple[bool, str, str]:
        """Evaluate a handback condition, allowing the screen to finish arriving.

        The operator's last click and their press of "release" are two separate
        events with nothing ordering them, so the screen is routinely still
        loading when the engine is handed back the wheel. Checking once and
        calling it a desync would fail every handback where the person was
        quick, and the failure would look exactly like the one that means they
        left the session somewhere wrong -- which is the one case this check
        exists to catch. Bounded, so a genuine desync still fails and fails
        loudly rather than hanging.
        """
        grace = self.profile.profile.surface.timeouts.slow_load_grace_ms
        for attempt in range(attempts):
            ctx = await self._context()

            # R-M6-3, checked first and refused immediately rather than given
            # the settle grace. A stuck pattern is a declared decision about a
            # screen, not a rendering artifact -- the ladder treats one as
            # terminal wherever else it appears, and waiting to see whether a
            # permission wall goes away on its own would be pretending
            # otherwise.
            if bad := self._bad_screen(ctx):
                kind, code, message = bad
                # `pattern_kind=`, not `kind=`: `Journal.emit`'s own first
                # parameter is `kind`, so passing one here swallows the event
                # with a TypeError -- the third time in this codebase that a
                # keyword collision has produced an R-M4-2 violation nobody
                # would have found by reading the branch.
                self.journal.emit("handback.blocked_by_pattern", pattern_kind=kind,
                                  code=code, detail=message,
                                  would_have_checked=described)
                return (False,
                        f"{described} -- refused: the screen matches the declared "
                        f"{kind.replace('_', ' ')} {code} ({message})",
                        code)

            if condition is None:
                return True, f"(unverified: {described})", ""
            if evaluate(condition, ctx):
                return True, described, ""
            if attempt == attempts - 1:
                break
            await self.surface.act(Action(ActionType.WAIT,
                                          timeout_ms=max(150, grace // attempts),
                                          reason="let the screen settle after a handback"))
        return False, described, ""

    def _step_name(self, index: int) -> str:
        if index >= len(self.artifact.steps):
            return "(end of flow)"
        return self.artifact.steps[index].id

    # ---- filing ---------------------------------------------------------

    async def _file_intervention(self, reason_class: str, step_id: str, human_message: str,
                                 *, index: int | None, expected: str, observed: str,
                                 takeover: bool,
                                 result_status: str = "") -> InterventionRequest | None:
        """Write the request, with a redacted picture of what stopped us.

        Returns None when there is no broker: the engine still journals and
        still returns the right result variant, it simply has nowhere to send
        human work. That is the offline default, and it is why every existing
        test sees exactly the behaviour it saw before.
        """
        if self.broker is None:
            return None

        session_id = self.lease.session_id if self.lease is not None else self.run_id
        request = InterventionRequest(
            run_id=self.run_id, session_id=session_id,
            capability_ref=self.artifact.ref, goal=self.goal, tenant=self.tenant,
            step_id=step_id, step_index=index,
            reason_class=reason_class, human_message=human_message,
            expected=expected, observed=observed,
            takeover=takeover, result_status=result_status,
        )
        request = await self._attach_evidence(request)
        self.broker.open(request)
        return request

    async def _attach_evidence(self, request: InterventionRequest) -> InterventionRequest:
        """A screenshot and an observation, both redacted, beside the request.

        Best-effort: an intervention that reaches a person without a picture is
        worth much more than one that never reaches them because the screenshot
        raised. The paths are written into the request only once the bytes are
        actually on disk, so the console never renders a link to nothing.
        """
        session = self.broker.sessions.get(request.session_id) if self.broker else None
        if session is None:
            return request

        directory = self.broker.evidence_root / "runs" / request.run_id / "interventions"
        update: dict[str, str] = {}
        try:
            # Going to disk, so the stricter mask applies (R-M6 / Part 3.6).
            observation, png = await session.snapshot(for_evidence=True)
            directory.mkdir(parents=True, exist_ok=True)
            if png is not None:
                shot = directory / f"{request.id}.png"
                shot.write_bytes(png)
                update["screenshot_ref"] = str(shot)
            observed = directory / f"{request.id}_observation.json"
            observed.write_text(observation.model_dump_json(indent=2))
            update["observation_ref"] = str(observed)
        except Exception as exc:  # evidence is best-effort; the page-out is not
            self.journal.emit("intervention.evidence_failed", intervention=request.id,
                              error=f"{type(exc).__name__}: {exc}")
        return request.model_copy(update=update) if update else request

    async def _file_for_failure(self, result: ReplayResult) -> ReplayResult:
        """Part 3.7's stuck triggers that resolve to a `failure`, not an escalation.

        An unrecovered checkpoint failure, an unresolvable locator and a policy
        denial are all things a person can look at and often fix -- and all
        things the CALLER should be told to debug rather than told to wait for.
        So the variant is unchanged and an intervention is filed alongside it.
        The run does not park: it has already ended, and `takeover=False` tells
        the console not to offer a wheel that is no longer attached to anything.
        """
        if self.broker is None or not isinstance(result, Failure):
            return result
        if result.failure_class not in INTERVENTION_WORTHY_FAILURES:
            return result

        request = await self._file_intervention(
            result.failure_class.value, result.at_step,
            f"{self.artifact.ref} stopped at {result.at_step or 'an early step'}: "
            f"{result.failure_class.value}. Expected {result.expected or 'the flow to continue'}; "
            f"saw {result.observed or 'something else'}.",
            index=None, expected=result.expected, observed=result.observed,
            takeover=False, result_status=result.status.value,
        )
        if request is None:
            return result
        return replace(result, intervention_id=request.id)

    def _fail(self, failure_class: FailureClass, *, at_step: str = "", expected: str = "",
              observed: str = "", detail: dict | None = None) -> Failure:
        self.journal.emit("run.finished", status="failed", failure_class=failure_class.value,
                          step=at_step, expected=expected, observed=observed)
        return Failure(failure_class=failure_class, at_step=at_step, expected=expected,
                       observed=observed, detail=detail or {}, **self._common())

    async def _action_failure(self, step: Step, result, started: float,
                              *, index: int | None = None) -> Failure | Escalated | _Resume:
        cls = {
            "LOCATOR_UNRESOLVED": FailureClass.LOCATOR_UNRESOLVED,
            "LOCATOR_AMBIGUOUS": FailureClass.LOCATOR_AMBIGUOUS,
            "PRECONDITION_FAILED": FailureClass.PRECONDITION_FAILED,
            "TIMEOUT": FailureClass.TIMEOUT,
            "POLICY_DENIED": FailureClass.POLICY_DENIED,
            "SURFACE_ERROR": FailureClass.SURFACE_ERROR,
        }.get(result.error_detail.get("failure_class", ""), FailureClass.SURFACE_ERROR)

        self._trace(step, ok=False, started=started, note=result.error or "")
        self.journal.emit("action.failed", step=step.id, failure_class=cls.value,
                          error=result.error)

        # Ambiguity is the case where a person must decide which control was
        # meant. Guessing is what this system refuses to do.
        if cls is FailureClass.LOCATOR_AMBIGUOUS:
            return await self._escalate(
                "AMBIGUOUS_TARGET", step.id, result.error or "",
                index=index,
                expected=f"exactly one control matching {step.action.target.target_id}"
                         if step.action.target else "exactly one matching control",
                observed=await self._observed_summary(),
            )

        return self._fail(cls, at_step=step.id,
                          expected=f"{step.action.type.value} on {step.action.target.target_id}"
                                   if step.action.target else step.action.type.value,
                          observed=result.error or "", detail=result.error_detail)

    # ---- helpers --------------------------------------------------------

    async def _context(self) -> EvalContext:
        observation = await self.surface.observe()
        url = await self.surface.current_url() if self.surface.capabilities.has_url else ""
        return EvalContext(observation=observation, page_url=url,
                           capabilities=self.surface.capabilities)

    async def _observed_summary(self) -> str:
        """What was actually on screen, for a debuggable failure message.

        Redacted, because this string is an egress and a busy one: it lands in
        the journal, in `Failure.observed`, and in the `observed` field of every
        intervention a person will read. The screen it summarizes is a member
        detail page carrying an SSN and a date of birth, and until M6 this
        method read it raw -- discovery and the console redacted, replay did
        not, and replay is the path that runs unattended in production.
        """
        observation = apply_sensitivity(await self.surface.observe(),
                                        self.profile.profile, self._secrets)
        interesting = [
            n for n in observation.nodes
            if n.visible and n.role in {"alert", "status", "dialog", "heading", "button"}
        ][:6]
        if not interesting:
            interesting = [n for n in observation.nodes if n.visible][:6]
        url = observation.frame_urls.get("content") or observation.url
        detectors = self.profile.profile.sensitivity.text_detectors
        summary = f"url={url} | " + "; ".join(n.describe() for n in interesting)
        # The detectors run over the assembled string as well as over the nodes:
        # a url can carry a value nobody declared, and node-level masking cannot
        # see it because it is not on a node.
        return scrub_text(summary, detectors) if detectors else summary

    async def _perform(self, spec: StepAction, params: dict, *, reason: str,
                       timeout_ms: int = 8000, risk=None):
        """Run one action spec.

        A `wait` carrying a `wait_for` condition is polled here rather than
        handed to the surface: the surface can sleep, but only the engine can
        evaluate a condition, and "wait until the spinner is gone" is the whole
        point of the transient-slowness recovery.
        """
        if spec.type is ActionType.WAIT and spec.wait_for is not None:
            return await self._wait_for(spec.wait_for, timeout_ms)
        action = await self._action_from(spec, params, reason=reason,
                                         timeout_ms=timeout_ms, risk=risk)
        return await self.surface.act(action)

    async def _wait_for(self, condition: Condition, timeout_ms: int):
        """Poll until a condition holds, or give up. Bounded, always."""
        deadline = time.monotonic() + timeout_ms / 1000
        attempts = 0
        while time.monotonic() < deadline:
            attempts += 1
            if evaluate(condition, await self._context()):
                self.journal.emit("wait.satisfied", condition=describe(condition),
                                  attempts=attempts)
                return _Ok(True)
            await self.surface.act(Action(ActionType.WAIT, timeout_ms=400,
                                          reason="poll interval"))
        self.journal.emit("wait.timed_out", condition=describe(condition), attempts=attempts)
        return _Ok(False, error=f"timed out waiting for {describe(condition)}")

    async def _build_action(self, step: Step, params: dict, *, for_login: bool = False) -> Action | None:
        return await self._action_from(step.action, params, reason=step.intent,
                                       timeout_ms=step.timeout_ms, risk=step.risk,
                                       for_login=for_login)

    async def _action_from(self, spec: StepAction, params: dict, *, reason: str,
                           timeout_ms: int = 8000, risk=None, for_login: bool = False) -> Action | None:
        value: str | None = None
        sensitive = False

        if spec.value_from is not None:
            if spec.value_from.param is not None:
                value = str(params.get(spec.value_from.param, ""))
                sensitive = bool(self.artifact.inputs.get(spec.value_from.param)
                                 and self.artifact.inputs[spec.value_from.param].sensitive)
            elif spec.value_from.literal is not None:
                value = spec.value_from.literal
            elif spec.value_from.credential is not None:
                ref = self.profile.profile.auth.credentials.get(spec.value_from.credential, "")
                value = self.credentials.resolve(ref) if ref else ""
                sensitive = True

        url = None
        if spec.url_template:
            url = self._render(spec.url_template, params)

        return Action(
            type=spec.type,
            target=spec.target,
            value=value,
            url=url,
            key=spec.key,
            timeout_ms=timeout_ms,
            risk=risk or RiskTier.READ_ONLY,
            reason=reason,
            sensitive=sensitive,
        )

    def _render(self, template: str, params: dict) -> str:
        out = template.replace("{base_url}", self.profile.base_url)
        for name, value in params.items():
            out = out.replace("{" + name + "}", str(value))
        return out

    def _trace(self, step: Step, *, ok: bool, started: float, resolved_by: str = "",
               degraded: bool = False, recoveries: tuple[str, ...] = (), note: str = "") -> None:
        self._traces.append(StepTrace(
            step_id=step.id, intent=step.intent, action=step.action.type.value, ok=ok,
            resolved_by=resolved_by, degraded=degraded, recoveries=recoveries, note=note,
            duration_ms=int((time.monotonic() - started) * 1000),
        ))

    def _resume_token(self) -> str | None:
        """The parked session's id, or None when there is no session to park.

        Without a lease the browser belongs to whoever constructed the engine
        and there is nothing to hand back to; returning the run id anyway would
        be a token that looks resumable and is not.
        """
        return self.lease.session_id if self.lease is not None else None

    def _common(self) -> dict:
        return {
            "capability_ref": self.artifact.ref,
            "tenant": self.tenant,
            "profile_hash": self.profile.hash,
            "run_id": self.run_id,
            "steps": tuple(self._traces),
            "duration_ms": int((time.monotonic() - self._started) * 1000),
        }


_MONEY = re.compile(r"-?[\d,]+(?:\.\d+)?")


def _parse(raw: str, spec: ParseSpec | None) -> Any:
    """Turn screen text into a typed value.

    Without this, every caller re-implements "strip the dollar sign" -- and a
    capability that returns "$4,210.75" as a string has not really declared a
    money output.
    """
    if spec is None or spec.type == "string":
        return raw.strip()

    if spec.type == "money":
        m = _MONEY.search(raw)
        if not m:
            raise ValueError("no numeric amount found")
        return m.group(0).replace(",", "")

    if spec.type in ("integer", "number"):
        m = _MONEY.search(raw)
        if not m:
            raise ValueError(f"no {spec.type} found")
        text = m.group(0).replace(",", "")
        return int(float(text)) if spec.type == "integer" else float(text)

    if spec.type == "regex":
        if not spec.pattern:
            raise ValueError("regex parse needs a pattern")
        m = re.search(spec.pattern, raw)
        if not m:
            raise ValueError(f"does not match {spec.pattern}")
        return m.group(spec.group)

    if spec.type == "date":
        return raw.strip()

    raise ValueError(f"unknown parse type {spec.type}")
