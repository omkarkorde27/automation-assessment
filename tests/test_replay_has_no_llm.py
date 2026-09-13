"""The central claim, enforced structurally.

"Deterministic replay runs without the LLM in the decision loop" is the whole
premise. Prose cannot enforce it and a code review will eventually miss it, so
this walks the actual import graph: if anything reachable from the replay engine
ever imports `anthropic`, this fails.

The failure mode it guards against is not somebody deliberately calling a model
from the executor. It is a helper being added to a shared module -- a "just ask
the model to find this element" fallback in the locator layer, say -- that
quietly makes replay non-deterministic everywhere it is used.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src" / "cua"

# Reachable from replay at runtime. Discovery and its recorder are deliberately
# excluded -- that is where a model belongs.
REPLAY_REACHABLE = [
    "replay", "conditions", "locators", "perception", "surfaces",
    "artifact", "profiles", "observability",
]

FORBIDDEN = {"anthropic", "openai"}


def _module_files() -> list[Path]:
    files: list[Path] = []
    for package in REPLAY_REACHABLE:
        files.extend((SRC / package).rglob("*.py"))
    return sorted(files)


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return found


def test_the_replay_path_never_imports_a_model_client():
    offenders = {
        str(path.relative_to(SRC)): sorted(_imports(path) & FORBIDDEN)
        for path in _module_files()
        if _imports(path) & FORBIDDEN
    }
    assert offenders == {}, (
        f"a module on the replay path imports a model client: {offenders}. "
        f"Replay must decide nothing with an LLM."
    )


def test_the_guard_actually_covers_the_engine():
    """A test that passes because it scanned nothing is worse than no test."""
    scanned = {str(p.relative_to(SRC)) for p in _module_files()}
    for required in ("replay/engine.py", "replay/result.py", "conditions/dsl.py",
                     "locators/resolve.py", "surfaces/web_playwright.py"):
        assert required in scanned, f"{required} was not scanned"
    assert len(scanned) >= 15


def test_importing_replay_does_not_pull_in_anthropic():
    """The static check catches source-level imports; this catches a transitive
    one arriving through a package __init__."""
    import subprocess
    import sys

    code = (
        "import sys; import cua.replay.engine, cua.conditions.dsl; "
        "bad=[m for m in sys.modules if m.split('.')[0] in {'anthropic','openai'}]; "
        "print(bad)"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(SRC.parent.parent))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", f"replay pulled in a model client: {out.stdout}"


def test_replay_result_is_importable_without_a_browser_or_a_key():
    """The result contract is what a calling agent depends on. It must not drag
    in Playwright or require credentials just to read a status."""
    import subprocess
    import sys

    code = (
        "import os; os.environ.pop('ANTHROPIC_API_KEY', None); "
        "from cua.replay.result import ReplayStatus, FailureClass; "
        "print(len(list(FailureClass)))"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         cwd=str(SRC.parent.parent))
    assert out.returncode == 0, out.stderr
    assert int(out.stdout.strip()) >= 10
