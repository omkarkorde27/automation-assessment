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
from ..observability.journal import Journal, MemoryJournal
from ..perception.model import Observation
from ..profiles.resolve import ResolvedProfile
from ..session.control import AUTOMATION, ControlLease
from ..surfaces.base import Action, ActionType, PolicyDenied, RiskTier, Surface
from .result import (
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

        self.run_id = f"run_{uuid.uuid4().hex[:12]}"
        self._traces: list[StepTrace] = []
        self._outputs: dict[str, Any] = {}
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
        return await self._file_for_failure(result)

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

        try:
            if session_failure := await self._ensure_session():
                return session_failure

            # Indexed rather than a plain `for`, because a mid-flow session
            # expiry rewinds the flow (profiles/schema.py: auth.reauth.resume_from).
            index = 0
            while index < len(self.artifact.steps):
                result = await self._run_step(self.artifact.steps[index],
                                              resolved_params, index=index)
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
        self.journal.emit("extraction.ok", output=extraction.output,
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

        self.journal.emit("run.finished", status="success", outputs=sorted(self._outputs))
        return Success(outputs=dict(self._outputs), **self._common())

    async def _business_outcome(self, outcome: OutcomeSpec, step_id: str) -> BusinessOutcome:
        message = outcome.description
        if outcome.message_from is not None:
            try:
                node, _ = resolve_node(outcome.message_from, await self.surface.observe())
                message = node.name or message
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
        verified, how = await self._verify_handback(target)
        self.journal.emit(
            "handback.verified" if verified else "handback.resync_failed",
            intervention=request.id, target_step=self._step_name(target),
            disposition=resolution.disposition.value, operator=resolution.operator,
            checked=how,
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
                        "intervention": request.id, "operator": resolution.operator},
            )

        if self.lease is not None:
            self.lease.resumed(reason=f"handback verified at {self._step_name(target)}")
        # Everything the human did happened on a screen we did not verify, so
        # nothing before this point is a safe resume point any more.
        self._last_checkpoint_index = min(self._last_checkpoint_index, target - 1)
        return _Resume(target)

    async def _verify_handback(self, target: int) -> tuple[bool, str]:
        """Is the live screen the one step `target` expects to start from?

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
                return True, "(unverified: the flow declares no success checkpoint)"
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

        return True, "(unverified: neither this step nor the one before it asserts anything)"

    async def _settle_for(self, condition: Condition, described: str,
                          attempts: int = 6) -> tuple[bool, str]:
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
            if evaluate(condition, await self._context()):
                return True, described
            if attempt == attempts - 1:
                break
            await self.surface.act(Action(ActionType.WAIT,
                                          timeout_ms=max(150, grace // attempts),
                                          reason="let the screen settle after a handback"))
        return False, described

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
            observation, png = await session.snapshot()
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
        """What was actually on screen, for a debuggable failure message."""
        observation = await self.surface.observe()
        interesting = [
            n for n in observation.nodes
            if n.visible and n.role in {"alert", "status", "dialog", "heading", "button"}
        ][:6]
        if not interesting:
            interesting = [n for n in observation.nodes if n.visible][:6]
        url = observation.frame_urls.get("content") or observation.url
        return f"url={url} | " + "; ".join(n.describe() for n in interesting)

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
