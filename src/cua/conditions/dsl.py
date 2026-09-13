"""Condition evaluation -- the deterministic half of replay.

Every checkpoint, outcome detector, recovery trigger and hard-failure detector
runs through here. There is no model in this path and no way to put one in: the
input is an `Observation` and a URL, the output is a bool, and the only thing
that decides is data from the artifact.

Two rules from the plan are enforced structurally rather than by convention:

**R-M3-1 -- an operator the surface cannot evaluate is never silently answered.**
`evaluate()` raises `UnsupportedOperator` rather than returning a bool it cannot
justify. Returning `False` would look like an honest checkpoint failure and send
a healthy run to a human; returning `True` would wave an unverified flow through.
The replay engine's preflight catches these before anything is clicked -- this
raise is the backstop for paths preflight did not reach.

**R-M3-2 -- the two `frame` defaults are opposite, on purpose.**
`ElementCondition.frame` unset means *any frame*: a checkpoint usually cares that
something is on screen, not where. `UrlCondition.frame` unset means *the
top-level page*: in a frameset the page URL is the shell, so a flow's real
location has to be named explicitly or a checkpoint would assert against a URL
that never changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..locators.resolve import frame_path_matches
from ..perception.model import Observation, UiNode, normalize_name
from ..surfaces.base import SurfaceCapabilities
from .model import (
    AllCondition,
    AnyCondition,
    Condition,
    ElementCondition,
    ElementMatch,
    NotCondition,
    TextCondition,
    UrlCondition,
    describe,
)


class UnsupportedOperator(Exception):
    """This surface cannot evaluate this operator (R-M3-1)."""

    failure_class = "CAPABILITY_UNSUPPORTED"

    def __init__(self, operator: str, reason: str, condition: str = "") -> None:
        super().__init__(f"cannot evaluate '{operator}' on this surface: {reason}")
        self.operator = operator
        self.reason = reason
        self.condition = condition

    def as_detail(self) -> dict:
        return {
            "failure_class": self.failure_class,
            "operator": self.operator,
            "reason": self.reason,
            "condition": self.condition,
        }


@dataclass(frozen=True)
class EvalContext:
    """Everything needed to answer a condition, and nothing else.

    No surface, no page, no driver: evaluation is a pure function of a snapshot,
    which is what makes a checkpoint reproducible from evidence after the fact.
    """

    observation: Observation
    page_url: str = ""
    capabilities: SurfaceCapabilities | None = None

    def supports(self, operator: str) -> tuple[bool, str]:
        caps = self.capabilities
        if caps is None:
            return True, ""
        if operator == "url" and not caps.has_url:
            return False, f"a {caps.kind.value} surface has no URL"
        if operator == "frame" and not caps.has_frames:
            return False, f"a {caps.kind.value} surface has no frames"
        return True, ""


# --------------------------------------------------------------------------
# node matching
# --------------------------------------------------------------------------

def _regex(pattern: str) -> re.Pattern | None:
    try:
        return re.compile(pattern)
    except re.error:
        return None


def _node_matches(node: UiNode, match: ElementMatch) -> bool:
    if match.role is not None and node.role != match.role:
        return False
    if match.name is not None and node.norm_name != normalize_name(match.name):
        return False
    if match.name_matches is not None:
        rx = _regex(match.name_matches)
        if rx is None or not rx.search(node.name or ""):
            return False
    if match.value is not None and (node.value or "") != match.value:
        return False
    if match.value_matches is not None:
        rx = _regex(match.value_matches)
        if rx is None or not rx.search(node.value or ""):
            return False
    if match.enabled is not None and node.enabled is not match.enabled:
        return False
    if match.scope is not None:
        # Reuse the locator layer's scope semantics so a condition and a locator
        # never disagree about what "within this row" means.
        from ..locators.resolve import _in_scope

        if not _in_scope(node, match.scope):
            return False
    return True


def _nodes_in_frame(ctx: EvalContext, frame) -> list[UiNode]:
    """R-M3-2: no frame means ANY frame for element and text conditions."""
    if frame is None:
        return list(ctx.observation.nodes)
    return [
        n for n in ctx.observation.nodes
        if frame_path_matches(n.frame_path, tuple(frame), ctx.observation)
    ]


def _url_for(ctx: EvalContext, frame) -> str:
    """R-M3-2: no frame means the TOP-LEVEL page url."""
    if frame is None:
        return ctx.page_url
    for path in ctx.observation.frame_paths:
        if frame_path_matches(path, tuple(frame), ctx.observation):
            return ctx.observation.frame_urls.get("/".join(path), "")
    return ""


# --------------------------------------------------------------------------
# evaluation
# --------------------------------------------------------------------------

def evaluate(cond: Condition, ctx: EvalContext) -> bool:
    """Answer one condition, or raise if this surface cannot."""

    if isinstance(cond, ElementCondition):
        if cond.frame is not None:
            ok, why = ctx.supports("frame")
            if not ok:
                raise UnsupportedOperator("frame", why, describe(cond))
        pool = _nodes_in_frame(ctx, cond.frame)
        matched = [n for n in pool if n.visible and _node_matches(n, cond.element)]

        if cond.text_matches is not None:
            rx = _regex(cond.text_matches)
            matched = [
                n for n in matched
                if rx is not None and rx.search(f"{n.name or ''} {n.value or ''}")
            ]

        if not cond.exists:
            return len(matched) == 0
        return len(matched) >= cond.min_count

    if isinstance(cond, UrlCondition):
        ok, why = ctx.supports("url")
        if not ok:
            raise UnsupportedOperator("url", why, describe(cond))
        rx = _regex(cond.url.matches)
        return bool(rx and rx.search(_url_for(ctx, cond.frame)))

    if isinstance(cond, TextCondition):
        if cond.frame is not None:
            ok, why = ctx.supports("frame")
            if not ok:
                raise UnsupportedOperator("frame", why, describe(cond))
        rx = _regex(cond.text_present)
        if rx is None:
            return False
        haystack = " \n".join(
            f"{n.name or ''} {n.value or ''}" for n in _nodes_in_frame(ctx, cond.frame) if n.visible
        )
        return bool(rx.search(haystack))

    if isinstance(cond, AllCondition):
        return all(evaluate(c, ctx) for c in cond.all)

    if isinstance(cond, AnyCondition):
        # Deliberately not short-circuiting past an unsupported operator: if one
        # branch cannot be evaluated, the disjunction's answer is unknown, and
        # an unknown must surface rather than be masked by a sibling that
        # happened to be true.
        results = [evaluate(c, ctx) for c in cond.any]
        return any(results)

    if isinstance(cond, NotCondition):
        return not evaluate(cond.not_, ctx)

    raise UnsupportedOperator(type(cond).__name__, "unknown condition type", str(cond))


# --------------------------------------------------------------------------
# preflight (R-M3-1, primary path)
# --------------------------------------------------------------------------

@dataclass
class OperatorUse:
    operator: str
    where: str
    condition: str


def operators_used(cond: Condition | None, where: str = "") -> list[OperatorUse]:
    """Every operator a condition tree reaches, for preflight."""
    if cond is None:
        return []
    if isinstance(cond, ElementCondition):
        used = [OperatorUse("element", where, describe(cond))]
        if cond.frame is not None:
            used.append(OperatorUse("frame", where, describe(cond)))
        return used
    if isinstance(cond, UrlCondition):
        return [OperatorUse("url", where, describe(cond))]
    if isinstance(cond, TextCondition):
        used = [OperatorUse("text_present", where, describe(cond))]
        if cond.frame is not None:
            used.append(OperatorUse("frame", where, describe(cond)))
        return used
    if isinstance(cond, AllCondition):
        return [u for c in cond.all for u in operators_used(c, where)]
    if isinstance(cond, AnyCondition):
        return [u for c in cond.any for u in operators_used(c, where)]
    if isinstance(cond, NotCondition):
        return operators_used(cond.not_, where)
    return []


def unsupported(
    conditions: list[tuple[str, Condition | None]], capabilities: SurfaceCapabilities
) -> list[OperatorUse]:
    """Which of these conditions this surface cannot evaluate.

    Called at replay preflight with every condition in the effective artifact
    AND the resolved profile. A non-empty result means the run stops before the
    first action, rather than discovering the problem after an irreversible step.
    """
    ctx = EvalContext(observation=Observation(), capabilities=capabilities)
    problems: list[OperatorUse] = []
    for where, cond in conditions:
        for use in operators_used(cond, where):
            ok, _ = ctx.supports(use.operator)
            if not ok:
                problems.append(use)
    return problems
