"""Headless-browser smoke tests for the web app's live-radio behaviour.

No SDR: control endpoints (/tune, /seek, /squelch) are intercepted and
recorded, the live WebSocket is detached, and server frames are injected by
calling the page's own applyMeta().  Playwright's fake clock drives the
debounce/hold/release timers deterministically.

Needs `pip install -e ".[ui]"` and `python -m playwright install chromium`;
skipped when Playwright isn't installed.
"""

import json
import time
from dataclasses import dataclass, field

import pytest

pytest.importorskip("playwright")

LIVE = {"state": "live", "band": "fm", "frequency": 98.1e6, "station_name": "KTEST"}


@dataclass
class Ui:
    page: object
    posts: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def meta(self, **overrides):
        self.page.evaluate("m => applyMeta(m)", {**LIVE, **overrides})

    def js(self, expr):
        return self.page.evaluate(expr)

    def tick(self, ms):
        self.page.clock.run_for(ms)

    def posted(self, path, contains="", wait=2.0):
        """Requests to path (body containing `contains`), polling briefly:
        the page's fetch() reaches the route handler asynchronously."""
        deadline = time.monotonic() + wait
        while True:
            hits = [d for _, u, d in self.posts if u.endswith(path) and contains in (d or "")]
            if hits or time.monotonic() > deadline:
                return hits
            self.page.wait_for_timeout(50)


@pytest.fixture
def ui(browser, app_url):
    ctx = browser.new_context()
    page = ctx.new_page()
    u = Ui(page)
    page.on("pageerror", lambda e: u.errors.append(str(e)))

    def intercept(route):
        req = route.request
        u.posts.append((req.method, req.url, req.post_data))
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"ok": True}))

    for pat in ("**/tune", "**/seek", "**/seek/stop", "**/squelch"):
        page.route(pat, intercept)

    # This device last used AM — exercises mirroring the server's band.
    page.add_init_script("localStorage.setItem('squelch.band', 'am')")
    page.clock.install()
    page.goto(app_url + "/")
    page.wait_for_function("typeof applyMeta === 'function'")
    # Detach the live socket (the tests supply every frame) and count
    # stream starts instead of opening /stream.
    page.evaluate("""() => {
        connectWs = () => {};
        if (ws) { ws.onmessage = null; ws.close(); }
        window.__starts = 0;
        _startStream = () => { window.__starts++; };
    }""")
    u.posts.clear()
    yield u
    ctx.close()
    assert not u.errors, u.errors


def test_mirrors_server_band_without_cancelling_its_seek(ui):
    ui.meta(seeking=True)
    assert ui.js("[currentBand, displayFreq]") == ["fm", 98.1]
    assert not ui.posted("/seek/stop", wait=0.5)


def test_stale_frame_does_not_snap_dial_back_after_tune(ui):
    ui.js("tune(101.1, 'fm')")
    ui.meta()                                   # still 98.1: stale
    assert ui.js("displayFreq") == 101.1
    ui.meta(frequency=101.1e6)                  # server confirms
    ui.meta(frequency=99.5e6)                   # then tuned elsewhere
    assert ui.js("displayFreq") == 99.5


def test_tune_hold_expires_if_never_confirmed(ui):
    ui.js("tune(102.3, 'fm')")
    ui.tick(3000)
    ui.meta()
    assert ui.js("displayFreq") == 98.1


def test_space_on_focused_button_does_not_toggle_play(ui):
    ui.page.focus("#btn-step-up")
    ui.page.keyboard.press(" ")
    assert ui.js("window.__starts") == 0
    ui.js("document.activeElement.blur()")
    ui.page.keyboard.press(" ")
    assert ui.js("window.__starts") == 1


def test_hd_chips_rebuild_only_on_change(ui):
    hd = dict(band="hd", hd_locked=True, hd_channels_available=[1, 2, 3], hd_channel=1)
    ui.meta(**hd)
    ui.js("window.__chip = document.querySelector('#hd-channels .chip')")
    ui.meta(**hd)
    assert ui.js("document.querySelector('#hd-channels .chip') === window.__chip")
    ui.meta(**{**hd, "hd_channel": 2})
    assert ui.js("document.querySelector('#hd-channels .chip.active').dataset.ch") == "2"


def test_identical_frames_do_not_rewrite_aria_live_text(ui):
    ui.meta()
    ui.js("""() => { window.__muts = 0;
        new MutationObserver(ms => window.__muts += ms.length).observe(
          document.getElementById('station-name'),
          { childList: true, characterData: true, subtree: true }); }""")
    ui.meta()
    ui.meta()
    # MutationObserver callbacks are microtasks — flush them before reading
    flush = "() => Promise.resolve().then(() => window.__muts)"
    assert ui.js(flush) == 0
    ui.meta(station_name="KOTHER")             # control: a real change is seen
    assert ui.js(flush) > 0


def test_auto_hd_retunes_without_starting_audio(ui):
    ui.js("localStorage.setItem('squelch.autohd', '1')")
    ui.meta(hd_available=True)
    assert ui.posted("/tune", '"band":"hd"')
    assert ui.js("window.__starts") == 0


def test_ruler_reports_readable_value(ui):
    ui.js("setBand('fm', { retune: false }); setDisplayFreq(91.1, { commit: false })")
    assert ui.page.get_attribute("#ruler", "aria-valuetext") == "91.1 MHz"


def test_stop_releases_stream_after_grace(ui):
    ui.js("player.setAttribute('src', '/nothing'); setPlayState(true); stopPlayback()")
    assert ui.js("player.getAttribute('src')") is not None   # quick resume still possible
    ui.tick(31_000)
    assert ui.js("player.getAttribute('src')") is None


def test_tune_during_recording_playback_switches_to_live(ui):
    ui.js("playRecording(1, 'My clip'); setPlayState(true)")
    ui.meta()
    assert ui.js("document.getElementById('station-name').textContent") == "My clip"
    ui.js("tune(99.5, 'fm')")
    assert ui.js("window.__starts") == 1
