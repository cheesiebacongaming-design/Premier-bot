from utils.statistics import build_row, identify_player_team, summarise_match


def _player(puuid, team_id, name=None, score=1000):
    return {
        "puuid": puuid,
        "name": name or puuid,
        "tag": "TEST",
        "team_id": team_id,
        "agent": {"name": "Sova", "id": "agent-sova"},
        "tier": {"id": 10, "name": "Gold 1"},
        "stats": {
            "kills": 10,
            "deaths": 8,
            "assists": 4,
            "score": score,
            "headshots": 10,
            "bodyshots": 20,
            "legshots": 10,
            "damage": {"dealt": 2000},
        },
    }


def _match_detail():
    players = [_player(f"red-{i}", "Red", score=2000 - i * 100) for i in range(5)]
    players += [_player(f"blue-{i}", "Blue", score=1500 - i * 100) for i in range(5)]
    return {
        "metadata": {"map": {"name": "Ascent"}, "started_at": "2026-11-01T12:00:00Z"},
        "players": players,
        "teams": [
            {"team_id": "Red", "won": False, "rounds": {"won": 8, "lost": 13},
             "premier_roster": {"id": "queue-id", "name": "Queue"}},
            {"team_id": "Blue", "won": True, "rounds": {"won": 13, "lost": 8},
             "premier_roster": {"id": "other-id", "name": "Opponents"}},
        ],
        "kills": [
            {"round": 1, "time_in_round_in_ms": 900, "killer": {"puuid": "red-0"},
             "victim": {"puuid": "blue-0"}},
            {"round": 1, "time_in_round_in_ms": 1200, "killer": {"puuid": "blue-1"},
             "victim": {"puuid": "red-1"}},
        ],
    }


def test_summary_identifies_linked_player_side_and_keeps_full_teams():
    summary = summarise_match(_match_detail(), {"RED-3"})

    assert summary["map"] == "Ascent"
    assert summary["score"] == "8-13"
    assert summary["won"] is False
    assert len(summary["rows"]) == 5
    assert len(summary["enemy_rows"]) == 5
    assert summary["rows"][0]["fb"] == 1


def test_summary_falls_back_to_premier_team_id_when_no_player_matches():
    summary = summarise_match(_match_detail(), set(), premier_team_id="queue-id")

    assert summary["us"] == "Queue"
    assert summary["them"] == "Opponents"
    assert summary["score"] == "8-13"


def test_player_team_identification_counts_registered_members():
    players = _match_detail()["players"]
    team_id, members = identify_player_team(players, {"red-1", "red-2", "blue-1"})

    assert team_id == "Red"
    assert len(members) == 3


def test_build_row_calculates_headshot_percent_without_divide_by_zero():
    player = _player("solo", "Red")
    player["stats"].update(headshots=0, bodyshots=0, legshots=0, score=2100)

    row = build_row(player, total_rounds=10, first_bloods=2)

    assert row["acs"] == 210
    assert row["hs"] == 0
    assert row["adr"] == 200
    assert row["fb"] == 2
