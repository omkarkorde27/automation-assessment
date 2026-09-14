"""The committed capabilities must run on a tenant they were not recorded on.

Invariant 9 says an artifact binds to a vendor product and a version range, not
to a tenant. `binding` said so from M2 and the schema enforced it -- and both
recorded capabilities were pinned to demo-cu anyway, through a door nobody was
watching: the recorder built URL checkpoints from the observed address, and the
observed address begins `/t/demo-cu`. `binding` claimed portability while eleven
checkpoints quietly denied it.

Nothing caught it because every test that replayed a RECORDED artifact replayed
it on the tenant it came from, and every test that replayed cross-tenant used
the hand-authored artifact from `factories.py`, whose conditions were written by
hand and were correct. Two half-covered axes read as one covered one.

So this suite crosses them on purpose: the artifacts that ship, on the tenant
they did not come from.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cua.artifact.schema import CapabilityArtifact
from cua.artifact.store import ArtifactStore
from cua.profiles import ProfileRepository
from cua.profiles.resolve import specialize
from cua.replay import ReplayEngine, Success
from cua.replay.engine import CredentialResolver
from cua.session.auth import Authenticator
from cua.surfaces.web_playwright import WebSurface

CREDS = CredentialResolver({"MOCKBANK_USER": "operator", "MOCKBANK_PASS": "demo-pass-not-real"})
COMMITTED = sorted(Path("capabilities").glob("*.json"))


def _url_patterns(node, out: list[str]) -> list[str]:
    """Every `matches` regex in every URL condition, wherever it is nested."""
    if isinstance(node, dict):
        url = node.get("url")
        if isinstance(url, dict) and "matches" in url:
            out.append(url["matches"])
        for value in node.values():
            _url_patterns(value, out)
    elif isinstance(node, list):
        for value in node:
            _url_patterns(value, out)
    return out


@pytest.mark.parametrize("path", COMMITTED, ids=lambda p: p.stem)
def test_no_committed_artifact_pins_the_tenant_it_was_recorded_on(path):
    """Static, cheap, and the one that would have caught this on day one.

    `recorded_against.tenant` is provenance and belongs in the artifact. The same
    string inside a checkpoint is identity, and does not.
    """
    data = json.loads(path.read_text())
    tenant = data["binding"]["recorded_against"]["tenant"]
    assert tenant, "provenance should still say where this was learned"

    # As it appears in a regex: `re.escape` is what the recorder ran it through.
    for pattern in _url_patterns(data, []):
        assert re.escape(tenant) not in pattern and tenant not in pattern, (
            f"{path.name} asserts {pattern!r}, which contains the tenant it was "
            f"recorded on. It will replay there and fail on every sibling."
        )


@pytest.mark.parametrize("path", COMMITTED, ids=lambda p: p.stem)
def test_every_committed_artifact_still_verifies_its_own_hash(path):
    """The seal covers the executable flow. If a checkpoint is edited, the hash
    moves with it -- an artifact that validates but does not verify has been
    changed by something that did not re-seal."""
    artifact = CapabilityArtifact.model_validate_json(path.read_text())
    assert artifact.verify_hash(), f"{path.name} is not sealed against its own contents"


async def test_the_recorded_capability_replays_on_a_tenant_it_never_saw(
        page, live_server):
    """The heterogeneity claim, end to end, with nothing hand-written in it.

    The artifact came from a real Opus run against demo-cu. Valley Credit Union
    runs the same vendor product with a renamed nav item, a renamed member-id
    field and a different interstitial. One recording, one overlay, no
    re-recording -- and the balance comes back.
    """
    store = ArtifactStore("capabilities")
    artifact = store.resolve_ref("member.lookup_balance")
    assert artifact.binding.recorded_against.tenant == "demo-cu", (
        "this test is only meaningful while the recording came from elsewhere"
    )

    repo = ProfileRepository("profiles")
    resolved = repo.resolve("valley-cu")
    surface_cfg = resolved.profile.surface.model_copy(
        update={"base_url": f"{live_server}/t/valley-cu"})
    resolved = resolved.__class__(
        profile=resolved.profile.model_copy(update={"surface": surface_cfg}),
        lineage=resolved.lineage, hash=resolved.hash)

    effective, report = specialize(artifact, resolved)
    assert set(report.locator_overrides_applied) == {"member_search_link", "member_id_input"}, (
        "the two controls Valley renamed are the two the overlay replaces"
    )
    assert report.unused_overrides == [], "a stale override is a silent no-op"

    surface = WebSurface(page)
    await Authenticator(surface, resolved, credentials=CREDS).sign_in()
    result = await ReplayEngine(surface, effective, resolved, credentials=CREDS,
                                tenant="valley-cu").run({"member_id": "12345"})

    assert isinstance(result, Success), getattr(result, "describe", lambda: result)()
    assert result.outputs["savings_balance"] == "4210.75"


async def test_the_same_recording_still_replays_where_it_came_from(page, live_server):
    """The other half of the pair. A portability fix that breaks the home tenant
    has traded one broken axis for another."""
    store = ArtifactStore("capabilities")
    artifact = store.resolve_ref("member.lookup_balance")

    repo = ProfileRepository("profiles")
    resolved = repo.resolve("demo-cu")
    surface_cfg = resolved.profile.surface.model_copy(
        update={"base_url": f"{live_server}/t/demo-cu"})
    resolved = resolved.__class__(
        profile=resolved.profile.model_copy(update={"surface": surface_cfg}),
        lineage=resolved.lineage, hash=resolved.hash)

    effective, _ = specialize(artifact, resolved)
    surface = WebSurface(page)
    await Authenticator(surface, resolved, credentials=CREDS).sign_in()
    result = await ReplayEngine(surface, effective, resolved, credentials=CREDS,
                                tenant="demo-cu").run({"member_id": "12345"})

    assert isinstance(result, Success), getattr(result, "describe", lambda: result)()
    assert result.outputs["savings_balance"] == "4210.75"
