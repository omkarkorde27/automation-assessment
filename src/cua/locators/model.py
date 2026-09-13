"""LocatorBundle -- how a recorded flow names a control.

A bundle is an ORDERED SET of independent strategies plus a match policy, not a
single selector. That shape is the answer to the environment the brief
describes: enterprise UIs are stable but hostile, so the failure mode is not
"the page changed", it is "the one clever selector we recorded was never durable
in the first place".

Ordering strongest-to-weakest lets replay report *which* strategy resolved. A
target that starts resolving by candidate 3 instead of candidate 1 is drifting
long before it breaks, and that signal is free.

The model never writes any of this. It picks a numbered node out of an
observation; the recorder derives the bundle from that node's descriptor. Locator
robustness is therefore a property of the system, not of model output quality.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

Role = str
Relation = Literal[
    "same_row", "same_column", "same_cell", "same_section", "same_fieldset",
    "following", "preceding", "nearest",
]


class Anchor(BaseModel):
    """A piece of visible text used to locate something near it."""

    model_config = ConfigDict(frozen=True)

    text: str | None = None
    pattern: str | None = None
    role: Role | None = None

    @model_validator(mode="after")
    def _one_of(self) -> "Anchor":
        if not self.text and not self.pattern:
            raise ValueError("anchor needs text or pattern")
        return self


class SectionScope(BaseModel):
    """Narrows a search to a region, so weak strategies stay usable.

    An ordinal is meaningless document-wide but perfectly stable inside a named
    panel -- which is why RoleOrdinal requires one of these.
    """

    model_config = ConfigDict(frozen=True)

    anchor: Anchor
    relation: Literal["within_section", "within_row", "within_table", "within_dialog"]


class FrameRef(BaseModel):
    """One hop of a frame path, resolved outside-in.

    `index` is a last resort and is journaled as degraded when used: frame order
    is far less stable than frame naming.
    """

    model_config = ConfigDict(frozen=True)

    name: str | None = None
    url_pattern: str | None = None
    index: int | None = None


# --------------------------------------------------------------------------
# candidates, strongest first
# --------------------------------------------------------------------------

class RoleNameExact(BaseModel):
    """Role + exact accessible name. Survives markup rewrites and id churn."""

    model_config = ConfigDict(frozen=True)
    strategy: Literal["role_name_exact"] = "role_name_exact"
    role: Role
    name: str
    scope: SectionScope | None = None


class RoleNamePattern(BaseModel):
    """Role + name regex. Survives branding, casing, and per-tenant wording."""

    model_config = ConfigDict(frozen=True)
    strategy: Literal["role_name_pattern"] = "role_name_pattern"
    role: Role
    name_pattern: str
    scope: SectionScope | None = None


class LabelFor(BaseModel):
    """Control named by an associated <label>. Survives table-layout forms."""

    model_config = ConfigDict(frozen=True)
    strategy: Literal["label_for"] = "label_for"
    control_role: Role
    label_text: str | None = None
    label_pattern: str | None = None

    @model_validator(mode="after")
    def _one_of(self) -> "LabelFor":
        if not self.label_text and not self.label_pattern:
            raise ValueError("label_for needs label_text or label_pattern")
        return self


class AnchorRelative(BaseModel):
    """The legacy-table workhorse.

    "The textbox in the same row as the cell reading 'Member ID'". This is the
    only strategy that finds a control with no accessible name, which in these
    applications is most of them.

    `scope` composes with `relation` to express the 2-D lookup every grid read
    needs: relation="same_column" anchored on "Balance", scoped within_row to
    the row containing "Savings", is literally "the Balance cell of the Savings
    row" -- the way a person reads it, and stable against row reordering, which
    an ordinal is not.
    """

    model_config = ConfigDict(frozen=True)
    strategy: Literal["anchor_relative"] = "anchor_relative"
    anchor: Anchor
    relation: Relation
    target_role: Role
    occurrence: int = 0
    scope: SectionScope | None = None


class RoleOrdinal(BaseModel):
    """Nth control of a role within a scope. Structural last resort."""

    model_config = ConfigDict(frozen=True)
    strategy: Literal["role_ordinal"] = "role_ordinal"
    role: Role
    ordinal: int
    scope: SectionScope
    """Required. A document-wide bare ordinal is never recorded -- it would
    silently retarget the moment anything is added above it on the page."""


class BBoxNormalized(BaseModel):
    """Normalized coordinates. Gated behind `allow_unstable` and never trusted.

    Kept because a desktop or screenshot-only surface may genuinely have nothing
    better, and because an explicitly-flagged weak locator is safer than a
    confident-looking one that is equally fragile.
    """

    model_config = ConfigDict(frozen=True)
    strategy: Literal["bbox_normalized"] = "bbox_normalized"
    x: float
    y: float
    w: float
    h: float
    unstable: Literal[True] = True


LocatorCandidate = Annotated[
    Union[RoleNameExact, RoleNamePattern, LabelFor, AnchorRelative, RoleOrdinal, BBoxNormalized],
    Field(discriminator="strategy"),
]

# Prior stability weight per strategy. Feeds the recorded stability score and
# the resolution score, so degradation is comparable across targets.
STRATEGY_WEIGHT: dict[str, float] = {
    "role_name_exact": 1.00,
    "role_name_pattern": 0.90,
    "label_for": 0.85,
    "anchor_relative": 0.80,
    "role_ordinal": 0.50,
    "bbox_normalized": 0.20,
}


class RecordedNode(BaseModel):
    """What the target looked like when it was recorded.

    Used for human review and for diffing drift. Carries `value_shape` -- a
    pattern -- and never a value, because artifacts are reviewed, copied between
    environments, and committed to git.
    """

    model_config = ConfigDict(frozen=True)

    role: Role
    name: str = ""
    value_shape: str | None = None
    states: dict[str, bool] = Field(default_factory=dict)
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    anchors: dict[str, str] = Field(default_factory=dict)


class LocatorBundle(BaseModel):
    model_config = ConfigDict(frozen=True)

    target_id: str
    """Stable address within the artifact. This is what a tenant override keys
    on, so a per-tenant fix replaces one whole bundle and stays reviewable."""

    frame_path: tuple[FrameRef, ...] = ()
    candidates: tuple[LocatorCandidate, ...] = Field(min_length=1)
    match_policy: Literal["unique_required", "best_scored"] = "unique_required"

    allow_unstable: bool = False
    """Gates the coordinate candidate. Off unless the recorder found nothing
    better, so a weak locator is always a deliberate, visible decision."""

    notes: str
    """REQUIRED. Why this bundle is expected to keep resolving: what the primary
    candidate keys on, what the fallbacks cover, and what is known to be
    unstable about the control.

    Not optional, because a locator without stated reasoning cannot be reviewed
    -- a reviewer deciding whether a capability may run unattended against a
    bank's back office has no way to judge "role=textbox, name='Member ID'"
    without knowing whether that name is stable, branded, or per-tenant. This is
    the brief's "how each target element is identified, with your reasoning
    about robustness", and making it defaultable quietly removed it."""

    recorded: RecordedNode | None = None
    """Optional, because not every bundle comes from a recording.

    The login recipe and the interstitial-dismiss bundles are hand-authored in
    the app profile -- they were never observed by the recorder, so there is no
    record-time snapshot to carry. Requiring one would force an author to
    fabricate a node, which is worse than admitting there isn't one. Populated
    for every recorder-produced bundle."""

    stability_score: float = 0.0
    """Optional for the same reason: it is strategy weight x uniqueness margin
    measured AT RECORD TIME, which a hand-authored bundle has no value for.
    0.0 means "not measured", not "measured as fragile" -- read it alongside
    `recorded`."""

    @model_validator(mode="after")
    def _policy_needs_justification(self) -> "LocatorBundle":
        if self.match_policy == "best_scored" and not self.notes.strip():
            raise ValueError(
                "match_policy='best_scored' accepts an ambiguous target and must "
                "carry a justification in `notes`"
            )
        return self

    def primary(self) -> LocatorCandidate:
        return self.candidates[0]

    def describe(self) -> str:
        head = self.candidates[0]
        return f"{self.target_id} via {head.strategy}"
