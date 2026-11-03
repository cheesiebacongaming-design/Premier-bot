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
| `GEMINI_API_KEY` | Optional Google Gemini API key for AI match recaps |
| `GEMINI_MODEL` | Optional recap model; defaults to `gemini-3.5-flash-lite` |
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
sent to Google Gemini. Google may use free-tier content to improve its products.
Free-tier limits vary by model and project; paid-tier usage can incur charges.
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
