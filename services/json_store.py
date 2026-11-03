"""Safe JSON persistence for small bot configuration and user-state files."""

import copy
import json
import logging
import os
import tempfile
from pathlib import Path


logger = logging.getLogger(__name__)


class JsonStoreError(RuntimeError):
    """A JSON state file is unreadable or cannot be persisted safely."""


def read_json(path, default):
    """Read a JSON file, returning a fresh default only when it is absent.

    Malformed or structurally unexpected files raise instead of being treated
    as empty state, which could otherwise lead a later save to erase records.
    """
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return copy.deepcopy(default)
    except (OSError, UnicodeDecodeError) as exc:
        raise JsonStoreError(f"Could not read JSON state file {path.name}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise JsonStoreError(
            f"JSON state file {path.name} is invalid at line {exc.lineno}, column {exc.colno}; "
            "it was left unchanged"
        ) from exc

    if default is not None and not isinstance(data, type(default)):
        raise JsonStoreError(
            f"JSON state file {path.name} has an unexpected root type; it was left unchanged"
        )
    return data


def write_json(path, data, indent=1):
    """Atomically replace a JSON file after fully serializing its new value."""
    path = Path(path)
    try:
        payload = json.dumps(data, indent=indent)
    except (TypeError, ValueError) as exc:
        raise JsonStoreError(f"Could not serialize JSON state for {path.name}: {exc}") from exc

    temp_path = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
        )
        temp_path = Path(temp_name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temp_path, path)
    except OSError as exc:
        logger.exception("Could not atomically save JSON state file %s", path.name)
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Could not remove temporary JSON file %s", temp_path.name)
        raise JsonStoreError(f"Could not save JSON state file {path.name}: {exc}") from exc
