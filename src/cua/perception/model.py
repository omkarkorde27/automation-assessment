"""Surface-neutral perception model.

`UiNode` is the vocabulary everything downstream speaks: locators, the condition
DSL, checkpoints, the replay engine, the operator console. It is deliberately
the *intersection* of what a browser accessibility tree and a desktop
accessibility API (Windows UIAutomation, macOS AX) can both report -- role,
name, value, states, bounds, and structural context.

That intersection is the whole heterogeneity argument. A desktop adapter emits
the same `UiNode`s, so nothing above this layer changes. Anything web-specific
that leaked into this model would quietly make the desktop story a lie, which is
why `tag` is documentation-only and there is no selector, no XPath, and no
element handle anywhere in here.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field

_WS = re.compile(r"\s+")
_TRAILING = re.compile(r"[:：*\s]+$")


def normalize_name(raw: str | None) -> str:
    """Canonical form for comparing accessible names.

    Unicode-normalize, collapse whitespace, drop trailing colons/asterisks that
    labels pick up ("Member ID:" vs "Member ID"), casefold.

    Interior punctuation is preserved on purpose: "Account Holder #" must stay
    distinguishable from "Account Holder", because on a different tenant those
    are different fields.
    """
    if not raw:
        return ""
    s = unicodedata.normalize("NFKC", raw)
    s = _WS.sub(" ", s).strip()
    s = _TRAILING.sub("", s)
    return s.casefold()


class BBox(BaseModel):
    """Bounds in two coordinate systems, deliberately.

    `x/y/w/h` are **page-absolute** viewport pixels -- what you actually click,
    already offset by the position of the containing frame.

    `nx/ny/nw/nh` are **frame-relative** fractions -- what gets recorded for the
    last-resort coordinate candidate and for redaction masking. Frame-relative
    is the right frame of reference to persist: a control's position within its
    own pane is far more stable than its position on a page whose surrounding
    chrome may differ per tenant.

    Normalized coordinates are never a primary targeting strategy either way:
    the same control lands at different pixels under a different window size or
    zoom level.
    """

    model_config = ConfigDict(frozen=True)

    x: float
    y: float
    w: float
    h: float
    nx: float = 0.0
    ny: float = 0.0
    nw: float = 0.0
    nh: float = 0.0

    @property
    def center(self) -> tuple[float, float]:
        return (self.x + self.w / 2, self.y + self.h / 2)

    @property
    def ncenter(self) -> tuple[float, float]:
        return (self.nx + self.nw / 2, self.ny + self.nh / 2)


class Anchors(BaseModel):
    """Structural context -- what a human uses to identify a control.

    This is what carries locating through legacy markup. When a control has no
    accessible name (no label/for, no aria-label), "the textbox in the row whose
    first cell reads 'Member ID'" is still unambiguous, and it survives the id
    churn and markup rewrites that break selectors.

    Every field has a desktop analogue: table rows, column headers, group
    labels, and dialog titles all exist in UIAutomation and AX.
    """

    model_config = ConfigDict(frozen=True)

    label: str = ""            # <label for> or ancestor <label>
    row_label: str = ""        # first non-empty cell of the containing row
    row_text: str = ""         # all text in the containing row (identifies data rows)
    col_header: str = ""       # header cell at this column index
    section_label: str = ""    # heading-like first child of an ancestor container
    nearest_heading: str = ""  # nearest preceding semantic heading
    preceding_text: str = ""   # nearest preceding text within the same parent
    dialog_label: str = ""     # title of the nearest ancestor dialog, if any


class UiNode(BaseModel):
    model_config = ConfigDict(frozen=True)

    node_id: str
    """Stable within ONE observation only. Never recorded into an artifact --
    the recorder converts a chosen node into a LocatorBundle instead."""

    role: str
    name: str
    value: str | None = None
    states: dict[str, bool] = Field(default_factory=dict)
    frame_path: tuple[str, ...] = ()
    bbox: BBox
    anchors: Anchors = Field(default_factory=Anchors)
    tag: str = ""
    """Source tag, for debugging and evidence only. Never used for locating --
    a desktop surface has no tags."""

    sensitive: bool = False
    """Set by the redaction layer when the app profile classifies this node as
    regulated. Sensitive nodes keep their role/name but never their value."""

    @property
    def norm_name(self) -> str:
        return normalize_name(self.name)

    @property
    def visible(self) -> bool:
        return self.states.get("visible", True)

    @property
    def enabled(self) -> bool:
        return self.states.get("enabled", True)

    def describe(self) -> str:
        """One-line human/model-readable form used in prompts and journals."""
        bits = [f"{self.role}"]
        if self.name:
            bits.append(f'"{self.name}"')
        elif self.anchors.row_label:
            bits.append(f"(in row {self.anchors.row_label!r})")
        elif self.anchors.label:
            bits.append(f"(labelled {self.anchors.label!r})")
        if self.value and not self.sensitive:
            bits.append(f"= {self.value!r}")
        elif self.value and self.sensitive:
            bits.append("= <redacted>")
        if not self.enabled:
            bits.append("[disabled]")
        return " ".join(bits)


class Observation(BaseModel):
    """One perception of the surface at a point in time."""

    model_config = ConfigDict(frozen=True)

    url: str = ""
    title: str = ""
    nodes: tuple[UiNode, ...] = ()
    frame_paths: tuple[tuple[str, ...], ...] = ()
    frame_urls: dict[str, str] = Field(default_factory=dict)
    """Joined frame path ("" for the top document) -> that frame's URL. Lets a
    frame be matched by URL pattern when frames are unnamed, which is common in
    generated legacy framesets."""

    captured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def in_frame(self, frame_path: tuple[str, ...]) -> list[UiNode]:
        return [n for n in self.nodes if n.frame_path == frame_path]

    def by_id(self, node_id: str) -> UiNode | None:
        return next((n for n in self.nodes if n.node_id == node_id), None)

    def by_role(self, role: str) -> list[UiNode]:
        return [n for n in self.nodes if n.role == role]

    def state_hash(self) -> str:
        """Fingerprint of *what is on screen*, ignoring values and positions.

        Two uses, both structural: detecting a stuck agent (the same hash three
        times means no progress, regardless of what it typed), and detecting
        per-tenant surface drift against a recorded canary.
        """
        parts = sorted(f"{n.role}|{n.norm_name}" for n in self.nodes if n.visible)
        return "sha256:" + hashlib.sha256("\n".join(parts).encode()).hexdigest()[:32]

    def summarize(self) -> str:
        return f"{len(self.nodes)} nodes across {len(self.frame_paths)} frame(s) at {self.url}"
