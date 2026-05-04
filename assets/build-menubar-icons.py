"""Generate macOS menu bar icon variants from the master Contorch logo.

Outputs (all in this directory):
  glyph-template.png         — solid-black silhouette of the C-flame on
                               transparent. Used as `template_image` in
                               rumps so macOS auto-tints it light/dark
                               to match the menu bar appearance.
  glyph-template-pulse.png   — bolder/heavier silhouette for the
                               recording-state pulse second frame.
  glyph-rec.png              — non-template, includes a tiny red dot
                               at the bottom-right. Used when title
                               needs a colour cue (rumps falls back to
                               this if the user disables template mode).

All sized 44x44 for retina (macOS will display at 22pt). Source is
contorch-orange.png. We extract the alpha channel only — the orange
colour itself is irrelevant; what matters is the shape mask.
"""
from pathlib import Path
from PIL import Image, ImageDraw

ASSETS = Path(__file__).resolve().parent
SRC = ASSETS / "contorch-orange.png"

TARGET_SIZE = 44  # 22pt @ 2x retina


def load_silhouette(white_cutoff: int = 230) -> Image.Image:
    """Take the source PNG, return a black-on-transparent silhouette.

    The source PNG from ChatGPT's image generator is RGB (no alpha) —
    its "transparent" background is actually solid white pixels. So we
    can't use the alpha channel as a mask; we have to detect the
    background by luminance. Any pixel below `white_cutoff` luminance
    becomes opaque black, everything brighter becomes transparent.
    """
    src = Image.open(SRC).convert("RGB")
    luma = src.convert("L")
    mask = luma.point(lambda v: 255 if v < white_cutoff else 0)
    out = Image.new("RGBA", src.size, (0, 0, 0, 0))
    black_layer = Image.new("RGBA", src.size, (0, 0, 0, 255))
    out.paste(black_layer, (0, 0), mask)
    return out


def fit(img: Image.Image, size: int) -> Image.Image:
    """Resize keeping aspect ratio, pad to square with transparent."""
    img = img.copy()
    img.thumbnail((size, size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    x = (size - img.width) // 2
    y = (size - img.height) // 2
    canvas.paste(img, (x, y), img)
    return canvas


def make_template():
    """Idle-state monochrome glyph."""
    sil = load_silhouette()
    out = fit(sil, TARGET_SIZE)
    out.save(ASSETS / "glyph-template.png")
    print(f"wrote {ASSETS / 'glyph-template.png'} ({out.size})")


def make_template_pulse():
    """Recording-state second frame — slightly bolder via a higher
    cutoff that catches the soft anti-aliased edges."""
    sil = load_silhouette(white_cutoff=245)  # higher cutoff = thicker mask
    out = fit(sil, TARGET_SIZE)
    out.save(ASSETS / "glyph-template-pulse.png")
    print(f"wrote {ASSETS / 'glyph-template-pulse.png'} ({out.size})")


def make_rec():
    """Recording-state colour glyph — black silhouette + tiny red dot."""
    sil = load_silhouette()
    out = fit(sil, TARGET_SIZE)
    draw = ImageDraw.Draw(out)
    # Bottom-right red dot ~ 25% diameter
    r = TARGET_SIZE // 6
    cx, cy = TARGET_SIZE - r - 1, TARGET_SIZE - r - 1
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(255, 60, 60, 255))
    out.save(ASSETS / "glyph-rec.png")
    print(f"wrote {ASSETS / 'glyph-rec.png'} ({out.size})")


if __name__ == "__main__":
    make_template()
    make_template_pulse()
    make_rec()
