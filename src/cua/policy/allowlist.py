"""The allowlist: where automation may go, and what it may do when it gets there.

Part 3.6's first bullet, and the half of the choke point that needs no node. It
runs in `Surface.act()`'s **pre-resolution** pass, because an origin, a path and
an action type are properties of the destination and the verb -- knowable before
anything resolves, and therefore checkable before anything is touched.

Three things worth stating plainly, because each is a decision rather than an
obvious default:

**Origins are anchored to the tenant, not written into the file.** A committed
policy cannot name every institution's host, and one that tried would be edited
on every onboarding until somebody widened it to `*`. So `allow_profile_origin`
means "the origin this tenant's resolved profile already declares" -- the same
`base_url` the flow was going to use anyway. The file then adds exceptions,
which is a much smaller and more reviewable list.

**A per-capability block only ever narrows.** Overlays that can widen are how a
policy file becomes decorative: the base looks strict and some capability three
screens down has quietly opened it up. So the effective rule is `default AND
capability` -- path patterns must satisfy both lists, action types intersect,
`max_navigations` takes the lower, and `denied_paths` unions. There is no syntax
for a capability to permit something the default forbids, on purpose.

**Honest limit, for REPORT §6.** This guard sees the navigations automation
*asks* for. A click on a link is a click, and the app decides where it lands --
no pre-action check can know that. The engine therefore re-checks the URL it
actually ended up on after every step (`replay.engine._offsite`), which is where
a link out of the allowed space is caught. Between those two, what is not
covered is a redirect chain that returns before the next observation, and that
is a statement about what this design can see, not a gap it forgot.
"""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field

from ..surfaces.base import Action, ActionType, PolicyDenied

DEFAULT_POLICY_PATH = Path("config/policy.yaml")
SUPPORTED_VERSION = 1

#: Everything the action vocabulary offers. A policy that lists nothing gets
#: this, so an absent `permitted_actions` is not silently an empty allowlist
#: that refuses the first click.
ALL_ACTIONS: tuple[str, ...] = tuple(a.value for a in ActionType)


class NotAllowed(PolicyDenied):
    """Refused by the allowlist rather than by risk or by the control lease.

    A distinct class because the three are the same `failure_class` and utterly
    different incidents: "the agent tried to leave the application" is not "the
    agent tried to post a transaction" is not "somebody acted without the lease".
    """

    def __init__(self, message: str, *, action: Action | None = None) -> None:
        super().__init__(message, action=action)
        self.denied_by = "allowlist"


class AllowlistRules(BaseModel):
    """One set of rules. The file holds a default and per-capability narrowings."""

    model_config = ConfigDict(frozen=True)

    allow_profile_origin: bool = True
    """Permit the origin of the resolved profile's `base_url`. Almost always the
    only origin a flow needs, and the one thing a committed file cannot name."""

    allowed_origins: tuple[str, ...] = ()
    """Extra origins, as `scheme://host[:port]`. Compared exactly, not by
    substring: `https://evil.com/#bank.example.com` contains the host name and
    is not the host."""

    allowed_paths: tuple[str, ...] = ()
    """Regexes searched against the URL path. Empty means any path -- an
    allowlist of origins with no path restriction is a coherent policy, and
    pretending otherwise would force every profile to write `.*`."""

    denied_paths: tuple[str, ...] = ()
    """Regexes that refuse regardless of `allowed_paths`. Deny wins, always."""

    permitted_actions: tuple[str, ...] = ALL_ACTIONS
    max_navigations: int = 50
    """Per session. A flow that navigates fifty times is not replaying a
    recorded flow, it is wandering -- which during discovery is the dead-end
    detector's job and during replay is a bug worth stopping."""

    def narrow(self, other: "AllowlistRules | None") -> "AllowlistRules":
        """`self AND other`. Never a widening; see the module docstring."""
        if other is None:
            return self
        return AllowlistRules(
            allow_profile_origin=self.allow_profile_origin and other.allow_profile_origin,
            allowed_origins=(tuple(o for o in self.allowed_origins if o in other.allowed_origins)
                             if other.allowed_origins else self.allowed_origins),
            allowed_paths=tuple(dict.fromkeys(self.allowed_paths + other.allowed_paths)),
            denied_paths=tuple(dict.fromkeys(self.denied_paths + other.denied_paths)),
            permitted_actions=tuple(a for a in self.permitted_actions
                                    if a in other.permitted_actions),
            max_navigations=min(self.max_navigations, other.max_navigations),
        )


class PolicyFile(BaseModel):
    """`config/policy.yaml`, parsed."""

    model_config = ConfigDict(frozen=True)

    version: int = 1
    """Schema version of the file. Checked on load rather than carried around:
    a policy written against a shape this build does not understand must fail
    loudly, because the failure mode of guessing is a guardrail that silently
    permits whatever it could not parse."""

    default: AllowlistRules = Field(default_factory=AllowlistRules)
    capabilities: dict[str, AllowlistRules] = Field(default_factory=dict)

    def for_capability(self, ref: str) -> AllowlistRules:
        """Effective rules for `id@version`, or for `id` if the file keys on that.

        Version-less keys are supported because a policy that had to be edited
        on every version bump would be out of date by the second bump.
        """
        rules = self.default
        bare = ref.split("@", 1)[0]
        for key in (ref, bare):
            if key in self.capabilities:
                return rules.narrow(self.capabilities[key])
        return rules


def load_policy(path: Path | str = DEFAULT_POLICY_PATH) -> PolicyFile:
    """Read the policy file. A missing file is an error, not an open door."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"no policy file at {p}. The allowlist is not optional -- if a run genuinely "
            f"needs no restrictions, say so in a file rather than by deleting one."
        )
    parsed = PolicyFile.model_validate(yaml.safe_load(p.read_text()) or {})
    if parsed.version != SUPPORTED_VERSION:
        raise ValueError(
            f"{p} declares policy version {parsed.version}; this build understands "
            f"{SUPPORTED_VERSION}. Refusing to interpret it: a policy read under the "
            f"wrong schema is a guardrail that permits whatever it could not parse."
        )
    return parsed


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


class AllowlistGuard:
    """Refuses actions that leave the permitted space.

    An `ActionGuard`: it is installed on the surface and runs inside `act()`, so
    no caller can route around it. It defines only `check` -- there is nothing
    it wants to know about the node that resolved, and R-M6-2's second pass is
    for guards that do.
    """

    def __init__(self, rules: AllowlistRules, *, base_url: str = "",
                 capability_ref: str = "") -> None:
        self.rules = rules
        self.capability_ref = capability_ref
        self.base_origin = origin_of(base_url)
        self.navigations = 0
        self.denied: list[str] = []

        self._allowed = [re.compile(p) for p in rules.allowed_paths]
        self._denied = [re.compile(p) for p in rules.denied_paths]

    # ---- the guard hook -------------------------------------------------

    def check(self, action: Action, surface=None) -> None:
        if action.type.value not in self.rules.permitted_actions:
            self._refuse(action, f"action type {action.type.value!r} is not permitted here "
                                 f"(allowed: {', '.join(sorted(self.rules.permitted_actions))})")

        if action.type is ActionType.NAVIGATE:
            if self.navigations >= self.rules.max_navigations:
                self._refuse(action, f"navigation budget of {self.rules.max_navigations} is "
                                     f"exhausted; this flow is wandering, not replaying")
            if problem := self.rejects(action.url or ""):
                self._refuse(action, problem)
            # Counted only once the URL passed, so a refused attempt does not
            # spend budget -- otherwise a run could be starved by denials.
            self.navigations += 1

    # ---- the same decision, reusable ------------------------------------

    def rejects(self, url: str) -> str | None:
        """Why this URL is out of bounds, or None. Also used by the engine to
        re-check the URL a click actually landed on."""
        if not url:
            return "an empty url"

        origin = origin_of(url)
        if origin and not self.permits_origin(origin):
            return (f"origin {origin} is not on the allowlist "
                    f"(permitted: {', '.join(self.origins()) or 'none'})")

        path = urlsplit(url).path or "/"
        for pattern in self._denied:
            if pattern.search(path):
                return f"path {path} matches the denied pattern /{pattern.pattern}/"
        if self._allowed and not any(p.search(path) for p in self._allowed):
            return (f"path {path} matches none of the allowed patterns "
                    f"({', '.join('/' + p.pattern + '/' for p in self._allowed)})")
        return None

    def permits_origin(self, origin: str) -> bool:
        return origin in self.origins()

    def origins(self) -> tuple[str, ...]:
        extra = tuple(self.rules.allowed_origins)
        if self.rules.allow_profile_origin and self.base_origin:
            return (self.base_origin,) + extra
        return extra

    def _refuse(self, action: Action, why: str) -> None:
        target = action.url or (action.target.target_id if action.target else action.type.value)
        self.denied.append(target)
        # The capability is named in the refusal because the rules that produced
        # it are per-capability: "refused" without "under which narrowing" sends
        # the reader to the default block, which is usually not the one that
        # decided.
        under = f" (rules for {self.capability_ref})" if self.capability_ref else ""
        raise NotAllowed(f"refusing {action.type.value} on {target!r}{under}: {why}",
                         action=action)


__all__ = [
    "ALL_ACTIONS", "AllowlistGuard", "AllowlistRules", "NotAllowed", "PolicyFile",
    "DEFAULT_POLICY_PATH", "SUPPORTED_VERSION", "load_policy", "origin_of",
]
