"""CLI entry point.

At M0 this serves the mockbank fixture and arms faults against it. The
`discover`, `replay`, and `serve-console` commands land with their milestones.
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


if __name__ == "__main__":
    app()
