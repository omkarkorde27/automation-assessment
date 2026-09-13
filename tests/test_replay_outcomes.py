"""Deterministic replay against the live fixture -- the highest-value suite.

What is under test is not "does it work", it is **how each condition gets
classified**. The brief's glossary names the mistake: treating "no such member"
as a crash. So every fault mockbank can inject is replayed here, and each one
asserts which of the four result variants comes back:

    not_found          -> business_outcome   (a legitimate answer)
    permission_denied  -> escalated          (nobody declared it; a human decides)
    interstitial       -> success            (recovered silently, journaled)
    slow_load          -> success            (waited out, journaled)
    error_500          -> failure            (the app faulted; terminal)
    session_timeout    -> success            (re-authenticated, resumed)

Getting any of these into the wrong bucket is the defect this project is
mostly about, which is why the assertions are on the variant rather than on
`ok`.
"""

from __future__ import annotations

import httpx
import pytest

from cua.conditions.model import ElementCondition, ElementMatch
from cua.observability import MemoryJournal
from cua.profiles import ProfileRepository
from cua.replay import BusinessOutcome, Escalated, Failure, FailureClass, ReplayEngine, Success
from cua.replay.engine import CredentialResolver
from cua.surfaces.web_playwright import WebSurface
from factories import lookup_balance_artifact

from cua.artifact.schema import OutcomeSpec

CREDS = CredentialResolver({"MOCKBANK_USER": "operator", "MOCKBANK_PASS": "demo-pass-not-real"})


@pytest.fixture
def profile(live_server):
    """The committed demo-cu profile, pointed at the ephemeral test server."""
    resolved = ProfileRepository("profiles").resolve("demo-cu")
    surface = resolved.profile.surface.model_copy(
        update={"base_url": f"{live_server}/t/demo-cu"}
    )
    return resolved.__class__(
        profile=resolved.profile.model_copy(update={"surface": surface}),
        lineage=resolved.lineage,
        hash=resolved.hash,
    )


@pytest.fixture
def engine_for(page, profile):
    def build(artifact=None, journal=None, tenant="demo-cu"):
        return ReplayEngine(
            WebSurface(page),
            artifact or lookup_balance_artifact(),
            profile,
            journal=journal or MemoryJournal(),
            credentials=CREDS,
            tenant=tenant,
        )
    return build


@pytest.fixture
async def signed_in(page, live_server):
    """A page that already holds a valid session.

    Fault tests need this. A fault is armed for the NEXT guarded request, and an
    unauthenticated run's next guarded request is part of signing in -- so the
    fault would fire during login and be spent before the flow under test began.
    Production replays usually start with a warm session anyway.
    """
    await page.goto(f"{live_server}/t/demo-cu/login", wait_until="networkidle")
    await page.fill("input[type=text]", "operator")
    await page.fill("input[type=password]", "demo-pass-not-real")
    await page.click("input[type=submit]")
    await page.wait_for_timeout(300)
    return page


def arm(live_server, fault, tenant="demo-cu", count=1):
    httpx.post(f"{live_server}/t/{tenant}/__control",
               params={"fault": fault, "count": count}, timeout=10)


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------

async def test_replay_succeeds_and_returns_typed_outputs(engine_for):
    journal = MemoryJournal()
    result = await engine_for(journal=journal).run({"member_id": "12345"})

    assert isinstance(result, Success), getattr(result, "describe", lambda: result)()
    assert result.outputs == {"savings_balance": "4210.75"}, "money must be parsed, not raw text"
    assert [s.step_id for s in result.steps] == ["s1", "s2", "s3", "s4", "s5"]
    assert all(s.ok for s in result.steps)
    assert "run.finished" in journal.kinds()


async def test_replay_signs_itself_in_using_the_profile_recipe(engine_for):
    """Auth is an app fact from the profile, executed by the same step machinery
    as any other step -- not a special path inside the engine."""
    journal = MemoryJournal()
    await engine_for(journal=journal).run({"member_id": "12345"})

    assert "session.expired" in journal.kinds()
    assert "session.reauth_ok" in journal.kinds()


async def test_the_same_artifact_replays_on_the_other_tenant(page, live_server):
    """One recording, two institutions -- the multi-tenant claim, executed."""
    resolved = ProfileRepository("profiles").resolve("valley-cu")
    surface = resolved.profile.surface.model_copy(
        update={"base_url": f"{live_server}/t/valley-cu"})
    valley = resolved.__class__(
        profile=resolved.profile.model_copy(update={"surface": surface}),
        lineage=resolved.lineage, hash=resolved.hash,
    )
    engine = ReplayEngine(WebSurface(page), lookup_balance_artifact(), valley,
                          credentials=CREDS, tenant="valley-cu")

    result = await engine.run({"member_id": "12345"})
    assert isinstance(result, Success), getattr(result, "describe", lambda: result)()
    assert result.outputs == {"savings_balance": "4210.75"}


async def test_the_run_records_which_locator_candidate_won(engine_for):
    """The drift signal. Degraded resolutions are normal here -- the member-id
    field can only be reached by its anchor candidate."""
    result = await engine_for().run({"member_id": "12345"})
    assert isinstance(result, Success)
    assert result.resolution_summary.get("anchor_relative", 0) >= 1
    assert result.degraded_count >= 1


# --------------------------------------------------------------------------
# business outcomes -- legitimate answers, NOT failures
# --------------------------------------------------------------------------

async def test_unknown_member_is_a_business_outcome_not_a_failure(engine_for):
    """The mistake the brief names. This is the test that matters most."""
    journal = MemoryJournal()
    result = await engine_for(journal=journal).run({"member_id": "99999"})

    assert isinstance(result, BusinessOutcome)
    assert not isinstance(result, Failure)
    assert result.code == "MEMBER_NOT_FOUND"
    assert result.at_step == "s4"
    assert result.ok is True, "the capability answered the question it was asked"
    assert "No records found" in result.message, "the institution's own wording"


async def test_injected_not_found_is_classified_the_same_way(signed_in, engine_for, live_server):
    arm(live_server, "not_found")
    result = await engine_for().run({"member_id": "12345"})
    assert isinstance(result, BusinessOutcome) and result.code == "MEMBER_NOT_FOUND"


async def test_the_outcome_is_detected_before_the_checkpoint(engine_for):
    """Ladder ordering, observed rather than assumed: the outcome fires at s4
    and no checkpoint failure is ever recorded."""
    journal = MemoryJournal()
    await engine_for(journal=journal).run({"member_id": "99999"})

    kinds = journal.kinds()
    assert "outcome.detected" in kinds
    assert "checkpoint.failed" not in kinds


# --------------------------------------------------------------------------
# recoverable conditions -- handled and journaled, never surfaced
# --------------------------------------------------------------------------

async def test_an_obstructing_modal_is_dismissed_and_the_run_still_succeeds(
    signed_in, engine_for, live_server
):
    """A dismissed interstitial is none of the caller's business."""
    journal = MemoryJournal()
    arm(live_server, "interstitial", count=2)
    result = await engine_for(journal=journal).run({"member_id": "12345"})

    assert isinstance(result, Success), getattr(result, "describe", lambda: result)()
    attempted = [e.data["recovery"] for e in journal.of("recovery.attempted")]
    assert "dismiss_marketing_interstitial" in attempted
    assert journal.of("recovery.succeeded")


async def test_transient_slowness_is_waited_out(signed_in, engine_for, live_server):
    """mockbank serves a real self-refreshing loading page, so this exercises
    waiting for a condition rather than sleeping a fixed time."""
    journal = MemoryJournal()
    arm(live_server, "slow_load")
    result = await engine_for(journal=journal).run({"member_id": "12345"})

    assert isinstance(result, Success), getattr(result, "describe", lambda: result)()
    assert "wait_out_spinner" in [e.data["recovery"] for e in journal.of("recovery.attempted")]


async def test_an_expired_session_is_re_authenticated_mid_flow(signed_in, engine_for, live_server):
    journal = MemoryJournal()
    arm(live_server, "session_timeout", count=1)
    result = await engine_for(journal=journal).run({"member_id": "12345"})

    assert isinstance(result, Success), getattr(result, "describe", lambda: result)()
    assert journal.of("session.reauth_ok")


async def test_recoveries_are_bounded(signed_in, engine_for, live_server):
    """A permanently-triggering condition must exhaust a budget, not spin."""
    journal = MemoryJournal()
    arm(live_server, "interstitial", count=99)
    engine = engine_for(journal=journal)
    engine.max_recoveries_per_run = 2
    engine._recovery_budget = 2
    await engine.run({"member_id": "12345"})

    assert len(journal.of("recovery.attempted")) <= 3


# --------------------------------------------------------------------------
# hard failures -- terminal, debuggable
# --------------------------------------------------------------------------

async def test_an_application_error_is_a_hard_failure(signed_in, engine_for, live_server):
    journal = MemoryJournal()
    arm(live_server, "error_500")
    result = await engine_for(journal=journal).run({"member_id": "12345"})

    assert isinstance(result, Failure)
    assert result.failure_class is FailureClass.SURFACE_ERROR
    assert result.detail.get("hard_failure") == "APP_ERROR"
    assert "hard_failure.detected" in journal.kinds()


async def test_a_failure_says_what_step_what_was_expected_and_what_was_seen(engine_for):
    """The debuggability requirement from §3.3, asserted rather than asserted-to."""
    artifact = lookup_balance_artifact()
    steps = list(artifact.steps)
    steps[1] = steps[1].model_copy(update={
        "checkpoint": ElementCondition(
            element=ElementMatch(role="heading", name="A Screen That Does Not Exist")),
        "retries": 0,
    })
    result = await engine_for(artifact=artifact.model_copy(update={"steps": tuple(steps)})).run(
        {"member_id": "12345"})

    assert isinstance(result, Failure)
    assert result.failure_class is FailureClass.CHECKPOINT_FAILED
    assert result.at_step == "s2"
    assert "A Screen That Does Not Exist" in result.expected
    assert result.observed, "a failure with no observed state is not debuggable"
    assert "url=" in result.observed


async def test_a_missing_control_fails_without_guessing(engine_for):
    artifact = lookup_balance_artifact()
    steps = list(artifact.steps)
    ghost = steps[3].action.target.model_copy(update={
        "candidates": (steps[3].action.target.candidates[0].model_copy(
            update={"name": "Approve Wire Transfer"}),)})
    steps[3] = steps[3].model_copy(
        update={"action": steps[3].action.model_copy(update={"target": ghost})})

    result = await engine_for(artifact=artifact.model_copy(update={"steps": tuple(steps)})).run(
        {"member_id": "12345"})
    assert isinstance(result, Failure)
    assert result.failure_class is FailureClass.LOCATOR_UNRESOLVED


# --------------------------------------------------------------------------
# escalation vs declared outcome -- the layering
# --------------------------------------------------------------------------

async def test_an_undeclared_permission_wall_escalates(engine_for):
    """Nobody anticipated this state, so a human decides. The profile's
    stuck_pattern supplies the routing context."""
    result = await engine_for().run({"member_id": "45678"})   # restricted member

    assert isinstance(result, Escalated)
    assert result.reason_class == "PERMISSION_REQUIRED"
    assert "not authorized" in result.human_message.lower()


async def test_declaring_the_same_condition_turns_it_into_a_business_outcome(engine_for):
    """The layering, demonstrated: a capability that DECLARES permission denial
    gets a typed result and no human is paged. Same app state, different answer,
    because the artifact anticipated it."""
    artifact = lookup_balance_artifact()
    declared = artifact.model_copy(update={
        "outcomes": artifact.outcomes + (
            OutcomeSpec(
                code="PERMISSION_DENIED",
                description="The service account may not view this member.",
                detect=ElementCondition(
                    element=ElementMatch(role="alert", name_matches="(?i)not authorized"),
                    exists=True),
            ),
        )
    })
    result = await engine_for(artifact=declared).run({"member_id": "45678"})

    assert isinstance(result, BusinessOutcome)
    assert result.code == "PERMISSION_DENIED"


# --------------------------------------------------------------------------
# caller errors and preflight
# --------------------------------------------------------------------------

async def test_a_bad_parameter_fails_before_the_browser_is_touched(engine_for):
    journal = MemoryJournal()
    result = await engine_for(journal=journal).run({"member_id": "not-an-id"})

    assert isinstance(result, Failure)
    assert result.failure_class is FailureClass.PARAM_INVALID
    assert "action.executed" not in journal.kinds()
    assert result.steps == (), "nothing ran"


async def test_url_condition_on_a_surface_without_urls_is_rejected_before_acting(profile):
    """The named test from R-M3-1.

    A desktop surface cannot evaluate the artifact's URL checkpoints. Preflight
    must catch that before the first action -- finding out mid-flow, after an
    irreversible step, is the failure this ordering exists to prevent.
    """
    from cua.surfaces.desktop_stub import DesktopSurface

    surface = DesktopSurface("meridian-core.exe")
    engine = ReplayEngine(surface, lookup_balance_artifact(), profile, credentials=CREDS)
    journal = MemoryJournal()
    engine.journal = journal

    result = await engine.run({"member_id": "12345"})

    assert isinstance(result, Failure)
    assert result.failure_class is FailureClass.CAPABILITY_UNSUPPORTED
    assert "preflight.unsupported" in journal.kinds()
    assert result.steps == (), "preflight must run before anything is performed"
    assert "url" in result.observed
    # Every offending condition is named, not just the first.
    assert len(result.detail["problems"]) >= 3
