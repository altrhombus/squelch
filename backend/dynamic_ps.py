"""
Dynamic-PS reassembly.

The RDS PS field is specified as a static 8-character station name, but many
stations "page" their now-playing text through it instead, e.g.:

    "Daughter" → " of Empi" → "re - Hum" → "bird    "  (repeating)
      reassembles to  "Daughter of Empire - Humbird"

DynamicPsAssembler watches the stream of PS values and decides which regime
the station is in:

  static  — PS unchanged for a while: it's a real station name.
  dynamic — PS keeps changing: collect the raw 8-char pages (spaces intact —
            they carry word boundaries), detect the cycle by the first page
            repeating, and majority-vote each page position across cycles to
            reject the bit-flipped pages RDS regularly produces.

feed() must receive the *unstripped* 8-character PS.
"""

import time
from collections import Counter
from difflib import SequenceMatcher
from typing import Optional


class PsResult:
    """dynamic: PS is currently paging (not a station name).
    text: newly completed reassembled message, or None."""

    __slots__ = ("dynamic", "text")

    def __init__(self, dynamic: bool, text: Optional[str] = None):
        self.dynamic = dynamic
        self.text = text


class DynamicPsAssembler:
    DYNAMIC_CHANGES = 3      # distinct PS changes inside WINDOW_SECS → dynamic
    WINDOW_SECS     = 20.0
    STATIC_SECS     = 15.0   # unchanged this long → back to static
    MAX_PAGES       = 16     # give up on a cycle that never repeats its anchor
    MAX_CYCLES      = 6      # voting history
    VOTES           = 2      # observations required per page position

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.reset()

    DEBUG_PAGES = 16   # raw pages kept for the diagnostics feed

    def reset(self):
        self._last_ps: Optional[str] = None
        self._last_change: float = 0.0
        self._change_times: list[float] = []
        self._dynamic = False
        self._anchor: Optional[str] = None
        self._current: list[str] = []
        self._cycles: list[list[str]] = []
        self._emitted: Optional[str] = None
        self._provisioned = False
        self._page_log: list[str] = []

    def feed(self, ps: str) -> PsResult:
        now = self._clock()
        changed = ps != self._last_ps
        if changed:
            self._last_ps = ps
            self._last_change = now
            self._change_times.append(now)
            self._change_times = [
                t for t in self._change_times if now - t <= self.WINDOW_SECS
            ]
            self._page_log.append(ps)
            del self._page_log[:-self.DEBUG_PAGES]

        if not self._dynamic:
            if len(self._change_times) >= self.DYNAMIC_CHANGES:
                # PS is cycling — switch to dynamic and start collecting
                self._dynamic = True
                self._anchor = ps
                self._current = [ps]
            return PsResult(False)

        # Dynamic mode: a long-stable PS means the station went back to a name
        if now - self._last_change > self.STATIC_SECS:
            self.reset()
            self._last_ps = ps
            self._last_change = now
            return PsResult(False)

        text = None
        if changed:
            # Fuzzy anchor matching tolerates a bit-flipped anchor page, but
            # two legitimately different pages can also score similar (seen:
            # 'Back In ' vs 'Black   ' at exactly 0.75) — so a fuzzy match
            # needs a longer cycle to count, and only an exact close is
            # trusted for provisional emission.
            exact = ps == self._anchor
            min_len = 2 if exact else 3
            if len(self._current) >= min_len and (exact or self._similar(ps, self._anchor)):
                self._cycles.append(self._current)
                self._cycles = self._cycles[-self.MAX_CYCLES:]
                self._current = [ps]
                text = self._vote()
                if text is None and exact:
                    text = self._provisional(self._cycles[-1])
            else:
                # Not the anchor — but if it exactly matches a page already
                # collected, the anchor is stale (the message changed under
                # us): re-anchor there instead of waiting MAX_PAGES to give up.
                match = next((i for i, p in enumerate(self._current[1:], 1)
                              if ps == p), None)
                if match is not None:
                    cycle = self._current[match:]
                    self._anchor = self._current[match]
                    self._cycles = [cycle] if len(cycle) >= 2 else []
                    self._current = [ps]
                    self._provisioned = False
                    if len(cycle) >= 2:
                        text = self._provisional(cycle)
                else:
                    self._current.append(ps)
                    if len(self._current) > self.MAX_PAGES:
                        # Anchor never repeated and nothing re-anchored —
                        # likely a corrupted anchor.  Start over from here.
                        self._cycles.clear()
                        self._provisioned = False
                        self._anchor = ps
                        self._current = [ps]

        return PsResult(True, text)

    def _provisional(self, cycle: list[str]) -> Optional[str]:
        """Emit the first completed cycle immediately rather than waiting for
        the vote — showing something in ~1 cycle (~10 s) beats a clean answer
        in ~60 s.  Voting corrects the text a few cycles later if one of the
        provisional pages was corrupted; downstream history dedup is fuzzy
        for exactly this kind of near-duplicate correction."""
        if self._provisioned:
            return None
        candidate = self._assemble(cycle)
        if not candidate or candidate == self._emitted:
            return None
        self._provisioned = True
        self._emitted = candidate
        return candidate

    def _vote(self) -> Optional[str]:
        """Majority-vote pages across collected cycles; emit when every page
        position has been observed identically at least VOTES times."""
        lengths = Counter(len(c) for c in self._cycles)
        modal_len, _ = lengths.most_common(1)[0]
        candidates = [c for c in self._cycles if len(c) == modal_len]
        if len(candidates) < self.VOTES:
            return None

        pages = []
        for i in range(modal_len):
            page, count = Counter(c[i] for c in candidates).most_common(1)[0]
            if count < self.VOTES:
                return None
            pages.append(page)

        text = self._assemble(pages)
        if not text or text == self._emitted:
            return None
        self._emitted = text
        return text

    @staticmethod
    def _assemble(pages: list[str]) -> str:
        # Collection starts at an arbitrary point in the cycle, so the pages
        # may be rotated.  The message tail is the space-padded page (messages
        # are rarely exact multiples of 8 chars); rotate it to the end.  Ties
        # go to the page with the most padding — a mid-message page ends with
        # at most one space (a word boundary), the true tail usually more.
        padding = [len(p) - len(p.rstrip(" ")) for p in pages]
        if max(padding) > 0:
            tail = max(range(len(pages)), key=padding.__getitem__)
            pages = pages[tail + 1:] + pages[:tail + 1]
        return "".join(pages).rstrip()

    def debug(self) -> dict:
        """Assembler state for the diagnostics feed — raw pages are the only
        way to see what a station is actually paging over the air."""
        return {
            "dynamic": self._dynamic,
            "pages": list(self._page_log),
            "anchor": self._anchor,
            "current_len": len(self._current),
            "cycle_lens": [len(c) for c in self._cycles],
            "emitted": self._emitted,
        }

    @staticmethod
    def _similar(a: str, b: str) -> bool:
        if a == b:
            return True
        return SequenceMatcher(None, a, b).ratio() >= 0.75
