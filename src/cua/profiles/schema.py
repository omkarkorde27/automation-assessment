"""AppProfile -- what an application is, as opposed to what a capability does.

The split is the multi-tenant answer. Session timeout, the generic error banner,
the marketing interstitial, which fields hold regulated data, how to log back in
-- none of that belongs to any one capability. It is true of the *application*,
so it is declared once per vendor product and inherited by every capability
recorded against it.

Tenants then overlay. Hundreds of institutions run the same vendor product with
different branding, labels, and one extra consent screen; an overlay expresses
exactly that delta. Re-recording per tenant does not scale, and neither does a
fork per tenant.

Inheritance is capped at two levels (product -> tenant) on purpose. Deeper
chains stop being reviewable at this many tenants, and a tenant that needs more
than an overlay is telling you it needs its own recording -- which `fingerprint`
is there to detect.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..artifact.schema import Step, StepAction
from ..conditions.model import Condition, ElementMatch
from ..locators.model import LocatorBundle
from ..surfaces.base import SurfaceKind

PROFILE_SCHEMA_VERSION = "1.0"
# Allows a vault key fragment ("vault:secret/data/core#password"), which is
# how HashiCorp-style references address a field within a secret.
_CRED_REF = re.compile(r"^(env|keyring|vault):[A-Za-z0-9_./#-]+$")


class ProfileMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    version: str = "1"
    vendor: str = ""
    product: str = ""
    display_name: str = ""

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"


class FrameSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    name: str
    required: bool = True


class Timeouts(BaseModel):
    model_config = ConfigDict(frozen=True)
    default_ms: int = 8000
    navigation_ms: int = 15000
    slow_load_grace_ms: int = 20000
    """How long to tolerate a stalled load before calling it a failure rather
    than transient slowness. Per-app because "slow" is an app-specific fact."""


class Viewport(BaseModel):
    model_config = ConfigDict(frozen=True)
    width: int = 1280
    height: int = 900


class SurfaceSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: SurfaceKind = SurfaceKind.LEGACY_WEB
    base_url: str | None = None
    """Null in a product profile -- a product has no host. The tenant supplies
    it, and resolution requires it before the profile can be used."""

    frame_topology: tuple[FrameSpec, ...] = ()
    """Declared so a missing frame is a clear error rather than a silent
    fallback to the top document, which would act on the wrong screen."""

    viewport: Viewport = Field(default_factory=Viewport)
    timeouts: Timeouts = Field(default_factory=Timeouts)


class ReauthSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    enabled: bool = True
    max_attempts: int = 1
    resume_from: Literal["last_checkpoint", "start", "fail"] = "last_checkpoint"


class LoginRecipe(BaseModel):
    """Re-uses the artifact's `Step`, so the replay engine executes a login with
    the same executor, checkpoints, and locator resolution as any other step.
    A second code path for auth would be a second thing to get wrong."""

    model_config = ConfigDict(frozen=True)

    steps: tuple[Step, ...] = ()
    success: Condition | None = None


class AuthSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    credentials: dict[str, str] = Field(default_factory=dict)
    """REFERENCES ONLY -- "env:MOCKBANK_PASS". Never a literal. Profiles are
    committed to git, copied between environments, and shown in review; a
    secret in one is a secret leaked. Validated below."""

    login_recipe: LoginRecipe | None = None
    session_expiry_detector: Condition | None = None
    reauth: ReauthSpec = Field(default_factory=ReauthSpec)

    @model_validator(mode="after")
    def _refs_only(self) -> "AuthSpec":
        for name, ref in self.credentials.items():
            if not _CRED_REF.match(ref):
                raise ValueError(
                    f"credential '{name}' must be a reference like 'env:NAME', not a literal "
                    f"value. Profiles are committed to version control."
                )
        return self


class ProfileRecovery(BaseModel):
    """App-wide recovery, inherited by every capability on this product."""

    model_config = ConfigDict(frozen=True)

    id: str
    trigger: Condition
    actions: tuple[StepAction, ...] = ()
    max_attempts: int = 2
    description: str = ""


class HardFailure(BaseModel):
    """Terminal application fault. Never retried, never a business outcome."""

    model_config = ConfigDict(frozen=True)

    code: str
    detect: Condition
    description: str = ""


class StuckPattern(BaseModel):
    """A recognizable dead end, and what to tell the human who must unblock it.

    Consulted only after declared outcomes fail to match: if a capability
    declares PERMISSION_DENIED as a legitimate result, that wins and no
    intervention is raised. These are the fallback for states no capability
    anticipated -- which is exactly when a person is needed.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    detect: Condition
    reason_class: str
    human_message: str
    description: str = ""


class NodeSensitivityRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    match: ElementMatch
    classification: str
    mask: tuple[Literal["screenshot", "text"], ...] = ("screenshot", "text")


class SensitivitySpec(BaseModel):
    """Where the regulated data is.

    Regex detectors are a net, not a guarantee -- they catch an SSN-shaped
    string anywhere. These node rules are the real mechanism: the institution's
    own screens declare which fields hold what, so masking does not depend on a
    value happening to look recognizable.
    """

    model_config = ConfigDict(frozen=True)

    node_rules: tuple[NodeSensitivityRule, ...] = ()
    text_detectors: tuple[str, ...] = ()
    output_redaction_defaults: dict[str, str] = Field(default_factory=dict)
    never_screenshot: tuple[str, ...] = ()


class IncludeSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    roles: tuple[str, ...] = ()
    name_pattern: str | None = None
    normalize: tuple[str, ...] = ("collapse_ws", "casefold")


class KeyScreen(BaseModel):
    """A screen whose structure is fingerprinted as a drift canary.

    `include` should select LABELS, not data. A fingerprint that varies with
    which member you looked up reports drift on every run and is worthless.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    reach: Condition
    include: IncludeSpec = Field(default_factory=IncludeSpec)


class DriftPolicy(BaseModel):
    model_config = ConfigDict(frozen=True)
    on_mismatch: Literal["demote_to_draft", "warn", "fail"] = "demote_to_draft"
    alert: bool = True


class FingerprintSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    algorithm: Literal["sha256_sorted_role_name"] = "sha256_sorted_role_name"
    key_screens: tuple[KeyScreen, ...] = ()
    drift_policy: DriftPolicy = Field(default_factory=DriftPolicy)


class Overrides(BaseModel):
    """The tenant delta. Everything here narrows or retargets; nothing here
    changes what the capability means."""

    model_config = ConfigDict(frozen=True)

    label_overrides: dict[str, str] = Field(default_factory=dict)
    locator_overrides: dict[str, LocatorBundle] = Field(default_factory=dict)
    """Keyed "capability_id:target_id". Replaces a WHOLE bundle rather than
    patching candidates, so an override is reviewable as one unit and cannot
    half-apply."""

    param_defaults: dict[str, str] = Field(default_factory=dict)
    disabled_recoveries: tuple[str, ...] = ()


class AppProfile(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = PROFILE_SCHEMA_VERSION
    profile: ProfileMeta
    extends: str | None = None
    surface: SurfaceSpec = Field(default_factory=SurfaceSpec)
    auth: AuthSpec = Field(default_factory=AuthSpec)
    recoveries: tuple[ProfileRecovery, ...] = ()
    hard_failures: tuple[HardFailure, ...] = ()
    stuck_patterns: tuple[StuckPattern, ...] = ()
    sensitivity: SensitivitySpec = Field(default_factory=SensitivitySpec)
    fingerprint: FingerprintSpec = Field(default_factory=FingerprintSpec)
    overrides: Overrides = Field(default_factory=Overrides)

    @property
    def ref(self) -> str:
        return self.profile.ref

    @model_validator(mode="after")
    def _unique_ids(self) -> "AppProfile":
        for label, items in (
            ("recoveries", [r.id for r in self.recoveries]),
            ("hard_failures", [h.code for h in self.hard_failures]),
            ("stuck_patterns", [s.id for s in self.stuck_patterns]),
            ("key_screens", [k.id for k in self.fingerprint.key_screens]),
        ):
            if len(items) != len(set(items)):
                raise ValueError(f"{label} ids must be unique within a profile")
        return self
