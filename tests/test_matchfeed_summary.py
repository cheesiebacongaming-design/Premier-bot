from utils.match_summary import _play_headline, _play_recap
from io import BytesIO
from unittest.mock import AsyncMock

import pytest


def test_registered_only_fallback_mentions_only_the_selected_player():
    registered = {"puuid": "p1", "name": "Friend", "team_id": "Red",
                  "stats": {"score": 2100, "kills": 10, "deaths": 12, "assists": 2}}
    other = {"puuid": "p2", "name": "Stranger", "team_id": "Red"}
    detail = {"players": [registered, other], "kills": [
        {"round": 0, "time_in_round_in_ms": 1000, "killer": {"puuid": "p2"}, "victim": {"puuid": "p1"}}
    ]}

    text = _play_recap("Haven", False, 8, 13, [], [registered], "30 min", detail, registered_only=True)
    headline = _play_headline([registered], 8, 13, registered_only=True)

    assert "Friend" in text
    assert "Stranger" not in text and "STRANGER" not in headline
    assert "topping the team" not in text


@pytest.mark.asyncio
async def test_matchfeed_card_sends_registered_players_to_summary_only(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    monkeypatch.setenv("HENRIK_API_KEY", "test-key")
    import bot

    friend = {"puuid": "p1", "name": "Friend", "team_id": "Red", "agent": {"id": "agent-id"}}
    stranger = {"puuid": "p2", "name": "Stranger", "team_id": "Red", "agent": {"id": "agent-id"}}
    team = [friend, stranger]
    detail = {"metadata": {"map": {}}, "players": team}
    ai = AsyncMock(return_value=None)
    captured = []

    def render(*args):
        captured.append(args)
        return BytesIO(b"png")

    monkeypatch.setattr(bot.client, "http_session", object(), raising=False)
    monkeypatch.setattr(bot, "icon_img", AsyncMock(return_value=None))
    monkeypatch.setattr(bot, "generate_recap", ai)
    monkeypatch.setattr(bot, "draw_play_result", render)
    card = await bot.play_match_image(detail, [friend], team, friend, summary_players=[friend])
    card.close()

    assert ai.await_args.args[4] == [friend]
    assert ai.await_args.kwargs["registered_only"] is True
    assert captured[0][2] == team
    assert captured[0][6] == [friend]
