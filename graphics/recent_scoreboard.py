"""Render the /recent match scoreboard in the matchfeed style."""

from datetime import timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from .assets import draw_soft_box, encode_png, match_font
from utils.formatting import parse_time
from utils.recent_scoreboard import build_scoreboard


def render_recent_scoreboard(detail, target, period, map_path, agent_icons, rank_icons):
    width, pad, hero, row_h = 1400, 24, 260, 70
    rows = build_scoreboard(detail, period)
    side = target.get("team_id")
    teams = detail.get("teams") or []
    team = next((t for t in teams if t.get("team_id") == side), {})
    ordered = [side] + [t.get("team_id") for t in teams if t.get("team_id") != side]
    ordered += [r["player"].get("team_id") for r in rows if r["player"].get("team_id") not in ordered]
    ordered = list(dict.fromkeys(ordered))
    groups = [(team_id, sorted((r for r in rows if r["player"].get("team_id") == team_id),
                               key=lambda r: r["score"], reverse=True)) for team_id in ordered]
    groups = [(team_id, group) for team_id, group in groups if group]
    height = hero + 76 + sum(48 + len(group) * row_h + 14 for _, group in groups) + 18
    image = Image.new("RGB", (width, height), (17, 17, 22))
    draw = ImageDraw.Draw(image)
    if map_path and Path(map_path).exists():
        try:
            with Image.open(map_path) as source:
                background = ImageOps.fit(source.convert("RGB"), (width, hero), method=Image.Resampling.LANCZOS)
            image.paste(Image.blend(background, Image.new("RGB", background.size, (8, 9, 12)), .6), (0, 0))
        except OSError:
            pass
    meta = detail.get("metadata") or {}
    map_name = (meta.get("map") or {}).get("name", "Unknown")
    started = parse_time(meta.get("started_at"))
    date = started.astimezone(timezone.utc).strftime("%b %d, %Y, %H:%M UTC") if started else "Time unavailable"
    duration = meta.get("game_length_in_ms") or 0
    draw.text((pad, 142), map_name.upper(), font=match_font(56, True), fill=(250, 250, 252), anchor="lm")
    draw.text((pad, 205), f"Competitive · {duration // 60000} min · {date}" if duration else f"Competitive · {date}",
              font=match_font(19), fill=(207, 205, 210), anchor="lm")
    score = team.get("rounds") or {}
    colour = (72, 181, 105) if team.get("won") else (220, 86, 94)
    box = (width - pad - 190, 177, width - pad, 230)
    draw_soft_box(image, box, colour, opacity=238, radius=7)
    draw.text(((box[0] + box[2]) // 2, 203), f"{score.get('won', '?')}-{score.get('lost', '?')}",
              font=match_font(23, True), fill="white", anchor="mm")
    period_title = {"full": "FULL MATCH", "first": "FIRST HALF", "second": "SECOND HALF + OVERTIME"}[period]
    draw.text((pad, hero + 36), f"SCOREBOARD  /  {period_title}", font=match_font(22, True),
              fill=(240, 99, 106), anchor="lm")
    columns = [("ACS", 535), ("K", 620), ("D", 690), ("A", 760), ("+/−", 845),
               ("K/D", 940), ("ADR", 1040), ("HS%", 1135), ("FB", 1230), ("FD", 1320)]
    y = hero + 76
    for team_id, group in groups:
        ours = team_id == side
        draw.rectangle((pad, y, width - pad, y + 38), fill=(33, 72, 59) if ours else (75, 38, 46))
        draw.text((pad + 15, y + 19), "YOUR TEAM" if ours else "OPPONENTS",
                  font=match_font(15, True), fill=(153, 224, 185) if ours else (244, 151, 158), anchor="lm")
        draw.text((410, y + 19), "RANK", font=match_font(14, True), fill=(192, 189, 198), anchor="mm")
        for label, x in columns:
            draw.text((x, y + 19), label, font=match_font(14, True), fill=(192, 189, 198), anchor="mm")
        y += 48
        for index, row in enumerate(group):
            player = row["player"]
            draw.rectangle((pad, y, width - pad, y + row_h - 3), fill=(28, 27, 32) if index % 2 == 0 else (34, 33, 39))
            if player.get("puuid") == target.get("puuid"):
                draw.rectangle((pad, y, pad + 4, y + row_h - 3), fill=(164, 194, 245))
            icon = agent_icons.get(player.get("puuid"))
            if icon:
                icon = ImageOps.contain(icon.convert("RGBA"), (55, 55), method=Image.Resampling.LANCZOS)
                image.paste(icon, (pad + 10 + (55 - icon.width) // 2, y + 6 + (55 - icon.height) // 2), icon)
            name = str(player.get("name") or "Unknown")
            name_font = match_font(19, True)
            while name and draw.textlength(name, font=name_font) > 225:
                name = name[:-1]
            if name != str(player.get("name") or "Unknown"):
                name = name[:-1] + "…"
            draw.text((pad + 78, y + 24), name, font=name_font, fill=(247, 246, 248), anchor="lm")
            draw.text((pad + 78, y + 49), "#" + str(player.get("tag") or ""), font=match_font(13), fill=(160, 157, 168), anchor="lm")
            tier = player.get("tier") or {}
            icon = rank_icons.get(str(tier.get("id")))
            if icon:
                icon = ImageOps.contain(icon.convert("RGBA"), (40, 40), method=Image.Resampling.LANCZOS)
                image.paste(icon, (390 + (40 - icon.width) // 2, y + 14 + (40 - icon.height) // 2), icon)
            values = [str(row["acs"]), str(row["k"]), str(row["d"]), str(row["a"]),
                      f"{row['k'] - row['d']:+d}", f"{row['k'] / max(row['d'], 1):.2f}",
                      str(row["adr"]), f"{row['hs']}%", str(row["fb"]), str(row["fd"])]
            for value, (_, x) in zip(values, columns):
                draw.text((x, y + 34), value, font=match_font(19, True), fill=(240, 239, 244), anchor="mm")
            y += row_h
        y += 14
    return encode_png(image)
