"""Profile resolution: product + tenant overlay -> one effective profile.

Merge rules, all chosen so a reviewer can predict the result by reading the
overlay alone:

  * scalars and maps -- overlay wins, shallow-merged per key;
  * keyed lists (recoveries, hard_failures, stuck_patterns, key_screens) --
    merged BY ID: same id replaces, new id appends, `disabled_recoveries`
    removes. Positional list merging would make an overlay's meaning depend on
    the base's ordering, which is how these become unreviewable;
  * depth capped at two (product -> tenant).

The result is hashed and journaled, so every replay records exactly which
layered configuration produced it. "It works on tenant A but not tenant B" needs
to be answerable from evidence, not by re-deriving a merge in your head.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from pydantic import ValidationError

from ..artifact.schema import CapabilityArtifact
from .schema import AppProfile

MAX_INHERITANCE_DEPTH = 2


class ProfileError(Exception):
    pass


class ProfileNotFound(ProfileError):
    pass


@dataclass(frozen=True)
class ResolvedProfile:
    profile: AppProfile
    lineage: tuple[str, ...]
    """Base-first, e.g. ("meridian-core@4.2", "valley-cu@1")."""

    hash: str

    @property
    def base_url(self) -> str:
        url = self.profile.surface.base_url
        if not url:
            raise ProfileError(
                f"{self.lineage[-1]} has no base_url; a product profile is not runnable on "
                f"its own -- a tenant overlay must supply the host."
            )
        return url.rstrip("/")

    def recovery(self, recovery_id: str):
        return next((r for r in self.profile.recoveries if r.id == recovery_id), None)

    def summary(self) -> str:
        return (
            f"{' <- '.join(self.lineage)} [{self.hash[:19]}] "
            f"{len(self.profile.recoveries)} recoveries, "
            f"{len(self.profile.stuck_patterns)} stuck patterns, "
            f"{len(self.profile.fingerprint.key_screens)} key screens"
        )


class ProfileRepository:
    """Loads profiles from disk. Products and tenants are separate directories
    because they are reviewed by different people: a product profile is owned by
    whoever understands the vendor app, a tenant overlay by whoever onboarded
    that institution."""

    def __init__(self, root: Path | str = "profiles") -> None:
        self.root = Path(root)

    def _find(self, ref: str) -> Path:
        name, _, version = ref.partition("@")
        candidates = [
            self.root / "tenants" / f"{name}.yaml",
            self.root / "products" / f"{name}-{version}.yaml" if version else None,
            self.root / "products" / f"{name}.yaml",
        ]
        for path in filter(None, candidates):
            if path.exists():
                return path
        raise ProfileNotFound(
            f"no profile '{ref}' under {self.root} "
            f"(looked for {[str(c) for c in candidates if c]})"
        )

    def load_raw(self, ref: str) -> AppProfile:
        path = self._find(ref)
        try:
            data = yaml.safe_load(path.read_text()) or {}
            return AppProfile.model_validate(data)
        except ValidationError as exc:
            raise ProfileError(f"{path} failed validation: {exc}") from exc

    def resolve(self, ref: str) -> ResolvedProfile:
        chain: list[AppProfile] = []
        current = ref
        seen: list[str] = []

        while current:
            profile = self.load_raw(current)
            # Compare the LOADED profile's canonical ref, not the requested
            # string: "demo-cu" and "demo-cu@1" name the same profile, so
            # de-duplicating on the request would miss a cycle and let the depth
            # cap report it as an over-deep chain instead.
            if profile.ref in seen:
                raise ProfileError(
                    f"circular profile inheritance: {' <- '.join([*seen, profile.ref])}"
                )
            seen.append(profile.ref)
            chain.append(profile)
            current = profile.extends

            if len(chain) > MAX_INHERITANCE_DEPTH:
                raise ProfileError(
                    f"profile inheritance deeper than {MAX_INHERITANCE_DEPTH} "
                    f"({' <- '.join(p.ref for p in chain)}). A tenant needing more than one "
                    f"overlay needs its own recording, not a deeper chain."
                )

        chain.reverse()  # base first
        merged = chain[0]
        for overlay in chain[1:]:
            merged = _merge(merged, overlay)

        return ResolvedProfile(
            profile=merged,
            lineage=tuple(p.ref for p in chain),
            hash=_hash_profile(merged),
        )


def _merge(base: AppProfile, overlay: AppProfile) -> AppProfile:
    base_d = base.model_dump(mode="json", by_alias=True)
    over_d = overlay.model_dump(mode="json", by_alias=True, exclude_unset=True)

    merged = dict(base_d)
    # Identity always comes from the overlay -- the effective profile IS the
    # tenant's, inheriting the product's facts.
    merged["profile"] = over_d.get("profile", base_d["profile"])
    merged["extends"] = overlay.extends

    for section in ("surface", "auth", "sensitivity", "fingerprint", "overrides"):
        merged[section] = _merge_mapping(base_d.get(section) or {}, over_d.get(section) or {})

    disabled = set(overlay.overrides.disabled_recoveries)
    merged["recoveries"] = [
        r for r in _merge_keyed(base_d.get("recoveries") or [],
                                over_d.get("recoveries") or [], key="id")
        if r["id"] not in disabled
    ]
    merged["hard_failures"] = _merge_keyed(
        base_d.get("hard_failures") or [], over_d.get("hard_failures") or [], key="code"
    )
    merged["stuck_patterns"] = _merge_keyed(
        base_d.get("stuck_patterns") or [], over_d.get("stuck_patterns") or [], key="id"
    )
    merged["fingerprint"]["key_screens"] = _merge_keyed(
        (base_d.get("fingerprint") or {}).get("key_screens") or [],
        (over_d.get("fingerprint") or {}).get("key_screens") or [],
        key="id",
    )
    return AppProfile.model_validate(merged)


def _merge_mapping(base: dict, overlay: dict) -> dict:
    """Shallow per-key merge, recursing one level into nested mappings.

    Deliberately shallow: an overlay that sets `surface.timeouts.default_ms`
    should not silently drop the other timeouts, but an overlay that sets
    `overrides.label_overrides` replaces that map wholesale, which is what an
    author expects when they write it out.
    """
    out = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict) and key not in _REPLACE_WHOLE:
            out[key] = {**out[key], **value}
        else:
            out[key] = value
    return out


_REPLACE_WHOLE = {"label_overrides", "locator_overrides", "param_defaults",
                  "output_redaction_defaults", "credentials"}


def _merge_keyed(base: list[dict], overlay: list[dict], *, key: str) -> list[dict]:
    """Same id replaces, new id appends, base order preserved."""
    by_id = {item[key]: item for item in base}
    order = [item[key] for item in base]
    for item in overlay:
        if item[key] not in by_id:
            order.append(item[key])
        by_id[item[key]] = item
    return [by_id[k] for k in order]


def _hash_profile(profile: AppProfile) -> str:
    data = profile.model_dump(mode="json", by_alias=True)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()[:32]


# --------------------------------------------------------------------------
# applying a tenant's overrides to an artifact
# --------------------------------------------------------------------------

@dataclass
class SpecializationReport:
    """What the overlay actually changed. Journaled with every replay, because
    "which locator did this tenant override" must be answerable from evidence."""

    capability_ref: str
    profile_lineage: tuple[str, ...]
    profile_hash: str
    locator_overrides_applied: list[str] = field(default_factory=list)
    param_defaults_applied: dict[str, str] = field(default_factory=dict)
    recoveries_inherited: list[str] = field(default_factory=list)
    unused_overrides: list[str] = field(default_factory=list)
    """Overrides that matched no target in this artifact -- almost always a
    stale override left behind after a re-record, and worth surfacing."""


def specialize(
    artifact: CapabilityArtifact, resolved: ResolvedProfile
) -> tuple[CapabilityArtifact, SpecializationReport]:
    """Apply a tenant's overrides, producing the effective artifact to run.

    The stored artifact is never mutated. What runs is base + overlay, computed
    fresh, reported, and discarded -- so there is exactly one recorded flow no
    matter how many tenants run it.
    """
    report = SpecializationReport(
        capability_ref=artifact.ref,
        profile_lineage=resolved.lineage,
        profile_hash=resolved.hash,
        recoveries_inherited=[r.id for r in resolved.profile.recoveries],
    )

    overrides = resolved.profile.overrides
    prefix = f"{artifact.capability.id}:"
    relevant = {
        k.split(":", 1)[1]: v for k, v in overrides.locator_overrides.items() if k.startswith(prefix)
    }
    report.unused_overrides = [
        k for k in overrides.locator_overrides
        if k.startswith(prefix) and k.split(":", 1)[1] not in set(artifact.target_ids)
    ]

    data = artifact.model_dump(mode="json", by_alias=True)

    def swap(bundle: dict | None) -> dict | None:
        if not bundle:
            return bundle
        replacement = relevant.get(bundle["target_id"])
        if replacement is None:
            return bundle
        report.locator_overrides_applied.append(bundle["target_id"])
        return replacement.model_dump(mode="json", by_alias=True)

    for step in data["steps"]:
        step["action"]["target"] = swap(step["action"]["target"])
    for extraction in data["extractions"]:
        extraction["target"] = swap(extraction["target"])
    for outcome in data["outcomes"]:
        outcome["message_from"] = swap(outcome["message_from"])
    for recovery in data["recoveries"]:
        for action in recovery["actions"]:
            action["target"] = swap(action["target"])

    for name, default in overrides.param_defaults.items():
        if name in data["inputs"]:
            data["inputs"][name]["default"] = default
            report.param_defaults_applied[name] = default

    # An overridden locator changes the flow's bytes, so the sealed hash no
    # longer describes it. Cleared rather than recomputed: the specialized form
    # is derived and ephemeral, and only the recorded artifact gets to be sealed.
    data["content_hash"] = ""
    return CapabilityArtifact.model_validate(data), report
