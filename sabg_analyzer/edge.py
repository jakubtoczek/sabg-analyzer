"""Edge-shadow rejection on the per-tile SABG+ mask.

Cell / structure boundaries cast thin local **shadows** (a darkening rim) that a
deconvolution SABG score reads as stain, producing false positives shaped like a
thin filigree tracing edges. Genuine SA-beta-Gal signal sits in **wider** patches.
We remove the rims with two complementary, configurable cues (an *agreement* of a
shape test and a colour test):

* **morphological opening** - a small disk opening drops structures thinner than
  ``min_width_um`` (the rim) while keeping wider blobs (real signal). The kernel is
  intentionally tiny (a few pixels) so faint-but-real specks in small zones survive.
* **shadow rejection** - a positive pixel that is both *dark* and *achromatic*
  (low brightness AND low saturation) is the shadow signature; real teal staining
  keeps its colour, so this spares pale-but-teal signal.

This refines the numerator only (a rim is real tissue, just not stain), and the
removed pixels are returned so the overlay can show exactly what was dropped.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np


@dataclass
class EdgeFilterParams:
    enabled: bool = True
    morph_open: bool = True          # drop structures thinner than min_width_um
    min_width_um: float = 1.5        # opening kernel diameter (~3-4 px at 0.44 um/px)
    reject_shadow: bool = True       # drop dark + achromatic (near-grey) positives
    shadow_dark_level: float = 0.60  # brightness (0-1) below this = dark
    shadow_sat_min: float = 0.10     # saturation below this = achromatic (near-grey)


def refine_positive(pos: np.ndarray, rgb: np.ndarray, um_per_px: float | None,
                    p: EdgeFilterParams) -> tuple[np.ndarray, np.ndarray]:
    """Return ``(kept, removed)`` boolean masks for a SABG+ tile mask *pos*.

    *removed* is ``pos & ~kept`` (the rejected edge-shadow pixels), for audit/overlay.
    """
    if not p.enabled or not pos.any():
        return pos, np.zeros_like(pos)

    kept = pos.copy()

    if p.morph_open and um_per_px and um_per_px > 0:
        r = max(1, int(round((p.min_width_um / um_per_px) / 2.0)))
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
        opened = cv2.morphologyEx(kept.astype(np.uint8), cv2.MORPH_OPEN, k)
        kept = opened.astype(bool)

    if p.reject_shadow:
        a = rgb.astype(np.float32) / 255.0
        maxc = a.max(axis=2)
        minc = a.min(axis=2)
        sat = np.where(maxc > 0, (maxc - minc) / np.maximum(maxc, 1e-6), 0.0)
        is_shadow = (maxc < p.shadow_dark_level) & (sat < p.shadow_sat_min)
        kept = kept & ~is_shadow

    return kept, pos & ~kept
