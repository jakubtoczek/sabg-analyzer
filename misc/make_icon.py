"""Generate sabg_gui/assets/sabg_analyzer.ico (+ .png): a tissue section on a dark tile,
some of its cells stained SA-beta-gal blue - what the app counts.

Run: python misc/make_icon.py  (needs Pillow, which scikit-image already brings)
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter

S = 1024          # drawn big, then downsampled - cheaper than antialiasing by hand
BG, TISSUE, EDGE, BLUE, CELL = "#232323", "#e6b8c0", "#c98e9b", "#2f6fd0", "#b9808e"

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle([0, 0, S - 1, S - 1], radius=int(S * 0.21), fill=BG)

# the section: a lumpy round slice, drawn as overlapping discs then softened
blob = Image.new("L", (S, S), 0)
b = ImageDraw.Draw(blob)
for cx, cy, r in ((0.50, 0.52, 0.31), (0.36, 0.40, 0.17), (0.64, 0.38, 0.16),
                  (0.33, 0.64, 0.15), (0.66, 0.66, 0.16)):
    b.ellipse([S * (cx - r), S * (cy - r), S * (cx + r), S * (cy + r)], fill=255)
blob = blob.filter(ImageFilter.GaussianBlur(S * 0.02)).point(lambda v: 255 if v > 128 else 0)
img.paste(Image.new("RGBA", (S, S), EDGE), (0, 0), blob)
inner = blob.filter(ImageFilter.MinFilter(int(S * 0.025) | 1))
img.paste(Image.new("RGBA", (S, S), TISSUE), (0, 0), inner)

# unstained nuclei (small, pink) and the stained cells (big, blue): big enough for 16 px
for cx, cy in ((0.30, 0.50), (0.45, 0.30), (0.58, 0.55), (0.42, 0.70), (0.70, 0.46), (0.52, 0.42)):
    r = S * 0.025
    d.ellipse([S * cx - r, S * cy - r, S * cx + r, S * cy + r], fill=CELL)
for cx, cy, r in ((0.40, 0.46, 0.085), (0.63, 0.34, 0.07), (0.62, 0.68, 0.08)):
    d.ellipse([S * (cx - r), S * (cy - r), S * (cx + r), S * (cy + r)], fill=BLUE)

out = Path(__file__).resolve().parent.parent / "sabg_gui" / "assets"
out.mkdir(parents=True, exist_ok=True)
img.resize((256, 256), Image.LANCZOS).save(out / "sabg_analyzer.png")
img.save(out / "sabg_analyzer.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64),
                                          (128, 128), (256, 256)])
print("wrote", out / "sabg_analyzer.ico")
