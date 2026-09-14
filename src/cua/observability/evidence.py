"""The evidence pack for one run.

`evidence/runs/<run_id>/` is what somebody reads when they want to know what
happened without having been there. It is written as the run proceeds rather
than assembled at the end, so a run that crashes still leaves its evidence.

Everything written here has already been through redaction: observations are
stored post-`apply_sensitivity`, screenshots post-masking. This module does not
re-redact, because a second redaction pass would imply the first one was
optional.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..perception.model import Observation
from .journal import FileJournal


class EvidenceWriter:
    def __init__(self, root: Path | str, run_id: str) -> None:
        self.dir = Path(root) / "runs" / run_id
        self.steps_dir = self.dir / "steps"
        self.steps_dir.mkdir(parents=True, exist_ok=True)
        self.journal = FileJournal(self.dir / "journal.jsonl")
        self.run_id = run_id

    def screenshot(self, index: int, png: bytes, *, phase: str = "post") -> Path:
        path = self.steps_dir / f"{index:03d}_{phase}.png"
        path.write_bytes(png)
        return path

    def observation(self, index: int, observation: Observation) -> Path:
        path = self.steps_dir / f"{index:03d}_observation.json"
        path.write_text(observation.model_dump_json(indent=2))
        return path

    def write_json(self, name: str, payload: Any) -> Path:
        path = self.dir / name
        path.write_text(json.dumps(payload, indent=2, default=str))
        return path

    def write_text(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text)
        return path

    def finish(self, status: str, **extra) -> Path:
        return self.write_json("result.json", {
            "run_id": self.run_id,
            "status": status,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            **extra,
        })
