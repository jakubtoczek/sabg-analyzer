"""Progress / ETA reporter for long full-res runs.

Work is counted in *tiles* (two passes per section). The reporter shows a single
**live line** that re-actualises in place for the current section, then moves on
to the next section. What it contains is configurable (per-section and/or total
progress, elapsed, ETA), and it can leave a persistent line behind at chosen
per-section percentages (``checkpoints``).

Rendering depends on where stdout goes:

* **Terminal (TTY):** the live line uses a carriage return (``\\r``); checkpoints
  print a normal newline so they stay.
* **Piped (the GUI):** the live line is prefixed with a ``\\x01`` sentinel and is
  newline-terminated so it streams; the GUI shows it on a dedicated status line
  instead of appending. Checkpoints / section results have no sentinel and are
  logged normally.

The ETA uses an exponential moving average of the recent tile rate, so it adapts
after the slow library-warm-up instead of being dragged down by it.
"""

from __future__ import annotations

import math
import sys
import time

LIVE = "\x01"        # prefix marking a self-replacing live line (piped mode)


def scene_tile_count(w: int, h: int, tile_size: int) -> int:
    """Number of tiles a scene of size ``w x h`` is split into."""
    return math.ceil(w / tile_size) * math.ceil(h / tile_size)


def _fmt(seconds: float) -> str:
    s = int(max(seconds, 0))
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m"


class _DefaultParams:
    section = total = elapsed = eta = True
    checkpoints: list[float] = []


class Progress:
    """Overall progress across every tile of every scene in a run."""

    def __init__(self, total: int, enabled: bool = True, label: str = "analyze",
                 params=None):
        self.total = max(int(total), 1)
        self.p = params or _DefaultParams()
        self.n = 0
        self.t0 = time.time()
        self.enabled = enabled
        self.label = label
        self._last = 0.0
        self._ema = 0.0                 # tiles/sec, exponential moving average
        self._emit_t = self.t0
        self._emit_n = 0
        self._tty = bool(getattr(sys.stdout, "isatty", lambda: False)())
        self._interval = 0.5 if self._tty else 3.0
        self._live_open = False         # a \r live line is currently on screen (TTY)
        # per-section state
        self.sec_name = ""
        self.sec_total = 1
        self.sec_n = 0
        self._cp_done: set[float] = set()
        self._tqdm = None
        if enabled and self._tty and not _wants_custom(self.p):
            try:
                from tqdm import tqdm
                self._tqdm = tqdm(total=self.total, unit="tile", desc=label)
            except Exception:
                self._tqdm = None

    # -- section boundaries ------------------------------------------------
    def start_section(self, name: str, section_total: int) -> None:
        self.sec_name = name
        self.sec_total = max(int(section_total), 1)
        self.sec_n = 0
        self._cp_done = set()

    # -- ticking -----------------------------------------------------------
    def update(self, k: int = 1) -> None:
        self.n += k
        self.sec_n += k
        if not self.enabled:
            return
        if self._tqdm is not None:
            self._tqdm.update(k)
            return
        self._maybe_checkpoint()
        now = time.time()
        done = self.n >= self.total
        if now - self._last < self._interval and not done:
            return
        self._last = now
        self._emit_live(now)

    # -- rendering ---------------------------------------------------------
    def _rate(self, now: float) -> float:
        dt = now - self._emit_t
        if dt > 0:
            inst = (self.n - self._emit_n) / dt
            self._ema = inst if self._ema == 0.0 else 0.3 * inst + 0.7 * self._ema
        self._emit_t, self._emit_n = now, self.n
        return self._ema

    def _message(self, now: float) -> str:
        parts: list[str] = []
        if self.p.section:
            sp = 100.0 * self.sec_n / self.sec_total
            parts.append(f"{self.sec_name} {sp:5.1f}% {self.sec_n}/{self.sec_total}")
        if self.p.total:
            tp = 100.0 * self.n / self.total
            parts.append(f"total {tp:5.1f}% {self.n}/{self.total} tiles")
        if self.p.elapsed:
            parts.append(f"elapsed {_fmt(now - self.t0)}")
        if self.p.eta:
            rate = self._ema if self._ema > 0 else self._rate(now)
            eta = (self.total - self.n) / rate if rate > 0 else 0.0
            parts.append(f"ETA {_fmt(eta)}")
        return "  " + " | ".join(parts)

    def _emit_live(self, now: float) -> None:
        self._rate(now)                 # refresh EMA even if eta is hidden
        msg = self._message(now)
        if self._tty:
            sys.stdout.write("\r" + msg + "    ")
            self._live_open = True
        else:
            sys.stdout.write(LIVE + msg + "\n")
        sys.stdout.flush()

    def _emit_persistent(self, msg: str) -> None:
        """A line that stays (checkpoints)."""
        if self._tty:
            if self._live_open:
                sys.stdout.write("\n")  # close the live line so the mark stays
                self._live_open = False
            sys.stdout.write(msg + "\n")
        else:
            sys.stdout.write(msg + "\n")   # no sentinel -> GUI logs it
        sys.stdout.flush()

    def _maybe_checkpoint(self) -> None:
        cps = getattr(self.p, "checkpoints", None) or []
        if not cps:
            return
        sp = 100.0 * self.sec_n / self.sec_total
        for cp in cps:
            if cp not in self._cp_done and sp >= cp:
                self._cp_done.add(cp)
                self._emit_persistent(
                    f"  [{self.sec_name} {cp:g}%] {self.sec_n}/{self.sec_total} tiles "
                    f"| elapsed {_fmt(time.time() - self.t0)}")

    def close(self) -> None:
        if self._tqdm is not None:
            self._tqdm.close()
        elif self.enabled and self._tty and self._live_open:
            sys.stdout.write("\n")
            sys.stdout.flush()


def _wants_custom(p) -> bool:
    """tqdm can't express per-section/checkpoints, so skip it when those are on."""
    return bool(getattr(p, "checkpoints", None)) or not getattr(p, "total", True)
