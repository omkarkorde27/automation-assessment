"""The operator console: the HTTP surface, and what it refuses.

Part 12 calls the console a deliberate cut -- *mechanism real, UI mocked*. These
tests are about the mechanism, so they exercise the endpoints rather than the
page. The properties worth asserting are mostly negative: what the console will
not do, and what it records when somebody asks it to.

R-M4-2 binds here. The console interprets untrusted input, so every request that
does so leaves a journal entry whether or not it was valid -- an intervention id
that does not exist, an action kind nobody supports, a node that has scrolled
off the screen. A 400 that leaves no trace is a decision nobody can reconstruct.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cua.escalation import (
    Disposition,
    InterventionBroker,
    InterventionRequest,
    InterventionStatus,
    InterventionStore,
    LiveSession,
    Resolution,
)
from cua.escalation.console import create_console
from cua.observability.journal import MemoryJournal
from cua.perception.model import Observation
from cua.profiles import ProfileRepository
from cua.session.control import ControlLease, ControlState
from cua.policy.risk import classify
from cua.surfaces.base import (
    ActionResult,
    PolicyDenied,
    SurfaceCapabilities,
    SurfaceKind,
)


class FakeSurface:
    """Enough surface to drive the console. No browser, no mockbank.

    The console's own contract -- what it exposes, what it refuses, what it
    records -- is decidable without a real screen, and the live-session
    behaviour is covered against a real browser in `test_escalation.py`.
    """

    def __init__(self) -> None:
        self.journal = MemoryJournal()
        self.performed: list = []
        self._guards: list = []

    @property
    def capabilities(self):
        return SurfaceCapabilities(kind=SurfaceKind.LEGACY_WEB, can_screenshot=False)

    def add_guard(self, guard):
        self._guards.append(guard)

    async def observe(self) -> Observation:
        return Observation(url="http://app/x", title="Screen", nodes=(), frame_paths=())

    async def act(self, action) -> ActionResult:
        # Mirrors `WebSurface`: a refusal is a failed result carrying
        # POLICY_DENIED, not an exception that escapes the choke point. A fake
        # that let the exception through would be testing a contract the real
        # surface does not have.
        try:
            for guard in self._guards:
                guard.check(action, self)
        except PolicyDenied as exc:
            return ActionResult(ok=False, action=action, error=exc.message,
                                error_detail={"failure_class": type(exc).failure_class,
                                              "denied_by": exc.denied_by})
        tier = classify(action.type, None)
        for guard in self._guards:
            if (resolved := getattr(guard, "check_resolved", None)) is not None:
                resolved(action, tier, None, self)
        self.performed.append(action)
        return ActionResult(ok=True, action=action, derived_risk=tier)

    async def screenshot(self, *, full_page: bool = False) -> bytes:
        raise RuntimeError("no screenshots here")

    async def current_url(self) -> str:
        return "http://app/x"


@pytest.fixture
def wired(tmp_path):
    log = MemoryJournal()
    store = InterventionStore(tmp_path / "evidence")
    broker = InterventionBroker(store, journal=log, evidence_root=tmp_path / "evidence")
    lease = ControlLease("sess_c", journal=log)
    surface = FakeSurface()
    surface.add_guard(lease)
    profile = ProfileRepository("profiles").resolve("demo-cu")
    broker.register(LiveSession(session_id="sess_c", surface=surface, lease=lease,
                                profile=profile, journal=log))

    request = broker.open(InterventionRequest(
        run_id="run_c", session_id="sess_c", capability_ref="member.lookup_balance@1.0.0",
        goal="read a balance", step_id="s5", step_index=4,
        reason_class="PERMISSION_REQUIRED", human_message="A privileged operator is needed.",
        expected="the member record", observed="an authorization alert", takeover=True))

    client = TestClient(create_console(broker))
    return type("Wired", (), {"client": client, "broker": broker, "store": store,
                              "lease": lease, "journal": log, "request": request,
                              "surface": surface})


# ---- reading -------------------------------------------------------------

def test_the_list_shows_what_an_operator_triages_on(wired):
    rows = wired.client.get("/api/interventions").json()
    assert len(rows) == 1
    row = rows[0]
    assert row["reason_class"] == "PERMISSION_REQUIRED"
    assert row["status"] == "open" and row["takeover"] is True
    assert row["live"] is True


def test_an_intervention_from_a_finished_run_is_readable_but_not_live(wired):
    wired.broker.unregister("sess_c")
    row = wired.client.get(f"/api/interventions/{wired.request.id}").json()
    assert row["live"] is False and row["lease"] is None

    refused = wired.client.post(f"/api/interventions/{wired.request.id}/take",
                                json={"operator": "alice"})
    assert refused.status_code == 409
    assert "no longer live" in refused.json()["detail"]


def test_a_console_with_no_run_attached_says_so_instead_of_pretending(tmp_path):
    store = InterventionStore(tmp_path / "evidence")
    store.save(InterventionRequest(run_id="r", session_id="s", reason_class="X",
                                   human_message="y", takeover=True))
    client = TestClient(create_console(store=store))

    assert len(client.get("/api/interventions").json()) == 1
    assert client.get("/api/interventions").json()[0]["live"] is False
    taken = client.post("/api/interventions/nope/take", json={})
    assert taken.status_code in (404, 409)


def test_the_page_and_health_endpoint_come_up(wired):
    assert "operator console" in wired.client.get("/").text
    health = wired.client.get("/api/health").json()
    assert health["ok"] is True and health["live_sessions"] == ["sess_c"]
    assert "click" in health["actions"] and "select_option" in health["actions"]


# ---- acting --------------------------------------------------------------

def test_a_non_holder_cannot_act_through_the_console_either(wired):
    """The lease is checked in `act()`, so the console gets no exemption from
    it -- the refusal comes from the same place the agent's would."""
    assert wired.lease.state is ControlState.PAUSED_PENDING_HUMAN
    response = wired.client.post(
        f"/api/interventions/{wired.request.id}/act",
        json={"operator": "alice", "kind": "press_key", "key": "Enter",
              "reason": "before taking control"})
    assert response.status_code == 422
    assert "control lease" in response.json()["error"]
    assert wired.surface.performed == []


def test_taking_control_moves_the_real_lease_and_then_actions_land(wired):
    wired.client.post(f"/api/interventions/{wired.request.id}/take",
                      json={"operator": "alice"})
    assert wired.lease.state is ControlState.HUMAN_OWNED
    assert wired.lease.holder == "alice"
    assert wired.store.get(wired.request.id).status is InterventionStatus.TAKEN

    response = wired.client.post(
        f"/api/interventions/{wired.request.id}/act",
        json={"operator": "alice", "kind": "press_key", "key": "Enter",
              "reason": "dismiss the alert"})
    assert response.status_code == 200 and response.json()["ok"] is True
    assert len(wired.surface.performed) == 1
    assert wired.surface.performed[0].reason.startswith("operator alice:")


def test_the_console_forwards_only_actions_the_vocabulary_can_express(wired):
    """No coordinates. A click the console cannot name is a click whose risk
    cannot be re-derived and which could never become a recorded step."""
    wired.client.post(f"/api/interventions/{wired.request.id}/take",
                      json={"operator": "alice"})
    response = wired.client.post(
        f"/api/interventions/{wired.request.id}/act",
        json={"operator": "alice", "kind": "click_at", "reason": "412,289"})
    assert response.status_code == 400
    assert "not an action this console can forward" in response.json()["detail"]
    assert wired.surface.performed == []


def test_a_node_that_is_not_on_the_screen_is_refused_not_guessed(wired):
    wired.client.post(f"/api/interventions/{wired.request.id}/take",
                      json={"operator": "alice"})
    response = wired.client.post(
        f"/api/interventions/{wired.request.id}/act",
        json={"operator": "alice", "kind": "click", "node_id": "content:n99",
              "reason": "it was there a second ago"})
    assert response.status_code == 400
    assert "refresh and pick again" in response.json()["detail"]


# ---- R-M4-2 --------------------------------------------------------------

@pytest.mark.parametrize("payload,why", [
    ({"operator": "alice", "kind": "click_at", "reason": "coords"}, "an unsupported kind"),
    ({"operator": "alice", "kind": "click", "node_id": "nope"}, "a stale node id"),
    ({"operator": "alice", "kind": "press_key", "key": "Enter"}, "a valid call"),
])
def test_every_forwarded_call_is_journaled_valid_or_not(wired, payload, why):
    """R-M4-2. Journaled once, before dispatch -- emitting per-branch is how the
    branch nobody thought about goes unrecorded."""
    wired.client.post(f"/api/interventions/{wired.request.id}/take",
                      json={"operator": "alice"})
    wired.client.post(f"/api/interventions/{wired.request.id}/act", json=payload)

    human = wired.journal.of("human.action")
    assert len(human) == 1, f"{why} left no record"
    assert human[0].data["operator"] == "alice"
    assert human[0].data["action"] == payload["kind"]
    assert human[0].data["reason"]  # never blank: "(none given)" is still a value


def test_an_unknown_intervention_is_journaled_rather_than_silently_404d(wired):
    assert wired.client.get("/api/interventions/int_nope").status_code == 404
    assert wired.journal.first("console.unknown_intervention") is not None


def test_a_nonsense_disposition_is_named_rather_than_absorbed(wired):
    response = wired.client.post(f"/api/interventions/{wired.request.id}/resolve",
                                 json={"operator": "alice", "disposition": "obviously_not"})
    assert response.status_code == 400
    assert "resume" in response.json()["detail"] and "abandon" in response.json()["detail"]
    assert wired.journal.first("console.resolve_requested") is not None


# ---- handback ------------------------------------------------------------

def test_releasing_hands_to_RESUMING_not_straight_back_to_automation(wired):
    """The engine has not re-verified anything yet. Returning the lease to
    automation here would mean it could act on a screen nobody checked."""
    wired.client.post(f"/api/interventions/{wired.request.id}/take",
                      json={"operator": "alice"})
    wired.client.post(f"/api/interventions/{wired.request.id}/resolve",
                      json={"operator": "alice", "disposition": "resume", "note": "fixed"})

    assert wired.lease.state is ControlState.RESUMING
    assert wired.store.get(wired.request.id).status is InterventionStatus.RESOLVED
    assert wired.store.get(wired.request.id).resolution.disposition is Disposition.RESUME


def test_abandoning_ends_the_lease(wired):
    wired.client.post(f"/api/interventions/{wired.request.id}/take",
                      json={"operator": "alice"})
    wired.client.post(f"/api/interventions/{wired.request.id}/resolve",
                      json={"operator": "alice", "disposition": "abandon", "note": "no"})
    assert wired.lease.state is ControlState.ABANDONED
    assert wired.store.get(wired.request.id).status is InterventionStatus.ABANDONED


def test_the_node_list_carries_what_an_operator_picks_by(wired, monkeypatch):
    """A legacy form's inputs have no accessible name -- the Member ID box and
    the Last Name box are both `textbox ""`. Only `describe` (which folds in the
    row label) and the frame tell them apart, so both must reach the page."""
    from cua.perception.model import Anchors, BBox, Observation, UiNode

    async def two_nameless_boxes():
        return Observation(url="http://app/f", title="Form", frame_paths=(("content",),),
                           nodes=tuple(
                               UiNode(node_id=f"content:n{i}", role="textbox", name="",
                                      frame_path=("content",), bbox=BBox(x=0, y=0, w=9, h=9),
                                      anchors=Anchors(row_label=label))
                               for i, label in enumerate(("Member ID", "Last Name"))))

    monkeypatch.setattr(wired.surface, "observe", two_nameless_boxes)
    wired.client.post(f"/api/interventions/{wired.request.id}/take", json={"operator": "a"})
    nodes = wired.client.get(f"/api/interventions/{wired.request.id}/screen").json()["nodes"]

    assert [n["node_id"] for n in nodes] == ["content:n0", "content:n1"]
    assert all(n["frame"] == "content" for n in nodes)
    described = [n["describe"] for n in nodes]
    assert len(set(described)) == 2, f"indistinguishable in the console: {described}"
    assert "Member ID" in described[0] and "Last Name" in described[1]


def test_a_resume_token_finds_the_work_item_it_refers_to(wired):
    """`Escalated.resume_token` is the parked session's id. This is what a
    calling agent does with it."""
    body = wired.client.get("/api/sessions/sess_c/interventions").json()
    assert body["live"] is True
    assert body["lease"]["state"] == "PAUSED_PENDING_HUMAN"
    assert [r["id"] for r in body["interventions"]] == [wired.request.id]

    stale = wired.client.get("/api/sessions/sess_gone/interventions").json()
    assert stale["live"] is False and stale["interventions"] == []


# ---- stored evidence -------------------------------------------------------

def test_the_screenshot_and_observation_filed_with_a_request_can_be_read_back(
        wired, tmp_path):
    """`screenshot_ref` and `observation_ref` are the whole reason an
    intervention from a finished run is reviewable at all. A path written into a
    JSON document that nothing ever serves is a field, not a feature."""
    directory = tmp_path / "evidence" / "runs" / "run_c" / "interventions"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n-not-really")
    (directory / "obs.json").write_text('{"url": "http://app/x", "nodes": []}')
    wired.store.save(wired.request.model_copy(update={
        "screenshot_ref": str(directory / "shot.png"),
        "observation_ref": str(directory / "obs.json")}))

    shot = wired.client.get(
        f"/api/interventions/{wired.request.id}/evidence/screenshot.png")
    assert shot.status_code == 200
    assert shot.headers["content-type"] == "image/png"
    assert shot.content.startswith(b"\x89PNG")

    observation = wired.client.get(
        f"/api/interventions/{wired.request.id}/evidence/observation")
    assert observation.status_code == 200
    assert observation.json()["url"] == "http://app/x"


def test_an_evidence_path_pointing_outside_the_store_is_refused(wired):
    """The path lives in a JSON file on disk. "Nobody edits that" is how
    directory traversal arrives."""
    wired.store.save(wired.request.model_copy(
        update={"screenshot_ref": "/etc/passwd"}))
    response = wired.client.get(
        f"/api/interventions/{wired.request.id}/evidence/screenshot.png")
    assert response.status_code == 404
    assert "inside the evidence store" in response.json()["detail"]


def test_an_intervention_with_no_stored_evidence_says_so(wired):
    response = wired.client.get(
        f"/api/interventions/{wired.request.id}/evidence/screenshot.png")
    assert response.status_code == 404
    assert "no screenshot_ref" in response.json()["detail"]


# ---- the store -----------------------------------------------------------

def test_a_half_written_file_loses_one_row_not_the_page(wired, tmp_path):
    (tmp_path / "evidence" / "runs" / "run_c" / "interventions" / "torn.json").write_text("{")
    assert len(wired.client.get("/api/interventions").json()) == 1


def test_interventions_are_filed_under_the_run_they_belong_to(wired, tmp_path):
    path = (tmp_path / "evidence" / "runs" / "run_c" / "interventions"
            / f"{wired.request.id}.json")
    assert path.exists(), "an intervention belongs beside the journal that caused it"


def test_resolution_survives_a_reload_from_disk(wired):
    wired.broker.resolve(wired.request.id, Resolution(
        disposition=Disposition.STEP_COMPLETED, operator="alice", note="did it"))
    fresh = InterventionStore(wired.store.root).get(wired.request.id)
    assert fresh.status is InterventionStatus.RESOLVED
    assert fresh.resolution.operator == "alice"
    assert fresh.resolution.disposition is Disposition.STEP_COMPLETED
