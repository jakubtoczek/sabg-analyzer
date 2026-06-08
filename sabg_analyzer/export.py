"""Publication figure export.

Selects a few *clean, representative* full-resolution FOVs per section, applies
optional white balance, burns in a scale bar, and writes PNG + TIFF.

"Representative" here means (per the user): almost entirely tissue (no mosaic
gaps / glass / edges), artifact-light (no dark folds or debris), and with a
local %SABG close to the section's global %SABG — i.e. typical of the staining,
not the richest hotspot.

Consumes the small overview maps written by `analyze` (maps/<slug>_*), so run
`analyze` first.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

import cv2
import numpy as np
import pandas as pd
from pylibCZIrw import czi as pyczi

from . import czi_io, overlay, scoring, whitebalance
from .czi_io import SceneInfo
from .metadata import _sniff_sep, build_aliases, load_metadata


def build_params(cfg, **overrides) -> "ExportParams":
    """Build :class:`ExportParams` from ``cfg.export`` (the YAML ``export:`` block),
    with optional explicit *overrides* (e.g. CLI flags). Keys absent everywhere fall
    back to the dataclass defaults; ``None`` overrides are ignored so a missing CLI
    flag defers to the config. List values for tuple fields are coerced to tuples."""
    d = ExportParams()
    merged = dict(cfg.export or {})
    merged.update({k: v for k, v in overrides.items() if v is not None})
    kwargs = {}
    for f in fields(ExportParams):
        if f.name not in merged:
            continue
        val = merged[f.name]
        if isinstance(getattr(d, f.name), tuple) and isinstance(val, (list, tuple)):
            val = tuple(val)
        kwargs[f.name] = val
    return ExportParams(**kwargs)


@dataclass
class ExportParams:
    fov_um: float = 500.0
    scalebar_um: float = 100.0
    scalebar_label: bool = False   # draw the "100 µm" text above the bar
    n_fov: int = 5
    min_tissue_frac: float = 0.85
    # Which base figures to write (two colour renderings of each FOV):
    wb: bool = True                # white-balanced (background neutralised)
    raw: bool = True               # original colours
    # For each base, which variants:
    plain: bool = True             # clean image, no overlay
    qc_overlay: bool = True        # SABG+/artifact overlay burned in (green/red)
    qc_bases: tuple[str, ...] = ("wb",)  # which bases get a qc copy (default: wb only)
    # Output file formats. JPEG by default (FOV crops are full-res; JPEG ~10x smaller).
    formats: tuple[str, ...] = ("jpg",)
    # Whole-section figures (downsampled, written to sections/<alias>_<variant>).
    section_figures: bool = True
    sec_variants: tuple[str, ...] = ("raw", "wb_scalebar", "wb_overlay_fov_scalebar")
    sec_formats: tuple[str, ...] = ("jpg",)
    section_um_per_px: float = 3.0  # section figure resolution (match maps_um_per_px)
    section_show_edge: bool = False  # draw blue edge-rejected pixels on section figures
    # Section scale bar (the `scalebar` variant token). Default ON + labelled, ~1 mm,
    # adaptive (snaps to a nice length near the target, <=~40% of the figure width).
    sec_scalebar_um: float = 1000.0   # target bar length (µm); 1000 = 1 mm
    sec_scalebar_adaptive: bool = True
    sec_scalebar_label: bool = True   # draw the "1 mm" text (same Arial font as FOVs)
    box_color: tuple[int, int, int] = (0, 0, 0)  # FOV box + label colour
    box_thickness: int = 2
    box_dash: int = 14             # dashed-segment length (px); gap = same
    box_label: bool = True         # draw 1,2,... at the box's top-left
    box_label_margin: int = 6      # small gap to the left of the label


# ---------------------------------------------------------------------------
# Scale bar
# ---------------------------------------------------------------------------
def draw_scalebar(img: np.ndarray, pixel_size_um: float, bar_um: float,
                  color=(0, 0, 0), label: bool = True) -> np.ndarray:
    """Burn a filled scale bar into the bottom-right corner.

    With *label* (default) the "<bar_um> µm" text is drawn above the bar;
    set it False for a bare bar.
    """
    out = img.copy()
    h, w = out.shape[:2]
    bar_px = int(round(bar_um / pixel_size_um))
    bar_px = min(bar_px, int(w * 0.8))
    bar_h = max(3, int(round(h * 0.012)))
    m = int(round(min(h, w) * 0.05))
    x2, y2 = w - m, h - m
    x1, y1 = x2 - bar_px, y2 - bar_h

    # white plate behind for contrast (taller when a label sits above the bar)
    plate_top = y1 - (int(h * 0.035) if label else 0)
    cv2.rectangle(out, (x1 - 6, plate_top - 6), (x2 + 6, y2 + 6),
                  (255, 255, 255), -1)
    cv2.rectangle(out, (x1, y1), (x2, y2), color, -1)
    if label:
        _put_label(out, _fmt_bar(bar_um), (x1, y1 - 4), color)
    return out


def _fmt_bar(bar_um: float) -> str:
    """Scale-bar label: millimetres for >=1 mm, else micrometres."""
    if bar_um >= 1000:
        return f"{bar_um / 1000:g} mm"
    return f"{bar_um:g} µm"


def _put_label(img, text, org, color):
    """Draw text with proper 'µm' via PIL if available, else cv2 ('um')."""
    try:
        from PIL import Image, ImageDraw, ImageFont
        pil = Image.fromarray(img)
        d = ImageDraw.Draw(pil)
        try:
            font = ImageFont.truetype("arial.ttf", max(14, int(img.shape[0] * 0.025)))
        except Exception:
            font = ImageFont.load_default()
        d.text((org[0], org[1] - int(img.shape[0] * 0.03)), text,
               fill=tuple(int(c) for c in color), font=font)
        img[:] = np.asarray(pil)
    except Exception:
        cv2.putText(img, text.replace("µm", "um"), org,
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 1, cv2.LINE_AA)


def draw_dashed_rect(img, p1, p2, color, thickness=2, dash=14) -> None:
    """Draw a dashed rectangle (corners *p1*, *p2*) in place."""
    x1, y1 = p1
    x2, y2 = p2
    col = tuple(int(c) for c in color)

    def _line(a, b):
        a = np.array(a, float)
        b = np.array(b, float)
        length = float(np.hypot(*(b - a)))
        if length < 1:
            return
        n = max(1, int(length // dash))
        for i in range(0, n, 2):
            s = a + (b - a) * (i / n)
            e = a + (b - a) * (min(i + 1, n) / n)
            cv2.line(img, (int(s[0]), int(s[1])), (int(e[0]), int(e[1])),
                     col, thickness, cv2.LINE_AA)

    _line((x1, y1), (x2, y1)); _line((x2, y1), (x2, y2))
    _line((x2, y2), (x1, y2)); _line((x1, y2), (x1, y1))


# Overlay layer-set "profiles" selectable per section variant (later layers paint
# on top). A variant whose tokens include one of these keys draws that layer set.
OVERLAY_PROFILES = {
    "overlay": ["nontissue", "artifact", "fold", "edge", "pos", "pos_fold"],
    "overlaysabg": ["pos", "pos_fold"],   # SABG positives only (incl. inside folds)
}


def adaptive_bar_um(width_px: int, um_per_px: float, target_um: float = 1000.0) -> float:
    """Pick a 'nice' scale-bar length near *target_um* but at most ~40% of the
    figure width. Falls back to the smallest nice value for tiny figures."""
    nice = [50, 100, 200, 250, 500, 1000, 2000, 5000, 10000]
    max_um = 0.4 * width_px * um_per_px
    cand = [n for n in nice if n <= max_um] or [nice[0]]
    return float(min(cand, key=lambda n: abs(n - target_um)))


def _section_figures(out_dir: Path, alias: str, doc, scene, ov_rgb, ov_tissue,
                     ov_pos, maps_dir: Path, fovs, fov_ov: float, p: ExportParams,
                     cfg) -> list[Path]:
    """Write whole-section figures to ``sections/`` as ``{alias}_{variant}`` (no
    ``section_`` token), sized by the fixed magnification ``section_um_per_px`` so
    every figure is proportional to its source section.

    Each ``p.sec_variants`` entry is a set of underscore-joined tokens:
      base      ``raw`` | ``wb``
      overlay   ``overlay`` (all layers) | ``overlaysabg`` (SABG only)
      ``fov``       numbered dashed FOV boxes
      ``scalebar``  burned-in adaptive scale bar (~1 mm, labelled by default)
    Legacy names (``wb``, ``wb_overlay``, ``wb_overlay_fov``) still parse.
    """
    variants = list(p.sec_variants)
    if not variants:
        return []
    sec_dir = out_dir / "sections"
    formats = p.sec_formats or p.formats
    Hm, Wm = ov_tissue.shape

    # Fixed-magnification base read from the CZI (fall back to the maps overview).
    base = ov_rgb
    if scene.pixel_size_um:
        sec_zoom = min(1.0, scene.pixel_size_um / max(p.section_um_per_px, 1e-6))
        try:
            base = czi_io.read_region(doc, scene.x, scene.y, scene.w, scene.h, zoom=sec_zoom)
        except Exception:
            base = ov_rgb
    secH, secW = base.shape[:2]
    rs = secW / Wm                       # maps-overview px -> section px
    sec_um_per_px = ((scene.w / secW) * scene.pixel_size_um
                     if scene.pixel_size_um else None)   # actual figure µm/px

    def _rz(mask):
        if mask is None:
            return None
        return cv2.resize(mask.astype(np.uint8), (secW, secH),
                          interpolation=cv2.INTER_NEAREST).astype(bool)

    def _m(name):
        m = _load_map(maps_dir, alias, name)
        return None if m is None else _rz(m > 0)

    tissue = _rz(ov_tissue); pos = _rz(ov_pos)
    art = _m("artifact"); fold = _m("fold"); edge = _m("edge"); pos_fold = _m("pos_fold")
    nontissue = (~tissue) & (base.max(axis=2) > cfg.tissue.gap_level)

    layer_specs = {
        "nontissue": (nontissue, cfg.overlay.nontissue_color, cfg.overlay.nontissue_alpha),
        "artifact": (art, cfg.overlay.artifact_color, cfg.overlay.artifact_alpha),
        "fold": (fold, cfg.overlay.fold_color, cfg.overlay.fold_alpha),
        "edge": (edge, cfg.overlay.edge_color, cfg.overlay.edge_alpha),
        "pos": (pos, cfg.overlay.sabg_color, cfg.overlay.sabg_alpha),
        "pos_fold": (pos_fold, cfg.overlay.sabg_color, cfg.overlay.sabg_alpha),
    }
    gates = {   # layers conditionally drawn even when listed in a profile
        "nontissue": cfg.overlay.show_nontissue,
        "edge": p.section_show_edge and cfg.edge.enabled,
        "pos_fold": cfg.overlay.fold_show_sabg,
    }

    def _profile_layers(profile):
        out = []
        for name in OVERLAY_PROFILES.get(profile, OVERLAY_PROFILES["overlay"]):
            spec = layer_specs.get(name)
            if not gates.get(name, True) or spec is None or spec[0] is None:
                continue
            out.append(spec)
        return out

    cache: dict[str, np.ndarray] = {}

    def _wb():
        if "wb" not in cache:
            cache["wb"] = whitebalance.white_balance(
                base, whitebalance.estimate_white_point(base))
        return cache["wb"]

    def _boxes(img):
        out = img.copy()
        half = int(round(fov_ov * rs / 2))
        th = max(1, int(round(p.box_thickness * rs)))
        dash = max(4, int(round(p.box_dash * rs)))
        for i, fov in enumerate(fovs):
            cx, cy = int(fov["cx"] * rs), int(fov["cy"] * rs)
            draw_dashed_rect(out, (cx - half, cy - half), (cx + half, cy + half),
                             p.box_color, th, dash)
            if p.box_label:
                _put_label(out, str(i + 1),
                           (cx - half + p.box_label_margin, cy - half), p.box_color)
        return out

    written: list = []
    for variant in variants:
        toks = variant.split("_")
        img = _wb() if "wb" in toks else base
        profile = next((k for k in OVERLAY_PROFILES if k in toks), None)
        if profile:
            img = overlay.composite_overlay(img, _profile_layers(profile))
        if "fov" in toks:
            img = _boxes(img)
        if "scalebar" in toks and sec_um_per_px:
            bar_um = (adaptive_bar_um(secW, sec_um_per_px, p.sec_scalebar_um)
                      if p.sec_scalebar_adaptive else p.sec_scalebar_um)
            img = draw_scalebar(img, sec_um_per_px, bar_um, color=p.box_color,
                                label=p.sec_scalebar_label)
        written += _save(img, sec_dir / f"{alias}_{variant}", formats)
    return written


# ---------------------------------------------------------------------------
# FOV selection (overview resolution)
# ---------------------------------------------------------------------------
def select_fovs(ov_rgb, ov_tissue, ov_pos, global_pct: float, n: int,
                fov_ov: float, min_tissue: float = 0.85,
                stride_frac: float = 0.5) -> list[dict]:
    H, W = ov_tissue.shape
    f = max(4, int(round(fov_ov)))
    half = f // 2
    stride = max(1, int(f * stride_frac))
    gray = ov_rgb.max(axis=2)
    tissue = ov_tissue.astype(bool)
    pos = ov_pos.astype(bool)

    cands = []
    for cy in range(half, H - half, stride):
        for cx in range(half, W - half, stride):
            ys = slice(cy - half, cy - half + f)
            xs = slice(cx - half, cx - half + f)
            tm = tissue[ys, xs]
            tfrac = float(tm.mean())
            if tfrac < min_tissue:
                continue
            tcount = int(tm.sum())
            if tcount == 0:
                continue
            local_pct = 100.0 * float((pos[ys, xs] & tm).sum()) / tcount
            dark = float(((gray[ys, xs] < 50) & tm).mean())   # folds/debris
            cost = abs(local_pct - global_pct) + 100.0 * (1.0 - tfrac) + 200.0 * dark
            cands.append((cost, cx, cy, local_pct, tfrac))

    cands.sort(key=lambda c: c[0])
    chosen: list[dict] = []
    for cost, cx, cy, lp, tf in cands:
        if all(abs(cx - c["cx"]) >= f or abs(cy - c["cy"]) >= f for c in chosen):
            chosen.append({"cx": cx, "cy": cy, "local_pct": lp, "tissue_frac": tf})
        if len(chosen) >= n:
            break
    return chosen


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------
def _save(img, base: Path, formats) -> list[Path]:
    base.parent.mkdir(parents=True, exist_ok=True)
    bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    out: list[Path] = []
    for ext in formats:
        params: list[int] = []
        e = ext.lower()
        if e in ("tif", "tiff"):
            # OpenCV writes TIFF uncompressed by default; force lossless DEFLATE
            # (COMPRESSION_ADOBE_DEFLATE = 8) so files are roughly PNG-sized.
            params = [cv2.IMWRITE_TIFF_COMPRESSION, 8]
        elif e in ("jpg", "jpeg"):
            params = [cv2.IMWRITE_JPEG_QUALITY, 92]
        p = base.with_suffix("." + ext)
        cv2.imwrite(str(p), bgr, params)
        out.append(p)
    return out


def _qc_masks(crop_rgb, conv, cfg, thr, thr_s, um_per_px=None):
    """Recompute SABG+/artifact masks for a full-res FOV crop, matching the
    analysis logic (per-pixel; the crop is ~all tissue, so no section blob)."""
    from .edge import refine_positive
    from .tissue import artifact_mask, tissue_mask
    opp = scoring.opponent_score(crop_rgb)
    deconv = scoring.deconvolution_score(crop_rgb, conv)
    raw_t = tissue_mask(crop_rgb, cfg.tissue)
    art = (artifact_mask(crop_rgb, opp, cfg.artifact) & raw_t
           if cfg.artifact.enabled else np.zeros(raw_t.shape, bool))
    if cfg.detection.primary == "opponent":
        p_s, s_s = opp, deconv
    else:
        p_s, s_s = deconv, opp
    pos = (raw_t & ~art) & (p_s >= thr)
    if cfg.detection.require_agreement and thr_s is not None:
        pos &= (s_s >= thr_s)
    if cfg.edge.enabled:
        pos, _ = refine_positive(pos, crop_rgb, um_per_px, cfg.edge)
    ep = max(0, int(cfg.detection.expand_px))      # match analyze: teal-gated growth
    if ep and pos.any():
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * ep + 1, 2 * ep + 1))
        grown = cv2.dilate(pos.astype(np.uint8), k).astype(bool) & raw_t & ~art
        if cfg.detection.expand_teal_min > 0:
            grown &= pos | (opp >= cfg.detection.expand_teal_min)
        pos = grown
    return pos, art


def export(data_dir, out_dir, p: ExportParams, cfg,
           only_scene: str | None = None, do_fov: bool = True,
           resume: bool = False, metadata: dict | None = None) -> Path:
    """Render figures from the maps written by `analyze`.

    *do_fov* False renders only the whole-section figures (the overlay), skipping the
    per-FOV full-res crops (used by analyze when ``output.export_on_analyze`` is off).
    *resume* skips scenes whose section figures already exist (Stop/Resume).
    *metadata* (when bundled with analyze) is reused so aliases match the maps that
    analyze wrote; standalone it is loaded from ``out_dir/sections.csv``.
    """
    out_dir = Path(out_dir)
    maps_dir = out_dir / "maps"
    exp_dir = out_dir / "exports"
    if not maps_dir.exists():
        raise FileNotFoundError(
            f"no overview maps in {maps_dir} - run `analyze` first")

    # stain matrix for the QC overlay (matches analyze defaults / config).
    conv = scoring.conv_matrix(cfg.detection.stain_matrix)

    results = out_dir / "results.csv"
    pct_by_key: dict[str, float] = {}
    thr_by_key: dict[str, float | None] = {}
    thrs_by_key: dict[str, float | None] = {}
    if results.exists():
        df = pd.read_csv(results, sep=_sniff_sep(results))
        for _, r in df.iterrows():
            k = str(r["key"])
            pct_by_key[k] = float(r["pct_sabg"])
            thr_by_key[k] = (None if pd.isna(r.get("threshold"))
                             else float(r["threshold"]))
            ts = r.get("threshold_secondary")
            thrs_by_key[k] = None if ts is None or pd.isna(ts) else float(ts)
    elif p.qc_overlay:
        print("  ! results.csv not found - QC overlay needs it; "
              "writing plain figures only")

    files = czi_io.list_czi_files(data_dir)
    by_file: dict[Path, list[SceneInfo]] = {}
    for path in files:
        for s in czi_io.list_scenes(path):
            if only_scene is None or s.key == only_scene:
                by_file.setdefault(path, []).append(s)

    # Aliases must match what `analyze` used to name the maps/outputs: reuse the
    # metadata passed in (bundled run), else load the out-folder sections.csv.
    if metadata is None:
        sections = out_dir / "sections.csv"
        metadata = load_metadata(sections) if sections.exists() else None
    all_scenes = [s for ss in by_file.values() for s in ss]
    aliases = build_aliases(all_scenes, metadata, cfg.alias)

    rows = []
    for path, scenes in by_file.items():
        print(f"[export] {path.name}")
        with pyczi.open_czi(str(path)) as doc:
            for s in scenes:
                got = _export_scene(doc, s, out_dir, p, cfg, conv,
                                    aliases.get(s.key, s.slug),
                                    pct_by_key.get(s.key, 0.0),
                                    thr_by_key.get(s.key),
                                    thrs_by_key.get(s.key),
                                    do_fov=do_fov, resume=resume)
                rows.extend(got)

    fovs_csv = exp_dir / "fovs.csv"
    if rows:
        exp_dir.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(rows).to_csv(fovs_csv, index=False)

    # Maps are a transient intermediate (FOV selection + section-figure base);
    # remove them once the figures are made unless the user wants to keep them.
    if not cfg.output.keep_maps:
        import shutil
        shutil.rmtree(maps_dir, ignore_errors=True)
        print(f"[export] removed maps/ (output.keep_maps=false)")

    if do_fov:
        print(f"[export] {len(rows)} FOVs -> {exp_dir}")
    else:
        print("[export] section figures only (output.export_on_analyze=false)")
    return fovs_csv


def _load_map(maps_dir: Path, slug: str, name: str):
    f = maps_dir / f"{slug}_{name}.png"
    if not f.exists():
        return None
    img = cv2.imread(str(f), cv2.IMREAD_UNCHANGED)
    return img


def _scene_done(out_dir: Path, alias: str, p: ExportParams, do_fov: bool) -> bool:
    """True if this scene's figures already exist on disk (for Stop/Resume skip):
    every requested section variant present, plus at least one FOV crop when *do_fov*."""
    sec_dir = out_dir / "sections"
    formats = p.sec_formats or p.formats
    if p.section_figures:
        for v in p.sec_variants:
            if not any((sec_dir / f"{alias}_{v}").with_suffix("." + ext).exists()
                       for ext in formats):
                return False
    if do_fov and not list((out_dir / "exports").glob(f"{alias}_fov0_*")):
        return False
    return True


def _export_scene(doc, s: SceneInfo, out_dir: Path, p: ExportParams, cfg, conv,
                  alias: str, global_pct: float, thr: float | None,
                  thr_s: float | None, do_fov: bool = True,
                  resume: bool = False) -> list[dict]:
    if resume and _scene_done(out_dir, alias, p, do_fov):
        print(f"        {s.key} [{alias}]: figures present, skipping (resume)")
        return []
    maps_dir = out_dir / "maps"
    ov_bgr = cv2.imread(str(maps_dir / f"{alias}_overview.jpg"))
    ov_tissue = _load_map(maps_dir, alias, "tissue")
    ov_pos = _load_map(maps_dir, alias, "pos")
    if ov_bgr is None or ov_tissue is None or ov_pos is None:
        print(f"        {s.key} [{alias}]: maps missing, skipping")
        return []
    ov_rgb = cv2.cvtColor(ov_bgr, cv2.COLOR_BGR2RGB)
    ov_tissue = ov_tissue > 0
    ov_pos = ov_pos > 0

    if not s.pixel_size_um:
        print(f"        {s.key}: no pixel size, skipping")
        return []

    H, W = ov_tissue.shape
    scale = W / s.w                                   # overview px per full-res px
    fov_full = int(round(p.fov_um / s.pixel_size_um))  # full-res FOV side (px)
    fov_ov = fov_full * scale

    fovs = select_fovs(ov_rgb, ov_tissue, ov_pos, global_pct, p.n_fov,
                       fov_ov, p.min_tissue_frac)

    # whole-section figures (written even if no clean FOV was found)
    sec_written: list = []
    if p.section_figures:
        sec_written = _section_figures(out_dir, alias, doc, s, ov_rgb, ov_tissue,
                                       ov_pos, maps_dir, fovs, fov_ov, p, cfg)

    if not do_fov:   # section figures only (analyze with export_on_analyze=false)
        if cfg.output.log_files and sec_written:
            overlay.log_written(out_dir, sec_written)
        return []

    if not fovs:
        if cfg.output.log_files and sec_written:
            overlay.log_written(out_dir, sec_written)
        print(f"        {s.key}: no clean FOV found")
        return []

    rows = []
    written: list = []
    for i, fov in enumerate(fovs):
        # overview centre -> full-res ROI origin
        fx = int(round(s.x + (fov["cx"] / scale) - fov_full / 2))
        fy = int(round(s.y + (fov["cy"] / scale) - fov_full / 2))
        raw = czi_io.read_region(doc, fx, fy, fov_full, fov_full, zoom=1.0)

        base = out_dir / "exports" / f"{alias}_fov{i}"

        # base colour renderings to write
        bases: list[tuple[str, np.ndarray]] = []
        if p.wb:
            wp = whitebalance.estimate_white_point(raw)
            bases.append(("wb", whitebalance.white_balance(raw, wp)))
        if p.raw:
            bases.append(("raw", raw))

        # QC masks (recomputed on the raw crop) shared by all bases
        pos = art = None
        qc_bases = set(p.qc_bases)
        if p.qc_overlay and thr is not None:
            pos, art = _qc_masks(raw, conv, cfg, thr, thr_s, s.pixel_size_um)

        for tag, img in bases:
            if p.plain:
                written += _save(draw_scalebar(img, s.pixel_size_um, p.scalebar_um,
                                               label=p.scalebar_label),
                                 Path(str(base) + f"_{tag}"), p.formats)
            if p.qc_overlay and pos is not None and tag in qc_bases:
                qc = overlay.two_color_overlay(
                    img, pos, art, cfg.overlay.sabg_color,
                    cfg.overlay.artifact_color,
                    sabg_alpha=cfg.overlay.sabg_alpha,
                    artifact_alpha=cfg.overlay.artifact_alpha)
                written += _save(draw_scalebar(qc, s.pixel_size_um, p.scalebar_um,
                                               label=p.scalebar_label),
                                 Path(str(base) + f"_{tag}_qc"), p.formats)

        rows.append({
            "file": s.file_stem, "scene": s.scene_index, "alias": alias, "fov": i,
            "center_x_um": round((fx + fov_full / 2) * s.pixel_size_um, 1),
            "center_y_um": round((fy + fov_full / 2) * s.pixel_size_um, 1),
            "fov_um": p.fov_um, "local_pct_sabg": round(fov["local_pct"], 3),
            "global_pct_sabg": round(global_pct, 3),
            "tissue_frac": round(fov["tissue_frac"], 3),
        })
        print(f"        {s.key} fov{i}: local={fov['local_pct']:.2f}% "
              f"(global {global_pct:.2f}%), tissue={fov['tissue_frac']:.2f}")
    if cfg.output.log_files:
        overlay.log_written(out_dir, sec_written + written)
    return rows
