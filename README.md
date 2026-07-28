# Kleinanzeigen Telegram Bot

An intelligent Telegram bot that automatically creates, manages and optimizes
[Kleinanzeigen](https://www.kleinanzeigen.de) classified listings. Send it photos
of an item and it drafts a title, description and price with an LLM; it also renews
expiring listings and can sync a Discogs record inventory to Kleinanzeigen.

> **Disclaimer.** This project is **not affiliated** with Kleinanzeigen or Discogs.
> Automating kleinanzeigen.de may conflict with its Terms of Service, and the Discogs
> features use the Discogs API under the [Discogs API Terms](https://www.discogs.com/developers).
> Use at your own risk, for personal purposes, and make sure you comply with the terms
> of any service you automate. No warranty of any kind.

## Features

- 📸 **Photo-based listing creation** – send photos, an LLM writes the title, description and estimates a price.
- 💬 **Feedback loop** – review and adjust each draft (title / text / price / free-text feedback) before it goes live.
- 🔄 **Daily listing check** – expiring listings are automatically optimized or proposed for deletion.
- 💿 **Discogs sync** – turn your Discogs record inventory into Kleinanzeigen listings.
- 🔒 **User restriction** – only allow-listed Telegram user IDs may use the bot (the bot refuses to start without one).

## Architecture

```
Telegram (user) <--> Telegram bot (python-telegram-bot)
                         |
                    LangGraph agents
                    +-- Create listing   (photo -> draft)
                    +-- Draft feedback loop
                    +-- Daily listing check (scheduler)
                    +-- Listing optimization / deletion
                    +-- Discogs inventory sync
                         |
                +--------+--------+
          OpenRouter API     kleinanzeigen-bot CLI
          (LLM calls)        (Kleinanzeigen interaction)
```

The bot orchestrates workflows with [LangGraph](https://github.com/langchain-ai/langgraph),
calls LLMs through [OpenRouter](https://openrouter.ai), and drives Kleinanzeigen through the
[kleinanzeigen-bot](https://github.com/Second-Hand-Friends/kleinanzeigen-bot) CLI.

## Prerequisites

- **Python 3.11+**
- **git** – one dependency (`kleinanzeigen-bot`) is installed directly from GitHub.
- **Chromium via Playwright** – `kleinanzeigen-bot` drives a real browser. On first start the
  bot runs `playwright install chromium` automatically, so an internet connection is required
  the first time (the Docker image bundles this).
- A **Telegram bot token** – create one via [@BotFather](https://t.me/BotFather).
- Your **Telegram user ID** – message [@userinfobot](https://t.me/userinfobot) to get it.
- An **OpenRouter API key** – from [openrouter.ai](https://openrouter.ai).
- *(optional, for Discogs sync)* a **Discogs user token** and **username** –
  create a token at *Discogs → Settings → Developers → Generate token*.
- *(optional)* a **LangSmith API key** for tracing – from [smith.langchain.com](https://smith.langchain.com).

## Installation

### Option A – Local (venv)

```bash
git clone <repo-url>
cd kleinanzeigen-telegram-bot

# run.sh creates a .venv, installs dependencies and starts the bot:
./run.sh
```

`run.sh` also supports:

```bash
./run.sh diagnose   # browser diagnostics only (no Telegram polling)
./run.sh cli-test   # invoke `kleinanzeigen-bot publish` directly (debugging)
```

Or install and run manually:

```bash
pip install -e .          # add ".[dev]" for lint/type/test tooling
python -m bot.main
```

> The `kleinanzeigen-bot` dependency is a pinned git reference, so `pip install` needs `git`
> on the host. Because of the direct git dependency, this project is installed from source and
> is not published to PyPI.

### Option B – Docker

```bash
cp .env.example .env      # then fill in your tokens (see Configuration)
# also create kleinanzeigen-config/config.yaml (see Configuration)
docker compose up -d --build
```

The image bundles Chromium, and `docker-entrypoint.sh` starts an Xvfb virtual display so the
browser can run in the container. `.env`, `./kleinanzeigen-config` and `./data` are mounted in.

## Configuration

### 1. Environment variables

```bash
cp .env.example .env
```

Fill in `.env` (see `.env.example` for the full list). Key variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot token from @BotFather |
| `TELEGRAM_ALLOWED_USER_IDS` | ✅ | Comma-separated allow-list of Telegram user IDs |
| `OPENROUTER_API_KEY` | ✅ | OpenRouter API key |
| `KLEINANZEIGEN_CONFIG_PATH` | – | Path to the kleinanzeigen-bot config dir (default `./kleinanzeigen-config`) |
| `DISCOGS_USER_TOKEN` / `DISCOGS_USERNAME` | – | Only needed for `/discogssync` |
| `DAILY_CHECK_HOUR` / `DAILY_CHECK_MINUTE` | – | When the daily check runs (default 06:00) |
| `SHIPPING_EXPIRING_DAYS` / `PICKUP_EXPIRING_DAYS` | – | "Expiring soon" thresholds (default 10 / 30 days) |
| `LANGCHAIN_TRACING_V2` / `LANGCHAIN_API_KEY` / `LANGCHAIN_PROJECT` | – | Optional LangSmith tracing |
| `LOG_LEVEL` / `DATABASE_PATH` | – | Logging level, SQLite path |

> The bot **refuses to start** if `TELEGRAM_ALLOWED_USER_IDS` is empty — this prevents an open
> bot that anyone could use (and spend your OpenRouter credits / post to your account).

### 2. Kleinanzeigen credentials

```bash
cp kleinanzeigen-config/config.example.yaml kleinanzeigen-config/config.yaml
```

Edit `kleinanzeigen-config/config.yaml` with your kleinanzeigen.de login and contact details:

```yaml
login:
  username: "your-email@example.com"
  password: "your-password"
ad_defaults:
  contact:
    name: "Your Name"
    zipcode: "12345"   # required
```

`config.yaml` contains credentials and is **gitignored** — never commit it. The bot manages the
`browser:` section (Chromium path, headless flags) automatically on startup.

## Usage

### Create a new listing

1. Send the bot one or more photos (up to 10) of your item.
2. It analyzes the images and generates a draft (title, description, price).
3. Review the draft and use the inline buttons:
   - **✅ Approve** – publish the listing to Kleinanzeigen
   - **✏️ Title / Text / Price** – edit individual fields
   - **💬 Feedback** – free-text feedback for a full revision
   - **❌ Discard** – delete the draft

### Daily listing check

- Runs automatically each day at the configured time, or trigger it manually with `/check`.
- Listings whose remaining runtime is below the type-specific threshold
  (`SHIPPING_EXPIRING_DAYS` / `PICKUP_EXPIRING_DAYS`) are processed:
  - **price < 5 EUR** → deletion suggested
  - **price ≥ 5 EUR** → an optimized draft is proposed for approval
- Reserved and Discogs-managed listings are excluded from renewal.

### Commands

| Command | Description |
|---------|-------------|
| `/start` | Start the bot |
| `/help` | Show help |
| `/check` | Run the daily listing check manually |
| `/weiter` | Process the next page of expiring listings |
| `/renew <id>` | Renew a single listing by its ID |
| `/discogssync` | Sync your Discogs inventory to Kleinanzeigen |
| `/diagnose` | Browser diagnostics (troubleshooting) |
| `/logs` | Show the last lines of the kleinanzeigen-bot log |

### Discogs sync

With `DISCOGS_USER_TOKEN` and `DISCOGS_USERNAME` set, `/discogssync` reads your Discogs
inventory and creates matching Kleinanzeigen listings, keeping a mapping so the daily check
does not interfere with Discogs-managed listings.

## Observability with LangSmith (optional)

Set `LANGCHAIN_TRACING_V2=true` plus `LANGCHAIN_API_KEY` in `.env` to visualize every LangGraph
node (image analysis, text generation, price estimation, …) in the
[LangSmith](https://smith.langchain.com) dashboard. Without these variables the bot runs normally.

## Configuration reference

### OpenRouter models

Models are mapped per task in [`config/models.py`](config/models.py) and are easy to swap:

| Task | Default model |
|------|---------------|
| Image analysis | `google/gemini-2.5-flash-lite` |
| Text generation | `google/gemini-2.5-flash-lite` |
| Price estimation | `google/gemini-2.5-flash-lite` |
| Optimization | `google/gemini-2.5-flash-lite` |
| Feedback | `google/gemini-2.5-flash-lite` |

### Database

A SQLite database at `DATABASE_PATH` (default `./data/bot.db`) stores drafts, a cache of active
listings, per-draft photo IDs, and Discogs listing mappings.

## Development

```bash
pip install -e ".[dev]"

pytest              # run the test suite
ruff check .        # lint
mypy .              # type-check
```

### Project structure

```
kleinanzeigen-telegram-bot/
├── bot/                     # Telegram handlers, keyboards, entry point (main.py)
│   └── handlers/            # /start, new_listing, feedback, daily_check, discogs_sync, logs, ...
├── agents/                  # LangGraph workflows
│   ├── nodes/               # individual workflow steps
│   ├── listing_creator.py   # create a new listing
│   ├── listing_optimizer.py # optimize an existing listing
│   └── discogs_sync.py      # Discogs inventory sync
├── config/                  # settings.py (pydantic), models.py (OpenRouter mapping)
├── db/                      # SQLite layer (aiosqlite)
├── models/                  # Pydantic data models
├── services/                # kleinanzeigen CLI wrapper, OpenRouter, Discogs, scheduler
├── tests/                   # unit tests
├── docs/diagrams/           # architecture diagrams (Mermaid)
└── kleinanzeigen-config/    # kleinanzeigen-bot config (config.yaml is gitignored)
    └── config.example.yaml
```

## Security notes

- Never commit secrets. All credentials live in `.env` and `kleinanzeigen-config/config.yaml`,
  both of which are gitignored.
- Only users listed in `TELEGRAM_ALLOWED_USER_IDS` can use the bot.

## License

Licensed under the **GNU Affero General Public License v3.0 or later (AGPL-3.0-or-later)** —
see [LICENSE](LICENSE).

This project uses [Second-Hand-Friends/kleinanzeigen-bot](https://github.com/Second-Hand-Friends/kleinanzeigen-bot),
which is licensed under AGPL-3.0. Because this bot links to it in-process, the combined work is
distributed under the AGPL-3.0 as well.
