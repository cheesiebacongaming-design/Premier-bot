import asyncio
import hashlib
import io
import json
import os
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from urllib.parse import quote, urlsplit

import aiohttp
import discord
from PIL import Image, ImageDraw, ImageFont
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.environ["DISCORD_TOKEN"]
HENRIK_KEY = os.environ["HENRIK_API_KEY"]
TEAM_ID = os.getenv("TEAM_ID")  # optional: roster UUID, more reliable than name/tag
TEAM_NAME = os.getenv("TEAM_NAME", "")
TEAM_TAG = os.getenv("TEAM_TAG", "")
REGIONS = ("eu", "na", "ap", "kr", "latam", "br")
# optional: "name#tag,name#tag" of your players; used to find Premier matches and your side
ROSTER = [r.strip() for r in os.getenv("ROSTER", "").split(",") if r.strip()]
AFFINITY = os.getenv("AFFINITY", "eu").lower()  # eu, na, ap, kr, latam, br
GUILD_ID = os.getenv("GUILD_ID")  # optional: instant slash-command sync in one server
BASE = "https://api.henrikdev.xyz"
X_BEARER = os.getenv("X_BEARER_TOKEN")  # optional: read the schedule tweet automatically
X_USERNAME = os.getenv("X_USERNAME")  # account that posts the Premier schedule (no @)
X_KEYWORD = os.getenv("X_KEYWORD", "premier").lower()  # tweet must contain this
CACHE_FILE = Path("cache.json")
SCHEDULE_FILE = Path("schedule.json")
QUEUE_SCHEDULE_FILE = Path("queue_schedule.json")
SEASON_FILE = Path("season.json")  # edit this each act
PLAYBOOK_FILE = Path("playbooks.json")
ACCOUNTS_FILE = Path("accounts.json")  # Discord user -> Riot ID
AGENTS_FILE = Path("agents.json")  # map -> {Discord user -> agent}
ICON_DIR = Path("icons")
ICON_DIR.mkdir(exist_ok=True)

intents = discord.Intents.default()
class BotClient(discord.Client):
    async def setup_hook(self):
        timeout = aiohttp.ClientTimeout(total=15, connect=5)
        self.http_session = aiohttp.ClientSession(timeout=timeout, connector=aiohttp.TCPConnector(limit=20, ttl_dns_cache=300))
        self.x_session = aiohttp.ClientSession(timeout=timeout)
        self.queue_reminder_task = asyncio.create_task(queue_reminder_loop())

    async def close(self):
        for session in (getattr(self, "http_session", None), getattr(self, "x_session", None)):
            if session and not session.closed:
                await session.close()
        save_task = globals().get("_cache_save_task")
        if save_task and not save_task.done():
            await save_task
        await super().close()


client = BotClient(intents=intents)
tree = app_commands.CommandTree(client)
sem = asyncio.Semaphore(3)  # be gentle with the rate limit
_cache_locks = {}
_icon_locks = {}
_cache_save_task = None
_cache_generation = 0


# ---------- API helpers ----------
async def api(path: str, **params):
    async with sem:
        async with client.http_session.get(
            BASE + path, headers={"Authorization": HENRIK_KEY}, params=params
        ) as r:
            body = await r.json()
            if r.status != 200:
                raise RuntimeError(f"API {r.status}: {body.get('errors') or body}")
            return body["data"]


# ---------- Cache (memory + disk) ----------
try:
    _cache = json.loads(CACHE_FILE.read_text())
except (FileNotFoundError, ValueError):
    _cache = {}



async def _flush_cache():
    await asyncio.sleep(0.3)
    while True:
        generation = _cache_generation
        payload = json.dumps(_cache)
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, CACHE_FILE.write_text, payload)
        if generation == _cache_generation:
            return
        await asyncio.sleep(0.05)


def _schedule_cache_save():
    global _cache_save_task, _cache_generation
    _cache_generation += 1
    if _cache_save_task is None or _cache_save_task.done():
        _cache_save_task = asyncio.create_task(_flush_cache())


async def cached(key, ttl, fn):
    """Return a fresh cached value, else call fn(). ttl=None means keep forever.
    If fn() fails and a stale value exists, the stale value is returned."""
    lock = _cache_locks.setdefault(key, asyncio.Lock())
    async with lock:
        hit = _cache.get(key)
        now = time.time()
        if hit and (hit["ttl"] is None or now - hit["t"] < hit["ttl"]):
            return hit["data"]
        try:
            data = await fn()
        except Exception:
            if hit:
                return hit["data"]
            raise
        _cache[key] = {"t": time.time(), "ttl": ttl, "data": data}
        _schedule_cache_save()
        return data


def cached_api(path, ttl, **params):
    return cached(path + json.dumps(params, sort_keys=True), ttl, lambda: api(path, **params))


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


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def ts(d, style="F"):
    if isinstance(d, str):
        d = parse_time(d)
    return f"<t:{int(d.timestamp())}:{style}>" if d else "unknown"


def team_path():
    if TEAM_ID:
        return f"/valorant/v1/premier/{TEAM_ID}"
    return f"/valorant/v1/premier/{quote(TEAM_NAME)}/{quote(TEAM_TAG)}"


async def team_data():
    """Live roster lookup (v2). Falls back to the v1 index lookup."""
    v2 = (
        f"/valorant/v2/premier/teams/{AFFINITY}/{TEAM_ID}"
        if TEAM_ID
        else f"/valorant/v2/premier/teams/{AFFINITY}/{quote(TEAM_NAME)}/{quote(TEAM_TAG)}"
    )
    try:
        return await cached_api(v2, 3600)
    except Exception:
        return await cached_api(team_path(), 3600)


async def team_members():
    try:
        team = await team_data()
    except Exception:
        if ROSTER:
            return {r.lower() for r in ROSTER}
        raise
    keys = set()
    for m in team.get("member") or team.get("members") or []:
        if m.get("puuid"):
            keys.add(m["puuid"].lower())
        if m.get("name"):
            keys.add(f"{m['name']}#{m.get('tag', '')}".lower())
    return keys


async def history_from_players():
    """Build the match list from the roster's own Premier match histories."""
    if ROSTER:
        found, err = {}, None
        async def fetch_player(entry):
            name, _, tag = entry.rpartition("#")
            return await cached_api(
                f"/valorant/v1/stored-matches/{AFFINITY}/{quote(name.strip())}/{quote(tag.strip())}",
                600, mode="premier", size=20,
            )

        results = await asyncio.gather(*(fetch_player(entry) for entry in ROSTER), return_exceptions=True)
        for items in results:
            if isinstance(items, Exception):
                err = items
                continue
            for item in items or []:
                meta = item.get("meta", {})
                if meta.get("id"):
                    found[meta["id"]] = {"id": meta["id"], "started_at": meta.get("started_at")}
        if not found and err:
            raise err
        return sorted(found.values(), key=lambda m: m["started_at"] or "", reverse=True)
    team = await team_data()
    puuids = [m["puuid"] for m in (team.get("member") or team.get("members") or []) if m.get("puuid")][:5]
    async def fetch_player_matches(puuid):
        return await cached_api(
            f"/valorant/v4/by-puuid/matches/{AFFINITY}/pc/{puuid}", 600, mode="premier", size=10
        )

    results = await asyncio.gather(*(fetch_player_matches(p) for p in puuids), return_exceptions=True)
    seen, last_error = {}, None
    for items in results:
        if isinstance(items, Exception):
            last_error = items
            continue
        for item in items or []:
            meta = item.get("metadata", {})
            mid = meta.get("match_id")
            if not mid:
                continue
            rosters = [(t.get("premier_roster") or {}).get("id") for t in item.get("teams", [])]
            rosters = [r for r in rosters if r]
            if TEAM_ID and rosters and TEAM_ID not in rosters:
                continue  # a different Premier team's match
            seen[mid] = {"id": mid, "started_at": meta.get("started_at")}
    if not seen and last_error:
        raise last_error
    return sorted(seen.values(), key=lambda m: m["started_at"] or "", reverse=True)


async def history():
    try:
        data = await cached_api(team_path() + "/history", 600)
        matches = data.get("league_matches", []) if isinstance(data, dict) else data
        if matches:
            return sorted(matches, key=lambda m: m.get("started_at") or "", reverse=True)
    except Exception:
        pass  # the team isn't in HenrikDev's index, so use the players' histories
    return await history_from_players()


async def match_detail(match_id: str):
    return await api(f"/valorant/v4/match/{AFFINITY}/{match_id}")


# ---------- Match summarising ----------
def build_row(p, total_rounds, fb=0):
    st = p.get("stats", {})
    shots = st.get("headshots", 0) + st.get("bodyshots", 0) + st.get("legshots", 0)
    agent = p.get("agent") or {}
    tier = p.get("tier") or {}
    return {
        "puuid": p.get("puuid", ""),
        "name": p.get("name", "?"),
        "tag": p.get("tag", ""),
        "agent": agent.get("name", "?"),
        "agent_id": (agent.get("id") or "").lower(),
        "tier": tier.get("id", 0),
        "tier_name": tier.get("name", ""),
        "k": st.get("kills", 0),
        "d": st.get("deaths", 0),
        "a": st.get("assists", 0),
        "acs": round(st.get("score", 0) / total_rounds),
        "hs": round(100 * st.get("headshots", 0) / shots) if shots else 0,
        "adr": round((st.get("damage") or {}).get("dealt", 0) / total_rounds),
        "fb": fb,
    }


def summarise(detail: dict, members: set, premier_team_id=None, premier_team_name=None, premier_team_tag=None):
    """Work out which side was ours, the score, and both teams' scoreboards."""
    meta = detail.get("metadata", {})
    players = detail.get("players", [])
    ours = [
        p
        for p in players
        if (p.get("puuid") or "").lower() in members
        or f"{p.get('name', '')}#{p.get('tag', '')}".lower() in members
    ]
    side = None
    if ours:
        side = max({p.get("team_id") for p in ours}, key=lambda t: sum(1 for p in ours if p.get("team_id") == t))
    teams = {t.get("team_id"): t for t in detail.get("teams", [])}
    if side is None and premier_team_id:
        side = next((
            t.get("team_id") for t in detail.get("teams", [])
            if str((t.get("premier_roster") or {}).get("id", "")) == str(premier_team_id)
        ), None)
        if side is not None:
            ours = [p for p in players if p.get("team_id") == side]
    if side is None and premier_team_name:
        target_name = premier_team_name.casefold()
        target_tag = (premier_team_tag or "").casefold()
        side = next((
            t.get("team_id") for t in detail.get("teams", [])
            if (t.get("premier_roster") or {}).get("name", "").casefold() == target_name
            and (not target_tag or not (t.get("premier_roster") or {}).get("tag")
                 or (t.get("premier_roster") or {}).get("tag", "").casefold() == target_tag)
        ), None)
        if side is not None:
            ours = [p for p in players if p.get("team_id") == side]
    mine = teams.get(side, {})
    other_id, theirs = next(((k, t) for k, t in teams.items() if k != side), (None, {}))
    rounds_won = (mine.get("rounds") or {}).get("won", 0)
    rounds_lost = (mine.get("rounds") or {}).get("lost", 0)
    total_rounds = max(rounds_won + rounds_lost, 1)

    # first bloods: the first kill of every round
    by_round = {}
    for k in detail.get("kills") or []:
        by_round.setdefault(k.get("round", 0), []).append(k)
    fbs = {}
    for ks in by_round.values():
        first = min(ks, key=lambda k: k.get("time_in_round_in_ms", 0))
        killer = (first.get("killer") or {}).get("puuid")
        fbs[killer] = fbs.get(killer, 0) + 1

    def rows_for(team_id):
        rows = [build_row(p, total_rounds, fbs.get(p.get("puuid"), 0)) for p in players if p.get("team_id") == team_id]
        return sorted(rows, key=lambda r: r["acs"], reverse=True)

    return {
        "map": (meta.get("map") or {}).get("name", "Unknown"),
        "started": meta.get("started_at"),
        "won": bool(mine.get("won")),
        "score": f"{rounds_won}-{rounds_lost}",
        "rw": rounds_won,
        "rl": rounds_lost,
        "us": (mine.get("premier_roster") or {}).get("name") or TEAM_NAME or "Your team",
        "them": (theirs.get("premier_roster") or {}).get("name") or "Opponent",
        "rows": rows_for(side),
        "enemy_rows": rows_for(other_id),
    }


def player_table(rows):
    lines = ["Player        Agent     K/D/A     ACS ADR  HS%"]
    for r in rows:
        lines.append(
            f"{r['name'][:12]:<13} {r['agent'][:9]:<9} {r['k']}/{r['d']}/{r['a']:<6} "
            f"{r['acs']:<3} {r['adr']:<4} {r['hs']}%"
        )
    return "```\n" + "\n".join(lines) + "\n```"


# ---------- Scoreboard image ----------
FONT_REG = ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"]
FONT_BOLD = ["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"]


def font(size, bold=False):
    for name in FONT_BOLD if bold else FONT_REG:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


async def icon_maps():
    """agent uuid -> icon url, rank tier -> icon url (valorant-api.com, cached a week)."""

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
            "agents": {a["uuid"].lower(): a["displayIcon"] for a in agents},
            "agent_names": {a["uuid"].lower(): a["displayName"] for a in agents},
            "tiers": {str(t["tier"]): t.get("smallIcon") for t in tiers[-1]["tiers"]},
        }

    return await cached("icons:maps2", 7 * 86400, fetch)


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
        return Image.open(path).convert("RGBA").resize((size, size))
    except Exception:
        return None


COLS = [("ACS", 500), ("K", 565), ("D", 610), ("A", 655), ("+/-", 715), ("K/D", 780), ("ADR", 845), ("HS%", 910), ("FB", 965)]
GREY = (150, 162, 175)
WHITE = (240, 244, 248)


def draw_scoreboard(s, icons):
    W, ROW, HEAD, TEAMHEAD, GAP = 1000, 44, 96, 34, 12
    n = len(s["rows"]) + len(s["enemy_rows"])
    H = HEAD + 2 * TEAMHEAD + n * ROW + 2 * GAP + 8
    img = Image.new("RGB", (W, H), (15, 25, 35))
    d = ImageDraw.Draw(img)
    accent = (46, 204, 113) if s["won"] else (231, 76, 60)

    d.rectangle([0, 0, W, 6], fill=accent)
    d.text((24, 48), "VICTORY" if s["won"] else "DEFEAT", font=font(30, True), fill=accent, anchor="lm")
    d.text((W // 2, 46), f"{s['rw']} : {s['rl']}", font=font(42, True), fill=WHITE, anchor="mm")
    started = parse_time(s.get("started"))
    sub = s["map"] + (started.astimezone(timezone.utc).strftime("  \u2022  %d %b %Y %H:%M UTC") if started else "")
    d.text((W - 24, 48), sub, font=font(18), fill=(190, 200, 210), anchor="rm")

    f_name, f_tag, f_val, f_lab = font(17, True), font(13), font(16), font(13, True)
    y = HEAD
    for label, rows, tint in ((s["us"], s["rows"], (52, 152, 219)), (s["them"], s["enemy_rows"], (231, 76, 60))):
        d.rectangle([0, y, W, y + TEAMHEAD], fill=(30, 42, 56))
        d.rectangle([0, y, 5, y + TEAMHEAD], fill=tint)
        d.text((24, y + TEAMHEAD // 2), label, font=font(17, True), fill=WHITE, anchor="lm")
        d.text((425, y + TEAMHEAD // 2), "RANK", font=f_lab, fill=GREY, anchor="mm")
        for name, x in COLS:
            d.text((x, y + TEAMHEAD // 2), name, font=f_lab, fill=GREY, anchor="mm")
        y += TEAMHEAD
        for i, r in enumerate(rows):
            if i % 2 == 0:
                d.rectangle([0, y, W, y + ROW], fill=(20, 32, 45))
            cy = y + ROW // 2
            ic = icons.get(("a", r["agent_id"]))
            if ic:
                img.paste(ic, (24, cy - 18), ic)
            else:
                d.text((42, cy), r["agent"][:3], font=f_lab, fill=GREY, anchor="mm")
            d.text((70, cy), r["name"], font=f_name, fill=WHITE, anchor="lm")
            d.text((70 + d.textlength(r["name"], font=f_name) + 6, cy), f"#{r['tag']}", font=f_tag, fill=GREY, anchor="lm")
            tic = icons.get(("t", r["tier"]))
            if tic:
                img.paste(tic, (425 - 15, cy - 15), tic)
            else:
                d.text((425, cy), "-", font=f_val, fill=GREY, anchor="mm")
            diff = r["k"] - r["d"]
            vals = [r["acs"], r["k"], r["d"], r["a"], f"{diff:+d}", f"{r['k'] / max(r['d'], 1):.2f}", r["adr"], f"{r['hs']}%", r.get("fb", 0)]
            for (name, x), v in zip(COLS, vals):
                colour = (46, 204, 113) if name == "+/-" and diff > 0 else (231, 76, 60) if name == "+/-" and diff < 0 else WHITE
                d.text((x, cy), str(v), font=f_val, fill=colour, anchor="mm")
            y += ROW
        y += GAP

    buf = io.BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return buf


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
def load_manual_schedule():
    try:
        return json.loads(SCHEDULE_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return None


def load_season():
    try:
        return json.loads(SEASON_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return None


def season_maps():
    season = load_season()
    return [w["map"] for w in season["weeks"] if "Playoffs" not in w["map"]] if season else []


def fmt_range(start, end):
    a = datetime.fromisoformat(start)
    b = datetime.fromisoformat(end)
    if a == b:
        return f"{a:%b} {a.day}"
    return f"{a:%b} {a.day}\u2013{b.day}" if a.month == b.month else f"{a:%b} {a.day} \u2013 {b:%b} {b.day}"


@tree.command(name="schedule", description="Premier maps for this act, plus match times")
async def schedule(interaction: discord.Interaction):
    await interaction.response.defer()
    season = load_season()
    embed = discord.Embed(title=f"Premier schedule{' ' + season['stage'] if season else ''}", colour=0xFF4655)

    if season:
        today = datetime.now(timezone.utc).date().isoformat()
        marked = False
        lines = []
        for w in season["weeks"]:
            if w["end"] < today:
                icon = "\u2705"
            elif not marked:
                icon = "\u25b6\ufe0f" if w["start"] <= today else "\u23ed\ufe0f"
                marked = True
            else:
                icon = "\u25ab\ufe0f"
            lines.append(f"{icon} **{w['label']}: {w['map']}** ({fmt_range(w['start'], w['end'])})")
        embed.description = "\n".join(lines)
        if season.get("note"):
            embed.set_footer(text=season["note"])
    else:
        embed.description = "No season.json found."

    # Match times: your /setschedule text, or the latest tweet, whichever is newer
    manual = load_manual_schedule()
    try:
        tweet = await latest_schedule_tweet()
    except Exception:
        tweet = None
    tweet_time = parse_time(tweet["created_at"]) if tweet else None
    manual_time = parse_time(manual["set_at"]) if manual else None
    if manual and (not tweet_time or manual_time > tweet_time):
        embed.add_field(
            name="Match times",
            value=f"{manual['text']}\nSet by {manual['set_by']} {ts(manual_time, 'R')}",
            inline=False,
        )
    elif tweet:
        link = f"https://x.com/{X_USERNAME}/status/{tweet['id']}"
        embed.add_field(name="Latest post", value=f"{tweet['text'][:900]}\n[View on X]({link})", inline=False)

    await interaction.followup.send(embed=embed)


@tree.command(name="setschedule", description="Set Premier times and/or weekly queue reminders")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(
    text="Optional schedule text for /schedule, e.g. Wed 8pm Ascent",
    days="Queue days, comma-separated (e.g. Tuesday,Thursday)",
    local_time="Queue start time in 24-hour HH:MM format",
    timezone_name="IANA time zone (e.g. Europe/London or America/Los_Angeles)",
    channel="Channel for reminders (defaults to this channel)",
)
async def setschedule(
    interaction: discord.Interaction,
    text: str = "",
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
    if not text.strip() and not all(supplied):
        return await interaction.response.send_message(
            "Set reminder days, local_time, and timezone_name, or provide schedule text for /schedule.", ephemeral=True
        )

    now = datetime.now(timezone.utc).isoformat()
    if text.strip():
        SCHEDULE_FILE.write_text(json.dumps({
            "text": text.strip(), "set_by": interaction.user.display_name, "set_at": now,
        }))

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

    parts = []
    if text.strip():
        parts.append("Premier schedule text updated.")
    if all(supplied):
        day_names = ", ".join(weekdays[d] for d in sorted(set(day_values)))
        parts.append(f"Reminders set for {day_names} at {parsed_time.strftime('%H:%M')} {zone.key} in {target.mention}, 30 minutes before queue.")
    await interaction.response.send_message(" ".join(parts), ephemeral=True)


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
    try:
        data = json.loads(QUEUE_SCHEDULE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save_queue_schedules(data):
    QUEUE_SCHEDULE_FILE.write_text(json.dumps(data, indent=2))


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
        configs = load_queue_schedules()
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
            save_queue_schedules(configs)
        await asyncio.sleep(60)


@tree.command(name="clearcache", description="Force fresh data on the next command")
@app_commands.default_permissions(manage_guild=True)
async def clearcache(interaction: discord.Interaction):
    # keep permanent match summaries, drop everything that expires
    for k in [k for k, v in _cache.items() if v["ttl"] is not None]:
        del _cache[k]
    _schedule_cache_save()
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
    try:
        return json.loads(ACCOUNTS_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_accounts(accounts):
    ACCOUNTS_FILE.write_text(json.dumps(accounts, indent=1))


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
    accounts[str(interaction.user.id)] = {"name": name, "tag": tag, "region": reg}
    save_accounts(accounts)
    tier = ((data or {}).get("current") or {}).get("tier") or {} if isinstance(data, dict) else {}
    msg = f"Registered **{name}#{tag}** ({reg})."
    if tier.get("name"):
        msg += f" Current rank: {tier['name']}."
    msg += "\nNow /rank and /player work without typing your name."
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

    wins = sum(r["won"] is True for r in rows)
    losses = sum(r["won"] is False for r in rows)
    kills, deaths = sum(r["k"] for r in rows), sum(r["d"] for r in rows)
    avg = lambda key: round(sum(r[key] for r in rows) / len(rows))
    lines = [
        f"{WIN if r['won'] else LOSS if r['won'] is False else DRAW} **{r['map']}** {r['score']} \u00b7 {r['agent']} \u00b7 "
        f"{r['k']}/{r['d']}/{r['a']} \u00b7 ACS {r['acs']} \u00b7 {ts(r['started'], 'd')}"
        for r in rows
    ]
    embed = discord.Embed(title=f"{name}#{tag} \u00b7 {mode_value}", description="\n".join(lines), colour=0xFF4655)
    embed.set_footer(
        text=f"{wins}W-{losses}L \u00b7 K/D {kills / max(deaths, 1):.2f} \u00b7 ACS {avg('acs')} \u00b7 ADR {avg('adr')} \u00b7 HS {avg('hs')}%"
    )
    await interaction.followup.send(embed=embed)


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
            hit = _cache.get(cache_key)
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
    try:
        return json.loads(AGENTS_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_agents(data):
    AGENTS_FILE.write_text(json.dumps(data, indent=1))


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


def draw_lineup_card(map_name, week_label, slots, agent_icons):
    width, header, row_height = 900, 156, 112
    rows = max(5, len(slots))
    height = header + rows * row_height + 24
    image = Image.new("RGB", (width, height), (15, 25, 35))
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, width, 7], fill=(255, 70, 85))
    draw.text((32, 48), map_name, font=font(34, True), fill=WHITE, anchor="lm")
    draw.text((34, 94), week_label or "Premier lineup", font=font(17), fill=GREY, anchor="lm")
    picked = sum(1 for slot in slots if slot.get("agent"))
    draw.text((width - 32, 70), f"{picked}/5 PICKS", font=font(20, True), fill=WHITE, anchor="rm")

    for index in range(rows):
        y = header + index * row_height
        slot = slots[index] if index < len(slots) else {}
        if index % 2 == 0:
            draw.rounded_rectangle([20, y + 5, width - 20, y + row_height - 5], radius=12, fill=(20, 32, 45))
        agent = slot.get("agent")
        icon = agent_icons.get(agent.casefold()) if agent else None
        if icon:
            image.paste(icon, (42, y + 20), icon)
        else:
            draw.rounded_rectangle([42, y + 20, 114, y + 92], radius=12, fill=(39, 53, 69))
            label = agent[:2].upper() if agent else "—"
            draw.text((78, y + 56), label, font=font(18, True), fill=WHITE, anchor="mm")
        player_name = slot.get("player") or "Open slot"
        draw.text((142, y + 43), player_name[:34], font=font(21, True), fill=WHITE, anchor="lm")
        subtext = slot.get("riot") or (agent if agent else "Waiting for an agent pick")
        draw.text((142, y + 77), subtext[:44], font=font(15), fill=GREY, anchor="lm")
    return image


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
    card = await loop.run_in_executor(None, draw_lineup_card, map_name, week_label, slots, agent_icons)
    buffer = io.BytesIO()
    card.save(buffer, "PNG")
    buffer.seek(0)
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


def load_playbooks():
    try:
        return json.loads(PLAYBOOK_FILE.read_text())
    except (FileNotFoundError, ValueError):
        return {}


def save_playbooks(data):
    PLAYBOOK_FILE.write_text(json.dumps(data, indent=1))


@tree.command(name="playbook", description="Show the notes and links for a map")
@app_commands.describe(map_name="Map to view")
async def playbook(interaction: discord.Interaction, map_name: str):
    picked_map = resolve_map(map_name)
    if not picked_map:
        return await interaction.response.send_message(NO_MAP, ephemeral=True)
    data = load_playbooks()
    entry = data.get(matching_map_key(data, picked_map))
    if not entry:
        return await interaction.response.send_message(f"No playbook has been created for {picked_map} yet.", ephemeral=True)
    embed = discord.Embed(title=f"Playbook: {picked_map}", description=entry.get("notes") or "No notes added.", colour=0xFF4655)
    links = entry.get("links") or []
    if links:
        rendered = "\n".join(f"• <{url}>" for url in links)
        embed.add_field(name="Links", value=rendered[:1024], inline=False)
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


@tree.command(name="createplaybook", description="Create or replace a map playbook (administrators only)")
@app_commands.default_permissions(administrator=True)
@app_commands.describe(map_name="Map", notes="Strategy notes", links="URLs separated by spaces, commas, or new lines")
async def createplaybook(interaction: discord.Interaction, map_name: str, notes: str, links: str = ""):
    if not interaction.permissions.administrator:
        return await interaction.response.send_message("Only server administrators can use this command.", ephemeral=True)
    picked_map = resolve_map(map_name)
    if not picked_map:
        return await interaction.response.send_message(NO_MAP, ephemeral=True)
    if len(notes) > 4000:
        return await interaction.response.send_message("Notes must be 4,000 characters or fewer.", ephemeral=True)
    urls = [part for part in links.replace(",", " ").split() if part]
    invalid = [url for url in urls if urlsplit(url).scheme not in ("http", "https") or not urlsplit(url).netloc]
    if invalid:
        return await interaction.response.send_message("Links must be complete http:// or https:// URLs.", ephemeral=True)
    if len("\n".join(f"• <{url}>" for url in urls)) > 1024:
        return await interaction.response.send_message("The links are too long for one playbook. Shorten or remove some URLs.", ephemeral=True)
    data = load_playbooks()
    stored_map = matching_map_key(data, picked_map)
    data[stored_map] = {"notes": notes.strip(), "links": urls}
    save_playbooks(data)
    await interaction.response.send_message(f"Saved the playbook for **{stored_map}**. Use /playbook to view it.", ephemeral=True)


@playbook.autocomplete("map_name")
@createplaybook.autocomplete("map_name")
async def playbook_map_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=m, value=m) for m in season_maps() if current.lower() in m.lower()][:25]


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
    ("Players", ["register", "unregister", "accounts", "rank", "player"]),
    ("Server managers", ["assignagent", "createplaybook", "setschedule", "testreminder", "clearcache"]),
    ("Troubleshooting", ["findteam", "debug"]),
]


@tree.command(name="help", description="List all commands")
async def help_cmd(interaction: discord.Interaction):
    embed = discord.Embed(title="Premier bot commands", colour=0xFF4655)
    listed = {n for _, names in HELP_SECTIONS for n in names}
    sections = list(HELP_SECTIONS) + [("Other", [c.name for c in tree.get_commands() if c.name not in listed])]
    for title, names in sections:
        lines = []
        for n in names:
            cmd = tree.get_command(n)
            if not cmd:
                continue
            params = " ".join(f"<{p.display_name}>" if p.required else f"[{p.display_name}]" for p in cmd.parameters)
            lines.append(f"`/{n}{' ' + params if params else ''}` {cmd.description}")
        if lines:
            embed.add_field(name=title, value="\n".join(lines)[:1024], inline=False)
    embed.set_footer(text="<required> [optional]")
    await interaction.response.send_message(embed=embed, ephemeral=True)


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


@client.event
async def on_ready():
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        tree.copy_global_to(guild=guild)
        await tree.sync(guild=guild)
    else:
        await tree.sync()
    asyncio.create_task(icon_maps())  # warm the agent/rank icon cache
    print(f"Logged in as {client.user}")
    print("Team lookup:", f"ID {TEAM_ID}" if TEAM_ID else f"name {TEAM_NAME}#{TEAM_TAG}")


if __name__ == "__main__":
    client.run(TOKEN)
