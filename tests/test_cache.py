import asyncio
import json
import time

import pytest

from services.cache import JsonCache


@pytest.mark.asyncio
async def test_fresh_cache_hit_skips_fetch(tmp_path):
    cache_path = tmp_path / "cache.json"
    cache_path.write_text(json.dumps({"key": {"t": time.time(), "ttl": 60, "data": [1, 2]}}))
    cache = JsonCache(cache_path)
    called = False

    async def fetch():
        nonlocal called
        called = True
        return []

    assert await cache.get("key", 60, fetch) == [1, 2]
    assert called is False
    assert cache.stats()["entries"] == 1


@pytest.mark.asyncio
async def test_expired_cache_value_is_returned_when_refresh_fails(tmp_path):
    cache = JsonCache(tmp_path / "cache.json")
    cache.entries["key"] = {"t": time.time() - 10, "ttl": 1, "data": "stale"}

    async def fetch():
        raise RuntimeError("temporary API outage")

    assert await cache.get("key", 1, fetch) == "stale"


@pytest.mark.asyncio
async def test_concurrent_requests_share_one_refresh(tmp_path):
    cache = JsonCache(tmp_path / "cache.json")
    calls = 0

    async def fetch():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return {"match": "abc"}

    first, second = await asyncio.gather(
        cache.get("match", 30, fetch), cache.get("match", 30, fetch)
    )
    await cache.flush_pending()

    assert first == second == {"match": "abc"}
    assert calls == 1
    assert cache.stats()["active_key_locks"] == 0


@pytest.mark.asyncio
async def test_clear_expiring_preserves_permanent_summaries(tmp_path):
    cache = JsonCache(tmp_path / "cache.json")
    cache.entries = {
        "summary": {"t": 1, "ttl": None, "data": {"score": "13-8"}},
        "temporary": {"t": 1, "ttl": 60, "data": []},
    }

    assert cache.clear_expiring() == 1
    await cache.flush_pending()
    assert set(cache.entries) == {"summary"}


@pytest.mark.asyncio
async def test_pruning_keeps_permanent_values_and_recent_stale_entries(tmp_path):
    cache = JsonCache(tmp_path / "cache.json", stale_retention=100, cleanup_interval=0)
    cache.entries = {
        "old": {"t": 1, "ttl": 10, "data": 0},
        "stale": {"t": 100, "ttl": 10, "data": 1},
        "permanent": {"t": 1, "ttl": None, "data": 2},
    }

    assert cache._prune_expired(now=200) == 1
    await cache.flush_pending()
    assert set(cache.entries) == {"stale", "permanent"}
