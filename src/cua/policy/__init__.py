"""Policy: what may be done, and what must never be seen.

Enforced at the choke point (`Surface.act()`) and at every egress, never as a
prompt instruction. M6 completes the allowlist and the replay-side gating.
"""

from .redaction import apply_sensitivity, classify, screenshot_masks, scrub_text
from .risk import IrreversibleActionGuard, classify as classify_risk

__all__ = [
    "apply_sensitivity", "classify", "screenshot_masks", "scrub_text",
    "IrreversibleActionGuard", "classify_risk",
]
