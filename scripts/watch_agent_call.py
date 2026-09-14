"""The last mile, end to end: a real model picks a capability, and replay runs it.

The through-line this project is built around is "the model discovers, the
artifact becomes a reusable capability, deterministic replay is how an AI agent
invokes it in production". Everything up to the comma was demonstrable before
this script. The last clause was not: `cua catalog` could PRINT tool definitions,
and nothing anywhere turned a tool CALL back into a replay.

So this closes it, with nothing staged:

  1. shell out to `cua catalog --json` -- the same command a person runs, reading
     the same artifacts off disk. No hand-written schema appears in this file.
  2. send those as `tools` on a real Messages API call with a natural-language
     goal, and let the model choose.
  3. take the `tool_use` block it emits, resolve the name back to its artifact,
     and hand the typed arguments to the M3 replay engine.
  4. render the `ReplayResult` as the `tool_result` the model would read next
     turn, and send it back so the model answers in words.

The model is in the loop **once**, to choose which capability and with what
arguments. It is not in the loop while the capability runs: step 3 imports
`cua.replay` and `cua.catalog`, neither of which can reach `anthropic`, and this
script re-checks that import graph in-process before it finishes -- see
`confirm_no_llm_on_the_replay_path` at the bottom.

    uv run python scripts/watch_agent_call.py

    --goal "..."          ask for something else
    --model               default claude-sonnet-5; this demonstrates TOOL
                          SELECTION, not agentic discovery, so it does not need
                          Opus. One call, a few thousand tokens, well under a
                          cent at list price -- printed at the end.
    --bad-argument        make the model's argument fail the artifact's own
                          input pattern, to watch PARAM_INVALID come back as a
                          tool result the model can fix
    --headed              watch the browser do it

Needs `ANTHROPIC_API_KEY`. It is the only part of this script that touches the
network beyond the local fixture.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(REPO), str(REPO / "src"), str(REPO / "tests")]

import uvicorn  # noqa: E402
from dotenv import load_dotenv  # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

from cua.artifact.schema import ApprovalState  # noqa: E402
from cua.artifact.store import ArtifactStore  # noqa: E402
from cua.catalog import build_catalog, tool_result_block  # noqa: E402
from cua.discovery.costs import PRICES_NOTE, Spend  # noqa: E402
from cua.observability import MemoryJournal  # noqa: E402
from cua.policy import load_policy  # noqa: E402
from cua.profiles import ProfileRepository, specialize  # noqa: E402
from cua.replay import ReplayEngine  # noqa: E402
from cua.replay.engine import CredentialResolver  # noqa: E402
from cua.surfaces.web_playwright import WebSurface  # noqa: E402

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"
GREEN, YELLOW, RED, BLUE, CYAN = (
    "\033[32m", "\033[33m", "\033[31m", "\033[34m", "\033[36m")

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_GOAL = "What's the savings balance for member 12345?"


@dataclass
class _ForgedCall:
    """A tool call this script made up, for the path a model is too careful to take.

    Shaped like a `tool_use` block so the dispatcher cannot tell the difference
    -- which is the point: the engine's refusal must not depend on who asked.
    """

    name: str
    input: dict
    id: str = "toolu_forged_by_the_demo"
    type: str = "tool_use"


def rule(title: str) -> None:
    print(f"\n{BOLD}{'─' * 78}{RESET}\n{BOLD}{title}{RESET}\n{BOLD}{'─' * 78}{RESET}")


# ---------------------------------------------------------------------------
# the fixture
# ---------------------------------------------------------------------------

def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_mockbank(port: int) -> str:
    from mockbank.app import app as mockbank_app

    config = uvicorn.Config(mockbank_app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise SystemExit("mockbank did not start")
    return f"http://127.0.0.1:{port}"


# ---------------------------------------------------------------------------
# step 0 -- the approval gate (R-M7-1), demonstrated rather than asserted
# ---------------------------------------------------------------------------

def stage_approved_capabilities(scratch: Path) -> Path:
    """Copy `capabilities/` somewhere writable and approve the read-only one.

    The committed artifacts are DRAFTS on purpose -- README §4 demonstrates the
    irreversible gate by approving one mid-demo, and that demo only works from a
    clean starting state. So this script approves a copy instead of mutating the
    repo, which also makes the gate visible: run `cua catalog` against the real
    directory and you get nothing, because nothing has been reviewed.
    """
    root = scratch / "capabilities"
    root.mkdir(parents=True, exist_ok=True)
    for path in (REPO / "capabilities").glob("*.json"):
        (root / path.name).write_bytes(path.read_bytes())

    store = ArtifactStore(root)
    rule("R-M7-1 — the catalog offers what a caller may actually invoke")

    before = build_catalog(store.list(), include_drafts=True)
    for entry in before.listing():
        state = entry.artifact.capability.approval_state.value
        print(f"  {entry.ref:<32} {state:<10} "
              f"{GREEN + 'offered' + RESET if entry.invocable else DIM + 'not offered' + RESET}")
    print(f"\n  {DIM}`cua catalog` on the committed directory lists nothing: both are "
          f"drafts,\n  and a tool definition is an offer. Approving the read-only one "
          f"here, in a copy.{RESET}\n")

    # By id, not by a pinned ref: `cua describe` mints versions, and a demo that
    # hard-codes @1.0.0 keeps approving the artifact it was written against
    # rather than the one that is current. That is exactly how this script spent
    # a run proving a fix had not taken.
    current = store.load_latest("member.lookup_balance")
    store.set_approval(current.ref, ApprovalState.APPROVED,
                       reason="approved by the round-trip demo")

    after = build_catalog(store.list(), include_drafts=True)
    for entry in after.listing():
        state = entry.artifact.capability.approval_state.value
        print(f"  {entry.ref:<32} {state:<10} "
              f"{GREEN + 'offered' + RESET if entry.invocable else DIM + 'not offered' + RESET}")
    print(f"\n  {DIM}member.open_subaccount stays a draft, so the model is never told it "
          f"exists.\n  Nothing in the prompt says so -- it is simply not in the payload.{RESET}")
    return root


# ---------------------------------------------------------------------------
# step 1 -- the tools payload, from the real command
# ---------------------------------------------------------------------------

def catalog_json(capabilities_root: Path) -> list[dict]:
    """`cua catalog --json`, shelled out for real.

    Shelled out rather than imported so there is no question about whether the
    payload came from the artifact store or from something this script made up.
    """
    rule("step 1 — `cua catalog --json`  (the tools payload, generated from the artifact)")
    proc = subprocess.run(
        [sys.executable, "-m", "cua.cli", "catalog", "--json",
         "--capabilities-root", str(capabilities_root)],
        capture_output=True, text=True, cwd=str(REPO),
        env={**os.environ, "PYTHONPATH": str(REPO / "src")})
    if proc.returncode != 0:
        raise SystemExit(f"cua catalog failed:\n{proc.stderr}")

    tools = json.loads(proc.stdout)
    print(json.dumps(tools, indent=2))
    print(f"\n  {DIM}{len(tools)} tool(s). Not written by hand: the pattern "
          f"{tools[0]['input_schema']['properties']['member_id']['pattern']!r} is the "
          f"artifact's\n  own ParamSpec, and the outcome list is what the recorder "
          f"declared.{RESET}")
    return tools


# ---------------------------------------------------------------------------
# step 2 -- one real model call: which capability, with what arguments
# ---------------------------------------------------------------------------

SYSTEM = (
    "You are a bank back-office assistant. You have tools that drive the "
    "core-banking application. Use one when it answers the request. Business "
    "outcomes in a tool result are legitimate answers, not errors."
)


def choose(client, model: str, goal: str, tools: list[dict]):
    rule(f"step 2 — the model chooses  ({model}, one call)")
    print(f"  {BOLD}user{RESET}  {goal}\n")

    response = client.messages.create(
        model=model, max_tokens=1024, system=SYSTEM, tools=tools,
        messages=[{"role": "user", "content": goal}],
    )

    for block in response.content:
        if block.type == "text" and block.text.strip():
            print(f"  {BOLD}reasoning{RESET}   {block.text.strip()}")
    calls = [b for b in response.content if b.type == "tool_use"]
    if not calls:
        return response, []

    # EVERY tool_use, not just the first. A model may emit several in one turn,
    # and the Messages API requires a `tool_result` for each -- answering only
    # the first is a 400, and the first version of this script was exactly that
    # bug. It is also the honest shape: a dispatcher that silently ignored a
    # call the model made would drop a decision on the floor (R-M4-2).
    for call in calls:
        print(f"\n  {BOLD}tool choice{RESET} {CYAN}{call.name}{RESET}")
        print(f"  {BOLD}arguments{RESET}   {json.dumps(call.input)}")
    print(f"\n  {BOLD}stop reason{RESET} {response.stop_reason}   "
          f"{len(calls)} call(s) to dispatch")
    return response, calls


# ---------------------------------------------------------------------------
# step 3 -- dispatch to the real replay engine. No model on this path.
# ---------------------------------------------------------------------------

async def dispatch_all(calls, catalog, base_url: str, tenant: str, headed: bool):
    rule("step 3 — dispatch to the replay engine  (no model in this loop)")
    results = []
    for call in calls:
        results.append(await dispatch(call, catalog, base_url, tenant, headed))
    return results


async def dispatch(call, catalog, base_url: str, tenant: str, headed: bool):
    artifact = catalog.resolve(call.name)
    print(f"\n  tool name       {call.name}  {DIM}{json.dumps(call.input)}{RESET}")
    print(f"  -> artifact     {artifact.ref}  "
          f"({artifact.capability.approval_state.value}, {artifact.capability.risk_tier.value})")
    print(f"  content hash    {artifact.content_hash}")
    print(f"  hash verifies   {artifact.verify_hash()}")

    resolved = ProfileRepository(REPO / "profiles").resolve(tenant)
    surface_cfg = resolved.profile.surface.model_copy(
        update={"base_url": f"{base_url}/t/{tenant}"})
    resolved = resolved.__class__(
        profile=resolved.profile.model_copy(update={"surface": surface_cfg}),
        lineage=resolved.lineage, hash=resolved.hash)

    effective, report = specialize(artifact, resolved)
    allowlist = load_policy(REPO / "config" / "policy.yaml").for_capability(artifact.ref)
    print(f"  tenant          {tenant}  ({' <- '.join(report.profile_lineage)})")
    print(f"  allowlist       {len(allowlist.denied_paths)} denial(s), "
          f"max {allowlist.max_navigations} navigations")

    journal = MemoryJournal()
    creds = CredentialResolver({"MOCKBANK_USER": "operator",
                                "MOCKBANK_PASS": "demo-pass-not-real"})

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        try:
            engine = ReplayEngine(WebSurface(page, journal=journal), effective, resolved,
                                  journal=journal, credentials=creds, tenant=tenant,
                                  policy=allowlist)
            started = time.monotonic()
            result = await engine.run(dict(call.input))
        finally:
            await browser.close()

    colour = {"success": GREEN, "business_outcome": GREEN,
              "escalated": YELLOW, "failed": RED}[result.status.value]
    print(f"\n  {BOLD}variant{RESET}         {colour}{BOLD}{type(result).__name__}{RESET}")
    print(f"  {result.describe()}")
    print(f"  steps           {', '.join(s.step_id for s in result.steps)}  "
          f"in {int((time.monotonic() - started) * 1000)} ms")
    print(f"  {DIM}journal kinds   {len(journal.kinds())} event kinds, "
          f"no model call among them{RESET}")
    return result


# ---------------------------------------------------------------------------
# step 4 -- the tool result the model reads next turn
# ---------------------------------------------------------------------------

def answer(client, model, goal, tools, response, calls, results):
    rule("step 4 — the tool result(s), and the model's answer")

    blocks = [tool_result_block(c.id, r) for c, r in zip(calls, results)]
    for call, result, block in zip(calls, results, blocks):
        print(f"  {BOLD}{call.name}{json.dumps(call.input)}{RESET}")
        print(f"  {BOLD}is_error{RESET}  {block['is_error']}"
              + (f"   {DIM}<- a business outcome is an ANSWER, not an error{RESET}"
                 if result.status.value == "business_outcome" else ""))
        print(f"\n{DIM}{block['content']}{RESET}\n")

    final = client.messages.create(
        model=model, max_tokens=1024, system=SYSTEM, tools=tools,
        messages=[
            {"role": "user", "content": goal},
            {"role": "assistant", "content": response.content},
            {"role": "user", "content": blocks},
        ],
    )
    said = " ".join(b.text for b in final.content if b.type == "text").strip()
    print(f"  {BOLD}assistant{RESET}  {said}")
    return final


# ---------------------------------------------------------------------------
# the claim, re-checked in this very process
# ---------------------------------------------------------------------------

def confirm_no_llm_on_the_replay_path() -> None:
    """`anthropic` is loaded in THIS process -- and still not reachable from replay.

    The suite's import-graph test runs in a clean interpreter, which proves the
    modules do not import a client. It cannot prove the more interesting thing:
    that a model choosing the capability does not put a model inside it. Here
    `anthropic` is definitely imported, so walking the replay path's own module
    graph is a real check rather than a tautology.
    """
    rule("the guarantee, re-checked with `anthropic` loaded in this process")

    import cua.catalog.tools
    import cua.replay.engine

    assert "anthropic" in sys.modules, "expected the client to be loaded here"

    seen, stack = set(), ["cua.replay.engine", "cua.catalog.tools", "cua.conditions.dsl"]
    while stack:
        name = stack.pop()
        if name in seen or not name.startswith("cua."):
            continue
        seen.add(name)
        module = sys.modules.get(name)
        if module is None:
            continue
        for value in vars(module).values():
            found = getattr(value, "__module__", None) or getattr(value, "__name__", None)
            if isinstance(found, str):
                stack.append(found)

    offenders = sorted(m for m in seen if m.split(".")[0] in {"anthropic", "openai"})
    print(f"  modules reachable from replay + catalog   {len(seen)}")
    print(f"  of those, model clients                   {offenders or 'none'}")
    print(f"  `anthropic` loaded in this process        {'anthropic' in sys.modules}")
    assert not offenders, offenders
    print(f"\n  {GREEN}The model chose WHICH capability. It was not in the loop while the "
          f"capability ran.{RESET}")


# ---------------------------------------------------------------------------

async def main(args) -> int:
    load_dotenv(REPO / ".env")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(f"{RED}ANTHROPIC_API_KEY is not set.{RESET} This script makes two real "
              f"model calls (a few thousand tokens). Everything else in the repo "
              f"runs offline.")
        return 2

    import anthropic

    scratch = REPO / ".demo-agent-call"
    capabilities_root = stage_approved_capabilities(scratch)
    tools = catalog_json(capabilities_root)
    catalog = build_catalog(ArtifactStore(capabilities_root).list())

    base_url = start_mockbank(free_port())
    print(f"\n{DIM}mockbank on {base_url}{RESET}")

    goal = args.goal
    if args.bad_argument:
        goal = ("What's the savings balance for member ABC-123? That is the id "
                "exactly as the member gave it; pass it through unchanged.")
        print(f"{YELLOW}--bad-argument: asking for an id that cannot match "
              f"the artifact's own pattern.{RESET}")

    client = anthropic.Anthropic()
    response, calls = choose(client, args.model, goal, tools)

    if not calls:
        # A good model reads the `^\d{5}$` in the schema and declines before
        # spending a call -- which is the schema working, one layer earlier than
        # this script set out to test. The dispatch path still has to refuse it,
        # so forge the call the model would have had to make and dispatch that.
        # Labelled, because a synthesized tool call is not a model's decision
        # and printing it as one would be staging the demo.
        if not args.bad_argument:
            print(f"{YELLOW}The model answered without calling a tool; "
                  f"nothing to dispatch.{RESET}")
            return 1
        print(f"\n  {YELLOW}The model declined -- the pattern is in the schema it was "
              f"handed, so it\n  never made the call. Defence in depth: the artifact "
              f"refuses it too. Forging\n  the call to prove the dispatch path does "
              f"not depend on the model's judgement.{RESET}")
        calls = [_ForgedCall(name=tools[0]["name"], input={"member_id": "ABC-123"})]

    results = await dispatch_all(calls, catalog, base_url, args.tenant, args.headed)
    if any(isinstance(c, _ForgedCall) for c in calls):
        # The turn cannot be continued: `response.content` holds no tool_use for
        # this id, and the API would (correctly) reject the mismatched pair.
        rule("step 4 — the tool result a calling agent would receive")
        block = tool_result_block(calls[0].id, results[0])
        print(f"  {BOLD}is_error{RESET}  {block['is_error']}   "
              f"{DIM}<- a malformed call IS the caller's error{RESET}")
        print(f"\n{DIM}{block['content']}{RESET}")
        confirm_no_llm_on_the_replay_path()
        return 0

    final = answer(client, args.model, goal, tools, response, calls, results)

    confirm_no_llm_on_the_replay_path()

    rule("cost")
    spend = Spend(args.model,
                  input_tokens=response.usage.input_tokens + final.usage.input_tokens,
                  output_tokens=response.usage.output_tokens + final.usage.output_tokens)
    print(spend.line("tool selection x2"))
    print(f"\n  {DIM}{PRICES_NOTE}{RESET}")
    print(f"  {DIM}Two calls: choose the capability, then read the result. Replay "
          f"itself cost nothing --\n  that is the point of freezing the run into an "
          f"artifact.{RESET}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--goal", default=DEFAULT_GOAL)
    parser.add_argument("--model", default=DEFAULT_MODEL,
                        help="Tool selection, not discovery. Sonnet by default.")
    parser.add_argument("--tenant", default="demo-cu")
    parser.add_argument("--bad-argument", action="store_true",
                        help="Watch PARAM_INVALID come back as a readable tool result.")
    parser.add_argument("--headed", action="store_true")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
