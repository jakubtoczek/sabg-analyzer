"""Generate sabg_gui/assets/sabg_analyzer.ico (+ .png): a grey section of spindle-shaped
cells on a dark tile, three of them stained SA-beta-gal blue-green around the nucleus -
what the app measures. Colours from an SA-beta-gal micrograph: grey field, stain hue ~160.

Run: python misc/make_icon.py  (needs Pillow, which scikit-image already brings)
"""

from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

S = 1024          # drawn big, then downsampled - cheaper than antialiasing by hand
BG, FIELD, EDGE = "#232323", "#c8cac7", "#9c9f9c"
CELL, NUCLEUS = "#9da19e", "#7b807d"            # unstained: grey
STAIN, CORE = "#12917a", "#cfe9e1"              # SA-beta-gal blue-green, pale nucleus

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(img).rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.21), fill=BG)

# the section: one smooth, slightly irregular slice
blob = Image.new("L", (S, S), 0)
b = ImageDraw.Draw(blob)
for cx, cy, r in ((0.50, 0.51, 0.33), (0.40, 0.42, 0.22), (0.61, 0.60, 0.22)):
    b.ellipse([S * (cx - r), S * (cy - r), S * (cx + r), S * (cy + r)], fill=255)
blob = blob.filter(ImageFilter.GaussianBlur(S * 0.03)).point(lambda v: 255 if v > 128 else 0)
img.paste(Image.new("RGBA", (S, S), EDGE), (0, 0), blob)
inner = blob.filter(ImageFilter.MinFilter(int(S * 0.02) | 1))
img.paste(Image.new("RGBA", (S, S), FIELD), (0, 0), inner)
cells = Image.new("RGBA", (S, S), (0, 0, 0, 0))   # clipped to the section at the end


def cell(cx, cy, length, width, angle, body, nucleus, k=0.45):
    """A spindle-shaped cell: an ellipse along *angle* (degrees) with an oval nucleus."""
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x, y, l, w = S * cx, S * cy, S * length / 2, S * width / 2
    d.ellipse([x - l, y - w, x + l, y + w], fill=body)
    d.ellipse([x - l * k, y - w * 0.55, x + l * k, y + w * 0.55], fill=nucleus)
    cells.alpha_composite(layer.rotate(angle, center=(x, y), resample=Image.BICUBIC))


for cx, cy, a in ((0.29, 0.57, 75), (0.48, 0.25, 5), (0.75, 0.51, 100), (0.47, 0.52, 40),
                  (0.37, 0.76, 165), (0.71, 0.75, 55)):
    cell(cx, cy, 0.21, 0.05, a, CELL, NUCLEUS, k=0.3)
for cx, cy, a in ((0.36, 0.39, 140), (0.63, 0.35, 25), (0.57, 0.66, 115)):
    cell(cx, cy, 0.22, 0.13, a, STAIN, CORE, k=0.35)
cells.putalpha(ImageChops.multiply(cells.getchannel("A"), inner))
img.alpha_composite(cells)

out = Path(__file__).resolve().parent.parent / "sabg_gui" / "assets"
out.mkdir(parents=True, exist_ok=True)
img.resize((256, 256), Image.LANCZOS).save(out / "sabg_analyzer.png")
img.save(out / "sabg_analyzer.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                                          (128, 128), (256, 256)])
print("wrote", out / "sabg_analyzer.ico")
