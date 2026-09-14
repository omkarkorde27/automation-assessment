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
