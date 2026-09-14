"""The control lease -- who is allowed to touch this session, enforced.

The brief's handoff requirement is easy to satisfy on paper ("the operator takes
over") and easy to get wrong in code, because the natural implementation is a
flag that everybody agrees to respect. This is not that. The lease is an
`ActionGuard`, so it is checked inside `Surface.act()` alongside the allowlist,
and an action from a non-holder does not happen -- not because the caller was
polite, but because the choke point refused it.

    AUTOMATION_OWNED -> PAUSED_PENDING_HUMAN -> HUMAN_OWNED -> RESUMING -> AUTOMATION_OWNED
                                                            -> ABANDONED

`PAUSED_PENDING_HUMAN` and `RESUMING` are deliberately states in which *nobody*
may act. The pause is the window between the engine deciding it is stuck and an
operator claiming the session: automation has stopped but no human has arrived,
and an action in that window belongs to neither of them. `RESUMING` is the
mirror image -- the human has let go and the engine has not yet re-verified
where it is standing, which is precisely the moment when a stray click would
invalidate the re-verification.

**How the holder is identified.** `act()` takes an `Action`, not an actor, and
adding an `actor` field would make identity a caller-supplied claim -- the exact
thing R-M6-2 exists to stop doing with risk tiers. Instead the actor is ambient:
a caller enters `lease.acting_as("automation")` and the guard reads it from a
`ContextVar`. In asyncio each task gets its own copy, so the engine's run task
and the console's request-handler task cannot see or inherit each other's actor
even though they share a loop and a browser. A caller who enters no context has
no actor and is refused, which is the right default for a piece of code nobody
thought about.

*Honest limit, for REPORT §5:* in one process nothing stops code from entering
`acting_as("automation")` while a human holds the lease. This is enforcement
against callers who are wrong, not against callers who are hostile; the latter
needs the lease to live behind a boundary the caller cannot reach (a session
service issuing signed grants), which is the documented seam and not something a
single-process take-home should pretend to have built.
"""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

from ..observability.journal import Journal, MemoryJournal
from ..surfaces.base import Action, PolicyDenied

AUTOMATION = "automation"

_ACTOR: contextvars.ContextVar[str | None] = contextvars.ContextVar("cua_actor", default=None)


class ControlState(str, Enum):
    AUTOMATION_OWNED = "AUTOMATION_OWNED"
    PAUSED_PENDING_HUMAN = "PAUSED_PENDING_HUMAN"
    HUMAN_OWNED = "HUMAN_OWNED"
    RESUMING = "RESUMING"
    ABANDONED = "ABANDONED"


#: Legal transitions. Anything not listed raises, because a lease that silently
#: accepts an impossible transition is a lease that will one day be in a state
#: nobody can explain.
_TRANSITIONS: dict[ControlState, frozenset[ControlState]] = {
    ControlState.AUTOMATION_OWNED: frozenset({ControlState.PAUSED_PENDING_HUMAN}),
    ControlState.PAUSED_PENDING_HUMAN: frozenset({
        ControlState.HUMAN_OWNED, ControlState.RESUMING, ControlState.ABANDONED}),
    ControlState.HUMAN_OWNED: frozenset({ControlState.RESUMING, ControlState.ABANDONED}),
    ControlState.RESUMING: frozenset({ControlState.AUTOMATION_OWNED, ControlState.ABANDONED}),
    ControlState.ABANDONED: frozenset(),
}


class NotControlHolder(PolicyDenied):
    """An action was attempted by somebody who does not hold the lease.

    A `PolicyDenied`, so it travels the path every other refusal travels: caught
    inside `act()`, returned as a failed `ActionResult` carrying POLICY_DENIED,
    journaled. Making it a separate exception class that escaped `act()` would
    give control violations their own error path, and a second error path is a
    second thing to get wrong.
    """

    def __init__(self, message: str, *, action: Action | None = None,
                 state: ControlState, actor: str | None, holder: str | None) -> None:
        super().__init__(message, action=action)
        self.denied_by = "control_lease"
        self.state = state
        self.actor = actor
        self.holder = holder


class IllegalTransition(RuntimeError):
    """The lease was asked to move somewhere it cannot go from where it is."""


@dataclass(frozen=True)
class Transfer:
    """One recorded change of hands. The audit trail the brief asks for."""

    frm: ControlState
    to: ControlState
    actor: str
    reason: str
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def describe(self) -> str:
        return f"{self.frm.value} -> {self.to.value} by {self.actor}: {self.reason}"


class ControlLease:
    """Who holds this session, and the history of how it changed hands."""

    def __init__(self, session_id: str, *, journal: Journal | None = None) -> None:
        self.session_id = session_id
        self.journal = journal or MemoryJournal()
        self.state = ControlState.AUTOMATION_OWNED
        self.holder: str | None = AUTOMATION
        self.history: list[Transfer] = []

    # ---- identity -------------------------------------------------------

    @contextmanager
    def acting_as(self, actor: str):
        """Scope every `act()` inside this block to `actor`.

        A context manager rather than a setter because an actor that outlives
        its own code path is how a released lease keeps working.
        """
        token = _ACTOR.set(actor)
        try:
            yield self
        finally:
            _ACTOR.reset(token)

    @staticmethod
    def current_actor() -> str | None:
        return _ACTOR.get()

    def permits(self, actor: str | None) -> bool:
        if actor is None:
            return False
        return actor == self.holder

    def check(self, action: Action, surface=None) -> None:
        """`ActionGuard` protocol. Runs pre-resolution: control is a property of
        the session, not of the control being operated, so it needs no node."""
        actor = self.current_actor()
        if self.permits(actor):
            return
        who = actor or "an unidentified caller"
        raise NotControlHolder(
            f"{who} may not act on session {self.session_id}: the control lease is "
            f"{self.state.value}"
            + (f" and is held by {self.holder}" if self.holder else " and is held by nobody"),
            action=action, state=self.state, actor=actor, holder=self.holder,
        )

    # ---- transitions ----------------------------------------------------

    def _to(self, state: ControlState, *, actor: str, reason: str,
            holder: str | None) -> Transfer:
        if state not in _TRANSITIONS[self.state]:
            raise IllegalTransition(
                f"cannot move the lease on {self.session_id} from {self.state.value} "
                f"to {state.value}"
            )
        transfer = Transfer(frm=self.state, to=state, actor=actor, reason=reason)
        self.state = state
        self.holder = holder
        self.history.append(transfer)
        self.journal.emit("lease.transferred", session=self.session_id,
                          **{"from": transfer.frm.value}, to=state.value,
                          actor=actor, reason=reason, holder=holder)
        return transfer

    def pause(self, *, reason: str, actor: str = AUTOMATION) -> Transfer:
        """Automation has stopped and is waiting for a person. Nobody may act."""
        return self._to(ControlState.PAUSED_PENDING_HUMAN, actor=actor, reason=reason,
                        holder=None)

    def take(self, operator: str, *, reason: str = "operator took control") -> Transfer:
        """A named person claims the session."""
        return self._to(ControlState.HUMAN_OWNED, actor=operator, reason=reason,
                        holder=operator)

    def begin_resume(self, *, actor: str, reason: str = "operator released control") -> Transfer:
        """The human has let go; the engine has not yet re-verified. Nobody acts."""
        return self._to(ControlState.RESUMING, actor=actor, reason=reason, holder=None)

    def resumed(self, *, reason: str = "precondition re-verified") -> Transfer:
        """Automation is back in control, having checked where it is standing."""
        return self._to(ControlState.AUTOMATION_OWNED, actor=AUTOMATION, reason=reason,
                        holder=AUTOMATION)

    def abandon(self, *, actor: str, reason: str) -> Transfer:
        """Terminal. The run is over and the session belongs to nobody."""
        return self._to(ControlState.ABANDONED, actor=actor, reason=reason, holder=None)

    # ---- reporting ------------------------------------------------------

    @property
    def automation_may_act(self) -> bool:
        return self.state is ControlState.AUTOMATION_OWNED

    def describe(self) -> str:
        return f"{self.session_id}: {self.state.value} (holder: {self.holder or 'nobody'})"

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "state": self.state.value,
            "holder": self.holder,
            "history": [
                {"from": t.frm.value, "to": t.to.value, "actor": t.actor,
                 "reason": t.reason, "at": t.at.isoformat()}
                for t in self.history
            ],
        }
