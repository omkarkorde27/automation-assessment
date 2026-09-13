"""The Surface seam.

This is the boundary between *how we perceive and act on a surface* and *the
recorded flow*. Everything above it -- artifacts, locators, the condition DSL,
replay, policy, escalation -- is written against this protocol and knows nothing
about Playwright, the DOM, or a browser.

That is what makes the heterogeneity story in the brief real rather than
aspirational: a legacy web app is the same adapter, and a desktop app is a new
adapter emitting the same `UiNode`s. See `desktop_stub.py`, which implements the
protocol without a body so the seam can be inspected instead of described.

`act()` is also the single choke point for policy. Allowlist checks, risk-tier
gating, and the control lease all run here, in `_run_guards`, so an action that
policy forbids cannot be performed by any caller -- the LLM, the replay engine,
or the operator console. A guardrail expressed as a prompt instruction is not a
guardrail. Guards land with M5/M6; the hook exists from the start so there is
exactly one place they can go.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

from ..locators.model import LocatorBundle
from ..locators.resolve import ResolvedLocator
from ..perception.model import Observation


class SurfaceKind(str, Enum):
    WEB = "web"
    LEGACY_WEB = "legacy_web"
    DESKTOP = "desktop"


@dataclass(frozen=True)
class SurfaceCapabilities:
    """What this surface can actually do.

    Declared rather than assumed, so a flow can degrade honestly: a desktop
    surface has no URL, which means URL-based conditions are unavailable there
    and the condition evaluator must say so instead of silently passing.
    """

    kind: SurfaceKind
    has_url: bool = True
    has_frames: bool = True
    can_navigate: bool = True
    can_screenshot: bool = True
    supports_coordinates: bool = True


class ActionType(str, Enum):
    CLICK = "click"
    FILL = "fill"
    SELECT_OPTION = "select_option"
    PRESS_KEY = "press_key"
    NAVIGATE = "navigate"
    SCROLL = "scroll"
    WAIT = "wait"


# Risk classification lives with the action vocabulary because it is a property
# of what the action DOES, not of who asked for it. M6 enforces it; recording it
# from the start keeps the taxonomy in one place.
class RiskTier(str, Enum):
    READ_ONLY = "read_only"
    NAVIGATE = "navigate"
    INPUT = "input"
    SUBMIT_REVERSIBLE = "submit_reversible"
    SUBMIT_IRREVERSIBLE = "submit_irreversible"


@dataclass(frozen=True)
class Action:
    type: ActionType
    target: LocatorBundle | None = None
    value: str | None = None
    url: str | None = None
    key: str | None = None
    timeout_ms: int = 8000
    risk: RiskTier | None = None
    reason: str = ""
    """Why this action was taken. Required of the model on every tool call, so
    the "what the agent did and why" log is structural rather than a side
    channel bolted on afterwards."""

    sensitive: bool = False
    """Value must never be journaled, screenshotted, or sent to a model."""


@dataclass(frozen=True)
class ActionResult:
    ok: bool
    action: Action
    resolved: ResolvedLocator | None = None
    duration_ms: int = 0
    error: str | None = None
    error_detail: dict = field(default_factory=dict)


class PolicyDenied(Exception):
    """An action was refused at the choke point. Not a surface failure."""

    failure_class = "POLICY_DENIED"

    def __init__(self, message: str, *, action: Action | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.action = action


@runtime_checkable
class ActionGuard(Protocol):
    """Runs before every action. Raises PolicyDenied to refuse it.

    Implemented by the allowlist and risk policy (M6) and the control lease
    (M5). Kept as a protocol so the surface never imports either.
    """

    def check(self, action: Action, surface: "Surface") -> None: ...


@runtime_checkable
class Surface(Protocol):
    """A drivable application surface."""

    @property
    def capabilities(self) -> SurfaceCapabilities: ...

    async def observe(self) -> Observation:
        """Snapshot the current state as surface-neutral nodes."""

    async def act(self, action: Action) -> ActionResult:
        """Perform one action. Guards run first; locators resolve uniquely or fail."""

    def add_guard(self, guard: ActionGuard) -> None:
        """Install a guard for the life of this session.

        Registration is part of the protocol so policy can only ever be attached
        to the choke point. A caller that wanted to enforce a rule itself --
        checking risk before calling `act()` -- would be creating a second
        enforcement point that the next caller would forget, which is how a
        guardrail quietly becomes a convention.
        """

    async def screenshot(self, *, full_page: bool = False) -> bytes: ...

    async def current_url(self) -> str: ...
