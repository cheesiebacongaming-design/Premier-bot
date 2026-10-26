import asyncio
import hashlib
import io
import json
import os
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

from urllib.parse import quote

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
SEASON_FILE = Path("season.json")  # edit this each act
ACCOUNTS_FILE = Path("accounts.json")  # Discord user -> Riot ID
AGENTS_FILE = Path("agents.json")  # map -> {Discord user -> agent}
ICON_DIR = Path("icons")
ICON_DIR.mkdir(exist_ok=True)

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
sem = asyncio.Semaphore(3)  # be gentle with the rate limit


# ---------- API helpers ----------
async def api(path: str, **params):
    async with sem:
        async with aiohttp.ClientSession() as s:
            async with s.get(
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


def _save():
    CACHE_FILE.write_text(json.dumps(_cache))


async def cached(key, ttl, fn):
    """Return a fresh cached value, else call fn(). ttl=None means keep forever.
    If fn() fails and a stale value exists, the stale value is returned."""
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
    _cache[key] = {"t": now, "ttl": ttl, "data": data}
    _save()
    return data


def cached_api(path, ttl, **params):
    return cached(path + json.dumps(params, sort_keys=True), ttl, lambda: api(path, **params))


async def x_get(path, **params):
    async with aiohttp.ClientSession() as s:
        async with s.get(
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
        for entry in ROSTER:
            name, _, tag = entry.rpartition("#")
            try:
                items = await cached_api(
                    f"/valorant/v1/stored-matches/{AFFINITY}/{quote(name.strip())}/{quote(tag.strip())}",
                    600,
                    mode="premier",
                    size=20,
                )
            except Exception as e:
                err = e
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
    seen, last_error = {}, None
    for puuid in puuids:
        try:
            items = await cached_api(
                f"/valorant/v4/by-puuid/matches/{AFFINITY}/pc/{puuid}", 600, mode="premier", size=10
            )
        except Exception as e:
            last_error = e
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


def summarise(detail: dict, members: set):
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
        async with aiohttp.ClientSession() as s:
            async with s.get("https://valorant-api.com/v1/agents?isPlayableCharacter=true") as r:
                agents = (await r.json())["data"]
            async with s.get("https://valorant-api.com/v1/competitivetiers") as r:
                tiers = (await r.json())["data"]
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
        if not path.exists():
            async with aiohttp.ClientSession() as s:
                async with s.get(url) as r:
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
    return discord.File(draw_scoreboard(s, icons), filename=filename)


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


@tree.command(name="setschedule", description="Paste the maps and times from the Premier tweet")
@app_commands.default_permissions(manage_guild=True)
@app_commands.describe(text="The schedule text, e.g. Wed 8pm Ascent, Thu 8pm Bind")
async def setschedule(interaction: discord.Interaction, text: str):
    SCHEDULE_FILE.write_text(
        json.dumps(
            {
                "text": text,
                "set_by": interaction.user.display_name,
                "set_at": datetime.now(timezone.utc).isoformat(),
            }
        )
    )
    await interaction.response.send_message("Schedule updated. Use /schedule to view it.", ephemeral=True)


@tree.command(name="clearcache", description="Force fresh data on the next command")
@app_commands.default_permissions(manage_guild=True)
async def clearcache(interaction: discord.Interaction):
    # keep permanent match summaries, drop everything that expires
    for k in [k for k, v in _cache.items() if v["ttl"] is not None]:
        del _cache[k]
    _save()
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


def resolve_player(caller, user, name, tag, region):
    """Return (name, tag, region) from typed input, a mentioned member, or the caller's registered account."""
    if name and not tag and "#" in name:
        parts = split_riot_id(name)
        if parts:
            name, tag = parts
    if name and tag:
        return name, tag, region
    if name or tag:
        return None
    acc = load_accounts().get(str((user or caller).id))
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


@tree.command(name="rank", description="Current and peak rank for any player")
@app_commands.describe(name="Riot name, or Name#TAG (leave empty for your registered account)", tag="Tag (after the #)", region="eu, na, ap, kr, latam or br", user="A member who has registered")
async def rank_cmd(interaction: discord.Interaction, name: str = "", tag: str = "", region: str = "", user: discord.Member = None):
    await interaction.response.defer()
    who = resolve_player(interaction.user, user, name, tag, region)
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


@tree.command(name="player", description="Recent matches and averages for any player")
@app_commands.describe(name="Riot name, or Name#TAG (leave empty for your registered account)", tag="Tag (after the #)", mode="Game mode (default competitive)", region="eu, na, ap, kr, latam or br", user="A member who has registered")
@app_commands.choices(
    mode=[
        app_commands.Choice(name="Competitive", value="competitive"),
        app_commands.Choice(name="Unrated", value="unrated"),
        app_commands.Choice(name="Premier", value="premier"),
    ]
)
async def player_cmd(interaction: discord.Interaction, name: str = "", tag: str = "", mode: app_commands.Choice[str] = None, region: str = "", user: discord.Member = None):
    await interaction.response.defer()
    who = resolve_player(interaction.user, user, name, tag, region)
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
    data = load_agents()
    picks = data.setdefault(picked_map, {})
    me = str(interaction.user.id)
    for uid, taken in picks.items():
        if uid != me and taken.lower() == agent.lower():
            return await interaction.response.send_message(
                f"{agent} is already taken on {picked_map} by <@{uid}>. Pick another agent.",
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
    picks[me] = agent
    save_agents(data)
    await interaction.response.send_message(
        f"{interaction.user.mention} will play **{agent}** on **{picked_map}**. ({len(picks)} picked, /lineup to see everyone)",
        allowed_mentions=discord.AllowedMentions.none(),
    )


@agent_cmd.autocomplete("agent_name")
async def agent_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=n, value=n) for n in await agent_names() if current.lower() in n.lower()
    ][:25]


@tree.command(name="lineup", description="See who is playing which agent on a map")
@app_commands.describe(map_name="Map (default: the current or next map)")
async def lineup(interaction: discord.Interaction, map_name: str = ""):
    picked_map = resolve_map(map_name)
    if not picked_map:
        return await interaction.response.send_message(NO_MAP, ephemeral=True)
    picks = load_agents().get(picked_map, {})
    embed = discord.Embed(title=f"Lineup: {picked_map}", colour=0xFF4655)
    season = load_season()
    week = next((w for w in season["weeks"] if w["map"] == picked_map), None) if season else None
    if week:
        embed.description = f"{week['label']} ({fmt_range(week['start'], week['end'])})\n\n"
    else:
        embed.description = ""
    if not picks:
        embed.description += "Nobody has picked an agent yet. Use /agent."
    else:
        accounts = load_accounts()
        lines = []
        for uid, agent in picks.items():
            acc = accounts.get(uid)
            riot = f" ({acc['name']}#{acc['tag']})" if acc else ""
            lines.append(f"<@{uid}>{riot} \u2192 **{agent}**")
        embed.description += "\n".join(lines)
        embed.set_footer(text=f"{len(picks)}/5 picked")
    await interaction.response.send_message(embed=embed, allowed_mentions=discord.AllowedMentions.none())


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


for _cmd in (agent_cmd, lineup, clearagent):
    _cmd.autocomplete("map_name")(map_autocomplete)


HELP_SECTIONS = [
    ("Premier", ["schedule", "results", "mapresults", "agent", "lineup", "clearagent"]),
    ("Players", ["register", "unregister", "accounts", "rank", "player"]),
    ("Server managers", ["setschedule", "clearcache"]),
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
