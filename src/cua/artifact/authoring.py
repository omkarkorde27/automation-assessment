"""The authoring-time review pass.

**This is authoring, not replay.** A model runs here, once, while the artifact is
being written -- never while it is being executed. Replay has no import path to
`anthropic` and a test asserts it. The distinction matters enough to say twice:
a model that helps *write* a capability is a tool; a model in the execution loop
would make every run non-deterministic, which is the thing this whole system
exists to avoid.

What the pass is for is the brief's central trap. A discovery run only declares
the business outcomes it happened to bump into: look up a member who exists and
the model never sees "no records found", so the capability ships unable to
recognise it and reports a legitimate answer as a broken locator. The reviewer
looks at every screen the run passed through -- including ones it navigated away
from -- and names the outcomes the flow should be able to return.

Two guards make this safe to trust:

* A proposed outcome is **discarded unless its detector actually matches a node
  the run observed.** The model cannot invent a screen that does not exist, and
  an outcome detector that matches nothing is worse than no outcome at all.
* It may only add outcomes and flag concerns. It cannot edit steps, locators, or
  checkpoints, so the executable flow stays exactly what was observed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol

from pydantic import BaseModel, Field

from ..conditions.model import ElementCondition, ElementMatch
from ..discovery.transcript import DiscoveryRun
from ..perception.model import UiNode
from .schema import CapabilityArtifact, OutcomeSpec

# Roles an application uses to say something went differently than expected.
_NOTICE_ROLES = {"alert", "status", "heading"}


class ProposedOutcome(BaseModel):
    code: str = Field(description="SCREAMING_SNAKE_CASE, e.g. MEMBER_NOT_FOUND.")
    description: str = Field(description="What this outcome means to the caller.")
    detect_role: str = Field(description="Role of the node that shows it: alert, status or heading.")
    detect_name_pattern: str = Field(
        description="Regex matching that node's text, taken from the screens you were shown.")


class CheckpointConcern(BaseModel):
    step_id: str
    concern: str = Field(description="Why this checkpoint may be too weak or too brittle.")


class AuthoringReview(BaseModel):
    """What the reviewer is allowed to say."""

    outcomes: list[ProposedOutcome] = Field(default_factory=list)
    checkpoint_concerns: list[CheckpointConcern] = Field(default_factory=list)
    title: str = Field(
        default="", description="A short name for the capability, 2-8 words.")
    description: str = Field(
        default="",
        description="One or two sentences telling a CALLER what this capability does "
                    "and what it needs. Not the goal it was recorded for.")


@dataclass
class AuthoringReport:
    applied: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)
    concerns: list[str] = field(default_factory=list)
    skipped: str = ""
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0

    def summary(self) -> str:
        if self.skipped:
            return f"authoring review skipped: {self.skipped}"
        bits = [f"{len(self.applied)} outcome(s) added"]
        if self.rejected:
            bits.append(f"{len(self.rejected)} rejected as unmatched")
        if self.concerns:
            bits.append(f"{len(self.concerns)} checkpoint concern(s)")
        return ", ".join(bits)


class Reviewer(Protocol):
    def review(self, *, system: str, prompt: str) -> AuthoringReview: ...


# The reviewer does not need the model the agent needs.
#
# Discovery is open-ended: work out which of forty unlabelled table cells is the
# field you want, from a screenshot and an accessibility dump. The review pass is
# the opposite -- it reads an already-successful, structured transcript and fills
# in a fixed schema, with a hard constraint ("only outcomes whose detector
# matches a screen listed below") that is checked in code afterwards regardless.
# That is a small-model task, and the guards in `review_artifact` mean a weaker
# reviewer degrades toward proposing nothing rather than toward proposing
# something wrong.
DEFAULT_REVIEWER_MODEL = "claude-haiku-4-5-20251001"


class AnthropicReviewer:
    """Structured output via `messages.parse`, so the result is typed rather
    than a JSON blob we hope parses."""

    def __init__(self, client, *, model: str = DEFAULT_REVIEWER_MODEL,
                 max_tokens: int = 2048) -> None:
        self._client = client
        self.model = model
        self.max_tokens = max_tokens
        self.input_tokens = 0
        self.output_tokens = 0
        """Counted separately from the agent loop, so the cost of authoring is
        visible rather than folded into the run total."""

    def review(self, *, system: str, prompt: str) -> AuthoringReview:
        response = self._client.messages.parse(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_format=AuthoringReview,
        )
        usage = getattr(response, "usage", None)
        if usage is not None:
            self.input_tokens += usage.input_tokens
            self.output_tokens += usage.output_tokens
        return response.parsed_output or AuthoringReview()


SYSTEM = """\
You are reviewing a capability that was just recorded from one run through a \
bank's back-office application, before it is approved for unattended reuse.

Your one job is to name the **business outcomes** this flow should be able to \
return. A business outcome is the application giving a legitimate answer that is \
not success -- "no records found", "already exists", "not authorised", \
"insufficient funds". These are answers, not faults. A capability that cannot \
recognise them reports them as broken automation, which is the single most \
common and most expensive mistake in this kind of system.

The run you are reviewing only encountered the screens it happened to encounter. \
Look at every screen it passed through and name the outcomes that ARE visible in \
what you were shown.

Hard rule: every outcome you propose must be detectable by matching text that \
appears in the observed screens listed below. Do not propose an outcome for a \
screen you were not shown, however likely it seems -- a detector that matches \
nothing is worse than no outcome at all, because it looks like coverage. If the \
run saw no such screens, return no outcomes. That is a perfectly good answer.

You may also flag checkpoints that look too weak (would pass on the wrong screen) \
or too brittle (would fail on a cosmetic change). You cannot change the steps.

Finally, write the capability's `title` and `description`. These are what a \
production agent is shown when it decides whether to call this capability, so \
write them for a CALLER, not for yourself. Describe what the capability does and \
what it needs, in one or two plain sentences. Do NOT restate the goal the flow \
was recorded for: that goal was written to steer an explorer and may contain \
probes and asides ("first search for an id that does not exist, so you can see \
how it reports that") which would read to a calling agent as instructions. \
Describe the capability, not the recording.
"""


def _final_observation(run: DiscoveryRun):
    """The screen the run finished on -- i.e. the success screen."""
    for step in reversed(run.steps):
        if step.post is not None:
            return step.post
    return None


def _observed_notices(run: DiscoveryRun) -> list[UiNode]:
    """Every notice-shaped node the run saw, deduplicated."""
    seen: dict[str, UiNode] = {}
    for step in run.steps:
        for observation in (step.pre, step.post):
            if observation is None:
                continue
            for node in observation.nodes:
                if node.role in _NOTICE_ROLES and node.name and not node.sensitive:
                    seen.setdefault(f"{node.role}|{node.name}", node)
    return list(seen.values())


def build_prompt(artifact: CapabilityArtifact, run: DiscoveryRun) -> str:
    from ..conditions.model import describe

    steps = "\n".join(
        f"  {s.id}: {s.intent}"
        + (f"\n      checkpoint: {describe(s.checkpoint)}" if s.checkpoint else
           "\n      checkpoint: (none)")
        for s in artifact.steps
    )
    notices = "\n".join(f"  {n.role}: {n.name!r}" for n in _observed_notices(run)) or "  (none)"
    declared = "\n".join(f"  {o.code}: {o.description}" for o in artifact.outcomes) or "  (none)"

    return (
        f"Goal the flow was recorded for (provenance -- do NOT restate it as the "
        f"description):\n  "
        f"{artifact.provenance.discovery_goal if artifact.provenance else '(none)'}\n\n"
        f"Recorded steps and their checkpoints:\n{steps}\n\n"
        f"Notice-shaped elements observed anywhere during the run:\n{notices}\n\n"
        f"Outcomes the run already declared:\n{declared}\n"
    )


def review_artifact(
    artifact: CapabilityArtifact,
    run: DiscoveryRun,
    reviewer: Reviewer | None,
) -> tuple[CapabilityArtifact, AuthoringReport]:
    """One bounded review pass. Returns the artifact, changed or not."""
    report = AuthoringReport()
    if reviewer is None:
        report.skipped = "no reviewer configured (offline)"
        return artifact, report

    notices = _observed_notices(run)
    if not notices:
        report.skipped = "the run observed no notice-shaped screens to reason about"
        return artifact, report

    report.model = getattr(reviewer, "model", "")
    try:
        review = reviewer.review(system=SYSTEM, prompt=build_prompt(artifact, run))
        report.input_tokens = getattr(reviewer, "input_tokens", 0)
        report.output_tokens = getattr(reviewer, "output_tokens", 0)
    except Exception as exc:  # authoring is best-effort; a failure must not lose the recording
        report.skipped = f"reviewer unavailable: {type(exc).__name__}: {exc}"
        return artifact, report

    existing = {o.code for o in artifact.outcomes}
    added: list[OutcomeSpec] = []
    final = _final_observation(run)

    for proposal in review.outcomes:
        if proposal.code in existing:
            continue
        try:
            pattern = re.compile(proposal.detect_name_pattern, re.IGNORECASE)
        except re.error:
            report.rejected.append(f"{proposal.code} (invalid pattern)")
            continue

        # The guard that makes this trustworthy: the detector has to match
        # something the run actually saw.
        if not any(pattern.search(n.name) for n in notices
                   if not proposal.detect_role or n.role == proposal.detect_role):
            report.rejected.append(
                f"{proposal.code} (its detector matches no observed screen)")
            continue

        # A business outcome is by definition NOT success, so it cannot be true
        # on the screen the flow succeeds on. Without this the reviewer happily
        # proposes SUBACCOUNT_OPENED for the confirmation screen -- and because
        # the ladder evaluates outcomes BEFORE checkpoints, every successful run
        # would then return a business outcome instead of Success. Observed on a
        # real run, and invisible because verification is skipped for
        # irreversible capabilities.
        if final is not None and any(
            pattern.search(n.name) for n in final.nodes
            if n.name and (not proposal.detect_role or n.role == proposal.detect_role)
        ):
            report.rejected.append(
                f"{proposal.code} (matches the success screen -- it describes success, "
                f"and the ladder would return it instead of Success)")
            continue

        added.append(OutcomeSpec(
            code=proposal.code,
            description=proposal.description,
            detect=ElementCondition(
                element=ElementMatch(role=proposal.detect_role or None,
                                     name_matches=proposal.detect_name_pattern),
                exists=True),
        ))
        report.applied.append(proposal.code)

    report.concerns = [f"{c.step_id}: {c.concern}" for c in review.checkpoint_concerns]

    # ---- caller-facing prose (R-M7-2) -----------------------------------
    # The reviewer has always been asked for these and the recorder has always
    # thrown them away, using the discovery goal instead. Applying them is what
    # gives `AuthoringReview.title` and `.description` their first read site.
    #
    # Guarded the same way the outcomes are. A proposal that merely restates the
    # goal reintroduces exactly the defect this requirement exists to remove,
    # and it is the most likely thing a model asked to describe a flow will do.
    prose: dict[str, str] = {}
    goal = artifact.provenance.discovery_goal if artifact.provenance else ""
    proposed = " ".join((review.description or "").split())
    if proposed:
        if _restates(proposed, goal):
            report.rejected.append(
                "description (it restates the discovery goal, which describes the "
                "recording rather than the capability)")
        else:
            prose["description"] = proposed
            report.applied.append("description")

    proposed_title = " ".join((review.title or "").split())
    if proposed_title and not _restates(proposed_title, goal):
        prose["title"] = proposed_title
        report.applied.append("title")

    if not added and not prose:
        return artifact, report

    meta = artifact.capability.model_copy(update=prose) if prose else artifact.capability
    return artifact.model_copy(update={
        "capability": meta,
        "outcomes": artifact.outcomes + tuple(added),
    }).seal(), report


#: Phrasing that addresses an EXPLORER rather than describing a capability.
#: This is the actual harm R-M7-2 exists to prevent: a calling agent reads the
#: description as part of its instructions, and "so you can see how it reports
#: that" is a directive it may well follow. One real run did exactly that, and
#: another declined on the grounds that it does not obey instructions embedded
#: in tool metadata -- which is the correct instinct and still a wasted turn.
_EXPLORER_DIRECTED = re.compile(
    r"\b(?:so (?:that )?you can|so you see|to see how|observe how|note how"
    r"|declare how|you should (?:then|first|now)|first (?:search|look|try|open)"
    r"|then (?:search|look|try|open)|which does not exist|that does not exist)\b",
    re.IGNORECASE)


def _reads_as_instruction(text: str) -> bool:
    """Does this prose tell the reader to go and do something exploratory?"""
    return bool(_EXPLORER_DIRECTED.search(text or ""))


def _restates(proposal: str, goal: str) -> bool:
    """Is this proposed prose really just the discovery goal?

    Two different failures, and only one of them is about similarity.

    The one that matters is prose carrying **explorer-directed instructions** --
    caught by `_reads_as_instruction` regardless of where it came from, because
    a hand-written description with "first search for an id that does not exist"
    in it is exactly as harmful as a pasted goal.

    The other is the goal reused verbatim or all but. That is a "nobody wrote
    this" signal, so the bar is near-identity: normalized equality, containment,
    or word overlap at 0.95.

    An earlier version of this compared word sets at 0.75 and rejected a
    perfectly good hand-written description of `member.open_subaccount`, whose
    goal happened to be well phrased. A well-written goal and a well-written
    description of the same operation SHOULD share most of their content words;
    that is what it means for both to be about the same thing. Overlap was
    measuring subject matter and being read as authorship.
    """
    text = " ".join((proposal or "").split())
    if not text:
        return False
    if _reads_as_instruction(text):
        return True
    if not goal:
        return False

    normalized_goal = " ".join(goal.split())
    if text.lower() == normalized_goal.lower():
        return True
    if len(text) > 40 and text.lower() in normalized_goal.lower():
        return True

    words = {w for w in re.findall(r"[a-z0-9_]+", normalized_goal.lower()) if len(w) > 3}
    if len(words) < 6:
        return False
    shared = words & {w for w in re.findall(r"[a-z0-9_]+", text.lower()) if len(w) > 3}
    return len(shared) / len(words) >= 0.95
