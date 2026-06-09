# SABG Analyzer

Quantify **SA-β-Gal (SABG) positive area** in brightfield CZI whole-slide images of
murine A549 tumor tissue (untreated vs. senescence-inducing treatment at 3 / 7 days).

For each tissue section:

> **% SABG⁺ area = SABG⁺ pixels / total tissue pixels × 100**

---

## Why this isn't a one-liner (read before tuning)

Inspecting the example slides revealed three facts that drive the whole design:

1. **The SABG signal is faint, punctate teal/cyan** — only ~0.6–1 % of tissue area.
   Downsampling destroys it: at 0.08× zoom the positive pixels (~24 600) dropped to **0**.
   ⇒ Quantification runs at **full resolution, in tiles**. Heavy downsampling is invalid
   (`process_zoom` floor ≈ 0.3; default 1.0).
2. **Background is two things**: white slide glass *and* black unsampled mosaic gaps.
   Both are excluded from "tissue".
3. **Signal is sparse**, so a naive Otsu threshold cuts into the tissue bulk. The default
   is the **triangle** method, computed from a full-res score histogram over tissue. The
   threshold is applied with **hysteresis** (seed + grow): a strict seed threshold marks
   confident teal, then each seed grows into the *connected* faint teal around it (down to
   a lower threshold, teal-gated), so faint staining contiguous with a strong region is
   recovered without admitting isolated faint/edge teal (`detection.hysteresis`).
4. **Tissue folds read as stain.** A fold stacks tissue on itself → dark in every channel
   → high optical density, which a deconvolution SABG score mistakes for teal. An
   **artifact pass** flags dark-but-not-teal pixels (folds/debris) plus an eroded tissue
   border and removes them from both the numerator and denominator; and SABG⁺ now requires
   **agreement** between the deconvolution and opponent scores (`require_agreement`).
   *Non-dense* folds that slip through still show as thin, curved **linear ridges** of false
   positives — an **optional** `fold` layer detects those on the overview (Frangi ridge ×
   structure-tensor coherence over the positive density), length/width-guarded, and rejects
   the band (orange). Off by default; it separates *linear from blobby*, not *fold from real*,
   so keep it conservative and audit the `*_compare.jpg`.

Each `.czi` here is a `Bgr24` mosaic holding **several scenes = tissue sections**
(4 / 3 / 3 / 3 in the example data). Pixel size is read from metadata (~0.44 µm/px) so
areas are reported in mm².

---

## Install

```powershell
cd C:\Code\SABG_analyzer\main
pip install -r requirements.txt
```

Outputs are written **outside this code folder** (default `..\outputs`, i.e.
`C:\Code\SABG_analyzer\outputs`) so the repo stays clean. Override with `--out`.
Run all commands from `C:\Code\SABG_analyzer\main`.

---

## Quick start (GUI)

If you'd rather click than type:

Double-click **`SABG_Analyzer.bat`**, or:

```powershell
cd C:\Code\SABG_analyzer\main
python sabg_gui.py
```

Then, top to bottom:

1. **Data** → Browse to the folder with your `.czi` files (defaults to `..\data`).
2. **Output** → Browse to where results should go (defaults to `..\outputs`).
3. **Scan** → finds the tissue sections and writes `sections.csv` + label/thumbnail
   images. Watch the timestamped **Log** pane; wait for `[exit 0]`.
4. **Info** → opens `sections.csv` and the `labels\` folder (configurable — see
   `gui.info_opens`, e.g. add `thumbs`). Read each slide label, fill the row's columns
   (see below), and save. *(Greyed out until Scan finishes.)*
5. **Analyze** → quantifies %SABG **and renders the figures** in one timed pass (the
   bundled export is on by default — set `output.export_on_analyze: false` for
   analysis-only, which still writes the section overlays but skips the FOV crops). A
   single **live progress line** shows on the status bar (per-section + overall % and
   tiles, with a moving-average ETA); section results, the `[analyze]`/`[export]`/
   `[total]` timings, and checkpoints stream into the log. It only warns if a section's
   **animal-ID fields (or `tag`)** are blank. Wait for `[exit 0]`.
6. **Export** → re-render the figures with the current config *(greyed out until Analyze
   finishes; needs `output.keep_maps: true` to re-export without re-analyzing)*.
7. **Stop / Resume** → one toggle: **Stop** ends the running job; once stopped, **Resume**
   finishes the rest (analyze + export), skipping sections already done. Results are
   checkpointed after every section, so a Stop never loses completed work.
8. **Help** opens this README; **Config** opens `outputs\config.yaml` (created from the
   example the first time) to tune thresholds / overlay / alias / export, then re-run.

The GUI is just a front-end for the commands below — anything it does you can also do on
the command line, and the two can be mixed freely. Each run also tees its console output
to a timestamped `outputs\YYYYMMDD-HHMM_run.log` (toggle/rename via `output.run_log` /
`output.run_log_name`). The log is timestamped per line, with a full date/time + UTC
offset banner at session start.

### Filling `sections.csv`
One row per section. Columns you edit:

| column | meaning |
|---|---|
| `animal`, `group` | **identification** — drive the section alias (e.g. `80_ctl`). |
| `tissue` | tissue type; appended to the alias only to disambiguate (`80_ctl_liver`). |
| `treatment`, `day` | optional extra metadata (carried into `results.csv`). |
| `analyze` | `yes` (default) or `no` to **skip** that section. |
| `tag` | optional custom alias that **overrides** the generated one. |

The **alias** (configurable under `alias:` in `config.yaml`) is used in `results.csv` and
in every output filename, so `80_ctl` replaces `2026_05_29__10215_s1`. Blank info → the
alias falls back to the raw scene id, so it always works.

---

## Workflow (two passes)

### 1. `scan` — detect sections, extract labels, build the metadata template
```powershell
python -m sabg_analyzer scan          # defaults: --data ..\data  --out ..\outputs
```
Produces (under `..\outputs`):
- `sections.csv` — one row per section with blank `animal/group/tissue/treatment/day`,
  plus `analyze` (default `yes`) and an optional `tag` (see *Filling `sections.csv`* above)
- `labels/<file>_label.png` — the **slide label image** (read it to fill the CSV)
- `thumbs/<file>_s<idx>.png` — scene thumbnails

Open each label image, then fill in the columns of `sections.csv`.

### 2. `analyze` — quantify
```powershell
python -m sabg_analyzer analyze       # shows a live progress/ETA bar in a terminal
```
Produces (under `..\outputs`):
- `results.csv` — `%SABG`, pixel counts, areas (mm²), thresholds, + your metadata
  (written after **every** section, so a Stop/crash keeps finished work)
- `sections/<alias>_<variant>.jpg` — whole-section figures, including the QC overlay
  (SABG⁺ = green, dark fold/debris = red, linear-fold band = orange, edge-shadow =
  blue, glass/background = grey). Variants are config-driven (see `export`).
- `debug/<file>_s<idx>_compare.jpg` — 6-panel audit (overview, both scores,
  SABG⁺/artifact masks, overlay, tissue). Add `--full-debug` for the large
  standalone heatmaps too.
- `config.yaml` — the thresholds that were used (edit + re-run to override)

By default `analyze` then runs `export` in the same process (one `[total]` time);
turn it off with `--no-export` or `output.export_on_analyze: false`. `sections.csv`
is auto-joined if present; or pass `--metadata path\to\sections.csv`.

Flags: `--export` / `--no-export` (run the figure export after analyze; default on),
`--continue` (resume: skip sections already in `results.csv`, finish the rest),
`--full-debug`, `--no-progress` / `--progress`, `--scene FILE:IDX` (one section),
`--config config.yaml`. Sections with `analyze=no` in `sections.csv` are skipped.

### 3. `export` — publication figures (runs automatically after `analyze`)
```powershell
python -m sabg_analyzer export                       # 5 FOVs/section
python -m sabg_analyzer export --fov-um 250 --scalebar-um 50 --n 3
python -m sabg_analyzer export --no-raw --formats tif png   # only wb, both formats
```
`analyze` already runs this in-process (unless `--no-export`), so you normally don't call
it directly — use it to **re-render with different settings** without re-analyzing (needs
`output.keep_maps: true`, since the maps are otherwise cleaned after each run).

It reads the overview maps left in `..\outputs\maps` and writes two kinds of figure:

- **Whole-section figures** → `sections/<alias>_<variant>.jpg`, sized by a fixed
  magnification (`section_um_per_px`) so each is proportional to its section. A variant
  is underscore-joined tokens: base `raw`|`wb`, overlay `overlay` (all layers) |
  `overlaysabg` (SABG only), `fov` (numbered FOV boxes), `scalebar` (adaptive ~1 mm,
  labelled). Defaults: `raw, wb_scalebar, wb_overlay_fov_scalebar`.
- **Representative FOV crops** → `exports/…` (skipped when analyze runs with
  `--no-export`). For each section it picks **clean, representative** full-resolution
  FOVs — almost entirely tissue (no gaps/glass/edges), artifact-light, and with a
  *local* %SABG close to the section's *global* %SABG (typical staining, not a hotspot).

Each FOV is written to `..\outputs\exports\` at full resolution with a burned-in scale
bar, as a matrix of **base × variant**:

| | `_wb` (white-balanced) | `_raw` (original colours) |
|---|---|---|
| **plain** | `…_fov<i>_wb` | `…_fov<i>_raw` |
| **+ QC overlay** | `…_fov<i>_wb_qc` | `…_fov<i>_raw_qc` |

- **white balance** neutralises each crop's yellow background to white (figures only —
  never affects quantification); **raw** is the original for side-by-side validation.
- **QC overlay** burns in the same green-SABG⁺ / red-artifact overlay as the analysis
  (recomputed at full resolution from `results.csv` thresholds).
- Formats: **JPEG by default** (full-res crops ~10× smaller); use `--formats tif png`
  for lossless. FOV scale-bar label text is **off by default** (`--scalebar-label` to
  add the "100 µm" caption); the whole-section scale bar is labelled **on** by default.

Turn any base/variant off (`--no-wb`, `--no-raw`, `--no-plain`, `--no-qc-overlay`) or set
the defaults in the `export:` block of `config.yaml`. `exports/fovs.csv` records each
chosen FOV's centre (µm) and local vs global %SABG.
Flags: `--fov-um`, `--scalebar-um`, `--scalebar-label`, `--n`, `--min-tissue`, `--wb/--no-wb`,
`--raw/--no-raw`, `--plain/--no-plain`, `--qc-overlay/--no-qc-overlay`, `--formats`,
`--config`, `--scene FILE:IDX`.

### One-shot wrapper
```powershell
python scripts\run_analysis.py            # scan + analyze
python scripts\run_analysis.py --config config.yaml --skip-scan
```

---

## Tuning (auto threshold → manual override)

The threshold is automatic by default. To override, edit `outputs/config.yaml`
(or copy `config.example.yaml` → `config.yaml`) and re-run `analyze --config config.yaml`.

Iterate fast on a single section:
```powershell
python -m sabg_analyzer analyze --config config.yaml --scene 2026_05_29__10215:0
```

Key knobs (`config.example.yaml` documents them all):

| Knob | Meaning |
|------|---------|
| `process_zoom` | Processing resolution. 1.0 = full res (recommended). Lower = faster, risks signal loss. |
| `detection.primary` | `deconvolution` (default) or `opponent`. Both are always exported to compare. |
| `detection.require_agreement` | SABG⁺ only where **both** scores fire (default true). Kills fold/density false positives. |
| `threshold.scale` | Multiplies the auto threshold (default 0.9). With hysteresis on this is the **seed** (high) threshold — keep it fairly strict. |
| `detection.hysteresis` / `hyst_low_scale` / `hyst_teal_min` | **Seed + grow** detection (default on). Seed at `threshold.scale`, then grow each seed into the *connected* faint teal down to `seed × hyst_low_scale` (default 0.5), only into pixels at least `hyst_teal_min` teal (0.04). Captures faint teal contiguous with strong teal; rejects isolated faint/edge teal. Connectivity is decided at maps res; full-res seeds always count. |
| `detection.auto_estimate` | Estimate the SABG stain vector per scene instead of using defaults. |
| `artifact.enabled` / `dark_level` / `teal_min` / `erode_px` | Dark fold/debris rejection: dark-and-non-teal pixels + eroded border, excluded from numerator and denominator. |
| `fold.enabled` / `combine` / `min_length_um` / `max_width_um` / `band_width_um` / … | **Optional** linear-fold rejection: thin curved ridges of *false* SABG+ that aren't dense. Ridge (Frangi) + structure-tensor coherence on the overview density, length/width-guarded, drawn orange. Off by default; `combine`: `product`/`agreement`/`union`/`frangi_only`. |
| `threshold.method` | `triangle` (default) / `otsu` / `percentile` / `fixed`. |
| `threshold.from_overview` | **Speed:** derive the threshold from the overview and skip full-res pass 1 (~2× faster; threshold may shift slightly). Default false. |
| `alias.fields` / `optional` / `spacer` / `tag_field` | How the section alias (results + filenames) is built from `sections.csv`. Default `animal_group`, `tissue` to disambiguate. |
| `progress.section` / `total` / `elapsed` / `eta` / `checkpoints` | What the live progress line shows; `checkpoints` (e.g. `[50]`) leaves a persistent mark at those per-section %. |
| `output.run_log` / `run_log_name` | Tee console output to a timestamped log file in the output folder (on by default). |
| `maps_um_per_px` / `maps_max_edge` | Maps/overlay canvas resolution as a magnification (µm/px, default 3 = match `section_um_per_px`) + a px safety cap. Lower µm/px = crisper overlays, more RAM. Never affects %SABG. |
| `overview_um_per_px` / `overview_max_edge` | Gating/fold-detection overview resolution (µm/px, default 7) + px safety cap. |
| `overlay.sabg_color` / `sabg_alpha` | SABG⁺ highlight colour + blend (default green, 0.60). |
| `overlay.artifact_color` / `artifact_alpha` | Artifact highlight colour + blend (default red, 0.60). |
| `output.debug` / `maps` | Toggle which image outputs `analyze` writes (keep `maps` on for `export`). |
| `output.export_on_analyze` | Run the figure export after analyze (default on; the section overlay lives in `sections/`). |
| `output.keep_maps` | Keep `maps/` after export so you can re-run `export` without re-analyzing. |
| `scenes["<file>:<idx>"].threshold` | Hard manual threshold for one section. |
| `scenes["<file>:<idx>"].skip` | Exclude a section. |

**How to judge a run:** open `sections/<alias>_wb_overlay_fov_scalebar.jpg` (or enable
`output.debug` for the 6-panel `debug/*_compare.jpg`). Green SABG⁺ pixels should sit on the
faint teal specks, **not** on khaki tissue or background; red marks excluded folds/debris.
If green bleeds into tissue, raise `threshold.scale` (stricter seeds); if real specks are
missed, lower it, or — for faint teal next to strong teal — lower `detection.hyst_low_scale`
so seeds grow further. If fold *streaks* still come through green, raise `artifact.dark_level`
(e.g. 0.55). Compare the
`opponent` vs `deconvolution` heatmaps and set `detection.primary` to whichever tracks the
true signal better.

---

## Output columns (`results.csv`)

`file, scene, key, alias, pct_sabg, tissue_px, positive_px, artifact_px, fold_px,
tissue_area_mm2, sabg_area_mm2, artifact_area_mm2, fold_area_mm2, threshold,
threshold_secondary, threshold_method, require_agreement, primary, pixel_size_um,
process_zoom` (+ `animal, group, tissue, treatment, day, tag` when metadata is joined).
`alias` is the short id used in filenames; `tissue_px` is *after* artifact/border (and,
when `fold.enabled` with `exclude_from_tissue`, fold-band) exclusion, so
`pct_sabg = positive_px / tissue_px`. `fold_px` reports the linear-fold band area.

## Notes
- These are **brightfield** SABG slides (X-Gal chromogen), so detection is color-based,
  not a fluorescent green/blue channel split.
- Default stain vectors were set from the example slides; for a very different staining
  batch, set `detection.auto_estimate: true` or supply `detection.stain_matrix`.
- The overlay marks an overview pixel positive if *any* full-res positive falls in it, so
  punctate signal stays visible; the reported `%` always uses exact full-res counts.

## Module map
`czi_io` (read/tiles/label) · `tissue` (masking + dark-artifact rejection) · `fold`
(linear-fold rejection) · `scoring` (deconvolution + opponent) · `threshold` (robust auto
threshold) · `overlay` (QC images) · `pipeline` (orchestration) · `export` (FOV figures) ·
`whitebalance` · `metadata` (sections.csv) · `progress` · `config` · `cli` ·
`sabg_gui.py` (Tkinter front-end).
