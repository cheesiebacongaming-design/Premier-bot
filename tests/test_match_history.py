import pytest

from services.match_history import MatchHistoryService
from services.matchfeed import format_match_notification, group_registered_players


@pytest.mark.asyncio
async def test_player_histories_are_deduplicated_and_sorted():
    requests = []

    async def cached_api(path, ttl, **params):
        requests.append((path, ttl, params))
        if "Alice" in path:
            return [
                {"meta": {"id": "match-a", "started_at": "2026-11-01T10:00:00Z"}},
                {"meta": {"id": "match-b", "started_at": "2026-11-02T10:00:00Z"}},
            ]
        return [
            {"meta": {"id": "match-a", "started_at": "2026-11-01T10:00:00Z"}},
            {"meta": {"id": "match-c", "started_at": "2026-11-03T10:00:00Z"}},
        ]

    service = MatchHistoryService(
        cached_api, None, "eu", roster=("Alice#ONE", "Bob#TWO")
    )
    matches = await service.history_from_players()

    assert [match["id"] for match in matches] == ["match-c", "match-b", "match-a"]
    assert len(requests) == 2
    assert all(request[1:] == (600, {"mode": "premier", "size": 20}) for request in requests)


@pytest.mark.asyncio
async def test_team_history_falls_back_to_roster_history():
    requests = []

    async def cached_api(path, ttl, **params):
        requests.append(path)
        if path.endswith("/history"):
            raise RuntimeError("team history unavailable")
        return [{"meta": {"id": "match-a", "started_at": "2026-11-01T10:00:00Z"}}]

    service = MatchHistoryService(
        cached_api, None, "eu", roster=("Alice#ONE",), team_id="team-123"
    )

    assert await service.history() == [
        {"id": "match-a", "started_at": "2026-11-01T10:00:00Z"}
    ]
    assert requests[0] == "/valorant/v1/premier/team-123/history"
    assert "/valorant/v1/stored-matches/eu/Alice/ONE" in requests


@pytest.mark.asyncio
async def test_match_detail_region_override_is_preserved():
    calls = []

    async def api(path):
        calls.append(path)
        return {"metadata": {"match_id": "abc"}}

    service = MatchHistoryService(None, api, "eu")
    await service.match_detail("abc", region="na")

    assert calls == ["/valorant/v4/match/na/abc"]


def test_registered_players_group_by_team_and_ignore_unregistered_teammates():
    players = [
        {"name": "Alice", "tag": "ONE", "team_id": "Red"},
        {"name": "Bob", "tag": "TWO", "team_id": "Red"},
        {"name": "Unlinked", "tag": "THREE", "team_id": "Red"},
        {"name": "Alice", "tag": "ONE", "team_id": "Blue"},
        {"name": "NoTeam", "tag": "FOUR"},
    ]
    linked = {
        "alice#one": {"user_ids": {"10"}},
        "bob#two": {"user_ids": {"20", "21"}},
    }

    groups = group_registered_players(players, linked)

    assert set(groups) == {"Red", "Blue"}
    assert groups["Red"]["account_keys"] == {"alice#one", "bob#two"}
    assert groups["Red"]["user_ids"] == {"10", "20", "21"}
    assert groups["Red"]["players"] == players[:2]
    assert groups["Blue"]["players"] == [players[3]]


def test_match_notification_mentions_registered_users_once():
    assert format_match_notification(
        ["101", "202", "101"], "won", "13-8", "Ascent"
    ) == "<@101> <@202> won 13-8 on Ascent!"
