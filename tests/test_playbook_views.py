from types import SimpleNamespace
from unittest.mock import AsyncMock
from io import BytesIO

import discord
import pytest

from commands.playbook_views import PlaybookView, strategy_pages
from commands.playbook_admin import PlaybookAdminView, DraftModal
from services.playbook_service import PlaybookService


@pytest.mark.asyncio
async def test_browse_map_side_and_strategy_without_mutating(tmp_path):
    service = PlaybookService(tmp_path / "playbooks.json")
    strategy = service.create({"name": "Execute", "map": "Ascent", "side": "attack", "objective": "Take A"})
    view = PlaybookView(service, ["Ascent", "Haven"], 123)
    interaction = SimpleNamespace(user=SimpleNamespace(id=123), response=SimpleNamespace(edit_message=AsyncMock()))
    selector = view.children[0]
    selector._values = ["0"]
    await selector.callback(interaction)
    assert view.map_name == "Ascent"
    button = next(item for item in view.children if getattr(item, "label", "").startswith("Attack"))
    await button.callback(interaction)
    assert view.side == "attack"
    selector = next(item for item in view.children if getattr(item, "placeholder", "") == "Choose a strategy")
    selector._values = [strategy["id"]]
    await selector.callback(interaction)
    assert "Take A" in view.embed().description
    assert service.get(strategy["id"])["revision"] == 1


@pytest.mark.asyncio
async def test_browse_rejects_other_users(tmp_path):
    view = PlaybookView(PlaybookService(tmp_path / "playbooks.json"), ["Ascent"], 123)
    interaction = SimpleNamespace(user=SimpleNamespace(id=456), response=SimpleNamespace(send_message=AsyncMock()))
    assert not await view.interaction_check(interaction)
    interaction.response.send_message.assert_awaited_once()


def test_long_strategy_is_paginated_and_mentions_are_escaped():
    pages = strategy_pages({"objective": "@everyone " + "x" * 7000})
    assert len(pages) > 1
    assert all(len(page) <= 3400 for page in pages)
    assert "@everyone" not in pages[0]


@pytest.mark.asyncio
async def test_admin_create_draft_save_and_confirm_delete(tmp_path):
    service = PlaybookService(tmp_path / "playbooks.json")
    view = PlaybookAdminView(service, ["Ascent"], 123, "Ascent")
    interaction = SimpleNamespace(user=SimpleNamespace(id=123), guild=object(),
        permissions=SimpleNamespace(administrator=True),
        response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()))
    attack = next(item for item in view.children if getattr(item, "label", "") == "Attack")
    await attack.callback(interaction)
    new = next(item for item in view.children if getattr(item, "label", "") == "New strategy")
    await new.callback(interaction)
    assert service.list() == []
    modal = DraftModal(view, "basic")
    modal.inputs["name"]._value = "A Execute"
    modal.inputs["objective"]._value = "Take A"
    modal.inputs["steps"]._value = "Smoke\nEnter"
    await modal.on_submit(interaction)
    save = next(item for item in view.children if getattr(item, "label", "") == "Save strategy")
    await save.callback(interaction)
    assert service.list()[0]["steps"] == ["Smoke", "Enter"]
    delete = next(item for item in view.children if getattr(item, "label", "") == "Delete")
    await delete.callback(interaction)
    assert len(service.list()) == 1
    confirm = next(item for item in view.children if getattr(item, "label", "") == "Confirm delete")
    await confirm.callback(interaction)
    assert service.list() == []


@pytest.mark.asyncio
async def test_admin_requires_admin_permissions(tmp_path):
    view = PlaybookAdminView(PlaybookService(tmp_path / "playbooks.json"), ["Ascent"], 123)
    interaction = SimpleNamespace(user=SimpleNamespace(id=123), guild=object(),
        permissions=SimpleNamespace(administrator=False), response=SimpleNamespace(send_message=AsyncMock()))
    assert not await view.interaction_check(interaction)
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_browse_adds_diagram_without_changing_stored_strategy(tmp_path):
    service = PlaybookService(tmp_path / "playbooks.json")
    strategy = service.create({"name": "Execute", "map": "Ascent", "side": "attack", "diagram": {
        "markers": [{"kind": "player", "x": 0.2, "y": 0.4, "label": "1"}]}})
    render = AsyncMock(return_value=discord.File(BytesIO(b"png"), filename="playbook-map.png"))
    view = PlaybookView(service, ["Ascent"], 123, "Ascent", render)
    view.side, view.strategy_id = "attack", strategy["id"]
    interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()), edit_original_response=AsyncMock())

    await view.update_message(interaction)

    render.assert_awaited_once()
    interaction.response.defer.assert_awaited_once()
    sent = interaction.edit_original_response.await_args.kwargs
    assert len(sent["attachments"]) == 1
    assert sent["embed"].image.url == "attachment://playbook-map.png"
    assert service.get(strategy["id"])["revision"] == 1


@pytest.mark.asyncio
async def test_admin_stale_draft_does_not_overwrite_another_edit(tmp_path):
    service = PlaybookService(tmp_path / "playbooks.json")
    strategy = service.create({"name": "Execute", "map": "Ascent", "side": "attack", "objective": "First"})
    view = PlaybookAdminView(service, ["Ascent"], 123, "Ascent")
    view.side, view.selected = "attack", strategy["id"]
    view.refresh()
    interaction = SimpleNamespace(user=SimpleNamespace(id=123), guild=object(),
        permissions=SimpleNamespace(administrator=True),
        response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()))
    edit = next(item for item in view.children if getattr(item, "label", "") == "Edit")
    await edit.callback(interaction)
    service.update(strategy["id"], 1, {**strategy, "objective": "Someone else's edit"})
    view.draft["objective"] = "Stale edit"
    save = next(item for item in view.children if getattr(item, "label", "") == "Save strategy")
    await save.callback(interaction)
    assert service.get(strategy["id"])["objective"] == "Someone else's edit"
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_lineup_import_requires_empty_draft_and_does_not_save(tmp_path):
    service = PlaybookService(tmp_path / "playbooks.json")
    loader = AsyncMock(return_value=[{"player": "Player", "agent": "Jett"}])
    view = PlaybookAdminView(service, ["Ascent"], 123, "Ascent", lineup_loader=loader)
    view.side = "attack"
    view.refresh()
    interaction = SimpleNamespace(user=SimpleNamespace(id=123), guild=object(),
        permissions=SimpleNamespace(administrator=True),
        response=SimpleNamespace(edit_message=AsyncMock(), defer=AsyncMock(), send_message=AsyncMock()),
        edit_original_response=AsyncMock(), followup=SimpleNamespace(send=AsyncMock()))
    await next(item for item in view.children if getattr(item, "label", "") == "New strategy").callback(interaction)
    await next(item for item in view.children if getattr(item, "label", "") == "Import lineup").callback(interaction)
    assert view.draft["assignments"] == [{"player": "Player", "agent": "Jett"}]
    assert service.list() == []
    await next(item for item in view.children if getattr(item, "label", "") == "Import lineup").callback(interaction)
    assert loader.await_count == 1
    interaction.response.send_message.assert_awaited_once()


@pytest.mark.asyncio
async def test_browser_pages_through_more_than_discord_select_limit(tmp_path):
    service = PlaybookService(tmp_path / "playbooks.json")
    for index in range(27):
        service.create({"name": f"Strategy {index}", "map": "Ascent", "side": "attack"})
    view = PlaybookView(service, [f"Map {i}" for i in range(30)] + ["Ascent"], 123, "Ascent")
    view.side = "attack"
    view.refresh()
    assert max(len(item.options) for item in view.children if isinstance(item, discord.ui.Select)) == 25
    interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))
    await next(item for item in view.children if getattr(item, "label", "") == "Next strategies").callback(interaction)
    selector = next(item for item in view.children if getattr(item, "placeholder", "") == "Choose a strategy")
    assert len(selector.options) == 2
