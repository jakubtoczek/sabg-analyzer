"""Configuration loading and defaults.

A YAML file (see ``config.example.yaml``) overrides the dataclass defaults
below. Per-scene overrides live under ``scenes[<file_stem>:<idx>]``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from .edge import EdgeFilterParams
from .fold import FoldParams
from .scoring import build_stain_matrix
from .threshold import ThresholdParams
from .tissue import ArtifactParams, TissueParams


@dataclass
class DetectionParams:
    primary: str = "deconvolution"          # deconvolution | opponent
    stain_matrix: np.ndarray | None = None  # 3x3, rows = SABG/counter/residual
    auto_estimate: bool = False
    require_agreement: bool = True          # SABG+ only where BOTH scores fire
    # Hysteresis thresholding: SEED at the (raised) threshold, then GROW each seed
    # into the CONNECTED faint teal around it (which can be large), so faint teal
    # contiguous with strong teal is recovered while isolated faint/edge teal is not.
    # Connectivity is decided at the maps (HD) resolution; full-res seeds are always
    # counted, so punctate signal is never lost. False = plain single-threshold.
    hysteresis: bool = True
    hyst_low_scale: float = 0.5             # grow/low threshold = seed threshold x this
    hyst_teal_min: float = 0.02             # only grow into pixels at least this teal (opponent)
    #                                         0.02 = session-16 tuning default (cfg07; was 0.04)
    expand_px: int = 2                      # dilate the final positive mask by this many px
    expand_teal_min: float = 0.02           # only grow into pixels this teal (opponent);
                                            # keeps growth on real teal, not edges/tissue.
                                            # 0 = grow into any tissue (old behaviour)


@dataclass
class IntensityParams:
    """Optional intensity (optical-density) quantification. %SABG by *area* is always
    reported; when enabled, two extra results.csv columns capture stain *amount* from the
    colour-deconvolution SABG score over the final positives: ``sabg_integrated_od`` (the
    OD summed over positives -- area weighted by intensity) and ``sabg_mean_od`` (the mean
    OD per positive pixel -- intensity alone). With ``per_tissue`` also on, a third column
    ``sabg_od_per_tissue`` reports the OD summed over *all tissue* px divided by tissue px
    (an intensity-weighted analogue of %SABG: stain amount normalised to tissue, not just
    positives). The OD value itself follows the existing stain vectors
    (``detection.stain_matrix`` / ``auto_estimate``)."""
    enabled: bool = False   # off by default: %SABG-area is the clean baseline output
    per_tissue: bool = False   # also write sabg_od_per_tissue (needs `enabled`)


@dataclass
class OverlayParams:
    sabg_color: tuple[int, int, int] = (0, 200, 0)      # SABG+ highlight (green)
    sabg_alpha: float = 0.60                            # SABG+ blend strength
    artifact_color: tuple[int, int, int] = (220, 0, 0)  # dark fold/debris (red)
    artifact_alpha: float = 0.60                        # artifact blend strength
    fold_color: tuple[int, int, int] = (255, 140, 0)    # linear-fold band (orange)
    fold_alpha: float = 0.60                            # fold blend strength
    fold_show_sabg: bool = True   # draw rejected SABG+ (green) on top of the fold band
    edge_color: tuple[int, int, int] = (138, 43, 226)   # edge-shadow rejection (violet; high-contrast)
    edge_alpha: float = 0.60                            # edge blend strength
    show_edge_rejected: bool = True                     # draw the edge-rejected pixels
    nontissue_color: tuple[int, int, int] = (150, 150, 150)  # glass/background (grey)
    nontissue_alpha: float = 0.50                       # non-tissue shade strength
    show_nontissue: bool = True                         # shade non-tissue to audit the mask
    excluded_color: tuple[int, int, int] = (255, 0, 255)  # manual exclusion mask (magenta)
    excluded_alpha: float = 0.55                        # exclusion blend strength
    sabg_candidate_color: tuple[int, int, int] = (0, 180, 180)  # pre-rejection SABG+ (cyan)
    sabg_candidate_alpha: float = 0.45                  # candidate blend (now a default-on primary layer)
    # Grouped "masked" display layer: non-tissue + excluded + artifact + fold merged
    # into ONE neutral shade (drawn as a union, not stacked). Used by the default
    # section figure (the clean 2-layer view: masked grey + SABG+ green).
    masked_color: tuple[int, int, int] = (128, 128, 128)  # grouped excluded/masked (grey)
    masked_alpha: float = 0.50                          # grouped masked blend strength


@dataclass
class WhiteBalanceParams:
    """Display-only white balance for publication figures (quantification always uses
    raw pixels). `estimate_white_point` averages the brightest `bright_frac` of pixels
    and scales each channel so that point maps to `target` (near-white)."""
    bright_frac: float = 0.2     # fraction of brightest pixels taken as the white point
    target: float = 250.0        # channel value the white point is scaled to (near-white)
    homogeneity_tol: float = 0.15  # reserved: max RGB spread for a manual white-point pick
    # auto=True  -> the white point is ESTIMATED (brightest bright_frac pixels of the scope's
    #               reference image). auto=False -> MANUAL: use a picked white point (GUI pipette,
    #               or `white_point` for the global scope).
    auto: bool = True
    # Auto de-cast strength: 0 = mild (brightest-pixel white point, original behaviour);
    # 1 = map the dominant glass colour (glass_percentile per channel) fully to white.
    neutralize: float = 0.0
    glass_percentile: float = 60.0   # per-channel percentile that defines the "glass" colour
    # Consistency scope of the white point (where the point comes from + how widely it is shared):
    #   image   - from the image being viewed (overview or ROI crop); each self-balances.
    #   section - one point per section (from its overview), reused for the overview + all its ROIs.
    #   global  - one point for the whole loaded dataset; every section/ROI shares it.
    scope: str = "image"
    white_point: list | None = None   # [R,G,B] for the GLOBAL manual pick (the one batch honours)
    # Display-only tone adjust (all default to no-op). temperature is a warm/cool nudge of the
    # white point (ZEN ±1 ≈ ±10 K); temperature_k is its per-step gain (calibration knob).
    temperature: float = 0.0
    temperature_k: float = 0.02
    brightness: float = 0.0      # additive on [0,1] after WB (-1..1)
    contrast: float = 0.0        # affine about mid-grey (-1..1)
    gamma: float = 1.0           # out = in**(1/gamma); >0


@dataclass
class OutputParams:
    """Toggle which image artifacts `analyze` writes. `results.csv` and the
    `config.yaml` snapshot are always written. NB: `export` needs `maps`."""
    debug: bool = False    # debug/<alias>_compare.jpg (6-panel audit; off by default)
    maps: bool = True      # maps/<alias>_* (consumed by `export`)
    keep_maps: bool = False  # keep maps/ after `export` (else removed once figures are made)
    # Run `export` automatically after `analyze` (one timed pipeline). When True the
    # FOV crops are produced too; when False, analyze still writes the section overlay
    # figures (so you always get an overlay) but skips the per-FOV crops.
    export_on_analyze: bool = True
    log_files: bool = True   # log the files written at each step
    run_log: bool = True   # also tee the console output to a timestamped log file
    run_log_name: str = "%Y%m%d-%H%M_run.log"   # strftime template, in the out dir


@dataclass
class ReportParams:
    """What the tabular run report contains (alongside the always-written
    ``results.csv`` + ``config.yaml``). ``metadata.csv`` = a human digest of the run
    (params used, timing, version) + per-CZI acquisition metadata; the optional
    ``results_detailed.csv`` = per-section layer px/areas + intersections + intensities;
    ``results.xlsx`` bundles results / metadata / details into one workbook (openpyxl).
    Content is customisable: empty allowlists mean 'everything available'."""
    metadata: bool = True            # write metadata.csv + workbook 'metadata' tab
    details: bool = False            # write results_detailed.csv + 'details' tab
    workbook: bool = True            # write results.xlsx (results [+metadata] [+details] tabs)
    workbook_name: str = "results.xlsx"
    # Which metadata scopes to include: run (timing/version/command), params (synthetic
    # analysis settings), acquisition (per-CZI scan metadata from the file).
    metadata_blocks: list[str] = field(default_factory=lambda: ["run", "params", "acquisition"])
    acquisition_fields: list[str] = field(default_factory=list)  # [] = all found
    detail_columns: list[str] = field(default_factory=list)      # [] = all


@dataclass
class ProgressParams:
    """What the progress reporter shows. The live line re-actualises in place
    (one section at a time); `checkpoints` leaves a persistent line behind at
    the given per-section percentages (e.g. [50] or [25, 50, 75])."""
    section: bool = True            # per-section progress (% + tiles)
    total: bool = True              # overall progress (% + tiles)
    elapsed: bool = True            # show elapsed time
    eta: bool = True                # show estimated time remaining
    checkpoints: list[float] = field(default_factory=list)   # persistent marks


@dataclass
class GuiParams:
    """GUI-only knobs (read by sabg_gui.py / the preview window)."""
    info_opens: list[str] = field(default_factory=lambda: ["sections", "labels"])
    # Preview/Tune window: ROI draw cap (default 2x export.fov_um = 1000 µm) and
    # whether the picker opens the selected thumb at higher resolution by default.
    preview_roi_cap_um: float = 1000.0
    preview_hi_res: bool = False
    # Default overlay-layer visibility in the Layers panel (Preview + Info). Only the
    # listed keys override the per-layer `default show` baked into `LAYER_SPEC`; any
    # layer not named here keeps its LAYER_SPEC default. Default flips the audit view to
    # candidate-on / SABG-off so the pre-rejection teal is what you see first.
    layer_defaults: dict[str, bool] = field(
        default_factory=lambda: {"sabg_candidate": True, "sabg": False})
    # Info viewer: default rotation of the slide-label image, in 90° counter-clockwise
    # quarter-turns (0-3). Labels are scanned sideways; 3 = upright the intended way (1 was
    # 180° off / upside-down). The in-viewer rotate buttons compose on top and reset per section.
    label_rotate_quarter_turns: int = 3
    # Preview scale-bar length presets (µm), offered in the "bar" dropdown (plus "Auto").
    # Shown as mm/µm labels; the live + exported bars use these. Edit to taste.
    scalebar_values: list[float] = field(
        default_factory=lambda: [10000, 5000, 2000, 1000, 500, 200, 100, 50, 20, 10])


@dataclass
class PathsParams:
    """Default folders + filename templates (GUI convenience). Blank folders fall back
    to the GUI's built-ins (../data, ../outputs). ``export_dir`` seeds the preview
    Export… dialog; ``preview_export_name`` is its default base name (tokens ``{alias}``,
    ``{kind}`` where kind is ``roi``/``thumb``)."""
    data_dir: str = ""
    out_dir: str = ""
    export_dir: str = ""
    preview_export_name: str = "{alias}_{kind}"


@dataclass
class AliasParams:
    """How each section's short alias (used in results + filenames) is built
    from the `sections.csv` metadata. `tag` (if filled) always wins."""
    fields: list[str] = field(default_factory=lambda: ["animal", "group"])
    optional: list[str] = field(default_factory=lambda: ["tissue"])
    spacer: str = "_"
    tag_field: str = "tag"


@dataclass
class Config:
    process_zoom: float = 1.0
    tile_size: int = 4096
    # Canvases are sized by magnification (µm/px) so they're proportional to each
    # section's physical size, each bounded by a px safety cap (huge slides).
    overview_um_per_px: float = 7.0    # gating/histogram/fold-detection overview
    overview_max_edge: int = 2500      # safety cap (px) for the gating overview
    maps_um_per_px: float = 3.0        # maps canvas (saved masks + FOV selection)
    maps_max_edge: int = 6000          # safety cap (px) for the maps canvas
    # `scan` thumbnails are sized the same way (µm/px proportional + px cap), so the
    # preview picker shows each section proportional to its real physical size.
    thumb_um_per_px: float = 12.0      # scan thumbnail resolution
    thumb_max_edge: int = 1280         # safety cap (px); high enough that typical
                                       # sections stay proportional (don't all clip)
    full_debug: bool = False
    tissue: TissueParams = field(default_factory=TissueParams)
    artifact: ArtifactParams = field(default_factory=ArtifactParams)
    fold: FoldParams = field(default_factory=FoldParams)
    edge: EdgeFilterParams = field(default_factory=EdgeFilterParams)
    detection: DetectionParams = field(default_factory=DetectionParams)
    threshold: ThresholdParams = field(default_factory=ThresholdParams)
    intensity: IntensityParams = field(default_factory=IntensityParams)
    overlay: OverlayParams = field(default_factory=OverlayParams)
    whitebalance: WhiteBalanceParams = field(default_factory=WhiteBalanceParams)
    output: OutputParams = field(default_factory=OutputParams)
    report: ReportParams = field(default_factory=ReportParams)
    progress: ProgressParams = field(default_factory=ProgressParams)
    gui: GuiParams = field(default_factory=GuiParams)
    paths: PathsParams = field(default_factory=PathsParams)
    alias: AliasParams = field(default_factory=AliasParams)
    export: dict[str, Any] = field(default_factory=dict)  # defaults for `export`
    scenes: dict[str, dict[str, Any]] = field(default_factory=dict)

    # -- per-scene helpers --------------------------------------------------
    def scene_override(self, key: str) -> dict[str, Any]:
        return self.scenes.get(key, {}) or {}

    def scene_skipped(self, key: str) -> bool:
        return bool(self.scene_override(key).get("skip", False))

    def scene_threshold(self, key: str) -> float | None:
        v = self.scene_override(key).get("threshold")
        return float(v) if v is not None else None

    def scene_tissue(self, key: str) -> TissueParams:
        """`tissue` params with any per-scene ``scenes.<key>.tissue`` overrides applied."""
        ov = self.scene_override(key).get("tissue")
        if not ov:
            return self.tissue
        from dataclasses import replace
        return replace(self.tissue,
                       **{k: v for k, v in ov.items() if hasattr(self.tissue, k)})

    def scene_exclude_mask(self, key: str) -> str | None:
        """Per-scene manual exclusion-mask path (relative to the output dir), or None.

        Drawn in the preview, stored as a PNG and referenced from
        ``scenes.<key>.exclude_mask``; the pipeline subtracts it from both the
        numerator and the denominator (resolved against out_dir at analyze time)."""
        v = self.scene_override(key).get("exclude_mask")
        return str(v) if v else None


def _update_dataclass(obj, data: dict[str, Any]) -> None:
    for k, v in data.items():
        if hasattr(obj, k):
            setattr(obj, k, v)


def load_config(path: str | Path | None) -> Config:
    """Load a Config from YAML, or return defaults if *path* is None."""
    cfg = Config()
    if path is None:
        _finalise(cfg)
        return cfg

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}

    for key in ("process_zoom", "tile_size", "overview_um_per_px",
                "overview_max_edge", "maps_um_per_px", "maps_max_edge",
                "thumb_um_per_px", "thumb_max_edge", "full_debug"):
        if key in raw:
            setattr(cfg, key, raw[key])

    if "tissue" in raw and raw["tissue"]:
        _update_dataclass(cfg.tissue, raw["tissue"])
    if "artifact" in raw and raw["artifact"]:
        _update_dataclass(cfg.artifact, raw["artifact"])
    if "fold" in raw and raw["fold"]:
        _update_dataclass(cfg.fold, raw["fold"])
    if "edge" in raw and raw["edge"]:
        _update_dataclass(cfg.edge, raw["edge"])
    if "detection" in raw and raw["detection"]:
        _update_dataclass(cfg.detection, raw["detection"])
    if "threshold" in raw and raw["threshold"]:
        _update_dataclass(cfg.threshold, raw["threshold"])
    if "intensity" in raw and raw["intensity"]:
        _update_dataclass(cfg.intensity, raw["intensity"])
    if "overlay" in raw and raw["overlay"]:
        ov = raw["overlay"]
        if "sabg_color" in ov:
            cfg.overlay.sabg_color = tuple(ov["sabg_color"])
        if "color" in ov:                       # back-compat: old single SABG color
            cfg.overlay.sabg_color = tuple(ov["color"])
        if "artifact_color" in ov:
            cfg.overlay.artifact_color = tuple(ov["artifact_color"])
        if "fold_color" in ov:
            cfg.overlay.fold_color = tuple(ov["fold_color"])
        if "edge_color" in ov:
            cfg.overlay.edge_color = tuple(ov["edge_color"])
        if "nontissue_color" in ov:
            cfg.overlay.nontissue_color = tuple(ov["nontissue_color"])
        # back-compat: a single `alpha:` seeds the per-mask alphas
        if ov.get("alpha") is not None:
            a = float(ov["alpha"])
            cfg.overlay.sabg_alpha = cfg.overlay.artifact_alpha = cfg.overlay.fold_alpha = a
        if ov.get("sabg_alpha") is not None:
            cfg.overlay.sabg_alpha = float(ov["sabg_alpha"])
        if ov.get("artifact_alpha") is not None:
            cfg.overlay.artifact_alpha = float(ov["artifact_alpha"])
        if ov.get("fold_alpha") is not None:
            cfg.overlay.fold_alpha = float(ov["fold_alpha"])
        if ov.get("fold_show_sabg") is not None:
            cfg.overlay.fold_show_sabg = bool(ov["fold_show_sabg"])
        if ov.get("edge_alpha") is not None:
            cfg.overlay.edge_alpha = float(ov["edge_alpha"])
        if ov.get("show_edge_rejected") is not None:
            cfg.overlay.show_edge_rejected = bool(ov["show_edge_rejected"])
        if ov.get("nontissue_alpha") is not None:
            cfg.overlay.nontissue_alpha = float(ov["nontissue_alpha"])
        if ov.get("show_nontissue") is not None:
            cfg.overlay.show_nontissue = bool(ov["show_nontissue"])
        if "excluded_color" in ov:
            cfg.overlay.excluded_color = tuple(ov["excluded_color"])
        if ov.get("excluded_alpha") is not None:
            cfg.overlay.excluded_alpha = float(ov["excluded_alpha"])
        if "sabg_candidate_color" in ov:
            cfg.overlay.sabg_candidate_color = tuple(ov["sabg_candidate_color"])
        if ov.get("sabg_candidate_alpha") is not None:
            cfg.overlay.sabg_candidate_alpha = float(ov["sabg_candidate_alpha"])
        if "masked_color" in ov:
            cfg.overlay.masked_color = tuple(ov["masked_color"])
        if ov.get("masked_alpha") is not None:
            cfg.overlay.masked_alpha = float(ov["masked_alpha"])
    if "whitebalance" in raw and raw["whitebalance"]:
        _update_dataclass(cfg.whitebalance, raw["whitebalance"])
    if "output" in raw and raw["output"]:
        _update_dataclass(cfg.output, raw["output"])
    if "report" in raw and raw["report"]:
        _update_dataclass(cfg.report, raw["report"])
    if "progress" in raw and raw["progress"]:
        _update_dataclass(cfg.progress, raw["progress"])
    if "gui" in raw and raw["gui"]:
        g = dict(raw["gui"])
        ld = g.pop("layer_defaults", None)
        if isinstance(ld, dict):                 # merge so naming one layer keeps the rest
            cfg.gui.layer_defaults.update({k: bool(v) for k, v in ld.items()})
        _update_dataclass(cfg.gui, g)
    if "paths" in raw and raw["paths"]:
        _update_dataclass(cfg.paths, raw["paths"])
    if "alias" in raw and raw["alias"]:
        _update_dataclass(cfg.alias, raw["alias"])
    if "export" in raw and raw["export"]:
        cfg.export = dict(raw["export"])
    if "scenes" in raw and raw["scenes"]:
        cfg.scenes = dict(raw["scenes"])

    _finalise(cfg)
    return cfg


def _finalise(cfg: Config) -> None:
    """Resolve the stain matrix (rows or None -> default vectors)."""
    sm = cfg.detection.stain_matrix
    if sm is None:
        cfg.detection.stain_matrix = build_stain_matrix()
    else:
        arr = np.asarray(sm, dtype=float)
        if arr.shape == (2, 3):       # only SABG + counter given -> complete it
            cfg.detection.stain_matrix = build_stain_matrix(arr[0], arr[1])
        else:
            cfg.detection.stain_matrix = arr
