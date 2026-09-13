"""The artifact contract: sealing, integrity, versioning, telemetry, validation.

Two properties are load-bearing and get the most attention here:

  * the content hash covers the EXECUTABLE FLOW and nothing else, so approving
    an artifact or recording a replay never looks like tampering with one;
  * structural validators refuse artifacts that would fail confusingly at
    runtime -- an undeclared output, an unknown input, an understated risk.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from cua.artifact import (
    ApprovalState,
    ArtifactError,
    ArtifactIntegrityError,
    ArtifactNotFound,
    ArtifactStore,
    CapabilityArtifact,
    CapabilityRisk,
    ParamValidationError,
)
from cua.artifact.schema import (
    CapabilityMeta,
    OutputSpec,
    ParamType,
    StepAction,
    ValueSource,
)
from cua.conditions.model import ElementCondition, ElementMatch, NotCondition
from factories import MEMBER_ID_FIELD, lookup_balance_artifact


@pytest.fixture
def store(tmp_path):
    return ArtifactStore(tmp_path / "capabilities")


# --------------------------------------------------------------------------
# sealing and integrity
# --------------------------------------------------------------------------

def test_seal_then_verify():
    sealed = lookup_balance_artifact().seal()
    assert sealed.content_hash.startswith("sha256:")
    assert sealed.verify_hash()


def test_hash_survives_a_json_round_trip():
    sealed = lookup_balance_artifact().seal()
    reloaded = CapabilityArtifact.model_validate_json(json.dumps(sealed.model_dump(mode="json")))
    assert reloaded.content_hash == sealed.content_hash
    assert reloaded.verify_hash()


def test_approving_does_not_change_the_hash():
    """Otherwise a review action would be indistinguishable from tampering."""
    sealed = lookup_balance_artifact().seal()
    approved = sealed.model_copy(
        update={"capability": sealed.capability.model_copy(
            update={"approval_state": ApprovalState.APPROVED})}
    )
    assert approved.compute_hash() == sealed.compute_hash()
    assert approved.verify_hash()


def test_changing_the_flow_changes_the_hash():
    a = lookup_balance_artifact().seal()
    steps = list(a.steps)
    steps[1] = steps[1].model_copy(update={"intent": "something else entirely"})
    b = a.model_copy(update={"steps": tuple(steps)})
    assert b.compute_hash() != a.content_hash
    assert not b.verify_hash()


def test_changing_a_locator_changes_the_hash():
    a = lookup_balance_artifact().seal()
    steps = list(a.steps)
    swapped = MEMBER_ID_FIELD.model_copy(update={"allow_unstable": True})
    steps[1] = steps[1].model_copy(
        update={"action": steps[1].action.model_copy(update={"target": swapped})}
    )
    assert a.model_copy(update={"steps": tuple(steps)}).compute_hash() != a.content_hash


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------

def test_save_and_load(store):
    path = store.save(lookup_balance_artifact())
    assert path.exists()
    loaded = store.load("member.lookup_balance@1.0.0")
    assert loaded.capability.title.startswith("Look up")
    assert loaded.verify_hash()


def test_saving_the_same_content_twice_is_idempotent(store):
    store.save(lookup_balance_artifact())
    store.save(lookup_balance_artifact())
    assert len(store.list()) == 1


def test_republishing_a_version_with_different_content_is_refused(store):
    """A ref is a published contract; callers hold it. Changing what it does
    without changing its version is how a caller runs something unreviewed."""
    store.save(lookup_balance_artifact())
    changed = lookup_balance_artifact()
    changed = changed.model_copy(
        update={"capability": changed.capability.model_copy(update={"title": "Different"})}
    )
    with pytest.raises(ArtifactError, match="already exists with different content"):
        store.save(changed)
    store.save(changed, overwrite=True)  # explicit is fine


def test_hand_edited_artifact_is_refused_loudly(store):
    """An artifact drives a bank's back office. Running a modified one silently
    is not an option."""
    path = store.save(lookup_balance_artifact())
    data = json.loads(path.read_text())
    data["steps"][1]["action"]["value_from"] = {"literal": "99999"}
    path.write_text(json.dumps(data))

    with pytest.raises(ArtifactIntegrityError, match="content hash mismatch"):
        store.load("member.lookup_balance@1.0.0")
    store.load("member.lookup_balance@1.0.0", verify=False)  # opt out deliberately


def test_negation_is_written_with_the_dsl_spelling_not_the_python_one(store):
    """`not` on disk, never `not_`.

    The DSL spells negation `not`; the Python field is `not_` because `not` is a
    keyword. Dumping without aliases writes `not_` into the artifact, so a
    system-written file and a hand-authored profile would spell the same
    operator differently and a reviewer would see a token that appears nowhere
    in the documented DSL. Both forms parse, which is why this needs a test
    rather than trusting a round-trip to catch it.
    """
    a = lookup_balance_artifact()
    negated = NotCondition(**{"not": ElementCondition(
        element=ElementMatch(role="dialog"), exists=True)})
    steps = list(a.steps)
    steps[0] = steps[0].model_copy(update={"preconditions": negated})
    store.save(a.model_copy(update={"steps": tuple(steps)}))

    raw = store.path_for("member.lookup_balance@1.0.0").read_text()
    assert '"not":' in raw
    # Key-scoped, not a bare substring: "not_" also occurs inside legitimate
    # identifiers such as the target_id "not_found_notice".
    assert '"not_":' not in raw

    reloaded = store.load("member.lookup_balance@1.0.0")
    assert isinstance(reloaded.steps[0].preconditions, NotCondition)
    assert reloaded.verify_hash()


def test_hashing_uses_the_canonical_spelling(store):
    """The hash is computed over the same bytes that get written, so a file can
    be re-hashed from disk without knowing how it was produced."""
    a = lookup_balance_artifact()
    negated = NotCondition(**{"not": ElementCondition(
        element=ElementMatch(role="dialog"), exists=True)})
    steps = list(a.steps)
    steps[0] = steps[0].model_copy(update={"preconditions": negated})
    sealed = a.model_copy(update={"steps": tuple(steps)}).seal()

    from_alias = CapabilityArtifact.model_validate(sealed.model_dump(mode="json", by_alias=True))
    from_field = CapabilityArtifact.model_validate(sealed.model_dump(mode="json"))
    assert from_alias.compute_hash() == from_field.compute_hash() == sealed.content_hash


def test_missing_artifact(store):
    with pytest.raises(ArtifactNotFound):
        store.load("member.nope@1.0.0")


def test_versioning(store):
    store.save(lookup_balance_artifact())
    assert store.next_version("member.lookup_balance") == "1.1.0"
    assert store.next_version("member.lookup_balance", "major") == "2.0.0"
    assert store.next_version("member.lookup_balance", "patch") == "1.0.1"
    assert store.next_version("member.brand_new") == "1.0.0"


def test_load_latest_orders_semver_numerically(store):
    for version in ("1.0.0", "1.9.0", "1.10.0"):
        a = lookup_balance_artifact()
        store.save(a.model_copy(
            update={"capability": a.capability.model_copy(update={"version": version})}))
    assert store.load_latest("member.lookup_balance").capability.version == "1.10.0"


def test_approved_only_lookup_gates_unattended_use(store):
    store.save(lookup_balance_artifact())
    with pytest.raises(ArtifactNotFound, match="no approved artifact"):
        store.load_latest("member.lookup_balance", approved_only=True)

    store.set_approval("member.lookup_balance@1.0.0", ApprovalState.APPROVED)
    found = store.load_latest("member.lookup_balance", approved_only=True)
    assert found.capability.approval_state is ApprovalState.APPROVED
    assert found.verify_hash(), "approval must not break the seal"


def test_resolve_ref_accepts_bare_id_or_pinned_version(store):
    store.save(lookup_balance_artifact())
    assert store.resolve_ref("member.lookup_balance").ref == "member.lookup_balance@1.0.0"
    assert store.resolve_ref("member.lookup_balance@1.0.0").ref == "member.lookup_balance@1.0.0"


# --------------------------------------------------------------------------
# telemetry sidecar
# --------------------------------------------------------------------------

def test_telemetry_accumulates_without_touching_the_artifact(store):
    ref = "member.lookup_balance@1.0.0"
    path = store.save(lookup_balance_artifact())
    before = path.read_text()

    store.record_replay(ref, status="success", tenant="demo-cu",
                        resolved_by={"anchor_relative": 2}, degraded=2)
    store.record_replay(ref, status="business_outcome", outcome_code="MEMBER_NOT_FOUND",
                        tenant="demo-cu")
    store.record_replay(ref, status="failed", failure_class="CHECKPOINT_FAILED",
                        tenant="valley-cu")

    t = store.telemetry(ref)
    assert t.replays == 3 and t.successes == 1
    assert t.business_outcomes["MEMBER_NOT_FOUND"] == 1
    assert t.failures["CHECKPOINT_FAILED"] == 1
    assert path.read_text() == before, "telemetry must not rewrite the sealed artifact"
    assert store.load(ref).verify_hash()


def test_telemetry_tracks_drift_signals_per_tenant(store):
    """Not just pass/fail -- which candidate resolved, per tenant. That is what
    shows one institution diverging before anything breaks."""
    ref = "member.lookup_balance@1.0.0"
    store.save(lookup_balance_artifact())
    store.record_replay(ref, status="success", tenant="demo-cu",
                        resolved_by={"role_name_exact": 3}, degraded=0)
    store.record_replay(ref, status="success", tenant="valley-cu",
                        resolved_by={"anchor_relative": 3}, degraded=3)

    t = store.telemetry(ref)
    assert t.degradation_rate == 0.5
    assert t.per_tenant["valley-cu"]["degraded"] == 3
    assert t.per_tenant["demo-cu"]["degraded"] == 0


def test_telemetry_defaults_when_absent(store):
    assert store.telemetry("member.lookup_balance@1.0.0").replays == 0


def test_telemetry_is_bound_to_the_flow_not_just_the_version(store):
    """A re-record at the same version must not inherit the old flow's history.

    The sidecar is addressed by ref, but a force-overwrite replaces the flow
    while keeping the ref. Inheriting those counters would show a capability as
    proven before it had ever run -- and reliability is part of the judgment of
    whether it may replay unattended.
    """
    ref = "member.lookup_balance@1.0.0"
    original = lookup_balance_artifact()
    store.save(original)
    for _ in range(5):
        store.record_replay(ref, status="success", tenant="demo-cu")

    proven = store.telemetry(ref)
    assert (proven.replays, proven.successes) == (5, 5)
    assert proven.artifact_hash == original.seal().content_hash

    # Re-record: same id, same version, different flow.
    rerecorded = original.model_copy(update={
        "steps": tuple(
            s.model_copy(update={"intent": "reworked after a UI change"}) if s.id == "s2" else s
            for s in original.steps
        )
    })
    store.save(rerecorded, overwrite=True)

    fresh = store.current_telemetry(ref)
    assert fresh.replays == 0 and fresh.successes == 0
    assert fresh.reset_count == 1
    assert fresh.previous_hash == original.seal().content_hash
    assert fresh.reset_at is not None

    store.record_replay(ref, status="success", tenant="demo-cu")
    after = store.telemetry(ref)
    assert (after.replays, after.successes) == (1, 1)
    assert after.artifact_hash == rerecorded.seal().content_hash
    assert after.previous_hash == original.seal().content_hash


def test_unchanged_flow_keeps_accumulating(store):
    """The reset must fire on a changed flow, not on every save."""
    ref = "member.lookup_balance@1.0.0"
    store.save(lookup_balance_artifact())
    store.record_replay(ref, status="success")
    store.save(lookup_balance_artifact(), overwrite=True)   # identical content
    store.record_replay(ref, status="success")

    t = store.telemetry(ref)
    assert t.replays == 2 and t.reset_count == 0


def test_approval_change_does_not_reset_counters(store):
    """Approval is excluded from the hash, so it must not look like a new flow."""
    ref = "member.lookup_balance@1.0.0"
    store.save(lookup_balance_artifact())
    store.record_replay(ref, status="success")
    store.set_approval(ref, ApprovalState.APPROVED)
    store.record_replay(ref, status="success")

    t = store.telemetry(ref)
    assert t.replays == 2 and t.reset_count == 0


# --------------------------------------------------------------------------
# the agent-facing contract
# --------------------------------------------------------------------------

def test_input_json_schema():
    schema = lookup_balance_artifact().input_json_schema()
    assert schema["required"] == ["member_id"]
    assert schema["properties"]["member_id"]["pattern"] == r"^\d{5}$"
    assert schema["additionalProperties"] is False


def test_tool_definition_advertises_business_outcomes():
    """A caller must be able to see that MEMBER_NOT_FOUND is a possible answer
    without reading the flow."""
    tool = lookup_balance_artifact().as_tool_definition()
    assert tool["name"] == "member_lookup_balance"
    assert "MEMBER_NOT_FOUND" in tool["description"]
    assert tool["input_schema"]["properties"]["member_id"]


@pytest.mark.parametrize(
    "params,message",
    [
        ({}, "missing required parameter 'member_id'"),
        ({"member_id": "abc"}, "does not match"),
        ({"member_id": "12345", "extra": 1}, "unknown parameter"),
    ],
)
def test_parameters_are_validated_before_anything_opens(params, message):
    with pytest.raises(ParamValidationError, match=message):
        lookup_balance_artifact().validate_params(params)


def test_valid_parameters_pass_through():
    assert lookup_balance_artifact().validate_params({"member_id": "12345"}) == {
        "member_id": "12345"
    }


def test_sensitive_parameter_values_never_appear_in_errors():
    a = lookup_balance_artifact()
    spec = a.inputs["member_id"].model_copy(update={"sensitive": True})
    a = a.model_copy(update={"inputs": {"member_id": spec}})
    with pytest.raises(ParamValidationError) as exc:
        a.validate_params({"member_id": "SECRET-VALUE"})
    assert "SECRET-VALUE" not in str(exc.value)
    assert "<redacted>" in str(exc.value)


def test_defaults_fill_in():
    a = lookup_balance_artifact()
    spec = a.inputs["member_id"].model_copy(update={"required": False, "default": "12345"})
    a = a.model_copy(update={"inputs": {"member_id": spec}})
    assert a.validate_params({})["member_id"] == "12345"


# --------------------------------------------------------------------------
# structural validators
# --------------------------------------------------------------------------

def test_duplicate_step_ids_rejected():
    a = lookup_balance_artifact().model_dump(mode="json")
    with pytest.raises(ValidationError, match="step ids must be unique"):
        CapabilityArtifact.model_validate({**a, "steps": [a["steps"][0], a["steps"][0]]})


def test_extraction_to_an_undeclared_output_rejected():
    a = lookup_balance_artifact()
    bad = a.extractions[0].model_copy(update={"output": "not_declared"})
    with pytest.raises(ValidationError, match="undeclared output"):
        CapabilityArtifact.model_validate(
            {**a.model_dump(mode="json"), "extractions": [bad.model_dump(mode="json")]}
        )


def test_declared_output_with_no_extraction_rejected():
    """A capability that promises a value it never reads is broken at the
    contract level, not at runtime."""
    a = lookup_balance_artifact()
    with pytest.raises(ValidationError, match="never extracted"):
        CapabilityArtifact.model_validate(
            {
                **a.model_dump(mode="json"),
                "outputs": {
                    **a.model_dump(mode="json")["outputs"],
                    "account_number": OutputSpec(type=ParamType.STRING).model_dump(mode="json"),
                },
            }
        )


def test_step_using_an_undeclared_input_rejected():
    a = lookup_balance_artifact()
    steps = [s.model_dump(mode="json") for s in a.steps]
    steps[1]["action"]["value_from"] = {"param": "nonexistent"}
    with pytest.raises(ValidationError, match="undeclared input"):
        CapabilityArtifact.model_validate({**a.model_dump(mode="json"), "steps": steps})


def test_outcome_referencing_an_unknown_step_rejected():
    a = lookup_balance_artifact()
    bad = a.outcomes[0].model_copy(update={"after_step": "s99"})
    with pytest.raises(ValidationError, match="unknown step"):
        CapabilityArtifact.model_validate(
            {**a.model_dump(mode="json"), "outcomes": [bad.model_dump(mode="json")]}
        )


def test_declared_risk_may_not_understate_what_the_flow_does():
    """The top-level risk tier is what a caller and a reviewer gate on, so a
    flow containing an irreversible step cannot claim to be read-only."""
    a = lookup_balance_artifact()
    steps = [s.model_dump(mode="json") for s in a.steps]
    steps[-1]["risk"] = "submit_irreversible"
    with pytest.raises(ValidationError, match="must not understate"):
        CapabilityArtifact.model_validate({**a.model_dump(mode="json"), "steps": steps})

    ok = CapabilityArtifact.model_validate(
        {
            **a.model_dump(mode="json"),
            "steps": steps,
            "capability": {
                **a.model_dump(mode="json")["capability"],
                "risk_tier": CapabilityRisk.WRITES_IRREVERSIBLE.value,
            },
        }
    )
    assert ok.capability.risk_tier is CapabilityRisk.WRITES_IRREVERSIBLE


def test_value_source_needs_exactly_one_origin():
    with pytest.raises(ValidationError):
        ValueSource(param="a", literal="b")
    with pytest.raises(ValidationError):
        ValueSource()


def test_action_shape_is_enforced():
    with pytest.raises(ValidationError, match="requires a target"):
        StepAction(type="click")
    with pytest.raises(ValidationError, match="requires value_from"):
        StepAction(type="fill", target=MEMBER_ID_FIELD)
    with pytest.raises(ValidationError, match="requires url_template"):
        StepAction(type="navigate")


@pytest.mark.parametrize(
    "bad,message",
    [
        ({"id": "Member Lookup"}, "dotted snake_case"),
        ({"id": "member-lookup"}, "dotted snake_case"),
        ({"version": "1.0"}, "semver"),
        ({"version": "v1.0.0"}, "semver"),
    ],
)
def test_capability_ids_and_versions_are_constrained(bad, message):
    meta = lookup_balance_artifact().capability.model_dump(mode="json")
    with pytest.raises(ValidationError, match=message):
        CapabilityMeta.model_validate({**meta, **bad})


def test_target_ids_lists_every_overridable_locator():
    ids = set(lookup_balance_artifact().target_ids)
    assert {"member_id_input", "search_button", "result_row_link",
            "savings_balance_cell", "not_found_notice"} <= ids
