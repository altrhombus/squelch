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
        self.apple_music_url: Optional[str] = None
        # lifecycle state pushed to the frontend for status display
        self.state: str = "idle"           # idle | tuning | buffering | live
        self._last_history_key: Optional[str] = None
        self._history_save_task: Optional[asyncio.Task] = None
        self._has_rtp: bool = False   # True once RT+ structured data received
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
            "apple_music_url": self.apple_music_url,
            "state": self.state,
        }

    def update_tune(self, frequency: float, band: str):
        # Cancel any pending debounced history/art save from the previous station
        if self._history_save_task and not self._history_save_task.done():
            self._history_save_task.cancel()
        self._history_save_task = None

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
        self.apple_music_url = None
        self._has_rtp = False
        self.state = "tuning"
        self._clear_art()

    def update_state(self, state: str):
        self.state = state

    def update_rds(self, ps: str = None, rt: str = None, pty: str = None, pi: str = None,
                   rtp_title: str = None, rtp_artist: str = None):
        changed = False

        if ps and ps.strip() and ps.strip() != self.station_name:
            self.station_name = ps.strip()
            changed = True

        # RT+ structured tags (IEC 62106 Annex A) take priority over heuristic
        # text splitting.  Once a station provides RT+ data, suppress the
        # fallback parser so stale RT text doesn't overwrite clean tag values.
        if rtp_title is not None and rtp_title != self.title:
            self.title = rtp_title
            self._has_rtp = True
            changed = True
        if rtp_artist is not None and rtp_artist != self.artist:
            self.artist = rtp_artist
            self._has_rtp = True
            changed = True

        # Heuristic RT parsing — only when no RT+ data has been received
        if rt and rt.strip() and not self._has_rtp:
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
            self._debounce_history_save()

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
            self._debounce_history_save()

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

    def _debounce_history_save(self):
        """Cancel any pending save and restart the 4-second stability window.

        RDS RadioText arrives incrementally and can contain transient errors.
        We wait until no new changes arrive for 4 seconds before persisting,
        which lets partial titles and self-correcting corruption settle.
        """
        if self._history_save_task and not self._history_save_task.done():
            self._history_save_task.cancel()
        self._history_save_task = asyncio.ensure_future(self._delayed_save())

    async def _delayed_save(self):
        try:
            await asyncio.sleep(4)
            await self.save_history()
            # If no native art arrived (HD stations often don't transmit LOT),
            # fall back to iTunes for any band that has a stable artist + title.
            if (not self.has_art
                    and self.artist
                    and self.title):
                from .art_lookup import fetch_itunes_art
                result = await fetch_itunes_art(self.artist, self.title)
                if result:
                    try:
                        shutil.copy2(result["art_path"], ART_PATH)
                        self.has_art = True
                        self.apple_music_url = result.get("apple_music_url")
                        await self.broadcast()
                    except OSError as exc:
                        logger.warning("Failed to copy iTunes art: %s", exc)
        except asyncio.CancelledError:
            pass

    async def save_history(self):
        key = f"{self.artist}|{self.title}|{self.station_name}"
        if (key == self._last_history_key
                or not self.station_name
                or not self.artist
                or not self.title):
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
    """Split RDS RadioText into (artist, title).

    Supports two separator formats:
      "Artist - Title"   →  dash separator, split on first ' - '
      "Title by Artist"  →  'by' separator, split on last ' by ' so that
                            a 'by' inside the title (e.g. "Stand By Me by
                            Ben E. King") lands correctly. Requires the
                            artist portion to be ≥ 3 chars to avoid
                            misreading "Stand By Me" as artist="Me".
    """
    if " - " in rt:
        parts = rt.split(" - ", 1)
        return parts[0].strip() or None, parts[1].strip() or None
    idx = rt.lower().rfind(" by ")
    if idx != -1:
        title  = rt[:idx].strip()
        artist = rt[idx + 4:].strip()
        if len(artist) >= 3:
            return artist or None, title or None
    return None, rt or None


# Module-level singleton
state = MetadataState()
