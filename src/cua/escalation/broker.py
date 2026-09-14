"""Parking a run for a human, and handing it back.

Two objects live here.

`LiveSession` is what the console is allowed to touch: a surface, its lease, and
the profile whose sensitivity rules decide what a screenshot may show. It exists
so the console never receives a `Surface` directly -- every human action goes
through `forward()`, which builds a real `Action` and puts it through the same
`act()` the engine uses, and therefore through the same allowlist, the same risk
re-derivation, and the same journal.

`InterventionBroker` is the rendezvous. The engine files a request and awaits;
the console resolves it; the engine wakes with the operator's decision. An
`asyncio.Event` rather than polling a file, because both halves are in one
process on one loop by design -- and because a poll interval is a latency
somebody would have to justify.

**Why the engine waits at all.** `Escalated` is a terminal result variant, and
the temptation is to return it and let a later command re-attach. That loses the
only property that makes takeover interesting: it is the *same live session*. A
new process gets a new browser on a new login, and "the human continues where
the robot stopped" quietly becomes "the human starts over". So the run parks in
place. When no broker is attached -- plain `cua replay`, every offline test --
the engine returns the terminal `Escalated` exactly as before.
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from ..artifact.recorder import RecorderRefusal, derive_bundle
from ..locators.model import BBoxNormalized, FrameRef, LocatorBundle
from ..observability.annotate import annotate
from ..observability.journal import Journal, MemoryJournal
from ..perception.model import Observation, UiNode
from ..policy.redaction import apply_sensitivity, screenshot_allowed, screenshot_masks
from ..profiles.resolve import ResolvedProfile
from ..session.control import AUTOMATION, ControlLease, ControlState
from ..surfaces.base import Action, ActionResult, ActionType, Surface
from .requests import (
    Disposition,
    InterventionRequest,
    InterventionStatus,
    InterventionStore,
    Resolution,
)

#: Action kinds an operator may forward. Not the whole `ActionType` vocabulary:
#: WAIT is meaningless from a console.
#:
#: A human who needs something outside this list can still click in the headed
#: browser window -- it is the same session and their changes are real -- but
#: that is NOT the same thing and the difference matters. Nothing in this
#: process intercepts input to Chromium: no CDP listener, no init script, no
#: `page.on` handler. A click made in the window therefore bypasses the journal,
#: the control lease and the post-resolution risk derivation, because it never
#: reaches `act()` at all. The system learns about it only the next time it
#: observes, and cannot say who did it or why.
#:
#: So: window-driving is an escape hatch for getting unstuck, and console
#: forwarding is the path that produces evidence. Only the latter is journaled
#: as `human.action`, and only the latter can be promoted into an artifact.
HUMAN_ACTIONS = {"click", "fill", "select_option", "press_key", "scroll", "navigate"}


class UnknownNode(ValueError):
    """The console named a node that is not on the current screen."""


@dataclass
class LiveSession:
    """A parked run, exposed to the console under the lease.

    Deliberately narrow. The console can look (`snapshot`) and it can act
    (`forward`); it cannot reach the surface, the engine, or the artifact.
    """

    session_id: str
    surface: Surface
    lease: ControlLease
    profile: ResolvedProfile
    journal: Journal = field(default_factory=MemoryJournal)
    run_id: str = ""

    secrets: set[str] = field(default_factory=set)
    """Regulated values seen masked on this session, carried across screens.
    See `policy.redaction.apply_sensitivity`."""

    forwarded: int = 0
    """How many actions the console has put through `act()` on this session.

    The discriminator for R-M6-4, and the reason it is a counter on the session
    rather than a scan of the journal: a journal can be swapped, filtered or
    shared between runs, and the question being asked -- "did anything come
    through this console for this session" -- is about this object."""

    _observation: Observation | None = None

    async def snapshot(self, *, for_evidence: bool = False) -> tuple[Observation, bytes | None]:
        """The current screen: redacted nodes, and a masked, annotated picture.

        Redaction runs here, not in the console, for the same reason it runs in
        `annotate` for the model: this is the last point at which the pixels and
        the node list are still ours. A console that redacted its own output
        would be a second copy of the policy.

        `for_evidence` says these bytes are going to disk rather than to the
        operator watching the session, which is a stricter audience: the file
        outlives the incident and travels, so every classified region is painted
        over rather than only the ones an operator may not see. The node list is
        identical either way -- text masking has no such split, because a node
        list is never the thing an operator reads a number off.
        """
        raw = await self.surface.observe()
        observation = apply_sensitivity(raw, self.profile.profile, self.secrets)
        self._observation = observation

        png: bytes | None = None
        # `sensitivity.never_screenshot` names screens that may not be captured
        # at all -- the finer instrument is masking boxes, and this is the one
        # for a page whose answer to "which parts are regulated" is "all of it".
        # Declared since M2 and, until M6, read by nothing.
        allowed = screenshot_allowed(raw.frame_urls.get("content") or raw.url,
                                     self.profile.profile)
        if not allowed:
            self.journal.emit("screenshot.suppressed", session=self.session_id,
                              reason="the profile forbids capturing this screen")
        if allowed and self.surface.capabilities.can_screenshot:
            try:
                shot = await self.surface.screenshot()
                png = annotate(shot, observation,
                               masks=screenshot_masks(raw, self.profile.profile,
                                                      persisted=for_evidence))
            except Exception:
                # A screenshot that fails must not take the node list with it --
                # the list is the part the operator acts on.
                png = None
        return observation, png

    async def forward(self, *, operator: str, kind: str, node_id: str = "",
                      value: str = "", key: str = "", url: str = "",
                      reason: str = "") -> ActionResult:
        """Perform one human action through the same actuator the engine uses.

        R-M4-2 applies here as squarely as it does to the model: the console
        interprets untrusted input, so the call is journaled ONCE, before
        dispatch, including the calls that turn out to be invalid. Journaling
        per-branch is how the branch nobody thought about goes unrecorded.
        """
        self.forwarded += 1
        self.journal.emit(
            # `action=`, not `kind=`: the journal's own event name is `kind`, and
            # shadowing it would swallow the entry -- an R-M4-2 violation
            # arriving through a keyword collision rather than through a branch.
            "human.action", session=self.session_id, operator=operator, action=kind,
            node_id=node_id, has_value=bool(value), key=key, url=url,
            reason=reason or "(none given)",
        )

        if kind not in HUMAN_ACTIONS:
            raise UnknownNode(
                f"{kind!r} is not an action this console can forward "
                f"({', '.join(sorted(HUMAN_ACTIONS))})"
            )

        action_type = ActionType(kind)
        target: LocatorBundle | None = None
        node: UiNode | None = None

        if action_type in (ActionType.CLICK, ActionType.FILL, ActionType.SELECT_OPTION):
            observation = self._observation or await self.surface.observe()
            node = observation.by_id(node_id)
            if node is None:
                raise UnknownNode(
                    f"no node {node_id!r} on the current screen -- refresh and pick again"
                )
            target = self._bundle_for(node, observation)

        action = Action(
            type=action_type, target=target, value=value or None, key=key or None,
            url=url or None,
            reason=f"operator {operator}: {reason}" if reason else f"operator {operator}",
            # A declaration only. `act()` re-derives the tier from the resolved
            # node (R-M6-2) and enforces against that, so a console that got
            # this wrong could not talk its way past the guard.
            risk=None,
        )

        with self.lease.acting_as(operator):
            return await self.surface.act(action)

    async def fingerprint(self) -> str:
        """A hash of what is on screen right now.

        Computed over the RAW observation, not the redacted one. It is a hash --
        nothing about the values survives it -- and redacting first would blind
        it to exactly the edits worth noticing, since a masked field is masked
        because it is the important one. Role, name, value and enabled state,
        sorted, plus the content frame's url: enough that a click that opened a
        dialog or a keystroke that filled a field changes it, and stable across
        this application's per-render id churn, which changes on every load and
        would otherwise report a change every time.
        """
        observation = await self.surface.observe()
        parts = sorted(
            f"{n.role}\x1f{n.name}\x1f{n.value or ''}\x1f{int(n.enabled)}"
            for n in observation.nodes if n.visible
        )
        url = observation.frame_urls.get("content") or observation.url
        digest = hashlib.sha256(("\x1e".join(parts) + "\x1d" + url).encode())
        return digest.hexdigest()[:16]

    def _bundle_for(self, node: UiNode, observation: Observation) -> LocatorBundle:
        """Turn the node the operator picked into a locator, using the recorder.

        Not a hand-rolled second implementation of "address this control". The
        recorder's ladder is the one that has been argued about and tested, and
        reusing it is what makes the plan's stated bonus true rather than
        aspirational: a human's fix is expressed in exactly the vocabulary a
        recorded step is, so promoting it into an artifact is a copy rather than
        a translation.

        Going through a bundle at all -- instead of clicking the node's box --
        means the operator's click resolves under the same unique-match rule as
        every other action. They get the control they named, or nothing.
        """
        try:
            return derive_bundle(node, observation, target_id=f"operator:{node.node_id}")
        except RecorderRefusal as refusal:
            # The recorder refuses controls it cannot address DURABLY, which is
            # the right answer for something being written into an artifact and
            # the wrong one for a click a person is making, now, on a screen in
            # front of them. So the live path degrades to the node's own box --
            # flagged unstable, journaled, scoped to the frame, and derived from
            # the observation just taken rather than from anything recorded.
            self.journal.emit("human.locator_degraded", session=self.session_id,
                              node=node.describe(), reason=str(refusal))
            b = node.bbox
            return LocatorBundle(
                target_id=f"operator:{node.node_id}",
                frame_path=tuple(FrameRef(name=part) for part in node.frame_path),
                candidates=(BBoxNormalized(x=b.nx, y=b.ny, w=b.nw, h=b.nh),),
                allow_unstable=True,
                notes="Live operator selection during takeover, on a control the recorder "
                      "will not address durably. Valid for this screen only and never "
                      "recorded into an artifact.",
            )


class InterventionBroker:
    """Files interventions and waits for a person to answer them.

    Holds one `asyncio.Event` per open request. The engine awaits it; the
    console sets it. Nothing polls.
    """

    def __init__(
        self,
        store: InterventionStore,
        *,
        journal: Journal | None = None,
        wait_timeout_s: float = 1800.0,
        evidence_root: Path | str = "evidence",
    ) -> None:
        self.store = store
        self.journal = journal or MemoryJournal()
        self.wait_timeout_s = wait_timeout_s
        """How long a run stays parked. A run that waits forever holds a browser
        and a database session open indefinitely, which in a bank's back office
        is its own incident; the timeout converts that into an abandoned
        intervention somebody can see."""

        self.evidence_root = Path(evidence_root)
        self.sessions: dict[str, LiveSession] = {}
        self._waiters: dict[str, asyncio.Event] = {}
        self._resolutions: dict[str, Resolution] = {}
        self._takeovers: dict[str, tuple[str, int]] = {}
        """intervention id -> (screen fingerprint, forwarded count) as they were
        the moment the operator took control. R-M6-4's before-picture."""

    # ---- registration ---------------------------------------------------

    def register(self, session: LiveSession) -> LiveSession:
        self.sessions[session.session_id] = session
        return session

    def unregister(self, session_id: str) -> None:
        self.sessions.pop(session_id, None)

    def session_for(self, request: InterventionRequest) -> LiveSession | None:
        return self.sessions.get(request.session_id)

    # ---- the engine side ------------------------------------------------

    def open(self, request: InterventionRequest, *,
             session: LiveSession | None = None) -> InterventionRequest:
        """File a request. Parks the lease if this one is waiting on a human.

        Synchronous: nothing here does I/O that needs a loop, and the engine
        files failure-side interventions from paths that are not worth making
        async for the sake of an `await` that would never yield.
        """
        if request.takeover:
            live = session or self.sessions.get(request.session_id)
            if live is not None and live.lease.automation_may_act:
                live.lease.pause(reason=f"{request.reason_class}: {request.id}")
            self._waiters[request.id] = asyncio.Event()

        self.store.save(request)
        self.journal.emit("intervention.opened", intervention=request.id,
                          run=request.run_id, reason=request.reason_class,
                          step=request.step_id, takeover=request.takeover)
        return request

    async def wait(self, request: InterventionRequest) -> Resolution:
        """Block the run until an operator answers, or the wait expires."""
        event = self._waiters.get(request.id)
        if event is None:  # not a parking intervention; nothing to wait for
            return Resolution(disposition=Disposition.ABANDON, operator="system",
                              note="this intervention was filed for information only")
        try:
            await asyncio.wait_for(event.wait(), timeout=self.wait_timeout_s)
        except asyncio.TimeoutError:
            resolution = Resolution(
                disposition=Disposition.ABANDON, operator="system",
                note=f"no operator responded within {self.wait_timeout_s:.0f}s",
            )
            self._finish(request, resolution, status=InterventionStatus.ABANDONED)
            # The run is over and nobody arrived, so nobody holds the session.
            # Leaving it PAUSED_PENDING_HUMAN would advertise a takeover that
            # can no longer happen -- an operator opening the console an hour
            # later would find a wheel attached to nothing.
            live = self.sessions.get(request.session_id)
            if live is not None and live.lease.state is not ControlState.ABANDONED:
                live.lease.abandon(actor="system",
                                   reason=f"no operator responded within "
                                          f"{self.wait_timeout_s:.0f}s")
            self.journal.emit("intervention.timed_out", intervention=request.id,
                              waited_s=self.wait_timeout_s)
            return resolution
        finally:
            self._waiters.pop(request.id, None)

        return self._resolutions.pop(request.id)

    # ---- the console side -----------------------------------------------

    def take(self, intervention_id: str, operator: str) -> InterventionRequest:
        """An operator claims the session. The lease moves to them."""
        request = self._require(intervention_id)
        live = self.sessions.get(request.session_id)
        if live is None:
            raise LookupError(
                f"session {request.session_id} is no longer live; this intervention "
                f"can be read but not driven"
            )
        live.lease.take(operator)
        updated = request.model_copy(update={"status": InterventionStatus.TAKEN})
        self.store.save(updated)
        self.journal.emit("intervention.taken", intervention=request.id, operator=operator)
        return updated

    async def mark_taken(self, request: InterventionRequest) -> None:
        """Photograph the screen before the operator touches it (R-M6-4).

        Separate from `take` and asynchronous because observing is I/O and
        `take` is not; the console calls both, in order. A session that is gone
        records nothing rather than raising -- there is no takeover to audit.
        """
        live = self.sessions.get(request.session_id)
        if live is None:
            return
        try:
            self._takeovers[request.id] = (await live.fingerprint(), live.forwarded)
        except Exception as exc:  # an audit must never be what breaks a takeover
            self.journal.emit("takeover.fingerprint_failed", intervention=request.id,
                              error=f"{type(exc).__name__}: {exc}")

    async def audit_release(self, request: InterventionRequest) -> InterventionRequest:
        """Did the screen change with nothing on the record to explain it?

        Called at release, BEFORE `resolve` wakes the run -- afterwards the
        engine starts driving and the screen stops being the one the operator
        left. Three outcomes:

          * nothing changed                       -> nothing to say
          * changed, and actions came through     -> the console explains it
          * changed, and none did                 -> `handback.unsanctioned_change`

        The third is the case §3.7's "or directly in the headed window" makes
        possible and the choke point cannot see: an OS-level click reaches
        Chromium without passing through `act()`, so there is no journal entry,
        no derived tier and no lease check. Keeping the escape hatch is
        deliberate -- it is what an operator uses when the action vocabulary
        cannot express the fix, and removing it would make the console the
        ceiling on what a person is allowed to repair. What is not acceptable is
        an evidence pack that shows a screen changing and says nothing.

        Honest limit: this detects THAT something happened off-channel, never
        WHAT. An operator holding the window is outside the choke point by
        construction; the guarantee is evidence completeness, not containment.
        """
        before = self._takeovers.pop(request.id, None)
        live = self.sessions.get(request.session_id)
        if before is None or live is None:
            return request

        before_hash, forwarded_at_take = before
        try:
            after_hash = await live.fingerprint()
        except Exception as exc:
            self.journal.emit("takeover.fingerprint_failed", intervention=request.id,
                              error=f"{type(exc).__name__}: {exc}")
            return request

        forwarded = live.forwarded - forwarded_at_take
        changed = after_hash != before_hash
        unsanctioned = changed and forwarded == 0

        self.journal.emit(
            "handback.unsanctioned_change" if unsanctioned else "handback.audited",
            intervention=request.id, session=request.session_id,
            screen_before=before_hash, screen_after=after_hash,
            changed=changed, forwarded_actions=forwarded,
            note=("the screen changed while the operator held the lease and nothing "
                  "was forwarded through the console -- the window was driven directly, "
                  "so what was done is not recorded anywhere"
                  if unsanctioned else ""),
        )

        if not unsanctioned:
            return request
        updated = request.model_copy(update={"unsanctioned_change": True})
        self.store.save(updated)
        return updated

    def resolve(self, intervention_id: str, resolution: Resolution) -> InterventionRequest:
        """Record the operator's decision and wake the parked run."""
        request = self._require(intervention_id)
        live = self.sessions.get(request.session_id)

        if live is not None and live.lease.state is not ControlState.ABANDONED:
            if resolution.disposition is Disposition.ABANDON:
                live.lease.abandon(actor=resolution.operator,
                                   reason=resolution.note or "operator abandoned the run")
            else:
                # Human -> RESUMING. Not straight back to AUTOMATION_OWNED: the
                # engine has not yet re-verified where it is standing, and a
                # stray action in that window is exactly what would invalidate
                # the check it is about to make.
                live.lease.begin_resume(actor=resolution.operator,
                                        reason=resolution.note or "operator released control")

        status = (InterventionStatus.ABANDONED
                  if resolution.disposition is Disposition.ABANDON
                  else InterventionStatus.RESOLVED)
        updated = self._finish(request, resolution, status=status)

        self._resolutions[request.id] = resolution
        if event := self._waiters.get(request.id):
            event.set()

        self.journal.emit("intervention.resolved", intervention=request.id,
                          disposition=resolution.disposition.value,
                          operator=resolution.operator, note=resolution.note)
        return updated

    # ---- helpers --------------------------------------------------------

    def _require(self, intervention_id: str) -> InterventionRequest:
        request = self.store.get(intervention_id)
        if request is None:
            raise LookupError(f"no intervention {intervention_id!r}")
        return request

    def _finish(self, request: InterventionRequest, resolution: Resolution,
                *, status: InterventionStatus) -> InterventionRequest:
        updated = request.model_copy(update={"status": status, "resolution": resolution})
        self.store.save(updated)
        return updated


__all__ = [
    "AUTOMATION", "HUMAN_ACTIONS", "InterventionBroker", "LiveSession", "UnknownNode",
]
