"""Surface fingerprinting and drift detection.

The fingerprint has exactly one job and one failure mode. Its job: notice that a
tenant's screen is no longer the one a capability was recorded against, and
demote that capability out of unattended use pending review. Its failure mode:
reacting to DATA. A fingerprint that changes with which member you looked up
cries wolf on every run and gets switched off, which is worse than not having one.

Most of these tests are about that second property.
"""

from __future__ import annotations

import pytest

from cua.artifact.schema import ApprovalState
from cua.perception.model import Anchors, BBox, Observation, UiNode
from cua.profiles import ProfileRepository, compare, describe_screen, fingerprint_screen
from cua.profiles.schema import DriftPolicy, IncludeSpec

LABELS = IncludeSpec(roles=("cell", "button"))


def node(role, name, *, y=0.0, **anchors) -> UiNode:
    return UiNode(
        node_id=f"{role}-{name}-{y}",
        role=role,
        name=name,
        frame_path=("content",),
        bbox=BBox(x=0, y=y, w=50, h=20),
        anchors=Anchors(**anchors),
        states={"visible": True, "enabled": True},
    )


def obs(*nodes) -> Observation:
    return Observation(nodes=tuple(nodes), frame_paths=(("content",),))


SEARCH_SCREEN = obs(
    node("cell", "Member ID"),
    node("cell", "Last Name", y=30),
    node("button", "Search"),
)


# --------------------------------------------------------------------------
# what the fingerprint ignores
# --------------------------------------------------------------------------

def test_same_screen_same_fingerprint():
    assert fingerprint_screen(SEARCH_SCREEN, LABELS) == fingerprint_screen(SEARCH_SCREEN, LABELS)


def test_data_does_not_move_the_fingerprint():
    """The single most important property. Looking up a different member is not
    drift."""
    detail = IncludeSpec(roles=("columnheader",))
    a = obs(node("columnheader", "Balance"), node("cell", "$4,210.75"))
    b = obs(node("columnheader", "Balance"), node("cell", "$88,000.00"))
    assert fingerprint_screen(a, detail) == fingerprint_screen(b, detail)


def test_typed_values_do_not_move_the_fingerprint():
    typed = SEARCH_SCREEN.model_copy(
        update={"nodes": tuple(
            n.model_copy(update={"value": "12345"}) for n in SEARCH_SCREEN.nodes
        )}
    )
    assert fingerprint_screen(typed, LABELS) == fingerprint_screen(SEARCH_SCREEN, LABELS)


def test_ordering_and_position_do_not_move_the_fingerprint():
    shuffled = obs(
        node("button", "Search", y=99),
        node("cell", "Last Name", y=5),
        node("cell", "Member ID", y=70),
    )
    assert fingerprint_screen(shuffled, LABELS) == fingerprint_screen(SEARCH_SCREEN, LABELS)


def test_casing_and_whitespace_do_not_move_the_fingerprint():
    restyled = obs(
        node("cell", "  MEMBER   ID "),
        node("cell", "Last Name", y=30),
        node("button", "Search"),
    )
    assert fingerprint_screen(restyled, LABELS) == fingerprint_screen(SEARCH_SCREEN, LABELS)


def test_unrelated_roles_are_excluded():
    noisy = obs(*SEARCH_SCREEN.nodes, node("link", "Sign Out"), node("heading", "Whatever"))
    assert fingerprint_screen(noisy, LABELS) == fingerprint_screen(SEARCH_SCREEN, LABELS)


def test_invisible_nodes_are_excluded():
    hidden = obs(*SEARCH_SCREEN.nodes,
                 node("cell", "Hidden Field").model_copy(update={"states": {"visible": False}}))
    assert fingerprint_screen(hidden, LABELS) == fingerprint_screen(SEARCH_SCREEN, LABELS)


# --------------------------------------------------------------------------
# what the fingerprint catches
# --------------------------------------------------------------------------

def test_a_renamed_field_is_drift():
    renamed = obs(
        node("cell", "Account Holder #"),
        node("cell", "Last Name", y=30),
        node("button", "Search"),
    )
    assert fingerprint_screen(renamed, LABELS) != fingerprint_screen(SEARCH_SCREEN, LABELS)


def test_a_removed_control_is_drift():
    reduced = obs(node("cell", "Member ID"), node("cell", "Last Name", y=30))
    assert fingerprint_screen(reduced, LABELS) != fingerprint_screen(SEARCH_SCREEN, LABELS)


def test_an_added_control_is_drift():
    extended = obs(*SEARCH_SCREEN.nodes, node("cell", "Branch"))
    assert fingerprint_screen(extended, LABELS) != fingerprint_screen(SEARCH_SCREEN, LABELS)


def test_a_role_change_is_drift():
    """A label turned into a button is a structural change even at the same text."""
    changed = obs(node("button", "Member ID"), node("cell", "Last Name", y=30),
                  node("button", "Search"))
    assert fingerprint_screen(changed, LABELS) != fingerprint_screen(SEARCH_SCREEN, LABELS)


# --------------------------------------------------------------------------
# verdicts
# --------------------------------------------------------------------------

def test_matching_fingerprints_verdict_ok():
    v = compare({"member_search": "sha256:a"}, {"member_search": "sha256:a"})
    assert v.verdict == "ok" and v.action == "none" and v.drifted_screens == []


def test_drift_verdict_carries_the_policy_action():
    v = compare({"member_search": "sha256:a"}, {"member_search": "sha256:b"},
                policy=DriftPolicy(on_mismatch="demote_to_draft"))
    assert v.verdict == "drifted"
    assert v.action == "demote_to_draft"
    assert v.drifted_screens == ["member_search"]


def test_drift_policy_is_configurable_per_app():
    v = compare({"s": "sha256:a"}, {"s": "sha256:b"}, policy=DriftPolicy(on_mismatch="warn"))
    assert v.action == "warn"


def test_a_screen_the_run_never_reached_is_not_drift():
    """Absence of observation is not observation of absence. Treating an
    unvisited screen as drift would demote capabilities for taking a shorter
    path through the app."""
    v = compare({"member_search": "sha256:a", "subaccount_form": "sha256:b"},
                {"member_search": "sha256:a"})
    assert v.verdict == "ok"
    assert [s.screen_id for s in v.screens] == ["member_search"]


def test_no_recorded_fingerprint_is_unknown_not_drift():
    v = compare({}, {"member_search": "sha256:a"})
    assert v.verdict == "unknown" and v.action == "none"
    assert "no recorded fingerprint" in v.describe()


def test_verdict_explains_what_actually_changed():
    """"Hash differs" is useless in review; a reviewer needs to see the rename."""
    v = compare(
        {"member_search": "sha256:a"},
        {"member_search": "sha256:b"},
        recorded_members={"member_search": ["cell|member id", "button|search"]},
        observed_members={"member_search": ["cell|account holder #", "button|search"]},
    )
    drift = v.screens[0]
    assert drift.removed == ["cell|member id"]
    assert drift.added == ["cell|account holder #"]
    assert "account holder #" in v.describe()


def test_describe_screen_lists_the_fingerprint_members():
    assert describe_screen(SEARCH_SCREEN, LABELS) == [
        "button|search", "cell|last name", "cell|member id"
    ]


def test_drift_demotes_out_of_unattended_use_without_deleting(tmp_path):
    """The flow is probably still correct -- it needs review, not deletion."""
    from cua.artifact import ArtifactStore
    from factories import lookup_balance_artifact

    store = ArtifactStore(tmp_path / "capabilities")
    store.save(lookup_balance_artifact())
    store.set_approval("member.lookup_balance@1.0.0", ApprovalState.APPROVED)

    verdict = compare({"member_search": "sha256:a"}, {"member_search": "sha256:b"})
    if verdict.action == "demote_to_draft":
        store.set_approval("member.lookup_balance@1.0.0", ApprovalState.DRIFTED)

    demoted = store.load("member.lookup_balance@1.0.0")
    assert demoted.capability.approval_state is ApprovalState.DRIFTED
    assert demoted.verify_hash(), "demotion must not break the seal"
    assert store.path_for("member.lookup_balance@1.0.0").exists()


# --------------------------------------------------------------------------
# live: the two tenants genuinely fingerprint differently
# --------------------------------------------------------------------------

async def test_tenants_fingerprint_differently_on_the_real_surface(page, live_server):
    """The cross-tenant drift signal, measured rather than asserted.

    Both tenants run Meridian Core 4.2, but valley-cu renamed the search field.
    That is exactly the divergence a capability recorded on one and replayed on
    the other needs flagged for review.
    """
    from cua.surfaces.web_playwright import WebSurface

    repo = ProfileRepository("profiles")
    screen = next(
        s for s in repo.resolve("demo-cu").profile.fingerprint.key_screens
        if s.id == "member_search"
    )

    prints, members = {}, {}
    surface = WebSurface(page)
    for tenant in ("demo-cu", "valley-cu"):
        await page.goto(f"{live_server}/t/{tenant}/login", wait_until="networkidle")
        await page.fill("input[type=text]", "operator")
        await page.fill("input[type=password]", "x")
        await page.click("input[type=submit]")
        await page.wait_for_timeout(300)
        await page.goto(f"{live_server}/t/{tenant}/members", wait_until="networkidle")

        observation = await surface.observe()
        prints[tenant] = fingerprint_screen(observation, screen.include)
        members[tenant] = describe_screen(observation, screen.include)

    assert prints["demo-cu"] != prints["valley-cu"]

    verdict = compare(
        {"member_search": prints["demo-cu"]},
        {"member_search": prints["valley-cu"]},
        recorded_members={"member_search": members["demo-cu"]},
        observed_members={"member_search": members["valley-cu"]},
    )
    assert verdict.verdict == "drifted"
    assert verdict.action == "demote_to_draft"
    assert "cell|member id" in verdict.screens[0].removed
    assert "cell|account holder #" in verdict.screens[0].added
    # ...and the parts that did NOT change are correctly not reported.
    assert "button|search" not in verdict.screens[0].removed
