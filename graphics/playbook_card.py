"""Render normalized tactical markers over a supplied top-down map image."""

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from .assets import encode_png, match_font


def render_playbook_card(map_name, side, strategy_name, background, markers):
    """Return a PNG buffer, or None if the map image is missing or unreadable.

    Marker coordinates are normalized to the displayed map image, not the card.
    `background` is a trusted local path or a Pillow image, never a remote URL.
    """
    try:
        if isinstance(background, Image.Image):
            base = background.convert("RGBA")
        elif background and Path(background).is_file():
            with Image.open(background) as source:
                base = source.convert("RGBA")
        else:
            return None
        base = ImageOps.contain(base, (928, 928), method=Image.Resampling.LANCZOS)
    except (OSError, ValueError, UnidentifiedImageError):
        return None
    if not base.width or not base.height:
        return None
    image = Image.new("RGB", (1024, 1080), (17, 17, 22))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 1024, 7), fill=(240, 99, 106))
    title = f"{map_name} · {side.title()}"
    draw.text((48, 35), title[:70], font=match_font(29, True), fill=(247, 246, 248))
    draw.text((48, 76), strategy_name[:90], font=match_font(19), fill=(190, 187, 195))
    left, top = (1024 - base.width) // 2, 125 + (928 - base.height) // 2
    draw.rounded_rectangle((47, 124, 977, 1054), radius=8, fill=(28, 27, 32), outline=(51, 49, 56))
    image.paste(base, (left, top), base)
    draw = ImageDraw.Draw(image)
    accent = (240, 99, 106)

    def point(x, y):
        return (left + round(x * max(base.width - 1, 0)), top + round(y * max(base.height - 1, 0)))

    for marker in markers:
        try:
            kind = marker["kind"]
            x, y = point(marker["x"], marker["y"])
            label = marker.get("label", "")[:40]
            if kind == "path":
                points = [point(px, py) for px, py in marker.get("points", [])]
                if len(points) < 2:
                    continue
                draw.line(points, fill=accent, width=5, joint="curve")
                end, before = points[-1], points[-2]
                angle = math.atan2(end[1] - before[1], end[0] - before[0])
                for direction in (-1, 1):
                    wing = (end[0] - 18 * math.cos(angle + direction * 0.55),
                            end[1] - 18 * math.sin(angle + direction * 0.55))
                    draw.line((end, wing), fill=accent, width=5)
            elif kind == "smoke":
                draw.ellipse((x - 22, y - 22, x + 22, y + 22), fill=(92, 89, 104), outline=(220, 216, 227), width=3)
            elif kind == "utility":
                draw.polygon(((x, y - 16), (x + 16, y), (x, y + 16), (x - 16, y)), fill=(234, 186, 87), outline=(255, 240, 194))
            elif kind in ("player", "step"):
                draw.ellipse((x - 17, y - 17, x + 17, y + 17), fill=accent, outline=(255, 241, 241), width=2)
            elif kind == "site":
                draw.rounded_rectangle((x - 18, y - 18, x + 18, y + 18), radius=4, fill=(31, 31, 38), outline=(247, 246, 248), width=2)
            else:
                continue
            if label:
                if kind in ("player", "step", "site") and len(label) <= 2:
                    draw.text((x, y), label, font=match_font(15, True), fill=(255, 255, 255), anchor="mm")
                else:
                    draw.text((x + 20, y - 12), label, font=match_font(16, True), fill=(255, 255, 255),
                              stroke_width=2, stroke_fill=(17, 17, 22))
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
    return encode_png(image)
