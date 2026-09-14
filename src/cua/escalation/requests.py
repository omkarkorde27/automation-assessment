"""The intervention request, and where it is written.

When automation stops, somebody has to be able to pick the problem up cold. The
useful unit is therefore not "an error occurred" but a request carrying enough
context that a person who was not watching can decide what to do: what the
capability was trying to achieve, which step it was on, what it expected, what
it actually saw, a redacted picture of the screen, and the session it is parked
on.

*Seam, stated plainly:* in production this is a queue message or a webhook into
whatever routes work to operators. Here it is a JSON file under the run's
evidence directory. The file store is the deliberate mock -- the plan is
explicit that queues and services are not what this exercise rewards -- but the
shape of the request is the real interface, and nothing above it knows which of
the two it is talking to.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class InterventionStatus(str, Enum):
    OPEN = "open"
    """Filed, nobody has claimed it."""

    TAKEN = "taken"
    """An operator holds the session."""

    RESOLVED = "resolved"
    """Disposed of -- see `disposition`."""

    ABANDONED = "abandoned"
    """The operator gave up, or nobody arrived before the wait expired."""


class Disposition(str, Enum):
    """What the operator decided. These are the only three answers, because a
    human staring at a parked flow has exactly three useful things to say."""

    RESUME = "resume"
    """I fixed the screen; carry on from the step you were on."""

    STEP_COMPLETED = "step_completed"
    """I did that step myself; carry on from the NEXT one."""

    ABANDON = "abandon"
    """This run should not continue."""


class Resolution(BaseModel):
    model_config = ConfigDict(frozen=True)

    disposition: Disposition
    operator: str
    note: str = ""
    at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class InterventionRequest(BaseModel):
    """One unit of human work, with the context to do it.

    Frozen: an intervention is a record of a moment. Status changes produce a
    new one (`model_copy`) that overwrites the file, so the store is the
    authority on current state and the object is never quietly mutated under a
    caller that is still reading it.
    """

    model_config = ConfigDict(frozen=True)

    id: str = Field(default_factory=lambda: f"int_{uuid.uuid4().hex[:10]}")
    run_id: str
    session_id: str

    capability_ref: str = ""
    """`capability_id@version` -- what was being attempted."""

    goal: str = ""
    tenant: str = ""

    step_id: str = ""
    step_index: int | None = None
    """Where in the flow. `None` for a discovery run, which has no fixed steps."""

    reason_class: str
    """The vocabulary the operator triages on: REQUIRES_HUMAN, AMBIGUOUS_TARGET,
    IRREVERSIBLE_INTERRUPTED, CHECKPOINT_FAILED, ... Free-form on purpose,
    because a profile declares its own stuck patterns and their reason classes."""

    human_message: str
    """Written for a person, not for a log. What is wrong and what to decide."""

    expected: str = ""
    observed: str = ""

    screenshot_ref: str = ""
    """Path to a REDACTED screenshot. Sensitive regions are painted out before
    the file is written -- an evidence pack is an egress like any other."""

    observation_ref: str = ""

    takeover: bool = False
    """Whether the run is parked and waiting on this. False means the run has
    already ended and the request is a notification: an operator can look, but
    there is no live session to drive. Only the escalated variants park."""

    result_status: str = ""
    """The `ReplayResult` variant this was filed alongside, when the run ended
    anyway -- so the console can show "failed (CHECKPOINT_FAILED)" rather than
    implying somebody can still rescue it."""

    status: InterventionStatus = InterventionStatus.OPEN
    resolution: Resolution | None = None
    opened_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def describe(self) -> str:
        where = f" at {self.step_id}" if self.step_id else ""
        return f"{self.id} [{self.status.value}] {self.reason_class}{where}"


class InterventionStore:
    """File-backed. One JSON document per request, under its own run.

    Living inside `evidence/runs/<run_id>/interventions/` rather than in a
    global table is deliberate: an intervention is part of the story of a run,
    and somebody reading the evidence pack for a failed replay should find the
    page-out sitting next to the journal that caused it.
    """

    def __init__(self, root: Path | str = "evidence") -> None:
        self.root = Path(root)

    def _dir(self, run_id: str) -> Path:
        return self.root / "runs" / run_id / "interventions"

    def _path(self, request: InterventionRequest) -> Path:
        return self._dir(request.run_id) / f"{request.id}.json"

    def save(self, request: InterventionRequest) -> Path:
        directory = self._dir(request.run_id)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{request.id}.json"
        path.write_text(request.model_dump_json(indent=2))
        return path

    def get(self, intervention_id: str) -> InterventionRequest | None:
        for path in self.root.glob(f"runs/*/interventions/{intervention_id}.json"):
            return InterventionRequest.model_validate_json(path.read_text())
        return None

    def list(self, *, status: InterventionStatus | None = None,
             run_id: str | None = None) -> list[InterventionRequest]:
        """Newest first -- an operator wants the thing that just broke."""
        pattern = f"runs/{run_id}/interventions/*.json" if run_id else "runs/*/interventions/*.json"
        out: list[InterventionRequest] = []
        for path in self.root.glob(pattern):
            try:
                request = InterventionRequest.model_validate_json(path.read_text())
            except (ValueError, OSError):
                # A half-written file from a crashed run is not a reason for the
                # console to 500. Skipping it loses one row; raising loses the page.
                continue
            if status is not None and request.status is not status:
                continue
            out.append(request)
        return sorted(out, key=lambda r: r.opened_at, reverse=True)

    def dump(self, request: InterventionRequest) -> dict:
        return json.loads(request.model_dump_json())
