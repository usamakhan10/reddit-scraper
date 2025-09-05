import os
import re
import time
import sqlite3
import logging
import threading
import signal
import random
from typing import List, Pattern, Callable, Optional
from http.server import BaseHTTPRequestHandler, HTTPServer
import socket

import requests
import praw
import prawcore
from dotenv import load_dotenv

# ------------------- Config (from environment) -------------------
load_dotenv()


def env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    val = val.strip().lower()
    return val in {"1", "true", "yes", "y", "on"}


_ENV_KEYWORDS = os.getenv("KEYWORDS", "machine learning,mlops").split(",")
_ENV_KEYWORDS = [k.strip() for k in _ENV_KEYWORDS if k.strip()]

INCLUDE_SUBS = os.getenv("INCLUDE_SUBS", "")
EXCLUDE_SUBS = set(s.strip().lower() for s in os.getenv("EXCLUDE_SUBS", "").split(",") if s.strip())

ALLOW_NSFW = env_bool("ALLOW_NSFW", False)

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
DB_PATH = os.getenv("DB_PATH", "seen.db")
CONTROL_HOST = os.getenv("CONTROL_HOST", "127.0.0.1")
CONTROL_PORT = int(os.getenv("CONTROL_PORT", "8787") or 8787)

CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
USER_AGENT = os.getenv("REDDIT_USER_AGENT", "KeywordWatcher/0.1 by u/yourusername")

# ------------------- Logging -------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("reddit_watcher")

# ------------------- DB helpers -------------------
def init_db(path: str):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            ts INTEGER NOT NULL
        )
        """
    )
    # Phase 2 tables for Discord bot integration
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discord_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reddit_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            subreddit TEXT,
            reddit_url TEXT NOT NULL,
            keywords TEXT,
            channel_id TEXT,
            message_id TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            UNIQUE(reddit_id),
            UNIQUE(message_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discord_replies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_message_id TEXT NOT NULL,
            reply_message_id TEXT NOT NULL,
            author_id TEXT,
            author_name TEXT,
            content TEXT,
            url TEXT,
            created_at INTEGER NOT NULL,
            UNIQUE(reply_message_id)
        )
        """
    )
    # Phase 3 normalized schema
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            keyword TEXT UNIQUE NOT NULL,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS matches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            reddit_id TEXT UNIQUE NOT NULL,
            reddit_url TEXT NOT NULL,
            subreddit TEXT,
            kind TEXT NOT NULL,
            title TEXT,
            body TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS match_keywords (
            match_id INTEGER NOT NULL,
            keyword_id INTEGER NOT NULL,
            PRIMARY KEY (match_id, keyword_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discord_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            channel_id TEXT,
            message_id TEXT UNIQUE NOT NULL,
            guild_id TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS discord_replies_ext (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            match_id INTEGER NOT NULL,
            discord_message_id TEXT,
            channel_id TEXT,
            message_id TEXT UNIQUE,
            guild_id TEXT,
            author_id TEXT,
            author_name TEXT,
            content TEXT,
            url TEXT,
            created_at INTEGER NOT NULL
        )
        """
    )
    # -------- Indexes for common queries --------
    conn.execute("CREATE INDEX IF NOT EXISTS idx_matches_created_at ON matches (created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_matches_subreddit_created ON matches (subreddit, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_keywords_keyword ON match_keywords (keyword_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_match_keywords_match ON match_keywords (match_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_discord_messages_match ON discord_messages (match_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_discord_replies_ext_match ON discord_replies_ext (match_id, created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_discord_posts_created ON discord_posts (created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_discord_replies_msg ON discord_replies (discord_message_id)")
    conn.commit()
    return conn


def already_seen(conn, _id: str) -> bool:
    cur = conn.execute("SELECT 1 FROM seen WHERE id = ?", (_id,))
    return cur.fetchone() is not None


def mark_seen(conn, _id: str, kind: str):
    conn.execute(
        "INSERT OR IGNORE INTO seen (id, kind, ts) VALUES (?, ?, ?)",
        (_id, kind, int(time.time())),
    )
    conn.commit()


KEYWORDS_LOCK = threading.RLock()
KEYWORDS: List[str] = []
PATTERNS: List[Pattern] = []


# ------------------- Keyword matching -------------------
def compile_patterns(keywords: List[str]) -> List[Pattern]:
    patterns = []
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        if " " in kw:
            pat = re.compile(re.escape(kw), re.IGNORECASE)
        else:
            pat = re.compile(rf"\b{re.escape(kw)}\b", re.IGNORECASE)
        patterns.append(pat)
    return patterns
def set_keywords(keywords: List[str]):
    global KEYWORDS, PATTERNS
    with KEYWORDS_LOCK:
        KEYWORDS = list(dict.fromkeys([k.strip() for k in keywords if k.strip()]))
        PATTERNS = compile_patterns(KEYWORDS)


def any_match(text: str) -> bool:
    t = text or ""
    with KEYWORDS_LOCK:
        patterns = list(PATTERNS)
    return any(p.search(t) for p in patterns)


def find_keywords(text: str) -> List[str]:
    t = text or ""
    with KEYWORDS_LOCK:
        kws = list(KEYWORDS)
        pats = list(PATTERNS)
    hits: List[str] = []
    for kw, pat in zip(kws, pats):
        if pat.search(t):
            hits.append(kw)
    return hits


# ------------------- Notifiers (Discord only) -------------------
def notify_print(payload: dict):
    kind = payload.get("kind")
    title = payload.get("title")
    if not title:
        body = payload.get("body", "")
        title = body if len(body) <= 80 else body[:80] + "..."
    url = payload.get("url")
    log.info("[%s] %s -> %s", (kind or "").upper(), title, url)


def notify_discord(payload: dict):
    if not DISCORD_WEBHOOK:
        notify_print(payload)
        return
    text = payload.get("title") or payload.get("body", "")
    snippet = text if len(text) <= 120 else text[:120] + "..."
    content = f"**[{payload.get('kind','').upper()}]** {snippet}\n{payload.get('url')}"
    try:
        r = requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=10)
        if r.status_code >= 400:
            log.warning("Discord webhook returned %s: %s", r.status_code, r.text[:200])
            notify_print(payload)
    except Exception as e:
        log.warning("Discord notify failed: %s", e)
        notify_print(payload)


_notify_handler: Optional[Callable[[dict], None]] = None


def register_notify_handler(handler: Callable[[dict], None]):
    global _notify_handler
    _notify_handler = handler


def notify(payload: dict):
    # If external notifier registered (e.g., Discord bot), use it first
    if _notify_handler is not None:
        try:
            _notify_handler(payload)
            return
        except Exception as e:
            log.exception("External notifier failed; falling back to webhook: %s", e)
    notify_discord(payload)


# ------------------- Reddit client -------------------
def reddit_client():
    if not CLIENT_ID or not CLIENT_SECRET:
        log.error("REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET must be set in environment")
        raise SystemExit(1)
    r = praw.Reddit(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        user_agent=USER_AGENT,
        ratelimit_seconds=5,
    )
    # Explicitly enforce read-only usage
    try:
        r.read_only = True
    except Exception:
        pass
    return r


def subreddit_target():
    subs = [s.strip() for s in INCLUDE_SUBS.split(",") if s.strip()]
    return "+".join(subs) if subs else "all"


# ------------------- Stream workers -------------------
_stop = False


def _backoff_sleep(base=5, factor=2, cap=300, attempt=0):
    # Exponential backoff with jitter
    delay = min(cap, base * (factor ** attempt))
    jitter = random.uniform(0.5, 1.5)
    time.sleep(delay * jitter)


def stream_submissions(reddit, conn):
    target = subreddit_target()
    sr = reddit.subreddit(target)
    attempt = 0
    while not _stop:
        try:
            for sub in sr.stream.submissions(skip_existing=True):
                if _stop:
                    break
                sub_name = sub.subreddit.display_name.lower()
                if sub_name in EXCLUDE_SUBS:
                    continue
                if not ALLOW_NSFW and getattr(sub, "over_18", False):
                    continue
                text_to_check = f"{sub.title}\n{getattr(sub, 'selftext', '')}"
                kw_hits = find_keywords(text_to_check)
                if kw_hits:
                    if not already_seen(conn, sub.id):
                        log.info("Match POST r/%s id=%s kw=%s", sub.subreddit.display_name, sub.id, ",".join(kw_hits))
                        mark_seen(conn, sub.id, "post")
                        payload = {
                            "kind": "post",
                            "title": sub.title,
                            "url": f"https://www.reddit.com{sub.permalink}",
                            "subreddit": sub.subreddit.display_name,
                            "reddit_id": sub.id,
                            "keywords": kw_hits,
                        }
                        notify(payload)
            attempt = 0
        except (
            prawcore.exceptions.RequestException,
            prawcore.exceptions.ResponseException,
            prawcore.exceptions.ServerError,
        ) as e:
            log.warning("Post stream error: %s (backing off)", e)
            _backoff_sleep(attempt=attempt)
            attempt += 1
        except Exception as e:
            log.exception("Unexpected post stream error: %s", e)
            _backoff_sleep(attempt=attempt)
            attempt += 1


def stream_comments(reddit, conn):
    target = subreddit_target()
    sr = reddit.subreddit(target)
    attempt = 0
    while not _stop:
        try:
            for c in sr.stream.comments(skip_existing=True):
                if _stop:
                    break
                sub_name = c.subreddit.display_name.lower()
                if sub_name in EXCLUDE_SUBS:
                    continue
                # subreddit objects have different attributes across versions; safely check NSFW
                over18 = getattr(c.subreddit, "over18", getattr(c.subreddit, "over_18", False))
                if not ALLOW_NSFW and over18:
                    continue
                kw_hits = find_keywords(c.body)
                if kw_hits:
                    if not already_seen(conn, c.id):
                        log.info("Match COMMENT r/%s id=%s kw=%s", c.subreddit.display_name, c.id, ",".join(kw_hits))
                        mark_seen(conn, c.id, "comment")
                        payload = {
                            "kind": "comment",
                            "body": c.body,
                            "url": f"https://www.reddit.com{c.permalink}",
                            "subreddit": c.subreddit.display_name,
                            "reddit_id": c.id,
                            "keywords": kw_hits,
                        }
                        notify(payload)
            attempt = 0
        except (
            prawcore.exceptions.RequestException,
            prawcore.exceptions.ResponseException,
            prawcore.exceptions.ServerError,
        ) as e:
            log.warning("Comment stream error: %s (backing off)", e)
            _backoff_sleep(attempt=attempt)
            attempt += 1
        except Exception as e:
            log.exception("Unexpected comment stream error: %s", e)
            _backoff_sleep(attempt=attempt)
            attempt += 1


# ------------------- Keyword refresh from DB (Phase 3/5 integration) -------------------
def _load_keywords_from_db(conn) -> List[str]:
    try:
        cur = conn.execute("SELECT keyword FROM keywords ORDER BY id ASC")
        return [row[0] for row in cur.fetchall() if (row and row[0])]
    except Exception:
        return []


def start_keywords_refresher(conn, interval_sec: int = 60):
    def _worker():
        last: Optional[List[str]] = None
        while not _stop:
            db_kws = _load_keywords_from_db(conn)
            # Union env + db, preserve order
            merged = list(dict.fromkeys([*(_ENV_KEYWORDS or []), *(db_kws or [])]))
            if merged and merged != last:
                set_keywords(merged)
                log.info("Keywords refreshed | total=%s", len(merged))
                last = merged
            for _ in range(interval_sec):
                if _stop:
                    break
                time.sleep(1)
    t = threading.Thread(target=_worker, name="keywords-refresher", daemon=True)
    t.start()


def reload_keywords_now(conn):
    db_kws = _load_keywords_from_db(conn)
    merged = list(dict.fromkeys([*(_ENV_KEYWORDS or []), *(db_kws or [])]))
    if merged:
        set_keywords(merged)
        log.info("Keywords reloaded NOW | total=%s", len(merged))


def start_control_server(conn):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.startswith("/reload"):
                try:
                    reload_keywords_now(conn)
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(b'{"status":"reloaded"}')
                except Exception as e:
                    log.warning("Reload failed: %s", e)
                    self.send_response(500)
                    self.end_headers()
                return
            self.send_response(404)
            self.end_headers()

        def log_message(self, format, *args):
            # Silence default HTTPServer logging; use our logger if needed
            return

    def _serve():
        # Allow quick reuse
        server_address = (CONTROL_HOST, CONTROL_PORT)
        httpd = HTTPServer(server_address, Handler)
        log.info("Control server on http://%s:%s", CONTROL_HOST, CONTROL_PORT)
        try:
            httpd.serve_forever(poll_interval=0.5)
        except Exception as e:
            log.warning("Control server stopped: %s", e)

    t = threading.Thread(target=_serve, name="control-server", daemon=True)
    t.start()

# ------------------- Phase 2 DB helpers (used by Discord bot) -------------------
def db_record_discord_post(conn, *, reddit_id: str, kind: str, subreddit: str, reddit_url: str,
                           keywords: List[str], channel_id: int, message_id: int):
    conn.execute(
        """
        INSERT OR REPLACE INTO discord_posts
        (reddit_id, kind, subreddit, reddit_url, keywords, channel_id, message_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            reddit_id,
            kind,
            subreddit,
            reddit_url,
            ",".join(keywords),
            str(channel_id),
            str(message_id),
            int(time.time()),
        ),
    )
    conn.commit()


def db_record_discord_reply(conn, *, discord_message_id: int, reply_message_id: int, author_id: int,
                            author_name: str, content: str, url: str):
    conn.execute(
        """
        INSERT OR IGNORE INTO discord_replies
        (discord_message_id, reply_message_id, author_id, author_name, content, url, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            str(discord_message_id),
            str(reply_message_id),
            str(author_id) if author_id is not None else None,
            author_name,
            content,
            url,
            int(time.time()),
        ),
    )
    conn.commit()


# ------------------- Phase 3 DB helpers -------------------
def db_get_or_create_keyword(conn, keyword: str) -> int:
    keyword = keyword.strip()
    if not keyword:
        raise ValueError("keyword must be non-empty")
    conn.execute(
        "INSERT OR IGNORE INTO keywords (keyword, created_at) VALUES (?, ?)",
        (keyword, int(time.time())),
    )
    row = conn.execute("SELECT id FROM keywords WHERE keyword = ?", (keyword,)).fetchone()
    return int(row[0])


def db_get_or_create_match(conn, *, reddit_id: str, reddit_url: str, subreddit: str, kind: str,
                           title: str = None, body: str = None) -> int:
    now = int(time.time())
    conn.execute(
        """
        INSERT OR IGNORE INTO matches
        (reddit_id, reddit_url, subreddit, kind, title, body, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (reddit_id, reddit_url, subreddit, kind, title, body, now),
    )
    row = conn.execute("SELECT id FROM matches WHERE reddit_id = ?", (reddit_id,)).fetchone()
    return int(row[0])


def db_link_keywords(conn, match_id: int, keywords: List[str]):
    for kw in keywords:
        try:
            kid = db_get_or_create_keyword(conn, kw)
        except ValueError:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO match_keywords (match_id, keyword_id) VALUES (?, ?)",
            (match_id, kid),
        )
    conn.commit()


def db_record_discord_message(conn, *, match_id: int, channel_id: int, message_id: int, guild_id: int = None):
    conn.execute(
        """
        INSERT OR IGNORE INTO discord_messages (match_id, channel_id, message_id, guild_id, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (match_id, str(channel_id), str(message_id), str(guild_id) if guild_id else None, int(time.time())),
    )
    conn.commit()


def db_find_match_id_by_message_id(conn, *, message_id: int) -> Optional[int]:
    row = conn.execute(
        "SELECT match_id FROM discord_messages WHERE message_id = ?",
        (str(message_id),),
    ).fetchone()
    return int(row[0]) if row else None


def db_record_discord_reply_ext(conn, *, match_id: int, discord_message_id: int, channel_id: int, message_id: int,
                                guild_id: int = None, author_id: int = None, author_name: str = None,
                                content: str = None, url: str = None):
    conn.execute(
        """
        INSERT OR IGNORE INTO discord_replies_ext
        (match_id, discord_message_id, channel_id, message_id, guild_id, author_id, author_name, content, url, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            match_id,
            str(discord_message_id) if discord_message_id else None,
            str(channel_id) if channel_id else None,
            str(message_id),
            str(guild_id) if guild_id else None,
            str(author_id) if author_id is not None else None,
            author_name,
            content,
            url,
            int(time.time()),
        ),
    )
    conn.commit()


# ------------------- Main -------------------
def main():
    global _stop
    conn = init_db(DB_PATH)
    r = reddit_client()
    # Initialize keywords (env + DB) and start refresher
    set_keywords(_ENV_KEYWORDS)
    start_keywords_refresher(conn)
    start_control_server(conn)

    log.info(
        "Starting watcher | subs=%s | exclude=%s | nsfw=%s | keywords=%s | notify=discord",
        subreddit_target(), 
        ",".join(sorted(EXCLUDE_SUBS)) or "-", 
        ALLOW_NSFW, 
        KEYWORDS,
    )

    def handle_sig(sig, frame):
        global _stop
        log.info("Shutting down...")
        _stop = True

    try:
        signal.signal(signal.SIGINT, handle_sig)
        signal.signal(signal.SIGTERM, handle_sig)
    except Exception:
        # Some environments may not support all signals
        pass

    t1 = threading.Thread(target=stream_submissions, args=(r, conn), daemon=True)
    t2 = threading.Thread(target=stream_comments, args=(r, conn), daemon=True)
    t1.start()
    t2.start()

    try:
        while not _stop:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Interrupted, shutting down")
        _stop = True


if __name__ == "__main__":
    main()
