"""Watch a stuck run park itself, hand it to a person, and watch it finish.

An inspection harness, not part of the system under test. M5's claim is the one
that reads best and is easiest to fake: "a human can take over the same live
session." This makes it watchable.

    uv run python scripts/watch_takeover.py

What happens, in one process:

  1. mockbank comes up on an ephemeral port and a HEADED browser signs in.
  2. `permission_denied` is armed to land on the step that opens the member's
     record. The committed product profile declares that screen as the
     `permission_wall` stuck pattern -- nothing here is staged for the demo.
  3. The replay engine hits it, files an intervention, and PARKS: the control
     lease moves to PAUSED_PENDING_HUMAN and the run stops mid-flow. It has not
     returned a result. The browser is still open on the failing screen.
  4. The operator console is serving on :8801 against that same live session.
     Open it, press "Take control", and drive it FROM THE CONSOLE'S NODE LIST.
     Those actions go through the same `act()` the engine uses and are journaled
     as `human.action`. Clicking in the browser window also works -- it is the
     same session -- but nothing intercepts input to Chromium, so window clicks
     are invisible to the journal, the lease and the risk check. If the run ends
     reporting "0 action(s)", that is what happened.
  5. Release. WHICH BUTTON depends on what you just did, and the two are not
     interchangeable:

       Release & resume        -> re-runs the step that failed, so the engine
                                  checks the screen that step STARTS FROM.
       I completed this step   -> skips it, so the engine checks the screen that
                                  step was supposed to PRODUCE.

     Clearing this demo's permission alert means opening the member record --
     which is what s5 produces. That is "I completed this step". Choosing
     "Release & resume" there asks the engine to re-run s5 from the search
     results, which are no longer on screen, and the handback correctly refuses.

The interesting thing to try is step 4 done badly: take control, wander to an
unrelated screen, and release either way. The handback refuses and the run fails
loudly rather than continuing against a screen nobody verified.

    --tenant valley-cu     drive the other institution's overlay
    --fault none           park on an ambiguity instead of a permission wall
    --headless             for a smoke check; there is nothing to watch
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO), str(REPO / "src"), str(REPO / "tests")]

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from cua.escalation import InterventionBroker, InterventionStore, LiveSession  # noqa: E402
from cua.escalation.console import create_console  # noqa: E402
from cua.profiles import ProfileRepository  # noqa: E402
from cua.replay import ReplayEngine  # noqa: E402
from cua.replay.engine import CredentialResolver  # noqa: E402
from cua.replay.result import to_dict  # noqa: E402
from cua.session.control import ControlLease  # noqa: E402
from cua.surfaces.web_playwright import WebSurface  # noqa: E402
from factories import lookup_balance_artifact  # noqa: E402

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, YELLOW, RED, BLUE, CYAN = ("\033[32m", "\033[33m", "\033[31m", "\033[34m", "\033[36m")

LOUD = {
    "step.started": BLUE,
    "action.resolved": CYAN,
    "checkpoint.passed": GREEN,
    "stuck.detected": YELLOW,
    "intervention.opened": BOLD + YELLOW,
    "lease.transferred": BOLD + CYAN,
    "intervention.taken": BOLD + CYAN,
    "human.action": BOLD + GREEN,
    "human.locator_degraded": YELLOW,
    "policy.checked": DIM,
    "policy.denied": RED,
    "policy.human_override": BOLD + YELLOW,
    "intervention.resolved": BOLD + CYAN,
    "handback.verified": BOLD + GREEN,
    "handback.resync_failed": BOLD + RED,
    "run.finished": BOLD,
}
QUIET = {"policy.checked", "observation.captured"}


def rule(title: str) -> None:
    print(f"\n{BOLD}{'─' * 78}{RESET}\n{BOLD}{title}{RESET}\n{BOLD}{'─' * 78}{RESET}")


class LiveJournal:
    """A `Journal` that narrates to the terminal as the run happens."""

    def __init__(self, *, verbose: bool = False) -> None:
        self.events: list[tuple[str, dict]] = []
        self.verbose = verbose

    def emit(self, kind: str, **data: Any) -> None:
        self.events.append((kind, data))
        if kind in QUIET and not self.verbose:
            return
        detail = " ".join(
            f"{k}={v}" for k, v in data.items()
            if k not in {"run_id", "profile", "profile_hash"} and v not in ("", None, [], {}))
        print(f"  {LOUD.get(kind, DIM)}{kind:<28}{RESET} {DIM}{detail[:150]}{RESET}")

    def of(self, kind: str) -> list[dict]:
        return [d for k, d in self.events if k == kind]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(app, port: int, name: str) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                           log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise SystemExit(f"{name} did not start on :{port}")
    return server


def retarget(resolved, base_url: str, tenant: str):
    surface = resolved.profile.surface.model_copy(
        update={"base_url": f"{base_url}/t/{tenant}"})
    return resolved.__class__(
        profile=resolved.profile.model_copy(update={"surface": surface}),
        lineage=resolved.lineage, hash=resolved.hash)


class ArmingJournal(LiveJournal):
    """Narrates, and arms the fault when the chosen step starts.

    Armed before the run it would be spent on sign-in -- the one screen nobody
    is watching.
    """

    def __init__(self, *, at_step: str, base_url: str, tenant: str, fault: str,
                 verbose: bool):
        super().__init__(verbose=verbose)
        self._at, self._base, self._tenant, self._fault = at_step, base_url, tenant, fault
        self._armed = False

    def emit(self, kind: str, **data: Any) -> None:
        if (not self._armed and self._fault != "none"
                and kind == "step.started" and data.get("step") == self._at):
            self._armed = True
            httpx.post(f"{self._base}/t/{self._tenant}/__control",
                       params={"fault": self._fault, "count": 1}, timeout=10)
            print(f"  {YELLOW}{'>> FAULT ARMED':<28}{RESET} {BOLD}{self._fault}{RESET} "
                  f"{DIM}-- fires during {self._at}{RESET}")
        super().emit(kind, **data)


async def watch_for_park(broker: InterventionBroker, console_url: str,
                         run_id: str) -> None:
    """Print the intervention the moment it is filed, with what to do next.

    Scoped to THIS run's id. The store is the whole `evidence/` tree, so every
    intervention any previous run ever filed is still sitting in it -- and
    announcing one of those as though it had just parked, next to a lease
    reading AUTOMATION_OWNED because it belongs to a browser that closed an hour
    ago, is worse than saying nothing.
    """
    seen: set[str] = set()
    while True:
        for request in broker.store.list(run_id=run_id):
            if request.id in seen or not request.takeover:
                continue
            seen.add(request.id)
            rule(f"PARKED — {request.reason_class}")
            print(f"  intervention   {request.id}")
            print(f"  capability     {request.capability_ref}")
            print(f"  step           {request.step_id} (index {request.step_index})")
            print(f"  expected       {request.expected}")
            print(f"  observed       {request.observed}")
            print(f"  screenshot     {request.screenshot_ref or '(none)'}")
            lease = broker.sessions[request.session_id].lease
            print(f"  lease          {lease.describe()}")
            print(f"\n  {BOLD}The run has NOT returned. The browser is still open.{RESET}")
            print(f"  Open {BOLD}{console_url}{RESET} and press {BOLD}Take control{RESET}.")
            print(f"\n  {BOLD}Drive it from the console's node list{RESET}, not the browser "
                  f"window:")
            print(f"    {DIM}window clicks are the same session and they work, but nothing")
            print(f"     intercepts input to Chromium -- they never reach act(), so they are")
            print(f"     not journaled, not lease-checked, not risk-classified.{RESET}")
            print(f"\n  {BOLD}Then pick the release that matches what you did:{RESET}")
            print(f"    {BOLD}I completed this step{RESET}  you opened the member record "
                  f"yourself {DIM}-> checks the screen {request.step_id} produces{RESET}")
            print(f"    {BOLD}Release & resume{RESET}        you put the search results back "
                  f"{DIM}-> checks the screen {request.step_id} starts from{RESET}")
            print(f"    {BOLD}Abandon{RESET}                 the entitlement is genuinely "
                  f"missing\n")
            print(f"  {DIM}To watch the handback refuse: wander somewhere unrelated "
                  f"first, then release.{RESET}\n")
        await asyncio.sleep(0.3)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tenant", default="demo-cu")
    ap.add_argument("--member", default="12345")
    ap.add_argument("--fault", default="permission_denied",
                    help="permission_denied | none")
    ap.add_argument("--arm-at-step", default="s5")
    ap.add_argument("--console-port", type=int, default=8801)
    ap.add_argument("--wait", type=float, default=900.0,
                    help="How long the run stays parked before abandoning.")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    app_port = free_port()
    base_url = f"http://127.0.0.1:{app_port}"
    serve("mockbank.app:app", app_port, "mockbank")

    resolved = retarget(ProfileRepository(REPO / "profiles").resolve(args.tenant),
                        base_url, args.tenant)
    evidence = REPO / "evidence"
    store = InterventionStore(evidence)

    journal = ArmingJournal(at_step=args.arm_at_step, base_url=base_url,
                            tenant=args.tenant, fault=args.fault, verbose=args.verbose)
    broker = InterventionBroker(store, journal=journal, wait_timeout_s=args.wait,
                                evidence_root=evidence)

    console_url = f"http://127.0.0.1:{args.console_port}/"
    serve(create_console(broker), args.console_port, "console")

    rule("M5 — control lease, intervention, takeover")
    print(f"  tenant         {args.tenant}  ({' <- '.join(resolved.lineage)})")
    print(f"  mockbank       {base_url}/t/{args.tenant}/")
    print(f"  console        {console_url}")
    print(f"  fault          {args.fault} (armed at {args.arm_at_step})")
    print(f"  evidence       {evidence}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=args.headless, slow_mo=120)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()

        # Sign in first: a fault armed mid-flow should land on the step being
        # watched, not be spent on the login POST.
        await page.goto(f"{base_url}/t/{args.tenant}/login", wait_until="networkidle")
        await page.fill("input[type=text]", "operator")
        await page.fill("input[type=password]", "demo-pass-not-real")
        await page.click("input[type=submit]")
        await page.wait_for_timeout(400)

        lease = ControlLease(f"sess_{args.tenant}", journal=journal)
        surface = WebSurface(page, journal=journal)
        surface.add_guard(lease)
        broker.register(LiveSession(session_id=lease.session_id, surface=surface,
                                    lease=lease, profile=resolved, journal=journal))

        engine = ReplayEngine(
            surface, lookup_balance_artifact(), resolved,
            journal=journal, tenant=args.tenant, broker=broker, lease=lease,
            goal="read a member's current savings balance",
            credentials=CredentialResolver(
                {"MOCKBANK_USER": "operator", "MOCKBANK_PASS": "demo-pass-not-real"}),
        )

        rule("replay — with a console attached")
        watcher = asyncio.create_task(
            watch_for_park(broker, console_url, engine.run_id))
        try:
            result = await engine.run({"member_id": args.member})
        finally:
            watcher.cancel()

        rule("result")
        print(f"  {BOLD}{type(result).__name__}{RESET} — {result.describe()}")
        print(f"  intervention   {result.intervention_id or '(none filed)'}")
        print(f"  lease          {lease.describe()}")
        print(f"\n  {BOLD}how control changed hands{RESET}")
        for transfer in lease.history:
            print(f"    • {transfer.describe()}")

        humans = journal.of("human.action")
        print(f"\n  {BOLD}what the operator did ({len(humans)} action(s)){RESET}")
        for action in humans:
            print(f"    • {action['action']:<8} {action.get('node_id') or action.get('url', '')}"
                  f"  {DIM}{action['reason']}{RESET}")
        if not humans:
            print(f"    {DIM}(nothing — nobody took control){RESET}")

        handback = journal.of("handback.verified") + journal.of("handback.resync_failed")
        if handback:
            print(f"\n  {BOLD}handback{RESET}")
            for entry in handback:
                print(f"    • target {entry['target_step']} — checked: {entry['checked']}")

        print(f"\n  {DIM}result.json ->{RESET} {to_dict(result)['status']}")
        if not args.headless:
            input(f"\n  {DIM}enter to close the browser{RESET} ")
        await browser.close()

    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
