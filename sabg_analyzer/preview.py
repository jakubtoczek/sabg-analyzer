"""In-process preview engine for interactive tuning (no Tk here).

The full SABG pipeline runs tile-by-tile over a whole scene, but every mask is a
plain array operation, so a single cap-bounded ROI fits in RAM and can be computed
in one shot. This module:

  * lists scannable sections (alias + thumbnail) for the picker,
  * maps a thumbnail rectangle to a capped full-resolution ROI,
  * computes every overlay layer for that ROI using the SAME shared mask functions
    as `pipeline.analyze_scene` (`masks.compute_region_masks` / `masks.detect_sabg`),
    so the preview can't drift from the real analysis,
  * exports the current settings back to a config.yaml.

Hysteresis on the ROI uses the real `_grow_connected` directly (the pipeline's
HD-canvas approximation isn't needed for one in-RAM crop). The seed threshold is
auto-estimated on the ROI tissue (mirroring pass 1) unless a manual value is given.

The GUI (`sabg_preview_gui.py`) owns the CZI `doc` handle and calls these on a
worker thread.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
import yaml

from . import czi_io, scoring
from .config import Config
from .czi_io import SceneInfo
from .fold import detect_folds, fold_density_map
from .masks import _primary_secondary, compute_region_masks, detect_sabg
from .metadata import build_aliases, load_metadata, section_skipped
from .pipeline import _build_config_snapshot, _score_ranges
from .threshold import ScoreHistogram, compute_threshold
from .tissue import erode_mask, estimate_background, segment_tissue


# ---------------------------------------------------------------------------
# section listing for the picker
# ---------------------------------------------------------------------------
@dataclass
class SectionEntry:
    scene: SceneInfo
    alias: str
    thumb_path: Path          # may not exist if `scan` wasn't run
    skipped: bool             # the sections.csv `analyze` cell says to skip


def list_sections(data_dir: str | Path, out_dir: str | Path,
                  cfg: Config) -> list[SectionEntry]:
    """All scenes under *data_dir*, with the alias + thumbnail used by the picker.

    Aliases are built exactly as `analyze` builds them (``build_aliases`` over the
    sections.csv metadata), so picker labels match the analysis output. Thumbnails
    are the proportional PNGs written by `scan` to ``<out>/thumbs/<slug>.png``.
    """
    out_dir = Path(out_dir)
    files = czi_io.list_czi_files(data_dir)
    scenes: list[SceneInfo] = []
    for f in files:
        scenes.extend(czi_io.list_scenes(f))

    sections_csv = out_dir / "sections.csv"
    metadata = load_metadata(sections_csv) if sections_csv.exists() else {}
    aliases = build_aliases(scenes, metadata, cfg.alias)

    thumbs_dir = out_dir / "thumbs"
    entries: list[SectionEntry] = []
    for s in scenes:
        md = metadata.get((s.file_stem, s.scene_index), {})
        entries.append(SectionEntry(
            scene=s,
            alias=aliases.get(s.key, s.slug),
            thumb_path=thumbs_dir / f"{s.slug}.png",
            skipped=section_skipped(md),
        ))
    return entries


# ---------------------------------------------------------------------------
# ROI geometry
# ---------------------------------------------------------------------------
def roi_rect_full(scene: SceneInfo, thumb_w: int, thumb_h: int,
                  rx: float, ry: float, rw: float, rh: float,
                  cap_um: float | None) -> tuple[int, int, int, int]:
    """Map a rectangle drawn on the thumbnail to a global full-res ROI.

    The thumbnail spans the scene's bounding box, so thumb px -> full-res px is a
    simple scale. The result is clamped to the scene and, if *cap_um* is set,
    centre-cropped so neither side exceeds the cap (in microns).
    Returns ``(x, y, w, h)`` in global full-resolution coordinates.
    """
    sx = scene.w / max(thumb_w, 1)
    sy = scene.h / max(thumb_h, 1)
    x = scene.x + int(round(rx * sx))
    y = scene.y + int(round(ry * sy))
    w = max(1, int(round(rw * sx)))
    h = max(1, int(round(rh * sy)))
    if cap_um and scene.pixel_size_um:
        cap_px = max(1, int(round(cap_um / scene.pixel_size_um)))
        if w > cap_px:
            x += (w - cap_px) // 2
            w = cap_px
        if h > cap_px:
            y += (h - cap_px) // 2
            h = cap_px
    # clamp to the scene bounds
    x = max(scene.x, min(x, scene.x + scene.w - 1))
    y = max(scene.y, min(y, scene.y + scene.h - 1))
    w = max(1, min(w, scene.x + scene.w - x))
    h = max(1, min(h, scene.y + scene.h - y))
    return x, y, w, h


def read_roi(doc, x: int, y: int, w: int, h: int, zoom: float = 1.0) -> np.ndarray:
    """Read a global ROI as RGB uint8 (thin wrapper over ``czi_io.read_region``)."""
    return czi_io.read_region(doc, x, y, w, h, zoom)


# ---------------------------------------------------------------------------
# mask computation on a single ROI (mirrors pipeline.analyze_scene)
# ---------------------------------------------------------------------------
def _roi_conv(rgb: np.ndarray, cfg: Config, region: np.ndarray) -> np.ndarray:
    """Stain matrix for the ROI, optionally auto-estimated from its tissue."""
    conv = scoring.conv_matrix(cfg.detection.stain_matrix)
    if cfg.detection.auto_estimate:
        est = scoring.estimate_sabg_od(rgb, region)
        if est is not None:
            sm = scoring.build_stain_matrix(est, cfg.detection.stain_matrix[1])
            conv = scoring.conv_matrix(sm)
    return conv


def roi_threshold(rgb: np.ndarray, t: np.ndarray, conv: np.ndarray, cfg: Config,
                  manual: float | None = None) -> tuple[float, float | None]:
    """Seed (and optional secondary) threshold for the ROI.

    Mirrors pass 1: build a histogram of the primary/secondary scores over the
    countable tissue *t* and run ``compute_threshold``. A *manual* value overrides
    the primary seed (like a per-scene threshold); the secondary stays auto when
    ``require_agreement``.
    """
    primary = cfg.detection.primary
    agree = cfg.detection.require_agreement
    dec = scoring.deconvolution_score(rgb, conv)
    opp = scoring.opponent_score(rgb)
    p_s, s_s = _primary_secondary(dec, opp, primary)
    (lo_p, hi_p), (lo_s, hi_s) = _score_ranges(primary)

    def _thr(vals, lo, hi):
        h = ScoreHistogram(lo, hi)
        h.add(vals)
        return compute_threshold(h, cfg.threshold)

    if t.any():
        thr = float(manual) if manual is not None else _thr(p_s[t], lo_p, hi_p)
        thr_s = _thr(s_s[t], lo_s, hi_s) if agree else None
    else:
        thr = float(manual) if manual is not None else cfg.threshold.min_score
        thr_s = None
    return thr, thr_s


def _roi_fold_band(rgb: np.ndarray, region: np.ndarray,
                   pixel_size_um: float | None, cfg: Config) -> np.ndarray:
    """Fold band for the ROI, detected at a coarse resolution then upsampled.

    The pipeline detects folds on the gating overview (``overview_um_per_px``) and
    upsamples the band to the full-res tiles; doing the same here keeps the fold
    parameters (all in µm) behaving as in analysis and keeps the skimage ridge
    detection fast (it is run on a small array, never the full-res ROI).
    """
    H, W = region.shape
    fold_um = cfg.overview_um_per_px
    if pixel_size_um and fold_um and pixel_size_um < fold_um:
        fscale = pixel_size_um / fold_um
        sw, sh = max(1, int(round(W * fscale))), max(1, int(round(H * fscale)))
        small = cv2.resize(rgb, (sw, sh), interpolation=cv2.INTER_AREA)
        small_region = cv2.resize(region.astype(np.uint8), (sw, sh),
                                  interpolation=cv2.INTER_NEAREST).astype(bool)
        signal = fold_density_map(small, small_region, fold_um, cfg.fold)
        band = detect_folds(signal, fold_um, small_region, cfg.fold)
        return cv2.resize(band.astype(np.uint8), (W, H),
                          interpolation=cv2.INTER_NEAREST).astype(bool)
    signal = fold_density_map(rgb, region, pixel_size_um, cfg.fold)
    return detect_folds(signal, pixel_size_um, region, cfg.fold)


def compute_roi_layers(rgb: np.ndarray, cfg: Config, pixel_size_um: float | None,
                       manual_thr: float | None = None) -> dict:
    """Compute every overlay layer for one full-res RGB ROI.

    Returns a dict of boolean masks (all the same H×W as *rgb*) plus the thresholds:
    ``tissue`` (countable), ``region`` (full tissue blob), ``artifact``, ``fold``,
    ``sabg``, ``edge_removed``, ``nontissue``, and ``thr`` / ``thr_s``.
    The mask order matches `pipeline.analyze_scene` exactly (it calls the same
    `compute_region_masks` / `detect_sabg`), so what you tune here is what analysis does.
    """
    tcfg = cfg.tissue
    region = segment_tissue(rgb, tcfg)
    conv = _roi_conv(rgb, cfg, region)
    region_c = (erode_mask(region, cfg.artifact.erode_px)
                if cfg.artifact.enabled else region)

    fold_band = None
    if cfg.fold.enabled:
        fold_band = _roi_fold_band(rgb, region, pixel_size_um, cfg)
        if cfg.fold.exclude_from_tissue:
            region_c = region_c & ~fold_band

    t, art, fold, opp = compute_region_masks(
        rgb, tcfg, cfg, region=region, region_c=region_c, fold_band=fold_band)
    thr, thr_s = roi_threshold(rgb, t, conv, cfg, manual=manual_thr)
    d = detect_sabg(rgb, t, opp, cfg, conv, thr, thr_s, pixel_size_um,
                    fold=fold, keep_here=None)

    return {
        "tissue": t,
        "region": region,
        "artifact": art,
        "fold": fold,
        "sabg": d["sabg"],
        "edge_removed": d["edge_removed"],
        "nontissue": ~region,
        "thr": thr,
        "thr_s": thr_s,
    }


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------
def export_config(cfg: Config, path: str | Path) -> Path:
    """Write the current settings to *path* as a full config.yaml.

    Reuses `pipeline._build_config_snapshot` (the same serialiser `analyze` uses),
    so the exported file mirrors every block and is directly re-loadable / editable.
    """
    path = Path(path)
    snapshot = _build_config_snapshot(cfg, [])
    path.write_text(yaml.safe_dump(snapshot, sort_keys=False), encoding="utf-8")
    return path
