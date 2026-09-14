"""Artifact -> tool definition, and tool call -> replay result -> tool result.

Three things live here, and they are deliberately the only three:

  `build_catalog`      which capabilities a calling agent may be offered
  `ToolCatalog.resolve`  a tool NAME back to the artifact it came from
  `tool_result_block`  a `ReplayResult` rendered as something a model can read

What is absent matters as much. There is no browser here, no profile resolution
and no engine construction: dispatch is the caller's job because the caller owns
the session (`cli.py` for `cua replay`, `scripts/watch_agent_call.py` for the
round trip, a service for anything real). A module that opened its own browser
would make "the human takes over the *same* session" impossible the moment an
agent was the one who started the run.


R-M7-1 -- a catalogued capability is an invocable one
-----------------------------------------------------
The catalog lists only `approved` capabilities by default. Drafts are shown by
`include_drafts=True`, marked, for review.

A tool definition is an offer. Listing a draft tells a model "you may call
this", and the call then meets a gate the definition never mentioned. The two
ways that goes wrong are not symmetrical:

  * a `writes_irreversible` draft fails loudly, as `IRREVERSIBLE_NOT_AUTHORIZED`
    -- annoying, and the model has no way to act on it, because approval is a
    human act it cannot perform;
  * a `read_only` draft **succeeds**, which is worse. Nothing refuses it, so
    unreviewed automation runs against a production back-office app and the only
    evidence that the review gate was skipped is a field nobody read.

So the gate is moved to where the disagreement is cheap: the catalog and the
replay-side gate now agree about what is callable. `approval_state` stops being
a label and becomes the thing that decides whether a capability is offered at
all. This is not a new refusal -- replay still runs a read-only draft when asked
directly by `cua replay`, which is the reviewer's own path -- it is a narrowing
of what an *agent* is ever told exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from ..artifact.schema import ApprovalState, CapabilityArtifact
from ..replay.result import BusinessOutcome, Escalated, Failure, ReplayResult, Success


class ToolNotFound(LookupError):
    """No catalogued capability answers to that tool name."""


class ToolNotInvocable(PermissionError):
    """The name resolves, but this artifact is not cleared for invocation.

    Distinct from `ToolNotFound` on purpose. "There is no such tool" and "that
    tool exists and has not been reviewed" are different facts about the world,
    and an operator reading a log needs to be able to tell a typo from a
    capability somebody forgot to approve.
    """


def tool_name_for(capability_id: str) -> str:
    """`member.lookup_balance` -> `member_lookup_balance`.

    A thin alias for `CapabilityArtifact.tool_name`, which is where the rule
    lives -- the name is a property of the capability contract, and the catalog
    only needs to map it back. Kept as a function for callers holding an id and
    no artifact.
    """
    return capability_id.replace(".", "_").replace("/", "_")


@dataclass(frozen=True)
class CatalogEntry:
    artifact: CapabilityArtifact
    invocable: bool
    """False only for a draft surfaced by `include_drafts`. An entry that is not
    invocable is shown to a person, never offered to a model."""

    @property
    def tool_name(self) -> str:
        return self.artifact.tool_name

    @property
    def ref(self) -> str:
        return self.artifact.ref


@dataclass(frozen=True)
class ToolCatalog:
    entries: tuple[CatalogEntry, ...]
    """EVERY artifact the catalog was built from, approved or not.

    Deliberately not pre-filtered. `include_drafts` decides what is *listed*;
    what is *offered* is decided by `invocable`; and `resolve` can see both, so
    a dispatcher can still say "that capability exists and nobody approved it"
    about a tool it never offered. Dropping drafts here instead would make the
    two collapse into "no such tool", which is the wrong answer to give an
    operator staring at the capability in question.
    """

    include_drafts: bool = False

    # ---- what a model is handed ----------------------------------------

    def definitions(self) -> list[dict]:
        """The Messages API `tools` payload. Invocable entries only, always.

        `include_drafts` has no effect here on purpose: it exists so a person
        can read the list, never so a model can call one.
        """
        return [e.artifact.as_tool_definition() for e in self.entries if e.invocable]

    def listing(self) -> list[CatalogEntry]:
        """What a person is shown -- drafts included only when asked for."""
        return [e for e in self.entries if e.invocable or self.include_drafts]

    # ---- what comes back -----------------------------------------------

    def resolve(self, tool_name: str) -> CapabilityArtifact:
        """A tool name from a model's `tool_use` block -> the artifact to replay.

        Raises rather than returning None. A dispatcher that silently skipped an
        unrecognised name would drop a decision on the floor, which is the thing
        R-M4-2 refuses anywhere untrusted input is interpreted -- and a tool name
        chosen by a model is untrusted input.
        """
        for entry in self.entries:
            if entry.tool_name == tool_name:
                if not entry.invocable:
                    raise ToolNotInvocable(
                        f"{entry.ref} is {entry.artifact.capability.approval_state.value}, "
                        f"not approved. `cua approve {entry.artifact.capability.id}` is the "
                        f"gate; it is a human act, not something a caller can assert.")
                return entry.artifact
        offered = sorted(e.tool_name for e in self.entries if e.invocable)
        raise ToolNotFound(f"no capability named {tool_name!r}. Offered: {offered}")

    def names(self) -> list[str]:
        return [e.tool_name for e in self.listing()]

    def __len__(self) -> int:
        return len(self.listing())


def build_catalog(artifacts: Iterable[CapabilityArtifact], *,
                  include_drafts: bool = False) -> ToolCatalog:
    """The capabilities a calling agent may be offered. See R-M7-1 above.

    Takes artifacts rather than a store so a caller can catalogue anything it
    already holds -- a test's factories, one artifact under review -- without
    the module needing to know where capabilities are kept.

    R-M7-2 -- ONE VERSION PER CAPABILITY, the highest that is invocable.
    `ArtifactStore.list()` returns every version on disk, and a tool name is
    derived from the capability id alone, so two versions of one capability
    produce two tool definitions with the same `name`. The Messages API rejects
    that outright, and `resolve()` would otherwise return whichever happened to
    sort first -- silently running an old flow. An agent is offered
    `member_lookup_balance`, not a version picker: choosing between 1.0.0 and
    1.1.0 is a decision about which contract is current, which is a human's to
    make by approving one. `cua replay id@version` still addresses any version
    directly, because a person debugging needs exactly that.

    "Highest INVOCABLE" rather than "highest": a newly minted version starts as
    a draft, and until somebody reviews it the approved predecessor is still
    what a caller should get. Falling back to the highest overall when none is
    approved keeps `--include-drafts` showing the version under review.
    """
    best: dict[str, CapabilityArtifact] = {}
    for artifact in artifacts:
        cid = artifact.capability.id
        current = best.get(cid)
        if current is None or _precedence(artifact) > _precedence(current):
            best[cid] = artifact

    entries = tuple(
        CatalogEntry(artifact=a,
                     invocable=a.capability.approval_state is ApprovalState.APPROVED)
        for a in sorted(best.values(), key=lambda a: a.capability.id))
    return ToolCatalog(entries=entries, include_drafts=include_drafts)


def _precedence(artifact: CapabilityArtifact) -> tuple:
    """Approved beats unapproved; among equals, the higher semver wins."""
    version = artifact.capability.version
    try:
        parts = tuple(int(p) for p in version.split("."))
    except ValueError:
        parts = (0, 0, 0)
    approved = artifact.capability.approval_state is ApprovalState.APPROVED
    return (approved, parts)


# ---------------------------------------------------------------------------
# result -> tool result
# ---------------------------------------------------------------------------

#: Guidance appended per variant, addressed to the model that will read it next
#: turn. The four-variant contract is worth nothing if the caller cannot tell
#: which of the four it got, and a model reading a bare JSON blob will guess.
_GUIDANCE = {
    "success": "The capability completed. `outputs` are the values you asked for.",
    "business_outcome": (
        "This is a legitimate answer from the application, NOT an error and NOT "
        "a reason to retry. Report it to the user as the answer."),
    "escalated": (
        "The run could not continue and a person has been asked to take over the "
        "live session. Do not retry -- the session is parked, not lost. Report "
        "that a human is handling it and cite the intervention id."),
    "failed": (
        "The run broke. Retry only if `retryable` is true -- otherwise this needs "
        "a person to debug, and calling again will break the same way."),
}

#: Failure classes a caller can do something about by calling again with
#: different arguments. Everything else is somebody's bug or the app's fault,
#: and a model retrying it just spends a second run reaching the same screen.
_RETRYABLE = {"PARAM_INVALID"}


def result_payload(result: ReplayResult) -> dict[str, Any]:
    """A `ReplayResult` as a JSON-able dict addressed to a calling agent.

    Secrets never reach a prompt (invariant 6), and a tool result IS a prompt on
    the caller's next turn. So `Success` is rendered from `evidence_outputs` --
    each value through its declared `OutputSpec.redact` -- and not from
    `outputs`, which is the unredacted rendering an in-process caller gets by
    holding the result object itself.

    Part 3.3 words the boundary as "persistence, not the return value", and that
    is still true of the return value: `Success.outputs` is untouched. This is
    about the second egress M6 found -- handing values to a model is handing
    them to something that will quote them back, which is persistence by another
    name. Fails CLOSED, exactly as `Success.to_dict` does, rather than quietly
    falling back to the unredacted values.
    """
    common = {
        "capability": result.capability_ref,
        "tenant": result.tenant,
        "run_id": result.run_id,
        "duration_ms": result.duration_ms,
    }
    if result.intervention_id:
        common["intervention_id"] = result.intervention_id

    if isinstance(result, Success):
        if result.outputs and not result.evidence_outputs:
            raise ValueError(
                f"{result.capability_ref} produced outputs with no redacted "
                f"rendering; refusing to hand unredacted values to a model.")
        return {**common, "status": "success", "outputs": dict(result.evidence_outputs),
                "guidance": _GUIDANCE["success"]}

    if isinstance(result, BusinessOutcome):
        return {**common, "status": "business_outcome", "code": result.code,
                "message": result.message, "at_step": result.at_step,
                "guidance": _GUIDANCE["business_outcome"]}

    if isinstance(result, Escalated):
        return {**common, "status": "escalated", "reason": result.reason_class,
                "detail": result.human_message, "at_step": result.at_step,
                "resume_token": result.resume_token,
                "guidance": _GUIDANCE["escalated"]}

    if isinstance(result, Failure):
        cls = result.failure_class.value
        payload = {**common, "status": "failed", "failure_class": cls,
                   "at_step": result.at_step, "expected": result.expected,
                   "observed": result.observed,
                   "retryable": cls in _RETRYABLE,
                   "guidance": _GUIDANCE["failed"]}
        if cls == "PARAM_INVALID":
            # The one failure a model can actually fix, so it gets the reasons
            # rather than a summary. `validate_params` already redacted any
            # value it reported for a `sensitive` parameter.
            payload["errors"] = list(result.detail.get("errors", ()))
        return payload

    raise TypeError(f"not a replay result: {type(result).__name__}")


def tool_result_block(tool_use_id: str, result: ReplayResult) -> dict[str, Any]:
    """The `tool_result` content block to send back in the next Messages turn.

    `is_error` is set for `failed` ONLY. A business outcome and an escalation are
    both answers about the world -- the call was well-formed and the system did
    its job -- and marking them as errors is precisely the collapse the
    four-variant contract exists to prevent, arriving one layer later than
    usual. It would also be read by the model as "you did something wrong",
    which for "that member does not exist" is a lie.
    """
    import json

    payload = result_payload(result)
    return {
        "type": "tool_result",
        "tool_use_id": tool_use_id,
        "content": json.dumps(payload, indent=2, default=str),
        "is_error": payload["status"] == "failed",
    }
