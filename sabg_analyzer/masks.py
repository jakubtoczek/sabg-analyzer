"""Shared mask math for SABG detection.

These are the per-tile array operations factored out of `pipeline.analyze_scene`
so the interactive preview (`preview.py`) computes the SAME masks as the batch
analysis and cannot drift from it. Everything here is stateless and works on any
RGB ROI; no tiling or overview/HD-canvas machinery lives here.

Two entry points, in pipeline order:

  compute_region_masks(rgb, tcfg, cfg, *, region, region_c, fold_band)
      -> (t, art, fold, opp)   # countable tissue, artifact, fold band, opponent score

  detect_sabg(rgb, t, opp, cfg, conv, thr, thr_s, um_per_px, *, fold, keep_here)
      -> {"sabg", "edge_removed", "hot"}   # seed -> hysteresis grow -> fold-drop
                                           # -> edge filter -> expand

`keep_here` selects the hysteresis flavour: the pipeline passes a tile-shaped
resample of its HD-canvas `hd_keep` (the connectivity decided once on the maps
canvas); the preview passes None, so the grow is computed directly on the ROI via
`_grow_connected` (more accurate, affordable on a single cap-bounded ROI).
"""

from __future__ import annotations

import cv2
import numpy as np

from . import edge as edge_filter, scoring
from .config import Config
from .tissue import artifact_mask, tissue_mask


def _primary_secondary(deconv: np.ndarray, opp: np.ndarray, primary: str):
    """Return (primary_score, secondary_score) for the configured primary."""
    return (opp, deconv) if primary == "opponent" else (deconv, opp)


def _grow_connected(seed: np.ndarray, cand: np.ndarray) -> np.ndarray:
    """Hysteresis reconstruction: keep the connected components of *cand* that
    contain at least one *seed* pixel (8-connectivity). Both are boolean masks of
    the same shape; *seed* must be a subset of *cand* for the result to include it.
    """
    if not cand.any() or not seed.any():
        return seed & cand
    n, lab = cv2.connectedComponents(cand.astype(np.uint8), connectivity=8)
    hit = np.unique(lab[seed & cand])
    hit = hit[hit != 0]                       # 0 is the background label
    if hit.size == 0:
        return np.zeros_like(cand)
    return np.isin(lab, hit)


def compute_region_masks(rgb: np.ndarray, tcfg, cfg: Config, *,
                         region: np.ndarray, region_c: np.ndarray,
                         fold_band: np.ndarray | None,
                         opp: np.ndarray | None = None):
    """Per-pixel tissue/artifact/fold masks for one RGB ROI or tile.

    *region* is the coarse (non-eroded) tissue blob; *region_c* is the eroded
    counting region (already minus the fold band when ``fold.exclude_from_tissue``);
    *fold_band* is the linear-fold band (or None when folds are disabled). All three
    must already be at the ROI/tile pixel grid. Returns ``(t, art, fold, opp)``:

      t    countable tissue = region_c & raw & ~art
      art  dark fold/debris (within region & raw)
      fold linear-fold band (within region & raw, disjoint from art)
      opp  opponent score (returned so callers can reuse it)
    """
    raw = tissue_mask(rgb, tcfg)               # fine gap/white-glass within the region
    if opp is None:
        opp = scoring.opponent_score(rgb)
    if cfg.artifact.enabled:
        art = artifact_mask(rgb, opp, cfg.artifact) & region & raw
    else:
        art = np.zeros(raw.shape, bool)
    if cfg.fold.enabled and fold_band is not None:
        fold = fold_band & region & raw & ~art
    else:
        fold = np.zeros(raw.shape, bool)
    t = region_c & raw & ~art
    return t, art, fold, opp


def detect_sabg(rgb: np.ndarray, t: np.ndarray, opp: np.ndarray, cfg: Config,
                conv: np.ndarray, thr: float, thr_s: float | None,
                um_per_px: float | None, *,
                fold: np.ndarray | None = None,
                keep_here: np.ndarray | None = None) -> dict:
    """SABG+ detection for one RGB ROI/tile: seed -> hysteresis grow -> fold-drop
    -> edge-shadow filter -> expand. Mirrors `pipeline.analyze_scene` pass 2.

    Args:
        t: countable tissue mask (from compute_region_masks).
        opp: opponent score (from compute_region_masks).
        thr, thr_s: primary (seed) and optional secondary thresholds.
        fold: fold band; only used to drop fold positives when
            ``fold.enabled and not fold.exclude_from_tissue``.
        keep_here: tile-shaped resample of the HD-canvas `hd_keep` (pipeline path).
            When None and hysteresis is on, the grow is computed directly on this
            array via `_grow_connected` (preview path).
    Returns:
        {"sabg": pos, "edge_removed": removed, "hot": hot, "sabg_candidate":
        candidate}. All are boolean masks; `sabg_candidate` is the pre-rejection
        positive set (before fold-drop / edge removal).
    """
    primary = cfg.detection.primary
    agree = cfg.detection.require_agreement

    deconv = scoring.deconvolution_score(rgb, conv)
    p_s, s_s = _primary_secondary(deconv, opp, primary)
    hot = (p_s >= thr)                         # seed / high threshold
    if agree and thr_s is not None:
        hot &= (s_s >= thr_s)

    pos = t & hot
    if cfg.detection.hysteresis:               # grow seeds into connected faint teal
        low_thr = thr * cfg.detection.hyst_low_scale
        cand = t & (p_s >= low_thr) & (opp >= cfg.detection.hyst_teal_min)
        if keep_here is not None:              # pipeline: HD-canvas connectivity
            if cand.any():
                pos = pos | (cand & keep_here)
        else:                                  # preview: real grow on this ROI
            pos = pos | _grow_connected(pos, cand)

    # Pre-rejection candidate (B1 audit layer): the grown positives BEFORE the
    # fold-drop / edge filter strip anything. The later expand step can grow the
    # final mask beyond this, so `candidate` is not a strict superset of `sabg`;
    # `candidate & ~sabg` is exactly what the fold/edge stages rejected.
    candidate = pos.copy()

    if cfg.fold.enabled and not cfg.fold.exclude_from_tissue and fold is not None:
        pos = pos & ~fold                      # numerator-only: drop fold positives

    if cfg.edge.enabled:                       # drop thin edge-shadow rims
        pos, removed = edge_filter.refine_positive(pos, rgb, um_per_px, cfg.edge)
    else:
        removed = np.zeros(pos.shape, bool)

    expand_px = max(0, int(cfg.detection.expand_px))
    if expand_px and pos.any():                # grow kept positives into nearby teal
        k = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (2 * expand_px + 1, 2 * expand_px + 1))
        grown = cv2.dilate(pos.astype(np.uint8), k).astype(bool) & t
        if cfg.detection.expand_teal_min > 0:  # don't grow into achromatic edges/tissue
            grown &= pos | (opp >= cfg.detection.expand_teal_min)
        pos = grown

    return {"sabg": pos, "edge_removed": removed, "hot": hot,
            "sabg_candidate": candidate}
