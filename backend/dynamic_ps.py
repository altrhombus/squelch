"""
Dynamic-PS reassembly.

The RDS PS field is specified as a static 8-character station name, but many
stations "page" their now-playing text through it instead, e.g.:

    "Daughter" → " of Empi" → "re - Hum" → "bird    "  (repeating)
      reassembles to  "Daughter of Empire - Humbird"

DynamicPsAssembler watches the stream of PS values and decides which regime
the station is in:

  static  — PS unchanged for a while: it's a real station name.
  dynamic — PS keeps changing: reconstruct the paged message.

Reconstruction uses a successor graph rather than whole-cycle matching:
every observed transition page_a → page_b adds evidence to an edge, and the
message is recovered by walking the strongest loop in the graph.  This
tolerates heavy page loss — on marginal signals whole pages vanish (a page
only survives decoding if all four of its RDS segments arrive clean and
in order), so complete cycles may *never* be observed, but the true edges
still accumulate more evidence than the loss-induced skip edges.

Corrupted pages appear as rarely-reinforced detours the walk ignores; the
space padding in raw pages carries word boundaries, so feed() must receive
the *unstripped* 8-character PS.
"""

import time
from typing import Optional


class PsResult:
    """dynamic: PS is currently paging (not a station name).
    text: newly reconstructed message, or None.
    pages: number of pages the text was assembled from (evidence size)."""

    __slots__ = ("dynamic", "text", "pages")

    def __init__(self, dynamic: bool, text: Optional[str] = None, pages: int = 0):
        self.dynamic = dynamic
        self.text = text
        self.pages = pages


class DynamicPsAssembler:
    DYNAMIC_CHANGES = 3      # distinct PS changes inside WINDOW_SECS → dynamic
    WINDOW_SECS     = 20.0
    STATIC_SECS     = 15.0   # unchanged this long → back to static
    MAX_WALK        = 24     # bound on message length in pages
    EDGE_CONFIDENT  = 2      # evidence needed on every edge for a firm emit
    PRUNE_SECS      = 60.0   # forget edges not reinforced within this window
    NOVEL_RESET     = 3      # consecutive never-seen pages → message changed
    DEBUG_PAGES     = 16     # raw pages kept for the diagnostics feed

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self.reset()

    def reset(self):
        self._last_ps: Optional[str] = None
        self._last_change: float = 0.0
        self._change_times: list[float] = []
        self._dynamic = False
        self._prev_page: Optional[str] = None
        # successor graph: page_a -> page_b -> [count, last_seen]
        self._edges: dict[str, dict[str, list]] = {}
        self._emitted: Optional[str] = None
        self._provisioned = False
        self._novel_run = 0
        self._page_log: list[str] = []

    # ------------------------------------------------------------------

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
                self._dynamic = True
                self._prev_page = ps
            return PsResult(False)

        # Dynamic mode: a long-stable PS means the station went back to a name
        if now - self._last_change > self.STATIC_SECS:
            self.reset()
            self._last_ps = ps
            self._last_change = now
            return PsResult(False)

        text, pages = None, 0
        if changed:
            self._observe(ps, now)
            extracted = self._extract()
            if extracted:
                text, pages = extracted
        return PsResult(True, text, pages)

    # ------------------------------------------------------------------

    def _observe(self, ps: str, now: float):
        known = ps in self._edges or any(
            ps in nbrs for nbrs in self._edges.values()
        )

        if self._prev_page is not None:
            edge = self._edges.setdefault(self._prev_page, {}).setdefault(ps, [0, now])
            edge[0] += 1
            edge[1] = now
        self._prev_page = ps

        # Message-change detection: a run of never-seen pages after we've
        # already emitted means the station moved on wholesale (new song).
        # Drop the stale evidence and seed the graph with the novel chain so
        # the new message can emit provisionally within ~one cycle.
        if known:
            self._novel_run = 0
        else:
            self._novel_run += 1
            if self._emitted is not None and self._novel_run >= self.NOVEL_RESET:
                seed = self._page_log[-self._novel_run:]
                self._edges = {}
                for a, b in zip(seed, seed[1:]):
                    self._edges.setdefault(a, {})[b] = [1, now]
                self._provisioned = False
                self._novel_run = 0

        # Age out edges that stopped being reinforced (old message remnants)
        for a in list(self._edges):
            nbrs = self._edges[a]
            for b in list(nbrs):
                if now - nbrs[b][1] > self.PRUNE_SECS:
                    del nbrs[b]
            if not nbrs:
                del self._edges[a]

    def _extract(self) -> Optional[tuple]:
        """Walk the strongest successor loop and emit (text, n_pages) if it
        clears the evidence bar (or once provisionally, for responsiveness)."""
        if not self._edges:
            return None

        start = max(
            self._edges,
            key=lambda a: sum(c for c, _ in self._edges[a].values()),
        )
        walk = [start]
        cur = start
        cycle = None
        for _ in range(self.MAX_WALK):
            nbrs = self._edges.get(cur)
            if not nbrs:
                return None
            # strongest successor; ties go to the most recently seen
            nxt = max(nbrs, key=lambda b: (nbrs[b][0], nbrs[b][1]))
            if nxt in walk:
                cycle = walk[walk.index(nxt):]
                break
            walk.append(nxt)
            cur = nxt
        if not cycle:
            return None

        counts = [
            self._edges[cycle[i]][cycle[(i + 1) % len(cycle)]][0]
            for i in range(len(cycle))
        ]
        text = self._assemble(cycle)
        if not text or text == self._emitted:
            return None

        if len(cycle) >= 2 and min(counts) >= self.EDGE_CONFIDENT:
            self._emitted = text
            self._provisioned = True
            return text, len(cycle)
        # Provisional: show the first plausible loop right away; the
        # evidence-backed walk corrects it within a few cycles if a page was
        # corrupted.  Two-page loops are usually fragments of a longer
        # message whose other pages were lost — not trusted provisionally.
        if not self._provisioned and len(cycle) >= 3:
            self._emitted = text
            self._provisioned = True
            return text, len(cycle)
        return None

    @staticmethod
    def _assemble(pages: list[str]) -> str:
        # The walk starts at an arbitrary point in the loop, so the pages may
        # be rotated.  The message tail is the space-padded page (messages
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
            "nodes": len(self._edges),
            "provisioned": self._provisioned,
            "emitted": self._emitted,
        }
