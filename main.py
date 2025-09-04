import os
import re
import time
import sqlite3
import logging
import threading
import signal
from typing import List, Pattern

import requests
import praw
import prawcore
from dotenv import load_dotenv

# ------------------- Config (from environment) -------------------
load_dotenv()
KEYWORDS = os.getenv("KEYWORDS", "machine learning,mlops").split(",")
KEYWORDS = [k.strip() for k in KEYWORDS if k.strip()]

INCLUDE_SUBS = os.getenv("INCLUDE_SUBS", "")
EXCLUDE_SUBS = set(s.strip().lower() for s in os.getenv("EXCLUDE_SUBS", "").split(",") if s.strip())

ALLOW_NSFW = os.getenv("ALLOW_NSFW", "0") == "1"

DISCORD_WEBHOOK = os.getenv("DISCORD_WEBHOOK", "")
DB_PATH = os.getenv("DB_PATH", "seen.db")

CLIENT_ID = os.getenv("REDDIT_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("REDDIT_CLIENT_SECRET", "")
USER_AGENT = os.getenv("REDDIT_USER_AGENT", "KeywordWatcher/0.1 by u/yourusername")

# ------------------- Logging -------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("reddit_watcher")

# ------------------- DB helpers -------------------
def init_db(path: str):
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS seen (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            ts INTEGER NOT NULL
        )
    """)
    conn.commit()
    return conn


def already_seen(conn, _id: str) -> bool:
    cur = conn.execute("SELECT 1 FROM seen WHERE id = ?", (_id,))
    return cur.fetchone() is not None


def mark_seen(conn, _id: str, kind: str):
    conn.execute("INSERT OR IGNORE INTO seen (id, kind, ts) VALUES (?, ?, ?)", (_id, kind, int(time.time())))
    conn.commit()

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

PATTERNS = compile_patterns(KEYWORDS)


def any_match(text: str) -> bool:
    t = text or ""
    return any(p.search(t) for p in PATTERNS)

# ------------------- Notifiers (Discord only) -------------------
def notify_print(payload: dict):
    kind = payload.get("kind")
    title = payload.get("title") or (payload.get("body", "")[:80] + "...")
    url = payload.get("url")
    log.info("[%s] %s -> %s", kind.upper(), title, url)


def notify_discord(payload: dict):
    if not DISCORD_WEBHOOK:
        notify_print(payload)
        return
    content = f"**[{payload.get('kind','').upper()}]** {payload.get('title') or payload.get('body','')[:120]}\n{payload.get('url')}"
    try:
        r = requests.post(DISCORD_WEBHOOK, json={"content": content}, timeout=10)
        if r.status_code >= 400:
            log.warning("Discord webhook returned %s: %s", r.status_code, r.text[:200])
            notify_print(payload)
    except Exception as e:
        log.warning("Discord notify failed: %s", e)
        notify_print(payload)


def notify(payload: dict):
    # Only Discord (falls back to print if webhook missing or failing)
    notify_discord(payload)

# ------------------- Reddit client -------------------
def reddit_client():
    if not CLIENT_ID or not CLIENT_SECRET:
        log.error("REDDIT_CLIENT_ID and REDDIT_CLIENT_SECRET must be set in environment")
        raise SystemExit(1)
    return praw.Reddit(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        user_agent=USER_AGENT,
        ratelimit_seconds=5,
    )


def subreddit_target():
    subs = [s.strip() for s in INCLUDE_SUBS.split(",") if s.strip()]
    return "+".join(subs) if subs else "all"

# ------------------- Stream workers -------------------
_stop = False


def _backoff_sleep(base=5, factor=2, cap=300, attempt=0):
    time.sleep(min(cap, base * (factor ** attempt)))


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
                if any_match(text_to_check):
                    if not already_seen(conn, sub.id):
                        mark_seen(conn, sub.id, "post")
                        payload = {
                            "kind": "post",
                            "title": sub.title,
                            "url": f"https://www.reddit.com{sub.permalink}",
                            "subreddit": sub.subreddit.display_name,
                        }
                        notify(payload)
            attempt = 0
        except (prawcore.exceptions.RequestException,
                prawcore.exceptions.ResponseException,
                prawcore.exceptions.ServerError) as e:
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
                if any_match(c.body):
                    if not already_seen(conn, c.id):
                        mark_seen(conn, c.id, "comment")
                        payload = {
                            "kind": "comment",
                            "body": c.body,
                            "url": f"https://www.reddit.com{c.permalink}",
                            "subreddit": c.subreddit.display_name,
                        }
                        notify(payload)
            attempt = 0
        except (prawcore.exceptions.RequestException,
                prawcore.exceptions.ResponseException,
                prawcore.exceptions.ServerError) as e:
            log.warning("Comment stream error: %s (backing off)", e)
            _backoff_sleep(attempt=attempt)
            attempt += 1
        except Exception as e:
            log.exception("Unexpected comment stream error: %s", e)
            _backoff_sleep(attempt=attempt)
            attempt += 1

# ------------------- Main -------------------

def main():
    global _stop
    conn = init_db(DB_PATH)
    r = reddit_client()

    log.info("Starting watcher | subs=%s | exclude=%s | nsfw=%s | keywords=%s | notify=discord",
             subreddit_target(), ",".join(sorted(EXCLUDE_SUBS)) or "-", ALLOW_NSFW, KEYWORDS)

    def handle_sig(sig, frame):
        global _stop
        log.info("Shutting down…")
        _stop = True

    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)

    t1 = threading.Thread(target=stream_submissions, args=(r, conn), daemon=True)
    t2 = threading.Thread(target=stream_comments, args=(r, conn), daemon=True)
    t1.start(); t2.start()

    try:
        while not _stop:
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Interrupted, shutting down")
        _stop = True


if __name__ == "__main__":
    main()
