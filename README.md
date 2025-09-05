# Reddit Keyword Monitor

Simple, production‑minded Reddit keyword monitor with:
- Live streaming of new posts and comments (PRAW)
- Keyword matching (word boundaries for single tokens; phrases match as substrings)
- Include/exclude subreddits + optional NSFW filtering
- Discord integration (webhook or full bot) with reply tracking
- REST API + Web dashboard (served by FastAPI)
- Instant keyword reloads and SQLite storage

## Quick Start
- Prereqs: Python 3.9+, a Discord application/bot, Reddit API credentials
- Install: `pip install -r requirements.txt`
- Copy `.env.example` to `.env` and fill values (see “Configuration” below)
- Invite your Discord bot to your server with basic permissions (see “Discord Setup”)
- Start the monitor + bot: `python run_with_bot.py`
- Start the API + UI: `uvicorn api:app --port 8000`
- Open the dashboard: http://localhost:8000/ui and add a keyword (e.g., `python`). Matches will post to Discord and populate the UI immediately.

## Configuration (.env)
- Reddit
  - `REDDIT_CLIENT_ID` / `REDDIT_CLIENT_SECRET`: from https://www.reddit.com/prefs/apps
  - `REDDIT_USER_AGENT`: descriptive string, e.g., `KeywordWatcher/0.2`
- Discord (bot mode)
  - `DISCORD_BOT_TOKEN`: your bot token
  - `DISCORD_CHANNEL_ID`: target text channel ID (Developer Mode → right‑click channel → Copy ID)
- Discord (webhook mode, optional)
  - `DISCORD_WEBHOOK`: if set, `main.py` can notify via webhook (no replies captured)
- Monitor
  - `KEYWORDS`: comma‑separated bootstrap list (DB‑managed keywords are merged and hot‑reloaded)
  - `INCLUDE_SUBS`: comma‑separated subreddits; empty means “all”
  - `EXCLUDE_SUBS`: comma‑separated subreddits to skip (case‑insensitive)
  - `ALLOW_NSFW`: accepts `0/1/true/false/yes/no`
  - `DB_PATH`: path to SQLite file (default `seen.db`)
- API / UI
  - `API_BASIC_USER` / `API_BASIC_PASS`: enables Basic Auth if both set
  - `API_CORS_ORIGINS`: comma‑separated origins to allow (if hosting UI separately)
- Control (instant reloads)
  - `CONTROL_HOST` / `CONTROL_PORT`: control server for hot keyword reloads (default `127.0.0.1:8787`)

## Discord Setup
1) Enable Message Content intent in the Discord Developer Portal:
   - Applications → Your App → Bot → Privileged Gateway Intents → toggle “Message Content Intent” → Save
2) Invite the bot to your server with minimal permissions:
   - OAuth2 URL: https://discord.com/api/oauth2/authorize?client_id=YOUR_CLIENT_ID&scope=bot&permissions=68608
   - Permissions included in `68608`: View Channels (1024), Send Messages (2048), Read Message History (65536)
3) Ensure the bot has access (channel permissions: View, Send, Read History). If using private channels/threads, grant explicitly.

## Running
- Monitor + Bot
  - `python run_with_bot.py`
  - Logs show: “Bot ready=True | Starting watcher …” then match logs like “Match POST r/<sub> id=<id> kw=<keywords>”
- API + UI
  - `uvicorn api:app --port 8000`
  - UI at http://localhost:8000/ui

## Dashboard
- Keywords view
  - Add or delete keywords (changes apply instantly)
  - Stats per keyword: posts, comments, matches, replies
- Activity view
  - Recent matches (defaults to last 24h on first visit)
  - Reply counts per match
- Posts view
  - All post matches with filters (keyword, subreddit, kind, date range)
  - CSV export
- Replies view
  - All Discord replies with associated match + keywords
  - Filters + CSV export

## API Reference (high‑level)
- `GET /health` — Health check
- Keywords
  - `GET /keywords` — List (`?q=python` to filter)
  - `POST /keywords` — Body `{"keyword": "mlops"}` to add
  - `DELETE /keywords/{id}` — Remove keyword and links
  - `GET /dashboard/keywords` — Stats per keyword
- Matches
  - `GET /matches` — List matches (filters: `keyword_id`, `keyword`, `subreddit`, `kind`, `from_ts`, `to_ts`; pagination)
  - `GET /matches/{keyword_id}` — Convenience for matches by keyword id
  - `GET /posts` — Posts only (same filters) + CSV: `?format=csv&all=1`
- Replies
  - `GET /replies/{match_id}` — Replies for a specific match
  - `GET /replies` — Replies with parent match info (filters include `kind`, `reply_from_ts`, `reply_to_ts`) + CSV
- Activity
  - `GET /dashboard/activity` — Recent matches + reply counts (filters: `from_ts`, `to_ts`)

Examples
- Add keyword: `curl -u user:pass -X POST http://localhost:8000/keywords -H 'Content-Type: application/json' -d '{"keyword":"python"}'`
- Export posts CSV for last day: `http://localhost:8000/posts?format=csv&all=1&from_ts=...&to_ts=...`

## Keyword Reloads
- Automatic: Monitor refreshes keywords from the DB every ~60s (merged with env `KEYWORDS`)
- Instant: On keyword add/delete, the API calls the local control server `GET /reload` (default http://127.0.0.1:8787/reload)
- Manual: `curl http://127.0.0.1:8787/reload`

## Database
- Normalized tables (also keeps legacy for compatibility):
  - `keywords(id, keyword, created_at)`
  - `matches(id, reddit_id, reddit_url, subreddit, kind, title, body, created_at)`
  - `match_keywords(match_id, keyword_id)`
  - `discord_messages(id, match_id, channel_id, message_id, guild_id, created_at)`
  - `discord_replies_ext(id, match_id, discord_message_id, channel_id, message_id, guild_id, author_id, author_name, content, url, created_at)`
  - Legacy: `discord_posts`, `discord_replies` (still written)
- Indexes added for common queries (created_at, subreddit+created, match/keyword link tables, etc.)

## Troubleshooting
- No data in UI
  - Ensure two processes are running: `python run_with_bot.py` AND `uvicorn api:app --port 8000`
  - Add a common keyword (e.g., `python`) and wait a moment; look for “Keywords reloaded NOW …” or the periodic refresh log
- Bot errors
  - `PrivilegedIntentsRequired`: Enable Message Content intent in the Developer Portal
  - `Missing Access (50001)`: Invite the bot to the server; fix channel permissions; verify `DISCORD_CHANNEL_ID`
- Nothing in `discord_*` tables
  - You’re likely running `main.py` (webhook path). Use `python run_with_bot.py` for bot posting and reply capture
- CORS in browser
  - Set `API_CORS_ORIGINS` (comma‑separated) if UI is hosted on a different origin
- Ports in use
  - Change `CONTROL_PORT` or API port as needed

## Notes
- SQLite file: `seen.db` (configurable via `DB_PATH`)
- Exponential backoff with jitter on Reddit/HTTP errors
- Reddit client is read‑only; PRAW rate‑limit respected
