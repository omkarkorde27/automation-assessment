"""R-M6-2: the risk tier is re-derived inside `act()`, never accepted.

The gap this closes was real and quiet. `IrreversibleActionGuard` ran inside the
choke point, which is correct, but it ran BEFORE the locator resolved -- so the
only tier available to it was the one the caller had written on the `Action`. A
guardrail that reads the caller's own assessment of how dangerous its action is
has checked nothing; it is a convention with a stack frame. It happened to hold
because the one caller was careful, which is exactly the property a choke point
is supposed to make unnecessary.

Pulled forward from M6 because M5 is the milestone that adds the second caller.
"""

from __future__ import annotations

import pytest

from cua.locators.model import FrameRef, LocatorBundle, RoleNameExact
from cua.observability.journal import MemoryJournal
from cua.policy.risk import IrreversibleActionGuard, classify
from cua.session.control import AUTOMATION, ControlLease
from cua.surfaces.base import Action, ActionType, RiskTier
from cua.surfaces.web_playwright import WebSurface

CONTENT = (FrameRef(name="content"),)


def target(name: str) -> LocatorBundle:
    return LocatorBundle(target_id=f"t:{name}", frame_path=CONTENT,
                         candidates=(RoleNameExact(role="button", name=name),),
                         notes="test bundle")


@pytest.fixture
async def confirm_screen(page, live_server):
    """The real sub-account confirmation screen, reached the ordinary way.

    Driven with raw Playwright rather than through the system: getting *to* the
    screen is fixture setup, and using `act()` here would mean the tests below
    depended on the guard they exist to test.

    "Confirm and Open Account" is a genuinely irreversible control in the
    fixture app, named by the app, not by us -- which is the whole point: the
    tier comes from what the button says it does.
    """
    await page.goto(f"{live_server}/t/demo-cu/login", wait_until="networkidle")
    await page.fill("input[type=text]", "operator")
    await page.fill("input[type=password]", "demo-pass-not-real")
    await page.click("input[type=submit]")
    await page.wait_for_timeout(300)
    await page.goto(f"{live_server}/t/demo-cu/", wait_until="networkidle")
    frame = next(f for f in page.frames if f.name == "content")
    await frame.goto(f"{live_server}/t/demo-cu/members/12345/subaccount/new",
                     wait_until="networkidle")
    await frame.select_option("select", "SAV")
    await frame.fill("input[name=initial_deposit]", "100.00")
    await frame.click("button, input[type=submit]")
    await page.wait_for_timeout(500)
    return page


def surface_with_guard(page, *, allow=False, lease=None):
    log = MemoryJournal()
    guard = IrreversibleActionGuard(allow_irreversible=allow, lease=lease)
    surface = WebSurface(page, journal=log)
    surface.add_guard(guard)
    if lease is not None:
        surface.add_guard(lease)
    return surface, guard, log


async def _irreversible_button(page) -> str:
    """Whatever the confirmation screen's committing control is actually called.

    Read off the live app rather than hard-coded, so this suite keeps testing
    the classifier against the fixture's real wording instead of against a
    string the test and the app agreed on separately.
    """
    observation = await WebSurface(page).observe()
    names = [n.name for n in observation.nodes
             if n.role == "button" and n.visible
             and classify(ActionType.CLICK, n) is RiskTier.SUBMIT_IRREVERSIBLE]
    assert names, "the confirmation screen should carry a committing control"
    return names[0]


# ---- the named tests -----------------------------------------------------

async def test_a_caller_cannot_mislabel_an_irreversible_action_to_get_past_the_guard(
        confirm_screen, page):
    """The `Action` says `input`. The button says "Open Account". The button wins."""
    name = await _irreversible_button(page)
    surface, guard, log = surface_with_guard(page)

    result = await surface.act(Action(
        ActionType.CLICK, target=target(name), risk=RiskTier.INPUT,
        reason="pretending this is harmless"))

    assert result.ok is False
    assert result.error_detail["failure_class"] == "POLICY_DENIED"
    assert result.derived_risk is RiskTier.SUBMIT_IRREVERSIBLE
    assert guard.denied == [f"t:{name}"]
    assert name in (result.error or "")


async def test_the_tier_recorded_in_the_journal_is_the_one_derived_not_the_one_claimed(
        confirm_screen, page):
    name = await _irreversible_button(page)
    surface, _, log = surface_with_guard(page, allow=True)

    await surface.act(Action(ActionType.CLICK, target=target(name),
                             risk=RiskTier.READ_ONLY, reason="claiming it is a look"))

    checked = log.first("policy.checked")
    assert checked is not None
    assert checked.data["claimed_risk"] == "read_only"
    assert checked.data["derived_risk"] == "submit_irreversible"
    assert checked.data["mismatch"] is True
    assert name in checked.data["node"]


# ---- the rest of the property --------------------------------------------

async def test_an_honest_caller_is_unaffected(confirm_screen, page):
    name = await _irreversible_button(page)
    surface, guard, log = surface_with_guard(page)

    result = await surface.act(Action(
        ActionType.CLICK, target=target(name), risk=RiskTier.SUBMIT_IRREVERSIBLE,
        reason="labelled correctly, and still refused"))

    assert result.ok is False
    assert log.first("policy.checked").data["mismatch"] is False


async def test_a_caller_cannot_mislabel_a_harmless_action_INTO_being_refused(
        confirm_screen, page):
    """The enforcement is symmetric: a wrong label neither raises nor lowers
    the tier, because the label is not consulted at all."""
    surface, guard, log = surface_with_guard(page)
    back = LocatorBundle(target_id="t:back", frame_path=CONTENT,
                         candidates=(RoleNameExact(role="link", name="Back"),),
                         notes="the harmless control on this screen")

    result = await surface.act(Action(
        ActionType.CLICK, target=back, risk=RiskTier.SUBMIT_IRREVERSIBLE,
        reason="over-claiming"))

    assert result.error_detail.get("failure_class") != "POLICY_DENIED", \
        "a plain link was refused on the caller's say-so"
    assert result.derived_risk is not RiskTier.SUBMIT_IRREVERSIBLE
    assert guard.denied == []


async def test_a_targetless_action_still_meets_the_enforcing_pass(confirm_screen, page):
    """No node does not mean no check. "Which actions are exempt" is not a
    question a choke point should have an answer to."""
    surface, _, log = surface_with_guard(page)
    await surface.act(Action(ActionType.SCROLL, value="200", reason="look further down"))

    checked = log.first("policy.checked")
    assert checked is not None and checked.data["derived_risk"] == "read_only"


async def test_a_person_holding_the_lease_may_do_what_the_agent_may_not(
        confirm_screen, page):
    """Takeover exists so an operator can do the thing automation would not.

    They do not leave the allowlist -- that guard is not lease-aware and never
    should be -- and the override lands in the journal with their name on it.
    """
    name = await _irreversible_button(page)
    lease = ControlLease("sess_r", journal=MemoryJournal())
    surface, guard, log = surface_with_guard(page, lease=lease)

    with lease.acting_as(AUTOMATION):
        refused = await surface.act(Action(ActionType.CLICK, target=target(name),
                                           reason="the agent tries it"))
    assert refused.ok is False and guard.denied

    lease.pause(reason="stuck")
    lease.take("alice")
    with lease.acting_as("alice"):
        allowed = await surface.act(Action(ActionType.CLICK, target=target(name),
                                           reason="alice decides to commit it"))
    assert allowed.ok, allowed.error
    assert guard.human_overrides == [f"t:{name}"]

    override = log.first("policy.human_override")
    assert override is not None and override.data["holder"] == "alice"
    assert override.data["derived_risk"] == "submit_irreversible"
