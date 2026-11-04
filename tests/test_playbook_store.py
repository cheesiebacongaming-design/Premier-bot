import json

import pytest

from services.json_store import JsonStoreError
from services.playbook_store import PlaybookDataError, load_playbook_store
import services.playbook_store as playbook_store


def test_missing_store_is_empty_without_creating_a_file(tmp_path):
    path = tmp_path / "playbooks.json"
    assert load_playbook_store(path) == {"version": 1, "strategies": []}
    assert not path.exists()


def test_legacy_migration_backs_up_exact_bytes_and_is_idempotent(tmp_path):
    path = tmp_path / "playbooks.json"
    original = json.dumps({"Ascent": {"notes": "Keep this\nline", "links": ["https://example.com"], "extra": 7}})
    path.write_text(original, encoding="utf-8")
    original_bytes = path.read_bytes()

    store = load_playbook_store(path)

    strategy = store["strategies"][0]
    assert strategy["side"] == "legacy"
    assert strategy["objective"] == "Keep this\nline"
    assert strategy["links"] == ["https://example.com"]
    assert strategy["legacy_source"]["extra"] == 7
    backups = list(tmp_path.glob("playbooks.json.legacy-*.bak"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original_bytes
    assert load_playbook_store(path) == store
    assert len(list(tmp_path.glob("*.bak"))) == 1


@pytest.mark.parametrize("data", [
    {"Ascent": {"notes": ["not text"], "links": []}},
    {"Ascent": {"notes": "valid", "links": [17]}},
    {"Ascent": "not an object"},
])
def test_invalid_legacy_data_is_left_untouched(tmp_path, data):
    path = tmp_path / "playbooks.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    original = path.read_bytes()
    with pytest.raises(PlaybookDataError):
        load_playbook_store(path)
    assert path.read_bytes() == original
    assert not list(tmp_path.glob("*.bak"))


def test_malformed_json_is_never_overwritten(tmp_path):
    path = tmp_path / "playbooks.json"
    path.write_bytes(b'{"Ascent":')
    with pytest.raises(JsonStoreError):
        load_playbook_store(path)
    assert path.read_bytes() == b'{"Ascent":'


def test_unknown_version_is_rejected_without_overwriting(tmp_path):
    path = tmp_path / "playbooks.json"
    path.write_text('{"version": 99, "strategies": []}', encoding="utf-8")
    with pytest.raises(PlaybookDataError, match="version"):
        load_playbook_store(path)
    assert json.loads(path.read_text())["version"] == 99


def test_duplicate_json_keys_are_not_silently_discarded(tmp_path):
    path = tmp_path / "playbooks.json"
    original = b'{"Ascent":{"notes":"first"},"Ascent":{"notes":"second"}}'
    path.write_bytes(original)
    with pytest.raises(PlaybookDataError, match="duplicate JSON key"):
        load_playbook_store(path)
    assert path.read_bytes() == original


def test_failed_migration_write_preserves_original_and_backup(tmp_path, monkeypatch):
    path = tmp_path / "playbooks.json"
    original = b'{"Ascent":{"notes":"keep me","links":[]}}'
    path.write_bytes(original)

    def fail_write(*args, **kwargs):
        raise JsonStoreError("simulated write failure")

    monkeypatch.setattr(playbook_store, "write_json", fail_write)
    with pytest.raises(JsonStoreError, match="simulated"):
        load_playbook_store(path)
    assert path.read_bytes() == original
    assert next(tmp_path.glob("playbooks.json.legacy-*.bak")).read_bytes() == original
