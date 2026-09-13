"""Shared fixtures: a live mockbank and a real browser.

The locator work only means something against the actual hostile markup, so the
perception tests drive a real server in a real browser rather than a captured
HTML string. It costs a few seconds and removes the possibility of proving the
extractor works against a fixture we quietly sanitized.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest
import pytest_asyncio
import uvicorn

from mockbank import data, faults


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_server() -> str:
    port = _free_port()
    config = uvicorn.Config("mockbank.app:app", host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    if not server.started:
        raise RuntimeError("mockbank did not start")

    yield f"http://127.0.0.1:{port}"

    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(autouse=True)
def _clean_fixture_state():
    faults.clear()
    data.reset()
    yield
    faults.clear()


@pytest_asyncio.fixture
async def browser():
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        try:
            yield b
        finally:
            await b.close()


@pytest_asyncio.fixture
async def page(browser):
    ctx = await browser.new_context(viewport={"width": 1280, "height": 900})
    p = await ctx.new_page()
    try:
        yield p
    finally:
        await ctx.close()
