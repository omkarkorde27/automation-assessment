"""Profile inheritance and tenant specialization.

The multi-tenant claim rests entirely on this layer: one recorded flow, many
institutions, deltas expressed as overlays. These tests pin the merge rules so
an overlay's effect is predictable from reading the overlay alone, and pin the
guardrails that keep the scheme reviewable at hundreds of tenants.

They run against the REAL profiles in ../profiles/, not synthetic ones, so the
committed configuration is covered too.
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from cua.profiles import ProfileRepository, specialize
from cua.profiles.resolve import MAX_INHERITANCE_DEPTH, ProfileError, ProfileNotFound
from cua.profiles.schema import AppProfile, AuthSpec
from factories import bundle, lookup_balance_artifact

from cua.locators.model import Anchor, AnchorRelative, RoleNameExact

REAL_PROFILES = "profiles"


@pytest.fixture
def repo():
    return ProfileRepository(REAL_PROFILES)


@pytest.fixture
def custom_repo(tmp_path):
    (tmp_path / "products").mkdir()
    (tmp_path / "tenants").mkdir()

    def write(kind: str, name: str, data: dict):
        (tmp_path / kind / f"{name}.yaml").write_text(yaml.safe_dump(data))

    return ProfileRepository(tmp_path), write


# --------------------------------------------------------------------------
# the committed profiles resolve
# --------------------------------------------------------------------------

def test_both_tenants_resolve_from_one_product(repo):
    demo = repo.resolve("demo-cu")
    valley = repo.resolve("valley-cu")
    assert demo.lineage == ("meridian-core@4.2", "demo-cu@1")
    assert valley.lineage == ("meridian-core@4.2", "valley-cu@1")


def test_a_product_profile_is_not_runnable_on_its_own(repo):
    """It has no host. Requiring a tenant to supply one is what keeps the
    product profile tenant-free instead of quietly favouring the first one."""
    with pytest.raises(ProfileError, match="no base_url"):
        _ = repo.resolve("meridian-core@4.2").base_url


def test_tenant_supplies_the_host(repo):
    assert repo.resolve("demo-cu").base_url.endswith("/t/demo-cu")
    assert repo.resolve("valley-cu").base_url.endswith("/t/valley-cu")


def test_app_level_facts_are_inherited_not_repeated(repo):
    """Onboarding a tenant should not mean restating how login works."""
    valley = repo.resolve("valley-cu").profile
    assert [s.id for s in valley.auth.login_recipe.steps] == [
        "login_navigate", "login_user", "login_password", "login_submit"
    ]
    assert valley.auth.session_expiry_detector is not None
    assert [s.id for s in valley.stuck_patterns] == ["permission_wall", "unexpected_dialog"]
    assert [h.code for h in valley.hard_failures] == ["APP_ERROR"]
    assert len(valley.fingerprint.key_screens) == 3


# --------------------------------------------------------------------------
# merge rules
# --------------------------------------------------------------------------

def test_keyed_lists_merge_by_id_not_by_position(repo):
    """valley-cu disables the vendor modal and adds its own consent modal, and
    keeps the inherited spinner recovery."""
    demo = [r.id for r in repo.resolve("demo-cu").profile.recoveries]
    valley = [r.id for r in repo.resolve("valley-cu").profile.recoveries]

    assert demo == ["dismiss_marketing_interstitial", "wait_out_spinner"]
    assert valley == ["wait_out_spinner", "valley_terms_interstitial"]


def test_disabled_recoveries_removes_an_inherited_entry(repo):
    assert repo.resolve("valley-cu").recovery("dismiss_marketing_interstitial") is None
    assert repo.resolve("demo-cu").recovery("dismiss_marketing_interstitial") is not None


def test_same_id_in_an_overlay_replaces_rather_than_duplicates(custom_repo):
    repo, write = custom_repo
    write("products", "base-1", {
        "profile": {"id": "base", "version": "1"},
        "recoveries": [{"id": "shared", "description": "from base",
                        "trigger": {"element": {"role": "dialog"}, "exists": True}}],
    })
    write("tenants", "t1", {
        "profile": {"id": "t1", "version": "1"},
        "extends": "base@1",
        "surface": {"base_url": "http://x"},
        "recoveries": [{"id": "shared", "description": "from tenant",
                        "trigger": {"element": {"role": "alert"}, "exists": True}}],
    })
    recoveries = repo.resolve("t1").profile.recoveries
    assert len(recoveries) == 1
    assert recoveries[0].description == "from tenant"


def test_nested_settings_merge_without_dropping_siblings(custom_repo):
    """Overriding one timeout must not silently reset the others."""
    repo, write = custom_repo
    write("products", "base-1", {
        "profile": {"id": "base", "version": "1"},
        "surface": {"timeouts": {"default_ms": 8000, "navigation_ms": 15000}},
    })
    write("tenants", "t1", {
        "profile": {"id": "t1", "version": "1"},
        "extends": "base@1",
        "surface": {"base_url": "http://x", "timeouts": {"default_ms": 20000}},
    })
    timeouts = repo.resolve("t1").profile.surface.timeouts
    assert timeouts.default_ms == 20000
    assert timeouts.navigation_ms == 15000


def test_override_maps_replace_wholesale(custom_repo):
    """An author writing out label_overrides expects exactly that map, not a
    union with whatever the base happened to declare."""
    repo, write = custom_repo
    write("products", "base-1", {
        "profile": {"id": "base", "version": "1"},
        "overrides": {"label_overrides": {"a": "1", "b": "2"}},
    })
    write("tenants", "t1", {
        "profile": {"id": "t1", "version": "1"}, "extends": "base@1",
        "surface": {"base_url": "http://x"},
        "overrides": {"label_overrides": {"a": "9"}},
    })
    assert repo.resolve("t1").profile.overrides.label_overrides == {"a": "9"}


# --------------------------------------------------------------------------
# guardrails
# --------------------------------------------------------------------------

def test_inheritance_depth_is_capped(custom_repo):
    """A tenant needing more than one overlay is telling you it needs its own
    recording, not a deeper chain."""
    repo, write = custom_repo
    write("products", "base-1", {"profile": {"id": "base", "version": "1"}})
    write("tenants", "mid", {"profile": {"id": "mid", "version": "1"}, "extends": "base@1"})
    write("tenants", "leaf", {"profile": {"id": "leaf", "version": "1"}, "extends": "mid@1"})

    with pytest.raises(ProfileError, match=f"deeper than {MAX_INHERITANCE_DEPTH}"):
        repo.resolve("leaf")


def test_circular_inheritance_is_caught(custom_repo):
    repo, write = custom_repo
    write("tenants", "a", {"profile": {"id": "a", "version": "1"}, "extends": "b@1"})
    write("tenants", "b", {"profile": {"id": "b", "version": "1"}, "extends": "a@1"})
    with pytest.raises(ProfileError, match="circular"):
        repo.resolve("a")


def test_missing_profile_says_where_it_looked(repo):
    with pytest.raises(ProfileNotFound, match="looked for"):
        repo.resolve("no-such-tenant")


def test_credentials_must_be_references_never_literals():
    """Profiles are committed to version control."""
    with pytest.raises(ValidationError, match="must be a reference"):
        AuthSpec(credentials={"password": "hunter2"})

    for ok in ("env:MOCKBANK_PASS", "keyring:core/svc", "vault:prod/core#password"):
        AuthSpec(credentials={"password": ok})


def test_committed_profiles_hold_no_literal_secrets(repo):
    for ref in ("meridian-core@4.2", "demo-cu", "valley-cu"):
        for value in repo.load_raw(ref).auth.credentials.values():
            assert value.split(":", 1)[0] in {"env", "keyring", "vault"}


def test_duplicate_ids_within_one_profile_rejected():
    with pytest.raises(ValidationError, match="must be unique"):
        AppProfile.model_validate({
            "profile": {"id": "x", "version": "1"},
            "recoveries": [
                {"id": "dup", "trigger": {"element": {"role": "dialog"}, "exists": True}},
                {"id": "dup", "trigger": {"element": {"role": "alert"}, "exists": True}},
            ],
        })


def test_resolution_is_deterministic_and_tenant_specific(repo):
    assert repo.resolve("demo-cu").hash == repo.resolve("demo-cu").hash
    assert repo.resolve("demo-cu").hash != repo.resolve("valley-cu").hash


# --------------------------------------------------------------------------
# specialization: applying a tenant's overrides to an artifact
# --------------------------------------------------------------------------

def test_specialization_leaves_the_recorded_artifact_untouched(repo):
    artifact = lookup_balance_artifact().seal()
    effective, report = specialize(artifact, repo.resolve("valley-cu"))

    assert artifact.verify_hash(), "the stored artifact must not be mutated"
    assert report.profile_lineage == ("meridian-core@4.2", "valley-cu@1")
    assert effective.capability.id == artifact.capability.id


def test_specialization_never_writes_to_the_stored_artifact(tmp_path, custom_repo):
    """The tamper-detection invariant, checked on disk rather than in memory.

    `content_hash` is how we detect a modified artifact. If specialization could
    reach the stored file, clearing the hash on the effective copy would blank
    the real one and disable that check exactly when a tenant override is in
    play. Asserted on raw bytes, not on a re-parsed object.
    """
    from cua.artifact import ArtifactStore

    repo, write = custom_repo
    replacement = bundle("member_id_input", RoleNameExact(role="textbox", name="Account Holder #"),
                         notes="This tenant exposes a proper accessible name on the field.")
    write("products", "base-1", {"profile": {"id": "base", "version": "1"}})
    write("tenants", "t1", {
        "profile": {"id": "t1", "version": "1"}, "extends": "base@1",
        "surface": {"base_url": "http://x"},
        "overrides": {"locator_overrides": {
            "member.lookup_balance:member_id_input": replacement.model_dump(mode="json")
        }},
    })

    store = ArtifactStore(tmp_path / "capabilities")
    path = store.save(lookup_balance_artifact())
    bytes_before = path.read_bytes()
    hash_before = store.load("member.lookup_balance@1.0.0").content_hash
    assert hash_before.startswith("sha256:")

    effective, report = specialize(store.load("member.lookup_balance@1.0.0"), repo.resolve("t1"))
    assert report.locator_overrides_applied == ["member_id_input"]
    assert effective.content_hash == "", "the derived view carries no seal"

    assert path.read_bytes() == bytes_before, "specialization must not rewrite the file"
    reloaded = store.load("member.lookup_balance@1.0.0")     # verify=True by default
    assert reloaded.content_hash == hash_before
    assert reloaded.verify_hash()
    fill_step = next(s for s in reloaded.steps if s.id == "s3")
    assert fill_step.action.target.candidates[0].name == "Member ID"


def test_a_specialized_artifact_is_not_sealed(repo):
    """It is derived and ephemeral. Only the recorded flow gets to carry a seal,
    or a tenant override would look like a second published capability."""
    effective, _ = specialize(lookup_balance_artifact().seal(), repo.resolve("valley-cu"))
    assert effective.content_hash == ""


def test_no_overrides_means_the_flow_runs_as_recorded(repo):
    """demo-cu contributes only a host. The base recording already fits, which
    is the outcome the whole design is aiming for."""
    _, report = specialize(lookup_balance_artifact(), repo.resolve("demo-cu"))
    assert report.locator_overrides_applied == []
    assert report.param_defaults_applied == {}


def test_a_locator_override_retargets_exactly_one_bundle(custom_repo):
    repo, write = custom_repo
    replacement = bundle(
        "member_id_input",
        RoleNameExact(role="textbox", name="Account Holder #"),
        notes="This tenant exposes a proper accessible name, so prefer it.",
    )
    write("products", "base-1", {"profile": {"id": "base", "version": "1"}})
    write("tenants", "t1", {
        "profile": {"id": "t1", "version": "1"}, "extends": "base@1",
        "surface": {"base_url": "http://x"},
        "overrides": {
            "locator_overrides": {
                "member.lookup_balance:member_id_input": replacement.model_dump(mode="json")
            }
        },
    })

    effective, report = specialize(lookup_balance_artifact(), repo.resolve("t1"))
    assert report.locator_overrides_applied == ["member_id_input"]

    # Looked up by step id, not position: the flow gained a navigation step
    # during M3 and an index-based assertion silently started checking the
    # wrong step.
    def step(artifact, step_id):
        return next(s for s in artifact.steps if s.id == step_id)

    target = step(effective, "s3").action.target
    assert target.candidates[0].strategy == "role_name_exact"
    assert target.candidates[0].name == "Account Holder #"
    # ...and nothing else moved.
    assert step(effective, "s4").action.target.target_id == "search_button"
    assert step(effective, "s4").action.target == step(lookup_balance_artifact(), "s4").action.target


def test_an_override_for_another_capability_is_ignored(custom_repo):
    repo, write = custom_repo
    write("products", "base-1", {"profile": {"id": "base", "version": "1"}})
    write("tenants", "t1", {
        "profile": {"id": "t1", "version": "1"}, "extends": "base@1",
        "surface": {"base_url": "http://x"},
        "overrides": {"locator_overrides": {
            "member.open_subaccount:member_id_input":
                bundle("member_id_input", RoleNameExact(role="textbox", name="X"),
                       notes="belongs to another capability").model_dump(mode="json")
        }},
    })
    _, report = specialize(lookup_balance_artifact(), repo.resolve("t1"))
    assert report.locator_overrides_applied == []
    assert report.unused_overrides == []


def test_a_stale_override_is_reported_rather_than_silently_ignored(custom_repo):
    """Almost always left behind after a re-record, and worth surfacing."""
    repo, write = custom_repo
    write("products", "base-1", {"profile": {"id": "base", "version": "1"}})
    write("tenants", "t1", {
        "profile": {"id": "t1", "version": "1"}, "extends": "base@1",
        "surface": {"base_url": "http://x"},
        "overrides": {"locator_overrides": {
            "member.lookup_balance:field_that_no_longer_exists":
                bundle("field_that_no_longer_exists",
                       RoleNameExact(role="textbox", name="X"),
                       notes="targets a field removed in a later app version").model_dump(mode="json")
        }},
    })
    _, report = specialize(lookup_balance_artifact(), repo.resolve("t1"))
    assert report.unused_overrides == ["member.lookup_balance:field_that_no_longer_exists"]


def test_param_defaults_apply_per_tenant(repo):
    """valley-cu defaults product_code to their premium product."""
    artifact = lookup_balance_artifact()
    artifact = artifact.model_copy(update={
        "inputs": {
            **artifact.inputs,
            "product_code": artifact.inputs["member_id"].model_copy(
                update={"pattern": None, "required": False}),
        }
    })
    effective, report = specialize(artifact, repo.resolve("valley-cu"))
    assert report.param_defaults_applied == {"product_code": "SAV2"}
    assert effective.inputs["product_code"].default == "SAV2"
    assert effective.validate_params({"member_id": "12345"})["product_code"] == "SAV2"


def test_overrides_reach_extractions_and_outcomes_too(custom_repo):
    """Every addressable locator is overridable, not just step targets."""
    repo, write = custom_repo
    replacement = bundle(
        "savings_balance_cell",
        AnchorRelative(anchor=Anchor(text="Amount"), relation="same_column", target_role="cell"),
        notes="This tenant labels the column Amount.",
    )
    write("products", "base-1", {"profile": {"id": "base", "version": "1"}})
    write("tenants", "t1", {
        "profile": {"id": "t1", "version": "1"}, "extends": "base@1",
        "surface": {"base_url": "http://x"},
        "overrides": {"locator_overrides": {
            "member.lookup_balance:savings_balance_cell": replacement.model_dump(mode="json")
        }},
    })
    effective, report = specialize(lookup_balance_artifact(), repo.resolve("t1"))
    assert report.locator_overrides_applied == ["savings_balance_cell"]
    assert effective.extractions[0].target.candidates[0].anchor.text == "Amount"


def test_report_records_what_ran(repo):
    """Evidence has to answer "which configuration produced this run"."""
    _, report = specialize(lookup_balance_artifact(), repo.resolve("valley-cu"))
    assert report.capability_ref == "member.lookup_balance@1.0.0"
    assert report.profile_hash.startswith("sha256:")
    assert "valley_terms_interstitial" in report.recoveries_inherited
    assert "dismiss_marketing_interstitial" not in report.recoveries_inherited
