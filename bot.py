import asyncio
from copy import deepcopy
import hashlib
import io
import json
import re
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from urllib.parse import quote, urlsplit

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from config import REGIONS, SETTINGS
from commands import install_command_cogs
from commands.playbook_views import PlaybookView
from commands.playbook_admin import PlaybookAdminView
from graphics.assets import load_icon_image
from graphics.help_card import render_help_card
from graphics.lineup import render_lineup_card
from graphics.match_card import draw_play_result
from graphics.player_history import render_player_history
from graphics.playbook_card import render_playbook_card, render_strategy_card
from graphics.rank_change import draw_rank_change_card
from graphics.schedule import render_schedule_card
from graphics.scoreboard import draw_scoreboard
from services.cache import JsonCache
from services.ai_recap import generate_recap
from services.json_store import JsonStoreError, read_json, write_json
from services.match_history import MatchHistoryService
from services.matchfeed import format_match_notification, group_registered_players
from services.playbook_service import PlaybookDataError, PlaybookService
from services.valorant_api import HenrikDevClient
from utils.formatting import fmt_range, parse_time
from utils.statistics import summarise_match

TOKEN = SETTINGS.discord_token
HENRIK_KEY = SETTINGS.henrik_api_key
TEAM_ID = SETTINGS.team_id
TEAM_NAME = SETTINGS.team_name
TEAM_TAG = SETTINGS.team_tag
ROSTER = list(SETTINGS.roster)
AFFINITY = SETTINGS.affinity
GUILD_ID = SETTINGS.guild_id
BASE = SETTINGS.api_base
X_BEARER = SETTINGS.x_bearer
X_USERNAME = SETTINGS.x_username
X_KEYWORD = SETTINGS.x_keyword
CACHE_FILE = SETTINGS.cache_file
QUEUE_SCHEDULE_FILE = SETTINGS.queue_schedule_file
SEASON_FILE = SETTINGS.season_file
PLAYBOOK_FILE = SETTINGS.playbook_file
ACCOUNTS_FILE = SETTINGS.accounts_file
AGENTS_FILE = SETTINGS.agents_file
PLAY_SESSIONS_FILE = SETTINGS.play_sessions_file
MATCHFEEDS_FILE = SETTINGS.matchfeeds_file
RANKSTATES_FILE = SETTINGS.rankstates_file
ICON_DIR = SETTINGS.icon_dir

intents = discord.Intents.default()
class BotClient(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._background_tasks_started = False
        self._command_cogs_loaded = False
        self._background_tasks = []
        self._commands_synced = False
        self._icons_warmed = False
        self._icon_warm_task = None

    async def setup_hook(self):
        await super().setup_hook()
        if not self._command_cogs_loaded:
            await install_command_cogs(self, tree)
            self._command_cogs_loaded = True
        if self._background_tasks_started:
            return
        ICON_DIR.mkdir(parents=True, exist_ok=True)
        timeout = aiohttp.ClientTimeout(total=15, connect=5)
        self.http_session = aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(limit=20, ttl_dns_cache=300))
        self.x_session = aiohttp.ClientSession(timeout=timeout)
        api_client.bind_session(self.http_session)
        self.queue_reminder_task = asyncio.create_task(queue_reminder_loop(), name="queue-reminders")
        self.play_session_task = asyncio.create_task(play_session_loop(), name="play-sessions")
        self.matchfeed_task = asyncio.create_task(matchfeed_loop(), name="match-feed")
        self._background_tasks = [
            self.queue_reminder_task,
            self.play_session_task,
            self.matchfeed_task,
        ]
        self._background_tasks_started = True

    async def close(self):
        tasks = list(self._background_tasks)
        if self._icon_warm_task is not None:
            tasks.append(self._icon_warm_task)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for session in (getattr(self, "http_session", None), getattr(self, "x_session", None)):
            if session and not session.closed:
                await session.close()
        await cache_store.flush_pending()
        await super().close()


client = BotClient(command_prefix=commands.when_mentioned, intents=intents, help_command=None)
tree = client.tree
cache_store = JsonCache(CACHE_FILE)
api_client = HenrikDevClient(BASE, HENRIK_KEY, max_concurrency=3)
_icon_locks = {}


# ---------- API helpers ----------
async def api(path: str, **params):
    return await api_client.get(path, **params)


async def cached(key, ttl, fn):
    return await cache_store.get(key, ttl, fn)


def cached_api(path, ttl, **params):
    return cache_store.get(path + json.dumps(params, sort_keys=True), ttl, lambda: api(path, **params))


match_history = MatchHistoryService(
    cached_api, api, AFFINITY, ROSTER, TEAM_ID, TEAM_NAME, TEAM_TAG
)


async def x_get(path, **params):
    async with client.x_session.get(
        "https://api.twitter.com/2" + path,
        headers={"Authorization": f"Bearer {X_BEARER}"},
        params=params,
    ) as r:
        body = await r.json()
        if r.status != 200:
            raise RuntimeError(f"X API {r.status}: {body}")
        return body


async def latest_schedule_tweet():
    """Newest recent tweet containing X_KEYWORD, cached for 30 minutes."""
    if not (X_BEARER and X_USERNAME):
        return None

    async def fetch():
        user = await x_get(f"/users/by/username/{X_USERNAME}")
        tweets = await x_get(
            f"/users/{user['data']['id']}/tweets",
            max_results=20,
            exclude="retweets,replies",
            **{"tweet.fields": "created_at"},
        )
        return next((t for t in tweets.get("data", []) if X_KEYWORD in t["text"].lower()), None)

    return await cached("x:latest", 1800, fetch)


def ts(d, style="F"):
    if isinstance(d, str):
        d = parse_time(d)
    return f"<t:{int(d.timestamp())}:{style}>" if d else "unknown"


def team_path():
    return match_history.team_path()


async def team_data():
    return await match_history.team_data()


async def team_members():
    return await match_history.team_members()


async def history_from_players():
    return await match_history.history_from_players()


async def history():
    return await match_history.history()


async def match_detail(match_id: str, region=None):
    return await match_history.match_detail(match_id, region)


def summarise(detail: dict, members: set, premier_team_id=None, premier_team_name=None,
              premier_team_tag=None):
    return summarise_match(
        detail, members, premier_team_id, premier_team_name, premier_team_tag, TEAM_NAME
    )


def player_table(rows):
    lines = ["Player        Agent     K/D/A     ACS ADR  HS%"]
    for r in rows:
        lines.append(
            f"{r['name'][:12]:<13} {r['agent'][:9]:<9} {r['k']}/{r['d']}/{r['a']:<6} "
            f"{r['acs']:<3} {r['adr']:<4} {r['hs']}%"
        )
    return "```\n" + "\n".join(lines) + "\n```"


# ---------- Scoreboard image ----------
# ---------- External image assets ----------


async def icon_maps():
    """Agent image URLs and rank tier icons from valorant-api.com, cached a week."""

    async def fetch():
        async def get_json(url):
            async with client.http_session.get(url) as r:
                r.raise_for_status()
                return await r.json()
        agents_data, tiers_data = await asyncio.gather(
            get_json("https://valorant-api.com/v1/agents?isPlayableCharacter=true"),
            get_json("https://valorant-api.com/v1/competitivetiers"),
        )
        agents, tiers = agents_data["data"], tiers_data["data"]
        return {
            "agents": {a["uuid"].lower(): (a.get("displayIcon") or a.get("bustPortrait") or a.get("killfeedPortrait") or a.get("fullPortrait")) for a in agents},
            "agent_icon_urls": {
                a["uuid"].lower(): list(dict.fromkeys(
                    url for url in (a.get("displayIcon"), a.get("bustPortrait"), a.get("killfeedPortrait"), a.get("fullPortrait")) if url
                )) for a in agents
            },
            "agent_names": {a["uuid"].lower(): a["displayName"] for a in agents},
            "tiers": {str(t["tier"]): t.get("smallIcon") for t in tiers[-1]["tiers"]},
        }

    try:
        return await cached("icons:maps5", 7 * 86400, fetch)
    except Exception:
        # Continue using the repository's older cached image catalog if the asset host is down.
        return await cached("icons:maps2", None, fetch)


async def icon_img(url, size):
    if not url:
        return None
    path = ICON_DIR / (hashlib.md5(url.encode()).hexdigest() + ".png")
    try:
        lock = _icon_locks.setdefault(url, asyncio.Lock())
        async with lock:
            if not path.exists():
                async with client.http_session.get(url) as r:
                    if r.status != 200:
                        return None
                    path.write_bytes(await r.read())
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, load_icon_image, path, size)
    except Exception:
        return None


COLS = [("ACS", 500), ("K", 565), ("D", 610), ("A", 655), ("+/-", 715), ("K/D", 780), ("ADR", 845), ("HS%", 910), ("FB", 965)]
GREY = (150, 162, 175)
WHITE = (240, 244, 248)
PLAY_TAG_STYLES = {
    "MVP": ((109, 79, 38), (255, 214, 128)),
    "LVP": ((105, 39, 45), (255, 151, 157)),
    "VICTIM": ((67, 68, 78), (211, 214, 224)),
    "ACE": ((141, 91, 32), (255, 223, 132)),
    "QUAD": ((119, 93, 33), (255, 218, 119)),
    "ENTRY": ((39, 78, 122), (163, 205, 255)),
    "DINK MACHINE": ((89, 59, 126), (219, 183, 255)),
    "ASSIST KING": ((35, 100, 91), (153, 235, 214)),
    "DAMAGE DEALER": ((137, 68, 37), (255, 187, 143)),
    "SURVIVOR": ((48, 98, 63), (170, 237, 178)),
    "CLUTCH": ((123, 55, 92), (255, 172, 218)),
}


async def current_rank(name, tag):
    """Player's current rank from the v3 MMR endpoint (cached 6 hours)."""
    if not name or not tag:
        return None
    try:
        data = await cached_api(f"/valorant/v3/mmr/{AFFINITY}/pc/{quote(name)}/{quote(tag)}", 6 * 3600)
    except Exception:
        return None
    cur = (data.get("current") or {}) if isinstance(data, dict) else {}
    tier = cur.get("tier") or {}
    return tier.get("id") or 0, tier.get("name") or "Unrated"


async def render_scoreboard(s, filename):
    # copy the rows so the live rank never gets written into the cached match summary
    s = {**s, "rows": [dict(r) for r in s["rows"]], "enemy_rows": [dict(r) for r in s["enemy_rows"]]}
    rows = s["rows"] + s["enemy_rows"]
    ranks = await asyncio.gather(*(current_rank(r.get("name"), r.get("tag")) for r in rows))
    for r, rank in zip(rows, ranks):
        if rank:
            r["tier"], r["tier_name"] = rank
    try:
        maps = await icon_maps()
    except Exception:
        maps = {"agents": {}, "tiers": {}}
    keys, jobs = [], []
    for r in rows:
        keys.append(("a", r["agent_id"]))
        jobs.append(icon_img(maps["agents"].get(r["agent_id"]), 36))
        keys.append(("t", r["tier"]))
        jobs.append(icon_img(maps["tiers"].get(str(r["tier"])), 30))
    icons = dict(zip(keys, await asyncio.gather(*jobs)))
    loop = asyncio.get_running_loop()
    image = await loop.run_in_executor(None, draw_scoreboard, s, icons)
    return discord.File(image, filename=filename)


async def load_summaries(matches):
    members = await team_members()

    async def one(m):
        async def build():
            return summarise(await match_detail(m["id"]), members)

        try:
            # finished matches never change, so cache them permanently
            return m, await cached(f"summary4:{m['id']}", None, build)
        except Exception as e:  # keep going if one match fails
            return m, {"error": str(e)}

    return await asyncio.gather(*(one(m) for m in matches))


# ---------- Commands ----------
def load_season():
    try:
        return read_json(SEASON_FILE, None)
    except JsonStoreError as exc:
        print(f"Could not load season configuration: {exc}")
        return None


def season_maps():
    season = load_season()
    return [w["map"] for w in season["weeks"] if "Playoffs" not in w["map"]] if season else []


async def valorant_map_catalog():
    async def fetch():
        async with client.http_session.get("https://valorant-api.com/v1/maps") as response:
            response.raise_for_status()
            return (await response.json()).get("data", [])
    return await cached("valorant:map-catalog", 7 * 86400, fetch)


async def map_background_path(map_uuid, map_name="Unknown"):
    """Fetch map splash art into the same cache path used by match-feed cards."""
    path = ICON_DIR / f"map_{map_uuid}.jpg"
    if path.exists():
        return path
    try:
        async with client.http_session.get(f"https://valorant-api.com/v1/maps/{map_uuid}") as response:
            response.raise_for_status()
            map_info = (await response.json()).get("data", {})
        image_url = map_info.get("splash") or map_info.get("listViewIcon")
        if image_url:
            async with client.http_session.get(image_url) as response:
                response.raise_for_status()
                path.write_bytes(await response.read())
    except Exception as exc:
        print(f"Map art unavailable for {map_name}: {exc}")
        return None
    return path if path.exists() else None


async def schedule_card_file(season, today):
    map_art = {}
    try:
        catalog = await valorant_map_catalog()
        by_name = {str(item.get("displayName", "")).casefold(): item for item in catalog}
        jobs = {}
        for week in season.get("weeks", []):
            map_name = week.get("map", "")
            info = by_name.get(map_name.casefold())
            map_uuid = (info or {}).get("uuid")
            if map_uuid:
                jobs[map_name.casefold()] = map_background_path(map_uuid, map_name)
        paths = await asyncio.gather(*jobs.values()) if jobs else []
        map_art = dict(zip(jobs.keys(), paths))
    except Exception as exc:
        print(f"Schedule map art unavailable: {exc}")
    loop = asyncio.get_running_loop()
    buffer = await loop.run_in_executor(None, render_schedule_card, season, map_art, today)
    return discord.File(buffer, filename="premier-schedule.png")


@tree.command(name="schedule", description="View the Premier map schedule")
async def schedule(interaction: discord.Interaction):
    await interaction.response.defer()
    season = load_season()
    if not season:
        return await interaction.followup.send("No season schedule is configured.")

    today = datetime.now(timezone.utc).date().isoformat()
    card = await schedule_card_file(season, today)
    await interaction.followup.send(file=card)


@tree.command(name="setschedule", description="Set weekly queue reminder days and times")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    days="Queue days, comma-separated (e.g. Tuesday,Thursday)",
    local_time="Queue start time in 24-hour HH:MM format",
    timezone_name="IANA time zone (e.g. Europe/London or America/Los_Angeles)",
    channel="Channel for reminders (defaults to this channel)",
)
async def setschedule(
    interaction: discord.Interaction,
    days: str = "",
    local_time: str = "",
    timezone_name: str = "",
    channel: discord.TextChannel = None,
):
    if not interaction.guild:
        return await interaction.response.send_message("Use this command in a server.", ephemeral=True)
    if not interaction.permissions.administrator:
        return await interaction.response.send_message("Only server administrators can set the schedule.", ephemeral=True)

    supplied = (days.strip(), local_time.strip(), timezone_name.strip())
    if any(supplied) and not all(supplied):
        return await interaction.response.send_message(
            "For reminders, provide days, local_time, and timezone_name together.", ephemeral=True
        )
    if not all(supplied):
        return await interaction.response.send_message(
            "Provide days, local_time, and timezone_name together to configure queue reminders.", ephemeral=True
        )

    if all(supplied):
        weekdays = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
        day_lookup = {name.casefold(): i for i, name in enumerate(weekdays)}
        day_lookup.update({name[:3].casefold(): i for i, name in enumerate(weekdays)})
        try:
            zone = ZoneInfo(timezone_name.strip())
        except (ZoneInfoNotFoundError, ValueError):
            return await interaction.response.send_message("I don't recognize that IANA time zone.", ephemeral=True)
        try:
            parsed_time = datetime.strptime(local_time.strip(), "%H:%M").time()
        except ValueError:
            return await interaction.response.send_message("Use 24-hour HH:MM for local_time, such as 20:00.", ephemeral=True)
        day_values = []
        for raw_day in days.split(","):
            day = day_lookup.get(raw_day.strip().casefold())
            if day is None:
                return await interaction.response.send_message("Use weekday names separated by commas.", ephemeral=True)
            if day not in day_values:
                day_values.append(day)
        if not day_values:
            return await interaction.response.send_message("Add at least one queue day.", ephemeral=True)
        target = channel or (interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None)
        if target is None:
            return await interaction.response.send_message("Choose a text channel for reminders.", ephemeral=True)
        configs = load_queue_schedules()
        configs[str(interaction.guild_id)] = {
            "days": sorted(day_values), "time": parsed_time.strftime("%H:%M"),
            "timezone": zone.key, "channel_id": target.id, "last_reminder_date": None,
        }
        save_queue_schedules(configs)

    day_names = ", ".join(weekdays[d] for d in sorted(set(day_values)))
    await interaction.response.send_message(
        f"Reminders set for {day_names} at {parsed_time.strftime('%H:%M')} {zone.key} in {target.mention}, 30 minutes before queue.",
        ephemeral=True,
    )


@tree.command(name="testreminder", description="Post a test queue reminder showing agent picks and mentioning picked players (administrators only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="Where to post the test reminder (defaults to the configured reminder channel)")
async def testreminder(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not interaction.guild:
        return await interaction.response.send_message("Use this command in a server.", ephemeral=True)
    if not interaction.permissions.administrator:
        return await interaction.response.send_message("Only server administrators can test reminders.", ephemeral=True)
    config = load_queue_schedules().get(str(interaction.guild_id))
    target = channel
    if target is None and config:
        target = interaction.guild.get_channel(int(config.get("channel_id", 0)))
    if target is None:
        return await interaction.response.send_message(
            "No reminder channel is configured. Set one with /setschedule, or choose a channel for this test.", ephemeral=True
        )
    label = "Test queue reminder"
    if config:
        try:
            zone = ZoneInfo(config["timezone"])
            hour, minute = map(int, config["time"].split(":"))
            days = ", ".join(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")[d] for d in config["days"])
            label = f"Test reminder · {days} at {hour:02d}:{minute:02d} {zone.key}"
        except (KeyError, ValueError, ZoneInfoNotFoundError):
            pass
    await interaction.response.defer(ephemeral=True)
    embed, picked_user_ids, lineup_file = await build_lineup_reminder(label, "This is a test reminder.", interaction.guild)
    mentions = " ".join(f"<@{uid}>" for uid in picked_user_ids)
    content = "Test queue reminder. Players with agent picks:" + (f" {mentions}" if mentions else " Nobody has picked an agent yet.")
    send_args = {
        "content": content,
        "embed": embed,
        "allowed_mentions": discord.AllowedMentions(users=[discord.Object(id=uid) for uid in picked_user_ids]),
    }
    if lineup_file:
        send_args["file"] = lineup_file
    await target.send(**send_args)
    await interaction.followup.send(f"Test reminder posted in {target.mention}.", ephemeral=True)


def load_queue_schedules():
    return read_json(QUEUE_SCHEDULE_FILE, {})


def save_queue_schedules(data):
    write_json(QUEUE_SCHEDULE_FILE, data, indent=2)


async def discord_display_name(guild, user_id):
    member = guild.get_member(user_id) if guild else None
    member = member or client.get_user(user_id)
    if member:
        return member.display_name
    if guild:
        try:
            return (await guild.fetch_member(user_id)).display_name
        except discord.DiscordException:
            pass
    return "Former server member"


async def build_lineup_reminder(title, intro, guild=None):
    embed = discord.Embed(title=title, description=intro, colour=0xFF4655)
    week = current_week()
    if not week:
        embed.add_field(name="Lineup", value="No current map is available in season.json.", inline=False)
        return embed, [], None

    map_name = week["map"]
    data = load_agents()
    picks = data.get(matching_map_key(data, map_name), {})
    picked_user_ids = []
    lines = []
    slots = []
    for user_id, agent in sorted(picks.items(), key=lambda item: str(item[0])):
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            continue
        picked_user_ids.append(uid)
        player_name = await discord_display_name(guild, uid)
        slots.append({"player": player_name, "agent": agent, "riot": ""})
        lines.append(f"<@{uid}> — **{agent}**")

    picked_count = len(slots)
    while len(slots) < 5:
        slots.append({"player": "Open slot", "agent": None, "riot": "Waiting for an agent pick"})
    if not lines:
        lines = ["Nobody has selected an agent yet. Use /agent to add a pick."]
    embed.add_field(name=f"{map_name} lineup", value="\n".join(lines)[:1024], inline=False)
    missing = max(0, 5 - picked_count)
    embed.set_footer(text=f"{picked_count}/5 agent picks · {missing} missing")
    week_label = f"{week['label']} · {fmt_range(week['start'], week['end'])}"
    lineup_file = await lineup_card_file(map_name, week_label, slots)
    embed.set_image(url="attachment://lineup.png")
    return embed, picked_user_ids, lineup_file


def next_queue_start(config, now_utc):
    zone = ZoneInfo(config["timezone"])
    local_now = now_utc.astimezone(zone)
    hour, minute = map(int, config["time"].split(":"))
    candidates = []
    for offset in range(8):
        day = local_now.date() + timedelta(days=offset)
        if day.weekday() not in config["days"]:
            continue
        candidate = datetime(day.year, day.month, day.day, hour, minute, tzinfo=zone)
        if candidate > local_now:
            candidates.append(candidate)
    return min(candidates) if candidates else None


async def queue_reminder_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        now = datetime.now(timezone.utc)
        try:
            configs = load_queue_schedules()
        except JsonStoreError as exc:
            print(f"Queue reminder state is unavailable: {exc}")
            await asyncio.sleep(60)
            continue
        original_configs = deepcopy(configs)
        dirty = False
        for guild_id, config in configs.items():
            try:
                queue_start = next_queue_start(config, now)
                if not queue_start:
                    continue
                seconds_until = (queue_start.astimezone(timezone.utc) - now).total_seconds()
                local_date = queue_start.date().isoformat()
                if 0 < seconds_until <= 30 * 60 and config.get("last_reminder_date") != local_date:
                    channel_id = int(config["channel_id"])
                    channel = client.get_channel(channel_id) or await client.fetch_channel(channel_id)
                    label = queue_start.strftime("%A %H:%M %Z")
                    guild = client.get_guild(int(guild_id))
                    embed, picked_user_ids, lineup_file = await build_lineup_reminder(
                        f"Queue reminder · {label}", "Queue window starts in about 30 minutes.", guild
                    )
                    mentions = " ".join(f"<@{uid}>" for uid in picked_user_ids)
                    content = "Players with agent picks:" + (f" {mentions}" if mentions else " nobody has picked an agent yet.")
                    send_args = {
                        "content": content,
                        "embed": embed,
                        "allowed_mentions": discord.AllowedMentions(users=[discord.Object(id=uid) for uid in picked_user_ids]),
                    }
                    if lineup_file:
                        send_args["file"] = lineup_file
                    await channel.send(**send_args)
                    config["last_reminder_date"] = local_date
                    dirty = True
            except (KeyError, ValueError, ZoneInfoNotFoundError, discord.DiscordException) as exc:
                print(f"Queue reminder failed for guild {guild_id}: {exc}")
        if dirty:
            try:
                latest = load_queue_schedules()
                for guild_id, updated in configs.items():
                    original = original_configs.get(guild_id)
                    current = latest.get(guild_id)
                    if not original or not current:
                        continue
                    original_schedule = {key: value for key, value in original.items() if key != "last_reminder_date"}
                    current_schedule = {key: value for key, value in current.items() if key != "last_reminder_date"}
                    if original_schedule == current_schedule:
                        current["last_reminder_date"] = updated.get("last_reminder_date")
                save_queue_schedules(latest)
            except JsonStoreError as exc:
                print(f"Could not save queue reminder state: {exc}")
        await asyncio.sleep(60)


@tree.command(name="clearcache", description="Force fresh data on the next command")
@app_commands.default_permissions(manage_guild=True)
async def clearcache(interaction: discord.Interaction):
    # keep permanent match summaries, drop everything that expires
    cache_store.clear_expiring()
    await interaction.response.send_message("Cleared. Match summaries stay cached.", ephemeral=True)


class ScoreboardView(discord.ui.View):
    """Dropdown under /results to open the full scoreboard of any listed match."""

    def __init__(self, matches, embed):
        super().__init__(timeout=900)
        self.matches = matches
        self.embed = embed
        options = []
        for i, (_, s) in enumerate(matches[:25]):
            d = parse_time(s.get("started"))
            options.append(
                discord.SelectOption(
                    label=f"{'WIN' if s['won'] else 'LOSS'} {s['score']} on {s['map']}"[:100],
                    description=d.strftime("%d %b %H:%M UTC") if d else None,
                    value=str(i),
                )
            )
        options[0].default = True  # the latest match is shown first
        self.select = discord.ui.Select(placeholder="View a match scoreboard", options=options)
        self.select.callback = self.on_select
        self.add_item(self.select)

    async def on_select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        idx = int(interaction.data["values"][0])
        filename = f"match{idx}.png"
        file = await render_scoreboard(self.matches[idx][1], filename)
        embed = self.embed.copy()
        embed.set_image(url=f"attachment://{filename}")
        for i, opt in enumerate(self.select.options):
            opt.default = i == idx  # keep the dropdown showing the chosen match
        # edit the original message in place instead of posting a new one
        await interaction.edit_original_response(embed=embed, attachments=[file], view=self)


@tree.command(name="results", description="Premier results from the past week")
@app_commands.describe(days="How many days back (default 7)")
async def results(interaction: discord.Interaction, days: int = 7):
    await interaction.response.defer()
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        recent = [m for m in await history() if (parse_time(m.get("started_at")) or cutoff) >= cutoff]
        if not recent:
            return await interaction.followup.send(f"No Premier matches in the last {days} days.")
        pairs = await load_summaries(recent[:10])
    except Exception as e:
        return await interaction.followup.send(f"Couldn't fetch results: {e}")

    embed = discord.Embed(title=f"{TEAM_NAME or 'Premier'} results, last {days} days", colour=0xFF4655)
    wins = 0
    for m, s in pairs:
        if "error" in s:
            embed.add_field(name="Match", value=f"Couldn't load details: {s['error'][:100]}", inline=False)
            continue
        wins += s["won"]
        top = s["rows"][0] if s["rows"] else None
        delta = (m.get("points_after", 0) or 0) - (m.get("points_before", 0) or 0)
        embed.add_field(
            name=f"{'WIN' if s['won'] else 'LOSS'} {s['score']} on {s['map']}",
            value=(
                f"{ts(s['started'])}" + (f"\nPoints: {delta:+d}" if "points_after" in m else "")
                + (f"\nTop: {top['name']} ({top['agent']}) {top['k']}/{top['d']}/{top['a']}, ACS {top['acs']}" if top else "")
            ),
            inline=False,
        )
    embed.set_footer(text=f"{wins}W - {len(pairs) - wins}L")
    good = [(m, s) for m, s in pairs if "error" not in s]
    if not good:
        return await interaction.followup.send(embed=embed)
    file = await render_scoreboard(good[0][1], "latest.png")
    embed.set_image(url="attachment://latest.png")
    view = ScoreboardView(good, embed) if len(good) > 1 else discord.utils.MISSING
    await interaction.followup.send(embed=embed, file=file, view=view)


@tree.command(name="mapresults", description="Results and player stats on a specific map")
@app_commands.describe(map_name="e.g. Ascent", limit="How many recent matches to show (max 5)")
async def mapresults(interaction: discord.Interaction, map_name: str, limit: int = 3):
    await interaction.response.defer()
    try:
        pairs = await load_summaries((await history())[:25])
    except Exception as e:
        return await interaction.followup.send(f"Couldn't fetch results: {e}")

    on_map = [(m, s) for m, s in pairs if "error" not in s and s["map"].lower() == map_name.lower()]
    if not on_map:
        return await interaction.followup.send(f"No recent Premier matches found on {map_name}.")

    wins = sum(s["won"] for _, s in on_map)
    embeds = [
        discord.Embed(
            title=f"{on_map[0][1]['map']}: {wins}W - {len(on_map) - wins}L",
            description=f"{len(on_map)} recent matches on this map",
            colour=0xFF4655,
        )
    ]
    files = []
    for i, (_, s) in enumerate(on_map[: max(1, min(limit, 5))]):
        e = discord.Embed(
            title=f"{'WIN' if s['won'] else 'LOSS'} {s['score']}",
            colour=0x2ECC71 if s["won"] else 0xE74C3C,
        )
        files.append(await render_scoreboard(s, f"m{i}.png"))
        e.set_image(url=f"attachment://m{i}.png")
        embeds.append(e)
    await interaction.followup.send(embeds=embeds, files=files)


@mapresults.autocomplete("map_name")
async def map_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=m, value=m) for m in season_maps() if current.lower() in m.lower()][:25]


WIN, LOSS, DRAW = "\u2705", "\u274c", "\u2796"


def parse_stored(m):
    """One entry of the stored-matches endpoint (same parsing as the tracker app)."""
    meta, s, teams = m.get("meta", {}), m.get("stats", {}), m.get("teams", {})
    team = str(s.get("team", "")).lower()
    won, score = None, ""
    if team in ("red", "blue") and "red" in teams and "blue" in teams:
        other = "blue" if team == "red" else "red"
        score = f"{teams[team]}-{teams[other]}"
        won = None if teams[team] == teams[other] else teams[team] > teams[other]
    shots, dmg = s.get("shots", {}), s.get("damage", {})
    rounds = max((teams.get("red") or 0) + (teams.get("blue") or 0), 1)
    total_shots = sum(shots.get(k, 0) for k in ("head", "body", "leg"))
    return {
        "started": meta.get("started_at"),
        "map": (meta.get("map") or {}).get("name", "?"),
        "agent": (s.get("character") or {}).get("name", "?"),
        "agent_id": str((s.get("character") or {}).get("id") or (s.get("character") or {}).get("uuid") or "").lower(),
        "k": s.get("kills", 0),
        "d": s.get("deaths", 0),
        "a": s.get("assists", 0),
        "won": won,
        "score": score,
        "acs": round(s.get("score", 0) / rounds),
        "adr": round(dmg.get("made", 0) / rounds),
        "hs": round(100 * shots.get("head", 0) / total_shots) if total_shots else 0,
    }


def load_accounts():
    return read_json(ACCOUNTS_FILE, {})


def save_accounts(accounts):
    write_json(ACCOUNTS_FILE, accounts, indent=1)


def load_play_sessions():
    return read_json(PLAY_SESSIONS_FILE, {})


def save_play_sessions(sessions):
    write_json(PLAY_SESSIONS_FILE, sessions, indent=1)


def load_matchfeeds():
    return read_json(MATCHFEEDS_FILE, {})


def save_matchfeeds(feeds):
    write_json(MATCHFEEDS_FILE, feeds, indent=1)


def save_matchfeed_progress(snapshot, guild_key):
    """Merge a poller's `seen` update without replacing newer admin settings."""
    updated = snapshot.get(guild_key)
    latest = load_matchfeeds()
    current = latest.get(guild_key)
    if not updated or not current:
        return False
    if (current.get("channel_id"), current.get("started_at")) != (
        updated.get("channel_id"), updated.get("started_at")
    ):
        return False
    current["seen"] = list(updated.get("seen", []))
    save_matchfeeds(latest)
    return True


def load_rank_states():
    return read_json(RANKSTATES_FILE, {})


def save_rank_states(states):
    write_json(RANKSTATES_FILE, states, indent=1)


@tree.command(name="play", description="Start or stop tracking a Valorant queue session")
@app_commands.describe(action="Start tracking or stop your active session", players="Optional queued players, as @mentions")
@app_commands.choices(action=[app_commands.Choice(name="Start", value="start"), app_commands.Choice(name="Stop", value="stop")])
async def play_cmd(interaction: discord.Interaction, action: app_commands.Choice[str], players: str = ""):
    key = str(interaction.channel_id)
    sessions = load_play_sessions()
    if action.value == "stop":
        existing = sessions.get(key)
        if not existing:
            return await interaction.response.send_message("There is no active play session in this channel.", ephemeral=True)
        sessions.pop(key, None)
        save_play_sessions(sessions)
        return await interaction.response.send_message("Play session stopped.")

    accounts = load_accounts()
    ids = [str(interaction.user.id)]
    ids.extend(re.findall(r"<@!?(\d+)>|\b(\d{15,20})\b", players))
    ids = list(dict.fromkeys(uid for item in ids for uid in (item if isinstance(item, tuple) else (item,)) if uid))
    missing = [uid for uid in ids if uid not in accounts]
    if missing:
        mentions = ", ".join(f"<@{uid}>" for uid in missing)
        return await interaction.response.send_message(f"Link an account with /register first for: {mentions}", ephemeral=True,
                                                       allowed_mentions=discord.AllowedMentions.none())
    sessions[key] = {"channel_id": interaction.channel_id, "guild_id": interaction.guild_id,
                     "owner_id": interaction.user.id, "players": ids, "seen": [],
                     "started_at": datetime.now(timezone.utc).isoformat()}
    save_play_sessions(sessions)
    tags = " ".join(f"<@{uid}>" for uid in ids)
    await interaction.response.send_message(f"Play session started for {tags}. I’ll check for completed competitive matches every 5 minutes.",
                                            allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="matchfeed", description="Set this server's automatic Valorant match and rank report channel")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(channel="Channel for match reports; leave blank to disable")
async def matchfeed_cmd(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not interaction.guild or not interaction.permissions.administrator:
        return await interaction.response.send_message("Only server administrators can configure the match feed.", ephemeral=True)
    feeds = load_matchfeeds()
    guild_key = str(interaction.guild_id)
    existing = feeds.get(guild_key) or {}
    if channel is None:
        feeds.pop(guild_key, None)
        save_matchfeeds(feeds)
        return await interaction.response.send_message("Automatic match reports disabled for this server.", ephemeral=True)
    if int(existing.get("channel_id", 0)) != channel.id:
        feeds[guild_key] = {
            "channel_id": channel.id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "seen": [],
        }
    else:
        existing["channel_id"] = channel.id
        existing.setdefault("started_at", datetime.now(timezone.utc).isoformat())
        existing.setdefault("seen", [])
        feeds[guild_key] = existing
    save_matchfeeds(feeds)
    await interaction.response.send_message(
        f"Automatic match reports will post in {channel.mention} for each new competitive match involving a registered account. Same-team players are combined into one post, and rank changes are announced there too.",
        ephemeral=True,
    )


async def play_session_loop():
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            sessions = load_play_sessions()
            accounts = load_accounts()
            matchfeeds = load_matchfeeds()
        except JsonStoreError as exc:
            print(f"Play session state is unavailable: {exc}")
            await asyncio.sleep(300)
            continue
        session_identities = {
            key: (session.get("owner_id"), session.get("started_at"), tuple(session.get("players", [])))
            for key, session in sessions.items()
        }
        dirty = False
        for key, session in list(sessions.items()):
            try:
                if str(session.get("guild_id")) in matchfeeds:
                    continue  # the global match feed owns auto-posts for this guild
                candidates = {}
                for uid in session.get("players", []):
                    account = accounts.get(str(uid))
                    if not account:
                        continue
                    region = account.get("region") or AFFINITY
                    data = await cached_api(f"/valorant/v1/stored-matches/{region}/{quote(account['name'])}/{quote(account['tag'])}",
                                            240, mode="competitive", size=10)
                    for item in data or []:
                        mid = (item.get("meta") or {}).get("id")
                        if mid:
                            candidates.setdefault(mid, {"item": item, "region": region})
                seen = set(session.get("seen", []))
                fresh = []
                for mid, candidate in candidates.items():
                    if mid in seen:
                        continue
                    started = parse_time((candidate["item"].get("meta") or {}).get("started_at"))
                    since = parse_time(session.get("started_at"))
                    seen.add(mid)
                    if started and since and started >= since:
                        fresh.append((mid, started, candidate["region"]))
                session["seen"] = list(seen)[-100:]
                session["started_at"] = session.get("started_at") or datetime.now(timezone.utc).isoformat()
                dirty = True
                channel = client.get_channel(int(session["channel_id"])) or await client.fetch_channel(int(session["channel_id"]))
                for mid, _, region in sorted(fresh, key=lambda item: item[1]):
                    detail = await cached(f"matchdetail:{mid}", 30 * 86400,
                                          lambda mid=mid, region=region: match_detail(mid, region))
                    queue_ids = set(session.get("players", []))
                    participants = detail.get("players", [])
                    # Only announce matches where at least one tracked account played.
                    acc_names = {f"{accounts[uid]['name']}#{accounts[uid]['tag']}".casefold() for uid in queue_ids if uid in accounts}
                    ours = [p for p in participants if f"{p.get('name','')}#{p.get('tag','')}".casefold() in acc_names]
                    if not ours:
                        continue
                    team = ours[0].get("team_id")
                    teammates = [p for p in participants if p.get("team_id") == team]
                    card = await play_match_image(detail, teammates, teammates, ours[0])
                    await channel.send(file=card, allowed_mentions=discord.AllowedMentions.none())
            except Exception as exc:
                print(f"Play session poll failed for channel {key}: {exc}")
        if dirty:
            try:
                latest = load_play_sessions()
                for key, updated in sessions.items():
                    current = latest.get(key)
                    if not current:
                        continue
                    identity = (current.get("owner_id"), current.get("started_at"), tuple(current.get("players", [])))
                    if identity == session_identities.get(key):
                        current["seen"] = list(updated.get("seen", []))
                        current["started_at"] = updated.get("started_at")
                save_play_sessions(latest)
            except JsonStoreError as exc:
                print(f"Could not save play session state: {exc}")
        await asyncio.sleep(300)


async def matchfeed_loop():
    """Poll linked accounts and post once per match when registered players appear."""
    await client.wait_until_ready()
    loop = asyncio.get_running_loop()
    while not client.is_closed():
        cycle_started = loop.time()
        try:
            feeds = load_matchfeeds()
            accounts = load_accounts()
            rank_states = load_rank_states()
        except JsonStoreError as exc:
            print(f"Match feed state is unavailable: {exc}")
            await asyncio.sleep(300)
            continue
        rank_state_updates = {}
        rank_changes = []
        try:
            if feeds and accounts:
                linked = {}
                for user_id, account in accounts.items():
                    name, tag = account.get("name"), account.get("tag")
                    if not name or not tag:
                        continue
                    riot_key = f"{name}#{tag}".casefold()
                    entry = linked.setdefault(riot_key, {"account": account, "user_ids": set()})
                    entry["user_ids"].add(str(user_id))

                candidates = {}
                request_count = 0
                for riot_key, entry in linked.items():
                    account = entry["account"]
                    region = account.get("region") or AFFINITY
                    if request_count:
                        await asyncio.sleep(2.3)  # pace all Riot API calls below the basic rate limit
                    request_count += 1

                    try:
                        rank_data = await cached_api(
                            f"/valorant/v3/mmr/{region}/pc/{quote(account['name'])}/{quote(account['tag'])}",
                            240,
                        )
                        current = (rank_data.get("current") or {}) if isinstance(rank_data, dict) else {}
                        tier = current.get("tier") or {}
                        if not tier:
                            raise ValueError("Rank response did not include a current tier")
                        tier_id = int(tier.get("id") or 0)
                        tier_name = tier.get("name") or "Unrated"
                        previous = rank_states.get(riot_key)
                        if previous is not None and int(previous.get("tier_id") or 0) != tier_id:
                            rank_changes.append({
                                "riot_key": riot_key,
                                "user_ids": sorted(entry["user_ids"]),
                                "name": account["name"],
                                "from_id": int(previous.get("tier_id") or 0),
                                "from_name": previous.get("tier_name") or "Unrated",
                                "to_id": tier_id,
                                "to_name": tier_name,
                                "direction": "up" if tier_id > int(previous.get("tier_id") or 0) else "down",
                                "from_rr": previous.get("rr"),
                                "to_rr": current.get("rr"),
                                "rr_change": current.get("last_change"),
                            })
                        updated_rank = {
                            "tier_id": tier_id, "tier_name": tier_name, "rr": current.get("rr")
                        }
                        if updated_rank != previous:
                            rank_state_updates[riot_key] = updated_rank
                    except Exception as exc:
                        print(f"Match feed rank check failed for {account['name']}#{account['tag']}: {exc}")

                    if request_count:
                        await asyncio.sleep(2.3)
                    request_count += 1
                    try:
                        history = await cached_api(
                            f"/valorant/v1/stored-matches/{region}/{quote(account['name'])}/{quote(account['tag'])}",
                            240, mode="competitive", size=10,
                        )
                    except Exception as exc:
                        print(f"Match feed history failed for {account['name']}#{account['tag']}: {exc}")
                        history = []
                    for item in history or []:
                        meta = item.get("meta") or {}
                        match_id = meta.get("id")
                        started = parse_time(meta.get("started_at"))
                        if not match_id or not started:
                            continue
                        candidate = candidates.setdefault(match_id, {
                            "started": started, "region": region, "account_keys": set(),
                        })
                        candidate["account_keys"].add(riot_key)

                member_cache = {}

                async def guild_member(guild, user_id):
                    cache_key = (guild.id, user_id)
                    if cache_key in member_cache:
                        return member_cache[cache_key]
                    member = guild.get_member(int(user_id))
                    if member is None:
                        try:
                            member = await guild.fetch_member(int(user_id))
                        except discord.NotFound:
                            member = None
                        except discord.HTTPException as exc:
                            print(f"Could not resolve match feed member {user_id} in {guild.id}: {exc}")
                            member = None
                    member_cache[cache_key] = member
                    return member

                for match_id, candidate in candidates.items():
                    eligible_feeds = []
                    for guild_key, feed in feeds.items():
                        seen = {str(mid) for mid in feed.get("seen", [])}
                        since = parse_time(feed.get("started_at"))
                        if str(match_id) not in seen and since and candidate["started"] >= since:
                            eligible_feeds.append((guild_key, feed))
                    if not eligible_feeds:
                        continue

                    await asyncio.sleep(2.3)
                    try:
                        detail = await cached(
                            f"matchdetail:{match_id}", 30 * 86400,
                            lambda mid=match_id, region=candidate["region"]: match_detail(mid, region),
                        )
                    except Exception as exc:
                        print(f"Match feed detail failed for {match_id}: {exc}")
                        continue

                    team_groups = group_registered_players(detail.get("players", []), linked)

                    shared_teams = [
                        (team_id, group) for team_id, group in team_groups.items()
                        if group["account_keys"]
                    ]
                    team_records = {str(team.get("team_id")): team for team in detail.get("teams", [])}
                    map_data = (detail.get("metadata") or {}).get("map") or {}
                    map_name = map_data.get("name", "Unknown") if isinstance(map_data, dict) else str(map_data)
                    notify_targets = []
                    for guild_key, feed in eligible_feeds:
                        if not shared_teams:
                            feed["seen"] = (list(feed.get("seen", [])) + [str(match_id)])[-1000:]
                            save_matchfeed_progress(feeds, guild_key)
                            continue
                        guild = client.get_guild(int(guild_key))
                        if guild is None:
                            continue
                        channel = client.get_channel(int(feed.get("channel_id", 0)))
                        if channel is None:
                            try:
                                channel = await client.fetch_channel(int(feed["channel_id"]))
                            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                                print(f"Could not resolve match feed channel for guild {guild_key}: {exc}")
                                continue

                        guild_groups = []
                        for team_id, group in shared_teams:
                            mentions, member_account_keys = [], set()
                            for riot_key in group["account_keys"]:
                                for user_id in linked[riot_key]["user_ids"]:
                                    member = await guild_member(guild, user_id)
                                    if member:
                                        member_account_keys.add(riot_key)
                                        mentions.append(user_id)
                            if member_account_keys:
                                guild_groups.append((team_id, group, list(dict.fromkeys(mentions)), member_account_keys))
                        if not guild_groups:
                            feed["seen"] = (list(feed.get("seen", [])) + [str(match_id)])[-1000:]
                            save_matchfeed_progress(feeds, guild_key)
                            continue
                        guild_groups.sort(key=lambda item: (
                            len(item[1]["account_keys"]),
                            bool(team_records.get(item[0], {}).get("won")),
                        ), reverse=True)
                        team_id, group, mentions, member_account_keys = guild_groups[0]
                        team_record = team_records.get(team_id, {})
                        rounds = team_record.get("rounds") or {}
                        score = f"{rounds.get('won', 0)}-{rounds.get('lost', 0)}"
                        result_word = "won" if team_record.get("won") else "lost"
                        all_team_players = [p for p in detail.get("players", []) if str(p.get("team_id")) == team_id]
                        registered_players = [
                            p for p in group["players"]
                            if f"{p.get('name', '')}#{p.get('tag', '')}".casefold() in member_account_keys
                        ]
                        notify_targets.append((guild_key, feed, channel, mentions, team_id,
                                               registered_players, all_team_players, result_word, score))

                    if notify_targets:
                        cards_by_roster = {}
                        for guild_key, feed, channel, mentions, team_id, registered_players, all_team_players, result_word, score in notify_targets:
                            try:
                                roster_key = (team_id, tuple(p.get("puuid") for p in registered_players))
                                if roster_key not in cards_by_roster:
                                    card = await play_match_image(detail, registered_players, all_team_players,
                                                                  registered_players[0], summary_players=registered_players)
                                    cards_by_roster[roster_key] = card.fp.getvalue()
                                await channel.send(
                                    content=format_match_notification(
                                        mentions, result_word, score, map_name
                                    ),
                                    file=discord.File(io.BytesIO(cards_by_roster[roster_key]), filename="match.png"),
                                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                                )
                            except discord.HTTPException as exc:
                                print(f"Could not post match feed {match_id} in guild {guild_key}: {exc}")
                                continue
                            feed["seen"] = (list(feed.get("seen", [])) + [str(match_id)])[-1000:]
                            save_matchfeed_progress(feeds, guild_key)

                for event in rank_changes:
                    try:
                        card = await rank_change_image(
                            event["name"], event["from_id"], event["from_name"],
                            event["to_id"], event["to_name"], event["direction"],
                            event["from_rr"], event["to_rr"], event["rr_change"],
                        )
                        image_bytes = card.fp.getvalue()
                    except Exception as exc:
                        print(f"Could not render rank change card for {event['name']}: {exc}")
                        continue
                    for guild_key, feed in feeds.items():
                        guild = client.get_guild(int(guild_key))
                        if guild is None:
                            continue
                        matching_users = []
                        for user_id in event["user_ids"]:
                            if await guild_member(guild, user_id):
                                matching_users.append(user_id)
                        if not matching_users:
                            continue
                        channel = client.get_channel(int(feed.get("channel_id", 0)))
                        if channel is None:
                            try:
                                channel = await client.fetch_channel(int(feed["channel_id"]))
                            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                                print(f"Could not resolve rank feed channel for guild {guild_key}: {exc}")
                                continue
                        direction_text = "ranked up" if event["direction"] == "up" else "deranked"
                        mention_text = " ".join(f"<@{user_id}>" for user_id in matching_users)
                        try:
                            await channel.send(
                                content=f"{mention_text} {direction_text}!",
                                file=discord.File(io.BytesIO(image_bytes), filename="rank-change.png"),
                                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                            )
                        except discord.HTTPException as exc:
                            print(f"Could not post rank change for {event['name']} in guild {guild_key}: {exc}")
                if rank_state_updates:
                    rank_states.update(rank_state_updates)
                    save_rank_states(rank_states)
        except Exception as exc:
            print(f"Match feed poll failed: {exc}")
        await asyncio.sleep(max(1, 300 - (loop.time() - cycle_started)))


async def play_match_image(detail, queued_players, all_teammates, representative, summary_players=None):
    map_data = (detail.get("metadata", {}).get("map") or {})
    map_uuid = map_data.get("id") or map_data.get("uuid")
    map_name = map_data.get("name", "Unknown")
    path = await map_background_path(map_uuid, map_name) if map_uuid else None
    raw_agents = [p.get("agent") or p.get("character") or {} for p in queued_players]
    needs_catalog = any(
        not ((agent.get("id") or agent.get("uuid")) if isinstance(agent, dict) else "")
        for agent in raw_agents
    )
    catalog = {}
    if needs_catalog:
        try:
            catalog = await icon_maps()
        except Exception as exc:
            print(f"Agent catalog unavailable; loading by player agent IDs: {exc}")
    ids_by_name = {name.casefold(): agent_id for agent_id, name in catalog.get("agent_names", {}).items()}
    icon_jobs = {}
    agent_names_by_puuid = {}
    for player in queued_players:
        agent_obj = player.get("agent") or player.get("character") or {}
        if isinstance(agent_obj, dict):
            agent_name = agent_obj.get("name") or agent_obj.get("display_name") or agent_obj.get("displayName") or ""
            raw_agent_id = agent_obj.get("id") or agent_obj.get("uuid") or ""
        else:
            agent_name, raw_agent_id = str(agent_obj), ""
        agent_id = str(raw_agent_id).lower()
        if agent_id not in catalog.get("agents", {}):
            agent_id = ids_by_name.get(agent_name.casefold(), agent_id)
        direct_icon_url = f"https://media.valorant-api.com/agents/{agent_id}/displayicon.png" if agent_id else None
        icon_urls = ([
            direct_icon_url,
            f"https://media.valorant-api.com/agents/{agent_id}/bustportrait.png",
            f"https://media.valorant-api.com/agents/{agent_id}/killfeedportrait.png",
        ] if direct_icon_url else [])
        icon_urls.extend(catalog.get("agent_icon_urls", {}).get(agent_id) or [catalog.get("agents", {}).get(agent_id)])
        icon_urls = list(dict.fromkeys(url for url in icon_urls if url))
        puuid = player.get("puuid")
        if icon_urls and puuid:
            agent_names_by_puuid[puuid] = agent_name
            async def fetch_agent_icon(urls=icon_urls):
                for url in urls:
                    image = await icon_img(url, 96)
                    if image is not None:
                        return image
                return None
            icon_jobs[puuid] = fetch_agent_icon()
    loaded = await asyncio.gather(*icon_jobs.values()) if icon_jobs else []
    icons_by_puuid = dict(zip(icon_jobs, loaded))
    missing = [agent_names_by_puuid[puuid] for puuid, icon in icons_by_puuid.items() if icon is None]
    if missing:
        print("Agent art unavailable for match card: " + ", ".join(missing))
    # Keep rendering input local; API response objects remain unchanged.
    render_players = [dict(p, _play_icon=icons_by_puuid.get(p.get("puuid"))) for p in queued_players]
    loop = asyncio.get_running_loop()
    focus = summary_players if summary_players is not None else all_teammates
    recap = await generate_recap(client.http_session, SETTINGS.groq_api_key, SETTINGS.groq_model,
                                 detail, focus, representative, registered_only=summary_players is not None)
    buffer = await loop.run_in_executor(None, draw_play_result, detail, render_players, all_teammates,
                                        representative, path, recap, summary_players)
    return discord.File(buffer, filename="match.png")


async def rank_change_image(player_name, from_id, from_name, to_id, to_name, direction,
                            from_rr=None, to_rr=None, rr_change=None):
    """Build a rank-change card in the same dark, coral-accented style as match cards."""
    try:
        tier_icons = (await icon_maps()).get("tiers", {})
    except Exception as exc:
        print(f"Rank icon catalog unavailable: {exc}")
        tier_icons = {}
    old_icon, new_icon = await asyncio.gather(
        icon_img(tier_icons.get(str(from_id)), 132),
        icon_img(tier_icons.get(str(to_id)), 132),
    )
    loop = asyncio.get_running_loop()
    buffer = await loop.run_in_executor(
        None, draw_rank_change_card, player_name, from_name, to_name, direction, old_icon, new_icon,
        from_rr, to_rr, rr_change
    )
    return discord.File(buffer, filename="rank-change.png")


@tree.command(name="testplaycard", description="Preview a sample play-session match card (administrators only)")
@app_commands.default_permissions(administrator=True)
async def test_play_card(interaction: discord.Interaction):
    if not interaction.guild or not interaction.permissions.administrator:
        return await interaction.response.send_message("Only server administrators can use this command.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    now = datetime.now(timezone.utc)
    kevin = {
        "puuid": "test-kevin-puuid", "name": "Kevin", "tag": "TEST", "team_id": "Red",
        "agent": {"id": "dade69b4-4f5a-8528-247b-219e5a1facd6", "name": "Fade"},
        "stats": {"kills": 14, "deaths": 17, "assists": 11, "score": 6963,
                  "headshots": 49, "bodyshots": 106, "legshots": 12},
    }
    spam = {
        "puuid": "test-spam-puuid", "name": "Spam", "tag": "TEST", "team_id": "Red",
        "agent": {"id": "22697a3d-45bf-8dd7-4fec-84a9e28c69d7", "name": "Chamber"},
        "stats": {"kills": 7, "deaths": 18, "assists": 7, "score": 3927,
                  "headshots": 18, "bodyshots": 74, "legshots": 6},
    }
    blue = {
        "puuid": "test-opponent-puuid", "name": "Opponent", "tag": "TEST", "team_id": "Blue",
        "agent": {"id": "707eab51-4836-f488-046a-cda6bf494859", "name": "Viper"},
        "stats": {"kills": 19, "deaths": 10, "assists": 5, "score": 6540,
                  "headshots": 44, "bodyshots": 83, "legshots": 10},
    }
    kills = []
    for round_number in range(1, 22):
        killer = kevin if round_number % 2 else spam
        if round_number == 11:
            killers = [spam, spam, spam, spam, kevin]
        else:
            killers = [killer, blue, kevin if killer is spam else spam]
        for kill_index, player in enumerate(killers):
            victim = blue if player["team_id"] == "Red" else (kevin if kill_index % 2 else spam)
            kills.append({"round": round_number, "time_in_round_in_ms": 1000 + kill_index * 2500,
                          "killer": {"puuid": player["puuid"]}, "victim": {"puuid": victim["puuid"]}})
    detail = {
        "metadata": {"map": {"id": "224b0a95-48b9-f703-1bd8-67aca101a61f", "name": "Abyss"},
                     "started_at": now.isoformat(), "game_length_in_ms": 44 * 60 * 1000},
        "teams": [
            {"team_id": "Red", "won": False, "rounds": {"won": 8, "lost": 13}, "premier_roster": {"name": "Queue"}},
            {"team_id": "Blue", "won": True, "rounds": {"won": 13, "lost": 8}, "premier_roster": {"name": "Opponents"}},
        ],
        "players": [kevin, spam, blue],
        "kills": kills,
    }
    card = await play_match_image(detail, [kevin, spam], [kevin, spam], kevin)
    await interaction.followup.send(file=card, ephemeral=True)


@tree.command(name="testrankcard", description="Preview a rank change image (administrators only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(scenario="Choose a subrank promotion, full-rank promotion, or derank preview")
@app_commands.choices(scenario=[
    app_commands.Choice(name="Subrank promotion: Platinum 1 → Platinum 2", value="subrank"),
    app_commands.Choice(name="Full promotion: Platinum 3 → Diamond 1", value="promotion"),
    app_commands.Choice(name="Derank: Diamond 1 → Platinum 3", value="derank"),
])
async def test_rank_card(interaction: discord.Interaction, scenario: app_commands.Choice[str]):
    if not interaction.guild or not interaction.permissions.administrator:
        return await interaction.response.send_message("Only server administrators can use this command.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    previews = {
        "subrank": (15, "Platinum 1", 16, "Platinum 2", "up"),
        "promotion": (17, "Platinum 3", 18, "Diamond 1", "up"),
        "derank": (18, "Diamond 1", 17, "Platinum 3", "down"),
    }
    from_id, from_name, to_id, to_name, direction = previews[scenario.value]
    from_rr, to_rr, rr_change = (0, 80, -20) if direction == "down" else (90, 15, 25)
    card = await rank_change_image(interaction.user.display_name, from_id, from_name, to_id, to_name,
                                   direction, from_rr, to_rr, rr_change)
    direction_text = "ranked up" if direction == "up" else "deranked"
    await interaction.followup.send(
        content=f"{interaction.user.mention} {direction_text}!", file=card, ephemeral=True
    )


@tree.command(name="recent", description="Show a player's most recent competitive match card")
@app_commands.describe(player="Mention a member with a linked account (defaults to you)")
async def recent_cmd(interaction: discord.Interaction, player: discord.Member = None):
    target = player or interaction.user
    account = load_accounts().get(str(target.id))
    if not account:
        return await interaction.response.send_message(
            f"{getattr(target, 'display_name', target.name)} needs to link a VALORANT account with /register first.",
            ephemeral=True,
        )
    await interaction.response.defer()
    region = account.get("region") or AFFINITY
    try:
        history = await cached_api(
            f"/valorant/v1/stored-matches/{region}/{quote(account['name'])}/{quote(account['tag'])}",
            120, mode="competitive", size=10,
        )
        matches = [m for m in history or [] if (m.get("meta") or {}).get("id")]
        matches.sort(key=lambda m: (m.get("meta") or {}).get("started_at") or "", reverse=True)
        if not matches:
            return await interaction.followup.send(f"No completed competitive matches found for {account['name']}#{account['tag']}.")
        latest = matches[0]
        meta = latest.get("meta") or {}
        match_id = meta["id"]
        detail = await cached(
            f"recentmatch:{region}:{match_id}", None,
            lambda: match_detail(match_id, region),
        )
        player_stats = latest.get("stats") or {}
        puuid = player_stats.get("puuid")
        participants = detail.get("players", [])
        target_player = next((p for p in participants if puuid and p.get("puuid") == puuid), None)
        if not target_player:
            wanted = f"{account['name']}#{account['tag']}".casefold()
            target_player = next((p for p in participants
                                  if f"{p.get('name', '')}#{p.get('tag', '')}".casefold() == wanted), None)
        if not target_player:
            return await interaction.followup.send("I found the match, but couldn't match the linked account to its scoreboard.")
        team_id = target_player.get("team_id")
        teammates = [p for p in participants if p.get("team_id") == team_id]
        card = await play_match_image(detail, teammates, teammates, target_player)
        await interaction.followup.send(file=card, allowed_mentions=discord.AllowedMentions.none())
    except Exception as exc:
        await interaction.followup.send(f"Couldn't load the recent match: {str(exc)[:300]}")


def split_riot_id(text):
    name, sep, tag = (text or "").strip().rpartition("#")
    return (name.strip(), tag.strip()) if sep and name.strip() and tag.strip() else None


def resolve_player(caller, member, name, tag, region):
    """Return (name, tag, region) from typed input, a selected Discord member, or the caller's registered account."""
    if name and not tag and "#" in name:
        parts = split_riot_id(name)
        if parts:
            name, tag = parts
    if name and tag:
        return name, tag, region
    if name or tag:
        return None
    acc = load_accounts().get(str((member or caller).id))
    if not acc:
        return None
    return acc["name"], acc["tag"], region or acc.get("region", "")


NO_PLAYER = "Give a name and tag (or Name#TAG), mention a member who has registered, or use /register to link your own account."


@tree.command(name="register", description="Link your Valorant account to your Discord user")
@app_commands.describe(riot_id="Your Riot ID, like Name#TAG", region="eu, na, ap, kr, latam or br (default: your team's)")
async def register(interaction: discord.Interaction, riot_id: str, region: str = ""):
    await register_account(interaction, interaction.user, riot_id, region)


@tree.command(name="registeruser", description="Link a Valorant account to another server member (administrators only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(member="Server member to register", riot_id="Riot ID, like Name#TAG", region="eu, na, ap, kr, latam or br (default: your team's)")
async def register_user(interaction: discord.Interaction, member: discord.Member, riot_id: str, region: str = ""):
    if not interaction.guild or not interaction.permissions.administrator:
        return await interaction.response.send_message("Only server administrators can use this command.", ephemeral=True)
    await register_account(interaction, member, riot_id, region)


async def register_account(interaction, target, riot_id, region):
    await interaction.response.defer(ephemeral=True)
    parts = split_riot_id(riot_id)
    if not parts:
        return await interaction.followup.send("Enter your Riot ID as Name#TAG, including the #.")
    name, tag = parts
    reg = region.lower() if region.lower() in REGIONS else AFFINITY
    data = None
    try:
        data = await cached_api(f"/valorant/v3/mmr/{reg}/pc/{quote(name)}/{quote(tag)}", 300)
    except Exception as e:
        if "404" in str(e):
            return await interaction.followup.send(f"Couldn't find {name}#{tag} in {reg}. Check the spelling and region.")
        # anything else (rate limit, API hiccup): save it anyway
    accounts = load_accounts()
    accounts[str(target.id)] = {"name": name, "tag": tag, "region": reg}
    save_accounts(accounts)
    tier = ((data or {}).get("current") or {}).get("tier") or {} if isinstance(data, dict) else {}
    is_self = target.id == interaction.user.id
    msg = f"Registered **{name}#{tag}** ({reg})"
    msg += "." if is_self else f" for {target.display_name}."
    if tier.get("name"):
        msg += f" Current rank: {tier['name']}."
    msg += ("\nNow /rank and /player work without typing your name." if is_self
            else "\nThey can now use /rank and /player without typing their name.")
    await interaction.followup.send(msg)


@tree.command(name="unregister", description="Remove your linked Valorant account")
async def unregister(interaction: discord.Interaction):
    accounts = load_accounts()
    if accounts.pop(str(interaction.user.id), None) is None:
        return await interaction.response.send_message("You don't have an account registered.", ephemeral=True)
    save_accounts(accounts)
    await interaction.response.send_message("Your account was unlinked.", ephemeral=True)


@tree.command(name="accounts", description="Show registered Valorant accounts")
async def accounts_cmd(interaction: discord.Interaction):
    accounts = load_accounts()
    if not accounts:
        return await interaction.response.send_message("Nobody has registered yet. Use /register.")
    lines = [f"<@{uid}> \u2192 {a['name']}#{a['tag']} ({a.get('region', '')})" for uid, a in accounts.items()]
    await interaction.response.send_message(
        "\n".join(lines)[:1900], allowed_mentions=discord.AllowedMentions.none()
    )


@tree.command(name="rank", description="Current and peak rank for a Riot ID, you, or a member with a linked account")
@app_commands.describe(name="Riot name, or Name#TAG", tag="Tag (after the #)", region="eu, na, ap, kr, latam or br", member="Select or @mention a member; uses their /register account")
async def rank_cmd(interaction: discord.Interaction, name: str = "", tag: str = "", region: str = "", member: discord.Member = None):
    await interaction.response.defer()
    if member and not load_accounts().get(str(member.id)):
        return await interaction.followup.send(f"{member.display_name} has not linked a Valorant account yet. They can use /register first.")
    who = resolve_player(interaction.user, member, name, tag, region)
    if not who:
        return await interaction.followup.send(NO_PLAYER)
    name, tag, region = who
    reg = region.lower() if region.lower() in REGIONS else AFFINITY
    try:
        d = await cached_api(f"/valorant/v3/mmr/{reg}/pc/{quote(name)}/{quote(tag)}", 300)
    except Exception as e:
        return await interaction.followup.send(f"Couldn't fetch rank: {str(e)[:300]}")
    cur, peak = d.get("current") or {}, d.get("peak") or {}
    tier = cur.get("tier") or {}
    embed = discord.Embed(title=f"{name}#{tag}", colour=0xFF4655)
    lines = [f"**{tier.get('name') or 'Unrated'}**"]
    if cur.get("rr") is not None:
        lines[0] += f" \u00b7 {cur['rr']} RR"
    if cur.get("last_change") is not None:
        lines.append(f"Last game: {cur['last_change']:+d} RR")
    if cur.get("leaderboard_placement", {}) and (cur["leaderboard_placement"] or {}).get("rank"):
        lines.append(f"Leaderboard #{cur['leaderboard_placement']['rank']}")
    embed.add_field(name="Current", value="\n".join(lines))
    if peak:
        ptier = peak.get("tier") or {}
        season = ((peak.get("season") or {}).get("short") or "").upper()
        embed.add_field(
            name="Peak",
            value=f"**{ptier.get('name', '?')}**" + (f" \u00b7 {peak['rr']} RR" if peak.get("rr") is not None else "") + (f"\n{season}" if season else ""),
        )
    try:
        url = (await icon_maps())["tiers"].get(str(tier.get("id") or 0))
        if url:
            embed.set_thumbnail(url=url)
    except Exception:
        pass
    await interaction.followup.send(embed=embed)


@tree.command(name="player", description="Recent matches and averages for a Riot ID, you, or a member with a linked account")
@app_commands.describe(name="Riot name, or Name#TAG", tag="Tag (after the #)", mode="Game mode (default competitive)", region="eu, na, ap, kr, latam or br", member="Select or @mention a member; uses their /register account")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="Competitive", value="competitive"),
        app_commands.Choice(name="Unrated", value="unrated"),
        app_commands.Choice(name="Premier", value="premier"),
    ]
)
async def player_cmd(interaction: discord.Interaction, name: str = "", tag: str = "", mode: app_commands.Choice[str] = None, region: str = "", member: discord.Member = None):
    await interaction.response.defer()
    if member and not load_accounts().get(str(member.id)):
        return await interaction.followup.send(f"{member.display_name} has not linked a Valorant account yet. They can use /register first.")
    who = resolve_player(interaction.user, member, name, tag, region)
    if not who:
        return await interaction.followup.send(NO_PLAYER)
    name, tag, region = who
    reg = region.lower() if region.lower() in REGIONS else AFFINITY
    mode_value = mode.value if mode else "competitive"
    try:
        data = await cached_api(
            f"/valorant/v1/stored-matches/{reg}/{quote(name)}/{quote(tag)}", 300, mode=mode_value, size=10
        )
    except Exception as e:
        return await interaction.followup.send(f"Couldn't fetch matches: {str(e)[:300]}")
    rows = sorted((parse_stored(m) for m in data or []), key=lambda r: r["started"] or "", reverse=True)
    if not rows:
        return await interaction.followup.send(f"No {mode_value} matches found for {name}#{tag}.")

    try:
        catalog = await icon_maps()
    except Exception as exc:
        print(f"Player history agent catalog unavailable: {exc}")
        catalog = {}
    by_name = {agent_name.casefold(): agent_id for agent_id, agent_name in catalog.get("agent_names", {}).items()}
    icon_urls = {}
    for row in rows:
        key = (row.get("agent_id") or row.get("agent") or "").casefold()
        agent_id = row.get("agent_id") or by_name.get((row.get("agent") or "").casefold())
        url = catalog.get("agents", {}).get(agent_id)
        if not url and agent_id:
            url = f"https://media.valorant-api.com/agents/{agent_id}/displayicon.png"
        if key and url:
            icon_urls[key] = url
    loaded = await asyncio.gather(*(icon_img(url, 68) for url in icon_urls.values()))
    icons = dict(zip(icon_urls, loaded))
    loop = asyncio.get_running_loop()
    image = await loop.run_in_executor(None, render_player_history, name, tag, mode_value, rows, icons)
    await interaction.followup.send(file=discord.File(image, filename="player-history.png"))


@tree.command(name="findteam", description="Find your team ID from a player's Riot ID")
@app_commands.describe(riot_name="Riot name (before the #)", riot_tag="Tag (after the #)")
async def findteam(interaction: discord.Interaction, riot_name: str, riot_tag: str):
    await interaction.response.defer(ephemeral=True)
    try:
        data = await api(f"/valorant/v2/premier/players/{AFFINITY}/{quote(riot_name)}/{quote(riot_tag)}")
        await interaction.followup.send(f"```json\n{json.dumps(data, indent=1)[:1800]}\n```")
    except Exception as e:
        await interaction.followup.send(str(e)[:1800])


def extract_premier_seasons(data):
    """Normalize the Premier seasons endpoint into newest-first {id, name} records."""
    source = data
    if isinstance(source, dict):
        source = source.get("seasons") or source.get("data") or source.get("premier_seasons") or source
    if isinstance(source, dict):
        if source.get("id") or source.get("season_id"):
            source = [source]
        else:
            source = [dict(value, id=value.get("id") or value.get("season_id") or key)
                      for key, value in source.items() if isinstance(value, dict)]
    if not isinstance(source, list):
        return []

    records, seen = [], set()
    for item in source:
        if not isinstance(item, dict):
            continue
        nested = item.get("season") if isinstance(item.get("season"), dict) else {}
        season_id = item.get("season_id") or item.get("id") or nested.get("id")
        if not season_id or str(season_id) in seen:
            continue
        seen.add(str(season_id))
        name = item.get("name") or item.get("short") or item.get("label") or nested.get("short") or str(season_id)
        records.append({"id": str(season_id), "name": str(name)})
    # HenrikDev returns Premier seasons oldest first; scout from the current season backwards.
    return list(reversed(records))



async def valorant_acts():
    """Return VALORANT act seasons from valorant-api.com, newest act first."""
    async def fetch():
        async with client.http_session.get("https://valorant-api.com/v1/seasons") as response:
            response.raise_for_status()
            body = await response.json()
            return body.get("data", [])
    data = await cached("valorant:acts", 86400, fetch)
    now = datetime.now(timezone.utc)
    acts = []
    for item in data:
        if not (item.get("type") or "").casefold().endswith("act"):
            continue
        start = parse_time(item.get("startTime"))
        end = parse_time(item.get("endTime"))
        if not start or not end:
            continue
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        if start <= now:
            acts.append({
                "id": item.get("uuid") or item.get("id"),
                "name": item.get("displayName") or item.get("name") or "VALORANT Act",
                "start": start,
                "end": end,
            })
    return sorted(acts, key=lambda act: act["start"], reverse=True)


@tree.command(name="scout", description="Scout a Premier team’s win rate and agent picks across VALORANT acts")
@app_commands.describe(
    team_name="Premier team name",
    team_tag="Premier team tag",
    acts="How many recent VALORANT acts to include (default: 1)",
)
async def scout(
    interaction: discord.Interaction,
    team_name: str,
    team_tag: str,
    acts: app_commands.Range[int, 1, 10] = 1,
):
    await interaction.response.defer()
    team_name, team_tag = team_name.strip(), team_tag.strip()
    if not team_name or not team_tag:
        return await interaction.followup.send("Provide both the Premier team name and tag.")

    team_endpoint = f"/valorant/v1/premier/{quote(team_name)}/{quote(team_tag)}"
    team_v2_endpoint = f"/valorant/v2/premier/teams/{AFFINITY}/{quote(team_name)}/{quote(team_tag)}"
    try:
        try:
            team = await cached_api(team_v2_endpoint, 86400)
        except Exception:
            team = await cached_api(team_endpoint, 86400)
        premier_season_data = await cached_api(f"/valorant/v1/premier/seasons/{AFFINITY}", 86400)
        act_list = await valorant_acts()
    except Exception as exc:
        return await interaction.followup.send(f"Couldn't look up **{team_name}#{team_tag}**: {str(exc)[:300]}")

    selected_acts = act_list[:acts]
    if not selected_acts:
        return await interaction.followup.send("Couldn't find current or past VALORANT acts from the VALORANT API.")
    premier_seasons = extract_premier_seasons(premier_season_data)
    if not premier_seasons:
        return await interaction.followup.send("Couldn't read Premier match history seasons from HenrikDev.")
    # VALORANT acts usually overlap one or two Premier seasons; avoid fetching every old season.
    premier_seasons = premier_seasons[:min(len(premier_seasons), max(2, int(acts) * 2))]

    team_id = team.get("id") or team.get("team_id")
    team_members_data = team.get("member") or team.get("members") or []
    member_keys = set()
    for player in team_members_data:
        if player.get("puuid"):
            member_keys.add(player["puuid"].lower())
        if player.get("name"):
            member_keys.add(f"{player['name']}#{player.get('tag', '')}".lower())

    async def season_history(season):
        cache_key = f"scout-history:{AFFINITY}:{team_name.casefold()}#{team_tag.casefold()}:{season['id']}"

        async def fetch_history():
            try:
                data = await api(team_endpoint + "/history", season=season["id"])
                return data.get("league_matches", []) if isinstance(data, dict) else data
            except Exception as exc:
                message = str(exc)
                # Teams often have no index entry in older Premier seasons; cache that as an empty result.
                if "404" in message and "Premier Team not found" in message:
                    return []
                print(f"Scout history lookup failed for season {season['id']}: {message}")
                return []

        return await cached(cache_key, 86400, fetch_history)

    history_lists = await asyncio.gather(*(season_history(season) for season in premier_seasons))
    act_windows = [(act["start"], act["end"]) for act in selected_acts]
    match_records = {}
    for season_matches in history_lists:
        for match in season_matches if isinstance(season_matches, list) else []:
            match_id = match.get("id") or match.get("match_id") or (match.get("meta") or {}).get("id")
            if not match_id:
                continue
            started = parse_time(match.get("started_at") or (match.get("meta") or {}).get("started_at"))
            if started:
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                if not any(start <= started <= end for start, end in act_windows):
                    continue
            match_records.setdefault(str(match_id), match)
    matches = list(match_records.values())
    if not matches:
        act_names = ", ".join(act["name"] for act in selected_acts)
        return await interaction.followup.send(
            f"No Premier matches found for **{team_name}#{team_tag}** during {act_names}."
        )

    detail_gate = asyncio.Semaphore(1)

    async def summarize_match(match):
        match_id = match.get("id") or match.get("match_id") or (match.get("meta") or {}).get("id")
        if not match_id:
            return None
        try:
            cache_key = f"scoutmatch:{match_id}"
            hit = cache_store.peek(cache_key)
            is_fresh = hit and (hit.get("ttl") is None or time.time() - hit["t"] < hit["ttl"])
            if is_fresh:
                detail = await cached(cache_key, None, lambda: match_detail(match_id))
            else:
                async with detail_gate:
                    await asyncio.sleep(1.25)
                    # Completed match details are immutable, so keep them in the disk cache.
                    detail = await cached(cache_key, None, lambda: match_detail(match_id))
            summary = summarise(detail, member_keys, team_id, team_name, team_tag)
            if not summary["rows"]:
                return None
            started = parse_time(summary.get("started"))
            if not started:
                return None
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            if not any(start <= started <= end for start, end in act_windows):
                return None
            return summary
        except Exception as exc:
            print(f"Scout match {match_id} failed: {exc}")
            return None

    summaries = await asyncio.gather(*(summarize_match(match) for match in matches))
    summaries = [item for item in summaries if item]
    if not summaries:
        return await interaction.followup.send(
            f"Found **{team_name}#{team_tag}**, but couldn't match its Premier history to detailed match data in the selected acts."
        )

    maps = {}
    wins = 0
    for summary in summaries:
        won = summary["won"]
        wins += int(won)
        record = maps.setdefault(summary["map"], {"wins": 0, "losses": 0, "agents": {}})
        record["wins" if won else "losses"] += 1
        for row in summary["rows"]:
            agent = row.get("agent") or "Unknown"
            if agent != "?":
                record["agents"][agent] = record["agents"].get(agent, 0) + 1

    total = len(summaries)
    act_names = ", ".join(act["name"] for act in selected_acts)
    embed = discord.Embed(
        title=f"Premier scout · {team_name}#{team_tag}",
        description=(f"**Record:** {wins}W–{total - wins}L · **Win rate:** {wins / total:.0%}\n"
                     f"Based on {total} detailed matches across {len(selected_acts)} VALORANT act(s): {act_names}."),
        colour=0xFF4655,
    )
    for map_name, record in sorted(maps.items(), key=lambda item: item[0].casefold()):
        picks = sorted(record["agents"].items(), key=lambda item: (-item[1], item[0].casefold()))
        pick_text = ", ".join(f"{agent} ×{count}" for agent, count in picks) or "No agent data"
        embed.add_field(
            name=f"{map_name} · {record['wins']}W–{record['losses']}L",
            value=f"Agent picks: {pick_text}"[:1024],
            inline=False,
        )
    embed.set_footer(text="Agent pick counts are pooled across players and matches on each map.")
    await interaction.followup.send(embed=embed)


FALLBACK_AGENTS = [
    "Astra", "Breach", "Brimstone", "Chamber", "Clove", "Cypher", "Deadlock", "Fade", "Gekko", "Harbor",
    "Iso", "Jett", "KAY/O", "Killjoy", "Neon", "Omen", "Phoenix", "Raze", "Reyna", "Sage", "Skye",
    "Sova", "Tejo", "Viper", "Vyse", "Waylay", "Yoru",
]


async def agent_names():
    try:
        maps = await asyncio.wait_for(icon_maps(), timeout=2.5)
        names = sorted(maps.get("agent_names", {}).values())
        if names:
            return names
    except Exception:
        pass
    return FALLBACK_AGENTS


def load_agents():
    return read_json(AGENTS_FILE, {})


def save_agents(data):
    write_json(AGENTS_FILE, data, indent=1)


def matching_map_key(data, map_name):
    normalized = map_name.strip().casefold()
    return next((key for key in data if key.strip().casefold() == normalized), map_name)


def assign_agent(map_name, user_id, agent):
    data = load_agents()
    map_name = matching_map_key(data, map_name)
    picks = data.setdefault(map_name, {})
    user_id = str(user_id)
    for uid, taken in picks.items():
        if uid != user_id and taken.lower() == agent.lower():
            return f"{agent} is already taken on {map_name} by <@{uid}>. Pick another agent.", len(picks)
    picks[user_id] = agent
    save_agents(data)
    return None, len(picks)


def current_week():
    season = load_season()
    if not season:
        return None
    today = datetime.now(timezone.utc).date().isoformat()
    return next((w for w in season["weeks"] if w["end"] >= today), None)


def resolve_map(text):
    """Map name from typed text (matched against season.json), or this week's map."""
    season = load_season()
    if text:
        if not season:
            return text.strip().title()
        return next((w["map"] for w in season["weeks"] if w["map"].lower() == text.strip().lower()), None)
    week = current_week()
    return week["map"] if week else None


NO_MAP = "I couldn't work out the map. Pick one from the list that appears as you type, or check season.json."


@tree.command(name="agent", description="Pick your agent for a Premier map")
@app_commands.describe(agent_name="The agent you'll play", map_name="Map (default: the current or next map)")
async def agent_cmd(interaction: discord.Interaction, agent_name: str, map_name: str = ""):
    agent = next((n for n in await agent_names() if n.lower() == agent_name.strip().lower()), None)
    if not agent:
        return await interaction.response.send_message(
            f"I don't know an agent called {agent_name}. Start typing to see the list.", ephemeral=True
        )
    picked_map = resolve_map(map_name)
    if not picked_map:
        return await interaction.response.send_message(NO_MAP, ephemeral=True)
    conflict, count = assign_agent(picked_map, interaction.user.id, agent)
    if conflict:
        return await interaction.response.send_message(
            conflict, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )
    await interaction.response.send_message(
        f"{interaction.user.mention} will play **{agent}** on **{picked_map}**. ({count} picked, /lineup to see everyone)",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@tree.command(name="assignagent", description="Assign an agent to a player (administrators only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(user="Player to assign", agent_name="Agent to assign", map_name="Map (default: the current or next map)")
async def assignagent(interaction: discord.Interaction, user: discord.Member, agent_name: str, map_name: str = ""):
    if not interaction.permissions.administrator:
        return await interaction.response.send_message("Only server administrators can use this command.", ephemeral=True)
    agent = next((n for n in await agent_names() if n.lower() == agent_name.strip().lower()), None)
    if not agent:
        return await interaction.response.send_message(
            f"I don't know an agent called {agent_name}. Start typing to see the list.", ephemeral=True
        )
    picked_map = resolve_map(map_name)
    if not picked_map:
        return await interaction.response.send_message(NO_MAP, ephemeral=True)
    conflict, count = assign_agent(picked_map, user.id, agent)
    if conflict:
        return await interaction.response.send_message(
            conflict, ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )
    await interaction.response.send_message(
        f"Assigned **{agent}** to {user.mention} on **{picked_map}**. ({count} picked, /lineup to see everyone)",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@agent_cmd.autocomplete("agent_name")
@assignagent.autocomplete("agent_name")
async def agent_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=n, value=n) for n in await agent_names() if current.lower() in n.lower()
    ][:25]


async def lineup_card_file(map_name, week_label, slots):
    agent_icons = {}
    try:
        catalog = await icon_maps()
        uuid_by_name = {name.casefold(): uuid for uuid, name in catalog.get("agent_names", {}).items()}
        icon_jobs = {}
        for slot in slots:
            agent = slot.get("agent")
            if not agent:
                continue
            agent_uuid = uuid_by_name.get(agent.casefold())
            url = catalog.get("agents", {}).get(agent_uuid) if agent_uuid else None
            if url and agent.casefold() not in icon_jobs:
                icon_jobs[agent.casefold()] = icon_img(url, 72)
        loaded_icons = await asyncio.gather(*icon_jobs.values()) if icon_jobs else []
        agent_icons = dict(zip(icon_jobs.keys(), loaded_icons))
    except Exception as exc:
        print(f"Lineup agent icons unavailable: {exc}")

    loop = asyncio.get_running_loop()
    buffer = await loop.run_in_executor(None, render_lineup_card, map_name, week_label, slots, agent_icons)
    return discord.File(buffer, filename="lineup.png")


@tree.command(name="lineup", description="See the team lineup and agent icons for a map")
@app_commands.describe(map_name="Map (default: the current or next map)")
async def lineup(interaction: discord.Interaction, map_name: str = ""):
    picked_map = resolve_map(map_name)
    if not picked_map:
        return await interaction.response.send_message(NO_MAP, ephemeral=True)
    if not interaction.response.is_done():
        await interaction.response.defer()

    agent_data = load_agents()
    picks = agent_data.get(matching_map_key(agent_data, picked_map), {})
    week = next((w for w in (load_season() or {}).get("weeks", []) if w["map"].casefold() == picked_map.casefold()), None)
    week_label = f"{week['label']} · {fmt_range(week['start'], week['end'])}" if week else "Premier lineup"
    slots = []
    lines = []
    for uid, agent in sorted(picks.items(), key=lambda item: str(item[0])):
        try:
            numeric_uid = int(uid)
        except (TypeError, ValueError):
            continue
        player_name = await discord_display_name(interaction.guild, numeric_uid)
        slots.append({"player": player_name, "agent": agent, "riot": ""})
        lines.append(f"<@{uid}> · **{agent}**")
    picked_count = len(slots)
    while len(slots) < 5:
        slots.append({"player": "Open slot", "agent": None, "riot": "Waiting for an agent pick"})
    if not lines:
        lines = ["No agent picks yet. Use /agent to claim a pick."]
    elif picked_count < 5:
        lines.append(f"**{5 - picked_count} pick(s) still open** · use /agent to fill them.")

    lineup_file = await lineup_card_file(picked_map, week_label, slots)
    embed = discord.Embed(
        title=f"Lineup · {picked_map}",
        description=f"{week_label}\n\n" + "\n".join(lines),
        colour=0xFF4655,
    )
    embed.set_image(url="attachment://lineup.png")
    embed.set_footer(text=f"{picked_count}/5 agent picks · Use /agent to pick or change your agent")
    await interaction.followup.send(
        embed=embed,
        file=lineup_file,
        allowed_mentions=discord.AllowedMentions.none(),
    )


playbook_service = PlaybookService(PLAYBOOK_FILE)


async def available_playbook_maps():
    unique = {}
    for name in SETTINGS.competitive_maps:
        if name:
            unique.setdefault(name.casefold(), name)
    return sorted(unique.values(), key=str.casefold)


def find_playbook_map(maps, map_name):
    if not map_name:
        return None
    key = map_name.strip().casefold()
    picked = next((name for name in maps if name.casefold() == key), None)
    if picked:
        return picked
    # Keep already-saved strategies on rotated-out maps accessible by name.
    return next((s["map"] for s in playbook_service.list() if s["map"].casefold() == key), None)


async def playbook_diagram_file(strategy):
    """Render markers on a local top-down map, or the public map catalog icon."""
    image_asset = (strategy.get("diagram") or {}).get("image_asset")
    if image_asset:
        try:
            image_path = playbook_service.image_path(image_asset)
            if image_path.is_file():
                return discord.File(image_path, filename="playbook-map." + image_path.suffix.lstrip("."))
        except (OSError, PlaybookDataError):
            pass
    if not ((strategy.get("diagram") or {}).get("markers") or []):
        return None
    map_name = strategy.get("map", "")
    slug = re.sub(r"[^a-z0-9]+", "-", map_name.casefold()).strip("-")
    local_path = Path(__file__).resolve().parent / "assets" / "playbook_maps" / f"{slug}.png"
    background = local_path if slug and local_path.is_file() else None
    if background is None:
        try:
            catalog = await valorant_map_catalog()
            entry = next((item for item in catalog if str(item.get("displayName", "")).casefold() == map_name.casefold()), {})
            background = await icon_img(entry.get("displayIcon"), 928)
        except Exception as exc:
            print(f"Playbook map image unavailable: {exc}")
    if background is None:
        return None
    loop = asyncio.get_running_loop()
    buffer = await loop.run_in_executor(None, render_playbook_card, map_name, strategy.get("side", ""),
                                        strategy.get("name", ""), background,
                                        (strategy.get("diagram") or {}).get("markers", []))
    return discord.File(buffer, filename="playbook-map.png") if buffer else None


async def playbook_strategy_file(strategy, page_text):
    """Compose strategy text with a local uploaded image or rendered tactical map."""
    diagram_file = await playbook_diagram_file(strategy)
    image_bytes = None
    if diagram_file:
        try:
            image_bytes = await asyncio.to_thread(diagram_file.fp.read)
        finally:
            diagram_file.close()
    loop = asyncio.get_running_loop()
    buffer = await loop.run_in_executor(None, render_strategy_card, strategy, page_text, image_bytes)
    return discord.File(buffer, filename="playbook.png"), bool(image_bytes)


@tree.command(name="playbook", description="Browse strategies by map and side")
@app_commands.describe(map_name="Optional map to open first")
async def playbook(interaction: discord.Interaction, map_name: str = ""):
    await interaction.response.defer()
    try:
        maps = await available_playbook_maps()
        picked = find_playbook_map(maps, map_name)
        if map_name and not picked:
            return await interaction.followup.send("Map not found. Choose a map from the menu.", ephemeral=True)
        view = PlaybookView(playbook_service, maps, interaction.user.id, picked, playbook_strategy_file)
        view.message = await interaction.followup.send(embed=view.embed(), view=view,
                                                       allowed_mentions=discord.AllowedMentions.none(), wait=True)
    except JsonStoreError as exc:
        await interaction.followup.send(f"Playbook unavailable: {exc}", ephemeral=True)


@tree.command(name="createplaybook", description="Create, edit or delete playbook strategies (administrators only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(map_name="Optional map")
async def createplaybook(interaction: discord.Interaction, map_name: str = ""):
    if not interaction.guild or not interaction.permissions.administrator:
        return await interaction.response.send_message("Only server administrators can use this command.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    try:
        maps = await available_playbook_maps()
        picked = find_playbook_map(maps, map_name)
        if map_name and not picked:
            return await interaction.followup.send("Map not found. Choose a map from the menu.", ephemeral=True)
        view = PlaybookAdminView(playbook_service, maps, interaction.user.id, picked)
        view.message = await interaction.followup.send(embed=view.embed(), view=view, ephemeral=True,
                                                       allowed_mentions=discord.AllowedMentions.none(), wait=True)
    except JsonStoreError as exc:
        await interaction.followup.send(f"Playbook unavailable: {exc}", ephemeral=True)


@playbook.autocomplete("map_name")
@createplaybook.autocomplete("map_name")
async def playbook_map_autocomplete(interaction: discord.Interaction, current: str):
    try:
        maps = await available_playbook_maps()
    except JsonStoreError:
        maps = list(SETTINGS.competitive_maps)
    return [app_commands.Choice(name=m[:100], value=m[:100]) for m in maps if current.casefold() in m.casefold()][:25]


@tree.command(name="clearagent", description="Remove your agent pick for a map")
@app_commands.describe(map_name="Map (default: the current or next map)")
async def clearagent(interaction: discord.Interaction, map_name: str = ""):
    picked_map = resolve_map(map_name)
    if not picked_map:
        return await interaction.response.send_message(NO_MAP, ephemeral=True)
    data = load_agents()
    if data.get(picked_map, {}).pop(str(interaction.user.id), None) is None:
        return await interaction.response.send_message(f"You hadn't picked an agent for {picked_map}.", ephemeral=True)
    save_agents(data)
    await interaction.response.send_message(f"Cleared your pick for {picked_map}.", ephemeral=True)


for _cmd in (agent_cmd, assignagent, lineup, clearagent):
    _cmd.autocomplete("map_name")(map_autocomplete)


HELP_SECTIONS = [
    ("Premier", ["schedule", "results", "mapresults", "scout", "agent", "lineup", "clearagent", "playbook"]),
    ("Players", ["register", "unregister", "accounts", "rank", "player", "recent", "play"]),
    ("Server managers", ["registeruser", "assignagent", "createplaybook", "setschedule", "testreminder", "testplaycard", "testrankcard", "clearcache"]),
    ("Troubleshooting", ["findteam", "debug"]),
]


@tree.command(name="help", description="List all commands")
async def help_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    listed = {n for _, names in HELP_SECTIONS for n in names}
    sections = list(HELP_SECTIONS) + [("Other", [c.name for c in tree.get_commands() if c.name not in listed])]
    content = []
    for title, names in sections:
        lines = []
        for n in names:
            cmd = tree.get_command(n)
            if not cmd:
                continue
            params = " ".join(f"<{p.display_name}>" if p.required else f"[{p.display_name}]" for p in cmd.parameters)
            lines.append((f"/{n}{' ' + params if params else ''}", cmd.description))
        if lines:
            content.append((title, lines))
    loop = asyncio.get_running_loop()
    image = await loop.run_in_executor(None, render_help_card, content)
    await interaction.followup.send(file=discord.File(image, filename="help.png"), ephemeral=True)


@tree.command(name="debug", description="Dump raw API data (to check field names)")
@app_commands.choices(
    what=[app_commands.Choice(name="schedule", value="schedule"), app_commands.Choice(name="history", value="history")]
)
async def debug(interaction: discord.Interaction, what: app_commands.Choice[str]):
    await interaction.response.defer(ephemeral=True)
    try:
        data = (
            await cached_api(f"/valorant/v1/premier/seasons/{AFFINITY}", 3600)
            if what.value == "schedule"
            else (await history())[:2]
        )
        await interaction.followup.send(f"```json\n{json.dumps(data, indent=1)[:1800]}\n```")
    except Exception as e:
        await interaction.followup.send(str(e))


async def warm_icon_cache():
    try:
        await icon_maps()
    except Exception as exc:
        print(f"Could not warm icon cache: {exc}")
    else:
        client._icons_warmed = True


@client.event
async def on_ready():
    if not client._commands_synced:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            tree.copy_global_to(guild=guild)
            await tree.sync(guild=guild)
        else:
            await tree.sync()
        client._commands_synced = True
    if not client._icons_warmed and (client._icon_warm_task is None or client._icon_warm_task.done()):
        client._icon_warm_task = asyncio.create_task(warm_icon_cache(), name="warm-icon-cache")
    print(f"Logged in as {client.user}")
    print("Team lookup:", f"ID {TEAM_ID}" if TEAM_ID else f"name {TEAM_NAME}#{TEAM_TAG}")


if __name__ == "__main__":
    client.run(TOKEN)
