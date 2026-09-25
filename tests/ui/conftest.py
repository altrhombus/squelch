"""Fixtures for the headless-browser UI smoke tests.

The real app runs in a background thread (lifespan included, temp DB and
recordings dir, no SDR).  Tests drive the page through Playwright's
Chromium; see test_frontend_smoke.py for how control endpoints and
WebSocket frames are handled.
"""

import os
import socket
import threading
import time

import pytest


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def app_url(tmp_path_factory):
    import uvicorn

    import backend.main as main_mod

    tmp = tmp_path_factory.mktemp("ui")
    port = _free_port()
    cfg = {
        "server": {"host": "127.0.0.1", "port": port},
        "sdr": {"device_index": 0, "gain": "auto", "ppm_correction": 0, "deemphasis_us": 75},
        "recordings": {"output_dir": str(tmp / "recordings")},
        "database": {"path": str(tmp / "ui.db")},
    }
    orig_load = main_mod._load_config
    main_mod._load_config = lambda: cfg

    server = uvicorn.Server(uvicorn.Config(main_mod.app, host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("UI test server failed to start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        main_mod._load_config = orig_load


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import Error, sync_playwright

    with sync_playwright() as p:
        # SQUELCH_UI_CHROMIUM points at a specific Chromium build when the
        # one Playwright expects isn't installed (CI installs the right one).
        exe = os.environ.get("SQUELCH_UI_CHROMIUM") or None
        try:
            b = p.chromium.launch(executable_path=exe)
        except Error as e:
            if os.environ.get("CI"):
                raise
            pytest.skip(f"Chromium not available ({e.message.splitlines()[0]}); "
                        "run `python -m playwright install chromium`")
        yield b
        b.close()
