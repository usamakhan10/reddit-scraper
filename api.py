import os
import time
import sqlite3
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, Depends, HTTPException, Query, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import main as monitor
import requests


# --- App + security config ---
app = FastAPI(title="Reddit Keyword Monitor API", version="0.1.0")
security = HTTPBasic()

API_USER = os.getenv("API_BASIC_USER", "").strip()
API_PASS = os.getenv("API_BASIC_PASS", "").strip()

ALLOW_ORIGINS = [o.strip() for o in os.getenv("API_CORS_ORIGINS", "").split(",") if o.strip()]
if ALLOW_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ALLOW_ORIGINS,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )


def require_basic(creds: HTTPBasicCredentials = Depends(security)):
    if not API_USER and not API_PASS:
        return  # auth disabled
    if not creds or creds.username != API_USER or creds.password != API_PASS:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized")


def get_db():
    conn = sqlite3.connect(monitor.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass


@app.on_event("startup")
def _on_startup():
    # Ensure DB schema exists
    monitor.init_db(monitor.DB_PATH)


@app.get("/health")
def health():
    return {"status": "ok", "time": int(time.time())}


# --- Keywords ---
@app.get("/keywords")
def list_keywords(q: Optional[str] = None, db: sqlite3.Connection = Depends(get_db), _: Any = Depends(require_basic)):
    if q:
        rows = db.execute(
            "SELECT id, keyword, created_at FROM keywords WHERE keyword LIKE ? ORDER BY id DESC",
            (f"%{q}%",),
        ).fetchall()
    else:
        rows = db.execute("SELECT id, keyword, created_at FROM keywords ORDER BY id DESC").fetchall()
    return [dict(r) for r in rows]


@app.post("/keywords", status_code=201)
def add_keyword(payload: Dict[str, str], db: sqlite3.Connection = Depends(get_db), _: Any = Depends(require_basic)):
    kw = (payload or {}).get("keyword", "").strip()
    if not kw:
        raise HTTPException(400, "keyword is required")
    try:
        kid = monitor.db_get_or_create_keyword(db, kw)
        db.commit()
    except Exception as e:
        raise HTTPException(500, f"failed to insert keyword: {e}")
    _notify_reload()
    return {"id": kid, "keyword": kw}


@app.delete("/keywords/{kid}")
def delete_keyword(kid: int, db: sqlite3.Connection = Depends(get_db), _: Any = Depends(require_basic)):
    db.execute("DELETE FROM match_keywords WHERE keyword_id = ?", (kid,))
    cur = db.execute("DELETE FROM keywords WHERE id = ?", (kid,))
    db.commit()
    if cur.rowcount == 0:
        raise HTTPException(404, "keyword not found")
    _notify_reload()
    return {"deleted": kid}


# --- Matches ---
def _build_matches_query(
    keyword_id: Optional[int],
    keyword: Optional[str],
    subreddit: Optional[str],
    kind: Optional[str],
    from_ts: Optional[int],
    to_ts: Optional[int],
):
    base = [
        "SELECT m.id, m.reddit_id, m.reddit_url, m.subreddit, m.kind, m.title, m.body, m.created_at,",
        "COALESCE(GROUP_CONCAT(k.keyword, ','), '') AS keywords",
        "FROM matches m",
        "LEFT JOIN match_keywords mk ON mk.match_id = m.id",
        "LEFT JOIN keywords k ON k.id = mk.keyword_id",
    ]
    where = []
    params: List[Any] = []

    if keyword_id is not None:
        where.append("m.id IN (SELECT match_id FROM match_keywords WHERE keyword_id = ?)")
        params.append(keyword_id)
    elif keyword:
        where.append(
            "m.id IN (SELECT mk.match_id FROM match_keywords mk JOIN keywords k ON k.id = mk.keyword_id WHERE k.keyword = ?)"
        )
        params.append(keyword)

    if subreddit:
        where.append("m.subreddit = ?")
        params.append(subreddit)
    if kind:
        where.append("m.kind = ?")
        params.append(kind)
    if from_ts is not None:
        where.append("m.created_at >= ?")
        params.append(from_ts)
    if to_ts is not None:
        where.append("m.created_at <= ?")
        params.append(to_ts)

    if where:
        base.append("WHERE " + " AND ".join(where))
    base.append("GROUP BY m.id ORDER BY m.created_at DESC")
    return "\n".join(base), params


@app.get("/matches")
def list_matches(
    keyword_id: Optional[int] = Query(default=None),
    keyword: Optional[str] = Query(default=None),
    subreddit: Optional[str] = Query(default=None),
    kind: Optional[str] = Query(default=None, regex="^(post|comment)$"),
    from_ts: Optional[int] = Query(default=None, description="unix timestamp"),
    to_ts: Optional[int] = Query(default=None, description="unix timestamp"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    db: sqlite3.Connection = Depends(get_db),
    _: Any = Depends(require_basic),
):
    sql, params = _build_matches_query(keyword_id, keyword, subreddit, kind, from_ts, to_ts)
    offset = (page - 1) * size
    sql_paged = f"{sql} LIMIT ? OFFSET ?"
    rows = db.execute(sql_paged, (*params, size, offset)).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["keywords"] = [k for k in (d.get("keywords") or "").split(",") if k]
        items.append(d)
    return {"page": page, "size": size, "items": items}


@app.get("/matches/{kid}")
def list_matches_by_keyword(kid: int, page: int = 1, size: int = 20, db: sqlite3.Connection = Depends(get_db), _: Any = Depends(require_basic)):
    return list_matches(keyword_id=kid, page=page, size=size, db=db)  # type: ignore[arg-type]


@app.get("/replies/{match_id}")
def list_replies(match_id: int, db: sqlite3.Connection = Depends(get_db), _: Any = Depends(require_basic)):
    rows = db.execute(
        """
        SELECT id, match_id, discord_message_id, channel_id, message_id, guild_id, author_id, author_name, content, url, created_at
        FROM discord_replies_ext
        WHERE match_id = ?
        ORDER BY created_at DESC
        """,
        (match_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# --- Dashboard endpoints ---
@app.get("/dashboard/keywords")
def dashboard_keywords(db: sqlite3.Connection = Depends(get_db), _: Any = Depends(require_basic)):
    rows = db.execute(
        """
        SELECT 
          k.id,
          k.keyword,
          k.created_at,
          COALESCE(mc.matches_count, 0) AS matches_count,
          COALESCE(pc.posts_count, 0) AS posts_count,
          COALESCE(cc.comments_count, 0) AS comments_count,
          COALESCE(rc.replies_count, 0) AS replies_count
        FROM keywords k
        LEFT JOIN (
          SELECT mk.keyword_id, COUNT(*) AS matches_count
          FROM match_keywords mk
          GROUP BY mk.keyword_id
        ) mc ON mc.keyword_id = k.id
        LEFT JOIN (
          SELECT mk.keyword_id, COUNT(*) AS posts_count
          FROM match_keywords mk
          JOIN matches m ON m.id = mk.match_id
          WHERE m.kind = 'post'
          GROUP BY mk.keyword_id
        ) pc ON pc.keyword_id = k.id
        LEFT JOIN (
          SELECT mk.keyword_id, COUNT(*) AS comments_count
          FROM match_keywords mk
          JOIN matches m ON m.id = mk.match_id
          WHERE m.kind = 'comment'
          GROUP BY mk.keyword_id
        ) cc ON cc.keyword_id = k.id
        LEFT JOIN (
          SELECT mk.keyword_id, COUNT(*) AS replies_count
          FROM match_keywords mk
          JOIN matches m ON m.id = mk.match_id
          JOIN discord_replies_ext dre ON dre.match_id = m.id
          GROUP BY mk.keyword_id
        ) rc ON rc.keyword_id = k.id
        ORDER BY k.id DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


@app.get("/dashboard/activity")
def dashboard_activity(
    limit: int = Query(default=20, ge=1, le=100),
    db: sqlite3.Connection = Depends(get_db),
    _: Any = Depends(require_basic),
):
    rows = db.execute(
        """
        SELECT 
          m.id, m.reddit_id, m.reddit_url, m.subreddit, m.kind, m.title, m.body, m.created_at,
          COALESCE(GROUP_CONCAT(k.keyword, ','), '') AS keywords,
          COALESCE(rc.reply_count, 0) AS reply_count,
          COALESCE(rc.last_reply_at, 0) AS last_reply_at
        FROM matches m
        LEFT JOIN match_keywords mk ON mk.match_id = m.id
        LEFT JOIN keywords k ON k.id = mk.keyword_id
        LEFT JOIN (
          SELECT match_id, COUNT(*) AS reply_count, MAX(created_at) AS last_reply_at
          FROM discord_replies_ext
          GROUP BY match_id
        ) rc ON rc.match_id = m.id
        GROUP BY m.id
        ORDER BY m.created_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["keywords"] = [k for k in (d.get("keywords") or "").split(",") if k]
        items.append(d)
    return {"items": items}


@app.get("/posts")
def list_posts(
    keyword_id: Optional[int] = Query(default=None),
    keyword: Optional[str] = Query(default=None),
    subreddit: Optional[str] = Query(default=None),
    from_ts: Optional[int] = Query(default=None),
    to_ts: Optional[int] = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    db: sqlite3.Connection = Depends(get_db),
    _: Any = Depends(require_basic),
):
    # Force kind='post' regardless of client input
    sql, params = _build_matches_query(keyword_id, keyword, subreddit, kind='post', from_ts=from_ts, to_ts=to_ts)
    offset = (page - 1) * size
    sql_paged = f"{sql} LIMIT ? OFFSET ?"
    rows = db.execute(sql_paged, (*params, size, offset)).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["keywords"] = [k for k in (d.get("keywords") or "").split(",") if k]
        items.append(d)
    return {"page": page, "size": size, "items": items}


@app.get("/replies")
def list_all_replies(
    keyword_id: Optional[int] = Query(default=None),
    keyword: Optional[str] = Query(default=None),
    subreddit: Optional[str] = Query(default=None),
    kind: Optional[str] = Query(default=None, regex="^(post|comment)$"),
    reply_from_ts: Optional[int] = Query(default=None),
    reply_to_ts: Optional[int] = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=100),
    db: sqlite3.Connection = Depends(get_db),
    _: Any = Depends(require_basic),
):
    base = [
        "SELECT",
        "  dre.id AS reply_id,",
        "  dre.url AS reply_url,",
        "  dre.author_name,",
        "  dre.author_id,",
        "  dre.content AS reply_content,",
        "  dre.created_at AS reply_created_at,",
        "  m.id AS match_id, m.reddit_id, m.reddit_url, m.subreddit, m.kind, m.title, m.body, m.created_at AS match_created_at,",
        "  (SELECT GROUP_CONCAT(k.keyword, ',') FROM match_keywords mk2 JOIN keywords k ON k.id = mk2.keyword_id WHERE mk2.match_id = m.id) AS keywords",
        "FROM discord_replies_ext dre",
        "JOIN matches m ON m.id = dre.match_id",
    ]
    where = []
    params: List[Any] = []

    if keyword_id is not None:
        where.append("EXISTS (SELECT 1 FROM match_keywords mk WHERE mk.match_id = m.id AND mk.keyword_id = ?)")
        params.append(keyword_id)
    elif keyword:
        where.append(
            "EXISTS (SELECT 1 FROM match_keywords mk JOIN keywords k ON k.id = mk.keyword_id WHERE mk.match_id = m.id AND k.keyword = ?)"
        )
        params.append(keyword)

    if subreddit:
        where.append("m.subreddit = ?")
        params.append(subreddit)
    if kind:
        where.append("m.kind = ?")
        params.append(kind)
    if reply_from_ts is not None:
        where.append("dre.created_at >= ?")
        params.append(reply_from_ts)
    if reply_to_ts is not None:
        where.append("dre.created_at <= ?")
        params.append(reply_to_ts)

    if where:
        base.append("WHERE " + " AND ".join(where))
    base.append("ORDER BY dre.created_at DESC")
    sql = "\n".join(base)

    offset = (page - 1) * size
    sql_paged = f"{sql} LIMIT ? OFFSET ?"
    rows = db.execute(sql_paged, (*params, size, offset)).fetchall()
    items = []
    for r in rows:
        d = dict(r)
        d["keywords"] = [k for k in (d.get("keywords") or "").split(",") if k]
        items.append(d)
    return {"page": page, "size": size, "items": items}


# --- Static UI (Phase 5) ---
try:
    app.mount("/ui", StaticFiles(directory="web", html=True), name="ui")
except RuntimeError:
    # Ignore if directory missing at import time (should exist in repo)
    pass


def _notify_reload():
    host = os.getenv("CONTROL_HOST", "127.0.0.1").strip()
    port = os.getenv("CONTROL_PORT", "8787").strip()
    url = f"http://{host}:{port}/reload"
    try:
        requests.get(url, timeout=2)
    except Exception:
        # Best-effort; monitor will refresh on its own timer
        pass
