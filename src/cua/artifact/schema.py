"""The capability artifact -- what a discovery run turns into.

This is the contract between three audiences, and every design choice here is
about serving all three at once:

  * **A calling AI agent** needs typed inputs, typed outputs, and a result it can
    branch on. It never sees the UI.
  * **A human reviewer** needs to read the flow and judge whether it is safe to
    run unattended -- which means intent in words, declared risk, and the
    reasoning behind each locator.
  * **The replay engine** needs enough to execute with no model in the loop:
    ordered steps, checkpoints, declared exceptional states, and recoveries.

Two things make this more than a step list:

  `outcomes[]` -- declared business outcomes with detection conditions. "No such
  member" is a legitimate answer the caller needs, not a crash. Because outcomes
  are declared data checked before checkpoints, it is structurally impossible
  for one to surface as a failure.

  `binding` -- the artifact binds to a VENDOR PRODUCT, not a tenant. One
  recording serves every tenant running that product; per-tenant differences are
  an overlay (see profiles/). Re-recording per tenant does not scale to hundreds
  of institutions, so the schema refuses to encode a tenant as identity.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from ..conditions.model import Condition
from ..locators.model import LocatorBundle
from ..surfaces.base import ActionType, RiskTier, SurfaceKind

SCHEMA_VERSION = "1.0"
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
_IDENT = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")


class ApprovalState(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    DEPRECATED = "deprecated"
    DRIFTED = "drifted"
    """Set automatically when the surface fingerprint stops matching. A drifted
    capability is not deleted -- it is demoted out of unattended use pending
    review, because the flow is probably still correct."""


class CapabilityRisk(str, Enum):
    """Risk of the capability as a whole -- the unit a caller reasons about."""

    READ_ONLY = "read_only"
    WRITES_REVERSIBLE = "writes_reversible"
    WRITES_IRREVERSIBLE = "writes_irreversible"


class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    MONEY = "money"
    DATE = "date"


class Redaction(str, Enum):
    NONE = "none"
    LAST4 = "last4"
    MASK = "mask"
    DROP = "drop"


# --------------------------------------------------------------------------
# inputs / outputs -- the agent-facing contract
# --------------------------------------------------------------------------

class ParamSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: ParamType = ParamType.STRING
    description: str = ""
    required: bool = True
    default: Any = None
    pattern: str | None = None
    enum: tuple[str, ...] | None = None
    minimum: float | None = None
    maximum: float | None = None
    example: str | None = None

    sensitive: bool = False
    """Never serialized into an artifact, journal, screenshot, or prompt. The
    value exists only in memory for the duration of one replay."""

    volatile: bool = False
    """The value was time-dependent when it was recorded (Part 5.3).

    A date typed during discovery is correct exactly once. Freezing it as a
    literal produces a capability that back-dates every future run to the day it
    was recorded -- which an application will usually accept, silently, and
    which nothing downstream would ever flag. So the recorder promotes such a
    value to a declared input and marks it here; `default` keeps the recorded
    value so the flow still runs unattended, and this flag is what tells a
    reviewer, and a caller reading `input_json_schema()`, that the default is
    stale by construction rather than merely unspecified."""

    def json_schema(self) -> dict:
        """JSON Schema fragment, so a calling agent can discover this capability
        as a typed tool without a second hand-written description."""
        base: dict[str, Any] = {
            ParamType.STRING: {"type": "string"},
            ParamType.INTEGER: {"type": "integer"},
            ParamType.NUMBER: {"type": "number"},
            ParamType.BOOLEAN: {"type": "boolean"},
            ParamType.MONEY: {"type": "string", "description": "decimal amount, e.g. 50.00"},
            ParamType.DATE: {"type": "string", "format": "date"},
        }[self.type].copy()
        description = self.description
        if self.volatile:
            # Said in the schema, not only in the artifact, because the calling
            # agent is the one holding a stale default.
            description = (f"{description} Time-dependent: the recorded default was "
                           f"correct on the day this was recorded and should be supplied "
                           f"by the caller.").strip()
        if description:
            base["description"] = (
                f"{base.get('description', '')} {description}".strip()
            )
        if self.pattern:
            base["pattern"] = self.pattern
        if self.enum:
            base["enum"] = list(self.enum)
        if self.minimum is not None:
            base["minimum"] = self.minimum
        if self.maximum is not None:
            base["maximum"] = self.maximum
        if self.default is not None:
            base["default"] = self.default
        return base


class OutputSpec(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: ParamType = ParamType.STRING
    description: str = ""
    optional: bool = False

    redact: Redaction = Redaction.NONE
    """How this value appears in LOGS AND EVIDENCE. It is still returned in full
    to the caller -- an agent that asked for an account number needs it. The
    redaction boundary is persistence, not the return value."""

    def json_schema(self) -> dict:
        d = ParamSpec(type=self.type, description=self.description).json_schema()
        return d


# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------

class ValueSource(BaseModel):
    """Where a step's value comes from. Never a baked-in literal of real data.

    `param` keeps recorded runs from freezing one member's id into the flow, and
    `credential` keeps secrets out of the artifact entirely -- the profile holds
    a reference, the runtime resolves it, nothing persists it.
    """

    model_config = ConfigDict(frozen=True)

    param: str | None = None
    literal: str | None = None
    credential: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "ValueSource":
        given = [x for x in (self.param, self.literal, self.credential) if x is not None]
        if len(given) != 1:
            raise ValueError("value_from needs exactly one of param/literal/credential")
        return self

    def describe(self) -> str:
        if self.param:
            return f"{{{self.param}}}"
        if self.credential:
            return f"<credential:{self.credential}>"
        return repr(self.literal)


class StepAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    type: ActionType
    target: LocatorBundle | None = None
    value_from: ValueSource | None = None
    url_template: str | None = None
    """May reference {base_url} and any declared input, so a recorded URL
    generalizes across tenants instead of pinning one host."""

    key: str | None = None
    wait_for: Condition | None = None
    scroll_by: int | None = None

    @model_validator(mode="after")
    def _shape(self) -> "StepAction":
        if self.type in (ActionType.CLICK, ActionType.FILL, ActionType.SELECT_OPTION):
            if self.target is None:
                raise ValueError(f"{self.type.value} requires a target")
        if self.type in (ActionType.FILL, ActionType.SELECT_OPTION) and self.value_from is None:
            raise ValueError(f"{self.type.value} requires value_from")
        if self.type is ActionType.NAVIGATE and not self.url_template:
            raise ValueError("navigate requires url_template")
        if self.type is ActionType.PRESS_KEY and not self.key:
            raise ValueError("press_key requires key")
        return self


class Step(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    intent: str
    """What this step is FOR, in words, carried over from the model's stated
    reason during discovery. This is what makes the artifact reviewable without
    the raw transcript -- which is deliberately not persisted, because a
    transcript contains observations of regulated data."""

    action: StepAction
    preconditions: Condition | None = None
    checkpoint: Condition | None = None
    """Asserted after the action. Without it, replay is assuming the click
    worked rather than confirming it."""

    completion_witness: Condition | None = None
    """R-M6-1. Observable proof that this step's effect already landed.

    Only meaningful on an irreversible step, and only consulted when a session
    drop has rewound the flow to a point that would replay it. The default is
    `None`, which means default-deny: the engine escalates with
    IRREVERSIBLE_INTERRUPTED rather than guess whether the write took effect.

    With a witness declared, the guess becomes an observation -- "a sub-account
    with this opening balance now appears on the member's record" -- and the
    engine decides deterministically instead of paging somebody. It is an
    ordinary `Condition`, the same vocabulary checkpoints and outcomes use, so
    it costs no new concepts and it is reviewable in the same read.

    Honest limit, stated where the field is declared: a witness makes SKIPPING
    the step safe. It does not make the rest of the flow possible -- when the
    screen the write produced carried data later steps needed, those steps will
    fail, and that failure is correct. See `Step.optional` for the artifact-side
    way to say "and this one no longer applies".
    """

    risk: RiskTier = RiskTier.READ_ONLY
    requires_confirmation: bool = False
    requires_human: bool = False
    timeout_ms: int = 8000
    retries: int = 2
    optional: bool = False
    """A step that may legitimately not apply on some tenants (an extra consent
    screen). A failed precondition skips it instead of failing the run."""


class ParseSpec(BaseModel):
    """How to turn screen text into a typed value.

    Extraction without parsing returns "$4,210.75" as a string and pushes the
    problem onto every caller. The artifact declares the shape, so the caller
    gets a number.
    """

    model_config = ConfigDict(frozen=True)

    type: Literal["string", "money", "integer", "number", "date", "regex"] = "string"
    locale: str = "en_US"
    pattern: str | None = None
    group: int = 0
    date_format: str | None = None


class Extraction(BaseModel):
    model_config = ConfigDict(frozen=True)

    output: str
    target: LocatorBundle
    source: Literal["name", "value", "text"] = "name"
    parse: ParseSpec | None = None
    after_step: str | None = None
    """Read at this point in the flow rather than at the end -- necessary when
    the value is only on screen mid-flow."""


# --------------------------------------------------------------------------
# exceptional states
# --------------------------------------------------------------------------

class OutcomeSpec(BaseModel):
    """A declared, legitimate, non-success result.

    The single most important idea in this schema. "No such member" is an answer
    the caller asked for; conflating it with a crash is the mistake the brief
    calls out by name. Because these are declared data evaluated before the
    step's checkpoint, a declared outcome can never be reported as a failure.
    """

    model_config = ConfigDict(frozen=True)

    code: str
    detect: Condition
    description: str = ""
    after_step: str | None = None
    """Only check after this step. Unset means check after every step -- correct
    for conditions like a permission wall that can appear anywhere."""

    message_from: LocatorBundle | None = None
    """Where to read the app's own wording, so the caller gets the institution's
    message rather than ours. Passed through the redactor before it is logged."""

    terminal: bool = True


class RecoverySpec(BaseModel):
    """A known, bounded, automatic fix for a condition that is not a failure.

    Recoveries are deliberately NOT a result variant. A dismissed interstitial
    or a retried transient load is something replay handles and journals; it
    only becomes a failure when the attempt budget is exhausted.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    trigger: Condition
    actions: tuple[StepAction, ...] = ()
    max_attempts: int = 2
    description: str = ""


class SuccessSpec(BaseModel):
    model_config = ConfigDict(frozen=True)
    checkpoint: Condition
    description: str = ""


# --------------------------------------------------------------------------
# identity, binding, provenance
# --------------------------------------------------------------------------

class CapabilityMeta(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    version: str = "1.0.0"
    title: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    owner: str = ""
    approval_state: ApprovalState = ApprovalState.DRAFT
    risk_tier: CapabilityRisk = CapabilityRisk.READ_ONLY
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _ids(self) -> "CapabilityMeta":
        if not _IDENT.match(self.id):
            raise ValueError(f"capability id must be dotted snake_case, got {self.id!r}")
        if not _SEMVER.match(self.version):
            raise ValueError(f"version must be semver, got {self.version!r}")
        return self


class ProductRef(BaseModel):
    model_config = ConfigDict(frozen=True)
    vendor: str
    product: str
    version_range: str = "*"


class RecordedAgainst(BaseModel):
    """Where this was recorded -- provenance, deliberately NOT identity.

    A capability recorded on one tenant is expected to run on its siblings. This
    block says which one it was learned from, so drift can be attributed.
    """

    model_config = ConfigDict(frozen=True)

    tenant: str = ""
    surface_fingerprint: dict[str, str] = Field(default_factory=dict)
    surface_kind: SurfaceKind = SurfaceKind.LEGACY_WEB


class Binding(BaseModel):
    model_config = ConfigDict(frozen=True)
    product: ProductRef
    app_profile_ref: str
    recorded_against: RecordedAgainst = Field(default_factory=RecordedAgainst)


class Provenance(BaseModel):
    model_config = ConfigDict(frozen=True)

    recorded_from_run: str = ""
    model: str = ""
    discovery_goal: str = ""
    recorded_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    steps_observed: int = 0
    steps_pruned: int = 0
    verified_by_replay: bool = False
    """Set when the recorder replayed the fresh artifact and it passed. An
    unverified artifact is a hypothesis."""


class Telemetry(BaseModel):
    """Mutable execution history. Stored beside the artifact, never inside it --
    see ArtifactStore. Recording a replay must not change the content hash.

    Counters describe ONE flow. The sidecar is addressed by ref (id@version) but
    bound to `artifact_hash`, because those two can come apart: re-recording and
    force-overwriting a version replaces the flow while keeping the ref. Without
    the hash binding, the new flow would silently inherit the old one's success
    history and look proven before it had ever run -- and these counters feed
    both the drift signal and the judgment of whether a capability is reliable
    enough for unattended replay.
    """

    model_config = ConfigDict(frozen=True)

    artifact_hash: str = ""
    """Content hash of the flow these counters describe."""

    reset_count: int = 0
    previous_hash: str = ""
    reset_at: datetime | None = None
    """Set when counters were discarded because the flow changed underneath
    them. Kept rather than deleted: "this capability was re-recorded on the 3rd"
    is exactly the context you want when reviewing a reliability number."""

    replays: int = 0
    successes: int = 0
    business_outcomes: dict[str, int] = Field(default_factory=dict)
    failures: dict[str, int] = Field(default_factory=dict)
    escalations: int = 0
    resolved_by: dict[str, int] = Field(default_factory=dict)
    degraded_resolutions: int = 0
    last_replay_at: datetime | None = None
    per_tenant: dict[str, dict[str, int]] = Field(default_factory=dict)
    """Per-tenant counters. The drift signal: one tenant's failure or
    degradation rate diverging from its siblings points at that tenant's
    variant, not at the capability."""

    def is_stale_for(self, content_hash: str) -> bool:
        """True when these counters describe a different flow than `content_hash`."""
        return bool(self.artifact_hash) and bool(content_hash) and self.artifact_hash != content_hash

    @property
    def success_rate(self) -> float:
        return self.successes / self.replays if self.replays else 0.0

    @property
    def degradation_rate(self) -> float:
        total = sum(self.resolved_by.values())
        return self.degraded_resolutions / total if total else 0.0


# --------------------------------------------------------------------------
# the artifact
# --------------------------------------------------------------------------

class CapabilityArtifact(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: str = SCHEMA_VERSION
    capability: CapabilityMeta
    binding: Binding
    inputs: dict[str, ParamSpec] = Field(default_factory=dict)
    outputs: dict[str, OutputSpec] = Field(default_factory=dict)
    steps: tuple[Step, ...] = Field(min_length=1)
    extractions: tuple[Extraction, ...] = ()
    outcomes: tuple[OutcomeSpec, ...] = ()
    recoveries: tuple[RecoverySpec, ...] = ()
    success: SuccessSpec
    provenance: Provenance | None = None
    content_hash: str = ""

    # ---- integrity -------------------------------------------------

    # Excluded from the hash: workflow state that changes without the flow
    # changing. The hash answers "did the executable flow change", not "did
    # somebody approve it" -- otherwise approving an artifact would look
    # identical to tampering with one.
    _HASH_EXCLUDE = {"content_hash"}

    def hashable(self) -> dict:
        # `exclude_defaults` is what makes the format additively evolvable, and
        # the reason is worth stating: without it, adding ANY optional field to
        # any nested model re-serializes every artifact ever sealed and breaks
        # its integrity check -- not because the flow changed but because the
        # schema grew a field nobody set. That happened, on a one-line addition
        # to `ElementMatch`, and every committed capability failed to load.
        #
        # A field nobody set says nothing, so it does not belong in a hash of
        # what the flow says. Two artifacts that differ only in a field
        # explicitly written to its own default mean the same thing and now hash
        # the same, which is the correct answer rather than a tolerated one.
        # `SCHEMA_VERSION` remains the signal for a change that is NOT additive.
        data = self.model_dump(mode="json", by_alias=True, exclude=self._HASH_EXCLUDE,
                               exclude_defaults=True)
        # `exclude_defaults` can drop these keys entirely, so pop defensively.
        data.get("capability", {}).pop("approval_state", None)
        data.get("capability", {}).pop("created_at", None)
        data.pop("provenance", None)
        return data

    def compute_hash(self) -> str:
        canonical = json.dumps(self.hashable(), sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()

    def seal(self) -> "CapabilityArtifact":
        return self.model_copy(update={"content_hash": self.compute_hash()})

    def verify_hash(self) -> bool:
        return bool(self.content_hash) and self.content_hash == self.compute_hash()

    # ---- identity --------------------------------------------------

    @property
    def ref(self) -> str:
        return f"{self.capability.id}@{self.capability.version}"

    @property
    def target_ids(self) -> list[str]:
        """Every addressable locator in the flow -- the set a tenant override
        may key on."""
        ids = [s.action.target.target_id for s in self.steps if s.action.target]
        ids += [e.target.target_id for e in self.extractions]
        ids += [o.message_from.target_id for o in self.outcomes if o.message_from]
        for rec in self.recoveries:
            ids += [a.target.target_id for a in rec.actions if a.target]
        return ids

    # ---- agent-facing contract -------------------------------------

    def input_json_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {k: v.json_schema() for k, v in self.inputs.items()},
            "required": [k for k, v in self.inputs.items() if v.required and v.default is None],
            "additionalProperties": False,
        }

    def output_json_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {k: v.json_schema() for k, v in self.outputs.items()},
            "required": [k for k, v in self.outputs.items() if not v.optional],
        }

    def as_tool_definition(self) -> dict:
        """Anthropic tool-use shape, so a calling agent can invoke this
        capability by name with typed args."""
        outcomes = ", ".join(o.code for o in self.outcomes)
        desc = self.capability.description or self.capability.title
        if outcomes:
            desc += f" May return these business outcomes instead of success: {outcomes}."
        return {
            "name": self.capability.id.replace(".", "_"),
            "description": desc,
            "input_schema": self.input_json_schema(),
        }

    def validate_params(self, params: dict) -> dict:
        """Check caller-supplied parameters BEFORE the browser is touched.

        A bad parameter is a caller error, reported as PARAM_INVALID without
        having opened a session or half-completed a flow.
        """
        errors: list[str] = []
        resolved: dict[str, Any] = {}

        for name, spec in self.inputs.items():
            if name not in params or params[name] is None:
                if spec.default is not None:
                    resolved[name] = spec.default
                elif spec.required:
                    errors.append(f"missing required parameter '{name}'")
                continue

            value = params[name]
            if spec.type in (ParamType.STRING, ParamType.MONEY, ParamType.DATE):
                value = str(value)
                if spec.pattern and not re.match(spec.pattern, value):
                    shown = "<redacted>" if spec.sensitive else repr(value)
                    errors.append(f"'{name}'={shown} does not match {spec.pattern}")
                if spec.enum and value not in spec.enum:
                    errors.append(f"'{name}' must be one of {list(spec.enum)}")
            elif spec.type in (ParamType.INTEGER, ParamType.NUMBER):
                try:
                    value = int(value) if spec.type is ParamType.INTEGER else float(value)
                except (TypeError, ValueError):
                    errors.append(f"'{name}' must be a {spec.type.value}")
                    continue
                if spec.minimum is not None and value < spec.minimum:
                    errors.append(f"'{name}' must be >= {spec.minimum}")
                if spec.maximum is not None and value > spec.maximum:
                    errors.append(f"'{name}' must be <= {spec.maximum}")
            elif spec.type is ParamType.BOOLEAN:
                value = bool(value)
            resolved[name] = value

        unknown = set(params) - set(self.inputs)
        if unknown:
            errors.append(f"unknown parameter(s): {sorted(unknown)}")

        if errors:
            raise ParamValidationError(errors)
        return resolved

    # ---- structural integrity --------------------------------------

    @model_validator(mode="after")
    def _coherent(self) -> "CapabilityArtifact":
        step_ids = [s.id for s in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("step ids must be unique")

        codes = [o.code for o in self.outcomes]
        if len(codes) != len(set(codes)):
            raise ValueError("outcome codes must be unique")

        rec_ids = [r.id for r in self.recoveries]
        if len(rec_ids) != len(set(rec_ids)):
            raise ValueError("recovery ids must be unique")

        for extraction in self.extractions:
            if extraction.output not in self.outputs:
                raise ValueError(
                    f"extraction writes undeclared output '{extraction.output}'"
                )
            if extraction.after_step and extraction.after_step not in step_ids:
                raise ValueError(f"extraction references unknown step '{extraction.after_step}'")

        declared = {o for o in self.outputs if not self.outputs[o].optional}
        produced = {e.output for e in self.extractions}
        if missing := declared - produced:
            raise ValueError(f"outputs declared but never extracted: {sorted(missing)}")

        for step in self.steps:
            vs = step.action.value_from
            if vs and vs.param and vs.param not in self.inputs:
                raise ValueError(f"step '{step.id}' uses undeclared input '{vs.param}'")

        for outcome in self.outcomes:
            if outcome.after_step and outcome.after_step not in step_ids:
                raise ValueError(
                    f"outcome '{outcome.code}' references unknown step '{outcome.after_step}'"
                )

        # A witness is only ever consulted for an irreversible step, so one
        # declared anywhere else is a misunderstanding rather than a harmless
        # extra -- it reads as protection that will never run.
        for step in self.steps:
            if step.completion_witness is not None and step.risk is not RiskTier.SUBMIT_IRREVERSIBLE:
                raise ValueError(
                    f"step '{step.id}' declares a completion_witness but is "
                    f"'{step.risk.value}'. A witness answers 'did this write land', which "
                    f"is only a question for submit_irreversible steps."
                )

        # A capability that performs an irreversible action must say so at the
        # top level, because that is the field a caller and a reviewer gate on.
        irreversible = any(s.risk is RiskTier.SUBMIT_IRREVERSIBLE for s in self.steps)
        if irreversible and self.capability.risk_tier is not CapabilityRisk.WRITES_IRREVERSIBLE:
            raise ValueError(
                "a step is marked submit_irreversible but capability.risk_tier is "
                f"'{self.capability.risk_tier.value}'; the declared risk must not understate "
                "what the flow actually does"
            )
        return self


class ParamValidationError(Exception):
    failure_class = "PARAM_INVALID"

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors
