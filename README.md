# reddit-scraper

Simple Reddit keyword monitor that streams new posts and comments via PRAW, filters by keywords and subreddit rules, dedupes with SQLite, and notifies via a Discord webhook.

## Setup
- Python 3.9+ recommended.
- Install dependencies: `pip install -r requirements.txt`.
- Copy `.env.example` to `.env` and fill in values:
  - `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` (script app).
  - `REDDIT_USER_AGENT` (any descriptive string).
  - `DISCORD_WEBHOOK` (optional; falls back to logging if empty).
  - `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` to enable the Discord bot (Phase 2).
  - `KEYWORDS` (comma-separated; single words match on word boundaries, phrases match as substrings).
  - `INCLUDE_SUBS` (comma-separated; leave empty for `all`).
  - `EXCLUDE_SUBS` (comma-separated; case-insensitive).
  - `ALLOW_NSFW` (accepts 0/1/true/false/yes/no).
  
## Run
```
python main.py
```

On start, the watcher logs the active config. It streams live content only (`skip_existing=True`).

## Discord Bot (Phase 2)
To post matches as a bot and track Discord replies:
- Ensure the bot has access to the target channel and Message Content Intent is enabled in the Discord developer portal.
- Fill `DISCORD_BOT_TOKEN` and `DISCORD_CHANNEL_ID` in `.env`.
- Run the combined runner:
```
python run_with_bot.py
```
The bot posts each match to the configured channel. Replies to the bot's message are stored in the database (`discord_replies`) and linked to the posted match (`discord_posts`).

## Phase 3: Database Upgrade
New normalized tables are created alongside legacy ones:
- `keywords`: all monitored keywords with IDs.
- `matches`: one row per Reddit item (post or comment).
- `match_keywords`: links each match to all matched keywords.
- `discord_messages`: maps a Discord message to a `match_id`.
- `discord_replies_ext`: replies linked back to `match_id` and message.

Legacy tables remain for compatibility:
- `discord_posts` and `discord_replies` are still populated.

You can query the new schema, e.g. recent matches with keywords:
```
SELECT m.id, m.subreddit, m.kind, m.reddit_url, group_concat(k.keyword, ',') AS keywords
FROM matches m
JOIN match_keywords mk ON mk.match_id = m.id
JOIN keywords k ON k.id = mk.keyword_id
GROUP BY m.id
ORDER BY m.id DESC
LIMIT 20;
```

## Phase 4: REST API
Simple API for managing keywords and browsing matches/replies.

### Run the API
```
uvicorn api:app --reload --port 8000
```

Configure (optional) in `.env`:
- `API_BASIC_USER` / `API_BASIC_PASS`: enable Basic Auth if both are set.
- `API_CORS_ORIGINS`: comma-separated origins for CORS.

### Endpoints
- `GET /health`: health check.
- `GET /keywords`: list keywords; filter with `?q=ml`.
- `POST /keywords`: `{ "keyword": "mlops" }` — upsert keyword.
- `DELETE /keywords/{id}`: remove keyword and links.
- `GET /matches`: list matches (pagination + filters).
  - Filters: `keyword_id`, `keyword`, `subreddit`, `kind` (post/comment), `from_ts`, `to_ts`.
  - Pagination: `page` (1+), `size` (1–100).
- `GET /matches/{keyword_id}`: shorthand for matches filtered by a keyword id.
- `GET /replies/{match_id}`: list Discord replies for a match.

Note: The monitor currently reads keywords from env at startup. Managing keywords via API updates the DB but does not hot-reload the running monitor yet (can be added later).

## Phase 5: Dashboard UI
A lightweight front-end is served at `/ui` by the API server.

### Run UI
```
uvicorn api:app --reload --port 8000
# Open http://localhost:8000/ui
```

### Features
- Manage keywords (list/add/delete).
- Browse matches with filters (keyword, subreddit, kind, date range).
- Pagination (Next/Prev).
- Load Discord replies per match.
- Basic Auth support (prompt) if API auth is enabled.


## Notes
- SQLite file: `seen.db` (path configurable via `DB_PATH`).
- App uses exponential backoff with jitter on Reddit/HTTP errors.
- Reddit client is set to read-only.
