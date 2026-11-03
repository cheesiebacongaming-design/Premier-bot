"""Pure rank promotion and demotion card renderer."""

import io

from PIL import Image, ImageDraw, ImageOps

from .assets import match_font


def draw_rank_change_card(player_name, from_name, to_name, direction, old_icon, new_icon):
    image = Image.new("RGB", (1200, 500), (17, 16, 20))
    draw = ImageDraw.Draw(image)
    accent = (77, 190, 116) if direction == "up" else (224, 86, 99)
    draw.rounded_rectangle((22, 22, 1178, 478), radius=18, fill=(28, 27, 32), outline=(57, 55, 63), width=2)
    draw.text((58, 70), "RANK UP" if direction == "up" else "DERANK", font=match_font(38, True), fill=accent, anchor="lm")
    draw.text((1140, 70), player_name, font=match_font(27, True), fill=(245, 244, 247), anchor="rm")
    draw.line((58, 108, 1142, 108), fill=(58, 56, 64), width=2)

    panels = ((60, 148, 530, 425), (670, 148, 1140, 425))
    for panel in panels:
        draw.rounded_rectangle(panel, radius=12, fill=(34, 33, 39), outline=(61, 59, 67), width=1)
    draw.text((295, 178), "FROM", font=match_font(16, True), fill=(154, 151, 161), anchor="mm")
    draw.text((905, 178), "TO", font=match_font(16, True), fill=(154, 151, 161), anchor="mm")

    def paste_tier_icon(icon, center_x):
        if icon is None:
            return
        fitted = ImageOps.contain(icon.convert("RGBA"), (132, 132), method=Image.Resampling.LANCZOS)
        image.paste(fitted, (center_x - fitted.width // 2, 202 + (132 - fitted.height) // 2), fitted)

    paste_tier_icon(old_icon, 295)
    paste_tier_icon(new_icon, 905)
    draw.text((295, 365), from_name.upper(), font=match_font(27, True), fill=(218, 216, 222), anchor="mm")
    draw.text((905, 365), to_name.upper(), font=match_font(27, True), fill=(250, 249, 251), anchor="mm")
    # Draw the arrow directly so it does not depend on font glyph coverage.
    draw.line((570, 282, 626, 282), fill=accent, width=6)
    draw.line((611, 266, 627, 282, 611, 298), fill=accent, width=6, joint="curve")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    return buffer
