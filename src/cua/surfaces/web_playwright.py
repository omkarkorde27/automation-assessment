"""Playwright web surface -- the one implemented adapter.

Playwright is used as a *driver* only: to navigate, to click at a point, to type.
Perception goes through our own injected extractor, and targeting goes through
LocatorBundle resolution. Playwright's own `get_by_role` / CSS selectors are
deliberately not used for recorded steps, because a recorded flow must not
depend on a selector language that only exists in a browser.

Acting on a resolved node happens by its bounding box, not by a selector. That
sounds like a step backwards until you notice it is the only thing a desktop
surface can also do -- and the coordinate is derived fresh from the node we just
resolved semantically, so it is not a recorded coordinate. Resolution stays
semantic; only the final delivery of the click is physical.
"""

from __future__ import annotations

import time
from pathlib import Path

from playwright.async_api import Frame, Page, TimeoutError as PWTimeout

from ..locators.model import LocatorBundle
from ..locators.resolve import LocatorError, resolve_node
from ..perception.model import Anchors, BBox, Observation, UiNode
from .base import (
    Action,
    ActionGuard,
    ActionResult,
    ActionType,
    PolicyDenied,
    SurfaceCapabilities,
    SurfaceKind,
)

_EXTRACTOR = (Path(__file__).parent.parent / "perception" / "extract.js").read_text()


class WebSurface:
    """A live browser page presented as a `Surface`."""

    def __init__(
        self,
        page: Page,
        *,
        guards: list[ActionGuard] | None = None,
        kind: SurfaceKind = SurfaceKind.LEGACY_WEB,
    ) -> None:
        self._page = page
        self._guards = guards or []
        self._kind = kind

    @property
    def page(self) -> Page:
        return self._page

    @property
    def capabilities(self) -> SurfaceCapabilities:
        return SurfaceCapabilities(
            kind=self._kind,
            has_url=True,
            has_frames=True,
            can_navigate=True,
            can_screenshot=True,
            supports_coordinates=True,
        )

    # ---- perception ----------------------------------------------------

    def _frame_path(self, frame: Frame) -> tuple[str, ...]:
        """Names from the top document down. Unnamed frames fall back to a
        positional token so the path is still total."""
        path: list[str] = []
        cur: Frame | None = frame
        while cur is not None and cur.parent_frame is not None:
            name = cur.name
            if not name:
                siblings = cur.parent_frame.child_frames
                name = f"#{siblings.index(cur)}"
            path.append(name)
            cur = cur.parent_frame
        return tuple(reversed(path))

    async def _frame_offset(self, frame: Frame) -> tuple[float, float]:
        """Where this frame sits in the top-level viewport.

        Playwright reports a frame element's box in main-frame coordinates, so
        one lookup covers nesting. Returns (0, 0) for the top document and for a
        frame whose element has no box (detached or display:none).
        """
        if frame.parent_frame is None:
            return (0.0, 0.0)
        try:
            element = await frame.frame_element()
            box = await element.bounding_box()
        except Exception:
            return (0.0, 0.0)
        return (box["x"], box["y"]) if box else (0.0, 0.0)

    async def observe(self) -> Observation:
        nodes: list[UiNode] = []
        frame_paths: list[tuple[str, ...]] = []
        frame_urls: dict[str, str] = {}

        for frame in self._page.frames:
            if frame.is_detached():
                continue
            path = self._frame_path(frame)
            key = "/".join(path)
            try:
                # A frame mid-navigation has a document but no content yet, and
                # extracting from it yields zero nodes -- which downstream looks
                # identical to "the control is gone" and produces a confident,
                # wrong LOCATOR_UNRESOLVED. Waiting for the parse removes that
                # whole class of false negative at the source. Bounded and
                # swallowed: a frame that never settles is simply observed as it
                # is, and the engine's settle loop handles the rest.
                await frame.wait_for_load_state("domcontentloaded", timeout=2000)
            except Exception:
                pass

            try:
                raw = await frame.evaluate(_EXTRACTOR)
            except Exception:
                # A frame that navigates mid-observation is normal, not fatal.
                # Skipping it is correct; the caller re-observes.
                continue

            # getBoundingClientRect() inside a frame is relative to THAT frame's
            # viewport, but the mouse takes page coordinates. Without this offset
            # every click inside a frame lands wherever the frame happens to sit
            # on the page -- silently, and on whatever control is really there.
            offset_x, offset_y = await self._frame_offset(frame)

            frame_paths.append(path)
            frame_urls[key] = raw.get("url", "")

            for item in raw.get("nodes", []):
                b = dict(item["bbox"])
                b["x"] += offset_x
                b["y"] += offset_y
                nodes.append(
                    UiNode(
                        # Namespaced so ids stay unique across frames.
                        node_id=f"{key}:{item['node_id']}" if key else item["node_id"],
                        role=item["role"],
                        name=item.get("name") or "",
                        value=item.get("value"),
                        states=item.get("states") or {},
                        frame_path=path,
                        bbox=BBox(**b),
                        anchors=Anchors(**(item.get("anchors") or {})),
                        tag=item.get("tag", ""),
                    )
                )

        return Observation(
            url=self._page.url,
            title=await self._page.title(),
            nodes=tuple(nodes),
            frame_paths=tuple(frame_paths),
            frame_urls=frame_urls,
        )

    # ---- action --------------------------------------------------------

    def add_guard(self, guard: ActionGuard) -> None:
        self._guards.append(guard)

    def _run_guards(self, action: Action) -> None:
        """The choke point. Every action, no exceptions, before anything moves."""
        for guard in self._guards:
            guard.check(action, self)

    async def act(self, action: Action) -> ActionResult:
        started = time.monotonic()

        def done(ok: bool, **kw) -> ActionResult:
            return ActionResult(
                ok=ok, action=action,
                duration_ms=int((time.monotonic() - started) * 1000), **kw
            )

        try:
            self._run_guards(action)
        except PolicyDenied as exc:
            return done(False, error=exc.message,
                        error_detail={"failure_class": PolicyDenied.failure_class})

        try:
            if action.type is ActionType.NAVIGATE:
                if not action.url:
                    return done(False, error="navigate requires a url")
                await self._page.goto(action.url, timeout=action.timeout_ms,
                                      wait_until="domcontentloaded")
                return done(True)

            if action.type is ActionType.WAIT:
                await self._page.wait_for_timeout(action.timeout_ms)
                return done(True)

            if action.type is ActionType.PRESS_KEY:
                if not action.key:
                    return done(False, error="press_key requires a key")
                await self._page.keyboard.press(action.key)
                return done(True)

            if action.type is ActionType.SCROLL:
                await self._page.mouse.wheel(0, int(action.value or 400))
                return done(True)

            # Everything below needs a resolved target.
            if action.target is None:
                return done(False, error=f"{action.type.value} requires a target")

            observation = await self.observe()
            node, resolved = resolve_node(action.target, observation)
            frame = self._frame_for(node.frame_path)
            if frame is None:
                return done(False, error=f"frame {node.frame_path} vanished before acting",
                            error_detail={"failure_class": "LOCATOR_UNRESOLVED"})

            if not node.enabled:
                return done(False, resolved=resolved,
                            error=f"target is disabled: {node.describe()}",
                            error_detail={"failure_class": "PRECONDITION_FAILED"})

            await self._deliver(action, node, frame)
            return done(True, resolved=resolved)

        except LocatorError as exc:
            return done(False, error=exc.message, error_detail=exc.as_detail())
        except PWTimeout as exc:
            return done(False, error=f"timed out: {exc}",
                        error_detail={"failure_class": "TIMEOUT"})
        except Exception as exc:  # surface-level fault, not a flow outcome
            return done(False, error=f"{type(exc).__name__}: {exc}",
                        error_detail={"failure_class": "SURFACE_ERROR"})

    async def _deliver(self, action: Action, node: UiNode, frame: Frame) -> None:
        """Physically perform the action on an already-resolved node."""
        cx, cy = node.bbox.center

        if action.type is ActionType.CLICK:
            await self._page.mouse.click(cx, cy)
            return

        if action.type is ActionType.FILL:
            # Focus by clicking the resolved coordinate, then clear and type --
            # the sequence a person performs, and the one a desktop surface can
            # also perform. No selector is involved.
            await self._page.mouse.click(cx, cy)
            await self._page.keyboard.press("ControlOrMeta+a")
            await self._page.keyboard.press("Delete")
            if action.value:
                await self._page.keyboard.type(action.value)
            return

        if action.type is ActionType.SELECT_OPTION:
            # `cx, cy` are page-absolute so the mouse can use them, but
            # elementFromPoint runs INSIDE the frame and takes frame-relative
            # coordinates. Handing it the page coordinate finds whatever happens
            # to sit at that offset within the frame -- the same mismatch that
            # made every framed click land on the wrong control, surfacing here
            # as "Element is not a <select> element".
            offset_x, offset_y = await self._frame_offset(frame)
            handle = await frame.evaluate_handle(
                "([x, y]) => document.elementFromPoint(x, y)",
                [cx - offset_x, cy - offset_y],
            )
            element = handle.as_element()
            if element is None:
                raise RuntimeError("no element at the resolved point")

            # Legacy dropdowns almost always carry a code as the option value
            # and a human label as the text ("HSA" vs "HSA - Health Savings").
            # A capability parameterized on the code must match the code, but a
            # flow recorded from what a person saw will carry the label, so both
            # are tried. Short timeouts: a miss here should fall through to the
            # next attempt, not sit in Playwright's 30s retry loop.
            for by in ("value", "label"):
                try:
                    await element.select_option(**{by: action.value}, timeout=2000)
                    return
                except PWTimeout:
                    continue
            raise RuntimeError(
                f"no option matching {action.value!r} by value or by label"
            )

        raise RuntimeError(f"unsupported action: {action.type}")

    def _frame_for(self, path: tuple[str, ...]) -> Frame | None:
        for frame in self._page.frames:
            if not frame.is_detached() and self._frame_path(frame) == path:
                return frame
        return None

    # ---- evidence ------------------------------------------------------

    async def screenshot(self, *, full_page: bool = False) -> bytes:
        return await self._page.screenshot(full_page=full_page)

    async def current_url(self) -> str:
        return self._page.url

    async def resolve_only(self, bundle: LocatorBundle) -> tuple[UiNode, object]:
        """Resolve without acting. Used by checkpoints and extractions."""
        return resolve_node(bundle, await self.observe())
