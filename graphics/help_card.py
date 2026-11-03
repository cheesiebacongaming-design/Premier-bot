"""Render the bot's grouped command reference as a compact image."""

from PIL import Image, ImageDraw

from .assets import encode_png, match_font


def _wrap(draw, text, font, width):
    lines, line = [], ""
    for word in str(text).split():
        while draw.textlength(word, font=font) > width and len(word) > 1:
            if line:
                lines.append(line)
                line = ""
            cut = len(word) - 1
            while cut > 1 and draw.textlength(word[:cut], font=font) > width:
                cut -= 1
            lines.append(word[:cut])
            word = word[cut:]
        candidate = f"{line} {word}".strip()
        if line and draw.textlength(candidate, font=font) > width:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    return lines


def render_help_card(sections):
    width, pad, gap = 1200, 24, 18
    column_width = (width - 2 * pad - gap) // 2
    name_font, desc_font = match_font(18, True), match_font(16)
    measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    prepared = []
    for title, commands in sections:
        if not commands:
            continue
        items = []
        for signature, description in commands:
            names = _wrap(measure, signature, name_font, column_width - 40)
            descriptions = _wrap(measure, description, desc_font, column_width - 40)
            items.append((names, descriptions))
        height = 53 + sum(len(names) * 23 + len(desc) * 21 + 14 for names, desc in items) + 12
        prepared.append((title, items, height))
    pairs = [prepared[i:i + 2] for i in range(0, len(prepared), 2)]
    height = 116 + sum(max(section[2] for section in pair) + gap for pair in pairs) + 32
    image = Image.new("RGB", (width, height), (17, 17, 22))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, width, 7), fill=(240, 99, 106))
    draw.text((pad, 30), "COMMANDS", font=match_font(36, True), fill=(247, 246, 248), anchor="lt")
    draw.text((pad, 81), "<required>   [optional]", font=desc_font, fill=(160, 157, 168), anchor="lt")
    y = 116
    for pair in pairs:
        for column, (title, items, section_height) in enumerate(pair):
            x = pad + column * (column_width + gap)
            draw.rounded_rectangle((x, y, x + column_width, y + section_height), radius=8,
                                   fill=(28, 27, 32), outline=(51, 49, 56))
            draw.text((x + 20, y + 18), title, font=match_font(22, True), fill=(240, 99, 106), anchor="lt")
            draw.line((x + 20, y + 46, x + column_width - 20, y + 46), fill=(55, 53, 60), width=1)
            item_y = y + 57
            for names, descriptions in items:
                for line in names:
                    draw.text((x + 20, item_y), line, font=name_font, fill=(164, 194, 245), anchor="lt")
                    item_y += 23
                for line in descriptions:
                    draw.text((x + 20, item_y), line, font=desc_font, fill=(220, 218, 225), anchor="lt")
                    item_y += 21
                item_y += 14
        y += max(section[2] for section in pair) + gap
    return encode_png(image)
