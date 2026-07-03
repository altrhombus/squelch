import asyncio
import logging
import os
import shutil
import time as _time
from difflib import SequenceMatcher
from typing import Optional
from fastapi import WebSocket

from .dynamic_ps import DynamicPsAssembler

logger = logging.getLogger(__name__)

ART_DIR = "/tmp/sdr-art"
ART_PATH = os.path.join(ART_DIR, "current.jpg")


class MetadataState:
    def __init__(self, config: Optional[dict] = None):
        meta_cfg = (config or {}).get("metadata") or {}
        # iTunes Search lookup (cover art, Apple Music link, canonical track
        # info).  Sends artist/title of the current track to Apple; disable
        # for privacy.  Order correction depends on the lookup response, so
        # it is implicitly off when the lookup is.
        self._itunes_enabled: bool = bool(meta_cfg.get("itunes_lookup", True))
        # Swap artist/title when the iTunes canonical names show the station
        # transmits "Title - Artist" order.
        self._order_correction: bool = bool(meta_cfg.get("order_correction", True))
        # Show non-song PS messages (show promos, DJ schedules) on the track
        # line, the way RadioText promos already display.  Garbled fragments
        # are always rejected regardless of this setting.
        self._show_ps_messages: bool = bool(meta_cfg.get("show_ps_messages", True))

        self.frequency: Optional[float] = None
        self.band: Optional[str] = None
        self.station_name: Optional[str] = None
        self.slogan: Optional[str] = None
        self.artist: Optional[str] = None
        self.title: Optional[str] = None
        self.pty: Optional[str] = None
        self.pi_code: Optional[str] = None
        self.signal_bars: int = 0          # 0–5
        self.hd_available: bool = False    # IBOC sidebands detected on analog FM
        self.hd_locked: bool = False
        self.hd_channel: Optional[int] = None       # 1-based (1=HD1, 2=HD2, …)
        self.hd_channels_available: list = []       # e.g. [1, 2, 3]
        self.stereo: bool = False
        self.diag: Optional[dict] = None            # live DSP diagnostics from the pipeline
        self.has_art: bool = False
        self.art_version: int = 0
        self.apple_music_url: Optional[str] = None
        # Art provenance: "lot" (HD Radio LOT transfer) always supersedes
        # "itunes" (search-based guess).  iTunes art is never written over
        # LOT art; LOT art always overwrites anything.
        self._art_source: Optional[str] = None
        self._itunes_art_applied: Optional[str] = None
        # lifecycle state pushed to the frontend for status display
        self.state: str = "idle"           # idle | tuning | buffering | live
        # Incremented on every tune.  Metadata callbacks from demod/decoder
        # threads carry the generation they were created under; anything from
        # a previous generation is stale (the old pipeline's queued callbacks
        # can land after update_tune() cleared the fields) and is dropped.
        self.tune_generation: int = 0
        self._last_history_key: Optional[str] = None
        self._history_save_task: Optional[asyncio.Task] = None
        self._has_rtp: bool = False   # True once RT+ structured data received
        self._has_rt: bool = False    # True once any RadioText received
        # Confidence in the current artist/title.  Provisional sources
        # (partial RadioText, single-evidence PS assembly) display
        # immediately but never reach history or the iTunes lookup —
        # display fast, write once confident.
        self._track_confident: bool = False
        self._ps_asm = DynamicPsAssembler()
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
            "hd_available": self.hd_available,
            "hd_locked": self.hd_locked,
            "hd_channel": self.hd_channel,
            "hd_channels_available": self.hd_channels_available,
            "stereo": self.stereo,
            "diag": self.diag,
            "has_art": self.has_art,
            "art_version": self.art_version,
            "art_url": "/art/current.jpg" if self.has_art else None,
            "apple_music_url": self.apple_music_url,
            "ps_debug": self._ps_asm.debug(),
            "state": self.state,
        }

    def update_tune(self, frequency: float, band: str):
        # Cancel any pending debounced history/art save from the previous station
        if self._history_save_task and not self._history_save_task.done():
            self._history_save_task.cancel()
        self._history_save_task = None

        self.tune_generation += 1
        self.frequency = frequency
        self.band = band
        self.station_name = None
        self.slogan = None
        self.artist = None
        self.title = None
        self.pty = None
        self.pi_code = None
        self.hd_available = False
        self.hd_locked = False
        self.hd_channel = None
        self.hd_channels_available = []
        self.stereo = False
        self.has_art = False
        self.art_version = 0
        self.apple_music_url = None
        self._art_source = None
        self._itunes_art_applied = None
        self._has_rtp = False
        self._has_rt = False
        self._track_confident = False
        self._ps_asm.reset()
        self.state = "tuning"
        self._clear_art()

    def update_state(self, state: str):
        self.state = state

    def update_rds(self, ps: str = None, rt: str = None, pty: str = None, pi: str = None,
                   rtp_title: str = None, rtp_artist: str = None,
                   rt_partial: bool = False, gen: int = None):
        if gen is not None and gen != self.tune_generation:
            return   # stale callback from a previous station's pipeline
        changed = False

        if ps and ps.strip():
            res = self._ps_asm.feed(ps)
            if res.dynamic:
                # The station pages song text through PS too fast for it to
                # be a station name — clear any fragment we optimistically
                # displayed.
                if self.station_name is not None:
                    self.station_name = None
                    changed = True
            elif ps.strip() != self.station_name:
                # Static (or slow-paging) PS: show it immediately so normal
                # stations display their name without delay.
                self.station_name = ps.strip()
                changed = True

            # Reassembled text applies regardless of the display regime —
            # slow pagers (one page per 30-60 s) never register as
            # "dynamic" but still deliver song info once a full cycle of
            # evidence exists.  Only when the station provides no real
            # RadioText (RT and RT+ are more reliable).
            # Junk guard (always on): 2-page loops are usually fragments
            # of a longer message whose other pages were lost to decode
            # errors — they assemble into plausible-looking garbage
            # (' Tomatoes - Lucy' from "Planting Tomatoes - Lucy …").
            # Require >=3 pages of structure before showing anything.
            if (res.text and res.pages >= 3
                    and not self._has_rt and not self._has_rtp
                    # A provisional (single-evidence) loop containing
                    # multiple ' - ' separators almost certainly has a
                    # corrupt page spliced in ('ing - Stthe feelthg - St'
                    # observed live) — wait for the confident pass.
                    and (res.confident or res.text.count(" - ") <= 1)):
                artist, title = _parse_rt(res.text)
                song_shaped = (artist and title
                               and len(artist) >= 2 and len(title) >= 2)
                if song_shaped:
                    if ((artist, title) != (self.artist, self.title)
                            or res.confident != self._track_confident):
                        self.artist, self.title = artist, title
                        self._track_confident = res.confident
                        changed = True
                elif self._show_ps_messages:
                    # Non-song message (show promo, DJ schedule…) —
                    # display on the title line like RadioText promos.
                    # Collapse padding artifacts for display.
                    msg = " ".join(res.text.split())
                    if msg and (None, msg) != (self.artist, self.title):
                        self.artist, self.title = None, msg
                        self._track_confident = False   # not a song — never saved
                        changed = True

        # RT+ structured tags (IEC 62106 Annex A) take priority over heuristic
        # text splitting.  Once a station provides RT+ data, suppress the
        # fallback parser so stale RT text doesn't overwrite clean tag values.
        if rtp_title is not None and rtp_title != self.title:
            self.title = rtp_title
            self._has_rtp = True
            self._track_confident = True
            changed = True
        if rtp_artist is not None and rtp_artist != self.artist:
            self.artist = rtp_artist
            self._has_rtp = True
            self._track_confident = True
            changed = True

        # Heuristic RT parsing — only when no RT+ data has been received
        if rt and rt.strip() and not self._has_rtp:
            self._has_rt = True
            parsed = _parse_rt(rt.strip())
            confident = not rt_partial
            if (parsed != (self.artist, self.title)
                    or confident != self._track_confident):
                self.artist, self.title = parsed
                self._track_confident = confident
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
        hd_channel: Optional[int] = None,
        hd_channels_available: Optional[list] = None,
        gen: int = None,
    ):
        if gen is not None and gen != self.tune_generation:
            return   # stale callback from a previous station's decoder
        changed = False
        if station_name and station_name != self.station_name:
            self.station_name = station_name
            changed = True
        if slogan and slogan != self.slogan:
            self.slogan = slogan
            changed = True
        if artist and artist != self.artist:
            self.artist = artist
            self._track_confident = True   # nrsc5 tags are authoritative
            changed = True
        if title and title != self.title:
            self.title = title
            self._track_confident = True
            changed = True
        if pty and pty != self.pty:
            self.pty = pty
            changed = True
        if hd_locked is not None and hd_locked != self.hd_locked:
            self.hd_locked = hd_locked
            changed = True
        if hd_channel is not None and hd_channel != self.hd_channel:
            self.hd_channel = hd_channel
            changed = True
        if hd_channels_available is not None and hd_channels_available != self.hd_channels_available:
            self.hd_channels_available = hd_channels_available
            changed = True
        if art_path:
            try:
                self._write_art(art_path)
                self._art_source = "lot"
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

    def _write_art(self, src: str):
        """Install new cover art atomically (temp file + rename) so a client
        GET that races the update never receives a torn image."""
        os.makedirs(ART_DIR, exist_ok=True)
        tmp = ART_PATH + ".tmp"
        shutil.copy2(src, tmp)
        os.replace(tmp, ART_PATH)
        self.has_art = True
        self.art_version += 1

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
        for ws in list(self._websockets):
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
        for ws in list(self._websockets):
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
            await self._finalize_track()
        except asyncio.CancelledError:
            pass

    async def _finalize_track(self):
        """Runs once metadata has settled: iTunes lookup (art + canonical
        artist/title order), then history save with the corrected fields.

        Confidence gate: provisional data (partial RadioText, single-evidence
        PS assembly) is display-only — no lookup, no history row.  When the
        confident version arrives it re-triggers the debounce and lands here
        again.  Display fast, write once confident.

        Art precedence: LOT art (HD Radio's own image transfer) always
        supersedes iTunes art — the lookup is skipped entirely while LOT art
        is showing, and an iTunes result is discarded if LOT art landed
        during the network round-trip.
        """
        if not self._track_confident:
            return
        gen = self.tune_generation
        result = None
        if (self._itunes_enabled
                and self.artist and self.title
                and self._art_source != "lot"):
            from .art_lookup import fetch_itunes_art
            result = await fetch_itunes_art(self.artist, self.title)

        if result and gen == self.tune_generation:
            if self._order_correction and self._maybe_swap_artist_title(result):
                await self.broadcast()
            if (self._art_source != "lot"
                    and result["art_path"] != self._itunes_art_applied):
                try:
                    self._write_art(result["art_path"])
                    self._art_source = "itunes"
                    self._itunes_art_applied = result["art_path"]
                    self.apple_music_url = result.get("apple_music_url")
                    await self.broadcast()
                except OSError as exc:
                    logger.warning("Failed to copy iTunes art: %s", exc)

        await self.save_history()

    def _maybe_swap_artist_title(self, hit: dict) -> bool:
        """RDS has no defined artist/title order — stations transmit both
        'Artist - Title' and 'Title - Artist'.  The iTunes hit carries the
        canonical names: when our fields match crosswise but not straight,
        swap them."""
        artist_name = hit.get("artist_name")
        track_name  = hit.get("track_name")
        if not artist_name or not track_name:
            return False
        straight = (_rds_similar(self.artist, artist_name)
                    and _rds_similar(self.title, track_name))
        crossed  = (_rds_similar(self.artist, track_name)
                    and _rds_similar(self.title, artist_name))
        if crossed and not straight:
            self.artist, self.title = self.title, self.artist
            logger.info("Artist/title order corrected via iTunes: %s — %s",
                        self.artist, self.title)
            return True
        return False

    async def save_history(self):
        if not self.station_name or not self.artist or not self.title:
            return
        key = f"{self.artist}|{self.title}|{self.station_name}"
        if key == self._last_history_key:
            return
        from .db import get_db
        now = int(_time.time())
        try:
            db = await get_db()
            # Fetch recent candidates and match in Python using fuzzy comparison.
            # Exact SQL matching fails when RDS bit errors produce e.g.
            # "The Devil Is iinthe Details" vs "The Devil Is in the Details" —
            # both are the same song and should share one history row.
            async with db.execute(
                """SELECT id, artist, title FROM history
                   WHERE seen_at > ?
                   ORDER BY seen_at DESC LIMIT 20""",
                (now - 300,),
            ) as cur:
                candidates = await cur.fetchall()

            matched_id = None
            for row in candidates:
                if (_rds_similar(row["artist"], self.artist)
                        and _rds_similar(row["title"], self.title)):
                    matched_id = row["id"]
                    break

            if matched_id:
                # Update with the latest values — more RDS cycles means better
                # error correction, so the newest data is the most accurate.
                await db.execute(
                    """UPDATE history
                       SET station_name = ?, artist = ?, title = ?
                       WHERE id = ?""",
                    (self.station_name, self.artist, self.title, matched_id),
                )
            else:
                await db.execute(
                    """INSERT INTO history
                           (station_name, artist, title, pty, frequency, band, seen_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (self.station_name, self.artist, self.title, self.pty,
                     self.frequency, self.band, now),
                )
            self._last_history_key = key
            await db.commit()
        except Exception as e:
            logger.warning("Failed to save history: %s", e)


def _rds_similar(a: Optional[str], b: Optional[str], threshold: float = 0.82) -> bool:
    """True when two RDS text fields are similar enough to be the same content.

    Uses SequenceMatcher ratio to tolerate bit-flip corruption that replaces,
    inserts, or drops a handful of characters in the artist or title.
    Threshold 0.82 accepts e.g. 'Boards of Canada ⬛G' ≈ 'Boards of Canada'
    and 'The Devil Is iinthe Details' ≈ 'The Devil Is in the Details' while
    rejecting genuinely different strings.
    """
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= threshold


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
