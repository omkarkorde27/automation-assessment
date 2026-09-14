"""The replay result contract.

Four variants, never collapsed. This is the piece the brief's glossary calls out
by name: *"'no such member' is a legitimate answer the caller needs, not a
crash. Conflating the two is the most common design mistake here."*

  success           the flow completed and the declared outputs were read
  business_outcome  the app gave a legitimate non-success answer the caller asked for
  escalated         a human is needed; the session is parked, not destroyed
  failure           something broke; here is what step, what was expected, what was seen

The distinction is not cosmetic. A calling agent branches on it: a
`business_outcome` is data it reports to a member, a `failure` is an incident, an
`escalation` is a queue item. Returning HTTP-500-shaped errors for "no such
member" would make the agent apologise for a system fault when the correct
answer was simply "that member does not exist".

Recoverable conditions are deliberately absent from this union. A dismissed
interstitial or a retried transient load is a step-level policy that replay
handles and journals; it only becomes a `failure` once its attempt budget is
exhausted. Promoting them to results would make callers handle conditions that
are none of their business.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Union


class ReplayStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    ESCALATED = "escalated"
    FAILED = "failed"


class FailureClass(str, Enum):
    """Why a run stopped. Chosen so each value implies a different response."""

    PARAM_INVALID = "PARAM_INVALID"
    """Caller error. Nothing was opened; fix the arguments."""

    CAPABILITY_UNSUPPORTED = "CAPABILITY_UNSUPPORTED"
    """This artifact cannot run on this surface (R-M3-1). Caught at preflight."""

    LOCATOR_UNRESOLVED = "LOCATOR_UNRESOLVED"
    LOCATOR_AMBIGUOUS = "LOCATOR_AMBIGUOUS"
    """Several controls matched. Never guessed -- see locators/resolve.py."""

    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    CHECKPOINT_FAILED = "CHECKPOINT_FAILED"
    """The action was performed but the expected state did not arrive."""

    TIMEOUT = "TIMEOUT"
    SESSION_EXPIRED = "SESSION_EXPIRED"
    """Only after re-authentication was attempted and failed."""

    SURFACE_ERROR = "SURFACE_ERROR"
    """The application faulted -- its own error page, a 500, a crash."""

    POLICY_DENIED = "POLICY_DENIED"
    EXTRACTION_FAILED = "EXTRACTION_FAILED"
    """The flow reached its end state but a declared output could not be read."""

    INTERNAL = "INTERNAL"


@dataclass(frozen=True)
class StepTrace:
    """What one step did. The unit of debugging."""

    step_id: str
    intent: str
    action: str
    ok: bool
    resolved_by: str = ""
    degraded: bool = False
    duration_ms: int = 0
    recoveries: tuple[str, ...] = ()
    note: str = ""


@dataclass(frozen=True)
class _Base:
    capability_ref: str = ""
    tenant: str = ""
    profile_hash: str = ""
    run_id: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    duration_ms: int = 0
    steps: tuple[StepTrace, ...] = ()
    evidence_ref: str = ""

    intervention_id: str | None = None
    """The human-work item this run filed, if any.

    On `_Base` rather than on `Escalated` alone, because filing an intervention
    and escalating are different decisions. An unrecovered checkpoint failure is
    still a `failure` -- the caller should debug it, not wait for a person -- and
    it should still reach an operator's queue. Collapsing the two would mean
    either paging somebody for every bug or silently swallowing the faults a
    person could actually fix."""

    @property
    def resolution_summary(self) -> dict[str, int]:
        """Which locator candidate won, per strategy -- the drift signal fed to
        telemetry after every run."""
        out: dict[str, int] = {}
        for step in self.steps:
            if step.resolved_by:
                out[step.resolved_by] = out.get(step.resolved_by, 0) + 1
        return out

    @property
    def degraded_count(self) -> int:
        return sum(1 for s in self.steps if s.degraded)


@dataclass(frozen=True)
class Success(_Base):
    outputs: dict[str, Any] = field(default_factory=dict)

    status = ReplayStatus.SUCCESS
    ok = True

    def describe(self) -> str:
        return f"success: {self.outputs}"


@dataclass(frozen=True)
class BusinessOutcome(_Base):
    """A legitimate answer that is not success. NOT an error."""

    code: str = ""
    message: str = ""
    """The institution's own wording where the artifact declared where to read
    it -- redacted before it is journaled, returned in full to the caller."""

    at_step: str = ""

    status = ReplayStatus.BUSINESS_OUTCOME
    ok = True
    """True on purpose: the capability did its job. The caller asked a question
    and got an answer, and treating that as a failure is the mistake this whole
    contract exists to prevent."""

    def describe(self) -> str:
        return f"business outcome {self.code}: {self.message}"


@dataclass(frozen=True)
class Escalated(_Base):
    """A human is needed. The session is parked for takeover, not torn down."""

    reason_class: str = ""
    human_message: str = ""
    at_step: str = ""

    resume_token: str | None = None
    """The parked session's id. A calling agent hands this, with the
    `intervention_id`, to whatever fronts the operator console; it is what makes
    "resume this run" address the live browser rather than start a new one."""

    status = ReplayStatus.ESCALATED
    ok = False

    def describe(self) -> str:
        return f"escalated at {self.at_step}: {self.reason_class} -- {self.human_message}"


@dataclass(frozen=True)
class Failure(_Base):
    """Something broke. Carries enough to debug without re-running."""

    failure_class: FailureClass = FailureClass.INTERNAL
    at_step: str = ""
    expected: str = ""
    """What the artifact asserted, in words -- from conditions.describe()."""

    observed: str = ""
    """What was actually on screen instead."""

    detail: dict = field(default_factory=dict)

    status = ReplayStatus.FAILED
    ok = False

    def describe(self) -> str:
        where = f" at {self.at_step}" if self.at_step else ""
        return (
            f"failed{where}: {self.failure_class.value}"
            + (f"\n  expected: {self.expected}" if self.expected else "")
            + (f"\n  observed: {self.observed}" if self.observed else "")
        )


ReplayResult = Union[Success, BusinessOutcome, Escalated, Failure]


def to_dict(result: ReplayResult) -> dict:
    """Serializable form, written to evidence/runs/<id>/result.json."""
    base = {
        "status": result.status.value,
        "ok": result.ok,
        "capability_ref": result.capability_ref,
        "tenant": result.tenant,
        "profile_hash": result.profile_hash,
        "run_id": result.run_id,
        "started_at": result.started_at.isoformat(),
        "duration_ms": result.duration_ms,
        "evidence_ref": result.evidence_ref,
        "resolution_summary": result.resolution_summary,
        "degraded_count": result.degraded_count,
        "intervention_id": result.intervention_id,
        "steps": [
            {
                "step_id": s.step_id,
                "intent": s.intent,
                "action": s.action,
                "ok": s.ok,
                "resolved_by": s.resolved_by,
                "degraded": s.degraded,
                "duration_ms": s.duration_ms,
                "recoveries": list(s.recoveries),
                "note": s.note,
            }
            for s in result.steps
        ],
    }
    if isinstance(result, Success):
        base["outputs"] = result.outputs
    elif isinstance(result, BusinessOutcome):
        base.update(code=result.code, message=result.message, at_step=result.at_step)
    elif isinstance(result, Escalated):
        base.update(
            reason_class=result.reason_class, human_message=result.human_message,
            at_step=result.at_step, resume_token=result.resume_token,
        )
    elif isinstance(result, Failure):
        base.update(
            failure_class=result.failure_class.value, at_step=result.at_step,
            expected=result.expected, observed=result.observed, detail=result.detail,
        )
    return base
