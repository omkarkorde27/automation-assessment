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
gating, and the control lease all run here, so an action that policy forbids
cannot be performed by any caller -- the LLM, the replay engine, or the operator
console. A guardrail expressed as a prompt instruction is not a guardrail.

The hook is in **two passes**, and the split is R-M6-2:

  * `check(action, surface)` runs BEFORE the locator resolves. Checks that need
    no node live here -- the allowlist and the control lease are properties of
    the session and the destination, not of the control being operated.
  * `check_resolved(action, tier, node, surface)` runs AFTER resolution, against
    a tier **re-derived from the node that actually resolved**. It is optional;
    a guard that does not define it simply has nothing to say at this point.

The second pass exists because the first one cannot do the job. A pre-resolution
guard has a `LocatorBundle`, not a node, so the only tier available to it is the
one the caller wrote on the `Action` -- and a choke point that reads the caller's
own description of how dangerous its action is has not checked anything. "The
button in this row" is neither reversible nor irreversible until you know which
button it is. `action.risk` therefore survives as a *declaration*: journaled,
compared against the derived tier, and never the thing policy trusts.
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

    derived_risk: RiskTier | None = None
    """The tier `act()` computed from the node that actually resolved (R-M6-2).

    Returned so callers record what was enforced rather than what they claimed.
    `None` only when the action never reached the post-resolution pass -- it was
    refused pre-resolution, or the locator did not resolve at all."""


class PolicyDenied(Exception):
    """An action was refused at the choke point. Not a surface failure."""

    failure_class = "POLICY_DENIED"

    def __init__(self, message: str, *, action: Action | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.action = action
        self.denied_by = "policy"
        """Which guard refused, for the journal. A run that was stopped by the
        control lease and one that was stopped by the irreversible guard are the
        same failure class and completely different incidents."""


@runtime_checkable
class ActionGuard(Protocol):
    """Runs before every action. Raises PolicyDenied to refuse it.

    Implemented by the risk policy and the control lease. Kept as a protocol so
    the surface never imports either.
    """

    def check(self, action: Action, surface: "Surface") -> None:
        """Pre-resolution. No node is available yet; see the module docstring."""

    # Optional second pass. Declared here so the contract is readable in one
    # place, and probed with `hasattr` at the call site so a guard with nothing
    # to say after resolution does not have to write an empty body.
    #
    # def check_resolved(self, action, tier, node, surface) -> None: ...


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
