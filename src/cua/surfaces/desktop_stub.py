"""Desktop surface -- the seam, deliberately unimplemented.

This file exists to be read. The claim in the write-up is that extending to a
native desktop application is a new adapter and nothing else: no change to the
artifact schema, the locator model, the condition DSL, the replay engine, the
policy layer, or the operator console. The only way to make that claim checkable
without building it is to show exactly what the adapter would have to provide.

Every method below is a real mapping onto a platform accessibility API, not a
placeholder:

    UiNode.role        <- UIA ControlType / AX AXRole
    UiNode.name        <- UIA Name / AX AXTitle or AXDescription
    UiNode.value       <- UIA ValuePattern / AX AXValue
    UiNode.states      <- IsEnabled, IsOffscreen, ToggleState / AXEnabled...
    UiNode.bbox        <- BoundingRectangle / AXPosition + AXSize
    UiNode.frame_path  <- window / pane hierarchy
    anchors.row_label  <- GridItemPattern row header
    anchors.col_header <- GridItemPattern column header
    anchors.section_label <- enclosing group / pane Name

Locator candidates 1-5 all survive that mapping; only `bbox_normalized` changes
meaning (screen-relative rather than viewport-relative), and only URL-based
conditions have no analogue -- which is why `SurfaceCapabilities.has_url` is
declared per surface rather than assumed.

What genuinely differs, and would need design work rather than plumbing:
  * no DOM-equivalent "load" event, so waits must poll for state instead;
  * no cheap full-tree snapshot on large apps -- observation would need scoping
    to the focused window;
  * modal handling is OS-level, so the interstitial recovery pattern moves from
    "dismiss a dialog element" to "dismiss a window".
"""

from __future__ import annotations

from ..perception.model import Observation
from .base import Action, ActionResult, SurfaceCapabilities, SurfaceKind

_WHY = (
    "Desktop support is a documented cut, not an oversight. The seam is this "
    "protocol; implementing it means binding pywinauto/UIAutomation (Windows) "
    "or pyobjc/AXUIElement (macOS) to the UiNode mapping in this module's "
    "docstring. Nothing above the Surface boundary changes."
)


class DesktopSurface:
    """Satisfies `Surface` structurally. Every call raises with the reason."""

    def __init__(self, app_ref: str) -> None:
        self._guards: list = []
        self._app_ref = app_ref

    @property
    def capabilities(self) -> SurfaceCapabilities:
        # Truthful even unimplemented: a desktop app has windows, not frames,
        # and no URL. Conditions written against a URL cannot be evaluated here,
        # and the engine must refuse rather than silently pass them.
        return SurfaceCapabilities(
            kind=SurfaceKind.DESKTOP,
            has_url=False,
            has_frames=False,
            can_navigate=False,
            can_screenshot=True,
            supports_coordinates=True,
        )

    def add_guard(self, guard) -> None:
        """Guards attach the same way on every surface -- that is the point of
        the protocol. A desktop adapter would run them in `act()` exactly as the
        web one does."""
        self._guards.append(guard)

    async def observe(self) -> Observation:
        raise NotImplementedError(_WHY)

    async def act(self, action: Action) -> ActionResult:
        raise NotImplementedError(_WHY)

    async def screenshot(self, *, full_page: bool = False) -> bytes:
        raise NotImplementedError(_WHY)

    async def current_url(self) -> str:
        raise NotImplementedError(
            "a desktop surface has no URL; check capabilities.has_url first"
        )
