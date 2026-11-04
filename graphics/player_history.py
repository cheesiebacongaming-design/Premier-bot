"""Render recent player matches in the matchfeed visual style."""

from datetime import timezone
import re

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


def rank_summary(mmr):
    """Extract the latest identifiable competitive act without assuming API order."""
    mmr = mmr if isinstance(mmr, dict) else {}
    current = mmr.get("current") or {}
    seasonal = mmr.get("seasonal") or []
    def season_key(item):
        short = str((item.get("season") or {}).get("short") or "")
        numbers = re.findall(r"\d+", short)
        return tuple(int(number) for number in numbers) if numbers else ()
    acts = [item for item in seasonal if isinstance(item, dict) and season_key(item)]
    act = max(acts, key=season_key) if acts else {}
    wins, games = act.get("wins"), act.get("games")
    return {"rank": (current.get("tier") or {}).get("name") or ("Unrated" if mmr else "Unavailable"),
            "tier_id": (current.get("tier") or {}).get("id"), "rr": current.get("rr"),
            "act": str((act.get("season") or {}).get("short") or "").upper(),
            "wins": wins, "games": games}


def render_player_history(name, tag, mode, rows, icons, mmr=None, rank_icon=None):
    """Draw one dated row per match; icons are preloaded Pillow images."""
    width, pad, row_h = 1200, 24, 96
    groups = []
    for row in rows:
        date, clock = _date_and_time(row.get("started"))
        if not groups or groups[-1][0] != date:
            groups.append((date, []))
        groups[-1][1].append((row, clock))
    height = 410 + sum(46 + len(items) * (row_h + 10) for _, items in groups) + 20
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
    rank = rank_summary(mmr)
    draw.rounded_rectangle((pad, 138, width - pad, 255), radius=9, fill=(28, 27, 32), outline=(51, 49, 56))
    if rank_icon:
        badge = ImageOps.contain(rank_icon.convert("RGBA"), (84, 84), method=Image.Resampling.LANCZOS)
        image.paste(badge, (pad + 16 + (84 - badge.width) // 2, 154 + (84 - badge.height) // 2), badge)
    draw.text((pad + 112, 163), "CURRENT RANK", font=label, fill=muted, anchor="lt")
    draw.text((pad + 112, 191), _fit(draw, rank["rank"], value, 260), font=value, fill=white, anchor="lt")
    rr = f"{rank['rr']} RR" if rank["rr"] is not None else "—"
    draw.text((470, 163), "RANK RATING", font=label, fill=muted, anchor="lt")
    draw.text((470, 191), rr, font=value, fill=white, anchor="lt")
    act_label = f"{rank['act']} · LATEST ACT" if rank["act"] else "ACT STATS UNAVAILABLE"
    draw.text((700, 163), act_label, font=label, fill=muted, anchor="lt")
    wins_act, games_act = rank["wins"], rank["games"]
    if isinstance(wins_act, int) and isinstance(games_act, int) and 0 <= wins_act <= games_act:
        act_record = f"{wins_act} W / {games_act} GAMES  ·  {round(100 * wins_act / games_act) if games_act else 0}% WIN"
    else:
        act_record = "—"
    draw.text((700, 191), act_record, font=value, fill=white, anchor="lt")

    draw.text((pad, 271), f"LAST {len(rows)} {mode.upper()} MATCHES", font=label, fill=muted, anchor="lt")
    draw.rounded_rectangle((pad, 302, width - pad, 377), radius=9, fill=(28, 27, 32), outline=(51, 49, 56))
    cell = (width - 2 * pad) / len(summaries)
    for index, (heading, text) in enumerate(summaries):
        x = pad + cell * (index + 0.5)
        draw.text((x, 321), heading, font=label, fill=muted, anchor="mm")
        draw.text((x, 351), text, font=value, fill=white, anchor="mm")

    y = 400
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
