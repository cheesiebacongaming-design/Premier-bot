"""Validated strategy operations on the versioned playbook store."""

import copy
import os
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from models.playbook import SIDES, Strategy
from services.json_store import write_json
from services.playbook_store import PlaybookDataError, load_playbook_store, validate_store


class PlaybookConflict(PlaybookDataError):
    """A strategy has changed since it was opened for editing."""


_LOCK = threading.RLock()


@contextmanager
def _file_lock(path):
    """Use an advisory non-blocking lock shared by bot processes."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with lock_path.open("a+b") as lock_file:
        try:
            if os.name == "nt":
                import msvcrt
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise PlaybookConflict("Playbooks are being updated; try again") from exc
        try:
            yield
        finally:
            if os.name == "nt":
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _text(value, field, limit, required=False):
    if not isinstance(value, str):
        raise PlaybookDataError(f"{field} must be text")
    value = value.strip()
    if (required and not value) or len(value) > limit:
        raise PlaybookDataError(f"{field} must be {'1 to ' if required else 'at most '}{limit} characters")
    return value


def _url(value):
    value = _text(value, "Link", 500, True)
    try:
        parsed = urlsplit(value)
        valid = parsed.scheme.lower() in ("http", "https") and bool(parsed.hostname) and not parsed.username and not parsed.password
        valid = valid and not any(char.isspace() or ord(char) < 32 for char in value)
        valid = valid and not any(char in value for char in '<>')
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise PlaybookDataError("Links must be complete http or https URLs")
    return value


def _coordinate(value):
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not 0 <= value <= 1:
        raise PlaybookDataError("Diagram coordinates must be numbers from 0 to 1")
    return value


def validate_draft(draft, allow_legacy=False):
    if not isinstance(draft, dict):
        raise PlaybookDataError("Strategy must be an object")
    side = draft.get("side")
    if side not in SIDES or (side == "legacy" and not allow_legacy):
        raise PlaybookDataError("Choose attack or defence")
    steps = draft.get("steps", [])
    if not isinstance(steps, list) or len(steps) > 30:
        raise PlaybookDataError("Use at most 30 steps")
    links = draft.get("links", [])
    if not isinstance(links, list) or len(links) > 10:
        raise PlaybookDataError("Use at most 10 links")
    assignments = draft.get("assignments", [])
    if not isinstance(assignments, list) or len(assignments) > 5 or not all(isinstance(a, dict) for a in assignments):
        raise PlaybookDataError("Use at most five player assignments")
    clean_assignments = []
    for assignment in assignments:
        clean_assignments.append({key: _text(assignment.get(key, ""), key.replace("_", " "), 500)
                                  for key in ("role", "player", "agent", "starting_position", "responsibilities", "utility")})
    diagram = draft.get("diagram")
    if diagram is not None:
        if not isinstance(diagram, dict) or not isinstance(diagram.get("markers", []), list) or len(diagram.get("markers", [])) > 100:
            raise PlaybookDataError("Diagram markers must be a list of at most 100 items")
        clean_markers = []
        for marker in diagram.get("markers", []):
            if not isinstance(marker, dict) or marker.get("kind") not in ("player", "smoke", "utility", "step", "site", "path"):
                raise PlaybookDataError("Unknown tactical marker")
            points = marker.get("points", [])
            if not isinstance(points, list) or len(points) > 40:
                raise PlaybookDataError("A path can have at most 40 points")
            clean_points = []
            for point in points:
                if not isinstance(point, (list, tuple)) or len(point) != 2:
                    raise PlaybookDataError("Each path point must have two coordinates")
                clean_points.append([_coordinate(point[0]), _coordinate(point[1])])
            if marker["kind"] == "path" and len(clean_points) < 2:
                raise PlaybookDataError("A path must have at least two points")
            clean_markers.append({"kind": marker["kind"], "x": _coordinate(marker.get("x")),
                                  "y": _coordinate(marker.get("y")), "label": _text(marker.get("label", ""), "Marker label", 40),
                                  "points": clean_points})
        image_url = diagram.get("image_url", "")
        diagram = {"markers": clean_markers, "image_url": _url(image_url) if image_url else ""}
    return {"name": _text(draft.get("name", ""), "Name", 80, True),
            "map": _text(draft.get("map", ""), "Map", 100, True), "side": side,
            "objective": _text(draft.get("objective", ""), "Objective", 4000),
            "steps": [_text(step, "Step", 500, True) for step in steps],
            "assignments": clean_assignments, "utility": _text(draft.get("utility", ""), "Utility", 2000),
            "links": [_url(link) for link in links], "diagram": diagram}


class PlaybookService:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def _store(self):
        with _LOCK, _file_lock(self.path):
            yield load_playbook_store(self.path)

    def list(self, map_name=None, side=None):
        with self._store() as data:
            return copy.deepcopy([s for s in data["strategies"]
                                  if (not map_name or s["map"].casefold() == map_name.strip().casefold())
                                  and (not side or s["side"] == side)])

    def get(self, strategy_id):
        with self._store() as data:
            return copy.deepcopy(next((s for s in data["strategies"] if s["id"] == strategy_id), None))

    @staticmethod
    def _check_duplicate(data, draft, exclude=None):
        if any(s["id"] != exclude and s["map"].casefold() == draft["map"].casefold()
               and s["side"] == draft["side"] and s["name"].casefold() == draft["name"].casefold()
               for s in data["strategies"]):
            raise PlaybookDataError("A strategy with that name already exists for this map and side")

    def create(self, draft):
        clean = validate_draft(draft)
        with self._store() as data:
            self._check_duplicate(data, clean)
            now = datetime.now(timezone.utc).isoformat()
            strategy = Strategy(id=uuid.uuid4().hex, created_at=now, updated_at=now, **clean).to_dict()
            data["strategies"].append(strategy)
            write_json(self.path, validate_store(data))
            return copy.deepcopy(strategy)

    def update(self, strategy_id, expected_revision, draft):
        with self._store() as data:
            current = next((s for s in data["strategies"] if s["id"] == strategy_id), None)
            if current is None or current["revision"] != expected_revision:
                raise PlaybookConflict("Strategy changed or was deleted; reopen it before saving")
            clean = validate_draft(draft, allow_legacy=current["side"] == "legacy")
            self._check_duplicate(data, clean, exclude=strategy_id)
            current.update(clean)
            current["revision"] += 1
            current["updated_at"] = datetime.now(timezone.utc).isoformat()
            write_json(self.path, validate_store(data))
            return copy.deepcopy(current)

    def delete(self, strategy_id, expected_revision):
        with self._store() as data:
            current = next((s for s in data["strategies"] if s["id"] == strategy_id), None)
            if current is None or current["revision"] != expected_revision:
                raise PlaybookConflict("Strategy changed or was deleted; reopen it before deleting")
            data["strategies"].remove(current)
            write_json(self.path, validate_store(data))
