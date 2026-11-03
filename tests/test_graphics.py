from io import BytesIO

from PIL import Image

from graphics.lineup import render_lineup_card
from graphics.match_card import _assign_unique_tags, draw_play_result
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
