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


def _glass_white_point(rgb: np.ndarray, percentile: float = 60.0) -> np.ndarray:
    """Dominant glass colour = per-channel *percentile* of the non-near-black pixels.

    Unlike ``estimate_white_point`` (the brightest pixels, which are nearly neutral and
    so under-correct), this lands on the bulk yellowish glass, so mapping it to white
    removes the background cast more strongly. Mirrors ``estimate_background`` without
    needing a tissue mask (glass dominates the frame)."""
    pix = rgb.reshape(-1, 3).astype(np.float32)
    pix = pix[pix.max(axis=1) > 30]                 # drop near-black mosaic gaps
    if len(pix) < 100:
        return np.array([245.0, 245.0, 245.0], np.float32)
    return np.percentile(pix, percentile, axis=0).astype(np.float32)


def auto_white_point(rgb: np.ndarray, bright_frac: float = 0.2,
                     neutralize: float = 0.0, glass_percentile: float = 60.0,
                     tissue: np.ndarray | None = None) -> np.ndarray:
    """Auto white point, blending the brightest-pixel estimate with the glass colour.

    *neutralize* in [0,1] interpolates between the mild brightest-pixel white point
    (0 -> original behaviour) and the dominant-glass colour (1 -> full background->white,
    strongest de-cast). Higher values remove more of the yellow glass cast. When a
    *tissue* mask is given, the glass colour is taken from the **non-tissue** pixels
    (``estimate_background``) — a cleaner glass sample than the whole frame, so the
    de-cast is more accurate regardless of resolution."""
    mild = estimate_white_point(rgb, bright_frac)
    s = float(np.clip(neutralize, 0.0, 1.0))
    if s <= 0.0:
        return mild
    glass = (estimate_background(rgb, tissue, glass_percentile) if tissue is not None
             else _glass_white_point(rgb, glass_percentile))
    return (1.0 - s) * mild + s * glass


def resolve_white_point(rgb: np.ndarray, wbp) -> np.ndarray:
    """White point to use for *rgb* given a WhiteBalanceParams *wbp*, honouring
    ``wbp.scope`` for cross-image consistency:

    - ``global`` + ``wbp.white_point`` set -> that fixed point (comparable figures);
    - otherwise the per-image auto estimate (``bright_frac`` + ``neutralize``). ``section``
      scope is handled by the caller; here it falls back to per-image so batch/export stay
      self-balancing unless a global point is set.
    """
    if getattr(wbp, "scope", "image") == "global" and getattr(wbp, "white_point", None):
        return np.asarray(wbp.white_point, np.float32)
    return auto_white_point(rgb, wbp.bright_frac,
                            getattr(wbp, "neutralize", 0.0),
                            getattr(wbp, "glass_percentile", 60.0))


def apply_temperature(wp: np.ndarray, delta: float, k: float = 0.02) -> np.ndarray:
    """Warm/cool nudge of a white point (display only). ZEN ±1 ≈ ±10 K.

    WB gain is ``target / wp``, so to *warm* the image (delta > 0: more red, less
    blue) we shrink the red channel of the white point and grow the blue one. ``k``
    is the per-step fraction — a calibration knob; flip its sign if a warm nudge
    reads cool against ZEN.
    """
    if not delta:
        return np.asarray(wp, np.float32)
    f = 1.0 + k * float(delta)
    return np.asarray(wp, np.float32) * np.array([1.0 / f, 1.0, f], np.float32)


def tone_curve(rgb: np.ndarray, brightness: float = 0.0, contrast: float = 0.0,
               gamma: float = 1.0) -> np.ndarray:
    """Display tone adjust on uint8 RGB (figures only; default is a no-op).

    On normalised [0,1]: gamma (``x**(1/gamma)``), then contrast (affine about
    mid-grey ``(x-0.5)*(1+contrast)+0.5``), then additive brightness.
    """
    if brightness == 0.0 and contrast == 0.0 and gamma == 1.0:
        return rgb
    x = rgb.astype(np.float32) / 255.0
    if gamma != 1.0:
        x = np.power(x, 1.0 / max(gamma, 1e-3))
    if contrast:
        x = (x - 0.5) * (1.0 + contrast) + 0.5
    if brightness:
        x = x + brightness
    return (x.clip(0.0, 1.0) * 255.0).astype(np.uint8)


def white_balance(rgb: np.ndarray, background: np.ndarray,
                  target: float = 250.0) -> np.ndarray:
    """Scale each channel so *background* maps to *target* (near-white)."""
    bg = np.maximum(np.asarray(background, np.float32), 1.0)
    gain = target / bg
    out = rgb.astype(np.float32) * gain
    return out.clip(0, 255).astype(np.uint8)


def balance_for_display(rgb: np.ndarray, wbp, white_point=None) -> np.ndarray:
    """Full display pipeline (figures only) shared by preview + export so they
    always match: resolve (or override) the white point, apply the temperature
    nudge, white-balance to ``wbp.target``, then the tone curve."""
    wp = (np.asarray(white_point, np.float32) if white_point is not None
          else resolve_white_point(rgb, wbp))
    wp = apply_temperature(wp, getattr(wbp, "temperature", 0.0),
                           getattr(wbp, "temperature_k", 0.02))
    out = white_balance(rgb, wp, target=wbp.target)
    return tone_curve(out, getattr(wbp, "brightness", 0.0),
                      getattr(wbp, "contrast", 0.0), getattr(wbp, "gamma", 1.0))


if __name__ == "__main__":  # ponytail: smallest self-check for the new tone/temp math
    g = np.full((4, 4, 3), 128, np.uint8)
    assert tone_curve(g) is g, "default tone must be a no-op (identity)"
    assert tone_curve(g, gamma=2.0).mean() > 128, "gamma>1 brightens mids"
    assert tone_curve(g, brightness=-0.5).mean() < 128, "negative brightness darkens"
    wp = np.array([200.0, 200.0, 200.0], np.float32)
    assert np.allclose(apply_temperature(wp, 0), wp), "delta=0 is identity"
    warm = apply_temperature(wp, +2)                  # warmer -> smaller red wp -> bigger red gain
    assert (250.0 / warm)[0] > (250.0 / wp)[0] > (250.0 / warm)[2], "warm raises red gain, cuts blue"
    # auto_white_point: yellow glass image (R>G>B). neutralize=0 == brightest estimate;
    # neutralize>0 pulls the white point toward the (yellower) glass -> lower blue -> more blue gain.
    rng = np.random.default_rng(1)
    glassy = rng.integers(0, 60, (80, 80, 3), dtype=np.uint8)            # dark "tissue"
    glassy[:60] = np.array([225, 215, 185], np.uint8)                    # majority yellow glass
    glassy[:6, :6] = np.array([250, 249, 246], np.uint8)                 # a little bright fill
    mild = auto_white_point(glassy, neutralize=0.0)
    full = auto_white_point(glassy, neutralize=1.0)
    assert np.array_equal(mild, estimate_white_point(glassy)), "neutralize=0 is the old estimate"
    assert full[2] < mild[2], "neutralize>0 lowers the blue white point (stronger de-cast)"
    print("whitebalance self-check OK")
