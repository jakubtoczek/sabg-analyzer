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
