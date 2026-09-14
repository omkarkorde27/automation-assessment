"""Signing in is an application fact, not a replay concern.

The profile says how to authenticate against a product; replay and discovery
both need it, for different reasons, and neither should own it.

For discovery the reason is sharper than convenience. The credentials are
`env:` references precisely so their values never reach a model, so the agent
cannot be allowed to discover its own way past a login screen -- it would have
to be handed a password to do it. Signing in happens *before* the agent is given
the surface, and the model never sees the login screen at all.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from ..conditions.dsl import EvalContext, evaluate
from ..conditions.model import Condition, describe
from ..observability.journal import Journal, MemoryJournal
from ..profiles.resolve import ResolvedProfile
from ..surfaces.base import Action, ActionType, Surface


class CredentialResolver:
    """Turns `env:NAME` into a value, in memory, for the length of one action.

    A separate object so nothing else ever holds a profile's raw credential map,
    and so the set of schemes that can produce a secret is one short list.
    """

    def __init__(self, env: dict[str, str] | None = None) -> None:
        self._env = env if env is not None else dict(os.environ)

    def resolve(self, ref: str) -> str:
        scheme, _, name = ref.partition(":")
        if scheme == "env":
            return self._env.get(name, "")
        raise ValueError(f"credential scheme '{scheme}' is not supported in this build")


@dataclass
class SignInResult:
    ok: bool
    reason: str = ""
    attempts: int = 0
    already_signed_in: bool = False


class Authenticator:
    """Executes a profile's login recipe against a surface."""

    def __init__(
        self,
        surface: Surface,
        profile: ResolvedProfile,
        *,
        credentials: CredentialResolver | None = None,
        journal: Journal | None = None,
    ) -> None:
        self.surface = surface
        self.profile = profile
        self.credentials = credentials or CredentialResolver()
        self.journal = journal or MemoryJournal()

    @property
    def _auth(self):
        return self.profile.profile.auth

    async def context(self) -> EvalContext:
        observation = await self.surface.observe()
        url = await self.surface.current_url() if self.surface.capabilities.has_url else ""
        return EvalContext(observation=observation, page_url=url,
                           capabilities=self.surface.capabilities)

    async def open_application(self) -> None:
        """Put the application on screen if nothing is open yet.

        "Is the session valid?" is unanswerable against about:blank.
        """
        if not self.surface.capabilities.can_navigate:
            return
        current = await self.surface.current_url()
        if current in ("", "about:blank"):
            self.journal.emit("session.opening_app", url=self.profile.base_url)
            await self.surface.act(Action(
                ActionType.NAVIGATE, url=f"{self.profile.base_url}/",
                reason="open the application before checking the session",
            ))

    async def is_expired(self) -> bool:
        detector = self._auth.session_expiry_detector
        if detector is None:
            return False
        return evaluate(detector, await self.context())

    async def sign_in(self, *, open_first: bool = True) -> SignInResult:
        """Run the login recipe if the app says we are not signed in."""
        if not self._auth.login_recipe or not self._auth.session_expiry_detector:
            return SignInResult(ok=True, already_signed_in=True)

        if open_first:
            await self.open_application()

        if not await self.is_expired():
            return SignInResult(ok=True, already_signed_in=True)

        self.journal.emit("session.expired",
                          detector=describe(self._auth.session_expiry_detector))
        return await self.run_recipe()

    async def run_recipe(self) -> SignInResult:
        if not self._auth.reauth.enabled:
            return SignInResult(ok=False, reason="re-authentication is disabled by the profile")

        recipe = self._auth.login_recipe
        for attempt in range(self._auth.reauth.max_attempts + 1):
            self.journal.emit("session.reauth_attempt", attempt=attempt + 1)
            for step in recipe.steps:
                result = await self.surface.act(self._action_for(step))
                if not result.ok:
                    self.journal.emit("session.reauth_step_failed", step=step.id,
                                      error=result.error)
                    break

            if recipe.success is None:
                return SignInResult(ok=True, attempts=attempt + 1)

            # Waited for, not checked once: signing in ends in a navigation, and
            # asserting the signed-in state the instant after the submit click
            # races the response.
            if await self._wait_for(recipe.success,
                                    self.profile.profile.surface.timeouts.navigation_ms):
                self.journal.emit("session.reauth_ok", attempt=attempt + 1)
                return SignInResult(ok=True, attempts=attempt + 1)

        return SignInResult(
            ok=False, attempts=self._auth.reauth.max_attempts + 1,
            reason="re-authentication did not reach the signed-in state",
        )

    def _action_for(self, step) -> Action:
        spec = step.action
        value = None
        sensitive = False
        if spec.value_from is not None:
            if spec.value_from.credential is not None:
                ref = self._auth.credentials.get(spec.value_from.credential, "")
                value = self.credentials.resolve(ref) if ref else ""
                sensitive = True
            elif spec.value_from.literal is not None:
                value = spec.value_from.literal

        url = None
        if spec.url_template:
            url = spec.url_template.replace("{base_url}", self.profile.base_url)

        return Action(
            type=spec.type, target=spec.target, value=value, url=url, key=spec.key,
            timeout_ms=step.timeout_ms, risk=step.risk, reason=step.intent,
            sensitive=sensitive,
        )

    async def _wait_for(self, condition: Condition, timeout_ms: int) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if evaluate(condition, await self.context()):
                return True
            await self.surface.act(Action(ActionType.WAIT, timeout_ms=400,
                                          reason="poll for the signed-in state"))
        self.journal.emit("wait.timed_out", condition=describe(condition))
        return False
