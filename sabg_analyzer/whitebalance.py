"""Background white-point white balance (for publication figures only).

The slides have a yellowish glass/paper background. We estimate that background
colour and apply a per-channel linear gain so it becomes neutral near-white;
because the gain is a simple scalar per channel, the histology colours shift
only slightly (same idea as GIMP's white-balance / white-point pick).

Quantification never uses this — it runs on raw pixels.
"""

from __future__ import annotations

import numpy as np


def estimate_background(rgb: np.ndarray, tissue: np.ndarray,
                        percentile: float = 60.0) -> np.ndarray:
    """Estimate the background (glass/paper) RGB from non-tissue pixels.

    Excludes near-black mosaic gaps; takes a mid-high percentile of the
    remaining background so the result reflects the bright yellowish glass.
    """
    bg = ~tissue.astype(bool)
    pix = rgb[bg].astype(np.float32) if bg.any() else rgb.reshape(-1, 3).astype(np.float32)
    maxc = pix.max(axis=1)
    pix = pix[maxc > 30]                      # drop black gaps
    if len(pix) < 100:
        return np.array([245.0, 245.0, 245.0], np.float32)
    return np.percentile(pix, percentile, axis=0).astype(np.float32)


def estimate_white_point(rgb: np.ndarray, bright_frac: float = 0.2) -> np.ndarray:
    """Per-image white point = mean RGB of the brightest *bright_frac* pixels.

    In SABG slides the empty/background areas are the brightest pixels (the teal
    stain is dark), so this picks up the local glass colour and neutralises each
    crop's own cast — the same as picking a background white point in GIMP.
    """
    gray = rgb.max(axis=2)
    thr = np.percentile(gray, 100.0 * (1.0 - bright_frac))
    sel = gray >= thr
    if sel.sum() < 50:
        return np.array([245.0, 245.0, 245.0], np.float32)
    return rgb[sel].astype(np.float32).mean(axis=0)


def white_balance(rgb: np.ndarray, background: np.ndarray,
                  target: float = 250.0) -> np.ndarray:
    """Scale each channel so *background* maps to *target* (near-white)."""
    bg = np.maximum(np.asarray(background, np.float32), 1.0)
    gain = target / bg
    out = rgb.astype(np.float32) * gain
    return out.clip(0, 255).astype(np.uint8)
