"""The authoring-time review pass.

A discovery run only declares the outcomes it happened to bump into. Look up a
member who exists and the model never sees "no records found", so the capability
ships unable to recognise it -- and reports the brief's canonical legitimate
answer as broken automation.

The review pass closes that gap, which makes it the most dangerous helpful thing
in the system: a model proposing conditions for screens it is only guessing at
would produce an artifact that *looks* like it has coverage. So the pass can
only ADD outcomes, and only ones whose detector matches a screen the run
actually observed. Those two limits are what these tests are really about.
"""

from __future__ import annotations

from cua.artifact.schema import Provenance
from cua.artifact.authoring import (
    AuthoringReview, CheckpointConcern, ProposedOutcome, build_prompt, review_artifact,
)
from cua.discovery.transcript import DiscoveryRun, DiscoveryStep
from cua.perception.model import Anchors, BBox, Observation, UiNode
from cua.surfaces.base import ActionType
from test_recorder import PRODUCT, node, obs, run_with, step
from cua.artifact.recorder import record


class FakeReviewer:
    def __init__(self, review: AuthoringReview, *, explode: bool = False) -> None:
        self.review_result = review
        self.explode = explode
        self.prompts: list[str] = []

    def review(self, *, system: str, prompt: str) -> AuthoringReview:
        self.prompts.append(prompt)
        if self.explode:
            raise RuntimeError("model unavailable")
        return self.review_result


def flow_with_notice(notice: UiNode | None):
    """A recorded lookup flow, optionally having passed a notice screen.

    With `notice=None` the run passes no notice-shaped node at all -- headings
    included, since a legacy app states plenty of outcomes as headings -- so the
    checkpoint has to come from the URL change instead.
    """
    field = node("textbox", row_label="Member ID", node_id="n1")
    before = obs(field, url="http://x/t/demo-cu/members")

    # The success screen the flow ends on. A notice screen is something the run
    # passed THROUGH on the way -- if it were still on screen at the end it
    # would not be a business outcome, it would be the result.
    success = (obs(node("heading", "Member Detail", node_id="n8"),
                   url="http://x/t/demo-cu/members/12345")
               if notice is not None else
               # The no-notice variant must contain no notice-shaped node at
               # all -- heading included, since an app states plenty of outcomes
               # as headings.
               obs(node("cell", "Member Detail", node_id="n8"),
                   url="http://x/t/demo-cu/members/12345"))

    steps = [
        step(1, "fill", ActionType.FILL, n=field, value="12345", param="member_id",
             is_param=True, pre=before, post=before),
    ]
    if notice is not None:
        steps.append(step(2, "click", ActionType.CLICK,
                          n=node("button", "Search", node_id="n2"),
                          pre=before,
                          post=obs(node("heading", "Search Results", node_id="n9"), notice,
                                   url="http://x/t/demo-cu/members/search")))
    else:
        steps.append(step(2, "click", ActionType.CLICK,
                          n=node("button", "Search", node_id="n2"),
                          pre=before,
                          post=obs(node("cell", "Done", node_id="n9"),
                                   url="http://x/t/demo-cu/members/search")))
    steps.append(step(3, "click", ActionType.CLICK,
                      n=node("link", "open", node_id="n3"),
                      pre=before, post=success))
    run = run_with(steps)
    artifact = record(run, capability_id="member.lookup", product=PRODUCT,
                      app_profile_ref="meridian-core@4.2")
    return artifact, run


NOT_FOUND = node("alert", "No records found matching your search criteria.", node_id="n7")


def test_an_outcome_the_run_actually_saw_is_added():
    """The case the pass exists for: the run walked past a 'no records found'
    notice without declaring it, and the capability would have shipped unable to
    return the brief's canonical business outcome."""
    artifact, run = flow_with_notice(NOT_FOUND)
    assert artifact.outcomes == (), "the run itself declared nothing"

    reviewer = FakeReviewer(AuthoringReview(outcomes=[ProposedOutcome(
        code="MEMBER_NOT_FOUND",
        description="The institution has no member with that id.",
        detect_role="alert",
        detect_name_pattern="(?i)no records found",
    )]))

    reviewed, report = review_artifact(artifact, run, reviewer)

    assert report.applied == ["MEMBER_NOT_FOUND"]
    assert [o.code for o in reviewed.outcomes] == ["MEMBER_NOT_FOUND"]
    assert reviewed.verify_hash(), "adding an outcome must re-seal the artifact"
    assert reviewed.content_hash != artifact.content_hash, "the flow's contract changed"


def test_an_outcome_for_a_screen_that_was_never_seen_is_rejected():
    """The guard that makes this trustworthy.

    'Insufficient funds' is a plausible outcome for a bank. Nothing in this run
    showed one, so proposing it would be the artifact claiming coverage it does
    not have -- a detector that matches nothing is worse than no outcome, because
    it looks like the case is handled.
    """
    artifact, run = flow_with_notice(NOT_FOUND)

    reviewer = FakeReviewer(AuthoringReview(outcomes=[
        ProposedOutcome(code="MEMBER_NOT_FOUND", description="Not found",
                        detect_role="alert", detect_name_pattern="(?i)no records found"),
        ProposedOutcome(code="INSUFFICIENT_FUNDS", description="Plausible, but invented",
                        detect_role="alert", detect_name_pattern="(?i)insufficient funds"),
    ]))

    reviewed, report = review_artifact(artifact, run, reviewer)

    assert report.applied == ["MEMBER_NOT_FOUND"]
    assert any("INSUFFICIENT_FUNDS" in r for r in report.rejected)
    assert [o.code for o in reviewed.outcomes] == ["MEMBER_NOT_FOUND"]


def test_the_reviewer_cannot_touch_the_executable_flow():
    """It may add outcomes and author the caller-facing prose. The steps,
    locators and checkpoints stay exactly what was observed -- a model editing
    the flow after the fact would undo the entire point of recording one.

    The prose used to be in this list. It was here because nothing consumed
    `AuthoringReview.title`/`.description`, so "unchanged" was the only
    observable behaviour and the test wrote it down as if it were the rule.
    R-M7-2 made the fields live: the reviewer is now the thing that names a
    capability for its callers. What must not move is the executable flow.
    """
    artifact, run = flow_with_notice(NOT_FOUND)
    reviewer = FakeReviewer(AuthoringReview(
        outcomes=[ProposedOutcome(code="MEMBER_NOT_FOUND", description="d",
                                  detect_role="alert",
                                  detect_name_pattern="(?i)no records found")],
        title="Something Else", description="A different description entirely",
    ))

    reviewed, _ = review_artifact(artifact, run, reviewer)

    assert reviewed.steps == artifact.steps
    assert reviewed.extractions == artifact.extractions
    assert reviewed.success == artifact.success
    assert reviewed.binding == artifact.binding
    assert reviewed.inputs == artifact.inputs
    assert reviewed.outputs == artifact.outputs


def test_the_reviewer_authors_the_caller_facing_prose():
    """R-M7-2. These fields were declared, filled every run, and thrown away."""
    artifact, run = flow_with_notice(NOT_FOUND)
    reviewer = FakeReviewer(AuthoringReview(
        title="Look up a savings balance",
        description="Look up a member by id and read their savings balance.",
    ))

    reviewed, report = review_artifact(artifact, run, reviewer)

    assert reviewed.capability.title == "Look up a savings balance"
    assert reviewed.capability.description.startswith("Look up a member by id")
    assert "description" in report.applied and "title" in report.applied


def test_the_reviewer_may_not_hand_back_the_discovery_goal():
    """The most likely thing a model asked to describe a flow will do, and the
    exact defect R-M7-2 exists to remove."""
    artifact, run = flow_with_notice(NOT_FOUND)
    goal = ("Search for member 99999, which does not exist, so you can see how "
            "this application reports a member it cannot find.")
    artifact = artifact.model_copy(update={
        "provenance": (artifact.provenance or Provenance()).model_copy(
            update={"discovery_goal": goal})})

    reviewed, report = review_artifact(artifact, run, FakeReviewer(
        AuthoringReview(description=goal)))

    assert reviewed.capability.description == artifact.capability.description
    assert any("restates the discovery goal" in r for r in report.rejected)


def test_concerns_are_reported_without_changing_anything():
    artifact, run = flow_with_notice(NOT_FOUND)
    reviewer = FakeReviewer(AuthoringReview(checkpoint_concerns=[
        CheckpointConcern(step_id="s2", concern="matches a heading that may be renamed")]))

    reviewed, report = review_artifact(artifact, run, reviewer)

    assert report.concerns == ["s2: matches a heading that may be renamed"]
    assert reviewed.content_hash == artifact.content_hash, "a concern is advice, not a change"


def test_a_run_with_no_notice_screens_skips_the_call_entirely():
    """No screens to reason about means nothing to review, and no reason to spend
    a request finding that out."""
    artifact, run = flow_with_notice(None)
    reviewer = FakeReviewer(AuthoringReview())

    reviewed, report = review_artifact(artifact, run, reviewer)

    assert reviewer.prompts == [], "the reviewer was called with nothing to say"
    assert "no notice-shaped screens" in report.skipped
    assert reviewed is artifact


def test_offline_recording_still_works():
    """Authoring needs a model; recording does not. With no reviewer the artifact
    is produced unchanged, which is why the whole suite runs without a key."""
    artifact, run = flow_with_notice(NOT_FOUND)
    reviewed, report = review_artifact(artifact, run, None)

    assert reviewed is artifact
    assert "offline" in report.skipped


def test_a_reviewer_failure_never_loses_the_recording():
    """Authoring is best-effort. A recording that survived discovery must not be
    thrown away because a second model call failed."""
    artifact, run = flow_with_notice(NOT_FOUND)
    reviewed, report = review_artifact(artifact, run, FakeReviewer(AuthoringReview(),
                                                                  explode=True))

    assert reviewed is artifact
    assert "reviewer unavailable" in report.skipped
    assert "RuntimeError" in report.skipped


def test_the_prompt_shows_the_reviewer_only_what_was_observed():
    artifact, run = flow_with_notice(NOT_FOUND)
    prompt = build_prompt(artifact, run)

    assert "No records found" in prompt
    assert "s1:" in prompt and "s2:" in prompt
    assert "checkpoint:" in prompt
    # It is told what was already declared, so it does not propose duplicates.
    assert "Outcomes the run already declared" in prompt


def test_an_invalid_pattern_is_rejected_rather_than_raised():
    artifact, run = flow_with_notice(NOT_FOUND)
    reviewer = FakeReviewer(AuthoringReview(outcomes=[ProposedOutcome(
        code="BROKEN", description="d", detect_role="alert",
        detect_name_pattern="(?i)[unclosed")]))

    reviewed, report = review_artifact(artifact, run, reviewer)

    assert any("BROKEN" in r and "invalid pattern" in r for r in report.rejected)
    assert reviewed.outcomes == ()


def test_an_outcome_describing_the_success_screen_is_rejected():
    """A business outcome is by definition not success.

    The ladder evaluates outcomes BEFORE checkpoints, so an outcome matching the
    success screen turns every successful run into a business outcome. The
    reviewer proposed exactly this on a real sub-account run
    (SUBACCOUNT_OPENED for the confirmation banner), and nothing else would have
    caught it -- verification is skipped for irreversible capabilities.
    """
    artifact, run = flow_with_notice(NOT_FOUND)
    reviewer = FakeReviewer(AuthoringReview(outcomes=[ProposedOutcome(
        code="LOOKUP_SUCCEEDED", description="The lookup worked",
        detect_role="heading", detect_name_pattern="(?i)member detail")]))

    reviewed, report = review_artifact(artifact, run, reviewer)

    assert reviewed.outcomes == ()
    assert any("LOOKUP_SUCCEEDED" in r and "success screen" in r for r in report.rejected)
