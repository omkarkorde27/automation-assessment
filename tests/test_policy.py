"""The allowlist, and the replay-side gate on irreversible flows.

Part 11 names this suite. Two properties, and both are about the choke point
holding for a caller who is wrong rather than for one who is careful:

  * an action outside the allowlist cannot be performed by any caller, because
    the refusal happens inside `act()` and there is no other way to act;
  * a capability that commits something runs only when it has been reviewed AND
    the caller asked for it on purpose.

The allowlist tests run against a real browser and a real server, because the
interesting cases are about URLs the application produced rather than URLs a
test made up.
"""

from __future__ import annotations

import pytest

from cua.artifact.schema import ApprovalState
from cua.observability.journal import MemoryJournal
from cua.policy.allowlist import (
    AllowlistGuard, AllowlistRules, NotAllowed, PolicyFile, load_policy,
)
from cua.profiles import ProfileRepository
from cua.replay import Escalated, Failure, FailureClass, ReplayEngine, Success
from cua.replay.engine import CredentialResolver
from cua.surfaces.base import Action, ActionType
from cua.surfaces.web_playwright import WebSurface
from factories import lookup_balance_artifact, open_subaccount_artifact

CREDS = CredentialResolver({"MOCKBANK_USER": "operator", "MOCKBANK_PASS": "demo-pass-not-real"})
PARAMS = {"member_id": "12345"}
SUBACCOUNT_PARAMS = {"member_id": "12345", "product_code": "HSA", "initial_deposit": "50.00"}


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


# ==========================================================================
# the committed file
# ==========================================================================

def test_the_committed_policy_file_parses_and_narrows_per_capability():
    """`config/policy.yaml` is a deliverable, not a fixture. If it stops
    parsing, every real run loses its allowlist -- silently, because nothing
    else reads it."""
    policy = load_policy("config/policy.yaml")
    read_only = policy.for_capability("member.lookup_balance@1.0.0")
    write = policy.for_capability("member.open_subaccount@1.0.0")

    assert "/subaccount(/|$)" in read_only.denied_paths, \
        "a read-only capability has no business inside the write flow"
    assert "/subaccount(/|$)" not in write.denied_paths
    assert read_only.max_navigations < policy.default.max_navigations
    assert "select_option" not in read_only.permitted_actions
    assert "/__control" in read_only.denied_paths, "base denials survive the overlay"


def test_a_capability_block_can_only_narrow_never_widen():
    """The property that makes the file worth reading top-down.

    An overlay that could widen means the default is advisory: the base looks
    strict and some capability three screens down has quietly opened it up.
    """
    base = AllowlistRules(
        allowed_paths=("^/t/",), denied_paths=("/admin",),
        permitted_actions=("click", "fill"), max_navigations=10,
    )
    greedy = AllowlistRules(
        allowed_paths=("^/anything",), denied_paths=(),
        permitted_actions=("click", "fill", "navigate"), max_navigations=999,
    )
    effective = base.narrow(greedy)

    assert "navigate" not in effective.permitted_actions, "cannot add an action type"
    assert effective.max_navigations == 10, "cannot raise the budget"
    assert "/admin" in effective.denied_paths, "cannot drop a denial"
    assert set(effective.allowed_paths) == {"^/t/", "^/anything"}, (
        "path patterns accumulate, and BOTH lists must be satisfied -- which is "
        "checked in rejects(), not here"
    )


def test_a_missing_policy_file_is_an_error_not_an_open_door(tmp_path):
    with pytest.raises(FileNotFoundError) as exc:
        load_policy(tmp_path / "nope.yaml")
    assert "not optional" in str(exc.value)


def test_a_policy_written_for_a_schema_this_build_does_not_know_is_refused(tmp_path):
    """Guessing at an unknown shape produces a guardrail that permits whatever
    it could not parse, and says nothing about it."""
    path = tmp_path / "policy.yaml"
    path.write_text("version: 99\ndefault: {max_navigations: 5}\n")
    with pytest.raises(ValueError) as exc:
        load_policy(path)
    assert "version 99" in str(exc.value)


# ==========================================================================
# enforcement inside act()
# ==========================================================================

async def test_a_navigation_off_the_allowed_origin_is_refused_inside_act(
        signed_in, live_server):
    """The headline. Not "the engine declines to navigate" -- `act()` refuses,
    so no caller can perform it, including one that never heard of the policy."""
    surface = WebSurface(signed_in, journal=MemoryJournal())
    surface.add_guard(AllowlistGuard(
        AllowlistRules(allowed_paths=("^/t/",)), base_url=f"{live_server}/t/demo-cu"))

    result = await surface.act(Action(
        type=ActionType.NAVIGATE, url="https://example.com/pay", reason="exfiltrate"))

    assert result.ok is False
    assert result.error_detail["failure_class"] == "POLICY_DENIED"
    assert result.error_detail["denied_by"] == "allowlist"
    assert "not on the allowlist" in result.error
    assert "example.com" in result.error


async def test_a_refusal_names_the_capability_whose_rules_decided(signed_in, live_server):
    """"Refused" without "under which narrowing" sends the reader to the default
    block, which is usually not the one that decided."""
    surface = WebSurface(signed_in, journal=MemoryJournal())
    surface.add_guard(AllowlistGuard(
        load_policy("config/policy.yaml").for_capability("member.lookup_balance@1.0.0"),
        base_url=f"{live_server}/t/demo-cu", capability_ref="member.lookup_balance@1.0.0"))

    result = await surface.act(Action(
        type=ActionType.NAVIGATE,
        url=f"{live_server}/t/demo-cu/members/12345/subaccount/new",
        reason="a read-only capability wandering into the write flow"))

    assert result.ok is False
    assert "member.lookup_balance@1.0.0" in result.error
    assert "/subaccount" in result.error


async def test_a_denied_path_is_refused_even_on_the_right_origin(signed_in, live_server):
    """Origin is not enough. `/__control` reconfigures the application the flow
    is driving, and it lives on the same host as everything else."""
    surface = WebSurface(signed_in, journal=MemoryJournal())
    surface.add_guard(AllowlistGuard(
        load_policy("config/policy.yaml").default, base_url=f"{live_server}/t/demo-cu"))

    result = await surface.act(Action(
        type=ActionType.NAVIGATE, url=f"{live_server}/t/demo-cu/__control?fault=duplicate",
        reason="arm a fault"))

    assert result.ok is False
    assert "denied pattern" in result.error


def test_a_host_that_merely_contains_the_allowed_name_is_not_the_allowed_host():
    """Substring matching is the classic way an origin check fails open."""
    guard = AllowlistGuard(AllowlistRules(), base_url="https://bank.example.com/t/x")
    assert guard.rejects("https://bank.example.com/t/x/members") is None
    for hostile in ("https://evil.com/#bank.example.com",
                    "https://bank.example.com.evil.com/t/x",
                    "http://bank.example.com/t/x"):
        assert guard.rejects(hostile), f"{hostile} must not pass"


def test_an_action_type_the_policy_does_not_permit_is_refused():
    guard = AllowlistGuard(AllowlistRules(permitted_actions=("click", "navigate")),
                           base_url="http://host", capability_ref="member.lookup_balance@1.0.0")
    with pytest.raises(NotAllowed) as exc:
        guard.check(Action(type=ActionType.FILL, value="x", reason="type something"))
    assert exc.value.denied_by == "allowlist"
    assert "not permitted" in exc.value.message
    assert guard.denied == ["fill"], "and the guard keeps its own tally, as the risk guard does"


def test_the_navigation_budget_stops_a_flow_that_is_wandering():
    guard = AllowlistGuard(AllowlistRules(max_navigations=2), base_url="http://host")
    for _ in range(2):
        guard.check(Action(type=ActionType.NAVIGATE, url="http://host/t/a", reason="go"))
    with pytest.raises(NotAllowed) as exc:
        guard.check(Action(type=ActionType.NAVIGATE, url="http://host/t/a", reason="go"))
    assert "budget" in exc.value.message


def test_a_refused_navigation_does_not_spend_budget():
    """Otherwise a run could be starved by its own denials, and the failure
    would report the budget rather than the thing that was actually wrong."""
    guard = AllowlistGuard(AllowlistRules(max_navigations=1), base_url="http://host")
    with pytest.raises(NotAllowed):
        guard.check(Action(type=ActionType.NAVIGATE, url="http://elsewhere/x", reason="go"))
    guard.check(Action(type=ActionType.NAVIGATE, url="http://host/t/a", reason="go"))
    assert guard.navigations == 1


async def test_the_engine_catches_a_link_click_that_leaves_the_allowed_space(
        signed_in, profile, live_server):
    """The honest half of the allowlist.

    A pre-action guard sees navigations automation ASKS for. A click is a click,
    and the app decides where it lands -- so the URL is re-checked after every
    step. Here the policy forbids the member-detail path, which the flow reaches
    by clicking a search result rather than by navigating.
    """
    journal = MemoryJournal()
    rules = AllowlistRules(allowed_paths=("^/t/",), denied_paths=(r"/members/\d+$",))
    engine = ReplayEngine(WebSurface(signed_in, journal=journal), lookup_balance_artifact(),
                          profile, journal=journal, credentials=CREDS, tenant="demo-cu",
                          policy=rules)
    result = await engine.run(PARAMS)

    assert isinstance(result, Failure), getattr(result, "describe", lambda: result)()
    assert result.failure_class is FailureClass.POLICY_DENIED
    assert result.detail["denied_by"] == "allowlist"
    assert result.detail["when"] == "after the action"
    assert journal.of("policy.offsite"), "the refusal is on the record"


async def test_a_flow_inside_its_own_allowlist_is_untouched(signed_in, profile):
    """The guard must not be a tax on the ordinary case."""
    policy = load_policy("config/policy.yaml").for_capability("member.lookup_balance@1.0.0")
    engine = ReplayEngine(WebSurface(signed_in), lookup_balance_artifact(), profile,
                          credentials=CREDS, tenant="demo-cu", policy=policy)
    result = await engine.run(PARAMS)

    assert isinstance(result, Success), getattr(result, "describe", lambda: result)()
    assert result.outputs["savings_balance"] == "4210.75"


# ==========================================================================
# risk gating: irreversible flows during replay (Part 3.6)
# ==========================================================================

async def test_an_irreversible_capability_will_not_run_without_explicit_confirmation(
        signed_in, profile):
    """Approved, but nobody asked for it. One boolean is the difference between
    an agent that decided to post a transaction and one that was told to."""
    journal = MemoryJournal()
    engine = ReplayEngine(WebSurface(signed_in), open_subaccount_artifact(), profile,
                          journal=journal, credentials=CREDS, tenant="demo-cu",
                          confirm_irreversible=False)
    result = await engine.run(SUBACCOUNT_PARAMS)

    assert isinstance(result, Escalated), getattr(result, "describe", lambda: result)()
    assert result.reason_class == "IRREVERSIBLE_NOT_AUTHORIZED"
    assert "confirm_irreversible" in result.human_message
    assert journal.of("policy.irreversible_refused")


async def test_an_unapproved_capability_will_not_run_even_when_the_caller_insists(
        signed_in, profile):
    """The other half. An emphatic caller must not be able to run a flow that
    nobody reviewed -- otherwise `approval_state` is decoration."""
    draft = open_subaccount_artifact()
    draft = draft.model_copy(update={"capability": draft.capability.model_copy(
        update={"approval_state": ApprovalState.DRAFT})})

    engine = ReplayEngine(WebSurface(signed_in), draft, profile,
                          credentials=CREDS, tenant="demo-cu", confirm_irreversible=True)
    result = await engine.run(SUBACCOUNT_PARAMS)

    assert isinstance(result, Escalated)
    assert result.reason_class == "IRREVERSIBLE_NOT_AUTHORIZED"
    assert "'draft', not 'approved'" in result.human_message


async def test_the_gate_refuses_before_the_browser_is_touched(signed_in, profile):
    """A run that is not permitted should not half-perform a flow first.

    Asserted through the journal: nothing between `run.started` and the refusal.
    """
    journal = MemoryJournal()
    engine = ReplayEngine(WebSurface(signed_in), open_subaccount_artifact(), profile,
                          journal=journal, credentials=CREDS, tenant="demo-cu")
    await engine.run(SUBACCOUNT_PARAMS)

    assert journal.of("step.started") == []
    assert "session.signed_in" not in journal.kinds()


async def test_a_read_only_capability_never_meets_the_gate(signed_in, profile):
    journal = MemoryJournal()
    engine = ReplayEngine(WebSurface(signed_in), lookup_balance_artifact(), profile,
                          journal=journal, credentials=CREDS, tenant="demo-cu")
    result = await engine.run(PARAMS)

    assert isinstance(result, Success)
    assert journal.of("policy.irreversible_refused") == []
    assert journal.of("policy.irreversible_authorized") == []


async def test_an_authorized_run_says_so_on_the_record(signed_in, profile):
    """Permission granted is as much a thing to journal as permission refused:
    "who authorized this write" is the first question after one goes wrong."""
    journal = MemoryJournal()
    engine = ReplayEngine(WebSurface(signed_in), open_subaccount_artifact(), profile,
                          journal=journal, credentials=CREDS, tenant="demo-cu",
                          confirm_irreversible=True)
    result = await engine.run(SUBACCOUNT_PARAMS)

    assert isinstance(result, Success), getattr(result, "describe", lambda: result)()
    granted = journal.of("policy.irreversible_authorized")[0]
    assert granted.data["approval"] == "approved"
    assert granted.data["steps"] == ["s10"]
