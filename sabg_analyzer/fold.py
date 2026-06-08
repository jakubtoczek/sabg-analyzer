"""Fold (linear-artifact) detection on the overview positive density.

Tissue folds create thin, often curved ridges of **false** SABG+ signal that the
per-pixel dark+non-teal artifact mask cannot catch (they aren't dense). They are
told apart from genuine signal by being **elongated / directionally coherent**
rather than blobby.

This runs once on the small overview (the linear structure isn't visible in a
single tile). From the projected positive mask we build a smoothed *density*,
then score "foldness" with two complementary, derivative-based cues:

* a **Hessian ridge** filter (skimage ``sato``) - responds to thin ridges, not
  blobs; multiscale / curvature-tolerant.
* the **structure-tensor coherence** - high where the density is locally
  one-directional, low where it's isotropic.

They are combined (``product`` by default, or ``agreement``/``union``/
``frangi_only``), thresholded, guarded by length/width/aspect (so only long thin
structures survive), and dilated into a band. The band is treated as an artifact
region: excluded from the SABG numerator and (by default) the tissue denominator,
and drawn in a distinct overlay colour for audit.

NB: this discriminates *linear vs. blobby*, not *fold vs. real* - genuinely
linear biology would also be flagged. It is conservative and fully auditable
(distinct colour + ``fold_px`` column + per-scene control); enabled by default,
set ``fold.enabled: false`` to turn it off.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class FoldParams:
    enabled: bool = True
    # What the ridge detector runs on:
    #   density - tissue optical-density excess (catches the physical fold ridge,
    #             independent of staining) - DEFAULT, what makes physical sense.
    #   sabg    - the SABG+ positive density (legacy; misses strongly-stained folds).
    source: str = "density"
    hp_um: float = 150.0            # density high-pass scale (local excess over this)
    border_um: float = 40.0         # ignore this much tissue border (the outline ridge)
    combine: str = "product"        # product | agreement | union | frangi_only
    smooth_um: float = 15.0         # density smoothing sigma
    min_length_um: float = 300.0    # ignore structures shorter than this
    max_width_um: float = 80.0      # ignore structures wider than this (not a fold)
    min_aspect: float = 3.0         # length/width must exceed this
    band_width_um: float = 60.0     # dilate the kept ridge into a band
    ridge_min: float = 0.12         # Frangi gate (agreement | union | frangi_only)
    coherence_min: float = 0.30     # structure-tensor gate (agreement | union)
    score_min: float = 0.05         # threshold on the combined product score
    exclude_from_tissue: bool = True  # drop band from denominator too (else numerator only)


def fold_density_map(rgb: np.ndarray, tissue: np.ndarray,
                     ov_um_per_px: float | None, p: FoldParams) -> np.ndarray:
    """Tissue optical-density *excess* map (overview resolution) for fold finding.

    A fold stacks tissue on itself, so it is locally **darker/denser** than the
    surrounding tissue. We take the max-channel optical density, subtract a
    fold-scale blurred copy (high-pass) to keep only the local excess, and zero
    everything outside the eroded tissue interior (so the tissue *outline* - itself
    a density edge - is not mistaken for a fold). Returns a float map.
    """
    from .tissue import erode_mask
    H, W = tissue.shape
    if not ov_um_per_px or ov_um_per_px <= 0:
        return np.zeros((H, W), np.float32)
    bright = rgb.astype(np.float32).max(axis=2) / 255.0
    od = -np.log10(np.clip(bright, 1e-3, 1.0))
    hp_sigma = max(3.0, p.hp_um / ov_um_per_px)
    excess = np.clip(od - cv2.GaussianBlur(od, (0, 0), hp_sigma), 0.0, None)
    interior = erode_mask(tissue.astype(bool), int(round(p.border_um / ov_um_per_px)))
    excess[~interior] = 0.0
    return excess


def _norm(a: np.ndarray, mask: np.ndarray | None = None) -> np.ndarray:
    """Scale to [0, 1] by the 99th percentile (over *mask* if given)."""
    v = a[mask] if mask is not None else a
    if v.size == 0:
        return np.zeros_like(a)
    hi = float(np.percentile(v, 99.0))
    if hi <= 0:
        return np.zeros_like(a)
    return np.clip(a / hi, 0.0, 1.0)


def detect_folds(signal: np.ndarray, ov_um_per_px: float | None,
                 tissue: np.ndarray, p: FoldParams) -> np.ndarray:
    """Return a boolean fold-band mask (overview resolution).

    *signal* is an overview-resolution density to find ridges in: the tissue
    optical-density excess (``source='density'``, via :func:`fold_density_map`) or
    a binary SABG+ estimate (``source='sabg'``). *ov_um_per_px* is the physical
    size of one overview pixel (µm). Returns all-False if the size is unknown or
    nothing fold-like is found.
    """
    H, W = signal.shape
    tissue = tissue.astype(bool)
    if not signal.any() or not ov_um_per_px or ov_um_per_px <= 0:
        return np.zeros((H, W), bool)

    sigma = max(0.5, p.smooth_um / ov_um_per_px)
    density = cv2.GaussianBlur(signal.astype(np.float32), (0, 0), sigma)
    dnorm = _norm(density, tissue)

    # Hessian ridge (bright ridges on the density map)
    from skimage.filters import sato
    half_w = max(1.0, (p.max_width_um / ov_um_per_px) / 2.0)
    sigmas = np.linspace(1.0, half_w, 4)
    ridge = sato(density, sigmas=sigmas, black_ridges=False)
    rnorm = _norm(ridge, tissue)

    # structure-tensor coherence (directional anisotropy)
    from skimage.feature import structure_tensor, structure_tensor_eigenvalues
    A = structure_tensor(density, sigma=sigma, order="rc")
    l1, l2 = structure_tensor_eigenvalues(A)
    coh = ((l1 - l2) / (l1 + l2 + 1e-9)) ** 2

    mode = p.combine
    if mode == "frangi_only":
        raw = rnorm >= p.ridge_min
    elif mode == "agreement":
        raw = (rnorm >= p.ridge_min) & (coh >= p.coherence_min) & (dnorm > 0.05)
    elif mode == "union":
        raw = ((rnorm >= p.ridge_min) | (coh >= p.coherence_min)) & (dnorm > 0.10)
    else:  # product (soft agreement)
        raw = (rnorm * coh * dnorm) >= p.score_min
    raw = raw & tissue
    if not raw.any():
        return np.zeros((H, W), bool)

    # geometric guards on the raw ridge (before band dilation)
    from skimage.measure import label, regionprops
    lab = label(raw)
    keep = np.zeros((H, W), bool)
    for r in regionprops(lab):
        major = getattr(r, "axis_major_length", None)
        if major is None:                       # older skimage
            major = r.major_axis_length
        length_px = max(r.perimeter / 2.0, major, 1.0)  # arc-robust
        width_px = r.area / length_px
        aspect = length_px / max(width_px, 1e-6)
        if (length_px * ov_um_per_px >= p.min_length_um
                and width_px * ov_um_per_px <= p.max_width_um
                and aspect >= p.min_aspect):
            keep[lab == r.label] = True
    if not keep.any():
        return np.zeros((H, W), bool)

    # dilate the kept ridge into a band
    bw = max(1, int(round(p.band_width_um / ov_um_per_px)))
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * bw + 1, 2 * bw + 1))
    band = cv2.dilate(keep.astype(np.uint8), k).astype(bool)
    return band & tissue
