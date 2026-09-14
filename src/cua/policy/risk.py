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

    Known gap, closed in M6: guards run BEFORE the locator resolves, so this
    sees `action.risk` as the caller classified it rather than re-deriving it
    from the node. The agent classifies from the node it actually chose, so the
    tier is honest today -- but it is trusted rather than enforced, and a caller
    that mislabels an action would slip through. M6 moves classification to
    after resolution, inside `act()`, where it cannot be supplied from outside.
    """

    def __init__(self, *, allow_irreversible: bool = False) -> None:
        self.allow_irreversible = allow_irreversible
        self.denied: list[str] = []

    def check(self, action: Action, surface) -> None:
        if self.allow_irreversible:
            return
        tier = action.risk
        if tier is not RiskTier.SUBMIT_IRREVERSIBLE:
            return
        target = action.target.target_id if action.target else action.type.value
        self.denied.append(target)
        raise PolicyDenied(
            f"refusing an irreversible action on {target!r} during discovery: "
            f"re-run with --allow-irreversible if that is genuinely intended",
            action=action,
        )
