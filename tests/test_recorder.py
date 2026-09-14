"""The recorder: observed behaviour in, replayable contract out.

Two things are being tested, and the second is the one with teeth.

The first is derivation -- that a node the model merely *pointed at* becomes a
locator bundle with a real candidate ladder, record-time stability scoring, and
stated reasoning. That is what makes locator robustness a property of the system
rather than of the model's selector-writing.

The second is R-M4-1. The artifact schema permits `{literal: "..."}` because
genuine constants exist, and nothing in the schema can tell a constant from this
run's member id or from a customer's name read off one screen and typed into
another. The guarantee has to live at record time, so it is tested at record
time -- including the case where the recorder is required to refuse outright.
"""

from __future__ import annotations

import re

import pytest

from cua.artifact.recorder import RecorderRefusal, derive_bundle, record, synthesize_checkpoint
from cua.artifact.schema import ProductRef
from cua.discovery.transcript import (
    DeclaredExtraction, DeclaredOutcome, DiscoveryRun, DiscoveryStep, value_shape,
)
from cua.perception.model import Anchors, BBox, Observation, UiNode
from cua.surfaces.base import ActionType, RiskTier

PRODUCT = ProductRef(vendor="meridian", product="core", version_range=">=4.2 <5")


def node(role, name="", *, node_id="n1", value=None, row_label="", label="",
         section="", frame=("content",)) -> UiNode:
    return UiNode(
        node_id=node_id, role=role, name=name, value=value, frame_path=frame,
        bbox=BBox(x=10, y=20, w=100, h=20, nx=0.1, ny=0.2, nw=0.3, nh=0.02),
        anchors=Anchors(row_label=row_label, label=label, section_label=section),
        states={"visible": True, "enabled": True},
    )


def obs(*nodes, url="http://x/t/demo-cu/members") -> Observation:
    return Observation(url=url, title="t", nodes=tuple(nodes),
                       frame_paths=(("content",),), frame_urls={"content": url})


def run_with(steps, *, extractions=(), outcomes=(), status="finished") -> DiscoveryRun:
    r = DiscoveryRun(run_id="disc_test", goal="Look up member 12345 and read the balance",
                     tenant="demo-cu", base_url="http://x/t/demo-cu", model="claude-opus-5")
    r.steps = list(steps)
    r.extractions = list(extractions)
    r.outcomes = list(outcomes)
    r.status = status
    r.finish_reason = "the member detail screen is shown"
    return r


def step(index, tool, action_type, *, n=None, value=None, param="", is_param=False,
         reason="because", pre=None, post=None, risk=RiskTier.NAVIGATE) -> DiscoveryStep:
    """Build a step, guaranteeing the acted-on node is present in `pre`.

    The model can only ever pick a node it was shown, so a `pre` observation
    that omits the target is a shape no real run produces -- and constructing
    one in a test would be testing behaviour the system never sees.
    """
    if n is not None and pre is not None and pre.by_id(n.node_id) is None:
        pre = pre.model_copy(update={"nodes": pre.nodes + (n,)})
    return DiscoveryStep(index=index, tool=tool, reason=reason, action_type=action_type,
                         node=n, value=value, param_name=param, is_param_candidate=is_param,
                         pre=pre, post=post, risk=risk)


# --------------------------------------------------------------------------
# R-M4-1
# --------------------------------------------------------------------------

def test_recorder_never_bakes_a_parameter_value_into_a_literal():
    """The named test from R-M4-1.

    The model typed the member id and marked it as a parameter. The recorded
    step must reference the parameter, so the capability can be called for a
    different member tomorrow.
    """
    field = node("textbox", row_label="Member ID", node_id="n1")
    before = obs(field)
    steps = [step(1, "fill", ActionType.FILL, n=field, value="12345",
                  param="member_id", is_param=True, pre=before, post=before,
                  risk=RiskTier.INPUT),
             step(2, "click", ActionType.CLICK, n=node("button", "Search", node_id="n2"),
                  pre=before, post=obs(node("heading", "Search Results", node_id="n9")),
                  risk=RiskTier.SUBMIT_REVERSIBLE)]

    artifact = record(run_with(steps), capability_id="member.lookup",
                      product=PRODUCT, app_profile_ref="meridian-core@4.2")

    fill = artifact.steps[0]
    assert fill.action.value_from.param == "member_id"
    assert fill.action.value_from.literal is None
    assert "12345" not in artifact.model_dump_json(), "the value itself must not survive"
    assert artifact.inputs["member_id"].pattern == r"^\d{5}$", "the shape is recorded, not the value"


def test_an_unmarked_value_matching_a_declared_parameter_is_still_a_parameter():
    """The model is not required to be perfect. If it marks a value once and
    types the same value again without marking it, recording the second as a
    literal would hard-code this run's member id into every future run."""
    field = node("textbox", row_label="Member ID", node_id="n1")
    second = node("textbox", row_label="Confirm Member ID", node_id="n2")
    before = obs(field, second)
    after = obs(node("heading", "Done", node_id="n9"))

    steps = [
        step(1, "fill", ActionType.FILL, n=field, value="12345", param="member_id",
             is_param=True, pre=before, post=before, risk=RiskTier.INPUT),
        step(2, "fill", ActionType.FILL, n=second, value="12345",
             pre=before, post=before, risk=RiskTier.INPUT),
        step(3, "click", ActionType.CLICK, n=node("button", "Go", node_id="n3"),
             pre=before, post=after),
    ]
    artifact = record(run_with(steps), capability_id="member.lookup",
                      product=PRODUCT, app_profile_ref="meridian-core@4.2")

    assert artifact.steps[1].action.value_from.param == "member_id"
    assert artifact.steps[1].action.value_from.literal is None


def test_a_value_read_off_the_screen_is_refused_outright():
    """A member's name, read from one screen and typed into another, is recorded
    customer data. There is no acceptable way to bake that into a reusable
    capability, so the recording fails rather than degrades -- an artifact that
    silently contains a real name is worse than no artifact, because it looks
    fine."""
    shown = node("cell", "Priya Raghunathan", node_id="n5")
    field = node("textbox", row_label="Account Holder", node_id="n1")
    before = obs(shown, field)

    steps = [step(1, "fill", ActionType.FILL, n=field, value="Priya Raghunathan",
                  pre=before, post=before, risk=RiskTier.INPUT),
             step(2, "click", ActionType.CLICK, n=node("button", "Save", node_id="n2"),
                  pre=before, post=obs(node("heading", "Saved", node_id="n9")))]

    with pytest.raises(RecorderRefusal) as exc:
        record(run_with(steps), capability_id="member.update",
               product=PRODUCT, app_profile_ref="meridian-core@4.2")

    assert "displayed on screen" in str(exc.value)
    assert "Priya Raghunathan" in str(exc.value)


def test_a_genuine_constant_is_still_allowed_to_be_a_literal():
    """R-M4-1 must not make constants impossible: a product code chosen from a
    fixed dropdown is exactly what `literal` is for."""
    select = node("combobox", "Product Code", node_id="n1")
    before = obs(select)
    steps = [step(1, "select_option", ActionType.SELECT_OPTION, n=select, value="SAV",
                  pre=before, post=before, risk=RiskTier.INPUT),
             step(2, "click", ActionType.CLICK, n=node("button", "Continue", node_id="n2"),
                  pre=before, post=obs(node("heading", "Review", node_id="n9")))]

    artifact = record(run_with(steps), capability_id="member.open",
                      product=PRODUCT, app_profile_ref="meridian-core@4.2")
    assert artifact.steps[0].action.value_from.literal == "SAV"


def test_a_url_containing_a_parameter_becomes_a_template():
    """A recorded URL with this run's member id in it is a capability that can
    only ever look up that member."""
    before = obs(node("link", "Open", node_id="n1"))
    steps = [
        step(1, "fill", ActionType.FILL, n=node("textbox", row_label="Member ID", node_id="n2"),
             value="12345", param="member_id", is_param=True, pre=before, post=before,
             risk=RiskTier.INPUT),
        step(2, "navigate", ActionType.NAVIGATE, pre=before,
             post=obs(node("heading", "Member Detail", node_id="n9"))),
    ]
    steps[1].url = "http://x/t/demo-cu/members/12345"

    artifact = record(run_with(steps), capability_id="member.lookup",
                      product=PRODUCT, app_profile_ref="meridian-core@4.2")
    assert artifact.steps[1].action.url_template == "{base_url}/members/{member_id}"


# --------------------------------------------------------------------------
# locator derivation -- the model pointed; the recorder writes the address
# --------------------------------------------------------------------------

def test_a_named_control_gets_a_name_candidate_first():
    button = node("button", "Search", node_id="n1")
    bundle = derive_bundle(button, obs(button, node("cell", "other", node_id="n2")),
                           target_id="search_button")

    assert bundle.candidates[0].strategy == "role_name_exact"
    assert bundle.notes, "a bundle with no stated reasoning is not reviewable"
    assert bundle.recorded.name == "Search"
    assert bundle.stability_score > 0


def test_an_unnamed_control_is_reached_through_its_row():
    """The legacy-table case, and the reason the anchor strategy exists: this
    control has no accessible name at all, and its id is regenerated per render,
    so the only durable handle is the text a person reads beside it."""
    field = node("textbox", row_label="Member ID", node_id="n1")
    bundle = derive_bundle(field, obs(field, node("cell", "Member ID", node_id="n2")),
                           target_id="member_id_input")

    assert bundle.candidates[0].strategy == "anchor_relative"
    assert bundle.candidates[0].anchor.text == "Member ID"
    assert "id churn" in bundle.notes


def test_a_candidate_that_was_already_ambiguous_is_not_recorded():
    """Recording a candidate that matched three nodes at record time turns a
    clean LOCATOR_UNRESOLVED into an ambiguity nobody can act on."""
    a = node("button", "Go", node_id="n1")
    b = node("button", "Go", node_id="n2")
    c = node("button", "Go", node_id="n3", section="Panel")
    bundle = derive_bundle(c, obs(a, b, c), target_id="go_button")

    assert all(cand.strategy != "role_name_exact" for cand in bundle.candidates)
    assert bundle.candidates[-1].strategy == "role_ordinal"
    assert bundle.candidates[-1].scope is not None, "a bare document-wide ordinal is never recorded"


def test_an_unaddressable_node_is_refused_rather_than_guessed():
    orphan = node("cell", "", node_id="n1")
    with pytest.raises(RecorderRefusal, match="durably"):
        derive_bundle(orphan, obs(orphan), target_id="x")


def test_the_pattern_candidate_spans_every_tenants_spelling():
    """This is what makes one recording run on both institutions. The spellings
    come from the tenant overlays on disk, not from a guess."""
    field = node("textbox", "Member ID", node_id="n1")
    bundle = derive_bundle(
        field, obs(field), target_id="member_id_input",
        label_variants={"member id": ["account holder #", "member id"]})

    patterns = [c for c in bundle.candidates if c.strategy == "role_name_pattern"]
    assert patterns, "no cross-tenant candidate was generated"
    # Asserted by matching, not by inspecting the escaping: what matters is that
    # the recorded pattern resolves on either institution's wording.
    rx = re.compile(patterns[0].name_pattern)
    assert rx.fullmatch("Member ID")
    assert rx.fullmatch("Account Holder #")
    assert not rx.fullmatch("Member ID Number")


def test_no_recorded_bundle_contains_a_selector():
    """Invariant 2, asserted on the artifact rather than on the tool schema."""
    field = node("textbox", row_label="Member ID", node_id="n1")
    before = obs(field)
    steps = [step(1, "fill", ActionType.FILL, n=field, value="12345", param="member_id",
                  is_param=True, pre=before, post=before, risk=RiskTier.INPUT),
             step(2, "click", ActionType.CLICK, n=node("button", "Search", node_id="n2"),
                  pre=before, post=obs(node("heading", "Results", node_id="n9")))]
    raw = record(run_with(steps), capability_id="member.lookup", product=PRODUCT,
                 app_profile_ref="meridian-core@4.2").model_dump_json()

    for forbidden in ("css=", "xpath", "querySelector", "#ctl00", "nth-child"):
        assert forbidden not in raw


# --------------------------------------------------------------------------
# checkpoints, pruning, provenance
# --------------------------------------------------------------------------

def test_a_checkpoint_is_built_from_what_changed_not_from_the_end_state():
    """Anything already on screen before the click proves nothing about it."""
    before = obs(node("button", "Search", node_id="n1"),
                 url="http://x/t/demo-cu/members")
    after = obs(node("button", "Search", node_id="n1"),
                node("heading", "Search Results", node_id="n2"),
                url="http://x/t/demo-cu/members/search")

    condition = synthesize_checkpoint(
        step(1, "click", ActionType.CLICK, n=node("button", "Search"), pre=before, post=after),
        content_frame=("content",), base_url="http://x/t/demo-cu")

    rendered = condition.model_dump_json()
    assert "Search Results" in rendered, "the heading that appeared should be asserted"
    assert "/members/search" in rendered
    assert '"Search"' not in rendered, "the button was already there; it proves nothing"


def test_a_synthesized_checkpoint_never_pins_the_tenant_it_was_recorded_on():
    """Invariant 9, enforced at the point the condition is written.

    A checkpoint built from the observed address keeps whatever prefix that
    address had. The recorded artifacts shipped with `/t/demo-cu` inside eleven
    URL checkpoints, so both capabilities replayed on the institution they were
    learned from and failed on its sibling at the first checkpoint -- while
    `binding` correctly claimed they were bound to the product.

    The action side had always stripped the deployment (`_templatize`); the
    condition side had not.
    """
    before = obs(node("button", "Search", node_id="n1"), url="http://x/t/demo-cu/members")
    after = obs(node("button", "Search", node_id="n1"),
                node("heading", "Search Results", node_id="n2"),
                url="http://x/t/demo-cu/members/search")

    condition = synthesize_checkpoint(
        step(1, "click", ActionType.CLICK, n=node("button", "Search"), pre=before, post=after),
        content_frame=("content",), base_url="http://x/t/demo-cu")

    rendered = condition.model_dump_json()
    assert "demo" not in rendered, f"the recording tenant survived into {rendered}"
    assert "/members/search" in rendered, "and the route itself is still asserted"


def test_a_url_outside_the_recorded_base_degrades_instead_of_guessing():
    """A redirect to another host is not a route under `base_url`. Stripping
    scheme and host is over-specific; inventing a prefix would be wrong."""
    before = obs(node("button", "Go", node_id="n1"), url="http://x/t/demo-cu/members")
    after = obs(node("heading", "Sign In", node_id="n2"), url="http://sso.example/login")

    condition = synthesize_checkpoint(
        step(1, "click", ActionType.CLICK, n=node("button", "Go"), pre=before, post=after),
        content_frame=("content",), base_url="http://x/t/demo-cu")

    assert "/login" in condition.model_dump_json()


def test_typing_produces_no_checkpoint():
    """Asserting the value we just typed is asserting our own action back to
    ourselves."""
    before = obs(node("textbox", row_label="Member ID"))
    assert synthesize_checkpoint(
        step(1, "fill", ActionType.FILL, n=node("textbox"), value="1", pre=before, post=before),
        content_frame=("content",), base_url="http://x/t/demo-cu") is None


def test_failed_and_exploratory_steps_are_pruned_from_the_flow():
    field = node("textbox", row_label="Member ID", node_id="n1")
    before = obs(field)
    after = obs(node("heading", "Results", node_id="n9"))
    failed = step(1, "click", ActionType.CLICK, n=node("link", "Wrong Way", node_id="n8"),
                  pre=before, post=before)
    failed.ok = False
    failed.error = "nope"
    failed.pruned = True
    failed.pruned_because = "the action failed: nope"

    steps = [failed,
             step(2, "fill", ActionType.FILL, n=field, value="12345", param="member_id",
                  is_param=True, pre=before, post=before, risk=RiskTier.INPUT),
             step(3, "click", ActionType.CLICK, n=node("button", "Search", node_id="n2"),
                  pre=before, post=after)]

    artifact = record(run_with(steps), capability_id="member.lookup", product=PRODUCT,
                      app_profile_ref="meridian-core@4.2")

    assert [s.id for s in artifact.steps] == ["s1", "s2"]
    assert artifact.provenance.steps_pruned == 1
    assert artifact.provenance.steps_observed == 3
    assert artifact.provenance.model == "claude-opus-5"


def test_a_run_that_never_reached_its_goal_is_not_recorded():
    """An artifact recorded from a flow that failed would replay that failure
    deterministically, which is worse than having no capability at all."""
    with pytest.raises(RecorderRefusal, match="gave_up"):
        record(run_with([], status="gave_up"), capability_id="x", product=PRODUCT,
               app_profile_ref="meridian-core@4.2")


def test_a_flow_that_asserts_nothing_cannot_be_recorded():
    """If no step produced an observable change there is nothing to call success,
    and a capability whose success condition is 'we ran out of steps' is not a
    capability."""
    same = obs(node("textbox", row_label="Member ID", node_id="n1"))
    steps = [step(1, "fill", ActionType.FILL,
                  n=node("textbox", row_label="Member ID", node_id="n1"),
                  value="12345", param="member_id", is_param=True,
                  pre=same, post=same, risk=RiskTier.INPUT)]
    with pytest.raises(RecorderRefusal, match="nothing to assert"):
        record(run_with(steps), capability_id="x", product=PRODUCT,
               app_profile_ref="meridian-core@4.2")


def test_declared_outcomes_become_typed_results():
    """The brief's named trap, closed at record time: the model saw the app say
    'no records found' and declared it, so the capability returns it as an
    answer rather than crashing."""
    field = node("textbox", row_label="Member ID", node_id="n1")
    before = obs(field)
    notice = node("alert", "No records found matching your search criteria.", node_id="n7")
    steps = [step(1, "fill", ActionType.FILL, n=field, value="99999", param="member_id",
                  is_param=True, pre=before, post=before, risk=RiskTier.INPUT),
             step(2, "click", ActionType.CLICK, n=node("button", "Search", node_id="n2"),
                  pre=before, post=obs(notice))]

    artifact = record(
        run_with(steps, outcomes=[DeclaredOutcome(
            code="MEMBER_NOT_FOUND", hint="No member with that id", at_step=2,
            observed_node=notice)]),
        capability_id="member.lookup", product=PRODUCT, app_profile_ref="meridian-core@4.2")

    assert [o.code for o in artifact.outcomes] == ["MEMBER_NOT_FOUND"]
    rx = re.compile(artifact.outcomes[0].detect.element.name_matches)
    assert rx.search("No records found matching your search criteria.")


def test_an_irreversible_step_raises_the_capabilitys_tier_and_demands_confirmation():
    before = obs(node("button", "Confirm and Open Account", node_id="n1"))
    after = obs(node("status", "Sub-account opened successfully.", node_id="n9"))
    steps = [step(1, "click", ActionType.CLICK,
                  n=node("button", "Confirm and Open Account", node_id="n1"),
                  pre=before, post=after, risk=RiskTier.SUBMIT_IRREVERSIBLE)]

    artifact = record(run_with(steps), capability_id="member.open_subaccount",
                      product=PRODUCT, app_profile_ref="meridian-core@4.2")

    assert artifact.capability.risk_tier.value == "writes_irreversible"
    assert artifact.steps[0].requires_confirmation is True


def test_the_recorded_artifact_is_sealed_and_verifies():
    field = node("textbox", row_label="Member ID", node_id="n1")
    before = obs(field)
    steps = [step(1, "fill", ActionType.FILL, n=field, value="12345", param="member_id",
                  is_param=True, pre=before, post=before, risk=RiskTier.INPUT),
             step(2, "click", ActionType.CLICK, n=node("button", "Search", node_id="n2"),
                  pre=before, post=obs(node("heading", "Results", node_id="n9")))]
    artifact = record(run_with(steps), capability_id="member.lookup", product=PRODUCT,
                      app_profile_ref="meridian-core@4.2")

    assert artifact.content_hash.startswith("sha256:")
    assert artifact.verify_hash()
    assert artifact.capability.approval_state.value == "draft", "a fresh recording is never approved"


# --------------------------------------------------------------------------
# value shapes
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("12345", r"^\d{5}$"),
    ("50.00", r"^\d+(\.\d{2})?$"),
    ("SAV2", r"^[A-Z0-9]{2,6}$"),
])
def test_a_shape_describes_a_value_without_being_one(value, expected):
    import re

    assert value_shape(value) == expected
    assert re.fullmatch(expected, value), "the shape must actually match its value"
    assert value not in expected


def test_an_exploratory_search_is_pruned_as_a_backtrack():
    """Part 5.1's "explicit backtracks", from a real run.

    A model asked to learn how the application reports a missing member searches
    for one that does not exist, sees the notice, then searches for the real one.
    Recorded verbatim the capability replays the failed search first and returns
    MEMBER_NOT_FOUND before it ever reaches the answer -- which is exactly what
    happened the first time this flow was discovered for real.
    """
    field = node("textbox", row_label="Member ID", node_id="n1")
    before = obs(field)
    not_found = obs(node("alert", "No records found", node_id="n7"))
    results = obs(node("heading", "Search Results", node_id="n9"))

    steps = [
        step(1, "fill", ActionType.FILL, n=field, value="99999", param="member_id",
             is_param=True, pre=before, post=before, reason="try a member that does not exist"),
        step(2, "click", ActionType.CLICK, n=node("button", "Search", node_id="n2"),
             pre=before, post=not_found, reason="see how not-found is reported"),
        step(3, "fill", ActionType.FILL, n=field, value="12345", param="member_id",
             is_param=True, pre=before, post=before, reason="now the real member"),
        step(4, "click", ActionType.CLICK, n=node("button", "Search", node_id="n2"),
             pre=before, post=results, reason="run the real search"),
    ]

    artifact = record(run_with(steps), capability_id="member.lookup", product=PRODUCT,
                      app_profile_ref="meridian-core@4.2")

    assert len(artifact.steps) == 2, "the exploratory search must not be replayed"
    assert artifact.steps[0].intent == "now the real member"
    assert artifact.steps[1].intent == "run the real search"


def test_a_multi_field_form_is_not_mistaken_for_a_backtrack():
    """Three different fields filled once each is a form, not an exploration.
    Over-pruning here would silently drop half a sub-account opening."""
    member = node("textbox", row_label="Member ID", node_id="n1")
    deposit = node("textbox", row_label="Initial Deposit", node_id="n2")
    nickname = node("textbox", "Nickname", node_id="n3")
    before = obs(member, deposit, nickname)

    steps = [
        step(1, "fill", ActionType.FILL, n=member, value="12345", param="member_id",
             is_param=True, pre=before, post=before),
        step(2, "fill", ActionType.FILL, n=deposit, value="50.00", param="initial_deposit",
             is_param=True, pre=before, post=before),
        step(3, "fill", ActionType.FILL, n=nickname, value="Holiday", param="nickname",
             is_param=True, pre=before, post=before),
        step(4, "click", ActionType.CLICK, n=node("button", "Continue", node_id="n4"),
             pre=before, post=obs(node("heading", "Review", node_id="n9"))),
    ]

    artifact = record(run_with(steps), capability_id="member.open", product=PRODUCT,
                      app_profile_ref="meridian-core@4.2")

    assert len(artifact.steps) == 4
    assert set(artifact.inputs) == {"member_id", "initial_deposit", "nickname"}


# --------------------------------------------------------------------------
# Part 5.3's third canonicalization: timestamps flagged volatile
#
# Recorded in the plan as "not implemented in M4" and deferred here with the
# rest of the safety hardening, because honouring it meant changing the M2
# artifact contract. The failure it prevents is quiet by construction: a frozen
# date back-dates every future run to the day of discovery, most applications
# accept it without complaint, and nothing downstream would ever surface it.
# --------------------------------------------------------------------------

def _date_flow(value: str, *, label="Effective Date"):
    field = node("textbox", row_label=label, node_id="n1")
    before = obs(field)
    return [step(1, "fill", ActionType.FILL, n=field, value=value,
                 reason="set the effective date", pre=before, post=before,
                 risk=RiskTier.INPUT),
            step(2, "click", ActionType.CLICK, n=node("button", "Search", node_id="n2"),
                 pre=before, post=obs(node("heading", "Results", node_id="n9")),
                 risk=RiskTier.SUBMIT_REVERSIBLE)]


@pytest.mark.parametrize("typed", ["2026-09-14", "09/14/2026", "14-SEP-2026", "14:05"])
def test_a_recorded_timestamp_becomes_a_declared_input_not_a_literal(typed):
    artifact = record(run_with(_date_flow(typed)), capability_id="member.lookup",
                      product=PRODUCT, app_profile_ref="meridian-core@4.2")

    source = artifact.steps[0].action.value_from
    assert source.literal is None, f"{typed!r} was frozen into the flow"
    assert source.param == "effective_date", "named from the field's own label"

    spec = artifact.inputs["effective_date"]
    assert spec.volatile is True
    assert spec.type.value == "date"
    assert spec.default == typed, (
        "the recorded value survives as a default, so unattended replay still runs"
    )


def test_the_volatility_is_visible_to_the_calling_agent_not_only_to_a_reviewer():
    """The caller is the one holding a stale default, so the generated JSON
    Schema has to say so -- a note in the artifact only reaches a human."""
    artifact = record(run_with(_date_flow("2026-09-14")), capability_id="member.lookup",
                      product=PRODUCT, app_profile_ref="meridian-core@4.2")

    described = artifact.input_json_schema()["properties"]["effective_date"]["description"]
    assert "Time-dependent" in described
    assert "supplied by the caller" in described


def test_an_ordinary_constant_is_untouched_by_the_timestamp_rule():
    """A detector that fires on ordinary text turns every recorded constant into
    a parameter the caller now has to supply."""
    field = node("combobox", row_label="Product Code", node_id="n1")
    before = obs(field)
    steps = [step(1, "select_option", ActionType.SELECT_OPTION, n=field, value="HSA",
                  reason="choose the product code", pre=before, post=before,
                  risk=RiskTier.INPUT),
             step(2, "click", ActionType.CLICK, n=node("button", "Continue", node_id="n2"),
                  pre=before, post=obs(node("heading", "Confirm", node_id="n9")),
                  risk=RiskTier.SUBMIT_REVERSIBLE)]

    artifact = record(run_with(steps), capability_id="member.open",
                      product=PRODUCT, app_profile_ref="meridian-core@4.2")
    assert artifact.steps[0].action.value_from.literal == "HSA"
    assert artifact.inputs == {}


def test_a_date_the_application_also_displays_is_parameterized_not_refused():
    """Ordering matters, and it is not obvious.

    R-M4-1 refuses a literal that matches text the app showed on screen, because
    that is a customer's data read off a record. A date is not that: it is the
    same value because it is today. Checking volatility first is what keeps the
    recorder from failing a perfectly good recording with a diagnosis that is
    simply wrong.
    """
    field = node("textbox", row_label="Effective Date", node_id="n1")
    shown = node("cell", "2026-09-14", node_id="n8")
    before = obs(field, shown)
    steps = [step(1, "fill", ActionType.FILL, n=field, value="2026-09-14",
                  reason="set the effective date", pre=before, post=before,
                  risk=RiskTier.INPUT),
             step(2, "click", ActionType.CLICK, n=node("button", "Search", node_id="n2"),
                  pre=before, post=obs(node("heading", "Results", node_id="n9")),
                  risk=RiskTier.SUBMIT_REVERSIBLE)]

    artifact = record(run_with(steps), capability_id="member.lookup",
                      product=PRODUCT, app_profile_ref="meridian-core@4.2")
    assert artifact.inputs["effective_date"].volatile is True


def test_an_unlabelled_date_field_still_records_rather_than_refusing():
    """Falling back to a positional name trades a cosmetic loss for a real
    capability. Refusing the whole recording over a missing label would not.

    The field is addressable (it sits in a named section, so the locator ladder
    is satisfied) but carries nothing a parameter could be named after -- which
    is the case this fallback is for, and is distinct from the unaddressable
    node the recorder refuses outright.
    """
    field = node("textbox", node_id="n1", section="Filters")
    before = obs(field)
    steps = [step(1, "fill", ActionType.FILL, n=field, value="2026-09-14",
                  reason="set a date", pre=before, post=before, risk=RiskTier.INPUT),
             step(2, "click", ActionType.CLICK, n=node("button", "Search", node_id="n2"),
                  pre=before, post=obs(node("heading", "Results", node_id="n9")),
                  risk=RiskTier.SUBMIT_REVERSIBLE)]

    artifact = record(run_with(steps), capability_id="member.lookup",
                      product=PRODUCT, app_profile_ref="meridian-core@4.2")
    name = artifact.steps[0].action.value_from.param
    assert name == "recorded_date_1"
    assert artifact.inputs[name].volatile is True
