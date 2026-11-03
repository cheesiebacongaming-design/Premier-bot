"""Shared font, color, and drawing helpers for Pillow renderers."""

from pathlib import Path
import io

from PIL import Image, ImageDraw, ImageFont, ImageOps


FONT_REG = [
    "segoeui.ttf", "arial.ttf", "DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
]
FONT_BOLD = [
    "segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
]
FONT_SERIF = [
    "Georgia.ttf", "DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf",
]
FONT_SERIF_BOLD = [
    "Georgia Bold.ttf", "DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSerif-Bold.ttf",
]
MATCH_FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"

COLS = [("ACS", 500), ("K", 565), ("D", 610), ("A", 655), ("+/-", 715), ("K/D", 780), ("ADR", 845), ("HS%", 910), ("FB", 965)]
GREY = (150, 162, 175)
WHITE = (240, 244, 248)
PLAY_TAG_STYLES = {
    "MVP": ((109, 79, 38), (255, 214, 128)),
    "LVP": ((105, 39, 45), (255, 151, 157)),
    "VICTIM": ((67, 68, 78), (211, 214, 224)),
    "ACE": ((141, 91, 32), (255, 223, 132)),
    "QUAD": ((119, 93, 33), (255, 218, 119)),
    "ENTRY": ((39, 78, 122), (163, 205, 255)),
    "DINK MACHINE": ((89, 59, 126), (219, 183, 255)),
    "ASSIST KING": ((35, 100, 91), (153, 235, 214)),
    "DAMAGE DEALER": ((137, 68, 37), (255, 187, 143)),
    "SURVIVOR": ((48, 98, 63), (170, 237, 178)),
    "CLUTCH": ((123, 55, 92), (255, 172, 218)),
}


def font(size, bold=False, serif=False):
    candidates = (FONT_SERIF_BOLD if bold else FONT_SERIF) if serif else (FONT_BOLD if bold else FONT_REG)
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def match_font(size, bold=False, serif=False):
    """Use bundled match-card fonts so Windows and Linux render consistently."""
    name = ("Lora-Bold.ttf" if bold else "Lora-Regular.ttf") if serif else (
        "Roboto-Bold.ttf" if bold else "Roboto-Regular.ttf"
    )
    try:
        return ImageFont.truetype(str(MATCH_FONT_DIR / name), size)
    except OSError:
        return font(size, bold=bold, serif=serif)


def draw_soft_box(image, box, fill, opacity=224, radius=4):
    """Draw a lightly transparent, antialiased chip on an existing RGB image."""
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    scale = 4
    layer = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    ImageDraw.Draw(layer).rounded_rectangle(
        (0, 0, width * scale - 1, height * scale - 1),
        radius=radius * scale,
        fill=(*fill, opacity),
    )
    layer = layer.resize((width, height), Image.Resampling.LANCZOS)
    base = image.crop((left, top, right, bottom)).convert("RGBA")
    base.alpha_composite(layer)
    image.paste(base.convert("RGB"), (left, top))


def load_icon_image(path, size):
    """Load, contain, and center a cached icon image."""
    source = Image.open(path).convert("RGBA")
    fitted = ImageOps.contain(source, (size, size), method=Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.alpha_composite(fitted, ((size - fitted.width) // 2, (size - fitted.height) // 2))
    return canvas


def encode_png(image):
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    return buffer


def wrap_image_text(draw, text, image_font, max_width, max_lines=2):
    words, lines, current, truncated = text.split(), [], "", False
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and draw.textlength(candidate, font=image_font) > max_width:
            lines.append(current)
            current = word
            if len(lines) == max_lines:
                truncated = True
                break
        else:
            current = candidate
    if current and len(lines) < max_lines:
        lines.append(current)
    if truncated and lines:
        lines[-1] = lines[-1].rstrip(" .") + "..."
    return lines
