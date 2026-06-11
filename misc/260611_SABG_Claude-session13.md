# SABG Analyzer — session 13 worklog (2026-06-11)  [Fable 5]

Resumed from `misc/260611_SABG_Claude-session12-handover.md`. Jakub narrowed this turn to
three pieces (plan: `…/plans/read-code-sabg-analyzer-misc-260611-sabg-magical-grove.md`):
A) layer order (#5, scoped) + edge recolour, B) optional/tunable intensity (OD)
quantification, C) README rework. Local-commit on `main`, **no push**. Sliders/colour-review
beyond edge deferred to session 14 (feedback CSV).

---

## A — Layer order + edge recolour  (commit 1)

**Pipeline-order claim verified** (Jakub: excluded, non-tissue, artifact, candidate SABG,
fold, edge-rejected, SABG): `masks.py:80` `t = region_c & raw & ~art` already excludes
non-tissue + artifact (manual exclusion upstream); `candidate = pos.copy()` (`:131`) is the
pre-rejection superset; `pos &= ~fold` (`:134`) then edge `refine_positive` (`:137`) remove
fold + edge; final `sabg = pos` (+ small `expand_px`). ✓

**Changes**
- `sabg_gui_widgets.py` `LAYER_SPEC` reordered to composite draw order (bottom→top):
  `nontissue, excluded, artifact, sabg_candidate, fold, edge_removed, sabg` — SABG+ now
  painted **on top**; candidate moved below fold; edge-rejected below SABG. Comment updated.
  `LAYER_PANEL_SPEC` formula unchanged (floats `excluded` to front) → panel order now
  `excluded, nontissue, artifact, sabg_candidate, fold, edge_removed, sabg` (the narrative).
- `config.py` + `config.example.yaml`: `overlay.edge_color` `(60,120,255)`→`(138,43,226)`
  (violet, high-contrast), `edge_alpha` 0.50→0.60. Tunable; final shade is Jakub's CSV call.
- Verified `export.OVERLAY_PROFILES["overlay"]` already draws `pos` after `edge` (matches),
  and `_overlay_order` iterates `LAYER_SPEC` (no code change). Recolour flows through
  `cfg.overlay.edge_color` in both preview and `export.py:252`.

Panel-vs-draw: only the excluded↔non-tissue pair differs (non-tissue drawn below excluded so
magenta stays visible) — same as before.

Verify: `compileall` OK; `LAYER_SPEC`/panel orders + `edge_color=(138,43,226)`/`alpha=0.6`
printed as expected.

---

## B — Optional, tunable intensity (OD) quantification, default OFF  (commit 2)

The A3.3 intensity columns were always-on. Now gated by a config toggle, **off by default**
(area-only is the clean baseline). "OD-weighted" = integrated OD (area x intensity).

**Changes**
- `config.py`: new `IntensityParams(enabled=False)` dataclass; `Config.intensity` field
  (after `threshold`); `load_config` parses an `intensity:` block (explicit-section loader).
- `pipeline.py` `analyze_scene`: `od_sum` accumulation gated on `cfg.intensity.enabled`
  (perf win when off); `sabg_integrated_od`/`sabg_mean_od` keys inserted into the row
  **only when enabled** (right after `pct_sabg`). Same gating in `_empty_row`. results.csv
  columns come from `pd.DataFrame(rows)` dict keys, so omitting keys omits columns — no
  fieldname plumbing. `_build_config_snapshot` emits `"intensity": {"enabled": ...}` so the
  analyze snapshot + GUI Save round-trip it.
- `sabg_gui_widgets.py`: `INTENSITY_FIELDS` + an "Intensity quantification" group in
  `OTHER_GROUPS` (Config window → "Other settings" tab, between Output artifacts and White
  balance). Bool → checkbox via existing `build_groups`.
- `config.example.yaml`: documented `intensity:` block (`enabled: false`).

Edge case (acceptable): a `--continue` resume mixing enabled/disabled runs yields
pandas-union columns with NaN in the rows from the other mode.

Verify: `compileall` OK; default `enabled=False`; snapshot block present; enable→dump→reload
round-trips True; "Intensity quantification" registered in `OTHER_GROUPS`. Column on/off
proven by the functional 77_Ctl run (see Verification).
