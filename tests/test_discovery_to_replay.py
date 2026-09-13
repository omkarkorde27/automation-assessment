"""The through-line, end to end and offline.

    The model discovers. The artifact becomes a reusable capability.
    Deterministic replay is how the AI agent invokes it in production.

This is the one test that exercises all three clauses in a single run: a
(scripted) model explores the live application, the recorder derives an artifact
from what it did, and that artifact is then replayed with **no model in the
loop** and asked to produce the same answer.

Nothing here is hand-authored. The artifact under replay is whatever the
recorder produced this run -- locators, checkpoints, parameters and all -- so a
recorder that writes plausible-looking but unreplayable artifacts fails here
rather than in production.
"""

from __future__ import annotations

import pytest

from cua.artifact.schema import ProductRef
from cua.artifact.store import ArtifactStore
from cua.discovery.agent import DiscoveryAgent
from cua.discovery.model import ScriptedClient, tool_turn
from cua.discovery.session import discover
from cua.profiles import ProfileRepository
from cua.replay import BusinessOutcome, ReplayEngine, Success
from cua.replay.engine import CredentialResolver
from cua.surfaces.web_playwright import WebSurface
from test_discovery_loop import node_id, sign_in

PRODUCT = ProductRef(vendor="meridian", product="core", version_range=">=4.2 <5")
CREDS = CredentialResolver({"MOCKBANK_USER": "operator", "MOCKBANK_PASS": "demo-pass-not-real"})
GOAL = "Look up member 12345 and read their current savings balance"


def build_profile(repo, base, tenant):
    resolved = repo.resolve(tenant)
    surface = resolved.profile.surface.model_copy(update={"base_url": f"{base}/t/{tenant}"})
    return resolved.__class__(
        profile=resolved.profile.model_copy(update={"surface": surface}),
        lineage=resolved.lineage, hash=resolved.hash,
    )


def lookup_script(get_agent):
    """The decisions a competent operator would make, scripted."""
    return [
        lambda: tool_turn(("click", {
            "node_id": node_id(get_agent(), "link", "Member Search"),
            "reason": "Open the member search screen from the navigation menu"})),
        lambda: tool_turn(("fill", {
            "node_id": node_id(get_agent(), "textbox", ""),
            "text": "12345", "is_param_candidate": True, "param_name": "member_id",
            "reason": "Enter the member id the request asked about"})),
        lambda: tool_turn(("click", {
            "node_id": node_id(get_agent(), "button", "Search"),
            "reason": "Run the search"})),
        lambda: tool_turn(("click", {
            "node_id": node_id(get_agent(), "link", "12345"),
            "reason": "Open the matching member's record"})),
        lambda: tool_turn(("extract", {
            "node_id": node_id(get_agent(), "cell", "4,210.75"),
            "output_name": "savings_balance",
            "reason": "The current savings balance the goal asked for"})),
        lambda: tool_turn(("finish", {
            "reason": "The member detail screen shows the savings balance"})),
    ]


@pytest.fixture
async def recorded(page, live_server, tmp_path):
    """Run discovery, record, and verify by replay -- the real pipeline."""
    await sign_in(page, live_server)
    repo = ProfileRepository("profiles")
    profile = build_profile(repo, live_server, "demo-cu")

    holder: dict = {}
    client = ScriptedClient(lookup_script(lambda: holder["agent"]))

    original = DiscoveryAgent.__init__

    def capture(self, *a, **kw):
        original(self, *a, **kw)
        holder["agent"] = self

    DiscoveryAgent.__init__ = capture
    try:
        outcome = await discover(
            WebSurface(page), client, profile,
            goal=GOAL, tenant="demo-cu", capability_id="member.lookup_balance",
            product=PRODUCT, app_profile_ref="meridian-core@4.2",
            evidence_root=tmp_path / "evidence",
            store=ArtifactStore(tmp_path / "capabilities"),
            repository=repo, credentials=CREDS,
        )
    finally:
        DiscoveryAgent.__init__ = original
    return outcome


async def test_the_recorded_artifact_replays_without_a_model(recorded):
    """The whole premise, in one assertion.

    The artifact was produced seconds ago by a model exploring; it is replayed
    here by an engine with no import path to `anthropic`, and it returns the
    same answer.
    """
    assert recorded.run.succeeded, recorded.run.stop_reason
    assert not recorded.refusal, recorded.refusal
    assert recorded.artifact is not None

    assert isinstance(recorded.verification, Success), (
        getattr(recorded.verification, "describe", lambda: recorded.verification)())
    assert recorded.verification.outputs == {"savings_balance": "4210.75"}
    assert recorded.artifact.provenance.verified_by_replay is True


async def test_the_recording_is_parameterized_not_hard_coded(recorded):
    """A capability that only works for member 12345 is not a capability."""
    artifact = recorded.artifact
    assert "member_id" in artifact.inputs

    fills = [s for s in artifact.steps if s.action.value_from is not None]
    assert fills, "the flow typed something; it must be recorded as coming from somewhere"
    assert all(s.action.value_from.param for s in fills), "a value was baked in as a literal"

    raw = artifact.model_dump_json()
    assert "12345" not in raw, "this run's member id survived into the artifact"


async def test_the_artifact_carries_its_provenance_and_seals(recorded):
    artifact = recorded.artifact
    assert artifact.verify_hash()
    assert artifact.capability.approval_state.value == "draft"
    assert artifact.provenance.recorded_from_run == recorded.run.run_id
    assert artifact.provenance.model == "claude-opus-5"
    assert artifact.provenance.steps_observed >= len(artifact.steps)


async def test_every_recorded_locator_states_its_reasoning(recorded):
    """§3.2 asks for the locator strategy 'with your reasoning'. The recorder
    writes that reasoning, because the recorder is what knows why."""
    for step in recorded.artifact.steps:
        if step.action.target is None:
            continue
        assert step.action.target.notes, f"{step.id} has an unexplained locator"
        assert step.action.target.recorded is not None, "no record-time snapshot to diff against"
        assert step.action.target.candidates


async def test_the_evidence_pack_is_complete(recorded):
    """§3.5: somebody has to be able to reconstruct what happened without
    having been there."""
    d = recorded.evidence_dir
    assert (d / "journal.jsonl").exists()
    assert (d / "discovery.json").exists()
    assert (d / "artifact.json").exists()
    assert (d / "verification.json").exists()
    assert (d / "result.json").exists()
    assert list((d / "steps").glob("*_observation.json"))

    journal = (d / "journal.jsonl").read_text()
    for event in ("discovery.started", "llm.decision", "action.executed",
                  "artifact.recorded", "verify.started", "verify.finished"):
        assert event in journal, f"{event} missing from the journal"

    # The model's own reason for each decision is in the evidence, not a
    # separate log somebody has to remember to write.
    assert "Run the search" in journal


async def test_no_regulated_data_reaches_the_evidence_pack(recorded):
    """The flow visits the member detail screen, which carries an SSN."""
    d = recorded.evidence_dir
    for path in list(d.rglob("*.json")) + list(d.rglob("*.jsonl")):
        text = path.read_text()
        assert "521-84-9077" not in text, f"an SSN reached {path.name}"


async def test_the_recorded_capability_runs_on_the_other_tenant(
    recorded, page, live_server
):
    """Recorded once against demo-cu, replayed against valley-cu, which spells
    the field differently. The pattern candidate that spans both spellings was
    generated at record time from the tenant overlays on disk."""
    repo = ProfileRepository("profiles")
    valley = build_profile(repo, live_server, "valley-cu")

    engine = ReplayEngine(WebSurface(page), recorded.artifact, valley,
                          credentials=CREDS, tenant="valley-cu")
    result = await engine.run({"member_id": "12345"})

    assert isinstance(result, Success), getattr(result, "describe", lambda: result)()
    assert result.outputs == {"savings_balance": "4210.75"}


async def test_the_recorded_capability_handles_a_member_that_does_not_exist(
    recorded, page, live_server
):
    """The recording never saw a missing member, so nothing declared
    MEMBER_NOT_FOUND. The honest result is a typed failure at the step that
    could not proceed -- not a crash, and not a silent wrong answer.

    This is what a second discovery pass, or a human adding one `declare_outcome`,
    is for; the artifact is a draft precisely because of gaps like this.
    """
    repo = ProfileRepository("profiles")
    profile = build_profile(repo, live_server, "demo-cu")

    engine = ReplayEngine(WebSurface(page), recorded.artifact, profile,
                          credentials=CREDS, tenant="demo-cu")
    result = await engine.run({"member_id": "99999"})

    assert not isinstance(result, Success)
    assert not isinstance(result, BusinessOutcome), (
        "nothing in this recording declared a not-found outcome, so claiming one "
        "would be the artifact asserting knowledge it does not have"
    )
    assert result.at_step, "a failure must name the step that could not proceed"
