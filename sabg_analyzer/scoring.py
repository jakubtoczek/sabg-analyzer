"""SABG scoring.

Two interchangeable per-pixel scores where a higher value = more SABG (teal):

* **opponent** -- ``mean(G, B) - R`` scaled to roughly [-1, 1]. The teal X-Gal
  product absorbs red, so teal pixels have low R relative to G/B. Fast and
  transparent.
* **deconvolution** -- colour unmixing (Ruifrok/Johnston). The teal stain is
  separated from the tissue counterstain in optical-density space; the score is
  the SABG channel amount.

Both are always computed; ``detection.primary`` in the config decides which one
drives the reported %SABG.
"""

from __future__ import annotations

import numpy as np

# skimage's separate_stains uses this for the OD log; we replicate it so we can
# compute only the SABG channel (a single mat-vec) instead of the full 3-stain
# separation -- identical numbers, less work.
_LOG_ADJUST = np.log(1e-6)

# Default stain vectors in RGB optical-density space (rows are normalised below).
#   SABG/teal  -> red is absorbed  => R-dominant OD.
#   counter    -> khaki/yellow tissue absorbs blue => B-dominant OD.
DEFAULT_SABG_OD = np.array([0.65, 0.45, 0.40])
DEFAULT_COUNTER_OD = np.array([0.35, 0.40, 0.75])


def opponent_score(rgb: np.ndarray) -> np.ndarray:
    """``mean(G, B) - R`` in [-1, 1]; higher = more teal."""
    a = rgb.astype(np.float32) / 255.0
    return (a[..., 1] + a[..., 2]) * 0.5 - a[..., 0]


def _normalise(v: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


def build_stain_matrix(
    sabg_od: np.ndarray | None = None,
    counter_od: np.ndarray | None = None,
) -> np.ndarray:
    """Return a 3x3 stain matrix (rows: SABG, counterstain, residual).

    The residual vector is the orthogonal complement so the matrix is invertible.
    """
    s = _normalise(np.asarray(sabg_od if sabg_od is not None else DEFAULT_SABG_OD, float))
    c = _normalise(np.asarray(counter_od if counter_od is not None else DEFAULT_COUNTER_OD, float))
    r = np.cross(s, c)
    if np.linalg.norm(r) < 1e-6:        # degenerate -> nudge
        r = np.array([0.0, 0.0, 1.0])
    r = _normalise(r)
    return np.vstack([s, c, r])


def conv_matrix(stain_matrix: np.ndarray) -> np.ndarray:
    """Inverse stain matrix, as required by ``skimage.color.separate_stains``."""
    return np.linalg.inv(stain_matrix)


def deconvolution_score(rgb: np.ndarray, conv: np.ndarray) -> np.ndarray:
    """SABG-channel amount from colour deconvolution; higher = more SABG.

    Equivalent to ``skimage.color.separate_stains(rgb, conv)[..., 0]`` but only
    the first stain channel is evaluated (one mat-vec over the last axis), which
    is markedly faster on full-res tiles.
    """
    a = rgb.astype(np.float64) / 255.0
    np.maximum(a, 1e-6, out=a)
    od = np.log(a) / _LOG_ADJUST          # optical density, like separate_stains
    score = od @ conv[:, 0]               # == (od @ conv)[..., 0]
    np.maximum(score, 0.0, out=score)     # separate_stains clamps stains at 0
    return score.astype(np.float32)


def estimate_sabg_od(
    rgb: np.ndarray, tissue: np.ndarray, top_frac: float = 0.02
) -> np.ndarray | None:
    """Estimate the SABG OD direction from the most teal tissue pixels.

    Picks the top ``top_frac`` of tissue pixels by opponent score and returns
    their mean optical-density vector (normalised). Returns None if too few.
    """
    if tissue.sum() < 1000:
        return None
    opp = opponent_score(rgb)
    vals = opp[tissue]
    cut = np.quantile(vals, 1.0 - top_frac)
    sel = tissue & (opp >= cut)
    if sel.sum() < 200:
        return None
    od = -np.log10(np.maximum(rgb[sel].astype(np.float32) / 255.0, 1e-6))
    v = od.mean(axis=0)
    return _normalise(v)
