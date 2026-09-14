"""Exercise the agent loop's control logic without calling a model.

The dead-end detector shipped with a bug that fired on a *working* run: the
state hash is over roles and names, not values, so `fill` and `select_option`
never change it, and any form with three fields reported "the screen did not
change across 3 consecutive actions". It was found by a real Opus run against
the sub-account flow -- about 55,000 input tokens to learn something a scripted
sequence proves in under a second.

That is the argument for this module. The control logic -- budgets, stopping
conditions, the dead-end detector, tool schemas, the policy choke point -- is
entirely decidable without a model. The model's *judgement* needs the real
thing; its scaffolding does not. So the scaffolding is checked first, for free,
and `cua discover` refuses to spend a real run until it passes.

These are the same checks `tests/test_discovery_loop.py` makes. They live here
too, and run as a preflight rather than only in CI, because the failure this
prevents is expensive precisely when nobody thought to run the tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from playwright.async_api import async_playwright

from ..observability.journal import MemoryJournal
from ..profiles.resolve import ResolvedProfile
from ..surfaces.web_playwright import WebSurface
from .agent import DiscoveryAgent, DiscoveryLimits
from .model import ModelResponse, ScriptedClient, tool_turn
from .tools import TOOLS


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class DryRunReport:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def render(self, indent: str = "") -> str:
        lines = [f"{indent}{'PASS' if c.ok else 'FAIL'}  {c.name}"
                 + (f"  -- {c.detail}" if c.detail else "") for c in self.checks]
        passed = sum(1 for c in self.checks if c.ok)
        lines.append(f"{indent}{passed}/{len(self.checks)} control checks passed "
                     f"(0 API calls, $0.00)")
        return "\n".join(lines)


# ---- schema checks: no browser, no network --------------------------------

def check_tool_schemas() -> list[Check]:
    checks: list[Check] = []

    missing = [t["name"] for t in TOOLS if "reason" not in t["input_schema"]["required"]]
    checks.append(Check(
        "every tool requires a `reason`", not missing,
        f"missing on {missing}" if missing else "the 'why' cannot go absent"))

    loose = [t["name"] for t in TOOLS
             if t["input_schema"].get("additionalProperties") is not False
             or not t.get("strict")]
    checks.append(Check("every tool schema is strict and closed", not loose,
                        f"loose: {loose}" if loose else ""))

    selectorish = [t["name"] for t in TOOLS
                   if set(t["input_schema"]["properties"]) & {"selector", "css", "xpath", "query"}]
    checks.append(Check("no tool lets the model write a selector", not selectorish,
                        f"offenders: {selectorish}" if selectorish else "invariant 2"))
    return checks


# ---- loop checks: real browser, real fixture, scripted decisions -----------

async def _drive(profile: ResolvedProfile, script, *, limits=None, page=None):
    agent = DiscoveryAgent(
        WebSurface(page), ScriptedClient(script), profile,
        goal="dry run", tenant="dry-run", journal=MemoryJournal(),
        limits=limits or DiscoveryLimits(), screenshots=False,
    )
    return await agent.run_discovery(), agent


async def run_control_checks(profile: ResolvedProfile) -> DryRunReport:
    """Every stopping condition, driven against the live fixture."""
    report = DryRunReport(checks=check_tool_schemas())

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        try:
            context = await browser.new_context(viewport={"width": 1280, "height": 900})
            page = await context.new_page()

            # 1. A form is not a dead end. THE regression this module exists for:
            #    typing does not change the state hash by construction, so an
            #    agent filling three fields correctly must not be called stuck.
            fills = [lambda: tool_turn(("fill", {
                "node_id": "content:n2", "text": str(i), "is_param_candidate": False,
                "param_name": "", "reason": "fill a field"})) for i in range(4)]
            run, _ = await _drive(profile, [*fills, tool_turn(
                ("give_up", {"reason": "done"}))], page=page)
            report.checks.append(Check(
                "filling a form is not mistaken for a dead end",
                run.status != "stuck",
                "typing never changes the state hash; counting it reported working "
                "runs as stuck" if run.status != "stuck" else f"status={run.status}"))

            # 2. A model that really is going nowhere IS caught.
            run, _ = await _drive(profile, [
                lambda: tool_turn(("observe", {"reason": "look again"}))] * 8, page=page)
            report.checks.append(Check(
                "a genuinely stuck agent is detected", run.status == "stuck",
                f"status={run.status}"))

            # 3. The budget belongs to the loop, not the model.
            run, _ = await _drive(
                profile,
                [lambda: tool_turn(("scroll", {"amount": 10, "reason": "again"}))] * 30,
                limits=DiscoveryLimits(max_steps=3, repeat_screen_limit=99), page=page)
            report.checks.append(Check(
                "the step budget stops the run", run.status == "budget_exhausted",
                f"status={run.status}"))

            # 4. stop_reason is read before content.
            run, _ = await _drive(profile, [ModelResponse(stop_reason="refusal")], page=page)
            refusal_ok = run.status == "aborted"
            run, _ = await _drive(profile, [ModelResponse(stop_reason="max_tokens")], page=page)
            report.checks.append(Check(
                "a refusal or truncated turn ends the run cleanly",
                refusal_ok and run.status == "aborted", f"status={run.status}"))

            # 5. The policy choke point actually has a guard installed.
            _, agent = await _drive(profile, [tool_turn(
                ("give_up", {"reason": "done"}))], page=page)
            installed = agent.guard in getattr(agent.surface, "_guards", [])
            report.checks.append(Check(
                "the irreversible guard is installed on the surface", installed,
                "" if installed else "the guard was built and never registered"))

            await context.close()
        finally:
            await browser.close()

    return report
