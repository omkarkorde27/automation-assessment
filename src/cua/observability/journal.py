"""Structured run journal.

Every decision the replay engine makes emits an event: which condition was
evaluated and what it answered, which recovery fired, which locator candidate
won, why a run stopped. That is the difference between "the replay failed" and
"step s4's checkpoint expected a heading matching /account opened/ and the page
showed an authorization alert instead".

JSONL because a run is an append-only sequence and the failure case is reading
it after the fact, possibly after a crash. An in-memory journal exists so tests
can assert on the ladder's decisions -- the ordering of recoveries, outcomes and
checkpoints is load-bearing, and the only way to verify it ran in that order is
to look at what it recorded.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol


@dataclass(frozen=True)
class Event:
    kind: str
    at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "at": self.at.isoformat(), **self.data}


class Journal(Protocol):
    def emit(self, kind: str, **data: Any) -> None: ...


class MemoryJournal:
    """Collects events in memory. Used by tests and as a null sink."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, kind: str, **data: Any) -> None:
        self.events.append(Event(kind=kind, data=data))

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def of(self, kind: str) -> list[Event]:
        return [e for e in self.events if e.kind == kind]

    def first(self, kind: str) -> Event | None:
        return next((e for e in self.events if e.kind == kind), None)


class FileJournal:
    """Appends to evidence/runs/<run_id>/journal.jsonl.

    Redaction is applied by the caller before events reach here -- a journal
    that had to know about sensitivity rules would be a second place for that
    policy to live, and therefore a second place for it to be wrong.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.events: list[Event] = []

    def emit(self, kind: str, **data: Any) -> None:
        event = Event(kind=kind, data=data)
        self.events.append(event)
        with self.path.open("a") as fh:
            fh.write(json.dumps(event.to_dict(), default=str) + "\n")

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def of(self, kind: str) -> list[Event]:
        return [e for e in self.events if e.kind == kind]
