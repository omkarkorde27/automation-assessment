"""The control lease: the state machine, and the refusal that makes it real.

Part 3.7's claim is that a non-holder *physically cannot* act on the session --
not that callers agree not to. The tests that matter here are therefore the
negative ones: paused, resuming and abandoned are all states in which a
perfectly well-formed action does not happen.
"""

from __future__ import annotations

import pytest

from cua.locators.model import LocatorBundle, RoleNameExact
from cua.observability.journal import MemoryJournal
from cua.session.control import (
    AUTOMATION,
    ControlLease,
    ControlState,
    IllegalTransition,
    NotControlHolder,
)
from cua.surfaces.base import Action, ActionType

TARGET = LocatorBundle(target_id="t", candidates=(RoleNameExact(role="button", name="Go"),),
                       notes="test bundle")
CLICK = Action(ActionType.CLICK, target=TARGET, reason="test")


def lease(journal=None) -> ControlLease:
    return ControlLease("sess_1", journal=journal or MemoryJournal())


# ---- the state machine ---------------------------------------------------

def test_the_happy_path_walks_the_whole_cycle():
    log = MemoryJournal()
    lse = lease(log)
    assert lse.state is ControlState.AUTOMATION_OWNED

    lse.pause(reason="ambiguous target")
    assert lse.state is ControlState.PAUSED_PENDING_HUMAN and lse.holder is None

    lse.take("alice")
    assert lse.state is ControlState.HUMAN_OWNED and lse.holder == "alice"

    lse.begin_resume(actor="alice")
    assert lse.state is ControlState.RESUMING and lse.holder is None

    lse.resumed()
    assert lse.state is ControlState.AUTOMATION_OWNED and lse.holder == AUTOMATION

    assert [e.data["to"] for e in log.of("lease.transferred")] == [
        "PAUSED_PENDING_HUMAN", "HUMAN_OWNED", "RESUMING", "AUTOMATION_OWNED"]
    assert all(e.data["actor"] and e.data["reason"] for e in log.of("lease.transferred"))


def test_a_paused_lease_can_be_abandoned_without_anybody_taking_it():
    """Nobody came. That is a transition, not a stuck state."""
    lse = lease()
    lse.pause(reason="nobody home")
    lse.abandon(actor="system", reason="wait expired")
    assert lse.state is ControlState.ABANDONED


#: (legal prefix, the illegal move). Each walk gets as far as it legitimately
#: can and then attempts the shortcut somebody would write by hand.
_SHORTCUTS = [
    ([], lambda l: l.take("alice"),
     "automation -> human, skipping the pause"),
    ([lambda l: l.pause(reason="x")], lambda l: l.resumed(),
     "paused -> automation, skipping the human entirely"),
    ([lambda l: l.pause(reason="x"), lambda l: l.take("alice")], lambda l: l.resumed(),
     "human -> automation, skipping the re-verification window"),
]


@pytest.mark.parametrize("prefix,illegal,why", _SHORTCUTS,
                         ids=[w[2] for w in _SHORTCUTS])
def test_an_impossible_transition_raises_rather_than_being_absorbed(prefix, illegal, why):
    lse = lease()
    for step in prefix:
        step(lse)
    with pytest.raises(IllegalTransition):
        illegal(lse)


def test_abandoned_is_terminal():
    lse = lease()
    lse.pause(reason="x")
    lse.abandon(actor="alice", reason="done")
    with pytest.raises(IllegalTransition):
        lse.take("bob")


# ---- enforcement ---------------------------------------------------------

def test_automation_may_act_only_while_it_holds_the_lease():
    lse = lease()
    with lse.acting_as(AUTOMATION):
        lse.check(CLICK)  # no raise

    lse.pause(reason="stuck")
    with lse.acting_as(AUTOMATION):
        with pytest.raises(NotControlHolder) as exc:
            lse.check(CLICK)
    assert exc.value.state is ControlState.PAUSED_PENDING_HUMAN
    assert exc.value.denied_by == "control_lease"


def test_the_operator_may_act_only_after_taking_control():
    lse = lease()
    lse.pause(reason="stuck")

    with lse.acting_as("alice"):
        with pytest.raises(NotControlHolder):
            lse.check(CLICK)

    lse.take("alice")
    with lse.acting_as("alice"):
        lse.check(CLICK)  # no raise


def test_one_operator_cannot_act_on_another_operators_session():
    lse = lease()
    lse.pause(reason="stuck")
    lse.take("alice")
    with lse.acting_as("bob"):
        with pytest.raises(NotControlHolder) as exc:
            lse.check(CLICK)
    assert exc.value.holder == "alice"


def test_a_caller_with_no_identity_is_refused():
    """The default for code nobody thought about is deny, not automation."""
    lse = lease()
    assert ControlLease.current_actor() is None
    with pytest.raises(NotControlHolder) as exc:
        lse.check(CLICK)
    assert "unidentified" in str(exc.value)


def test_nobody_may_act_while_the_lease_is_resuming():
    """The window between the human letting go and the engine re-verifying is
    exactly when a stray action would invalidate the check about to be made."""
    lse = lease()
    lse.pause(reason="stuck")
    lse.take("alice")
    lse.begin_resume(actor="alice")
    for actor in (AUTOMATION, "alice"):
        with lse.acting_as(actor):
            with pytest.raises(NotControlHolder):
                lse.check(CLICK)


def test_the_actor_does_not_outlive_its_block():
    lse = lease()
    with lse.acting_as(AUTOMATION):
        assert ControlLease.current_actor() == AUTOMATION
    assert ControlLease.current_actor() is None


async def test_two_concurrent_actors_do_not_see_each_others_identity():
    """The engine's task and a console request handler share a loop and a
    browser. If the actor leaked between them the lease would be decorative."""
    import asyncio

    lse = lease()
    lse.pause(reason="stuck")
    lse.take("alice")
    seen: dict[str, bool] = {}

    async def automation():
        with lse.acting_as(AUTOMATION):
            await asyncio.sleep(0)
            seen["automation_allowed"] = lse.permits(ControlLease.current_actor())

    async def operator():
        with lse.acting_as("alice"):
            await asyncio.sleep(0)
            seen["alice_allowed"] = lse.permits(ControlLease.current_actor())

    await asyncio.gather(automation(), operator())
    assert seen == {"automation_allowed": False, "alice_allowed": True}
