"""Fault injection.

The brief's point is that in stable enterprise UIs the interesting failures are
*runtime* conditions, not layout drift. This module is how we produce them on
demand so replay's error taxonomy can be exercised deterministically.

Two ways to arm a fault:
  * ``POST /t/{tenant}/__control`` with ``fault=<name>&count=<n>`` -- persists
    across requests, used by tests and the CLI's ``--inject`` flag.
  * ``?inject=<name>`` on any request -- one-shot, used for ad-hoc demos.

Each fault fires at a specific point in the flow (see FAULT_POINTS) and is
consumed when it fires, so a run recovers naturally on the following attempt.
That is what makes "transient" faults genuinely transient.
"""

from collections import defaultdict

# fault name -> where in the flow it takes effect (documentation + validation)
FAULT_POINTS: dict[str, str] = {
    "not_found": "member search returns no records regardless of the query",
    "validation_error": "sub-account form rejects an otherwise valid submission",
    "permission_denied": "member detail returns an authorization failure",
    "interstitial": "an unexpected modal covers the next content page",
    "session_timeout": "the next content request bounces to the login screen",
    "slow_load": "the next content page stalls past the normal wait",
    "error_500": "the next content page returns an application error",
    "duplicate": "sub-account confirmation reports an existing duplicate",
}

# tenant slug -> fault name -> remaining firings
_armed: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))


def arm(tenant: str, fault: str, count: int = 1) -> None:
    if fault not in FAULT_POINTS:
        raise ValueError(f"unknown fault: {fault}")
    _armed[tenant][fault] += count


def peek(tenant: str, fault: str) -> bool:
    """Is this fault armed? Does not consume it."""
    return _armed[tenant].get(fault, 0) > 0


def consume(tenant: str, fault: str) -> bool:
    """Fire the fault if armed, consuming one firing."""
    if _armed[tenant].get(fault, 0) > 0:
        _armed[tenant][fault] -= 1
        return True
    return False


def state(tenant: str) -> dict[str, int]:
    return {k: v for k, v in _armed[tenant].items() if v > 0}


def clear(tenant: str | None = None) -> None:
    if tenant is None:
        _armed.clear()
    else:
        _armed[tenant].clear()
