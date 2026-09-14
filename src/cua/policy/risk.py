"""Risk classification, and the one guard discovery needs.

Risk is a property of what an action DOES, not of who asked for it, so it is
derived here from the action type plus the semantics of the control being
operated -- a button named "Confirm and Open Account" is irreversible whatever
the caller believes.

M6 adds the allowlist and the replay-side gating. What lives here now is the
part M4 cannot ship without: an LLM exploring a bank's back office must not be
able to post a transaction because it was curious.
"""

from __future__ import annotations

import re

from ..perception.model import UiNode
from ..surfaces.base import Action, ActionType, PolicyDenied, RiskTier

# Verbs that commit. Matched against the control's accessible name, because in a
# legacy app the button text is the only declaration of intent available.
IRREVERSIBLE_NAME = re.compile(
    r"(?i)\b(confirm|submit|post|transfer|delete|remove|close account|open account"
    r"|approve|authorize|issue|void|reverse|disburse)\b"
)

# Reversible submits: they change what is on screen, not what the bank holds.
REVERSIBLE_NAME = re.compile(r"(?i)\b(search|find|filter|lookup|look up|refresh|continue|next)\b")


def classify(action_type: ActionType, node: UiNode | None) -> RiskTier:
    """Best-effort tier for an action about to be performed.

    Deliberately pessimistic on ambiguity: an unnamed button that submits a form
    is treated as a reversible submit rather than as a click, because "we could
    not tell" should never round down to safe.
    """
    if action_type in (ActionType.WAIT, ActionType.SCROLL):
        return RiskTier.READ_ONLY
    if action_type is ActionType.NAVIGATE:
        return RiskTier.NAVIGATE
    if action_type in (ActionType.FILL, ActionType.SELECT_OPTION, ActionType.PRESS_KEY):
        return RiskTier.INPUT

    name = node.name if node else ""
    if IRREVERSIBLE_NAME.search(name):
        return RiskTier.SUBMIT_IRREVERSIBLE
    if REVERSIBLE_NAME.search(name):
        return RiskTier.SUBMIT_REVERSIBLE
    if node is not None and node.role == "button":
        return RiskTier.SUBMIT_REVERSIBLE
    return RiskTier.NAVIGATE


class IrreversibleActionGuard:
    """Refuses irreversible actions unless the run explicitly allows them.

    An `ActionGuard`, so it runs inside `Surface.act()` -- the choke point every
    caller funnels through. This is the difference between a guardrail and a
    prompt instruction: the model cannot act outside it even if it decides to,
    and the attempt is journaled as POLICY_DENIED rather than silently dropped.

    **R-M6-2.** The decision is taken in `check_resolved`, against a tier that
    `act()` re-derived from the node that actually resolved. `check` -- the
    pre-resolution pass -- deliberately does nothing: at that point the only
    tier available is the one the caller wrote on the `Action`, and a guard that
    reads the caller's own risk assessment is a convention with a stack frame.
    The gap was real and is closed here: an `Action` labelled `input` whose
    target resolves to "Confirm and Open Account" is refused on the label the
    button carries, not the one the caller supplied.

    **A human may go further than the agent.** When the control lease is held by
    a person, the guard stands down: takeover exists so an operator can do the
    thing automation would not, and a guard that blocked them would make the
    console decorative. They do not leave the allowlist -- that guard is not
    lease-aware, and never should be -- and the override is journaled with their
    name against it.
    """

    def __init__(self, *, allow_irreversible: bool = False, lease=None) -> None:
        self.allow_irreversible = allow_irreversible
        self.lease = lease
        """Optional `ControlLease`. Duck-typed rather than imported, so `policy`
        keeps not depending on `session` and this guard stays usable in a run
        that has no lease at all."""

        self.denied: list[str] = []
        self.human_overrides: list[str] = []

    def check(self, action: Action, surface=None) -> None:
        """Pre-resolution: nothing to decide yet. See the class docstring."""
        return None

    def check_resolved(self, action: Action, tier: RiskTier, node, surface=None) -> None:
        if tier is not RiskTier.SUBMIT_IRREVERSIBLE:
            return
        target = action.target.target_id if action.target else action.type.value

        if self._human_holds():
            self.human_overrides.append(target)
            journal = getattr(surface, "journal", None)
            if journal is not None:
                journal.emit("policy.human_override", target=target,
                             derived_risk=tier.value, holder=self.lease.holder,
                             note="irreversible action permitted: a person holds the lease")
            return

        if self.allow_irreversible:
            return

        self.denied.append(target)
        raise PolicyDenied(
            f"refusing an irreversible action on {target!r}: the control it resolved to "
            f"is {node.describe() if node else 'unnamed'}. Re-run with "
            f"--allow-irreversible if that is genuinely intended.",
            action=action,
        )

    def _human_holds(self) -> bool:
        lease = self.lease
        if lease is None:
            return False
        return getattr(lease.state, "value", "") == "HUMAN_OWNED"
