"""The discovery loop, driven against the live fixture with a scripted model.

No API key, no network. The scripted client lets the tests provoke the paths
that matter and are otherwise unreachable on demand: a refusal, a truncated
turn, a model that clicks the same dead control forever, a model that reaches
for the irreversible button.

What is NOT scripted is the application. Every one of these runs drives the real
mockbank in a real browser, so the observations, the id churn, and the frame
topology are genuine -- only the decisions are fixed.
"""

from __future__ import annotations

import pytest

from cua.discovery.agent import DiscoveryAgent, DiscoveryLimits
from cua.discovery.model import ModelResponse, ScriptedClient, tool_turn
from cua.discovery.tools import TOOLS
from cua.observability import MemoryJournal
from cua.profiles import ProfileRepository
from cua.surfaces.web_playwright import WebSurface

GOAL = "Look up member 12345 and read their current savings balance"


@pytest.fixture
def profile(live_server):
    resolved = ProfileRepository("profiles").resolve("demo-cu")
    surface = resolved.profile.surface.model_copy(
        update={"base_url": f"{live_server}/t/demo-cu"})
    return resolved.__class__(
        profile=resolved.profile.model_copy(update={"surface": surface}),
        lineage=resolved.lineage, hash=resolved.hash,
    )


def node_id(agent, role: str, name_contains: str) -> str:
    """Find a node the way the model does -- by looking at the current screen.

    Ids are regenerated per render by design, so a script cannot hard-code them.
    """
    obs = agent._last_observation
    assert obs is not None, "the agent has not observed anything yet"
    for node in obs.nodes:
        if node.role == role and name_contains.casefold() in (node.name or "").casefold():
            return node.node_id
    raise AssertionError(
        f"no {role} matching {name_contains!r} on screen; saw: "
        + ", ".join(n.describe() for n in obs.nodes if n.role == role)
    )


def build(page, profile, script, **kw) -> DiscoveryAgent:
    client = ScriptedClient(script)
    return DiscoveryAgent(
        WebSurface(page), client, profile,
        goal=GOAL, tenant="demo-cu", journal=MemoryJournal(),
        limits=DiscoveryLimits(max_steps=kw.pop("max_steps", 25)),
        screenshots=kw.pop("screenshots", False),
        **kw,
    )


async def sign_in(page, live_server, tenant="demo-cu"):
    """Sign in, then land on the application shell.

    The shell matters: signing in leaves the browser on a bare page, but the
    real application is a frameset, and `cua discover` always starts from a
    fresh tab so it opens the shell itself. A test that skipped it would be
    exercising a screen layout production never sees.
    """
    await page.goto(f"{live_server}/t/{tenant}/login", wait_until="networkidle")
    await page.fill("input[type=text]", "operator")
    await page.fill("input[type=password]", "demo-pass-not-real")
    await page.click("input[type=submit]")
    await page.wait_for_timeout(300)
    await page.goto(f"{live_server}/t/{tenant}/", wait_until="networkidle")


# --------------------------------------------------------------------------
# the happy path
# --------------------------------------------------------------------------

@pytest.fixture
async def walkthrough(page, profile, live_server):
    """A scripted run that actually completes the lookup goal."""
    await sign_in(page, live_server)
    agent: DiscoveryAgent | None = None

    script = [
        lambda: tool_turn(("click", {
            "node_id": node_id(agent, "link", "Member Search"),
            "reason": "Open the member search screen from the navigation menu"})),
        lambda: tool_turn(("fill", {
            "node_id": node_id(agent, "textbox", ""),
            "text": "12345", "is_param_candidate": True, "param_name": "member_id",
            "reason": "Enter the member id the request asked about"})),
        lambda: tool_turn(("click", {
            "node_id": node_id(agent, "button", "Search"),
            "reason": "Run the search"})),
        lambda: tool_turn(("click", {
            "node_id": node_id(agent, "link", "12345"),
            "reason": "Open the matching member's record"})),
        lambda: tool_turn(("extract", {
            "node_id": node_id(agent, "cell", "4,210.75"),
            "output_name": "savings_balance",
            "reason": "This is the current savings balance the goal asked for"})),
        lambda: tool_turn(("finish", {
            "reason": "The member detail screen shows the savings balance"})),
    ]

    agent = build(page, profile, script)
    return agent


async def test_a_scripted_run_reaches_the_goal_and_records_what_it_did(walkthrough):
    run = await walkthrough.run_discovery()

    assert run.status == "finished", run.stop_reason
    assert run.succeeded
    assert [s.tool for s in run.kept_steps] == ["click", "fill", "click", "click"]
    assert run.params_used() == {"member_id": "12345"}
    assert [e.output_name for e in run.extractions] == ["savings_balance"]


async def test_every_step_carries_the_models_own_reason(walkthrough):
    """The brief asks for a log of what the agent did AND why. `reason` is a
    required parameter on every tool, so the why cannot go missing."""
    run = await walkthrough.run_discovery()

    assert all(s.reason for s in run.steps), "a step with no stated reason"
    assert any("member id" in s.reason.lower() for s in run.steps)

    for tool in TOOLS:
        assert "reason" in tool["input_schema"]["required"], tool["name"]
        assert tool["input_schema"]["additionalProperties"] is False
        assert tool["strict"] is True


async def test_the_model_is_never_offered_a_way_to_write_a_selector():
    """Invariant 2, enforced by the tool schemas rather than by the prompt."""
    for tool in TOOLS:
        keys = set(tool["input_schema"]["properties"])
        assert not (keys & {"selector", "css", "xpath", "query"}), tool["name"]
    targeting = {t["name"] for t in TOOLS if "node_id" in t["input_schema"]["properties"]}
    assert {"click", "fill", "extract"} <= targeting


# --------------------------------------------------------------------------
# stopping conditions -- all four belong to the loop, not to the model
# --------------------------------------------------------------------------

async def test_a_refusal_stops_the_run_cleanly(page, profile, live_server):
    await sign_in(page, live_server)
    agent = build(page, profile, [ModelResponse(stop_reason="refusal")])
    run = await agent.run_discovery()

    assert run.status == "aborted"
    assert "refusal" in run.stop_reason
    assert run.kept_steps == []


async def test_a_truncated_turn_is_not_retried_blind(page, profile, live_server):
    """max_tokens means half a decision arrived. Continuing from it is how a
    loop starts acting on an intent the model never finished forming."""
    await sign_in(page, live_server)
    agent = build(page, profile, [ModelResponse(stop_reason="max_tokens")])
    run = await agent.run_discovery()

    assert run.status == "aborted"
    assert "max_tokens" in run.stop_reason


async def test_the_step_budget_is_the_loops_decision_not_the_models(
    page, profile, live_server
):
    await sign_in(page, live_server)
    script = [lambda: tool_turn(("scroll", {"amount": 10, "reason": "keep going"}))] * 20
    agent = build(page, profile, script, max_steps=4)
    agent.limits.repeat_screen_limit = 99  # scrolling does not change the screen
    run = await agent.run_discovery()

    assert run.status == "budget_exhausted"
    assert len(run.steps) <= 5


async def test_a_model_getting_nowhere_is_detected(page, profile, live_server):
    """Three identical screens in a row. Three rather than two, because a
    legitimate retry after a slow load looks exactly like two."""
    await sign_in(page, live_server)
    script = [lambda: tool_turn(("observe", {"reason": "look again"}))] * 10
    agent = build(page, profile, script)
    run = await agent.run_discovery()

    assert run.status == "stuck"
    assert "did not change" in run.stop_reason


async def test_giving_up_is_a_recorded_result_not_a_crash(page, profile, live_server):
    await sign_in(page, live_server)
    agent = build(page, profile, [
        tool_turn(("give_up", {"reason": "The member search screen is not reachable"}))])
    run = await agent.run_discovery()

    assert run.status == "gave_up"
    assert "not reachable" in run.stop_reason


# --------------------------------------------------------------------------
# safety
# --------------------------------------------------------------------------

async def test_an_irreversible_action_is_refused_at_the_choke_point(
    page, profile, live_server
):
    """The model asks to open an account; policy refuses inside act().

    This is the difference between a guardrail and a prompt instruction. The
    prompt does mention it, but the prompt is not what stops it -- the guard is,
    and it holds whether or not the model cooperates.
    """
    await page.goto(f"{live_server}/t/demo-cu/login", wait_until="networkidle")
    await page.fill("input[type=text]", "operator")
    await page.fill("input[type=password]", "demo-pass-not-real")
    await page.click("input[type=submit]")
    await page.wait_for_timeout(200)
    await page.goto(f"{live_server}/t/demo-cu/members/12345/subaccount/new",
                    wait_until="networkidle")

    agent: DiscoveryAgent | None = None
    script = [
        lambda: tool_turn(("fill", {
            "node_id": node_id(agent, "textbox", ""), "text": "50.00",
            "is_param_candidate": True, "param_name": "initial_deposit",
            "reason": "Enter the opening deposit so the form validates"})),
        lambda: tool_turn(("click", {
            "node_id": node_id(agent, "button", "Continue"),
            "reason": "Continue to the review screen"})),
        lambda: tool_turn(("click", {
            "node_id": node_id(agent, "button", "Confirm and Open Account"),
            "reason": "Open the account"})),
    ]
    agent = build(page, profile, script)
    run = await agent.run_discovery()

    assert run.status == "denied"
    assert "irreversible" in run.stop_reason
    denied = [s for s in run.steps if s.failure_class == "POLICY_DENIED"]
    assert denied and denied[0].pruned, "a refused action must never be recorded as a step"
    assert "policy.denied" in agent.journal.kinds()


async def test_the_same_action_is_permitted_when_the_run_allows_it(
    page, profile, live_server
):
    """The guard is a gate, not a wall: the flag is what a person uses to
    authorise a recording of an irreversible capability."""
    await page.goto(f"{live_server}/t/demo-cu/login", wait_until="networkidle")
    await page.fill("input[type=text]", "operator")
    await page.fill("input[type=password]", "demo-pass-not-real")
    await page.click("input[type=submit]")
    await page.wait_for_timeout(200)
    await page.goto(f"{live_server}/t/demo-cu/members/12345/subaccount/new",
                    wait_until="networkidle")

    agent: DiscoveryAgent | None = None
    script = [
        lambda: tool_turn(("fill", {
            "node_id": node_id(agent, "textbox", ""), "text": "50.00",
            "is_param_candidate": True, "param_name": "initial_deposit",
            "reason": "Enter the opening deposit"})),
        lambda: tool_turn(("click", {
            "node_id": node_id(agent, "button", "Continue"),
            "reason": "Continue to the review screen"})),
        lambda: tool_turn(("click", {
            "node_id": node_id(agent, "button", "Confirm and Open Account"),
            "reason": "Open the account"})),
        lambda: tool_turn(("finish", {"reason": "The confirmation screen is shown"})),
    ]
    agent = build(page, profile, script, allow_irreversible=True)
    run = await agent.run_discovery()

    assert run.status == "finished", run.stop_reason
    assert not [s for s in run.steps if s.failure_class == "POLICY_DENIED"]


async def test_regulated_data_never_reaches_the_model(page, profile, live_server):
    """Invariant 6, asserted against what was actually sent.

    The member detail screen carries an SSN and a date of birth. Discovery's
    entire job is to look at that screen, which makes this the egress that
    matters -- and the one place where "we redact at every egress" is either
    true or a sentence in a README.
    """
    await sign_in(page, live_server)
    await page.goto(f"{live_server}/t/demo-cu/members/12345", wait_until="networkidle")

    agent = build(page, profile, [
        tool_turn(("observe", {"reason": "read the member record"})),
        tool_turn(("finish", {"reason": "done"})),
    ])
    await agent.run_discovery()

    sent = str(agent.client.sent)
    assert "521-84-9077" not in sent, "an SSN reached the model"
    assert "1971-03-04" not in sent, "a date of birth reached the model"
    assert "<redacted>" in sent, "the field should still be visible as existing"
    # The agent must still be able to see that the field is there.
    assert "SSN" in sent


async def test_observations_are_redacted_before_they_are_stored(page, profile, live_server):
    """The transcript is persisted as evidence, so it is redacted at capture
    rather than on the way out -- there is no window where an unredacted
    observation exists inside the agent."""
    await sign_in(page, live_server)
    await page.goto(f"{live_server}/t/demo-cu/members/12345", wait_until="networkidle")

    agent = build(page, profile, [
        tool_turn(("observe", {"reason": "read the member record"})),
        tool_turn(("finish", {"reason": "done"})),
    ])
    run = await agent.run_discovery()

    for step in run.steps:
        for obs in (step.pre, step.post):
            if obs is None:
                continue
            assert not any("521-84-9077" in (n.name or "") for n in obs.nodes)
            assert any(n.sensitive for n in obs.nodes), "nothing was classified on this screen"


# --------------------------------------------------------------------------
# context management
# --------------------------------------------------------------------------

async def test_only_the_most_recent_screenshots_are_kept(page, profile, live_server):
    """Images dominate the context window and a screenshot from ten steps ago
    cannot inform the next click. The text listing stays, so nothing is
    forgotten -- only the picture of it."""
    await sign_in(page, live_server)
    script = [lambda: tool_turn(("observe", {"reason": f"look {i}"})) for i in range(6)]
    script.append(tool_turn(("give_up", {"reason": "enough"})))
    agent = build(page, profile, script, screenshots=True)
    agent.limits.repeat_screen_limit = 99  # not what this test is about
    await agent.run_discovery()

    images = sum(
        1 for message in agent._messages
        if isinstance(message.get("content"), list)
        for part in message["content"] if part.get("type") == "image"
    )
    assert images <= 3, f"{images} screenshots still in context"
    dropped = str(agent._messages).count("screenshot dropped")
    assert dropped >= 1, "older screenshots should have been replaced by a placeholder"


async def test_the_system_prompt_is_marked_cacheable(walkthrough):
    await walkthrough.run_discovery()
    system = walkthrough.client.sent[0]["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}
    # Tool order is part of the cached prefix; it must not vary per turn.
    orders = [[t["name"] for t in call["tools"]] for call in walkthrough.client.sent]
    assert all(o == orders[0] for o in orders)


# --------------------------------------------------------------------------
# R-M4-2: evidence completeness -- no model output is handled silently
# --------------------------------------------------------------------------

async def test_every_tool_call_is_journalled_before_it_is_dispatched(walkthrough):
    """The principle, not the individual bug.

    Journalling per-branch means the branch nobody thought about is the one that
    goes unrecorded. One entry is emitted for every call before anything decides
    what to do with it, so `llm.decision` count equals tool-call count exactly --
    including finish, observe, and calls that turn out to be invalid.
    """
    run = await walkthrough.run_discovery()

    decisions = walkthrough.journal.of("llm.decision")
    assert len(decisions) == len(run.steps), (
        f"{len(decisions)} journal entries for {len(run.steps)} tool calls")
    assert all(e.data["tool"] for e in decisions)
    assert all("reason" in e.data for e in decisions)
    # finish is a decision too; it is the one that ends the run.
    assert decisions[-1].data["tool"] == "finish"


async def test_an_invalid_node_reference_is_recorded_not_silently_answered(
    page, profile, live_server
):
    """A model inventing node ids left gaps in the step numbering and nothing
    else -- which is how a weaker model hallucinating ids stayed invisible."""
    await sign_in(page, live_server)
    agent = build(page, profile, [
        tool_turn(("click", {"node_id": "content:n999", "reason": "click a thing"})),
        tool_turn(("give_up", {"reason": "done"})),
    ])
    run = await agent.run_discovery()

    bad = [s for s in run.steps if s.failure_class == "LOCATOR_UNRESOLVED"]
    assert bad, "the invalid call left no trace"
    assert bad[0].pruned and "not on screen" in bad[0].pruned_because
    assert [s.index for s in run.steps] == list(range(1, len(run.steps) + 1)), \
        "step numbering must have no gaps"


async def test_an_unknown_tool_is_recorded_and_answered(page, profile, live_server):
    await sign_in(page, live_server)
    agent = build(page, profile, [
        tool_turn(("teleport", {"reason": "why not"})),
        tool_turn(("give_up", {"reason": "done"})),
    ])
    run = await agent.run_discovery()

    unknown = [s for s in run.steps if s.tool == "teleport"]
    assert unknown and unknown[0].pruned
    assert "llm.unknown_tool" in agent.journal.kinds()
    assert len(agent.journal.of("llm.decision")) == len(run.steps)


async def test_a_missing_required_field_is_not_coerced_into_a_string(
    page, profile, live_server
):
    """`str(args.get("url"))` turned a missing field into the literal "None" --
    a navigate to "None", a keypress of "None". Strict schemas should prevent
    it; relying on that is how a silent coercion survives."""
    await sign_in(page, live_server)
    agent = build(page, profile, [
        tool_turn(("navigate", {"reason": "go somewhere"})),   # no url
        tool_turn(("give_up", {"reason": "done"})),
    ])
    run = await agent.run_discovery()

    nav = [s for s in run.steps if s.tool == "navigate"][0]
    assert not nav.ok
    assert nav.url != "None", "a missing url became the string 'None'"
    assert "requires a url" in nav.error


async def test_a_turn_with_no_tool_call_keeps_what_the_model_said(
    page, profile, live_server
):
    await sign_in(page, live_server)
    agent = build(page, profile, [
        ModelResponse(stop_reason="end_turn", text="I am not sure how to proceed here.")])
    run = await agent.run_discovery()

    assert run.status == "aborted"
    said = agent.journal.of("llm.no_action")
    assert said and "not sure how to proceed" in said[0].data["text"]


async def test_a_transport_failure_does_not_vaporize_the_transcript(
    page, profile, live_server
):
    """The steps taken before the failure are still evidence, and often the most
    useful kind."""
    await sign_in(page, live_server)

    class Flaky:
        def __init__(self):
            self.calls = 0

        def send(self, **kw):
            self.calls += 1
            if self.calls == 1:
                return tool_turn(("observe", {"reason": "look first"}))
            raise ConnectionError("connection reset by peer")

    agent = DiscoveryAgent(
        WebSurface(page), Flaky(), profile, goal=GOAL, tenant="demo-cu",
        journal=MemoryJournal(), screenshots=False)
    run = await agent.run_discovery()

    assert run.status == "aborted"
    assert "ConnectionError" in run.stop_reason
    assert len(run.steps) == 1, "the step taken before the failure survived"
    assert "llm.transport_error" in agent.journal.kinds()
