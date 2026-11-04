from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
async def test_map_menu_excludes_rotated_maps_but_saved_entries_remain_accessible(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", "test-token")
    monkeypatch.setenv("HENRIK_API_KEY", "test-key")
    import bot

    monkeypatch.setattr(bot, "SETTINGS", SimpleNamespace(competitive_maps=("Haven", "Bind", "haven")))
    monkeypatch.setattr(bot, "playbook_service", SimpleNamespace(list=lambda: [{"map": "Ascent"}]))

    maps = await bot.available_playbook_maps()

    assert maps == ["Bind", "Haven"]
    assert bot.find_playbook_map(maps, "ASCENT") == "Ascent"
    assert bot.find_playbook_map(maps, "The Range") is None
