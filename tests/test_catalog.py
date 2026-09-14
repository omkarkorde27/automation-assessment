"""The agent-facing capability interface (R-M7-1).

Three claims are under test, and only the first is about plumbing:

  * a catalogued capability is an INVOCABLE one -- drafts are not offered;
  * a tool call's arguments are validated by the artifact's own input spec,
    and a bad one comes back as PARAM_INVALID without a browser being touched;
  * all four result variants render as something a model can read next turn,
    and only `failed` is marked as an error.

The third is the four-variant contract arriving one layer later than usual. A
`business_outcome` that reaches a calling agent as `is_error: true` has been
collapsed into a failure just as surely as one that was thrown away, and the
model will apologise for a system fault when the answer was "that member does
not exist".
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from cua.artifact.authoring import _reads_as_instruction, _restates
from cua.artifact.schema import ApprovalState, CapabilityRisk
from cua.artifact.store import ArtifactStore
from cua.catalog import (
    ToolNotFound, ToolNotInvocable, build_catalog, result_payload, tool_result_block,
)
from cua.cli import app
from cua.observability import MemoryJournal
from cua.profiles import ProfileRepository
from cua.replay import BusinessOutcome, Escalated, Failure, FailureClass, ReplayEngine, Success
from cua.replay.engine import CredentialResolver
from cua.surfaces.web_playwright import WebSurface
from factories import lookup_balance_artifact, open_subaccount_artifact

CREDS = CredentialResolver({"MOCKBANK_USER": "operator", "MOCKBANK_PASS": "demo-pass-not-real"})


def approved(artifact):
    return artifact.model_copy(update={
        "capability": artifact.capability.model_copy(
            update={"approval_state": ApprovalState.APPROVED})})


def draft(artifact):
    return artifact.model_copy(update={
        "capability": artifact.capability.model_copy(
            update={"approval_state": ApprovalState.DRAFT})})


# --------------------------------------------------------------------------
# R-M7-1: approval gates the offer
# --------------------------------------------------------------------------

def test_catalog_excludes_draft_capabilities_by_default():
    """The named regression. A draft is a recording nobody reviewed, and a tool
    definition is an offer -- so it is not made."""
    catalog = build_catalog([draft(lookup_balance_artifact()),
                             approved(open_subaccount_artifact())])

    assert [e.ref for e in catalog.listing()] == ["member.open_subaccount@1.0.0"]
    assert [d["name"] for d in catalog.definitions()] == ["member_open_subaccount"]
    with pytest.raises(ToolNotInvocable):
        catalog.resolve("member_lookup_balance")


def test_include_drafts_shows_them_but_never_offers_them():
    """`--include-drafts` is for a person reading the list. The tools payload is
    unchanged, because that is the thing a model is handed."""
    catalog = build_catalog([draft(lookup_balance_artifact())], include_drafts=True)

    assert len(catalog.listing()) == 1
    assert catalog.listing()[0].invocable is False
    assert catalog.definitions() == []


def test_a_draft_and_a_typo_are_different_facts():
    """An operator reading a log has to be able to tell "you misspelled it" from
    "somebody forgot to approve it"."""
    catalog = build_catalog([draft(lookup_balance_artifact())], include_drafts=True)

    with pytest.raises(ToolNotInvocable) as unapproved:
        catalog.resolve("member_lookup_balance")
    assert "cua approve" in str(unapproved.value)

    with pytest.raises(ToolNotFound):
        catalog.resolve("member_lookup_balanc")


def test_resolve_returns_the_artifact_the_name_came_from():
    catalog = build_catalog([approved(lookup_balance_artifact())])
    assert catalog.resolve("member_lookup_balance").ref == "member.lookup_balance@1.0.0"


def test_cli_catalog_excludes_drafts_by_default(tmp_path):
    """The same rule at the boundary a person actually types."""
    store = ArtifactStore(tmp_path)
    store.save(draft(lookup_balance_artifact()).seal())
    runner = CliRunner()

    default = runner.invoke(app, ["catalog", "--capabilities-root", str(tmp_path)])
    assert default.exit_code == 1
    # Not "nothing recorded" -- the capability exists and is deliberately unoffered.
    assert "0 of 1 recorded capability are approved" in default.output
    assert "cua approve" in default.output

    listed = runner.invoke(app, ["catalog", "--include-drafts",
                                 "--capabilities-root", str(tmp_path)])
    assert listed.exit_code == 0
    assert "member.lookup_balance@1.0.0" in listed.output
    assert "DRAFT -- not offered to callers" in listed.output

    payload = runner.invoke(app, ["catalog", "--json", "--include-drafts",
                                  "--capabilities-root", str(tmp_path)])
    assert json.loads(payload.output) == [], "a draft must never reach the tools payload"


# --------------------------------------------------------------------------
# the tool definition
# --------------------------------------------------------------------------

def test_tool_definition_states_what_comes_back():
    """`output_json_schema()` had no read site until this. A caller was handed
    typed inputs and left to learn the shape of the answer by looking at one."""
    tool = lookup_balance_artifact().as_tool_definition()

    assert "Returns:" in tool["description"]
    for declared in lookup_balance_artifact().outputs:
        assert declared in tool["description"]


def test_tool_definition_says_a_business_outcome_is_not_an_error():
    tool = lookup_balance_artifact().as_tool_definition()
    assert "MEMBER_NOT_FOUND" in tool["description"]
    assert "not errors" in tool["description"]


def test_tool_definition_warns_that_an_irreversible_capability_is_gated():
    """An agent cannot be expected to discover `confirm_irreversible` by having
    a call refused."""
    tool = open_subaccount_artifact().as_tool_definition()
    assert open_subaccount_artifact().capability.risk_tier is CapabilityRisk.WRITES_IRREVERSIBLE
    assert "IRREVERSIBLE" in tool["description"]
    assert "refused" in tool["description"]


def test_tool_names_are_messages_api_legal():
    for artifact in (lookup_balance_artifact(), open_subaccount_artifact()):
        name = artifact.as_tool_definition()["name"]
        assert name == artifact.tool_name
        assert "." not in name and name.replace("_", "").isalnum()


# --------------------------------------------------------------------------
# result -> tool result: the four variants, one layer out
# --------------------------------------------------------------------------

def test_success_renders_the_redacted_outputs_not_the_raw_ones():
    """A tool result IS a prompt on the caller's next turn (invariant 6), so it
    takes the rendering each output's `redact` declared."""
    result = Success(capability_ref="member.lookup_balance@1.0.0",
                     outputs={"account_number": "1234567890"},
                     evidence_outputs={"account_number": "••••7890"})

    payload = result_payload(result)
    assert payload["outputs"] == {"account_number": "••••7890"}
    assert "1234567890" not in json.dumps(payload)


def test_success_fails_closed_when_the_redacted_rendering_is_missing():
    """The same fail-closed rule `Success.to_dict` uses. Falling back to the raw
    values would make the guarantee hold only when somebody remembered it."""
    with pytest.raises(ValueError, match="refusing to hand unredacted"):
        result_payload(Success(outputs={"savings_balance": "4210.75"}))


def test_a_business_outcome_is_not_marked_as_an_error():
    """The trap, at the interface boundary. `is_error` would tell the model it
    did something wrong, which for "no such member" is a lie."""
    block = tool_result_block("toolu_1", BusinessOutcome(
        code="MEMBER_NOT_FOUND", message="No member matches that id.", at_step="s4"))

    assert block["is_error"] is False
    body = json.loads(block["content"])
    assert body["status"] == "business_outcome"
    assert body["code"] == "MEMBER_NOT_FOUND"
    assert "NOT an error" in body["guidance"]


def test_an_escalation_tells_the_caller_not_to_retry():
    block = tool_result_block("toolu_2", Escalated(
        reason_class="UNEXPECTED_STATE", human_message="A permission wall appeared.",
        at_step="s3", intervention_id="iv_abc", resume_token="sess_9"))

    assert block["is_error"] is False
    body = json.loads(block["content"])
    assert body["status"] == "escalated"
    assert body["intervention_id"] == "iv_abc"
    assert body["resume_token"] == "sess_9"
    assert "Do not retry" in body["guidance"]


def test_a_failure_is_an_error_and_says_whether_retrying_helps():
    surface = tool_result_block("toolu_3", Failure(
        failure_class=FailureClass.SURFACE_ERROR, at_step="s3",
        expected="the member record", observed="HTTP 500"))
    assert surface["is_error"] is True
    assert json.loads(surface["content"])["retryable"] is False

    bad_args = tool_result_block("toolu_4", Failure(
        failure_class=FailureClass.PARAM_INVALID,
        detail={"errors": ["'member_id'='abc' does not match ^\\d{5}$"]}))
    body = json.loads(bad_args["content"])
    assert body["retryable"] is True, "the one failure a model can fix by itself"
    assert body["errors"] == ["'member_id'='abc' does not match ^\\d{5}$"]


def test_every_variant_carries_the_run_id_a_reader_would_need():
    for result in (Success(run_id="r1", evidence_outputs={}),
                   BusinessOutcome(run_id="r1"), Escalated(run_id="r1"),
                   Failure(run_id="r1")):
        assert result_payload(result)["run_id"] == "r1"


# --------------------------------------------------------------------------
# the round trip, against the live fixture
# --------------------------------------------------------------------------

@pytest.fixture
def profile(live_server):
    resolved = ProfileRepository("profiles").resolve("demo-cu")
    surface = resolved.profile.surface.model_copy(
        update={"base_url": f"{live_server}/t/demo-cu"})
    return resolved.__class__(
        profile=resolved.profile.model_copy(update={"surface": surface}),
        lineage=resolved.lineage, hash=resolved.hash)


@pytest.fixture
def dispatch(page, profile):
    """What a dispatcher does: a tool name and the model's arguments in, a
    `tool_result` content block out. No model on this path."""
    async def call(catalog, tool_name, arguments, *, tool_use_id="toolu_live"):
        artifact = catalog.resolve(tool_name)
        engine = ReplayEngine(WebSurface(page), artifact, profile,
                              journal=MemoryJournal(), credentials=CREDS, tenant="demo-cu")
        return tool_result_block(tool_use_id, await engine.run(arguments))
    return call


async def test_a_tool_call_with_a_bad_argument_is_param_invalid(dispatch):
    """The artifact's own `^\\d{5}$` is what rejects it, and nothing opens.

    Checked deliberately rather than assumed from the schema being declared
    correctly: the pattern reaching the model's tool definition and the pattern
    being enforced on the model's answer are two different code paths, and only
    one of them keeps a browser closed.
    """
    catalog = build_catalog([approved(lookup_balance_artifact())])
    block = await dispatch(catalog, "member_lookup_balance", {"member_id": "not-an-id"})

    body = json.loads(block["content"])
    assert block["is_error"] is True
    assert body["failure_class"] == "PARAM_INVALID"
    assert body["retryable"] is True
    assert any("does not match" in e for e in body["errors"])
    assert body["at_step"] == "", "nothing ran -- the flow was never entered"


async def test_a_tool_call_that_finds_nothing_comes_back_as_an_answer(dispatch):
    """MEMBER_NOT_FOUND through the whole interface: real browser, real flow,
    and a tool result the model can reason over rather than an error."""
    catalog = build_catalog([approved(lookup_balance_artifact())])
    block = await dispatch(catalog, "member_lookup_balance", {"member_id": "99999"})

    body = json.loads(block["content"])
    assert block["is_error"] is False
    assert body["status"] == "business_outcome"
    assert body["code"] == "MEMBER_NOT_FOUND"


async def test_a_tool_call_that_works_returns_the_typed_output(dispatch):
    catalog = build_catalog([approved(lookup_balance_artifact())])
    block = await dispatch(catalog, "member_lookup_balance", {"member_id": "12345"})

    body = json.loads(block["content"])
    assert block["is_error"] is False
    assert body["status"] == "success"
    assert body["outputs"] == {"savings_balance": "4210.75"}


# --------------------------------------------------------------------------
# R-M7-2: a tool description is not a discovery goal
# --------------------------------------------------------------------------

LOOKUP_GOAL = (
    "Search for member 99999, which does not exist, so you can see and declare how "
    "this application reports a member it cannot find. Then search for member "
    "{member_id} and read their current savings balance.")


def with_goal(artifact, goal, description):
    from cua.artifact.schema import Provenance

    prov = artifact.provenance or Provenance()
    return artifact.model_copy(update={
        "capability": artifact.capability.model_copy(update={"description": description}),
        "provenance": prov.model_copy(update={"discovery_goal": goal}),
    })


def test_a_tool_description_is_not_the_discovery_goal():
    """The named regression. A goal is written to steer an explorer; a
    description is read by the agent that will invoke."""
    assert _restates(LOOKUP_GOAL, LOOKUP_GOAL)
    assert not _restates(
        "Look up a credit-union member by their member id and read the current "
        "balance of their savings account.", LOOKUP_GOAL)


def test_explorer_directed_prose_is_refused_however_it_was_written():
    """Pasting the goal is not the harm -- the instructions inside it are. A
    hand-written description carrying them is exactly as bad."""
    assert _restates("Open a sub-account. First search for a member that does not "
                     "exist so you can see the error.", "")
    assert _reads_as_instruction("…so you can see how it reports that")
    assert not _reads_as_instruction(
        "Open an additional sub-account for an existing member and report the new "
        "account number the application assigns.")


def test_a_well_phrased_goal_does_not_block_a_good_description():
    """The regression in the guard itself. Comparing word sets at 0.75 rejected a
    correct hand-written description of `member.open_subaccount`, whose goal was
    simply well phrased -- measuring subject matter and reading it as authorship."""
    goal = ("Open a new sub-account for member {member_id} with product code "
            "{product_code} and an initial deposit of {initial_deposit}.")
    assert not _restates(
        "Open an additional sub-account for an existing credit-union member, under a "
        "given product code and with a given opening deposit, and report the new "
        "account number the application assigns.", goal)


def test_approve_refuses_a_capability_described_by_its_own_goal(tmp_path):
    """R-M7-2 at the gate. With R-M7-1 -- only approved capabilities are offered
    -- this means a goal-as-description can never reach a calling agent."""
    store = ArtifactStore(tmp_path)
    store.save(with_goal(draft(lookup_balance_artifact()), LOOKUP_GOAL, LOOKUP_GOAL).seal())
    runner = CliRunner()

    refused = runner.invoke(app, ["approve", "member.lookup_balance",
                                  "--capabilities-root", str(tmp_path)])
    assert refused.exit_code == 2
    assert "describes the RECORDING" in refused.output
    assert "cua describe" in refused.output


def test_approve_refuses_a_capability_with_no_description_at_all(tmp_path):
    """The recorder now leaves it empty rather than filling it with the goal, so
    "empty" is the state a fresh recording arrives in."""
    store = ArtifactStore(tmp_path)
    store.save(with_goal(draft(lookup_balance_artifact()), LOOKUP_GOAL, "").seal())

    result = CliRunner().invoke(app, ["approve", "member.lookup_balance",
                                      "--capabilities-root", str(tmp_path)])
    assert result.exit_code == 2
    assert "has no description" in result.output


def test_describe_mints_a_new_version_and_leaves_the_flow_alone(tmp_path):
    store = ArtifactStore(tmp_path)
    original = with_goal(draft(lookup_balance_artifact()), LOOKUP_GOAL, LOOKUP_GOAL).seal()
    store.save(original)

    result = CliRunner().invoke(app, [
        "describe", "member.lookup_balance", "--capabilities-root", str(tmp_path),
        "--title", "Look up a savings balance",
        "--description", "Look up a member by id and read their savings balance."])
    assert result.exit_code == 0, result.output

    assert store.versions("member.lookup_balance") == ["1.0.0", "1.1.0"]
    revised = store.load("member.lookup_balance@1.1.0")
    assert revised.capability.description.startswith("Look up a member by id")
    assert revised.verify_hash() and revised.content_hash != original.content_hash
    # The recording is untouched -- only the contract's prose changed.
    assert [s.id for s in revised.steps] == [s.id for s in original.steps]
    assert revised.provenance.discovery_goal == LOOKUP_GOAL
    assert revised.capability.approval_state is ApprovalState.DRAFT, \
        "a new contract is a new review"


def test_describe_refuses_the_goal_pasted_back_in(tmp_path):
    store = ArtifactStore(tmp_path)
    store.save(with_goal(draft(lookup_balance_artifact()), LOOKUP_GOAL, "").seal())

    result = CliRunner().invoke(app, [
        "describe", "member.lookup_balance", "--capabilities-root", str(tmp_path),
        "--description", LOOKUP_GOAL])
    assert result.exit_code == 2
    assert "restates the discovery goal" in result.output
    assert store.versions("member.lookup_balance") == ["1.0.0"], "nothing was minted"


def test_the_recorder_no_longer_writes_the_goal_as_a_description():
    """The real fix site. `provenance.discovery_goal` keeps the goal; the
    description is left for a reviewer to author."""
    import inspect

    from cua.artifact import recorder

    source = inspect.getsource(recorder)
    assert "description=goal" not in source
    assert "discovery_goal=goal" in source, "the goal must still be kept in provenance"


# --------------------------------------------------------------------------
# R-M7-2: one version per capability
# --------------------------------------------------------------------------

def test_catalog_offers_one_version_per_capability():
    """Two versions of one capability share a tool NAME. The Messages API rejects
    duplicate names outright, and `resolve` would otherwise silently pick
    whichever sorted first -- running an old flow nobody chose."""
    v1 = approved(lookup_balance_artifact())
    v2 = approved(lookup_balance_artifact(
        capability=lookup_balance_artifact().capability.model_copy(
            update={"version": "1.1.0"})))

    catalog = build_catalog([v1, v2])
    assert len(catalog.definitions()) == 1
    assert [e.ref for e in catalog.listing()] == ["member.lookup_balance@1.1.0"]
    assert catalog.resolve("member_lookup_balance").capability.version == "1.1.0"


def test_an_approved_predecessor_outranks_an_unreviewed_new_version():
    """`cua describe` mints a draft. Until somebody reviews it, the approved
    version is still what a caller should be given."""
    base = lookup_balance_artifact()
    old = approved(base)
    new = draft(base.model_copy(update={
        "capability": base.capability.model_copy(update={"version": "2.0.0"})}))

    catalog = build_catalog([old, new])
    assert catalog.resolve("member_lookup_balance").capability.version == "1.0.0"


def test_the_committed_capabilities_are_describable_to_a_caller():
    """The artifacts this repo actually ships. Both were version-bumped after the
    round-trip demo caught the model following a description into a wasted
    replay."""
    catalog = build_catalog(ArtifactStore("capabilities").list(), include_drafts=True)
    assert len(catalog.listing()) == 2

    for entry in catalog.listing():
        meta = entry.artifact.capability
        assert meta.version == "1.1.0"
        assert not _restates(meta.description, entry.artifact.provenance.discovery_goal)
        assert not _reads_as_instruction(entry.artifact.as_tool_definition()["description"])
