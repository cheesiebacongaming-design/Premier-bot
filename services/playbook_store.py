"""Versioned playbook persistence and non-destructive legacy migration."""

import copy
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from models.playbook import SCHEMA_VERSION, SIDES, Strategy
from services.json_store import JsonStoreError, read_json, write_json


class PlaybookDataError(JsonStoreError):
    """Playbook data cannot be safely loaded or migrated."""


def _timestamp():
    return datetime.now(timezone.utc).isoformat()


def validate_store(data):
    if not isinstance(data, dict) or type(data.get("version")) is not int or data.get("version") != SCHEMA_VERSION:
        raise PlaybookDataError("Unsupported or invalid playbook schema version")
    strategies = data.get("strategies")
    if not isinstance(strategies, list):
        raise PlaybookDataError("Playbook strategies must be a list")
    ids = set()
    for entry in strategies:
        if not isinstance(entry, dict):
            raise PlaybookDataError("A playbook strategy is not an object")
        if any(not isinstance(entry.get(k), str) or not entry[k].strip() for k in ("id", "name", "map")):
            raise PlaybookDataError("A playbook strategy is missing its ID, name or map")
        if entry["id"] in ids or entry.get("side") not in SIDES:
            raise PlaybookDataError("A playbook strategy has a duplicate ID or invalid side")
        ids.add(entry["id"])
        if not isinstance(entry.get("steps"), list) or not all(isinstance(x, str) for x in entry["steps"]):
            raise PlaybookDataError("A playbook strategy has invalid steps")
        if not isinstance(entry.get("links"), list) or not all(isinstance(x, str) for x in entry["links"]):
            raise PlaybookDataError("A playbook strategy has invalid links")
        if (not isinstance(entry.get("assignments"), list) or len(entry["assignments"]) > 5
                or not all(isinstance(assignment, dict) and all(isinstance(value, str) for value in assignment.values())
                           for assignment in entry["assignments"])):
            raise PlaybookDataError("A playbook strategy has invalid assignments")
        if not isinstance(entry.get("revision"), int) or isinstance(entry["revision"], bool) or entry["revision"] < 1:
            raise PlaybookDataError("A playbook strategy has an invalid revision")
        for key in ("objective", "utility", "created_at", "updated_at"):
            if not isinstance(entry.get(key), str):
                raise PlaybookDataError(f"A playbook strategy has invalid {key}")
        if entry.get("diagram") is not None and not isinstance(entry["diagram"], dict):
            raise PlaybookDataError("A playbook strategy has an invalid diagram")
    return data


def _convert_legacy(data):
    timestamp = _timestamp()
    strategies = []
    for map_name, entry in data.items():
        if not isinstance(map_name, str) or not map_name.strip() or not isinstance(entry, dict):
            raise PlaybookDataError("Legacy playbook contains an invalid map or entry; original retained")
        notes, links = entry.get("notes", ""), entry.get("links", [])
        if not isinstance(notes, str) or not isinstance(links, list) or not all(isinstance(x, str) for x in links):
            raise PlaybookDataError(f"Legacy playbook for {map_name} has invalid notes or links; original retained")
        strategies.append(Strategy(
            id=uuid.uuid4().hex, name="Legacy playbook", map=map_name, side="legacy",
            objective=notes, links=copy.deepcopy(links), created_at=timestamp, updated_at=timestamp,
            legacy_source=copy.deepcopy(entry),
        ).to_dict())
    return validate_store({"version": SCHEMA_VERSION, "strategies": strategies})


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PlaybookDataError(f"Playbook contains duplicate JSON key {key!r}; original retained")
        result[key] = value
    return result


def load_playbook_store(path):
    """Read the store, preserving an exclusive byte-for-byte backup before migration."""
    path = Path(path)
    if not path.exists():
        return {"version": SCHEMA_VERSION, "strategies": []}
    data = read_json(path, {})
    try:
        original = path.read_bytes()
        parsed = json.loads(original.decode("utf-8"), object_pairs_hook=_unique_object)
        if parsed != data:
            raise PlaybookDataError("Playbook changed while reading; retry")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlaybookDataError(f"Could not safely read playbooks: {exc}") from exc
    if "version" in data and not isinstance(data.get("version"), dict):
        return validate_store(data)
    converted = _convert_legacy(data)
    try:
        backup = path.with_name(f"{path.name}.legacy-{uuid.uuid4().hex}.bak")
        with backup.open("xb") as output:
            output.write(original)
            output.flush()
            os.fsync(output.fileno())
        if path.read_bytes() != original:
            raise PlaybookDataError(f"Playbook changed during migration; backup retained as {backup.name}")
        write_json(path, converted)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PlaybookDataError(f"Could not safely migrate playbooks: {exc}") from exc
    return converted
