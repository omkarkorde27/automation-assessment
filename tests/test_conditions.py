"""Condition evaluation, including the two requirements the plan names.

R-M3-1 — an operator the surface cannot evaluate is never answered with a bool.
R-M3-2 — the two `frame` defaults are opposite, on purpose.
"""

from __future__ import annotations

import pytest

from cua.conditions.dsl import (
    EvalContext,
    UnsupportedOperator,
    evaluate,
    operators_used,
    unsupported,
)
from cua.conditions.model import (
    AllCondition,
    AnyCondition,
    ElementCondition,
    ElementMatch,
    NotCondition,
    TextCondition,
    UrlCondition,
    UrlMatch,
    describe,
)
from cua.locators.model import Anchor, FrameRef, SectionScope
from cua.perception.model import Anchors, BBox, Observation, UiNode
from cua.surfaces.base import SurfaceCapabilities, SurfaceKind

CONTENT = (FrameRef(name="content"),)
NAV = (FrameRef(name="nav"),)

WEB = SurfaceCapabilities(kind=SurfaceKind.LEGACY_WEB, has_url=True, has_frames=True)
DESKTOP = SurfaceCapabilities(
    kind=SurfaceKind.DESKTOP, has_url=False, has_frames=False, can_navigate=False
)


def node(role, name="", *, frame=("content",), value=None, visible=True, enabled=True, **anchors):
    return UiNode(
        node_id=f"{frame}-{role}-{name}",
        role=role,
        name=name,
        value=value,
        frame_path=frame,
        bbox=BBox(x=0, y=0, w=10, h=10),
        anchors=Anchors(**anchors),
        states={"visible": visible, "enabled": enabled},
    )


def obs(*nodes, url="http://app/t/demo-cu/", content_url="http://app/t/demo-cu/members"):
    paths = sorted({n.frame_path for n in nodes})
    return Observation(
        url=url,
        nodes=tuple(nodes),
        frame_paths=tuple(paths),
        frame_urls={"content": content_url, "nav": "http://app/t/demo-cu/nav", "": url},
    )


def ctx(observation, *, caps=WEB, page_url="http://app/t/demo-cu/"):
    return EvalContext(observation=observation, page_url=page_url, capabilities=caps)


# --------------------------------------------------------------------------
# element conditions
# --------------------------------------------------------------------------

def test_element_exists():
    c = ElementCondition(element=ElementMatch(role="alert"))
    assert evaluate(c, ctx(obs(node("alert", "No records found"))))
    assert not evaluate(c, ctx(obs(node("cell", "Member ID"))))


def test_element_absent_is_expressed_as_exists_false():
    """The plan lists `absent` as an operator; it is expressed `exists: false`."""
    c = ElementCondition(element=ElementMatch(role="dialog"), exists=False)
    assert evaluate(c, ctx(obs(node("cell", "x"))))
    assert not evaluate(c, ctx(obs(node("dialog", "What's New"))))


def test_name_matching_is_normalized_and_pattern_capable():
    exact = ElementCondition(element=ElementMatch(role="cell", name="member id"))
    assert evaluate(exact, ctx(obs(node("cell", "Member ID:"))))

    pattern = ElementCondition(
        element=ElementMatch(role="cell", name_matches=r"(?i)member\s*id|account holder")
    )
    assert evaluate(pattern, ctx(obs(node("cell", "Account Holder #"))))


def test_invisible_nodes_do_not_satisfy_a_condition():
    c = ElementCondition(element=ElementMatch(role="alert"))
    assert not evaluate(c, ctx(obs(node("alert", "hidden", visible=False))))


def test_value_and_enabled_matching():
    filled = ElementCondition(element=ElementMatch(role="textbox", value_matches=r"^\d{5}$"))
    assert evaluate(filled, ctx(obs(node("textbox", "", value="12345"))))

    disabled = ElementCondition(element=ElementMatch(role="button", enabled=False))
    assert evaluate(disabled, ctx(obs(node("button", "Confirm", enabled=False))))
    assert not evaluate(disabled, ctx(obs(node("button", "Confirm"))))


def test_min_count_asserts_at_least_n():
    c = ElementCondition(element=ElementMatch(role="row"), min_count=2)
    assert not evaluate(c, ctx(obs(node("row", "r1"))))
    assert evaluate(c, ctx(obs(node("row", "r1"), node("row", "r2"))))


def test_scope_reuses_the_locator_semantics():
    """A checkpoint and a locator must agree on what "within this row" means."""
    c = ElementCondition(element=ElementMatch(
        role="cell", scope=SectionScope(anchor=Anchor(text="SSN"), relation="within_row")))
    assert evaluate(c, ctx(obs(node("cell", "521-84-9077", row_label="SSN"))))
    assert not evaluate(c, ctx(obs(node("cell", "Riverside", row_label="Branch"))))


def test_text_matches_narrows_an_element_condition():
    c = ElementCondition(element=ElementMatch(role="alert"), text_matches=r"(?i)no records")
    assert evaluate(c, ctx(obs(node("alert", "No records found matching your criteria."))))
    assert not evaluate(c, ctx(obs(node("alert", "Not authorized."))))


# --------------------------------------------------------------------------
# R-M3-2: the two frame defaults are opposite
# --------------------------------------------------------------------------

def test_element_condition_without_a_frame_searches_any_frame():
    """A checkpoint usually cares that something is on screen, not where."""
    c = ElementCondition(element=ElementMatch(role="link", name="Sign Out"))
    assert evaluate(c, ctx(obs(node("link", "Sign Out", frame=("nav",)))))


def test_element_condition_with_a_frame_is_restricted_to_it():
    c = ElementCondition(element=ElementMatch(role="link", name="Sign Out"), frame=CONTENT)
    assert not evaluate(c, ctx(obs(node("link", "Sign Out", frame=("nav",)))))
    assert evaluate(c, ctx(obs(node("link", "Sign Out", frame=("content",)))))


def test_url_condition_without_a_frame_reads_the_top_level_page():
    c = UrlCondition(url=UrlMatch(matches="/members$"))
    # The shell URL is the tenant root; only the content frame is at /members.
    assert not evaluate(c, ctx(obs(node("cell", "x"))))


def test_url_condition_with_a_frame_reads_that_frames_url():
    """In a frameset the page URL is the shell and never changes, so a flow's
    real location must be named explicitly or the checkpoint asserts nothing."""
    c = UrlCondition(url=UrlMatch(matches="/members$"), frame=CONTENT)
    assert evaluate(c, ctx(obs(node("cell", "x"))))


def test_the_two_frame_defaults_are_genuinely_opposite():
    """Pinning R-M3-2 as one statement, so a future 'tidy-up' cannot unify them."""
    o = obs(node("link", "Sign Out", frame=("nav",)))
    element_any_frame = ElementCondition(element=ElementMatch(role="link", name="Sign Out"))
    url_top_level_only = UrlCondition(url=UrlMatch(matches="/members$"))

    assert evaluate(element_any_frame, ctx(o)) is True     # found outside the content frame
    assert evaluate(url_top_level_only, ctx(o)) is False   # did NOT fall through to a frame url


# --------------------------------------------------------------------------
# combinators
# --------------------------------------------------------------------------

def test_all_any_not():
    present = ElementCondition(element=ElementMatch(role="alert"))
    absent = ElementCondition(element=ElementMatch(role="dialog"), exists=False)
    o = ctx(obs(node("alert", "x")))

    assert evaluate(AllCondition(all=(present, absent)), o)
    assert evaluate(AnyCondition(any=(present, present)), o)
    assert not evaluate(NotCondition(**{"not": present}), o)
    assert evaluate(NotCondition(**{"not": absent}), ctx(obs(node("dialog", "d"))))


def test_nested_combinators():
    c = AllCondition(all=(
        AnyCondition(any=(
            UrlCondition(url=UrlMatch(matches="/members$"), frame=CONTENT),
            ElementCondition(element=ElementMatch(role="heading", name="Results")),
        )),
        NotCondition(**{"not": ElementCondition(element=ElementMatch(role="dialog"))}),
    ))
    assert evaluate(c, ctx(obs(node("cell", "x"))))
    assert not evaluate(c, ctx(obs(node("cell", "x"), node("dialog", "What's New"))))


def test_text_present_searches_visible_text():
    c = TextCondition(text_present=r"(?i)servletexception")
    assert evaluate(c, ctx(obs(node("cell", "javax.servlet.ServletException: boom"))))
    assert not evaluate(c, ctx(obs(node("cell", "all good"))))


# --------------------------------------------------------------------------
# R-M3-1: unsupported operators
# --------------------------------------------------------------------------

def test_evaluator_never_silently_passes_an_unsupported_operator():
    """The named test from R-M3-1.

    Returning False would look like an honest checkpoint failure and send a
    healthy run to a human; returning True would wave an unverified flow
    through. Neither is acceptable, so it raises.
    """
    c = UrlCondition(url=UrlMatch(matches="/members$"))
    with pytest.raises(UnsupportedOperator) as exc:
        evaluate(c, ctx(obs(node("cell", "x")), caps=DESKTOP))

    assert exc.value.operator == "url"
    assert exc.value.failure_class == "CAPABILITY_UNSUPPORTED"
    assert "desktop" in exc.value.reason


def test_frame_scoping_is_unsupported_on_a_frameless_surface():
    c = ElementCondition(element=ElementMatch(role="cell"), frame=CONTENT)
    with pytest.raises(UnsupportedOperator) as exc:
        evaluate(c, ctx(obs(node("cell", "x")), caps=DESKTOP))
    assert exc.value.operator == "frame"


def test_an_unsupported_branch_is_not_masked_by_a_true_sibling():
    """`any` does not short-circuit past an operator it cannot evaluate: if one
    branch is unknown the disjunction is unknown, and an unknown must surface."""
    c = AnyCondition(any=(
        ElementCondition(element=ElementMatch(role="cell")),          # true
        UrlCondition(url=UrlMatch(matches="/members$")),              # unevaluable
    ))
    with pytest.raises(UnsupportedOperator):
        evaluate(c, ctx(obs(node("cell", "x")), caps=DESKTOP))


def test_supported_operators_still_evaluate_on_a_desktop_surface():
    """The point is capability-awareness, not refusing desktop surfaces."""
    c = ElementCondition(element=ElementMatch(role="button", name="Post"))
    assert evaluate(c, ctx(obs(node("button", "Post", frame=())), caps=DESKTOP))


def test_no_capabilities_declared_means_evaluate_everything():
    """Evaluating a condition against a saved observation, after the fact, has
    no surface to ask -- and must not fail for that reason."""
    c = UrlCondition(url=UrlMatch(matches="/members$"), frame=CONTENT)
    assert evaluate(c, EvalContext(observation=obs(node("cell", "x"))))


# --------------------------------------------------------------------------
# preflight
# --------------------------------------------------------------------------

def test_operators_used_walks_the_whole_tree():
    c = AllCondition(all=(
        ElementCondition(element=ElementMatch(role="cell"), frame=CONTENT),
        NotCondition(**{"not": UrlCondition(url=UrlMatch(matches="/x"))}),
        TextCondition(text_present="y"),
    ))
    assert {u.operator for u in operators_used(c, "checkpoint")} == {
        "element", "frame", "url", "text_present"
    }


def test_unsupported_reports_every_offending_condition_with_its_location():
    problems = unsupported(
        [
            ("step s1 checkpoint", UrlCondition(url=UrlMatch(matches="/a"))),
            ("outcome NOT_FOUND", ElementCondition(element=ElementMatch(role="alert"))),
            ("success checkpoint", UrlCondition(url=UrlMatch(matches="/b"))),
        ],
        DESKTOP,
    )
    assert [p.where for p in problems] == ["step s1 checkpoint", "success checkpoint"]
    assert all(p.operator == "url" for p in problems)


def test_unsupported_is_empty_on_a_capable_surface():
    assert unsupported([("c", UrlCondition(url=UrlMatch(matches="/a")))], WEB) == []


# --------------------------------------------------------------------------
# describe(): failure messages a reviewer can act on
# --------------------------------------------------------------------------

def test_describe_renders_conditions_in_words():
    c = ElementCondition(element=ElementMatch(role="heading", name_matches="(?i)account opened"))
    assert describe(c) == "heading matching /(?i)account opened/ is present"

    assert "url matches" in describe(UrlCondition(url=UrlMatch(matches="/confirm")))
    assert " AND " in describe(AllCondition(all=(c, c)))
    assert describe(NotCondition(**{"not": c})).startswith("NOT ")
