"""Escalation end to end: file, park, take over, hand back, resume.

The claim under test is Part 3.7's, and it is a behavioural one: a stuck run
parks *the same live session*, a person drives it through the same actuator, and
the engine re-verifies the screen before it takes the wheel back. So these run
against the real mockbank in a real browser -- an in-memory double would prove
the plumbing and none of the property.

`permission_denied` is the fault used to provoke it: mockbank returns an
authorization alert on member detail, the committed product profile declares
that as the `permission_wall` stuck pattern, and the ladder escalates. Nothing
about the escalation is staged for the test.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from cua.escalation import (
    Disposition,
    InterventionBroker,
    InterventionStatus,
    InterventionStore,
    LiveSession,
    Resolution,
)
from cua.observability.journal import MemoryJournal
from cua.profiles import ProfileRepository
from cua.replay import Escalated, Failure, ReplayEngine, Success
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
    """A run wired for takeover: lease, broker, live session, engine.

    Returns a builder so each test decides which artifact and which fault.
    """
    log = MemoryJournal()
    store = InterventionStore(tmp_path / "evidence")
    broker = InterventionBroker(store, journal=log, wait_timeout_s=10,
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
            broker=broker, lease=lease, goal="read a member's savings balance",
        )

    return type("Parked", (), {
        "build": staticmethod(build), "broker": broker, "store": store,
        "lease": lease, "session": session, "journal": log, "surface": surface,
    })


async def _until_parked(broker, timeout=15.0):
    """Wait for a run to file a takeover intervention and stop on it."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        open_now = [r for r in broker.store.list() if r.takeover
                    and r.id in broker._waiters]
        if open_now and broker.sessions["sess_test"].lease.state \
                is ControlState.PAUSED_PENDING_HUMAN:
            return open_now[0]
        await asyncio.sleep(0.05)
    raise AssertionError("the run never parked")


# ---- with nobody listening -----------------------------------------------

async def test_without_a_broker_an_escalation_is_exactly_what_it_always_was(
        signed_in, page, profile, live_server):
    """The offline default. Attaching a console must not change the contract a
    calling agent sees when there is no console."""
    arm(live_server, "permission_denied")
    engine = ReplayEngine(WebSurface(page), lookup_balance_artifact(), profile,
                          credentials=CREDS, tenant="demo-cu")
    result = await engine.run(PARAMS)

    assert isinstance(result, Escalated), result.describe()
    assert result.reason_class == "PERMISSION_REQUIRED"
    assert result.intervention_id is None
    assert result.resume_token is None


# ---- filing ---------------------------------------------------------------

async def test_a_stuck_run_files_a_request_a_stranger_could_act_on(
        signed_in, parked, live_server, tmp_path):
    arm(live_server, "permission_denied")
    task = asyncio.create_task(parked.build().run(PARAMS))
    request = await _until_parked(parked.broker)

    assert request.reason_class == "PERMISSION_REQUIRED"
    assert request.takeover is True
    assert request.status is InterventionStatus.OPEN
    assert request.capability_ref.startswith("member.lookup_balance@")
    assert request.goal == "read a member's savings balance"
    assert request.session_id == "sess_test"
    assert request.step_id and request.step_index is not None
    assert "authorized" in request.human_message.lower()
    assert request.observed, "an operator cannot triage without what was on screen"
    assert request.expected, "observed with nothing to compare it against is half a report"

    # Redacted evidence, on disk, beside the journal that caused it.
    assert request.screenshot_ref and request.observation_ref
    assert (tmp_path / "evidence" / "runs" / request.run_id
            / "interventions" / f"{request.id}.png").exists()

    parked.broker.resolve(request.id, Resolution(
        disposition=Disposition.ABANDON, operator="alice", note="cleanup"))
    await task


async def test_filing_parks_the_lease_so_nothing_acts_while_nobody_owns_it(
        signed_in, parked, live_server):
    arm(live_server, "permission_denied")
    task = asyncio.create_task(parked.build().run(PARAMS))
    request = await _until_parked(parked.broker)

    assert parked.lease.state is ControlState.PAUSED_PENDING_HUMAN
    assert parked.lease.holder is None

    # The console cannot act either: taking control is a separate, recorded act.
    from cua.escalation import UnknownNode  # noqa: F401
    result = await parked.session.forward(
        operator="alice", kind="click", node_id=_any_button(parked), reason="jumping the queue")
    assert result.ok is False
    assert "control lease" in (result.error or "")
    assert result.error_detail["denied_by"] == "control_lease"

    parked.broker.resolve(request.id, Resolution(
        disposition=Disposition.ABANDON, operator="alice", note="cleanup"))
    await task


def _any_button(parked) -> str:
    obs = parked.session._observation
    assert obs is not None, "snapshot() should have run when the request was filed"
    return next(n.node_id for n in obs.nodes if n.role in ("button", "link") and n.visible)


# ---- takeover and handback ------------------------------------------------

async def _pick(session, role: str, pattern: str, *, frame=("content",),
                timeout: float = 8.0):
    """Find one node the way an operator does: by what it says on screen.

    Polls, because `forward()` returns when the action is delivered, not when
    the app has finished responding to it. A person watching the console waits
    for the screen to change; a test has to say so.
    """
    import re

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    seen: list = []
    while loop.time() < deadline:
        observation, _ = await session.snapshot()
        # Matched against `describe()`, which is exactly the line the console
        # shows the operator -- an unnamed textbox reads as its row label there,
        # because that is the only thing a person could pick it by.
        seen = [n for n in observation.nodes
                if n.role == role and n.visible and n.frame_path == frame
                and re.search(pattern, n.describe(), re.I)]
        if len(seen) == 1:
            return seen[0]
        await asyncio.sleep(0.2)
    raise AssertionError(
        f"expected one {role} matching {pattern!r}, got {[n.describe() for n in seen] or 'none'}")


async def _redo_the_search(parked, member_id: str = "12345") -> None:
    """The operator clicks their way back to the search results.

    Three ordinary actions through the console, each one going through the same
    `act()` the engine uses. Nothing here is a test shortcut into the browser.
    """
    async def do(**kw):
        result = await parked.session.forward(operator="alice", **kw)
        assert result.ok, result.error

    nav = await _pick(parked.session, "link", r"member search", frame=("nav",))
    await do(kind="click", node_id=nav.node_id, reason="back to member search")
    box = await _pick(parked.session, "textbox", r"row 'Member ID'")
    await do(kind="fill", node_id=box.node_id, value=member_id, reason="re-enter the id")
    button = await _pick(parked.session, "button", r'"Search"')
    await do(kind="click", node_id=button.node_id, reason="re-run the search")


async def test_an_operator_redoes_the_screen_and_the_engine_re_enters_the_same_step(
        signed_in, parked, live_server):
    """The whole claim, in one test: one browser, one login, control changing
    hands, and the flow finishing.

    The operator does not repair anything magical -- they click the same
    controls the recording clicks, through the same actuator, and every one of
    those clicks is journaled as `human.action`. Then they let go, the engine
    checks that the screen really is the one step s5 starts from, and re-runs
    the step that failed.
    """
    arm(live_server, "permission_denied")
    task = asyncio.create_task(parked.build().run(PARAMS))
    request = await _until_parked(parked.broker)

    parked.broker.take(request.id, "alice")
    assert parked.lease.state is ControlState.HUMAN_OWNED
    assert parked.store.get(request.id).status is InterventionStatus.TAKEN

    await _redo_the_search(parked)

    parked.broker.resolve(request.id, Resolution(
        disposition=Disposition.RESUME, operator="alice",
        note="authorization blip cleared; search results are back up"))

    result = await asyncio.wait_for(task, timeout=45)
    assert isinstance(result, Success), result.describe()
    assert result.outputs.get("savings_balance") is not None

    # Control went round the whole cycle, and every hop is on the record.
    assert [t.to.value for t in parked.lease.history] == [
        "PAUSED_PENDING_HUMAN", "HUMAN_OWNED", "RESUMING", "AUTOMATION_OWNED"]
    verified = parked.journal.first("handback.verified")
    assert verified is not None and verified.data["disposition"] == "resume"
    assert verified.data["target_step"] == "s5"

    # Three human actions, each with the operator's own reason attached.
    humans = parked.journal.of("human.action")
    assert [e.data["action"] for e in humans] == ["click", "fill", "click"]
    assert all(e.data["operator"] == "alice" and e.data["reason"] for e in humans)
    assert parked.store.get(request.id).status is InterventionStatus.RESOLVED


async def test_a_step_the_operator_did_by_hand_is_not_done_again(
        signed_in, parked, live_server):
    """"I completed that step myself" advances by exactly one and verifies THAT.

    On the last step there is no next step to check, so what gets checked is the
    flow's success condition -- otherwise the run walks straight into extraction
    against whatever screen the operator happened to leave behind.
    """
    arm(live_server, "permission_denied")
    task = asyncio.create_task(parked.build().run(PARAMS))
    request = await _until_parked(parked.broker)

    parked.broker.take(request.id, "alice")
    # The permission wall replaced the results screen, so the operator redoes
    # the search before they can open the record -- exactly as they would.
    await _redo_the_search(parked)
    link = await _pick(parked.session, "link", r"12345")
    acted = await parked.session.forward(
        operator="alice", kind="click", node_id=link.node_id,
        reason="open the member record by hand now the blip has cleared")
    assert acted.ok, acted.error

    parked.broker.resolve(request.id, Resolution(
        disposition=Disposition.STEP_COMPLETED, operator="alice",
        note="I opened the record myself"))

    result = await asyncio.wait_for(task, timeout=45)
    assert isinstance(result, Success), result.describe()

    verified = parked.journal.first("handback.verified")
    assert verified is not None
    assert verified.data["disposition"] == "step_completed"
    assert verified.data["target_step"] == "(end of flow)"
    assert "success state" in verified.data["checked"]

    # s5 ran once, by the human. The engine did not click it again.
    assert not [e for e in parked.journal.of("step.started")
                if e.data["step"] == "s5" and e.at > verified.at]


async def test_abandoning_ends_the_run_as_escalated_and_names_the_request(
        signed_in, parked, live_server):
    arm(live_server, "permission_denied")
    task = asyncio.create_task(parked.build().run(PARAMS))
    request = await _until_parked(parked.broker)

    parked.broker.take(request.id, "alice")
    parked.broker.resolve(request.id, Resolution(
        disposition=Disposition.ABANDON, operator="alice",
        note="the service account genuinely lacks the entitlement"))

    result = await asyncio.wait_for(task, timeout=30)
    assert isinstance(result, Escalated)
    assert result.intervention_id == request.id
    assert result.resume_token == "sess_test"
    assert parked.lease.state is ControlState.ABANDONED
    assert parked.store.get(request.id).status is InterventionStatus.ABANDONED


async def test_nobody_answering_abandons_rather_than_holding_the_browser_forever(
        signed_in, parked, live_server):
    arm(live_server, "permission_denied")
    parked.broker.wait_timeout_s = 0.4
    result = await asyncio.wait_for(parked.build().run(PARAMS), timeout=30)

    assert isinstance(result, Escalated)
    assert result.intervention_id
    timed_out = parked.journal.first("intervention.timed_out")
    assert timed_out is not None
    assert parked.store.get(result.intervention_id).status is InterventionStatus.ABANDONED
    # And the lease is released, not left advertising a takeover that can no
    # longer happen: an operator opening the console later finds a wheel
    # attached to nothing.
    assert parked.lease.state is ControlState.ABANDONED


async def test_a_handback_onto_the_wrong_screen_fails_loudly(
        signed_in, parked, live_server):
    """The operator says it is ready and it is not. Believing them is the bug.

    Resync is bounded to {same step, next step}: there is no search for a step
    that happens to match, so the only two outcomes are re-entry or a failure
    naming the precondition that did not hold.
    """
    arm(live_server, "permission_denied")
    task = asyncio.create_task(parked.build().run(PARAMS))
    request = await _until_parked(parked.broker)

    parked.broker.take(request.id, "alice")
    # Drive the session somewhere the flow does not expect, then claim the step
    # is done -- so the NEXT step's precondition is checked against a stranger.
    await parked.session.forward(
        operator="alice", kind="navigate", url=f"{live_server}/t/demo-cu/",
        reason="wander off")
    parked.broker.resolve(request.id, Resolution(
        disposition=Disposition.STEP_COMPLETED, operator="alice",
        note="I did it by hand (they did not)"))

    result = await asyncio.wait_for(task, timeout=30)
    assert isinstance(result, Failure), result.describe()
    assert result.detail.get("intervention") == request.id
    assert result.detail.get("operator") == "alice"
    assert parked.journal.first("handback.resync_failed") is not None
    assert parked.lease.state is ControlState.ABANDONED


# ---- failure-side filing ---------------------------------------------------

async def test_a_checkpoint_failure_stays_a_failure_and_still_reaches_an_operator(
        signed_in, parked, live_server):
    """Part 3.7 routes it to escalation; Part 3.5 says the caller should debug
    it. Both are right, and they are answers to different questions."""
    artifact = lookup_balance_artifact()
    broken = artifact.steps[-1].model_copy(update={"retries": 0})
    from cua.conditions.model import ElementCondition
    broken = broken.model_copy(update={"checkpoint": ElementCondition(
        element={"role": "heading", "name_matches": "(?i)this heading does not exist"},
        exists=True)})
    artifact = artifact.model_copy(
        update={"steps": artifact.steps[:-1] + (broken,)})

    result = await parked.build(artifact).run(PARAMS)

    assert isinstance(result, Failure), result.describe()
    assert result.failure_class.value == "CHECKPOINT_FAILED"
    assert result.intervention_id, "an operator never heard about this"

    filed = parked.store.get(result.intervention_id)
    assert filed.takeover is False, "the run has ended; do not offer a wheel"
    assert filed.result_status == "failed"
    assert filed.reason_class == "CHECKPOINT_FAILED"
    # And it did not park: nothing waited, the lease is untouched.
    assert parked.lease.state is ControlState.AUTOMATION_OWNED


async def test_a_caller_error_does_not_page_anybody(parked):
    """PARAM_INVALID opened nothing. There is no screen and no session, so
    there is nothing for a person in a back office to do about it."""
    result = await parked.build().run({"member_id": ""})
    assert isinstance(result, Failure)
    assert result.failure_class.value == "PARAM_INVALID"
    assert result.intervention_id is None
    assert parked.store.list() == []
