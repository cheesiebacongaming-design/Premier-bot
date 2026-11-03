import io
from datetime import timezone

from PIL import Image, ImageDraw

from .assets import COLS, GREY, WHITE, font
from utils.formatting import parse_time


def draw_scoreboard(s, icons):
    W, ROW, HEAD, TEAMHEAD, GAP = 1000, 44, 96, 34, 12
    n = len(s["rows"]) + len(s["enemy_rows"])
    H = HEAD + 2 * TEAMHEAD + n * ROW + 2 * GAP + 8
    img = Image.new("RGB", (W, H), (15, 25, 35))
    d = ImageDraw.Draw(img)
    accent = (46, 204, 113) if s["won"] else (231, 76, 60)

    d.rectangle([0, 0, W, 6], fill=accent)
    d.text((24, 48), "VICTORY" if s["won"] else "DEFEAT", font=font(30, True), fill=accent, anchor="lm")
    d.text((W // 2, 46), f"{s['rw']} : {s['rl']}", font=font(42, True), fill=WHITE, anchor="mm")
    started = parse_time(s.get("started"))
    sub = s["map"] + (started.astimezone(timezone.utc).strftime("  \u2022  %d %b %Y %H:%M UTC") if started else "")
    d.text((W - 24, 48), sub, font=font(18), fill=(190, 200, 210), anchor="rm")

    f_name, f_tag, f_val, f_lab = font(17, True), font(13), font(16), font(13, True)
    y = HEAD
    for label, rows, tint in ((s["us"], s["rows"], (52, 152, 219)), (s["them"], s["enemy_rows"], (231, 76, 60))):
        d.rectangle([0, y, W, y + TEAMHEAD], fill=(30, 42, 56))
        d.rectangle([0, y, 5, y + TEAMHEAD], fill=tint)
        d.text((24, y + TEAMHEAD // 2), label, font=font(17, True), fill=WHITE, anchor="lm")
        d.text((425, y + TEAMHEAD // 2), "RANK", font=f_lab, fill=GREY, anchor="mm")
        for name, x in COLS:
            d.text((x, y + TEAMHEAD // 2), name, font=f_lab, fill=GREY, anchor="mm")
        y += TEAMHEAD
        for i, r in enumerate(rows):
            if i % 2 == 0:
                d.rectangle([0, y, W, y + ROW], fill=(20, 32, 45))
            cy = y + ROW // 2
            ic = icons.get(("a", r["agent_id"]))
            if ic:
                img.paste(ic, (24, cy - 18), ic)
            else:
                d.text((42, cy), r["agent"][:3], font=f_lab, fill=GREY, anchor="mm")
            d.text((70, cy), r["name"], font=f_name, fill=WHITE, anchor="lm")
            d.text((70 + d.textlength(r["name"], font=f_name) + 6, cy), f"#{r['tag']}", font=f_tag, fill=GREY, anchor="lm")
            tic = icons.get(("t", r["tier"]))
            if tic:
                img.paste(tic, (425 - 15, cy - 15), tic)
            else:
                d.text((425, cy), "-", font=f_val, fill=GREY, anchor="mm")
            diff = r["k"] - r["d"]
            vals = [r["acs"], r["k"], r["d"], r["a"], f"{diff:+d}", f"{r['k'] / max(r['d'], 1):.2f}", r["adr"], f"{r['hs']}%", r.get("fb", 0)]
            for (name, x), v in zip(COLS, vals):
                colour = (46, 204, 113) if name == "+/-" and diff > 0 else (231, 76, 60) if name == "+/-" and diff < 0 else WHITE
                d.text((x, cy), str(v), font=f_val, fill=colour, anchor="mm")
            y += ROW
        y += GAP

    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf
