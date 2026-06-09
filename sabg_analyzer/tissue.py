"""Tissue segmentation and artifact rejection.

A pixel is *tissue* if it is neither (a) an unsampled black mosaic gap nor
(b) white slide glass. Both are excluded so that %SABG is taken over real
tissue only.

On top of that, an **artifact pass** flags fold/debris pixels: regions that are
much darker than stained tissue but are *not* teal (so genuine dark SABG specks
survive). Folds stack tissue on itself, raising optical density in all channels,
which a colour-deconvolution SABG score otherwise mistakes for stain. Artifact
pixels are excluded from both the numerator and the denominator of %SABG, and a
border erosion of the tissue blob drops edge halos.

The per-pixel tests (`tissue_mask`, `artifact_mask`) run on every full-res tile
during counting. The morphological cleanup (`clean_tissue_mask`, `erode_mask`)
runs once on the small overview mask — it must not be applied tile-by-tile, or
it would erode signal at tile borders and is too slow at full res.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class TissueParams:
    gap_level: int = 15        # max(R,G,B) <= this => black mosaic gap
    white_level: float = 0.92  # brightness (0-1) >= this AND ...
    sat_min: float = 0.10      # ... saturation <= this => white glass
    min_object_px: int = 2000  # remove tissue objects smaller than this (overview px)
    fill_holes: bool = False    # off: filling re-absorbs black mosaic gaps as tissue
    # Adaptive background (for pale/tinted glass the fixed white test misses):
    # estimate the glass colour per scene, then a pixel is glass only if it is
    # (a) close to that colour, (b) carries no teal, AND (c) is texture-smooth.
    # The teal + texture guards keep faint, near-glass-coloured tissue (which is
    # smooth-coloured but cellularly *textured*) from being dropped as background.
    adaptive: bool = True
    bg_margin: float = 0.086        # RGB distance (0-1) within which a pixel matches glass
    bg_bright_quantile: float = 0.75  # glass colour = median of the brightest this-fraction
    bg_teal_guard: float = 0.03     # opponent score above this => keep as tissue (never glass)
    texture_min: float = 0.0045     # local std above this => textured => tissue (higher = less sensitive)
    texture_win: int = 15           # window (px) for the local-std texture estimate
    close_px: int = 3               # morphological close to solidify textured tissue (overview)
    fill_holes_max_frac: float = 0.0012  # fill enclosed non-tissue holes up to this frame fraction
    bg_max_tissue_frac: float = 0.97  # if adaptive keeps more than this, retry with a stricter bg
    # Reclaim faint interior tissue wrongly dropped as glass (enclosed holes inside the
    # tissue blob). A hole is FILLED only when it shows POSITIVE tissue evidence -- a
    # meaningful fraction of its pixels are textured (local std >= texture_min) OR teal
    # (opponent >= bg_teal_guard). Smooth non-teal glass and black mosaic gaps carry no
    # such evidence, so they stay open.
    fill_interior_holes: bool = False
    interior_hole_min_tissue_frac: float = 0.10  # fill a hole only if >= this fraction of
                                                 # its pixels are textured or teal (tissue)
    interior_hole_max_frac: float = 0.02  # upper guard: never fill a hole bigger than this
                                          # frame fraction (protects large enclosed glass)


@dataclass
class ArtifactParams:
    """Fold / debris rejection. Flags tissue pixels that are much darker than
    stained tissue *and* not teal, plus an eroded tissue border (edge halos)."""
    enabled: bool = True
    dark_level: float = 0.45   # max(R,G,B)/255 below this = suspiciously dark
    teal_min: float = 0.04     # opponent score above this = real teal -> keep it
    erode_px: int = 3          # erode the tissue border by this many overview px
    min_object_px: int = 12    # drop dark components smaller than this (processed px);
                               # keeps tiny dust/nuclei specks countable, only excludes
                               # substantial dark debris/folds. 0 = no size filter.


def estimate_background(rgb: np.ndarray, p: TissueParams) -> np.ndarray | None:
    """Estimate the glass/background colour of a scene as an RGB vector in [0, 1].

    Glass is the brightest large region, so the median colour of the brightest
    ``bg_bright_quantile`` of (non-gap) pixels is a robust estimate even when tissue
    touches the border. Returns None if there aren't enough bright pixels.
    """
    a = rgb.astype(np.float32) / 255.0
    maxc = a.max(axis=2)
    nongap = maxc > (p.gap_level / 255.0)
    vals = maxc[nongap]
    if vals.size < 100:
        return None
    cut = float(np.quantile(vals, p.bg_bright_quantile))
    sel = nongap & (maxc >= cut)
    if int(sel.sum()) < 100:
        return None
    return np.median(a[sel], axis=0).astype(np.float32)


def tissue_mask(rgb: np.ndarray, p: TissueParams,
                bg: np.ndarray | None = None) -> np.ndarray:
    """Boolean tissue mask for an RGB uint8 image (per-pixel, no morphology).

    *bg* is the optional per-scene glass colour from :func:`estimate_background`;
    when given (and ``p.adaptive``) pale/tinted glass that the fixed white test
    misses is also removed.
    """
    rgb_i = rgb.astype(np.int16)
    maxc = rgb_i.max(axis=2)

    # (a) black mosaic gaps: all channels near zero.
    gap = maxc <= p.gap_level

    # (b) white glass: bright and low-saturation.
    bright = maxc / 255.0
    minc = rgb_i.min(axis=2)
    sat = np.where(maxc > 0, (maxc - minc) / np.maximum(maxc, 1), 0.0)
    glass = (bright >= p.white_level) & (sat <= p.sat_min)

    # (c) adaptive glass: close to the estimated glass colour AND not teal AND
    # texture-smooth (cellular tissue is textured even when its colour ~ glass).
    if p.adaptive and bg is not None:
        from .scoring import opponent_score
        a = rgb.astype(np.float32) / 255.0
        dist = np.sqrt(((a - bg.reshape(1, 1, 3)) ** 2).sum(axis=2))
        unstained = opponent_score(rgb) < p.bg_teal_guard   # stain guard: keep faint tissue
        w = max(3, int(p.texture_win) | 1)                  # odd window
        g = a.mean(axis=2)
        m = cv2.blur(g, (w, w))
        lstd = np.sqrt(np.clip(cv2.blur(g * g, (w, w)) - m * m, 0.0, None))
        smooth = lstd < p.texture_min                       # texture guard: keep faint tissue
        glass = glass | ((dist <= p.bg_margin) & unstained & smooth)

    return ~(gap | glass)


def artifact_mask(rgb: np.ndarray, opp: np.ndarray, p: ArtifactParams) -> np.ndarray:
    """Per-pixel fold/debris mask: dark AND not teal.

    *opp* is the opponent score (``scoring.opponent_score``); passing it in avoids
    recomputing it. Genuine SABG specks can be dark too, so the teal guard
    (``opp >= teal_min``) keeps them out of the artifact mask.
    """
    if not p.enabled:
        return np.zeros(rgb.shape[:2], bool)
    bright = rgb.astype(np.float32).max(axis=2) / 255.0
    mask = (bright < p.dark_level) & (opp < p.teal_min)
    if p.min_object_px > 0 and mask.any():
        mask = _remove_small_objects(mask, p.min_object_px)
    return mask


def _remove_small_objects(mask: np.ndarray, min_px: int) -> np.ndarray:
    """Drop connected components smaller than *min_px* (so tiny dark specks stay
    countable instead of being rejected as debris). Tile-safe: real specks are
    well within a tile, and large debris/folds stay large in each tile."""
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    keep = st[:, cv2.CC_STAT_AREA] >= min_px
    keep[0] = False                       # component 0 is the background
    return keep[lab]


def erode_mask(mask: np.ndarray, px: int) -> np.ndarray:
    """Erode a boolean mask by *px* (overview pixels). Drops border halos."""
    if px <= 0:
        return mask.astype(bool)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * px + 1, 2 * px + 1))
    return cv2.erode(mask.astype(np.uint8), k).astype(bool)


def clean_tissue_mask(mask: np.ndarray, p: TissueParams) -> np.ndarray:
    """Solidify (close), remove small specks, and optionally fill holes. Overview only."""
    m = mask.astype(np.uint8)

    if p.close_px > 0:   # connect sparse textured tissue into solid blobs
        ck = cv2.getStructuringElement(
            cv2.MORPH_ELLIPSE, (2 * p.close_px + 1, 2 * p.close_px + 1))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, ck)

    if p.fill_holes:
        # Flood the exterior background; whatever stays background is an interior hole.
        h, w = m.shape
        ff = m.copy()
        flood = np.zeros((h + 2, w + 2), np.uint8)
        cv2.floodFill(ff, flood, (0, 0), 1)
        holes = (ff == 0)
        m[holes] = 1

    if p.min_object_px > 0:
        n, labels, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
        keep = np.zeros(n, bool)
        for i in range(1, n):
            keep[i] = stats[i, cv2.CC_STAT_AREA] >= p.min_object_px
        m = keep[labels].astype(np.uint8)

    if p.fill_holes_max_frac > 0:   # fill small enclosed holes (interior speckle),
        m = _fill_small_holes(m, int(p.fill_holes_max_frac * m.size))  # not the big glass

    return m.astype(bool)


def _fill_small_holes(m: np.ndarray, max_px: int) -> np.ndarray:
    """Fill enclosed background components up to *max_px* (interior speckle).

    A hole that touches the image border (the open glass margin) is never filled,
    so this solidifies tissue interiors without re-absorbing the glass.
    """
    m = m.astype(np.uint8)
    H, W = m.shape
    n, lab, st, _ = cv2.connectedComponentsWithStats((1 - m).astype(np.uint8), 8)
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if area <= max_px and not (x == 0 or y == 0 or x + w == W or y + h == H):
            m[lab == i] = 1
    return m


def _fill_interior_holes_guarded(m: np.ndarray, rgb: np.ndarray,
                                 bg: np.ndarray | None, p: TissueParams) -> np.ndarray:
    """Reclaim faint interior tissue that the glass tests wrongly dropped.

    Faint near-glass tissue gets classified as glass by the *adaptive* test and
    becomes an enclosed hole inside the tissue blob. An enclosed (non-border) hole
    is filled only when it shows POSITIVE tissue evidence: at least
    ``interior_hole_min_tissue_frac`` of its pixels are either *textured* (local std
    >= ``texture_min``) or *teal* (opponent score >= ``bg_teal_guard``). Smooth,
    non-teal glass and black mosaic gaps carry no such evidence, so they stay open --
    this is the inverse of the old "fill unless it clearly looks like glass" guard,
    which absorbed faint real glass for merely lacking glass evidence.

    The hole must also be no larger than ``interior_hole_max_frac`` of the frame (an
    upper guard against re-absorbing large enclosed glass lakes). Border-touching
    holes (the exterior glass margin) are never filled. ``bg`` is unused now that the
    fill keys off tissue evidence rather than proximity to the estimated glass colour.
    """
    m = m.astype(np.uint8)
    H, W = m.shape
    from .scoring import opponent_score
    a = rgb.astype(np.float32) / 255.0
    g = a.mean(axis=2)
    win = max(3, int(p.texture_win) | 1)
    mb = cv2.blur(g, (win, win))
    lstd = np.sqrt(np.clip(cv2.blur(g * g, (win, win)) - mb * mb, 0.0, None))
    opponent = opponent_score(rgb)
    tissue_evidence = (lstd >= p.texture_min) | (opponent >= p.bg_teal_guard)

    max_px = (int(p.interior_hole_max_frac * m.size)
              if p.interior_hole_max_frac > 0 else m.size)
    min_frac = max(0.0, float(p.interior_hole_min_tissue_frac))
    n, lab, st, _ = cv2.connectedComponentsWithStats((1 - m).astype(np.uint8), 8)
    for i in range(1, n):
        x, y, w, h, area = st[i]
        if x == 0 or y == 0 or x + w == W or y + h == H:
            continue                                  # exterior glass margin
        if area > max_px:
            continue                                  # too large: likely real glass
        comp = (lab == i)
        if float(tissue_evidence[comp].mean()) >= min_frac:   # tissue evidence -> reclaim
            m[comp] = 1
    return m.astype(bool)


def segment_tissue(rgb: np.ndarray, p: TissueParams) -> np.ndarray:
    """Full overview tissue mask: estimate background, classify, clean, with an
    anti-collapse retry (if the adaptive pass keeps almost everything, the glass
    colour estimate was unreliable, so retry from the brightest pixels only)."""
    bg = estimate_background(rgb, p)
    m = clean_tissue_mask(tissue_mask(rgb, p, bg), p)
    use_p, use_bg = p, bg
    if p.adaptive and bg is not None:
        frame = rgb.astype(np.int16).max(axis=2) > p.gap_level
        if frame.any() and float(m[frame].mean()) > p.bg_max_tissue_frac:
            from dataclasses import replace
            strict = replace(p, bg_bright_quantile=max(p.bg_bright_quantile, 0.92))
            bg2 = estimate_background(rgb, strict)
            if bg2 is not None:
                m = clean_tissue_mask(tissue_mask(rgb, strict, bg2), strict)
                use_p, use_bg = strict, bg2
    if p.fill_interior_holes:   # reclaim faint interior tissue dropped as glass
        m = _fill_interior_holes_guarded(m, rgb, use_bg, use_p)
    return m
