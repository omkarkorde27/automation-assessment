"""CLI entry point.

Serves the mockbank fixture, arms faults against it, runs discovery, and serves
the operator console. `replay` lands with M7's demo path.
"""

from __future__ import annotations

import json

import typer

app = typer.Typer(
    add_completion=False,
    help="Computer-use automation: discover a UI flow once, replay it deterministically.",
)


@app.command("serve-app")
def serve_app(
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8800, help="Port to serve mockbank on."),
    reload: bool = typer.Option(False, help="Auto-reload on source changes."),
) -> None:
    """Serve the mockbank target surface."""
    import uvicorn

    typer.echo(f"mockbank -> http://{host}:{port}/t/demo-cu/  (also /t/valley-cu/)")
    uvicorn.run("mockbank.app:app", host=host, port=port, reload=reload)


@app.command("inject")
def inject(
    fault: str = typer.Argument(..., help="Fault name, or 'list' to show all."),
    tenant: str = typer.Option("demo-cu", help="Tenant slug."),
    count: int = typer.Option(1, help="How many times it should fire."),
    base_url: str = typer.Option("http://127.0.0.1:8800", help="Running mockbank instance."),
) -> None:
    """Arm a fault on a running mockbank, to exercise replay's error handling."""
    import httpx

    from mockbank.faults import FAULT_POINTS

    if fault == "list":
        for name, where in FAULT_POINTS.items():
            typer.echo(f"  {name:<18} {where}")
        raise typer.Exit(0)

    if fault not in FAULT_POINTS:
        typer.echo(f"unknown fault: {fault}. Try 'cua inject list'.", err=True)
        raise typer.Exit(2)

    resp = httpx.post(
        f"{base_url}/t/{tenant}/__control", params={"fault": fault, "count": count}, timeout=10
    )
    typer.echo(json.dumps(resp.json(), indent=2))


@app.command("dry-run")
def dry_run_cmd(
    tenant: str = typer.Option("demo-cu", help="Tenant profile to drive against."),
    base_url: str = typer.Option("", help="Override the profile's base_url."),
    profiles_root: str = typer.Option("profiles", help="Profile directory."),
) -> None:
    """Exercise the discovery loop's control logic with no model and no API key.

    Budgets, stopping conditions, the dead-end detector, the tool schemas and the
    policy choke point are all decidable without a model. `discover` runs this
    first by default; run it on its own while changing the loop.
    """
    import asyncio

    from .discovery.dryrun import run_control_checks
    from .profiles.resolve import ProfileRepository

    repository = ProfileRepository(profiles_root)
    resolved = repository.resolve(tenant)
    if base_url:
        surface_cfg = resolved.profile.surface.model_copy(update={"base_url": base_url})
        resolved = resolved.__class__(
            profile=resolved.profile.model_copy(update={"surface": surface_cfg}),
            lineage=resolved.lineage, hash=resolved.hash,
        )

    report = asyncio.run(run_control_checks(resolved))
    typer.echo(report.render())
    raise typer.Exit(0 if report.ok else 1)


@app.command("serve-console")
def serve_console(
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8801, help="Port for the operator console."),
    evidence_root: str = typer.Option("evidence", help="Where interventions are filed."),
) -> None:
    """Serve the operator console over the evidence directory.

    **Read-only.** Interventions filed by finished runs can be reviewed here,
    but nothing can be taken over: takeover needs the live browser, and the
    browser belongs to the process that opened it. A second process gets a
    second browser, at which point "the human continues where the automation
    stopped" has quietly become "the human starts over" -- which is the whole
    thing the design refuses to do.

    To drive a live session, the console is embedded in the run's own process.
    `scripts/watch_takeover.py` does exactly that and is the demo path.
    """
    import uvicorn

    from .escalation.console import create_console
    from .escalation.requests import InterventionStore

    store = InterventionStore(evidence_root)
    open_now = len(store.list())
    typer.echo(f"operator console -> http://{host}:{port}/")
    typer.echo(f"evidence         {evidence_root}  ({open_now} intervention(s) on file)")
    typer.echo("mode             read-only -- no live session is attached to this process\n")
    uvicorn.run(create_console(store=store), host=host, port=port)


@app.command("catalog")
def catalog_cmd(
    as_json: bool = typer.Option(False, "--json", help="Emit the tool definitions themselves."),
    include_drafts: bool = typer.Option(
        False, "--include-drafts",
        help="Also show capabilities nobody has approved. For review: a draft is "
             "listed, marked, and never emitted into the --json tools payload."),
    capabilities_root: str = typer.Option("capabilities", help="Where artifacts live."),
) -> None:
    """What a calling agent sees: the recorded capabilities as typed tools.

    This is the end of the through-line. A model discovered the flow once, the
    recorder froze it into an artifact, and what a production agent gets is a
    tool definition with typed inputs, typed outputs and the declared business
    outcomes it must branch on -- generated from the artifact, never written by
    hand, and never involving a model at call time.

    Only APPROVED capabilities are listed (R-M7-1). A tool definition is an
    offer, and offering a draft means a model calls something nobody reviewed.
    `--include-drafts` shows them for review.

    `--json` is the payload you would hand to the Messages API `tools` array.
    """
    import json as _json

    from .artifact.store import ArtifactStore
    from .catalog import build_catalog

    store = ArtifactStore(capabilities_root)
    all_artifacts = store.list()
    catalog = build_catalog(all_artifacts, include_drafts=include_drafts)

    if not all_artifacts:
        typer.echo("no capabilities recorded yet -- run `cua discover`.", err=True)
        raise typer.Exit(1)
    if not catalog.listing():
        # Not "nothing recorded". The distinction is the whole of R-M7-1: these
        # capabilities exist and are deliberately not being offered.
        #
        # Counted over CAPABILITIES, not files. `store.list()` returns every
        # version on disk, so after one `cua describe` the file count is twice
        # the capability count -- and this sentence is the first thing a reader
        # of the README meets.
        drafts = len(build_catalog(all_artifacts, include_drafts=True).listing())
        typer.echo(
            f"0 of {drafts} recorded capabilit{'y' if drafts == 1 else 'ies'} are "
            f"approved, so none is offered to a calling agent.\n"
            f"A draft is a recording nobody has reviewed.\n"
            f"  cua catalog --include-drafts     read them\n"
            f"  cua approve <capability-id>      the gate", err=True)
        raise typer.Exit(1)

    if as_json:
        typer.echo(_json.dumps(catalog.definitions(), indent=2))
        raise typer.Exit(0)

    for entry in catalog.listing():
        artifact = entry.artifact
        meta = artifact.capability
        mark = "" if entry.invocable else "   [DRAFT -- not offered to callers]"
        typer.echo(f"{artifact.ref}{mark}")
        typer.echo(f"  {meta.title or meta.description}")
        typer.echo(f"  risk        {meta.risk_tier.value}   "
                   f"approval {meta.approval_state.value}")
        typer.echo(f"  tool name   {artifact.tool_name}")
        required = artifact.input_json_schema().get("required", [])
        for name, spec in artifact.inputs.items():
            flags = []
            if name in required:
                flags.append("required")
            if spec.volatile:
                flags.append("volatile")
            if spec.sensitive:
                flags.append("sensitive")
            typer.echo(f"    in   {name}: {spec.type.value}"
                       + (f"  [{', '.join(flags)}]" if flags else ""))
        for name, spec in artifact.outputs.items():
            typer.echo(f"    out  {name}: {spec.type.value}"
                       + (f"  [redact {spec.redact.value}]"
                          if spec.redact.value != "none" else ""))
        for outcome in artifact.outcomes:
            # The four-variant contract, at the point a caller reads it: these
            # are answers, not errors, and a caller that treats them as errors
            # has fallen into the trap the brief names in its own glossary.
            typer.echo(f"    outcome  {outcome.code}  (not a failure)")
        typer.echo("")


@app.command("describe")
def describe_cmd(
    capability: str = typer.Argument(..., help="Capability id, or id@version."),
    description: str = typer.Option(..., "--description",
                                    help="What this capability does, for a CALLER."),
    title: str = typer.Option("", "--title", help="A short name, 2-8 words."),
    bump: str = typer.Option("minor", help="major | minor | patch."),
    capabilities_root: str = typer.Option("capabilities", help="Where artifacts live."),
) -> None:
    """Author a capability's caller-facing prose. Mints a NEW version (R-M7-2).

    The description is the single most load-bearing string in the agent-facing
    interface: it is what a production model reads when it decides whether to
    call this capability at all. The recorder deliberately leaves it empty,
    because it knows only what was asked of the explorer -- and a discovery goal
    can contain probes and asides that read to a calling agent as instructions.
    One real run followed a goal's "search for member 99999 first" into a wasted
    replay, and another named it as an embedded instruction it declined to obey.

    This mints a version rather than editing in place because the description is
    part of the contract a caller depends on. Changing it changes the contract,
    and the content hash is supposed to notice -- editing the file by hand would
    look exactly like tampering, which is the one thing the hash exists to make
    visible. The flow itself is untouched: same steps, same locators, same
    checkpoints, same recording.

    The new version starts as a DRAFT, whatever its predecessor was. A caller
    reading a new description is being offered a new contract, and the point of
    R-M7-1 is that nobody is offered a contract a person has not read.
    """
    from .artifact.schema import ApprovalState, CapabilityMeta
    from .artifact.store import ArtifactStore

    text = " ".join(description.split())
    if not text:
        typer.echo("--description cannot be empty.", err=True)
        raise typer.Exit(2)

    store = ArtifactStore(capabilities_root)
    artifact = store.resolve_ref(capability)
    # `provenance` is optional: a hand-authored artifact has none, and one that
    # does not record a goal cannot have been described by it.
    goal = artifact.provenance.discovery_goal if artifact.provenance else ""

    from .artifact.authoring import _restates
    if _restates(text, goal):
        # The same guard the authoring reviewer's proposal goes through. A human
        # pasting the goal in is the identical defect arriving by hand.
        typer.echo(
            "That description restates the discovery goal, which describes the "
            "RECORDING rather than the capability.\n"
            f"  goal: {goal[:120]}...\n"
            "Write what a caller needs to know: what it does, and what it needs.",
            err=True)
        raise typer.Exit(2)

    version = store.next_version(artifact.capability.id, bump)
    meta: CapabilityMeta = artifact.capability.model_copy(update={
        "version": version,
        "description": text,
        "title": " ".join(title.split()) or artifact.capability.title,
        "approval_state": ApprovalState.DRAFT,
    })
    revised = artifact.model_copy(update={"capability": meta}).seal()
    store.save(revised)

    typer.echo(f"{artifact.ref} -> {revised.ref}   ({revised.capability.approval_state.value})")
    typer.echo(f"  title        {revised.capability.title}")
    typer.echo(f"  description  {revised.capability.description}")
    typer.echo(f"  hash         {artifact.content_hash[:23]}... -> "
               f"{revised.content_hash[:23]}...")
    typer.echo(f"  flow         unchanged: {len(revised.steps)} step(s), "
               f"{len(revised.outcomes)} outcome(s), recorded from "
               f"{(revised.provenance.recorded_from_run if revised.provenance else '') or 'n/a'}")
    typer.echo(f"\n`cua approve {revised.capability.id}` to offer it to callers.")


@app.command("approve")
def approve_cmd(
    capability: str = typer.Argument(..., help="Capability id, or id@version."),
    state: str = typer.Option("approved", help="approved | draft | deprecated | drifted."),
    reason: str = typer.Option("", help="Why. Recorded beside the artifact."),
    capabilities_root: str = typer.Option("capabilities", help="Where artifacts live."),
) -> None:
    """Move a capability's approval state -- the human review gate.

    An irreversible capability replays only when it is `approved` AND the caller
    passes `--confirm-irreversible`. This is the half a person controls. It is a
    command rather than an edit because approving must be an act somebody
    performs, not a field somebody changes: the state is excluded from the
    content hash precisely so that approving does not look like tampering.
    """
    from .artifact.schema import ApprovalState
    from .artifact.store import ArtifactStore

    try:
        target = ApprovalState(state)
    except ValueError:
        typer.echo(f"{state!r} is not an approval state "
                   f"({', '.join(s.value for s in ApprovalState)})", err=True)
        raise typer.Exit(2)

    store = ArtifactStore(capabilities_root)
    artifact = store.resolve_ref(capability)

    if target is ApprovalState.APPROVED:
        # R-M7-2, enforced at the gate rather than advised in a doc. Combined
        # with R-M7-1 -- only approved capabilities are offered -- this means a
        # description that is really a discovery goal can never reach a calling
        # agent. Checked here rather than in the schema so the defective 1.0.0
        # artifacts stay loadable as history; they simply cannot be offered.
        from .artifact.authoring import _restates

        text = " ".join((artifact.capability.description or "").split())
        goal = artifact.provenance.discovery_goal if artifact.provenance else ""
        problem = ("has no description" if not text else
                   "describes the RECORDING, not the capability -- it restates the "
                   "discovery goal" if _restates(text, goal) else "")
        if problem:
            typer.echo(
                f"{artifact.ref} {problem}.\n"
                f"A description is what a production model reads when it decides "
                f"whether to call this capability.\n"
                f"  cua describe {artifact.capability.id} --description \"...\"",
                err=True)
            raise typer.Exit(2)

    was = artifact.capability.approval_state.value
    updated = store.set_approval(artifact.ref, target, reason=reason)

    typer.echo(f"{updated.ref}: {was} -> {updated.capability.approval_state.value}")
    typer.echo(f"content hash unchanged: {updated.verify_hash()}  {updated.content_hash}")
    if target is ApprovalState.APPROVED and \
            updated.capability.risk_tier.value == "writes_irreversible":
        typer.echo("\nThis capability commits something. Replay still requires "
                   "--confirm-irreversible on every call.")


@app.command("replay")
def replay_cmd(
    capability: str = typer.Argument(..., help="Capability id, or id@version."),
    params: str = typer.Option("{}", "--params", help="JSON object of inputs."),
    tenant: str = typer.Option("demo-cu", help="Tenant profile to run against."),
    base_url: str = typer.Option("", help="Override the profile's base_url."),
    inject: str = typer.Option("", "--inject",
                               help="Arm a mockbank fault before the run, to exercise "
                                    "the error paths on demand."),
    arm_at_step: str = typer.Option("", "--arm-at-step",
                                    help="Arm --inject when this step starts, instead of "
                                         "before the run. A fault armed up front is spent "
                                         "on the login POST."),
    confirm_irreversible: bool = typer.Option(
        False, "--confirm-irreversible",
        help="Explicit intent to perform this capability's writes. Required, together "
             "with an approved artifact, before an irreversible flow will replay."),
    headed: bool = typer.Option(False, help="Show the browser."),
    screenshots: str = typer.Option("failure", help="failure | all | none."),
    policy_file: str = typer.Option("config/policy.yaml", "--policy",
                                    help="Allowlist policy, enforced inside act()."),
    profiles_root: str = typer.Option("profiles", help="Profile directory."),
    capabilities_root: str = typer.Option("capabilities", help="Where artifacts live."),
    evidence_root: str = typer.Option("evidence", help="Where to write the run's evidence."),
) -> None:
    """Replay a recorded capability. No model, no API key, no network but the app.

    Exit code is the result variant, so a caller can branch on it without
    parsing anything: 0 success, 2 business outcome, 3 escalated, 1 failure.
    A business outcome is NOT a failure -- "no such member" is an answer the
    caller asked for, and collapsing it into an error is the trap the brief
    names in its own glossary.
    """
    import asyncio
    import json as _json

    from dotenv import load_dotenv

    # The profile holds credential REFERENCES (`env:MOCKBANK_USER`), never
    # values, so the values have to come from somewhere at run time. This does
    # not make replay need an API key -- `.env.example` ships the fixture's fake
    # credentials and nothing else is read from it here.
    load_dotenv()

    try:
        parsed = _json.loads(params)
    except _json.JSONDecodeError as exc:
        typer.echo(f"--params is not valid JSON: {exc}", err=True)
        raise typer.Exit(2)

    code = asyncio.run(_run_replay(
        capability=capability, params=parsed, tenant=tenant, base_url=base_url,
        inject=inject, arm_at_step=arm_at_step,
        confirm_irreversible=confirm_irreversible, headed=headed,
        screenshots=screenshots, policy_file=policy_file, profiles_root=profiles_root,
        capabilities_root=capabilities_root, evidence_root=evidence_root,
    ))
    raise typer.Exit(code)


#: Result variant -> process exit code. Four variants, four codes: the contract
#: a shell script sees is the same one the calling agent sees.
EXIT_FOR = {"success": 0, "failure": 1, "business_outcome": 2, "escalated": 3}


async def _run_replay(
    *, capability, params, tenant, base_url, inject, arm_at_step, confirm_irreversible,
    headed, screenshots, policy_file, profiles_root, capabilities_root, evidence_root,
) -> int:
    import httpx
    from playwright.async_api import async_playwright

    from .artifact.store import ArtifactStore
    from .observability.evidence import EvidenceWriter
    from .observability.journal import Journal
    from .policy.allowlist import load_policy
    from .profiles.resolve import ProfileRepository, specialize
    from .replay.engine import CredentialResolver, ReplayEngine
    from .session.auth import Authenticator
    from .surfaces.web_playwright import WebSurface

    store = ArtifactStore(capabilities_root)
    # `resolve_ref` accepts "id" or "id@version" -- a demo command should not
    # make somebody type a semver they can read off the filename.
    artifact = store.resolve_ref(capability)

    repository = ProfileRepository(profiles_root)
    resolved = repository.resolve(tenant)
    if base_url:
        surface_cfg = resolved.profile.surface.model_copy(update={"base_url": base_url})
        resolved = resolved.__class__(
            profile=resolved.profile.model_copy(update={"surface": surface_cfg}),
            lineage=resolved.lineage, hash=resolved.hash,
        )

    # The tenant overlay, applied at LOAD time (Part 6). One recording serves
    # every institution on the product; what runs is base + overlay, computed
    # fresh and journaled so the specialization is traceable rather than
    # invisible. The stored artifact is never mutated.
    effective, report = specialize(artifact, resolved)

    allowlist = load_policy(policy_file).for_capability(artifact.ref)

    # Fail here rather than three screens in as SESSION_EXPIRED. An unresolved
    # credential reference and a genuinely expiring session produce the same
    # symptom, and only one of them is fixed by copying a file.
    credentials = CredentialResolver()
    unresolved = [
        f"{name} -> {ref}"
        for name, ref in resolved.profile.auth.credentials.items()
        if not credentials.resolve(ref)
    ]
    if unresolved:
        typer.echo(
            f"credential reference(s) resolve to nothing: {', '.join(unresolved)}.\n"
            f"The profile holds references, never values -- copy .env.example to .env "
            f"(the fixture's credentials are fake and committed there).", err=True)
        return 1
    evidence = EvidenceWriter(evidence_root, f"run_{__import__('uuid').uuid4().hex[:12]}")

    origin = resolved.base_url.rsplit(f"/t/{tenant}", 1)[0]

    def arm() -> None:
        httpx.post(f"{origin}/t/{tenant}/__control",
                   params={"fault": inject, "count": 1}, timeout=10)

    class ArmAtStep(type(evidence.journal)):
        """Arms a fault when a named step starts.

        A fault armed before the run is spent on the login POST, so choosing
        WHERE the session breaks means arming from inside the run -- and the
        journal is the only thing the engine tells about its own progress.
        """

        armed = False

        def emit(self, kind: str, **data):
            if (not ArmAtStep.armed and kind == "step.started"
                    and data.get("step") == arm_at_step):
                ArmAtStep.armed = True
                arm()
            super().emit(kind, **data)

    journal: Journal = evidence.journal
    if inject and arm_at_step:
        journal = ArmAtStep(evidence.dir / "journal.jsonl")

    typer.echo(f"capability  {artifact.ref}  ({artifact.capability.approval_state.value})")
    typer.echo(f"tenant      {tenant}  ({' <- '.join(resolved.lineage)})")
    typer.echo(f"target      {resolved.base_url}")
    typer.echo(f"allowlist   {policy_file}  ({len(allowlist.denied_paths)} denial(s), "
               f"max {allowlist.max_navigations} navigations)")
    if inject:
        typer.echo(f"fault       {inject}"
                   + (f" armed at {arm_at_step}" if arm_at_step else " armed now"))
    if report.locator_overrides_applied or report.param_defaults_applied:
        typer.echo(f"overlay     {len(report.locator_overrides_applied)} locator override(s) "
                   f"{report.locator_overrides_applied}, param defaults "
                   f"{report.param_defaults_applied or dict()}")
    if report.unused_overrides:
        # Almost always a stale override left behind after a re-record, and
        # silently doing nothing is how it stays that way.
        typer.echo(f"overlay     WARNING unused override(s): {report.unused_overrides}")
    typer.echo(f"evidence    {evidence.dir}\n")

    journal.emit(
        "profile.specialized", capability=artifact.ref, tenant=tenant,
        lineage=list(report.profile_lineage), profile_hash=report.profile_hash,
        locator_overrides=report.locator_overrides_applied,
        param_defaults=report.param_defaults_applied,
        recoveries_inherited=report.recoveries_inherited,
        unused_overrides=report.unused_overrides,
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed)
        context = await browser.new_context(
            viewport={"width": resolved.profile.surface.viewport.width,
                      "height": resolved.profile.surface.viewport.height})
        # Part 7's third failure artifact. Tracing is started unconditionally and
        # kept only when the run ends badly: you cannot decide to start tracing
        # after the thing you needed traced. Chromium's own recorded timeline,
        # openable with `playwright show-trace`, is the one debugging aid this
        # system cannot reconstruct from its own journal.
        await context.tracing.start(screenshots=True, snapshots=True, sources=False)
        page = await context.new_page()
        surface = WebSurface(page, journal=journal)
        result = None
        try:
            # Sign in before arming, for the same reason the fault is armed
            # mid-run: authentication is not the thing under test.
            await Authenticator(surface, resolved, credentials=credentials,
                                journal=journal).sign_in()
            if inject and not arm_at_step:
                arm()

            engine = ReplayEngine(
                surface, effective, resolved, journal=journal,
                credentials=credentials, tenant=tenant, policy=allowlist,
                confirm_irreversible=confirm_irreversible,
                evidence=evidence, screenshots=screenshots,
            )
            result = await engine.run(params)
        finally:
            try:
                if result is not None and not result.ok:
                    pack = evidence.dir / "failure"
                    pack.mkdir(parents=True, exist_ok=True)
                    await context.tracing.stop(path=pack / "trace.zip")
                else:
                    await context.tracing.stop()
            except Exception:
                pass  # a trace is never what breaks a run
            await browser.close()

    typer.echo(result.describe())
    typer.echo(f"\nevidence    {evidence.dir}")
    return EXIT_FOR.get(result.status.value, 1)


@app.command("discover")
def discover_cmd(
    goal: str = typer.Option(..., "--goal", help="What the capability should achieve."),
    capability_id: str = typer.Option(..., "--capability-id",
                                      help="Id to record it under, e.g. member.lookup_balance."),
    tenant: str = typer.Option("demo-cu", help="Tenant profile to run against."),
    base_url: str = typer.Option("", help="Override the profile's base_url."),
    allow_irreversible: bool = typer.Option(
        False, "--allow-irreversible",
        help="Permit actions that commit something. Off by default: an exploring model "
             "must not be able to post a transaction because it was curious."),
    max_steps: int = typer.Option(25, help="Step budget."),
    headed: bool = typer.Option(True, help="Show the browser while it works."),
    verify: bool = typer.Option(True, help="Replay the recording once, to prove it works."),
    effort: str = typer.Option("", help="Model effort: high | medium | low. "
                                       "Defaults to $CUA_EFFORT, else high."),
    model: str = typer.Option("", help="Model for the discovery loop. Defaults to "
                                       "$CUA_MODEL, else claude-opus-5. Point it at a "
                                       "cheaper model while iterating; the graded run "
                                       "uses the default."),
    reviewer_model: str = typer.Option("", help="Model for the authoring review pass. "
                                                "Defaults to $CUA_REVIEWER_MODEL, else "
                                                "Haiku -- it fills a fixed schema from "
                                                "an already-successful transcript."),
    preflight: bool = typer.Option(True, help="Dry-run the loop's control logic against "
                                              "scripted observations before spending a "
                                              "single token on the real model."),
    profiles_root: str = typer.Option("profiles", help="Profile directory."),
    capabilities_root: str = typer.Option("capabilities", help="Where to save the artifact."),
    policy_file: str = typer.Option("config/policy.yaml", "--policy",
                                    help="Allowlist policy. Enforced inside act(); a missing "
                                         "file is an error, not an open door."),
) -> None:
    """Discover a flow with the LLM, record it as a capability, and verify it.

    The only command that needs ANTHROPIC_API_KEY. Replay, the console and the
    whole test suite run offline.
    """
    import asyncio

    from dotenv import load_dotenv

    import os

    from .artifact.authoring import DEFAULT_REVIEWER_MODEL

    load_dotenv()
    asyncio.run(_run_discovery(
        goal=goal, capability_id=capability_id, tenant=tenant, base_url=base_url,
        allow_irreversible=allow_irreversible, max_steps=max_steps, headed=headed,
        verify=verify,
        effort=effort or os.environ.get("CUA_EFFORT", "high"),
        model=model or os.environ.get("CUA_MODEL", "claude-opus-5"),
        reviewer_model=(reviewer_model or os.environ.get("CUA_REVIEWER_MODEL")
                        or DEFAULT_REVIEWER_MODEL),
        preflight=preflight,
        profiles_root=profiles_root, capabilities_root=capabilities_root,
        policy_file=policy_file,
    ))


async def _run_discovery(
    *, goal, capability_id, tenant, base_url, allow_irreversible, max_steps, headed,
    verify, effort, model, reviewer_model, preflight, profiles_root, capabilities_root,
    policy_file,
) -> None:
    from playwright.async_api import async_playwright

    from .artifact.schema import ProductRef
    from .artifact.store import ArtifactStore
    from .discovery.agent import DiscoveryLimits
    from .artifact.authoring import AnthropicReviewer
    from .discovery.model import AnthropicClient
    from .discovery.session import discover
    from .profiles.resolve import ProfileRepository
    from .policy.allowlist import load_policy
    from .replay.engine import CredentialResolver
    from .surfaces.web_playwright import WebSurface

    repository = ProfileRepository(profiles_root)
    resolved = repository.resolve(tenant)
    if base_url:
        surface_cfg = resolved.profile.surface.model_copy(update={"base_url": base_url})
        resolved = resolved.__class__(
            profile=resolved.profile.model_copy(update={"surface": surface_cfg}),
            lineage=resolved.lineage, hash=resolved.hash,
        )

    product_ref = resolved.profile.extends or resolved.profile.ref
    base = repository.load_raw(product_ref)

    # Loaded before the client is built, so a malformed policy costs nothing.
    # `load_policy` raises on a missing file rather than defaulting to "allow
    # everything" -- a run with no restrictions has to be a file somebody wrote.
    allowlist = load_policy(policy_file).for_capability(capability_id)

    # Preflight BEFORE the client is built, so a control-logic bug costs nothing.
    if preflight:
        from .discovery.dryrun import run_control_checks

        typer.echo("preflight  dry-running the loop's control logic (no API calls)...")
        report = await run_control_checks(resolved)
        typer.echo(report.render(indent="           "))
        if not report.ok:
            typer.echo("\npreflight FAILED -- refusing to spend a real run on a broken loop.",
                       err=True)
            raise typer.Exit(4)

    client = AnthropicClient(model=model, effort=effort)

    typer.echo(f"goal      {goal}")
    typer.echo(f"tenant    {tenant}  ({' <- '.join(resolved.lineage)})")
    typer.echo(f"target    {resolved.base_url}")
    typer.echo(f"model     {model} (effort={effort}, adaptive thinking)")
    typer.echo(f"reviewer  {reviewer_model}")
    typer.echo(f"policy    irreversible actions "
               f"{'ALLOWED' if allow_irreversible else 'blocked'}")
    typer.echo(f"allowlist {policy_file}: {resolved.base_url} + "
               f"{len(allowlist.allowed_paths)} path rule(s), "
               f"{len(allowlist.denied_paths)} denial(s), "
               f"max {allowlist.max_navigations} navigations\n")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=not headed)
        context = await browser.new_context(
            viewport={"width": resolved.profile.surface.viewport.width,
                      "height": resolved.profile.surface.viewport.height})
        page = await context.new_page()
        try:
            outcome = await discover(
                WebSurface(page), client, resolved,
                goal=goal, tenant=tenant, capability_id=capability_id,
                product=ProductRef(vendor=base.profile.vendor,
                                   product=base.profile.product or base.profile.id,
                                   version_range=f">={base.profile.version}"),
                app_profile_ref=product_ref,
                store=ArtifactStore(capabilities_root),
                repository=repository,
                limits=DiscoveryLimits(max_steps=max_steps),
                allow_irreversible=allow_irreversible,
                model_name=model,
                verify=verify,
                credentials=CredentialResolver(),
                reviewer=AnthropicReviewer(client._client, model=reviewer_model),
                policy=allowlist,
            )
        finally:
            await browser.close()

    typer.echo("\n" + outcome.summary())
    if outcome.refusal:
        raise typer.Exit(3)
    if not outcome.run.succeeded:
        raise typer.Exit(1)


if __name__ == "__main__":
    app()
