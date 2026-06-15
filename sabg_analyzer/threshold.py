"""Robust thresholding of the SABG score over tissue pixels.

The signal is sparse (often <1 % of tissue), so the score histogram is strongly
unimodal-with-a-tail. Plain Otsu (which assumes two comparable modes) tends to
cut into the tissue bulk; the **triangle** method handles the skewed shape far
better and is the default. A score histogram is accumulated across all full-res
tiles (pass 1) and the threshold is derived from it once.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from skimage.filters import threshold_otsu, threshold_triangle


@dataclass
class ThresholdParams:
    method: str = "triangle"   # triangle | otsu | percentile | fixed
    percentile: float = 99.0
    value: float | None = None
    min_score: float = 0.0
    scale: float = 0.70        # multiply the auto threshold (<1 = more sensitive).
    #   0.70 = session-16 tuning default (cfg07; was 0.825).
    #   With hysteresis on (detection.hysteresis) this is the SEED/high threshold:
    #   keep it fairly strict (~0.8-0.9) and let the hysteresis grow recover faint teal.
    from_overview: bool = False  # derive threshold from the in-memory overview
    #   instead of streaming full-res pass 1 (~2x faster; threshold may shift
    #   slightly because the sparse tail is thinner when downsampled).


@dataclass
class ScoreHistogram:
    """Streaming histogram of tissue-pixel scores for one scene."""

    lo: float
    hi: float
    bins: int = 4096
    counts: np.ndarray = field(default=None)  # type: ignore[assignment]

    def __post_init__(self):
        if self.counts is None:
            self.counts = np.zeros(self.bins, dtype=np.int64)
        self._edges = np.linspace(self.lo, self.hi, self.bins + 1)

    def add(self, values: np.ndarray) -> None:
        if values.size:
            c, _ = np.histogram(values, bins=self._edges)
            self.counts += c

    @property
    def centers(self) -> np.ndarray:
        return 0.5 * (self._edges[:-1] + self._edges[1:])

    def total(self) -> int:
        return int(self.counts.sum())


def compute_threshold(hist: ScoreHistogram, p: ThresholdParams) -> float:
    """Derive a scalar threshold from an accumulated histogram."""
    if p.method == "fixed":
        if p.value is None:
            raise ValueError("threshold.method='fixed' requires threshold.value")
        return max(p.value, p.min_score)   # manual value: not scaled

    centers = hist.centers
    counts = hist.counts
    if counts.sum() == 0:
        return p.min_score

    if p.method == "percentile":
        cdf = np.cumsum(counts) / counts.sum()
        idx = int(np.searchsorted(cdf, p.percentile / 100.0))
        idx = min(idx, len(centers) - 1)
        thr = float(centers[idx])
    elif p.method in ("triangle", "otsu"):
        # Reconstruct a representative sample from the histogram for the
        # skimage estimators (they take an image/array, not a histogram).
        # Cap the sample size for speed; the shape is preserved.
        weights = counts.astype(np.float64)
        total = weights.sum()
        target = min(int(total), 2_000_000)
        reps = np.maximum((weights / total * target).round().astype(np.int64), 0)
        sample = np.repeat(centers, reps)
        if sample.size < 2:
            return p.min_score
        fn = threshold_triangle if p.method == "triangle" else threshold_otsu
        thr = float(fn(sample))
    else:
        raise ValueError(f"unknown threshold method: {p.method!r}")

    return max(thr * p.scale, p.min_score)
