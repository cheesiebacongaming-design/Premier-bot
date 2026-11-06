from PIL import Image

from graphics.recent_scoreboard import render_recent_scoreboard
from utils.recent_scoreboard import build_scoreboard, has_half_data


def match():
    players = [{"puuid": "one", "name": "One", "tag": "T", "team_id": "Red",
                "stats": {"kills": 2, "deaths": 1, "assists": 1, "score": 300,
                          "headshots": 2, "bodyshots": 2, "legshots": 0, "damage": {"dealt": 200}}},
               {"puuid": "two", "name": "Two", "tag": "T", "team_id": "Blue", "stats": {}}]
    def round_entry(number):
        return {"id": number, "stats": [
            {"player": {"puuid": "one"}, "stats": {"score": 100, "kills": 1, "headshots": 1},
             "damage_events": [{"damage": 100}]},
            {"player": {"puuid": "two"}, "stats": {}, "damage_events": []}]}
    kills = [{"round": n, "time_in_round_in_ms": 1000, "killer": {"puuid": "one"},
              "victim": {"puuid": "two"}, "assistants": []} for n in (0, 12)]
    return {"metadata": {"map": {"name": "Abyss"}}, "players": players,
            "teams": [{"team_id": "Red", "won": True, "rounds": {"won": 13, "lost": 12}},
                      {"team_id": "Blue", "rounds": {"won": 12, "lost": 13}}],
            "rounds": [round_entry(n) for n in range(25)], "kills": kills}


def test_half_scores_and_overtime_are_separated():
    detail = match()
    assert has_half_data(detail)
    first = build_scoreboard(detail, "first")
    second = build_scoreboard(detail, "second")
    assert first[0]["k"] == 1 and second[0]["k"] == 1
    assert first[0]["acs"] == 100 and second[0]["adr"] == 100
    assert first[0]["fb"] == second[0]["fb"] == 1
    assert first[1]["d"] == second[1]["d"] == 1
    assert build_scoreboard(detail, "full")[0]["k"] == 2


def test_half_availability_and_scoreboard_render():
    detail = match()
    buffer = render_recent_scoreboard(detail, detail["players"][0], "full", None, {}, {})
    with Image.open(buffer) as image:
        assert image.width == 1400 and image.height > 500
    detail["rounds"][0]["stats"] = []
    assert not has_half_data(detail)


def test_half_excludes_friendly_kills_and_damage():
    detail = match()
    detail["rounds"][0]["stats"][0]["damage_events"].append({"player": {"puuid": "one"}, "damage": 999})
    detail["kills"].append({"round": 0, "killer": {"puuid": "one"}, "victim": {"puuid": "one"}})
    row = build_scoreboard(detail, "first")[0]
    assert row["k"] == 1
    assert row["damage"] == 1200
