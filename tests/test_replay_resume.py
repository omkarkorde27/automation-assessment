"""Resuming a flow after the session drops mid-way.

The profile declares `auth.reauth.resume_from` (§3.3.1) and, until this suite
existed, nothing in `src/` read it: the engine always re-ran the interrupted
step. That works only when the step is self-sufficient -- a `navigate` -- and
the single test covering it happened to arm the fault on exactly such a step.
Anywhere deeper, re-authentication lands on the app's post-login screen and the
re-run step looks for a control that is no longer there, reporting
LOCATOR_UNRESOLVED for a perfectly healthy application.

So the interesting axis is WHERE the session dies, and these tests sweep it.

The irreversible flow is here for a different reason. Getting the resume point
wrong on `member.lookup_balance` costs a wasted run; getting it wrong on
`member.open_subaccount` opens a real account twice. "Exactly one" is asserted
against the fixture's own ledger, not inferred from the result variant.
"""

from __future__ import annotations

import httpx
import pytest

from cua.artifact.schema import CapabilityArtifact, Step, StepAction
from cua.conditions.model import (
    AllCondition, ElementCondition, ElementMatch, UrlCondition, UrlMatch,
)
from cua.observability import MemoryJournal
from cua.profiles import ProfileRepository
from cua.replay import (
    BusinessOutcome, Escalated, Failure, FailureClass, ReplayEngine, Success,
)
from cua.replay.engine import CredentialResolver
from cua.surfaces.web_playwright import WebSurface
from factories import CONTENT, bundle, lookup_balance_artifact, open_subaccount_artifact
from cua.locators.model import RoleNameExact
from mockbank import data

RETURN_TO_MEMBER_LINK = bundle(
    "return_to_member_link",
    RoleNameExact(role="link", name="Return to Member Detail"),
    notes="The done screen's own link back to the record. Test-only continuation step.",
)

CREDS = CredentialResolver({"MOCKBANK_USER": "operator", "MOCKBANK_PASS": "demo-pass-not-real"})

SUBACCOUNT_PARAMS = {"member_id": "12345", "product_code": "HSA", "initial_deposit": "50.00"}


class ArmAtStep(MemoryJournal):
    """Arms a fault when a named step starts.

    A fault armed before the run is spent on the first guarded request, which is
    part of signing in. To choose *where* the session dies, it has to be armed
    from inside the run -- and the journal is the only thing the engine tells
    about its own progress, which makes it the honest hook.
    """

    def __init__(self, base_url: str, tenant: str, fault: str, at_step: str) -> None:
        super().__init__()
        self._base_url = base_url
        self._tenant = tenant
        self._fault = fault
        self._at_step = at_step
        self.armed = False

    def emit(self, kind: str, **data) -> None:
        if not self.armed and kind == "step.started" and data.get("step") == self._at_step:
            self.armed = True
            httpx.post(f"{self._base_url}/t/{self._tenant}/__control",
                       params={"fault": self._fault, "count": 1}, timeout=10)
        super().emit(kind, **data)


@pytest.fixture
def profile(live_server):
    resolved = ProfileRepository("profiles").resolve("demo-cu")
    surface = resolved.profile.surface.model_copy(
        update={"base_url": f"{live_server}/t/demo-cu"})
    return resolved.__class__(
        profile=resolved.profile.model_copy(update={"surface": surface}),
        lineage=resolved.lineage, hash=resolved.hash,
    )


def reauth_policy(profile, **changes):
    """A copy of the profile with a different `auth.reauth` policy."""
    auth = profile.profile.auth
    reauth = auth.reauth.model_copy(update=changes)
    return profile.__class__(
        profile=profile.profile.model_copy(
            update={"auth": auth.model_copy(update={"reauth": reauth})}),
        lineage=profile.lineage, hash=profile.hash,
    )


@pytest.fixture
async def signed_in(page, live_server):
    await page.goto(f"{live_server}/t/demo-cu/login", wait_until="networkidle")
    await page.fill("input[type=text]", "operator")
    await page.fill("input[type=password]", "demo-pass-not-real")
    await page.click("input[type=submit]")
    await page.wait_for_timeout(300)
    return page


def run_with(page, artifact, profile, journal, tenant="demo-cu",
             confirm_irreversible=True):
    """An engine for the flow under test.

    `confirm_irreversible` defaults to True here because these tests are about
    resume behaviour, not about M6's authorization gate -- without it every
    write-flow test below would stop at the gate and prove nothing about the
    thing it is named after. The gate itself is exercised in `test_policy.py`,
    where refusing is the point rather than the obstacle.
    """
    return ReplayEngine(WebSurface(page), artifact, profile, journal=journal,
                        credentials=CREDS, tenant=tenant,
                        confirm_irreversible=confirm_irreversible)


# --------------------------------------------------------------------------
# one test per arming point -- the sweep the original single test was missing
# --------------------------------------------------------------------------

@pytest.mark.parametrize("at_step", ["s1", "s2", "s3", "s4", "s5"])
async def test_the_session_can_drop_at_any_step_and_the_run_still_completes(
    signed_in, profile, live_server, at_step
):
    """The regression sweep.

    Before `resume_from` was honoured, s1 and s2 passed and s3/s4/s5 failed with
    LOCATOR_UNRESOLVED -- an alarm naming the wrong subsystem entirely. Every
    arming point is covered now precisely because covering only the easy one is
    what hid this for a whole milestone.
    """
    journal = ArmAtStep(live_server, "demo-cu", "session_timeout", at_step)
    result = await run_with(signed_in, lookup_balance_artifact(), profile, journal).run(
        {"member_id": "12345"})

    assert journal.armed, f"the fault was never armed at {at_step}"
    assert isinstance(result, Success), getattr(result, "describe", lambda: result)()
    assert result.outputs == {"savings_balance": "4210.75"}
    assert journal.of("session.reauth_ok"), "the session was never re-established"
    assert journal.of("session.resuming"), "the flow was never rewound"


async def test_the_resume_point_is_a_checkpoint_that_is_still_true(
    signed_in, profile, live_server
):
    """The distinction the first fix got wrong.

    s4's checkpoint passed, so the last *recorded* checkpoint is s4. But signing
    in again lands on /home, where s4's checkpoint is false. Resuming at the
    step after s4 would run s5 against the dashboard. The engine must re-check
    the recorded checkpoints against the screen in front of it and rewind to one
    that actually holds.
    """
    journal = ArmAtStep(live_server, "demo-cu", "session_timeout", "s5")
    result = await run_with(signed_in, lookup_balance_artifact(), profile, journal).run(
        {"member_id": "12345"})

    assert isinstance(result, Success)
    event = journal.of("session.resuming")[0]
    assert event.data["interrupted_at"] == "s5"
    assert event.data["last_verified"] == "s4", "s4's checkpoint did pass earlier"
    assert event.data["resume_at"] == "s2", (
        "s4 and s2 are both false on the post-login screen; s1's checkpoint is the "
        "deepest one that still holds, so the flow re-enters at s2"
    )


async def test_a_step_with_no_checkpoint_is_never_a_resume_point(
    signed_in, profile, live_server
):
    """s3 fills a field and asserts nothing. Nothing verified that screen, so it
    cannot be a point to resume onto -- resuming onto an unverified state is
    exactly the failure this mechanism exists to avoid."""
    journal = ArmAtStep(live_server, "demo-cu", "session_timeout", "s4")
    await run_with(signed_in, lookup_balance_artifact(), profile, journal).run(
        {"member_id": "12345"})

    assert [e.data["last_verified"] for e in journal.of("session.resuming")] == ["s2"], \
        "s3 has no checkpoint and must never be recorded as verified"


# --------------------------------------------------------------------------
# resume_from is a policy, and the other two values mean what they say
# --------------------------------------------------------------------------

async def test_resume_from_start_replays_the_whole_flow(signed_in, profile, live_server):
    journal = ArmAtStep(live_server, "demo-cu", "session_timeout", "s5")
    result = await run_with(signed_in, lookup_balance_artifact(),
                            reauth_policy(profile, resume_from="start"), journal).run(
        {"member_id": "12345"})

    assert isinstance(result, Success)
    assert journal.of("session.resuming")[0].data["resume_at"] == "s1"


async def test_resume_from_fail_refuses_to_resume(signed_in, profile, live_server):
    """Some applications must not have a half-finished flow picked back up. The
    profile says so, and the engine obeys rather than deciding for itself."""
    journal = ArmAtStep(live_server, "demo-cu", "session_timeout", "s4")
    result = await run_with(signed_in, lookup_balance_artifact(),
                            reauth_policy(profile, resume_from="fail"), journal).run(
        {"member_id": "12345"})

    assert isinstance(result, Failure)
    assert result.failure_class is FailureClass.SESSION_EXPIRED
    assert journal.of("session.resume_refused")
    assert not journal.of("session.resuming")


# --------------------------------------------------------------------------
# the irreversible flow -- where a wrong resume point costs something real
# --------------------------------------------------------------------------

async def test_the_irreversible_flow_replays_end_to_end(signed_in, profile):
    """Baseline, so the resume tests below are measured against a known-good run."""
    result = await run_with(signed_in, open_subaccount_artifact(), profile,
                            MemoryJournal()).run(SUBACCOUNT_PARAMS)

    assert isinstance(result, Success), getattr(result, "describe", lambda: result)()
    assert len(data.OPENED) == 1
    assert result.outputs["account_number"] == data.OPENED[0]["account_number"]


async def test_a_session_drop_on_the_irreversible_step_escalates_instead_of_resuming(
    signed_in, profile, live_server
):
    """R-M6-1, named test 1.

    The session dies on s10 -- the click that posts the account opening. In THIS
    fixture the request was rejected before the write, so a resume would in fact
    have been harmless. The engine cannot know that: from outside the
    application, "rejected before writing" and "wrote, then lost the response"
    look identical. So it refuses to guess and hands the question to a person.

    Asserted against the fixture's own ledger as well as the variant, because a
    double-open would still report Success.
    """
    journal = ArmAtStep(live_server, "demo-cu", "session_timeout", "s10")
    result = await run_with(signed_in, open_subaccount_artifact(), profile, journal).run(
        SUBACCOUNT_PARAMS)

    assert journal.armed
    assert isinstance(result, Escalated), getattr(result, "describe", lambda: result)()
    assert result.reason_class == "IRREVERSIBLE_INTERRUPTED"
    assert result.at_step == "s10"
    assert "s10" in result.human_message
    assert data.OPENED == [], "nothing was written, and nothing was re-attempted"

    blocked = journal.of("resume.blocked_irreversible")[0]
    assert blocked.data["irreversible"] == ["s10"]
    assert journal.of("step.started")[-1].data["step"] == "s10", "s10 was never retried"


async def test_a_completed_irreversible_step_is_never_replayed_by_a_later_resume(
    signed_in, profile, live_server
):
    """R-M6-1, named test 2 -- the case that is easy to miss.

    Here the account IS opened: s10 completes and its checkpoint passes. The
    session then dies on a later step. Signing in again lands on the dashboard,
    where s10's checkpoint is false, so the rewind walks back past it and the
    naive resume replays the whole form -- opening a second account for a member
    who now has one.

    The window the guard inspects is therefore [resume_at, interrupted], not
    just the interrupted step.
    """
    artifact = open_subaccount_artifact()
    after = Step(
        id="s11", intent="Return to the member record",
        action=StepAction(type="click", target=RETURN_TO_MEMBER_LINK),
        risk="navigate",
        checkpoint=UrlCondition(url=UrlMatch(matches=r"/members/\d+$"), frame=CONTENT),
    )
    artifact = artifact.model_copy(update={"steps": artifact.steps + (after,)})

    journal = ArmAtStep(live_server, "demo-cu", "session_timeout", "s11")
    result = await run_with(signed_in, artifact, profile, journal).run(SUBACCOUNT_PARAMS)

    assert isinstance(result, Escalated)
    assert result.reason_class == "IRREVERSIBLE_INTERRUPTED"
    assert result.at_step == "s11", "the interruption was at s11..."
    assert journal.of("resume.blocked_irreversible")[0].data["irreversible"] == ["s10"], \
        "...but s10 is what must not be replayed"
    assert len(data.OPENED) == 1, (
        f"the account was opened {len(data.OPENED)} times: {data.OPENED}"
    )


async def test_a_drop_on_a_reversible_step_still_resumes(
    signed_in, profile, live_server
):
    """R-M6-1, named test 3 -- the guard must not be over-broad.

    s9 reaches the review screen and is `submit_reversible`; nothing in the
    replay window has posted anything. If this escalated too, every dropped
    session anywhere in a write flow would page a human, and the mechanism would
    be worse than useless.
    """
    journal = ArmAtStep(live_server, "demo-cu", "session_timeout", "s9")
    result = await run_with(signed_in, open_subaccount_artifact(), profile, journal).run(
        SUBACCOUNT_PARAMS)

    assert isinstance(result, Success), getattr(result, "describe", lambda: result)()
    assert len(data.OPENED) == 1
    assert journal.of("session.resuming")[0].data["interrupted_at"] == "s9"


async def test_if_a_resume_ever_re_submits_a_commit_the_declared_outcome_catches_it(
    signed_in, profile, live_server
):
    """Defence in depth, and an honest limit.

    Resume is safe here because this application's session guard rejects the
    request before writing. An application that wrote first and expired second
    would be re-posted by any resume strategy -- no ordering of replay can
    distinguish "never happened" from "happened, response lost". What protects
    the member at that point is the application's own duplicate check, declared
    in the artifact as an outcome: the second attempt returns
    SUBACCOUNT_ALREADY_EXISTS instead of opening a second account.
    """
    httpx.post(f"{live_server}/t/demo-cu/__control",
               params={"fault": "duplicate", "count": 1}, timeout=10)

    result = await run_with(signed_in, open_subaccount_artifact(), profile,
                            MemoryJournal()).run(SUBACCOUNT_PARAMS)

    assert isinstance(result, BusinessOutcome)
    assert result.code == "SUBACCOUNT_ALREADY_EXISTS"
    assert result.at_step == "s10"
    assert result.ok is True, "the institution already holds it -- an answer, not a fault"
    assert data.OPENED == [], "nothing may be written when the app reports a duplicate"


# --------------------------------------------------------------------------
# R-M6-1's upgrade: Step.completion_witness
#
# Default-deny (above) is the floor: when a resume would replay an irreversible
# step, escalate. A witness turns the unanswerable question into an observable
# one. In this application the observable consequence of opening a sub-account
# is that a row for it appears on the member's record -- and the opening balance
# is what makes that row identifiable, since the app labels every new
# sub-account "Savings" and the member already has one.
# --------------------------------------------------------------------------

#: "A sub-account with this opening balance now appears on the member's record."
#: An ordinary Condition in the same vocabulary as every checkpoint, which is
#: the point: a witness costs no new concepts and is reviewable in the same read.
OPENED_50 = AllCondition(all=(
    UrlCondition(url=UrlMatch(matches=r"/members/\d+$"), frame=CONTENT),
    ElementCondition(element=ElementMatch(role="cell", name_matches=r"\$50\.00"),
                     frame=CONTENT),
))


def with_witness(artifact, step_id="s10", witness=OPENED_50):
    steps = tuple(s.model_copy(update={"completion_witness": witness}) if s.id == step_id else s
                  for s in artifact.steps)
    return artifact.model_copy(update={"steps": steps})


async def test_a_witness_that_says_the_write_did_not_land_lets_the_run_continue(
    signed_in, profile, live_server
):
    """R-M6-1, the valuable half -- and the half a reader expects to be the
    other one.

    The session dies on s10 itself. Without a witness this escalates, correctly,
    because "rejected before writing" and "wrote, then lost the response" are
    indistinguishable from outside. With one, they are distinguishable: the
    member's record shows no $50.00 account, so the write did not land, so
    replaying it is not a duplicate. Nobody is paged and the run completes.

    Asserted against the fixture's ledger, because the failure this could
    introduce -- a witness that reads false when the write DID land -- would
    show up as two accounts and a Success.
    """
    journal = ArmAtStep(live_server, "demo-cu", "session_timeout", "s10")
    artifact = with_witness(open_subaccount_artifact())
    result = await run_with(signed_in, artifact, profile, journal).run(SUBACCOUNT_PARAMS)

    assert journal.armed
    assert isinstance(result, Success), getattr(result, "describe", lambda: result)()
    assert len(data.OPENED) == 1, f"opened {len(data.OPENED)} times: {data.OPENED}"

    absent = journal.of("resume.witness_absent")
    assert absent and absent[0].data["step"] == "s10"
    assert journal.of("resume.blocked_irreversible") == [], "nobody should have been paged"


async def test_a_witness_that_says_the_write_landed_skips_the_step(
    signed_in, profile, live_server
):
    """R-M6-1, the other half, and the honest limit that comes with it.

    s10 completes; the session dies on s11. Re-authentication lands back on the
    member record, where the $50.00 row is now visible -- so the engine can see
    that the write landed and skips s10 rather than paging a human about it.

    The run does NOT go on to succeed, and that is not a bug being tolerated: the
    account number this capability returns is only ever on the screen the write
    produced, and skipping the write means never seeing that screen. A witness
    makes SKIPPING safe; it does not make the rest of the flow possible. What it
    buys here is precise -- exactly one account, no second write, and no page-out
    for a question the screen already answered.
    """
    artifact = with_witness(open_subaccount_artifact())
    after = Step(
        id="s11", intent="Return to the member record",
        action=StepAction(type="click", target=RETURN_TO_MEMBER_LINK),
        risk="navigate",
        checkpoint=UrlCondition(url=UrlMatch(matches=r"/members/\d+$"), frame=CONTENT),
    )
    artifact = artifact.model_copy(update={"steps": artifact.steps + (after,)})

    journal = ArmAtStep(live_server, "demo-cu", "session_timeout", "s11")
    result = await run_with(signed_in, artifact, profile, journal).run(SUBACCOUNT_PARAMS)

    assert len(data.OPENED) == 1, f"opened {len(data.OPENED)} times: {data.OPENED}"
    satisfied = journal.of("resume.witness_satisfied")
    assert satisfied and satisfied[0].data["step"] == "s10"
    assert satisfied[0].data["seen_at"] == "s6", (
        "the witness was answered on the member record the flow replays through, "
        "not on the dashboard the resume landed on -- which is the whole reason "
        "witnesses are polled forward rather than evaluated once at the rewind"
    )
    assert journal.of("resume.blocked_irreversible") == [], \
        "the screen answered the question, so nobody should have been paged"

    skipped = [e for e in journal.of("step.skipped") if e.data["step"] == "s10"]
    assert skipped and "witness" in skipped[0].data["reason"]
    assert [e.data["step"] for e in journal.of("step.started")].count("s10") == 1, \
        "s10 ran once, on the first pass, and was never re-entered"

    # The declared limit, asserted rather than described: s11 wanted the link on
    # the screen the write produced, and skipping the write means never seeing
    # that screen.
    assert isinstance(result, Failure)
    assert result.at_step == "s11"


async def test_a_step_with_no_witness_still_defaults_to_deny(
    signed_in, profile, live_server
):
    """The upgrade must not weaken the floor. Where no witness exists -- a write
    whose only trace is a nightly batch -- escalation stays the answer."""
    journal = ArmAtStep(live_server, "demo-cu", "session_timeout", "s10")
    result = await run_with(signed_in, open_subaccount_artifact(), profile,
                            journal).run(SUBACCOUNT_PARAMS)

    assert isinstance(result, Escalated)
    assert result.reason_class == "IRREVERSIBLE_INTERRUPTED"
    assert journal.of("resume.witness_satisfied") == []
    assert journal.of("resume.witness_absent") == []


def test_a_witness_on_a_reversible_step_is_rejected_at_authoring_time():
    """A witness answers "did this write land", which is only a question for an
    irreversible step. One declared anywhere else reads as protection that will
    never run, and unread protection is worse than none."""
    artifact = open_subaccount_artifact()
    # Validated, not model_copy'd: `model_copy` skips validators by design, and
    # the check that matters is the one an artifact loaded from disk goes
    # through.
    with pytest.raises(ValueError) as exc:
        CapabilityArtifact.model_validate(
            with_witness(artifact, step_id="s9").model_dump(mode="json"))
    assert "completion_witness" in str(exc.value)
    assert "submit_irreversible" in str(exc.value)

