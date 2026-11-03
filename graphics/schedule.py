from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from .assets import encode_png, match_font, wrap_image_text
from utils.formatting import fmt_range


def draw_schedule_card(season, map_art, today):
    weeks = season.get("weeks", [])
    width, margin, row_gap, row_height = 1200, 28, 8, 118
    header_height = 128
    note = (season.get("note") or "").strip()
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    note_font = match_font(23, True)
    note_lines = wrap_image_text(measure, note, note_font, width - margin * 2, 2) if note else []
    footer_height = 46 + len(note_lines) * 31 if note_lines else 20
    height = header_height + len(weeks) * (row_height + row_gap) + footer_height + 20
    image = Image.new("RGB", (width, height), (17, 16, 20))
    draw = ImageDraw.Draw(image)
    coral = (241, 94, 106)
    draw.rectangle((0, 0, width, 7), fill=coral)
    stage = season.get("stage", "Premier")
    draw.text((margin, 56), "PREMIER SCHEDULE", font=match_font(38, True), fill=(249, 248, 250), anchor="lm")
    draw.text((width - margin, 58), stage.upper(), font=match_font(21, True), fill=(168, 165, 174), anchor="rm")
    draw.line((margin, 104, width - margin, 104), fill=(58, 56, 64), width=2)

    marked = False
    for index, week in enumerate(weeks):
        y = header_height + index * (row_height + row_gap)
        start, end = week.get("start", ""), week.get("end", "")
        if end and end < today:
            status, accent = "COMPLETE", (139, 136, 145)
        elif not marked:
            status, accent = ("CURRENT" if start <= today else "UP NEXT"), (77, 190, 116)
            marked = True
        else:
            status, accent = "UPCOMING", coral
        box = (margin, y, width - margin, y + row_height)
        draw.rectangle(box, fill=(31, 30, 35), outline=(58, 56, 64), width=1)
        map_name = week.get("map", "Unknown")
        map_path = map_art.get(map_name.casefold())
        if map_path and Path(map_path).exists():
            try:
                with Image.open(map_path) as source:
                    background = ImageOps.fit(source.convert("RGB"), (width - margin * 2 - 2, row_height - 2), method=Image.Resampling.LANCZOS)
                overlay = Image.new("RGBA", background.size, (14, 13, 17, 142))
                background = Image.alpha_composite(background.convert("RGBA"), overlay).convert("RGB")
                image.paste(background, (margin + 1, y + 1))
            except OSError:
                pass
        draw = ImageDraw.Draw(image)
        draw.rectangle((margin, y, margin + 7, y + row_height), fill=accent)
        label = week.get("label", f"Week {index + 1}").upper()
        draw.text((margin + 28, y + 33), label, font=match_font(15, True), fill=(196, 193, 201), anchor="lm")
        name_size = 46 if len(map_name) <= 13 else 38
        draw.text((margin + 28, y + 82), map_name.upper(), font=match_font(name_size, True), fill=(250, 249, 251), anchor="lm")
        date_text = fmt_range(start, end) if start and end else "Dates to be announced"
        draw.text((width - margin - 24, y + 48), date_text.upper(), font=match_font(19, True), fill=(246, 244, 247), anchor="rm")
        draw.text((width - margin - 24, y + 83), status, font=match_font(15, True), fill=accent, anchor="rm")

    footer_y = header_height + len(weeks) * (row_height + row_gap) + 9
    draw = ImageDraw.Draw(image)
    if note_lines:
        draw.text((margin, footer_y + 13), "QUEUE WINDOWS", font=match_font(15, True), fill=coral, anchor="lm")
        for line in note_lines:
            footer_y += 31
            draw.text((margin, footer_y + 12), line, font=note_font, fill=(229, 227, 232), anchor="lm")
    return image


def render_schedule_card(season, map_art, today):
    return encode_png(draw_schedule_card(season, map_art, today))
