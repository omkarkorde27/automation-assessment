"""The operator console -- embedded, minimal, and real where it matters.

Part 12 calls this out as a deliberate cut: *mechanism real, UI mocked*. The UI
is one page of vanilla JavaScript polling three JSON endpoints. What is not
mocked is everything underneath it:

  * the console never receives a `Surface`. It gets a `LiveSession`, and the
    only way it can move anything is `forward()`, which builds a real `Action`
    and puts it through the same `act()` the replay engine uses. Same allowlist,
    same post-resolution risk derivation, same journal.
  * taking control moves the actual lease. Until it does, the operator's clicks
    are refused by the choke point, not by a disabled button.
  * releasing does not resume anything by itself. It moves the lease to
    RESUMING and wakes the engine, which re-verifies the screen before it
    reclaims control.

**Untrusted input (R-M4-2).** Every request that interprets something a person
typed -- an intervention id, a node id, an action kind -- is journaled once,
before dispatch, valid or not. The rule was written for the model's tool calls
and it binds here for the same reason: an evidence pack that omits the decision
nobody anticipated is not evidence. Invalid input produces a 4xx *and* a
journal entry, never a silent 400 from the framework.

**Serving it.** The console shares the process and the event loop with the run
it is watching, because "the human drives the *same* session" is not achievable
across processes -- a second process gets a second browser. `uv run cua
serve-console` runs it standalone against the evidence directory, which is the
read-only case: interventions from finished runs can be reviewed, but nothing is
live to take over.

**What the console does NOT cover, stated plainly.** The browser window is
headed and a person can click in it. Those clicks work -- it is the same session
-- but they do not reach `act()`, so they are not journaled, not lease-checked,
and not risk-classified. Nothing here intercepts input to Chromium, and adding
an interceptor would be a *second* recording path alongside the actuator, which
is the arrangement this design exists to avoid: two places that record what
happened is two places for the record to be wrong, and the one that bypasses the
choke point is the one that would drift.

The consequence is worth naming rather than burying: the control lease enforces
that no *code path* acts without holding it. It does not, and cannot from
inside this process, enforce that no *person* touches the browser. An operator
who drives the window directly leaves a session whose state changed for reasons
the evidence pack cannot explain -- which is exactly the failure R-M4-2 is about,
arriving through the one door the actuator does not sit in front of. The
mitigation available today is procedural (drive through the node list) and the
verifiable half is the handback check, which refuses to resume onto a screen it
cannot recognise however that screen came about.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from ..observability.journal import Journal, MemoryJournal
from .broker import HUMAN_ACTIONS, InterventionBroker, LiveSession, UnknownNode
from .requests import Disposition, InterventionStatus, InterventionStore, Resolution


class TakeBody(BaseModel):
    operator: str = "operator"


class ActBody(BaseModel):
    operator: str = "operator"
    kind: str
    node_id: str = ""
    value: str = ""
    key: str = ""
    url: str = ""
    reason: str = ""


class ResolveBody(BaseModel):
    operator: str = "operator"
    disposition: str
    note: str = ""


def create_console(
    broker: InterventionBroker | None = None,
    *,
    store: InterventionStore | None = None,
    journal: Journal | None = None,
) -> FastAPI:
    """Build the console app.

    `broker` is optional so the console can run standalone over an evidence
    directory. Without one there are no live sessions, so takeover and action
    forwarding return 409 rather than pretending: a console that accepts a click
    into a browser that closed an hour ago is worse than one that says no.
    """
    resolved_store = store or (broker.store if broker else InterventionStore())
    log = journal or (broker.journal if broker else MemoryJournal())
    app = FastAPI(title="cua operator console", docs_url=None, redoc_url=None)

    def note(event: str, request: Request, **data: Any) -> None:
        """One journal entry per interpreted request, before dispatch (R-M4-2).

        The parameter is `event`, not `kind`: `Journal.emit`'s own first
        argument is `kind`, and a payload field of the same name would collide
        with it -- an evidence gap arriving through a keyword clash rather than
        through a branch anybody would review.
        """
        log.emit(event, path=request.url.path, **data)

    def require_broker() -> InterventionBroker:
        if broker is None:
            raise HTTPException(409, "this console is read-only: no run is attached to it")
        return broker

    _live_cache: dict[str, tuple[float, Any, bytes | None]] = {}
    _LIVE_TTL_S = 1.0

    async def live_snapshot(intervention_id: str, session: LiveSession):
        """One `snapshot()` per tick, shared by the node list and the picture.

        `snapshot()` runs the extractor over every frame and takes a Playwright
        screenshot, both against the session a human is watching. The page polls
        `/screen` and `/screenshot.png` together, so the naive version did that
        work twice a second -- and the repaints were visible as a flicker in the
        headed window, on the one screen where an operator is trying to read
        something. They are two endpoints because an <img> needs its own GET,
        not because they are two different observations.

        Deliberately not in the broker: `mark_taken` and the release audit
        fingerprint the screen at moments that must not be served a cached
        answer. This TTL is scoped to the viewfinder.
        """
        now = time.monotonic()
        hit = _live_cache.get(intervention_id)
        if hit is not None and now - hit[0] < _LIVE_TTL_S:
            return hit[1], hit[2]
        observation, png = await session.snapshot()
        _live_cache[intervention_id] = (now, observation, png)
        return observation, png

    def require_live(intervention_id: str) -> tuple[Any, LiveSession]:
        found = resolved_store.get(intervention_id)
        if found is None:
            raise HTTPException(404, f"no intervention {intervention_id!r}")
        session = require_broker().session_for(found)
        if session is None:
            raise HTTPException(
                409,
                f"intervention {intervention_id} has no live session: the run it belonged "
                f"to has ended. It can be read, not driven.",
            )
        return found, session

    # ---- reading --------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _PAGE

    @app.get("/api/interventions")
    async def list_interventions(open_only: bool = False) -> JSONResponse:
        status = InterventionStatus.OPEN if open_only else None
        rows = resolved_store.list(status=status)
        live = set(broker.sessions) if broker else set()
        return JSONResponse([
            {**resolved_store.dump(r), "live": r.session_id in live}
            for r in rows
        ])

    @app.get("/api/sessions/{session_id}/interventions")
    async def by_session(session_id: str) -> JSONResponse:
        """Everything filed against one session -- the `resume_token` round trip.

        `Escalated.resume_token` carries the parked session's id back to the
        calling agent. This is the other end of that: the agent hands the token
        to whatever fronts the console and gets the work item back. Without it
        the token is a string in a JSON document that nothing can be done with,
        which is the shape a field takes when it was declared and never wired.
        """
        rows = [r for r in resolved_store.list() if r.session_id == session_id]
        live = bool(broker and session_id in broker.sessions)
        return JSONResponse({
            "session_id": session_id,
            "live": live,
            "lease": broker.sessions[session_id].lease.to_dict() if live else None,
            "interventions": [{**resolved_store.dump(r), "live": live} for r in rows],
        })

    @app.get("/api/interventions/{intervention_id}")
    async def get_intervention(intervention_id: str, request: Request) -> JSONResponse:
        found = resolved_store.get(intervention_id)
        if found is None:
            note("console.unknown_intervention", request, intervention=intervention_id)
            raise HTTPException(404, f"no intervention {intervention_id!r}")
        live = bool(broker and broker.session_for(found))
        lease = None
        if live:
            lease = broker.session_for(found).lease.to_dict()
        return JSONResponse({**resolved_store.dump(found), "live": live, "lease": lease})

    def _stored(intervention_id: str, attribute: str) -> Path:
        """Resolve a path recorded IN the request, and refuse to leave the store.

        `screenshot_ref` and `observation_ref` are strings inside a JSON
        document. They are written by this system and are not user input today,
        but "not user input today" is how directory traversal arrives: the file
        is on disk, an operator can edit it, and a console that hands back
        whatever path it is told to would happily serve /etc/passwd. Confining
        the resolved path to the store root costs two lines.
        """
        found = resolved_store.get(intervention_id)
        if found is None:
            raise HTTPException(404, f"no intervention {intervention_id!r}")
        raw = getattr(found, attribute, "")
        if not raw:
            raise HTTPException(404, f"this intervention has no {attribute}")
        path = Path(raw).resolve()
        root = Path(resolved_store.root).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404, f"{attribute} does not point inside the evidence store")
        return path

    @app.get("/api/interventions/{intervention_id}/evidence/screenshot.png")
    async def stored_screenshot(intervention_id: str) -> Response:
        """The redacted screenshot taken when the request was filed.

        The one thing that makes an intervention from a FINISHED run reviewable:
        the live endpoint below needs a browser that is still open, and by the
        time most people read a page-out it is not.
        """
        return Response(content=_stored(intervention_id, "screenshot_ref").read_bytes(),
                        media_type="image/png")

    @app.get("/api/interventions/{intervention_id}/evidence/observation")
    async def stored_observation(intervention_id: str) -> Response:
        """The redacted node list as it was when the run stopped."""
        return Response(content=_stored(intervention_id, "observation_ref").read_text(),
                        media_type="application/json")

    @app.get("/api/interventions/{intervention_id}/screen")
    async def screen(intervention_id: str) -> JSONResponse:
        """The live screen as the operator's numbered node list.

        The same `Observation` the agent would see, through the same redaction.
        An operator picks a node id from here; there is no other way to name a
        control, which is what keeps every human action expressible as an
        `Action` -- and therefore promotable into an artifact.
        """
        _, session = require_live(intervention_id)
        observation, _ = await live_snapshot(intervention_id, session)
        return JSONResponse({
            "url": observation.url,
            "title": observation.title,
            "nodes": [
                {"node_id": n.node_id, "role": n.role, "name": n.name,
                 "frame": "/".join(n.frame_path) or "(top)",
                 "value": None if n.sensitive else n.value,
                 "sensitive": n.sensitive, "enabled": n.enabled,
                 # The line the operator actually picks by. A legacy form's
                 # inputs have no accessible name at all -- the Member ID box
                 # and the Last Name box are both `textbox ""` -- and only the
                 # row label tells them apart. Rendering `name` alone makes the
                 # table unusable on exactly the screens this exists for.
                 "describe": n.describe()}
                for n in observation.nodes if n.visible
            ],
        })

    @app.get("/api/interventions/{intervention_id}/screenshot.png")
    async def screenshot(intervention_id: str) -> Response:
        """Polled about once a second by the page. Masked before it is encoded."""
        _, session = require_live(intervention_id)
        _, png = await live_snapshot(intervention_id, session)
        if png is None:
            raise HTTPException(503, "this surface cannot produce a screenshot")
        return Response(content=png, media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    # ---- acting ---------------------------------------------------------

    @app.post("/api/interventions/{intervention_id}/take")
    async def take(intervention_id: str, body: TakeBody, request: Request) -> JSONResponse:
        note("console.take_requested", request, intervention=intervention_id,
             operator=body.operator)
        broker_ = require_broker()
        try:
            updated = broker_.take(intervention_id, body.operator)
        except LookupError as exc:
            raise HTTPException(409, str(exc)) from exc
        except RuntimeError as exc:  # IllegalTransition -- already taken, or over
            raise HTTPException(409, str(exc)) from exc
        # R-M6-4: photograph the screen the operator is inheriting, so a change
        # made outside this console is visible at release. After the lease has
        # moved, so the before-picture is of the screen they actually got.
        await broker_.mark_taken(updated)
        return JSONResponse(resolved_store.dump(updated))

    @app.post("/api/interventions/{intervention_id}/act")
    async def act(intervention_id: str, body: ActBody, request: Request) -> JSONResponse:
        """Forward one human action into the live session.

        Journaled by `LiveSession.forward` before anything is dispatched, so an
        invalid node id and a refused irreversible click both leave a record.
        Note what is NOT here: no coordinates. A click the console cannot name
        is a click whose risk cannot be re-derived and which could never become
        a recorded step, and adding it would put a hole through the choke point
        for the sake of convenience.
        """
        note("console.act_requested", request, intervention=intervention_id,
             operator=body.operator, action=body.kind, node_id=body.node_id)
        _, session = require_live(intervention_id)
        try:
            result = await session.forward(
                operator=body.operator, kind=body.kind, node_id=body.node_id,
                value=body.value, key=body.key, url=body.url, reason=body.reason,
            )
        except UnknownNode as exc:
            _live_cache.pop(intervention_id, None)
            raise HTTPException(400, str(exc)) from exc
        # The screen just moved. Serving the cached pre-action view for up to a
        # second would show the operator the state they just left.
        _live_cache.pop(intervention_id, None)

        return JSONResponse({
            "ok": result.ok,
            "error": result.error,
            "derived_risk": result.derived_risk.value if result.derived_risk else None,
            "resolved": result.resolved.describe() if result.resolved else None,
        }, status_code=200 if result.ok else 422)

    @app.post("/api/interventions/{intervention_id}/resolve")
    async def resolve(intervention_id: str, body: ResolveBody,
                      request: Request) -> JSONResponse:
        note("console.resolve_requested", request, intervention=intervention_id,
             operator=body.operator, disposition=body.disposition)
        try:
            disposition = Disposition(body.disposition)
        except ValueError as exc:
            raise HTTPException(
                400,
                f"{body.disposition!r} is not a disposition "
                f"({', '.join(d.value for d in Disposition)})",
            ) from exc

        resolution = Resolution(disposition=disposition, operator=body.operator,
                                note=body.note)
        broker_ = require_broker()
        try:
            found = resolved_store.get(intervention_id)
            if found is None:
                raise LookupError(f"no intervention {intervention_id!r}")
            # R-M6-4, BEFORE the run is woken: once `resolve` sets the event the
            # engine starts driving, and the screen stops being the one the
            # operator left behind.
            await broker_.audit_release(found)
            updated = broker_.resolve(intervention_id, resolution)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        return JSONResponse(resolved_store.dump(updated))

    @app.get("/api/health")
    async def health() -> dict:
        return {
            "ok": True,
            "live_sessions": sorted(broker.sessions) if broker else [],
            "actions": sorted(HUMAN_ACTIONS),
            "evidence_root": str(getattr(resolved_store, "root", Path("evidence"))),
        }

    return app


_PAGE = """<!doctype html>
<meta charset="utf-8"><title>cua operator console</title>
<style>
 :root { color-scheme: light dark; }
 body { font: 13px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace; margin: 0;
        display: grid; grid-template-columns: 340px 1fr; height: 100vh;
        overflow: hidden; }
 /* A grid item defaults to min-height:auto, so it grows to fit its content
    instead of scrolling: with a long node table #main overflowed a 100vh body
    that has nothing to scroll, and the rows below the fold could not be
    reached at all. Whether you saw it depended on how tall the table was
    relative to the window, which is why it reproduced in one browser and not
    another. `min-height: 0` is what lets `overflow: auto` actually apply. */
 #list, #main { min-height: 0; }
 #list { border-right: 1px solid #8884; overflow-y: auto; padding: 8px; }
 #main { overflow-y: auto; overflow-x: hidden; padding: 12px; }
 /* ONE scroll container, and it is #main. An inner scroll box for the rows
    sounded tidier and was worse to use: the wheel over the table scrolled the
    table, the wheel over the picture scrolled the page, and which one you got
    depended on where the pointer happened to be. */
 #nodes { border: 1px solid #8884; border-radius: 4px; }
 #nodes table { margin: 0; }
 #nodes .urlbar { position: sticky; top: 0; background: Canvas; margin: 0;
                  padding: 4px 6px; border-bottom: 1px solid #8884; }
 /* `img { max-width: 100% }` constrains the WIDTH. This application is a
    frameset and its screenshot is far taller than the viewport, so the picture
    alone filled the window and pushed every node row below the fold -- which
    looks exactly like a page that will not scroll. Cap the height and let the
    aspect ratio give back the width. */
 #shot { display: block; max-height: 40vh; width: auto; max-width: 100%;
         object-fit: contain; object-position: top left; }
 /* `[hidden]` is only `display:none` from the UA sheet, and the rule above
    outranks it. Without this the toggle sets the attribute and nothing moves. */
 #shot[hidden] { display: none; }
 .row { padding: 6px 8px; border: 1px solid #8884; border-radius: 4px; margin-bottom: 6px;
        cursor: pointer; }
 .row.on { border-color: #d24; }
 .cls { font-weight: 700; }
 .dim { opacity: .65; }
 .tag { font-size: 11px; border: 1px solid #8886; border-radius: 3px; padding: 0 4px; }
 img { max-width: 100%; border: 1px solid #8884; }
 button { font: inherit; padding: 3px 8px; margin-right: 4px; }
 table { border-collapse: collapse; width: 100%; font-size: 12px; }
 td { padding: 2px 6px; border-bottom: 1px solid #8883; vertical-align: top; }
 tr.pick:hover { background: #8882; cursor: pointer; }
 pre { white-space: pre-wrap; background: #8881; padding: 8px; border-radius: 4px; }
 input { font: inherit; width: 220px; }
</style>
<div id="list">Loading…</div>
<div id="main" class="dim">Pick an intervention.</div>
<script>
let current = null, operator = "operator", shotTimer = null;
let lastSig = null, polling = false;

async function j(url, opts) {
  const r = await fetch(url, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || r.statusText);
  return body;
}
const esc = s => String(s ?? "").replace(/[<>&]/g, c => ({'<':'&lt;','>':'&gt;','&':'&amp;'}[c]));

async function refreshList() {
  const rows = await j("/api/interventions");
  document.getElementById("list").innerHTML = rows.length ? rows.map(r => `
    <div class="row ${r.id === current ? 'on' : ''}" onclick="open_('${r.id}')">
      <div class="cls">${esc(r.reason_class)}</div>
      <div class="dim">${esc(r.capability_ref)} ${r.step_id ? '· ' + esc(r.step_id) : ''}</div>
      <div><span class="tag">${esc(r.status)}</span>
           <span class="tag">${r.live ? (r.takeover ? 'live · takeover' : 'live') : 'ended'}</span></div>
    </div>`).join("") : "<div class='dim'>No interventions filed.</div>";
}

async function open_(id) {
  current = id; clearInterval(shotTimer); lastSig = null;
  const r = await j("/api/interventions/" + id);
  const drivable = r.live && r.takeover;
  document.getElementById("main").className = "";
  document.getElementById("main").innerHTML = `
    <h3>${esc(r.reason_class)} <span class="tag">${esc(r.status)}</span></h3>
    <p>${esc(r.human_message)}</p>
    <table>
      <tr><td>capability</td><td>${esc(r.capability_ref)}</td></tr>
      <tr><td>tenant</td><td>${esc(r.tenant || "–")}</td></tr>
      <tr><td>goal</td><td>${esc(r.goal)}</td></tr>
      <tr><td>step</td><td>${esc(r.step_id)} (${r.step_index ?? "–"})</td></tr>
      <tr><td>expected</td><td>${esc(r.expected)}</td></tr>
      <tr><td>observed</td><td>${esc(r.observed)}</td></tr>
      <tr><td>run ended as</td><td>${esc(r.result_status || "still running")}</td></tr>
      <tr><td>opened</td><td>${esc(r.opened_at)}</td></tr>
      <tr><td>lease</td><td>${r.lease ? esc(r.lease.state + " · " + (r.lease.holder || "nobody")) : "–"}</td></tr>
      ${r.unsanctioned_change ? `<tr><td>audit</td><td><b>unsanctioned change</b> —
        the screen changed while the operator held the lease and nothing came through
        this console. The window was driven directly; what was done is not recorded.</td></tr>` : ""}
      ${r.resolution ? `<tr><td>resolved</td><td>${esc(r.resolution.disposition)} by
        ${esc(r.resolution.operator)} at ${esc(r.resolution.at)}<br>${esc(r.resolution.note)}</td></tr>` : ""}
    </table>
    ${r.screenshot_ref ? `<h4>When it stopped</h4>
      <img src="/api/interventions/${r.id}/evidence/screenshot.png"
           alt="redacted screen at the moment the run stopped">
      <p class="dim"><a href="/api/interventions/${r.id}/evidence/observation">observation JSON</a>
         · ${esc(r.observation_ref || "")}</p>` : ""}
    ${drivable ? `
      <p style="margin-top:12px">
        <input id="op" value="${esc(operator)}" placeholder="your name">
        <button onclick="take()">Take control</button>
        <button onclick="done('resume')">Release &amp; resume</button>
        <button onclick="done('step_completed')">I completed this step</button>
        <button onclick="done('abandon')">Abandon</button>
      </p>
      <h4>Live <button onclick="togglePic()" id="pic-btn">hide picture</button></h4>
      <details><summary class="dim">how driving this works</summary>
            <p class="dim">Picking a row below drives the session through the same
         <code>act()</code> the engine uses: journaled, lease-checked, risk
         re-derived. You can also click in the browser window itself — it is the
         same session and the changes are real — but nothing intercepts input to
         the browser, so those actions are <b>not journaled, not risk-checked and
         cannot be promoted into the capability</b>. The release will be recorded
         as an unsanctioned change (R-M6-4): the evidence pack will say the screen
         moved, and will not be able to say how.</p></details>
      <img id="shot" alt="live screen">
      <div id="nodes"></div>`
     : `<p class="dim" style="margin-top:12px">This run has ended. Read-only —
          the evidence above is what it looked like when it stopped.</p>`}
  `;
  await refreshList();
  if (drivable) { poll(); shotTimer = setInterval(poll, 1500); }
}

async function poll() {
  const img = document.getElementById("shot");
  if (!img) { clearInterval(shotTimer); return; }
  if (polling) return;   // a slow snapshot must not queue more of itself
  polling = true;
  try {
    // Decode into a detached image and swap only once it is ready. Assigning
    // .src directly blanks the element while the next PNG loads, which at a
    // one-second cadence is a visible flicker.
    const next = new Image();
    next.onload = () => { img.src = next.src; };
    next.src = `/api/interventions/${current}/screenshot.png?t=` + Date.now();

    const s = await j(`/api/interventions/${current}/screen`);
    // Rewriting innerHTML every tick threw away the operator's scroll position
    // in the node table -- which reads exactly like "the table will not
    // scroll". Only redraw when the screen has actually changed.
    const sig = s.url + "|" + s.nodes.map(n =>
      n.node_id + n.describe + (n.sensitive ? "*" : n.value ?? "") + n.enabled).join("~");
    if (sig === lastSig) return;
    lastSig = sig;
    document.getElementById("nodes").innerHTML =
      `<p class="dim urlbar">${esc(s.url)}</p><table>` +
      s.nodes.map(n => `<tr class="pick" onclick="pick('${n.node_id}','${esc(n.role)}')">
        <td class="dim">${esc(n.frame)}</td><td>${esc(n.node_id)}</td>
        <td>${esc(n.describe)}</td>
        <td>${n.sensitive ? "&lt;redacted&gt;" : esc(n.value ?? "")}</td>
        <td>${n.enabled ? "" : "<span class='tag'>disabled</span>"}</td>
      </tr>`).join("") + "</table>";
  } finally {
    polling = false;
  }
}

function togglePic() {
  // The rows are what an operator clicks; the picture is only orientation. On a
  // short window it can still push the table further down than is comfortable,
  // so it collapses. The state lives on the element rather than in the
  // re-rendered HTML, so the 1.5s redraw does not undo it.
  const img = document.getElementById("shot"), btn = document.getElementById("pic-btn");
  if (!img) return;
  img.hidden = !img.hidden;
  btn.textContent = img.hidden ? "show picture" : "hide picture";
}

async function pick(nodeId, role) {
  const kind = (role === "textbox" || role === "combobox")
      ? (role === "combobox" ? "select_option" : "fill") : "click";
  const value = kind === "click" ? "" : prompt(`value for ${nodeId}`) ?? "";
  if (kind !== "click" && value === "") return;
  const reason = prompt("why? (recorded in the journal)") ?? "";
  try {
    const r = await j(`/api/interventions/${current}/act`, {
      method: "POST", headers: {"content-type": "application/json"},
      body: JSON.stringify({operator, kind, node_id: nodeId, value, reason}),
    });
    console.log("acted", r);
  } catch (e) { alert(e.message); }
  poll();
}

async function take() {
  operator = document.getElementById("op").value || "operator";
  try { await j(`/api/interventions/${current}/take`, {
    method: "POST", headers: {"content-type": "application/json"},
    body: JSON.stringify({operator}) }); }
  catch (e) { alert(e.message); }
  open_(current);
}

async function done(disposition) {
  const note = prompt("note for the record") ?? "";
  try { await j(`/api/interventions/${current}/resolve`, {
    method: "POST", headers: {"content-type": "application/json"},
    body: JSON.stringify({operator, disposition, note}) }); }
  catch (e) { alert(e.message); }
  clearInterval(shotTimer);
  open_(current);
}

refreshList(); setInterval(refreshList, 3000);
</script>
"""
