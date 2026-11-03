import io
from datetime import timezone
from pathlib import Path

from PIL import Image, ImageDraw, ImageOps

from .assets import PLAY_TAG_STYLES, draw_soft_box, match_font, wrap_image_text
from utils.formatting import parse_time
from utils.match_summary import _clutch_wins, _deaths_by_round, _play_headline, _play_recap


def draw_play_result(detail, queued_players, all_teammates, representative, map_path, ai_recap=None):
    meta = detail.get("metadata", {})
    teams = {t.get("team_id"): t for t in detail.get("teams", [])}
    team = teams.get(representative.get("team_id"), {})
    won = bool(team.get("won"))
    our_score = (team.get("rounds") or {}).get("won", 0)
    their_score = (team.get("rounds") or {}).get("lost", 0)
    map_data = meta.get("map") or {}
    map_name = map_data.get("name", "Unknown")
    started = parse_time(meta.get("started_at"))
    duration_ms = meta.get("game_length_in_ms") or 0
    duration = f"{duration_ms // 60000} min" if duration_ms else "Competitive"
    stats_by_id, kills_per_round, first_bloods, first_deaths = {}, {}, {}, {}
    for kill in detail.get("kills") or []:
        killer = (kill.get("killer") or {}).get("puuid")
        round_number = kill.get("round", 0) or 0
        if killer:
            kills_per_round.setdefault(round_number, {}).setdefault(killer, 0)
            kills_per_round[round_number][killer] += 1
    kill_events_by_round = {}
    for event in detail.get("kills") or []:
        kill_events_by_round.setdefault(event.get("round", 0) or 0, []).append(event)
    for events in kill_events_by_round.values():
        if not events:
            continue
        first_event = min(events, key=lambda event: event.get("time_in_round_in_ms", 0))
        first_killer = (first_event.get("killer") or {}).get("puuid")
        first_victim = (first_event.get("victim") or {}).get("puuid")
        if first_killer:
            first_bloods[first_killer] = first_bloods.get(first_killer, 0) + 1
        if first_victim:
            first_deaths[first_victim] = first_deaths.get(first_victim, 0) + 1
    for p in all_teammates:
        st = p.get("stats", {})
        stats_by_id[p.get("puuid")] = st
    team_mvp_id = max(stats_by_id, key=lambda puuid: stats_by_id[puuid].get("score", 0)) if stats_by_id else None
    team_lvp_id = min(stats_by_id, key=lambda puuid: stats_by_id[puuid].get("score", 0)) if stats_by_id else None
    round_count = max(our_score + their_score, 1)
    deaths_by_round = _deaths_by_round(detail)
    clutch_wins = _clutch_wins(detail, all_teammates)
    assists_by_id = {p.get("puuid"): (p.get("stats") or {}).get("assists", 0) for p in all_teammates}
    damage_by_id = {
        p.get("puuid"): round(((p.get("stats") or {}).get("damage") or {}).get("dealt", 0) / round_count)
        for p in all_teammates
    }
    most_assists = max(assists_by_id.values(), default=0)
    highest_adr = max(damage_by_id.values(), default=0)
    lowest_deaths = min(((p.get("stats") or {}).get("deaths", 0) for p in all_teammates), default=0)
    tracked = []
    for p in queued_players:
        st = p.get("stats", {})
        shots = st.get("headshots", 0) + st.get("bodyshots", 0) + st.get("legshots", 0)
        hs = round(st.get("headshots", 0) * 100 / shots) if shots else 0
        tags = []
        puuid = p.get("puuid")
        best_round = max((kills_per_round.get(r, {}).get(puuid, 0) for r in kills_per_round), default=0)
        if best_round >= 5: tags.append("ACE")
        elif best_round == 4: tags.append("QUAD")
        if puuid == team_mvp_id: tags.append("MVP")
        if first_bloods.get(puuid, 0) >= 3: tags.append("ENTRY")
        if st.get("kills", 0) >= 15 and hs >= 30: tags.append("DINK MACHINE")
        if most_assists >= 8 and assists_by_id.get(puuid, 0) == most_assists: tags.append("ASSIST KING")
        if highest_adr >= 160 and damage_by_id.get(puuid, 0) == highest_adr: tags.append("DAMAGE DEALER")
        if round_count >= 10 and ((st.get("deaths", 0) / round_count) <= 0.45) and st.get("deaths", 0) == lowest_deaths:
            tags.append("SURVIVOR")
        clutch_count = clutch_wins.get(puuid, 0)
        max_clutches = max(clutch_wins.values(), default=0)
        if max_clutches > 0 and clutch_count == max_clutches:
            tags.append(f"CLUTCH x{clutch_count}")
        death_rounds = sum(1 for events in deaths_by_round.values() if events.get(puuid, 0) > 0)
        if round_count > 0 and death_rounds >= round_count: tags.append("VICTIM")
        if puuid == team_lvp_id: tags.append("LVP")
        tracked.append({"p": p, "st": st, "hs": hs, "tags": tags,
                        "fb": first_bloods.get(puuid, 0), "fd": first_deaths.get(puuid, 0),
                        "adr": damage_by_id.get(puuid, 0)})

    width, hero_h, gap = 1200, 270, 18
    recap = (ai_recap or {}).get("recap") or _play_recap(map_name, won, our_score, their_score, tracked, all_teammates, duration, detail)
    headline = ((ai_recap or {}).get("headline") or _play_headline(all_teammates, our_score, their_score)).upper()
    recap_font = match_font(23)
    measure_draw = ImageDraw.Draw(Image.new("RGB", (width, 1)))
    columns, side_pad, column_gap = 2, 24, 18
    recap_lines = wrap_image_text(measure_draw, recap, recap_font, width - 2 * side_pad, 20)
    summary_h = 114 + max(0, len(recap_lines) - 1) * 42
    card_width = (width - 2 * side_pad - column_gap) // columns
    tag_font = match_font(14, True)
    tag_width_limit = card_width - 132
    max_tag_rows = 1
    for item in tracked:
        used, tag_rows = 0, 1
        for tag in item["tags"]:
            tag_width = int(measure_draw.textlength(tag, font=tag_font)) + 16
            if used and used + tag_width > tag_width_limit:
                tag_rows += 1
                used = 0
            used += tag_width + 6
        max_tag_rows = max(max_tag_rows, tag_rows)
    card_h = 208 + (max_tag_rows - 1) * 28
    rows = max(1, (len(tracked) + columns - 1) // columns)
    cards_top = hero_h + summary_h + 22
    height = cards_top + rows * (card_h + gap) - gap + 26
    image = Image.new("RGB", (width, height), (17, 17, 22))
    draw = ImageDraw.Draw(image)
    if map_path and Path(map_path).exists():
        try:
            bg = Image.open(map_path).convert("RGB")
            ratio = max(width / bg.width, hero_h / bg.height)
            bg = bg.resize((int(bg.width * ratio), int(bg.height * ratio)))
            left, top = (bg.width - width) // 2, (bg.height - hero_h) // 2
            bg = bg.crop((left, top, left + width, top + hero_h))
            bg = Image.blend(bg, Image.new("RGB", bg.size, (8, 9, 12)), 0.60)
            image.paste(bg, (0, 0))
        except Exception:
            pass
    draw.rectangle((0, hero_h, width, height), fill=(17, 17, 22))
    accent = (72, 181, 105) if won else (220, 86, 94)
    f_sub = match_font(19)
    draw.text((side_pad, 169), map_name.upper(), font=match_font(56, True), fill=(250, 250, 252), anchor="lm")
    if started:
        match_time = started.astimezone(timezone.utc)
        date_label = f"{match_time.strftime('%b %d, %Y, ')}{match_time.strftime('%I').lstrip('0')}:{match_time.strftime('%M:%S %p')}"
    else:
        date_label = "Time unavailable"
    draw.text((side_pad, 224), f"Competitive · {duration} · {date_label}", font=f_sub, fill=(207, 205, 210), anchor="lm")
    badge = f"{our_score}-{their_score}"
    badge_box = (width - side_pad - 208, 186, width - side_pad, 238)
    draw_soft_box(image, badge_box, accent, opacity=238, radius=7)
    draw.text(((badge_box[0] + badge_box[2]) // 2, (badge_box[1] + badge_box[3]) // 2), badge,
              font=match_font(21, True), fill=(255, 250, 250), anchor="mm")
    coral = (240, 99, 106)
    headline_size = 32
    headline_font = match_font(headline_size, True)
    while headline_size > 22 and draw.textlength(headline, font=headline_font) > width - 64:
        headline_size -= 1
        headline_font = match_font(headline_size, True)
    if draw.textlength(headline, font=headline_font) > width - 64:
        while headline and draw.textlength(headline + "...", font=headline_font) > width - 64:
            headline = headline[:-1]
        headline = headline.rstrip() + "..."
    draw.text((side_pad, hero_h + 44), headline, font=headline_font, fill=coral, anchor="lm")
    for line_index, line in enumerate(recap_lines):
        draw.text((side_pad, hero_h + 100 + line_index * 42), line, font=recap_font, fill=(226, 226, 232), anchor="lm")
    for index, item in enumerate(tracked):
        col, row = index % columns, index // columns
        if len(tracked) % columns and row == rows - 1:
            x = (width - card_width) // 2
        else:
            x = side_pad + col * (card_width + column_gap)
        y = cards_top + row * (card_h + gap)
        right = x + card_width
        draw.rounded_rectangle((x, y, right, y + card_h), radius=14, fill=(28, 27, 31), outline=(42, 40, 45), width=1)
        p, st = item["p"], item["st"]
        icon = p.get("_play_icon")
        icon_size = 76
        icon_x, icon_y = x + 18, y + 18
        draw.rounded_rectangle((icon_x, icon_y, icon_x + icon_size, icon_y + icon_size), radius=8, fill=(28, 27, 31))
        if icon:
            icon = ImageOps.contain(icon.convert("RGBA"), (icon_size, icon_size), method=Image.Resampling.LANCZOS)
            fitted_x = icon_x + (icon_size - icon.width) // 2
            fitted_y = icon_y + (icon_size - icon.height) // 2
            image.paste(icon, (fitted_x, fitted_y), icon)
        player_name = str(p.get("name", "?"))
        player_name_size = 25
        player_name_font = match_font(player_name_size, True)
        player_name_x = x + 112
        while player_name_size > 18 and draw.textlength(player_name, font=player_name_font) > right - player_name_x - 20:
            player_name_size -= 1
            player_name_font = match_font(player_name_size, True)
        player_blue = (164, 194, 245)
        draw.text((player_name_x, y + 18), player_name, font=player_name_font, fill=player_blue, anchor="lt")
        tx, tag_row = player_name_x, 0
        for tag in item["tags"]:
            tw = int(draw.textlength(tag, font=tag_font)) + 16
            max_right = right - 20
            if tx + tw > max_right:
                tag_row += 1
                tx = player_name_x
            tag_key = tag.split(" x", 1)[0]
            tag_colour, tag_text = PLAY_TAG_STYLES.get(tag_key, ((68, 68, 78), (235, 235, 240)))
            tag_y = y + 54 + tag_row * 32
            draw_soft_box(image, (tx, tag_y, tx + tw, tag_y + 30), tag_colour, opacity=222, radius=3)
            text_bbox = tag_font.getbbox(tag, anchor="mm")
            text_center_offset = (text_bbox[1] + text_bbox[3]) / 2
            draw.text((tx + tw / 2, tag_y + 15 - text_center_offset),
                      tag, font=tag_font, fill=tag_text, anchor="mm")
            tx += tw + 6
        stat_label_y = y + 118 + tag_row * 32
        stat_value_y = stat_label_y + 31
        labels = [("KDA", f"{st.get('kills',0)}/{st.get('deaths',0)}/{st.get('assists',0)}"),
                  ("ACS", str(round(st.get("score", 0) / round_count))),
                  ("KD", f"{st.get('kills',0) / max(st.get('deaths',0),1):.2f}")]
        cell_w = (right - x - 44) // 3
        for cell, (label, value) in enumerate(labels):
            cx = x + 22 + cell * cell_w + cell_w // 2
            draw.text((cx, stat_label_y), label, font=match_font(16), fill=(154, 151, 160), anchor="mm")
            draw.text((cx, stat_value_y), value, font=match_font(24, True), fill=(247, 246, 248), anchor="mm")
        subtext_y = stat_value_y + 33
        draw.text(((x + right) // 2, subtext_y),
                  f"HS {item['hs']}% | FB {item['fb']} | FD {item['fd']}",
                  font=match_font(15), fill=(155, 152, 161), anchor="mm")
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    buffer.seek(0)
    return buffer
