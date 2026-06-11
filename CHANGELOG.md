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

## [Unreleased] — 0.2.0

### Added
- **Versioning.** Single-source `__version__`, `sabg_analyzer --version` flag, version in the
  GUI title and a `v0.2.0` label in the button row, `analyzer_version` column in `results.csv`,
  and this changelog.

_(Session-14 feature items land here as they ship.)_

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

[Unreleased]: https://github.com/jakubtoczek/sabg-analyzer/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/jakubtoczek/sabg-analyzer/releases/tag/v0.1.0
