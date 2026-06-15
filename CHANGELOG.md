# Changelog

All notable changes to **SABG Analyzer** are recorded here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

**Versioning:** [Semantic Versioning](https://semver.org/), pre-1.0 (`0.MINOR.PATCH`). While
the tool is in beta:

- **MINOR** (`0.2.0`) — a feature-bearing release: new features, new config parameters, or
  schema additions. Pre-1.0, even breaking changes ride a MINOR bump.
- **PATCH** (`0.2.1`) — bugfix / polish only, no new capability.

One tagged release per feature-bearing work session. `__version__`
(`sabg_analyzer/__init__.py`) is the single source of truth and is stamped into every
`results.csv` row (`analyzer_version` column) for provenance. It reflects the in-progress
minor; the git tag (`vX.Y.Z`) marks when that minor is blessed.

A move to **1.0.0** requires: config schema frozen, GUI/UX settled, a minimal
smoke/regression test, finalized docs, and a successful run by a second user.

## [Unreleased] — 0.5.0

Session 17.

### Added
- **Live (non-burned) scale bar** in Preview/Tune: a new "on image" toggle in the scale-bar strip
  draws the bar on the image itself, following the existing length / label / corner controls. It
  stays in its corner while panning and rescales while zooming (length is held in physical µm), so
  it reads as a true scale at any zoom. Export still burns its own bar into the saved files.

### Changed
- **Default detection parameters → session-16 tuning (cfg07):** `threshold.scale` 0.825 → **0.70**
  (a free win — every treated section rose, negatives stayed flat) and the teal grow/expand floors
  `detection.hyst_teal_min` / `expand_teal_min` 0.04 → **0.02** (lifts faint treated teal, negatives
  held at the baseline floor). The aggressive teal-0.01 (cfg09) option stays available in config.
- **Tuning sliders re-centred on the defaults:** each guided sensitivity slider now opens at its
  midpoint (50) when the parameter is at its program default — notably `threshold.scale` centred on
  0.70. Ranges were adjusted to keep the default at centre; the raw entry still accepts any value.
- **Info slide-label default rotation** flipped 180° (`gui.label_rotate_quarter_turns` 1 → **3**) so
  labels load upright the intended way (the previous default was upside-down).
- **More compact tuning panel:** each detection stage is now a single always-visible row (composite
  slider + inline Reset); the per-stage description moved into the "details" expander and the slider
  tooltip, so all five stages fit at 1320×840 with details collapsed (no scrolling).

### Removed
- The scale-bar **preview schematic** (the small white square showing the bar's corner) — the new
  "on image" live bar shows the real placement directly.

## [0.4.0] — 2026-06-12

Session 16.

### Added
- **Result staleness indicator** (Preview tuning panel): a provenance line under the Result
  shows which section + ROI the numbers came from and flips amber ("view changed — recompute")
  once the section or ROI moves on.
- **Configurable default layer visibility** (`gui.layer_defaults`): the Layers panel now starts
  candidate-on / SABG-off by default so the pre-rejection teal is what you audit first.
- **Configurable default slide-label rotation** (`gui.label_rotate_quarter_turns`, default 90°
  CCW): labels are scanned sideways, so the Info viewer starts them upright.

### Changed
- **Selectable Info text:** the Info viewer title and the file/scene table cells are now
  copy-selectable (read-only entries), appearance unchanged.
- **Compact tuning panel:** tightened per-stage and panel spacing so the detection sliders fit
  without scrolling; candidate overlay alpha default raised 0.30 → 0.45 (now a default-on layer).

## [0.3.0] — 2026-06-12

Session 15.

### Added
- **Per-section ROI + view memory.** The Preview remembers each section's ROI rectangle and
  zoom/pan; a remembered rect shows as a dashed outline and Draw ROI edits it (Clear ROI forgets
  it).
- **Optional `sabg_od_per_tissue` intensity column** (D3): mean OD accumulated over tissue
  pixels, off by default (needs intensity `enabled`). Clearer per-metric tooltips.

### Changed
- **Section list:** focus the current button so Up/Down arrow navigation works in both the
  Preview and Info pickers.
- **Compact sliders (step 1):** dropped the per-slider sensitivity/strength sub-label from the
  detection tuning panel; end-labels and the slider tooltip already convey it.

### Fixed
- Grey out the section-list **"%SABG ↓"** order until a `results.csv` exists to sort by.

## [0.2.0] — 2026-06-11

Session 14.

### Added
- **Versioning.** Single-source `__version__`, `sabg_analyzer --version` flag, version in the
  GUI title and a `v…` label in the button row, `analyzer_version` column in `results.csv`,
  and this changelog.
- **Section-list ordering** (Preview + Info): a current-section marker, arrow-key navigation,
  and selectable order modes — **Scan order** (default), **Alias A–Z**, **%SABG ↓**.
- **Unsaved-state warnings:** warn before discarding an unsaved exclusion mask or unsaved
  config changes (section switch / window close).

### Changed
- **Sticky Draw ROI** now edits the pending rectangle instead of erasing it.
- **Preserve the viewed area across a resolution change** (zoom/pan captured as image-fractions
  and re-applied after reload).
- **D6 slider end-labels** read in SABG⁺ outcome terms (`← fewer SABG⁺ / more SABG⁺ →` for
  detection, `← keep more / reject more →` for rejection) with clarifying tooltips.

## [0.1.0] — 2026-06-11

Baseline: the pushed session-13 state. Summarizes development through sessions 8–13.

### Added
- **Full-resolution tiled analysis pipeline** (`scan` → `analyze` → `export`): streams large
  CZI scenes in tiles, gates counting on a cleaned tissue region, computes a robust threshold,
  detects SABG⁺ by deconvolution/opponent scoring with hysteresis, and reports `%SABG` by area
  plus pixel counts and mm² areas per section to `results.csv`.
- **Artifact rejection:** fold/debris and edge-shadow masks; optional linear-fold ridge
  detection (Frangi + structure tensor).
- **Preview/Tune window:** in-app interactive ROI analysis that recomputes on parameter changes
  using the same mask math as the batch pipeline; manual exclusion brush and resizable ROI.
- **In-app Info and Config windows:** edit `sections.csv` (thumbnail picker + table) and
  `config.yaml` (detection tuning + settings) without leaving the GUI.
- **Figure export:** representative-FOV selection (average / deciles / cleanest), display-only
  white balance, and physical scale bars burned into PNG/TIFF output.
- **Optional intensity (OD) metrics**, off by default: `sabg_integrated_od` / `sabg_mean_od`.
- **Composite overlay** with a fixed layer draw order (SABG⁺ on top), violet edge-rejected rims,
  and a candidate (pre-rejection) layer.
- Documented `config.example.yaml` (~40 parameters) and a thorough README.

### Notes
- Single-user local tool: run from `C:\Code\SABG_analyzer\main` with sibling `..\data` /
  `..\outputs`. Not yet pip-installable; no automated tests.

[Unreleased]: https://github.com/jakubtoczek/sabg-analyzer/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/jakubtoczek/sabg-analyzer/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/jakubtoczek/sabg-analyzer/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/jakubtoczek/sabg-analyzer/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/jakubtoczek/sabg-analyzer/releases/tag/v0.1.0
