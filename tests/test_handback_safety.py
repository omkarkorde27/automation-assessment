"""R-M6-3 and R-M6-4 -- the two things running M5 for real turned up.

Both are about the same gap seen from opposite sides: the moment control comes
back from a person is the moment the system knows least, and M5 trusted two
things there that it had not checked.

**R-M6-3.** Handback was verified against the target step's condition, and a
condition can hold on a screen that is definitively wrong. `member.lookup_balance`
asserts its success by URL, and this application's authorization wall lives at
the same URL as the member record -- so an operator who took control, did
nothing, and pressed "I completed this step" got `handback.verified` on a
permission error. The run failed a step later at extraction, pointing at the
wrong thing. The fix is precedence, not a longer checkpoint: a profile-declared
bad screen is positive evidence of the wrong state, a passing condition is only
the absence of it, and the two are not equal weight.

**R-M6-4.** §3.7 offers the operator the headed browser window as well as the
console, and only the console goes through `act()`. A click in the window
reaches the page through the OS: no journal entry, no derived tier, no lease
check. It stays -- it is what an operator uses when the action vocabulary cannot
express the fix -- but the release now says so.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from cua.escalation import (
    Disposition,
    InterventionBroker,
    InterventionRequest,
    InterventionStore,
    LiveSession,
    Resolution,
)
from cua.escalation.console import create_console
from cua.observability.journal import MemoryJournal
from cua.profiles import ProfileRepository
from cua.replay import Failure, FailureClass, ReplayEngine, Success
from cua.replay.engine import CredentialResolver
from cua.session.control import ControlLease, ControlState
from cua.surfaces.web_playwright import WebSurface
from factories import lookup_balance_artifact

CREDS = CredentialResolver({"MOCKBANK_USER": "operator", "MOCKBANK_PASS": "demo-pass-not-real"})
PARAMS = {"member_id": "12345"}


@pytest.fixture
def profile(live_server):
    resolved = ProfileRepository("profiles").resolve("demo-cu")
    surface = resolved.profile.surface.model_copy(
        update={"base_url": f"{live_server}/t/demo-cu"})
    return resolved.__class__(
        profile=resolved.profile.model_copy(update={"surface": surface}),
        lineage=resolved.lineage, hash=resolved.hash)


@pytest.fixture
async def signed_in(page, live_server):
    await page.goto(f"{live_server}/t/demo-cu/login", wait_until="networkidle")
    await page.fill("input[type=text]", "operator")
    await page.fill("input[type=password]", "demo-pass-not-real")
    await page.click("input[type=submit]")
    await page.wait_for_timeout(300)
    return page


def arm(live_server, fault, tenant="demo-cu", count=1):
    httpx.post(f"{live_server}/t/{tenant}/__control",
               params={"fault": fault, "count": count}, timeout=10)


@pytest.fixture
def parked(tmp_path, page, profile):
    """A run wired for takeover -- the same shape `scripts/watch_takeover.py` uses."""
    log = MemoryJournal()
    store = InterventionStore(tmp_path / "evidence")
    broker = InterventionBroker(store, journal=log, wait_timeout_s=20,
                                evidence_root=tmp_path / "evidence")
    lease = ControlLease("sess_test", journal=log)
    surface = WebSurface(page, journal=log)
    surface.add_guard(lease)
    session = broker.register(LiveSession(
        session_id="sess_test", surface=surface, lease=lease, profile=profile, journal=log))

    def build(artifact=None):
        return ReplayEngine(
            surface, artifact or lookup_balance_artifact(), profile,
            journal=log, credentials=CREDS, tenant="demo-cu",
            broker=broker, lease=lease, goal="read a member's savings balance")

    return type("Parked", (), {
        "build": staticmethod(build), "broker": broker, "store": store,
        "lease": lease, "session": session, "journal": log, "surface": surface})


async def _until_parked(broker, timeout=20.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        open_now = [r for r in broker.store.list() if r.takeover and r.id in broker._waiters]
        if open_now and broker.sessions["sess_test"].lease.state \
                is ControlState.PAUSED_PENDING_HUMAN:
            return open_now[0]
        await asyncio.sleep(0.05)
    raise AssertionError("the run never parked")


# ==========================================================================
# R-M6-3 -- a declared stuck pattern outranks a passing checkpoint
# ==========================================================================

async def test_handback_is_refused_when_a_stuck_pattern_is_on_screen_even_though_the_checkpoint_passes(
        signed_in, parked, live_server):
    """The `none-step` scenario, frozen.

    Park at the permission wall. The operator takes control, does nothing at
    all, and reports the step done. The flow's success checkpoint -- `url
    matches /members/\\d+$` -- is TRUE of the screen in front of us, because the
    authorization page is served at the member's own URL. Before R-M6-3 this
    produced `handback.verified` and a run that failed one step later at
    extraction.
    """
    arm(live_server, "permission_denied")
    task = asyncio.create_task(parked.build().run(PARAMS))
    request = await _until_parked(parked.broker)

    parked.broker.take(request.id, "Omkar")
    parked.broker.resolve(request.id, Resolution(
        disposition=Disposition.STEP_COMPLETED, operator="Omkar",
        note="did nothing at all"))
    result = await task

    assert isinstance(result, Failure), getattr(result, "describe", lambda: result)()
    assert result.failure_class is FailureClass.PRECONDITION_FAILED, (
        f"expected the handback to be refused; got {result.describe()}"
    )
    assert result.detail["blocked_by"] == "PERMISSION_REQUIRED"
    assert "handback.verified" not in parked.journal.kinds(), (
        "the engine must not report a verification it did not earn"
    )

    blocked = parked.journal.of("handback.blocked_by_pattern")[0]
    assert blocked.data["pattern_kind"] == "stuck_pattern"
    assert blocked.data["code"] == "PERMISSION_REQUIRED"
    # And the condition that WOULD have passed is on the record beside it --
    # otherwise the next person to read this cannot see why it was close.
    assert "members" in blocked.data["would_have_checked"]


async def test_the_refusal_names_the_pattern_and_the_intervention_it_came_from(
        signed_in, parked, live_server):
    """A refusal an operator cannot act on is an outage with extra steps."""
    arm(live_server, "permission_denied")
    task = asyncio.create_task(parked.build().run(PARAMS))
    request = await _until_parked(parked.broker)

    parked.broker.take(request.id, "Rae")
    parked.broker.resolve(request.id, Resolution(
        disposition=Disposition.RESUME, operator="Rae", note="looks fine to me"))
    result = await task

    assert isinstance(result, Failure)
    assert result.detail["intervention"] == request.id
    assert result.detail["operator"] == "Rae"
    assert "PERMISSION_REQUIRED" in result.expected
    assert "not authorized" in result.expected, "the profile's own words, not ours"

    resync = parked.journal.of("handback.resync_failed")[0]
    assert resync.data["blocked_by"] == "PERMISSION_REQUIRED"
    assert parked.lease.state is ControlState.ABANDONED, (
        "a refused handback does not quietly leave the wheel with nobody"
    )


async def as_step(session, artifact, step_id: str, operator="Omkar", **kw):
    """Forward the action a recorded step would have taken, as a human.

    The operator's picks are resolved from the artifact's own bundles so the
    test does not hard-code this application's churning element ids. It also
    makes the point the design rests on: a human's action and a recorded step
    address the same control in the same vocabulary, which is what makes
    promoting one into the other a copy rather than a translation.
    """
    from cua.locators.resolve import resolve_node

    observation, _ = await session.snapshot()
    step = next(s for s in artifact.steps if s.id == step_id)
    node, _ = resolve_node(step.action.target, observation)
    return await session.forward(
        operator=operator, kind=step.action.type.value, node_id=node.node_id,
        reason=f"operator repeating {step_id}: {step.intent}", **kw)


async def test_a_clean_screen_still_hands_back_on_the_first_matching_condition(
        signed_in, parked, live_server):
    """The guard must not be over-broad, or every handback becomes a refusal.

    Here the operator does what an operator actually does: walks the flow back
    to the search results through the console -- three picks -- and releases.
    The `permission_denied` fault is single-shot and has been spent, so the
    record opens on the retry. Nothing declared matches the screen, and the
    ordinary R-M5-1 check decides.

    Worth saying because a reader will ask: the operator's three actions are
    load-bearing, not decoration. Doing nothing and releasing is the test above,
    and it fails.
    """
    artifact = lookup_balance_artifact()
    arm(live_server, "permission_denied")
    task = asyncio.create_task(parked.build(artifact).run(PARAMS))
    request = await _until_parked(parked.broker)

    parked.broker.take(request.id, "Omkar")
    await as_step(parked.session, artifact, "s2")                       # member search
    await as_step(parked.session, artifact, "s3", value=PARAMS["member_id"])
    await as_step(parked.session, artifact, "s4")                       # run the search
    parked.broker.resolve(request.id, Resolution(
        disposition=Disposition.RESUME, operator="Omkar",
        note="walked back to the search results; the record should open now"))
    result = await task

    assert isinstance(result, Success), getattr(result, "describe", lambda: result)()
    assert result.outputs["savings_balance"] == "4210.75"
    assert parked.journal.of("handback.blocked_by_pattern") == []
    assert parked.journal.of("handback.verified")[0].data["blocked_by"] == ""
    assert len(parked.journal.of("human.action")) == 3


# ==========================================================================
# R-M6-4 -- direct-window driving is detected, not silently trusted
# ==========================================================================

async def test_a_screen_change_with_no_forwarded_action_is_journaled_as_unsanctioned(
        signed_in, parked, live_server):
    """The case §3.7 makes possible and the choke point cannot see.

    `page.goto` is the honest model of it: it drives the same browser without
    passing through `act()`, exactly as an operator's click in the window does.
    """
    request = InterventionRequest(
        run_id="run_x", session_id="sess_test", reason_class="PERMISSION_REQUIRED",
        human_message="unblock this", takeover=True)
    parked.broker.open(request)
    parked.broker.take(request.id, "Omkar")
    await parked.broker.mark_taken(request)

    await signed_in.goto(f"{live_server}/t/demo-cu/members/12345", wait_until="networkidle")
    audited = await parked.broker.audit_release(request)

    assert audited.unsanctioned_change is True
    assert parked.store.get(request.id).unsanctioned_change is True, "and it is on disk"

    flagged = parked.journal.of("handback.unsanctioned_change")[0]
    assert flagged.data["changed"] is True
    assert flagged.data["forwarded_actions"] == 0
    assert flagged.data["screen_before"] != flagged.data["screen_after"]
    assert "not recorded anywhere" in flagged.data["note"]


async def test_console_driven_changes_are_not_flagged(signed_in, parked, live_server):
    """The discriminator is the journal, not the screen.

    The fingerprints differ here too -- the operator did move the session. What
    makes this case different is that the move came through `act()`, so it is
    already recorded, risk-derived and attributable.
    """
    request = InterventionRequest(
        run_id="run_x", session_id="sess_test", reason_class="REQUIRES_HUMAN",
        human_message="unblock this", takeover=True)
    parked.broker.open(request)
    parked.broker.take(request.id, "Omkar")
    await parked.broker.mark_taken(request)

    await parked.session.forward(
        operator="Omkar", kind="navigate",
        url=f"{live_server}/t/demo-cu/members/12345", reason="open the record")
    audited = await parked.broker.audit_release(request)

    assert audited.unsanctioned_change is False
    assert parked.journal.of("handback.unsanctioned_change") == []
    audit = parked.journal.of("handback.audited")[0]
    assert audit.data["changed"] is True, "the screen did move..."
    assert audit.data["forwarded_actions"] == 1, "...and the console says how"


async def test_an_operator_who_changed_nothing_is_not_flagged(signed_in, parked, live_server):
    """Looking at a screen is not an unrecorded action."""
    await signed_in.goto(f"{live_server}/t/demo-cu/members", wait_until="networkidle")
    request = InterventionRequest(
        run_id="run_x", session_id="sess_test", reason_class="REQUIRES_HUMAN",
        human_message="have a look", takeover=True)
    parked.broker.open(request)
    parked.broker.take(request.id, "Omkar")
    await parked.broker.mark_taken(request)

    audited = await parked.broker.audit_release(request)

    assert audited.unsanctioned_change is False
    assert parked.journal.of("handback.audited")[0].data["changed"] is False


async def test_the_fingerprint_ignores_this_applications_id_churn(signed_in, live_server, profile):
    """mockbank regenerates element ids on every render, on purpose.

    A fingerprint that included them would report a change on every reload, and
    R-M6-4 would flag every takeover -- which is the same as flagging none.
    """
    session = LiveSession(session_id="sess_x", surface=WebSurface(signed_in),
                          lease=ControlLease("sess_x"), profile=profile)
    await signed_in.goto(f"{live_server}/t/demo-cu/members", wait_until="networkidle")
    first = await session.fingerprint()
    await signed_in.reload(wait_until="networkidle")
    assert await session.fingerprint() == first

    await signed_in.goto(f"{live_server}/t/demo-cu/members/12345", wait_until="networkidle")
    assert await session.fingerprint() != first, "but a different screen is a different screen"


# ==========================================================================
# the wiring -- the hooks are useless if the routes do not call them
# ==========================================================================

async def test_the_console_audits_a_take_and_release_it_served(
        signed_in, parked, live_server):
    """Driven over the console's own HTTP surface, in this loop.

    The broker hooks are only worth anything if the two routes an operator
    actually touches invoke them, in the right order -- the release audit has to
    run BEFORE `resolve` wakes the run, or the screen it photographs is one the
    engine has already started driving.
    """
    request = InterventionRequest(
        run_id="run_x", session_id="sess_test", reason_class="REQUIRES_HUMAN",
        human_message="unblock this", takeover=True)
    parked.broker.open(request)

    app = create_console(broker=parked.broker, store=parked.store, journal=parked.journal)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://console") as client:
        taken = await client.post(f"/api/interventions/{request.id}/take",
                                  json={"operator": "Omkar"})
        assert taken.status_code == 200

        # The operator drives the window instead of the node list.
        await signed_in.goto(f"{live_server}/t/demo-cu/members/12345",
                             wait_until="networkidle")

        released = await client.post(f"/api/interventions/{request.id}/resolve",
                                     json={"operator": "Omkar", "disposition": "resume",
                                           "note": "fixed it in the window"})
        assert released.status_code == 200
        assert released.json()["unsanctioned_change"] is True

    assert parked.journal.of("handback.unsanctioned_change")
