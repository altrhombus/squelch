import asyncio
import logging
import os
import shutil
import time
from typing import Optional
from fastapi import WebSocket

logger = logging.getLogger(__name__)

ART_DIR = "/tmp/sdr-art"
ART_PATH = os.path.join(ART_DIR, "current.jpg")
PLACEHOLDER_ART = os.path.join(os.path.dirname(__file__), "..", "frontend", "placeholder.jpg")


class MetadataState:
    def __init__(self):
        self.frequency: Optional[float] = None
        self.band: Optional[str] = None
        self.station_name: Optional[str] = None
        self.slogan: Optional[str] = None
        self.artist: Optional[str] = None
        self.title: Optional[str] = None
        self.pty: Optional[str] = None
        self.pi_code: Optional[str] = None
        self.signal_bars: int = 0          # 0–5
        self.hd_locked: bool = False
        self.stereo: bool = False
        self.has_art: bool = False
        # lifecycle state pushed to the frontend for status display
        self.state: str = "idle"           # idle | tuning | buffering | live
        self._last_history_key: Optional[str] = None
        self._websockets: set[WebSocket] = set()

    def to_dict(self) -> dict:
        return {
            "frequency": self.frequency,
            "band": self.band,
            "station_name": self.station_name,
            "slogan": self.slogan,
            "artist": self.artist,
            "title": self.title,
            "pty": self.pty,
            "pi_code": self.pi_code,
            "signal_bars": self.signal_bars,
            "hd_locked": self.hd_locked,
            "stereo": self.stereo,
            "has_art": self.has_art,
            "art_url": "/art/current.jpg" if self.has_art else None,
            "state": self.state,
        }

    def update_tune(self, frequency: float, band: str):
        self.frequency = frequency
        self.band = band
        self.station_name = None
        self.slogan = None
        self.artist = None
        self.title = None
        self.pty = None
        self.pi_code = None
        self.hd_locked = False
        self.stereo = False
        self.has_art = False
        self.state = "tuning"
        self._clear_art()

    def update_state(self, state: str):
        self.state = state

    def update_rds(self, ps: str = None, rt: str = None, pty: str = None, pi: str = None):
        changed = False
        if ps and ps.strip() and ps.strip() != self.station_name:
            self.station_name = ps.strip()
            changed = True
        if rt and rt.strip():
            parsed = _parse_rt(rt.strip())
            if parsed != (self.artist, self.title):
                self.artist, self.title = parsed
                changed = True
        if pty and pty != self.pty:
            self.pty = pty
            changed = True
        if pi and pi != self.pi_code:
            self.pi_code = pi
            changed = True
        if changed:
            asyncio.ensure_future(self.broadcast())
            asyncio.ensure_future(self.save_history())

    def update_nrsc5(
        self,
        station_name: str = None,
        slogan: str = None,
        artist: str = None,
        title: str = None,
        pty: str = None,
        art_path: str = None,
        hd_locked: bool = None,
    ):
        changed = False
        if station_name and station_name != self.station_name:
            self.station_name = station_name
            changed = True
        if slogan and slogan != self.slogan:
            self.slogan = slogan
            changed = True
        if artist and artist != self.artist:
            self.artist = artist
            changed = True
        if title and title != self.title:
            self.title = title
            changed = True
        if pty and pty != self.pty:
            self.pty = pty
            changed = True
        if hd_locked is not None and hd_locked != self.hd_locked:
            self.hd_locked = hd_locked
            changed = True
        if art_path:
            try:
                os.makedirs(ART_DIR, exist_ok=True)
                shutil.copy2(art_path, ART_PATH)
                self.has_art = True
                changed = True
            except OSError as e:
                logger.warning("Failed to copy cover art: %s", e)
        if changed:
            asyncio.ensure_future(self.broadcast())
            asyncio.ensure_future(self.save_history())

    def update_signal(self, bars: int, stereo: bool = None):
        self.signal_bars = max(0, min(5, bars))
        if stereo is not None:
            self.stereo = stereo

    def _clear_art(self):
        try:
            if os.path.exists(ART_PATH):
                os.remove(ART_PATH)
        except OSError:
            pass

    def register_ws(self, ws: WebSocket):
        self._websockets.add(ws)

    def unregister_ws(self, ws: WebSocket):
        self._websockets.discard(ws)

    async def broadcast(self):
        if not self._websockets:
            return
        import json
        msg = json.dumps(self.to_dict())
        dead = set()
        for ws in self._websockets:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        self._websockets -= dead

    async def broadcast_event(self, event: str):
        if not self._websockets:
            return
        import json
        msg = json.dumps({"event": event})
        dead = set()
        for ws in self._websockets:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.add(ws)
        self._websockets -= dead

    async def save_history(self):
        key = f"{self.artist}|{self.title}|{self.station_name}"
        if key == self._last_history_key or not (self.artist or self.title):
            return
        self._last_history_key = key
        from .db import get_db
        db = await get_db()
        try:
            await db.execute(
                """INSERT INTO history (station_name, artist, title, pty, frequency, band)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (self.station_name, self.artist, self.title, self.pty, self.frequency, self.band),
            )
            await db.commit()
        except Exception as e:
            logger.warning("Failed to save history: %s", e)
        finally:
            await db.close()


def _parse_rt(rt: str) -> tuple[Optional[str], Optional[str]]:
    """Split RDS RadioText into (artist, title) when separated by ' - '."""
    if " - " in rt:
        parts = rt.split(" - ", 1)
        return parts[0].strip() or None, parts[1].strip() or None
    return None, rt or None


# Module-level singleton
state = MetadataState()
