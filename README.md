# Premier Bot

Discord bot for VALORANT Premier schedules, match reports, player accounts,
agent lineups, playbooks, play sessions, and automatic match/rank notifications.
Image cards are rendered locally with Pillow.

## Setup

Use Python 3.10 or newer. Run commands from the repository root because JSON
state paths are relative to the working directory.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
# Edit .env with your Discord token and HenrikDev API key.
python bot.py
```

On Windows, activate with `.venv\Scripts\Activate.ps1` and use `Copy-Item
.env.example .env`. Invite the bot with the `bot` and `applications.commands`
scopes and permission to view channels, send messages, embed links, and attach
files. Configure automatic reports with `/matchfeed` in the target server.

## Configuration

| Variable | Purpose |
| --- | --- |
| `DISCORD_TOKEN` | Required Discord bot token |
| `HENRIK_API_KEY` | Required HenrikDev API key |
| `GROQ_API_KEY` | Optional Groq API key for AI match recaps |
| `GROQ_MODEL` | Optional recap model; defaults to `qwen/qwen3.8-27b` |
| `TEAM_ID` | Preferred Premier team identifier |
| `TEAM_NAME`, `TEAM_TAG` | Team lookup when no ID is configured |
| `ROSTER` | Optional comma-separated `Name#Tag` entries for history fallback |
| `AFFINITY` | VALORANT region; defaults to `eu` |
| `GUILD_ID` | Optional server ID for guild command synchronization |
| `X_BEARER_TOKEN`, `X_USERNAME` | Optional X schedule lookup credentials/account |
| `X_KEYWORD` | Schedule search keyword; defaults to `premier` |

Keep `.env` private. The older `env.example` is retained for existing installs.
No Discord or API credentials are required by the unit tests.
When AI recaps are enabled, player names, match statistics, and kill events are
sent to Groq. Free-tier limits vary by model and account; paid-tier usage can incur charges.
If no key is set or a request fails, a local recap is used.

## Deployment on Ubuntu / AWS

Install Python, its venv package, and a fallback font:

```bash
sudo apt-get update
sudo apt-get install python3-venv fonts-dejavu-core
```

Use the setup instructions above, then run the bot under your existing process
manager. For systemd, set `WorkingDirectory` to the checkout and `ExecStart` to
the absolute path of `.venv/bin/python` followed by the absolute path to
`bot.py`. Run only one bot process per state directory to avoid duplicate
polling and conflicting JSON writes.

Before updating, stop the bot and back up `.env`, all local JSON files, and
custom assets outside the checkout. Pull the intended deployment branch,
install `requirements.txt` again, then restart the service. Refactor branches
are development branches; review and merge them before deploying from main.
Never use `git clean` to resolve a deployment conflict: it can delete local
state and downloaded assets.

Accounts, agent selections, playbooks, queue schedules, sessions, matchfeeds,
rank states, and cache data live in local JSON files. Keep existing files in
place; no database migration is required. Corrupt state files raise errors
instead of silently resetting data. Cache writes use atomic replacement.
`season.json` is local schedule data and should be preserved too.

## Interactive playbooks

`/playbook` opens a private map, side, and strategy browser. Choose another map
or side in the same menu; use its page buttons for long lists or strategy text.
Menus expire after ten minutes and can be reopened with `/playbook`.

Administrators can use `/createplaybook` to open a private editor:

1. Choose a map and Attack or Defence. Choose **New strategy**, or select an
   existing strategy and choose **Edit**.
2. Use **Details** for the name, objective, and ordered steps (one per line).
   Use **Assignments** for up to five lines in this format:
   `role | player | agent | position | responsibilities | utility`.
3. Use **Utility & links** for utility notes and one HTTP(S) URL per line.
   **Import lineup** copies the existing agent picks for that map into an empty
   draft; it never changes the saved `/agent` lineup.
4. Use **Diagram** to add a public image URL, marker JSON, or both. Review the
   draft and choose **Save strategy**. Cancel or let the session expire to
   discard unsaved work. Editing uses the same workflow. Deletion requires a
   separate confirmation; stale revisions are rejected instead of overwritten.

The optional `map_name`, `notes`, and `links` command arguments can seed the
editor; they do not save until the draft is confirmed. An image URL is displayed
by Discord and is not downloaded by the bot. Make sure the URL remains valid;
temporary attachment URLs can expire.

### Tactical diagrams

For marker-based diagrams the bot looks for a trusted local top-down image at
`assets/playbook_maps/<map-slug>.png` (for example `assets/playbook_maps/ascent.png`
or `assets/playbook_maps/sunset.png`). The slug is the lowercase map name with
non-alphanumeric characters replaced by hyphens. If no local image exists, the
bot attempts to use the map's top-down `displayIcon` from valorant-api.com and
caches it in `icons/`. If neither is available, the written strategy remains
available. Local images are deployment assets: back them up and copy them to
new hosts. They are not downloaded from arbitrary strategy URLs.

The Diagram form accepts a JSON array. Coordinates are normalized from 0 to 1:
`[0, 0]` is the top left of the displayed source image and `[1, 1]` is the
bottom right, including any transparent padding in that image. Available kinds
are `player`, `smoke`, `utility`, `step`, `site`, and `path`. Paths need at
least two points; all markers include `x` and `y`. For example:

```json
[
  {"kind":"player", "x":0.20, "y":0.70, "label":"1"},
  {"kind":"smoke", "x":0.65, "y":0.35, "label":"Heaven"},
  {"kind":"path", "x":0.20, "y":0.70, "points":[[0.20,0.70],[0.65,0.35]]},
  {"kind":"site", "x":0.78, "y":0.27, "label":"A"}
]
```

Marker input is JSON, not a graphical map editor. Clearing both marker JSON and
the image URL removes the diagram. A rendered marker diagram takes precedence
over the reference image when a map background is available.

### Storage and migration

The store remains `playbooks.json` in the bot's working directory. The current
schema is `{"version": 1, "strategies": [...]}`. Each strategy has an ID,
name, map, side (`attack`, `defence`, or migrated `legacy`), objective, ordered
`steps`, `assignments`, `utility`, `links`, optional `diagram` (`image_url` and
`markers`), creation/update timestamps, and a revision number. Assignments
contain `role`, `player`, `agent`, `starting_position`, `responsibilities`, and
`utility`. Diagram paths and markers use normalized coordinates.

On first playbook use, valid old map-to-notes entries are migrated into the
**Legacy** side without guessing Attack or Defence. Original notes, links, and
other entry fields are retained, and a byte-for-byte
`playbooks.json.legacy-<id>.bak` is created before the atomic replacement.
Malformed or unsupported data stops migration and is left untouched. Keep the
backup along with regular backups of the state file. A lock file coordinates
playbook operations across bot processes that use this service; stop the bot
before editing JSON directly. Do not run an older bot version against the
migrated file: restore a backup first if rolling back.

Downloaded icons live in `icons/`. Optional fonts in `assets/fonts/` are
`Roboto-Regular.ttf`, `Roboto-Bold.ttf`, `Lora-Regular.ttf`, and `Lora-Bold.ttf`.
System fonts are used when these are missing; copy your existing fonts to keep
the same rendering across hosts.

## Development and checks

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
python -m ruff check .
python -m ruff format --check services/matchfeed.py
```

Use `python -m ruff format <file>` when formatting a changed file. Ruff's initial
lint rules focus on correctness; repository-wide formatting is not enforced
yet to avoid mixing a large style rewrite into the modular refactor.
GitHub Actions runs lint, compilation, and pytest on Python 3.10 and 3.12.
Tests cover cache behavior, history deduplication, player grouping, notification
formatting, statistics, JSON persistence, and image rendering. Live Discord
delivery and the long-running polling loops still need integration testing.

## Code layout

- `bot.py`: entry point, Discord runtime, and remaining orchestration.
- `config.py`: environment settings and startup validation.
- `commands/`: command groups.
- `services/`: HTTP API, cache, JSON persistence, history, and matchfeed helpers.
- `utils/`: statistics, formatting, and match summaries.
- `graphics/`: Pillow renderers and shared font/image helpers.
- `tests/`: offline regression tests.
