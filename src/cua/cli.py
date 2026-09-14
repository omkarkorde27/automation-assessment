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
    ))


async def _run_discovery(
    *, goal, capability_id, tenant, base_url, allow_irreversible, max_steps, headed,
    verify, effort, model, reviewer_model, preflight, profiles_root, capabilities_root,
) -> None:
    from playwright.async_api import async_playwright

    from .artifact.schema import ProductRef
    from .artifact.store import ArtifactStore
    from .discovery.agent import DiscoveryLimits
    from .artifact.authoring import AnthropicReviewer
    from .discovery.model import AnthropicClient
    from .discovery.session import discover
    from .profiles.resolve import ProfileRepository
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
               f"{'ALLOWED' if allow_irreversible else 'blocked'}\n")

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
