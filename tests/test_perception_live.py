"""Perception and locating against the real fixture in a real browser.

This is the test that decides whether M1 is worth anything. The unit tests pin
the resolver's semantics; these pin the claim that the semantics survive contact
with markup that is actively hostile -- churned ids, no test ids, frames, and
controls with no accessible name.
"""

from __future__ import annotations

import pytest

from cua.locators import (
    Anchor,
    AnchorRelative,
    FrameRef,
    LabelFor,
    LocatorBundle,
    RoleNameExact,
    RoleNamePattern,
    SectionScope,
    resolve,
    resolve_node,
)
from cua.surfaces.base import Action, ActionType
from cua.surfaces.web_playwright import WebSurface

CONTENT = (FrameRef(name="content"),)
NAV = (FrameRef(name="nav"),)


def bundle(target_id, *candidates, frame=CONTENT, **kw) -> LocatorBundle:
    kw.setdefault("notes", "live-surface test fixture")
    return LocatorBundle(target_id=target_id, frame_path=frame, candidates=candidates, **kw)


# The member-id field: no name, no label/for, churned id. Only anchors reach it.
MEMBER_ID_FIELD = bundle(
    "member_id_input",
    RoleNameExact(role="textbox", name="Member ID"),          # will not match -- by design
    AnchorRelative(anchor=Anchor(pattern=r"(?i)member\s*id|account holder\s*#"),
                   relation="same_row", target_role="textbox"),
    notes=(
        "No accessible name and a per-render id, so candidate 0 never matches -- it is "
        "recorded only to detect the day this app starts labelling the field. The "
        "anchor-relative candidate keys on the label cell in the same row, and its "
        "pattern spans both tenants' wording."
    ),
)
SEARCH_BUTTON = bundle("search_button", RoleNameExact(role="button", name="Search"))
SIGN_IN_BUTTON = bundle("sign_in", RoleNameExact(role="button", name="Sign In"))
USER_FIELD = bundle("user", LabelFor(control_role="textbox", label_text="User ID"))
PASS_FIELD = bundle("pass", LabelFor(control_role="textbox", label_text="Password"))
NAV_SEARCH_LINK = bundle(
    "nav_member_search",
    RoleNamePattern(role="link", name_pattern=r"(?i)(member search|account holder lookup)"),
    frame=NAV,
)


async def signed_in_surface(page, live_server, tenant="demo-cu") -> WebSurface:
    """Sign in using the same semantic path replay would: bundles, not selectors."""
    await page.goto(f"{live_server}/t/{tenant}/", wait_until="networkidle")
    surface = WebSurface(page)

    assert (await surface.act(Action(ActionType.FILL, target=USER_FIELD, value="operator",
                                     reason="supply operator id"))).ok
    assert (await surface.act(Action(ActionType.FILL, target=PASS_FIELD, value="demo-pass-not-real",
                                     reason="supply password", sensitive=True))).ok
    assert (await surface.act(Action(ActionType.CLICK, target=SIGN_IN_BUTTON, reason="sign in"))).ok
    await page.wait_for_timeout(400)
    return surface


# --------------------------------------------------------------------------
# observation shape
# --------------------------------------------------------------------------

async def test_observation_spans_frames_and_keeps_paths(page, live_server):
    surface = WebSurface(page)
    await page.goto(f"{live_server}/t/demo-cu/", wait_until="networkidle")
    o = await surface.observe()

    paths = {p for p in o.frame_paths}
    assert ("nav",) in paths and ("content",) in paths
    assert any(n.frame_path == ("nav",) for n in o.nodes)
    assert any(n.frame_path == ("content",) for n in o.nodes)
    assert o.frame_urls["content"].endswith("/login?next=/t/demo-cu/home")


async def test_node_ids_are_unique_across_frames(page, live_server):
    surface = WebSurface(page)
    await page.goto(f"{live_server}/t/demo-cu/", wait_until="networkidle")
    o = await surface.observe()
    ids = [n.node_id for n in o.nodes]
    assert len(ids) == len(set(ids))


async def test_coordinates_are_page_absolute_but_normalized_stays_frame_relative(page, live_server):
    """Regression: getBoundingClientRect() is frame-relative, the mouse is not.

    Before the frame offset was applied, every click inside the content frame
    landed ~191px to the left -- on whatever the nav frame had at that point.
    It failed silently, which is why this is pinned.
    """
    surface = WebSurface(page)
    await page.goto(f"{live_server}/t/demo-cu/", wait_until="networkidle")
    o = await surface.observe()

    nav_width = 190
    content_nodes = [n for n in o.nodes if n.frame_path == ("content",)]
    assert content_nodes
    assert all(n.bbox.x >= nav_width for n in content_nodes), (
        "content-frame nodes must be offset past the nav frame in page coordinates"
    )
    # ...while the recorded normalized form stays relative to its own frame.
    assert all(0.0 <= n.bbox.nx < 1.0 for n in content_nodes)


async def test_the_search_field_really_has_no_accessible_name(page, live_server):
    """If this ever starts passing a name, the fixture stopped being hostile and
    the anchor-relative strategy stopped being exercised."""
    surface = await signed_in_surface(page, live_server)
    await page.frame(name="content").goto(f"{live_server}/t/demo-cu/members")
    o = await surface.observe()

    boxes = [n for n in o.nodes if n.role == "textbox" and n.frame_path == ("content",)]
    assert boxes, "expected text inputs on the search screen"
    target = next(n for n in boxes if n.anchors.row_label == "Member ID")
    assert target.name == ""
    assert target.anchors.label == ""
    assert target.anchors.section_label == "Member Search"


async def test_password_values_never_leave_the_dom(page, live_server):
    await page.goto(f"{live_server}/t/demo-cu/login", wait_until="networkidle")
    surface = WebSurface(page)
    await page.fill("input[type=password]", "demo-pass-not-real")
    o = await surface.observe()
    assert not any("demo-pass-not-real" in (n.value or "") for n in o.nodes)
    assert any(n.value == "<password>" for n in o.nodes)


# --------------------------------------------------------------------------
# the core claim: locating survives id churn
# --------------------------------------------------------------------------

async def test_anchor_relative_survives_id_churn_across_renders(page, live_server):
    """Same bundle, three renders, three different id sets, same control.

    This is the whole argument for LocatorBundle in one test.
    """
    surface = await signed_in_surface(page, live_server)
    content = page.frame(name="content")

    seen_ids, resolutions = set(), []
    for _ in range(3):
        await content.goto(f"{live_server}/t/demo-cu/members")
        o = await surface.observe()
        seen_ids.update(
            await content.eval_on_selector_all("[id^=ctl00_]", "els => els.map(e => e.id)")
        )
        node, res = resolve_node(MEMBER_ID_FIELD, o)
        resolutions.append((node.anchors.row_label, res.strategy, res.candidate_index))

    assert len(seen_ids) > 2, "ids did not churn; the fixture regressed"
    assert resolutions == [("Member ID", "anchor_relative", 1)] * 3
    # Candidate 0 never matches here, so every resolution is correctly degraded.
    assert all(r[2] == 1 for r in resolutions)


async def test_same_bundle_resolves_on_a_second_tenant(page, live_server):
    """One recording, two tenants -- via the pattern/anchor candidates, with no
    per-tenant override needed."""
    for tenant in ("demo-cu", "valley-cu"):
        surface = await signed_in_surface(page, live_server, tenant)
        await page.frame(name="content").goto(f"{live_server}/t/{tenant}/members")
        o = await surface.observe()
        node, res = resolve_node(MEMBER_ID_FIELD, o)
        assert node.role == "textbox"
        assert res.strategy == "anchor_relative"


# --------------------------------------------------------------------------
# acting through resolved locators, end to end
# --------------------------------------------------------------------------

async def test_full_lookup_flow_driven_entirely_by_bundles(page, live_server):
    surface = await signed_in_surface(page, live_server)

    # Cross-frame: the link lives in `nav`, the result lands in `content`.
    assert (await surface.act(Action(ActionType.CLICK, target=NAV_SEARCH_LINK,
                                     reason="open member search"))).ok
    await page.wait_for_timeout(400)

    fill = await surface.act(Action(ActionType.FILL, target=MEMBER_ID_FIELD, value="12345",
                                    reason="enter the member id"))
    assert fill.ok and fill.resolved.strategy == "anchor_relative"

    assert (await surface.act(Action(ActionType.CLICK, target=SEARCH_BUTTON,
                                     reason="run the search"))).ok
    await page.wait_for_timeout(400)

    o = await surface.observe()
    assert any("Whitfield" in n.name for n in o.nodes)

    # ...into the detail screen, then read the savings balance as a grid lookup.
    assert (await surface.act(Action(ActionType.CLICK,
                                     target=bundle("result_link",
                                                   RoleNameExact(role="link", name="12345")),
                                     reason="open the member record"))).ok
    await page.wait_for_timeout(400)

    balance = bundle(
        "savings_balance",
        AnchorRelative(anchor=Anchor(text="Balance"), relation="same_column", target_role="cell",
                       scope=SectionScope(anchor=Anchor(pattern="Savings"), relation="within_row")),
    )
    node, res = resolve_node(balance, await surface.observe())
    assert node.name == "$4,210.75"
    assert res.competitor_count == 0


async def test_grid_lookup_is_not_positional(page, live_server):
    """Checking accounts sit above savings in the fixture. A row ordinal would
    read the wrong balance; the anchored lookup does not."""
    surface = await signed_in_surface(page, live_server)
    await page.frame(name="content").goto(f"{live_server}/t/demo-cu/members/34567")
    o = await surface.observe()

    savings = resolve_node(
        bundle("savings",
               AnchorRelative(anchor=Anchor(text="Balance"), relation="same_column",
                              target_role="cell",
                              scope=SectionScope(anchor=Anchor(pattern="Savings"),
                                                 relation="within_row"))),
        o,
    )[0]
    certificate = resolve_node(
        bundle("cert",
               AnchorRelative(anchor=Anchor(text="Balance"), relation="same_column",
                              target_role="cell",
                              scope=SectionScope(anchor=Anchor(pattern="Certificate"),
                                                 relation="within_row"))),
        o,
    )[0]
    assert savings.name == "$750.00"
    assert certificate.name == "$25,000.00"


# --------------------------------------------------------------------------
# failure behaviour on the live surface
# --------------------------------------------------------------------------

async def test_acting_on_a_missing_target_fails_without_touching_the_page(page, live_server):
    surface = await signed_in_surface(page, live_server)
    await page.frame(name="content").goto(f"{live_server}/t/demo-cu/members")
    before = page.frame(name="content").url

    result = await surface.act(Action(
        ActionType.CLICK,
        target=bundle("ghost", RoleNameExact(role="button", name="Approve Wire Transfer")),
        reason="click a control that does not exist",
    ))
    assert not result.ok
    assert result.error_detail["failure_class"] == "LOCATOR_UNRESOLVED"
    assert page.frame(name="content").url == before


async def test_an_obstructing_modal_is_visible_in_the_observation(page, live_server):
    """The interstitial must be perceivable, or recovery can never detect it."""
    surface = await signed_in_surface(page, live_server)
    import httpx

    httpx.post(f"{live_server}/t/demo-cu/__control", params={"fault": "interstitial"}, timeout=10)
    await page.frame(name="content").goto(f"{live_server}/t/demo-cu/members")
    o = await surface.observe()

    dialogs = [n for n in o.nodes if n.role == "dialog"]
    assert dialogs and dialogs[0].name.startswith("What's New")
    close = resolve(
        bundle("dismiss",
               RoleNameExact(role="button", name="Close",
                             scope=SectionScope(anchor=Anchor(pattern=r"(?i)what's new"),
                                                relation="within_dialog"))),
        o,
    )
    assert close.node_id
