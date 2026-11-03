from PIL import Image, ImageDraw

from .assets import GREY, WHITE, encode_png, font


def draw_lineup_card(map_name, week_label, slots, agent_icons):
    width, header, row_height = 900, 156, 112
    rows = max(5, len(slots))
    height = header + rows * row_height + 24
    image = Image.new("RGB", (width, height), (15, 25, 35))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, width, 7], fill=(255, 70, 85))
    draw.text((32, 48), map_name, font=font(34, True), fill=WHITE, anchor="lm")
    draw.text((34, 94), week_label or "Premier lineup", font=font(17), fill=GREY, anchor="lm")
    picked = sum(1 for slot in slots if slot.get("agent"))
    draw.text((width - 32, 70), f"{picked}/5 PICKS", font=font(20, True), fill=WHITE, anchor="rm")

    for index in range(rows):
        y = header + index * row_height
        slot = slots[index] if index < len(slots) else {}
        if index % 2 == 0:
            draw.rounded_rectangle([20, y + 5, width - 20, y + row_height - 5], radius=12, fill=(20, 32, 45))
        agent = slot.get("agent")
        icon = agent_icons.get(agent.casefold()) if agent else None
        if icon:
            image.paste(icon, (42, y + 20), icon)
        else:
            draw.rounded_rectangle([42, y + 20, 114, y + 92], radius=12, fill=(39, 53, 69))
            label = agent[:2].upper() if agent else "—"
            draw.text((78, y + 56), label, font=font(18, True), fill=WHITE, anchor="mm")
        player_name = slot.get("player") or "Open slot"
        draw.text((142, y + 43), player_name[:34], font=font(21, True), fill=WHITE, anchor="lm")
        subtext = slot.get("riot") or (agent if agent else "Waiting for an agent pick")
        draw.text((142, y + 77), subtext[:44], font=font(15), fill=GREY, anchor="lm")
    return image


def render_lineup_card(map_name, week_label, slots, agent_icons):
    return encode_png(draw_lineup_card(map_name, week_label, slots, agent_icons))
