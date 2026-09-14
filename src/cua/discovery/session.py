"""One discovery run, end to end: explore -> record -> verify by replay.

Kept out of `cli.py` so the whole pipeline is testable without a terminal, and
out of `agent.py` so the agent stays a loop rather than a workflow.

The verify-by-replay step at the end is the part worth arguing for. Recording an
artifact and shipping it unverified means the first person to find out whether
the recording is any good is whoever called the capability in production. Here,
the recorder's output is immediately replayed against the same application with
no model involved, and the artifact says so in its provenance. Record-then-prove
is cheap and it makes the artifact honest before it is ever offered to a caller.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..artifact.authoring import AuthoringReport, Reviewer, review_artifact
from ..artifact.recorder import RecorderRefusal, record
from ..artifact.schema import CapabilityArtifact, CapabilityRisk, ProductRef
from ..artifact.store import ArtifactStore
from ..observability.evidence import EvidenceWriter
from ..profiles.resolve import ProfileRepository, ResolvedProfile
from ..replay.engine import ReplayEngine
from ..session.auth import Authenticator, CredentialResolver
from ..replay.result import ReplayResult, Success
from ..surfaces.base import Surface
from .agent import DiscoveryAgent, DiscoveryLimits
from .costs import RunCost, Spend
from .model import ModelClient
from .transcript import DiscoveryRun


@dataclass
class DiscoveryOutcome:
    run: DiscoveryRun
    artifact: CapabilityArtifact | None = None
    artifact_path: Path | None = None
    verification: ReplayResult | None = None
    authoring: AuthoringReport | None = None
    verify_skipped: str = ""
    refusal: str = ""
    evidence_dir: Path | None = None

    @property
    def verified(self) -> bool:
        return isinstance(self.verification, Success)

    def summary(self) -> str:
        lines = [
            f"run          {self.run.run_id}",
            f"status       {self.run.status} -- {self.run.stop_reason}",
            f"steps        {len(self.run.steps)} taken, {len(self.run.kept_steps)} kept",
        ]
        if self.refusal:
            lines.append(f"recorder     REFUSED: {self.refusal}")
        if self.artifact is not None:
            lines.append(f"artifact     {self.artifact.ref}  {self.artifact.content_hash[:23]}...")
            lines.append(f"             {len(self.artifact.steps)} steps, "
                         f"{len(self.artifact.inputs)} input(s), "
                         f"{len(self.artifact.outputs)} output(s), "
                         f"{len(self.artifact.outcomes)} declared outcome(s)")
        if self.authoring is not None:
            lines.append(f"authoring    {self.authoring.summary()}")
        if self.verify_skipped:
            lines.append(f"verify       SKIPPED -- {self.verify_skipped}")
        elif self.verification is not None:
            kind = type(self.verification).__name__
            lines.append(f"verify       {kind}"
                         + (f" -- outputs {self.verification.outputs}" if self.verified else ""))
        if self.evidence_dir:
            lines.append(f"evidence     {self.evidence_dir}")
        lines.append("cost")
        lines.append(self.cost.report())
        alt = self.cost.counterfactual(reviewer_model=self.run.model)
        if alt:
            lines.append(alt)
        return "\n".join(lines)

    @property
    def cost(self) -> RunCost:
        """Measured usage, split by which model did what.

        Split rather than totalled because the two calls have completely
        different shapes: the loop re-sends a growing conversation every turn,
        the reviewer sends one small prompt once. A single number hides that the
        expensive part is the loop and the cheap part does not need an expensive
        model.
        """
        reviewer = None
        if self.authoring is not None and self.authoring.model:
            reviewer = Spend(self.authoring.model, self.authoring.input_tokens,
                             self.authoring.output_tokens)
        return RunCost(
            agent=Spend(self.run.model, self.run.input_tokens, self.run.output_tokens),
            reviewer=reviewer,
        )


async def discover(
    surface: Surface,
    client: ModelClient,
    profile: ResolvedProfile,
    *,
    goal: str,
    tenant: str,
    capability_id: str,
    product: ProductRef,
    app_profile_ref: str,
    evidence_root: Path | str = "evidence",
    store: ArtifactStore | None = None,
    repository: ProfileRepository | None = None,
    limits: DiscoveryLimits | None = None,
    allow_irreversible: bool = False,
    model_name: str = "claude-opus-5",
    verify: bool = True,
    credentials: CredentialResolver | None = None,
    reviewer: Reviewer | None = None,
) -> DiscoveryOutcome:
    agent = DiscoveryAgent(
        surface, client, profile,
        goal=goal, tenant=tenant, limits=limits,
        allow_irreversible=allow_irreversible, model_name=model_name,
    )
    evidence = EvidenceWriter(evidence_root, agent.run.run_id)
    agent.journal = evidence.journal
    agent.evidence = evidence

    # Sign in BEFORE the agent is given the surface. Credentials are `env:`
    # references so their values never reach a model; letting the agent discover
    # its own way past a login screen would require handing it a password. The
    # model therefore never sees the login screen at all -- and authentication
    # is not something worth spending discovery steps on, because the profile
    # already knows how to do it.
    signed_in = await Authenticator(
        surface, profile, credentials=credentials, journal=evidence.journal,
    ).sign_in()
    if not signed_in.ok:
        evidence.journal.emit("discovery.aborted", reason=signed_in.reason)
        evidence.finish("auth_failed", reason=signed_in.reason)
        outcome = DiscoveryOutcome(run=agent.run, evidence_dir=evidence.dir)
        outcome.run.status = "aborted"
        outcome.run.stop_reason = f"could not sign in: {signed_in.reason}"
        return outcome

    run = await agent.run_discovery()
    outcome = DiscoveryOutcome(run=run, evidence_dir=evidence.dir)

    # The transcript is written whatever happened. A run that gave up is
    # evidence too, and often the more interesting kind.
    evidence.write_json("discovery.json", run.to_dict())
    for step in run.steps:
        if step.post is not None:
            evidence.observation(step.index, step.post)

    if not run.succeeded:
        evidence.finish(run.status, reason=run.stop_reason)
        return outcome

    variants = (repository.label_variants(app_profile_ref) if repository else {})
    try:
        artifact = record(
            run, capability_id=capability_id, product=product,
            app_profile_ref=app_profile_ref, label_variants=variants,
        )
    except RecorderRefusal as exc:
        outcome.refusal = str(exc)
        evidence.journal.emit("recorder.refused", reason=str(exc))
        evidence.finish("recorder_refused", reason=str(exc))
        return outcome

    # One bounded authoring pass (Part 5.4). A discovery run only declares the
    # outcomes it happened to bump into; this names the ones visible in screens
    # it passed through. Authoring, never replay -- and it can only ADD outcomes
    # whose detectors match a screen the run actually saw.
    artifact, authoring = review_artifact(artifact, run, reviewer)
    outcome.authoring = authoring
    evidence.journal.emit("authoring.reviewed", summary=authoring.summary(),
                          applied=authoring.applied, rejected=authoring.rejected,
                          concerns=authoring.concerns)

    outcome.artifact = artifact
    evidence.write_text("artifact.json", artifact.model_dump_json(indent=2))
    evidence.journal.emit("artifact.recorded", ref=artifact.ref, hash=artifact.content_hash,
                          steps=len(artifact.steps), outcomes=len(artifact.outcomes))

    if store is not None:
        outcome.artifact_path = store.save(artifact, overwrite=True)

    # Verifying an irreversible capability by replaying it means performing it a
    # second time. The application is right to refuse the duplicate, so the
    # replay fails and the artifact looks broken when the recording was fine --
    # observed on the first real run of this flow. Record-then-prove-it-replays
    # is a property of reversible capabilities; for the rest, proving it costs a
    # real account and belongs in an environment that can absorb one.
    if verify and artifact.capability.risk_tier is CapabilityRisk.WRITES_IRREVERSIBLE:
        outcome.verify_skipped = (
            "this capability is irreversible, and replaying it to verify it would "
            "commit the action a second time. Verify it against a sacrificial "
            "environment, or approve it on review of the recording."
        )
        evidence.journal.emit("verify.skipped", ref=artifact.ref,
                              reason="capability is irreversible")
        verify = False

    if verify:
        result = await _verify(surface, artifact, profile, tenant, evidence, credentials,
                               params=run.params_used())
        outcome.verification = result
        verified = isinstance(result, Success)
        # Provenance records whether the recording was proved, not whether we
        # hoped it was. A draft that failed verification stays on disk with the
        # flag false rather than being quietly deleted.
        artifact = artifact.model_copy(update={
            "provenance": artifact.provenance.model_copy(update={"verified_by_replay": verified})
        }).seal()
        outcome.artifact = artifact
        evidence.write_text("artifact.json", artifact.model_dump_json(indent=2))
        if store is not None:
            outcome.artifact_path = store.save(artifact, overwrite=True)

    evidence.finish(
        run.status,
        artifact=artifact.ref,
        content_hash=artifact.content_hash,
        verified_by_replay=outcome.verified,
        verification_skipped=outcome.verify_skipped or None,
        cost={
            "agent": {"model": run.model, "input_tokens": run.input_tokens,
                      "output_tokens": run.output_tokens, "usd": outcome.cost.agent.usd},
            "reviewer": ({"model": authoring.model, "input_tokens": authoring.input_tokens,
                          "output_tokens": authoring.output_tokens,
                          "usd": outcome.cost.reviewer.usd if outcome.cost.reviewer else None}
                         if authoring.model else None),
            "total_usd": outcome.cost.total_usd,
            "note": "token counts measured; prices are list-price assumptions",
        },
    )
    return outcome


async def _verify(surface, artifact, profile, tenant, evidence, credentials,
                  *, params: dict[str, str]) -> ReplayResult:
    """Replay the fresh artifact once, with no model in the loop.

    Parameters come from the discovery run in memory, never from the artifact:
    the artifact records a value's *shape*, so it has nothing to replay with by
    design. Passing the run's own values is also the stronger test -- it proves
    the recording reproduces the exact flow that was just observed.
    """
    from ..replay.result import to_dict

    evidence.journal.emit("verify.started", ref=artifact.ref, params=sorted(params))

    engine = ReplayEngine(
        surface, artifact, profile,
        journal=evidence.journal, tenant=tenant,
        credentials=credentials or CredentialResolver(),
    )
    result = await engine.run(params)
    evidence.write_json("verification.json", to_dict(result))
    evidence.journal.emit("verify.finished", variant=type(result).__name__)
    return result
