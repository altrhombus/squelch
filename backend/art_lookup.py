"""
iTunes Search API cover art lookup.

Fetches 600×600 JPEG artwork for a given artist+title and caches the result
on disk. The cache is keyed on (artist, title) and persists for the process
lifetime — identical songs won't hit the network a second time.

All network I/O runs in a thread pool so it never blocks the event loop.
"""

import asyncio
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)

_ART_DIR = "/tmp/sdr-art"

# In-memory result cache: (artist, title) -> local JPEG path, or None if no art found.
# None entries are cached too so a "not found" result doesn't trigger a repeat lookup.
_cache: dict[tuple[str, str], Optional[str]] = {}


async def fetch_itunes_art(artist: str, title: str) -> Optional[str]:
    """
    Return a local JPEG path for the given artist/title, or None.
    First call hits the network; subsequent calls with the same key are instant.
    """
    key = (artist, title)
    if key in _cache:
        return _cache[key]

    path = await asyncio.to_thread(_lookup_blocking, artist, title)
    _cache[key] = path
    return path


def _lookup_blocking(artist: str, title: str) -> Optional[str]:
    """Synchronous iTunes lookup + download — safe to run in a thread pool."""
    query = f"{artist} {title}"
    params = urllib.parse.urlencode({
        "term":   query,
        "entity": "song",
        "media":  "music",
        "limit":  "5",
    })
    api_url = f"https://itunes.apple.com/search?{params}"

    try:
        with urllib.request.urlopen(api_url, timeout=6) as resp:
            data = json.loads(resp.read())
    except Exception as exc:
        logger.debug("iTunes lookup failed for %r: %s", query, exc)
        return None

    results = data.get("results") or []
    if not results:
        return None

    art_url = results[0].get("artworkUrl100", "")
    if not art_url:
        return None

    # Upgrade from the 100px thumbnail to 600px high-res
    art_url = art_url.replace("100x100bb.", "600x600bb.")

    # Download to a stable per-song cache file so we only fetch each image once
    safe = "".join(c if c.isalnum() else "_" for c in query)[:80]
    os.makedirs(_ART_DIR, exist_ok=True)
    dest = os.path.join(_ART_DIR, f"itunes_{safe}.jpg")
    try:
        with urllib.request.urlopen(art_url, timeout=8) as img_resp:
            with open(dest, "wb") as f:
                f.write(img_resp.read())
        logger.debug("iTunes art cached for %r → %s", query, dest)
        return dest
    except Exception as exc:
        logger.debug("iTunes art download failed for %r: %s", art_url, exc)
        return None
