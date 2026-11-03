import pytest

from services.ai_recap import generate_recap


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
        "content": {"parts": [{"text": "A match recap."}]}}]}))
    player = {"puuid": "p1", "name": "Player", "team_id": "Red", "stats": {"kills": 12}}
    detail = {"players": [player], "metadata": {"map": {"name": "Ascent"}}, "teams": [], "kills": []}

    result = await generate_recap(session, "test-key", "gemini-3.5-flash-lite", detail, [player], player)

    assert result == "A match recap."
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
