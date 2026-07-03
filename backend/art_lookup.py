"""
iTunes Search API cover art lookup.

Fetches 600×600 JPEG artwork for a given artist+title and caches the result
on disk. Also captures the Apple Music trackViewUrl so the frontend can link
directly to the song.

The cache is keyed on (artist, title) and persists for the process lifetime —
identical songs won't hit the network a second time.  A None entry means the
lookup already ran and found nothing, so it won't retry.

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

from .metadata import ART_DIR as _ART_DIR

logger = logging.getLogger(__name__)

# In-memory cache: (artist, title) -> {"art_path": str, "apple_music_url": str} | None
_cache: dict[tuple[str, str], Optional[dict]] = {}


async def fetch_itunes_art(artist: str, title: str) -> Optional[dict]:
    """
    Return {"art_path": local_jpeg_path, "apple_music_url": url} for the given
    artist/title, or None if no result was found.
    Subsequent calls with the same key are served from the in-memory cache.
    """
    key = (artist, title)
    if key in _cache:
        return _cache[key]

    result = await asyncio.to_thread(_lookup_blocking, artist, title)
    _cache[key] = result
    return result


def _lookup_blocking(artist: str, title: str) -> Optional[dict]:
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

    hit = results[0]
    art_url = hit.get("artworkUrl100", "")
    if not art_url:
        return None

    # Upgrade from the 100px thumbnail to 600px hi-res
    art_url = art_url.replace("100x100bb.", "600x600bb.")
    apple_music_url = hit.get("trackViewUrl") or hit.get("collectionViewUrl")

    # Download to a stable per-song cache file so we only fetch each image once
    safe = "".join(c if c.isalnum() else "_" for c in query)[:80]
    os.makedirs(_ART_DIR, exist_ok=True)
    dest = os.path.join(_ART_DIR, f"itunes_{safe}.jpg")
    try:
        with urllib.request.urlopen(art_url, timeout=8) as img_resp:
            with open(dest, "wb") as f:
                f.write(img_resp.read())
        logger.debug("iTunes art cached for %r → %s", query, dest)
        return {"art_path": dest, "apple_music_url": apple_music_url}
    except Exception as exc:
        logger.debug("iTunes art download failed for %r: %s", art_url, exc)
        return None
