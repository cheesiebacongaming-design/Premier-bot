from io import BytesIO

from PIL import Image

from graphics.lineup import render_lineup_card
from graphics.help_card import render_help_card
from graphics.match_card import _assign_unique_tags, draw_play_result
from graphics.player_history import render_player_history
from graphics.playbook_card import render_playbook_card, render_strategy_card
from graphics.rank_change import draw_rank_change_card
from graphics.schedule import render_schedule_card


def _png_size(buffer):
    with Image.open(BytesIO(buffer.getvalue())) as image:
        assert image.format == "PNG"
        return image.size


def test_tied_tags_go_to_eligible_player_with_fewer_awards():
    players = [
        {"tags": ["MVP", "ASSIST KING", "CLUTCH x1"]},
        {"tags": ["ASSIST KING", "CLUTCH x1"]},
        {"tags": ["LVP"]},
    ]

    _assign_unique_tags(players)

    assert players[0]["tags"] == ["MVP", "CLUTCH x1"]
    assert players[1]["tags"] == ["ASSIST KING"]
    assert players[2]["tags"] == ["LVP"]


def test_schedule_renderer_returns_png_without_external_art():
    buffer = render_schedule_card(
        {
            "stage": "Stage One",
            "note": "Queue Tuesday",
            "weeks": [{"label": "Week 1", "map": "Ascent", "start": "2026-11-01", "end": "2026-11-01"}],
        },
        {},
        "2026-11-01",
    )

    width, height = _png_size(buffer)
    assert width == 1200
    assert height > 128


def test_lineup_renderer_returns_a_five_slot_card():
    buffer = render_lineup_card("Ascent", "Week 1", [], {})

    width, height = _png_size(buffer)
    assert width == 900
    assert height == 156 + 5 * 112 + 24


def test_player_history_renders_grouped_rows_with_missing_icons():
    rows = [
        {"started": "2026-11-03T12:00:00Z", "map": "Haven", "agent": "Sova", "won": True,
         "score": "13-9", "k": 14, "d": 10, "a": 5, "acs": 200, "adr": 145, "hs": 25},
        {"started": "2026-11-02T12:00:00Z", "map": "Abyss", "agent": "Clove", "won": None,
         "score": "12-12", "k": 10, "d": 10, "a": 8, "acs": 170, "adr": 120, "hs": 20},
    ]

    width, height = _png_size(render_player_history("Player", "EU", "competitive", rows, {}))

    assert width == 1200
    assert height == 248 + 2 * (46 + 106) + 20


def test_help_card_grows_to_fit_long_commands_and_descriptions():
    short = [("Players", [("/rank [member]", "Show rank")])]
    long = [("Players", [("/rank " + "[option] " * 20, "Long description " * 60)])]

    short_width, short_height = _png_size(render_help_card(short))
    long_width, long_height = _png_size(render_help_card(long))

    assert short_width == long_width == 1200
    assert long_height > short_height


def test_tactical_map_renders_markers_and_missing_map_is_optional(tmp_path):
    assert render_playbook_card("Ascent", "attack", "Execute", tmp_path / "missing.png", []) is None
    background = Image.new("RGBA", (600, 400), (70, 70, 75, 255))
    markers = [{"kind": "player", "x": 0.2, "y": 0.5, "label": "1"},
               {"kind": "smoke", "x": 0.7, "y": 0.5, "label": ""},
               {"kind": "path", "x": 0.2, "y": 0.5, "points": [[0.2, 0.5], [0.7, 0.5]]}]
    assert _png_size(render_playbook_card("Ascent", "attack", "Execute", background, markers)) == (1024, 1080)


def test_strategy_card_grows_for_description_and_keeps_uploaded_image():
    short = {"name": "A Execute", "map": "Haven", "side": "attack"}
    data = BytesIO()
    Image.new("RGB", (400, 200), (120, 80, 40)).save(data, "PNG")
    _, short_height = _png_size(render_strategy_card(short, "Short description"))
    image = render_strategy_card(short, "Long description " * 80, data.getvalue())
    width, height = _png_size(image)
    assert width == 1200
    assert height > short_height + 200


def test_rank_change_renderer_returns_fixed_size_png_without_icons():
    buffer = draw_rank_change_card("Player", "Gold 3", "Platinum 1", "up", None, None)

    assert _png_size(buffer) == (1200, 500)


def test_match_card_renderer_accepts_missing_icons_and_map_art():
    players = [
        {
            "puuid": f"player-{index}", "name": f"Player {index}", "tag": "EU", "team_id": "Red",
            "stats": {"kills": 10, "deaths": 8, "assists": 4, "score": 2100,
                      "headshots": 10, "bodyshots": 20, "legshots": 10,
                      "damage": {"dealt": 1800}},
        }
        for index in range(5)
    ]
    representative = players[0]
    detail = {
        "metadata": {"map": {"name": "Ascent"}, "game_length_in_ms": 1_200_000,
                     "started_at": "2026-11-01T12:00:00Z"},
        "teams": [{"team_id": "Red", "won": True, "rounds": {"won": 13, "lost": 8}}],
        "players": players,
        "kills": [],
        "rounds": [],
    }

    buffer = draw_play_result(detail, players, players, representative, None)

    width, height = _png_size(buffer)
    assert width == 1200
    assert height > 650  # five teammate cards render across three rows
