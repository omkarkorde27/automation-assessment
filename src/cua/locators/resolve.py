"""Locator resolution: bundle + observation -> exactly one node, or an error.

Two rules carry the whole design:

  1. **Unique match or nothing.** A candidate that matches several nodes is not
     resolved by taking the first one. It is refined using the remaining
     candidates as filters, and if it is still ambiguous the resolution FAILS
     and the run escalates to a human. Positional tie-breaking is how automation
     silently operates on the wrong customer's record, which in this domain is
     the failure that actually matters.

  2. **Report how it resolved.** Every resolution records which candidate won.
     A target that starts resolving by candidate 3 instead of candidate 1 is
     drifting, and that telemetry is what surfaces per-tenant drift before
     anything breaks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..perception.model import Observation, UiNode, normalize_name
from .model import (
    STRATEGY_WEIGHT,
    Anchor,
    AnchorRelative,
    BBoxNormalized,
    FrameRef,
    LabelFor,
    LocatorBundle,
    LocatorCandidate,
    RoleNameExact,
    RoleNamePattern,
    RoleOrdinal,
    SectionScope,
)

BBOX_TOLERANCE = 0.04  # fraction of the frame viewport


@dataclass(frozen=True)
class ResolvedLocator:
    node_id: str
    strategy: str
    candidate_index: int
    score: float
    competitor_count: int
    degraded: bool
    """True when the primary candidate did not win. Not an error -- a signal."""

    disambiguated_by: tuple[str, ...] = ()

    def describe(self) -> str:
        via = f"{self.strategy}[{self.candidate_index}]"
        if self.disambiguated_by:
            via += f" +filters({', '.join(self.disambiguated_by)})"
        return f"{self.node_id} via {via} score={self.score:.2f}"


class LocatorError(Exception):
    """Base for resolution failures. Carries structure for the result contract."""

    failure_class = "LOCATOR_UNRESOLVED"

    def __init__(self, message: str, *, target_id: str, tried: list[str] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.target_id = target_id
        self.tried = tried or []

    def as_detail(self) -> dict:
        return {
            "failure_class": self.failure_class,
            "target_id": self.target_id,
            "message": self.message,
            "candidates_tried": self.tried,
        }


class LocatorUnresolved(LocatorError):
    failure_class = "LOCATOR_UNRESOLVED"


class LocatorAmbiguous(LocatorError):
    """Several nodes matched and no candidate could separate them.

    Deliberately an error rather than a guess.
    """

    failure_class = "LOCATOR_AMBIGUOUS"

    def __init__(self, message: str, *, target_id: str, tried: list[str] | None = None,
                 matches: list[str] | None = None) -> None:
        super().__init__(message, target_id=target_id, tried=tried)
        self.matches = matches or []

    def as_detail(self) -> dict:
        d = super().as_detail()
        d["ambiguous_matches"] = self.matches
        return d


class FrameUnresolved(LocatorUnresolved):
    """The frame path itself did not resolve.

    Never falls back to the main document: silently acting on the wrong frame is
    worse than failing.
    """


# --------------------------------------------------------------------------
# text + scope matching
# --------------------------------------------------------------------------

def _text_matches(actual: str, anchor: Anchor) -> bool:
    if anchor.pattern:
        try:
            return bool(re.search(anchor.pattern, actual or ""))
        except re.error:
            return False
    return normalize_name(actual) == normalize_name(anchor.text)


def _row_text_matches(row_text: str, anchor: Anchor) -> bool:
    """Row contents match: containment, since a row holds more than the anchor."""
    if anchor.pattern:
        try:
            return bool(re.search(anchor.pattern, row_text or ""))
        except re.error:
            return False
    return normalize_name(anchor.text) in normalize_name(row_text)


def _in_scope(node: UiNode, scope: SectionScope | None) -> bool:
    if scope is None:
        return True
    a = node.anchors
    match scope.relation:
        case "within_section" | "within_table":
            # `within_table` uses the section label too: in these layouts a grid
            # is identified by its panel header, not by a table caption.
            return _text_matches(a.section_label, scope.anchor)
        case "within_row":
            # A form row is identified by its label cell; a DATA row has no
            # label and is identified by its contents ("the row containing
            # Savings"). Both are legitimate, so both are accepted.
            return _text_matches(a.row_label, scope.anchor) or _row_text_matches(a.row_text, scope.anchor)
        case "within_dialog":
            return _text_matches(a.dialog_label, scope.anchor)
    return False


def frame_path_matches(path: tuple[str, ...], refs: tuple[FrameRef, ...], obs: Observation) -> bool:
    """Outside-in frame path match. Empty refs mean the top-level document.

    Public because the condition evaluator needs the same semantics -- a
    checkpoint scoped to the content frame and a locator scoped to the content
    frame must agree on what "the content frame" means.
    """
    if len(path) != len(refs):
        return False
    for depth, (segment, ref) in enumerate(zip(path, refs)):
        if ref.name is not None:
            if segment != ref.name:
                return False
        elif ref.url_pattern is not None:
            url = obs.frame_urls.get("/".join(path[: depth + 1]), "")
            if not re.search(ref.url_pattern, url):
                return False
        elif ref.index is not None:
            known = [p for p in obs.frame_paths if len(p) == depth + 1]
            if not (0 <= ref.index < len(known)) or known[ref.index][depth] != segment:
                return False
        else:
            return False
    return True


def _frame_matches(node: UiNode, refs: tuple[FrameRef, ...], obs: Observation) -> bool:
    return frame_path_matches(node.frame_path, refs, obs)


# --------------------------------------------------------------------------
# candidate matching
# --------------------------------------------------------------------------

def _anchor_nodes(pool: list[UiNode], anchor: Anchor) -> list[UiNode]:
    return [
        n for n in pool
        if (anchor.role is None or n.role == anchor.role) and _text_matches(n.name, anchor)
    ]


def _matches(cand: LocatorCandidate, pool: list[UiNode]) -> list[UiNode]:
    """Nodes in `pool` satisfying `cand`. `pool` is already frame-filtered and
    in document order, which is what makes ordinals and following/preceding
    well-defined."""

    if isinstance(cand, RoleNameExact):
        target = normalize_name(cand.name)
        return [n for n in pool
                if n.role == cand.role and n.norm_name == target and _in_scope(n, cand.scope)]

    if isinstance(cand, RoleNamePattern):
        try:
            rx = re.compile(cand.name_pattern)
        except re.error:
            return []
        return [n for n in pool
                if n.role == cand.role and rx.search(n.name or "") and _in_scope(n, cand.scope)]

    if isinstance(cand, LabelFor):
        anchor = Anchor(text=cand.label_text, pattern=cand.label_pattern)
        return [n for n in pool
                if n.role == cand.control_role and _text_matches(n.anchors.label, anchor)]

    if isinstance(cand, AnchorRelative):
        return _match_anchor_relative(cand, pool)

    if isinstance(cand, RoleOrdinal):
        scoped = [n for n in pool if n.role == cand.role and _in_scope(n, cand.scope)]
        return [scoped[cand.ordinal]] if 0 <= cand.ordinal < len(scoped) else []

    if isinstance(cand, BBoxNormalized):
        cx, cy = cand.x + cand.w / 2, cand.y + cand.h / 2
        near = [
            n for n in pool
            if abs(n.bbox.ncenter[0] - cx) <= BBOX_TOLERANCE
            and abs(n.bbox.ncenter[1] - cy) <= BBOX_TOLERANCE
        ]
        return near

    return []


def _match_anchor_relative(cand: AnchorRelative, pool: list[UiNode]) -> list[UiNode]:
    # `scope` narrows first, so relation and scope compose into a 2-D lookup.
    role_pool = [n for n in pool if n.role == cand.target_role and _in_scope(n, cand.scope)]

    # Structural relations read straight off the recorded anchors -- these are
    # the ones that rescue unnamed controls in table layouts.
    if cand.relation in ("same_row", "same_column", "same_section", "same_fieldset", "same_cell"):
        field = {
            "same_row": lambda n: n.anchors.row_label,
            "same_column": lambda n: n.anchors.col_header,
            "same_section": lambda n: n.anchors.section_label,
            "same_fieldset": lambda n: n.anchors.section_label,
            # "same cell" = the anchor text sits immediately before the control
            # inside the same container.
            "same_cell": lambda n: n.anchors.preceding_text,
        }[cand.relation]
        found = [n for n in role_pool if _text_matches(field(n), cand.anchor)]

    # Positional relations are computed from document order and geometry, so
    # they work even when the anchor shares no structural container.
    else:
        anchors = _anchor_nodes(pool, cand.anchor)
        if not anchors:
            return []
        order = {n.node_id: i for i, n in enumerate(pool)}
        found = []
        for a in anchors:
            ai = order[a.node_id]
            if cand.relation == "following":
                after = [n for n in role_pool if order[n.node_id] > ai]
                if after:
                    found.append(after[0])
            elif cand.relation == "preceding":
                before = [n for n in role_pool if order[n.node_id] < ai]
                if before:
                    found.append(before[-1])
            elif cand.relation == "nearest":
                if role_pool:
                    ax, ay = a.bbox.center
                    found.append(
                        min(role_pool, key=lambda n: (n.bbox.center[0] - ax) ** 2
                            + (n.bbox.center[1] - ay) ** 2)
                    )
        # de-duplicate, preserving document order
        seen: set[str] = set()
        found = [n for n in found if not (n.node_id in seen or seen.add(n.node_id))]

    if cand.occurrence:
        return [found[cand.occurrence]] if 0 <= cand.occurrence < len(found) else []
    return found


# --------------------------------------------------------------------------
# resolution
# --------------------------------------------------------------------------

def resolve(bundle: LocatorBundle, observation: Observation) -> ResolvedLocator:
    """Resolve to exactly one node, or raise.

    Walks candidates strongest-first. A multi-match is refined using the
    remaining candidates as filters; if refinement cannot reach one node, this
    raises LocatorAmbiguous rather than picking.
    """
    pool = [n for n in observation.nodes if _frame_matches(n, bundle.frame_path, observation)]
    if not pool:
        wanted = "/".join(r.name or r.url_pattern or str(r.index) for r in bundle.frame_path) or "(top)"
        raise FrameUnresolved(
            f"no nodes in frame path {wanted}; observed frames: "
            f"{[list(p) for p in observation.frame_paths]}",
            target_id=bundle.target_id,
        )

    tried: list[str] = []

    for idx, cand in enumerate(bundle.candidates):
        if isinstance(cand, BBoxNormalized) and not bundle.allow_unstable:
            tried.append(f"{cand.strategy}(skipped: allow_unstable=False)")
            continue

        found = _matches(cand, pool)
        tried.append(f"{cand.strategy}({len(found)} match)")

        if len(found) == 1:
            return ResolvedLocator(
                node_id=found[0].node_id,
                strategy=cand.strategy,
                candidate_index=idx,
                score=STRATEGY_WEIGHT.get(cand.strategy, 0.5),
                competitor_count=0,
                degraded=idx > 0,
            )

        if len(found) == 0:
            continue

        # Ambiguous: narrow using the remaining candidates as filters.
        narrowed, filters = _disambiguate(found, bundle.candidates[idx + 1:], bundle.allow_unstable)

        if len(narrowed) == 1:
            return ResolvedLocator(
                node_id=narrowed[0].node_id,
                strategy=cand.strategy,
                candidate_index=idx,
                score=max(0.0, STRATEGY_WEIGHT.get(cand.strategy, 0.5) - 0.05 * len(filters)),
                competitor_count=len(found) - 1,
                degraded=idx > 0 or bool(filters),
                disambiguated_by=tuple(filters),
            )

        if bundle.match_policy == "best_scored":
            # Opt-in, and the schema requires a written justification for it.
            return ResolvedLocator(
                node_id=narrowed[0].node_id,
                strategy=cand.strategy,
                candidate_index=idx,
                score=STRATEGY_WEIGHT.get(cand.strategy, 0.5) * 0.5,
                competitor_count=len(narrowed) - 1,
                degraded=True,
                disambiguated_by=tuple(filters),
            )

        raise LocatorAmbiguous(
            f"{len(narrowed)} nodes matched '{bundle.target_id}' via {cand.strategy} and no "
            f"further candidate could separate them; refusing to guess",
            target_id=bundle.target_id,
            tried=tried,
            matches=[n.describe() for n in narrowed[:6]],
        )

    raise LocatorUnresolved(
        f"no candidate resolved '{bundle.target_id}' among {len(pool)} nodes in frame",
        target_id=bundle.target_id,
        tried=tried,
    )


def _disambiguate(
    matches: list[UiNode], rest: tuple[LocatorCandidate, ...], allow_unstable: bool
) -> tuple[list[UiNode], list[str]]:
    """Intersect an ambiguous match set with later candidates.

    A filter that empties the set is discarded rather than applied -- it is
    evidence the filter is wrong, not that the target is gone.
    """
    current = matches
    used: list[str] = []
    for cand in rest:
        if len(current) == 1:
            break
        if isinstance(cand, BBoxNormalized) and not allow_unstable:
            continue
        refined = [n for n in _matches(cand, current)]
        if refined and len(refined) < len(current):
            current = refined
            used.append(cand.strategy)
    return current, used


def resolve_node(bundle: LocatorBundle, observation: Observation) -> tuple[UiNode, ResolvedLocator]:
    res = resolve(bundle, observation)
    node = observation.by_id(res.node_id)
    if node is None:  # pragma: no cover - resolve only returns ids from the pool
        raise LocatorUnresolved("resolved node vanished from observation",
                                target_id=bundle.target_id)
    return node, res
