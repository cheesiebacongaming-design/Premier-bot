"""Versioned, JSON-compatible strategy data models."""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


SCHEMA_VERSION = 1
SIDES = ("attack", "defence", "legacy")


@dataclass
class PlayerAssignment:
    role: str = ""
    player: str = ""
    agent: str = ""
    starting_position: str = ""
    responsibilities: str = ""
    utility: str = ""


@dataclass
class TacticalMarker:
    kind: str
    x: float
    y: float
    label: str = ""
    points: List[List[float]] = field(default_factory=list)


@dataclass
class Strategy:
    id: str
    name: str
    map: str
    side: str
    objective: str = ""
    steps: List[str] = field(default_factory=list)
    assignments: List[Dict[str, str]] = field(default_factory=list)
    utility: str = ""
    links: List[str] = field(default_factory=list)
    diagram: Optional[Dict[str, Any]] = None
    created_at: str = ""
    updated_at: str = ""
    revision: int = 1
    legacy_source: Optional[Dict[str, Any]] = None

    def to_dict(self):
        return asdict(self)
