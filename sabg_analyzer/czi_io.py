"""CZI input/output.

Big pixel data is read at full resolution in tiles with Zeiss `pylibCZIrw`.
The embedded slide ``Label`` (and ``SlidePreview``) attachments are read with
`czifile`, because pylibCZIrw exposes no attachment API.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import numpy as np
from pylibCZIrw import czi as pyczi


# ---------------------------------------------------------------------------
# Scene metadata
# ---------------------------------------------------------------------------
@dataclass
class SceneInfo:
    """One tissue section (= one CZI scene)."""

    file_stem: str
    path: str
    scene_index: int
    x: int          # global full-res origin of the scene bounding rectangle
    y: int
    w: int          # full-res width / height
    h: int
    pixel_size_um: Optional[float]

    @property
    def key(self) -> str:
        """Stable id used in configs / filenames, e.g. ``2026_05_29__10215:0``."""
        return f"{self.file_stem}:{self.scene_index}"

    @property
    def slug(self) -> str:
        """Filesystem-safe id, e.g. ``2026_05_29__10215_s0``."""
        return f"{self.file_stem}_s{self.scene_index}"


def list_czi_files(data_dir: str | Path) -> list[Path]:
    """All ``*.czi`` files in *data_dir*, sorted by name."""
    return sorted(Path(data_dir).glob("*.czi"))


def get_pixel_size_um(raw_metadata: str) -> Optional[float]:
    """Parse the X pixel size (metres in the XML) and return micrometres/px."""
    m = re.search(
        r'<Distance Id="X">.*?<Value>([^<]+)</Value>', raw_metadata, re.S
    )
    if not m:
        return None
    try:
        return float(m.group(1)) * 1e6
    except ValueError:
        return None


def _fmt_ms(ms: str) -> str:
    """Format a millisecond duration string as ``H:MM:SS`` (best effort)."""
    try:
        s = float(ms) / 1000.0
    except ValueError:
        return ms
    h, rem = divmod(int(s), 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def get_scan_metadata(raw_metadata: str) -> dict[str, str]:
    """Best-effort acquisition metadata from the CZI XML (only found fields returned).

    Mirrors the per-section "Acquisition Information" panel in ZEN
    (see ``misc/screenshots/5.jpg``): µm/px, magnification, objective + NA, bit depth,
    acquisition date + duration, microscope, contrast method, light-source intensity,
    flash duration, camera, binning, depth of focus, compression, channel names. Each
    item is looked up independently so a missing tag just omits that key.
    """
    out: dict[str, str] = {}
    px = get_pixel_size_um(raw_metadata)
    if px is not None:
        out["pixel_size_um"] = f"{px:.4f}"

    def _first(pattern: str) -> str | None:
        m = re.search(pattern, raw_metadata, re.S)
        return m.group(1).strip() if m else None

    mag = _first(r"<NominalMagnification>([^<]+)</NominalMagnification>")
    if mag:
        try:
            out["magnification"] = f"{float(mag):g}x"
        except ValueError:
            out["magnification"] = mag
    date = (_first(r"<AcquisitionDateAndTime>([^<]+)</AcquisitionDateAndTime>")
            or _first(r"<AcquisitionDate>([^<]+)</AcquisitionDate>"))
    if date:
        out["acquired"] = date
    dur = _first(r"<AcquisitionDuration>([^<]+)</AcquisitionDuration>")
    if dur:
        out["acquisition_duration"] = _fmt_ms(dur)
    # simple single-value tags -> output key
    for tag, key in (
        ("ObjectiveName", "objective"),
        ("LensNA", "effective_na"),
        ("ComponentBitCount", "bit_depth"),
        ("ContrastMethod", "contrast_method"),
        ("IlluminationType", "illumination"),
        ("FlashDuration", "flash_duration"),
        ("CameraName", "camera"),
        ("DepthOfFocus", "depth_of_focus"),
        ("CompressionMethod", "compression"),
    ):
        v = _first(rf"<{tag}[^>]*>([^<]+)</{tag}>")
        if v:
            out[key] = v
    mic = _first(r'<Microscope[^>]*\sName="([^"]+)"')
    if mic:
        out["microscope"] = mic
    binning = _first(r"<Binning[^>]*>([^<]+)</Binning>")
    if binning:
        out["binning"] = binning
    intensity = _first(r"<Intensity[^>]*>([0-9.]+\s*%)</Intensity>")  # the "% " form
    if intensity:
        out["light_intensity"] = intensity
    chans = re.findall(r'<Channel[^>]*\sName="([^"]+)"', raw_metadata)
    if chans:
        # de-duplicate, keep order
        seen: list[str] = []
        for c in chans:
            if c not in seen:
                seen.append(c)
        out["channels"] = ", ".join(seen)
    return out


def list_scenes(path: str | Path) -> list[SceneInfo]:
    """Open a CZI and describe every scene (tissue section) it contains."""
    path = Path(path)
    scenes: list[SceneInfo] = []
    with pyczi.open_czi(str(path)) as doc:
        px = get_pixel_size_um(doc.raw_metadata)
        rects = doc.scenes_bounding_rectangle
        if rects:
            for idx, rect in rects.items():
                scenes.append(
                    SceneInfo(path.stem, str(path), int(idx),
                              rect.x, rect.y, rect.w, rect.h, px)
                )
        else:
            # Single-scene file: fall back to the total bounding rectangle.
            r = doc.total_bounding_rectangle
            scenes.append(SceneInfo(path.stem, str(path), 0,
                                    r.x, r.y, r.w, r.h, px))
    return scenes


# ---------------------------------------------------------------------------
# Pixel reading (pylibCZIrw)
# ---------------------------------------------------------------------------
def _bgr_to_rgb(arr: np.ndarray) -> np.ndarray:
    """pylibCZIrw returns Bgr24 as (Y, X, 3) in BGR order -> RGB uint8."""
    return np.ascontiguousarray(arr[..., ::-1])


def read_region(doc, x: int, y: int, w: int, h: int, zoom: float) -> np.ndarray:
    """Read a global ROI at *zoom* and return an RGB uint8 array (Y, X, 3)."""
    arr = doc.read(roi=(x, y, w, h), zoom=zoom, pixel_type="Bgr24")
    return _bgr_to_rgb(arr[..., :3])


def iter_tiles(
    doc, scene: SceneInfo, tile_size: int, zoom: float
) -> Iterator[tuple[np.ndarray, int, int, int, int]]:
    """Stream a scene as tiles read at *zoom*.

    Yields ``(rgb, off_x, off_y, tw, th)`` where ``off_x/off_y`` and ``tw/th``
    are full-res pixel position/extent relative to the scene origin (so callers
    can map the tile onto an overview canvas). ``rgb`` itself is at *zoom* scale.
    """
    for off_y in range(0, scene.h, tile_size):
        th = min(tile_size, scene.h - off_y)
        for off_x in range(0, scene.w, tile_size):
            tw = min(tile_size, scene.w - off_x)
            rgb = read_region(doc, scene.x + off_x, scene.y + off_y, tw, th, zoom)
            yield rgb, off_x, off_y, tw, th


def acquired_tiles(scene: SceneInfo) -> list[tuple[int, int, int, int]]:
    """Global-coord ``(x, y, w, h)`` rectangles of the acquired full-res mosaic tiles.

    Read from the CZI sub-block directory via ``czifile`` (pylibCZIrw exposes no tile
    geometry). ZEN tissue-detection scans acquire an *irregular* footprint, so the tile
    positions live only here. Coordinates share the frame of ``scene.x/scene.y``, so a
    caller maps a tile onto any canvas with that canvas's ``scale`` from :func:`read_overview`.
    """
    import czifile
    out: list[tuple[int, int, int, int]] = []
    with czifile.CziFile(scene.path) as czi:
        sbs = list(czi.filtered_subblock_directory)
        if not sbs:
            return out
        ax = {d: i for i, d in enumerate(sbs[0].dims)}
        if "X" not in ax or "Y" not in ax:
            return out
        for s in sbs:
            if s.is_pyramid or s.scene_index != scene.scene_index:
                continue
            out.append((s.start[ax["X"]], s.start[ax["Y"]],
                        s.shape[ax["X"]], s.shape[ax["Y"]]))
    return out


def read_overview(
    doc, scene: SceneInfo, max_edge: int | None = None, zoom_cap: float = 1.0,
    um_per_px: float | None = None,
) -> tuple[np.ndarray, float]:
    """Read a whole scene downscaled for display/QC.

    Sizing is by **magnification** (*um_per_px*, a fixed physical resolution so the
    canvas is proportional to the section's physical size), bounded by an optional
    *max_edge* px safety cap and never upscaled past *zoom_cap* (full res).
    Returns ``(rgb, scale)`` where ``scale`` is overview-px per full-res-px.
    """
    long_edge = max(scene.w, scene.h)
    scale = zoom_cap
    if um_per_px and scene.pixel_size_um:
        scale = min(scale, scene.pixel_size_um / um_per_px)
    if max_edge and long_edge:
        scale = min(scale, max_edge / long_edge)
    scale = min(scale, 1.0)                       # never upscale
    rgb = read_region(doc, scene.x, scene.y, scene.w, scene.h, scale)
    return rgb, scale


# ---------------------------------------------------------------------------
# Attachment images (czifile)
# ---------------------------------------------------------------------------
def _to_uint8_rgb(arr: np.ndarray) -> np.ndarray:
    """Normalise an attachment image (often uint16) to 8-bit RGB."""
    arr = np.asarray(arr)
    if arr.ndim == 2:
        arr = np.stack([arr] * 3, axis=-1)
    if arr.shape[-1] == 4:
        arr = arr[..., :3]
    if arr.dtype != np.uint8:
        a = arr.astype(np.float64)
        hi = a.max()
        if hi <= 255:
            arr = a.clip(0, 255).astype(np.uint8)
        else:
            lo = a.min()
            rng = hi - lo if hi > lo else 1.0
            arr = ((a - lo) / rng * 255).clip(0, 255).astype(np.uint8)
    return np.ascontiguousarray(arr)


def extract_attachment_image(path: str | Path, name: str) -> Optional[np.ndarray]:
    """Return a named attachment image (e.g. ``"Label"``) as uint8 RGB, or None."""
    import czifile

    with czifile.CziFile(str(path)) as czi:
        for att in czi.attachments():
            if att.attachment_entry.name == name:
                try:
                    return _to_uint8_rgb(att.data())
                except Exception:
                    return None
    return None


def extract_label(path: str | Path) -> Optional[np.ndarray]:
    """The slide ``Label`` image (handwritten/printed slide label) as uint8 RGB."""
    return extract_attachment_image(path, "Label")
