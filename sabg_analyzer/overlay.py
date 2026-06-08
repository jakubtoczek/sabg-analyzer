"""QC overlay and debug image exports.

The overlay is drawn on the small scene *overview* image. The positive mask is
max-pooled down from full res (done in the pipeline) so punctate SABG pixels
survive the shrink and stay visible.
"""

from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np


def log_written(out_dir: str | Path, paths, max_per_dir: int = 10) -> None:
    """Print a compact 'wrote' summary of *paths*, one line per directory.

    Paths are shown relative to *out_dir*; long lists are truncated with a count.
    """
    from collections import defaultdict
    out_dir = Path(out_dir)
    groups: "defaultdict[str, list[str]]" = defaultdict(list)
    for p in paths:
        if p is None:
            continue
        p = Path(p)
        try:
            rel = p.relative_to(out_dir)
        except ValueError:
            rel = p
        groups[rel.parent.as_posix()].append(rel.name)
    for d, names in groups.items():
        shown = ", ".join(names[:max_per_dir])
        if len(names) > max_per_dir:
            shown += f", … (+{len(names) - max_per_dir} more)"
        print(f"          + {d}/: {shown}")


def save_rgb(path: str | Path, rgb: np.ndarray) -> None:
    """Write an RGB uint8 image to *path* (creating parent dirs)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2BGR))


def save_jpg(path: str | Path, rgb: np.ndarray, quality: int = 90) -> None:
    """Write an RGB image as JPEG (much smaller; for photographic QC images)."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), cv2.cvtColor(np.ascontiguousarray(rgb), cv2.COLOR_RGB2BGR),
                [cv2.IMWRITE_JPEG_QUALITY, quality])


def alpha_overlay(
    rgb: np.ndarray,
    mask: np.ndarray,
    color: tuple[int, int, int] = (255, 0, 0),
    alpha: float = 0.60,
) -> np.ndarray:
    """Blend *color* onto *rgb* where *mask* is True, at the given *alpha*."""
    out = rgb.astype(np.float32).copy()
    col = np.array(color, np.float32)
    m = mask.astype(bool)
    out[m] = (1.0 - alpha) * out[m] + alpha * col
    return out.clip(0, 255).astype(np.uint8)


def two_color_overlay(
    rgb: np.ndarray,
    sabg_mask: np.ndarray,
    artifact_mask: np.ndarray,
    sabg_color: tuple[int, int, int] = (0, 200, 0),
    artifact_color: tuple[int, int, int] = (220, 0, 0),
    sabg_alpha: float = 0.60,
    artifact_alpha: float = 0.60,
) -> np.ndarray:
    """Red = excluded fold/debris, green = SABG+. Artifact first, SABG on top.

    Each mask blends at its own alpha.
    """
    out = alpha_overlay(rgb, artifact_mask, artifact_color, artifact_alpha)
    out = alpha_overlay(out, sabg_mask, sabg_color, sabg_alpha)
    return out


def composite_overlay(rgb: np.ndarray, layers) -> np.ndarray:
    """Blend a sequence of ``(mask, color, alpha)`` layers onto *rgb* in order
    (later layers paint on top). None/empty masks are skipped."""
    out = rgb
    for mask, color, alpha in layers:
        if mask is not None and bool(np.any(mask)):
            out = alpha_overlay(out, mask, color, alpha)
    return out


def _heatmap(score: np.ndarray, thr: float | None = None) -> np.ndarray:
    """Render a score array as an 8-bit RGB heatmap (turbo); mark *thr* if given."""
    s = score.astype(np.float32)
    lo, hi = np.percentile(s, 1), np.percentile(s, 99)
    if hi <= lo:
        hi = lo + 1e-6
    norm = ((s - lo) / (hi - lo)).clip(0, 1)
    u8 = (norm * 255).astype(np.uint8)
    bgr = cv2.applyColorMap(u8, cv2.COLORMAP_TURBO)
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)


def save_debug_panels(
    out_dir: str | Path,
    slug: str,
    overview_rgb: np.ndarray,
    tissue_mask: np.ndarray,
    opp_score: np.ndarray,
    deconv_score: np.ndarray,
    pos_mask: np.ndarray,
    artifact_mask: np.ndarray,
    thr: float,
    primary: str,
    fold_mask: np.ndarray | None = None,
    edge_mask: np.ndarray | None = None,
    full: bool = False,
) -> None:
    """Write per-scene debug images used to audit segmentation.

    The 6-panel ``*_compare.jpg`` (overview, tissue, both scores, masks, overlay)
    is always written. The large standalone full-res heatmaps/masks are only
    written when *full* is True (they are redundant with the compare panel and
    cost ~10x the disk).
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Side-by-side comparison figure (always).
    _save_compare(
        out_dir / f"{slug}_compare.jpg",
        overview_rgb, tissue_mask, opp_score, deconv_score, pos_mask,
        artifact_mask, fold_mask, edge_mask, thr, primary,
    )

    if not full:
        return

    tissue_vis = overview_rgb.copy()
    tissue_vis[~tissue_mask.astype(bool)] = (
        0.4 * tissue_vis[~tissue_mask.astype(bool)]
    ).astype(np.uint8)
    save_jpg(out_dir / f"{slug}_tissue.jpg", tissue_vis)

    for name, score in (("opponent", opp_score), ("deconv", deconv_score)):
        hm = _heatmap(score)
        hm[~tissue_mask.astype(bool)] = 30
        save_jpg(out_dir / f"{slug}_score_{name}.jpg", hm)

    mask_vis = np.zeros((*pos_mask.shape, 3), np.uint8)
    mask_vis[pos_mask.astype(bool)] = (255, 255, 255)
    save_rgb(out_dir / f"{slug}_mask.png", mask_vis)   # binary -> PNG


def _save_compare(path, rgb, tissue, opp, deconv, pos, artifact, fold, edge,
                  thr, primary) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fold = fold if fold is not None else np.zeros_like(pos, bool)
    edge = edge if edge is not None else np.zeros_like(pos, bool)
    # Size the 2x3 figure to ~5x the overview so panels are legible (relative to
    # the overview, not a constant width). Cap to keep matplotlib/file size sane.
    h, w = rgb.shape[:2]
    cell_w = float(np.clip(w / 90.0 * 5.0, 5.0, 26.0))   # inches per panel column
    cell_h = cell_w * (h / max(w, 1))
    fig, ax = plt.subplots(2, 3, figsize=(3 * cell_w, 2 * cell_h))
    ax = ax.ravel()
    ax[0].imshow(rgb); ax[0].set_title("overview")
    o = opp.copy().astype(float); o[~tissue.astype(bool)] = np.nan
    ax[1].imshow(o, cmap="turbo"); ax[1].set_title("opponent score")
    d = deconv.copy().astype(float); d[~tissue.astype(bool)] = np.nan
    ax[2].imshow(d, cmap="turbo"); ax[2].set_title("deconvolution score")
    # masks: green = SABG+, red = dark artifact, orange = fold band, blue = edge-shadow
    masks = np.zeros((*pos.shape, 3), np.uint8)
    masks[fold.astype(bool)] = (255, 140, 0)
    masks[artifact.astype(bool)] = (220, 0, 0)
    masks[edge.astype(bool)] = (60, 120, 255)
    masks[pos.astype(bool)] = (0, 200, 0)
    ax[3].imshow(masks)
    ax[3].set_title(f"SABG+ grn / artifact red / fold org / edge blu, thr={thr:.3f}")
    ax[4].imshow(composite_overlay(rgb, [(artifact, (220, 0, 0), 0.6),
                                         (fold, (255, 140, 0), 0.55),
                                         (edge, (60, 120, 255), 0.5),
                                         (pos, (0, 200, 0), 0.6)]))
    ax[4].set_title(f"overlay ({primary}+agreement)")
    ax[5].imshow(tissue, cmap="gray"); ax[5].set_title("tissue mask")
    for a in ax:
        a.axis("off")
    fig.tight_layout()
    fig.savefig(path, dpi=90, pil_kwargs={"quality": 88})
    plt.close(fig)
