"""Pipeline orchestration: scan (metadata pass) and analyze (quantification pass).

Per scene, quantification is full-resolution and tiled:

  1. Read a small overview; derive the cleaned **tissue region** (excludes white
     glass + black mosaic gaps, removes specks). A border erosion + per-tile
     **artifact mask** (dark, non-teal folds/debris) further trims the countable
     tissue. This region gates all counting.
  2. Pass 1 - stream full-res tiles, accumulate histograms of BOTH SABG scores
     (deconvolution + opponent) over countable tissue, and derive a threshold
     for each (primary may be a manual override).
  3. Pass 2 - stream tiles again; a pixel is SABG+ only where the primary score
     clears its threshold AND (when require_agreement) the secondary score does
     too. Count tissue/positive/artifact pixels and project the full-res masks
     onto both the gating overview (debug) and a higher-res canvas (overlay/maps)
     so punctate signal stays visible.
"""

from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np
import pandas as pd
import yaml
from pylibCZIrw import czi as pyczi

from . import czi_io, edge as edge_filter, overlay, scoring
from .config import Config
from .czi_io import SceneInfo
from .metadata import (LABEL_COLUMNS, build_aliases, section_skipped,
                       write_sections_template)
from .progress import Progress, scene_tile_count
from .threshold import ScoreHistogram, compute_threshold


# ---------------------------------------------------------------------------
# scan: detect sections, extract label + thumbnails, write metadata template
# ---------------------------------------------------------------------------
def scan(data_dir: str | Path, out_dir: str | Path, cfg: Config) -> Path:
    out_dir = Path(out_dir)
    thumbs_dir = out_dir / "thumbs"
    labels_dir = out_dir / "labels"

    files = czi_io.list_czi_files(data_dir)
    if not files:
        raise FileNotFoundError(f"no .czi files in {data_dir}")

    all_scenes: list[SceneInfo] = []
    thumb_rel: dict[str, str] = {}
    label_rel: dict[str, str] = {}

    for path in files:
        print(f"[scan] {path.name}")
        written: list = []
        label = czi_io.extract_label(path)
        if label is not None:
            lp = labels_dir / f"{path.stem}_label.png"
            overlay.save_rgb(lp, label)
            label_rel[path.stem] = str(lp.relative_to(out_dir).as_posix())
            written.append(lp)

        scenes = czi_io.list_scenes(path)
        all_scenes.extend(scenes)
        with pyczi.open_czi(str(path)) as doc:
            meta = czi_io.get_scan_metadata(doc.raw_metadata)
            for k in ("magnification", "objective", "pixel_size_um", "acquired",
                      "channels"):
                if meta.get(k):
                    unit = " µm/px" if k == "pixel_size_um" else ""
                    print(f"        {k}: {meta[k]}{unit}")
            px_um = scenes[0].pixel_size_um if scenes else None
            for s in scenes:
                ov, _ = czi_io.read_overview(doc, s, max_edge=512, zoom_cap=1.0)
                tp = thumbs_dir / f"{s.slug}.png"
                overlay.save_rgb(tp, ov)
                thumb_rel[s.slug] = str(tp.relative_to(out_dir).as_posix())
                written.append(tp)
                dims = f"{s.w}x{s.h}px"
                if px_um:
                    dims += f"  ({s.w * px_um / 1000:.1f}x{s.h * px_um / 1000:.1f} mm)"
                print(f"        scene {s.scene_index}: {dims}")
        if cfg.output.log_files:
            overlay.log_written(out_dir, written)

    csv = write_sections_template(all_scenes, out_dir / "sections.csv",
                                  thumb_rel, label_rel)
    if cfg.output.log_files:
        overlay.log_written(out_dir, [csv])
    print(f"[scan] {len(all_scenes)} sections -> {csv}")
    return csv


# ---------------------------------------------------------------------------
# analyze
# ---------------------------------------------------------------------------
def _score_ranges(primary: str) -> tuple[tuple[float, float], tuple[float, float]]:
    """Histogram (lo, hi) for the primary and secondary score, in that order."""
    dec = (-0.5, 2.5)
    opp = (-1.0, 1.0)
    return (opp, dec) if primary == "opponent" else (dec, opp)


def _primary_secondary(deconv: np.ndarray, opp: np.ndarray, primary: str):
    """Return (primary_score, secondary_score) for the configured primary."""
    return (opp, deconv) if primary == "opponent" else (deconv, opp)


def _overview_block(off, extent, scale, limit):
    a = int(round(off * scale))
    b = int(round((off + extent) * scale))
    a = max(0, min(a, limit))
    b = max(a, min(b, limit))
    return a, b


def _project(canvas, mask, off_x, off_y, tw, th, scale):
    """OR a full-res tile *mask* onto an overview *canvas* at *scale*."""
    h, w = canvas.shape
    bx0, bx1 = _overview_block(off_x, tw, scale, w)
    by0, by1 = _overview_block(off_y, th, scale, h)
    if bx1 <= bx0 or by1 <= by0:
        return
    block = cv2.resize(mask.astype(np.uint8), (bx1 - bx0, by1 - by0),
                       interpolation=cv2.INTER_AREA)
    canvas[by0:by1, bx0:bx1] |= (block > 0)


def analyze_scene(doc, scene: SceneInfo, cfg: Config, out_dir: Path,
                  alias: str | None = None, progress=None) -> dict:
    z = cfg.process_zoom
    alias = alias or scene.slug
    n_tiles = scene_tile_count(scene.w, scene.h, cfg.tile_size)
    if progress is not None:
        progress.start_section(alias, 2 * n_tiles)   # two passes per section

    def _advance(k=1):
        if progress is not None:
            progress.update(k)

    # --- overview + tissue region -----------------------------------------
    ov_rgb, scale = czi_io.read_overview(
        doc, scene, max_edge=cfg.overview_max_edge, zoom_cap=z,
        um_per_px=cfg.overview_um_per_px)
    from .tissue import (artifact_mask, clean_tissue_mask, erode_mask,
                         estimate_background, segment_tissue, tissue_mask)
    tcfg = cfg.scene_tissue(scene.key)   # tissue params (+ any per-scene overrides)
    # Per-scene glass colour (for pale/tinted backgrounds the fixed white test misses).
    bg = estimate_background(ov_rgb, tcfg)
    ov_tissue = segment_tissue(ov_rgb, tcfg)
    H, W = ov_tissue.shape
    # border erosion (edge halos) on the *counting* region only.
    ov_tissue_count = (erode_mask(ov_tissue, cfg.artifact.erode_px)
                       if cfg.artifact.enabled else ov_tissue)

    # higher-res canvas for overlays + maps (decoupled from the gating overview).
    hd_rgb, hd_scale = czi_io.read_overview(
        doc, scene, max_edge=cfg.maps_max_edge, zoom_cap=z,
        um_per_px=cfg.maps_um_per_px)
    Hh, Wh = hd_rgb.shape[:2]

    # stain matrix (optionally auto-estimated from this scene)
    conv = scoring.conv_matrix(cfg.detection.stain_matrix)
    if cfg.detection.auto_estimate:
        est = scoring.estimate_sabg_od(ov_rgb, ov_tissue)
        if est is not None:
            sm = scoring.build_stain_matrix(est, cfg.detection.stain_matrix[1])
            conv = scoring.conv_matrix(sm)

    if ov_tissue.sum() == 0:
        print(f"        {scene.key}: no tissue detected, skipping")
        _advance(2 * n_tiles)   # keep the overall bar accurate
        return _empty_row(scene, cfg, alias)

    primary = cfg.detection.primary
    agree = cfg.detection.require_agreement
    ov_fold = np.zeros((H, W), bool)         # filled in after the threshold pass

    def tile_masks(rgb, off_x, off_y, tw, th):
        """Per-tile masks. Returns (t_count, artifact, fold, opp): *t_count* is
        countable tissue (region ∩ raw tissue, minus artifacts/eroded border and,
        when excluded, the fold band); *artifact* is the dark fold/debris mask;
        *fold* is the linear-fold band within tissue (disjoint from artifact)."""
        ox0, ox1 = _overview_block(off_x, tw, scale, W)
        oy0, oy1 = _overview_block(off_y, th, scale, H)
        if ox1 <= ox0 or oy1 <= oy0:
            return None, None, None, None

        def _resize(src):
            crop = src[oy0:oy1, ox0:ox1].astype(np.uint8)
            return cv2.resize(crop, (rgb.shape[1], rgb.shape[0]),
                              interpolation=cv2.INTER_NEAREST).astype(bool)

        region = _resize(ov_tissue)            # coarse blob (non-eroded), adaptive
        region_c = _resize(ov_tissue_count)    # eroded blob (for counting)
        raw = tissue_mask(rgb, tcfg)           # fine gap/white-glass within the region
        opp = scoring.opponent_score(rgb)
        art = (artifact_mask(rgb, opp, cfg.artifact) & region & raw
               if cfg.artifact.enabled else np.zeros(raw.shape, bool))
        fold = (_resize(ov_fold) & region & raw & ~art
                if cfg.fold.enabled else np.zeros(raw.shape, bool))
        t = region_c & raw & ~art
        return t, art, fold, opp

    # --- pass 1: thresholds for both scores over countable tissue ---------
    thr_override = cfg.scene_threshold(scene.key)
    (lo_p, hi_p), (lo_s, hi_s) = _score_ranges(primary)
    need_primary = thr_override is None
    need_secondary = agree

    def _thr(values, lo, hi):
        h = ScoreHistogram(lo, hi)
        h.add(values)
        return compute_threshold(h, cfg.threshold)

    # Overview score estimate, shared by the from_overview threshold and the
    # fold detector (both need an overview-resolution positive estimate).
    need_ov = cfg.threshold.from_overview or cfg.fold.enabled
    if need_ov:
        opp_o = scoring.opponent_score(ov_rgb)
        dec_o = scoring.deconvolution_score(ov_rgb, conv)
        art_o = (artifact_mask(ov_rgb, opp_o, cfg.artifact) & ov_tissue
                 if cfg.artifact.enabled else np.zeros(ov_tissue.shape, bool))
        t_o = ov_tissue_count & ~art_o
        p_o, s_o = _primary_secondary(dec_o, opp_o, primary)

    if cfg.threshold.from_overview:
        # Fast path: derive thresholds from the in-memory overview, skip pass 1.
        thr = thr_override if thr_override is not None else _thr(p_o[t_o], lo_p, hi_p)
        thr_s = _thr(s_o[t_o], lo_s, hi_s) if need_secondary else None
        _advance(n_tiles)                 # pass 1 skipped
    elif need_primary or need_secondary:
        hist_p = ScoreHistogram(lo_p, hi_p) if need_primary else None
        hist_s = ScoreHistogram(lo_s, hi_s) if need_secondary else None
        for rgb, ox, oy, tw, th in czi_io.iter_tiles(doc, scene, cfg.tile_size, z):
            t, _art, _fold, opp = tile_masks(rgb, ox, oy, tw, th)
            if t is not None and t.any():
                deconv = scoring.deconvolution_score(rgb, conv)
                p_s, s_s = _primary_secondary(deconv, opp, primary)
                if hist_p is not None:
                    hist_p.add(p_s[t])
                if hist_s is not None:
                    hist_s.add(s_s[t])
            _advance()
        thr = thr_override if thr_override is not None else compute_threshold(hist_p, cfg.threshold)
        thr_s = compute_threshold(hist_s, cfg.threshold) if hist_s is not None else None
    else:
        thr = thr_override
        thr_s = None
        _advance(n_tiles)

    # --- fold pre-pass: flag thin linear ridges (folds) -------------------
    if cfg.fold.enabled:
        from .fold import detect_folds, fold_density_map
        ov_um_per_px = (scene.pixel_size_um / scale
                        if scene.pixel_size_um and scale else None)
        if cfg.fold.source == "sabg":   # legacy: ridges of the SABG+ density
            signal = (t_o & (p_o >= thr)).astype(np.float32)
            if agree and thr_s is not None:
                signal *= (s_o >= thr_s)
        else:                           # density: ridges of tissue optical-density excess
            signal = fold_density_map(ov_rgb, ov_tissue, ov_um_per_px, cfg.fold)
        ov_fold = detect_folds(signal, ov_um_per_px, ov_tissue, cfg.fold)
        if cfg.fold.exclude_from_tissue:
            ov_tissue_count = ov_tissue_count & ~ov_fold

    # --- pass 2: count + project positives/artifacts ----------------------
    # Rejected SABG+ inside the fold band is tracked separately so the overlay can
    # still show it (green on top of the orange band) while it stays out of the count.
    # Rejected SABG+ inside the fold band, tracked for the section/overlay figures
    # (independent of output.overlay so `export` can paint it on the section figure).
    want_fold_pos = cfg.fold.enabled and cfg.overlay.fold_show_sabg
    edge_on = cfg.edge.enabled
    px_um_proc = (scene.pixel_size_um / z) if scene.pixel_size_um else None
    expand_px = max(0, int(cfg.detection.expand_px))
    expand_teal_min = cfg.detection.expand_teal_min
    expand_k = (cv2.getStructuringElement(
        cv2.MORPH_ELLIPSE, (2 * expand_px + 1, 2 * expand_px + 1))
        if expand_px else None)
    ov_pos = np.zeros((H, W), bool)
    ov_art = np.zeros((H, W), bool)
    ov_edge = np.zeros((H, W), bool)
    hd_pos = np.zeros((Hh, Wh), bool)
    hd_art = np.zeros((Hh, Wh), bool)
    hd_pos_fold = np.zeros((Hh, Wh), bool)
    hd_edge = np.zeros((Hh, Wh), bool)
    tissue_px = 0
    positive_px = 0
    artifact_px = 0
    fold_px = 0
    edge_px = 0
    for rgb, ox, oy, tw, th in czi_io.iter_tiles(doc, scene, cfg.tile_size, z):
        t, art, fold, opp = tile_masks(rgb, ox, oy, tw, th)
        _advance()
        if t is None:
            continue
        if art is not None and art.any():
            artifact_px += int(art.sum())
            _project(ov_art, art, ox, oy, tw, th, scale)
            _project(hd_art, art, ox, oy, tw, th, hd_scale)
        fold_hot = fold is not None and bool(fold.any())
        if fold_hot:
            fold_px += int(fold.sum())
        if not t.any() and not (want_fold_pos and fold_hot):
            continue
        deconv = scoring.deconvolution_score(rgb, conv)
        p_s, s_s = _primary_secondary(deconv, opp, primary)
        hot = (p_s >= thr)
        if agree and thr_s is not None:
            hot &= (s_s >= thr_s)
        if t.any():
            pos = t & hot
            if cfg.fold.enabled and not cfg.fold.exclude_from_tissue and fold is not None:
                pos &= ~fold              # numerator-only: drop fold positives
            if edge_on:                   # drop thin edge-shadow rims (numerator only)
                pos, removed = edge_filter.refine_positive(
                    pos, rgb, px_um_proc, cfg.edge)
                if removed.any():
                    edge_px += int(removed.sum())
                    _project(ov_edge, removed, ox, oy, tw, th, scale)
                    _project(hd_edge, removed, ox, oy, tw, th, hd_scale)
            if expand_k is not None and pos.any():   # grow kept positives into nearby teal
                grown = cv2.dilate(pos.astype(np.uint8), expand_k).astype(bool) & t
                if expand_teal_min > 0:               # don't grow into achromatic edges/tissue
                    grown &= pos | (opp >= expand_teal_min)
                pos = grown
            tissue_px += int(t.sum())
            positive_px += int(pos.sum())
            if pos.any():
                _project(ov_pos, pos, ox, oy, tw, th, scale)
                _project(hd_pos, pos, ox, oy, tw, th, hd_scale)
        if want_fold_pos and fold_hot:
            pos_fold = fold & hot         # SABG+ pixels excluded by the fold band
            if edge_on:
                pos_fold, _ = edge_filter.refine_positive(
                    pos_fold, rgb, px_um_proc, cfg.edge)
            if pos_fold.any():
                _project(hd_pos_fold, pos_fold, ox, oy, tw, th, hd_scale)

    pct = 100.0 * positive_px / tissue_px if tissue_px else 0.0

    # --- areas in mm^2 (processed-pixel size) -----------------------------
    px_um = (scene.pixel_size_um / z) if scene.pixel_size_um else None
    if px_um:
        mm2 = (px_um / 1000.0) ** 2
        tissue_mm2 = tissue_px * mm2
        sabg_mm2 = positive_px * mm2
        artifact_mm2 = artifact_px * mm2
        fold_mm2 = fold_px * mm2
        edge_mm2 = edge_px * mm2
    else:
        tissue_mm2 = sabg_mm2 = artifact_mm2 = fold_mm2 = edge_mm2 = None

    # HD fold band (overview mask upsampled) for overlay / maps.
    hd_fold = (cv2.resize(ov_fold.astype(np.uint8), (Wh, Hh),
                          interpolation=cv2.INTER_NEAREST).astype(bool)
               if cfg.fold.enabled else np.zeros((Hh, Wh), bool))

    # HD tissue for the maps (export derives the grey non-tissue shade per figure).
    hd_tissue = segment_tissue(hd_rgb, tcfg)

    # --- overview maps (higher-res; consumed by `export`) -----------------
    written: list = []
    if cfg.output.maps:
        maps_dir = out_dir / "maps"
        mp = maps_dir / f"{alias}_overview.jpg"
        overlay.save_jpg(mp, hd_rgb, quality=90); written.append(mp)
        # tissue map for `export` FOV selection: tissue minus flagged artifacts/folds.
        for name, arr in (("tissue", hd_tissue & ~hd_art & ~hd_fold),
                          ("pos", hd_pos), ("artifact", hd_art)):
            mp = maps_dir / f"{alias}_{name}.png"
            cv2.imwrite(str(mp), arr.astype(np.uint8) * 255); written.append(mp)
        if cfg.fold.enabled:
            mp = maps_dir / f"{alias}_fold.png"
            cv2.imwrite(str(mp), hd_fold.astype(np.uint8) * 255); written.append(mp)
        if want_fold_pos:
            mp = maps_dir / f"{alias}_pos_fold.png"
            cv2.imwrite(str(mp), hd_pos_fold.astype(np.uint8) * 255); written.append(mp)
        if cfg.edge.enabled:
            mp = maps_dir / f"{alias}_edge.png"
            cv2.imwrite(str(mp), hd_edge.astype(np.uint8) * 255); written.append(mp)

    # The whole-section QC overlay now lives in sections/ (rendered by `export` from
    # the maps written above), so analyze no longer writes a standalone overlays/.

    if cfg.output.debug:
        opp_ov = scoring.opponent_score(ov_rgb)
        dec_ov = scoring.deconvolution_score(ov_rgb, conv)
        overlay.save_debug_panels(
            out_dir / "debug", alias, ov_rgb, ov_tissue,
            opp_ov, dec_ov, ov_pos, ov_art, thr, cfg.detection.primary,
            fold_mask=ov_fold, edge_mask=ov_edge, full=cfg.full_debug)
        written.append(out_dir / "debug" / f"{alias}_compare.jpg")

    print(f"        {scene.key} [{alias}]: {pct:.2f}% SABG  "
          f"(thr={thr:.3f}, tissue={tissue_px:,}px, "
          f"artifact={artifact_px:,}px, fold={fold_px:,}px, edge={edge_px:,}px)")
    if cfg.output.log_files:
        overlay.log_written(out_dir, written)

    return {
        "file": scene.file_stem, "scene": scene.scene_index, "key": scene.key,
        "alias": alias,
        "pct_sabg": round(pct, 4),
        "tissue_px": tissue_px, "positive_px": positive_px,
        "artifact_px": artifact_px, "fold_px": fold_px, "edge_px": edge_px,
        "tissue_area_mm2": tissue_mm2, "sabg_area_mm2": sabg_mm2,
        "artifact_area_mm2": artifact_mm2, "fold_area_mm2": fold_mm2,
        "edge_area_mm2": edge_mm2,
        "threshold": round(float(thr), 5),
        "threshold_secondary": (round(float(thr_s), 5) if thr_s is not None else None),
        "threshold_method": "override" if thr_override is not None else cfg.threshold.method,
        "require_agreement": agree,
        "primary": cfg.detection.primary,
        "pixel_size_um": scene.pixel_size_um, "process_zoom": z,
    }


def _empty_row(scene: SceneInfo, cfg: Config, alias: str | None = None) -> dict:
    return {
        "file": scene.file_stem, "scene": scene.scene_index, "key": scene.key,
        "alias": alias or scene.slug,
        "pct_sabg": 0.0, "tissue_px": 0, "positive_px": 0,
        "artifact_px": 0, "fold_px": 0, "edge_px": 0,
        "tissue_area_mm2": 0.0, "sabg_area_mm2": 0.0,
        "artifact_area_mm2": 0.0, "fold_area_mm2": 0.0, "edge_area_mm2": 0.0,
        "threshold": None, "threshold_secondary": None,
        "threshold_method": cfg.threshold.method,
        "require_agreement": cfg.detection.require_agreement,
        "primary": cfg.detection.primary,
        "pixel_size_um": scene.pixel_size_um, "process_zoom": cfg.process_zoom,
    }


def analyze(
    data_dir: str | Path,
    out_dir: str | Path,
    cfg: Config,
    metadata: dict | None = None,
    only_scene: str | None = None,
    show_progress: bool | None = None,
    continue_run: bool = False,
) -> Path:
    import time
    t_start = time.perf_counter()
    out_dir = Path(out_dir)
    files = czi_io.list_czi_files(data_dir)
    if not files:
        raise FileNotFoundError(f"no .czi files in {data_dir}")

    # Resume: keep the rows already in results.csv and skip those scenes.
    prior_rows: list[dict] = []
    done_keys: set[str] = set()
    if continue_run:
        prev = out_dir / "results.csv"
        if prev.exists():
            from .metadata import _read_table
            pdf = _read_table(prev)
            if "key" in pdf.columns:
                prior_rows = pdf.to_dict("records")
                done_keys = {str(r["key"]) for r in prior_rows}
                print(f"[analyze] continue: {len(done_keys)} section(s) already done, skipping")

    # Build the work list first so the ETA spans the whole run, not one scene.
    # Skip sections the config excludes or whose `analyze` cell says "no".
    plan: list[tuple[Path, SceneInfo]] = []
    for path in files:
        for s in czi_io.list_scenes(path):
            if only_scene is not None and s.key != only_scene:
                continue
            if s.key in done_keys:
                continue
            if cfg.scene_skipped(s.key):
                continue
            md = metadata.get((s.file_stem, s.scene_index)) if metadata else None
            if section_skipped(md):
                print(f"[analyze] skip {s.key} (sections.csv analyze=no)")
                continue
            plan.append((path, s))

    # Short alias per section from the metadata (used in results + filenames).
    aliases = build_aliases([s for _, s in plan], metadata, cfg.alias)

    if show_progress is None:
        show_progress = sys.stdout.isatty()
    total_tiles = 2 * sum(
        scene_tile_count(s.w, s.h, cfg.tile_size) for _, s in plan)
    bar = Progress(total_tiles, enabled=show_progress, params=cfg.progress)

    # Group planned scenes by file so each CZI is opened once.
    by_file: dict[Path, list[SceneInfo]] = {}
    for path, s in plan:
        by_file.setdefault(path, []).append(s)

    rows: list[dict] = []
    results = out_dir / "results.csv"
    results.parent.mkdir(parents=True, exist_ok=True)

    def _checkpoint() -> None:
        # Persist after each section so a Stop (or crash) keeps finished work;
        # Resume (analyze --continue) then skips these and finishes the rest.
        _safe_to_csv(pd.DataFrame(prior_rows + rows), results)
        _write_config_snapshot(out_dir / "config.yaml", cfg, rows)

    try:
        for path, scenes in by_file.items():
            print(f"[analyze] {path.name}")
            with pyczi.open_czi(str(path)) as doc:
                for s in scenes:
                    row = analyze_scene(doc, s, cfg, out_dir,
                                        alias=aliases.get(s.key), progress=bar)
                    if metadata:
                        md = metadata.get((s.file_stem, s.scene_index), {})
                        for c in LABEL_COLUMNS:
                            row[c] = md.get(c, "")
                    rows.append(row)
                    _checkpoint()
    finally:
        bar.close()

    results = _safe_to_csv(pd.DataFrame(prior_rows + rows), results)
    _write_config_snapshot(out_dir / "config.yaml", cfg, rows)
    if cfg.output.log_files:
        overlay.log_written(out_dir, [results, out_dir / "config.yaml"])
    analyze_secs = time.perf_counter() - t_start
    from .progress import _fmt
    print(f"[analyze] {len(rows)} sections -> {results}  (analysis {_fmt(analyze_secs)})")

    # Bundled export: one timed pipeline (analyze + figures). With export_on_analyze
    # off we still render the section overlays (do_fov=False) so there's always an
    # overlay; on, the per-FOV crops are produced too.
    if cfg.output.maps and (out_dir / "maps").exists():
        from .export import build_params, export as _run_export
        t_exp = time.perf_counter()
        try:
            _run_export(data_dir, out_dir, build_params(cfg), cfg,
                        only_scene=only_scene, metadata=metadata,
                        do_fov=cfg.output.export_on_analyze, resume=continue_run)
            print(f"[export] done  (export {_fmt(time.perf_counter() - t_exp)})")
        except Exception as exc:                      # don't lose results on a figure error
            print(f"  ! export failed: {exc}")
    else:
        print("[export] skipped (no maps/ to render from)")
    print(f"[total] analyze + export in {_fmt(time.perf_counter() - t_start)}")
    return results


def _safe_to_csv(df: pd.DataFrame, path: Path) -> Path:
    """Write CSV; if the target is locked (open in Excel), use a fallback name."""
    try:
        df.to_csv(path, index=False)
        return path
    except PermissionError:
        alt = path.with_name(path.stem + "_new.csv")
        df.to_csv(alt, index=False)
        print(f"  ! {path.name} is locked (open elsewhere?) -> wrote {alt.name} instead")
        return alt


def _write_config_snapshot(path: Path, cfg: Config, rows: list[dict]) -> None:
    """Persist the thresholds used, so the user can edit + re-run."""
    scenes = dict(cfg.scenes)
    for r in rows:
        if r["threshold"] is None:
            continue
        entry = dict(scenes.get(r["key"], {}) or {})
        entry.setdefault("threshold", r["threshold"])
        scenes[r["key"]] = entry

    snapshot = {
        "process_zoom": cfg.process_zoom,
        "tile_size": cfg.tile_size,
        "overview_um_per_px": cfg.overview_um_per_px,
        "overview_max_edge": cfg.overview_max_edge,
        "maps_um_per_px": cfg.maps_um_per_px,
        "maps_max_edge": cfg.maps_max_edge,
        "tissue": {"gap_level": cfg.tissue.gap_level,
                   "white_level": cfg.tissue.white_level,
                   "sat_min": cfg.tissue.sat_min,
                   "min_object_px": cfg.tissue.min_object_px,
                   "fill_holes": cfg.tissue.fill_holes,
                   "adaptive": cfg.tissue.adaptive,
                   "bg_margin": cfg.tissue.bg_margin,
                   "bg_bright_quantile": cfg.tissue.bg_bright_quantile,
                   "bg_teal_guard": cfg.tissue.bg_teal_guard,
                   "texture_min": cfg.tissue.texture_min,
                   "texture_win": cfg.tissue.texture_win,
                   "close_px": cfg.tissue.close_px,
                   "fill_holes_max_frac": cfg.tissue.fill_holes_max_frac,
                   "bg_max_tissue_frac": cfg.tissue.bg_max_tissue_frac},
        "artifact": {"enabled": cfg.artifact.enabled,
                     "dark_level": cfg.artifact.dark_level,
                     "teal_min": cfg.artifact.teal_min,
                     "erode_px": cfg.artifact.erode_px,
                     "min_object_px": cfg.artifact.min_object_px},
        "edge": {"enabled": cfg.edge.enabled,
                 "morph_open": cfg.edge.morph_open,
                 "min_width_um": cfg.edge.min_width_um,
                 "reject_shadow": cfg.edge.reject_shadow,
                 "shadow_dark_level": cfg.edge.shadow_dark_level,
                 "shadow_sat_min": cfg.edge.shadow_sat_min,
                 "teal_keep": cfg.edge.teal_keep},
        "fold": {"enabled": cfg.fold.enabled,
                 "source": cfg.fold.source,
                 "hp_um": cfg.fold.hp_um,
                 "border_um": cfg.fold.border_um,
                 "combine": cfg.fold.combine,
                 "smooth_um": cfg.fold.smooth_um,
                 "min_length_um": cfg.fold.min_length_um,
                 "max_width_um": cfg.fold.max_width_um,
                 "min_aspect": cfg.fold.min_aspect,
                 "ecc_min": cfg.fold.ecc_min,
                 "band_width_um": cfg.fold.band_width_um,
                 "ridge_min": cfg.fold.ridge_min,
                 "coherence_min": cfg.fold.coherence_min,
                 "score_min": cfg.fold.score_min,
                 "exclude_from_tissue": cfg.fold.exclude_from_tissue},
        "detection": {"primary": cfg.detection.primary,
                      "auto_estimate": cfg.detection.auto_estimate,
                      "require_agreement": cfg.detection.require_agreement,
                      "expand_px": cfg.detection.expand_px,
                      "expand_teal_min": cfg.detection.expand_teal_min},
        "threshold": {"method": cfg.threshold.method,
                      "percentile": cfg.threshold.percentile,
                      "min_score": cfg.threshold.min_score,
                      "scale": cfg.threshold.scale,
                      "from_overview": cfg.threshold.from_overview},
        "overlay": {"sabg_color": list(cfg.overlay.sabg_color),
                    "sabg_alpha": cfg.overlay.sabg_alpha,
                    "artifact_color": list(cfg.overlay.artifact_color),
                    "artifact_alpha": cfg.overlay.artifact_alpha,
                    "fold_color": list(cfg.overlay.fold_color),
                    "fold_alpha": cfg.overlay.fold_alpha,
                    "fold_show_sabg": cfg.overlay.fold_show_sabg,
                    "edge_color": list(cfg.overlay.edge_color),
                    "edge_alpha": cfg.overlay.edge_alpha,
                    "show_edge_rejected": cfg.overlay.show_edge_rejected,
                    "nontissue_color": list(cfg.overlay.nontissue_color),
                    "nontissue_alpha": cfg.overlay.nontissue_alpha,
                    "show_nontissue": cfg.overlay.show_nontissue},
        "output": {"debug": cfg.output.debug,
                   "maps": cfg.output.maps,
                   "keep_maps": cfg.output.keep_maps,
                   "export_on_analyze": cfg.output.export_on_analyze,
                   "log_files": cfg.output.log_files,
                   "run_log": cfg.output.run_log,
                   "run_log_name": cfg.output.run_log_name},
        "progress": {"section": cfg.progress.section,
                     "total": cfg.progress.total,
                     "elapsed": cfg.progress.elapsed,
                     "eta": cfg.progress.eta,
                     "checkpoints": list(cfg.progress.checkpoints)},
        "gui": {"info_opens": list(cfg.gui.info_opens)},
        "alias": {"fields": list(cfg.alias.fields),
                  "optional": list(cfg.alias.optional),
                  "spacer": cfg.alias.spacer,
                  "tag_field": cfg.alias.tag_field},
        "export": _export_snapshot(cfg.export),
        "scenes": scenes,
    }
    path.write_text(yaml.safe_dump(snapshot, sort_keys=False), encoding="utf-8")


def _export_snapshot(overrides: dict) -> dict:
    """Effective `export` defaults (built-ins merged with any config overrides),
    so the written config.yaml always shows the full, editable export matrix."""
    from .export import ExportParams
    d = ExportParams()
    base = {"fov_um": d.fov_um, "scalebar_um": d.scalebar_um,
            "scalebar_label": d.scalebar_label, "n_fov": d.n_fov,
            "min_tissue_frac": d.min_tissue_frac, "wb": d.wb, "raw": d.raw,
            "plain": d.plain, "qc_overlay": d.qc_overlay,
            "qc_bases": list(d.qc_bases), "formats": list(d.formats),
            "section_figures": d.section_figures, "sec_variants": list(d.sec_variants),
            "sec_formats": list(d.sec_formats), "section_um_per_px": d.section_um_per_px,
            "section_show_edge": d.section_show_edge,
            "sec_scalebar_um": d.sec_scalebar_um,
            "sec_scalebar_adaptive": d.sec_scalebar_adaptive,
            "sec_scalebar_label": d.sec_scalebar_label,
            "box_color": list(d.box_color), "box_thickness": d.box_thickness,
            "box_dash": d.box_dash, "box_label": d.box_label,
            "box_label_margin": d.box_label_margin}
    base.update(overrides or {})
    return base
