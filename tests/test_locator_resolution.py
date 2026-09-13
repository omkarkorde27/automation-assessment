"""Resolver semantics. No browser, no server -- hand-built observations.

The behaviour under test is the part that is easy to get wrong and expensive to
get wrong: what happens when a locator matches more than one thing. These tests
exist to pin "escalate rather than guess" in place, because the tempting fix
when a locator goes ambiguous is always to take the first match.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cua.locators import (
    Anchor,
    AnchorRelative,
    BBoxNormalized,
    FrameRef,
    LabelFor,
    LocatorAmbiguous,
    LocatorBundle,
    LocatorUnresolved,
    RoleNameExact,
    RoleNamePattern,
    RoleOrdinal,
    SectionScope,
    resolve,
)
from cua.locators.resolve import FrameUnresolved
from cua.perception.model import Anchors, BBox, Observation, UiNode, normalize_name


def node(node_id, role, name="", *, frame=("content",), y=0.0, **anchors) -> UiNode:
    return UiNode(
        node_id=node_id,
        role=role,
        name=name,
        frame_path=frame,
        bbox=BBox(x=10, y=y, w=100, h=20, nx=0.01, ny=y / 1000, nw=0.1, nh=0.02),
        anchors=Anchors(**anchors),
        states={"visible": True, "enabled": True},
    )


def obs(*nodes, frames=(("content",),)) -> Observation:
    return Observation(
        url="http://app/t/demo-cu/members",
        nodes=tuple(nodes),
        frame_paths=tuple(frames),
        frame_urls={"content": "http://app/t/demo-cu/members"},
    )


def bundle(*candidates, **kw) -> LocatorBundle:
    kw.setdefault("frame_path", (FrameRef(name="content"),))
    # Boilerplate notes: these tests exercise resolution semantics, where the
    # reasoning text is irrelevant. Enforcement of the real requirement is
    # pinned by test_a_bundle_without_reasoning_is_rejected below.
    kw.setdefault("notes", "resolver unit-test fixture")
    return LocatorBundle(target_id=kw.pop("target_id", "t1"), candidates=candidates, **kw)


# --------------------------------------------------------------------------
# name normalization
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Member ID", "member id"),
        ("  Member   ID  ", "member id"),
        ("Member ID:", "member id"),
        ("Member ID *", "member id"),
        ("Account Holder #", "account holder #"),   # interior punctuation kept
        (None, ""),
    ],
)
def test_normalize_name(raw, expected):
    assert normalize_name(raw) == expected


def test_a_bundle_without_reasoning_is_rejected():
    """`notes` is required at the schema level.

    A locator with no stated reasoning cannot be reviewed: someone deciding
    whether a capability may run unattended against a bank's back office cannot
    judge role=textbox name='Member ID' without knowing whether that name is
    stable, branded, or per-tenant. The brief asks for the reasoning explicitly,
    so the schema refuses a bundle that omits it.
    """
    candidates = (RoleNameExact(role="button", name="Search"),)
    with pytest.raises(ValidationError):
        LocatorBundle(target_id="t1", candidates=candidates)

    ok = LocatorBundle(target_id="t1", candidates=candidates,
                       notes="Named by @value, stable across tenants.")
    assert ok.notes


def test_hand_authored_bundles_need_no_record_time_snapshot():
    """`recorded` and `stability_score` stay optional on purpose: the login
    recipe and interstitial-dismiss bundles are authored in the app profile and
    were never observed by the recorder."""
    authored = LocatorBundle(
        target_id="login_submit_button",
        candidates=(RoleNameExact(role="button", name="Sign In"),),
        notes="Submit input named by @value.",
    )
    assert authored.recorded is None
    assert authored.stability_score == 0.0


def test_normalization_keeps_tenants_distinguishable():
    """The two tenants' labels must not collapse into each other."""
    assert normalize_name("Account Holder #") != normalize_name("Account Holder")


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------

def test_exact_name_resolves_and_is_not_degraded():
    o = obs(node("n1", "button", "Search"), node("n2", "textbox", "Last Name"))
    r = resolve(bundle(RoleNameExact(role="button", name="Search")), o)
    assert (r.node_id, r.strategy, r.degraded, r.candidate_index) == ("n1", "role_name_exact", False, 0)


def test_name_matching_is_normalized():
    o = obs(node("n1", "textbox", "Last Name:"))
    assert resolve(bundle(RoleNameExact(role="textbox", name="last name")), o).node_id == "n1"


def test_pattern_candidate_spans_tenant_wording():
    """One bundle, both tenants -- the reason a pattern candidate is recorded."""
    cand = RoleNamePattern(role="cell", name_pattern=r"(?i)(member\s*id|account holder\s*#)")
    for label in ("Member ID", "Account Holder #"):
        assert resolve(bundle(cand), obs(node("n1", "cell", label))).node_id == "n1"


def test_falling_back_to_a_later_candidate_is_flagged_degraded():
    """Not an error -- the early-warning signal for drift."""
    o = obs(node("n1", "textbox", "", row_label="Member ID"))
    r = resolve(
        bundle(
            RoleNameExact(role="textbox", name="Member ID"),          # no longer matches
            AnchorRelative(anchor=Anchor(text="Member ID"), relation="same_row", target_role="textbox"),
        ),
        o,
    )
    assert r.node_id == "n1"
    assert r.degraded and r.candidate_index == 1 and r.strategy == "anchor_relative"


# --------------------------------------------------------------------------
# ambiguity: the rule that matters
# --------------------------------------------------------------------------

def test_ambiguous_match_raises_rather_than_guessing():
    o = obs(node("n1", "button", "Search", y=10), node("n2", "button", "Search", y=90))
    with pytest.raises(LocatorAmbiguous) as exc:
        resolve(bundle(RoleNameExact(role="button", name="Search")), o)
    assert exc.value.failure_class == "LOCATOR_AMBIGUOUS"
    assert len(exc.value.matches) == 2


def test_ambiguity_never_resolves_to_the_first_in_document_order():
    """Explicit: document order is not a tiebreaker."""
    o = obs(node("n1", "button", "Go", y=10), node("n2", "button", "Go", y=90))
    with pytest.raises(LocatorAmbiguous):
        resolve(bundle(RoleNameExact(role="button", name="Go")), o)


def test_a_later_candidate_can_disambiguate():
    o = obs(
        node("n1", "button", "Search", section_label="Member Search", y=10),
        node("n2", "button", "Search", section_label="Account Filter", y=90),
    )
    r = resolve(
        bundle(
            RoleNameExact(role="button", name="Search"),
            RoleNameExact(role="button", name="Search",
                          scope=SectionScope(anchor=Anchor(text="Member Search"),
                                             relation="within_section")),
        ),
        o,
    )
    assert r.node_id == "n1"
    assert r.disambiguated_by == ("role_name_exact",)
    assert r.competitor_count == 1
    assert r.degraded


def test_a_filter_that_matches_nothing_is_discarded_not_applied():
    """An over-specific filter is evidence the filter is wrong, not that the
    target disappeared -- so it must not turn ambiguity into unresolved."""
    o = obs(node("n1", "button", "Search", y=10), node("n2", "button", "Search", y=90))
    with pytest.raises(LocatorAmbiguous):
        resolve(
            bundle(
                RoleNameExact(role="button", name="Search"),
                RoleNameExact(role="button", name="Search",
                              scope=SectionScope(anchor=Anchor(text="Nowhere"),
                                                 relation="within_section")),
            ),
            o,
        )


def test_best_scored_policy_accepts_ambiguity_but_demands_justification():
    # Constructed directly rather than through the helper: the helper supplies
    # boilerplate notes, which would satisfy the validator and hide what is
    # under test here -- that opting into ambiguity requires saying why.
    with pytest.raises(ValidationError, match="justification"):
        LocatorBundle(
            target_id="t1",
            frame_path=(FrameRef(name="content"),),
            candidates=(RoleNameExact(role="row", name="result"),),
            match_policy="best_scored",
            notes="",
        )

    o = obs(node("n1", "row", "result", y=10), node("n2", "row", "result", y=90))
    r = resolve(
        bundle(RoleNameExact(role="row", name="result"),
               match_policy="best_scored", notes="first result row is intended"),
        o,
    )
    assert r.node_id == "n1" and r.degraded and r.competitor_count == 1


# --------------------------------------------------------------------------
# unresolved
# --------------------------------------------------------------------------

def test_no_candidate_matches_raises_unresolved_with_what_was_tried():
    o = obs(node("n1", "textbox", "Last Name"))
    with pytest.raises(LocatorUnresolved) as exc:
        resolve(bundle(RoleNameExact(role="button", name="Search")), o)
    assert exc.value.failure_class == "LOCATOR_UNRESOLVED"
    assert exc.value.tried and "role_name_exact(0 match)" in exc.value.tried


def test_wrong_frame_never_silently_falls_back_to_the_top_document():
    o = obs(node("n1", "button", "Search", frame=()), frames=((),))
    with pytest.raises(FrameUnresolved):
        resolve(bundle(RoleNameExact(role="button", name="Search"),
                       frame_path=(FrameRef(name="content"),)), o)


def test_frame_can_be_matched_by_url_pattern_when_unnamed():
    o = Observation(
        nodes=(node("n1", "button", "Search", frame=("#0",)),),
        frame_paths=(("#0",),),
        frame_urls={"#0": "http://app/t/demo-cu/members"},
    )
    r = resolve(bundle(RoleNameExact(role="button", name="Search"),
                       frame_path=(FrameRef(url_pattern=r"/members$"),)), o)
    assert r.node_id == "n1"


# --------------------------------------------------------------------------
# anchor-relative: unnamed controls in table layouts
# --------------------------------------------------------------------------

def test_anchor_relative_finds_a_control_with_no_accessible_name():
    """The fixture's member-id field. Nothing else can reach it."""
    o = obs(
        node("n1", "cell", "Member ID", row_label="", y=10),
        node("n2", "textbox", "", row_label="Member ID", y=10),
        node("n3", "textbox", "", row_label="Last Name", y=40),
    )
    r = resolve(
        bundle(AnchorRelative(anchor=Anchor(text="Member ID"),
                              relation="same_row", target_role="textbox")),
        o,
    )
    assert r.node_id == "n2"


def test_anchor_relative_occurrence_selects_among_siblings():
    o = obs(
        node("n1", "textbox", "", row_label="Member ID", y=10),
        node("n2", "textbox", "", row_label="Member ID", y=10),
    )
    got = resolve(
        bundle(AnchorRelative(anchor=Anchor(text="Member ID"), relation="same_row",
                              target_role="textbox", occurrence=1)),
        o,
    )
    assert got.node_id == "n2"


def test_grid_read_is_a_two_dimensional_lookup():
    """The savings balance: the Balance cell of the row containing 'Savings'.

    Stable against row reordering, which an ordinal would not be.
    """
    rows = []
    for i, (num, kind, bal) in enumerate(
        [("100045219", "Checking", "$812.30"), ("100045218", "Savings", "$4,210.75")]
    ):
        row = f"{num} {kind} {bal} Open"
        rows += [
            node(f"a{i}", "cell", num, row_label="", row_text=row, col_header="Account Number", y=i * 30),
            node(f"k{i}", "cell", kind, row_label=num, row_text=row, col_header="Type", y=i * 30),
            node(f"b{i}", "cell", bal, row_label=num, row_text=row, col_header="Balance", y=i * 30),
        ]
    r = resolve(
        bundle(AnchorRelative(
            anchor=Anchor(text="Balance"), relation="same_column", target_role="cell",
            scope=SectionScope(anchor=Anchor(pattern="Savings"), relation="within_row"),
        )),
        obs(*rows),
    )
    assert r.node_id == "b1"   # the savings row, not the first row


def test_label_for_candidate():
    o = obs(node("n1", "combobox", "Product Code", label="Product Code"))
    assert resolve(bundle(LabelFor(control_role="combobox", label_text="Product Code")), o).node_id == "n1"


def test_positional_relations_use_document_order():
    o = obs(
        node("n1", "cell", "Nickname", y=10),
        node("n2", "textbox", "", y=10),
        node("n3", "textbox", "", y=40),
    )
    following = resolve(
        bundle(AnchorRelative(anchor=Anchor(text="Nickname"), relation="following",
                              target_role="textbox")),
        o,
    )
    assert following.node_id == "n2"


# --------------------------------------------------------------------------
# weak strategies stay gated
# --------------------------------------------------------------------------

def test_ordinal_requires_a_scope_at_schema_level():
    """A document-wide ordinal retargets the moment anything is inserted above
    it, so the schema refuses to express one."""
    with pytest.raises(ValidationError):
        RoleOrdinal(role="textbox", ordinal=0)


def test_scoped_ordinal_resolves():
    o = obs(
        node("n1", "textbox", "", section_label="Member Search", y=10),
        node("n2", "textbox", "", section_label="Member Search", y=40),
        node("n3", "textbox", "", section_label="Other Panel", y=70),
    )
    r = resolve(
        bundle(RoleOrdinal(role="textbox", ordinal=1,
                           scope=SectionScope(anchor=Anchor(text="Member Search"),
                                              relation="within_section"))),
        o,
    )
    assert r.node_id == "n2"


def test_coordinate_candidate_is_skipped_unless_explicitly_allowed():
    o = obs(node("n1", "button", "", y=100))
    b = bundle(BBoxNormalized(x=0.01, y=0.1, w=0.1, h=0.02))
    with pytest.raises(LocatorUnresolved) as exc:
        resolve(b, o)
    assert any("skipped" in t for t in exc.value.tried)

    allowed = bundle(BBoxNormalized(x=0.01, y=0.1, w=0.1, h=0.02), allow_unstable=True)
    assert resolve(allowed, o).node_id == "n1"


def test_resolution_score_ranks_strategies():
    exact = obs(node("n1", "button", "Search"))
    weak = obs(node("n1", "textbox", "", section_label="Panel"))
    s_exact = resolve(bundle(RoleNameExact(role="button", name="Search")), exact).score
    s_weak = resolve(
        bundle(RoleOrdinal(role="textbox", ordinal=0,
                           scope=SectionScope(anchor=Anchor(text="Panel"),
                                              relation="within_section"))),
        weak,
    ).score
    assert s_exact > s_weak


# --------------------------------------------------------------------------
# observation-level signals
# --------------------------------------------------------------------------

def test_state_hash_ignores_values_but_tracks_structure():
    a = obs(node("n1", "textbox", "Member ID"))
    b = Observation(nodes=(node("n1", "textbox", "Member ID").model_copy(update={"value": "12345"}),))
    assert a.state_hash() == b.state_hash(), "typing must not look like progress"

    c = obs(node("n1", "textbox", "Member ID"), node("n2", "heading", "Search Results"))
    assert a.state_hash() != c.state_hash(), "a new screen must look like progress"
