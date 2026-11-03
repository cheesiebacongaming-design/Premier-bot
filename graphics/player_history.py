"""Render recent player matches in the matchfeed visual style."""

from datetime import timezone

from PIL import Image, ImageDraw, ImageOps

from .assets import encode_png, match_font
from utils.formatting import parse_time


def _date_and_time(value):
    parsed = parse_time(value)
    if not parsed:
        return "DATE UNKNOWN", "Time unavailable"
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc)
    return parsed.strftime("%b %d, %Y").upper(), parsed.strftime("%H:%M UTC")


def _fit(draw, text, font, width):
    text = str(text)
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "...", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "..."


def render_player_history(name, tag, mode, rows, icons):
    """Draw one dated row per match; icons are preloaded Pillow images."""
    width, pad, row_h = 1200, 24, 96
    groups = []
    for row in rows:
        date, clock = _date_and_time(row.get("started"))
        if not groups or groups[-1][0] != date:
            groups.append((date, []))
        groups[-1][1].append((row, clock))
    height = 248 + sum(46 + len(items) * (row_h + 10) for _, items in groups) + 20
    image = Image.new("RGB", (width, height), (17, 17, 22))
    draw = ImageDraw.Draw(image)
    white, muted, blue, coral = (247, 246, 248), (160, 157, 168), (164, 194, 245), (240, 99, 106)
    title, body, label, value = match_font(38, True), match_font(19), match_font(14, True), match_font(22, True)
    draw.rectangle((0, 0, width, 7), fill=coral)
    draw.text((pad, 42), _fit(draw, f"{name}#{tag}", title, 1120), font=title, fill=white, anchor="lt")
    draw.text((pad, 100), f"{mode.upper()}  ·  LAST {len(rows)} MATCHES", font=body, fill=muted, anchor="lt")

    wins = sum(row.get("won") is True for row in rows)
    losses = sum(row.get("won") is False for row in rows)
    kills, deaths = sum(row["k"] for row in rows), sum(row["d"] for row in rows)
    average = lambda key: round(sum(row[key] for row in rows) / len(rows)) if rows else 0
    summaries = [("RECORD", f"{wins}W / {losses}L"), ("K/D", f"{kills / max(deaths, 1):.2f}"),
                 ("AVG ACS", str(average("acs"))), ("AVG ADR", str(average("adr"))),
                 ("AVG HS", f"{average('hs')}%")]
    draw.rounded_rectangle((pad, 140, width - pad, 215), radius=9, fill=(28, 27, 32), outline=(51, 49, 56))
    cell = (width - 2 * pad) / len(summaries)
    for index, (heading, text) in enumerate(summaries):
        x = pad + cell * (index + 0.5)
        draw.text((x, 159), heading, font=label, fill=muted, anchor="mm")
        draw.text((x, 189), text, font=value, fill=white, anchor="mm")

    y = 238
    for date, items in groups:
        draw.text((pad, y + 17), f"{date}  ·  {len(items)} MATCH{'ES' if len(items) != 1 else ''}",
                  font=match_font(18, True), fill=coral, anchor="lm")
        y += 46
        for row, clock in items:
            won = row.get("won")
            accent = (76, 190, 116) if won is True else (225, 90, 98) if won is False else (154, 151, 160)
            draw.rounded_rectangle((pad, y, width - pad, y + row_h), radius=9,
                                   fill=(28, 27, 32), outline=(51, 49, 56))
            draw.rounded_rectangle((pad, y + 14, pad + 5, y + row_h - 14), radius=2, fill=accent)
            icon = icons.get((row.get("agent_id") or row.get("agent") or "").casefold())
            if icon:
                icon = ImageOps.contain(icon.convert("RGBA"), (68, 68), method=Image.Resampling.LANCZOS)
                image.paste(icon, (pad + 16 + (68 - icon.width) // 2, y + 14 + (68 - icon.height) // 2), icon)
            else:
                draw.text((pad + 50, y + 48), "?", font=value, fill=muted, anchor="mm")
            draw.text((pad + 100, y + 23), f"{clock}  ·  {mode.title()}", font=match_font(14), fill=muted, anchor="lm")
            draw.text((pad + 100, y + 53), _fit(draw, row.get("map", "?"), value, 170), font=value, fill=white, anchor="lm")
            draw.text((pad + 100, y + 76), _fit(draw, row.get("agent", "?"), match_font(14), 170),
                      font=match_font(14), fill=muted, anchor="lm")
            kd = row["k"] / max(row["d"], 1)
            fields = [("SCORE", row.get("score") or "—", accent), ("K/D", f"{kd:.2f}", white),
                      ("K/D/A", f"{row['k']}/{row['d']}/{row['a']}", blue),
                      ("ADR", str(row["adr"]), white), ("HS%", f"{row['hs']}%", white),
                      ("ACS", str(row["acs"]), white)]
            for (heading, text, colour), x in zip(fields, (365, 500, 665, 835, 965, 1090)):
                draw.text((x, y + 30), heading, font=label, fill=muted, anchor="mm")
                draw.text((x, y + 62), text, font=value, fill=colour, anchor="mm")
            y += row_h + 10
    return encode_png(image)
