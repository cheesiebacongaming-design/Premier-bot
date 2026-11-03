"""Application settings loaded from the process environment or a local .env file."""

from dataclasses import dataclass
import os
from pathlib import Path
from typing import Optional, Tuple

from dotenv import load_dotenv


REGIONS = ("eu", "na", "ap", "kr", "latam", "br")


@dataclass(frozen=True)
class Settings:
    discord_token: str
    henrik_api_key: str
    team_id: Optional[str]
    team_name: str
    team_tag: str
    roster: Tuple[str, ...]
    affinity: str
    guild_id: Optional[str]
    x_bearer: Optional[str]
    x_username: Optional[str]
    x_keyword: str
    api_base: str
    cache_file: Path
    queue_schedule_file: Path
    season_file: Path
    playbook_file: Path
    accounts_file: Path
    agents_file: Path
    play_sessions_file: Path
    matchfeeds_file: Path
    rankstates_file: Path
    icon_dir: Path
    font_dir: Path
    gemini_api_key: Optional[str]
    gemini_model: str


def load_settings(environ=None) -> Settings:
    """Read settings, reporting all missing required variables at once."""
    if environ is None:
        load_dotenv()
        environ = os.environ

    discord_token = (environ.get("DISCORD_TOKEN") or "").strip()
    henrik_api_key = (environ.get("HENRIK_API_KEY") or "").strip()
    missing = []
    if not discord_token:
        missing.append("DISCORD_TOKEN")
    if not henrik_api_key:
        missing.append("HENRIK_API_KEY")
    if missing:
        raise RuntimeError(
            "Missing required configuration: " + ", ".join(missing) + ". "
            "Set these in the process environment or in a local .env file."
        )

    return Settings(
        discord_token=discord_token,
        henrik_api_key=henrik_api_key,
        team_id=(environ.get("TEAM_ID") or "").strip() or None,
        team_name=(environ.get("TEAM_NAME") or "").strip(),
        team_tag=(environ.get("TEAM_TAG") or "").strip(),
        roster=tuple(
            entry.strip()
            for entry in (environ.get("ROSTER") or "").split(",")
            if entry.strip()
        ),
        affinity=(environ.get("AFFINITY") or "eu").strip().lower() or "eu",
        guild_id=(environ.get("GUILD_ID") or "").strip() or None,
        x_bearer=(environ.get("X_BEARER_TOKEN") or "").strip() or None,
        x_username=(environ.get("X_USERNAME") or "").strip() or None,
        x_keyword=(environ.get("X_KEYWORD") or "premier").strip().lower() or "premier",
        api_base="https://api.henrikdev.xyz",
        cache_file=Path("cache.json"),
        queue_schedule_file=Path("queue_schedule.json"),
        season_file=Path("season.json"),
        playbook_file=Path("playbooks.json"),
        accounts_file=Path("accounts.json"),
        agents_file=Path("agents.json"),
        play_sessions_file=Path("play_sessions.json"),
        matchfeeds_file=Path("matchfeeds.json"),
        rankstates_file=Path("rankstates.json"),
        icon_dir=Path("icons"),
        font_dir=Path(__file__).resolve().parent / "assets" / "fonts",
        gemini_api_key=(environ.get("GEMINI_API_KEY") or "").strip() or None,
        gemini_model=(environ.get("GEMINI_MODEL") or "gemini-3.5-flash-lite").strip(),
    )


SETTINGS = load_settings()
