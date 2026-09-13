"""Watch a deterministic replay happen in a real, visible browser.

An inspection harness, not part of the system under test. It exists because the
two things M2 and M3 claim are hard to believe from green tests:

  * M2 -- one recording runs against a *different institution* because a tenant
    overlay was merged over the product profile. You should be able to watch the
    same artifact drive a differently-labelled, differently-branded screen.
  * M3 -- the evaluation ladder classifies what it sees. You should be able to
    watch an injected fault hit a live screen and see which of the four result
    variants comes back, with the ladder narrating itself as it goes.

Everything printed here comes from the system's own journal and the M2 profile
resolver. Nothing is staged for the demo.

    # 1. the same capability, on the other tenant, through the merged profile
    uv run python scripts/watch_replay.py --tenant valley-cu

    # 2. the failure path and the recovery path, watchable
    uv run python scripts/watch_replay.py --inject error_500
    uv run python scripts/watch_replay.py --inject session_timeout

`--inject` arms the fault mid-flow (at `--arm-at-step`, default s4) rather than
before the run, so the fault lands on the screen you are watching instead of
being spent during sign-in.
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

from cua.profiles import ProfileRepository, specialize  # noqa: E402
from cua.replay import BusinessOutcome, Escalated, Failure, ReplayEngine, Success  # noqa: E402
from cua.replay.engine import CredentialResolver  # noqa: E402
from cua.surfaces.web_playwright import WebSurface  # noqa: E402
from factories import lookup_balance_artifact  # noqa: E402

# ---------------------------------------------------------------------------
# terminal dressing
# ---------------------------------------------------------------------------

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, YELLOW, RED, BLUE, CYAN = (
    "\033[32m", "\033[33m", "\033[31m", "\033[34m", "\033[36m")


def rule(title: str) -> None:
    print(f"\n{BOLD}{'─' * 78}{RESET}\n{BOLD}{title}{RESET}\n{BOLD}{'─' * 78}{RESET}")


# Journal kinds worth narrating, and how to colour them. Anything not listed is
# printed dim -- the interesting events should stand out while the run scrolls.
LOUD = {
    "step.started": BLUE,
    "action.resolved": CYAN,
    "checkpoint.passed": GREEN,
    "checkpoint.retry": YELLOW,
    "checkpoint.failed": RED,
    "outcome.detected": GREEN,
    "hard_failure.detected": RED,
    "stuck.detected": YELLOW,
    "recovery.attempted": YELLOW,
    "recovery.succeeded": GREEN,
    "recovery.ineffective": RED,
    "session.expired": YELLOW,
    "session.expired_mid_flow": YELLOW,
    "session.reauth_ok": GREEN,
    "step.retrying_after_reauth": YELLOW,
    "run.finished": BOLD,
}


class LiveJournal:
    """A `Journal` that narrates to the terminal as the run happens."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, kind: str, **data: Any) -> None:
        self.events.append((kind, data))
        colour = LOUD.get(kind, DIM)
        detail = " ".join(
            f"{k}={v}" for k, v in data.items()
            if k not in {"run_id", "profile", "profile_hash"} and v not in ("", None, [], {})
        )
        print(f"  {colour}{kind:<28}{RESET} {DIM}{detail[:150]}{RESET}")

    def kinds(self) -> list[str]:
        return [k for k, _ in self.events]

    def of(self, kind: str) -> list[dict]:
        return [d for k, d in self.events if k == kind]


class ArmingJournal(LiveJournal):
    """Narrates, and arms a fault when a named step starts.

    A fault armed before the run is spent on whatever request happens first --
    usually sign-in, which is exactly the screen nobody is watching. Arming on
    `step.started` puts it on the step you care about.
    """

    def __init__(self, *, at_step: str, base_url: str, tenant: str, fault: str, count: int):
        super().__init__()
        self._at_step = at_step
        self._base_url = base_url
        self._tenant = tenant
        self._fault = fault
        self._count = count
        self._armed = False

    def emit(self, kind: str, **data: Any) -> None:
        if not self._armed and kind == "step.started" and data.get("step") == self._at_step:
            self._armed = True
            arm_fault(self._base_url, self._tenant, self._fault, self._count)
            print(f"  {YELLOW}{'>> FAULT ARMED':<28}{RESET} {BOLD}{self._fault}{RESET} "
                  f"{DIM}-- fires on the next guarded request, during {self._at_step}{RESET}")
        super().emit(kind, **data)


# ---------------------------------------------------------------------------
# fixture plumbing
# ---------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_mockbank(port: int) -> str:
    server = uvicorn.Server(
        uvicorn.Config("mockbank.app:app", host="127.0.0.1", port=port, log_level="error")
    )
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise SystemExit("mockbank did not start")
    return f"http://127.0.0.1:{port}"


def arm_fault(base_url: str, tenant: str, fault: str, count: int) -> None:
    httpx.post(f"{base_url}/t/{tenant}/__control",
               params={"fault": fault, "count": count}, timeout=10)


def retarget(resolved, base_url: str, tenant: str):
    """Point the resolved profile at this run's ephemeral port.

    Only `base_url` changes; every merged recovery, detector and sensitivity
    rule is the real thing loaded from disk.
    """
    surface = resolved.profile.surface.model_copy(
        update={"base_url": f"{base_url}/t/{tenant}"})
    return resolved.__class__(
        profile=resolved.profile.model_copy(update={"surface": surface}),
        lineage=resolved.lineage,
        hash=resolved.hash,
    )


# ---------------------------------------------------------------------------
# what M2 produced, printed before anything runs
# ---------------------------------------------------------------------------

def show_profile(resolved, tenant: str) -> None:
    rule(f"M2 — effective profile for {tenant}")
    print(f"  lineage        {' <- '.join(resolved.lineage)}")
    print(f"  profile hash   {resolved.hash}")
    print(f"  base_url       {resolved.base_url}")

    prof = resolved.profile
    base = ProfileRepository(REPO / "profiles").load_raw(prof.extends) if prof.extends else None
    inherited = {r.id for r in base.recoveries} if base else set()
    disabled = set(prof.overrides.disabled_recoveries)

    print(f"\n  {BOLD}recoveries after merge{RESET}")
    for r in prof.recoveries:
        tag = f"{DIM}inherited{RESET}" if r.id in inherited else f"{GREEN}appended by tenant{RESET}"
        print(f"    • {r.id:<34} {tag}")
    for r in sorted(disabled):
        print(f"    • {RED}{r:<34} removed by disabled_recoveries{RESET}")

    if prof.overrides.label_overrides:
        print(f"\n  {BOLD}label_overrides{RESET}")
        for k, v in prof.overrides.label_overrides.items():
            print(f"    {k!r} -> {v!r}")
    if prof.overrides.param_defaults:
        print(f"\n  {BOLD}param_defaults{RESET}   {dict(prof.overrides.param_defaults)}")


def show_specialization(report) -> None:
    rule("M2 — specialize(): what the overlay changed in the artifact")
    print(f"  capability              {report.capability_ref}")
    print(f"  profile lineage         {' <- '.join(report.profile_lineage)}")
    print(f"  profile hash            {report.profile_hash}")
    print(f"  locator_overrides       {report.locator_overrides_applied or '(none)'}")
    print(f"  param_defaults applied  {report.param_defaults_applied or '(none)'}")
    print(f"  recoveries inherited    {report.recoveries_inherited}")
    print(f"  stale/unused overrides  {report.unused_overrides or '(none)'}")
    print(f"\n  {DIM}No locator override is needed for the renamed field: the recorded "
          f"bundle's\n  pattern candidate already spans both tenants' wording. Watch the "
          f"action.resolved\n  lines below to see which candidate actually wins.{RESET}")


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

async def sign_in(page, base_url: str, tenant: str, user: str, password: str) -> None:
    """Establish a session before the flow, visibly.

    Faults are consumed by the next guarded request. Without a warm session that
    request belongs to sign-in, so the fault would fire on a screen nobody is
    watching and be gone before the flow starts.
    """
    await page.goto(f"{base_url}/t/{tenant}/login", wait_until="networkidle")
    await page.fill("input[type=text]", user)
    await page.fill("input[type=password]", password)
    await page.click("input[type=submit]")
    await page.wait_for_timeout(400)


def show_result(result) -> None:
    rule("M3 — result")
    variant = type(result).__name__
    colour = {"Success": GREEN, "BusinessOutcome": GREEN,
              "Escalated": YELLOW, "Failure": RED}[variant]
    print(f"  {BOLD}variant{RESET}          {colour}{BOLD}{variant}{RESET}")

    if isinstance(result, Success):
        print(f"  outputs          {result.outputs}")
    elif isinstance(result, BusinessOutcome):
        print(f"  code             {result.code}")
        print(f"  message          {result.message!r}")
        print(f"  at step          {result.at_step}")
        print(f"  {DIM}ok={result.ok} -- a legitimate answer, not an error{RESET}")
    elif isinstance(result, Escalated):
        print(f"  reason_class     {result.reason_class}")
        print(f"  human_message    {result.human_message!r}")
        print(f"  at step          {result.at_step}")
    elif isinstance(result, Failure):
        print(f"  failure_class    {result.failure_class.value}")
        print(f"  at step          {result.at_step or '-'}")
        print(f"  expected         {result.expected}")
        print(f"  observed         {result.observed}")
        if result.detail:
            print(f"  detail           {result.detail}")

    print(f"\n  {BOLD}step trace{RESET}")
    for s in result.steps:
        mark = f"{GREEN}ok{RESET}" if s.ok else f"{RED}FAILED{RESET}"
        extra = f" via {s.resolved_by}" if s.resolved_by else ""
        extra += f" {YELLOW}(degraded){RESET}" if s.degraded else ""
        extra += f" recoveries={list(s.recoveries)}" if s.recoveries else ""
        print(f"    {s.step_id:<4} {mark:<16} {s.intent[:44]:<46}{extra}")

    print(f"\n  resolution summary  {result.resolution_summary}")
    print(f"  degraded count      {result.degraded_count}")
    print(f"  duration            {result.duration_ms} ms")


async def main(args) -> int:
    base_url = args.base_url or start_mockbank(args.port or free_port())
    print(f"{DIM}mockbank on {base_url}{RESET}")

    resolved = retarget(ProfileRepository(REPO / "profiles").resolve(args.tenant),
                        base_url, args.tenant)
    show_profile(resolved, args.tenant)

    # The M4 recorder does not exist yet, so the hand-authored M3 artifact is
    # sealed here rather than loaded pre-sealed from `capabilities/`.
    artifact = lookup_balance_artifact().seal()
    effective, report = specialize(artifact, resolved)
    show_specialization(report)
    derived = effective.content_hash or "(cleared -- derived, never sealed)"
    print(f"\n  {DIM}sealed artifact hash    {artifact.content_hash}")
    print(f"  effective artifact hash {derived}")
    print(f"  {DIM}Only the recorded artifact gets to be sealed; the tenant-specialized "
          f"form is\n  computed per run and discarded.{RESET}")

    if args.inject:
        journal = ArmingJournal(at_step=args.arm_at_step, base_url=base_url,
                                tenant=args.tenant, fault=args.inject, count=args.count)
    else:
        journal = LiveJournal()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=args.headless, slow_mo=args.slow_mo)
        ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await ctx.new_page()

        if args.inject:
            rule(f"establishing a session first, so '{args.inject}' lands mid-flow")
            await sign_in(page, base_url, args.tenant,
                          args.user, args.password)
            print(f"  {GREEN}signed in{RESET}")

        rule(f"M3 — replaying {effective.ref} on {args.tenant}"
             + (f", injecting {args.inject} at {args.arm_at_step}" if args.inject else ""))

        engine = ReplayEngine(
            WebSurface(page), effective, resolved,
            journal=journal,
            credentials=CredentialResolver({"MOCKBANK_USER": args.user,
                                            "MOCKBANK_PASS": args.password}),
            tenant=args.tenant,
        )
        result = await engine.run({"member_id": args.member})

        show_result(result)

        if not args.headless and not args.no_pause:
            print(f"\n{BOLD}Browser is still open on the final screen. "
                  f"Press Enter to close.{RESET}")
            await asyncio.get_running_loop().run_in_executor(None, input)

        await ctx.close()
        await browser.close()

    return 0 if not isinstance(result, Failure) else 1


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--tenant", default="demo-cu", choices=["demo-cu", "valley-cu"])
    p.add_argument("--member", default="12345", help="member id parameter")
    p.add_argument("--inject", default=None,
                   help="fault to arm mid-flow: error_500, session_timeout, "
                        "interstitial, slow_load, not_found, permission_denied")
    p.add_argument("--arm-at-step", default="s4",
                   help="arm the fault when this step starts (default s4, the search submit)")
    p.add_argument("--count", type=int, default=1, help="how many times the fault fires")
    p.add_argument("--slow-mo", type=int, default=400,
                   help="ms to slow each Playwright operation, so it is watchable")
    p.add_argument("--headless", action="store_true", help="no window (for CI)")
    p.add_argument("--no-pause", action="store_true", help="close the browser immediately")
    p.add_argument("--base-url", default=None,
                   help="use an already-running mockbank instead of starting one")
    p.add_argument("--port", type=int, default=None, help="port for the started mockbank")
    p.add_argument("--user", default="operator")
    p.add_argument("--password", default="demo-pass-not-real")
    return p.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(parse_args())))
