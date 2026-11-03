import json

import pytest

from services.json_store import JsonStoreError, read_json, write_json


def test_missing_file_returns_a_fresh_default(tmp_path):
    path = tmp_path / "accounts.json"
    default = {"nested": []}

    first = read_json(path, default)
    first["nested"].append("local")

    assert read_json(path, default) == {"nested": []}


def test_atomic_write_round_trips_json_and_leaves_no_temp_file(tmp_path):
    path = tmp_path / "nested" / "accounts.json"
    expected = {"123": {"name": "Player", "tag": "EU"}}

    write_json(path, expected)

    assert read_json(path, {}) == expected
    assert list(path.parent.iterdir()) == [path]


def test_invalid_json_is_not_treated_as_empty_or_overwritten(tmp_path):
    path = tmp_path / "accounts.json"
    original = b'{"player": '
    path.write_bytes(original)

    with pytest.raises(JsonStoreError, match="invalid"):
        read_json(path, {})

    assert path.read_bytes() == original


def test_unexpected_root_type_is_rejected(tmp_path):
    path = tmp_path / "agents.json"
    path.write_text(json.dumps(["not", "an", "object"]))

    with pytest.raises(JsonStoreError, match="root type"):
        read_json(path, {})


def test_serialization_failure_leaves_previous_file_unchanged(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"safe": true}')

    with pytest.raises(JsonStoreError, match="serialize"):
        write_json(path, {"bad": object()})

    assert path.read_text() == '{"safe": true}'
