"""Artifact persistence: save, load, version, seal, and track telemetry.

A directory of JSON files. That is a deliberate choice, not a shortcut: these
artifacts are meant to be code-reviewed, diffed in a pull request, and promoted
between environments. A database would make the most important workflow -- "show
me what changed in this capability" -- harder, and the brief explicitly does not
reward building storage infrastructure.

Two separations matter:

  * **Telemetry is a sidecar.** Execution history changes constantly; the flow
    does not. Keeping counters out of the artifact file means the sealed hash
    stays valid across thousands of replays, and `git diff` on a capability
    shows flow changes rather than churn.

  * **Approval is not part of the hash.** Approving an artifact must not look
    like tampering with one. The hash covers the executable flow; approval state
    and provenance live alongside it.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from .schema import ApprovalState, CapabilityArtifact, Telemetry


class ArtifactError(Exception):
    pass


class ArtifactNotFound(ArtifactError):
    pass


class ArtifactIntegrityError(ArtifactError):
    """The file's content hash does not match its contents.

    Loud by default: an artifact is an instruction to drive a bank's back
    office, and silently executing a modified one is not an option.
    """


class ArtifactStore:
    def __init__(self, root: Path | str = "capabilities") -> None:
        self.root = Path(root)
        self.telemetry_dir = self.root / ".telemetry"

    # ---- paths ---------------------------------------------------------

    def path_for(self, ref: str) -> Path:
        return self.root / f"{ref}.json"

    def telemetry_path(self, ref: str) -> Path:
        return self.telemetry_dir / f"{ref}.json"

    # ---- write ---------------------------------------------------------

    def save(self, artifact: CapabilityArtifact, *, overwrite: bool = False) -> Path:
        """Seal and write. Refuses to silently replace a different flow.

        A capability id+version is a published contract: callers hold the ref.
        Changing what it does without changing its version is how a caller ends
        up invoking something it never reviewed, so that requires `overwrite`.
        """
        sealed = artifact.seal()
        path = self.path_for(sealed.ref)

        if path.exists() and not overwrite:
            existing = self.load(sealed.ref, verify=False)
            if existing.content_hash != sealed.content_hash:
                raise ArtifactError(
                    f"{sealed.ref} already exists with different content "
                    f"({existing.content_hash[:19]}... vs {sealed.content_hash[:19]}...). "
                    f"Bump the version or pass overwrite=True."
                )
            return path

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(_canonical(sealed), indent=2) + "\n")
        return path

    def set_approval(self, ref: str, state: ApprovalState, *, reason: str = "") -> CapabilityArtifact:
        artifact = self.load(ref)
        updated = artifact.model_copy(
            update={"capability": artifact.capability.model_copy(update={"approval_state": state})}
        )
        # The hash is unchanged by design -- approval is workflow state, and this
        # assertion keeps that guarantee honest.
        assert updated.compute_hash() == artifact.compute_hash()
        self.save(updated, overwrite=True)
        return updated

    # ---- read ----------------------------------------------------------

    def load(self, ref: str, *, verify: bool = True) -> CapabilityArtifact:
        path = self.path_for(ref)
        if not path.exists():
            raise ArtifactNotFound(f"no artifact at {path}")
        try:
            artifact = CapabilityArtifact.model_validate_json(path.read_text())
        except ValidationError as exc:
            raise ArtifactError(f"{ref} failed schema validation: {exc}") from exc

        if verify and not artifact.verify_hash():
            raise ArtifactIntegrityError(
                f"{ref} content hash mismatch: recorded {artifact.content_hash[:19]}..., "
                f"computed {artifact.compute_hash()[:19]}.... The file was edited by hand or "
                f"corrupted; re-seal it deliberately rather than running it."
            )
        return artifact

    def load_latest(self, capability_id: str, *, approved_only: bool = False) -> CapabilityArtifact:
        candidates = [a for a in self.list() if a.capability.id == capability_id]
        if approved_only:
            candidates = [
                a for a in candidates if a.capability.approval_state is ApprovalState.APPROVED
            ]
        if not candidates:
            what = "approved " if approved_only else ""
            raise ArtifactNotFound(f"no {what}artifact for '{capability_id}'")
        return max(candidates, key=lambda a: _semver_key(a.capability.version))

    def resolve_ref(self, ref: str, *, approved_only: bool = False) -> CapabilityArtifact:
        """Accept either 'id@1.2.0' or a bare 'id' (meaning latest)."""
        if "@" in ref:
            return self.load(ref)
        return self.load_latest(ref, approved_only=approved_only)

    def list(self) -> list[CapabilityArtifact]:
        if not self.root.exists():
            return []
        out = []
        for path in sorted(self.root.glob("*.json")):
            try:
                out.append(CapabilityArtifact.model_validate_json(path.read_text()))
            except ValidationError:
                continue  # not an artifact; leave it alone
        return out

    def versions(self, capability_id: str) -> list[str]:
        return sorted(
            (a.capability.version for a in self.list() if a.capability.id == capability_id),
            key=_semver_key,
        )

    def next_version(self, capability_id: str, bump: str = "minor") -> str:
        existing = self.versions(capability_id)
        if not existing:
            return "1.0.0"
        major, minor, patch = _semver_key(existing[-1])
        if bump == "major":
            return f"{major + 1}.0.0"
        if bump == "patch":
            return f"{major}.{minor}.{patch + 1}"
        return f"{major}.{minor + 1}.0"

    # ---- telemetry (sidecar) -------------------------------------------

    def telemetry(self, ref: str) -> Telemetry:
        """Counters as stored. May be stale -- see `current_telemetry`."""
        path = self.telemetry_path(ref)
        if not path.exists():
            return Telemetry()
        try:
            return Telemetry.model_validate_json(path.read_text())
        except ValidationError:
            return Telemetry()

    def current_hash(self, ref: str) -> str:
        try:
            return self.load(ref, verify=False).content_hash
        except (ArtifactNotFound, ArtifactError):
            return ""

    def current_telemetry(self, ref: str) -> Telemetry:
        """Counters for the flow CURRENTLY at this ref.

        The sidecar is addressed by ref but bound to a content hash. If a
        re-record replaced the flow at the same version, the stored counters
        describe something else, and reporting them would overstate how proven
        the current flow is. Returns empty counters in that case, preserving the
        reset lineage.
        """
        stored = self.telemetry(ref)
        content_hash = self.current_hash(ref)
        if stored.is_stale_for(content_hash):
            return Telemetry(
                artifact_hash=content_hash,
                reset_count=stored.reset_count + 1,
                previous_hash=stored.artifact_hash,
                reset_at=datetime.now(timezone.utc),
            )
        return stored

    def record_replay(
        self,
        ref: str,
        *,
        status: str,
        tenant: str = "",
        outcome_code: str | None = None,
        failure_class: str | None = None,
        resolved_by: dict[str, int] | None = None,
        degraded: int = 0,
    ) -> Telemetry:
        """Fold one replay into the running counters.

        This is the drift signal. Not just pass/fail: WHICH locator candidate
        resolved, per tenant. A capability whose targets start resolving by
        candidate 3 on one tenant is drifting there, and that shows up here long
        before anything actually breaks.
        """
        # Starts from the flow-bound view, so a re-recorded capability begins
        # its reliability history at zero instead of inheriting the previous
        # flow's successes.
        current = self.current_telemetry(ref)
        data = current.model_dump()
        data["artifact_hash"] = self.current_hash(ref) or current.artifact_hash

        data["replays"] += 1
        data["last_replay_at"] = datetime.now(timezone.utc)
        if status == "success":
            data["successes"] += 1
        elif status == "business_outcome" and outcome_code:
            data["business_outcomes"][outcome_code] = data["business_outcomes"].get(outcome_code, 0) + 1
        elif status == "escalated":
            data["escalations"] += 1
        elif status == "failed" and failure_class:
            data["failures"][failure_class] = data["failures"].get(failure_class, 0) + 1

        for strategy, count in (resolved_by or {}).items():
            data["resolved_by"][strategy] = data["resolved_by"].get(strategy, 0) + count
        data["degraded_resolutions"] += degraded

        if tenant:
            per = data["per_tenant"].setdefault(tenant, {})
            per["replays"] = per.get("replays", 0) + 1
            per[status] = per.get(status, 0) + 1
            per["degraded"] = per.get("degraded", 0) + degraded

        updated = Telemetry.model_validate(data)
        self.telemetry_path(ref).parent.mkdir(parents=True, exist_ok=True)
        self.telemetry_path(ref).write_text(
            json.dumps(updated.model_dump(mode="json", by_alias=True), indent=2) + "\n"
        )
        return updated


def _canonical(artifact: CapabilityArtifact) -> dict:
    """The on-disk form of an artifact.

    `by_alias=True` matters: the condition DSL spells negation `not`, but the
    Python field is `not_` (a keyword). Dumping without aliases writes `not_`
    into the file, so a system-written artifact and a hand-authored profile
    would spell the same operator two different ways, and a reviewer would see
    a token that appears nowhere in the documented DSL. Both forms parse, which
    is exactly why it would have gone unnoticed.
    """
    return artifact.model_dump(mode="json", by_alias=True)


def _semver_key(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(p) for p in version.split("."))
    return (major, minor, patch)
