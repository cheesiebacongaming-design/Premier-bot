import pytest

from services.ai_recap import generate_recap, recap_facts


class FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self.body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def json(self, **kwargs):
        return self.body


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.request = None

    def post(self, url, **kwargs):
        self.request = (url, kwargs)
        return self.response


@pytest.mark.asyncio
async def test_gemini_recap_uses_match_facts_and_extracts_text():
    session = FakeSession(FakeResponse(200, {"candidates": [{"finishReason": "STOP",
        "content": {"parts": [{"text": '{"headline":"A wild match", "recap":"A match recap."}'}]}}]}))
    player = {"puuid": "p1", "name": "Player", "team_id": "Red", "stats": {"kills": 12}}
    detail = {"players": [player], "metadata": {"map": {"name": "Ascent"}}, "teams": [], "kills": []}

    result = await generate_recap(session, "test-key", "gemini-3.5-flash-lite", detail, [player], player)

    assert result == {"headline": "A wild match", "recap": "A match recap."}
    url, request = session.request
    assert url.endswith("/gemini-3.5-flash-lite:generateContent")
    assert request["headers"]["x-goog-api-key"] == "test-key"
    assert "Ascent" in request["json"]["contents"][0]["parts"][0]["text"]


@pytest.mark.asyncio
async def test_gemini_error_falls_back_without_exposing_response(caplog):
    session = FakeSession(FakeResponse(429, {"error": {"status": "RESOURCE_EXHAUSTED"}}))

    assert await generate_recap(session, "secret-key", "model", {}, [], {}) is None
    assert "RESOURCE_EXHAUSTED" in caplog.text
    assert "secret-key" not in caplog.text


@pytest.mark.asyncio
async def test_invalid_generated_fields_fall_back():
    session = FakeSession(FakeResponse(200, {"candidates": [{"finishReason": "STOP",
        "content": {"parts": [{"text": '{"headline":"", "recap":"Some recap."}'}]}}]}))

    assert await generate_recap(session, "test-key", "model", {}, [], {}) is None


def test_match_and_round_times_are_converted_to_seconds():
    detail = {"metadata": {"game_length_in_ms": 1_980_000}, "kills": [
        {"round": 0, "time_in_round_in_ms": 12_500, "killer": {}, "victim": {}},
        {"round": 1, "time_in_round_in_ms": None, "killer": {}, "victim": {}},
    ]}

    facts = recap_facts(detail, [], {})

    assert facts["duration_seconds"] == 1980
    assert facts["kill_events"][0]["time_in_round_seconds"] == 12.5
    assert facts["kill_events"][1]["time_in_round_seconds"] is None
