"""
HERE — red/black/white social network (FastAPI + SQLite).

Run:  ./venv/bin/uvicorn app:app --host 127.0.0.1 --port 8002
DB:   here.db in this directory (SQLite), or HERE_DB env.
Uploads: uploads/ in this directory (avatars/, posts/, pages/), or HERE_UPLOADS env.

Sessions: signed random-token cookie sessions (sessions table, cookie
`here_session`). Owner panel uses its own cookie `here_owner` against
owner_sessions; the owner password hash is baked in as OWNER_PW_HASH.

Implements the binding contract in ROUTE_CONTRACT.md exactly.
"""
import base64
import hashlib
import hmac
import html
import json
import mimetypes
import os
import re
import secrets
import shutil
import smtplib
import sqlite3
import tempfile
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
import urllib.error
import urllib.request
from urllib.parse import parse_qs, quote, urlencode, urlparse

import bcrypt
from email.message import EmailMessage
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import ads
import moderation
import storage

# Web Push (phone notifications). Imported defensively: if the library
# is ever missing, the site runs exactly as before with push disabled —
# notifications must never be able to take HERE down.
try:
    from pywebpush import WebPushException, webpush as _webpush_send
    from py_vapid import Vapid as _Vapid
    _PUSH_OK = True
except Exception:
    _PUSH_OK = False

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("HERE_DB", os.path.join(BASE, "here.db"))
UPLOAD_DIR = os.environ.get("HERE_UPLOADS", os.path.join(BASE, "uploads"))
# Locked Content media lives OUTSIDE UPLOAD_DIR on purpose: the
# /uploads static mount must never be able to serve it. Same volume,
# sibling directory (prod: /data/locked_uploads on the persistent
# disk); in R2 mode the same separation is the "locked/" key prefix.
LOCKED_DIR = os.environ.get(
    "HERE_LOCKED_UPLOADS",
    os.path.join(os.path.dirname(os.path.abspath(UPLOAD_DIR)),
                 "locked_uploads"))

SESSION_COOKIE = "here_session"
OWNER_COOKIE = "here_owner"

# Owner password hash — build-time constant. The plaintext lives nowhere in
# this repo; the hash was pasted in from owner_hash.txt during the build.
# Owner password hash. Prefer the HERE_OWNER_HASH env var when set (Render
# dashboard -> Environment -> HERE_OWNER_HASH, sync: false); otherwise the
# baked-in build-time hash is used. The plaintext lives nowhere in this repo.
OWNER_PW_HASH = os.environ.get("HERE_OWNER_HASH", "$2b$12$5KZ9EpDumnhhvCrmdfDB0OV.jnMBAob23CAQEEawWakJgB4/x8mLC")

KINDS = ("text", "photo", "feeling", "clothes", "link", "video", "audio")
PRIVACIES = ("public", "friends", "onlyme")
PRIVACY_LABELS = {"public": "Public", "friends": "Friends only", "onlyme": "Only me"}
PTYPES = ("community", "market", "fan_club", "seller")
# HERE Market: fixed listing categories + conditions (the sell form's
# selects and server-side validation share these lists).
MARKET_CATEGORIES = ("Vehicles", "Electronics", "Home & Garden",
                     "Furniture", "Clothing", "Shoes", "Games & Toys",
                     "Tools", "Free Stuff", "Other")
MARKET_CONDITIONS = ("New", "Like new", "Good", "Fair")
MARKET_MAX_PRICE_CENTS = 999999 * 100  # $999,999 cap
MARKET_SAFETY_NOTE = ("Meet in a public place. HERE doesn't handle "
                      "payments \u2014 pay the seller directly.")
IMG_EXTS = {"jpg", "jpeg", "png", "gif", "webp"}
AUD_EXTS = {"mp3", "wav", "ogg", "m4a"}
VID_EXTS = {"mp4", "mov", "webm"}
MAX_UPLOAD_MB = 25
# Post video uploads get a much bigger cap than other media. The hard
# ceiling is the 1GB disk at /data (DB + ALL uploads live there) plus
# upload time on a phone connection — there are no chunked/resumable
# uploads, so one interrupted upload restarts from zero.
POST_VIDEO_MAX_MB = 200
# Post reactions (extends likes; likes.kind defaults to 'like' for old rows)
REACTIONS = [("like", "👍", "Like"), ("love", "❤️", "Love"),
             ("laugh", "😂", "Haha"), ("wow", "😮", "Wow"),
             ("sad", "😢", "Sad"), ("angry", "😡", "Angry")]
REACTION_EMOJI = {k: e for k, e, _ in REACTIONS}
REACTION_LABEL = {k: l for k, e, l in REACTIONS}
# Stories: 24-hour photo/short-video posts, friends-only
STORY_EXTS = IMG_EXTS | {"mp4", "mov", "webm"}
STORY_TTL_HOURS = 24
# Story lifetimes: the composer offers fixed choices plus a custom-hours
# value; the server always clamps the total into [5, 1440] minutes.
STORY_LIFETIME_PRESETS = {"5m": 5, "10m": 10, "15m": 15, "1h": 60,
                          "24h": 24 * 60}
STORY_MIN_MINUTES = 5
STORY_MAX_MINUTES = 24 * 60
ONLINE_MINUTES = 5

for _d in ("avatars", "posts", "pages", "backgrounds"):
    os.makedirs(os.path.join(UPLOAD_DIR, _d), exist_ok=True)
os.makedirs(os.path.join(BASE, "static"), exist_ok=True)

app = FastAPI(title="HERE")
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")
# Build U: in R2 mode /uploads is NOT a static mount — the
# uploads_serve route below resolves each key (disk first, so legacy
# pre-migration files keep serving; then a 302 to R2). Disk mode
# keeps the historical mount, byte-for-byte.
_R2_MODE = storage.r2_enabled()
if not _R2_MODE:
    app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))


# ---------------- media storage (Build U) ----------------
# Short-lived cache of R2 existence/read URLs for the /uploads route:
# media URLs are hot (every avatar + photo in a feed), and a HEAD per
# render would tax every page. Misses (ghosts) are cached too.
_uploads_cache = {}
_UPLOADS_CACHE_TTL = 120


def _uploads_r2_url(key):
    """The read URL for key when the object lives in R2, else None."""
    now = time.time()
    hit = _uploads_cache.get(key)
    if hit and now - hit[0] < _UPLOADS_CACHE_TTL:
        return hit[1]
    url = None
    try:
        if storage.exists(key):
            url = storage.read_url(key)
    except storage.StorageError:
        url = None
    _uploads_cache[key] = (now, url)
    return url


if _R2_MODE:
    @app.get("/uploads/{key:path}")
    async def uploads_serve(key: str):
        if not key or any(p in ("", ".", "..") for p in key.split("/")):
            raise HTTPException(status_code=404)
        # Build LOCKED CONTENT: locked/ keys are NEVER public — they
        # are served only by the /locked/... gate routes after an
        # entitlement check, so this route refuses them outright.
        if key.startswith("locked/"):
            raise HTTPException(status_code=404)
        disk = os.path.join(UPLOAD_DIR, key)
        if os.path.isfile(disk):
            return FileResponse(disk)
        url = _uploads_r2_url(key)
        if url:
            return RedirectResponse(url, status_code=302)
        raise HTTPException(status_code=404)


def fancy_time(value):
    """Render a stored UTC ISO timestamp as friendly text."""
    if not value:
        return ""
    dt = None
    if isinstance(value, datetime):
        dt = value
    else:
        v = str(value).strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.strptime(v, fmt)
                break
            except ValueError:
                pass
        if dt is None:
            try:
                dt = datetime.fromisoformat(v)
            except Exception:
                return value
    return dt.strftime("%b %-d, %Y \u00b7 %-I:%M %p")


templates.env.filters["fancy_time"] = fancy_time


# ---------------- DB ----------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def now_iso():
    return datetime.now(timezone.utc).isoformat()


# ---------------- Chicago time (daily challenge + streaks) ----------------
# All stored timestamps are UTC, but the Daily Challenge runs on the
# user's home days: America/Chicago. zoneinfo is stdlib; when tzdata
# is present (verified in the venv) it handles CDT/CST exactly. The
# manual fallback below applies US DST rules (2nd Sunday of March to
# 1st Sunday of November = CDT, UTC-5; otherwise CST, UTC-6) so the
# helpers still work on a host without tzdata.
try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    CHICAGO_TZ = _ZoneInfo("America/Chicago")
except Exception:  # pragma: no cover - tzdata present in practice
    CHICAGO_TZ = None


def _nth_weekday(year, month, weekday, n):
    """The date of the n-th `weekday` (Mon=0) of a month."""
    d = date(year, month, 1)
    delta = (weekday - d.weekday()) % 7
    return d + timedelta(days=delta + 7 * (n - 1))


def _chicago_now():
    if CHICAGO_TZ is not None:
        return datetime.now(CHICAGO_TZ)
    now_utc = datetime.now(timezone.utc)
    dst_start = _nth_weekday(now_utc.year, 3, 6, 2)   # 2nd Sun of Mar
    dst_end = _nth_weekday(now_utc.year, 11, 6, 1)    # 1st Sun of Nov
    in_dst = dst_start <= now_utc.date() < dst_end
    off = timedelta(hours=-5 if in_dst else -6)
    return (now_utc + off).replace(tzinfo=timezone(off))


def _today_chicago():
    """Today's calendar date in America/Chicago."""
    return _chicago_now().date()


def _today_str():
    """Today's Chicago date as an ISO string (YYYY-MM-DD)."""
    return _today_chicago().isoformat()


def _day_ordinal(day=None):
    """Ordinal of a Chicago day (defaults to today) — the prompt pick."""
    return (day or _today_chicago()).toordinal()


# ---------------- welcome topics + daily prompts ----------------
TOPICS = [("music", "Music 🎵"), ("cars", "Cars 🚗"),
          ("comedy", "Comedy 😂"), ("faith", "Faith 🙏"),
          ("sports", "Sports 🏈"), ("food", "Food 🍗"),
          ("fashion", "Fashion 👟"), ("gaming", "Gaming 🎮"),
          ("money", "Money 💰"), ("hometown", "Hometown Life 🏡")]
TOPIC_KEYS = [k for k, _ in TOPICS]
TOPIC_LABELS = dict(TOPICS)

DAILY_PROMPTS = [
    "Show your ride",
    "Your Sunday plate",
    "View from your porch",
    "Your workspace",
    "Something red",
    "Your hometown in one shot",
    "Your best friend on four legs",
    "Fresh kicks check",
    "Your people — who holds you down?",
    "What's for breakfast?",
    "Tonight's sunset",
    "Tools of your trade",
    "Your garden or your yard",
    "Throwback — an old favorite photo",
    "Your favorite corner of home",
    "Your morning view",
    "What's cooking?",
    "Your ride-or-die song spot",
    "Small-town gold — a local gem",
    "Your workspace glow-up",
    "Game day energy",
]


def daily_prompt(day=None):
    """The one Daily Challenge prompt for a Chicago day (defaults to
    today). Same result for everyone all Chicago day."""
    d = day or _today_chicago()
    return DAILY_PROMPTS[d.toordinal() % len(DAILY_PROMPTS)]


# ---------------- activity days + streaks ----------------
def record_activity(uid):
    """Mark uid active on today's Chicago day (idempotent per day).
    Opens its own connection so callers never thread one through."""
    if not uid:
        return
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO user_activity_days(user_id, day) "
        "VALUES(?,?)", (uid, _today_str()))
    conn.commit()
    conn.close()


def current_streak(uid):
    """Consecutive active Chicago days ending today — or yesterday
    when today hasn't happened yet (the streak is still alive)."""
    if not uid:
        return 0
    conn = db()
    rows = conn.execute(
        "SELECT day FROM user_activity_days WHERE user_id=?",
        (uid,)).fetchall()
    conn.close()
    days = {r["day"] for r in rows}
    today = _today_chicago()
    if today.isoformat() in days:
        anchor = today
    elif (today - timedelta(days=1)).isoformat() in days:
        anchor = today - timedelta(days=1)
    else:
        return 0
    n = 0
    cur = anchor
    while cur.isoformat() in days:
        n += 1
        cur -= timedelta(days=1)
    return n


# ---------------- hashtags ----------------
# Parsed from post bodies at create time into post_hashtags (see
# init_db for the table + the one-time video backfill). A tag is '#'
# followed by 1-50 of [A-Za-z0-9_]; stored normalized: lowercase, no '#'.
HASHTAG_RE = re.compile(r"#([A-Za-z0-9_]{1,50})")
# Bare-URL pattern, defined HERE (above its first use): parse_hashtags
# blanks URLs out, and init_db's backfill calls parse_hashtags while
# the module is still importing — a definition further down the file
# would not exist yet on a populated DB.
_URL_RE = re.compile(r"https?://[^\s<>\"']+")


def parse_hashtags(body):
    """Normalized hashtags from a post body, de-duplicated in
    first-seen order. Build TAGS: bare URLs are blanked out first —
    a '#fragment' inside a link is part of the link, never a hashtag
    (the renderer, linkify_body, has always treated it that way; the
    indexer, trends and tag pages now share this one definition)."""
    out = []
    for m in HASHTAG_RE.finditer(_URL_RE.sub(" ", body or "")):
        t = m.group(1).lower()
        if t not in out:
            out.append(t)
    return out


# ---------------- invite codes ----------------
# Every user carries a short, shareable invite code (users.invite_code).
# The alphabet drops look-alike characters (I/L/O/0/1) so a code read
# aloud or typed from a screenshot can't be mis-copied.
INVITE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"


def gen_invite_code(conn):
    """A fresh 8-char invite code not used by any user row visible on
    `conn` (pass the caller's connection so uncommitted backfill /
    insert work still collides correctly)."""
    while True:
        code = "".join(secrets.choice(INVITE_ALPHABET) for _ in range(8))
        row = conn.execute(
            "SELECT 1 FROM users WHERE invite_code=?", (code,)).fetchone()
        if not row:
            return code


def init_db():
    conn = db()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL, email TEXT NOT NULL UNIQUE, pw_hash TEXT NOT NULL,
        bio TEXT DEFAULT '', avatar TEXT DEFAULT '',
        last_seen TEXT, last_seen_at TEXT,
        status_mode TEXT DEFAULT 'auto', inbox_status INTEGER DEFAULT 1,
        created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS sessions (
        token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS posts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, page_id INTEGER,
        kind TEXT NOT NULL DEFAULT 'text',
        body TEXT DEFAULT '', feeling TEXT DEFAULT '',
        link_url TEXT DEFAULT '', video_url TEXT DEFAULT '',
        media TEXT DEFAULT '', created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        body TEXT NOT NULL, created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS likes (
        post_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(post_id, user_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS friendships (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, friend_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
        UNIQUE(user_id, friend_id))""")
    # follows: the one-way graph (independent of friendship). A follow
    # is an attention subscription only — it never unlocks a private
    # profile (can_view_post doesn't consult it); it exists so people
    # can keep up with, and be found by, people they aren't friends
    # with, and it feeds live-invite eligibility ("friends & following").
    c.execute("""CREATE TABLE IF NOT EXISTS follows (
        follower_id INTEGER NOT NULL, followee_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(follower_id, followee_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER NOT NULL, receiver_id INTEGER NOT NULL,
        body TEXT NOT NULL, is_read INTEGER DEFAULT 0, created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS message_hidden (
        message_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(message_id, user_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS thread_deletes (
        user_id INTEGER NOT NULL, other_id INTEGER NOT NULL,
        hidden_before TEXT NOT NULL, PRIMARY KEY(user_id, other_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS pages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        owner_id INTEGER NOT NULL, name TEXT NOT NULL,
        ptype TEXT NOT NULL DEFAULT 'community',
        description TEXT DEFAULT '', cover TEXT DEFAULT '',
        created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS page_members (
        page_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(page_id, user_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS taps (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tapper_id INTEGER NOT NULL, tapped_id INTEGER NOT NULL,
        created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS notifications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, kind TEXT NOT NULL,
        text TEXT DEFAULT '', link TEXT DEFAULT '',
        is_read INTEGER DEFAULT 0, created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS moderation_flags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        item_type TEXT NOT NULL, item_id INTEGER NOT NULL,
        author_id INTEGER NOT NULL, reasons TEXT DEFAULT '',
        excerpt TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS owner_sessions (
        token TEXT PRIMARY KEY, created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS hidden_posts (
        user_id INTEGER NOT NULL, post_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(user_id, post_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS profile_views (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        viewer_id INTEGER NOT NULL, viewed_id INTEGER NOT NULL,
        viewed_at TEXT NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pviews_pair "
              "ON profile_views(viewer_id, viewed_id, viewed_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pviews_viewed "
              "ON profile_views(viewed_id, viewed_at)")
    # --- one-go feature pass migrations (safe on existing DBs) ---
    _post_cols = [r["name"] for r in c.execute("PRAGMA table_info(posts)").fetchall()]
    if "privacy" not in _post_cols:
        c.execute("ALTER TABLE posts ADD COLUMN privacy TEXT DEFAULT 'public'")
    if "pinned" not in _post_cols:
        c.execute("ALTER TABLE posts ADD COLUMN pinned INTEGER DEFAULT 0")
    if "shared_post_id" not in _post_cols:
        c.execute("ALTER TABLE posts ADD COLUMN shared_post_id INTEGER")
    if "page_id" not in _post_cols:
        c.execute("ALTER TABLE posts ADD COLUMN page_id INTEGER")
    # --- post music: a YouTube song attached to the post (safe) ---
    for _mc in ("music_video_id", "music_title", "music_artist",
                "music_thumb"):
        if _mc not in _post_cols:
            c.execute(f"ALTER TABLE posts ADD COLUMN {_mc} TEXT DEFAULT ''")
    if "music_start" not in _post_cols:
        c.execute("ALTER TABLE posts ADD COLUMN music_start INTEGER DEFAULT 0")
    # --- story replies/reactions ride the messages table (safe) ---
    # A message linked to a story carries the story's id plus the
    # uploads-relative path of a SHARED clip copy (uploads/story_replies/)
    # made when the first reply/reaction to that story lands, so the
    # thread can show the clip forever — even after the story itself
    # expires and is pruned. Plain messages keep story_id NULL.
    _msg_cols = [r["name"] for r in
                 c.execute("PRAGMA table_info(messages)").fetchall()]
    if "story_id" not in _msg_cols:
        c.execute("ALTER TABLE messages ADD COLUMN story_id INTEGER")
    if "story_media" not in _msg_cols:
        c.execute("ALTER TABLE messages ADD COLUMN story_media TEXT DEFAULT ''")
    # --- tap acknowledgments (safe) ---
    # A tap is just "I'm trying to get your attention": once the
    # recipient has seen/answered it (visits the tapper's profile or
    # taps them back — the same moments that delete the tap's bell
    # notification), the pair's taps stop counting toward the number
    # shown beside the Tap button. Taps after the ack count again.
    c.execute("""CREATE TABLE IF NOT EXISTS tap_acks (
        tapper_id INTEGER NOT NULL, tapped_id INTEGER NOT NULL,
        acked_at TEXT NOT NULL, PRIMARY KEY(tapper_id, tapped_id))""")
    # --- stories (safe on existing DBs) ---
    c.execute("""CREATE TABLE IF NOT EXISTS stories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, media TEXT NOT NULL DEFAULT '',
        caption TEXT DEFAULT '', created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS story_views (
        story_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        viewed_at TEXT NOT NULL, UNIQUE(story_id, user_id))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_stories_user_exp "
              "ON stories(user_id, expires_at)")
    # --- stories v2: overlay text, music, per-story lifetime (safe) ---
    # expires_at already exists on any DB this code created; the column
    # add + backfill below only fire on a DB that predates it.
    _story_cols = [r["name"] for r in
                   c.execute("PRAGMA table_info(stories)").fetchall()]
    if "expires_at" not in _story_cols:
        c.execute("ALTER TABLE stories ADD COLUMN expires_at TEXT DEFAULT ''")
        _story_cols.append("expires_at")
    for _col, _ddl in (
            ("overlay_text", "ALTER TABLE stories ADD COLUMN overlay_text TEXT DEFAULT ''"),
            ("overlay_color", "ALTER TABLE stories ADD COLUMN overlay_color TEXT DEFAULT ''"),
            ("overlay_pos", "ALTER TABLE stories ADD COLUMN overlay_pos TEXT DEFAULT 'bottom'"),
            ("music_kind", "ALTER TABLE stories ADD COLUMN music_kind TEXT"),
            ("music_ref", "ALTER TABLE stories ADD COLUMN music_ref TEXT DEFAULT ''"),
            ("music_title", "ALTER TABLE stories ADD COLUMN music_title TEXT DEFAULT ''"),
            ("music_artist", "ALTER TABLE stories ADD COLUMN music_artist TEXT DEFAULT ''"),
            ("music_start", "ALTER TABLE stories ADD COLUMN music_start INTEGER DEFAULT 0"),
            ("music_len", "ALTER TABLE stories ADD COLUMN music_len INTEGER DEFAULT 15"),
            ("music_volume", "ALTER TABLE stories ADD COLUMN music_volume INTEGER DEFAULT 80"),
            ("music_mute_orig", "ALTER TABLE stories ADD COLUMN music_mute_orig INTEGER DEFAULT 0"),
            ("music_thumb", "ALTER TABLE stories ADD COLUMN music_thumb TEXT DEFAULT ''")):
        if _col not in _story_cols:
            c.execute(_ddl)
    # --- story editor (Build STORY EDITOR): the visual editor's
    # element list + per-story audience ride two more columns.
    for _col, _ddl in (
            ("edit_json", "ALTER TABLE stories ADD COLUMN edit_json TEXT DEFAULT ''"),
            ("audience", "ALTER TABLE stories ADD COLUMN audience TEXT DEFAULT 'friends'")):
        if _col not in _story_cols:
            c.execute(_ddl)
            _story_cols.append(_col)
    # Backfill: any story with no expiry gets created_at + 24h (the old
    # fixed TTL), computed in Python so ISO offsets parse exactly.
    for _r in c.execute(
            "SELECT id, created_at FROM stories "
            "WHERE expires_at IS NULL OR expires_at=''").fetchall():
        try:
            _exp = (datetime.fromisoformat(_r["created_at"])
                    + timedelta(hours=24)).isoformat()
        except Exception:
            _exp = now_iso()
        c.execute("UPDATE stories SET expires_at=? WHERE id=?",
                  (_exp, _r["id"]))
    # --- legacy story-music tables (kept, unused) ---
    # The preset catalog + MP3-upload era left these two tables behind.
    # They are created only so old DBs keep their shape; nothing seeds
    # or reads them anymore — story/post music is YouTube-only now.
    c.execute("""CREATE TABLE IF NOT EXISTS music_tracks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL, artist TEXT NOT NULL DEFAULT '',
        genre TEXT NOT NULL DEFAULT '', file_path TEXT NOT NULL DEFAULT '',
        license_note TEXT NOT NULL DEFAULT '', attribution TEXT NOT NULL DEFAULT '',
        use_count INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS story_music_uploads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, file_path TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)""")
    # --- reactions: likes.kind (safe on existing DBs) ---
    _like_cols = [r["name"] for r in c.execute("PRAGMA table_info(likes)").fetchall()]
    if "kind" not in _like_cols:
        c.execute("ALTER TABLE likes ADD COLUMN kind TEXT DEFAULT 'like'")
    # --- verification system (safe on existing DBs) ---
    c.execute("""CREATE TABLE IF NOT EXISTS verification_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL, target_id INTEGER NOT NULL,
        requester_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL, reviewed_at TEXT, note TEXT DEFAULT '')""")
    _user_cols = [r["name"] for r in c.execute("PRAGMA table_info(users)").fetchall()]
    if "is_verified" not in _user_cols:
        c.execute("ALTER TABLE users ADD COLUMN is_verified INTEGER DEFAULT 0")
    if "verification_status" not in _user_cols:
        c.execute("ALTER TABLE users ADD COLUMN verification_status TEXT DEFAULT 'none'")
    _page_cols = [r["name"] for r in c.execute("PRAGMA table_info(pages)").fetchall()]
    if "is_verified" not in _page_cols:
        c.execute("ALTER TABLE pages ADD COLUMN is_verified INTEGER DEFAULT 0")
    if "verification_status" not in _page_cols:
        c.execute("ALTER TABLE pages ADD COLUMN verification_status TEXT DEFAULT 'none'")
    # backfill any NULLs left on rows that predated the columns
    c.execute("UPDATE users SET is_verified=0 WHERE is_verified IS NULL")
    c.execute("UPDATE users SET verification_status='none' WHERE verification_status IS NULL")
    c.execute("UPDATE pages SET is_verified=0 WHERE is_verified IS NULL")
    c.execute("UPDATE pages SET verification_status='none' WHERE verification_status IS NULL")
    # --- Verified User / Verified Vendor stickers (Ken, Oct 30 2026):
    # two more owner-granted badges beside the name. is_verified_user /
    # is_verified_vendor are the on/off flags; vuser_status /
    # vvendor_status track the settings request lifecycle
    # ('none'|'pending'|'approved'|'declined'). Requests ride the SAME
    # verification_requests table with kind 'vuser' / 'vvendor'
    # (target_id = the user's id), so the owner queue below is the
    # single door for every badge on HERE.
    if "is_verified_user" not in _user_cols:
        c.execute("ALTER TABLE users ADD COLUMN is_verified_user INTEGER DEFAULT 0")
    if "vuser_status" not in _user_cols:
        c.execute("ALTER TABLE users ADD COLUMN vuser_status TEXT DEFAULT 'none'")
    if "is_verified_vendor" not in _user_cols:
        c.execute("ALTER TABLE users ADD COLUMN is_verified_vendor INTEGER DEFAULT 0")
    if "vvendor_status" not in _user_cols:
        c.execute("ALTER TABLE users ADD COLUMN vvendor_status TEXT DEFAULT 'none'")
    c.execute("UPDATE users SET is_verified_user=0 WHERE is_verified_user IS NULL")
    c.execute("UPDATE users SET vuser_status='none' WHERE vuser_status IS NULL")
    c.execute("UPDATE users SET is_verified_vendor=0 WHERE is_verified_vendor IS NULL")
    c.execute("UPDATE users SET vvendor_status='none' WHERE vvendor_status IS NULL")
    # --- MySpace-style full profile backgrounds (safe on existing DBs) ---
    if "background" not in _user_cols:
        c.execute("ALTER TABLE users ADD COLUMN background TEXT DEFAULT ''")
    c.execute("UPDATE users SET background='' WHERE background IS NULL")
    # --- presence: lightning-bolt status (safe on existing DBs) ---
    # last_seen_at is the throttled heartbeat timestamp (written at most
    # every ~30s per user); status_mode is the user's chosen display mode
    # ('auto'|'online'|'away'|'offline'|'hidden'); inbox_status toggles
    # whether the bolt may show in Messages at all.
    if "last_seen_at" not in _user_cols:
        c.execute("ALTER TABLE users ADD COLUMN last_seen_at TEXT")
    if "status_mode" not in _user_cols:
        c.execute("ALTER TABLE users ADD COLUMN status_mode TEXT DEFAULT 'auto'")
    if "inbox_status" not in _user_cols:
        c.execute("ALTER TABLE users ADD COLUMN inbox_status INTEGER DEFAULT 1")
    c.execute("UPDATE users SET status_mode='auto' WHERE status_mode IS NULL")
    c.execute("UPDATE users SET inbox_status=1 WHERE inbox_status IS NULL")
    # --- "Your Spot": per-profile name + theme (safe on existing DBs) ---
    # spot_name is the owner's own title for their profile ("Robert's
    # House", "The Trap"); the three style columns drive the CSS vars
    # emitted on /user/{id} pages only. NULL = not set = default look.
    if "spot_name" not in _user_cols:
        c.execute("ALTER TABLE users ADD COLUMN spot_name TEXT")
    if "spot_text_color" not in _user_cols:
        c.execute("ALTER TABLE users ADD COLUMN spot_text_color TEXT")
    if "spot_card_color" not in _user_cols:
        c.execute("ALTER TABLE users ADD COLUMN spot_card_color TEXT")
    if "spot_card_opacity" not in _user_cols:
        c.execute("ALTER TABLE users ADD COLUMN spot_card_opacity INTEGER")
    # --- Box Transparency: per-viewer box solidity (safe on existing DBs) ---
    if "box_opacity" not in _user_cols:
        c.execute("ALTER TABLE users ADD COLUMN box_opacity INTEGER DEFAULT 100")
    # --- Build GLASS: whether the viewer has ever saved the unified
    # Glass transparency slider. box_opacity alone can't say (its
    # column default backfills 100 for everyone), and 100 used to BE
    # the untouched default — so "never chose" needs its own flag:
    # glass_set=0 -> the default glass look; glass_set=1 -> the saved
    # box_opacity drives (100 then means solid BY CHOICE). ---
    if "glass_set" not in _user_cols:
        c.execute("ALTER TABLE users ADD COLUMN glass_set INTEGER DEFAULT 0")
    # --- Build LOOKS: the look scheme + surface/accent colors.
    # look_scheme: 'glass' (default for everybody) / 'solid' / 'mix'.
    # look_surface / look_accent: #rrggbb or NULL (unset = theme
    # default). One-time backfill: anyone who had already CHOSEN a
    # solidity under Build GLASS (glass_set=1) was effectively living
    # in Mix, so their scheme starts there; everyone else is Glass.
    # Build CLEAR deliberately does NOT revisit that backfill: from
    # here on only the settings doors assign schemes, and nobody is
    # mass-moved between Glass and Mix after the fact. ---
    if "look_scheme" not in _user_cols:
        c.execute("ALTER TABLE users ADD COLUMN look_scheme TEXT "
                  "DEFAULT 'glass'")
        c.execute("UPDATE users SET look_scheme='mix' WHERE glass_set=1")
    if "look_surface" not in _user_cols:
        c.execute("ALTER TABLE users ADD COLUMN look_surface TEXT")
    if "look_accent" not in _user_cols:
        c.execute("ALTER TABLE users ADD COLUMN look_accent TEXT")
    # --- Build ICON STYLES: the viewer's icon finish ('emoji'
    # default = the classic look; solid/glass/jelly/chrome/gold/
    # transparent swap every chrome icon for custom vector art). ---
    if "icon_style" not in _user_cols:
        c.execute("ALTER TABLE users ADD COLUMN icon_style TEXT "
                  "DEFAULT 'emoji'")
    # --- hide-from-specific-people + post tags (safe on existing DBs) ---
    c.execute("""CREATE TABLE IF NOT EXISTS post_hidden_from (
        post_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(post_id, user_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS post_tags (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER NOT NULL, user_id INTEGER, page_id INTEGER,
        created_at TEXT NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_post_tags_post "
              "ON post_tags(post_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_hidden_from_post "
              "ON post_hidden_from(post_id)")
    # --- voice/video calls (safe on existing DBs) ---
    # Media flows phone-to-phone via WebRTC; these tables only carry the
    # call state machine and the small signaling messages (SDP/ICE) the
    # two browsers exchange while setting a call up.
    c.execute("""CREATE TABLE IF NOT EXISTS calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        caller_id INTEGER NOT NULL, callee_id INTEGER NOT NULL,
        kind TEXT NOT NULL DEFAULT 'voice',
        status TEXT NOT NULL DEFAULT 'ringing',
        created_at TEXT NOT NULL, answered_at TEXT, ended_at TEXT,
        decline_note TEXT DEFAULT '')""")
    # decline-with-a-message: the callee's reply text rides on the call
    # row so the caller's overlay can show it the moment the call drops.
    _call_cols = [r["name"] for r in c.execute("PRAGMA table_info(calls)").fetchall()]
    if "decline_note" not in _call_cols:
        c.execute("ALTER TABLE calls ADD COLUMN decline_note TEXT DEFAULT ''")
    c.execute("""CREATE TABLE IF NOT EXISTS call_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        call_id INTEGER NOT NULL, sender_id INTEGER NOT NULL,
        type TEXT NOT NULL, payload TEXT DEFAULT '',
        created_at TEXT NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_callee "
              "ON calls(callee_id, status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_calls_caller "
              "ON calls(caller_id, status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_call_signals_call "
              "ON call_signals(call_id, id)")
    # --- call requests (Ken's rule: only friends can be called directly;
    # anyone else must send a request the other person approves first) ---
    c.execute("""CREATE TABLE IF NOT EXISTS call_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        requester_id INTEGER NOT NULL, target_id INTEGER NOT NULL,
        kind TEXT NOT NULL DEFAULT 'voice',
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL, responded_at TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_callreq_target "
              "ON call_requests(target_id, status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_callreq_pair "
              "ON call_requests(requester_id, target_id, status)")
    # --- go live (safe on existing DBs) ---
    # Same architecture as calls: media flows phone-to-phone via WebRTC,
    # these tables only carry room state, chat, presence, join requests,
    # and the small signaling messages (SDP/ICE) browsers exchange.
    c.execute("""CREATE TABLE IF NOT EXISTS live_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        host_id INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'live',
        created_at TEXT NOT NULL, ended_at TEXT,
        host_seen_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS live_signals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        live_id INTEGER NOT NULL, from_id INTEGER NOT NULL,
        to_id INTEGER NOT NULL, kind TEXT NOT NULL,
        payload TEXT DEFAULT '', created_at TEXT NOT NULL,
        consumed INTEGER NOT NULL DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS live_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        live_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        body TEXT NOT NULL, created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS live_viewers (
        live_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        joined_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
        left_at TEXT,
        PRIMARY KEY (live_id, user_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS live_join_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        live_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL, decided_at TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_live_sessions_host "
              "ON live_sessions(host_id, status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_live_signals_room "
              "ON live_signals(live_id, to_id, id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_live_comments_room "
              "ON live_comments(live_id, id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_live_join_room "
              "ON live_join_requests(live_id, status)")
    # --- go live panel upgrade (safe on existing DBs) ---
    # TikTok-style panel: the host plus up to 15 guests on camera,
    # per-live admins appointed by the host, room bans, and likes.
    # live_members is the panel roster EXCLUDING the host (the host is
    # always panelist #1 via live_sessions.host_id); panel size is
    # therefore 1 + (live_members rows with left_at NULL).
    _live_sess_cols = [r["name"] for r in
                       c.execute("PRAGMA table_info(live_sessions)")
                       .fetchall()]
    if "like_count" not in _live_sess_cols:
        c.execute("ALTER TABLE live_sessions ADD COLUMN like_count "
                  "INTEGER DEFAULT 0")
    if "panel_order" not in _live_sess_cols:
        c.execute("ALTER TABLE live_sessions ADD COLUMN panel_order "
                  "TEXT")
    _jr_cols = [r["name"] for r in
                c.execute("PRAGMA table_info(live_join_requests)")
                .fetchall()]
    if "kind" not in _jr_cols:
        # 'request' = a watcher asks to join; 'invite' = the host or
        # an admin invites a watcher. Old rows are all requests.
        c.execute("ALTER TABLE live_join_requests ADD COLUMN kind "
                  "TEXT DEFAULT 'request'")
    if "inviter_id" not in _jr_cols:
        # For kind='invite' rows, user_id is the INVITEE (the column
        # keeps its "the person this row is about" meaning) and the
        # inviter rides here; NULL on plain join requests.
        c.execute("ALTER TABLE live_join_requests ADD COLUMN "
                  "inviter_id INTEGER")
    if "spot_idx" not in _jr_cols:
        # Spots (Oct 30): the cast spot an applicant applied
        # for (index into the live's spot_names), NULL = any.
        c.execute("ALTER TABLE live_join_requests ADD COLUMN "
                  "spot_idx INTEGER")
    if "suggested_by" not in _jr_cols:
        # An admin's ⭐ suggestion on this application — the
        # host still decides; this just carries the advice.
        c.execute("ALTER TABLE live_join_requests ADD COLUMN "
                  "suggested_by INTEGER")
    c.execute("""CREATE TABLE IF NOT EXISTS live_members (
        live_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        muted INTEGER DEFAULT 0,
        joined_at TEXT NOT NULL, left_at TEXT,
        PRIMARY KEY (live_id, user_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS live_admins (
        live_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        appointed_by INTEGER, created_at TEXT NOT NULL,
        revoked_at TEXT,
        PRIMARY KEY (live_id, user_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS live_bans (
        live_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        by_id INTEGER, created_at TEXT NOT NULL,
        PRIMARY KEY (live_id, user_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS live_like_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        live_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_live_members_room "
              "ON live_members(live_id, left_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_live_admins_room "
              "ON live_admins(live_id, revoked_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_live_bans_room "
              "ON live_bans(live_id, user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_live_likes_room "
              "ON live_like_events(live_id, id)")
    # --- Build W3: self-reported camera health (video_ok) ---
    # A panel member's client verifies its own outgoing video
    # track (exists + readyState 'live') and reports the result
    # on its heartbeat; peers render video_ok=false members as an
    # avatar tile ("📷 Camera off") instead of a black box. The
    # host is never a live_members row, so the host's flag lives
    # on live_sessions. NULL = never reported (a legacy client)
    # and reads as True everywhere — old clients never look
    # camera-off.
    _lm_cols = [r["name"] for r in
                c.execute("PRAGMA table_info(live_members)")
                .fetchall()]
    if "video_ok" not in _lm_cols:
        c.execute("ALTER TABLE live_members ADD COLUMN video_ok "
                  "INTEGER")
    if "host_video_ok" not in _live_sess_cols:
        c.execute("ALTER TABLE live_sessions ADD COLUMN "
                  "host_video_ok INTEGER")
    # Talk-show build (Ken, Oct 28): a member's RANK in the room
    # ('guest' steps up to 'regular' when the host/admin says
    # they earned it; 'admin' is the appointment in live_admins,
    # not a rank value) and the room's SPOTLIGHT — the panelist
    # whose tile takes the big stage spot (NULL = nobody).
    if "rank" not in _lm_cols:
        c.execute("ALTER TABLE live_members ADD COLUMN rank "
                  "TEXT DEFAULT 'guest'")
    if "spotlight_id" not in _live_sess_cols:
        c.execute("ALTER TABLE live_sessions ADD COLUMN "
                  "spotlight_id INTEGER")
    # Ken's 2-4-7 build (Oct 28): the Lobby (a live that never
    # auto-ends), live selling (a listing can ride a live), and
    # Original 100 founding seats.
    if "is_lobby" not in _live_sess_cols:
        c.execute("ALTER TABLE live_sessions ADD COLUMN "
                  "is_lobby INTEGER DEFAULT 0")
    # --- Build X: TikTok-style panel grid state ---
    # panel_open: the host has opened the panel grid (or the first
    # guest was approved onto it — _live_add_member opens it).
    # While open, the stage renders the equal-tile grid with empty
    # "+ Request" squares; closed + no guests = solo fullscreen.
    # panel_boxes: how many GUEST squares the host allows (1..8,
    # default 5 — host + 5, the reference layout). Approving a
    # join request at the boxes limit is refused; the hard cap
    # LIVE_PANEL_MAX still bounds the panel absolutely.
    if "panel_open" not in _live_sess_cols:
        c.execute("ALTER TABLE live_sessions ADD COLUMN "
                  "panel_open INTEGER DEFAULT 0")
    if "panel_boxes" not in _live_sess_cols:
        c.execute("ALTER TABLE live_sessions ADD COLUMN "
                  "panel_boxes INTEGER DEFAULT 5")
    c.execute("UPDATE live_sessions SET like_count=0 "
              "WHERE like_count IS NULL")
    # Backfill pre-upgrade rooms: an approved join request WAS the
    # on-camera guest — turn those rows into panel members and seed
    # panel_order so a room live during the upgrade survives intact.
    for _lr in c.execute(
            "SELECT id, host_id FROM live_sessions "
            "WHERE panel_order IS NULL").fetchall():
        _approved = c.execute(
            "SELECT user_id, created_at FROM live_join_requests "
            "WHERE live_id=? AND status='approved' ORDER BY id ASC",
            (_lr["id"],)).fetchall()
        _order = [_lr["host_id"]]
        for _a in _approved:
            if _a["user_id"] == _lr["host_id"] \
                    or _a["user_id"] in _order:
                continue
            c.execute(
                "INSERT OR IGNORE INTO live_members(live_id, "
                "user_id, muted, joined_at, left_at) "
                "VALUES(?,?,0,?,NULL)",
                (_lr["id"], _a["user_id"], _a["created_at"]))
            _order.append(_a["user_id"])
        c.execute("UPDATE live_sessions SET panel_order=? WHERE id=?",
                  (json.dumps(_order), _lr["id"]))
    # --- phone push notifications (Web Push) ---
    # app_settings is a tiny key/value store; it holds the VAPID private
    # key (PEM) so the server's push identity survives restarts on the
    # persistent disk. push_subscriptions holds one row per subscribed
    # browser/device; endpoint is the push-service URL (unique).
    c.execute("""CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS push_subscriptions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        endpoint TEXT NOT NULL UNIQUE,
        p256dh TEXT NOT NULL, auth TEXT NOT NULL,
        user_agent TEXT DEFAULT '',
        created_at TEXT NOT NULL, last_seen TEXT NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pushsubs_user "
              "ON push_subscriptions(user_id)")
    # --- comment controls: replies + per-post comment blocks ---
    # comments.parent_id ties a reply to its TOP-LEVEL comment (replies
    # never nest deeper than one level). comments.reply_to_id remembers
    # the exact comment the author tapped Reply on, so the UI can prefix
    # a linked "@Name" when that target was itself a reply
    # (Facebook-style flattening).
    _comment_cols = [r["name"] for r in
                     c.execute("PRAGMA table_info(comments)").fetchall()]
    if "parent_id" not in _comment_cols:
        c.execute("ALTER TABLE comments ADD COLUMN parent_id INTEGER")
    if "reply_to_id" not in _comment_cols:
        c.execute("ALTER TABLE comments ADD COLUMN reply_to_id INTEGER")
    c.execute("CREATE INDEX IF NOT EXISTS idx_comments_parent "
              "ON comments(parent_id)")
    # Per-post comment blocks: a post's author can stop ONE person from
    # adding new comments/replies on THAT post only. Their existing
    # comments stay put (the author deletes those separately if wanted)
    # and the block applies nowhere else on the site.
    c.execute("""CREATE TABLE IF NOT EXISTS post_comment_blocks (
        post_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, UNIQUE(post_id, user_id))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cblocks_post "
              "ON post_comment_blocks(post_id)")
    # --- privacy + notification hygiene (safe on existing DBs) ---
    # mutes: one-way silence. The muter gets no notifications/push from
    # the muted user and the muted user's posts leave the muter's home
    # feed; friendship and messaging are untouched, and the muted user
    # is never told.
    c.execute("""CREATE TABLE IF NOT EXISTS mutes (
        muter_id INTEGER NOT NULL, muted_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(muter_id, muted_id))""")
    # blocks: one row (blocker -> blocked) severs the pair completely
    # and is enforced BOTH ways (see blocked_either_way()).
    c.execute("""CREATE TABLE IF NOT EXISTS blocks (
        blocker_id INTEGER NOT NULL, blocked_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(blocker_id, blocked_id))""")
    _user_cols_now = [r["name"] for r in
                      c.execute("PRAGMA table_info(users)").fetchall()]
    if "private_profile" not in _user_cols_now:
        c.execute("ALTER TABLE users ADD COLUMN private_profile "
                  "INTEGER NOT NULL DEFAULT 0")
    # --- invites: per-user shareable code + who invited them (safe) ---
    # invite_code is backfilled for every existing user below, then a
    # UNIQUE index keeps codes one-per-person (new signups generate
    # their code at insert time in POST /signup).
    if "invite_code" not in _user_cols_now:
        c.execute("ALTER TABLE users ADD COLUMN invite_code TEXT")
    if "invited_by" not in _user_cols_now:
        c.execute("ALTER TABLE users ADD COLUMN invited_by INTEGER")
    for _urow in c.execute(
            "SELECT id FROM users WHERE invite_code IS NULL "
            "OR invite_code=''").fetchall():
        c.execute("UPDATE users SET invite_code=? WHERE id=?",
                  (gen_invite_code(conn), _urow["id"]))
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_invite_code "
              "ON users(invite_code)")
    # --- My Views: hashtags, learning affinity, view events (safe) ---
    # post_hashtags: one row per (post, tag) parsed from the post body
    # at create time (hook in create_post_from_form covers EVERY new
    # post, any kind — the wall can use them later; My Views filters
    # and ranks on them now).
    c.execute("""CREATE TABLE IF NOT EXISTS post_hashtags (
        post_id INTEGER NOT NULL, tag TEXT NOT NULL,
        PRIMARY KEY(post_id, tag))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_htags_tag "
              "ON post_hashtags(tag)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_htags_post "
              "ON post_hashtags(post_id)")
    # user_affinity: the My Views learning scores — one row per
    # (user, kind 'tag'|'author', key); score is nudged by watch /
    # engage / interest events and read (clamped) by the feed ranker.
    c.execute("""CREATE TABLE IF NOT EXISTS user_affinity (
        user_id INTEGER NOT NULL, kind TEXT NOT NULL, key TEXT NOT NULL,
        score REAL NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
        PRIMARY KEY(user_id, kind, key))""")
    # view_events: per-viewer per-post watch facts for My Views —
    # 'seen' (started playing; demotes the post in that viewer's feed),
    # 'completed' (>=85% watched), 'hidden' (the viewer said
    # "not interested" to THIS post; it leaves their feed entirely).
    c.execute("""CREATE TABLE IF NOT EXISTS view_events (
        user_id INTEGER NOT NULL, post_id INTEGER NOT NULL,
        kind TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(user_id, post_id, kind))""")
    # Hashtag backfill (one-time in effect): parse the bodies of
    # EXISTING video posts that have no hashtag rows yet — they predate
    # the create-time hook. Keyed on "no rows yet" so re-running is a
    # no-op for anything already parsed.
    for _bp in c.execute(
            "SELECT id, body FROM posts WHERE kind='video' AND NOT EXISTS "
            "(SELECT 1 FROM post_hashtags WHERE post_id=posts.id)"
            ).fetchall():
        for _t in parse_hashtags(_bp["body"] or ""):
            c.execute("INSERT OR IGNORE INTO post_hashtags(post_id, tag) "
                      "VALUES(?,?)", (_bp["id"], _t))
    # --- welcome flow + daily challenge (safe on existing DBs) ---
    # users.onboarded: 0 = still owes the /welcome flow. The backfill
    # runs exactly once, in the same pass that adds the column: every
    # row that exists at migration time is a pre-existing account and
    # is marked onboarded; new signups afterwards get DEFAULT 0.
    _user_cols_w = [r["name"] for r in
                    c.execute("PRAGMA table_info(users)").fetchall()]
    if "onboarded" not in _user_cols_w:
        c.execute("ALTER TABLE users ADD COLUMN onboarded "
                  "INTEGER DEFAULT 0")
        c.execute("UPDATE users SET onboarded=1")
    c.execute("""CREATE TABLE IF NOT EXISTS user_topics (
        user_id INTEGER NOT NULL, topic TEXT NOT NULL,
        PRIMARY KEY(user_id, topic))""")
    # posts.challenge_date: the Chicago day (YYYY-MM-DD) a post answered
    # the Daily Challenge on; NULL on ordinary posts.
    _post_cols_w = [r["name"] for r in
                    c.execute("PRAGMA table_info(posts)").fetchall()]
    if "challenge_date" not in _post_cols_w:
        c.execute("ALTER TABLE posts ADD COLUMN challenge_date TEXT")
    # --- Build WALL (safe on existing DBs) ---
    # users.wall_open: 1 = friends may write on your profile wall
    # (DEFAULT 1 — every existing and new account starts OPEN).
    # users.wall_approve_first: 1 = wall posts wait for your approval.
    _user_cols_wall = [r["name"] for r in
                       c.execute("PRAGMA table_info(users)").fetchall()]
    if "wall_open" not in _user_cols_wall:
        c.execute("ALTER TABLE users ADD COLUMN wall_open "
                  "INTEGER DEFAULT 1")
    if "wall_approve_first" not in _user_cols_wall:
        c.execute("ALTER TABLE users ADD COLUMN wall_approve_first "
                  "INTEGER DEFAULT 0")
    # posts.wall_user_id: the profile wall this post was written on
    # (NULL = an ordinary post on the author's own profile).
    # posts.wall_pending: 1 = an approve-first wall post still waiting
    # for the wall owner's approval — invisible to everyone but them.
    if "wall_user_id" not in _post_cols_w:
        c.execute("ALTER TABLE posts ADD COLUMN wall_user_id INTEGER")
    if "wall_pending" not in _post_cols_w:
        c.execute("ALTER TABLE posts ADD COLUMN wall_pending "
                  "INTEGER DEFAULT 0")
    # --- Build LOCKED CONTENT (posts side) ---
    # posts.lock_price: tickets a viewer pays to open this post
    # (0/NULL = not locked). posts.lock_adult: the seller's 18+
    # marker — unlocking then needs the buyer's explicit confirm.
    # A locked post's media NEVER lives under uploads/: it is stored
    # under the locked/ key prefix (LOCKED_DIR on disk, the locked/
    # prefix in R2) and served only by the /locked/post/{id} gate.
    if "lock_price" not in _post_cols_w:
        c.execute("ALTER TABLE posts ADD COLUMN lock_price "
                  "INTEGER DEFAULT 0")
    if "lock_adult" not in _post_cols_w:
        c.execute("ALTER TABLE posts ADD COLUMN lock_adult "
                  "INTEGER DEFAULT 0")
    # user_activity_days: one row per (user, Chicago day) the user did
    # something streak-worthy (visited home, posted, reacted, commented).
    c.execute("""CREATE TABLE IF NOT EXISTS user_activity_days (
        user_id INTEGER NOT NULL, day TEXT NOT NULL,
        PRIMARY KEY(user_id, day))""")
    # --- games pack (Build F): polls everywhere + the My Views
    # contest (safe on existing DBs) ---
    # Post polls: one poll per post (the post's body is the question),
    # 2-4 options, one vote row per user (a re-vote UPDATES the row,
    # so changing your pick never double-counts).
    c.execute("""CREATE TABLE IF NOT EXISTS polls (
        post_id INTEGER PRIMARY KEY)""")
    c.execute("""CREATE TABLE IF NOT EXISTS poll_options (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        post_id INTEGER NOT NULL, idx INTEGER NOT NULL,
        text TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS poll_votes (
        post_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        option_id INTEGER NOT NULL,
        PRIMARY KEY(post_id, user_id))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pollopts_post "
              "ON poll_options(post_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pollvotes_post "
              "ON poll_votes(post_id)")
    # Story polls: binary by design — exactly two choices ('a'/'b').
    # Feed trainer (This-or-Not): each user's explicit more/less
    # answers about kinds + hashtags, keyed 'kind:video' / 'tag:music'.
    c.execute("""CREATE TABLE IF NOT EXISTS user_topic_prefs (
        user_id INTEGER NOT NULL, key TEXT NOT NULL,
        pref TEXT NOT NULL, updated_at TEXT NOT NULL,
        PRIMARY KEY (user_id, key))""")
    c.execute("""CREATE TABLE IF NOT EXISTS story_polls (
        story_id INTEGER PRIMARY KEY,
        question TEXT NOT NULL, a_text TEXT NOT NULL,
        b_text TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS story_poll_votes (
        story_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        choice TEXT NOT NULL,
        PRIMARY KEY(story_id, user_id))""")
    # stories.ama: 1 = the story carries an "Ask me a question" box.
    _story_cols_f = [r["name"] for r in
                     c.execute("PRAGMA table_info(stories)").fetchall()]
    if "ama" not in _story_cols_f:
        c.execute("ALTER TABLE stories ADD COLUMN ama INTEGER DEFAULT 0")
    # messages.ama: 1 = this story-linked message came in through the
    # story's AMA box (thread/inbox label it as a question).
    _msg_cols_f = [r["name"] for r in
                   c.execute("PRAGMA table_info(messages)").fetchall()]
    if "ama" not in _msg_cols_f:
        c.execute("ALTER TABLE messages ADD COLUMN ama INTEGER DEFAULT 0")
    # Live polls: binary too. correct NULL = a plain poll; 'a'/'b' =
    # a trivia poll (the answer is NEVER sent to clients while the
    # poll is active). Only ONE active poll per live — starting a new
    # one closes the previous.
    c.execute("""CREATE TABLE IF NOT EXISTS live_polls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        live_id INTEGER NOT NULL, question TEXT NOT NULL,
        a_text TEXT NOT NULL, b_text TEXT NOT NULL,
        correct TEXT, created_by INTEGER NOT NULL,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL)""")
    c.execute("""CREATE TABLE IF NOT EXISTS live_poll_votes (
        poll_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        choice TEXT NOT NULL,
        PRIMARY KEY(poll_id, user_id))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_livepolls_room "
              "ON live_polls(live_id, active)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_livepollvotes_poll "
              "ON live_poll_votes(poll_id)")
    # Live room games (Games Pack 2): host/admin start a room game,
    # the whole room plays. 'guess' keeps a server-only secret
    # (1-100) in live_games.secret — it is NEVER serialized into an
    # active-game payload, only revealed once the game has ended.
    # 'dice' rolls server-side per play. Only ONE active game per
    # live: starting a new one ends the previous; ending the live
    # ends its active game (see _end_live_row).
    c.execute("""CREATE TABLE IF NOT EXISTS live_games (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        live_id INTEGER NOT NULL, kind TEXT NOT NULL,
        secret INTEGER, status TEXT NOT NULL DEFAULT 'active',
        created_by INTEGER NOT NULL, created_at TEXT NOT NULL,
        ended_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS live_game_plays (
        game_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        value INTEGER, best INTEGER, plays INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(game_id, user_id))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_livegames_room "
              "ON live_games(live_id, status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_livegameplays_game "
              "ON live_game_plays(game_id)")
    # Friend challenges (Games Pack 2): asynchronous 1-v-1 matches
    # from the Games Hub. state is JSON in a TEXT column. ttt:
    # {"board": [null x9]} with a_id = X moving first (turn_id tracks
    # whose move it is). rps: {a_pick, b_pick, round, a_wins, b_wins,
    # last} — picks stay server-side until both are in; rounds run
    # until someone reaches 2 round-wins.
    c.execute("""CREATE TABLE IF NOT EXISTS game_matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL, a_id INTEGER NOT NULL, b_id INTEGER NOT NULL,
        state TEXT NOT NULL, turn_id INTEGER, winner_id INTEGER,
        is_draw INTEGER NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'active',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_gamematches_a "
              "ON game_matches(a_id, status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_gamematches_b "
              "ON game_matches(b_id, status)")
    # --- HERE Market: local buy/sell (safe on existing DBs) ---
    # A listing is a PUBLIC OFFER (see the Market section of
    # ROUTE_CONTRACT.md): it stays visible in browse, on its detail
    # page, and on the public /m/{id} page even when the seller's
    # profile is private -- posting it is the seller's own public act.
    # Blocks are still enforced both ways in-app. status:
    # 'active' | 'sold' | 'deleted' (soft delete: thread snapshot
    # cards and feed posts keep pointing at the listing, whose
    # detail page turns into the friendly "gone" page). photos is a
    # JSON array of uploads-relative paths (0-4, uploads/market/).
    c.execute("""CREATE TABLE IF NOT EXISTS market_listings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        seller_id INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '',
        price_cents INTEGER NOT NULL DEFAULT 0,
        category TEXT NOT NULL DEFAULT 'Other',
        condition TEXT NOT NULL DEFAULT 'Good',
        description TEXT NOT NULL DEFAULT '',
        location TEXT NOT NULL DEFAULT '',
        photos TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'active',
        share_to_feed INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL, sold_at TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_market_seller "
              "ON market_listings(seller_id, status)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_market_status "
              "ON market_listings(status, created_at)")
    _mk_cols = [r["name"] for r in
                c.execute("PRAGMA table_info(market_listings)")
                .fetchall()]
    if "live_id" not in _mk_cols:
        c.execute("ALTER TABLE market_listings ADD COLUMN "
                  "live_id INTEGER")
    if "claimed_by" not in _mk_cols:
        c.execute("ALTER TABLE market_listings ADD COLUMN "
                  "claimed_by INTEGER")
        c.execute("ALTER TABLE market_listings ADD COLUMN "
                  "claim_expires_at TEXT")
    c.execute("""CREATE TABLE IF NOT EXISTS live_auctions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        live_id INTEGER NOT NULL, listing_id INTEGER,
        title TEXT NOT NULL DEFAULT '', photo TEXT NOT NULL DEFAULT '',
        start_cents INTEGER NOT NULL DEFAULT 0,
        current_cents INTEGER NOT NULL DEFAULT 0,
        leader_id INTEGER, ends_at REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL DEFAULT 'running',
        created_by INTEGER NOT NULL, created_at TEXT NOT NULL,
        settled INTEGER NOT NULL DEFAULT 0)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_auct_live "
              "ON live_auctions(live_id, status)")
    # Money build: post gifts get their own table (page_gifts
    # pattern + post_id); the live entry door (entry price on the
    # session + who paid); market boosts; HERE Plus columns.
    c.execute("""CREATE TABLE IF NOT EXISTS post_gifts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER NOT NULL, recipient_id INTEGER NOT NULL,
        post_id INTEGER NOT NULL,
        gift_key TEXT NOT NULL DEFAULT '',
        tickets INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT '')""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_post_gifts_post "
              "ON post_gifts(post_id, id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_post_gifts_sender "
              "ON post_gifts(sender_id, recipient_id, id)")
    c.execute("""CREATE TABLE IF NOT EXISTS live_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        live_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        tickets INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT '',
        UNIQUE(live_id, user_id))""")
    _ls_cols = [r["name"] for r in c.execute(
        "PRAGMA table_info(live_sessions)").fetchall()]
    if "entry_tickets" not in _ls_cols:
        c.execute("ALTER TABLE live_sessions ADD COLUMN "
                  "entry_tickets INTEGER DEFAULT 0")
    # Platforms (Oct 30): a live is the host's own platform —
    # a category shelf + hashtags for discovery, a backdrop
    # image that brands the room, and per-seat custom names.
    if "category" not in _ls_cols:
        c.execute("ALTER TABLE live_sessions ADD COLUMN "
                  "category TEXT DEFAULT ''")
    if "tags" not in _ls_cols:
        c.execute("ALTER TABLE live_sessions ADD COLUMN "
                  "tags TEXT DEFAULT ''")
    if "backdrop" not in _ls_cols:
        c.execute("ALTER TABLE live_sessions ADD COLUMN "
                  "backdrop TEXT DEFAULT ''")
    if "backdrop_fit" not in _ls_cols:
        c.execute("ALTER TABLE live_sessions ADD COLUMN "
                  "backdrop_fit TEXT DEFAULT 'auto'")
    if "spot_names" not in _ls_cols:
        c.execute("ALTER TABLE live_sessions ADD COLUMN "
                  "spot_names TEXT DEFAULT ''")
    _lm_cols = [r["name"] for r in c.execute(
        "PRAGMA table_info(live_members)").fetchall()]
    if "seat_label" not in _lm_cols:
        c.execute("ALTER TABLE live_members ADD COLUMN "
                  "seat_label TEXT DEFAULT ''")
    _ml_cols = [r["name"] for r in c.execute(
        "PRAGMA table_info(market_listings)").fetchall()]
    if "boost_until" not in _ml_cols:
        c.execute("ALTER TABLE market_listings ADD COLUMN "
                  "boost_until REAL DEFAULT 0")
    _mu_cols = [r["name"] for r in c.execute(
        "PRAGMA table_info(users)").fetchall()]
    if "plus_until" not in _mu_cols:
        c.execute("ALTER TABLE users ADD COLUMN plus_until TEXT")
    if "stripe_sub_id" not in _mu_cols:
        c.execute("ALTER TABLE users ADD COLUMN stripe_sub_id TEXT")
    # Growth round: tip goals on profiles (base = gift_lifetime when
    # the goal was set, so the bar only counts NEW gifts).
    if "tip_goal_label" not in _mu_cols:
        c.execute("ALTER TABLE users ADD COLUMN "
                  "tip_goal_label TEXT DEFAULT ''")
    if "tip_goal_tickets" not in _mu_cols:
        c.execute("ALTER TABLE users ADD COLUMN "
                  "tip_goal_tickets INTEGER DEFAULT 0")
    if "tip_goal_base" not in _mu_cols:
        c.execute("ALTER TABLE users ADD COLUMN "
                  "tip_goal_base INTEGER DEFAULT 0")
    if "media_mode" not in _mu_cols:
        c.execute("ALTER TABLE users ADD COLUMN "
                  "media_mode TEXT DEFAULT ''")
    if "media_skin" not in _mu_cols:
        c.execute("ALTER TABLE users ADD COLUMN "
                  "media_skin TEXT DEFAULT ''")
    if "artist_verified" not in _mu_cols:
        c.execute("ALTER TABLE users ADD COLUMN "
                  "artist_verified INTEGER DEFAULT 0")
    if "artist_contract" not in _mu_cols:
        c.execute("ALTER TABLE users ADD COLUMN "
                  "artist_contract TEXT DEFAULT ''")
    # Growth round: scheduled shows + their reminder subscribers.
    c.execute("""CREATE TABLE IF NOT EXISTS live_schedule (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        host_id INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '',
        starts_at REAL NOT NULL DEFAULT 0,
        entry_tickets INTEGER NOT NULL DEFAULT 0,
        live_id INTEGER, created_at TEXT NOT NULL DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS live_reminders (
        sched_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(sched_id, user_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS media_tracks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '',
        file TEXT NOT NULL DEFAULT '', rights TEXT NOT NULL DEFAULT '',
        plays INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT '')""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_media_tracks_user "
              "ON media_tracks(user_id, id)")
    c.execute("""CREATE TABLE IF NOT EXISTS artist_applications (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, note TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'new',
        created_at TEXT NOT NULL DEFAULT '',
        decided_at TEXT NOT NULL DEFAULT '')""")
    _aa_cols = [r["name"] for r in c.execute(
        "PRAGMA table_info(artist_applications)")]
    if "signed_name" not in _aa_cols:
        c.execute("ALTER TABLE artist_applications ADD COLUMN "
                  "signed_name TEXT DEFAULT ''")
    if "read_playbook" not in _aa_cols:
        c.execute("ALTER TABLE artist_applications ADD COLUMN "
                  "read_playbook INTEGER DEFAULT 0")
    c.execute("""CREATE TABLE IF NOT EXISTS artist_contracts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'offered',
        terms_version TEXT NOT NULL DEFAULT 'v1',
        offered_at TEXT NOT NULL DEFAULT '',
        signed_at TEXT NOT NULL DEFAULT '',
        signed_name TEXT NOT NULL DEFAULT '',
        ended_at TEXT NOT NULL DEFAULT '',
        ended_by TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS media_track_likes (
        track_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (track_id, user_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS track_gifts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER NOT NULL, recipient_id INTEGER NOT NULL,
        track_id INTEGER NOT NULL,
        gift_key TEXT NOT NULL DEFAULT '',
        tickets INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS label_fees (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        payout_request_id INTEGER NOT NULL DEFAULT 0,
        tickets INTEGER NOT NULL DEFAULT 0,
        cents INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS artist_fund_grants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        cents INTEGER NOT NULL DEFAULT 0,
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS gift_nudges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
        kind TEXT NOT NULL, target_id INTEGER NOT NULL,
        created_at TEXT NOT NULL DEFAULT '')""")
    c.execute("""CREATE TABLE IF NOT EXISTS app_suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, text TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'new',
        created_at TEXT NOT NULL DEFAULT '',
        decided_at TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS market_saves (
        user_id INTEGER NOT NULL, listing_id INTEGER NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(user_id, listing_id))""")
    # messages.listing_*: a Market "Message seller" inquiry carries a
    # SNAPSHOT of the listing (id + title + price at send time) so the
    # thread card survives later edits and even the listing's
    # deletion -- the same trick as story_id/story_media above.
    # Plain messages leave all three NULL.
    _msg_cols_m = [r["name"] for r in
                   c.execute("PRAGMA table_info(messages)").fetchall()]
    if "listing_id" not in _msg_cols_m:
        c.execute("ALTER TABLE messages ADD COLUMN listing_id INTEGER")
    if "listing_title" not in _msg_cols_m:
        c.execute("ALTER TABLE messages ADD COLUMN listing_title TEXT")
    if "listing_price_cents" not in _msg_cols_m:
        c.execute("ALTER TABLE messages ADD COLUMN "
                  "listing_price_cents INTEGER")
    # --- Build LOCKED CONTENT (DM side) ---
    # messages.lock_media: the locked/ storage key of a paid DM
    # attachment ('' on plain messages); lock_price/lock_adult mirror
    # the post columns. The message's sender_id IS the seller.
    if "lock_media" not in _msg_cols_m:
        c.execute("ALTER TABLE messages ADD COLUMN lock_media "
                  "TEXT DEFAULT ''")
    if "lock_price" not in _msg_cols_m:
        c.execute("ALTER TABLE messages ADD COLUMN lock_price "
                  "INTEGER DEFAULT 0")
    if "lock_adult" not in _msg_cols_m:
        c.execute("ALTER TABLE messages ADD COLUMN lock_adult "
                  "INTEGER DEFAULT 0")
    # --- Live Tickets: the live gift economy (safe on existing DBs) ---
    # TWO ledgers per user (see the Live Tickets section of
    # ROUTE_CONTRACT.md): tickets_balance = SPENDABLE tickets (bought,
    # minus gifts/shouts sent); gift_earned = tickets EARNED by
    # hosting (received gifts/shouts) that have not been cashed out
    # or put on hold by a payout request. ticket_txns is the audit
    # ledger: its `tickets` column is the signed delta against the
    # ledger named by `kind` ('buy'/'gift_send'/'shout' move the
    # spendable side; 'gift_receive'/'cashout_hold'/'cashout_return'
    # move the earned side; 'cashout_paid' is a 0-delta audit marker
    # — the tickets left the earned ledger at hold time).
    _user_cols_lt = [r["name"] for r in
                     c.execute("PRAGMA table_info(users)").fetchall()]
    if "tickets_balance" not in _user_cols_lt:
        c.execute("ALTER TABLE users ADD COLUMN tickets_balance "
                  "INTEGER DEFAULT 0")
    if "gift_earned" not in _user_cols_lt:
        c.execute("ALTER TABLE users ADD COLUMN gift_earned "
                  "INTEGER DEFAULT 0")
    _lifetime_backfill = False
    if "gift_lifetime" not in _user_cols_lt:
        # Build P-GIFTS: lifetime tickets EARNED (gifts + shout
        # receives). Never spent down — cash-outs move gift_earned,
        # not this — so it only ever grows; it drives the streamer
        # Live Level (see STREAMER_LEVELS). Backfilled from the
        # audit ledger (below, once ticket_txns exists) so today's
        # earners keep their standing.
        # Invariant: gift_lifetime == SUM(tickets) over the user's
        # 'gift_receive' ticket_txns rows.
        c.execute("ALTER TABLE users ADD COLUMN gift_lifetime "
                  "INTEGER DEFAULT 0")
        _lifetime_backfill = True
    if "founding_no" not in _user_cols_lt:
        c.execute("ALTER TABLE users ADD COLUMN founding_no "
                  "INTEGER")
    if "founding" not in _user_cols_lt:
        c.execute("ALTER TABLE users ADD COLUMN founding "
                  "INTEGER DEFAULT 0")
        # Original 100 backfill: the earliest 100 REAL accounts
        # (by id, survivors — deleted test accounts hold no seat)
        # are the founding members. Runs once, at column birth.
        c.execute(
            "UPDATE users SET founding=1 WHERE id IN "
            "(SELECT id FROM users ORDER BY id ASC LIMIT 100)")
        for _i, _r in enumerate(c.execute(
                "SELECT id FROM users WHERE founding=1 "
                "ORDER BY id ASC").fetchall(), start=1):
            c.execute("UPDATE users SET founding_no=? WHERE id=?",
                      (_i, _r["id"]))
    if c.execute("SELECT COUNT(*) c FROM users WHERE founding=1 "
                 "AND founding_no IS NULL").fetchone()["c"]:
        for _i, _r in enumerate(c.execute(
                "SELECT id FROM users WHERE founding=1 "
                "ORDER BY id ASC").fetchall(), start=1):
            c.execute("UPDATE users SET founding_no=? WHERE id=?",
                      (_i, _r["id"]))
    if "stripe_acct_id" not in _user_cols_lt:
        # Build R: the streamer's Stripe Connect (Express) account
        # id, created by POST /payouts/setup; payout_ready is the
        # cached readiness flag (refreshed from Stripe on /tickets
        # loads — see _refresh_payout_ready).
        c.execute("ALTER TABLE users ADD COLUMN stripe_acct_id TEXT")
    if "payout_ready" not in _user_cols_lt:
        c.execute("ALTER TABLE users ADD COLUMN payout_ready "
                  "INTEGER DEFAULT 0")
    c.execute("""CREATE TABLE IF NOT EXISTS ticket_txns (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, kind TEXT NOT NULL,
        tickets INTEGER NOT NULL DEFAULT 0,
        live_id INTEGER, gift_key TEXT,
        stripe_session TEXT, note TEXT DEFAULT '',
        created_at TEXT NOT NULL)""")
    # Build MONEY: per-stream earnings attribution rides on
    # ticket_txns.live_id — the gift-send and shout-out writers set
    # it on every credit, but a DB created before the column joined
    # the CREATE TABLE above gets it here. Credits whose live_id
    # stays NULL (written before attribution existed) are honest
    # history: the /earnings dashboard shows them as one
    # "Earlier streams" bucket.
    if "live_id" not in [r["name"] for r in
                         c.execute("PRAGMA table_info(ticket_txns)")
                         .fetchall()]:
        c.execute("ALTER TABLE ticket_txns ADD COLUMN live_id INTEGER")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ticket_txns_user "
              "ON ticket_txns(user_id, id)")
    # Idempotent purchase crediting: one buy txn per Stripe checkout
    # session, enforced by a partial unique index (NULL sessions —
    # every non-buy row — never collide).
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS "
              "idx_ticket_txns_session ON ticket_txns(stripe_session) "
              "WHERE stripe_session IS NOT NULL")
    # Build T: the owner money panel aggregates 'buy' rows inside
    # created_at windows — keep those range lookups index-served.
    c.execute("CREATE INDEX IF NOT EXISTS "
              "idx_ticket_txns_kind_created "
              "ON ticket_txns(kind, created_at)")
    if _lifetime_backfill:
        c.execute(
            "UPDATE users SET gift_lifetime = COALESCE((SELECT "
            "SUM(tickets) FROM ticket_txns WHERE "
            "user_id=users.id AND kind='gift_receive'), 0)")
    c.execute("""CREATE TABLE IF NOT EXISTS live_gifts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        live_id INTEGER NOT NULL, sender_id INTEGER NOT NULL,
        host_id INTEGER NOT NULL, gift_key TEXT NOT NULL,
        tickets INTEGER NOT NULL, created_at TEXT NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_live_gifts_room "
              "ON live_gifts(live_id, id)")
    _lg_cols = [r["name"] for r in
                c.execute("PRAGMA table_info(live_gifts)").fetchall()]
    if "recipient_id" not in _lg_cols:
        # Build PANEL GIFTS: who ACTUALLY received the gift (any
        # panelist), as opposed to host_id (the room's host, kept
        # for its historical meaning). NULL on pre-build rows —
        # readers treat NULL as "the host received it".
        c.execute("ALTER TABLE live_gifts ADD COLUMN recipient_id "
                  "INTEGER")
    c.execute("""CREATE TABLE IF NOT EXISTS live_shouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        live_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        body TEXT NOT NULL, tickets INTEGER NOT NULL,
        expires_at TEXT NOT NULL, created_at TEXT NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_live_shouts_room "
              "ON live_shouts(live_id, id)")
    # Build PAGE GIFTS / Creator Mode: the per-user switch that
    # turns a profile (and its Pages) into a creator surface —
    # Creator Mode ON is what makes the 🎁 Gift button appear and
    # the gift routes accept sends (Locked Content will ride the
    # same flag in the next build). Default off; a NULL on a
    # pre-creator row reads as off.
    _ucols_cm = [r["name"] for r in
                 c.execute("PRAGMA table_info(users)").fetchall()]
    if "creator_mode" not in _ucols_cm:
        c.execute("ALTER TABLE users ADD COLUMN creator_mode "
                  "INTEGER DEFAULT 0")
    # --- Build PAGE GIFTS: gifts sent to a profile or a Page ---
    # One row per gift sent OUTSIDE a live (the 🎁 Gift button on
    # profiles + Pages). recipient_id is always the USER who earns
    # (a profile's owner, or a Page's owner_id — never anyone else);
    # page_id is NULL for a profile gift and names the Page for a
    # page gift, so the Recent-gifts strips read one table. The
    # money itself moves through the same two ledgers as live
    # gifts; the gift_receive ledger row carries note='page' (and
    # live_id NULL) so /earnings can show page gifts as their own
    # line instead of the "Earlier streams" bucket.
    c.execute("""CREATE TABLE IF NOT EXISTS page_gifts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sender_id INTEGER NOT NULL, recipient_id INTEGER NOT NULL,
        page_id INTEGER, gift_key TEXT NOT NULL,
        tickets INTEGER NOT NULL, created_at TEXT NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_page_gifts_recipient "
              "ON page_gifts(recipient_id, page_id, id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_page_gifts_sender "
              "ON page_gifts(sender_id, recipient_id, id)")
    # --- Build LOCKED CONTENT: purchase records ---
    # One row per (content, buyer): the UNIQUE constraint is the
    # hard guarantee a second unlock can never double-charge — the
    # unlock transaction rolls back if this insert collides.
    # content_type is 'post' or 'dm'; seller_id/price are snapshots
    # of the sale. Unlocking is forever: rows are never deleted
    # except when the content itself is deleted.
    c.execute("""CREATE TABLE IF NOT EXISTS content_unlocks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        content_type TEXT NOT NULL, content_id INTEGER NOT NULL,
        buyer_id INTEGER NOT NULL, seller_id INTEGER NOT NULL,
        price INTEGER NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(content_type, content_id, buyer_id))""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_unlocks_buyer "
              "ON content_unlocks(buyer_id, content_type, content_id)")
    # --- Live Showdown battles (Build W2) ---
    # A battle LINKS two existing live rooms; it never merges them.
    # Scores are ticket-points added by the gift handler in the same
    # transaction as each gift. finish_reason: 'timer' | 'forfeit'.
    # effects_done guards the exactly-once result posts + notices.
    c.execute("""CREATE TABLE IF NOT EXISTS live_battles (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        challenger_live_id INTEGER NOT NULL,
        challenger_user_id INTEGER NOT NULL,
        opponent_live_id INTEGER NOT NULL,
        opponent_user_id INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        duration_sec INTEGER NOT NULL DEFAULT 300,
        started_at TEXT, ends_at TEXT,
        challenger_score INTEGER NOT NULL DEFAULT 0,
        opponent_score INTEGER NOT NULL DEFAULT 0,
        winner_user_id INTEGER,
        finish_reason TEXT,
        effects_done INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL, decided_at TEXT)""")
    # One battle per live, per role column (the code-level busy
    # check covers cross-column involvement; these are the backstop).
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS "
              "idx_live_battles_chal_live ON live_battles"
              "(challenger_live_id) WHERE status IN ('pending','active')")
    c.execute("CREATE UNIQUE INDEX IF NOT EXISTS "
              "idx_live_battles_opp_live ON live_battles"
              "(opponent_live_id) WHERE status IN ('pending','active')")
    c.execute("CREATE INDEX IF NOT EXISTS idx_live_battles_recent "
              "ON live_battles(status, decided_at)")
    c.execute("""CREATE TABLE IF NOT EXISTS payout_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL, tickets INTEGER NOT NULL,
        dollars_cents INTEGER NOT NULL,
        payout_note TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL, decided_at TEXT)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_payouts_status "
              "ON payout_requests(status, id)")
    # Build R: how a decided request was settled + the Stripe
    # transfer id when it went to a bank. status also gains a
    # transient 'sending' state (the approve-and-send claim — see
    # owner_payout_send). NULL method = pending / pre-Build-R row.
    _pr_cols = [r["name"] for r in
                c.execute("PRAGMA table_info(payout_requests)").fetchall()]
    if "method" not in _pr_cols:
        c.execute("ALTER TABLE payout_requests ADD COLUMN method TEXT")
    if "stripe_transfer_id" not in _pr_cols:
        c.execute("ALTER TABLE payout_requests ADD COLUMN "
                  "stripe_transfer_id TEXT")
    # Growth round: the public "I got paid" share card token. Minted
    # lazily by the creator from their own paid request only.
    if "share_token" not in _pr_cols:
        c.execute("ALTER TABLE payout_requests ADD COLUMN "
                  "share_token TEXT")
    if "fee_tickets" not in _pr_cols:
        c.execute("ALTER TABLE payout_requests ADD COLUMN "
                  "fee_tickets INTEGER DEFAULT 0")
    # Auto-pay era (Oct 30): the owner may pay a creator BY HAND
    # on top of a still-queued request (Ken paid Sunflwr $20 by
    # hand; her $13 bank send still goes through). hand_note is
    # that payment's display annotation — the request itself
    # stays pending until its bank send completes it.
    if "hand_note" not in _pr_cols:
        c.execute("ALTER TABLE payout_requests ADD COLUMN "
                  "hand_note TEXT DEFAULT ''")
    # Build O: password recovery. token_hash is the ONLY credential
    # copy (sha256 of the raw token); token_plain holds the raw token
    # ONLY while delivery='queued' so the owner panel can show the
    # link for a hand-send — it is cleared the moment the row is
    # emailed, hand-sent, used or superseded (see ROUTE_CONTRACT.md).
    # delivery: 'queued' (owner queue / hand-send) | 'emailed' |
    # 'hand_sent'. Resets deliberately write NOTHING to
    # moderation_flags or notifications — the flag-watch and bughunt
    # tooling must never trip on recovery rows; this table + the
    # owner panel queue are the whole admin surface.
    c.execute("""CREATE TABLE IF NOT EXISTS password_resets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        token_hash TEXT NOT NULL UNIQUE,
        token_plain TEXT,
        delivery TEXT NOT NULL DEFAULT 'queued',
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        used_at TEXT, sent_at TEXT,
        requester_ip TEXT DEFAULT '')""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pwresets_user "
              "ON password_resets(user_id, id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pwresets_ip "
              "ON password_resets(requester_ip, created_at)")
    # --- ads (Build ADS): house + page ads and their event log ---
    ads.ensure_schema(conn)
    conn.commit()
    conn.close()


init_db()


# ---------------- auth / session helpers (fresh build, postit-style pattern) ----------------
def get_user(uid):
    if not uid:
        return None
    conn = db()
    r = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    return dict(r) if r else None


def current_user(request: Request):
    token = request.cookies.get(SESSION_COOKIE, "")
    if not token:
        return None
    conn = db()
    s = conn.execute("SELECT user_id FROM sessions WHERE token=?", (token,)).fetchone()
    conn.close()
    if not s:
        return None
    u = get_user(s["user_id"])
    if u:
        touch_presence(u)
    return u


def is_owner(request: Request):
    token = request.cookies.get(OWNER_COOKIE, "")
    if not token:
        return False
    conn = db()
    s = conn.execute("SELECT token FROM owner_sessions WHERE token=?", (token,)).fetchone()
    conn.close()
    return bool(s)


def require_login(request: Request):
    u = current_user(request)
    if not u:
        return None, redir("/login", "Please log in first.")
    return u, None


def require_owner(request: Request):
    if not is_owner(request):
        return redir("/owner/login", "Owner access only.")
    return None


def touch_seen(user_id):
    conn = db()
    conn.execute("UPDATE users SET last_seen=? WHERE id=?", (now_iso(), user_id))
    conn.commit()
    conn.close()


def is_online(last_seen):
    if not last_seen:
        return False
    try:
        dt = datetime.fromisoformat(str(last_seen))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - dt <= timedelta(minutes=ONLINE_MINUTES)
    except Exception:
        return False


# ---------------- presence (lightning-bolt status) ----------------
# Every user gets a ⚡ bolt wherever people are listed: green = active,
# yellow = away / undetermined, red = offline. In Settings each user can
# force a status, hide it completely (no bolt anywhere), and separately
# choose whether the bolt shows in Messages (inbox list + thread header).
PRESENCE_ONLINE_SECONDS = 3 * 60     # auto: seen within 3 min -> green
PRESENCE_AWAY_SECONDS = 30 * 60      # auto: seen within 30 min -> yellow
PRESENCE_WRITE_SECONDS = 30          # heartbeat writes at most this often
STATUS_MODES = ("auto", "online", "away", "offline", "hidden")
STATUS_LABELS = {"online": "Active now", "away": "Away", "offline": "Offline"}


def _parse_ts(value):
    """Parse a stored UTC ISO timestamp into an aware datetime (or None)."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _uget(u, key, default=None):
    """Read a field from a user record whether it's a dict or sqlite3.Row."""
    if u is None:
        return default
    if isinstance(u, dict):
        return u.get(key, default)
    try:
        return u[key]
    except Exception:
        return default


def touch_presence(u):
    """Throttled heartbeat: refresh users.last_seen_at, but only when the
    stored value is missing or older than ~30s, so the ~4s call poller and
    normal page loads don't turn into a DB write per request."""
    if not u:
        return
    last = _parse_ts(_uget(u, "last_seen_at"))
    if last is not None and (datetime.now(timezone.utc) - last) \
            <= timedelta(seconds=PRESENCE_WRITE_SECONDS):
        return
    conn = db()
    conn.execute("UPDATE users SET last_seen_at=? WHERE id=?",
                 (now_iso(), u["id"]))
    conn.commit()
    conn.close()


def presence_of(u, inbox=False):
    """The presence OTHERS may see for a user row/dict, as
    {'status': 'online'|'away'|'offline', 'label': str} — or None when
    nothing may be shown: the user hid their status entirely, or
    (inbox=True) the user turned off status in Messages. Manual modes
    force their color; 'auto' derives from last_seen_at."""
    if not u:
        return None
    if inbox:
        show_inbox = _uget(u, "inbox_status")
        if show_inbox is None:
            show_inbox = 1
        if not show_inbox:
            return None
    mode = (_uget(u, "status_mode") or "auto")
    if mode == "hidden":
        return None
    if mode in STATUS_LABELS:
        status = mode
    else:
        last = _parse_ts(_uget(u, "last_seen_at"))
        if last is None:
            status = "offline"
        else:
            age = (datetime.now(timezone.utc) - last).total_seconds()
            if age <= PRESENCE_ONLINE_SECONDS:
                status = "online"
            elif age <= PRESENCE_AWAY_SECONDS:
                status = "away"
            else:
                status = "offline"
    return {"status": status, "label": STATUS_LABELS[status]}


def render(request: Request, name, ctx=None):
    """Every page view gets the global template context; authenticated views
    also refresh last_seen."""
    ctx = dict(ctx or {})
    ctx.setdefault("icon_style", "emoji")
    ctx.setdefault("ic", lambda key: icon_html(key, "emoji"))
    u = current_user(request)
    if u:
        touch_seen(u["id"])
        u = get_user(u["id"])  # refresh (last_seen changed)
        ctx["user"] = {"id": u["id"], "name": u["name"], "email": u["email"],
                       "bio": u.get("bio", ""),
                       "avatar": avatar_url(u.get("avatar", "")),
                       # Build LOCKED CONTENT: templates gate the
                       # creator-only lock fields on this flag (the
                       # server re-checks on every write regardless).
                       "creator_mode": 1 if u.get("creator_mode") else 0,
                       "media_skin": avatar_url(
                           u.get("media_skin", "")),
                       "artist_verified":
                           1 if u.get("artist_verified") else 0,
                       "artist_contract":
                           u.get("artist_contract") or ""}
        conn = db()
        ctx.setdefault("post_gift", _post_gift_ctx(u))
        ctx["pending_count"] = conn.execute(
            "SELECT COUNT(*) c FROM friendships WHERE friend_id=? AND status='pending'",
            (u["id"],)).fetchone()["c"]
        ctx["unread_count"] = conn.execute(
            "SELECT COUNT(*) c FROM notifications WHERE user_id=? AND is_read=0",
            (u["id"],)).fetchone()["c"]
        ctx["unread_msgs"] = conn.execute(
            "SELECT COUNT(*) c FROM messages m WHERE m.receiver_id=? "
            "AND m.is_read=0" + _vis_frag("m"),
            (u["id"], u["id"], u["id"], u["id"])).fetchone()["c"]
        conn.close()
        # Build ICON STYLES: the viewer's icon finish drives the
        # ic() template helper + the body class (base.html).
        _istyle = icon_style_of(u)
        ctx["icon_style"] = _istyle
        ctx["ic"] = lambda key: icon_html(key, _istyle)
        if _istyle != "emoji":
            import json as _json
            ctx["icons_js"] = _json.dumps(
                {k: str(icon_html(k, _istyle)) for k in
                 ("mic", "mute", "phone", "camera", "lives",
                 "speaker", "gift", "eye")})
        # The viewer's own profile background follows them around the whole
        # logged-in site. Routes may override page_bg (profile pages show the
        # profile owner's background instead).
        ctx.setdefault("page_bg", avatar_url(u.get("background", "")))
        # Same for the viewer's "Your Spot" colors: their text/bubble theme
        # follows them site-wide via <body> vars (see base.html). Profile
        # routes override these with "" — a profile page is ruled entirely
        # by its owner's spot (its .spot-scope vars), never the viewer's.
        v_style, v_classes = viewer_spot_for(u)
        ctx.setdefault("viewer_spot_style", v_style)
        ctx.setdefault("viewer_spot_classes", v_classes)
        # Build CLEAR: the legacy --box-alpha layer is the historical
        # Glass mechanism — it is emitted only for a Glass viewer with
        # a saved solidity below 100. Mix gets its glass from the new
        # --glass-alpha-soft token instead, never from this layer.
        ctx.setdefault("viewer_box_alpha",
                       viewer_box_alpha_for(u)
                       if look_scheme_for(u) == "glass" else "")
        ctx.setdefault("viewer_glass_alpha", glass_alpha_for(u))
        ctx.setdefault("viewer_glass_alpha_soft",
                       glass_alpha_soft_for(u))
        ctx.setdefault("viewer_glass_rgb", look_rgb_str_for(u))
        ctx.setdefault("viewer_look_accent", look_accent_for(u))
        ctx.setdefault("viewer_look_classes", look_scope_class_for(u))
        # Build SHINE: the accent as rgba() components (for glows)
        # + the scheme gate class for the lit-glass finish.
        ctx.setdefault("viewer_look_accent_rgb",
                       look_accent_rgb_str_for(u))
        ctx.setdefault("viewer_scheme_class", look_scheme_class_for(u))
        ctx.setdefault("viewer_gold", is_gold_inviter(u["id"]))
    else:
        ctx["user"] = None
        ctx["pending_count"] = 0
        ctx["unread_count"] = 0
        ctx["unread_msgs"] = 0
        ctx.setdefault("viewer_gold", False)
        ctx.setdefault("page_bg", "")
        ctx.setdefault("viewer_spot_style", "")
        ctx.setdefault("viewer_spot_classes", "")
        ctx.setdefault("viewer_box_alpha", "")
        ctx.setdefault("viewer_glass_alpha", GLASS_DEFAULT_ALPHA)
        ctx.setdefault("viewer_glass_alpha_soft", GLASS_DEFAULT_ALPHA)
        ctx.setdefault("viewer_glass_rgb", look_rgb_str_for(None))
        ctx.setdefault("viewer_look_accent", LOOK_DEFAULT_ACCENT)
        ctx.setdefault("viewer_look_classes", "")
        ctx.setdefault("viewer_look_accent_rgb",
                       look_accent_rgb_str_for(None))
        ctx.setdefault("viewer_scheme_class", "scheme-glass")
    ctx.setdefault("flash", request.query_params.get("flash", ""))
    return templates.TemplateResponse(request, name, ctx)


def redir(url, flash=""):
    if flash:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}flash={flash}"
    return RedirectResponse(url, status_code=303)


# ---- owner-panel in-place actions (Ken's order Oct 30, 2026) ----
# "I hate when I click on something and make a change it jumps to
# another page — let me make the change and be able to make that
# change again on someone else." Every owner POST route below
# answers JSON when the panel's script asks (X-Owner-Ajax: 1) and
# keeps the classic redirect for a plain form submit, so the panel
# updates the row where it sits — no reload, no lost scroll spot.
def _oa(request):
    return request.headers.get("x-owner-ajax") == "1"


def _oa_json(msg, **extra):
    body = {"ok": True, "msg": msg}
    body.update(extra)
    return JSONResponse(body)


def _oa_fail(msg):
    return JSONResponse({"ok": False, "msg": msg})


def back(request: Request, fallback="/home"):
    ref = request.headers.get("referer", "")
    # only allow same-origin relative redirects
    if ref.startswith("/"):
        return redir(ref)
    return redir(fallback)


def set_session_cookie(resp, token, cookie_name, days=30):
    resp.set_cookie(cookie_name, token, httponly=True, samesite="lax",
                   max_age=days * 86400, path="/")
    return resp


def new_session(user_id):
    token = secrets.token_urlsafe(32)
    conn = db()
    conn.execute("INSERT INTO sessions(token, user_id, created_at) VALUES(?,?,?)",
                 (token, user_id, now_iso()))
    conn.commit()
    conn.close()
    return token


def new_owner_session():
    token = secrets.token_urlsafe(32)
    conn = db()
    conn.execute("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
                 (token, now_iso()))
    conn.commit()
    conn.close()
    return token


# ---------------- small helpers ----------------
# ---------------- silence (mute) + block helpers ----------------
def muted_id_set(uid):
    """User ids uid has silenced (one direction: uid's own mutes)."""
    conn = db()
    rows = conn.execute("SELECT muted_id FROM mutes WHERE muter_id=?",
                        (uid,)).fetchall()
    conn.close()
    return {r["muted_id"] for r in rows}


def blocked_id_set(uid):
    """User ids in a block relationship with uid in EITHER direction."""
    conn = db()
    rows = conn.execute(
        "SELECT blocker_id, blocked_id FROM blocks "
        "WHERE blocker_id=? OR blocked_id=?", (uid, uid)).fetchall()
    conn.close()
    out = set()
    for r in rows:
        out.add(r["blocked_id"] if r["blocker_id"] == uid
                else r["blocker_id"])
    return out


def blocked_either_way(a, b):
    """True when a block exists between the pair in either direction.
    Blocking is mutual in effect (Facebook-style): whoever pressed the
    button, BOTH users lose access to each other."""
    if not a or not b or a == b:
        return False
    conn = db()
    r = conn.execute(
        "SELECT 1 FROM blocks WHERE (blocker_id=? AND blocked_id=?) OR "
        "(blocker_id=? AND blocked_id=?)", (a, b, b, a)).fetchone()
    conn.close()
    return bool(r)


def notifications_allowed(recipient_id, actor_id):
    """The shared bell + push gate. An actor's actions stay silent for a
    recipient who silenced (muted) the actor, and for any pair with a
    block in either direction. No actor (system notices) always passes,
    as does the recipient acting on themselves."""
    if not actor_id or actor_id == recipient_id:
        return True
    conn = db()
    m = conn.execute(
        "SELECT 1 FROM mutes WHERE muter_id=? AND muted_id=?",
        (recipient_id, actor_id)).fetchone()
    if m:
        conn.close()
        return False
    b = conn.execute(
        "SELECT 1 FROM blocks WHERE (blocker_id=? AND blocked_id=?) OR "
        "(blocker_id=? AND blocked_id=?)",
        (recipient_id, actor_id, actor_id, recipient_id)).fetchone()
    conn.close()
    return not b


def notify(user_id, kind, text, link="", actor_id=None):
    if not user_id:
        return
    if not notifications_allowed(user_id, actor_id):
        return
    conn = db()
    conn.execute(
        "INSERT INTO notifications(user_id, kind, text, link, created_at) "
        "VALUES(?,?,?,?,?)", (user_id, kind, text, link, now_iso()))
    conn.commit()
    conn.close()


# ---------------- phone push notifications (Web Push) ----------------
# Real OS-level notifications through the Web Push protocol: the phone's
# push service (Apple/Google/Mozilla) delivers to the device and wakes
# the service worker just long enough to show the notification — it
# works even when HERE is completely closed, and nothing "runs in the
# background". Platform rules that shape this feature:
#   * iPhone: push exists ONLY inside the home-screen-installed web app
#     (iOS 16.4+); a plain Safari tab has no PushManager at all.
#   * Android Chrome: works in the installed PWA and in the browser.
#   * Our native WebView wrappers (APK/IPA) do NOT support Web Push —
#     the Settings / Get-the-app UI says so honestly.
VAPID_SUBJECT = "mailto:cooleykendrick7@gmail.com"

_vapid_cache = {}
_vapid_lock = threading.Lock()


def _vapid():
    """(Vapid signer, public key as unpadded base64url) for this server.

    The keypair is generated ONCE and the private key (PEM) is stored in
    the app_settings table on the persistent disk. Regenerating it later
    would orphan every existing subscription, so it is never rotated.
    """
    if "pair" in _vapid_cache:
        return _vapid_cache["pair"]
    with _vapid_lock:
        if "pair" in _vapid_cache:
            return _vapid_cache["pair"]
        conn = db()
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key='vapid_private_pem'"
        ).fetchone()
        pem = row["value"] if row else None
        if not pem:
            fresh = _Vapid()
            fresh.generate_keys()
            pem = fresh.private_pem().decode()
            conn.execute(
                "INSERT OR REPLACE INTO app_settings(key, value) "
                "VALUES('vapid_private_pem', ?)", (pem,))
            conn.commit()
        conn.close()
        signer = _Vapid.from_pem(pem.encode())
        from cryptography.hazmat.primitives.serialization import (
            Encoding, PublicFormat)
        raw = signer.public_key.public_bytes(
            Encoding.X962, PublicFormat.UncompressedPoint)
        pub = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        _vapid_cache["pair"] = (signer, pub)
        return _vapid_cache["pair"]


def _push_sub_count(user_id):
    conn = db()
    n = conn.execute(
        "SELECT COUNT(*) AS n FROM push_subscriptions WHERE user_id=?",
        (user_id,)).fetchone()["n"]
    conn.close()
    return n


def send_push(user_id, title, body, url="/", tag="", ttl=86400):
    """Fire-and-forget Web Push to every device the user subscribed.

    Runs in a daemon thread and swallows ALL failures (dead endpoint,
    push service down, library trouble): a notification must never be
    able to break sending a message or starting a call. A 404/410 from
    the push service means the subscription is gone for good (app
    uninstalled / permission revoked) — the row is deleted.
    """
    if not user_id or not _PUSH_OK:
        return

    def _work():
        try:
            conn = db()
            rows = conn.execute(
                "SELECT * FROM push_subscriptions WHERE user_id=?",
                (user_id,)).fetchall()
            conn.close()
            if not rows:
                return
            signer, _pub = _vapid()
            payload = json.dumps(
                {"title": title, "body": body, "url": url, "tag": tag})
            for row in rows:
                sub_info = {"endpoint": row["endpoint"],
                            "keys": {"p256dh": row["p256dh"],
                                     "auth": row["auth"]}}
                try:
                    _webpush_send(
                        sub_info, data=payload, vapid_private_key=signer,
                        vapid_claims={"sub": VAPID_SUBJECT},
                        ttl=ttl, timeout=10)
                    conn = db()
                    conn.execute(
                        "UPDATE push_subscriptions SET last_seen=? "
                        "WHERE id=?", (now_iso(), row["id"]))
                    conn.commit()
                    conn.close()
                except WebPushException as e:
                    code = getattr(getattr(e, "response", None),
                                   "status_code", None)
                    if code in (404, 410):
                        conn = db()
                        conn.execute(
                            "DELETE FROM push_subscriptions WHERE id=?",
                            (row["id"],))
                        conn.commit()
                        conn.close()
                except Exception:
                    pass
        except Exception:
            pass

    threading.Thread(target=_work, daemon=True).start()


_UPLOAD_FAIL = "That file couldn't be uploaded — try a different one."


def _save_upload_r2(up, subdir, ext, cap_mb):
    """R2-mode save_upload: spool the stream to a temp file (counting
    bytes), then one signed PUT. Cap policy: post videos and story
    media are UNCAPPED in R2 mode (the browser-direct path is the
    intended big-file route, but a relayed video gets the same
    policy); image surfaces keep their historical silent cap. Any
    storage failure becomes the generic friendly error — never a 500,
    and NEVER a silent disk write (mixed storage would strand files
    where the read path doesn't expect them)."""
    uncapped = subdir == "stories" or (subdir == "posts"
                                       and ext in VID_EXTS)
    limit = None if uncapped else cap_mb * 1024 * 1024
    key = f"{subdir}/{uuid.uuid4().hex}.{ext}"
    src = up.file
    try:
        src.seek(0)
    except Exception:
        pass
    total = 0
    tmp = tempfile.TemporaryFile()
    try:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if limit is not None and total > limit:
                raise ValueError(_UPLOAD_FAIL)
            tmp.write(chunk)
        tmp.seek(0)
        ctype = (getattr(up, "content_type", "") or
                 mimetypes.guess_type(up.filename or "")[0] or
                 "application/octet-stream")
        try:
            storage.put_object(key, tmp, ctype, size=total)
        except storage.StorageError:
            raise ValueError(_UPLOAD_FAIL)
    finally:
        tmp.close()
    return key


def _accept_media_key(user, key, subdir, allowed_exts):
    """Validate a browser-direct (presigned) upload key submitted with
    a post/story form. The key must be one THIS server minted for THIS
    user + surface ('<subdir>/u<uid>-<uuid>.<ext>'), its extension
    must be allowed on the surface, and the object must actually exist
    in R2 (a minted-but-never-uploaded key is worthless). Returns
    (media, error) — exactly one of the two is non-empty."""
    m = re.fullmatch(
        rf"{subdir}/u{user['id']}-([0-9a-f]{{32}})\.([a-z0-9]+)",
        (key or "").strip())
    if not m or m.group(2) not in allowed_exts:
        return "", "That upload couldn't be used — please try again."
    try:
        if not storage.exists(key):
            return "", "That upload couldn't be found — please try again."
    except storage.StorageError:
        return "", "That upload couldn't be checked — please try again."
    return key, ""


def save_upload(up, subdir, allowed, max_mb=0):
    """Save an uploaded file under UPLOAD_DIR/subdir with a uuid name.
    Returns the relative path (e.g. 'avatars/abc123.png') or '' if no file.
    Raises ValueError on bad extension or oversize. max_mb=0 uses MAX_UPLOAD_MB.
    Streams to disk in 1MB chunks so a 200MB video never sits in RAM; an
    oversize upload aborts mid-stream and the partial file is removed."""
    if up is None or not getattr(up, "filename", ""):
        return ""
    fname = up.filename or ""
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    if ext not in allowed:
        raise ValueError(f"File type .{ext} not allowed.")
    cap_mb = max_mb or MAX_UPLOAD_MB
    if storage.r2_enabled():
        return _save_upload_r2(up, subdir, ext, cap_mb)
    limit = cap_mb * 1024 * 1024
    name = f"{uuid.uuid4().hex}.{ext}"
    dest = os.path.join(UPLOAD_DIR, subdir, name)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    src = up.file
    try:
        src.seek(0)
    except Exception:
        pass
    total = 0
    try:
        with open(dest, "wb") as f:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ValueError(
                        "That file couldn't be uploaded — "
                        "try a different one.")
                f.write(chunk)
    except Exception:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise
    return f"{subdir}/{name}"


# ---------------------------------------------------------------------------
# Build LOCKED CONTENT — paid media storage + access helpers.
# Locked files are stored under keys "locked/<kind>/<uuid>.<ext>"
# (kind = "posts" | "dm"). On disk they live in LOCKED_DIR (never
# UPLOAD_DIR, so the /uploads static mount can never serve them); in
# R2 they live under the locked/ prefix and are handed out only as a
# short-lived presigned GET minted by the gate routes — the public
# read_url is never used for them.
# ---------------------------------------------------------------------------

def save_locked_upload(up, kind, allowed, max_mb=0):
    """Save a locked-content file; returns its locked/ key. Mirrors
    save_upload's validation + streaming + cap discipline (max_mb=0
    uses MAX_UPLOAD_MB); raises ValueError with a user-facing
    message on refusal."""
    if up is None or not getattr(up, "filename", ""):
        return ""
    fname = up.filename or ""
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    if ext not in allowed:
        raise ValueError(f"File type .{ext} not allowed.")
    cap_mb = max_mb or MAX_UPLOAD_MB
    if storage.r2_enabled():
        return _save_locked_upload_r2(up, kind, ext, cap_mb)
    limit = cap_mb * 1024 * 1024
    name = f"{uuid.uuid4().hex}.{ext}"
    dest = os.path.join(LOCKED_DIR, kind, name)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    src = up.file
    try:
        src.seek(0)
    except Exception:
        pass
    total = 0
    try:
        with open(dest, "wb") as f:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise ValueError(
                        f"That file is too big (max {cap_mb} MB).")
                f.write(chunk)
    except Exception:
        try:
            os.remove(dest)
        except OSError:
            pass
        raise
    return f"locked/{kind}/{name}"


def _save_locked_upload_r2(up, kind, ext, cap_mb):
    """R2 twin of save_locked_upload (spool + one signed PUT), with
    the same cap policy as _save_upload_r2 for post videos."""
    uncapped = (kind == "posts" and ext in VID_EXTS)
    limit = None if uncapped else cap_mb * 1024 * 1024
    key = f"locked/{kind}/{uuid.uuid4().hex}.{ext}"
    src = up.file
    try:
        src.seek(0)
    except Exception:
        pass
    total = 0
    tmp = tempfile.TemporaryFile()
    try:
        while True:
            chunk = src.read(1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if limit is not None and total > limit:
                raise ValueError(_UPLOAD_FAIL)
            tmp.write(chunk)
        tmp.seek(0)
        ctype = (getattr(up, "content_type", "") or
                 mimetypes.guess_type(up.filename or "")[0] or
                 "application/octet-stream")
        try:
            storage.put_object(key, tmp, ctype, size=total)
        except storage.StorageError:
            raise ValueError(_UPLOAD_FAIL)
    finally:
        tmp.close()
    return key


def _has_unlock(conn, uid, ctype, cid):
    """True when uid has paid to open this content (unlocks are
    forever — rows are only removed when the content is deleted)."""
    if not uid:
        return False
    return conn.execute(
        "SELECT 1 FROM content_unlocks WHERE content_type=? "
        "AND content_id=? AND buyer_id=?",
        (ctype, cid, uid)).fetchone() is not None


def _serve_locked_media(key):
    """Serve a locked/ key to a caller the gate route has ALREADY
    authorized: presigned redirect in R2 mode, file off LOCKED_DIR
    in disk mode. Anything missing reads as 404."""
    if not key or not key.startswith("locked/"):
        return HTMLResponse("Not found", status_code=404)
    if storage.r2_enabled():
        try:
            return RedirectResponse(storage.presign_get(key),
                                    status_code=302)
        except storage.StorageError:
            return HTMLResponse("Not found", status_code=404)
    path = os.path.join(LOCKED_DIR, key[len("locked/"):])
    if os.path.isfile(path):
        return FileResponse(path)
    return HTMLResponse("Not found", status_code=404)


def video_embed_url(url):
    """YouTube/Vimeo watch URL -> embed src, or ''."""
    if not url:
        return ""
    u = str(url).strip()
    m = re.search(r"youtu\.be/([A-Za-z0-9_-]{6,})", u)
    if m:
        return f"https://www.youtube.com/embed/{m.group(1)}"
    m = re.search(r"youtube\.com/embed/([A-Za-z0-9_-]{6,})", u)
    if m:
        return f"https://www.youtube.com/embed/{m.group(1)}"
    if "youtube.com" in u or "youtu.be" in u:
        m = re.search(r"[?&]v=([A-Za-z0-9_-]{6,})", u)
        if m:
            return f"https://www.youtube.com/embed/{m.group(1)}"
    m = re.search(r"vimeo\.com/(\d+)", u)
    if m:
        return f"https://player.vimeo.com/video/{m.group(1)}"
    return ""


def friend_id_set(uid):
    conn = db()
    rows = conn.execute(
        "SELECT user_id, friend_id FROM friendships "
        "WHERE status='accepted' AND (user_id=? OR friend_id=?)", (uid, uid)).fetchall()
    conn.close()
    out = set()
    for r in rows:
        out.add(r["friend_id"] if r["user_id"] == uid else r["user_id"])
    return out


def is_friend(a_id, b_id):
    """True when a_id and b_id are accepted friends (either direction
    — friendships are stored once per pair)."""
    if not a_id or not b_id or a_id == b_id:
        return False
    return b_id in friend_id_set(a_id)


# ---------------- follows (one-way graph) ----------------
def following_ids(uid):
    """Everyone uid follows (the followees). A follow is created ONLY
    by an explicit POST /follow — nothing derives follows implicitly
    (not friendship, not story views, not boot-time migration)."""
    conn = db()
    rows = conn.execute(
        "SELECT followee_id FROM follows WHERE follower_id=?",
        (uid,)).fetchall()
    conn.close()
    return {r["followee_id"] for r in rows}


def follower_ids(uid):
    conn = db()
    rows = conn.execute(
        "SELECT follower_id FROM follows WHERE followee_id=?",
        (uid,)).fetchall()
    conn.close()
    return {r["follower_id"] for r in rows}


def follower_count(uid):
    conn = db()
    n = conn.execute(
        "SELECT COUNT(*) c FROM follows WHERE followee_id=?",
        (uid,)).fetchone()["c"]
    conn.close()
    return n


def following_count(uid):
    conn = db()
    n = conn.execute(
        "SELECT COUNT(*) c FROM follows WHERE follower_id=?",
        (uid,)).fetchone()["c"]
    conn.close()
    return n


# ---------------- invites ----------------
def invite_count(uid):
    """How many people joined HERE from uid's invite link."""
    conn = db()
    n = conn.execute(
        "SELECT COUNT(*) c FROM users WHERE invited_by=?",
        (uid,)).fetchone()["c"]
    conn.close()
    return n


def is_gold_inviter(uid):
    """Gold frame milestone: 3+ people joined from uid's invite."""
    return invite_count(uid) >= 3


def is_insider(uid):
    """HERE Insider milestone: 10+ people joined from uid's invite."""
    return invite_count(uid) >= 10


def ensure_invite_code(conn, user_row):
    """The user's invite code, generating + saving one on `conn` when
    the row doesn't have one yet (rows predating the column, or any
    insert path that skipped it)."""
    code = _uget(user_row, "invite_code") or ""
    if str(code).strip():
        return str(code).strip()
    code = gen_invite_code(conn)
    conn.execute("UPDATE users SET invite_code=? WHERE id=?",
                 (code, _uget(user_row, "id")))
    conn.commit()
    return code


def is_following(follower_id, followee_id):
    if not follower_id or not followee_id or follower_id == followee_id:
        return False
    conn = db()
    r = conn.execute(
        "SELECT 1 FROM follows WHERE follower_id=? AND followee_id=?",
        (follower_id, followee_id)).fetchone()
    conn.close()
    return bool(r)


def req_status(viewer_id, profile_id):
    """'none'|'pending_out'|'pending_in'|'friends' from viewer's perspective."""
    if viewer_id == profile_id:
        return "friends"
    conn = db()
    row = conn.execute(
        "SELECT user_id, status FROM friendships "
        "WHERE (user_id=? AND friend_id=?) OR (user_id=? AND friend_id=?)",
        (viewer_id, profile_id, profile_id, viewer_id)).fetchone()
    conn.close()
    if not row:
        return "none"
    if row["status"] == "accepted":
        return "friends"
    return "pending_out" if row["user_id"] == viewer_id else "pending_in"


def pending_id_set(uid):
    """User ids with a pending friend request in either direction."""
    conn = db()
    rows = conn.execute(
        "SELECT user_id, friend_id FROM friendships "
        "WHERE status='pending' AND (user_id=? OR friend_id=?)",
        (uid, uid)).fetchall()
    conn.close()
    out = set()
    for r in rows:
        out.add(r["friend_id"] if r["user_id"] == uid else r["user_id"])
    return out


def hidden_from_ids(post_id):
    """User ids the author chose to hide this specific post from."""
    conn = db()
    rows = conn.execute("SELECT user_id FROM post_hidden_from WHERE post_id=?",
                        (post_id,)).fetchall()
    conn.close()
    return {r["user_id"] for r in rows}


def comment_blocked_ids(post_id):
    """User ids the author has blocked from commenting on this post."""
    conn = db()
    rows = conn.execute(
        "SELECT user_id FROM post_comment_blocks WHERE post_id=?",
        (post_id,)).fetchall()
    conn.close()
    return {r["user_id"] for r in rows}


def _form_ids(form, field):
    """Parse a repeated checkbox form field into a set of ints."""
    out = set()
    try:
        vals = form.getlist(field)
    except Exception:
        vals = []
    if not vals:
        single = form.get(field)
        if single:
            vals = [single]
    for v in vals:
        try:
            out.add(int(v))
        except (TypeError, ValueError):
            pass
    return out


def taggable_pages(user_id):
    """Pages the user owns or is a member of (id+name, name order)."""
    conn = db()
    rows = conn.execute(
        "SELECT DISTINCT pg.id, pg.name FROM pages pg "
        "LEFT JOIN page_members pm ON pm.page_id=pg.id AND pm.user_id=? "
        "WHERE pg.owner_id=? OR pm.user_id IS NOT NULL ORDER BY pg.name",
        (user_id, user_id)).fetchall()
    conn.close()
    return [{"id": r["id"], "name": r["name"]} for r in rows]


def composer_choices(user):
    """(friends, pages) lists for the composer tag / hide-from pickers."""
    fids = friend_id_set(user["id"])
    conn = db()
    friends = []
    for fid in fids:
        f = conn.execute("SELECT id,name FROM users WHERE id=?", (fid,)).fetchone()
        if f:
            friends.append({"id": f["id"], "name": f["name"]})
    conn.close()
    friends.sort(key=lambda x: x["name"].lower())
    return friends, taggable_pages(user["id"])


def _tags_from_json(form, body, user):
    """Parse the composer @-picker's hidden tags_json field
    ([{kind:'user'|'page', id, name}, ...]) into (user_ids, page_ids).

    Server re-validates everything the client claimed: the id must be a
    real friend (users) or an owned/member page, AND the literal text
    '@<canonical name>' must appear in the post body — a tag whose @Name
    was deleted from the text is dropped here even if the client kept it.
    """
    uids, pids = set(), set()
    raw = form.get("tags_json") or ""
    if not raw:
        return uids, pids
    try:
        items = json.loads(raw)
    except Exception:
        return uids, pids
    if not isinstance(items, list):
        return uids, pids
    fids = friend_id_set(user["id"])
    page_names = {pg["id"]: pg["name"] for pg in taggable_pages(user["id"])}
    conn = db()
    for it in items[:20]:
        if not isinstance(it, dict):
            continue
        try:
            tid = int(it.get("id"))
        except (TypeError, ValueError):
            continue
        kind = it.get("kind")
        if kind == "user" and tid in fids:
            u = conn.execute("SELECT name FROM users WHERE id=?",
                             (tid,)).fetchone()
            if u and ("@" + u["name"]) in body:
                uids.add(tid)
        elif kind == "page" and tid in page_names:
            if ("@" + page_names[tid]) in body:
                pids.add(tid)
    conn.close()
    return uids, pids


_URL_TAIL = ".,;:!?"


def _split_url_tail(s):
    """Peel sentence punctuation off the end of a matched URL so
    'check https://x.com.' links the URL, not the period."""
    tail = ""
    while s and s[-1] in _URL_TAIL:
        tail = s[-1] + tail
        s = s[:-1]
    if s.endswith(")") and "(" not in s:
        tail = ")" + tail
        s = s[:-1]
    return s, tail


def linkify_body(body, tagged_users=(), tagged_pages=()):
    """HTML for post/comment text: the text is HTML-escaped FIRST, then
    bare URLs become clickable links, '@Name' mentions of this post's
    actual tags become profile/page links (Facebook-style), and
    #hashtags become links to their tag page (Build TAGS). A single
    regex pass means inserted links are never re-matched, and a tag
    name inside a URL is never linkified separately. Longest tag names
    match first so 'Anna' wins over 'Ann'. The hashtag alternative
    sits LAST and carries a (?<![\\w&]) guard: a '#' glued to a word
    (or to the '&' of an escaped entity like &#x27;) is never a
    hashtag, and a '#fragment' inside a bare URL is consumed by the
    URL alternative before the hashtag one can see it."""
    if not body:
        return ""
    text = html.escape(body)
    targets = {}
    for t in tagged_users:
        targets[html.escape("@" + t["name"])] = f"/user/{t['id']}"
    for t in tagged_pages:
        targets[html.escape("@" + t["name"])] = f"/page/{t['id']}"
    alts = [_URL_RE.pattern]
    if targets:
        needles = sorted(targets, key=len, reverse=True)
        alts.append("|".join(re.escape(n) for n in needles))
    alts.append(r"(?<![\w&])#(?P<htag>[A-Za-z0-9_]{1,50})")
    pat = re.compile("|".join(alts))

    def repl(m):
        s = m.group(0)
        if m.groupdict().get("htag"):
            return (f'<a class="tag-link" href="/tag/'
                    f'{m.group("htag").lower()}">{s}</a>')
        if s in targets:
            return f'<a class="tag-link" href="{targets[s]}">{s}</a>'
        core, tail = _split_url_tail(s)
        if len(core) <= len("https://"):
            return s
        return (f'<a href="{core}" target="_blank" rel="noopener">'
                f"{core}</a>{tail}")

    return pat.sub(repl, text)


def first_embed_in_text(body):
    """First YouTube/Vimeo URL in free text -> embed src, or ''."""
    if not body:
        return ""
    for m in _URL_RE.finditer(body):
        emb = video_embed_url(m.group(0))
        if emb:
            return emb
    return ""


# ---------------- discovery: profile views, suggested people, trending ----------------
def discover_people(uid, limit=10):
    """Who's HERE?: non-friends ranked by profile views received in
    the last 7 days. Falls back to newest signups when there is no view data
    yet, so the list is never empty. Excludes self, friends, anyone with
    a pending request in either direction, and anyone in a block
    relationship with the viewer (either direction)."""
    excluded = friend_id_set(uid) | pending_id_set(uid) \
        | blocked_id_set(uid) | {uid}
    conn = db()
    rows = conn.execute(
        "SELECT u.id, u.name, u.avatar, u.last_seen_at, u.status_mode, "
        "u.inbox_status, COUNT(v.id) AS views "
        "FROM users u LEFT JOIN profile_views v "
        "ON v.viewed_id=u.id AND v.viewed_at > datetime('now','-7 days') "
        "GROUP BY u.id ORDER BY views DESC, u.created_at DESC").fetchall()
    conn.close()
    out = []
    for r in rows:
        if r["id"] in excluded:
            continue
        out.append({"id": r["id"], "name": r["name"],
                    "avatar": avatar_url(r["avatar"]),
                    "presence": presence_of(r),
                    "req": req_status(uid, r["id"]),
                    "views": r["views"]})
        if len(out) >= limit:
            break
    return out


TREND_STOPWORDS = frozenset(
    "about after again against all also always and are because been before being "
    "both could does doing down each even every from further have having here hers "
    "him himself into just know like more most much need only other over same "
    "should some such than that their them then there these they this those "
    "through under until want were what when where which while with would your "
    "youre gonna gotta wanna".split())


def trending_topics(limit=10, viewer_id=None):
    """Top #hashtags from posts in the last 7 days. When nobody has used
    hashtags yet, falls back to the most frequent meaningful words.
    With a viewer, posts the viewer may not see never feed the trends:
    authors in a block relationship with the viewer, and private-profile
    authors the viewer isn't friends with (same rules as can_view_post)."""
    conn = db()
    rows = conn.execute(
        "SELECT body, user_id FROM posts WHERE created_at > "
        "datetime('now','-7 days')").fetchall()
    conn.close()
    if viewer_id:
        excluded = blocked_id_set(viewer_id)
        conn = db()
        priv_ids = {r["id"] for r in conn.execute(
            "SELECT id FROM users WHERE private_profile=1").fetchall()}
        conn.close()
        if priv_ids:
            fids = friend_id_set(viewer_id)
            excluded |= {pid for pid in priv_ids
                         if pid not in fids and pid != viewer_id}
        rows = [r for r in rows if r["user_id"] not in excluded]
    tags = {}
    for r in rows:
        # Build TAGS: count with parse_hashtags — the same definition
        # the indexer and the renderer use (no URL #fragments).
        for t in parse_hashtags(r["body"]):
            if len(t) >= 2:
                tags[t] = tags.get(t, 0) + 1
    if tags:
        top = sorted(tags.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
        return [{"label": "#" + t, "q": "#" + t, "count": c} for t, c in top]
    words = {}
    for r in rows:
        for w in re.findall(r"[a-zA-Z]{4,}", r["body"] or ""):
            w = w.lower()
            if w in TREND_STOPWORDS:
                continue
            words[w] = words.get(w, 0) + 1
    top = sorted(((w, c) for w, c in words.items() if c >= 2),
                 key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [{"label": w, "q": w, "count": c} for w, c in top]


def trending_tags(limit=10, viewer=None, min_count=2):
    """Build TAGS: the Trending Tags strip — top hashtags of the last
    7 days, counted from the post_hashtags index over exactly the
    posts THIS viewer may see (can_view_post gates + posts the viewer
    hid + silenced authors never count), so a tag's number never
    leaks a post the viewer couldn't open. A tag needs at least
    min_count visible posts so the strip is never one-off junk.
    Returns [{tag, label, count}] — the raw material a future
    interest ranking can weight by the viewer's own tags."""
    if not viewer:
        return []
    conn = db()
    rows = conn.execute(
        "SELECT ph.tag AS tag, p.* FROM post_hashtags ph "
        "JOIN posts p ON p.id = ph.post_id "
        "WHERE p.created_at > datetime('now','-7 days')").fetchall()
    hidden = {r["post_id"] for r in conn.execute(
        "SELECT post_id FROM hidden_posts WHERE user_id=?",
        (viewer["id"],)).fetchall()}
    conn.close()
    muted = muted_id_set(viewer["id"])
    counts = {}
    for r in rows:
        d = dict(r)
        if d["user_id"] in muted:
            continue
        if (d.get("shared_post_id") or d["id"]) in hidden:
            continue
        if not can_view_post(viewer, d):
            continue
        counts[d["tag"]] = counts.get(d["tag"], 0) + 1
    top = sorted(((t, c) for t, c in counts.items() if c >= min_count),
                 key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [{"tag": t, "label": "#" + t, "count": c} for t, c in top]


def can_view_post(viewer, p, owner_on=False):
    """Privacy gate. viewer: user dict or None. p: post row dict.
    Page posts follow page visibility (unchanged behavior); personal posts
    respect their privacy setting. On top of all of that, a viewer on the
    post's hide-from list never sees it (the author always does, and the
    site owner does in the owner context). Tagging never grants access.
    Two more pair rules sit on top: a block in either direction between
    viewer and author hides the post completely, and a private-profile
    author's personal posts are friends-only no matter the post's own
    privacy setting. The owner context bypasses both pair rules."""
    if not viewer:
        return False
    # Build WALL: a PENDING wall post is visible to NOBODY but the
    # wall owner (and the owner context) — not even its author.
    if p.get("wall_pending"):
        return bool(owner_on or viewer["id"] == p.get("wall_user_id"))
    if viewer["id"] == p["user_id"]:
        return True
    if not owner_on and blocked_either_way(viewer["id"], p["user_id"]):
        return False
    # Build WALL, approved posts: (a) a block between the viewer and
    # the WALL OWNER hides the post just like a block with the author
    # does; (b) the wall owner always sees posts on their own wall —
    # the post sits on their profile — ahead of the post's own privacy
    # and the author's hide-from list; (c) a PRIVATE wall owner's wall
    # is friends-only, exactly like their profile.
    if p.get("wall_user_id"):
        if not owner_on and blocked_either_way(viewer["id"],
                                                p["wall_user_id"]):
            return False
        if viewer["id"] == p["wall_user_id"]:
            return True
        if not owner_on:
            _wo = get_user(p["wall_user_id"])
            if _wo and _wo.get("private_profile") \
                    and viewer["id"] not in friend_id_set(_wo["id"]):
                return False
    if not owner_on and viewer["id"] in hidden_from_ids(p["id"]):
        return False
    if p.get("page_id"):
        return True
    if not owner_on:
        author = get_user(p["user_id"])
        if author and author.get("private_profile"):
            return viewer["id"] in friend_id_set(p["user_id"])
    priv = (p.get("privacy") or "public")
    if priv == "public":
        return True
    if priv == "friends":
        return viewer["id"] in friend_id_set(p["user_id"])
    return False  # onlyme


def _poll_for_post(conn, pid, viewer):
    """The games-pack poll attached to a post, shaped for the post
    card / post page and for the vote endpoint's JSON: every option
    with its vote count + percentage + whether it's the VIEWER's pick,
    plus the total and the viewer's pick. None when the post has no
    poll. Percentages are whole numbers (0 when nobody has voted)."""
    row = conn.execute("SELECT post_id FROM polls WHERE post_id=?",
                       (pid,)).fetchone()
    if not row:
        return None
    counts = {r["option_id"]: r["c"] for r in conn.execute(
        "SELECT option_id, COUNT(*) c FROM poll_votes WHERE post_id=? "
        "GROUP BY option_id", (pid,)).fetchall()}
    total = sum(counts.values())
    my_pick = None
    if viewer:
        mr = conn.execute(
            "SELECT option_id FROM poll_votes WHERE post_id=? "
            "AND user_id=?", (pid, viewer["id"])).fetchone()
        if mr:
            my_pick = mr["option_id"]
    options = []
    for o in conn.execute(
            "SELECT * FROM poll_options WHERE post_id=? "
            "ORDER BY idx ASC, id ASC", (pid,)).fetchall():
        n = counts.get(o["id"], 0)
        options.append({
            "id": o["id"], "text": o["text"], "votes": n,
            "pct": int(round(100.0 * n / total)) if total else 0,
            "mine": my_pick is not None and o["id"] == my_pick})
    return {"options": options, "total": total,
            "my_option_id": my_pick, "voted": my_pick is not None}


def post_card(p, viewer, owner_on=False):
    """Build the post-card dict the partials/post_card.html template expects.
    Share rows (shared_post_id set) render the ORIGINAL post's content with a
    share banner; card actions target the original post. Returns None when the
    viewer may not see the post (deleted original or privacy)."""
    conn = db()
    # Resolve shares: a share row renders the ORIGINAL post's content.
    share = None
    if p.get("shared_post_id"):
        share = {"id": p["id"], "by_id": p["user_id"],
                 "at": fancy_time(p["created_at"])}
        orig = conn.execute("SELECT * FROM posts WHERE id=?",
                            (p["shared_post_id"],)).fetchone()
        if not orig:
            conn.close()
            return None
        p = dict(orig)
    if viewer and not can_view_post(viewer, p, owner_on):
        conn.close()
        return None
    a = conn.execute("SELECT id,name,avatar,is_verified,"
                     "is_verified_user,is_verified_vendor,"
                     "private_profile,founding_no,creator_mode,"
                     "plus_until "
                     "FROM users WHERE id=?",
                     (p["user_id"],)).fetchone()
    likes = conn.execute("SELECT COUNT(*) c FROM likes WHERE post_id=?",
                         (p["id"],)).fetchone()["c"]
    gift_count = conn.execute(
        "SELECT COUNT(*) c FROM post_gifts WHERE post_id=?",
        (p["id"],)).fetchone()["c"]
    liked = False
    my_reaction = None
    if viewer:
        r = conn.execute(
            "SELECT kind FROM likes WHERE post_id=? AND user_id=?",
            (p["id"], viewer["id"])).fetchone()
        if r:
            liked = True
            my_reaction = r["kind"] or "like"
    reactions_top = [
        {"emoji": REACTION_EMOJI.get(r["kind"], "👍"),
         "key": r["kind"], "count": r["c"]}
        for r in conn.execute(
            "SELECT kind, COUNT(*) c FROM likes WHERE post_id=? "
            "GROUP BY kind ORDER BY c DESC LIMIT 3", (p["id"],)).fetchall()
        if r["kind"] in REACTION_EMOJI]
    cc = conn.execute("SELECT COUNT(*) c FROM comments WHERE post_id=?",
                      (p["id"],)).fetchone()["c"]
    share_count = conn.execute(
        "SELECT COUNT(*) c FROM posts WHERE shared_post_id=?",
        (p["id"],)).fetchone()["c"]
    hidden_by_viewer = False
    if viewer:
        # Hiding is by root post: hiding a share hides the original everywhere
        # for you, and vice versa.
        hidden_by_viewer = bool(conn.execute(
            "SELECT 1 FROM hidden_posts WHERE user_id=? AND post_id=?",
            (viewer["id"], p["id"])).fetchone())
    tagged_users, tagged_pages = [], []
    for t in conn.execute(
            "SELECT t.user_id, t.page_id, u.name AS uname, pg.name AS pname "
            "FROM post_tags t LEFT JOIN users u ON u.id=t.user_id "
            "LEFT JOIN pages pg ON pg.id=t.page_id "
            "WHERE t.post_id=? ORDER BY t.id", (p["id"],)).fetchall():
        if t["user_id"]:
            tagged_users.append({"id": t["user_id"],
                                 "name": t["uname"] or "Deleted user"})
        elif t["page_id"]:
            tagged_pages.append({"id": t["page_id"],
                                 "name": t["pname"] or "Deleted page"})
    hidden_from_count = conn.execute(
        "SELECT COUNT(*) c FROM post_hidden_from WHERE post_id=?",
        (p["id"],)).fetchone()["c"]
    # Per-post comment blocks: every viewer gets a flag (the post page
    # swaps the comment box for the blocked note); the author also gets
    # the blocked list itself, for the "Blocked from this post" sheet.
    _blocked_ids = comment_blocked_ids(p["id"])
    viewer_comment_blocked = bool(viewer and not share
                                  and viewer["id"] in _blocked_ids
                                  and viewer["id"] != p["user_id"])
    blocked_commenters = []
    if viewer and viewer["id"] == p["user_id"] and not share:
        for bid in sorted(_blocked_ids):
            bu = conn.execute("SELECT id,name FROM users WHERE id=?",
                              (bid,)).fetchone()
            blocked_commenters.append(
                {"id": bid, "name": bu["name"] if bu else "Deleted user"})
    # Eyeball popup data (author's own posts only): the author's friends
    # with their current hidden-from state pre-checked.
    audience_friends = []
    if viewer and viewer["id"] == p["user_id"] and not share:
        _hidden = hidden_from_ids(p["id"])
        _friends, _ = composer_choices(viewer)
        audience_friends = [{"id": f["id"], "name": f["name"],
                             "hidden": f["id"] in _hidden}
                            for f in _friends]
    page_name, page_id, is_page = "", 0, False
    if p.get("page_id"):
        pg = conn.execute("SELECT id,name FROM pages WHERE id=?",
                          (p["page_id"],)).fetchone()
        if pg:
            page_name, page_id, is_page = pg["name"], pg["id"], True
    # Build WALL: a wall post carries its wall owner for the banner;
    # shares resolve to the original post above, whose own wall target
    # (if any) is what shows.
    wall_owner_id = p.get("wall_user_id") or 0
    wall_owner_name = ""
    if wall_owner_id:
        wu = conn.execute("SELECT name FROM users WHERE id=?",
                          (wall_owner_id,)).fetchone()
        wall_owner_name = wu["name"] if wu else "Deleted user"
    shared_by_name = ""
    if share:
        s = conn.execute("SELECT name FROM users WHERE id=?",
                         (share["by_id"],)).fetchone()
        shared_by_name = s["name"] if s else "Deleted user"
    # Games pack: a poll rides under the post body when one is attached
    # (share rows resolve to the original above, so its poll shows).
    poll_data = _poll_for_post(conn, p["id"], viewer)
    conn.close()
    media = p["media"] or ""
    avatar = ""
    if a and a["avatar"]:
        avatar = avatar_url(a["avatar"])
    priv = p.get("privacy") or "public"
    # Build SHARE: the text riding along in share intents — the post's
    # first non-empty line, else the author's name.
    share_text = ""
    for _line in (p["body"] or "").splitlines():
        if _line.strip():
            share_text = " ".join(_line.split())[:100]
            break
    if not share_text:
        share_text = ((a["name"] if a else "Someone") + " on HERE")
    card = {
        "id": p["id"], "user_id": p["user_id"],
        "dom_id": ("post-share-%d" % share["id"]) if share else ("post-%d" % p["id"]),
        "author_name": a["name"] if a else "Deleted user",
        "author_founding_no": (a["founding_no"] if a else None),
        "author_is_plus": _is_plus(a) if a else False,
        "author_creator": bool(a["creator_mode"]) if a else False,
        "author_avatar": avatar,
        "author_verified": bool(a["is_verified"]) if a else False,
        "author_vuser": bool(a["is_verified_user"]) if a else False,
        "author_vvendor": bool(a["is_verified_vendor"]) if a else False,
        "kind": p["kind"], "body": p["body"] or "",
        "body_html": linkify_body(p["body"] or "", tagged_users, tagged_pages),
        "feeling": p["feeling"] or "", "link_url": p["link_url"] or "",
        "video_url": p["video_url"] or "",
        # Locked media is served ONLY by the /locked/post/{id} gate;
        # unlocked-kind media keeps the plain uploads URL.
        "media": (("/locked/post/%d" % p["id"])
                  if media.startswith("locked/")
                  else ("/uploads/" + media if media else "")),
        # Embeds: an old Video-kind post's URL wins, then an old Link-kind
        # post's URL, then the first YouTube/Vimeo URL pasted in the body
        # (links just work now — no separate link/video box anymore).
        "embed_url": (video_embed_url(p["video_url"])
                      or video_embed_url(p["link_url"])
                      or first_embed_in_text(p["body"])),
        # Attached song (YouTube): rendered as a music bar with a lazy
        # player; the snapshot fields ride on the post row itself.
        "music": ({"video_id": p.get("music_video_id"),
                   "title": p.get("music_title") or "",
                   "artist": p.get("music_artist") or "",
                   "thumb": p.get("music_thumb") or "",
                   "start": p.get("music_start") or 0}
                  if p.get("music_video_id") else None),
        "created_at": fancy_time(p["created_at"]),
        "likes": likes, "liked": liked, "comments_count": cc,
        "gift_count": gift_count,
        "my_reaction": my_reaction, "reactions_top": reactions_top,
        "reaction_kinds": REACTIONS,
        "share_count": share_count,
        "privacy": priv,
        "pinned": bool(p.get("pinned")) and not share,
        "can_pin": bool(viewer and viewer["id"] == p["user_id"]
                         and not p.get("page_id") and not share),
        "can_share": bool(viewer and not share and priv == "public"),
        # Build SHARE: the external share sheet is offered ONLY when
        # the /p/{id} public page would actually render this post to a
        # stranger — post privacy 'public' AND the author is not a
        # private profile. Anything else keeps the internal share
        # behavior above and never exposes a working external link.
        "can_share_external": bool(
            viewer and priv == "public"
            and not (a and a["private_profile"])),
        "share_url": "https://joincomehere.com/p/%d" % p["id"],
        "share_text": share_text,
        "can_hide": bool(viewer),
        "hidden_by_viewer": hidden_by_viewer,
        "is_share": bool(share),
        "share_id": share["id"] if share else 0,
        "shared_by_id": share["by_id"] if share else 0,
        "shared_by_name": shared_by_name,
        "shared_at": share["at"] if share else "",
        "can_unshare": bool(share and viewer and
                            (viewer["id"] == share["by_id"] or owner_on)),
        "can_delete": bool(not share and viewer and
                            (viewer["id"] == p["user_id"] or owner_on
                             or (wall_owner_id
                                 and viewer["id"] == wall_owner_id))),
        # Build WALL card fields.
        "is_wall_post": bool(wall_owner_id),
        "wall_owner_id": wall_owner_id,
        "wall_owner_name": wall_owner_name,
        "wall_pending": bool(p.get("wall_pending")),
        "is_page_post": is_page, "page_name": page_name, "page_id": page_id,
        "tagged_users": tagged_users, "tagged_pages": tagged_pages,
        "hidden_from_count": hidden_from_count,
        "viewer_comment_blocked": viewer_comment_blocked,
        "blocked_commenters": blocked_commenters,
        "audience_friends": audience_friends,
        "is_mine": bool(viewer and viewer["id"] == p["user_id"] and not share),
        "can_edit_audience": bool(viewer and viewer["id"] == p["user_id"]
                                  and not share),
        "poll": poll_data,
    }
    # --- Build LOCKED CONTENT: lock state + masking. Post privacy
    # already ran above (can_view_post): a lock never widens who can
    # see the post EXISTS, and unlocking never overrides privacy.
    # The author and the owner context always see the content open;
    # a buyer sees it open forever (content_unlocks row); everyone
    # else gets a LOCK CARD — body, feeling, links, embeds, music,
    # poll and the media URL are blanked HERE, server-side, so the
    # real content never reaches the page for a non-unlocker (no
    # view-source leak, no CSS-blur trick). Shares resolve to the
    # original above, so sharing a locked post shares the lock. ---
    lock = None
    _lprice = p.get("lock_price") or 0
    if _lprice:
        lock_open = bool(owner_on or (viewer
                                      and viewer["id"] == p["user_id"]))
        if not lock_open and viewer:
            _lc = db()
            lock_open = _has_unlock(_lc, viewer["id"], "post", p["id"])
            _lc.close()
        lock = {"price": _lprice, "adult": bool(p.get("lock_adult")),
                "open": lock_open}
        if not lock_open:
            card.update({
                "body": "", "body_html": "", "feeling": "",
                "link_url": "", "video_url": "", "media": "",
                "embed_url": "", "music": None, "poll": None,
                "share_text": "🔒 Locked content on HERE",
            })
    card["lock"] = lock
    return card


def flag_item(item_type, item_id, author_id, author_name, text):
    """Scan text; on hit insert a moderation_flags row. Item stays published."""
    flagged, reasons = moderation.scan(text or "", author_name or "")
    if flagged:
        excerpt = (text or "")[:140]
        conn = db()
        conn.execute(
            "INSERT INTO moderation_flags(item_type, item_id, author_id, reasons, "
            "excerpt, status, created_at) VALUES(?,?,?,?,?,'pending',?)",
            (item_type, item_id, author_id, ",".join(reasons), excerpt, now_iso()))
        conn.commit()
        conn.close()
    return flagged


def avatar_url(path):
    """Return the /uploads/ URL for a stored relative path, or '' when there
    is no path or the file is missing from disk. The '' case makes every
    template fall back to the letter-placeholder avatar instead of rendering
    a broken <img> (iOS shows those as a box-with-question-mark)."""
    if not path:
        return ""
    if storage.r2_enabled():
        # The object may live in R2 or on disk (pre-migration); the
        # /uploads route resolves either. A ref to a truly deleted
        # file renders a broken <img> instead of the placeholder —
        # accepted tradeoff (a HEAD per avatar render is too slow).
        return "/uploads/" + str(path)
    try:
        if not os.path.isfile(os.path.join(UPLOAD_DIR, str(path))):
            return ""
    except Exception:
        return ""
    return "/uploads/" + path


# ---------------- app install (PWA + Android APK) ----------------
@app.get("/sw.js")
async def service_worker():
    # Served from the root so the worker's scope is the whole site.
    # The worker itself never caches pages — see static/sw.js.
    return FileResponse(
        os.path.join(BASE, "static", "sw.js"),
        media_type="application/javascript",
        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"},
    )


@app.get("/download/android")
async def download_android():
    apk = os.path.join(BASE, "static", "here.apk")
    if not os.path.exists(apk):
        raise HTTPException(status_code=404, detail="Android app not available yet")
    return FileResponse(
        apk,
        media_type="application/vnd.android.package-archive",
        filename="here.apk",
    )


@app.get("/download/ios")
async def download_ios():
    # The IPA is built unsigned by GitHub Actions (see ios/ and
    # .github/workflows/build-ipa.yml) and committed back as
    # static/here.ipa; until the first CI run lands it, this 404s.
    # It installs via AltStore/SideStore sideloading — iOS cannot
    # install it straight from Safari (Apple's rule, explained on the
    # login page / /app next to the button).
    ipa = os.path.join(BASE, "static", "here.ipa")
    if not os.path.exists(ipa):
        raise HTTPException(status_code=404, detail="iPhone app not available yet")
    return FileResponse(
        ipa,
        media_type="application/octet-stream",
        filename="here.ipa",
    )


@app.get("/app", response_class=HTMLResponse)
async def app_page(request: Request):
    # The "Get the HERE app" card for people who are already logged in —
    # /login bounces them to /home, so they need their own way to reach it.
    user, err = require_login(request)
    if err:
        return err
    return render(request, "app.html")


# ---------------- push subscription endpoints ----------------
@app.get("/push/vapid-public-key")
async def push_vapid_public_key():
    # Public by design: browsers need the key to subscribe. It identifies
    # this server to the push services (the private half never leaves).
    if not _PUSH_OK:
        return JSONResponse(
            {"ok": False, "error": "Push is unavailable on this server."},
            status_code=503)
    try:
        _signer, pub = _vapid()
    except Exception:
        return JSONResponse(
            {"ok": False, "error": "Push is unavailable on this server."},
            status_code=503)
    return JSONResponse({"ok": True, "public_key": pub})


@app.post("/push/subscribe")
async def push_subscribe(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    endpoint = str(data.get("endpoint") or "").strip()
    keys = data.get("keys") or {}
    if not isinstance(keys, dict):
        keys = {}
    p256dh = str(keys.get("p256dh") or "").strip()
    auth = str(keys.get("auth") or "").strip()
    if not endpoint.startswith(("https://", "http://")) \
            or not p256dh or not auth:
        return JSONResponse(
            {"ok": False, "error": "That subscription doesn't look "
                                   "right."}, status_code=400)
    # One row per endpoint: re-subscribing (or a different account on
    # the same browser) replaces the row's owner + keys.
    stamp = now_iso()
    conn = db()
    conn.execute(
        "INSERT INTO push_subscriptions(user_id, endpoint, p256dh, auth, "
        "user_agent, created_at, last_seen) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(endpoint) DO UPDATE SET user_id=excluded.user_id, "
        "p256dh=excluded.p256dh, auth=excluded.auth, "
        "user_agent=excluded.user_agent, last_seen=excluded.last_seen",
        (user["id"], endpoint, p256dh, auth,
         (request.headers.get("user-agent") or "")[:300], stamp, stamp))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.post("/push/unsubscribe")
async def push_unsubscribe(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    endpoint = str(data.get("endpoint") or "").strip()
    if endpoint:
        # Scoped to the caller's own subscriptions: naming somebody
        # else's endpoint can't remove their row.
        conn = db()
        conn.execute(
            "DELETE FROM push_subscriptions WHERE endpoint=? AND "
            "user_id=?", (endpoint, user["id"]))
        conn.commit()
        conn.close()
    return JSONResponse({"ok": True})


@app.get("/push/status")
async def push_status(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    return JSONResponse({"ok": True, "devices": _push_sub_count(user["id"])})


@app.post("/push/test")
async def push_test(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    n = _push_sub_count(user["id"])
    if n:
        send_push(user["id"], "🔔 HERE",
                  "Test notification — if this buzzed your phone, "
                  "HERE notifications work.",
                  "/app", tag="test")
    return JSONResponse({"ok": True, "devices": n})


# ---------------- direct-to-R2 uploads (Build U) ----------------
# The browser asks here first; the server mints the object key itself
# (never trust a client-chosen key) and hands back a short-lived
# presigned PUT URL. The big file then travels browser -> R2 directly,
# and the post/story form submits with media_key instead of a file.
# Only VIDEO goes direct (that's the big-file problem); images keep
# relaying through save_upload, and in disk mode nothing is direct.
_PRESIGN_SURFACES = {"post": "posts", "story": "stories"}
_PRESIGN_VIDEO_TYPES = {"video/mp4": "mp4", "video/webm": "webm",
                        "video/quicktime": "mov"}


@app.post("/api/uploads/presign")
async def uploads_presign(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    subdir = _PRESIGN_SURFACES.get(str(data.get("surface") or ""))
    if subdir is None:
        return JSONResponse({"ok": False, "error": "surface"},
                            status_code=400)
    ext = _PRESIGN_VIDEO_TYPES.get(
        str(data.get("content_type") or "").lower())
    if not storage.r2_enabled() or ext is None:
        return JSONResponse({"ok": True, "direct": False})
    key = f"{subdir}/u{user['id']}-{uuid.uuid4().hex}.{ext}"
    try:
        url = storage.presign_put(key, expires=1800)
    except storage.StorageError:
        return JSONResponse({"ok": True, "direct": False})
    return JSONResponse({"ok": True, "direct": True,
                         "key": key, "url": url})


# ---------------- root / auth ----------------
@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    if current_user(request):
        return redir("/home")
    return redir("/login")


def _inviter_for_code(code):
    """The user who owns an invite code, or None. Exact match after
    stripping — codes are stored uppercase from INVITE_ALPHABET."""
    code = (code or "").strip()
    if not code:
        return None
    conn = db()
    r = conn.execute("SELECT * FROM users WHERE invite_code=?",
                     (code,)).fetchone()
    conn.close()
    return dict(r) if r else None


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    if current_user(request):
        return redir("/home")
    inviter = None
    inv = _inviter_for_code(request.cookies.get("here_invite", ""))
    if inv:
        inviter = {"name": inv["name"],
                   "avatar": avatar_url(inv.get("avatar", ""))}
    _taken, _left = founding_seat_counts()
    _pc = db()
    _paid = _paid_total_cents(_pc)
    _pc.close()
    return render(request, "auth/signup.html",
                  {"seats_taken": _taken, "seats_left": _left,
                   "paid_total_cents": _paid,
                   "error": request.query_params.get("error", ""),
                   "next": _safe_next(
                       request.query_params.get("next")),
                   "inviter": inviter})


@app.post("/signup")
async def signup(request: Request):
    form = await request.form()
    name = (form.get("name") or "").strip()
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    if not name or not email or not password:
        return redir("/signup?error=" + "Name, email and password are required.")
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    conn = db()
    try:
        cur = conn.execute(
            "INSERT INTO users(name, email, pw_hash, created_at, invite_code) "
            "VALUES(?,?,?,?,?)",
            (name, email, pw_hash, now_iso(), gen_invite_code(conn)))
        uid = cur.lastrowid
        # Original 100: while founding seats remain, this real
        # signup takes one. First hundred accounts, ever — when
        # they're gone, they're gone.
        conn.execute(
            "UPDATE users SET founding=1, founding_no="
            "(SELECT COUNT(*) FROM users WHERE founding=1) + 1 "
            "WHERE id=? AND "
            "(SELECT COUNT(*) FROM users WHERE founding=1) < 100",
            (uid,))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return redir("/signup?error=" + "That email is already registered.")
    conn.close()
    # Invite credit — ONLY after a successful insert (a duplicate-email
    # failure above returns early: no credit, and the invite cookie is
    # left in place so the visitor can retry with another email).
    inviter = _inviter_for_code(request.cookies.get("here_invite", ""))
    credited = False
    if inviter and inviter["id"] != uid:
        conn = db()
        conn.execute("UPDATE users SET invited_by=? WHERE id=?",
                     (inviter["id"], uid))
        conn.commit()
        conn.close()
        credited = True
        notify(inviter["id"], "invite",
               f"🎉 {name} joined HERE from your invite!", "/invite",
               actor_id=uid)
    token = new_session(uid)
    # Fresh accounts (onboarded=0) land on the welcome flow first;
    # everyone else keeps the plain /home landing.
    conn = db()
    _ob = conn.execute("SELECT onboarded FROM users WHERE id=?",
                       (uid,)).fetchone()
    conn.close()
    onboarded = _ob["onboarded"] if _ob and "onboarded" in _ob.keys() \
        else 0
    nxt = _safe_next(form.get("next"))
    resp = redir(nxt or ("/home" if onboarded else "/welcome"))
    if credited or request.cookies.get("here_invite"):
        resp.delete_cookie("here_invite", path="/")
    return set_session_cookie(resp, token, SESSION_COOKIE)


def _safe_next(raw):
    """A post-auth landing path we trust: same-site, never the
    owner panel, never protocol-relative."""
    raw = str(raw or "").strip()
    if (raw.startswith("/") and not raw.startswith("//")
            and "://" not in raw
            and not raw.startswith("/owner")):
        return raw
    return ""


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if current_user(request):
        return redir("/home")
    return render(request, "auth/login.html",
                  {"error": request.query_params.get("error", ""),
                   "next": _safe_next(
                       request.query_params.get("next"))})


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    password = form.get("password") or ""
    conn = db()
    u = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
    conn.close()
    if not u or not bcrypt.checkpw(password.encode(), u["pw_hash"].encode()):
        return redir("/login?error=" + "Invalid email or password.")
    token = new_session(u["id"])
    resp = redir(_safe_next(form.get("next")) or "/home")
    return set_session_cookie(resp, token, SESSION_COOKIE)


@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE, "")
    if token:
        conn = db()
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))
        conn.commit()
        conn.close()
    resp = redir("/login")
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# ---------------- password recovery (Build O) ----------------
# Forgot-password machinery, run day-to-day by Milli from the
# support mailbox (SUPPORT_EMAIL) + the owner panel queue. The rules
# that must never weaken:
#  * a reset link is only EVER sent to the email already on the
#    account — there is no "send it somewhere else" path, for anyone;
#  * POST /forgot answers IDENTICALLY whether or not the email has
#    an account (no enumeration), even when rate-limited;
#  * tokens carry 32 random bytes, are stored only as a sha256 hash,
#    work once, and die 1 hour after issue; a successful reset kills
#    every other session the account has;
#  * there is NO free-form AI anywhere in recovery — the on-page
#    helper is a fixed, scripted set of steps.
# Delivery: with HERE_SMTP_USER + HERE_SMTP_PASS set (a Gmail app
# password for the support mailbox), the site emails the link
# itself. Without SMTP — or when a send fails — the row waits in the
# owner panel as delivery='queued' for a hand-send.

SUPPORT_EMAIL = "wearehereyouarehere@gmail.com"
RESET_TTL = timedelta(hours=1)
RESET_MAX_PER_EMAIL_HOUR = 3
RESET_MAX_PER_IP_HOUR = 10


def _reset_token_hash(token):
    return hashlib.sha256((token or "").encode()).hexdigest()


def _smtp_cfg():
    """SMTP settings from env, or None when email delivery is off.
    HERE_SMTP_USER + HERE_SMTP_PASS are the required pair;
    HERE_SMTP_HOST / HERE_SMTP_PORT default to Gmail submission."""
    user = os.environ.get("HERE_SMTP_USER", "").strip()
    pw = os.environ.get("HERE_SMTP_PASS", "").strip()
    if not user or not pw:
        return None
    host = os.environ.get("HERE_SMTP_HOST", "").strip() \
        or "smtp.gmail.com"
    try:
        port = int(os.environ.get("HERE_SMTP_PORT", "") or 587)
    except ValueError:
        port = 587
    return {"user": user, "pass": pw, "host": host, "port": port}


def _send_reset_email(cfg, to_email, link):
    """Send the reset email to the ACCOUNT address. True/False —
    NEVER raises: a mail failure queues the request for the owner
    queue instead of erroring the visitor's page."""
    try:
        msg = EmailMessage()
        msg["From"] = cfg["user"]
        msg["To"] = to_email
        msg["Subject"] = "HERE \u2014 reset your password"
        msg.set_content(
            "Hi,\n\n"
            "You asked to reset your HERE password. Open this link "
            "within 1 hour to choose a new one:\n\n"
            f"{link}\n\n"
            "If you didn't ask for this, ignore this email \u2014 your "
            "password won't change.\n\n"
            "\u2014 HERE support")
        msg.add_alternative(
            "<p>Hi,</p>"
            "<p>You asked to reset your HERE password. "
            f'<a href="{html.escape(link, quote=True)}">Tap here to '
            "choose a new password</a> \u2014 the link works for "
            "1 hour.</p>"
            "<p>If you didn't ask for this, ignore this email \u2014 "
            "your password won't change.</p>"
            "<p>\u2014 HERE support</p>", subtype="html")
        with smtplib.SMTP(cfg["host"], cfg["port"], timeout=10) as s:
            s.starttls()
            s.login(cfg["user"], cfg["pass"])
            s.send_message(msg)
        return True
    except Exception:
        return False


def _reset_row_for_token(token):
    """The password_resets row for a raw token IF it is still usable
    (hash match, unused, unexpired) \u2014 else None."""
    if not token:
        return None
    conn = db()
    row = conn.execute(
        "SELECT * FROM password_resets WHERE token_hash=?",
        (_reset_token_hash(token),)).fetchone()
    conn.close()
    if not row or row["used_at"]:
        return None
    exp = _parse_ts(row["expires_at"])
    if not exp or exp <= datetime.now(timezone.utc):
        return None
    return row


@app.get("/forgot", response_class=HTMLResponse)
async def forgot_page(request: Request):
    if current_user(request):
        return redir("/home")
    return render(request, "auth/forgot.html", {"sent": False})


@app.post("/forgot", response_class=HTMLResponse)
async def forgot_submit(request: Request):
    form = await request.form()
    email = (form.get("email") or "").strip().lower()
    ip = request.client.host if request.client else ""
    if email:
        conn = db()
        u = conn.execute("SELECT * FROM users WHERE email=?",
                         (email,)).fetchone()
        if u:
            now = datetime.now(timezone.utc)
            since = (now - timedelta(hours=1)).isoformat()
            n_email = conn.execute(
                "SELECT COUNT(*) c FROM password_resets WHERE "
                "user_id=? AND created_at>?",
                (u["id"], since)).fetchone()["c"]
            n_ip = 0
            if ip:
                n_ip = conn.execute(
                    "SELECT COUNT(*) c FROM password_resets WHERE "
                    "requester_ip=? AND created_at>?",
                    (ip, since)).fetchone()["c"]
            if (n_email < RESET_MAX_PER_EMAIL_HOUR
                    and n_ip < RESET_MAX_PER_IP_HOUR):
                token = secrets.token_urlsafe(32)
                stamp = now.isoformat()
                # One live token per account: every older unused
                # token dies (and its queued raw copy is wiped) the
                # moment a new one is minted.
                conn.execute(
                    "UPDATE password_resets SET used_at=?, "
                    "token_plain=NULL WHERE user_id=? AND "
                    "used_at IS NULL", (stamp, u["id"]))
                cur = conn.execute(
                    "INSERT INTO password_resets(user_id, token_hash, "
                    "token_plain, delivery, created_at, expires_at, "
                    "requester_ip) VALUES(?,?,?,'queued',?,?,?)",
                    (u["id"], _reset_token_hash(token), token, stamp,
                     (now + RESET_TTL).isoformat(), ip))
                rid = cur.lastrowid
                conn.commit()
                cfg = _smtp_cfg()
                if cfg and _send_reset_email(
                        cfg, u["email"],
                        _abs_url(request, f"/reset/{token}")):
                    conn.execute(
                        "UPDATE password_resets SET "
                        "delivery='emailed', sent_at=?, "
                        "token_plain=NULL WHERE id=?",
                        (now_iso(), rid))
                    conn.commit()
        conn.close()
    # The SAME page in every case \u2014 account found or not, emailed
    # or queued, even rate-limited: nobody can probe which emails
    # have accounts from this response.
    return render(request, "auth/forgot.html", {"sent": True})


@app.get("/reset/{token}", response_class=HTMLResponse)
async def reset_page(request: Request, token: str):
    if not _reset_row_for_token(token):
        return render(request, "auth/reset.html", {"dead": True})
    return render(request, "auth/reset.html",
                  {"dead": False, "token": token, "error": ""})


@app.post("/reset/{token}")
async def reset_submit(request: Request, token: str):
    row = _reset_row_for_token(token)
    if not row:
        return render(request, "auth/reset.html", {"dead": True})
    form = await request.form()
    pw1 = form.get("password") or ""
    pw2 = form.get("confirm") or ""
    # Signup's only strength rule is non-empty; the one extra rule
    # here is that the two boxes must match. A refused attempt does
    # NOT consume the token.
    if not pw1:
        return render(request, "auth/reset.html",
                      {"dead": False, "token": token,
                       "error": "Enter a new password."})
    if pw1 != pw2:
        return render(request, "auth/reset.html",
                      {"dead": False, "token": token,
                       "error": "Those passwords don't match \u2014 "
                               "try again."})
    pw_hash = bcrypt.hashpw(pw1.encode(), bcrypt.gensalt()).decode()
    conn = db()
    # Claim the token in the same breath: the guarded UPDATE only
    # lands while the token is still unused, so a double-submit (or
    # a race) can never set two passwords from one link.
    cur = conn.execute(
        "UPDATE password_resets SET used_at=?, token_plain=NULL "
        "WHERE id=? AND used_at IS NULL", (now_iso(), row["id"]))
    claimed = cur.rowcount
    if claimed == 1:
        conn.execute("UPDATE users SET pw_hash=? WHERE id=?",
                     (pw_hash, row["user_id"]))
        # A reset kills every session the account has, everywhere.
        conn.execute("DELETE FROM sessions WHERE user_id=?",
                     (row["user_id"],))
    conn.commit()
    conn.close()
    if claimed != 1:
        return render(request, "auth/reset.html", {"dead": True})
    return redir("/login",
                 "Password changed \u2014 log in with your new password.")


# ---------------- public pages + invites (growth door) ----------------
# The ONLY anonymous surface beyond /, /login and /signup: /p/{post},
# /l/{live} and /u/{user} render a public face ONLY when the content is
# genuinely public (see the gates in each route); everything else gets
# the join teaser, which leaks nothing — no body, no author name, no
# media. The teaser and the public pages are standalone templates (no
# base.html): a logged-out visitor has no nav to render.
def _public_teaser(request: Request, logged_in):
    return render(request, "public_teaser.html",
                  {"logged_in": bool(logged_in)})


def _abs_url(request: Request, path):
    return str(request.base_url).rstrip("/") + path


@app.get("/p/{post_id}", response_class=HTMLResponse)
async def public_post(request: Request, post_id: int):
    viewer = current_user(request)
    conn = db()
    row = conn.execute("SELECT * FROM posts WHERE id=?",
                       (post_id,)).fetchone()
    p = dict(row) if row else None
    author = None
    if p:
        arow = conn.execute("SELECT * FROM users WHERE id=?",
                            (p["user_id"],)).fetchone()
        author = dict(arow) if arow else None
    # Build WALL: a pending wall post never renders publicly, and a
    # wall post on a PRIVATE profile's wall stays behind that wall's
    # privacy even when the post itself is public.
    wall_owner_private = False
    if p and p.get("wall_user_id"):
        wrow = conn.execute("SELECT private_profile FROM users "
                            "WHERE id=?", (p["wall_user_id"],)).fetchone()
        wall_owner_private = bool(wrow and wrow["private_profile"])
    if not p or p.get("shared_post_id") or not author \
            or p.get("wall_pending") or wall_owner_private \
            or (p.get("privacy") or "public") != "public" \
            or author.get("private_profile"):
        conn.close()
        return _public_teaser(request, viewer)
    like_count = conn.execute(
        "SELECT COUNT(*) c FROM likes WHERE post_id=?",
        (p["id"],)).fetchone()["c"]
    comment_count = conn.execute(
        "SELECT COUNT(*) c FROM comments WHERE post_id=?",
        (p["id"],)).fetchone()["c"]
    # Build LOCKED CONTENT: a locked post renders open on this
    # public page ONLY for a logged-in author or buyer (the owner
    # context too); everyone else — anonymous visitors and link
    # scrapers included — gets the generic teaser, exactly like a
    # private post. The og image NEVER points at locked media.
    _locked = bool(p.get("lock_price"))
    if _locked:
        _lock_open = bool(
            is_owner(request)
            or (viewer and (viewer["id"] == p["user_id"]
                            or _has_unlock(conn, viewer["id"],
                                           "post", p["id"]))))
        if not _lock_open:
            conn.close()
            return _public_teaser(request, viewer)
    conn.close()
    media = p.get("media") or ""
    media_url = ""
    if media:
        media_url = ("/locked/post/%d" % p["id"]
                     if media.startswith("locked/")
                     else "/uploads/" + media)
    media_kind = ""
    if media:
        ext = media.rsplit(".", 1)[-1].lower() if "." in media else ""
        if ext in IMG_EXTS:
            media_kind = "photo"
        elif ext in VID_EXTS:
            media_kind = "video"
        elif ext in AUD_EXTS:
            media_kind = "audio"
    embed_url = (video_embed_url(p.get("video_url"))
                 or video_embed_url(p.get("link_url"))
                 or first_embed_in_text(p.get("body")))
    music_embed = ""
    if p.get("music_video_id"):
        music_embed = ("https://www.youtube.com/embed/"
                       + str(p["music_video_id"]))
    author_ctx = {"id": author["id"], "name": author["name"],
                  "avatar": avatar_url(author.get("avatar", ""))}
    og_image = ""
    if media_kind == "photo" and media_url and not _locked:
        og_image = _abs_url(request, media_url)
    # No avatar fallback: share previews use the HERE logo
    # (template fallback og-logo.jpg) — Ken's call, 2026-10-25.
    return render(request, "public_post.html", {
        "author": author_ctx,
        "body": p.get("body") or "",
        "media_url": media_url, "media_kind": media_kind,
        "embed_url": embed_url, "music_embed": music_embed,
        "like_count": like_count, "comment_count": comment_count,
        "created": fancy_time(p.get("created_at")),
        "excerpt": ((p.get("body") or "").strip()[:200]
                    or f"{author['name']} posted on HERE — join free "
                       "to see more."),
        "og_image": og_image,
        "logged_in": bool(viewer),
    })


@app.get("/l/{live_id}", response_class=HTMLResponse)
async def public_live(request: Request, live_id: int):
    viewer = current_user(request)
    conn = db()
    _sweep_lives(conn)
    live = _live_get(conn, live_id)
    host = get_user(live["host_id"]) if live else None
    if not live or live.get("status") != "live" or not host \
            or host.get("private_profile"):
        conn.close()
        return _public_teaser(request, viewer)
    viewers = _live_viewer_count(conn, live_id)
    conn.close()
    host_ctx = {"id": host["id"], "name": host["name"],
                "avatar": avatar_url(host.get("avatar", ""))}
    # Share preview = HERE logo (template fallback), never the host's face.
    og_image = ""
    return render(request, "public_live.html", {
        "live": {"id": live["id"], "title": live.get("title") or ""},
        "host": host_ctx, "viewers": viewers,
        "og_image": og_image,
        "logged_in": bool(viewer),
    })


@app.get("/u/{user_id}", response_class=HTMLResponse)
async def public_profile(request: Request, user_id: int):
    viewer = current_user(request)
    prof = get_user(user_id)
    if not prof or prof.get("private_profile"):
        return _public_teaser(request, viewer)
    conn = db()
    prows = conn.execute(
        "SELECT id, media FROM posts WHERE user_id=? AND page_id IS NULL "
        "AND shared_post_id IS NULL AND (privacy IS NULL OR privacy='' "
        "OR privacy='public') "
        "AND media IS NOT NULL AND media != '' "
        # Build LOCKED CONTENT: locked posts never appear in the
        # public photo grid — their media is not public at any price
        # of scraping; buyers open them from the post itself.
        "AND COALESCE(lock_price, 0) = 0 "
        "ORDER BY created_at DESC LIMIT 30", (user_id,)).fetchall()
    conn.close()
    photos = []
    for r in prows:
        media = r["media"] or ""
        ext = media.rsplit(".", 1)[-1].lower() if "." in media else ""
        if ext in IMG_EXTS:
            kind = "photo"
        elif ext in VID_EXTS:
            kind = "video"
        else:
            continue
        photos.append({"post_id": r["id"], "kind": kind,
                       "url": "/uploads/" + media})
    prof_ctx = {"id": prof["id"], "name": prof["name"],
                "avatar": avatar_url(prof.get("avatar", "")),
                "spot_name": (prof.get("spot_name") or "").strip(),
                "follower_count": follower_count(user_id),
                "following_count": following_count(user_id),
                # Build LOOKS: even logged-out visitors see the
                # profile in its owner's look.
                "look_style": look_scope_style_for(prof),
                "look_classes": look_scope_class_for(prof),
                # Build SHINE: the owner's scheme gates the public
                # card's lit-glass finish too.
                "look_scheme_class": look_scheme_class_for(prof),
                "founding": (prof["founding"]
                             if "founding" in prof.keys() else 0),
                "founding_no": (prof["founding_no"]
                                if "founding_no" in prof.keys()
                                else None)}
    # Share preview = HERE logo (template fallback), never the person's face.
    og_image = ""
    return render(request, "public_profile.html", {
        "profile": prof_ctx, "photos": photos,
        "og_image": og_image,
        "logged_in": bool(viewer),
    })


@app.get("/join/{code}")
async def join_invite(request: Request, code: str):
    inviter = _inviter_for_code(code)
    if not inviter:
        return redir("/signup")
    resp = redir("/signup")
    resp.set_cookie("here_invite", (code or "").strip(), httponly=True,
                    samesite="lax", max_age=30 * 86400, path="/")
    return resp


@app.get("/invite", response_class=HTMLResponse)
async def invite_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    code = ensure_invite_code(conn, user)
    rows = conn.execute(
        "SELECT id, name, avatar, created_at FROM users "
        "WHERE invited_by=? ORDER BY created_at DESC",
        (user["id"],)).fetchall()
    conn.close()
    invitees = [{"id": r["id"], "name": r["name"],
                 "avatar": avatar_url(r["avatar"]),
                 "joined": fancy_time(r["created_at"])} for r in rows]
    _bc = db()
    bonus_total = _bc.execute(
        "SELECT COALESCE(SUM(tickets),0) AS s FROM ticket_txns "
        "WHERE user_id=? AND kind='invite_bonus'",
        (user["id"],)).fetchone()["s"]
    _race = _race_mine(_bc, user["id"])
    _bc.close()
    count = len(invitees)
    link = _abs_url(request, "/join/" + code)
    return render(request, "invite.html", {
        "invite_link": link,
        "qr_src": ("https://api.qrserver.com/v1/create-qr-code/"
                   "?size=180x180&data=" + quote(link, safe="")),
        "invitees": invitees, "count": count,
        "bonus_total": bonus_total,
        "race": {**_race,
                 "phase": _race_phase(),
                 "min": RACE_MIN_QUALIFIED},
        "gold_at": 3, "insider_at": 10,
        "gold_pct": min(100, int(count * 100 / 3)),
        "insider_pct": min(100, int(count * 100 / 10)),
        "is_gold": count >= 3, "is_insider": count >= 10,
    })


# ---------------- welcome flow + daily challenge ----------------
def _saved_topics(conn, uid):
    return [r["topic"] for r in conn.execute(
        "SELECT topic FROM user_topics WHERE user_id=?", (uid,)).fetchall()]


def _welcome_suggestions(viewer):
    """People-to-follow payload for /welcome (also served as JSON).
    Ranked by (a) distinct hashtags on the author's posts from the
    last 30 days matching MY saved topics, desc; (b) follower count
    desc; (c) most recent post, newest first (no posts last). Self
    and anyone blocked either way with me never appear."""
    conn = db()
    my_topics = _saved_topics(conn, viewer["id"])
    my_set = set(my_topics)
    blocked = blocked_id_set(viewer["id"])
    following = following_ids(viewer["id"])
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=30)).isoformat()
    rows = conn.execute("SELECT id, name, avatar FROM users WHERE id!=?",
                        (viewer["id"],)).fetchall()
    out = []
    for u in rows:
        if u["id"] in blocked:
            continue
        matched = []
        if my_set:
            ph = ",".join("?" * len(my_set))
            hit = {r["tag"] for r in conn.execute(
                "SELECT DISTINCT ph.tag FROM post_hashtags ph "
                "JOIN posts p ON p.id=ph.post_id "
                f"WHERE p.user_id=? AND p.created_at>=? AND ph.tag IN ({ph})",
                (u["id"], cutoff, *sorted(my_set))).fetchall()}
            matched = [k for k in TOPIC_KEYS if k in hit]
        fc = conn.execute(
            "SELECT COUNT(*) c FROM follows WHERE followee_id=?",
            (u["id"],)).fetchone()["c"]
        last = conn.execute(
            "SELECT MAX(created_at) m FROM posts WHERE user_id=?",
            (u["id"],)).fetchone()["m"] or ""
        out.append({"id": u["id"], "name": u["name"],
                    "avatar": avatar_url(u["avatar"]),
                    "follower_count": fc, "matched_topics": matched,
                    "matched_labels": [TOPIC_LABELS.get(k, k)
                                       for k in matched],
                    "following": u["id"] in following,
                    "_match_n": len(matched), "_fc": fc, "_last": last})
    conn.close()
    # Stable sorts, least significant key first: recency, then
    # followers, then topic matches (the primary key) last.
    out.sort(key=lambda x: x["_last"], reverse=True)
    out.sort(key=lambda x: -x["_fc"])
    out.sort(key=lambda x: -x["_match_n"])
    for o in out:
        del o["_match_n"], o["_fc"], o["_last"]
    return out[:8]


def _welcome_trending(viewer):
    """Top 6 trending My Views for the welcome page: feed-viewable
    videos from the last 14 days, scored 2*reactions + 3*comments
    (ties: newest first). Video URL mirrors _views_feed exactly."""
    conn = db()
    cands = _views_candidates(conn, viewer)
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=14)).isoformat()
    scored = []
    for p in cands:
        if (p.get("created_at") or "") < cutoff:
            continue
        rc = conn.execute(
            "SELECT COUNT(*) c FROM likes WHERE post_id=?",
            (p["id"],)).fetchone()["c"]
        cc = conn.execute(
            "SELECT COUNT(*) c FROM comments WHERE post_id=?",
            (p["id"],)).fetchone()["c"]
        # Build LOCKED CONTENT: locked Views join welcome trending
        # only for a viewer entitled to open them (author/buyer),
        # and then only through the /locked gate.
        _wurl = "/uploads/" + p["media"]
        if p.get("lock_price"):
            if not (p["user_id"] == viewer["id"]
                    or _has_unlock(conn, viewer["id"], "post",
                                   p["id"])):
                continue
            _wurl = "/locked/post/%d" % p["id"]
        scored.append({"post_id": p["id"],
                       "video_url": _wurl,
                       "poster_url": _wurl,
                       "media": _wurl,
                       "reaction_count": rc, "comment_count": cc,
                       "_score": 2 * rc + 3 * cc,
                       "_created": p["created_at"],
                       "_author_id": p["user_id"]})
    conn.close()
    scored.sort(key=lambda it: it["_created"], reverse=True)
    scored.sort(key=lambda it: -it["_score"])
    out = []
    for it in scored[:6]:
        a = get_user(it["_author_id"])
        out.append({"post_id": it["post_id"],
                    "video_url": it["video_url"],
                    "poster_url": it["poster_url"],
                    "media": it["media"],
                    "author_id": it["_author_id"],
                    "author_name": a["name"] if a else "Deleted user",
                    "author_avatar": (avatar_url(a["avatar"])
                                      if a else ""),
                    "reaction_count": it["reaction_count"],
                    "comment_count": it["comment_count"]})
    return out


@app.get("/welcome", response_class=HTMLResponse)
async def welcome_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    saved = _saved_topics(conn, user["id"])
    conn.close()
    topics = [{"key": k, "label": label, "selected": k in saved}
              for k, label in TOPICS]
    return render(request, "welcome.html", {
        "topics": topics, "saved_topics": saved,
        "suggestions": _welcome_suggestions(user),
        "trending": _welcome_trending(user),
        "onboarded": bool(user.get("onboarded")),
        # Build LOOKS: the 🎨 Pick your vibe preset cards.
        "look_presets": look_preset_views(),
        "look_scheme": look_scheme_for(user),
    })


@app.post("/welcome/look")
async def welcome_look(request: Request):
    """🎨 Pick your vibe: one tap on a welcome preset card applies
    that look to the (new) user immediately, through the same
    apply_preset/apply_look path as Settings. Skipping the step —
    or picking nothing — keeps the classic glass default."""
    user, err = require_login(request)
    if err:
        return err
    form = await request.form()
    conn = db()
    name = apply_preset(conn, user["id"], (form.get("preset") or "")
                        .strip())
    conn.commit()
    conn.close()
    if name:
        return redir("/welcome",
                     f"Look applied: {name} 🎨 Change it anytime in "
                     "Settings → 🎨 My Look.")
    return redir("/welcome")


@app.post("/welcome/topics")
async def welcome_topics(request: Request):
    user, err = require_login(request)
    if err:
        return err
    try:
        data = await request.json()
    except Exception:
        data = {}
    raw = []
    if isinstance(data, dict) and "topics" in data:
        raw = data.get("topics") or []
    if not raw:
        try:
            form = await request.form()
            vals = form.getlist("topics")
            if len(vals) == 1:
                v = (vals[0] or "").strip()
                if v.startswith("["):
                    try:
                        raw = json.loads(v)
                    except Exception:
                        raw = [v]
                elif v:
                    raw = [x.strip() for x in v.split(",")]
            elif vals:
                raw = list(vals)
        except Exception:
            raw = []
    if isinstance(raw, str):
        raw = [raw]
    saved = []
    for t in raw if isinstance(raw, (list, tuple)) else []:
        k = str(t).strip().lower()
        if k in TOPIC_KEYS and k not in saved:
            saved.append(k)
        if len(saved) >= 5:
            break
    conn = db()
    conn.execute("DELETE FROM user_topics WHERE user_id=?", (user["id"],))
    for k in saved:
        conn.execute(
            "INSERT OR IGNORE INTO user_topics(user_id, topic) VALUES(?,?)",
            (user["id"], k))
        _affinity_bump(conn, user["id"], "tag", k, 4.0)
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "topics": saved})


@app.get("/welcome/suggestions.json")
async def welcome_suggestions_json(request: Request):
    user, err = require_login(request)
    if err:
        return err
    return JSONResponse({"ok": True,
                         "suggestions": _welcome_suggestions(user)})


@app.post("/welcome/done")
async def welcome_done(request: Request):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    conn.execute("UPDATE users SET onboarded=1 WHERE id=?", (user["id"],))
    conn.commit()
    conn.close()
    return redir("/home")


def _daily_visible_posts(viewer, day_str, owner_on):
    """Post rows answering the challenge on `day_str` that the viewer
    may see: can_view_post passes and the author is neither blocked
    either way with the viewer nor muted by the viewer (feed rules).
    Newest first."""
    conn = db()
    rows = conn.execute(
        "SELECT * FROM posts WHERE challenge_date=? "
        "ORDER BY created_at DESC", (day_str,)).fetchall()
    conn.close()
    blocked = blocked_id_set(viewer["id"])
    muted = muted_id_set(viewer["id"])
    out = []
    for r in rows:
        d = dict(r)
        if d["user_id"] in blocked or d["user_id"] in muted:
            continue
        if not can_view_post(viewer, d, owner_on):
            continue
        out.append(d)
    return out


@app.get("/daily", response_class=HTMLResponse)
async def daily_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    owner_on = is_owner(request)
    today = _today_chicago()
    yday = today - timedelta(days=1)
    answers = []
    for d in _daily_visible_posts(user, _today_str(), owner_on):
        card = post_card(d, user, owner_on)
        if card:
            answers.append(card)
    ycount = len(_daily_visible_posts(user, yday.isoformat(), owner_on))
    blocked = blocked_id_set(user["id"])
    conn = db()
    cand_ids = [r["user_id"] for r in conn.execute(
        "SELECT DISTINCT user_id FROM user_activity_days").fetchall()]
    conn.close()
    board = []
    for uid in cand_ids:
        if uid in blocked:
            continue
        s = current_streak(uid)
        if s <= 0:
            continue
        u = get_user(uid)
        if not u:
            continue
        board.append({"id": uid, "name": u["name"],
                      "avatar": avatar_url(u.get("avatar", "")),
                      "streak": s})
    board.sort(key=lambda x: x["name"].lower())
    board.sort(key=lambda x: -x["streak"])
    return render(request, "daily.html", {
        "prompt": daily_prompt(today),
        "daily_date": today.strftime("%A, %B %d, %Y"),
        "daily_today": _today_str(),
        "streak": current_streak(user["id"]),
        "leaderboard": board[:5],
        "answers": answers, "answers_count": len(answers),
        "yesterday_prompt": daily_prompt(yday),
        "yesterday_count": ycount,
    })


# ---------------- home / posts ----------------
def _home_posts(user, owner_on):
    """Own posts + friends' posts + posts on joined pages, newest first.
    Respects per-post privacy and the viewer's hidden-posts list. Posts
    by people the viewer silenced (muted) never appear — silencing is
    about your own space, so it applies here and only here."""
    fids = friend_id_set(user["id"])
    muted = muted_id_set(user["id"])
    conn = db()
    page_ids = [r["page_id"] for r in conn.execute(
        "SELECT page_id FROM page_members WHERE user_id=?", (user["id"],)).fetchall()]
    hidden = {r["post_id"] for r in conn.execute(
        "SELECT post_id FROM hidden_posts WHERE user_id=?", (user["id"],)).fetchall()}
    conn.close()
    clauses = ["p.user_id = ?"]
    params = [user["id"]]
    if fids:
        clauses.append("p.user_id IN (%s)" % ",".join("?" * len(fids)))
        params += list(fids)
    if page_ids:
        clauses.append("(p.page_id IS NOT NULL AND p.page_id IN (%s))"
                       % ",".join("?" * len(page_ids)))
        params += page_ids
    conn = db()
    rows = conn.execute(
        "SELECT p.* FROM posts p WHERE " + " OR ".join(clauses) +
        " ORDER BY p.created_at DESC LIMIT 200", params).fetchall()
    conn.close()
    # For share rows the visible content is the ORIGINAL post, so a
    # muted author hides shares of their posts too.
    orig_authors = {}
    share_ids = [r["shared_post_id"] for r in rows if r["shared_post_id"]]
    if share_ids:
        conn = db()
        for orow in conn.execute(
                "SELECT id, user_id FROM posts WHERE id IN (%s)"
                % ",".join("?" * len(share_ids)), share_ids).fetchall():
            orig_authors[orow["id"]] = orow["user_id"]
        conn.close()
    cards = []
    for r in rows:
        d = dict(r)
        # hidden_posts stores root post ids: hiding hides the original and
        # all of its shares for you.
        if (d.get("shared_post_id") or d["id"]) in hidden:
            continue
        if muted and d["user_id"] != user["id"] and (
                d["user_id"] in muted
                or orig_authors.get(d.get("shared_post_id")) in muted):
            continue
        card = post_card(d, user, owner_on)
        if card:
            cards.append(card)
    return cards


@app.get("/api/tag-suggest")
async def tag_suggest(request: Request):
    """Composer @-tag autocomplete: the current user's friends plus pages
    they own or belong to, filtered by ?q= (case-insensitive substring).
    Up to 8 results, friends first. Login required."""
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    q = (request.query_params.get("q") or "").strip().lower()
    uid = user["id"]
    conn = db()
    out = []
    frows = []
    for fid in friend_id_set(uid):
        f = conn.execute("SELECT id,name,avatar FROM users WHERE id=?",
                         (fid,)).fetchone()
        if f:
            frows.append(f)
    frows.sort(key=lambda r: r["name"].lower())
    for f in frows:
        if q and q not in f["name"].lower():
            continue
        out.append({"kind": "user", "id": f["id"], "name": f["name"],
                    "avatar": avatar_url(f["avatar"])})
    prows = conn.execute(
        "SELECT DISTINCT pg.id, pg.name, pg.cover FROM pages pg "
        "LEFT JOIN page_members pm ON pm.page_id=pg.id AND pm.user_id=? "
        "WHERE pg.owner_id=? OR pm.user_id IS NOT NULL ORDER BY pg.name",
        (uid, uid)).fetchall()
    conn.close()
    for pg in prows:
        if q and q not in pg["name"].lower():
            continue
        out.append({"kind": "page", "id": pg["id"], "name": pg["name"],
                    "avatar": avatar_url(pg["cover"])})
    return JSONResponse({"ok": True, "results": out[:8]})


INTEREST_KINDS = ("video", "photo", "link", "feeling", "text")


def _topic_prefs(uid):
    """The viewer's This-or-Not answers: {key: 'this'|'not'}."""
    conn = db()
    rows = conn.execute(
        "SELECT key, pref FROM user_topic_prefs WHERE user_id=?",
        (uid,)).fetchall()
    conn.close()
    return {r["key"]: r["pref"] for r in rows}


def _interest_candidates(uid):
    """Prompts the trainer can still ask this viewer: trending
    hashtags first, then post kinds — anything already answered is
    never asked again."""
    prefs = _topic_prefs(uid)
    out = []
    viewer = get_user(uid) or {"id": uid}
    for topic in trending_tags(8, viewer):
        tag = (topic.get("tag") or "").strip().lower()
        if tag and ("tag:" + tag) not in prefs:
            out.append({"key": "tag:" + tag,
                        "label": "More #" + tag + " in your feed?"})
    kind_labels = {"video": "More videos in your feed?",
                   "photo": "More photos in your feed?",
                   "link": "More link posts in your feed?",
                   "feeling": "More feelings & life updates?",
                   "text": "More written posts in your feed?"}
    for k in INTEREST_KINDS:
        if ("kind:" + k) not in prefs:
            out.append({"key": "kind:" + k, "label": kind_labels[k]})
    return out


def _next_interest_prompt(uid):
    cands = _interest_candidates(uid)
    if not cands:
        return None
    import hashlib
    seed = int(hashlib.sha256(
        f"{uid}:{_today_str()}".encode()).hexdigest()[:6], 16)
    return cands[seed % len(cands)]


@app.post("/feed/interest")
async def feed_interest(request: Request):
    """Record a This-or-Not answer. JSON {key, pref}. Keys are
    'kind:<kind>' from the interest whitelist or 'tag:<hashtag>' for
    a hashtag that exists on HERE."""
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    try:
        data = await request.json()
    except Exception:
        data = {}
    key = str(data.get("key") or "").strip().lower()
    pref = str(data.get("pref") or "").strip()
    if pref not in ("this", "not"):
        return JSONResponse({"ok": False, "error": "bad pref"},
                            status_code=400)
    ok = False
    if key.startswith("kind:") and key[5:] in INTEREST_KINDS:
        ok = True
    elif key.startswith("tag:"):
        import re as _re
        tag = key[4:]
        if _re.fullmatch(r"[a-z0-9_]{1,40}", tag):
            conn = db()
            ok = bool(conn.execute(
                "SELECT 1 FROM post_hashtags WHERE tag=? LIMIT 1",
                (tag,)).fetchone())
            conn.close()
    if not ok:
        return JSONResponse({"ok": False, "error": "bad key"},
                            status_code=400)
    conn = db()
    conn.execute(
        "INSERT INTO user_topic_prefs(user_id, key, pref, updated_at) "
        "VALUES(?,?,?,?) ON CONFLICT(user_id, key) "
        "DO UPDATE SET pref=excluded.pref, updated_at=excluded.updated_at",
        (user["id"], key, pref, now_iso()))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


def _post_tags_by_id(conn, post_ids):
    if not post_ids:
        return {}
    ph = ",".join("?" * len(post_ids))
    out = {}
    for r in conn.execute(
            f"SELECT post_id, tag FROM post_hashtags "
            f"WHERE post_id IN ({ph})", list(post_ids)).fetchall():
        out.setdefault(r["post_id"], set()).add(r["tag"])
    return out


def _discovery_posts(user, exclude_ids, limit=6):
    """The random side of the home feed (Ken's order): PUBLIC posts
    from people outside the viewer's circle, rolled into the scroll
    so the viewer can find new people to follow. The viewer's
    This-or-Not answers steer it: 'not' keys never surface, 'this'
    keys float up. Friends' posts are never part of this — they
    live in the main list and are never filtered."""
    import random as _random
    uid = user["id"]
    prefs = _topic_prefs(uid)
    not_keys = {k for k, v in prefs.items() if v == "not"}
    this_keys = {k for k, v in prefs.items() if v == "this"}
    circle = friend_id_set(uid) | muted_id_set(uid) | blocked_id_set(uid)
    conn = db()
    blocked_me = {r["blocker_id"] for r in conn.execute(
        "SELECT blocker_id FROM blocks WHERE blocked_id=?",
        (uid,)).fetchall()}
    hidden = {r["post_id"] for r in conn.execute(
        "SELECT post_id FROM hidden_posts WHERE user_id=?",
        (uid,)).fetchall()}
    rows = conn.execute(
        "SELECT p.* FROM posts p WHERE p.page_id IS NULL "
        "AND p.shared_post_id IS NULL AND p.privacy='public' "
        "AND p.user_id != ? ORDER BY p.created_at DESC LIMIT 120",
        (uid,)).fetchall()
    conn.close()
    rows = [r for r in rows
            if r["user_id"] not in circle
            and r["user_id"] not in blocked_me
            and r["id"] not in exclude_ids and r["id"] not in hidden]
    if not rows:
        return []
    conn = db()
    tags = _post_tags_by_id(conn, [r["id"] for r in rows])
    conn.close()
    scored = []
    for r in rows:
        keys = {"kind:" + (r["kind"] or "text")}
        keys |= {"tag:" + tg for tg in tags.get(r["id"], set())}
        if keys & not_keys:
            continue
        scored.append((1 if keys & this_keys else 0, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    boosted = [r for s, r in scored if s]
    rest = [r for s, r in scored if not s]
    _random.shuffle(rest)
    picked = (boosted + rest)[:limit]
    cards = []
    conn = db()
    for r in picked:
        card = post_card(dict(r), user, False)
        if card:
            card["discovery"] = True
            cards.append(card)
    conn.close()
    return cards


def _home_feed_items(user, post_cards_list):
    """Interleave Sponsored cards into the home feed (Build ADS): at
    most one ad per 8 feed items, plus one trailing ad on a short or
    empty feed so a fresh account still meets the ad system. Ads are
    chosen by the ads engine (schedule/targeting/rotation caps) and
    each slotted ad records its impression for this viewer at render.
    With no eligible ads the feed is exactly the post cards, as
    before."""
    n = len(post_cards_list)
    slots = n // 8 + (1 if (n % 8 or n == 0) else 0)
    conn = db()
    chosen = ads.pick_feed_ads(
        conn, user["id"], slots,
        now=_chicago_now().replace(tzinfo=None))
    cards = []
    for ad in chosen:
        sponsor_name, sponsor_avatar = "HERE", ""
        if ad.get("kind") == "page" and ad.get("page_id"):
            pg = conn.execute("SELECT name, cover FROM pages WHERE id=?",
                              (ad["page_id"],)).fetchone()
            if pg:
                sponsor_name = pg["name"]
                sponsor_avatar = avatar_url(pg["cover"] or "")
        cards.append(ads.compose_card(ad, sponsor_name, sponsor_avatar))
        ads.record_impression(conn, ad["id"], user["id"])
    if chosen:
        conn.commit()
    conn.close()
    out = []
    ci = 0
    for i, p in enumerate(post_cards_list):
        out.append({"kind": "post", "post": p})
        if (i + 1) % 8 == 0 and ci < len(cards):
            out.append({"kind": "ad", "ad": cards[ci]})
            ci += 1
    while ci < len(cards):
        out.append({"kind": "ad", "ad": cards[ci]})
        ci += 1
    # Feed modules (redesign Phase 1 — the familiar river):
    # a People-you-may-know card a few posts in, and a Reels
    # rail of the feed's own video posts further down. Both
    # ride the same visibility-filtered post list, so nothing
    # new can leak.
    people = _feed_people_suggestions(user["id"])
    vids = []
    for pc in post_cards_list:
        m = (pc.get("media") or "").lower()
        if m.endswith((".mp4", ".mov", ".webm")):
            vids.append({"id": pc["id"], "media": pc["media"],
                         "name": pc.get("author_name") or "",
                         "avatar": pc.get("author_avatar") or "",
                         "body": (pc.get("body") or "")[:80]})
        if len(vids) >= 8:
            break
    if len(vids) >= 2:
        out.insert(min(9, len(out)),
                   {"kind": "reels", "videos": vids})
    # Discovery (Ken's feed order): PUBLIC posts from outside the
    # viewer's circle woven through the scroll — a couple early,
    # then one every several items — each badged so the viewer can
    # follow or add the author on the spot. The viewer's This-or-Not
    # answers steer what surfaces (see _discovery_posts).
    try:
        disco = _discovery_posts(
            user, {pc["id"] for pc in post_cards_list})
    except Exception:
        disco = []
    if disco:
        woven, di, since = [], 0, 0
        for item in out:
            woven.append(item)
            since += 1
            if di < len(disco) and since >= (5 if di == 0 else 7):
                woven.append({"kind": "post", "post": disco[di]})
                di += 1
                since = 0
        while di < len(disco):
            woven.append({"kind": "post", "post": disco[di]})
            di += 1
        out = woven
    # Who's HERE? rides after the discovery weave so even a short
    # feed (few friend posts, fresh account) still meets new people
    # a few items into the scroll.
    if people and len(out) >= 3:
        out.insert(3, {"kind": "pymk", "people": people})
    # This-or-Not trainer card: one unanswered prompt per load,
    # seated mid-scroll. Answering teaches the discovery mix.
    try:
        prompt = _next_interest_prompt(user["id"])
    except Exception:
        prompt = None
    if prompt and len(out) >= 3:
        out.insert(min(11, len(out)),
                   {"kind": "interest", "prompt": prompt})
    # The $100 Invite Race (Ken): while the race runs, the feed
    # shows who's winning only SOMETIMES — roughly one load in
    # three, seated a few items in — a sighting, not furniture.
    try:
        import random as _rnd
        if _race_phase() == "open" and _rnd.random() < 0.34:
            _rc = db()
            _rb = _race_board(_rc, limit=1)
            _rc.close()
            if _rb and (_rb[0]["qualified"] >= 1
                        or _rb[0]["raw"] >= 3):
                out.insert(min(4, len(out)),
                           {"kind": "race", "race": _rb[0],
                            "min": RACE_MIN_QUALIFIED})
    except Exception:
        pass
    return out


def _feed_people_suggestions(viewer_id, limit=6):
    """People-you-may-know for the home feed module: not me,
    not friends, no request pending either way, not blocked
    either way, not silenced; ranked by mutual friends."""
    conn = db()
    my_friends = friend_id_set(viewer_id)
    blocked = blocked_id_set(viewer_id)
    muted = muted_id_set(viewer_id)
    pending = {r["user_id"] for r in conn.execute(
        "SELECT user_id FROM friendships WHERE friend_id=? "
        "AND status='pending'", (viewer_id,)).fetchall()}
    pending |= {r["friend_id"] for r in conn.execute(
        "SELECT friend_id FROM friendships WHERE user_id=? "
        "AND status='pending'", (viewer_id,)).fetchall()}
    rows = conn.execute(
        "SELECT id, name, avatar FROM users WHERE id!=?",
        (viewer_id,)).fetchall()
    out = []
    for u in rows:
        uid = u["id"]
        if (uid in my_friends or uid in blocked or uid in muted
                or uid in pending):
            continue
        theirs = friend_id_set(uid)
        mutual = len(my_friends & theirs)
        out.append({"id": uid, "name": u["name"],
                    "avatar": avatar_url(u["avatar"]),
                    "mutual": mutual})
    conn.close()
    out.sort(key=lambda x: (-x["mutual"], x["name"].lower()))
    return out[:limit]


@app.get("/reels", response_class=HTMLResponse)
@app.get("/peeps", response_class=HTMLResponse)
async def reels_page(request: Request):
    """Reels (redesign Phase 1): a grid of video posts from
    the same universe as the home feed. Phase 3 turns this
    into the full-screen swipe feed."""
    user, err = require_login(request)
    if err:
        return err
    record_activity(user["id"])
    owner_on = is_owner(request)
    posts = _home_posts(user, owner_on)
    vids = []
    for pc in posts:
        m = (pc.get("media") or "").lower()
        if m.endswith((".mp4", ".mov", ".webm")):
            vids.append(pc)
    return render(request, "reels.html", {"videos": vids})


@app.get("/home", response_class=HTMLResponse)
async def home(request: Request):
    user, err = require_login(request)
    if err:
        return err
    record_activity(user["id"])
    owner_on = is_owner(request)
    posts = _home_posts(user, owner_on)
    feed_items = _home_feed_items(user, posts)
    fids = friend_id_set(user["id"])
    contacts = []
    if fids:
        conn = db()
        for fid in fids:
            f = conn.execute(
                "SELECT id,name,avatar,last_seen_at,status_mode,inbox_status "
                "FROM users WHERE id=?", (fid,)).fetchone()
            if f:
                contacts.append({"id": f["id"], "name": f["name"],
                                 "avatar": avatar_url(f["avatar"]),
                                 "presence": presence_of(f)})
        conn.close()
        contacts.sort(key=lambda c: (
            0 if (c["presence"] and c["presence"]["status"] == "online") else 1,
            c["name"].lower()))
    cfriends, cpages = composer_choices(user)
    _lc = db()
    lobby_card = _lobby_card(_lc)
    seats_left = founding_seat_counts(_lc)[1]
    paid_total = _paid_total_cents(_lc)
    upcoming = _upcoming_shows(_lc, user, 5)
    _lc.close()
    return render(request, "home.html",
                  {"posts": posts, "feed_items": feed_items,
                   "contacts": contacts,
                   "stories_tray": stories_tray(user["id"]),
                   "live_now": live_now_for(user["id"], owner_on),
                   "lobby": lobby_card,
                   "seats_left": seats_left,
                   "paid_total_cents": paid_total,
                   "upcoming_shows": upcoming,
                   "discover_people": discover_people(user["id"], 3),
                   "trends": trending_topics(5, user["id"]),
                   "daily_prompt": daily_prompt(),
                   "daily_streak": current_streak(user["id"]),
                   "daily_today": _today_str(),
                   "composer_friends": cfriends, "composer_pages": cpages})


def create_post_from_form(user, form, page_id=None):
    """Validate + insert a post. Returns (post_id, error_flash)."""
    kind = (form.get("kind") or "text").strip()
    # Retired post kinds (Ken's order): 'clothes' and 'audio' (MP3
    # uploads) are no longer offered anywhere in the composer, and new
    # posts of those kinds are refused outright. EXISTING clothes /
    # audio posts keep rendering exactly as before — post_card and the
    # KINDS list are untouched; only creation is closed.
    if kind in ("clothes", "audio"):
        return None, "That post type is no longer available."
    # --- games pack: kind='poll' — the body is the question and the
    # option1..option4 form fields carry the choices. A poll post is a
    # plain text post (no media) with poll rows attached after the
    # insert. Fewer than 2 non-empty options refuses the whole post:
    # nothing is created. Options are trimmed, capped at 60 chars. ---
    poll_options = None
    if kind == "poll":
        poll_options = []
        for _i in (1, 2, 3, 4):
            _t = (form.get(f"option{_i}") or "").strip()[:60]
            if _t:
                poll_options.append(_t)
        if len(poll_options) < 2:
            return None, "A poll needs at least 2 options."
        kind = "text"
    if kind not in KINDS:
        return None, "Invalid post kind."
    privacy = (form.get("privacy") or "public").strip()
    if privacy not in PRIVACIES:
        privacy = "public"
    if page_id:
        privacy = "public"  # page posts are visible to page viewers
    # --- Build WALL: a wall_user_id form field targets another user's
    # profile wall. ONE pipeline: the post stays an ordinary post (any
    # kind, music, hide-from, privacy, @tags, moderation) with a wall
    # target stamped on it. The gate runs BEFORE any upload is saved,
    # so a refusal creates nothing and leaves no files behind. Page
    # posts never target a wall, and the author writing on their OWN
    # profile is just a normal post (no gate, wall_user_id NULL).
    # Gate order: target exists → blocked either way → wall closed →
    # writer must be the owner's friend. ---
    wall_user_id = None
    wall_pending = 0
    if not page_id:
        _wraw = (form.get("wall_user_id") or "").strip()
        if _wraw:
            try:
                _wt = int(_wraw)
            except (TypeError, ValueError):
                _wt = 0
            if _wt and _wt != user["id"]:
                _wo = get_user(_wt)
                if not _wo:
                    return None, "That wall isn't available."
                if blocked_either_way(user["id"], _wt):
                    return None, "You can't write on that wall."
                _wopen = _wo.get("wall_open")
                if _wopen is None:
                    _wopen = 1  # pre-WALL rows: open by default
                if not _wopen:
                    return None, (f"{_wo['name']}'s wall is closed — "
                                  "friends can't write on it right now.")
                if _wt not in friend_id_set(user["id"]):
                    return None, (f"Only {_wo['name']}'s friends can "
                                  "write on their wall.")
                wall_user_id = _wt
                wall_pending = 1 if _wo.get("wall_approve_first") else 0
    # --- Build LOCKED CONTENT: an optional lock_price form field
    # turns this post into paid content. Parsed and gated HERE,
    # before any file is saved, so a refusal creates nothing. Only
    # creators (users.creator_mode ON) may sell; the price is an
    # integer 5..5000 tickets — out-of-range is a plain refusal,
    # never a silent clamp. Page posts can't be locked (Pages earn
    # through Page Gifts); polls can't be locked (the question IS
    # the content). Turning Creator Mode off later never touches
    # existing locks — only this create path is gated. ---
    lock_price = 0
    lock_adult = 0
    _lraw = (form.get("lock_price") or "").strip()
    if _lraw not in ("", "0"):
        if page_id:
            return None, "Locked posts aren't available on Pages."
        if poll_options is not None:
            return None, "Polls can't be locked."
        if not user.get("creator_mode"):
            return None, ("Turn on Creator Mode to sell locked "
                          "content.")
        try:
            lock_price = int(_lraw)
        except (TypeError, ValueError):
            return None, "Lock price must be a whole number of tickets."
        if lock_price < 5 or lock_price > 5000:
            return None, ("Lock price must be between 5 and 5,000 "
                          "tickets.")
        lock_adult = 1 if (form.get("lock_adult") or "").strip() in (
            "1", "on", "true") else 0
    body = (form.get("body") or "").strip()
    feeling = (form.get("feeling") or "").strip()
    link_url = (form.get("link_url") or "").strip()
    video_url = (form.get("video_url") or "").strip()
    # Daily Challenge answer: the composer on /daily posts challenge=1.
    # The #HereDaily tag rides in the body itself (added BEFORE the
    # insert so the hashtag hook below stores it like any other tag;
    # parse_hashtags lowercases it to 'heredaily').
    challenge = (form.get("challenge") or "").strip() in ("1", "true", "on")
    if challenge and "heredaily" not in parse_hashtags(body):
        body = (body + " #HereDaily").strip()

    # link_url / video_url are no longer offered in the composer (any link
    # pasted in the body just works, and YouTube/Vimeo links auto-embed),
    # but the fields are still accepted so old clients don't break and old
    # posts keep rendering exactly as before.
    if link_url and not (link_url.startswith("http://") or
                         link_url.startswith("https://")):
        return None, "Link must start with http:// or https://."
    if video_url and ("youtube" not in video_url and "youtu.be" not in video_url
                      and "vimeo" not in video_url):
        return None, "Video URL must be a YouTube or Vimeo link."

    # --- music: a YouTube song may ride along on any post kind. Same
    # rule as stories: a song that can't be embedded fails the save. ---
    try:
        music = _resolve_music(form)
    except ValueError as e:
        return None, str(e)
    try:
        music_start = max(0, min(3600, int(form.get("music_start") or 0)))
    except (TypeError, ValueError):
        music_start = 0

    media_file = form.get("media")
    # Audio files are no longer accepted on ANY new post (the MP3 post
    # option was retired with kind='audio' above) — the upload in a
    # forged form now fails validation like any other bad extension.
    allowed = IMG_EXTS | VID_EXTS
    if kind == "photo":
        allowed = IMG_EXTS
    elif kind == "video":
        allowed = VID_EXTS
    # Post videos get the big cap; photos/audio keep the default cap.
    _mname = getattr(media_file, "filename", "") or ""
    _mext = _mname.rsplit(".", 1)[-1].lower() if "." in _mname else ""
    cap = (PLUS_VIDEO_MB if _is_plus(user)
           else POST_VIDEO_MAX_MB) if _mext in VID_EXTS else 0
    if poll_options is not None:
        media = ""  # polls are text-only by design; any file is ignored
    elif lock_price:
        # Locked media goes to the locked store (LOCKED_DIR / the
        # locked/ R2 prefix), NEVER the public uploads — and the
        # browser-direct media_key path can't be used: those objects
        # already sit at a public posts/ key, so a "locked" post
        # pointing at one would leak. Refuse plainly instead.
        if (form.get("media_key") or "").strip():
            return None, ("Locked posts need the file attached right "
                          "here — the quick upload can't be locked.")
        try:
            media = save_locked_upload(media_file, "posts", allowed,
                                       max_mb=cap)
        except ValueError as e:
            return None, str(e)
    elif (form.get("media_key") or "").strip() and storage.r2_enabled():
        # Browser-direct upload (Build U): the file already sits in
        # R2; the form carries only the server-minted key.
        media, _kerr = _accept_media_key(
            user, (form.get("media_key") or "").strip(), "posts", allowed)
        if _kerr:
            return None, _kerr
    else:
        try:
            media = save_upload(media_file, "posts", allowed, max_mb=cap)
        except ValueError as e:
            return None, str(e)

    conn = db()
    cur = conn.execute(
        "INSERT INTO posts(user_id, page_id, kind, body, feeling, link_url, "
        "video_url, media, privacy, music_video_id, music_title, "
        "music_artist, music_thumb, music_start, created_at, "
        "wall_user_id, wall_pending, lock_price, lock_adult) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user["id"], page_id, kind, body, feeling, link_url, video_url,
         media, privacy,
         music["video_id"] if music else "",
         music["title"] if music else "",
         music["artist"] if music else "",
         music["thumb"] if music else "",
         music_start if music else 0,
         now_iso(), wall_user_id, wall_pending, lock_price, lock_adult))
    pid = cur.lastrowid
    # Hashtags: parsed from the body of EVERY new post (any kind) into
    # post_hashtags. My Views consumes them now; the wall may later.
    for _t in parse_hashtags(body):
        conn.execute(
            "INSERT OR IGNORE INTO post_hashtags(post_id, tag) "
            "VALUES(?,?)", (pid, _t))
    if challenge:
        conn.execute("UPDATE posts SET challenge_date=? WHERE id=?",
                     (_today_str(), pid))
    if poll_options is not None:
        conn.execute("INSERT INTO polls(post_id) VALUES(?)", (pid,))
        for _idx, _t in enumerate(poll_options):
            conn.execute(
                "INSERT INTO poll_options(post_id, idx, text) "
                "VALUES(?,?,?)", (pid, _idx, _t))
    conn.commit()
    conn.close()
    # --- hide-from list + tags: validated server-side. Only friends can be
    # hidden from or tagged; only pages the author owns or belongs to can be
    # tagged. Invalid ids are silently dropped. ---
    fids = friend_id_set(user["id"])
    hide_ids = _form_ids(form, "hide_from") & fids
    tag_uids = _form_ids(form, "tag_users") & fids
    valid_pids = {pg["id"] for pg in taggable_pages(user["id"])}
    tag_pids = _form_ids(form, "tag_pages") & valid_pids
    # Facebook-style @tags from the composer picker (validated: real
    # friend / owned-or-member page + the @Name must be in the body).
    at_uids, at_pids = _tags_from_json(form, body, user)
    tag_uids |= at_uids
    tag_pids |= at_pids
    if hide_ids or tag_uids or tag_pids:
        conn = db()
        for hid in hide_ids:
            conn.execute(
                "INSERT OR IGNORE INTO post_hidden_from(post_id, user_id, "
                "created_at) VALUES(?,?,?)", (pid, hid, now_iso()))
        for tuid in sorted(tag_uids):
            conn.execute(
                "INSERT INTO post_tags(post_id, user_id, created_at) "
                "VALUES(?,?,?)", (pid, tuid, now_iso()))
        for tpid in sorted(tag_pids):
            conn.execute(
                "INSERT INTO post_tags(post_id, page_id, created_at) "
                "VALUES(?,?,?)", (pid, tpid, now_iso()))
        conn.commit()
        conn.close()
    for tuid in sorted(tag_uids):
        notify(tuid, "tag", f"{user['name']} tagged you in a post.",
               f"/post/{pid}", actor_id=user["id"])
    flag_item("post", pid, user["id"], user["name"],
              " ".join(x for x in (body, feeling, link_url, video_url) if x))
    record_activity(user["id"])
    # Build WALL: the wall owner hears about it either way — a live
    # wall post links straight to the post; a pending one links to
    # their profile, where the Approve / Delete buttons live. (Owner
    # notifications only — approving sends the author nothing.)
    if wall_user_id:
        if wall_pending:
            notify(wall_user_id, "wall_pending",
                   f"{user['name']} wants to write on your wall — "
                   "approve it from your profile.",
                   f"/user/{wall_user_id}", actor_id=user["id"])
        else:
            notify(wall_user_id, "wall",
                   f"{user['name']} wrote on your wall.",
                   f"/post/{pid}", actor_id=user["id"])
    return pid, None


@app.post("/posts")
async def create_post(request: Request):
    user, err = require_login(request)
    if err:
        return err
    form = await request.form()
    pid, ferr = create_post_from_form(user, form)
    # Optional same-origin 'next' (the My Views composer sends
    # next=/views so posting a View lands back on the feed). Only a
    # local path is honored — anything else falls back to /home.
    nxt = (form.get("next") or "").strip()
    if not (nxt.startswith("/") and not nxt.startswith("//")):
        nxt = "/home"
    if ferr:
        return redir(nxt, ferr)
    return redir(nxt)


def _thread_comments(conn, p, viewer, owner_on=False):
    """The post page's comment thread, assembled ONCE here and shared
    by GET /post/{id} (HTML) and GET /views/{id}/comments (JSON): same
    query, same block filtering, same per-comment dicts (can_delete /
    can_block / reply_prefix / one-level reply nesting). p is the post
    row as a dict; viewer is the viewing user's dict."""
    pid = p["id"]
    crows = conn.execute(
        "SELECT c.* FROM comments c WHERE c.post_id=? ORDER BY c.created_at ASC",
        (pid,)).fetchall()
    if not owner_on:
        # Comments from anyone in a block relationship with the viewer
        # (either direction) don't render for that viewer; everyone
        # else's view of the same thread is untouched.
        _blocked_view = blocked_id_set(viewer["id"])
        if _blocked_view:
            crows = [c for c in crows
                     if c["user_id"] not in _blocked_view]
    _authors = {}

    def _author(uid):
        if uid not in _authors:
            _authors[uid] = conn.execute(
                "SELECT id,name,avatar,founding_no,plus_until "
                "FROM users "
                "WHERE id=?", (uid,)).fetchone()
        return _authors[uid]

    _by_id = {c["id"]: c for c in crows}

    def _comment_dict(c):
        a = _author(c["user_id"])
        # Facebook-style flattening: when a reply was aimed at another
        # reply (not at the top-level comment), it renders under the
        # top-level comment with a linked "@Name" prefix naming the
        # person it was aimed at.
        prefix = None
        if c["parent_id"] and c["reply_to_id"] \
                and c["reply_to_id"] != c["parent_id"] \
                and c["reply_to_id"] in _by_id:
            tgt = _by_id[c["reply_to_id"]]
            ta = _author(tgt["user_id"])
            prefix = {"id": tgt["user_id"],
                      "name": ta["name"] if ta else "Deleted user"}
        return {
            "id": c["id"], "user_id": c["user_id"],
            "author_name": a["name"] if a else "Deleted user",
            "author_founding_no": (a["founding_no"] if a else None),
            "author_is_plus": _is_plus(a) if a else False,
            "author_avatar": avatar_url(a["avatar"]) if a and a["avatar"] else "",
            "body": c["body"], "body_html": linkify_body(c["body"]),
            "created_at": fancy_time(c["created_at"]),
            # The ⋯ menu shows Delete to the comment's author and to
            # the post's author (and in the owner context); Block only
            # to the post's author, and never against their own comment.
            "can_delete": bool(viewer["id"] == c["user_id"] or
                               viewer["id"] == p["user_id"] or owner_on),
            "can_block": bool(viewer["id"] == p["user_id"] and
                              c["user_id"] != viewer["id"]),
            "reply_prefix": prefix,
            "replies": [],
        }

    comments = []
    _tops = {}
    for c in crows:
        if not c["parent_id"]:
            d = _comment_dict(c)
            _tops[c["id"]] = d
            comments.append(d)
    for c in crows:
        if c["parent_id"]:
            d = _comment_dict(c)
            if c["parent_id"] in _tops:
                _tops[c["parent_id"]]["replies"].append(d)
            else:
                # Orphaned reply (its top-level comment is gone): show
                # it as a top-level comment rather than dropping it.
                comments.append(d)
    return comments


@app.get("/post/{pid}", response_class=HTMLResponse)
async def view_post(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    owner_on = is_owner(request)
    conn = db()
    p = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if not p:
        conn.close()
        return redir("/home", "Post not found.")
    if p["shared_post_id"]:
        # Share rows redirect to the original post.
        orig_id = p["shared_post_id"]
        conn.close()
        return redir(f"/post/{orig_id}")
    if not can_view_post(user, dict(p), is_owner(request)):
        conn.close()
        return render(request, "post_gone.html", {})
    comments = _thread_comments(conn, dict(p), user, owner_on)
    author = conn.execute("SELECT * FROM users WHERE id=?",
                          (p["user_id"],)).fetchone()
    conn.close()
    pc = post_card(dict(p), user, owner_on)
    pc["comments"] = comments
    # The post page is the author's stage too (Ken's clear
    # glass order — 'even with the comments, even with the
    # post'): it wears the author's background, and every
    # glass surface on it (the post card, the comment
    # bubbles, the chrome) runs the author's showcase alpha
    # via the --profile-glass vars post.html emits.
    author = dict(author) if author else {}
    ctx = {"post": pc,
           "profile_glass": profile_showcase_alpha_for(author),
           "profile_glass_soft": profile_showcase_soft_for(author)}
    _abg = avatar_url(author.get("background", ""))
    if _abg:
        # Only an author WITH a background restages the page;
        # otherwise the viewer's own backdrop (render default)
        # stays exactly as before.
        ctx["page_bg"] = _abg
    return render(request, "post.html", ctx)


@app.post("/post/{pid}/comment")
async def add_comment(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    p = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not p:
        return redir("/home", "Post not found.")
    p = dict(p)
    if p.get("shared_post_id"):
        # Comments land on the original post, not the share pointer.
        return redir(f"/post/{p['shared_post_id']}")
    if not can_view_post(user, p, is_owner(request)):
        return render(request, "post_gone.html", {})
    if user["id"] in comment_blocked_ids(pid):
        # The post's author blocked this user from THIS post only.
        return redir(f"/post/{pid}",
                     "🚫 The author of this post has blocked you from "
                     "commenting here.")
    form = await request.form()
    body = (form.get("body") or "").strip()
    if not body:
        return redir(f"/post/{pid}", "Comment can't be empty.")
    # Optional reply: the form's parent_id names the comment being
    # replied to. Replies flatten to ONE level — the stored parent_id
    # is always the TOP-LEVEL comment; reply_to_id keeps the exact tap
    # target so the post page can prefix a linked "@Name" when someone
    # replies to a reply. A stale/forged parent_id (missing comment, or
    # a comment on another post) just falls back to a top-level comment.
    parent_id = None
    reply_to_id = None
    reply_target = None
    raw_parent = form.get("parent_id")
    if raw_parent:
        try:
            want = int(raw_parent)
        except (TypeError, ValueError):
            want = None
        if want:
            conn = db()
            trow = conn.execute(
                "SELECT * FROM comments WHERE id=? AND post_id=?",
                (want, pid)).fetchone()
            conn.close()
            if trow:
                reply_target = dict(trow)
                reply_to_id = trow["id"]
                parent_id = trow["parent_id"] or trow["id"]
    conn = db()
    cur = conn.execute(
        "INSERT INTO comments(post_id, user_id, body, parent_id, reply_to_id, "
        "created_at) VALUES(?,?,?,?,?,?)",
        (pid, user["id"], body, parent_id, reply_to_id, now_iso()))
    cid = cur.lastrowid
    conn.commit()
    conn.close()
    flag_item("comment", cid, user["id"], user["name"], body)
    if reply_target is not None:
        # A reply notifies the author of the comment replied to.
        if reply_target["user_id"] != user["id"]:
            notify(reply_target["user_id"], "reply",
                   f"{user['name']} replied to your comment.",
                   f"/post/{pid}", actor_id=user["id"])
    elif p["user_id"] != user["id"]:
        notify(p["user_id"], "comment",
               f"{user['name']} commented on your post.", f"/post/{pid}",
               actor_id=user["id"])
    record_activity(user["id"])
    return redir(f"/post/{pid}")


@app.post("/comment/{cid}/delete")
async def delete_comment(request: Request, cid: int):
    """Delete a comment. Allowed: the comment's author, the post's
    author, or the owner context — anyone else gets a 403. Deleting a
    top-level comment takes all its replies with it; deleting a reply
    removes only that reply (replies have no children by construction)."""
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    conn = db()
    c = conn.execute("SELECT * FROM comments WHERE id=?", (cid,)).fetchone()
    if not c:
        conn.close()
        return JSONResponse({"ok": False, "error": "not found"},
                            status_code=404)
    p = conn.execute("SELECT user_id FROM posts WHERE id=?",
                     (c["post_id"],)).fetchone()
    allowed = bool(c["user_id"] == user["id"] or is_owner(request) or
                   (p and p["user_id"] == user["id"]))
    if not allowed:
        conn.close()
        return JSONResponse({"ok": False, "error": "forbidden"},
                            status_code=403)
    pid = c["post_id"]
    conn.execute("DELETE FROM comments WHERE id=? OR parent_id=?", (cid, cid))
    conn.commit()
    conn.close()
    return redir(f"/post/{pid}", "Comment deleted.")


@app.post("/post/{pid}/comment-block")
async def block_commenter(request: Request, pid: int):
    """Post author only: stop one user from adding NEW comments or
    replies on this post. Their existing comments are NOT deleted, and
    the block applies to this post only."""
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    conn = db()
    p = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not p:
        return JSONResponse({"ok": False, "error": "not found"},
                            status_code=404)
    if p["user_id"] != user["id"]:
        return JSONResponse({"ok": False, "error": "forbidden"},
                            status_code=403)
    form = await request.form()
    try:
        target = int(form.get("user_id") or 0)
    except (TypeError, ValueError):
        target = 0
    if target and target != user["id"]:
        conn = db()
        conn.execute(
            "INSERT OR IGNORE INTO post_comment_blocks(post_id, user_id, "
            "created_at) VALUES(?,?,?)", (pid, target, now_iso()))
        conn.commit()
        conn.close()
        t = get_user(target)
        return redir(f"/post/{pid}",
                     f"🚫 {t['name'] if t else 'That person'} is blocked "
                     "from commenting on this post.")
    return redir(f"/post/{pid}")


@app.post("/post/{pid}/comment-unblock")
async def unblock_commenter(request: Request, pid: int):
    """Post author only: lift a per-post comment block."""
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    conn = db()
    p = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not p:
        return JSONResponse({"ok": False, "error": "not found"},
                            status_code=404)
    if p["user_id"] != user["id"]:
        return JSONResponse({"ok": False, "error": "forbidden"},
                            status_code=403)
    form = await request.form()
    try:
        target = int(form.get("user_id") or 0)
    except (TypeError, ValueError):
        target = 0
    if target:
        conn = db()
        conn.execute(
            "DELETE FROM post_comment_blocks WHERE post_id=? AND user_id=?",
            (pid, target))
        conn.commit()
        conn.close()
        t = get_user(target)
        return redir(f"/post/{pid}",
                     f"{t['name'] if t else 'That person'} can comment on "
                     "this post again.")
    return redir(f"/post/{pid}")


@app.post("/post/{pid}/like")
async def toggle_like(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    conn = db()
    p = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if not p:
        conn.close()
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    p = dict(p)
    if p.get("shared_post_id"):
        # Likes land on the original post, not the share pointer.
        pid = p["shared_post_id"]
        p = dict(conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone() or {})
        if not p:
            conn.close()
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    if not can_view_post(user, p, is_owner(request)):
        conn.close()
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    existing = conn.execute("SELECT 1 FROM likes WHERE post_id=? AND user_id=?",
                            (pid, user["id"])).fetchone()
    if existing:
        conn.execute("DELETE FROM likes WHERE post_id=? AND user_id=?",
                     (pid, user["id"]))
        liked = False
    else:
        conn.execute("INSERT INTO likes(post_id, user_id, kind, created_at) VALUES(?,?,?,?)",
                     (pid, user["id"], "like", now_iso()))
        liked = True
    likes = conn.execute("SELECT COUNT(*) c FROM likes WHERE post_id=?",
                         (pid,)).fetchone()["c"]
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "likes": likes, "liked": liked})


@app.post("/post/{pid}/react")
async def react_post(request: Request, pid: int):
    """Set/change/remove a reaction kind. Same kind twice = toggle off."""
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    form = await request.form()
    kind = (form.get("kind") or "like").strip()
    if kind not in REACTION_EMOJI:
        kind = "like"
    conn = db()
    p = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if not p:
        conn.close()
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    p = dict(p)
    if p.get("shared_post_id"):
        # Reactions land on the original post, not the share pointer.
        pid = p["shared_post_id"]
        p = dict(conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone() or {})
        if not p:
            conn.close()
            return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    if not can_view_post(user, p, is_owner(request)):
        conn.close()
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    row = conn.execute("SELECT kind FROM likes WHERE post_id=? AND user_id=?",
                       (pid, user["id"])).fetchone()
    if row and (row["kind"] or "like") == kind:
        conn.execute("DELETE FROM likes WHERE post_id=? AND user_id=?",
                     (pid, user["id"]))
        my_kind = None
    elif row:
        conn.execute("UPDATE likes SET kind=?, created_at=? WHERE post_id=? AND user_id=?",
                     (kind, now_iso(), pid, user["id"]))
        my_kind = kind
    else:
        conn.execute("INSERT INTO likes(post_id, user_id, kind, created_at) VALUES(?,?,?,?)",
                     (pid, user["id"], kind, now_iso()))
        my_kind = kind
    likes = conn.execute("SELECT COUNT(*) c FROM likes WHERE post_id=?",
                         (pid,)).fetchone()["c"]
    top = [{"emoji": REACTION_EMOJI.get(r["kind"], "👍"), "count": r["c"]}
           for r in conn.execute(
               "SELECT kind, COUNT(*) c FROM likes WHERE post_id=? "
               "GROUP BY kind ORDER BY c DESC LIMIT 3", (pid,)).fetchall()
           if r["kind"] in REACTION_EMOJI]
    conn.commit()
    conn.close()
    if my_kind is not None:
        record_activity(user["id"])
    return JSONResponse({"ok": True, "likes": likes, "reacted": my_kind is not None,
                         "kind": my_kind, "top": top})


@app.post("/post/{pid}/poll/vote")
async def poll_vote(request: Request, pid: int):
    """Vote in a post's poll. JSON {option_id}. Login + can_view_post
    gate exactly like reactions (unviewable = 404); the post must
    carry a poll and the option must belong to it. One row per user —
    voting again CHANGES the pick (the PK upsert never doubles).
    Votes send no notification, but they do count as streak activity
    (record_activity), like a reaction that lands."""
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        option_id = int((data or {}).get("option_id") or 0)
    except (TypeError, ValueError):
        option_id = 0
    conn = db()
    p = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if p and p["shared_post_id"]:
        # Votes land on the original post, not the share pointer.
        p = conn.execute("SELECT * FROM posts WHERE id=?",
                         (p["shared_post_id"],)).fetchone()
    if not p:
        conn.close()
        return JSONResponse({"ok": False, "error": "not found"},
                            status_code=404)
    p = dict(p)
    if not can_view_post(user, p, is_owner(request)):
        conn.close()
        return JSONResponse({"ok": False, "error": "not found"},
                            status_code=404)
    pid = p["id"]
    if not conn.execute("SELECT 1 FROM polls WHERE post_id=?",
                        (pid,)).fetchone():
        conn.close()
        return JSONResponse({"ok": False, "error": "No poll on this post."},
                            status_code=404)
    opt = conn.execute(
        "SELECT id FROM poll_options WHERE id=? AND post_id=?",
        (option_id, pid)).fetchone()
    if not opt:
        conn.close()
        return JSONResponse({"ok": False, "error": "Unknown option."},
                            status_code=400)
    conn.execute(
        "INSERT INTO poll_votes(post_id, user_id, option_id) "
        "VALUES(?,?,?) ON CONFLICT(post_id, user_id) DO UPDATE SET "
        "option_id=excluded.option_id",
        (pid, user["id"], option_id))
    conn.commit()
    poll = _poll_for_post(conn, pid, user)
    conn.close()
    record_activity(user["id"])
    return JSONResponse({"ok": True, "poll": poll,
                         "total": poll["total"],
                         "my_option_id": poll["my_option_id"],
                         "options": poll["options"]})


# ---------------- My Views (vertical video feed) ----------------
# HERE's Reels: full-screen vertical feed of UPLOADED video posts,
# ranked per viewer by a small learning algorithm (documented in
# ROUTE_CONTRACT.md — "My Views ranking"). YouTube/Vimeo link posts
# (video_url set, no uploaded file) never appear here: they stay on
# the wall. The only privacy gate is can_view_post, exactly like the
# wall; muted and blocked authors are excluded on top of it.
VIEWS_W_FRIEND = 30.0        # author is the viewer's friend
VIEWS_W_FOLLOW = 25.0        # viewer follows the author
VIEWS_AUTHOR_AFF_CAP = 40.0  # learned author affinity, clamped ±
VIEWS_TAG_AFF_EACH_CAP = 30.0   # per-hashtag affinity clamp ±
VIEWS_TAG_AFF_TOTAL_CAP = 45.0  # total tag contribution clamp ±
VIEWS_POP_PER_REACTION = 2.0
VIEWS_POP_PER_COMMENT = 3.0
VIEWS_POP_CAP = 20.0
VIEWS_RECENCY_MAX = 20.0     # a brand-new video gets +20, decaying daily
VIEWS_SEEN_PENALTY = 35.0    # already-seen videos sink (not removed)
# Learning deltas (user_affinity points)
VIEWS_LEARN_SEEN_TAG, VIEWS_LEARN_SEEN_AUTHOR = 0.5, 0.5
VIEWS_LEARN_COMPLETED_TAG, VIEWS_LEARN_COMPLETED_AUTHOR = 1.0, 1.0
VIEWS_LEARN_REACT = 2.0      # tag AND author
VIEWS_LEARN_COMMENT = 3.0    # tag AND author
VIEWS_LEARN_INTEREST = 10.0  # +10 interested / -10 not interested


def _media_ext(media):
    m = media or ""
    return m.rsplit(".", 1)[-1].lower() if "." in m else ""


def _post_tag_list(conn, pid):
    """A post's hashtags in the order they first appeared in the body
    (post_hashtags rowid order = insertion order)."""
    return [r["tag"] for r in conn.execute(
        "SELECT tag FROM post_hashtags WHERE post_id=? ORDER BY rowid",
        (pid,)).fetchall()]


def _affinity_bump(conn, uid, kind, key, delta):
    conn.execute(
        "INSERT INTO user_affinity(user_id, kind, key, score, updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(user_id, kind, key) DO UPDATE SET "
        "score = score + excluded.score, "
        "updated_at = excluded.updated_at",
        (uid, kind, key, float(delta), now_iso()))


def _affinity_map(conn, uid, kind):
    return {r["key"]: r["score"] for r in conn.execute(
        "SELECT key, score FROM user_affinity WHERE user_id=? AND kind=?",
        (uid, kind)).fetchall()}


def _teach_from_post(conn, uid, post, tag_delta, author_delta):
    """Apply a learning signal from one post to a viewer: each of the
    post's hashtags gets tag_delta tag-affinity, the post's author
    gets author_delta author-affinity. Your own posts teach nothing
    about their author (you are not a signal about yourself)."""
    if tag_delta:
        for t in _post_tag_list(conn, post["id"]):
            _affinity_bump(conn, uid, "tag", t, tag_delta)
    if author_delta and post["user_id"] != uid:
        _affinity_bump(conn, uid, "author", str(post["user_id"]),
                       author_delta)


def _views_candidates(conn, viewer, tag=None):
    """Feed-eligible post rows (dicts) for a viewer, BEFORE ranking:
    the post carries an uploaded VIDEO file (media ext in VID_EXTS —
    kind is not the test; a text post with a video file counts, a
    kind='video' link post does not), it is not a share row, the
    viewer has not 'hidden' it (not-interested), the author is not
    blocked either way with the viewer and not muted by the viewer,
    and can_view_post passes. Optional single-hashtag filter."""
    rows = conn.execute(
        "SELECT * FROM posts WHERE media != '' AND shared_post_id IS NULL "
        "ORDER BY created_at DESC").fetchall()
    blocked = blocked_id_set(viewer["id"])
    muted = muted_id_set(viewer["id"])
    hidden = {r["post_id"] for r in conn.execute(
        "SELECT post_id FROM view_events WHERE user_id=? AND kind='hidden'",
        (viewer["id"],)).fetchall()}
    out = []
    for r in rows:
        p = dict(r)
        if _media_ext(p["media"]) not in VID_EXTS:
            continue
        if p["id"] in hidden:
            continue
        if p["user_id"] in blocked or p["user_id"] in muted:
            continue
        if tag is not None and tag not in set(
                _post_tag_list(conn, p["id"])):
            continue
        if not can_view_post(viewer, p):
            continue
        out.append(p)
    return out


def _views_feed(viewer, tag=None, offset=0, limit=10):
    """Ranked My Views items for a viewer. Score (per candidate):
      +30 friend author, +25 followed author,
      + author affinity clamped ±40,
      + the post's hashtag affinities, each clamped ±30, the total
        tag contribution clamped ±45,
      + popularity min(20, 2*reactions + 3*comments),
      + recency max(0, 20 - age_in_days),
      -35 when the viewer already has a 'seen' event for the post.
    Order: score DESC, then created_at DESC. Page via offset/limit."""
    conn = db()
    posts = _views_candidates(conn, viewer, tag)
    friends = friend_id_set(viewer["id"])
    following = following_ids(viewer["id"])
    tag_aff = _affinity_map(conn, viewer["id"], "tag")
    author_aff = _affinity_map(conn, viewer["id"], "author")
    seen = {r["post_id"] for r in conn.execute(
        "SELECT post_id FROM view_events WHERE user_id=? AND kind='seen'",
        (viewer["id"],)).fetchall()}
    now = datetime.now(timezone.utc)
    items = []
    for p in posts:
        pid = p["id"]
        tags = _post_tag_list(conn, pid)
        rc = conn.execute(
            "SELECT COUNT(*) c FROM likes WHERE post_id=?",
            (pid,)).fetchone()["c"]
        cc = conn.execute(
            "SELECT COUNT(*) c FROM comments WHERE post_id=?",
            (pid,)).fetchone()["c"]
        mine = conn.execute(
            "SELECT kind FROM likes WHERE post_id=? AND user_id=?",
            (pid, viewer["id"])).fetchone()
        score = 0.0
        if p["user_id"] in friends:
            score += VIEWS_W_FRIEND
        if p["user_id"] in following:
            score += VIEWS_W_FOLLOW
        score += max(-VIEWS_AUTHOR_AFF_CAP, min(
            VIEWS_AUTHOR_AFF_CAP,
            author_aff.get(str(p["user_id"]), 0.0)))
        tag_sum = sum(max(-VIEWS_TAG_AFF_EACH_CAP, min(
            VIEWS_TAG_AFF_EACH_CAP, tag_aff.get(t, 0.0))) for t in tags)
        score += max(-VIEWS_TAG_AFF_TOTAL_CAP,
                     min(VIEWS_TAG_AFF_TOTAL_CAP, tag_sum))
        score += min(VIEWS_POP_CAP,
                     VIEWS_POP_PER_REACTION * rc
                     + VIEWS_POP_PER_COMMENT * cc)
        created = _parse_ts(p["created_at"])
        age_days = ((now - created).total_seconds() / 86400.0
                    if created else 365.0)
        score += max(0.0, VIEWS_RECENCY_MAX - age_days)
        if pid in seen:
            score -= VIEWS_SEEN_PENALTY
        a = get_user(p["user_id"])
        # Build LOCKED CONTENT: a locked View carries its lock into
        # the player — no video URL and no caption for anyone who
        # hasn't opened it; the author and buyers play it through
        # the /locked gate instead of the public uploads path.
        _vlock = None
        _vurl = "/uploads/" + p["media"]
        _vbody = p["body"] or ""
        if p.get("lock_price"):
            _vopen = bool(p["user_id"] == viewer["id"]
                          or _has_unlock(conn, viewer["id"], "post",
                                         pid))
            _vlock = {"price": p["lock_price"],
                      "adult": bool(p.get("lock_adult")),
                      "open": _vopen}
            if _vopen:
                _vurl = "/locked/post/%d" % pid
            else:
                _vurl = ""
                _vbody = ""
        items.append({
            "post_id": pid,
            "video_url": _vurl,
            "poster_url": _vurl,
            "lock": _vlock,
            "author": {
                "id": p["user_id"],
                "name": a["name"] if a else "Deleted user",
                "avatar": avatar_url(a["avatar"]) if a else "",
                "is_friend": p["user_id"] in friends,
                "following": p["user_id"] in following,
                "is_self": p["user_id"] == viewer["id"],
            },
            "body": _vbody,
            "hashtags": tags,
            "reaction_count": rc,
            "my_reaction": (mine["kind"] or "like") if mine else None,
            "comment_count": cc,
            "created_at": p["created_at"],
            "score": round(score, 2),
            "_created": p["created_at"],
        })
    conn.close()
    # Order: score DESC, then created_at DESC. Two stable passes —
    # created first (ISO strings sort chronologically), then score —
    # so equal scores keep newest-first order.
    items.sort(key=lambda it: it["_created"], reverse=True)
    items.sort(key=lambda it: -it["score"])
    for it in items:
        del it["_created"]
    return items[offset:offset + limit]


def _views_clean_tag(raw):
    """Normalize a ?tag= / chip value to a stored-tag shape, or None."""
    t = (raw or "").strip().lstrip("#").lower()
    if t and re.fullmatch(r"[a-z0-9_]{1,50}", t):
        return t
    return None


@app.get("/views", response_class=HTMLResponse)
async def views_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    tag = _views_clean_tag(request.query_params.get("tag")) or ""
    return render(request, "views.html", {"view_tag": tag})


@app.get("/views.json")
async def views_feed_json(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    q = request.query_params
    tag = _views_clean_tag(q.get("tag"))
    try:
        offset = max(0, int(q.get("offset") or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(q.get("limit") or 10)
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(20, limit))
    views = _views_feed(user, tag, offset, limit)
    resp = {"ok": True, "tag": tag, "views": views}
    # Sponsors in Peeps (Ken's order): every 8 peeps crossed by
    # this page earns one Sponsored slide, chosen by the same ads
    # engine as the home feed (schedule, rotation, per-user daily
    # caps) with the impression recorded at serve. Ads ride
    # BESIDE the peeps list — never inside it — so the client's
    # offset math (peeps only) can never skip or repeat a peep.
    crossed = (offset + len(views)) // 8 - offset // 8
    if crossed > 0:
        conn = db()
        chosen = ads.pick_feed_ads(
            conn, user["id"], crossed,
            now=_chicago_now().replace(tzinfo=None))
        cards = []
        for ad in chosen:
            sponsor_name, sponsor_avatar = "HERE", ""
            if ad.get("kind") == "page" and ad.get("page_id"):
                pg = conn.execute(
                    "SELECT name, cover FROM pages WHERE id=?",
                    (ad["page_id"],)).fetchone()
                if pg:
                    sponsor_name = pg["name"]
                    sponsor_avatar = avatar_url(pg["cover"] or "")
            cards.append(ads.compose_card(ad, sponsor_name,
                                          sponsor_avatar))
            ads.record_impression(conn, ad["id"], user["id"])
        if chosen:
            conn.commit()
        conn.close()
        if cards:
            resp["ads"] = cards
    return JSONResponse(resp)


@app.get("/views/trending")
async def views_trending(request: Request):
    """Top 10 hashtags by number of feed-viewable videos for THIS
    viewer (same candidate rules as the feed itself)."""
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    conn = db()
    ids = [p["id"] for p in _views_candidates(conn, user)]
    counts = {}
    if ids:
        ph = ",".join("?" * len(ids))
        for r in conn.execute(
                f"SELECT tag, COUNT(*) c FROM post_hashtags "
                f"WHERE post_id IN ({ph}) GROUP BY tag", ids).fetchall():
            counts[r["tag"]] = r["c"]
    conn.close()
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    tags = [{"tag": t, "count": c} for t, c in top]
    # Games pack: this week's contest tag is pinned FIRST in trending
    # (the My Views top bar renders a "contest": true chip as a 🏆
    # link to /contest instead of a filter). Its count is the number
    # of entered videos THIS viewer can see.
    _ctag = contest_tag()
    tags = [t for t in tags if t["tag"] != _ctag]
    tags.insert(0, {"tag": _ctag, "count": counts.get(_ctag, 0),
                    "contest": True})
    return JSONResponse({"ok": True, "tags": tags})


def _views_gate(request, pid):
    """Shared gate for the /views/{id}/* endpoints: (user, post, err).
    Login required; the post must exist (share rows resolve to their
    original, like reactions do) and pass can_view_post — the same
    'not found' face for missing and unviewable posts."""
    user = current_user(request)
    if not user:
        return None, None, JSONResponse(
            {"ok": False, "error": "login"}, status_code=401)
    conn = db()
    p = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if p and p["shared_post_id"]:
        p = conn.execute("SELECT * FROM posts WHERE id=?",
                         (p["shared_post_id"],)).fetchone()
    conn.close()
    if not p:
        return None, None, JSONResponse(
            {"ok": False, "error": "not found"}, status_code=404)
    p = dict(p)
    if not can_view_post(user, p, is_owner(request)):
        return None, None, JSONResponse(
            {"ok": False, "error": "not found"}, status_code=404)
    return user, p, None


def _views_record_event(request, pid, kind):
    """'seen' / 'completed' watch events: idempotent (PK + INSERT OR
    IGNORE); only the FIRST event teaches (seen: tags/author +0.5,
    completed: +1/+1)."""
    user, p, err = _views_gate(request, pid)
    if err:
        return err
    conn = db()
    cur = conn.execute(
        "INSERT OR IGNORE INTO view_events(user_id, post_id, kind, "
        "created_at) VALUES(?,?,?,?)",
        (user["id"], p["id"], kind, now_iso()))
    first = cur.rowcount > 0
    if first:
        if kind == "seen":
            _teach_from_post(conn, user["id"], p,
                             VIEWS_LEARN_SEEN_TAG, VIEWS_LEARN_SEEN_AUTHOR)
        elif kind == "completed":
            _teach_from_post(conn, user["id"], p,
                             VIEWS_LEARN_COMPLETED_TAG,
                             VIEWS_LEARN_COMPLETED_AUTHOR)
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "first": first})


@app.post("/views/{pid}/seen")
async def views_seen(request: Request, pid: int):
    return _views_record_event(request, pid, "seen")


@app.post("/views/{pid}/completed")
async def views_completed(request: Request, pid: int):
    return _views_record_event(request, pid, "completed")


@app.post("/views/{pid}/engage")
async def views_engage(request: Request, pid: int):
    """Companion learning signal for a reaction or comment the viewer
    just made from the My Views UI (the react/comment POSTs themselves
    are the wall's endpoints, untouched). The client reports the act;
    the server trusts it the same way it trusts 'seen' — the deltas
    are small and only ever tune the caller's own feed. react: tags
    and author +2; comment: +3."""
    user, p, err = _views_gate(request, pid)
    if err:
        return err
    try:
        data = await request.json()
    except Exception:
        data = {}
    kind = (data or {}).get("kind")
    if kind not in ("react", "comment"):
        return JSONResponse({"ok": False, "error": "bad kind"},
                            status_code=400)
    delta = VIEWS_LEARN_REACT if kind == "react" else VIEWS_LEARN_COMMENT
    conn = db()
    _teach_from_post(conn, user["id"], p, delta, delta)
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.post("/views/{pid}/interest")
async def views_interest(request: Request, pid: int):
    """The explicit teacher. interested=true: the post's hashtags and
    its author get +10 affinity. interested=false: -10 AND this post
    is 'hidden' for the viewer (it leaves their feed for good)."""
    user, p, err = _views_gate(request, pid)
    if err:
        return err
    try:
        data = await request.json()
    except Exception:
        data = {}
    interested = bool((data or {}).get("interested"))
    delta = VIEWS_LEARN_INTEREST if interested else -VIEWS_LEARN_INTEREST
    conn = db()
    _teach_from_post(conn, user["id"], p, delta, delta)
    if not interested:
        conn.execute(
            "INSERT OR IGNORE INTO view_events(user_id, post_id, kind, "
            "created_at) VALUES(?,?,?,?)",
            (user["id"], p["id"], "hidden", now_iso()))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "interested": interested})


@app.get("/views/{pid}/comments")
async def views_comments(request: Request, pid: int):
    """The post's comment thread as JSON — the SAME assembly the post
    page uses (_thread_comments: same query, block filtering, delete
    rules), plus whether the viewer is comment-blocked on this post
    (the post page's blocked-note rule, so the feed's sheet can swap
    the composer for the note)."""
    user, p, err = _views_gate(request, pid)
    if err:
        return err
    conn = db()
    comments = _thread_comments(conn, p, user, is_owner(request))
    blocked = user["id"] in comment_blocked_ids(p["id"])
    conn.close()
    return JSONResponse({"ok": True, "comments": comments,
                         "comment_blocked": blocked})


# ---------------- My Views weekly contest (games pack) ----------------
# A rotating hashtag runs a week-long contest over My Views videos.
# The tag for an ISO week is CONTEST_TAGS[iso_week % len] — the same
# tag for everyone all week, flipping on Chicago Mondays. A video is
# ENTERED when it carries the week's tag, has an uploaded video file,
# and passes the My Views candidate rules for the viewer (the winner
# of a finished week is computed globally — see _contest_winner).
# Score, counted INSIDE the week's Chicago window (Mon 00:00:00 –
# Sun 23:59:59 America/Chicago, compared in UTC):
#   completed views (view_events 'completed') × 1
# + reactions (likes rows) × 2
# + comments × 3
# Ties break to the EARLIEST post. See ROUTE_CONTRACT.md.
CONTEST_TAGS = ["hereshow", "myride", "sundayplate", "porchview",
                "redcarpet", "hometownhero", "freshkicks", "goldenhour"]


def contest_tag(for_date=None):
    """The contest hashtag for the ISO week containing for_date
    (a date; defaults to today, Chicago). Deterministic per week."""
    d = for_date or _today_chicago()
    return CONTEST_TAGS[d.isocalendar()[1] % len(CONTEST_TAGS)]


def week_bounds(for_date=None):
    """(monday, sunday) dates of the week containing for_date
    (defaults to today, Chicago)."""
    d = for_date or _today_chicago()
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def _week_window_utc(for_date=None):
    """The week's Chicago window as aware UTC datetimes:
    Mon 00:00:00 through Sun 23:59:59 America/Chicago."""
    monday, sunday = week_bounds(for_date)
    tz = _chicago_now().tzinfo
    start = datetime(monday.year, monday.month, monday.day, tzinfo=tz)
    end = datetime(sunday.year, sunday.month, sunday.day,
                   23, 59, 59, tzinfo=tz)
    return (start.astimezone(timezone.utc),
            end.astimezone(timezone.utc))


def _contest_parts(conn, pid, start, end):
    """One post's contest score parts inside a UTC window: completed
    views + 2×reactions + 3×comments, each counted from its own
    table's created_at (parsed, so any stored ISO shape compares
    correctly against the window)."""
    def _count(table, extra=""):
        n = 0
        for r in conn.execute(
                f"SELECT created_at FROM {table} "
                f"WHERE post_id=?{extra}", (pid,)).fetchall():
            ts = _parse_ts(r["created_at"])
            if ts is not None and start <= ts <= end:
                n += 1
        return n
    views = _count("view_events", " AND kind='completed'")
    likes = _count("likes")
    comments = _count("comments")
    return {"views": views, "likes": likes, "comments": comments,
            "score": views + 2 * likes + 3 * comments}


def _contest_entry(conn, p, start, end):
    """A leaderboard/winner entry dict for post row p."""
    parts = _contest_parts(conn, p["id"], start, end)
    a = get_user(p["user_id"])
    return {"post_id": p["id"], "user_id": p["user_id"],
            "author_name": a["name"] if a else "Deleted user",
            "author_avatar": avatar_url(a["avatar"]) if a else "",
            "media": "/uploads/" + p["media"] if p["media"] else "",
            "body": p["body"] or "",
            "created_at": p["created_at"], **parts}


def _contest_rank(entries):
    """Best first: score DESC, then earliest post (created_at, id)."""
    entries.sort(key=lambda e: (e["created_at"] or "", e["post_id"]))
    entries.sort(key=lambda e: -e["score"])
    return entries


def _contest_entries_for_viewer(conn, viewer, for_date=None):
    """This week's entered videos FOR a viewer: the My Views
    candidate rules with the week's contest tag, ranked."""
    tag = contest_tag(for_date)
    start, end = _week_window_utc(for_date)
    entries = [_contest_entry(conn, p, start, end)
               for p in _views_candidates(conn, viewer, tag)]
    return _contest_rank(entries)


def _contest_winner(for_date=None):
    """The winning entry of the week containing for_date (any date in
    that week), computed globally — every non-share post with an
    uploaded video file carrying that week's tag, whatever its
    privacy (the profile chip is a public fact about the AUTHOR).
    None when the week had no entries."""
    tag = contest_tag(for_date)
    start, end = _week_window_utc(for_date)
    conn = db()
    rows = conn.execute(
        "SELECT p.* FROM posts p JOIN post_hashtags h "
        "ON h.post_id = p.id WHERE h.tag=? AND p.media != '' "
        "AND p.shared_post_id IS NULL", (tag,)).fetchall()
    entries = []
    for r in rows:
        p = dict(r)
        if _media_ext(p["media"]) not in VID_EXTS:
            continue
        entries.append(_contest_entry(conn, p, start, end))
    conn.close()
    if not entries:
        return None
    return _contest_rank(entries)[0]


def _last_week_winner():
    """The winner of the most recently COMPLETED week (last week's
    Sunday sits in last week, so it names the week + its tag)."""
    monday, _ = week_bounds()
    return _contest_winner(monday - timedelta(days=1))


def contest_winner_user_id():
    """The user id who won the most recently completed week, or None."""
    w = _last_week_winner()
    return w["user_id"] if w else None


@app.get("/contest", response_class=HTMLResponse)
async def contest_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    monday, sunday = week_bounds()
    conn = db()
    entries = _contest_entries_for_viewer(conn, user)[:10]
    conn.close()
    for i, e in enumerate(entries):
        e["rank"] = i + 1
    winner = _last_week_winner()
    return render(request, "contest.html", {
        "contest_tag": contest_tag(),
        "week_sunday": sunday,
        "entries": entries,
        "winner": winner,
        "last_tag": contest_tag(monday - timedelta(days=1))})


# ---------------- Games Hub + friend challenges (Games Pack 2) ----------------
# /games is the one home for play on HERE: a weekly featured game,
# asynchronous friend challenges (Tic-Tac-Toe + Rock-Paper-Scissors
# in game_matches), pointers to the games that live inside lives,
# and the standing ways to play (contest / daily / My Views).
GAMES_CATALOG = [
    {"key": "ttt", "emoji": "❌⭕", "title": "Tic-Tac-Toe",
     "blurb": "The classic three-in-a-row. Challenge a friend and "
              "play it out move by move, right here.",
     "link": "/games"},
    {"key": "rps", "emoji": "🪨📄✂️", "title": "Rock-Paper-Scissors",
     "blurb": "Best of three against a friend — picks stay hidden "
              "until you both throw.",
     "link": "/games"},
    {"key": "guess", "emoji": "🔢", "title": "Guess the Number",
     "blurb": "Somebody in a live is thinking of a number 1–100. "
              "Closest guess takes the crown.",
     "link": "/lives"},
    {"key": "dice", "emoji": "🎲", "title": "Dice Duel",
     "blurb": "Roll in a live room — your best roll is your score. "
              "Highest best wins.",
     "link": "/lives"},
    {"key": "trivia", "emoji": "🎲", "title": "Live Trivia",
     "blurb": "Two-choice trivia questions inside a live, run by "
              "the host. Fast fingers win.",
     "link": "/lives"},
    {"key": "contest", "emoji": "🏆", "title": "My Views Contest",
     "blurb": "Post a video with the week's tag — views, reactions "
              "and comments turn into points.",
     "link": "/contest"},
    {"key": "daily", "emoji": "🔥", "title": "Daily Challenge",
     "blurb": "One prompt every day. Post it, keep your streak "
              "burning.",
     "link": "/daily"},
    {"key": "myviews", "emoji": "🎬", "title": "My Views",
     "blurb": "The endless reel feed that learns what you love — "
              "every watch is a play.",
     "link": "/views"},
]
GAME_TITLES = {g["key"]: g["title"] for g in GAMES_CATALOG}
MATCH_KINDS = ("ttt", "rps")
RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
RPS_EMOJI = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
TTT_LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7),
             (2, 5, 8), (0, 4, 8), (2, 4, 6)]


def featured_game(for_date=None):
    """The Game of the Week: GAMES_CATALOG[iso_week % len] on the
    Chicago calendar — the contest_tag rotation pattern. The same
    entry for everyone all week; it flips on Mondays."""
    d = for_date or _today_chicago()
    return GAMES_CATALOG[d.isocalendar()[1] % len(GAMES_CATALOG)]


def _match_other(match, uid):
    return match["b_id"] if match["a_id"] == uid else match["a_id"]


def _match_state(match):
    try:
        s = json.loads(match["state"] or "{}")
    except Exception:
        s = {}
    return s if isinstance(s, dict) else {}


def _match_status_text(match, uid):
    """The My-matches label for uid: 'Your move 🔴', 'Waiting on
    {name}', or the result once the match is done."""
    other = get_user(_match_other(match, uid)) or {}
    oname = other.get("name", "Deleted user")
    title = GAME_TITLES.get(match["kind"], "Game")
    if match["status"] == "done":
        if match["is_draw"]:
            return f"🤝 Draw in {title}"
        if match["winner_id"] == uid:
            return f"🏆 You won {title}!"
        return f"{oname} won {title}"
    if match["kind"] == "ttt":
        return ("Your move 🔴" if match["turn_id"] == uid
                else f"Waiting on {oname}")
    state = _match_state(match)
    mine = state.get("a_pick") if match["a_id"] == uid \
        else state.get("b_pick")
    return "Your move 🔴" if not mine else f"Waiting on {oname}"


def _match_json(match, viewer_id):
    """A match for API responses, from the viewer's side. RPS picks
    are NEVER exposed while a round is open: the viewer sees only
    their own pick + whether the opponent has picked. Once a round
    resolves, its picks live in state['last'] (public history)."""
    state = _match_state(match)
    other_id = _match_other(match, viewer_id)
    other = get_user(other_id) or {}
    out = {"id": match["id"], "kind": match["kind"],
           "status": match["status"],
           "opponent": {"id": other_id,
                        "name": other.get("name", "Deleted user")},
           "turn_id": match["turn_id"],
           "winner_id": match["winner_id"],
           "is_draw": bool(match["is_draw"]),
           "status_text": _match_status_text(match, viewer_id)}
    if match["kind"] == "ttt":
        out["board"] = state.get("board") or [None] * 9
        out["my_mark"] = "X" if match["a_id"] == viewer_id else "O"
    else:
        mine = state.get("a_pick") if match["a_id"] == viewer_id \
            else state.get("b_pick")
        theirs = state.get("b_pick") if match["a_id"] == viewer_id \
            else state.get("a_pick")
        out["round"] = state.get("round", 1)
        out["my_wins"] = state.get("a_wins", 0) \
            if match["a_id"] == viewer_id else state.get("b_wins", 0)
        out["opp_wins"] = state.get("b_wins", 0) \
            if match["a_id"] == viewer_id else state.get("a_wins", 0)
        out["my_pick"] = mine
        out["opp_picked"] = theirs is not None
        out["last"] = state.get("last")
    return out


def _get_match(conn, mid):
    r = conn.execute("SELECT * FROM game_matches WHERE id=?",
                     (mid,)).fetchone()
    return dict(r) if r else None


def _save_match(conn, match, state=None):
    if state is not None:
        match["state"] = json.dumps(state)
    match["updated_at"] = now_iso()
    conn.execute(
        "UPDATE game_matches SET state=?, turn_id=?, winner_id=?, "
        "is_draw=?, status=?, updated_at=? WHERE id=?",
        (match["state"], match["turn_id"], match["winner_id"],
         match["is_draw"], match["status"], match["updated_at"],
         match["id"]))


def _new_match(conn, kind, a_id, b_id):
    """Insert a fresh match (no commit): a_id challenges b_id; in
    ttt a_id is X and moves first."""
    if kind == "ttt":
        state = {"board": [None] * 9}
        turn = a_id
    else:
        state = {"a_pick": None, "b_pick": None, "round": 1,
                 "a_wins": 0, "b_wins": 0, "last": None}
        turn = None
    stamp = now_iso()
    cur = conn.execute(
        "INSERT INTO game_matches(kind, a_id, b_id, state, turn_id, "
        "status, created_at, updated_at) VALUES(?,?,?,?,?,'active',"
        "?,?)", (kind, a_id, b_id, json.dumps(state), turn, stamp,
                 stamp))
    return cur.lastrowid


@app.get("/games", response_class=HTMLResponse)
async def games_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    friends = []
    for fid in sorted(friend_id_set(user["id"])):
        fu = get_user(fid)
        if fu:
            friends.append({"id": fu["id"], "name": fu["name"],
                            "avatar": avatar_url(fu.get("avatar", ""))})
    friends.sort(key=lambda f: f["name"].lower())
    rows = conn.execute(
        "SELECT * FROM game_matches WHERE a_id=? OR b_id=? "
        "ORDER BY (status='active') DESC, updated_at DESC LIMIT 50",
        (user["id"], user["id"])).fetchall()
    matches = []
    for r in rows:
        m = dict(r)
        other = get_user(_match_other(m, user["id"])) or {}
        matches.append({
            "id": m["id"], "kind": m["kind"],
            "title": GAME_TITLES.get(m["kind"], "Game"),
            "emoji": {"ttt": "❌⭕", "rps": "🪨📄✂️"}.get(m["kind"], "🎮"),
            "opponent_id": _match_other(m, user["id"]),
            "opponent_name": other.get("name", "Deleted user"),
            "status_text": _match_status_text(m, user["id"]),
            "active": m["status"] == "active"})
    conn.close()
    return render(request, "games.html", {
        "featured": featured_game(),
        "friends": friends,
        "matches": matches})


@app.post("/games/challenge")
async def games_challenge(request: Request):
    """Challenge a FRIEND to ttt or rps. Non-friends are refused
    (403), as is challenging yourself. The target gets a bell:
    '🎮 {name} challenged you to {game}!' linking the new match."""
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict) or not data:
        try:
            form = await request.form()
            data = {"kind": form.get("kind"),
                    "user_id": form.get("user_id")}
        except Exception:
            data = {}
    kind = str(data.get("kind") or "").strip()
    if kind not in MATCH_KINDS:
        return JSONResponse({"ok": False, "error": "Unknown game."},
                            status_code=400)
    try:
        target_id = int(data.get("user_id") or 0)
    except (TypeError, ValueError):
        target_id = 0
    if target_id == user["id"]:
        return JSONResponse({"ok": False,
                             "error": "You can't challenge yourself."},
                            status_code=400)
    target = get_user(target_id)
    if not target:
        return JSONResponse({"ok": False, "error": "not found"},
                            status_code=404)
    if not is_friend(user["id"], target_id):
        return JSONResponse(
            {"ok": False,
             "error": "You can only challenge your friends."},
            status_code=403)
    conn = db()
    mid = _new_match(conn, kind, user["id"], target_id)
    conn.commit()
    conn.close()
    notify(target_id, "game",
           f"🎮 {user['name']} challenged you to "
           f"{GAME_TITLES[kind]}!", f"/games/match/{mid}",
           actor_id=user["id"])
    return JSONResponse({"ok": True, "match_id": mid,
                         "url": f"/games/match/{mid}"})


@app.get("/games/match/{mid}", response_class=HTMLResponse)
async def game_match_page(request: Request, mid: int):
    """One match, participants only — anyone else gets a 404 (the
    match's existence is not leaked). RPS: the page renders ONLY the
    viewer's own pick while a round is open; the opponent's pick is
    never in the page until both picks are in and the round is
    history (state['last'])."""
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    match = _get_match(conn, mid)
    conn.close()
    if not match or user["id"] not in (match["a_id"], match["b_id"]):
        return HTMLResponse("<h1>Match not found</h1>",
                            status_code=404)
    state = _match_state(match)
    other = get_user(_match_other(match, user["id"])) or {}
    ctx = {"match": match,
           "other_name": other.get("name", "Deleted user"),
           "other_id": _match_other(match, user["id"]),
           "title": GAME_TITLES.get(match["kind"], "Game"),
           "status_text": _match_status_text(match, user["id"]),
           "me": user["id"]}
    if match["kind"] == "ttt":
        ctx["board"] = state.get("board") or [None] * 9
        ctx["my_mark"] = "X" if match["a_id"] == user["id"] else "O"
        ctx["my_turn"] = (match["status"] == "active"
                          and match["turn_id"] == user["id"])
    else:
        is_a = match["a_id"] == user["id"]
        ctx["round"] = state.get("round", 1)
        ctx["my_pick"] = state.get("a_pick") if is_a \
            else state.get("b_pick")
        ctx["opp_picked"] = (state.get("b_pick") if is_a
                             else state.get("a_pick")) is not None
        ctx["my_wins"] = state.get("a_wins", 0) if is_a \
            else state.get("b_wins", 0)
        ctx["opp_wins"] = state.get("b_wins", 0) if is_a \
            else state.get("a_wins", 0)
        last = state.get("last") or {}
        ctx["last"] = last
        ctx["last_my_pick"] = last.get("a_pick") if is_a \
            else last.get("b_pick")
        ctx["last_opp_pick"] = last.get("b_pick") if is_a \
            else last.get("a_pick")
        ctx["rps_emoji"] = RPS_EMOJI
    return render(request, "game_match.html", ctx)


@app.post("/games/match/{mid}/move")
async def game_match_move(request: Request, mid: int):
    """One move. ttt {cell 0-8}: my turn + empty cell or 4xx; a win
    is any of the 8 lines; a full board is a draw. rps {choice}:
    my pick is stored hidden; when both picks are in the round
    resolves (rock>scissors>paper>rock), the winner's wins go up,
    picks clear, and the first player to 2 round-wins takes the
    match. The opponent is notified on every move / resolution."""
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    conn = db()
    match = _get_match(conn, mid)
    if not match or user["id"] not in (match["a_id"], match["b_id"]):
        conn.close()
        return JSONResponse({"ok": False, "error": "not found"},
                            status_code=404)
    if match["status"] != "active":
        conn.close()
        return JSONResponse({"ok": False,
                             "error": "This match is over."},
                            status_code=409)
    title = GAME_TITLES.get(match["kind"], "Game")
    other_id = _match_other(match, user["id"])
    state = _match_state(match)
    note = None
    if match["kind"] == "ttt":
        if match["turn_id"] != user["id"]:
            conn.close()
            return JSONResponse({"ok": False,
                                 "error": "It's not your turn."},
                                status_code=403)
        try:
            cell = int(data.get("cell"))
        except (TypeError, ValueError):
            conn.close()
            return JSONResponse({"ok": False, "error": "Bad cell."},
                                status_code=400)
        board = state.get("board") or [None] * 9
        if cell < 0 or cell > 8 or board[cell] is not None:
            conn.close()
            return JSONResponse(
                {"ok": False, "error": "That cell is taken."},
                status_code=400)
        mark = "X" if match["a_id"] == user["id"] else "O"
        board[cell] = mark
        state["board"] = board
        won = any(all(board[i] == mark for i in line)
                  for line in TTT_LINES)
        if won:
            match["status"] = "done"
            match["winner_id"] = user["id"]
            match["turn_id"] = None
            note = f"🏆 {user['name']} won {title} against you!"
        elif all(c is not None for c in board):
            match["status"] = "done"
            match["is_draw"] = 1
            match["turn_id"] = None
            note = f"🤝 Your {title} match with {user['name']} " \
                   f"is a draw."
        else:
            match["turn_id"] = other_id
            note = f"🎮 Your move in {title} vs {user['name']}"
    else:
        choice = str(data.get("choice") or "").strip()
        if choice not in RPS_BEATS:
            conn.close()
            return JSONResponse({"ok": False,
                                 "error": "Pick rock, paper or scissors."},
                                status_code=400)
        mine_key = "a_pick" if match["a_id"] == user["id"] else "b_pick"
        theirs_key = "b_pick" if mine_key == "a_pick" else "a_pick"
        if state.get(mine_key):
            conn.close()
            return JSONResponse(
                {"ok": False,
                 "error": "You already picked this round."},
                status_code=400)
        state[mine_key] = choice
        if state.get(theirs_key):
            a_pick, b_pick = state["a_pick"], state["b_pick"]
            if a_pick == b_pick:
                round_winner = None
            elif RPS_BEATS[a_pick] == b_pick:
                round_winner = match["a_id"]
            else:
                round_winner = match["b_id"]
            if round_winner == match["a_id"]:
                state["a_wins"] = state.get("a_wins", 0) + 1
            elif round_winner == match["b_id"]:
                state["b_wins"] = state.get("b_wins", 0) + 1
            state["last"] = {"a_pick": a_pick, "b_pick": b_pick,
                             "winner_id": round_winner,
                             "round": state.get("round", 1)}
            state["a_pick"] = None
            state["b_pick"] = None
            state["round"] = state.get("round", 1) + 1
            if state["a_wins"] >= 2 or state["b_wins"] >= 2:
                match["status"] = "done"
                match["winner_id"] = (match["a_id"]
                                       if state["a_wins"] >= 2
                                       else match["b_id"])
                note = (f"🏆 {user['name']} won {title} against you!"
                        if match["winner_id"] != other_id else
                        f"🏆 You won {title} vs {user['name']}!")
            else:
                note = f"🎮 Your move in {title} vs {user['name']}"
        else:
            note = f"🎮 Your move in {title} vs {user['name']}"
    _save_match(conn, match, state)
    conn.commit()
    out = _match_json(match, user["id"])
    conn.close()
    record_activity(user["id"])
    if note:
        notify(other_id, "game", note, f"/games/match/{mid}",
               actor_id=user["id"])
    return JSONResponse({"ok": True, "match": out})


@app.post("/games/match/{mid}/rematch")
async def game_match_rematch(request: Request, mid: int):
    """Start a fresh match of the same kind between the same two
    players (the finished match only). ttt swaps who starts: the
    previous b_id is the new a_id (X, first move). rps keeps a/b.
    The other player is notified; we redirect to the new match."""
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    match = _get_match(conn, mid)
    if not match or user["id"] not in (match["a_id"], match["b_id"]):
        conn.close()
        return HTMLResponse("<h1>Match not found</h1>",
                            status_code=404)
    if match["status"] != "done":
        conn.close()
        return JSONResponse({"ok": False,
                             "error": "Finish this match first."},
                            status_code=409)
    if match["kind"] == "ttt":
        a_id, b_id = match["b_id"], match["a_id"]
    else:
        a_id, b_id = match["a_id"], match["b_id"]
    new_id = _new_match(conn, match["kind"], a_id, b_id)
    conn.commit()
    conn.close()
    other_id = _match_other(match, user["id"])
    notify(other_id, "game",
           f"🎮 {user['name']} wants a rematch in "
           f"{GAME_TITLES.get(match['kind'], 'Game')}!",
           f"/games/match/{new_id}", actor_id=user["id"])
    return redir(f"/games/match/{new_id}")


@app.post("/games/match/{mid}/forfeit")
async def game_match_forfeit(request: Request, mid: int):
    """Give up an active match: it ends, the other player wins."""
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    conn = db()
    match = _get_match(conn, mid)
    if not match or user["id"] not in (match["a_id"], match["b_id"]):
        conn.close()
        return JSONResponse({"ok": False, "error": "not found"},
                            status_code=404)
    if match["status"] != "active":
        conn.close()
        return JSONResponse({"ok": False,
                             "error": "This match is over."},
                            status_code=409)
    other_id = _match_other(match, user["id"])
    match["status"] = "done"
    match["winner_id"] = other_id
    match["turn_id"] = None
    _save_match(conn, match)
    conn.commit()
    conn.close()
    notify(other_id, "game",
           f"🏆 {user['name']} forfeited your "
           f"{GAME_TITLES.get(match['kind'], 'Game')} match — you win!",
           f"/games/match/{mid}", actor_id=user["id"])
    return JSONResponse({"ok": True})


# ---------------- stories (friends-only, custom lifetimes) ----------------
STORY_FONTS = ("classic", "deco", "bubble", "typewriter", "meme",
               "strong", "neon")
STORY_FILTERS = ("original", "vivid", "noir", "gold", "cool", "fade",
                 "glow", "punch")
STORY_ART = ("heart", "star", "flame", "crown", "bolt", "moon",
             "music", "rose", "gem", "like", "party", "eyes")
STORY_TEXT_BGS = ("dusk", "ember", "ocean", "forest", "grape", "mono")


def _clean_story_edit(raw):
    """Validate the visual editor's JSON payload. Anything malformed
    is dropped piece by piece; the result is always a safe dict the
    viewer can render without trusting the client."""
    import json as _json
    try:
        d = _json.loads(raw or "{}")
    except Exception:
        return {}
    if not isinstance(d, dict):
        return {}

    def _f(v, lo, hi, default):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, v))

    out = {}
    if d.get("mode") == "text":
        out["mode"] = "text"
        bg = str(d.get("textbg") or "dusk")
        out["textbg"] = bg if bg in STORY_TEXT_BGS else "dusk"
    if d.get("filter") in STORY_FILTERS and d["filter"] != "original":
        out["filter"] = d["filter"]
    if isinstance(d.get("trim"), dict):
        st = _f(d["trim"].get("start"), 0, 3600, 0)
        en = _f(d["trim"].get("end"), 0, 3600, 0)
        if en > st + 0.4:
            out["trim"] = {"start": round(st, 2), "end": round(en, 2)}
    if d.get("boomerang"):
        out["boomerang"] = True
    texts = []
    for tx in (d.get("texts") or [])[:6]:
        if not isinstance(tx, dict):
            continue
        body = str(tx.get("t") or "").strip()[:120]
        if not body:
            continue
        color = str(tx.get("color") or "#ffffff")
        if not _spot_hex_ok(color):
            color = "#ffffff"
        texts.append({
            "t": body,
            "font": tx.get("font") if tx.get("font") in STORY_FONTS
                    else "classic",
            "color": color, "bg": bool(tx.get("bg")),
            "align": tx.get("align") if tx.get("align") in
                     ("left", "center", "right") else "center",
            "x": _f(tx.get("x"), 0, 100, 50), "y": _f(tx.get("y"), 0,
                                                      100, 50),
            "scale": _f(tx.get("scale"), 0.4, 4, 1),
            "rot": _f(tx.get("rot"), -180, 180, 0)})
    if texts:
        out["texts"] = texts
    stickers = []
    for sk in (d.get("stickers") or [])[:12]:
        if not isinstance(sk, dict):
            continue
        kind = str(sk.get("kind") or "")
        item = {"x": _f(sk.get("x"), 0, 100, 50),
                "y": _f(sk.get("y"), 0, 100, 50),
                "scale": _f(sk.get("scale"), 0.4, 4, 1),
                "rot": _f(sk.get("rot"), -180, 180, 0)}
        if kind in STORY_ART:
            item["kind"] = kind
        elif kind == "location":
            label = str(sk.get("label") or "").strip()[:60]
            if not label:
                continue
            item.update(kind="location", label=label)
        elif kind == "time":
            item["kind"] = "time"
        elif kind == "link":
            url = str(sk.get("url") or "").strip()[:300]
            if not url.lower().startswith(("http://", "https://")):
                continue
            item.update(kind="link", url=url,
                        label=str(sk.get("label") or "Visit link")[:40])
        elif kind == "mention":
            try:
                uid = int(sk.get("uid"))
            except (TypeError, ValueError):
                continue
            name = str(sk.get("name") or "").strip()[:80]
            if uid <= 0 or not name:
                continue
            item.update(kind="mention", uid=uid, name=name)
        elif kind in ("question", "addyours"):
            prompt = str(sk.get("prompt") or "").strip()[:120]
            if not prompt:
                continue
            item.update(kind=kind, prompt=prompt)
        else:
            continue
        stickers.append(item)
    if stickers:
        out["stickers"] = stickers
    caps = str(d.get("captions") or "").strip()[:500]
    if caps:
        out["captions"] = caps
    mentions = []
    for mid in (d.get("mentions") or [])[:10]:
        try:
            mid = int(mid)
        except (TypeError, ValueError):
            continue
        if mid > 0 and mid not in mentions:
            mentions.append(mid)
    if mentions:
        out["mentions"] = mentions
    return out


def _story_edit_of(s):
    """A story row's cleaned editor payload ({} when none/garbled)."""
    import json as _json
    if isinstance(s, dict):
        raw = s.get("edit_json") or ""
    else:
        try:
            raw = s["edit_json"] or ""
        except Exception:
            raw = ""
    if not raw:
        return {}
    try:
        d = _json.loads(raw)
    except Exception:
        return {}
    return d if isinstance(d, dict) else {}


def _story_row_viewable(viewer_id, s):
    """May viewer see this story row? Own + friends (the classic rule),
    plus the editor era: PUBLIC stories for anyone signed in, and any
    story that MENTIONS the viewer."""
    if not s or (s["expires_at"] or "") < now_iso():
        return False
    if s["user_id"] == viewer_id:
        return True
    if s["user_id"] in friend_id_set(viewer_id):
        return True
    if (s["audience"] or "friends") == "public":
        return True
    edit = _story_edit_of(dict(s))
    return viewer_id in (edit.get("mentions") or [])


def _story_extra_authors(viewer_id):
    """Authors outside the friend set whose stories may still reach
    this viewer: anyone with a PUBLIC live story + anyone who mentioned
    the viewer in a live story."""
    conn = db()
    now = now_iso()
    rows = conn.execute(
        "SELECT user_id, audience, edit_json FROM stories "
        "WHERE expires_at > ?", (now,)).fetchall()
    conn.close()
    out = set()
    for r in rows:
        if r["user_id"] in out:
            continue
        if (r["audience"] or "friends") == "public":
            # Public means public: the author joins the viewer's tray
            # and queue (their friends-only stories still filter out
            # per-story in _story_row_viewable).
            out.add(r["user_id"])
            continue
        edit = _story_edit_of(dict(r))
        if viewer_id in (edit.get("mentions") or []):
            out.add(r["user_id"])
    return out


def story_expires_at(minutes=STORY_TTL_HOURS * 60):
    return (datetime.now(timezone.utc)
            + timedelta(minutes=minutes)).isoformat()


def prune_stories():
    """Lazy expiry: drop expired stories and orphaned views."""
    conn = db()
    conn.execute("DELETE FROM stories WHERE expires_at < ?", (now_iso(),))
    conn.execute("DELETE FROM story_views WHERE story_id NOT IN (SELECT id FROM stories)")
    conn.commit()
    conn.close()


STORY_TEXT_GRADS = {
    "dusk": "linear-gradient(160deg,#23233a,#4a1942 55%,#8f1d22)",
    "ember": "linear-gradient(160deg,#1c0f0a,#7a2408 55%,#e04707)",
    "ocean": "linear-gradient(160deg,#04141f,#0a4d5c 55%,#0e86a0)",
    "forest": "linear-gradient(160deg,#0a1a10,#14532d 55%,#3f9e4d)",
    "grape": "linear-gradient(160deg,#170a26,#4c1d95 55%,#9333ea)",
    "mono": "linear-gradient(160deg,#0a0a0c,#26262c 55%,#52525b)",
}


def stories_tray(viewer_id):
    """Tray entries for viewer + friends with active stories. Self first, then newest."""
    prune_stories()
    conn = db()
    now = now_iso()
    tray, others = [], []
    _authors = friend_id_set(viewer_id) | {viewer_id} \
        | _story_extra_authors(viewer_id)
    for fid in _authors:
        u = conn.execute("SELECT id,name,avatar FROM users WHERE id=?", (fid,)).fetchone()
        if not u:
            continue
        ss = conn.execute(
            "SELECT id, media, created_at, audience, edit_json, expires_at, user_id FROM stories WHERE user_id=? AND expires_at > ? "
            "ORDER BY created_at DESC", (fid, now)).fetchall()
        if fid not in friend_id_set(viewer_id) and fid != viewer_id:
            ss = [s for s in ss if _story_row_viewable(viewer_id, s)]
        if not ss:
            continue
        sids = [s["id"] for s in ss]
        ph = ",".join("?" * len(sids))
        seen = {r["story_id"] for r in conn.execute(
            f"SELECT story_id FROM story_views WHERE user_id=? AND story_id IN ({ph})",
            (viewer_id, *sids)).fetchall()}
        # Card preview = the NEWEST story's own face: a photo
        # story shows its photo, a video story its video, a text
        # story its gradient. Media is stored RELATIVE ('stories/
        # x.jpg') — the URL needs the /uploads/ prefix or the tray
        # <img> 404s and shows the broken-image icon (Ken's report
        # Oct 27: 'Your story' card black with a ? in the middle).
        thumb, thumb_video, thumb_grad = "", "", ""
        for s in ss:
            m = (s["media"] or "")
            ml = m.lower()
            url = m if m.startswith(("http://", "https://", "/")) \
                else ("/uploads/" + m if m else "")
            if ml.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
                thumb = url
                break
            if ml.endswith((".mp4", ".mov", ".webm")):
                thumb_video = url
                break
            ed = _story_edit_of(s)
            if ed.get("mode") == "text":
                thumb_grad = STORY_TEXT_GRADS.get(
                    ed.get("textbg") or "dusk", STORY_TEXT_GRADS["dusk"])
                break
        entry = {
            "user_id": u["id"], "name": u["name"],
            "avatar": avatar_url(u["avatar"]),
            "is_self": fid == viewer_id,
            "first_id": ss[-1]["id"], "count": len(ss),
            "all_viewed": all(s in seen for s in sids),
            "latest": ss[0]["created_at"] or "",
            "thumb": thumb, "thumb_video": thumb_video,
            "thumb_grad": thumb_grad,
        }
        (tray if fid == viewer_id else others).append(entry)
    conn.close()
    others.sort(key=lambda t: t["latest"], reverse=True)
    return tray + others


def _get_story(sid):
    conn = db()
    s = conn.execute("SELECT * FROM stories WHERE id=?", (sid,)).fetchone()
    conn.close()
    return dict(s) if s else None


def _can_view_story(user, s):
    if not user or not s:
        return False
    return _story_row_viewable(user["id"], s)


# ---- YouTube music helpers ----
# Story/post music is YouTube-only: the song streams through YouTube's
# own embedded player (the artist's official upload, YouTube's license).
# Only a strictly-validated 11-char video id is ever stored or embedded,
# so a pasted link can never turn into an arbitrary iframe.
_YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
MUSIC_EMBED_ERR = ("That video can't be played inside HERE — "
                   "try a different upload of the song.")


def youtube_id_from(text):
    """Extract an 11-char YouTube video id from a pasted link (watch,
    youtu.be, music.youtube.com, shorts, embed, live, m.youtube.com)
    or a bare id. Returns None for anything else."""
    if not text:
        return None
    t = str(text).strip()
    if _YT_ID_RE.match(t):
        return t
    try:
        u = urlparse(t)
    except Exception:
        return None
    host = (u.netloc or "").lower().split("@")[-1].split(":")[0]
    cand = None
    if host == "youtu.be":
        cand = (u.path or "").strip("/").split("/")[0]
    elif host in ("youtube.com", "www.youtube.com", "music.youtube.com",
                  "m.youtube.com"):
        if u.path == "/watch":
            cand = (parse_qs(u.query).get("v") or [None])[0]
        else:
            parts = [p for p in (u.path or "").split("/") if p]
            if len(parts) >= 2 and parts[0] in ("shorts", "embed", "live"):
                cand = parts[1]
    if cand and _YT_ID_RE.match(cand):
        return cand
    return None


def _oembed_fetch(video_id):
    """Raw YouTube oEmbed fetch — the one network call, kept separate
    so tests can stub it. Returns the parsed JSON dict, or None on any
    failure. oEmbed answers 401/404 for videos that may not be embedded
    or don't exist, which is exactly the gate music attach needs."""
    url = ("https://www.youtube.com/oembed?url=" +
           quote("https://www.youtube.com/watch?v=" + video_id, safe="") +
           "&format=json")
    req = urllib.request.Request(url, headers={"User-Agent": "HERE/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def youtube_oembed(video_id):
    """{title, artist, thumb} for an embeddable video, else None."""
    d = _oembed_fetch(video_id)
    if not d or not d.get("title"):
        return None
    return {"title": str(d.get("title") or "")[:120],
            "artist": str(d.get("author_name") or "")[:80],
            "thumb": str(d.get("thumbnail_url") or "")[:300]}


def _music_snapshot(video_id):
    """Title/artist/thumb snapshot from an existing attachment of this
    video (a story or a post), or None. 'Popular on HERE' picks reuse
    it — the song is already playing on HERE, so no oEmbed call is
    needed at save time."""
    conn = db()
    r = conn.execute(
        "SELECT music_title AS t, music_artist AS a, music_thumb AS th "
        "FROM stories WHERE music_kind='youtube' AND music_ref=? "
        "AND music_title != '' LIMIT 1", (video_id,)).fetchone()
    if not r:
        r = conn.execute(
            "SELECT music_title AS t, music_artist AS a, music_thumb AS th "
            "FROM posts WHERE music_video_id=? AND music_title != '' "
            "LIMIT 1", (video_id,)).fetchone()
    conn.close()
    if not r:
        return None
    return {"title": r["t"] or "", "artist": r["a"] or "",
            "thumb": r["th"] or ""}


def _resolve_music(form):
    """Resolve a composer's music fields (music_url pasted link, or
    music_video_id from a Popular pick) to a snapshot dict
    {video_id, title, artist, thumb}. Returns None when no music was
    asked for; raises ValueError with a user-facing message when a song
    was asked for but can't be played inside HERE."""
    url = (form.get("music_url") or "").strip()
    picked = (form.get("music_video_id") or "").strip()
    if not url and not picked:
        return None
    vid = youtube_id_from(url) if url else None
    if vid is None and picked and _YT_ID_RE.match(picked):
        vid = picked
    if vid is None:
        raise ValueError("That link doesn't look like a YouTube link.")
    if not url:
        snap = _music_snapshot(vid)
        if snap:
            return {"video_id": vid, **snap}
    meta = youtube_oembed(vid)
    if meta is None:
        raise ValueError(MUSIC_EMBED_ERR)
    return {"video_id": vid, **meta}


def _story_payload(conn, s, viewer_id, seen):
    media_url = "/uploads/" + s["media"] if s["media"] else ""
    overlay = None
    if (s["overlay_text"] or "").strip():
        overlay = {"text": s["overlay_text"],
                   "color": s["overlay_color"] or "#ffffff",
                   "pos": s["overlay_pos"] or "bottom"}
    # Music is YouTube-only now. Stories saved in the preset-catalog /
    # MP3-upload era carry kind 'catalog'/'upload' — they render with
    # NO music (their files are gone), never a broken audio element.
    music = None
    if s["music_kind"] == "youtube" and s["music_ref"]:
        music = {"kind": "youtube", "video_id": s["music_ref"],
                 "title": s["music_title"] or "",
                 "artist": s["music_artist"] or "",
                 "thumb": s["music_thumb"] or "",
                 "start": s["music_start"] or 0,
                 "len": s["music_len"] or 15,
                 "volume": s["music_volume"] if s["music_volume"] is not None else 80,
                 "mute_orig": bool(s["music_mute_orig"])}
    # Games pack: the story's binary poll (when one is attached) rides
    # the same payload — counts for both choices + the VIEWER's pick,
    # so the viewer can render buttons or result bars without another
    # fetch. ama is the "Ask me a question" flag.
    poll = None
    prow = conn.execute("SELECT * FROM story_polls WHERE story_id=?",
                        (s["id"],)).fetchone()
    if prow:
        counts = {r["choice"]: r["c"] for r in conn.execute(
            "SELECT choice, COUNT(*) c FROM story_poll_votes "
            "WHERE story_id=? GROUP BY choice", (s["id"],)).fetchall()}
        mine = conn.execute(
            "SELECT choice FROM story_poll_votes WHERE story_id=? "
            "AND user_id=?", (s["id"], viewer_id)).fetchone()
        poll = {"question": prow["question"],
                "a_text": prow["a_text"], "b_text": prow["b_text"],
                "a_votes": counts.get("a", 0),
                "b_votes": counts.get("b", 0),
                "total": counts.get("a", 0) + counts.get("b", 0),
                "my_choice": mine["choice"] if mine else None}
    return {
        "id": s["id"], "media": media_url,
        "is_video": (s["media"] or "").lower().endswith((".mp4", ".mov", ".webm")),
        "caption": s["caption"] or "",
        "created_at": s["created_at"], "expires_at": s["expires_at"],
        "mine": s["user_id"] == viewer_id, "seen": bool(seen),
        "audience": s["audience"] or "friends",
        "edit": _story_edit_of(dict(s)),
        "overlay": overlay, "music": music,
        "poll": poll,
        "ama": bool(s["ama"]) if "ama" in s.keys() else False,
    }


def _story_queue(viewer_id):
    """Full ordered queue for the full-screen viewer: the viewer's own
    group first, then friends by most recent story (same order as the
    tray); stories inside a group run oldest -> newest. Only stories
    the viewer may see (own + friends', unexpired) are ever included."""
    prune_stories()
    conn = db()
    now = now_iso()
    groups = []
    _qauthors = friend_id_set(viewer_id) | {viewer_id} \
        | _story_extra_authors(viewer_id)
    for fid in _qauthors:
        u = conn.execute("SELECT id,name,avatar FROM users WHERE id=?",
                         (fid,)).fetchone()
        if not u:
            continue
        ss = conn.execute(
            "SELECT * FROM stories WHERE user_id=? AND expires_at > ? "
            "ORDER BY created_at ASC", (fid, now)).fetchall()
        if fid not in friend_id_set(viewer_id) and fid != viewer_id:
            ss = [s for s in ss if _story_row_viewable(viewer_id, s)]
        if not ss:
            continue
        sids = [s["id"] for s in ss]
        ph = ",".join("?" * len(sids))
        seen = {r["story_id"] for r in conn.execute(
            f"SELECT story_id FROM story_views WHERE user_id=? AND story_id IN ({ph})",
            (viewer_id, *sids)).fetchall()}
        groups.append({
            "user_id": u["id"], "name": u["name"],
            "avatar": avatar_url(u["avatar"]),
            "is_self": fid == viewer_id,
            "latest": ss[-1]["created_at"] or "",
            "stories": [_story_payload(conn, s, viewer_id, s["id"] in seen)
                        for s in ss],
        })
    conn.close()
    mine = [g for g in groups if g["is_self"]]
    rest = sorted((g for g in groups if not g["is_self"]),
                  key=lambda g: g["latest"], reverse=True)
    return mine + rest


def _story_lifetime_minutes(form):
    """Composer lifetime -> minutes, always clamped to [5, 1440]."""
    choice = (form.get("lifetime") or "24h").strip()
    if choice in STORY_LIFETIME_PRESETS:
        mins = STORY_LIFETIME_PRESETS[choice]
    elif choice == "custom":
        try:
            hours = float(form.get("custom_hours") or 24)
        except (TypeError, ValueError):
            hours = 24
        mins = int(round(hours * 60))
    else:
        mins = STORY_TTL_HOURS * 60
    return max(STORY_MIN_MINUTES, min(STORY_MAX_MINUTES, mins))


def _lifetime_flash(mins):
    if mins < 60:
        return f"Story posted — it disappears in {mins} minutes."
    if mins == 60:
        return "Story posted — it disappears in 1 hour."
    if mins < STORY_MAX_MINUTES:
        return f"Story posted — it disappears in {mins // 60} hours."
    return "Story posted — it disappears in 24 hours."


@app.post("/music/preview")
async def music_preview(request: Request):
    """Validate a pasted YouTube link for the music picker: extract the
    id and ask YouTube's oEmbed whether the video may be embedded."""
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    try:
        data = await request.json()
    except Exception:
        data = {}
    url = (data.get("url") or "") if isinstance(data, dict) else ""
    vid = youtube_id_from(url)
    if not vid:
        return JSONResponse(
            {"ok": False,
             "error": "That link doesn't look like a YouTube link."})
    meta = youtube_oembed(vid)
    if meta is None:
        return JSONResponse({"ok": False, "error": MUSIC_EMBED_ERR})
    return JSONResponse({"ok": True, "video_id": vid,
                         "title": meta["title"], "artist": meta["artist"],
                         "thumb": meta["thumb"]})


@app.get("/music/popular")
async def music_popular(request: Request):
    """Most-attached YouTube songs across stories + posts on HERE."""
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    conn = db()
    rows = conn.execute(
        "SELECT video_id, MAX(title) AS title, MAX(artist) AS artist, "
        "MAX(thumb) AS thumb, COUNT(*) AS uses FROM ("
        "  SELECT music_ref AS video_id, music_title AS title, "
        "         music_artist AS artist, music_thumb AS thumb "
        "  FROM stories WHERE music_kind='youtube' AND music_ref != '' "
        "  UNION ALL "
        "  SELECT music_video_id AS video_id, music_title AS title, "
        "         music_artist AS artist, music_thumb AS thumb "
        "  FROM posts WHERE music_video_id != ''"
        ") GROUP BY video_id ORDER BY uses DESC, title ASC LIMIT 10"
    ).fetchall()
    conn.close()
    return JSONResponse({"ok": True, "songs": [
        {"video_id": r["video_id"], "title": r["title"] or "",
         "artist": r["artist"] or "", "thumb": r["thumb"] or "",
         "uses": r["uses"]} for r in rows]})


def _own_media_path(uid, path):
    """True when `path` is one of THIS user's own uploads (a post's or
    story's media, their avatar or background) with a story-legal
    extension - the picker grid may only reference media the user
    already owns on HERE."""
    path = (path or "").strip()
    if not path or ".." in path or path.startswith("/"):
        return False
    if ("." + path.rsplit(".", 1)[-1].lower()) not in \
            {"." + e for e in STORY_EXTS}:
        return False
    conn = db()
    hit = conn.execute(
        "SELECT 1 FROM posts WHERE user_id=? AND media=?", (uid, path)
    ).fetchone() or conn.execute(
        "SELECT 1 FROM stories WHERE user_id=? AND media=?", (uid, path)
    ).fetchone() or conn.execute(
        "SELECT 1 FROM users WHERE id=? AND (avatar=? OR background=?)",
        (uid, path, path)).fetchone()
    conn.close()
    return bool(hit)


@app.get("/stories/new", response_class=HTMLResponse)
async def story_new_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    prompt = (request.query_params.get("prompt") or "").strip()[:120]
    return render(request, "story_new.html", {"addyours_prompt": prompt})


@app.get("/stories/media")
async def story_media_recent(request: Request):
    """The editor picker's grid: the signed-in user's own recent media
    on HERE (posts + stories + avatar/background), newest first."""
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    conn = db()
    items, seen_paths = [], set()

    def _add(path):
        if not path or path in seen_paths:
            return
        ext = ("." + path.rsplit(".", 1)[-1].lower()) if "." in path \
            else ""
        if ext not in {"." + e for e in STORY_EXTS}:
            return
        if not _upload_exists(path):
            return
        seen_paths.add(path)
        items.append({
            "path": path, "url": "/uploads/" + path,
            "video": ext in (".mp4", ".mov", ".webm")})

    for r in conn.execute(
            "SELECT media FROM posts WHERE user_id=? AND page_id IS NULL "
            "AND media != '' ORDER BY id DESC LIMIT 48",
            (user["id"],)).fetchall():
        _add(r["media"])
    for r in conn.execute(
            "SELECT media FROM stories WHERE user_id=? AND media != '' "
            "ORDER BY id DESC LIMIT 24", (user["id"],)).fetchall():
        _add(r["media"])
    me = conn.execute("SELECT avatar, background FROM users WHERE id=?",
                      (user["id"],)).fetchone()
    if me:
        _add(me["avatar"])
        _add(me["background"])
    conn.close()
    return JSONResponse({"ok": True, "items": items[:60]})


@app.get("/stories/people")
async def story_people_search(request: Request):
    """Mention picker search: people by name (blocked pairs excluded)."""
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    q = (request.query_params.get("q") or "").strip()[:60]
    if len(q) < 2:
        return JSONResponse({"ok": True, "people": []})
    conn = db()
    rows = conn.execute(
        "SELECT id, name, avatar FROM users WHERE name LIKE ? "
        "AND id != ? AND id NOT IN (SELECT blocked_id FROM blocks "
        "WHERE blocker_id=?) AND id NOT IN (SELECT blocker_id FROM "
        "blocks WHERE blocked_id=?) ORDER BY name LIMIT 10",
        ("%" + q + "%", user["id"], user["id"], user["id"])).fetchall()
    conn.close()
    return JSONResponse({"ok": True, "people": [
        {"id": r["id"], "name": r["name"],
         "avatar": avatar_url(r["avatar"])} for r in rows]})


@app.post("/story/{sid}/reshare")
async def story_reshare(request: Request, sid: int):
    """A mentioned viewer shares the story to their own story (the
    rule from Ken's spec video: anyone tagged can share it onward).
    The copy is a fresh friends-audience story; the poll and the
    mention list do NOT carry over."""
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    s = _get_story(sid)
    if not s or s["user_id"] == user["id"] \
            or not _can_view_story(user, s):
        return JSONResponse({"ok": False, "error": "not found"},
                            status_code=404)
    edit = _story_edit_of(s)
    if user["id"] not in (edit.get("mentions") or []):
        return JSONResponse({"ok": False, "error": "not mentioned"},
                            status_code=403)
    new_edit = {k: v for k, v in edit.items() if k != "mentions"}
    import json as _json
    conn = db()
    cur = conn.execute(
        "INSERT INTO stories(user_id, media, caption, created_at, "
        "expires_at, music_kind, music_ref, music_title, music_artist, "
        "music_thumb, music_start, music_len, music_volume, "
        "music_mute_orig, edit_json, audience) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user["id"], s["media"], s["caption"] or "", now_iso(),
         story_expires_at(), s["music_kind"], s["music_ref"] or "",
         s["music_title"] or "", s["music_artist"] or "",
         s["music_thumb"] or "", s["music_start"] or 0,
         s["music_len"] or 15,
         s["music_volume"] if s["music_volume"] is not None else 80,
         s["music_mute_orig"] or 0, _json.dumps(new_edit), "friends"))
    new_id = cur.lastrowid
    conn.commit()
    conn.close()
    notify(s["user_id"], "story_reshare",
           f"{user['name']} shared your story to their story",
           f"/story/{new_id}", actor_id=user["id"])
    return JSONResponse({"ok": True, "id": new_id})


@app.post("/stories/new")
async def story_create(request: Request):
    user, err = require_login(request)
    if err:
        return err
    form = await request.form()
    caption = (form.get("caption") or "").strip()[:280]
    # --- write-on-story overlay (validated; bad values fall back) ---
    overlay_text = (form.get("overlay_text") or "").strip()[:80]
    overlay_color = (form.get("overlay_color") or "").strip()
    if not _spot_hex_ok(overlay_color):
        overlay_color = "#ffffff" if overlay_text else ""
    overlay_pos = (form.get("overlay_pos") or "bottom").strip()
    if overlay_pos not in ("top", "middle", "bottom"):
        overlay_pos = "bottom"
    if not overlay_text:
        overlay_color, overlay_pos = "", "bottom"
    # --- music trim/volume controls (clamped no matter what posts) ---
    def _int_field(name, lo, hi, default):
        try:
            v = int(form.get(name))
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, v))
    music_start = _int_field("music_start", 0, 3600, 0)
    music_len = _int_field("music_len", 5, 30, 15)
    music_volume = _int_field("music_volume", 0, 100, 80)
    music_mute_orig = 1 if (form.get("music_mute_orig") or "") == "1" else 0
    # --- games pack: story poll + AMA (validated BEFORE anything is
    # saved — a partial poll refuses the whole story, nothing created).
    # The poll is all-or-nothing: question + BOTH choices, or none of
    # the three fields may be filled in. ---
    poll_q = (form.get("poll_question") or "").strip()[:120]
    poll_a = (form.get("poll_a") or "").strip()[:40]
    poll_b = (form.get("poll_b") or "").strip()[:40]
    story_poll = None
    if poll_q or poll_a or poll_b:
        if not (poll_q and poll_a and poll_b):
            return redir("/stories/new",
                         "Finish the poll — a question and both "
                         "choices — or leave all three blank.")
        story_poll = (poll_q, poll_a, poll_b)
    story_ama = 1 if (form.get("ama") or "") in ("1", "true", "on") else 0
    # --- music: YouTube only. Resolved BEFORE the media is saved, and
    # a song that can't be embedded fails the whole save (no story is
    # created) so the user picks a different upload of the song. ---
    try:
        music = _resolve_music(form)
    except ValueError as e:
        return redir("/stories/new", str(e))
    # --- media; a bad story file rejects the whole post ---
    _skey = (form.get("media_key") or "").strip()
    if _skey and storage.r2_enabled():
        # Browser-direct upload (Build U), same contract as posts.
        media, _kerr = _accept_media_key(user, _skey, "stories",
                                         STORY_EXTS)
        if _kerr:
            return redir("/stories/new", _kerr)
    else:
        try:
            media = save_upload(
                form.get("media"), "stories", STORY_EXTS,
                max_mb=(PLUS_UPLOAD_MB if _is_plus(user) else 0))
        except ValueError as e:
            return redir("/stories/new", str(e))
    # --- visual editor payload + audience + picker media ---------
    import json as _json
    edit = _clean_story_edit(form.get("edit_json") or "")
    audience = "public" if (form.get("audience") or "") == "public" \
        else "friends"
    if not media:
        _existing = (form.get("existing_media") or "").strip()
        if _existing and _own_media_path(user["id"], _existing):
            media = _existing
    if not media and edit.get("mode") != "text":
        return redir("/stories/new", "Choose a photo or short video.")
    media = media or ""
    mins = _story_lifetime_minutes(form)
    conn = db()
    cur = conn.execute(
        "INSERT INTO stories(user_id, media, caption, created_at, expires_at, "
        "overlay_text, overlay_color, overlay_pos, music_kind, music_ref, "
        "music_title, music_artist, music_thumb, music_start, music_len, "
        "music_volume, music_mute_orig, ama, edit_json, audience) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user["id"], media, caption, now_iso(), story_expires_at(mins),
         overlay_text, overlay_color, overlay_pos,
         "youtube" if music else None,
         music["video_id"] if music else "",
         music["title"] if music else "",
         music["artist"] if music else "",
         music["thumb"] if music else "",
         music_start, music_len, music_volume, music_mute_orig,
         story_ama, _json.dumps(edit), audience))
    sid = cur.lastrowid
    if story_poll:
        conn.execute(
            "INSERT INTO story_polls(story_id, question, a_text, b_text) "
            "VALUES(?,?,?,?)", (sid, story_poll[0], story_poll[1],
                                story_poll[2]))
    conn.commit()
    conn.close()
    for _mid in edit.get("mentions") or []:
        if _mid != user["id"] and get_user(_mid):
            notify(_mid, "story_mention",
                   f"{user['name']} mentioned you in their story",
                   f"/story/{sid}", actor_id=user["id"])
    _flag_parts = [caption, overlay_text, poll_q]
    _flag_parts += [tx["t"] for tx in edit.get("texts") or []]
    flag_item("story", sid, user["id"], user["name"],
              " ".join(x for x in _flag_parts if x))
    return redir("/home", _lifetime_flash(mins))


@app.get("/stories/queue")
async def stories_queue(request: Request):
    """JSON queue for the full-screen viewer (own + friends' stories)."""
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    return JSONResponse({"ok": True, "me": user["id"],
                         "groups": _story_queue(user["id"])})


@app.get("/story/{sid}", response_class=HTMLResponse)
async def story_view(request: Request, sid: int):
    user, err = require_login(request)
    if err:
        return err
    s = _get_story(sid)
    if not _can_view_story(user, s):
        return render(request, "post_gone.html",
                      {"message": "This story isn't available."})
    # Entering a story marks it viewed (same as the old viewer); the
    # full-screen viewer also posts /story/{id}/view as it advances.
    conn = db()
    conn.execute("INSERT OR IGNORE INTO story_views(story_id, user_id, viewed_at) "
                 "VALUES(?,?,?)", (sid, user["id"], now_iso()))
    conn.commit()
    conn.close()
    return render(request, "story.html", {"start_id": sid})


@app.post("/story/{sid}/view")
async def story_mark_view(request: Request, sid: int):
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    s = _get_story(sid)
    if not _can_view_story(user, s):
        return JSONResponse({"ok": False, "error": "not found"},
                            status_code=404)
    conn = db()
    conn.execute("INSERT OR IGNORE INTO story_views(story_id, user_id, viewed_at) "
                 "VALUES(?,?,?)", (sid, user["id"], now_iso()))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.get("/story/{sid}/viewers")
async def story_viewers(request: Request, sid: int):
    """Who viewed my story — the story's owner only."""
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    s = _get_story(sid)
    if not s or s["user_id"] != user["id"]:
        return JSONResponse({"ok": False, "error": "not found"},
                            status_code=404)
    conn = db()
    rows = conn.execute(
        "SELECT u.id, u.name, u.avatar, v.viewed_at FROM story_views v "
        "JOIN users u ON u.id = v.user_id "
        "WHERE v.story_id=? AND v.user_id != ? ORDER BY v.viewed_at ASC",
        (sid, s["user_id"])).fetchall()
    conn.close()
    return JSONResponse({"ok": True, "viewers": [
        {"id": r["id"], "name": r["name"], "avatar": avatar_url(r["avatar"]),
         "viewed_at": r["viewed_at"], "when": fancy_time(r["viewed_at"])}
        for r in rows]})


def _upload_exists(rel):
    """Existence of a stored upload in whichever backend holds it:
    disk, or (R2 mode) R2. In disk mode this is exactly isfile."""
    if not rel:
        return False
    if os.path.isfile(os.path.join(UPLOAD_DIR, str(rel))):
        return True
    if storage.r2_enabled():
        try:
            return storage.exists(rel)
        except storage.StorageError:
            return False
    return False


def _story_clip_for_replies(conn, s):
    """Uploads-relative path of the shared clip copy for story s —
    uploads/story_replies/<story_id>-<basename> — created on the FIRST
    story-linked message for the story and reused by every later one.
    The copy lives outside stories/ on purpose: prune_stories deletes
    the story row (and the original is the story's own file), but this
    snapshot belongs to the message thread and must survive the story
    expiring — Facebook keeps the clip in the inbox the same way.
    Returns '' when the story has no media, the file is missing, or
    the copy fails; the thread then renders the 📖 placeholder tile."""
    row = conn.execute(
        "SELECT story_media FROM messages WHERE story_id=? "
        "AND story_media != '' LIMIT 1", (s["id"],)).fetchone()
    if row and _upload_exists(row["story_media"]):
        return row["story_media"]
    src_rel = s["media"] or ""
    if not src_rel:
        return ""
    dest_rel = f"story_replies/{s['id']}-{os.path.basename(src_rel)}"
    if storage.r2_enabled():
        # Server-side copy inside the bucket; a pre-migration source
        # that only exists on disk falls through to the disk copy
        # below (the /uploads route serves either home).
        try:
            if storage.exists(src_rel):
                storage.copy_object(src_rel, dest_rel)
                return dest_rel
        except storage.StorageError:
            pass
    src = os.path.join(UPLOAD_DIR, src_rel)
    if not os.path.isfile(src):
        return ""
    dest = os.path.join(UPLOAD_DIR, dest_rel)
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(src, dest)
    except Exception:
        return ""
    return dest_rel


def _story_interact_gate(user, sid):
    """Shared gate for POST /story/{sid}/reply and /react. Returns
    ((story, owner), None) or (None, JSONResponse). Rules mirror the
    viewer + the message route: the story must exist; a pair blocked
    either way is refused with the message route's exact wording; the
    sender must be able to VIEW the story (owner + friends, unexpired —
    the queue's rule); and nobody replies/reacts to their own story."""
    s = _get_story(sid)
    if not s:
        return None, JSONResponse({"ok": False, "error": "not found"},
                                  status_code=404)
    if blocked_either_way(user["id"], s["user_id"]):
        return None, JSONResponse(
            {"ok": False, "error": "You can't message this person."},
            status_code=403)
    if s["user_id"] == user["id"]:
        return None, JSONResponse(
            {"ok": False, "error": "That's your own story."},
            status_code=400)
    if not _can_view_story(user, s):
        return None, JSONResponse({"ok": False, "error": "not found"},
                                  status_code=404)
    owner = get_user(s["user_id"])
    if not owner:
        return None, JSONResponse({"ok": False, "error": "not found"},
                                  status_code=404)
    return (s, owner), None


@app.post("/story/{sid}/reply")
async def story_reply(request: Request, sid: int):
    """Reply to someone's story: lands in their inbox as a normal
    message (same insert path, badge, bell and push as any DM) with
    the story clip attached. JSON {body} — body rules match the
    message route (trimmed; empty is refused)."""
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    gate, resp = _story_interact_gate(user, sid)
    if resp:
        return resp
    s, owner = gate
    try:
        data = await request.json()
    except Exception:
        data = {}
    body = str((data or {}).get("body") or "").strip()
    if not body:
        return JSONResponse({"ok": False,
                             "error": "Write something first."},
                            status_code=400)
    conn = db()
    clip = _story_clip_for_replies(conn, s)
    conn.close()
    mid = _deliver_message(user, owner["id"], body, story_id=sid,
                           story_media=clip)
    return JSONResponse({"ok": True, "id": mid})


@app.post("/story/{sid}/react")
async def story_react(request: Request, sid: int):
    """React to someone's story with one of the 6 standard reactions:
    lands in their inbox as a story-linked message whose body is the
    reaction emoji itself (the thread renders it as
    "{name} reacted to your story")."""
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    gate, resp = _story_interact_gate(user, sid)
    if resp:
        return resp
    s, owner = gate
    try:
        data = await request.json()
    except Exception:
        data = {}
    kind = str((data or {}).get("kind") or "").strip()
    if kind not in REACTION_EMOJI:
        return JSONResponse({"ok": False, "error": "Unknown reaction."},
                            status_code=400)
    conn = db()
    clip = _story_clip_for_replies(conn, s)
    conn.close()
    mid = _deliver_message(user, owner["id"], REACTION_EMOJI[kind],
                           story_id=sid, story_media=clip)
    return JSONResponse({"ok": True, "id": mid})


def _story_poll_json(conn, sid, viewer_id):
    """A story poll's tallies for the viewer: both counts, the total,
    whole-number percentages, and the viewer's own pick."""
    counts = {r["choice"]: r["c"] for r in conn.execute(
        "SELECT choice, COUNT(*) c FROM story_poll_votes "
        "WHERE story_id=? GROUP BY choice", (sid,)).fetchall()}
    a_votes, b_votes = counts.get("a", 0), counts.get("b", 0)
    total = a_votes + b_votes
    mine = conn.execute(
        "SELECT choice FROM story_poll_votes WHERE story_id=? "
        "AND user_id=?", (sid, viewer_id)).fetchone()
    return {"a_votes": a_votes, "b_votes": b_votes, "total": total,
            "a_pct": int(round(100.0 * a_votes / total)) if total else 0,
            "b_pct": int(round(100.0 * b_votes / total)) if total else 0,
            "my_choice": mine["choice"] if mine else None}


@app.post("/story/{sid}/poll/vote")
async def story_poll_vote(request: Request, sid: int):
    """Vote in a story's binary poll. JSON {choice: 'a'|'b'}. Gates
    are the story-interact gate exactly (blocked pair 403, own story
    400, non-friend / expired story 404 — the same rules the story
    viewer applies). One row per user; voting again CHANGES the pick."""
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    gate, resp = _story_interact_gate(user, sid)
    if resp:
        return resp
    try:
        data = await request.json()
    except Exception:
        data = {}
    choice = str((data or {}).get("choice") or "").strip()
    if choice not in ("a", "b"):
        return JSONResponse({"ok": False, "error": "Pick a side."},
                            status_code=400)
    conn = db()
    if not conn.execute("SELECT 1 FROM story_polls WHERE story_id=?",
                        (sid,)).fetchone():
        conn.close()
        return JSONResponse({"ok": False, "error": "No poll on this story."},
                            status_code=404)
    conn.execute(
        "INSERT INTO story_poll_votes(story_id, user_id, choice) "
        "VALUES(?,?,?) ON CONFLICT(story_id, user_id) DO UPDATE SET "
        "choice=excluded.choice", (sid, user["id"], choice))
    conn.commit()
    out = _story_poll_json(conn, sid, user["id"])
    conn.close()
    return JSONResponse({"ok": True, **out})


@app.post("/story/{sid}/ama")
async def story_ama(request: Request, sid: int):
    """Ask the story's author a question: rides the EXISTING story
    reply path (_deliver_message with the shared story clip) so it
    lands in the author's inbox thread as a story message — marked
    ama=1, which is what makes the thread/inbox label it
    "❓ Question about your story:". Gates are the story-reply gate
    exactly (_story_interact_gate: blocked/stranger/own-story rules
    identical); text rules mirror replies (trimmed, empty refused)
    plus the message composer's 2000-char cap. The story must carry
    the AMA box (stories.ama=1)."""
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    gate, resp = _story_interact_gate(user, sid)
    if resp:
        return resp
    s, owner = gate
    if not (s.get("ama") if isinstance(s, dict) else s["ama"]):
        return JSONResponse(
            {"ok": False, "error": "This story isn't taking questions."},
            status_code=400)
    try:
        data = await request.json()
    except Exception:
        data = {}
    body = str((data or {}).get("text") or (data or {}).get("body")
               or "").strip()
    if not body:
        return JSONResponse({"ok": False,
                             "error": "Write something first."},
                            status_code=400)
    if len(body) > 2000:
        return JSONResponse({"ok": False, "error": "That's too long."},
                            status_code=400)
    conn = db()
    clip = _story_clip_for_replies(conn, s)
    conn.close()
    mid = _deliver_message(user, owner["id"], body, story_id=sid,
                           story_media=clip, ama=1)
    return JSONResponse({"ok": True, "id": mid})


@app.post("/story/{sid}/delete")
async def story_delete(request: Request, sid: int):
    user, err = require_login(request)
    if err:
        return err
    s = _get_story(sid)
    owner_on = is_owner(request)
    if not s or (s["user_id"] != user["id"] and not owner_on):
        return redir("/home", "Story not found.")
    conn = db()
    conn.execute("DELETE FROM story_views WHERE story_id=?", (sid,))
    conn.execute("DELETE FROM story_polls WHERE story_id=?", (sid,))
    conn.execute("DELETE FROM story_poll_votes WHERE story_id=?", (sid,))
    conn.execute("DELETE FROM stories WHERE id=?", (sid,))
    conn.commit()
    conn.close()
    return redir("/home", "Story deleted.")

def _delete_post(pid):
    conn = db()
    conn.execute("DELETE FROM likes WHERE post_id=?", (pid,))
    conn.execute("DELETE FROM comments WHERE post_id=?", (pid,))
    conn.execute("DELETE FROM post_comment_blocks WHERE post_id=?", (pid,))
    conn.execute("DELETE FROM post_hidden_from WHERE post_id=?", (pid,))
    conn.execute("DELETE FROM post_tags WHERE post_id=?", (pid,))
    conn.execute("DELETE FROM moderation_flags WHERE item_type='post' AND item_id=?",
                 (pid,))
    # Shares pointing at this post die with it.
    share_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM posts WHERE shared_post_id=?", (pid,)).fetchall()]
    for sid in share_ids:
        conn.execute("DELETE FROM likes WHERE post_id=?", (sid,))
        conn.execute("DELETE FROM comments WHERE post_id=?", (sid,))
        conn.execute("DELETE FROM post_comment_blocks WHERE post_id=?", (sid,))
        conn.execute("DELETE FROM hidden_posts WHERE post_id=?", (sid,))
        conn.execute("DELETE FROM post_hidden_from WHERE post_id=?", (sid,))
        conn.execute("DELETE FROM post_tags WHERE post_id=?", (sid,))
    conn.execute("DELETE FROM posts WHERE shared_post_id=?", (pid,))
    conn.execute("DELETE FROM hidden_posts WHERE post_id=?", (pid,))
    # Build LOCKED CONTENT: purchase records die with the post (the
    # media file itself is left in the locked store, unreachable —
    # the same leave-the-file discipline as uploads above).
    conn.execute("DELETE FROM content_unlocks WHERE "
                 "content_type='post' AND content_id=?", (pid,))
    for sid in share_ids:
        conn.execute("DELETE FROM content_unlocks WHERE "
                     "content_type='post' AND content_id=?", (sid,))
    conn.execute("DELETE FROM posts WHERE id=?", (pid,))
    conn.commit()
    conn.close()


@app.post("/post/{pid}/delete")
async def delete_post(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    p = conn.execute("SELECT user_id, wall_user_id FROM posts WHERE id=?",
                     (pid,)).fetchone()
    conn.close()
    if not p:
        return back(request)
    # Build WALL: the wall owner may delete ANY post sitting on their
    # own wall, on top of the author + owner-context permissions.
    if p["user_id"] != user["id"] and p["wall_user_id"] != user["id"] \
            and not is_owner(request):
        return back(request, "/home")
    _delete_post(pid)
    return back(request, "/home")


@app.post("/wall/{pid}/approve")
async def wall_approve(request: Request, pid: int):
    """Build WALL: the wall owner approves a pending wall post — it
    becomes a normal visible wall post. Wall-owner (or owner context)
    only; anything else is a quiet no-op back."""
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    p = conn.execute("SELECT wall_user_id, wall_pending FROM posts "
                     "WHERE id=?", (pid,)).fetchone()
    if not p or not p["wall_pending"] \
            or (p["wall_user_id"] != user["id"]
                and not is_owner(request)):
        conn.close()
        return back(request, "/home")
    conn.execute("UPDATE posts SET wall_pending=0 WHERE id=?", (pid,))
    conn.commit()
    conn.close()
    return back(request, f"/user/{p['wall_user_id']}")


@app.post("/wall/{pid}/delete")
async def wall_delete_pending(request: Request, pid: int):
    """Build WALL: the wall owner deletes a pending wall post without
    approving it. Same gate as approve; approved wall posts are
    removed through the normal /post/{pid}/delete route instead."""
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    p = conn.execute("SELECT wall_user_id, wall_pending FROM posts "
                     "WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not p or not p["wall_pending"] \
            or (p["wall_user_id"] != user["id"]
                and not is_owner(request)):
        return back(request, "/home")
    _delete_post(pid)
    return back(request, f"/user/{p['wall_user_id']}")


# ---------------- share / hide / pin ----------------
def _root_post(pid):
    """Follow shared_post_id chain to the original post row (dict or None)."""
    conn = db()
    p = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    seen = set()
    while p and p["shared_post_id"] and p["id"] not in seen:
        seen.add(p["id"])
        p = conn.execute("SELECT * FROM posts WHERE id=?",
                         (p["shared_post_id"],)).fetchone()
    conn.close()
    return dict(p) if p else None


@app.post("/post/{pid}/share")
async def share_post(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    root = _root_post(pid)
    if not root or not can_view_post(user, root, is_owner(request)):
        return back(request, "/home")
    if (root.get("privacy") or "public") != "public":
        return back(request, "/home")
    conn = db()
    exists = conn.execute(
        "SELECT 1 FROM posts WHERE user_id=? AND shared_post_id=?",
        (user["id"], root["id"])).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO posts(user_id, kind, privacy, shared_post_id, created_at) "
            "VALUES(?,'text','public',?,?)",
            (user["id"], root["id"], now_iso()))
        conn.commit()
    conn.close()
    if not exists and root["user_id"] != user["id"]:
        notify(root["user_id"], "share",
               f"{user['name']} shared your post.", f"/post/{root['id']}",
               actor_id=user["id"])
    return back(request)


@app.post("/share/{sid}/remove")
async def unshare(request: Request, sid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    s = conn.execute(
        "SELECT * FROM posts WHERE id=? AND shared_post_id IS NOT NULL",
        (sid,)).fetchone()
    if s and (s["user_id"] == user["id"] or is_owner(request)):
        conn.execute("DELETE FROM hidden_posts WHERE post_id=?", (sid,))
        conn.execute("DELETE FROM posts WHERE id=?", (sid,))
        conn.commit()
    conn.close()
    return back(request)


@app.post("/post/{pid}/hide")
async def hide_post(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    p = conn.execute("SELECT id FROM posts WHERE id=?", (pid,)).fetchone()
    if p:
        conn.execute(
            "INSERT OR IGNORE INTO hidden_posts(user_id, post_id, created_at) "
            "VALUES(?,?,?)", (user["id"], pid, now_iso()))
        conn.commit()
    conn.close()
    return back(request)


@app.post("/post/{pid}/unhide")
async def unhide_post(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    conn.execute("DELETE FROM hidden_posts WHERE user_id=? AND post_id=?",
                 (user["id"], pid))
    conn.commit()
    conn.close()
    return back(request)


@app.get("/hidden", response_class=HTMLResponse)
async def hidden_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    owner_on = is_owner(request)
    conn = db()
    rows = conn.execute(
        "SELECT p.* FROM posts p JOIN hidden_posts h ON h.post_id=p.id "
        "WHERE h.user_id=? ORDER BY h.created_at DESC", (user["id"],)).fetchall()
    conn.close()
    posts = []
    for r in rows:
        card = post_card(dict(r), user, owner_on)
        if card:
            posts.append(card)
    return render(request, "hidden.html", {"posts": posts})


@app.post("/post/{pid}/pin")
async def pin_post(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    p = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    if p and p["user_id"] == user["id"] and not p["page_id"] \
            and not p["shared_post_id"]:
        conn.execute("UPDATE posts SET pinned=0 WHERE user_id=?", (user["id"],))
        conn.execute("UPDATE posts SET pinned=1 WHERE id=?", (pid,))
        conn.commit()
    conn.close()
    return back(request)


@app.post("/post/{pid}/unpin")
async def unpin_post(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    conn.execute("UPDATE posts SET pinned=0 WHERE id=? AND user_id=?",
                 (pid, user["id"]))
    conn.commit()
    conn.close()
    return back(request)


# ---------------- per-post audience (hide from specific people) ----------------
def _own_post_or_none(user, request, pid):
    """The post row when the caller may edit its audience (author or owner
    session), else None. Share rows have no audience of their own."""
    conn = db()
    p = conn.execute("SELECT * FROM posts WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not p or p["shared_post_id"]:
        return None
    if p["user_id"] != user["id"] and not is_owner(request):
        return None
    return dict(p)


@app.get("/post/{pid}/audience", response_class=HTMLResponse)
async def audience_page(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    p = _own_post_or_none(user, request, pid)
    if not p:
        return redir("/home", "Post not found.")
    hidden = hidden_from_ids(pid)
    author = get_user(p["user_id"]) or user
    friends, _ = composer_choices(author)
    for f in friends:
        f["hidden"] = f["id"] in hidden
    return render(request, "audience.html",
                  {"post_id": pid, "friends": friends,
                   "hidden_count": len(hidden)})


@app.post("/post/{pid}/audience")
async def audience_save(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    p = _own_post_or_none(user, request, pid)
    if not p:
        return redir("/home", "Post not found.")
    form = await request.form()
    # Only the author's friends can be on the hide-from list.
    hide_ids = _form_ids(form, "hide_from") & friend_id_set(p["user_id"])
    conn = db()
    conn.execute("DELETE FROM post_hidden_from WHERE post_id=?", (pid,))
    for hid in hide_ids:
        conn.execute(
            "INSERT OR IGNORE INTO post_hidden_from(post_id, user_id, "
            "created_at) VALUES(?,?,?)", (pid, hid, now_iso()))
    conn.commit()
    conn.close()
    # Return to the page the form was submitted from (feed, wall, or the
    # post page itself) when it's one of ours; else the post page.
    dest = f"/post/{pid}"
    ref = request.headers.get("referer", "")
    if ref:
        try:
            pr = urlparse(ref)
            if pr.path.startswith("/") and (
                    not pr.netloc
                    or pr.netloc == request.headers.get("host", "")):
                dest = pr.path
        except Exception:
            pass
    return redir(dest, "Audience updated.")


# ---------------- "Your Spot" (per-profile name + theme) ----------------
SPOT_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
SPOT_NAME_MAX = 60
SPOT_OPACITY_MIN, SPOT_OPACITY_MAX = 30, 100
BOX_OPACITY_MIN, BOX_OPACITY_MAX = 0, 100
# Theme defaults a spot falls back to (matches the glass look in base.html).
SPOT_DEFAULT_TEXT = "#1c1e21"
SPOT_DEFAULT_CARD_RGB = (232, 232, 238)
SPOT_DEFAULT_ALPHA = "0.55"


def _spot_hex_ok(v):
    return bool(v) and bool(SPOT_HEX_RE.match(str(v).strip()))


def _hex_to_rgb(h):
    h = str(h).strip().lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _spot_values(u):
    """(text, rgb, alpha) chosen by user u — each None when unset/invalid."""
    text = (u.get("spot_text_color") or "").strip()
    text = text.lower() if _spot_hex_ok(text) else None
    card = (u.get("spot_card_color") or "").strip()
    rgb = _hex_to_rgb(card) if _spot_hex_ok(card) else None
    alpha = None
    op = u.get("spot_card_opacity")
    if op is not None:
        try:
            op = max(SPOT_OPACITY_MIN, min(SPOT_OPACITY_MAX, int(op)))
        except (TypeError, ValueError):
            op = None
        if op is not None:
            alpha = f"{op / 100:.2f}".rstrip("0").rstrip(".")
    return text, rgb, alpha


def spot_style_for(u):
    """CSS vars + scope classes for the profile owner's "Your Spot" look.

    Returns (style_attr, classes). Only SET values are emitted; with
    nothing set both are empty and the profile renders exactly as the
    default theme. The vars are consumed by the .spot-scope rules in
    base.html (profile pages) and, via viewer_spot_for(), by the
    body-level rules that let a viewer's own spot follow them around
    the rest of the site.
    """
    text, rgb, alpha = _spot_values(u)
    parts, classes = [], []
    if text:
        parts.append(f"--spot-text:{text}")
        classes.append("spot-has-text")
    if rgb:
        parts.append(f"--spot-card-rgb:{rgb[0]},{rgb[1]},{rgb[2]}")
    if alpha is not None:
        parts.append(f"--spot-card-alpha:{alpha}")
    if rgb or alpha is not None:
        classes.append("spot-has-card")
    return ";".join(parts), " ".join(classes)


def spot_scope_style_for(u):
    """All three spot vars for the /user/{id} .spot-scope, ALWAYS emitted.

    The owner's chosen values where set, the theme defaults everywhere
    else. Because every var is defined inline on the scope, a viewer's
    body-level spot vars can never cascade into somebody else's profile:
    an unset profile renders with exactly the default values.
    """
    text, rgb, alpha = _spot_values(u)
    if not text:
        text = SPOT_DEFAULT_TEXT
    if not rgb:
        rgb = SPOT_DEFAULT_CARD_RGB
    if alpha is None:
        alpha = SPOT_DEFAULT_ALPHA
    return (f"--spot-text:{text};--spot-card-rgb:{rgb[0]},{rgb[1]},{rgb[2]}"
            f";--spot-card-alpha:{alpha}")


def viewer_spot_for(u):
    """A logged-in viewer's own spot as (style_attr, body classes).

    Same vars as spot_style_for(), but the marker classes are the
    body-level viewer-spot-text / viewer-spot-card pair that base.html
    roots its site-wide rules at. Empty strings when nothing is set.
    """
    style, classes = spot_style_for(u)
    classes = (classes.replace("spot-has-text", "viewer-spot-text")
                      .replace("spot-has-card", "viewer-spot-card"))
    return style, classes


def viewer_box_alpha_for(u):
    """The viewer's own Box Transparency as a CSS alpha string.

    users.box_opacity is a solidity percent (0-100). Returns "" when
    the viewer is at (or defaults to) 100 — fully solid, the default
    look with no glass overrides at all — otherwise the alpha as a
    compact decimal string (55 -> "0.55", 0 -> "0").
    """
    if not u:
        return ""
    op = _uget(u, "box_opacity")
    if op is None:
        op = 100
    try:
        op = int(op)
    except (TypeError, ValueError):
        op = 100
    op = max(BOX_OPACITY_MIN, min(BOX_OPACITY_MAX, op))
    if op >= 100:
        return ""
    return "%g" % (op / 100.0)


# --- Build GLASS: the ONE transparency preference -------------------
# The slider in Settings ("Glass transparency") stores into the same
# box_opacity column Box Transparency always used; glass_set records
# that the value was CHOSEN. The rendered --glass-alpha var drives the
# fills of the main surfaces site-wide (base.html) and multiplies the
# Your Spot bubble tint, unifying both older systems:
#   never saved (or logged out) -> the default clean glass, 0.82
#   saved < 100                 -> that value (same as --box-alpha)
#   saved 100                   -> "1": solid, by the viewer's choice
GLASS_DEFAULT_SOLIDITY = 82
GLASS_DEFAULT_ALPHA = "0.82"


def _saved_glass_alpha_for(u):
    """The viewer's chosen Glass solidity as an alpha string: the
    saved box_opacity once glass_set (0 and 100 included — completely
    clear and solid-by-choice are both real choices), else the house
    default clean glass."""
    if not u or not _uget(u, "glass_set"):
        return GLASS_DEFAULT_ALPHA
    a = viewer_box_alpha_for(u)
    return a if a else "1"


def glass_alpha_for(u):
    """The viewer's CARD-family alpha string for --glass-alpha.

    Build CLEAR scheme semantics:
    glass -> the saved Glass solidity (the slider's own scheme);
    solid -> "1"; mix -> "1" too, because Mix cards are solid slabs
    (Mix's glass lives on the small surfaces; see
    glass_alpha_soft_for)."""
    if not u:
        return GLASS_DEFAULT_ALPHA
    scheme = look_scheme_for(u)
    if scheme == "glass":
        return _saved_glass_alpha_for(u)
    return "1"


def glass_alpha_soft_for(u):
    """The viewer's SMALL-surface alpha for --glass-alpha-soft
    (post/comment/message bubbles, chips, nav pills).

    glass -> the same saved solidity as the cards; solid -> "1";
    mix -> the saved Glass solidity, so Mix bubbles borrow the
    viewer's Glass transparency while their cards stay solid."""
    if not u:
        return GLASS_DEFAULT_ALPHA
    scheme = look_scheme_for(u)
    if scheme == "solid":
        return "1"
    if scheme == "glass":
        return _saved_glass_alpha_for(u)
    return _saved_glass_alpha_for(u)



# --- Build ICON STYLES: one custom vector icon set, seven finishes --
# Ken's order (Oct 27): the iPhone-emoji icons are "mediocre" — every
# chrome icon is redrawn once as clean vector art and finished per
# viewer: emoji (DEFAULT — the classic look, byte-identical output),
# solid, glass, jelly, chrome, gold, transparent. The finish is pure
# CSS (base.html paints body.icons-<style> .hic from one gradient
# sprite); the art never changes. Storage: users.icon_style.
ICON_STYLES = ("emoji", "solid", "glass", "jelly", "chrome", "gold",
               "transparent")
ICON_STYLE_LABELS = {
    "emoji": "Emoji — the classic HERE look (default)",
    "solid": "Solid — flat, bold, one clean color",
    "glass": "Glass — frosted see-through, bright edge",
    "jelly": "Jelly — glossy candy red",
    "chrome": "Chrome — liquid silver metal",
    "gold": "Gold — polished gold metal",
    "transparent": "Transparent — outline only, pure see-through",
}
ICON_EMOJI = {
    "home": "🏠", "peeps": "🎬", "friends": "👥", "market": "🛒",
    "alerts": "🔔", "menu": "☰", "create": "✚", "lives": "🔴",
    "messages": "💬", "comment": "💬", "search": "🔍", "post": "✍️",
    "story": "📸", "camera": "📸", "games": "🎮", "tickets": "🎟️",
    "discover": "🧭", "daily": "🔥", "contest": "🏆", "gift": "🎁",
    "like": "👍", "love": "❤️", "laugh": "😂", "wow": "😮",
    "sad": "😢", "angry": "😡", "share": "↗", "pin": "📌",
    "hide": "🙈", "eye": "👁", "delete": "🗑", "verified": "★",
    "bolt": "⚡", "status": "⚡", "sliders": "🎛", "music": "🎵",
    "poll": "📊", "lock": "🔒", "invite": "💌", "page": "📄",
    "addfriend": "➕", "app": "📱", "logout": "🚪", "studio": "🎬",
    "mic": "🎤", "flip": "🔄", "gear": "⚙️", "swords": "⚔️",
    "die": "🎲", "speaker": "🔊", "mute": "🔇", "phone": "📞",
    "crown": "👑", "moneybag": "💰", "minus": "➖", "check": "✓",
    "x": "✕", "clock": "🕐", "send": "📤", "block": "🚫",
    "back": "←", "dots": "⋯", "play": "▶", "profile": "👤",
    "glass": "🪟", "expand": "⤢", "link": "🔗", "repost": "🔁",
    "unhide": "↩", "smile": "😊", "note": "📝", "star2": "⭐",
    "medal": "🥇", "palette": "🎨", "photo": "🖼️", "tap": "🖐️",
    "hourglass": "⏳", "stop": "🛑", "shield": "🛡", "mirror": "🪞",
    "ask": "🙋", "inbox": "📥", "wave": "👋", "growth": "📈",
    "tag": "🏷️", "mega": "📣", "unlock": "🔓",
}
# The custom art: 24x24 fill shapes (themed by CSS), matching the
# specimen board Ken approved on Oct 27. `.stk` sub-shapes are
# stroke-only details (broadcast arcs, mic cradle, speaker waves).
ICON_ART = {
    "home": '<path d="M12 3l9 8h-3v9h-4.2v-6H10.2v6H6v-9H3z"/>',
    "peeps": '<path fill-rule="evenodd" d="M4 5h16a2 2 0 012 2v10a2 2 0 01-2 2H4a2 2 0 01-2-2V7a2 2 0 012-2zm6.2 4.6v4.8L14.6 12z"/>',
    "friends": '<path d="M9 10.6A3.6 3.6 0 1 0 9 3.4a3.6 3.6 0 0 0 0 7.2zM2.8 20.2c.6-3.9 3-6 6.2-6s5.6 2.1 6.2 6H2.8zM16.9 10.3a3.2 3.2 0 1 0-1.6-6.1 4.4 4.4 0 0 1 0 5.4c.5.2 1 .4 1.6.7zM17.5 14.4c2.2.7 3.6 2.6 4 5.8h-4.9c-.2-2.4-.9-4.4-2.1-5.9.9.1 2 .1 3 .1z"/>',
    "market": '<path d="M2.5 4h2.2l2.3 11.6A2.2 2.2 0 009.2 17.5h7.9a2.2 2.2 0 002.1-1.7L21 7.5H6.2l-.5-2.4A1.2 1.2 0 004.5 4.2H2.5V4zm6.7 18a2.1 2.1 0 1 0 0-4.2 2.1 2.1 0 0 0 0 4.2zm8.6 0a2.1 2.1 0 1 0 0-4.2 2.1 2.1 0 0 0 0 4.2z"/>',
    "alerts": '<path d="M12 3a6.2 6.2 0 00-6.2 6.2v3.6L3.6 17h16.8L18.2 12.8V9.2A6.2 6.2 0 0012 3zM9.8 19a2.3 2.3 0 004.4 0H9.8z"/>',
    "menu": '<rect x="4" y="5.6" width="16" height="2.6" rx="1.3"/><rect x="4" y="10.7" width="16" height="2.6" rx="1.3"/><rect x="4" y="15.8" width="16" height="2.6" rx="1.3"/>',
    "create": '<path d="M10.7 4.5h2.6v6.2h6.2v2.6h-6.2v6.2h-2.6v-6.2H4.5v-2.6h6.2z"/>',
    "lives": '<circle cx="12" cy="12" r="2.7"/><path class="stk" d="M7.5 7.5a6.4 6.4 0 000 9M16.5 7.5a6.4 6.4 0 010 9M5 5a10 10 0 000 14M19 5a10 10 0 010 14"/>',
    "messages": '<path d="M5 3.5h14A2.5 2.5 0 0121.5 6v8a2.5 2.5 0 01-2.5 2.5H9.5L5 20.5v-4A2.5 2.5 0 012.5 14V6A2.5 2.5 0 015 3.5z"/>',
    "comment": '<path fill-rule="evenodd" d="M5 3.5h14A2.5 2.5 0 0121.5 6v8a2.5 2.5 0 01-2.5 2.5H9.5L5 20.5v-4A2.5 2.5 0 012.5 14V6A2.5 2.5 0 015 3.5zM6.5 8h11V6.4h-11V8zm0 3.6h7.5V10H6.5v1.6z"/>',
    "search": '<path fill-rule="evenodd" d="M10.8 3.5a7.3 7.3 0 104.9 13.4l4.6 4.6 1.8-1.8-4.6-4.6A7.3 7.3 0 0010.8 3.5zm0 2.6a4.7 4.7 0 110 9.4 4.7 4.7 0 010-9.4z"/>',
    "post": '<path d="M3.6 20.4l1.1-4.1L16 5a1.9 1.9 0 012.7 0l.3.3a1.9 1.9 0 010 2.7L7.7 19.3l-4.1 1.1zM17.6 6.2l1.2 1.2.8-.8a1.9 1.9 0 00-2.7-2.7l-.8.8 1.5 1.5z"/>',
    "story": '<path fill-rule="evenodd" d="M4.5 7h2.8L9.4 4.5h5.2L16.7 7h2.8A1.5 1.5 0 0121 8.5v10A1.5 1.5 0 0119.5 20h-15A1.5 1.5 0 013 18.5v-10A1.5 1.5 0 014.5 7zM12 17.2a3.7 3.7 0 1 0 0-7.4 3.7 3.7 0 0 0 0 7.4z"/>',
    "camera": '<path fill-rule="evenodd" d="M4.5 7h2.8L9.4 4.5h5.2L16.7 7h2.8A1.5 1.5 0 0121 8.5v10A1.5 1.5 0 0119.5 20h-15A1.5 1.5 0 013 18.5v-10A1.5 1.5 0 014.5 7zM12 17.2a3.7 3.7 0 1 0 0-7.4 3.7 3.7 0 0 0 0 7.4z"/>',
    "games": '<path fill-rule="evenodd" d="M7.2 7.5h9.6A5.2 5.2 0 0122 12.7v1.6a3.1 3.1 0 01-5.5 1.9L15 14.4H9l-1.5 1.8A3.1 3.1 0 012 14.3v-1.6A5.2 5.2 0 017.2 7.5zM8 10.2H6.4v1.7H4.7v1.7h1.7v1.7H8v-1.7h1.7v-1.7H8v-1.7zm8.9 1a1.15 1.15 0 100-2.3 1.15 1.15 0 000 2.3zm2.3 2.3a1.15 1.15 0 100-2.3 1.15 1.15 0 000 2.3z"/>',
    "tickets": '<path fill-rule="evenodd" d="M4 6.5h16A1.5 1.5 0 0121.5 8v2.2a2.8 2.8 0 000 5.6V18a1.5 1.5 0 01-1.5 1.5H4A1.5 1.5 0 012.5 18v-2.2a2.8 2.8 0 000-5.6V8A1.5 1.5 0 014 6.5zm10.5 2.2v8.6h1.8V8.7h-1.8z"/>',
    "discover": '<path fill-rule="evenodd" d="M12 2.5a9.5 9.5 0 100 19 9.5 9.5 0 000-19zm0 2.4a7.1 7.1 0 110 14.2 7.1 7.1 0 010-14.2z"/><path d="M15.8 8.2l-2.1 5.4-5.5 2.2 2.2-5.5z"/>',
    "daily": '<path d="M13.6 1.8c.5 2.9-.3 5-1.9 7.2C10.2 11.1 10.9 13 12 14.7c-1.3-.4-2.2-1.3-2.7-2.7-1.3 1.6-2 3.2-2 5.1A5.2 5.2 0 0012.5 22 5.2 5.2 0 0017.7 16.8c0-2-.8-4.1-2-5.7-.4 1.3-1.2 2.1-2.4 2.5.5-3.8.2-8.3.3-11.8z"/>',
    "contest": '<path d="M6.5 3.5h11V9a5.5 5.5 0 01-11 0V3.5zM6.5 5H4a3.5 3.5 0 003.6 4.9A5.4 5.4 0 016.5 7V5zm11 0h2.5v2a5.4 5.4 0 01-1.1 2.9A3.5 3.5 0 0020 6V5zM11 14.6h2v2.9h3.2v2.6H7.8v-2.6H11v-2.9z"/>',
    "gift": '<path d="M3.8 10h16.4v10.2H3.8zM2.8 6.2A1.7 1.7 0 014.5 4.5h3.7a2.8 2.8 0 012.8 2.8V10H4.5a1.7 1.7 0 01-1.7-1.7V6.2zm18.4 0v2.1a1.7 1.7 0 01-1.7 1.7H13V7.3a2.8 2.8 0 012.8-2.8h3.7a1.7 1.7 0 011.7 1.7zM11 10h2v10.2h-2z"/>',
    "like": '<path d="M6.8 21.5v-11H3.2v11h3.6zM20.4 10.2H14l.9-4.2c.2-1.9-1-3.2-2.6-3.5L6.8 10.6v10.9h9.6a2.3 2.3 0 002.2-1.8l1.3-6.9a2.3 2.3 0 00-2.3-2.6h2.8z"/>',
    "love": '<path d="M12 21.35l-1.45-1.32C5.4 15.36 2 12.28 2 8.5 2 5.42 4.42 3 7.5 3c1.74 0 3.41.81 4.5 2.09C13.09 3.81 14.76 3 16.5 3 19.58 3 22 5.42 22 8.5c0 3.78-3.4 6.86-8.55 11.54L12 21.35z"/>',
    "laugh": '<path fill-rule="evenodd" d="M12 2.5a9.5 9.5 0 100 19 9.5 9.5 0 000-19zM8.2 9.2a1.5 1.5 0 110 3 1.5 1.5 0 010-3zm7.6 0a1.5 1.5 0 110 3 1.5 1.5 0 010-3zM7 13.6h10c-.6 3-2.5 4.7-5 4.7s-4.4-1.7-5-4.7z"/>',
    "wow": '<path fill-rule="evenodd" d="M12 2.5a9.5 9.5 0 100 19 9.5 9.5 0 000-19zM8.4 8.6a1.7 1.7 0 110 3.4 1.7 1.7 0 010-3.4zm7.2 0a1.7 1.7 0 110 3.4 1.7 1.7 0 010-3.4zM12 14.2a2.2 2.2 0 110 4.4 2.2 2.2 0 010-4.4z"/>',
    "sad": '<path fill-rule="evenodd" d="M12 2.5a9.5 9.5 0 100 19 9.5 9.5 0 000-19zM8.2 9.2a1.5 1.5 0 110 3 1.5 1.5 0 010-3zm7.6 0a1.5 1.5 0 110 3 1.5 1.5 0 010-3z"/><path class="stk" d="M8 17.6a5.4 5.4 0 018 0"/>',
    "angry": '<path fill-rule="evenodd" d="M12 2.5a9.5 9.5 0 100 19 9.5 9.5 0 000-19zM6.7 8.6l3.9 1.1-.4 1.5-3.9-1.1zm10.6 0l.4 1.5-3.9 1.1-.4-1.5zM8.4 12.4a1.4 1.4 0 110 2.8 1.4 1.4 0 010-2.8zm7.2 0a1.4 1.4 0 110 2.8 1.4 1.4 0 010-2.8z"/><path class="stk" d="M8.2 18.4a5.2 5.2 0 017.6 0"/>',
    "share": '<circle cx="6" cy="12" r="2.6"/><circle cx="17.5" cy="5.5" r="2.6"/><circle cx="17.5" cy="18.5" r="2.6"/><path d="M8.3 10.8l6.9-4 .9 1.6-6.9 4zM8.3 13.2l6.9 4-.9 1.6-6.9-4z"/>',
    "pin": '<path fill-rule="evenodd" d="M12 2.2a7.3 7.3 0 00-7.3 7.3c0 5.4 7.3 12.3 7.3 12.3s7.3-6.9 7.3-12.3A7.3 7.3 0 0012 2.2zm0 10a2.7 2.7 0 110-5.4 2.7 2.7 0 010 5.4z"/>',
    "hide": '<path fill-rule="evenodd" d="M12 5.2c4.9 0 9.1 2.9 10.9 6.8C21.1 15.9 16.9 18.8 12 18.8S2.9 15.9 1.1 12C2.9 8.1 7.1 5.2 12 5.2zm0 2.3c-3.7 0-7 1.8-8.7 4.5 1.7 2.7 5 4.5 8.7 4.5s7-1.8 8.7-4.5c-1.7-2.7-5-4.5-8.7-4.5zm0 1.3a3.2 3.2 0 110 6.4 3.2 3.2 0 010-6.4z"/><path d="M4.2 3.4L21 20.2l-1.5 1.5L2.7 4.9z"/>',
    "eye": '<path fill-rule="evenodd" d="M12 5.2c4.9 0 9.1 2.9 10.9 6.8C21.1 15.9 16.9 18.8 12 18.8S2.9 15.9 1.1 12C2.9 8.1 7.1 5.2 12 5.2zm0 2.3c-3.7 0-7 1.8-8.7 4.5 1.7 2.7 5 4.5 8.7 4.5s7-1.8 8.7-4.5c-1.7-2.7-5-4.5-8.7-4.5zm0 1.3a3.2 3.2 0 110 6.4 3.2 3.2 0 010-6.4z"/>',
    "delete": '<path d="M9.2 3h5.6l.9 2h4.6v2.6H3.7V5h4.6l.9-2zM5.7 9.2h12.6l-.9 11.3a1.6 1.6 0 01-1.6 1.5H8.2a1.6 1.6 0 01-1.6-1.5L5.7 9.2z"/>',
    "verified": '<path d="M12 2.4l2.95 6.35 6.95.8-5.15 4.8 1.35 6.85L12 17.85l-6.1 3.35 1.35-6.85-5.15-4.8 6.95-.8z"/>',
    "bolt": '<path d="M13.2 2L3.6 14.2h6.7L8.9 22l9.6-12.2h-6.7L13.2 2z"/>',
    "status": '<rect x="3.5" y="7" width="17" height="2.3" rx="1.15"/><rect x="3.5" y="15" width="17" height="2.3" rx="1.15"/><circle cx="9" cy="8.15" r="2.9"/><circle cx="15.5" cy="16.15" r="2.9"/>',
    "sliders": '<rect x="3.5" y="7" width="17" height="2.3" rx="1.15"/><rect x="3.5" y="15" width="17" height="2.3" rx="1.15"/><circle cx="9" cy="8.15" r="2.9"/><circle cx="15.5" cy="16.15" r="2.9"/>',
    "music": '<circle cx="6.8" cy="17.8" r="3.1"/><circle cx="17.9" cy="15.8" r="3.1"/><path d="M9.4 17.8V5.4L20.5 3.6v12.2h-2.6V6.5L12 8.1v9.7H9.4z"/>',
    "poll": '<path d="M3.5 12.5h4.4V21H3.5zM9.8 7.8h4.4V21H9.8zM16.1 3.2h4.4V21h-4.4z"/>',
    "lock": '<path d="M6.8 10.2V8a5.2 5.2 0 0110.4 0v2.2h.8a1.8 1.8 0 011.8 1.8v8.2a1.8 1.8 0 01-1.8 1.8H6a1.8 1.8 0 01-1.8-1.8V12A1.8 1.8 0 016 10.2h.8zm2.3 0h5.8V8a2.9 2.9 0 00-5.8 0v2.2zM12 14a2.1 2.1 0 011 3.9v1.6h-2.2v-1.6A2.1 2.1 0 0112 14z"/>',
    "invite": '<path fill-rule="evenodd" d="M3.5 5A1.8 1.8 0 015.3 3.2h13.4A1.8 1.8 0 0120.5 5v14a1.8 1.8 0 01-1.8 1.8H5.3A1.8 1.8 0 013.5 19V5zm2.3.6l6.2 4.9 6.2-4.9H5.8zM19 8.2l-7 5.5-7-5.5v13.3h14V8.2z"/>',
    "page": '<path fill-rule="evenodd" d="M6 2.5h7.5L19 8v12a1.5 1.5 0 01-1.5 1.5h-11A1.5 1.5 0 015 20V4A1.5 1.5 0 016 2.5zm6.5 1.7V9H17.3L12.5 4.2z"/>',
    "addfriend": '<path d="M10 11.2A3.8 3.8 0 1 0 10 3.6a3.8 3.8 0 0 0 0 7.6zM3.4 20.6c.7-4 3.2-6.2 6.6-6.2 1.5 0 2.9.5 4 1.4-.6 1.3-1 2.9-1.1 4.8H3.4zM19 12.2h2.4v2.3H19v2.4h-2.3v-2.4h-2.4v-2.3h2.4V9.8H19v2.4z"/>',
    "app": '<path fill-rule="evenodd" d="M8 2.2h8A1.8 1.8 0 0117.8 4v16A1.8 1.8 0 0116 21.8H8A1.8 1.8 0 016.2 20V4A1.8 1.8 0 018 2.2zm4 17.3a1.4 1.4 0 100-2.8 1.4 1.4 0 000 2.8z"/>',
    "logout": '<path d="M4.5 3h9.5v2.6H7.2v12.8h6.8V21H4.5V3zM16.8 8.2l4 3.8-4 3.8v-2.7h-6v-2.2h6V8.2z"/>',
    "studio": '<rect x="5" y="4" width="2.4" height="16" rx="1.2"/><rect x="10.8" y="4" width="2.4" height="16" rx="1.2"/><rect x="16.6" y="4" width="2.4" height="16" rx="1.2"/><rect x="3.4" y="8" width="5.6" height="3.4" rx="1.7"/><rect x="9.2" y="13.5" width="5.6" height="3.4" rx="1.7"/><rect x="15" y="6.5" width="5.6" height="3.4" rx="1.7"/>',
    "mic": '<path d="M12 2.5a3 3 0 013 3v5a3 3 0 01-6 0v-5a3 3 0 013-3z"/><path class="stk" d="M5.8 11.2a6.2 6.2 0 0012.4 0M12 17.6V21M8.6 21h6.8"/>',
    "flip": '<path d="M12.5 3.5A8.5 8.5 0 0120.6 10l1.7-1.7v5.2h-5.2l2-2A6.5 6.5 0 0012.5 5.5v-2zM11.5 20.5A8.5 8.5 0 013.4 14l-1.7 1.7V10.5h5.2l-2 2a6.5 6.5 0 006.6 6v2z"/>',
    "gear": '<path fill-rule="evenodd" d="M10.2 2.6h3.6l.6 2.9a8.4 8.4 0 012.5 1.4l2.7-1.3 1.8 3.1-2.4 1.9a8.6 8.6 0 010 2.9l2.4 1.9-1.8 3.1-2.7-1.3a8.4 8.4 0 01-2.5 1.4l-.6 2.9h-3.6L9.6 18.7a8.4 8.4 0 01-2.5-1.4l-2.7 1.3-1.8-3.1L5 13.6a8.6 8.6 0 010-2.9L2.6 8.8l1.8-3.1 2.7 1.3a8.4 8.4 0 012.5-1.4l.6-3zM12 15.4a3.4 3.4 0 100-6.8 3.4 3.4 0 000 6.8z"/>',
    "swords": '<path d="M4.1 3.2l12.7 12.7-2 2L2.1 5.2zM19.9 3.2L7.2 15.9l2 2L21.9 5.2z"/><circle cx="12" cy="12" r="1.7"/><path d="M15.6 16.4l3.2-3.2 1.7 1.7-3.2 3.2zM8.4 16.4L5.2 13.2l-1.7 1.7 3.2 3.2z"/>',
    "die": '<path fill-rule="evenodd" d="M5 3.5h14A1.5 1.5 0 0120.5 5v14a1.5 1.5 0 01-1.5 1.5H5A1.5 1.5 0 013.5 19V5A1.5 1.5 0 015 3.5zM5.6 7.5a1.9 1.9 0 103.8 0 1.9 1.9 0 10-3.8 0zm8.9 0a1.9 1.9 0 103.8 0 1.9 1.9 0 10-3.8 0zM10.1 12a1.9 1.9 0 103.8 0 1.9 1.9 0 10-3.8 0zM5.6 16.5a1.9 1.9 0 103.8 0 1.9 1.9 0 10-3.8 0zm8.9 0a1.9 1.9 0 103.8 0 1.9 1.9 0 10-3.8 0z"/>',
    "speaker": '<path d="M3.5 9.5v5h3.5L12 19V5L7 9.5H3.5z"/><path class="stk" d="M15 9.2a4 4 0 010 5.6M17.6 6.8a7.6 7.6 0 010 10.4"/>',
    "mute": '<path d="M3.5 9.5v5h3.5L12 19V5L7 9.5H3.5zM15.3 9.6l5.1 5.1-1.4 1.4-5.1-5.1-5.1 5.1-1.4-1.4 5.1-5.1-5.1-5.1 1.4-1.4 5.1 5.1 5.1-5.1 1.4 1.4-5.1 5.1z" transform="translate(2.5 0) scale(.88)"/>',
    "phone": '<path d="M6.6 10.8a15.9 15.9 0 006.6 6.6l2.2-2.2a1 1 0 011-.25 11.4 11.4 0 003.6.6 1 1 0 011 1V20a1 1 0 01-1 1A17 17 0 013 4a1 1 0 011-1h3.5a1 1 0 011 1c0 1.3.2 2.4.6 3.6a1 1 0 01-.25 1l-2.25 2.2z"/>',
    "crown": '<path d="M3 18.5h18v2H3zM2.5 7l4.8 3.4L12 5.5l4.7 4.9L21.5 7l-1.8 9.5H4.3L2.5 7z"/>',
    "moneybag": '<path d="M9.2 2.8h5.6l-1.3 3.1H10.5L9.2 2.8zM12 7.3c3.9 0 6.8 2.9 6.8 6.6 0 4-3 7.3-6.8 7.3s-6.8-3.3-6.8-7.3c0-3.7 2.9-6.6 6.8-6.6z"/>',
    "minus": '<rect x="5" y="10.8" width="14" height="2.5" rx="1.25"/>',
    "check": '<path d="M9.6 16.9L4.9 12.2 3.4 13.7l6.2 6.2L20.9 8.6 19.4 7.1z"/>',
    "x": '<path d="M6 4.7l6 6 6-6 1.9 1.9-6 6 6 6-1.9 1.9-6-6-6 6-1.9-1.9 6-6-6-6z"/>',
    "clock": '<path fill-rule="evenodd" d="M12 2.5a9.5 9.5 0 100 19 9.5 9.5 0 000-19zm0 2.4a7.1 7.1 0 110 14.2 7.1 7.1 0 010-14.2z"/><path class="stk" d="M12 8v4.4l3 1.8"/>',
    "send": '<path d="M21.8 2.2L2.4 10.6l7 3 2.4 7.4 3.3-5.4 5.2 3.6 1.5-17zM10.9 12.4l7.9-5.6-5.9 10.4-2-4.8z"/>',
    "block": '<path fill-rule="evenodd" d="M12 2.5a9.5 9.5 0 100 19 9.5 9.5 0 000-19zm0 2.4a7.1 7.1 0 014.6 12.4L5.3 6A7.1 7.1 0 0112 4.9zm0 14.7a7.1 7.1 0 01-4.6-12.4L18.7 18A7.1 7.1 0 0112 19.6z"/>',
    "back": '<path d="M20 11H7.8l4.6-4.6L11 5l-7 7 7 7 1.4-1.4L7.8 13H20z"/>',
    "dots": '<circle cx="5.5" cy="12" r="2"/><circle cx="12" cy="12" r="2"/><circle cx="18.5" cy="12" r="2"/>',
    "play": '<path d="M8.5 5.5v13l11-6.5z"/>',
    "profile": '<path d="M12 11.6a4.3 4.3 0 1 0 0-8.6 4.3 4.3 0 0 0 0 8.6zM4 21.2c.9-4.4 4-6.7 8-6.7s7.1 2.3 8 6.7H4z"/>',
    "glass": '<path fill-rule="evenodd" d="M5 3.5h14A1.5 1.5 0 0120.5 5v14a1.5 1.5 0 01-1.5 1.5H5A1.5 1.5 0 013.5 19V5A1.5 1.5 0 015 3.5zm.2 1.7v13.6h13.6V5.2H5.2z"/><path d="M6.8 15.8l6.8-8.4h2.9l-6.8 8.4H6.8zm4.6 2.2l4.2-5.1h2.7L14.1 18h-2.7z"/>',
    "expand": '<path d="M9.2 3.8H4v5.2h2V6.6l3.2 3.2 1.5-1.5L7.5 5.1h1.7V3.8zM20 9V3.8h-5.2v1.3h2.4l-3.2 3.2 1.5 1.5L18.7 6.6V9H20zm-9.2 5.2H4V20h5.2v-2H6.6l3.2-3.2-1.5-1.5-3.2 3.2H9.2zM14.8 20H20v-5.2h-2v2.4l-3.2-3.2-1.5 1.5 3.2 3.2h-1.7V20z" transform="translate(1.6 1.6) scale(.87)"/>',
    "link": '<path d="M10.6 13.4a4.24 4.24 0 005.99 0l3-3a4.24 4.24 0 10-5.99-6l-1.5 1.5 1.41 1.42 1.5-1.5a2.12 2.12 0 113 3l-3 3a2.12 2.12 0 01-3 0l-1.4-1.4zM13.4 10.6a4.24 4.24 0 00-6 0l-3 3a4.24 4.24 0 106 6l1.5-1.5-1.41-1.42-1.5 1.5a2.12 2.12 0 11-3-3l3-3a2.12 2.12 0 013 0l1.4 1.4z"/>',
    "repost": '<path d="M12.5 3.5A8.5 8.5 0 0120.6 10l1.7-1.7v5.2h-5.2l2-2A6.5 6.5 0 0012.5 5.5v-2zM11.5 20.5A8.5 8.5 0 013.4 14l-1.7 1.7V10.5h5.2l-2 2a6.5 6.5 0 006.6 6v2z"/>',
    "unhide": '<path d="M9 5.5L3 11.5 9 17.5v-3.7c5 0 8.5 1.6 11 4.7-.8-6-4.5-9.7-11-10.3V5.5z"/>',
    "smile": '<path fill-rule="evenodd" d="M12 2.5a9.5 9.5 0 100 19 9.5 9.5 0 000-19zM8.2 9.4a1.5 1.5 0 110 3 1.5 1.5 0 010-3zm7.6 0a1.5 1.5 0 110 3 1.5 1.5 0 010-3z"/><path class="stk" d="M7.8 13.8a5.6 5.6 0 008.4 0"/>',
    "note": '<path d="M3.6 20.4l1.1-4.1L16 5a1.9 1.9 0 012.7 0l.3.3a1.9 1.9 0 010 2.7L7.7 19.3l-4.1 1.1zM17.6 6.2l1.2 1.2.8-.8a1.9 1.9 0 00-2.7-2.7l-.8.8 1.5 1.5z"/>',
    "star2": '<path d="M12 2.4l2.95 6.35 6.95.8-5.15 4.8 1.35 6.85L12 17.85l-6.1 3.35 1.35-6.85-5.15-4.8 6.95-.8z"/>',
    "medal": '<path d="M8.2 3h3l-1.4 5.2L7 9.6 8.2 3zm7.6 0h-3l1.4 5.2L17 9.6 15.8 3z"/><circle cx="12" cy="14.8" r="5.6"/><circle cx="12" cy="14.8" r="2.4" fill="none"/>',
    "palette": '<path fill-rule="evenodd" d="M12 3a9 9 0 100 18c1.4 0 2-.9 2-1.9 0-.8-.5-1.3-.5-2.1 0-.9.7-1.5 1.7-1.5h2.9a3.4 3.4 0 003.4-3.3C21.4 7.2 17.1 3 12 3zM7.6 10.4a1.6 1.6 0 110-3.2 1.6 1.6 0 010 3.2zm3.5-4a1.6 1.6 0 110 3.2 1.6 1.6 0 010-3.2zm4.9.8a1.6 1.6 0 110 3.2 1.6 1.6 0 010-3.2z"/>',
    "photo": '<path fill-rule="evenodd" d="M4 4.5h16A1.5 1.5 0 0121.5 6v12a1.5 1.5 0 01-1.5 1.5H4A1.5 1.5 0 012.5 18V6A1.5 1.5 0 014 4.5zm13.4 3a1.7 1.7 0 100 3.4 1.7 1.7 0 000-3.4z"/><path d="M4.8 18.2l5-5 3.2 3.2 3-3 4.7 4.7v.6H4.8v-.5z"/>',
    "tap": '<path d="M9.2 12.6V5.8a1.7 1.7 0 013.4 0V4.2a1.7 1.7 0 013.4 0v1.9a1.7 1.7 0 013.4.8v6.2c0 4.9-3 8.9-7.6 8.9-3.8 0-5.9-2.1-7.7-5.6l-1.5-3c-.5-1.1.9-2.1 1.9-1.4l2.7 2v-1.4z"/>',
    "hourglass": '<path d="M6 3h12v2.8H6zM6 18.2h12V21H6zM7.6 5.2h8.8c0 3.3-1.9 4.4-3.1 5.3-.8.6-.8 1.4 0 2 1.2.9 3.1 2 3.1 5.3H7.6c0-3.3 1.9-4.4 3.1-5.3.8-.6.8-1.4 0-2C9.5 9.6 7.6 8.5 7.6 5.2z"/>',
    "stop": '<path d="M8.6 3h6.8L21 8.6v6.8L15.4 21H8.6L3 15.4V8.6L8.6 3z"/>',
    "shield": '<path d="M12 2.5l8 3v6c0 5-3.4 8.4-8 10-4.6-1.6-8-5-8-10v-6l8-3z"/>',
    "mirror": '<path d="M7 3h10a2 2 0 012 2v10a2 2 0 01-2 2H7a2 2 0 01-2-2V5a2 2 0 012-2zm4.2 14h1.6v2.4h3.4V21H7.8v-1.6h3.4V17z"/>',
    "ask": '<path d="M9.2 12.6V5.8a1.7 1.7 0 013.4 0V4.2a1.7 1.7 0 013.4 0v1.9a1.7 1.7 0 013.4.8v6.2c0 4.9-3 8.9-7.6 8.9-3.8 0-5.9-2.1-7.7-5.6l-1.5-3c-.5-1.1.9-2.1 1.9-1.4l2.7 2v-1.4z"/>',
    "inbox": '<path d="M4 4.5h16A1.5 1.5 0 0121.5 6v12a1.5 1.5 0 01-1.5 1.5H4A1.5 1.5 0 012.5 18V6A1.5 1.5 0 014 4.5zm6.2 1.8v4.2H7.4l4.6 4.6 4.6-4.6h-2.8V6.3H10.2zM5 15.5h14V18H5z" transform="scale(.96) translate(.5 .5)"/>',
    "wave": '<path d="M9.2 12.6V5.8a1.7 1.7 0 013.4 0V4.2a1.7 1.7 0 013.4 0v1.9a1.7 1.7 0 013.4.8v6.2c0 4.9-3 8.9-7.6 8.9-3.8 0-5.9-2.1-7.7-5.6l-1.5-3c-.5-1.1.9-2.1 1.9-1.4l2.7 2v-1.4z"/>',
    "growth": '<path d="M3.5 12.5h4.4V21H3.5zM9.8 7.8h4.4V21H9.8zM16.1 3.2h4.4V21h-4.4z"/>',
    "tag": '<path fill-rule="evenodd" d="M3.5 3.5h8.1l9 9a2 2 0 010 2.8l-5.7 5.7a2 2 0 01-2.8 0l-8.6-8.6v-8.9zm3.4 3.1a1.8 1.8 0 100 3.6 1.8 1.8 0 000-3.6z"/>',
    "mega": '<path d="M3 10.2v3.6l3 .6v-4.8l-3 .6zM19 5L8 8.8v6.4L19 19V5zM10.2 15.4l-.1 1.8a2.6 2.6 0 005.1.4l-.3-1.5-4.7-.7z"/>',
    "unlock": '<path d="M6.8 10.2V8a5.2 5.2 0 0110.3-.8l-2 .9A3 3 0 009.2 8v2.2h8a1.8 1.8 0 011.8 1.8v8a1.8 1.8 0 01-1.8 1.8H6A1.8 1.8 0 014.2 20v-8A1.8 1.8 0 016 10.2h.8zM12 14a2.1 2.1 0 011 3.9v1.6h-2v-1.6A2.1 2.1 0 0112 14z"/>',
}


def icon_style_of(u):
    """The viewer's icon finish; anything unrecognized is 'emoji'."""
    s = ((u or {}).get("icon_style") or "emoji").strip().lower()
    return s if s in ICON_STYLES else "emoji"


def icon_html(key, style="emoji"):
    """One chrome icon as the viewer wants it: the classic emoji
    glyph for 'emoji' (and unknown keys), else the custom vector
    art as an inline SVG that base.html's finish CSS paints.
    Returned as Markup so templates never escape it."""
    from markupsafe import Markup
    if style == "emoji" or key not in ICON_ART:
        return ICON_EMOJI.get(key, "")
    return Markup(
        '<svg class="hic" viewBox="0 0 24 24" aria-hidden="true">'
        + ICON_ART[key] + "</svg>")


# --- Build LOOKS: schemes, surface/accent colors, presets -----------
# A "look" is: a scheme (glass / solid / mix), an optional surface
# color (tints the glass; IS the card color when solid) and an
# optional accent color (primary buttons, active states, content
# links). Storage: users.look_scheme / look_surface / look_accent.
# Emitted per viewer as --glass-alpha + --glass-rgb + --look-accent
# on <body> (render ctx), and per profile owner on the .spot-scope.
LOOK_SCHEMES = ("glass", "solid", "mix")
LOOK_DEFAULT_ACCENT = "#d41f2e"          # HERE red
LOOK_DEFAULT_RGB = (255, 255, 255)       # the glass fills' own white


def look_scheme_for(u):
    """The user's effective look scheme. A row whose stored value is
    missing/invalid falls back to the Build GLASS semantics: having
    chosen a solidity (glass_set) means Mix, otherwise Glass."""
    s = str(_uget(u, "look_scheme") or "").strip().lower()
    if s in LOOK_SCHEMES:
        return s
    return "mix" if _uget(u, "glass_set") else "glass"


def _look_surface_hex(u):
    """The user's surface color as a valid lowercase #rrggbb, or None."""
    v = str(_uget(u, "look_surface") or "").strip().lower()
    return v if _spot_hex_ok(v) else None


def _look_accent_hex(u):
    """The user's accent color as valid #rrggbb, or None (= HERE red)."""
    v = str(_uget(u, "look_accent") or "").strip().lower()
    return v if _spot_hex_ok(v) else None


def look_rgb_for(u):
    """(r, g, b) behind --glass-rgb: the surface color when set, else
    the exact white triplet the glass fills have always used — so an
    unset look renders pixel-identical to before."""
    surf = _look_surface_hex(u)
    return _hex_to_rgb(surf) if surf else LOOK_DEFAULT_RGB


def look_rgb_str_for(u):
    r, g, b = look_rgb_for(u)
    return f"{r},{g},{b}"


def look_accent_for(u):
    return _look_accent_hex(u) or LOOK_DEFAULT_ACCENT


def look_accent_rgb_str_for(u):
    """The accent as an 'r,g,b' triplet for --look-accent-rgb: the
    Build SHINE glows ride rgba() and need the accent in components.
    look_accent_for always yields a valid hex, so this never fails;
    a guest (None) gets the HERE red triplet."""
    return ",".join(str(c) for c in _hex_to_rgb(look_accent_for(u)))


def look_scheme_class_for(u):
    """'scheme-glass' / 'scheme-solid' / 'scheme-mix' — the Build
    SHINE gate class for any scope ruled by u's look: <body> for the
    viewer, the profile .spot-scope for its owner. A guest (None)
    lives in the default glass."""
    if not u:
        return "scheme-glass"
    return "scheme-" + look_scheme_for(u)


def _hex_luminance(rgb):
    """Relative luminance (sRGB coefficients) of an (r, g, b) triplet,
    0.0 (black) .. 1.0 (white). Drives the readability guardrail."""
    r, g, b = (c / 255.0 for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def look_scope_class_for(u):
    """The readability-guardrail class for a scope carrying u's look:
    'look-on-dark' when u's surface color is dark (text on the look
    surfaces flips light), 'look-on-light' when it is light, and ""
    when no surface is set (the scope renders exactly as today)."""
    if not _look_surface_hex(u):
        return ""
    return ("look-on-light" if _hex_luminance(look_rgb_for(u)) >= 0.5
            else "look-on-dark")


def look_vars_for(u):
    """(alpha, soft_alpha, rgb_str, accent, scope_class) — the full
    look var set for a user record (or the guest defaults for None).
    alpha drives the card family; soft_alpha the small surfaces."""
    if not u:
        return (GLASS_DEFAULT_ALPHA, GLASS_DEFAULT_ALPHA,
                look_rgb_str_for(None), LOOK_DEFAULT_ACCENT, "")
    return (glass_alpha_for(u), glass_alpha_soft_for(u),
            look_rgb_str_for(u), look_accent_for(u),
            look_scope_class_for(u))


def profile_showcase_alpha_for(u):
    """The alpha a profile page showcases with: the owner's
    own glass value, or CLEAR GLASS (0.35) when they have a
    background, never saved a slider value, and run the
    Glass scheme. Shared by the profile scope vars and the
    profile page's --profile-glass (chrome) var so every
    surface on the page agrees."""
    if (u and u.get("background") and not u.get("glass_set")
            and look_scheme_for(u) == "glass"):
        return "0.35"
    return glass_alpha_for(u)


def profile_showcase_soft_for(u):
    """The small-surface twin of profile_showcase_alpha_for:
    the owner's soft (bubble) alpha, or CLEAR GLASS (0.35)
    under the same background/no-choice/Glass conditions."""
    if (u and u.get("background") and not u.get("glass_set")
            and look_scheme_for(u) == "glass"):
        return "0.35"
    return glass_alpha_soft_for(u)


def look_scope_style_for(u):
    """The owner's look vars as an inline style for a profile scope:
    their --glass-alpha + --glass-alpha-soft (by scheme), --glass-rgb,
    --look-accent and its --look-accent-rgb triplet (Build SHINE),
    always all of them, so a viewer's own vars can never cascade into
    somebody else's profile (the spot_scope_style_for discipline)."""
    alpha, soft, rgb, accent, _cls = look_vars_for(u)
    # Transparency (Ken's order): a profile with a custom
    # background must SHOW it — CLEAR GLASS, Ken's picture of
    # his own profile (Oct 25 shot, sent again Oct 27): the
    # pattern plainly visible through every box, text still
    # readable on the frost. An owner who never touched the
    # transparency slider gets that clear scope default
    # (0.35) on the cards AND the small surfaces (comment
    # bubbles included — Ken: 'even with the comments, even
    # with the post'); an explicit slider choice always
    # wins, and only the Glass scheme is see-through by
    # design.
    if (u and u.get("background") and not u.get("glass_set")
            and look_scheme_for(u) == "glass"):
        alpha = profile_showcase_alpha_for(u)
        soft = profile_showcase_soft_for(u)
    return (f"--glass-alpha:{alpha};--glass-alpha-soft:{soft}"
            f";--glass-rgb:{rgb};--look-accent:{accent}"
            f";--look-accent-rgb:{look_accent_rgb_str_for(u)}")


def apply_look(conn, user_id, values):
    """Write a look to a user's row — THE one writer, shared by the
    Settings My Look form, the preset cards, Steal This Look and the
    welcome vibe picker. `values` may carry any of: scheme, surface,
    accent, box_opacity, spot_text_color, spot_card_color. Only the
    keys present are written. Colors validate as #rrggbb; an invalid
    or empty color stores NULL (unset). box_opacity parses + clamps
    to 0..100 and marks glass_set=1 (a chosen solidity — 0 is a real
    choice: completely clear)."""
    sets, args = [], []

    def _color(key, column):
        if key in values:
            v = values[key]
            v = str(v).strip().lower() if v else None
            sets.append(f"{column}=?")
            args.append(v if _spot_hex_ok(v) else None)

    if "scheme" in values:
        s = str(values["scheme"] or "").strip().lower()
        sets.append("look_scheme=?")
        args.append(s if s in LOOK_SCHEMES else "glass")
    _color("surface", "look_surface")
    _color("accent", "look_accent")
    _color("spot_text_color", "spot_text_color")
    _color("spot_card_color", "spot_card_color")
    if values.get("box_opacity") is not None:
        try:
            op = int(values["box_opacity"])
        except (TypeError, ValueError):
            op = None
        if op is not None:
            sets.append("box_opacity=?")
            args.append(max(BOX_OPACITY_MIN, min(BOX_OPACITY_MAX, op)))
            sets.append("glass_set=1")
    if not sets:
        return
    args.append(user_id)
    conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", args)


# Preset looks: one tap applies the whole bundle through apply_look.
# surface/accent None = unset (theme default). Midnight Gold carries
# Spot colors too — Ken's screenshot-2 look (solid near-black cards,
# gold name) is a Spot text color as much as a scheme.
LOOK_PRESETS = {
    "classic": {"name": "Classic HERE Glass", "scheme": "glass",
                "surface": None, "accent": None},
    "midnight_gold": {"name": "Midnight Gold", "scheme": "solid",
                      "surface": "#101010", "accent": "#d4af37",
                      "spot_text_color": "#ffd700",
                      "spot_card_color": "#101010"},
    "ice": {"name": "Ice Glass", "scheme": "glass",
            "surface": "#cfe8ff", "accent": "#38bdf8"},
    "royal": {"name": "Royal Purple", "scheme": "mix", "box_opacity": 70,
              "surface": "#2a1450", "accent": "#a78bfa"},
    "money": {"name": "Money Green", "scheme": "solid",
              "surface": "#0d2818", "accent": "#22c55e"},
    "rose": {"name": "Rose Glow", "scheme": "glass",
             "surface": "#3d1220", "accent": "#fb7185"},
}
LOOK_PRESET_ORDER = ["classic", "midnight_gold", "ice", "royal",
                     "money", "rose"]
_PRESET_KEYS = ("scheme", "surface", "accent", "box_opacity",
                "spot_text_color", "spot_card_color")


def apply_preset(conn, user_id, key):
    """Apply a LOOK_PRESETS entry through apply_look. Returns the
    preset's display name, or None for an unknown key."""
    p = LOOK_PRESETS.get(key or "")
    if not p:
        return None
    apply_look(conn, user_id, {k: p[k] for k in _PRESET_KEYS if k in p})
    return p["name"]


def look_preset_views():
    """Preset cards for templates (Settings + welcome): the stored
    values plus the swatch colors a card should paint with."""
    out = []
    for key in LOOK_PRESET_ORDER:
        p = LOOK_PRESETS[key]
        out.append({"key": key, "name": p["name"],
                    "scheme": p["scheme"],
                    "surface": p.get("surface"),
                    "accent": p.get("accent") or LOOK_DEFAULT_ACCENT,
                    "box_opacity": p.get("box_opacity")})
    return out


def _album_photos(viewer, owner_id, owner_on=False, limit=None):
    """Visible photo album for owner_id: personal posts (page_id IS NULL)
    with image media, newest first, each gated by can_view_post."""
    conn = db()
    rows = conn.execute(
        "SELECT * FROM posts WHERE user_id=? AND page_id IS NULL "
        "AND media != '' ORDER BY created_at DESC", (owner_id,)).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        media = d.get("media") or ""
        ext = media.rsplit(".", 1)[-1].lower() if "." in media else ""
        if ext not in IMG_EXTS:
            continue
        if not can_view_post(viewer, d, owner_on):
            continue
        # Build LOCKED CONTENT: a locked photo joins the album only
        # for viewers entitled to open it, and then only through the
        # /locked gate — never a public uploads URL.
        if d.get("lock_price"):
            entitled = bool(owner_on or viewer["id"] == d["user_id"])
            if not entitled:
                _lc = db()
                entitled = _has_unlock(_lc, viewer["id"], "post",
                                       d["id"])
                _lc.close()
            if not entitled:
                continue
            out.append({"url": "/locked/post/%d" % d["id"],
                        "post_id": d["id"],
                        "when": fancy_time(d["created_at"])})
            if limit is not None and len(out) >= limit:
                break
            continue
        out.append({"url": "/uploads/" + media, "post_id": d["id"],
                    "when": fancy_time(d["created_at"])})
        if limit is not None and len(out) >= limit:
            break
    return out


# ---------------- profile / settings ----------------
@app.get("/user/{uid}", response_class=HTMLResponse)
async def profile(request: Request, uid: int):
    user, err = require_login(request)
    if err:
        return err
    owner_on = is_owner(request)
    prof = get_user(uid)
    if not prof:
        return redir("/home", "User not found.")
    # A block in either direction makes the profile unavailable to the
    # viewer (the owner context always sees through it).
    if user["id"] != uid and not owner_on \
            and blocked_either_way(user["id"], uid):
        return render(request, "profile_unavailable.html", {})
    conn = db()
    # --- profile view tracking: count one view per viewer/viewed pair per hour,
    # never count self-views ---
    if user["id"] != uid:
        conn.execute(
            "INSERT INTO profile_views (viewer_id, viewed_id, viewed_at) "
            "SELECT ?, ?, ? WHERE NOT EXISTS ("
            "SELECT 1 FROM profile_views WHERE viewer_id=? AND viewed_id=? "
            "AND viewed_at > datetime('now','-1 hour'))",
            (user["id"], uid, now_iso(), user["id"], uid))
        # --- tap hygiene: visiting someone's profile clears the tap
        # notifications they sent me (their tap note points at THIS
        # profile), so a seen tap doesn't sit in the bell forever. The
        # same visit ACKNOWLEDGES their taps to me: the number beside
        # the Tap button counts only unacknowledged taps, so it clears
        # here too — a fresh tap from them afterwards counts again. ---
        conn.execute(
            "DELETE FROM notifications WHERE user_id=? AND kind='tap' "
            "AND link=?", (user["id"], f"/user/{uid}"))
        _ack_taps(conn, uid, user["id"])
        conn.commit()
    fcount = conn.execute(
        "SELECT COUNT(*) c FROM friendships WHERE status='accepted' AND "
        "(user_id=? OR friend_id=?)", (uid, uid)).fetchone()["c"]
    # The number beside the Tap button counts only UNACKNOWLEDGED taps
    # this profile has received (see _ack_taps): once the owner has
    # visited a tapper's profile or tapped them back, that tapper's
    # taps stop counting and the number shrinks or vanishes entirely.
    tcount = _unacked_tap_count(conn, uid)
    # Build WALL: the profile's post list is the owner's own posts
    # PLUS approved posts written on their wall. Only the owner's own
    # pins float (a pinned wall post keeps its natural place); pending
    # wall posts are skipped here and rendered in the owner's pending
    # section instead.
    prows = conn.execute(
        "SELECT * FROM posts WHERE (user_id=? OR wall_user_id=?) "
        "AND page_id IS NULL "
        "ORDER BY (pinned=1 AND user_id=?) DESC, created_at DESC "
        "LIMIT 100", (uid, uid, uid)).fetchall()
    hidden = {r["post_id"] for r in conn.execute(
        "SELECT post_id FROM hidden_posts WHERE user_id=?", (user["id"],)).fetchall()}
    conn.close()
    _, spot_classes = spot_style_for(prof)
    status = req_status(user["id"], uid)
    # Go Live badge: this profile's active live, shown only when the
    # viewer could actually watch it (the locked face hides it too —
    # a private host's non-friends don't even learn they're live).
    _lconn = db()
    _sweep_lives(_lconn)
    live_row = _lconn.execute(
        "SELECT id FROM live_sessions WHERE host_id=? AND "
        "status='live' ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
    _lconn.close()
    prof_live_id = live_row["id"] if live_row else None
    # Build W2: ⚔️ Showdown record (finished battles only).
    _bconn = db()
    _sw, _sl, _sd = _showdown_record(_bconn, uid)
    _bconn.close()
    # Private profile: non-friends see a locked profile (public face
    # only) — the owner, their friends, and the owner context see all.
    locked = bool(prof.get("private_profile")) and user["id"] != uid \
        and not owner_on and status != "friends"
    profile_ctx = {
        "id": prof["id"], "name": prof["name"], "bio": prof.get("bio", ""),
        "avatar": avatar_url(prof.get("avatar", "")),
        "created_at": fancy_time(prof["created_at"]),
        "friend_count": fcount,
        "follower_count": follower_count(uid),
        "following_count": following_count(uid),
        "invite_count": invite_count(uid),
        "is_gold_inviter": is_gold_inviter(uid),
        "is_insider": is_insider(uid),
        "live_level": _streamer_level(prof.get("gift_lifetime")),
        "showdown_w": _sw, "showdown_l": _sl, "showdown_d": _sd,
        "streak": current_streak(uid),
        "contest_winner": contest_winner_user_id() == uid,
        "viewer_follows": bool(user["id"] != uid
                               and is_following(user["id"], uid)),
        "is_self": user["id"] == uid,
        # Build PAGE GIFTS / Creator Mode: the owner's own profile
        # shows the switch state as a small line (no badge).
        "creator_mode": 1 if prof.get("creator_mode") else 0,
        "artist_verified": 1 if prof.get("artist_verified") else 0,
        "artist_contract": prof.get("artist_contract") or "",
        "req_status": status,
        "tap_count": tcount,
        "is_verified": bool(prof.get("is_verified")),
        "is_verified_user": bool(prof.get("is_verified_user")),
        "vuser_status": prof.get("vuser_status") or "none",
        "is_verified_vendor": bool(prof.get("is_verified_vendor")),
        "vvendor_status": prof.get("vvendor_status") or "none",
        "founding": bool(prof.get("founding")),
        "founding_no": prof.get("founding_no"),
        "is_plus": _is_plus(prof),
        "verification_status": prof.get("verification_status") or "none",
        "background": avatar_url(prof.get("background", "")),
        "presence": presence_of(prof),
        "is_private": bool(prof.get("private_profile")),
        "is_private_locked": locked,
        "live_id": prof_live_id if not locked else None,
        "muted_by_viewer": bool(user["id"] != uid
                                and uid in muted_id_set(user["id"])),
        "spot_name": (prof.get("spot_name") or "").strip(),
        # The scope ALWAYS carries all three vars (owner's values or the
        # theme defaults) so a viewer's body-level spot can never cascade
        # into this profile — see spot_scope_style_for().
        "spot_style": spot_scope_style_for(prof),
        "spot_classes": spot_classes,
        # Build LOOKS: the scope ALSO carries the OWNER's look vars
        # (their scheme alpha, surface rgb, accent + readability
        # class) — a visitor sees this profile in its owner's look,
        # while their own chrome keeps their own look.
        "look_style": look_scope_style_for(prof),
        "look_classes": look_scope_class_for(prof),
        # Build SHINE: the .spot-scope shines by the OWNER's scheme.
        "look_scheme_class": look_scheme_class_for(prof),
        "can_steal_look": bool(user["id"] != uid and not locked),
    }
    posts = []
    if not locked:
        for r in prows:
            d = dict(r)
            if d.get("wall_pending"):
                continue  # the owner's pending section below owns these
            if (d.get("shared_post_id") or d["id"]) in hidden:
                continue
            card = post_card(d, user, owner_on)
            if card:
                posts.append(card)
    # Build WALL: pending wall posts, visible ONLY to the wall owner
    # (or the owner context) on their own profile.
    pending_wall = []
    if not locked and (user["id"] == uid or owner_on):
        _pconn = db()
        _prows = _pconn.execute(
            "SELECT * FROM posts WHERE wall_user_id=? AND wall_pending=1 "
            "ORDER BY created_at DESC LIMIT 50", (uid,)).fetchall()
        _pconn.close()
        for r in _prows:
            card = post_card(dict(r), user, owner_on)
            if card:
                pending_wall.append(card)
    # Build WALL: who may write here, and the owner's wall switches.
    _wall_open = prof.get("wall_open")
    wall_open = 1 if _wall_open is None else (1 if _wall_open else 0)
    wall_approve_first = 1 if prof.get("wall_approve_first") else 0
    can_write_wall = bool(user["id"] != uid and not locked
                          and status == "friends" and wall_open)
    wall_closed_note = bool(user["id"] != uid and not locked
                            and status == "friends" and not wall_open)
    composer_friends = []
    if user["id"] == uid or can_write_wall:
        composer_friends, _pages = composer_choices(user)
    if locked:
        album_photos = []
        album_count = 0
    else:
        _album_all = _album_photos(user, uid, owner_on)
        album_photos = _album_all[:6]
        album_count = len(_album_all)
    # On a profile page the PROFILE OWNER's background wins over the
    # viewer's own (render() only defaults page_bg when unset). Same for
    # the viewer's spot colors: blanked here, the owner's .spot-scope
    # vars rule the whole page.
    # 🛒 Selling strip: the profile owner's non-deleted listings
    # (actives first, solds badged). Hidden with everything else on a
    # locked profile; a blocked pair never reaches this route.
    if locked:
        selling = []
        selling_count = 0
    else:
        _mconn = db()
        _mrows = _mconn.execute(
            "SELECT l.*, u.name AS sname, u.avatar AS savatar, u.is_verified_vendor AS svvendor "
            "FROM market_listings l JOIN users u ON u.id=l.seller_id "
            "WHERE l.seller_id=? AND l.status!='deleted' "
            "ORDER BY (l.status='sold'), l.created_at DESC LIMIT 6",
            (uid,)).fetchall()
        selling_count = _mconn.execute(
            "SELECT COUNT(*) c FROM market_listings WHERE seller_id=? "
            "AND status!='deleted'", (uid,)).fetchone()["c"]
        _mconn.close()
        selling = [_listing_card(r) for r in _mrows]
    # Build PAGE GIFTS: the 🎁 Gift button + sheet ride on every
    # profile except your own (you can't gift yourself) and except
    # locked private profiles; Creator Mode gates it too — the
    # button only exists while the profile owner is a creator.
    # The Recent gifts strip shows unless the profile is locked.
    # A blocked pair never reaches here.
    page_gift = None
    gift_locked = None
    if (user["id"] != uid and not locked
            and prof.get("creator_mode")):
        page_gift = _page_gift_ctx(user, f"/user/{uid}/gift",
                                   prof["name"])
    elif user["id"] != uid and not locked:
        gift_locked = {"name": prof["name"], "kind": "profile",
                       "id": uid}
    recent_page_gifts = ([] if locked
                         else _recent_page_gifts(uid, None))
    media_mode = (prof.get("media_mode") or "") if not locked \
        else ""
    media_tracks = (_media_tracks_for(uid, user["id"])
                    if media_mode else None)
    media_cap = _media_cap(prof)
    tip_goal = None
    if prof["creator_mode"] and (prof["tip_goal_tickets"] or 0) > 0:
        _tg_goal = prof["tip_goal_tickets"]
        _tg_got = max(0, (prof["gift_lifetime"] or 0)
                      - (prof["tip_goal_base"] or 0))
        tip_goal = {"label": prof["tip_goal_label"] or "Tip goal",
                    "goal": _tg_goal, "got": _tg_got,
                    "pct": min(100, _tg_got * 100 // _tg_goal),
                    "done": _tg_got >= _tg_goal}
    return render(request, "profile.html",
                  {"profile": profile_ctx, "posts": posts,
                   "pending_wall_posts": pending_wall,
                   "can_write_wall": can_write_wall,
                   "wall_closed_note": wall_closed_note,
                   "wall_open": wall_open,
                   "wall_approve_first": wall_approve_first,
                   "page_gift": page_gift,
                   "gift_locked": gift_locked,
                   "media_mode": media_mode,
                   "media_tracks": media_tracks,
                   "media_cap": media_cap,
                   "media_flash":
                       request.query_params.get("media") or "",
                   "recent_page_gifts": recent_page_gifts,
                   "composer_friends": composer_friends,
                   "album_photos": album_photos,
                   "album_count": album_count,
                   "selling": selling, "selling_count": selling_count,
                   "tip_goal": tip_goal,
                   "page_bg": profile_ctx["background"],
                   "profile_glass": profile_showcase_alpha_for(prof),
                   "profile_glass_soft":
                       profile_showcase_soft_for(prof),
                   "viewer_spot_style": "", "viewer_spot_classes": ""})


@app.post("/user/{uid}/steal-look")
async def steal_look(request: Request, uid: int):
    """🎨 Steal This Look: copy another user's look — scheme, surface
    + accent colors, their blend solidity and their Spot COLORS — to
    the caller's own row, through the shared apply_look writer. The
    owner's spot NAME/title and their background photo are NOT looks
    and never copy. Visibility matches the profile itself: a blocked
    pair gets the unavailable page; a private profile the caller
    can't fully view refuses. The owner is notified."""
    user, err = require_login(request)
    if err:
        return err
    prof = get_user(uid)
    if not prof:
        return redir("/home", "User not found.")
    if user["id"] == uid:
        return redir(f"/user/{uid}", "That's your own look 😄")
    owner_on = is_owner(request)
    if not owner_on and blocked_either_way(user["id"], uid):
        return render(request, "profile_unavailable.html", {})
    status = req_status(user["id"], uid)
    locked = bool(prof.get("private_profile")) and not owner_on \
        and status != "friends"
    if locked:
        return redir(f"/user/{uid}",
                     "That look isn't up for grabs — "
                     "the profile is private.")
    values = {"scheme": look_scheme_for(prof),
              "surface": prof.get("look_surface"),
              "accent": prof.get("look_accent"),
              "spot_text_color": prof.get("spot_text_color"),
              "spot_card_color": prof.get("spot_card_color")}
    if prof.get("glass_set") and prof.get("box_opacity") is not None:
        values["box_opacity"] = prof.get("box_opacity")
    conn = db()
    apply_look(conn, user["id"], values)
    conn.commit()
    conn.close()
    notify(uid, "look_steal",
           f"{user['name']} stole your look 🎨",
           f"/user/{user['id']}", actor_id=user["id"])
    return redir(f"/user/{uid}",
                 f"Look stolen from {prof['name']} 🎨 Colors + style "
                 "are yours now — their background photo stays "
                 "theirs.")


@app.get("/user/{uid}/photos", response_class=HTMLResponse)
async def user_photos(request: Request, uid: int):
    user, err = require_login(request)
    if err:
        return err
    owner_on = is_owner(request)
    prof = get_user(uid)
    if not prof:
        return redir("/home", "User not found.")
    if user["id"] != uid and not owner_on \
            and blocked_either_way(user["id"], uid):
        return render(request, "profile_unavailable.html", {})
    status = req_status(user["id"], uid)
    locked = bool(prof.get("private_profile")) and user["id"] != uid \
        and not owner_on and status != "friends"
    _, spot_classes = spot_style_for(prof)
    ctx = {"id": prof["id"], "name": prof["name"],
           "avatar": avatar_url(prof.get("avatar", "")),
           "is_self": user["id"] == uid,
           "is_private_locked": locked,
           "spot_style": spot_scope_style_for(prof),
           "spot_classes": spot_classes,
           "look_style": look_scope_style_for(prof),
           "look_classes": look_scope_class_for(prof),
           "look_scheme_class": look_scheme_class_for(prof)}
    if locked:
        return render(request, "photos.html",
                      {"profile": ctx, "cover": [], "photos": [],
                       "photo_count": 0,
                       "page_bg": avatar_url(prof.get("background", "")),
                       "viewer_spot_style": "",
                       "viewer_spot_classes": ""})
    photos = _album_photos(user, uid, owner_on)
    cover = []
    av = avatar_url(prof.get("avatar", ""))
    if av:
        cover.append({"url": av, "label": "Profile picture"})
    bg = avatar_url(prof.get("background", ""))
    if bg:
        cover.append({"url": bg, "label": "Cover photo"})
    return render(request, "photos.html",
                  {"profile": ctx, "cover": cover, "photos": photos,
                   "photo_count": len(photos),
                   "page_bg": avatar_url(prof.get("background", "")),
                   "viewer_spot_style": "", "viewer_spot_classes": ""})


@app.get("/settings/profile", response_class=HTMLResponse)
async def profile_edit_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    mode = (user.get("status_mode") or "auto")
    if mode == "hidden":
        status_label = "Hidden — nobody sees a bolt for you"
    else:
        pres = presence_of(user)
        status_label = pres["label"] if pres else "Offline"
    conn = db()
    muted_people = [{"id": r["id"], "name": r["name"]} for r in conn.execute(
        "SELECT u.id, u.name FROM mutes m JOIN users u ON u.id=m.muted_id "
        "WHERE m.muter_id=? ORDER BY u.name", (user["id"],)).fetchall()]
    blocked_people = [{"id": r["id"], "name": r["name"]} for r in
                      conn.execute(
        "SELECT u.id, u.name FROM blocks b JOIN users u "
        "ON u.id=b.blocked_id WHERE b.blocker_id=? ORDER BY u.name",
        (user["id"],)).fetchall()]
    _my_sugg = [{"text": r["text"], "status": r["status"],
                 "label": SUGGESTION_STATUS_LABELS.get(
                     r["status"], r["status"]),
                 "created_at": r["created_at"]}
                for r in conn.execute(
                    "SELECT text, status, created_at FROM "
                    "app_suggestions WHERE user_id=? ORDER BY id "
                    "DESC LIMIT 10", (user["id"],)).fetchall()]
    _artist_pending = bool(conn.execute(
        "SELECT 1 FROM artist_applications WHERE user_id=? "
        "AND status='new'", (user["id"],)).fetchone())
    conn.close()
    return render(request, "profile_edit.html", {
        "artist_app_pending": _artist_pending,
        "my_suggestions": _my_sugg,
        "idea_flash": request.query_params.get("idea") or "",
        "media_flash": request.query_params.get("media") or "",
        "artist_flash": request.query_params.get("artist")
        or "",
        "icon_style": icon_style_of(user),
        "icon_styles": [(s, ICON_STYLE_LABELS[s]) for s in ICON_STYLES],
        "icon_previews": {s: {k: icon_html(k, s) for k in
                              ("home", "peeps", "love", "gift", "gear")}
                          for s in ICON_STYLES},
        "muted_people": muted_people,
        "blocked_people": blocked_people,
        "profile": {"id": user["id"], "name": user["name"],
                    "bio": user.get("bio", ""),
                    "avatar": avatar_url(user.get("avatar", "")),
                    "background": avatar_url(user.get("background", "")),
                    "is_verified": bool(user.get("is_verified")),
                    "verification_status": user.get("verification_status") or "none",
                    "is_verified_user": bool(user.get("is_verified_user")),
                    "vuser_status": user.get("vuser_status") or "none",
                    "is_verified_vendor": bool(user.get("is_verified_vendor")),
                    "vvendor_status": user.get("vvendor_status") or "none",
                    "status_mode": mode,
                    "private_profile": 1 if user.get("private_profile")
                    else 0,
                    # Build WALL: the 🧱 Wall card's switches (a NULL
                    # wall_open on a pre-WALL row reads as OPEN).
                    "wall_open": (1 if user.get("wall_open") is None
                                  else (1 if user.get("wall_open")
                                        else 0)),
                    "wall_approve_first": (1 if user.get(
                        "wall_approve_first") else 0),
                    # Build PAGE GIFTS / Creator Mode: the 🎨 card's
                    # switch (default off).
                    "creator_mode": 1 if user.get("creator_mode")
                    else 0,
                    "media_mode": user.get("media_mode") or "",
                    "media_skin": user.get("media_skin") or "",
                    "artist_verified": 1 if user.get(
                        "artist_verified") else 0,
                    "artist_contract": user.get(
                        "artist_contract") or "",
                    "inbox_status": 1 if user.get("inbox_status") is None
                    else (1 if user.get("inbox_status") else 0),
                    "status_label": status_label,
                    "spot_name": user.get("spot_name") or "",
                    "spot_text_color": user.get("spot_text_color") or "",
                    "spot_card_color": user.get("spot_card_color") or "",
                    "spot_card_opacity": user.get("spot_card_opacity"),
                    # Build GLASS: the slider SHOWS the effective
                    # solidity — the saved value once chosen, else
                    # the default-glass 82% so the thumb sits where
                    # the site actually looks.
                    "box_opacity": (user.get("box_opacity")
                                    if user.get("glass_set")
                                    and user.get("box_opacity") is not None
                                    else GLASS_DEFAULT_SOLIDITY),
                    # Build LOOKS: the 🎨 My Look card's state.
                    "look_scheme": look_scheme_for(user),
                    "look_surface": user.get("look_surface") or "",
                    "look_accent": user.get("look_accent") or "",
                    "look_presets": look_preset_views()}})


@app.post("/settings/status")
async def settings_status(request: Request):
    user, err = require_login(request)
    if err:
        return err
    form = await request.form()
    mode = (form.get("status_mode") or "auto").strip()
    if mode not in STATUS_MODES:
        mode = "auto"
    inbox_on = 1 if (form.get("inbox_status") or "") == "1" else 0
    conn = db()
    conn.execute("UPDATE users SET status_mode=?, inbox_status=? WHERE id=?",
                 (mode, inbox_on, user["id"]))
    conn.commit()
    conn.close()
    return redir("/settings/profile", "Your status settings were saved.")


@app.post("/settings/privacy")
async def settings_privacy(request: Request):
    """Private profile toggle: when on, only the user's friends can see
    their profile and their posts (owner context always can)."""
    user, err = require_login(request)
    if err:
        return err
    form = await request.form()
    on = 1 if (form.get("private_profile") or "") == "1" else 0
    conn = db()
    conn.execute("UPDATE users SET private_profile=? WHERE id=?",
                 (on, user["id"]))
    conn.commit()
    conn.close()
    return redir("/settings/profile",
                 "Your profile is now private — only your friends can "
                 "see it." if on else
                 "Your profile is now public.")


@app.post("/settings/wall")
async def settings_wall(request: Request):
    """Build WALL: save the wall switches — wall_open (friends can
    write on your wall) and wall_approve_first (each wall post waits
    for your approval). One route serves the Settings 🧱 Wall card AND
    the quick toggle on your own profile; a same-origin 'next' field
    picks where you land (anything else falls back to Settings)."""
    user, err = require_login(request)
    if err:
        return err
    form = await request.form()
    open_ = 1 if (form.get("wall_open") or "") == "1" else 0
    approve = 1 if (form.get("wall_approve_first") or "") == "1" else 0
    conn = db()
    conn.execute("UPDATE users SET wall_open=?, wall_approve_first=? "
                 "WHERE id=?", (open_, approve, user["id"]))
    conn.commit()
    conn.close()
    nxt = (form.get("next") or "").strip()
    if not (nxt.startswith("/") and not nxt.startswith("//")):
        nxt = "/settings/profile"
    return redir(nxt, "Your wall settings were saved.")


@app.post("/settings/creator")
async def settings_creator(request: Request):
    """Build PAGE GIFTS / Creator Mode: save the 🎨 Creator Mode
    switch. ON makes you a creator — your profile and your Pages
    get the 🎁 Gift button and the gift routes accept sends to
    you (Locked Content will ride the same flag in a later build).
    Fans need nothing to gift. One route serves the Settings 🎨
    Creator Mode card; a same-origin 'next' field picks where you
    land (anything else falls back to Settings)."""
    user, err = require_login(request)
    if err:
        return err
    form = await request.form()
    on = 1 if (form.get("creator_mode") or "") == "1" else 0
    conn = db()
    conn.execute("UPDATE users SET creator_mode=? WHERE id=?",
                 (on, user["id"]))
    conn.commit()
    conn.close()
    nxt = (form.get("next") or "").strip()
    if not (nxt.startswith("/") and not nxt.startswith("//")):
        nxt = "/settings/profile"
    msg = ("Creator Mode is on 🎨 — your profile and Pages can "
           "take gifts now." if on else
           "Creator Mode is off — your gift buttons are hidden.")
    return redir(nxt, msg)


@app.post("/settings/spot")
async def settings_spot(request: Request):
    """Save (or reset) the logged-in user's own "Your Spot" look.

    Only ever touches the caller's own row. Colors that aren't valid
    #rrggbb are stored as unset; opacity clamps to 30-100; an empty
    spot name clears the title. The name is moderation-scanned like
    any other content — a hit files a flag for the owner and the name
    still saves (flags never block on this platform).
    """
    user, err = require_login(request)
    if err:
        return err
    form = await request.form()
    conn = db()
    if (form.get("reset") or "") == "1":
        conn.execute(
            "UPDATE users SET spot_name=NULL, spot_text_color=NULL, "
            "spot_card_color=NULL, spot_card_opacity=NULL WHERE id=?",
            (user["id"],))
        conn.commit()
        conn.close()
        return redir("/settings/profile",
                     "Your spot is back to the default HERE look.")
    name = (form.get("spot_name") or "").strip()
    if len(name) > SPOT_NAME_MAX:
        conn.close()
        return redir("/settings/profile",
                     f"Spot name is too long — {SPOT_NAME_MAX} characters max.")
    text_color = (form.get("spot_text_color") or "").strip().lower()
    if not _spot_hex_ok(text_color):
        text_color = None
    card_color = (form.get("spot_card_color") or "").strip().lower()
    if not _spot_hex_ok(card_color):
        card_color = None
    opacity = None
    if "spot_card_opacity" not in form:
        # Build GLASS: the Your Spot form no longer carries a bubble
        # transparency field (the unified Glass slider owns see-
        # through now) — keep the stored tint strength as-is instead
        # of wiping it to NULL on every Spot save.
        opacity = user.get("spot_card_opacity")
    raw_op = (form.get("spot_card_opacity") or "").strip()
    if raw_op:
        try:
            opacity = max(SPOT_OPACITY_MIN,
                          min(SPOT_OPACITY_MAX, int(raw_op)))
        except ValueError:
            opacity = None
    conn.execute(
        "UPDATE users SET spot_name=?, spot_text_color=?, "
        "spot_card_color=?, spot_card_opacity=? WHERE id=?",
        (name or None, text_color, card_color, opacity, user["id"]))
    conn.commit()
    conn.close()
    if name:
        flag_item("spot_name", user["id"], user["id"], user["name"], name)
    return redir("/settings/profile",
                 "Your spot was saved — that's how your profile looks now.")


@app.post("/settings/boxes")
async def settings_boxes(request: Request):
    """Save the logged-in user's own Box Transparency (box solidity).

    Only ever touches the caller's own row, and it is purely a
    per-viewer display preference: it changes how see-through the
    boxes are on THEIR screen, everywhere on HERE. One exception
    (Ken's transparency order): on their own PROFILE page the
    owner's saved value rules what visitors see, so their custom
    background shows the way they set it. A non-number changes
    nothing.
    """
    user, err = require_login(request)
    if err:
        return err
    form = await request.form()
    raw_op = (form.get("box_opacity") or "").strip()
    try:
        op = int(raw_op)
    except ValueError:
        return redir("/settings/profile",
                     "Box transparency needs a number from 0 to 100.")
    op = max(BOX_OPACITY_MIN, min(BOX_OPACITY_MAX, op))
    conn = db()
    # Build CLEAR: this endpoint IS the Glass-transparency door —
    # saving a solidity here puts the saver in the Glass scheme,
    # the only scheme the slider belongs to.
    conn.execute("UPDATE users SET box_opacity=?, glass_set=1, "
                 "look_scheme='glass' WHERE id=?",
                 (op, user["id"]))
    conn.commit()
    conn.close()
    return redir("/settings/profile",
                 f"Glass is now {op}% solid on your screen.")


@app.post("/settings/look")
async def settings_look(request: Request):
    """Save the logged-in user's own 🎨 My Look — scheme + surface /
    accent colors (+ the transparency slider when the scheme is
    Glass) in ONE atomic apply_look write. Also the target of the preset cards
    (preset=<key>) and of Reset-to-classic (reset=1). Only ever
    touches the caller's own row. The standalone /settings/boxes
    route keeps working for back-compat."""
    user, err = require_login(request)
    if err:
        return err
    form = await request.form()
    conn = db()
    name = apply_preset(conn, user["id"], (form.get("preset") or "")
                        .strip())
    if name:
        conn.commit()
        conn.close()
        return redir("/settings/profile", f"Look applied: {name} 🎨")
    if (form.get("reset") or "") == "1":
        apply_look(conn, user["id"], {"scheme": "glass",
                                      "surface": None, "accent": None})
        conn.commit()
        conn.close()
        return redir("/settings/profile",
                     "Your look is back to the classic HERE glass.")
    scheme = (form.get("scheme") or "glass").strip().lower()
    if scheme not in LOOK_SCHEMES:
        scheme = "glass"
    values = {"scheme": scheme,
              "surface": (None if (form.get("surface_clear") or "") == "1"
                          else (form.get("surface") or "").strip()
                          or None),
              "accent": (None if (form.get("accent_clear") or "") == "1"
                         else (form.get("accent") or "").strip()
                         or None)}
    if scheme == "glass":
        raw = (form.get("box_opacity") or "").strip()
        if raw:
            values["box_opacity"] = raw
    apply_look(conn, user["id"], values)
    conn.commit()
    conn.close()
    return redir("/settings/profile", "Your look was saved 🎨")


@app.post("/settings/icons")
async def settings_icons(request: Request):
    """Save the caller's icon finish (Build ICON STYLES). Purely a
    per-viewer display preference — it changes how the icons look
    on THEIR screen everywhere on HERE. Anything off the whitelist
    changes nothing."""
    user, err = require_login(request)
    if err:
        return err
    form = await request.form()
    style = (form.get("icon_style") or "").strip().lower()
    if style not in ICON_STYLES:
        return redir("/settings/profile",
                     "That icon style isn't one of the choices.")
    conn = db()
    conn.execute("UPDATE users SET icon_style=? WHERE id=?",
                 (style, user["id"]))
    conn.commit()
    conn.close()
    return redir("/settings/profile",
                 "Icon style saved — " + ICON_STYLE_LABELS[style] + ".")


def _delete_upload(rel_path):
    """Best-effort removal of a stored upload (e.g. replaced backgrounds)."""
    try:
        if rel_path and not rel_path.startswith(("http://", "https://", "/")):
            if storage.r2_enabled():
                try:
                    storage.delete_object(rel_path)
                except storage.StorageError:
                    pass
            p = os.path.join(UPLOAD_DIR, rel_path)
            if os.path.isfile(p):
                os.remove(p)
    except Exception:
        pass


@app.post("/settings/profile")
async def profile_edit(request: Request):
    user, err = require_login(request)
    if err:
        return err
    form = await request.form()
    name = (form.get("name") or "").strip() or user["name"]
    bio = (form.get("bio") or "").strip()
    try:
        avatar = save_upload(form.get("avatar"), "avatars", IMG_EXTS)
        background = save_upload(form.get("background"), "backgrounds", IMG_EXTS, max_mb=5)
    except ValueError as e:
        return redir("/settings/profile", str(e))
    remove_bg = (form.get("remove_background") or "") == "1"
    conn = db()
    sets, vals = ["name=?", "bio=?"], [name, bio]
    if avatar:
        sets.append("avatar=?")
        vals.append(avatar)
    old_bg = user.get("background", "")
    if background:
        sets.append("background=?")
        vals.append(background)
        _delete_upload(old_bg)
    elif remove_bg and old_bg:
        sets.append("background=''")
        _delete_upload(old_bg)
    vals.append(user["id"])
    conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE id=?", vals)
    conn.commit()
    conn.close()
    return redir(f"/user/{user['id']}")


# ---------------- friends ----------------
@app.get("/friends", response_class=HTMLResponse)
async def friends_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    incoming = conn.execute(
        "SELECT u.id,u.name,u.avatar,u.last_seen_at,u.status_mode,u.inbox_status,"
        "f.created_at FROM friendships f "
        "JOIN users u ON u.id=f.user_id WHERE f.friend_id=? AND f.status='pending' "
        "ORDER BY f.created_at DESC", (user["id"],)).fetchall()
    sent = conn.execute(
        "SELECT u.id,u.name,u.avatar,u.last_seen_at,u.status_mode,u.inbox_status "
        "FROM friendships f "
        "JOIN users u ON u.id=f.friend_id WHERE f.user_id=? AND f.status='pending' "
        "ORDER BY f.created_at DESC", (user["id"],)).fetchall()
    acc = conn.execute(
        "SELECT u.id,u.name,u.avatar,u.last_seen_at,u.status_mode,u.inbox_status "
        "FROM friendships f "
        "JOIN users u ON u.id = CASE WHEN f.user_id=? THEN f.friend_id ELSE f.user_id END "
        "WHERE f.status='accepted' AND (f.user_id=? OR f.friend_id=?) "
        "ORDER BY u.name", (user["id"], user["id"], user["id"])).fetchall()
    conn.close()
    return render(request, "friends.html", {
        "incoming": [{"id": r["id"], "name": r["name"],
                      "avatar": avatar_url(r["avatar"]),
                      "presence": presence_of(r),
                      "created_at": fancy_time(r["created_at"])} for r in incoming],
        "sent": [{"id": r["id"], "name": r["name"],
                  "avatar": avatar_url(r["avatar"]),
                  "presence": presence_of(r)} for r in sent],
        "friends": [{"id": r["id"], "name": r["name"],
                     "avatar": avatar_url(r["avatar"]),
                     "presence": presence_of(r)} for r in acc],
    })


@app.post("/friends/request/{uid}")
async def friend_request(request: Request, uid: int):
    user, err = require_login(request)
    if err:
        return err
    if uid == user["id"]:
        return back(request, "/friends")
    target = get_user(uid)
    if not target:
        return back(request, "/friends")
    if blocked_either_way(user["id"], uid):
        return redir("/friends",
                     "You can't send a friend request to this person.")
    conn = db()
    exists = conn.execute(
        "SELECT 1 FROM friendships WHERE (user_id=? AND friend_id=?) OR "
        "(user_id=? AND friend_id=?)", (user["id"], uid, uid, user["id"])).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO friendships(user_id, friend_id, status, created_at) "
            "VALUES(?,?,'pending',?)", (user["id"], uid, now_iso()))
        conn.commit()
    conn.close()
    if not exists:
        notify(uid, "friend_request",
               f"{user['name']} sent you a friend request.", "/friends",
               actor_id=user["id"])
        if notifications_allowed(uid, user["id"]):
            send_push(uid, f"👋 {user['name']} sent you a friend request",
                      "Open HERE to see it and accept.",
                      "/friends", tag=f"friend-{user['id']}")
    return back(request, "/friends")


@app.post("/friends/accept/{uid}")
async def friend_accept(request: Request, uid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    conn.execute(
        "UPDATE friendships SET status='accepted' WHERE user_id=? AND friend_id=? "
        "AND status='pending'", (uid, user["id"]))
    conn.commit()
    conn.close()
    return redir("/friends")


@app.post("/friends/decline/{uid}")
async def friend_decline(request: Request, uid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    conn.execute(
        "DELETE FROM friendships WHERE user_id=? AND friend_id=? AND status='pending'",
        (uid, user["id"]))
    conn.commit()
    conn.close()
    return redir("/friends")


@app.post("/friends/cancel/{uid}")
async def friend_cancel(request: Request, uid: int):
    """Cancel an outgoing (sent) friend request."""
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    conn.execute(
        "DELETE FROM friendships WHERE user_id=? AND friend_id=? AND status='pending'",
        (user["id"], uid))
    conn.commit()
    conn.close()
    return back(request, "/friends")


@app.post("/friends/remove/{uid}")
async def friend_remove(request: Request, uid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    conn.execute(
        "DELETE FROM friendships WHERE status='accepted' AND "
        "((user_id=? AND friend_id=?) OR (user_id=? AND friend_id=?))",
        (user["id"], uid, uid, user["id"]))
    conn.commit()
    conn.close()
    return redir("/friends")


# ---------------- follows ----------------
@app.post("/follow/{uid}")
async def follow_user(request: Request, uid: int):
    """Follow a user: a one-way attention subscription, independent
    of friendship. It changes NOTHING about what the follower can see
    (a private profile stays locked to non-friends; can_view_post
    never consults follows) — it only records the relationship for
    follower/following lists, counts, and live-invite eligibility."""
    user, err = require_login(request)
    if err:
        return err
    if uid == user["id"]:
        return back(request, f"/user/{uid}")
    target = get_user(uid)
    if not target:
        return redir("/home", "User not found.")
    if blocked_either_way(user["id"], uid):
        return redir(f"/user/{uid}", "You can't follow this person.")
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO follows(follower_id, followee_id, "
        "created_at) VALUES(?,?,?)", (user["id"], uid, now_iso()))
    conn.commit()
    conn.close()
    return back(request, f"/user/{uid}")


@app.post("/unfollow/{uid}")
async def unfollow_user(request: Request, uid: int):
    user, err = require_login(request)
    if err:
        return err
    if uid == user["id"]:
        return back(request, f"/user/{uid}")
    if blocked_either_way(user["id"], uid):
        return redir(f"/user/{uid}", "You can't follow this person.")
    conn = db()
    conn.execute(
        "DELETE FROM follows WHERE follower_id=? AND followee_id=?",
        (user["id"], uid))
    conn.commit()
    conn.close()
    return back(request, f"/user/{uid}")


def _follow_list_ctx(request, user, uid, which):
    """Shared loader for the followers/following pages: the profile
    must exist and not be blocked off from the viewer (same face as
    the profile itself), and anyone blocked either way WITH THE
    VIEWER is hidden from the list."""
    prof = get_user(uid)
    if not prof:
        return None, redir("/home", "User not found.")
    if user["id"] != uid and not is_owner(request) \
            and blocked_either_way(user["id"], uid):
        return None, render(request, "profile_unavailable.html", {})
    if which == "followers":
        sql = ("SELECT u.* FROM follows f JOIN users u "
               "ON u.id=f.follower_id WHERE f.followee_id=? "
               "ORDER BY f.created_at DESC")
    else:
        sql = ("SELECT u.* FROM follows f JOIN users u "
               "ON u.id=f.followee_id WHERE f.follower_id=? "
               "ORDER BY f.created_at DESC")
    conn = db()
    rows = conn.execute(sql, (uid,)).fetchall()
    conn.close()
    blocked = blocked_id_set(user["id"])
    people = [{"id": r["id"], "name": r["name"],
               "avatar": avatar_url(r["avatar"] or ""),
               "presence": presence_of(r)}
              for r in rows if r["id"] not in blocked]
    return {"profile": {"id": prof["id"], "name": prof["name"]},
            "people": people, "which": which}, None


@app.get("/user/{uid}/followers", response_class=HTMLResponse)
async def followers_page(request: Request, uid: int):
    user, err = require_login(request)
    if err:
        return err
    ctx, rerr = _follow_list_ctx(request, user, uid, "followers")
    if rerr:
        return rerr
    return render(request, "follow_list.html", ctx)


@app.get("/user/{uid}/following", response_class=HTMLResponse)
async def following_page(request: Request, uid: int):
    user, err = require_login(request)
    if err:
        return err
    ctx, rerr = _follow_list_ctx(request, user, uid, "following")
    if rerr:
        return rerr
    return render(request, "follow_list.html", ctx)


# ---------------- tap ----------------
def _ack_taps(conn, tapper_id, tapped_id):
    """Acknowledge tapper_id's taps to tapped_id: the recipient has
    seen or answered them, so they stop counting toward the number
    shown beside the Tap button (Ken's rule: a tap is only an
    attention ping). Fires at the exact two moments the tap's bell
    notification deletes itself — the recipient visiting the tapper's
    profile, or tapping them back. A tap that lands AFTER this moment
    is fresh and counts again (acked_at is compared per tap)."""
    conn.execute(
        "INSERT OR REPLACE INTO tap_acks(tapper_id, tapped_id, acked_at) "
        "VALUES(?,?,?)", (tapper_id, tapped_id, now_iso()))


def _unacked_tap_count(conn, tapped_id):
    """Taps RECEIVED by tapped_id that still show as a number: every
    tap with no ack for its (tapper, recipient) pair at or after the
    tap's own moment. Acknowledged taps render NO number at all."""
    return conn.execute(
        "SELECT COUNT(*) c FROM taps t WHERE t.tapped_id=? AND NOT EXISTS ("
        "SELECT 1 FROM tap_acks a WHERE a.tapper_id=t.tapper_id "
        "AND a.tapped_id=t.tapped_id AND a.acked_at>=t.created_at)",
        (tapped_id,)).fetchone()["c"]


@app.post("/tap/{uid}")
async def tap(request: Request, uid: int):
    user, err = require_login(request)
    if err:
        return err
    if uid == user["id"]:
        return back(request, "/home")
    target = get_user(uid)
    if not target:
        return back(request, "/home")
    conn = db()
    conn.execute("INSERT INTO taps(tapper_id, tapped_id, created_at) VALUES(?,?,?)",
                 (user["id"], uid, now_iso()))
    conn.commit()
    conn.close()
    notify(uid, "tap", f"{user['name']} tapped you. 👆", f"/user/{user['id']}",
           actor_id=user["id"])
    # Answering a tap clears the tap notifications THAT person sent me:
    # their tap note points at their profile, and once I've tapped back
    # it has done its job. The tap-back also ACKNOWLEDGES their taps to
    # me, so the number beside the Tap button clears at the same moment.
    conn = db()
    conn.execute(
        "DELETE FROM notifications WHERE user_id=? AND kind='tap' "
        "AND link=?", (user["id"], f"/user/{uid}"))
    _ack_taps(conn, uid, user["id"])
    conn.commit()
    conn.close()
    return back(request)


# ---------------- silence (mute) + block ----------------
@app.post("/mute/{uid}")
async def mute_user(request: Request, uid: int):
    """Silence a user: their actions stop notifying/pushing me and
    their posts leave my home feed. They are never told, we stay
    friends, and they can still message me (silently)."""
    user, err = require_login(request)
    if err:
        return err
    if uid == user["id"] or not get_user(uid):
        return back(request, "/home")
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO mutes(muter_id, muted_id, created_at) "
        "VALUES(?,?,?)", (user["id"], uid, now_iso()))
    conn.commit()
    conn.close()
    return back(request, f"/user/{uid}")


@app.post("/unmute/{uid}")
async def unmute_user(request: Request, uid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    conn.execute("DELETE FROM mutes WHERE muter_id=? AND muted_id=?",
                 (user["id"], uid))
    conn.commit()
    conn.close()
    return back(request, f"/user/{uid}")


@app.post("/block/{uid}")
async def block_user(request: Request, uid: int):
    """Block a user: one row severs the pair — friendship (either
    direction, any status) and any mutes between them are deleted in
    the same pass — and blocked_either_way() enforcement hides each
    from the other everywhere. Unblocking does NOT restore friendship."""
    user, err = require_login(request)
    if err:
        return err
    if uid == user["id"]:
        return redir("/home", "You can't block yourself.")
    target = get_user(uid)
    if not target:
        return redir("/home", "User not found.")
    conn = db()
    conn.execute(
        "INSERT OR IGNORE INTO blocks(blocker_id, blocked_id, "
        "created_at) VALUES(?,?,?)", (user["id"], uid, now_iso()))
    conn.execute(
        "DELETE FROM friendships WHERE (user_id=? AND friend_id=?) OR "
        "(user_id=? AND friend_id=?)",
        (user["id"], uid, uid, user["id"]))
    conn.execute(
        "DELETE FROM mutes WHERE (muter_id=? AND muted_id=?) OR "
        "(muter_id=? AND muted_id=?)",
        (user["id"], uid, uid, user["id"]))
    conn.execute(
        "DELETE FROM follows WHERE (follower_id=? AND followee_id=?) OR "
        "(follower_id=? AND followee_id=?)",
        (user["id"], uid, uid, user["id"]))
    conn.commit()
    conn.close()
    return redir("/home", f"You blocked {target['name']}. You won't see "
                          f"each other on HERE.")


@app.post("/unblock/{uid}")
async def unblock_user(request: Request, uid: int):
    """Remove MY block on a user. Their block on me (if any) stays, and
    the friendship a block removed is NOT restored."""
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    conn.execute("DELETE FROM blocks WHERE blocker_id=? AND blocked_id=?",
                 (user["id"], uid))
    conn.commit()
    conn.close()
    return back(request, "/settings/profile")


# ---------------- messages ----------------
def _deliver_message(sender, receiver_id, body, story_id=None,
                     story_media="", ama=0, listing_id=None,
                     listing_title=None, listing_price_cents=None,
                     lock_media="", lock_price=0, lock_adult=0):
    """THE message insert path. POST /messages/{fid} and the story
    reply/react endpoints all land here, so every message gets the
    identical treatment: the row insert, the moderation scan, the bell
    notification, and the push (silence/block-gated inside notify() /
    notifications_allowed). Story-linked messages additionally carry
    story_id + the shared clip copy path (see _story_clip_for_replies);
    plain messages leave both empty. ama=1 marks a message that came
    in through a story's "Ask me a question" box (still a story-linked
    message in every other way). Market inquiries (POST
    /market/{id}/message) pass listing_id + a title/price SNAPSHOT so
    the thread can render a listing card that survives the listing
    being edited or deleted. Returns the new message id."""
    conn = db()
    cur = conn.execute(
        "INSERT INTO messages(sender_id, receiver_id, body, created_at, "
        "story_id, story_media, ama, listing_id, listing_title, "
        "listing_price_cents, lock_media, lock_price, lock_adult) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (sender["id"], receiver_id, body, now_iso(), story_id, story_media,
         1 if ama else 0, listing_id, listing_title, listing_price_cents,
         lock_media or "", lock_price or 0, 1 if lock_adult else 0))
    mid = cur.lastrowid
    conn.commit()
    conn.close()
    flag_item("message", mid, sender["id"], sender["name"], body)
    notify(receiver_id, "message", f"New message from {sender['name']}.",
           f"/messages/{sender['id']}", actor_id=sender["id"])
    preview = body if len(body) <= 80 else body[:79] + "…"
    if lock_price:
        # A locked message's caption never rides the push either.
        preview = "🔒 Locked content"
    if notifications_allowed(receiver_id, sender["id"]):
        send_push(receiver_id, f"💬 {sender['name']}", preview,
                  f"/messages/{sender['id']}", tag=f"msg-{sender['id']}")
    return mid


def _msg_preview(body, story_id, ama=0, listing_id=None, lock_price=0):
    """Inbox/Sent preview text for one message row. Story-linked
    messages announce themselves: a reply reads "📖 Story reply: …"
    and a reaction (body is exactly one reaction emoji) reads
    "📖 {emoji} reacted to a story". A story AMA question reads
    "❓ Question about your story: …" (checked first — an AMA message
    is story-linked too, but the question label wins). Plain messages
    preview as-is. A LOCKED message previews as the lock itself —
    its caption stays hidden until the recipient pays."""
    b = (body or "").strip()
    if lock_price:
        return "🔒 Locked content"
    if ama:
        return f"❓ Question about your story: {b}"
    if listing_id:
        return f"🛒 Listing: {b}"
    if story_id:
        if b in REACTION_EMOJI.values():
            return f"📖 {b} reacted to a story"
        return f"📖 Story reply: {b}"
    return body or ""


def _vis_frag(alias="m"):
    """SQL AND-fragment hiding messages the VIEWER deleted-for-me.

    Delete-for-me never removes rows from `messages` (the other person
    keeps their copy; owner moderation works on the raw rows). It hides
    them per viewer in two ways, and every query a user sees goes
    through this fragment:
      * message_hidden — single messages the viewer deleted;
      * thread_deletes — a per-conversation watermark: rows at or
        before hidden_before are hidden; a NEW message after the
        watermark revives the thread with only the new rows visible.
    The fragment carries 3 placeholders, ALL bound to the viewer's id,
    in order. `alias` is the messages-table alias used by the query.
    """
    return (
        f" AND NOT EXISTS (SELECT 1 FROM message_hidden mh "
        f"WHERE mh.message_id={alias}.id AND mh.user_id=?)"
        f" AND NOT EXISTS (SELECT 1 FROM thread_deletes td "
        f"WHERE td.user_id=? AND td.other_id="
        f"(CASE WHEN {alias}.sender_id=? THEN {alias}.receiver_id "
        f"ELSE {alias}.sender_id END) "
        f"AND {alias}.created_at<=td.hidden_before)")


@app.get("/messages", response_class=HTMLResponse)
async def inbox(request: Request):
    user, err = require_login(request)
    if err:
        return err
    box = (request.query_params.get("box") or "inbox").strip()
    blocked = blocked_id_set(user["id"])
    conn = db()
    if box == "sent":
        rows = conn.execute(
            "SELECT m.*, u.name AS rname, u.avatar AS ravatar, "
            "u.last_seen_at AS rlast, u.status_mode AS rmode, "
            "u.inbox_status AS rinbox FROM messages m "
            "JOIN users u ON u.id=m.receiver_id "
            "WHERE m.sender_id=?" + _vis_frag("m") +
            " ORDER BY m.created_at DESC LIMIT 200",
            (user["id"], user["id"], user["id"], user["id"])).fetchall()
        conn.close()
        sent = [{"friend_id": r["receiver_id"],
                 "friend_name": r["rname"] or "Deleted user",
                 "friend_avatar": avatar_url(r["ravatar"]) if r["ravatar"] else "",
                 "presence": presence_of(
                     {"last_seen_at": r["rlast"], "status_mode": r["rmode"],
                      "inbox_status": r["rinbox"]}, inbox=True),
                 "body": _msg_preview(
                     r["body"], r["story_id"],
                     r["ama"] if "ama" in r.keys() else 0,
                     r["listing_id"] if "listing_id" in r.keys() else None,
                     r["lock_price"] if "lock_price" in r.keys() else 0),
                 "created_at": fancy_time(r["created_at"])}
                for r in rows if r["receiver_id"] not in blocked]
        return render(request, "inbox.html",
                      {"box": "sent", "threads": [], "sent": sent})
    rows = conn.execute(
        "SELECT CASE WHEN m.sender_id=? THEN m.receiver_id ELSE m.sender_id "
        "END AS fid, MAX(m.created_at) AS last_at FROM messages m "
        "WHERE (m.sender_id=? OR m.receiver_id=?)" + _vis_frag("m") +
        " GROUP BY fid ORDER BY last_at DESC",
        (user["id"],) * 6).fetchall()
    threads = []
    for r in rows:
        fid = r["fid"]
        if fid in blocked:
            # Conversations with blocked-either-way users leave the
            # inbox for BOTH parties (the rows themselves stay put).
            continue
        f = conn.execute(
            "SELECT id,name,avatar,last_seen_at,status_mode,inbox_status "
            "FROM users WHERE id=?", (fid,)).fetchone()
        last = conn.execute(
            "SELECT m.body, m.created_at, m.story_id, m.ama, m.listing_id, m.lock_price FROM messages m WHERE "
            "((m.sender_id=? AND m.receiver_id=?) OR "
            "(m.sender_id=? AND m.receiver_id=?))" + _vis_frag("m") +
            " ORDER BY m.created_at DESC LIMIT 1",
            (user["id"], fid, fid, user["id"],
             user["id"], user["id"], user["id"])).fetchone()
        unread = conn.execute(
            "SELECT COUNT(*) c FROM messages m WHERE m.sender_id=? "
            "AND m.receiver_id=? AND m.is_read=0" + _vis_frag("m"),
            (fid, user["id"], user["id"], user["id"], user["id"]),
            ).fetchone()["c"]
        threads.append({
            "friend_id": fid,
            "friend_name": f["name"] if f else "Deleted user",
            "friend_avatar": avatar_url(f["avatar"]) if f and f["avatar"] else "",
            "presence": presence_of(f, inbox=True) if f else None,
            "last_body": _msg_preview(
                last["body"], last["story_id"],
                last["ama"] if "ama" in last.keys() else 0,
                last["listing_id"] if "listing_id" in last.keys() else None,
                last["lock_price"] if "lock_price" in last.keys() else 0)
            if last else "",
            "last_at": fancy_time(last["created_at"]) if last else "",
            "unread": unread,
        })
    conn.close()
    return render(request, "inbox.html",
                  {"box": "inbox", "threads": threads, "sent": []})


@app.get("/messages/{fid}", response_class=HTMLResponse)
async def thread(request: Request, fid: int):
    user, err = require_login(request)
    if err:
        return err
    friend = get_user(fid)
    if not friend:
        return redir("/messages", "User not found.")
    if blocked_either_way(user["id"], fid):
        return redir("/messages", "You can't message this person.")
    conn = db()
    conn.execute(
        "UPDATE messages SET is_read=1 WHERE sender_id=? AND receiver_id=?",
        (fid, user["id"]))
    conn.commit()
    rows = conn.execute(
        "SELECT m.* FROM messages m WHERE "
        "((m.sender_id=? AND m.receiver_id=?) OR "
        "(m.sender_id=? AND m.receiver_id=?))" + _vis_frag("m") +
        " ORDER BY m.created_at ASC",
        (user["id"], fid, fid, user["id"],
         user["id"], user["id"], user["id"])).fetchall()
    # Calling permissions for the header buttons: friends call directly;
    # anyone else goes through a call request (pending one shown inline).
    _sweep_call_requests(conn)
    pending_req = _pending_request_between(conn, user["id"], fid)
    # Market inquiry thumbnails: the card shows the listing's CURRENT
    # first photo (fetched live); a deleted listing gets no thumb, so
    # the template renders the 🛒 placeholder instead. The title/price
    # on the card are the message's send-time snapshot.
    _lthumbs = {}
    for r in rows:
        _lid = r["listing_id"] if "listing_id" in r.keys() else None
        if _lid and _lid not in _lthumbs:
            _lr = conn.execute(
                "SELECT photos, status FROM market_listings WHERE id=?",
                (_lid,)).fetchone()
            _t = ""
            if _lr and _lr["status"] != "deleted":
                _ph = _market_photos(_lr)
                if _ph:
                    _t = "/uploads/" + _ph[0]
            _lthumbs[_lid] = _t
    conn.close()
    call_req = None
    if pending_req:
        call_req = {"id": pending_req["id"], "kind": pending_req["kind"],
                    "direction": ("out" if pending_req["requester_id"] == user["id"]
                                  else "in")}
    msgs = []
    for r in rows:
        d = {"id": r["id"], "sender_id": r["sender_id"], "body": r["body"],
             "created_at": fancy_time(r["created_at"]),
             "mine": r["sender_id"] == user["id"], "story": None,
             "listing": None, "locked": None}
        if "lock_price" in r.keys() and r["lock_price"]:
            # Build LOCKED CONTENT: the sender always sees their own
            # content open; the recipient sees it open only after
            # unlocking. Until then the caption is blanked HERE and
            # no media URL is emitted — the thread renders a lock
            # card. The media itself serves only via /locked/dm/{id}.
            _lopen = bool(d["mine"])
            if not _lopen:
                _lc = db()
                _lopen = _has_unlock(_lc, user["id"], "dm", r["id"])
                _lc.close()
            _lkey = r["lock_media"] or ""
            _lext = (_lkey.rsplit(".", 1)[-1].lower()
                     if "." in _lkey else "")
            d["locked"] = {
                "price": r["lock_price"],
                "adult": bool(r["lock_adult"]),
                "open": _lopen,
                "is_video": _lext in VID_EXTS,
                "media_url": (f"/locked/dm/{r['id']}" if _lopen
                              else ""),
            }
            if not _lopen:
                d["body"] = ""
        if "listing_id" in r.keys() and r["listing_id"]:
            d["listing"] = {
                "id": r["listing_id"],
                "title": r["listing_title"] or "",
                "price": _money(r["listing_price_cents"] or 0),
                "thumb": _lthumbs.get(r["listing_id"], "")}
        if r["story_id"]:
            # Story reply/reaction: the bubble carries the story clip
            # (the shared story_replies copy, so it outlives the story)
            # plus a label naming the interaction. A reaction's body
            # IS the emoji; anything else is a reply. The receiver of a
            # story message is always the story's owner.
            reacted = (r["body"] or "").strip() in REACTION_EMOJI.values()
            is_ama = bool(r["ama"]) if "ama" in r.keys() else False
            if is_ama:
                # AMA question (mirror the reply labels, question-style):
                # the receiver reads the ❓ label verbatim, the sender
                # reads who they asked.
                label = (f"❓ You asked {friend['name']} a question "
                         f"about their story" if d["mine"] else
                         "❓ Question about your story")
            elif d["mine"]:
                label = (f"You reacted to {friend['name']}'s story"
                         if reacted else
                         f"You replied to {friend['name']}'s story")
            else:
                label = (f"{friend['name']} reacted to your story"
                         if reacted else
                         f"{friend['name']} replied to your story")
            d["story"] = {
                "media": avatar_url(r["story_media"]) if r["story_media"]
                else "",
                "is_video": (r["story_media"] or "").lower().endswith(
                    (".mp4", ".mov", ".webm")),
                "label": label}
        msgs.append(d)
    return render(request, "thread.html", {
        "friend": {"id": friend["id"], "name": friend["name"],
                   "avatar": avatar_url(friend.get("avatar", "")),
                   "presence": presence_of(friend, inbox=True)},
        "msgs": msgs,
        "is_friend": fid in friend_id_set(user["id"]),
        "call_req": call_req})


@app.post("/messages/{fid}")
async def send_message(request: Request, fid: int):
    user, err = require_login(request)
    if err:
        return err
    friend = get_user(fid)
    if not friend:
        return redir("/messages", "User not found.")
    if blocked_either_way(user["id"], fid):
        return redir("/messages", "You can't message this person.")
    form = await request.form()
    # --- Build LOCKED CONTENT: a lock_media file in the form makes
    # this a paid attachment instead of a plain text message. Gated
    # + priced BEFORE the file is saved, so a refusal stores
    # nothing. The caption (the form's body field) is stored on the
    # row but stays hidden from the recipient until they unlock. ---
    _lock_up = form.get("lock_media")
    if _lock_up is not None and getattr(_lock_up, "filename", ""):
        if not user.get("creator_mode"):
            return redir(f"/messages/{fid}",
                         "Turn on Creator Mode to sell locked content.")
        _lraw = (form.get("lock_price") or "").strip()
        try:
            _lprice = int(_lraw)
        except (TypeError, ValueError):
            return redir(f"/messages/{fid}",
                         "Lock price must be a whole number of tickets.")
        if _lprice < 5 or _lprice > 5000:
            return redir(f"/messages/{fid}",
                         "Lock price must be between 5 and 5,000 "
                         "tickets.")
        _lname = _lock_up.filename or ""
        _lext = _lname.rsplit(".", 1)[-1].lower() if "." in _lname else ""
        _lcap = POST_VIDEO_MAX_MB if _lext in VID_EXTS else 0
        try:
            _lkey = save_locked_upload(_lock_up, "dm",
                                       IMG_EXTS | VID_EXTS, max_mb=_lcap)
        except ValueError as e:
            return redir(f"/messages/{fid}", str(e))
        _ladult = 1 if (form.get("lock_adult") or "").strip() in (
            "1", "on", "true") else 0
        _deliver_message(user, fid, (form.get("body") or "").strip(),
                         lock_media=_lkey, lock_price=_lprice,
                         lock_adult=_ladult)
        return redir(f"/messages/{fid}")
    body = (form.get("body") or "").strip()
    if not body:
        return redir(f"/messages/{fid}")
    _deliver_message(user, fid, body)
    return redir(f"/messages/{fid}")


@app.post("/messages/{mid}/delete")
async def delete_message_for_me(request: Request, mid: int):
    """Delete-for-me, one message: hides it from the CALLER's views
    only (inbox, Sent, thread, unread counts). The other participant
    keeps their copy and the row itself stays in `messages` — owner
    moderation/flags keep working on it. Only a participant (sender or
    receiver) may hide a message; anyone else just gets the same
    not-found redirect, so message ids can't be probed. Idempotent."""
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    m = conn.execute("SELECT * FROM messages WHERE id=?", (mid,)).fetchone()
    if not m or (m["sender_id"] != user["id"]
                 and m["receiver_id"] != user["id"]):
        conn.close()
        return redir("/messages", "Message not found.")
    other = (m["receiver_id"] if m["sender_id"] == user["id"]
             else m["sender_id"])
    conn.execute(
        "INSERT OR IGNORE INTO message_hidden(message_id, user_id, "
        "created_at) VALUES(?,?,?)", (mid, user["id"], now_iso()))
    conn.commit()
    conn.close()
    return back(request, f"/messages/{other}")


@app.post("/messages/{fid}/delete-thread")
async def delete_thread_for_me(request: Request, fid: int):
    """Delete-for-me, whole conversation: upserts the caller's
    watermark for this partner to now, so every message up to this
    moment disappears from the caller's inbox, Sent tab, thread view
    and unread counts. The partner's copy is untouched. A NEW message
    after the watermark revives the thread, showing only what came
    after. Requires an existing conversation. Idempotent — deleting
    again just moves the watermark forward."""
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    exists = conn.execute(
        "SELECT 1 FROM messages WHERE (sender_id=? AND receiver_id=?) "
        "OR (sender_id=? AND receiver_id=?) LIMIT 1",
        (user["id"], fid, fid, user["id"])).fetchone()
    if not exists:
        conn.close()
        return redir("/messages", "Conversation not found.")
    conn.execute(
        "INSERT OR REPLACE INTO thread_deletes(user_id, other_id, "
        "hidden_before) VALUES(?,?,?)",
        (user["id"], fid, now_iso()))
    conn.commit()
    conn.close()
    return redir("/messages", "Conversation deleted.")


# ---------------- voice / video calls (WebRTC, DB-signaled) ----------------
# Voice/video calls run on WebRTC: the media flows directly phone-to-phone
# and never touches or gets stored on this server. The server only runs
# the call state machine (calls table) and passes the small setup messages
# (SDP offer/answer + ICE candidates, call_signals table) between the two
# browsers, which poll for them — the same polling style as the rest of
# HERE, no websockets. A finished call leaves ONE plain system note in the
# pair's message thread, exactly like a phone's call log.
CALL_RING_SECONDS = 45
CALL_SIGNAL_TYPES = ("offer", "answer", "ice", "hangup")
CALL_MAX_PAYLOAD = 65536  # an SDP blob is a few KB; 64KB is generous
CALL_TERMINAL = ("ended", "declined", "missed", "busy")


def _call_label(kind):
    return "Video" if kind == "video" else "Voice"


def _call_icon(kind):
    return "📹" if kind == "video" else "📞"


def _call_duration_text(call):
    """'2:35' from answered_at -> ended_at (or now, while a call is live)."""
    if not call.get("answered_at"):
        return None
    try:
        start = datetime.fromisoformat(str(call["answered_at"]))
        end = (datetime.fromisoformat(str(call["ended_at"]))
               if call.get("ended_at") else datetime.now(timezone.utc))
        secs = max(0, int((end - start).total_seconds()))
    except Exception:
        return None
    return f"{secs // 60}:{secs % 60:02d}"


def _call_note(conn, caller_id, callee_id, text):
    """Drop ONE plain system note into the pair's thread (same messages
    table normal chat uses, so it shows in the inbox like any message)."""
    conn.execute(
        "INSERT INTO messages(sender_id, receiver_id, body, is_read, "
        "created_at) VALUES(?,?,?,0,?)",
        (caller_id, callee_id, text, now_iso()))


def _finish_call(conn, call, status, actor_id=None):
    """Move a live call to a terminal state: stamp ended_at, drop the
    thread note, and leave a hangup signal so the other side's call UI
    stops right away instead of waiting for its next status poll."""
    if call["status"] in CALL_TERMINAL:
        return
    ended = now_iso()
    conn.execute("UPDATE calls SET status=?, ended_at=? WHERE id=?",
                 (status, ended, call["id"]))
    call = dict(call)
    call["status"] = status
    call["ended_at"] = ended
    icon = _call_icon(call["kind"])
    label = _call_label(call["kind"])
    if status == "ended":
        dur = _call_duration_text(call)
        text = f"{icon} {label} call · {dur}" if dur else f"{icon} {label} call"
    elif status == "declined":
        text = f"{icon} Call declined"
    elif status == "missed":
        text = f"{icon} Missed {label.lower()} call"
    else:  # busy
        text = f"{icon} Call busy"
    _call_note(conn, call["caller_id"], call["callee_id"], text)
    conn.execute(
        "INSERT INTO call_signals(call_id, sender_id, type, payload, "
        "created_at) VALUES(?,?,'hangup','',?)",
        (call["id"], actor_id or call["caller_id"], now_iso()))


def _sweep_calls(conn):
    """Lazy ring timeout, run on every call read/start: a call still
    ringing after CALL_RING_SECONDS becomes a missed call (thread note
    plus a bell note for the person who was rung)."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=CALL_RING_SECONDS)).isoformat()
    stale = conn.execute(
        "SELECT * FROM calls WHERE status='ringing' AND created_at<?",
        (cutoff,)).fetchall()
    if not stale:
        return
    missed = []
    for row in stale:
        call = dict(row)
        _finish_call(conn, call, "missed", actor_id=call["caller_id"])
        missed.append(call)
    # Commit BEFORE notify(): it writes through its own connection, and
    # SQLite would refuse it ("database is locked") while this connection
    # still holds the sweep's uncommitted writes.
    conn.commit()
    for call in missed:
        caller = get_user(call["caller_id"])
        if caller:
            notify(call["callee_id"], "call",
                   f"Missed {_call_label(call['kind']).lower()} call "
                   f"from {caller['name']}.",
                   f"/messages/{call['caller_id']}",
                   actor_id=call["caller_id"])


def _live_call_for(conn, uid):
    """The user's current ringing/active call row (dict), or None."""
    r = conn.execute(
        "SELECT * FROM calls WHERE status IN ('ringing','active') AND "
        "(caller_id=? OR callee_id=?) ORDER BY id DESC LIMIT 1",
        (uid, uid)).fetchone()
    return dict(r) if r else None


def _call_json(call, viewer_id):
    other_id = (call["callee_id"] if call["caller_id"] == viewer_id
                else call["caller_id"])
    other = get_user(other_id) or {}
    return {
        "id": call["id"], "kind": call["kind"], "status": call["status"],
        "i_am_caller": call["caller_id"] == viewer_id,
        "created_at": call["created_at"],
        "answered_at": call["answered_at"], "ended_at": call["ended_at"],
        "decline_note": call.get("decline_note") or "",
        "other": {"id": other_id,
                  "name": other.get("name", "Deleted user"),
                  "avatar": avatar_url(other.get("avatar", ""))},
    }


def _participant_call(request, cid):
    """(user, call, err) for the /calls/{cid} endpoints. Only the two
    people on the call may touch it; everyone else gets a 404 so call
    ids can't be probed."""
    user = current_user(request)
    if not user:
        return None, None, JSONResponse({"ok": False, "error": "login"},
                                        status_code=401)
    conn = db()
    _sweep_calls(conn)
    row = conn.execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone()
    conn.close()
    if not row:
        return None, None, JSONResponse({"ok": False, "error": "not found"},
                                        status_code=404)
    call = dict(row)
    if user["id"] not in (call["caller_id"], call["callee_id"]):
        return None, None, JSONResponse({"ok": False, "error": "not found"},
                                        status_code=404)
    return user, call, None


@app.post("/calls/start")
async def call_start(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        target_id = int(data.get("user_id") or 0)
    except (TypeError, ValueError):
        target_id = 0
    kind = data.get("kind") if data.get("kind") in ("voice", "video") else "voice"
    target = get_user(target_id)
    if not target or target_id == user["id"]:
        return JSONResponse({"ok": False, "error": "User not found."},
                            status_code=404)
    if blocked_either_way(user["id"], target_id):
        return JSONResponse(
            {"ok": False, "error": "You can't call this person."},
            status_code=403)
    # Ken's rule: direct calls are friends-only. Anyone else must send a
    # call request and be approved first (see /call-requests).
    if target_id not in friend_id_set(user["id"]):
        return JSONResponse({
            "ok": False, "needs_request": True,
            "error": f"You can only call friends directly. Send "
                     f"{target['name']} a call request — they have to "
                     f"approve it before the call can start."})
    conn = db()
    _sweep_calls(conn)
    if _live_call_for(conn, user["id"]):
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "You are already in a call."},
            status_code=409)
    if _live_call_for(conn, target_id):
        # Busy: record the attempt as a busy call + thread note, the way
        # a phone logs it, and tell the caller straight away.
        stamp = now_iso()
        conn.execute(
            "INSERT INTO calls(caller_id, callee_id, kind, status, "
            "created_at, ended_at) VALUES(?,?,?,'busy',?,?)",
            (user["id"], target_id, kind, stamp, stamp))
        _call_note(conn, user["id"], target_id, f"{_call_icon(kind)} Call busy")
        conn.commit()
        conn.close()
        return JSONResponse({"ok": False, "busy": True,
                             "error": f"{target['name']} is on another call."})
    cur = conn.execute(
        "INSERT INTO calls(caller_id, callee_id, kind, status, created_at) "
        "VALUES(?,?,?,'ringing',?)", (user["id"], target_id, kind, now_iso()))
    cid = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone()
    out = _call_json(dict(row), user["id"])
    conn.close()
    # Buzz the callee's phone too — a ringing call shouldn't have to
    # wait for them to wander past an open HERE tab. Short TTL: a call
    # push that arrives after the ring is worse than none. A callee who
    # silenced the caller gets no buzz (the in-app ring still shows).
    if notifications_allowed(target_id, user["id"]):
        send_push(target_id,
                  f"{_call_icon(kind)} {user['name']} is calling you…",
                  f"Incoming {_call_label(kind).lower()} call — tap to "
                  f"answer.", f"/messages/{user['id']}",
                  tag=f"call-{cid}", ttl=60)
    return JSONResponse({"ok": True, "call": out})


@app.get("/calls/incoming")
async def call_incoming(request: Request):
    """Global poller feed: the ringing call waiting for ME (if any), plus
    MY current live call in either direction, so any page can show the
    incoming sheet or re-attach the call overlay after a navigation."""
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    conn = db()
    _sweep_calls(conn)
    inc = conn.execute(
        "SELECT * FROM calls WHERE callee_id=? AND status='ringing' "
        "ORDER BY id DESC LIMIT 1", (user["id"],)).fetchone()
    current = _live_call_for(conn, user["id"])
    conn.close()
    return JSONResponse({
        "ok": True,
        "incoming": _call_json(dict(inc), user["id"]) if inc else None,
        "current": _call_json(current, user["id"]) if current else None})


@app.get("/calls/{cid}")
async def call_state(request: Request, cid: int):
    user, call, err = _participant_call(request, cid)
    if err:
        return err
    return JSONResponse({"ok": True, "call": _call_json(call, user["id"])})


@app.post("/calls/{cid}/answer")
async def call_answer(request: Request, cid: int):
    user, call, err = _participant_call(request, cid)
    if err:
        return err
    if call["callee_id"] != user["id"] or call["status"] != "ringing":
        return JSONResponse(
            {"ok": False, "error": "This call can't be answered."},
            status_code=409)
    conn = db()
    conn.execute(
        "UPDATE calls SET status='active', answered_at=? WHERE id=?",
        (now_iso(), cid))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.post("/calls/{cid}/decline")
async def call_decline(request: Request, cid: int):
    """Decline a ringing call — callee only. An optional JSON body
    {message: "..."} is the iPhone-style 'reply with text': the text
    lands in the thread as a normal message from the callee AND on the
    call row (decline_note), so the caller's overlay can show
    'Declined · "I'll call you later"' as the call drops. No/empty
    message = the plain decline it has always been."""
    user, call, err = _participant_call(request, cid)
    if err:
        return err
    if call["callee_id"] != user["id"] or call["status"] != "ringing":
        return JSONResponse(
            {"ok": False, "error": "This call can't be declined."},
            status_code=409)
    try:
        data = await request.json()
    except Exception:
        data = {}
    note = (data.get("message") or "").strip()[:300] \
        if isinstance(data, dict) else ""
    conn = db()
    mid = None
    if note:
        conn.execute("UPDATE calls SET decline_note=? WHERE id=?",
                     (note, cid))
        cur = conn.execute(
            "INSERT INTO messages(sender_id, receiver_id, body, "
            "created_at) VALUES(?,?,?,?)",
            (user["id"], call["caller_id"], note, now_iso()))
        mid = cur.lastrowid
    _finish_call(conn, call, "declined", actor_id=user["id"])
    conn.commit()
    conn.close()
    if mid is not None:
        # Same post-send handling a normal message gets (moderation
        # scan + the caller's bell), now that our writes are committed.
        flag_item("message", mid, user["id"], user["name"], note)
        notify(call["caller_id"], "message",
               f"New message from {user['name']}.",
               f"/messages/{user['id']}", actor_id=user["id"])
    return JSONResponse({"ok": True})


@app.post("/calls/{cid}/end")
async def call_end(request: Request, cid: int):
    """Hang up. Works for either side and takes no body, so the call UI
    can fire it from beforeunload with sendBeacon. Idempotent: ending an
    already-finished call is a no-op."""
    user, call, err = _participant_call(request, cid)
    if err:
        return err
    if call["status"] in CALL_TERMINAL:
        return JSONResponse({"ok": True})
    conn = db()
    if call["status"] == "ringing":
        # Caller hanging up before an answer = a missed call for the
        # person being rung; the callee hanging up a ring = a decline.
        if call["caller_id"] == user["id"]:
            _finish_call(conn, call, "missed", actor_id=user["id"])
        else:
            _finish_call(conn, call, "declined", actor_id=user["id"])
    else:  # active
        _finish_call(conn, call, "ended", actor_id=user["id"])
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.post("/calls/{cid}/signal")
async def call_signal_send(request: Request, cid: int):
    user, call, err = _participant_call(request, cid)
    if err:
        return err
    if call["status"] not in ("ringing", "active"):
        return JSONResponse({"ok": False, "error": "Call is over."},
                            status_code=409)
    try:
        data = await request.json()
    except Exception:
        data = {}
    stype = data.get("type")
    if stype not in CALL_SIGNAL_TYPES:
        return JSONResponse({"ok": False, "error": "Bad signal type."},
                            status_code=400)
    payload = data.get("payload")
    if not isinstance(payload, str):
        payload = json.dumps(payload or {})
    if len(payload) > CALL_MAX_PAYLOAD:
        return JSONResponse({"ok": False, "error": "Signal too big."},
                            status_code=400)
    conn = db()
    cur = conn.execute(
        "INSERT INTO call_signals(call_id, sender_id, type, payload, "
        "created_at) VALUES(?,?,?,?,?)",
        (cid, user["id"], stype, payload, now_iso()))
    sid = cur.lastrowid
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "id": sid})


@app.get("/calls/{cid}/signals")
async def call_signals_poll(request: Request, cid: int):
    """Deliver the OTHER side's signals after ?since=<last id seen>, in
    order, plus the call's current status so one poll drives the whole
    call UI (ring timeouts and remote hangups included)."""
    user, call, err = _participant_call(request, cid)
    if err:
        return err
    try:
        since = int(request.query_params.get("since") or 0)
    except (TypeError, ValueError):
        since = 0
    conn = db()
    rows = conn.execute(
        "SELECT * FROM call_signals WHERE call_id=? AND id>? AND "
        "sender_id!=? ORDER BY id ASC LIMIT 200",
        (cid, since, user["id"])).fetchall()
    fresh = conn.execute("SELECT status, decline_note FROM calls WHERE id=?",
                         (cid,)).fetchone()
    conn.close()
    sigs = []
    for r in rows:
        payload = r["payload"]
        try:
            payload = json.loads(payload)
        except Exception:
            pass
        sigs.append({"id": r["id"], "type": r["type"], "payload": payload,
                     "created_at": r["created_at"]})
    return JSONResponse({
        "ok": True,
        "status": fresh["status"] if fresh else call["status"],
        "decline_note": (fresh["decline_note"] or "") if fresh
                        else (call.get("decline_note") or ""),
        "signals": sigs})


# ---------------- call requests (non-friends must be approved) ----------------
# Ken's rule, stated plainly: you can only call people you are friends
# with. Calling anyone else starts here — a request the other person
# must approve; approving creates the real (ringing) call above, and
# from there the normal call flow takes over. A decline locks the
# requester out for 24 hours so requests can't be spammed.
CALL_REQUEST_TTL_HOURS = 24
CALL_REQUEST_COOLDOWN_HOURS = 24


def _sweep_call_requests(conn):
    """Lazy expiry: a pending call request dies after 24 hours."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(hours=CALL_REQUEST_TTL_HOURS)).isoformat()
    conn.execute(
        "UPDATE call_requests SET status='expired' "
        "WHERE status='pending' AND created_at<?", (cutoff,))
    conn.commit()


def _pending_request_between(conn, a, b):
    """The pending call request between a pair (either direction)."""
    r = conn.execute(
        "SELECT * FROM call_requests WHERE status='pending' AND "
        "((requester_id=? AND target_id=?) OR (requester_id=? AND "
        "target_id=?)) ORDER BY id DESC LIMIT 1", (a, b, b, a)).fetchone()
    return dict(r) if r else None


def _call_request_json(req):
    requester = get_user(req["requester_id"]) or {}
    target = get_user(req["target_id"]) or {}
    return {
        "id": req["id"], "kind": req["kind"], "status": req["status"],
        "created_at": req["created_at"],
        "requester": {"id": req["requester_id"],
                      "name": requester.get("name", "Deleted user"),
                      "avatar": avatar_url(requester.get("avatar", ""))},
        "target": {"id": req["target_id"],
                   "name": target.get("name", "Deleted user"),
                   "avatar": avatar_url(target.get("avatar", ""))}}


@app.post("/call-requests")
async def call_request_create(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        target_id = int(data.get("user_id") or 0)
    except (TypeError, ValueError):
        target_id = 0
    kind = data.get("kind") if data.get("kind") in ("voice", "video") else "voice"
    target = get_user(target_id)
    if not target or target_id == user["id"]:
        return JSONResponse({"ok": False, "error": "User not found."},
                            status_code=404)
    if blocked_either_way(user["id"], target_id):
        return JSONResponse(
            {"ok": False, "error": "You can't call this person."},
            status_code=403)
    if target_id in friend_id_set(user["id"]):
        return JSONResponse({
            "ok": False, "friends": True,
            "error": f"You're friends with {target['name']} — just call "
                     f"them directly."})
    conn = db()
    _sweep_call_requests(conn)
    cooldown_cutoff = (datetime.now(timezone.utc)
                       - timedelta(hours=CALL_REQUEST_COOLDOWN_HOURS)).isoformat()
    declined = conn.execute(
        "SELECT id FROM call_requests WHERE requester_id=? AND "
        "target_id=? AND status='declined' AND responded_at>? LIMIT 1",
        (user["id"], target_id, cooldown_cutoff)).fetchone()
    if declined:
        conn.close()
        return JSONResponse({
            "ok": False, "cooldown": True,
            "error": f"{target['name']} declined your call request — you "
                     f"can send another one tomorrow."}, status_code=409)
    dup = conn.execute(
        "SELECT id FROM call_requests WHERE requester_id=? AND "
        "target_id=? AND status='pending' LIMIT 1",
        (user["id"], target_id)).fetchone()
    if dup:
        conn.close()
        return JSONResponse({
            "ok": False,
            "error": f"You already have a call request waiting for "
                     f"{target['name']}."}, status_code=409)
    cur = conn.execute(
        "INSERT INTO call_requests(requester_id, target_id, kind, status, "
        "created_at) VALUES(?,?,?,'pending',?)",
        (user["id"], target_id, kind, now_iso()))
    rid = cur.lastrowid
    # Note in the pair's thread — but only if they already have one, so a
    # request never invents a conversation out of thin air.
    has_thread = conn.execute(
        "SELECT 1 FROM messages WHERE (sender_id=? AND receiver_id=?) OR "
        "(sender_id=? AND receiver_id=?) LIMIT 1",
        (user["id"], target_id, target_id, user["id"])).fetchone()
    if has_thread:
        _call_note(conn, user["id"], target_id,
                   f"{_call_icon(kind)} Call request: {user['name']} wants "
                   f"to {_call_label(kind).lower()} call — waiting for "
                   f"approval")
    conn.commit()
    row = conn.execute("SELECT * FROM call_requests WHERE id=?",
                       (rid,)).fetchone()
    out = _call_request_json(dict(row))
    conn.close()
    notify(target_id, "call_request",
           f"{user['name']} wants to {_call_label(kind).lower()} call "
           f"you. Approve their call request to start the call.",
           f"/messages/{user['id']}", actor_id=user["id"])
    if notifications_allowed(target_id, user["id"]):
        send_push(target_id,
                  f"📞 {user['name']} wants to call you — tap to review",
                  f"{_call_label(kind)} call request — open HERE to "
                  f"approve or decline it.",
                  f"/messages/{user['id']}", tag=f"callreq-{rid}")
    return JSONResponse({"ok": True, "request": out})


@app.get("/call-requests/incoming")
async def call_requests_incoming(request: Request):
    """Pending call requests waiting for ME — feeds the global poller's
    request sheet."""
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    conn = db()
    _sweep_call_requests(conn)
    rows = conn.execute(
        "SELECT * FROM call_requests WHERE target_id=? AND "
        "status='pending' ORDER BY id DESC LIMIT 20",
        (user["id"],)).fetchall()
    conn.close()
    return JSONResponse({"ok": True,
                         "requests": [_call_request_json(dict(r))
                                      for r in rows]})


def _target_request(request, rid):
    """(user, req, err) — only the person the request was sent TO may
    act on it; everyone else gets a 404."""
    user = current_user(request)
    if not user:
        return None, None, JSONResponse({"ok": False, "error": "login"},
                                        status_code=401)
    conn = db()
    _sweep_call_requests(conn)
    row = conn.execute("SELECT * FROM call_requests WHERE id=?",
                       (rid,)).fetchone()
    conn.close()
    if not row or row["target_id"] != user["id"]:
        return None, None, JSONResponse({"ok": False, "error": "not found"},
                                        status_code=404)
    return user, dict(row), None


@app.post("/call-requests/{rid}/approve")
async def call_request_approve(request: Request, rid: int):
    """Approving creates the real call in 'ringing' state (requester =
    caller, approver = callee) and both clients are pulled into the
    normal call UI by their pollers. The request stays usable if one
    side is mid-call — approval just can't start a second call."""
    user, req, err = _target_request(request, rid)
    if err:
        return err
    if req["status"] != "pending":
        msg = ("This call request has expired."
               if req["status"] == "expired"
               else "This call request was already answered.")
        return JSONResponse({"ok": False, "error": msg}, status_code=409)
    conn = db()
    _sweep_calls(conn)
    if _live_call_for(conn, user["id"]):
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "You're on another call right now."},
            status_code=409)
    requester = get_user(req["requester_id"])
    if _live_call_for(conn, req["requester_id"]):
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": f"{requester['name'] if requester else 'They'} is on "
                      f"another call right now."}, status_code=409)
    stamp = now_iso()
    cur = conn.execute(
        "INSERT INTO calls(caller_id, callee_id, kind, status, created_at) "
        "VALUES(?,?,?,'ringing',?)",
        (req["requester_id"], user["id"], req["kind"], stamp))
    cid = cur.lastrowid
    conn.execute(
        "UPDATE call_requests SET status='approved', responded_at=? "
        "WHERE id=?", (stamp, rid))
    conn.commit()
    row = conn.execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone()
    out = _call_json(dict(row), user["id"])
    conn.close()
    return JSONResponse({"ok": True, "call": out})


@app.post("/call-requests/{rid}/decline")
async def call_request_decline(request: Request, rid: int):
    user, req, err = _target_request(request, rid)
    if err:
        return err
    if req["status"] != "pending":
        msg = ("This call request has expired."
               if req["status"] == "expired"
               else "This call request was already answered.")
        return JSONResponse({"ok": False, "error": msg}, status_code=409)
    conn = db()
    conn.execute(
        "UPDATE call_requests SET status='declined', responded_at=? "
        "WHERE id=?", (now_iso(), rid))
    _call_note(conn, user["id"], req["requester_id"],
               f"{_call_icon(req['kind'])} Call request declined")
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


# ---------------- go live (WebRTC, DB-signaled) ----------------
# Ken's Go Live: any user can stream from their phone; friends and the
# public (minus blocks / private-profile rules) watch, comment live,
# and join on camera as part of a panel — the host plus up to 15
# guests (live_members), with per-live admins, kicks/bans, mutes,
# tile order, invites, and likes. The architecture mirrors
# 1:1 calls exactly — media flows phone-to-phone via WebRTC, the DB
# only carries room state, chat, presence, join requests, and the
# small signaling messages (SDP/ICE) browsers poll for. No websockets,
# nothing is recorded or stored server-side.
#
# Topology: PANELISTS send to every watcher (one send peer per
# watcher, the panelist always offers) and are two-way with each
# other: for a panelist pair, whoever sits EARLIER in panel_order
# offers. Watchers are receive-only and answer. Fixed offerer roles
# per pair mean there is never negotiation glare to resolve.
LIVE_STALE_SECONDS = 90        # host heartbeat gap that auto-ends a live
LIVE_MEMBER_STALE_SECONDS = 120  # member heartbeat gap that drops a
# ghost panelist (closed the app, lost signal — iOS often never
# delivers the leave beacon, so presence is the only truth)
LIVE_VIEWER_WINDOW = 45       # viewer counts if seen within this window
LIVE_TITLE_MAX = 80
LIVE_COMMENT_MAX = 300
LIVE_JOIN_COOLDOWN = 60       # seconds after a decline before re-asking
LIVE_SIGNAL_TYPES = ("hello", "offer", "answer", "ice", "bye")
LIVE_MAX_PAYLOAD = 65536      # same SDP headroom as calls
LIVE_PANEL_MAX = 9            # panel size INCL. host: host + up to 8 guests


def _sweep_lives(conn):
    """Lazy stale sweep, run on live reads/writes like _sweep_calls:
    a live whose host stopped heartbeating (closed the tab, lost
    signal) auto-ends so nobody watches a ghost room forever."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=LIVE_STALE_SECONDS)).isoformat()
    stale = conn.execute(
        "SELECT * FROM live_sessions WHERE status='live' AND "
        "COALESCE(is_lobby,0)=0 AND host_seen_at<?",
        (cutoff,)).fetchall()
    for row in stale:
        _end_live_row(conn, dict(row))
    # Panel hygiene: a member who is GONE must not squat on the
    # panel forever — their frozen tile haunts the host and their
    # slot blocks new approvals ('Panel is full'). Presence is
    # the member's viewer heartbeat row: drop an ACTIVE member
    # whose heartbeat row says they left, or whose last
    # heartbeat is older than the member cutoff. A member with
    # NO presence row is NOT touched — the roster row alone is
    # the seat's truth (the panel-cap contract), and every real
    # member's client heartbeats from the live page, so a true
    # ghost always has a row gone stale. The joined_at guard is
    # the join grace: a just-seated member still counts.
    mcut = (datetime.now(timezone.utc)
            - timedelta(seconds=LIVE_MEMBER_STALE_SECONDS)).isoformat()
    ghosts = conn.execute(
        "SELECT m.live_id AS lid, m.user_id AS uid "
        "FROM live_members m "
        "JOIN live_sessions s ON s.id=m.live_id AND s.status='live' "
        "JOIN live_viewers v ON v.live_id=m.live_id "
        "AND v.user_id=m.user_id "
        "WHERE m.left_at IS NULL AND m.joined_at<? AND "
        "(v.left_at IS NOT NULL OR v.last_seen_at<?)",
        (mcut, mcut)).fetchall()
    for g in ghosts:
        lrow = conn.execute(
            "SELECT * FROM live_sessions WHERE id=?",
            (g["lid"],)).fetchone()
        if lrow:
            _drop_guest(conn, dict(lrow), g["uid"])
    # Join asks and invites are perishable. A request the host
    # never settles pinned the asker in a frozen 'Requested' state
    # forever (Ken's stuck-panel report, Oct 28) — and the host's
    # list filled with ghosts. 'expired' is terminal but carries
    # NO decline cooldown: the asker can re-ask the moment they
    # are back and looking.
    _req_cut = (datetime.now(timezone.utc)
                - timedelta(seconds=180)).isoformat()
    conn.execute(
        "UPDATE live_join_requests SET status='expired', "
        "decided_at=? WHERE status='pending' AND kind='request' "
        "AND created_at<?", (now_iso(), _req_cut))
    _inv_cut = (datetime.now(timezone.utc)
                - timedelta(seconds=600)).isoformat()
    conn.execute(
        "UPDATE live_join_requests SET status='expired', "
        "decided_at=? WHERE status='pending' AND kind='invite' "
        "AND created_at<?", (now_iso(), _inv_cut))
    # Build W2: lazy Showdown resolution rides the same sweep —
    # active battles past their clock finish here (the state poll
    # and the lives list both pass through), then any finished
    # battle still missing its result posts/notices gets settled.
    _changed = _battle_expire(conn)
    if _auction_tick(conn):
        _changed = True
    if stale or _changed or ghosts:
        conn.commit()
    _pending_fx = conn.execute(
        "SELECT id FROM live_battles WHERE status='finished' AND "
        "effects_done=0 ORDER BY id ASC LIMIT 5").fetchall()
    for _r in _pending_fx:
        _battle_settle_effects(_r["id"])


def _end_live_row(conn, live):
    """End a live session in place (no commit): pending join requests
    are declined and an on-camera guest is dropped back to watcher, so
    every client's next state poll settles into the ended screen."""
    if live["status"] != "live":
        return
    stamp = now_iso()
    conn.execute(
        "UPDATE live_sessions SET status='ended', ended_at=? WHERE id=?",
        (stamp, live["id"]))
    conn.execute(
        "UPDATE live_join_requests SET status='declined', decided_at=? "
        "WHERE live_id=? AND status='pending'", (stamp, live["id"]))
    conn.execute(
        "UPDATE live_join_requests SET status='left', decided_at=? "
        "WHERE live_id=? AND status='approved'", (stamp, live["id"]))
    # The panel roster and the admin list die with the room: members
    # are marked left and appointments revoked (the rows stay as the
    # room's history, exactly like settled join requests do).
    conn.execute(
        "UPDATE live_members SET left_at=? WHERE live_id=? AND "
        "left_at IS NULL", (stamp, live["id"]))
    conn.execute(
        "UPDATE live_admins SET revoked_at=? WHERE live_id=? AND "
        "revoked_at IS NULL", (stamp, live["id"]))
    # Games pack: an active poll closes with the room.
    conn.execute(
        "UPDATE live_polls SET active=0 WHERE live_id=? AND active=1",
        (live["id"],))
    # Games Pack 2: an active room game ends with the room too.
    conn.execute(
        "UPDATE live_games SET status='ended', ended_at=? "
        "WHERE live_id=? AND status='active'", (stamp, live["id"]))
    # Build W2: a Showdown dies with the room — an ACTIVE battle is
    # finished on the spot as a forfeit win for the other host, and
    # a still-pending challenge involving this room is cancelled.
    _bb = conn.execute(
        "SELECT * FROM live_battles WHERE status='active' AND "
        "(challenger_live_id=? OR opponent_live_id=?)",
        (live["id"], live["id"])).fetchone()
    if _bb:
        _bb = dict(_bb)
        _winner = (_bb["opponent_user_id"]
                   if _bb["challenger_live_id"] == live["id"]
                   else _bb["challenger_user_id"])
        _battle_finish(conn, _bb, "forfeit", winner_user_id=_winner)
    conn.execute(
        "UPDATE live_battles SET status='cancelled', decided_at=? "
        "WHERE status='pending' AND (challenger_live_id=? OR "
        "opponent_live_id=?)", (stamp, live["id"], live["id"]))


def _live_get(conn, lid):
    r = conn.execute("SELECT * FROM live_sessions WHERE id=?",
                     (lid,)).fetchone()
    return dict(r) if r else None


def _live_can_watch(viewer, live, owner_on=False):
    """Who may watch/comment/join a live: the host always; the site
    owner context always; anyone logged in EXCEPT a pair blocked
    either way with the host; a viewer banned from THIS room never
    (a room ban gives the ended-face page and drops the room from
    the home strip, because both go through this gate); and when
    the host's profile is private, only the host's friends (the
    privacy suite's rule)."""
    if not viewer:
        return False
    if viewer["id"] == live["host_id"] or owner_on:
        return True
    _bc = db()
    try:
        if _live_banned(_bc, live["id"], viewer["id"]):
            return False
    finally:
        _bc.close()
    if blocked_either_way(viewer["id"], live["host_id"]):
        return False
    host = get_user(live["host_id"])
    if host and host.get("private_profile"):
        return viewer["id"] in friend_id_set(live["host_id"])
    return True


def _live_guest_req(conn, lid):
    """Legacy helper: the most recently approved join request. The
    panel roster (live_members) is the source of truth for who is
    on camera now; this survives only for pre-upgrade rows and the
    legacy guest-leave fallback paths."""
    r = conn.execute(
        "SELECT * FROM live_join_requests WHERE live_id=? AND "
        "status='approved' ORDER BY id DESC LIMIT 1", (lid,)).fetchone()
    return dict(r) if r else None


# ---------------- live panel: roles, roster, bans ----------------
def _live_banned(conn, lid, uid):
    """True when uid carries a room ban for this live (a 'room'
    kick). Bans are per-live only — they touch nothing site-wide."""
    if not uid:
        return False
    r = conn.execute(
        "SELECT 1 FROM live_bans WHERE live_id=? AND user_id=?",
        (lid, uid)).fetchone()
    return bool(r)


def _live_member_row(conn, lid, uid):
    """The user's ACTIVE panel-member row (left_at NULL), or None.
    The host is never a member — the host is panelist #1 by role."""
    r = conn.execute(
        "SELECT * FROM live_members WHERE live_id=? AND user_id=? "
        "AND left_at IS NULL", (lid, uid)).fetchone()
    return dict(r) if r else None


def _live_active_members(conn, lid):
    """Active panel members (host excluded), in join order."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM live_members WHERE live_id=? AND "
        "left_at IS NULL ORDER BY joined_at ASC, user_id ASC",
        (lid,)).fetchall()]


def _live_is_admin(conn, lid, uid):
    """True when uid holds an ACTIVE admin appointment in this live
    (live_admins row, not revoked). The host is not an "admin" by
    this measure — the host outranks admins and is checked apart."""
    if not uid:
        return False
    r = conn.execute(
        "SELECT 1 FROM live_admins WHERE live_id=? AND user_id=? "
        "AND revoked_at IS NULL", (lid, uid)).fetchone()
    return bool(r)


def _live_role(conn, live, uid):
    """'host' | 'admin' | 'member' | 'watcher' for uid in this live.
    A user can be both member and admin (on camera AND moderating);
    admin is reported because it carries the wider powers, and the
    host trumps everything."""
    if not uid:
        return "watcher"
    if uid == live["host_id"]:
        return "host"
    if _live_is_admin(conn, live["id"], uid):
        return "admin"
    if _live_member_row(conn, live["id"], uid):
        return "member"
    return "watcher"


def _live_stored_panel_order(conn, lid):
    """panel_order as stored (a JSON list of user ids incl. the
    host), or None when unset/unparseable."""
    r = conn.execute(
        "SELECT panel_order FROM live_sessions WHERE id=?",
        (lid,)).fetchone()
    if not r or not r["panel_order"]:
        return None
    try:
        ids = json.loads(r["panel_order"])
    except Exception:
        return None
    if not isinstance(ids, list):
        return None
    out = []
    for v in ids:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def _live_set_panel_order(conn, lid, ids):
    conn.execute(
        "UPDATE live_sessions SET panel_order=? WHERE id=?",
        (json.dumps([int(i) for i in ids]), lid))


def _live_panel_ids(conn, live):
    """Ordered active panelist ids for tile rendering: panel_order
    filtered to the host + active members (stale ids dropped),
    with anyone missing appended at the end. When panel_order is
    missing/stale the sensible order is host first, then members
    by joined_at."""
    lid = live["id"]
    host_id = live["host_id"]
    member_ids = [m["user_id"] for m in _live_active_members(conn, lid)]
    allowed = [host_id] + [m for m in member_ids if m != host_id]
    stored = _live_stored_panel_order(conn, lid)
    if stored is None:
        return allowed
    out = []
    for uid in stored:
        if uid in allowed and uid not in out:
            out.append(uid)
    if host_id not in out:
        out.insert(0, host_id)
    for uid in allowed:
        if uid not in out:
            out.append(uid)
    return out


def _live_panel_size(conn, lid):
    """Panel size = the host + active members."""
    r = conn.execute(
        "SELECT COUNT(*) c FROM live_members WHERE live_id=? AND "
        "left_at IS NULL", (lid,)).fetchone()
    return 1 + (r["c"] if r else 0)


def _live_panel_boxes(live):
    """Build X: the host's guest-square allowance for this live
    (live_sessions.panel_boxes), clamped to 1..8; legacy rows with
    no value read as the default 5. (Platforms rename: this is
    the host's CAST SIZE — how many spots the platform has.)"""
    try:
        n = int(live.get("panel_boxes") or 5)
    except (TypeError, ValueError):
        n = 5
    return max(1, min(8, n))


def _live_spot_names(live):
    """The host's names for the cast spots, in order (guest
    spot 1 first); '' entries are unnamed spots. Stored as a
    JSON list on the live row (spot-names endpoint)."""
    try:
        names = json.loads(live.get("spot_names") or "[]")
    except Exception:
        return []
    if not isinstance(names, list):
        return []
    return [" ".join(str(n).split())[:24] for n in names[:8]]


def _apply_spot_name(conn, live, uid, spot_idx=None):
    """Fill a freshly seated panelist's seat_label from the
    platform's spot names: the spot they applied for when they
    picked one, else the spot matching their seat position.
    Only fills an EMPTY label — a name already set (by the
    host, an admin, or the person) is never overridden."""
    names = _live_spot_names(live)
    if not names:
        return
    name = ""
    if spot_idx is not None and 0 <= spot_idx < len(names):
        name = names[spot_idx]
    if not name:
        ids = _live_panel_ids(conn, live)
        if uid in ids:
            gi = ids.index(uid) - 1
            if 0 <= gi < len(names):
                name = names[gi]
    if not name:
        return
    row = _live_member_row(conn, live["id"], uid)
    if row and not (row.get("seat_label") or ""):
        conn.execute(
            "UPDATE live_members SET seat_label=? "
            "WHERE live_id=? AND user_id=?",
            (name, live["id"], uid))


def _live_add_member(conn, live, uid):
    """Put uid on the panel (no commit). Returns False when the
    panel is already at the LIVE_PANEL_MAX cap; True when uid is
    on the panel afterwards (the host trivially qualifies, and an
    existing member is a no-op that also repairs panel_order)."""
    lid = live["id"]
    if uid == live["host_id"]:
        return True
    if _live_member_row(conn, lid, uid):
        ids = _live_panel_ids(conn, live)
        if uid not in ids:
            ids.append(uid)
            _live_set_panel_order(conn, lid, ids)
        # Build X: a guest on the panel means the panel grid is
        # open (the first approval opens it for everyone).
        conn.execute(
            "UPDATE live_sessions SET panel_open=1 WHERE id=?",
            (lid,))
        return True
    if _live_panel_size(conn, lid) >= LIVE_PANEL_MAX:
        return False
    stamp = now_iso()
    conn.execute(
        "INSERT INTO live_members(live_id, user_id, muted, "
        "joined_at, left_at) VALUES(?,?,0,?,NULL) "
        "ON CONFLICT(live_id, user_id) DO UPDATE SET "
        "left_at=NULL, joined_at=excluded.joined_at, muted=0",
        (lid, uid, stamp))
    ids = _live_panel_ids(conn, live)
    if uid not in ids:
        ids.append(uid)
    _live_set_panel_order(conn, lid, ids)
    # Build X: first guest on the panel opens the grid.
    conn.execute(
        "UPDATE live_sessions SET panel_open=1 WHERE id=?", (lid,))
    return True


def _live_signal_teardown(conn, live, target_id):
    """Leave 'bye' signals between target_id and every other active
    room participant (panelists + active watchers), in BOTH
    directions, so every peer holding a connection to the target
    tears it down at once — _drop_guest's loops, generalized."""
    lid = live["id"]
    others = []
    for uid in _live_panel_ids(conn, live):
        if uid != target_id and uid not in others:
            others.append(uid)
    for v in _live_viewer_rows(conn, lid):
        if v["user_id"] != target_id and v["user_id"] not in others:
            others.append(v["user_id"])
    for other in others:
        _live_signal(conn, lid, target_id, other, "bye")
        _live_signal(conn, lid, other, target_id, "bye")


def _live_remove_member(conn, live, target_id):
    """Take target_id OFF the panel (no commit, no signals): the
    member row is marked left, their approved join/invite rows
    settle to 'left' (so my_join reads sensibly), and panel_order
    drops them."""
    lid = live["id"]
    stamp = now_iso()
    conn.execute(
        "UPDATE live_members SET left_at=? WHERE live_id=? AND "
        "user_id=? AND left_at IS NULL", (stamp, lid, target_id))
    conn.execute(
        "UPDATE live_join_requests SET status='left', decided_at=? "
        "WHERE live_id=? AND user_id=? AND status='approved'",
        (stamp, lid, target_id))
    ids = [i for i in _live_panel_ids(conn, live) if i != target_id]
    _live_set_panel_order(conn, lid, ids)
    if live.get("spotlight_id") == target_id:
        conn.execute(
            "UPDATE live_sessions SET spotlight_id=NULL "
            "WHERE id=?", (lid,))


def _live_parse_body(data):
    return data if isinstance(data, dict) else {}


def _live_body_int(data, key, default=0):
    try:
        return int(data.get(key) or default)
    except (TypeError, ValueError):
        return default


def _live_viewer_rows(conn, lid):
    """Viewers counted right now: heartbeat fresh, not left. The guest
    heartbeats as a viewer too, so they're included; the host isn't."""
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=LIVE_VIEWER_WINDOW)).isoformat()
    return conn.execute(
        "SELECT v.*, u.name, u.avatar FROM live_viewers v "
        "LEFT JOIN users u ON u.id=v.user_id WHERE v.live_id=? AND "
        "v.left_at IS NULL AND v.last_seen_at>=? ORDER BY v.joined_at",
        (lid, cutoff)).fetchall()


def _live_viewer_count(conn, lid):
    return len(_live_viewer_rows(conn, lid))


def _live_signal(conn, live_id, from_id, to_id, kind, payload=""):
    conn.execute(
        "INSERT INTO live_signals(live_id, from_id, to_id, kind, "
        "payload, created_at) VALUES(?,?,?,?,?,?)",
        (live_id, from_id, to_id, kind, payload, now_iso()))


def _drop_guest(conn, live, guest_id):
    """Drop an on-camera panelist back to watcher (no commit):
    'bye' signals to every peer, then the roster settles. Kept
    under its original name — the panel helpers generalize it."""
    _live_signal_teardown(conn, live, guest_id)
    _live_remove_member(conn, live, guest_id)


def _live_access(request, lid, need_live=False):
    """(user, live, err) for the /live/{lid} JSON endpoints. Mirrors
    _participant_call: login first, 404 for unknown ids, and the
    watch-permission rule for everything except bare existence."""
    user = current_user(request)
    if not user:
        return None, None, JSONResponse({"ok": False, "error": "login"},
                                        status_code=401)
    conn = db()
    _sweep_lives(conn)
    live = _live_get(conn, lid)
    conn.close()
    if not live:
        return None, None, JSONResponse({"ok": False,
                                         "error": "not found"},
                                        status_code=404)
    if need_live and live["status"] != "live":
        return None, None, JSONResponse({"ok": False,
                                         "error": "This live has ended."},
                                        status_code=409)
    if not _live_can_watch(user, live, owner_on=is_owner(request)):
        return None, None, JSONResponse(
            {"ok": False, "error": "You can't watch this live."},
            status_code=403)
    return user, live, None


def _live_comment_json(row, viewer_id, host_id, viewer_is_admin=False,
                       rank=None):
    # can_delete: the author always; the host always; and any admin
    # of THIS live (moderation mute/kick powers extend to chat).
    # rank: the author's gifter rank dict (Build P-GIFTS) when the
    # caller computed it — rendered as a chip beside the name.
    out = {
        "id": row["id"], "user_id": row["user_id"],
        "name": row["name"] or "Deleted user",
        "avatar": avatar_url(row["avatar"] or ""),
        "body": row["body"], "created_at": row["created_at"],
        "mine": row["user_id"] == viewer_id,
        "from_host": row["user_id"] == host_id,
        "can_delete": row["user_id"] == viewer_id
                      or viewer_id == host_id
                      or bool(viewer_is_admin)}
    if rank is not None:
        out["sender_rank"] = rank
    out["founding_no"] = row.get("founding_no")
    out["is_plus"] = _is_plus(row)
    return out


def _live_person(uid):
    u = get_user(uid) or {}
    return {"id": uid, "name": u.get("name", "Deleted user"),
            "avatar": avatar_url(u.get("avatar", "")),
            "founding_no": u.get("founding_no"),
            "is_plus": _is_plus(u)}


def live_now_for(viewer_id, owner_on=False):
    """Active lives this viewer may watch — the home 'Live now' strip.
    Blocked pairs and private hosts' non-friends never appear."""
    viewer = get_user(viewer_id)
    if not viewer:
        return []
    conn = db()
    _sweep_lives(conn)
    rows = conn.execute(
        "SELECT * FROM live_sessions WHERE status='live' "
        "ORDER BY created_at DESC").fetchall()
    out = []
    for r in rows:
        live = dict(r)
        if not _live_can_watch(viewer, live, owner_on=owner_on):
            continue
        host = _live_person(live["host_id"])
        host_user = get_user(live["host_id"]) or {}
        lvl = _streamer_level(host_user.get("gift_lifetime"))
        _cat = live.get("category") or (
            "talk" if live.get("is_lobby") else "")
        out.append({"id": live["id"], "host_id": live["host_id"],
                    "name": host["name"], "avatar": host["avatar"],
                    "title": live["title"],
                    "category": _cat,
                    "category_label": PLATFORM_CAT_LABELS.get(
                        _cat, ""),
                    "tags": _live_tags(live),
                    "backdrop": live.get("backdrop") or "",
                    "viewers": _live_viewer_count(conn, live["id"]),
                    "panel_count": 1 + len(_live_active_members(conn, live["id"])),
                    "created_at": live["created_at"],
                    "host_level": lvl["level"],
                    "host_level_label": lvl["label"],
                    "entry_tickets": live.get("entry_tickets") or 0,
                    "is_self": live["host_id"] == viewer_id})
    conn.close()
    return out


def _lives_page_list(viewer, owner_on=False):
    """Lives for the /lives discovery page: live_now_for rows annotated
    with "following" and ordered friends/following first, then viewers
    DESC, then created_at DESC (most recent first)."""
    viewer_id = viewer["id"] if isinstance(viewer, dict) else viewer
    rows = live_now_for(viewer_id, owner_on=owner_on)
    friends = friend_id_set(viewer_id)
    following = following_ids(viewer_id)
    for r in rows:
        r["following"] = False if r.get("is_self") else bool(r["host_id"] in following)
    # Build W1: my pending panel ask per room, so /lives cards can
    # show "🙋 Ask to join panel" vs "⏳ Asked — waiting" truthfully
    # (one query for the whole page, not one per card).
    pend_ids = set()
    if rows:
        conn = db()
        pend_ids = {r2["live_id"] for r2 in conn.execute(
            "SELECT live_id FROM live_join_requests WHERE user_id=? "
            "AND status='pending' AND kind='request'",
            (viewer_id,)).fetchall()}
        conn.close()
    for r in rows:
        r["my_join_pending"] = r["id"] in pend_ids
    # Python stable sorts compose the keys: apply in reverse priority order.
    rows.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    rows.sort(key=lambda r: r.get("viewers") or 0, reverse=True)
    rows.sort(key=lambda r: 0 if r["host_id"] in friends else (1 if r.get("following") else 2))
    return rows


# ---------------- WebRTC ICE config (single source) ----------------
# Both WebRTC clients (1:1 calls in base.html, the live mesh in
# live.html) fetch GET /ice.json and use what it returns for new
# RTCPeerConnections, falling back to their hardcoded copy of these
# same defaults when the fetch fails. HERE_ICE_SERVERS (a JSON array
# of RTCIceServer dicts) overrides the defaults with NO code change —
# the Open Relay entries below are a free community relay with a
# data cap, fine for launch but not a guaranteed relay; swap in
# private TURN credentials via the env var when reliability matters.
DEFAULT_ICE_SERVERS = [
    {"urls": "stun:stun.l.google.com:19302"},
    {"urls": "stun:stun1.l.google.com:19302"},
    {"urls": "stun:openrelay.metered.ca:80"},
    {"urls": "turn:openrelay.metered.ca:80",
     "username": "openrelayproject", "credential": "openrelayproject"},
    {"urls": "turn:openrelay.metered.ca:3478",
     "username": "openrelayproject", "credential": "openrelayproject"},
    {"urls": "turn:openrelay.metered.ca:443",
     "username": "openrelayproject", "credential": "openrelayproject"},
    {"urls": "turn:openrelay.metered.ca:443?transport=tcp",
     "username": "openrelayproject", "credential": "openrelayproject"},
    # TLS TURN (Build W1): the variant that survives strict phone /
    # captive networks that block plain UDP and plain TCP alike —
    # port 443 over TLS looks like ordinary HTTPS to the network.
    {"urls": "turns:openrelay.metered.ca:443?transport=tcp",
     "username": "openrelayproject", "credential": "openrelayproject"},
]


_CF_ICE_CACHE = {"exp": 0.0, "servers": None}


def _cf_turn_fetch(key_id, token):
    """One Cloudflare Realtime call: mint ephemeral TURN
    credentials (24h TTL) via generate-ice-servers. Returns
    the normalized LIST of RTCIceServer dicts (Cloudflare's
    STUN + TURN set), or None on any failure. Kept as its
    own function so the suite can stand in for the
    network."""
    import urllib.request as _ur
    body = json.dumps({"ttl": 86400}).encode()
    req = _ur.Request(
        "https://rtc.live.cloudflare.com/v1/turn/keys/"
        + key_id + "/credentials/generate-ice-servers",
        data=body, method="POST",
        headers={"Authorization": "Bearer " + token,
                 "Content-Type": "application/json",
                 # Cloudflare's edge 403s the default
                 # Python-urllib signature as a bot — the
                 # two-403s-with-fresh-keys lesson.
                 "User-Agent": "Mozilla/5.0 (Macintosh; "
                 "Intel Mac OS X 10_15_7) "
                 "AppleWebKit/537.36 (KHTML, like Gecko) "
                 "Chrome/126.0 Safari/537.36"})
    try:
        with _ur.urlopen(req, timeout=4) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        # Failure class + Cloudflare's own error text (their
        # error payloads carry a code and message, never the
        # token or minted credentials) — never our secrets.
        detail = ""
        try:
            detail = e.read().decode()[:200].replace("\n", " ")
        except Exception:
            pass
        print("CF TURN mint failed:", type(e).__name__,
              getattr(e, "code", ""), detail, flush=True)
        return None
    raw = (data or {}).get("iceServers")
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return None
    out = []
    for srv in raw:
        if isinstance(srv, dict) and srv.get("urls"):
            entry = {"urls": srv["urls"]}
            if srv.get("username"):
                entry["username"] = srv["username"]
            if srv.get("credential"):
                entry["credential"] = srv["credential"]
            out.append(entry)
    return out or None


def _cf_ice_servers():
    """STUN + fresh Cloudflare TURN when the CF keys are in
    the env; cached until an hour before the minted
    credentials expire. None when unconfigured or the mint
    fails — the caller then serves the built-in defaults,
    exactly as before this layer existed."""
    key_id = os.environ.get("HERE_CF_TURN_KEY_ID", "").strip()
    token = os.environ.get("HERE_CF_TURN_TOKEN", "").strip()
    if not key_id or not token:
        return None
    now = time.time()
    if _CF_ICE_CACHE["servers"] \
            and _CF_ICE_CACHE["exp"] > now + 3600:
        return [dict(e) for e in _CF_ICE_CACHE["servers"]]
    try:
        servers = _cf_turn_fetch(key_id, token)
    except Exception:
        servers = None
    if not servers:
        return None
    # Cloudflare's minted set is complete on its own (its
    # STUN entry plus the TURN/turns transports).
    out = [dict(e) for e in servers]
    _CF_ICE_CACHE["servers"] = out
    _CF_ICE_CACHE["exp"] = now + 86400
    return [dict(e) for e in out]


def get_ice_servers():
    """ICE servers for /ice.json, in order: the HERE_ICE_SERVERS
    env override (JSON) when set AND valid; ephemeral Cloudflare
    TURN when its keys are in the env; else the built-in STUN +
    Open Relay defaults."""

    raw = os.environ.get("HERE_ICE_SERVERS", "")
    if raw and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            out = [e for e in parsed
                   if isinstance(e, dict) and e.get("urls")]
            if out:
                return out
    cf = _cf_ice_servers()
    if cf:
        return cf
    return [dict(e) for e in DEFAULT_ICE_SERVERS]


@app.get("/ice.json")
async def ice_json(request: Request):
    """Public on purpose: the STUN/TURN values are the same community
    credentials already shipped in the page templates; the live and
    call clients fetch this before creating peer connections."""
    return JSONResponse({"iceServers": get_ice_servers()})


def _lives_sections(rows):
    """Group /lives rows by PLATFORM CATEGORY (Ken's Platforms
    order Oct 30): one shelf per category present, in shelf
    order, uncategorized platforms last under '🌐 Just
    Chilling'. Rows keep their _lives_page_list order inside a
    shelf; every live lands in exactly one section; empty
    sections are omitted. (Streamer levels still ride the
    cards as chips — they stopped being the organizer.)"""
    groups = [
        (f"cat-{k}", label,
         [r for r in rows if (r.get("category") or "") == k])
        for k, label in PLATFORM_CATEGORIES
    ]
    groups.append(
        ("cat-general", "🌐 Just Chilling",
         [r for r in rows
          if (r.get("category") or "") not in PLATFORM_CAT_LABELS]))
    return [{"key": k, "title": t, "lives": rs}
            for k, t, rs in groups if rs]


@app.get("/lives", response_class=HTMLResponse)
async def lives_page(request: Request):
    # Public window (deterrent fix): strangers may SEE who is
    # live; watching itself still passes the live gate.
    user = current_user(request)
    rows = _lives_page_list(user, is_owner(request))
    cat = (request.query_params.get("cat") or "").strip().lower()
    tag = (request.query_params.get("tag") or "").strip().lower()
    if cat in PLATFORM_CAT_LABELS:
        rows = [r for r in rows
                if (r.get("category") or "") == cat]
    else:
        cat = ""
    if tag:
        rows = [r for r in rows if tag in (r.get("tags") or [])]
    _bconn = db()
    _recent = _recent_showdowns(_bconn)
    _bconn.close()
    return render(request, "lives.html",
                  {"lives": rows, "sections": _lives_sections(rows),
                   "recent_showdowns": _recent,
                   "cats": PLATFORM_CATEGORIES,
                   "cats_json": json.dumps(PLATFORM_CATEGORIES),
                   "active_cat": cat, "active_tag": tag})


@app.get("/lives.json")
async def lives_json(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    rows = _lives_page_list(user, is_owner(request))
    cat = (request.query_params.get("cat") or "").strip().lower()
    tag = (request.query_params.get("tag") or "").strip().lower()
    if cat in PLATFORM_CAT_LABELS:
        rows = [r for r in rows
                if (r.get("category") or "") == cat]
    if tag:
        rows = [r for r in rows if tag in (r.get("tags") or [])]
    return JSONResponse({"ok": True, "lives": [
        {"id": r["id"], "host_id": r["host_id"],
         "host_name": r["name"], "host_avatar": r["avatar"],
         "title": r["title"], "viewers": r["viewers"],
         "category": r.get("category") or "",
         "category_label": r.get("category_label") or "",
         "tags": r.get("tags") or [],
         "panel_count": r["panel_count"], "following": r["following"],
         "my_join_pending": bool(r.get("my_join_pending")),
         "host_level": r.get("host_level") or 1,
         "host_level_label": r.get("host_level_label") or "",
         "is_self": r["is_self"]} for r in rows]})


def founding_seat_counts(conn=None):
    """(taken, left) for the Original 100 — always the REAL
    numbers: seats are genuine accounts only."""
    own = conn is None
    if own:
        conn = db()
    taken = conn.execute(
        "SELECT COUNT(*) c FROM users WHERE founding=1"
    ).fetchone()["c"]
    if own:
        conn.close()
    return taken, max(0, 100 - taken)


def _lobby_captain_id(conn, live):
    """The Lobby runs itself (Ken's top-tier round, Oct 28): while
    the host is NOT in the room, the earliest-seated active
    panelist is the captain — host-lite moderation (mute / kick /
    move / spotlight) until the host walks in or they leave.
    Returns None outside the Lobby or while the host is present."""
    if not live.get("is_lobby"):
        return None
    fresh = (datetime.now(timezone.utc)
             - timedelta(seconds=120)).isoformat()
    if (live.get("host_seen_at") or "") >= fresh:
        return None
    row = conn.execute(
        "SELECT user_id FROM live_members WHERE live_id=? AND "
        "left_at IS NULL AND user_id!=? ORDER BY joined_at ASC, "
        "user_id ASC LIMIT 1",
        (live["id"], live["host_id"])).fetchone()
    return row["user_id"] if row else None


def _is_lobby_captain(conn, live, uid):
    return _lobby_captain_id(conn, live) == uid


BOOST_TICKETS = 100          # 7 days Featured on the Market
BOOST_SECONDS = 7 * 86400
AD_WEEK_CENTS = 1000         # $10 for a 7-day paid ad run
AD_DAYS = 7
PLUS_CENTS = 499             # HERE Plus: $4.99/month
PLUS_VIDEO_MB = 500
PLUS_UPLOAD_MB = 100


def _is_plus(row):
    """True when a user row/dict holds a live HERE Plus sub."""
    if not row:
        return False
    try:
        pu = row["plus_until"]
    except Exception:
        pu = (row.get("plus_until") if isinstance(row, dict)
              else None)
    return bool(pu) and str(pu) > now_iso()


AUCTION_MIN_STEP_CENTS = 50
AUCTION_SNIPE_WINDOW = 10.0
AUCTION_SNIPE_EXTEND = 10.0


def _auction_json(conn, a, viewer_id):
    if not a:
        return None
    leader_name = None
    if a["leader_id"]:
        lu = get_user(a["leader_id"])
        leader_name = lu["name"] if lu else "Someone"
    return {
        "id": a["id"], "title": a["title"], "photo": a["photo"],
        "listing_id": a["listing_id"],
        "start_cents": a["start_cents"],
        "current_cents": a["current_cents"],
        "current": _money(a["current_cents"]),
        "start": _money(a["start_cents"]),
        "leader_id": a["leader_id"], "leader_name": leader_name,
        "seller_id": a["created_by"],
        "ends_at": a["ends_at"], "status": a["status"],
        "mine_leading": a["leader_id"] == viewer_id,
        "next_min_cents": (a["current_cents"]
                           if not a["leader_id"] else
                           a["current_cents"]
                           + AUCTION_MIN_STEP_CENTS),
        "next_min": _money(a["current_cents"]
                           if not a["leader_id"] else
                           a["current_cents"]
                           + AUCTION_MIN_STEP_CENTS)}


def _auction_tick(conn):
    """Lazy auction settlement, riding the live sweep: a running
    auction past its clock ends here. The winner is NOT charged —
    HERE's Market never moves money — the seller and winner are
    notified and connected by a message thread carrying the
    listing snapshot, exactly like a normal Market inquiry.
    Returns True when anything changed."""
    import time as _time
    now = _time.time()
    due = conn.execute(
        "SELECT * FROM live_auctions WHERE status='running' AND "
        "ends_at<=?", (now,)).fetchall()
    changed = False
    for a in due:
        conn.execute(
            "UPDATE live_auctions SET status='ended', settled=1 "
            "WHERE id=?", (a["id"],))
        changed = True
        if not a["leader_id"]:
            continue
        # notify()/_deliver_message() write through their OWN
        # connections — release this connection's write lock
        # first or SQLite deadlocks the settlement.
        conn.commit()
        winner = get_user(a["leader_id"])
        seller = get_user(a["created_by"])
        if not winner or not seller:
            continue
        amt = _money(a["current_cents"])
        notify(a["leader_id"], "auction_won",
               f"🏆 You won the auction for “{a['title']}” at "
               f"{amt}! The seller has your thread — set up "
               f"payment and pickup with them.",
               f"/messages/{a['created_by']}",
               actor_id=a["created_by"])
        notify(a["created_by"], "auction_sold",
               f"🏆 Your auction “{a['title']}” ended — "
               f"{winner['name']} won at {amt}. A thread with "
               f"them is in your inbox.",
               f"/messages/{a['leader_id']}",
               actor_id=a["leader_id"])
        try:
            _deliver_message(
                dict(seller), a["leader_id"],
                f"🏆 You won my live auction for “{a['title']}” "
                f"at {amt}! Message me here to set up payment "
                f"and pickup.",
                listing_id=a["listing_id"],
                listing_title=a["title"],
                listing_price_cents=a["current_cents"])
        except Exception:
            pass
    return changed


LOBBY_HOST_EMAIL = os.environ.get(
    "HERE_LOBBY_HOST", "cooleykendrick7@gmail.com")
LOBBY_TITLE = "\U0001F3E0 The Lobby \u2014 always open"


def _lobby_live(conn):
    """The Lobby: ONE standing live room whose door never closes
    (Ken's pick #2, Oct 28). Hosted by the owner's account; the
    host need not be present — panelists run the room, and the
    sweep never auto-ends it. Created lazily; None when the host
    account does not exist on this deployment."""
    row = conn.execute(
        "SELECT * FROM live_sessions WHERE COALESCE(is_lobby,0)=1 "
        "AND status='live' ORDER BY id DESC LIMIT 1").fetchone()
    if row:
        return dict(row)
    host = conn.execute(
        "SELECT * FROM users WHERE email=?",
        (LOBBY_HOST_EMAIL,)).fetchone()
    if not host:
        return None
    stamp = now_iso()
    cur = conn.execute(
        "INSERT INTO live_sessions(host_id, title, status, "
        "created_at, host_seen_at, like_count, panel_order, "
        "is_lobby, panel_open) VALUES(?,?,'live',?,?,0,?,1,1)",
        (host["id"], LOBBY_TITLE, stamp,
         (datetime.now(timezone.utc)
          - timedelta(days=1)).isoformat(),
         json.dumps([host["id"]])))
    conn.commit()
    return _live_get(conn, cur.lastrowid)


def _lobby_card(conn):
    live = _lobby_live(conn)
    if not live:
        return None
    inside = conn.execute(
        "SELECT COUNT(*) c FROM live_viewers WHERE live_id=? "
        "AND left_at IS NULL AND last_seen_at>?",
        (live["id"], (datetime.now(timezone.utc)
                      - timedelta(seconds=180)).isoformat())
        ).fetchone()["c"]
    return {"id": live["id"], "inside": inside}


# ---------------- paid ads (Money build) ----------------
def _ad_verify_session(user, session_id):
    """Verify a returned Checkout session against Stripe and, if
    it truly paid for THIS user's ad, activate the ad for its
    7-day run. Same discipline as ticket purchases: Stripe is the
    only witness — payment_status must be 'paid', the metadata
    must name this user and a real unpaid ad, and the session id
    is burned onto the ad so it can never activate twice."""
    if not session_id or not stripe_enabled():
        return ""
    conn = db()
    ad = conn.execute(
        "SELECT * FROM ads WHERE paid_session=?",
        (session_id,)).fetchone()
    conn.close()
    if ad and ad["status"] != "unpaid":
        return "Your ad is live 🎉"
    try:
        sess = _stripe_api(
            "GET", f"/v1/checkout/sessions/{session_id}")
    except Exception:
        return ""
    meta = sess.get("metadata") or {}
    if (sess.get("payment_status") != "paid"
            or meta.get("kind") != "ad"
            or str(meta.get("user_id")) != str(user["id"])):
        return ""
    try:
        ad_id = int(meta.get("ad_id") or 0)
    except (TypeError, ValueError):
        return ""
    if not ad_id:
        return ""
    import ads as ads_mod
    start = ads_mod.local_now().date()
    end = start + timedelta(days=AD_DAYS)
    conn = db()
    cur = conn.execute(
        "UPDATE ads SET status='active', starts_on=?, ends_on=?, "
        "paid_session=? WHERE id=? AND creator_id=? AND "
        "status='unpaid'",
        (start.isoformat(), end.isoformat(), session_id, ad_id,
         user["id"]))
    conn.commit()
    conn.close()
    if cur.rowcount == 1:
        return "Your ad is live 🎉 — it runs for 7 days."
    return ""


@app.get("/ads/buy", response_class=HTMLResponse)
async def ads_buy_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    pages = conn.execute(
        "SELECT id, name FROM pages WHERE owner_id=? ORDER BY name",
        (user["id"],)).fetchall()
    conn.close()
    return render(request, "ads_buy.html",
                  {"price": AD_WEEK_CENTS // 100,
                   "stripe_on": stripe_enabled(),
                   "user_pages": [dict(r) for r in pages]})


@app.post("/ads/buy")
async def ads_buy(request: Request):
    """Create the ad as 'unpaid', then hand the buyer a Stripe
    Checkout for the 7-day run. The ad joins the rotation ONLY
    when _ad_verify_session confirms the payment — creating the
    row alone never shows anything to anyone."""
    user, err = require_login(request)
    if err:
        return err
    form = await request.form()
    title = (form.get("title") or "").strip()[:80]
    body = (form.get("body") or "").strip()[:220]
    link = (form.get("link_url") or "").strip()[:500]
    cta = (form.get("cta_label") or "Learn more").strip()[:24] \
        or "Learn more"
    page_id = None
    try:
        page_id = int(form.get("page_id") or 0) or None
    except (TypeError, ValueError):
        page_id = None
    if not title or not body:
        return redir("/ads/buy", "Give your ad a title and a line "
                                 "of text first.")
    import ads as ads_mod
    conn = db()
    ads_mod.ensure_schema(conn)
    if page_id:
        own = conn.execute(
            "SELECT 1 FROM pages WHERE id=? AND owner_id=?",
            (page_id, user["id"])).fetchone()
        if not own:
            page_id = None
    cur = conn.execute(
        "INSERT INTO ads(creator_id, page_id, kind, title, body, "
        "link_url, cta_label, status, created_at) VALUES(?,?,?,?,?,"
        "?,?, 'unpaid', ?)",
        (user["id"], page_id, "paid", title, body, link, cta,
         now_iso()))
    ad_id = cur.lastrowid
    conn.commit()
    conn.close()
    if not stripe_enabled():
        return redir("/ads/mine", "Your ad is saved. Payments "
                                  "aren't open yet — it goes live "
                                  "as soon as they are.")
    base = str(request.base_url).rstrip("/")
    params = {
        "mode": "payment",
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]":
            str(AD_WEEK_CENTS),
        "line_items[0][price_data][product_data][name]":
            "HERE Ad — 7 days",
        "line_items[0][quantity]": "1",
        "metadata[user_id]": str(user["id"]),
        "metadata[kind]": "ad",
        "metadata[ad_id]": str(ad_id),
        "success_url": base + "/ads/mine?session_id="
                              "{CHECKOUT_SESSION_ID}",
        "cancel_url": base + "/ads/mine",
    }
    try:
        sess = _stripe_api("POST", "/v1/checkout/sessions", params)
    except Exception as e:
        return redir("/ads/mine", f"Stripe hiccup: {e}")
    url = sess.get("url")
    if not url:
        return redir("/ads/mine", "Stripe didn't return a checkout "
                                  "link — your ad is saved, pay "
                                  "any time from My Ads.")
    conn = db()
    conn.execute("UPDATE ads SET paid_session=? WHERE id=?",
                 (sess.get("id") or "", ad_id))
    conn.commit()
    conn.close()
    return RedirectResponse(url, status_code=303)


@app.get("/ads/mine", response_class=HTMLResponse)
async def ads_mine(request: Request):
    user, err = require_login(request)
    if err:
        return err
    msg = _ad_verify_session(
        user, (request.query_params.get("session_id") or "").strip())
    import ads as ads_mod
    conn = db()
    ads_mod.ensure_schema(conn)
    rows = conn.execute(
        "SELECT * FROM ads WHERE creator_id=? ORDER BY id DESC",
        (user["id"],)).fetchall()
    conn.close()
    mine = []
    for r in rows:
        d = dict(r)
        d["ctr"] = ads_mod.ctr(d)
        mine.append(d)
    return render(request, "ads_mine.html",
                  {"my_ads": mine, "msg": msg,
                   "price": AD_WEEK_CENTS // 100})


# ---------------- HERE Plus (Money build) ----------------
def _plus_apply_subscription(user_id, sub):
    """Set plus_until + the subscription id from a Stripe
    subscription object (the only writer of both)."""
    end = sub.get("current_period_end")
    if not end:
        return False
    until = datetime.fromtimestamp(
        int(end), tz=timezone.utc).isoformat()
    conn = db()
    conn.execute(
        "UPDATE users SET plus_until=?, stripe_sub_id=? WHERE id=?",
        (until, sub.get("id") or "", user_id))
    conn.commit()
    conn.close()
    return True


def _plus_verify_session(user, session_id):
    """The /plus return path: Stripe is the only witness. The
    session must be paid, name THIS user with kind='plus', and
    carry a subscription that Stripe reports active/trialing —
    only then does Plus switch on."""
    if not session_id or not stripe_enabled():
        return ""
    me = get_user(user["id"]) or {}
    if me.get("stripe_sub_id") and _is_plus(me):
        return "⭐ HERE Plus is on — enjoy!"
    try:
        sess = _stripe_api(
            "GET", f"/v1/checkout/sessions/{session_id}")
    except Exception:
        return ""
    meta = sess.get("metadata") or {}
    if (sess.get("payment_status") != "paid"
            or meta.get("kind") != "plus"
            or str(meta.get("user_id")) != str(user["id"])):
        return ""
    sub_id = sess.get("subscription")
    if not sub_id:
        return ""
    try:
        sub = _stripe_api("GET", f"/v1/subscriptions/{sub_id}")
    except Exception:
        return ""
    if sub.get("status") not in ("active", "trialing"):
        return ""
    if _plus_apply_subscription(user["id"], sub):
        return "⭐ HERE Plus is on — enjoy!"
    return ""


def _plus_refresh(user_row):
    """Lazy renewal check: when a subscriber's period is within
    3 days of ending (or just ended), ask Stripe once and extend
    plus_until to the new period end. A canceled subscription
    simply stops extending — Plus fades at the period end, which
    is exactly what cancel-at-period-end promises."""
    sub_id = (user_row or {}).get("stripe_sub_id")
    if not sub_id or not stripe_enabled():
        return
    pu = (user_row or {}).get("plus_until") or ""
    horizon = (datetime.now(timezone.utc)
               + timedelta(days=3)).isoformat()
    if pu and pu > horizon:
        return
    try:
        sub = _stripe_api("GET", f"/v1/subscriptions/{sub_id}")
    except Exception:
        return
    if sub.get("status") in ("active", "trialing"):
        _plus_apply_subscription(user_row["id"], sub)


@app.get("/plus", response_class=HTMLResponse)
async def plus_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    msg = _plus_verify_session(
        user, (request.query_params.get("session_id") or "").strip())
    me = get_user(user["id"]) or {}
    _plus_refresh(me)
    me = get_user(user["id"]) or {}
    return render(request, "plus.html", {
        "is_plus": _is_plus(me),
        "plus_until": me.get("plus_until") or "",
        "has_sub": bool(me.get("stripe_sub_id")),
        "price": PLUS_CENTS / 100,
        "stripe_on": stripe_enabled(),
        "msg": msg})


@app.post("/plus/checkout")
async def plus_checkout(request: Request):
    user, err = require_login(request)
    if err:
        return err
    if not stripe_enabled():
        return redir("/plus", "Plus payments aren't open yet — "
                              "check back soon.")
    base = str(request.base_url).rstrip("/")
    params = {
        "mode": "subscription",
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": str(PLUS_CENTS),
        "line_items[0][price_data][recurring][interval]": "month",
        "line_items[0][price_data][product_data][name]":
            "HERE Plus",
        "line_items[0][quantity]": "1",
        "metadata[user_id]": str(user["id"]),
        "metadata[kind]": "plus",
        "success_url": base + "/plus?session_id="
                              "{CHECKOUT_SESSION_ID}",
        "cancel_url": base + "/plus",
    }
    try:
        sess = _stripe_api("POST", "/v1/checkout/sessions", params)
    except Exception as e:
        return redir("/plus", f"Stripe hiccup: {e}")
    url = sess.get("url")
    if not url:
        return redir("/plus", "Stripe didn't return a checkout "
                              "link — try again in a moment.")
    return RedirectResponse(url, status_code=303)


@app.post("/plus/cancel")
async def plus_cancel(request: Request):
    """Cancel at period end — Plus stays on until the paid month
    runs out, then fades on its own (nothing more to charge)."""
    user, err = require_login(request)
    if err:
        return err
    me = get_user(user["id"]) or {}
    sub_id = me.get("stripe_sub_id")
    if not sub_id:
        return redir("/plus", "No Plus subscription to cancel.")
    try:
        _stripe_api("POST", f"/v1/subscriptions/{sub_id}",
                    {"cancel_at_period_end": "true"})
    except Exception as e:
        return redir("/plus", f"Stripe hiccup: {e}")
    return redir("/plus", "Done — Plus stays on until the end of "
                          "this paid month, then stops. No more "
                          "charges.")



def _upcoming_shows(conn, user, limit=8):
    """Scheduled shows that are still coming (or live right now via
    their linked live). Reminders ride along for the signed-in
    viewer. A show drops off 3h after its start if it never went
    live; a linked show rides while its live runs."""
    now = time.time()
    rows = conn.execute(
        "SELECT s.*, u.name AS host_name, u.avatar AS host_avatar, "
        "u.founding_no AS host_founding FROM live_schedule s "
        "JOIN users u ON u.id = s.host_id "
        "WHERE (s.live_id IS NULL AND s.starts_at > ?) "
        "OR s.live_id IN (SELECT id FROM live_sessions "
        "WHERE ended_at IS NULL) "
        "ORDER BY s.starts_at ASC LIMIT ?",
        (now - 3 * 3600, limit)).fetchall()
    out = []
    mine = set()
    if user:
        mine = {r["sched_id"] for r in conn.execute(
            "SELECT sched_id FROM live_reminders WHERE user_id=?",
            (user["id"],)).fetchall()}
    for r in rows:
        d = dict(r)
        d["reminded"] = r["id"] in mine
        d["is_live"] = bool(r["live_id"])
        d["starts_label"] = datetime.fromtimestamp(
            r["starts_at"], _chicago_now().tzinfo).strftime(
                "%a %b %d · %I:%M %p")
        d["remind_count"] = conn.execute(
            "SELECT COUNT(*) c FROM live_reminders WHERE sched_id=?",
            (r["id"],)).fetchone()["c"]
        out.append(d)
    return out


def _link_schedule_for_live(host, lid):
    """When a host goes live near a scheduled show's start, link the
    two and ring every reminder subscriber (bell + push)."""
    now = time.time()
    conn = db()
    row = conn.execute(
        "SELECT * FROM live_schedule WHERE host_id=? "
        "AND live_id IS NULL AND starts_at BETWEEN ? AND ? "
        "ORDER BY starts_at ASC LIMIT 1",
        (host["id"], now - 3 * 3600, now + 45 * 60)).fetchone()
    subs, title_txt = [], ""
    if row:
        conn.execute("UPDATE live_schedule SET live_id=? WHERE id=?",
                     (lid, row["id"]))
        subs = [r["user_id"] for r in conn.execute(
            "SELECT user_id FROM live_reminders WHERE sched_id=?",
            (row["id"],)).fetchall()]
        title_txt = row["title"]
        conn.commit()
    conn.close()
    if not row:
        return
    text = (f"\U0001f514 {host['name']} is LIVE — the show you "
            f"asked about: {title_txt}")
    for uid in subs:
        if uid == host["id"]:
            continue
        notify(uid, "show_start", text, f"/live/{lid}",
               actor_id=host["id"])
        if notifications_allowed(uid, host["id"]):
            send_push(uid, "\U0001f514 The show is starting",
                      title_txt or "Tap to watch now.",
                      f"/live/{lid}", tag=f"show-{row['id']}",
                      ttl=3600)


def _creator_quest(conn, user):
    """The First $10 Quest: the plain path from brand-new account to
    first cash-out, computed from real state. Shown on Earnings and
    the Wallet until every step is done."""
    uid = user["id"]

    def _n(sql):
        return conn.execute(sql, (uid,)).fetchone()["c"]

    steps = [
        {"key": "creator", "label": "Turn on Creator Mode",
         "done": bool(user["creator_mode"]), "href": "/settings"},
        {"key": "avatar", "label": "Add a profile picture",
         "done": bool((user["avatar"] or "").strip()),
         "href": f"/user/{uid}"},
        {"key": "post", "label": "Make your first post",
         "done": _n("SELECT COUNT(*) c FROM posts WHERE user_id=?") > 0,
         "href": "/home"},
        {"key": "live", "label": "Go live one time",
         "done": _n("SELECT COUNT(*) c FROM live_sessions "
                    "WHERE host_id=?") > 0, "href": "/home"},
        {"key": "gift", "label": "Receive your first gift",
         "done": (user["gift_lifetime"] or 0) > 0, "href": "/earnings"},
        {"key": "bank", "label": "Connect your bank for payouts",
         "done": bool(user["payout_ready"]), "href": "/tickets"},
        {"key": "cashout", "label": "Cash out your first $10",
         "done": _n("SELECT COUNT(*) c FROM payout_requests "
                    "WHERE user_id=?") > 0, "href": "/tickets"},
    ]
    done = sum(1 for s in steps if s["done"])
    return {"steps": steps, "done": done, "total": len(steps),
            "complete": done == len(steps)}


@app.get("/paidwall", response_class=HTMLResponse)
async def paidwall_page(request: Request):
    """The Wall of Paid: public proof that HERE pays its creators —
    every paid cash-out, newest first, plus the all-time total."""
    user = current_user(request)
    conn = db()
    total = _paid_total_cents(conn)
    creators = conn.execute(
        "SELECT COUNT(DISTINCT user_id) c FROM payout_requests "
        "WHERE status='paid'").fetchone()["c"]
    rows = conn.execute(
        "SELECT pr.dollars_cents, pr.tickets, pr.decided_at, "
        "pr.created_at, u.name, u.avatar, u.founding_no "
        "FROM payout_requests pr JOIN users u ON u.id = pr.user_id "
        "WHERE pr.status='paid' ORDER BY "
        "COALESCE(pr.decided_at, pr.created_at) DESC LIMIT 50"
        ).fetchall()
    # Money in motion (deterrent fix): while the wall itself
    # is empty, the page proves the economy is real with live
    # aggregates — tickets sold, fan dollars in, and what
    # creators have already banked toward cash-out.
    _sales_n, _gross_c, _fee_c = _buy_aggregates(conn)
    sold_tickets = conn.execute(
        "SELECT COALESCE(SUM(tickets),0) s FROM ticket_txns "
        "WHERE kind='buy'").fetchone()["s"]
    owed_tickets = conn.execute(
        "SELECT COALESCE(SUM(gift_earned),0) s FROM users"
        ).fetchone()["s"]
    earning = conn.execute(
        "SELECT COUNT(*) c FROM users WHERE gift_earned > 0"
        ).fetchone()["c"]
    conn.close()
    wall = [{"name": r["name"], "avatar": avatar_url(r["avatar"]),
             "founding_no": r["founding_no"],
             "dollars": _money(r["dollars_cents"]),
             "tickets": r["tickets"],
             "when": (r["decided_at"] or r["created_at"] or "")[:10]}
            for r in rows]
    return render(request, "paidwall.html",
                  {"wall": wall, "total_cents": total,
                   "total_dollars": _money(total),
                   "creators": creators,
                   "sold_tickets": sold_tickets,
                   "sold_dollars": _money(_gross_c),
                   "owed_dollars": _money(
                       _earned_dollars(owed_tickets)),
                   "earning": earning})


@app.get("/paid/{token}", response_class=HTMLResponse)
async def paid_card_page(request: Request, token: str):
    """One creator's public 'I got paid' card. The token is minted
    by the creator from their own paid request; nothing sensitive
    rides here — name, amount and date are the whole point."""
    user = current_user(request)
    conn = db()
    pr = conn.execute(
        "SELECT pr.dollars_cents, pr.tickets, pr.decided_at, "
        "pr.created_at, u.name, u.avatar, u.founding_no "
        "FROM payout_requests pr JOIN users u ON u.id = pr.user_id "
        "WHERE pr.share_token=? AND pr.status='paid'",
        (token,)).fetchone()
    conn.close()
    card = None
    if pr:
        card = {"name": pr["name"], "avatar": pr["avatar"],
                "founding_no": pr["founding_no"],
                "dollars": _money(pr["dollars_cents"]),
                "tickets": pr["tickets"],
                "when": (pr["decided_at"] or pr["created_at"]
                         or "")[:10],
                "url": f"https://joincomehere.com/paid/{token}"}
    return render(request, "paid_card.html", {"card": card})


@app.post("/tickets/payout/{prid}/share")
async def payout_share(request: Request, prid: int):
    """Mint (or reuse) the share token for MY OWN paid cash-out and
    land on the public card."""
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    pr = conn.execute(
        "SELECT * FROM payout_requests WHERE id=? AND user_id=?",
        (prid, user["id"])).fetchone()
    if not pr or pr["status"] != "paid":
        conn.close()
        return redir("/tickets", "That payout can't be shared.")
    token = pr["share_token"]
    if not token:
        token = secrets.token_urlsafe(9)
        conn.execute("UPDATE payout_requests SET share_token=? "
                     "WHERE id=?", (token, prid))
        conn.commit()
    conn.close()
    return redir(f"/paid/{token}")


@app.post("/settings/tip-goal")
async def tip_goal_set(request: Request):
    """Set or clear my profile tip goal. The bar counts gifts from
    NOW (base = current gift_lifetime), across every gift kind."""
    user, err = require_login(request)
    if err:
        return err
    form = await request.form()
    label = str(form.get("label", "")).strip()[:60]
    try:
        goal = int(form.get("tickets") or 0)
    except (TypeError, ValueError):
        goal = 0
    goal = max(0, min(goal, 1000000))
    conn = db()
    me = conn.execute("SELECT gift_lifetime FROM users WHERE id=?",
                      (user["id"],)).fetchone()
    conn.execute(
        "UPDATE users SET tip_goal_label=?, tip_goal_tickets=?, "
        "tip_goal_base=? WHERE id=?",
        (label, goal, (me["gift_lifetime"] or 0) if goal else 0,
         user["id"]))
    conn.commit()
    conn.close()
    return redir(f"/user/{user['id']}",
                  "Tip goal saved \U0001f3af" if goal else
                  "Tip goal cleared.")


@app.get("/shows", response_class=HTMLResponse)
async def shows_page(request: Request):
    """Scheduled shows: what's coming up, plus the host's form."""
    user = current_user(request)
    conn = db()
    shows = _upcoming_shows(conn, user, limit=30)
    mine = []
    if user:
        mine = [s for s in shows if s["host_id"] == user["id"]]
    conn.close()
    return render(request, "shows.html",
                  {"shows": shows, "mine": mine})


@app.post("/live/schedule")
async def live_schedule_create(request: Request):
    """Schedule a show. Same money rule as going live: a ticket
    door needs Creator Mode."""
    user, err = require_login(request)
    if err:
        return err
    form = await request.form()
    title = str(form.get("title", "")).strip()[:120]
    raw = str(form.get("starts_at", "")).strip()
    try:
        entry = int(form.get("entry_tickets") or 0)
    except (TypeError, ValueError):
        entry = 0
    entry = max(0, min(entry, 100000))
    if entry > 0 and not user["creator_mode"]:
        return redir("/shows", "Turn on Creator Mode to charge a "
                               "ticket door.")
    if not title:
        return redir("/shows", "Give your show a title first.")
    try:
        dt = datetime.strptime(raw, "%Y-%m-%dT%H:%M")
    except ValueError:
        return redir("/shows", "Pick a day and time for the show.")
    dt = dt.replace(tzinfo=_chicago_now().tzinfo)
    ts = dt.timestamp()
    now = time.time()
    if ts < now - 600 or ts > now + 30 * 86400:
        return redir("/shows", "Pick a time in the next 30 days.")
    conn = db()
    conn.execute(
        "INSERT INTO live_schedule(host_id, title, starts_at, "
        "entry_tickets, created_at) VALUES(?,?,?,?,?)",
        (user["id"], title, ts, entry, now_iso()))
    conn.commit()
    conn.close()
    return redir("/shows", "Show scheduled \u23f0")


@app.post("/live/schedule/{sid}/remind")
async def live_schedule_remind(request: Request, sid: int):
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False}, status_code=401)
    conn = db()
    has = conn.execute(
        "SELECT 1 FROM live_reminders WHERE sched_id=? AND "
        "user_id=?", (sid, user["id"])).fetchone()
    if has:
        conn.execute("DELETE FROM live_reminders WHERE sched_id=? "
                     "AND user_id=?", (sid, user["id"]))
        reminded = False
    else:
        conn.execute("INSERT OR IGNORE INTO live_reminders"
                     "(sched_id, user_id, created_at) VALUES(?,?,?)",
                     (sid, user["id"], now_iso()))
        reminded = True
    count = conn.execute(
        "SELECT COUNT(*) c FROM live_reminders WHERE sched_id=?",
        (sid,)).fetchone()["c"]
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "reminded": reminded,
                         "count": count})


@app.post("/live/schedule/{sid}/cancel")
async def live_schedule_cancel(request: Request, sid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    row = conn.execute("SELECT * FROM live_schedule WHERE id=?",
                       (sid,)).fetchone()
    if row and row["host_id"] == user["id"] and not row["live_id"]:
        conn.execute("DELETE FROM live_reminders WHERE sched_id=?",
                     (sid,))
        conn.execute("DELETE FROM live_schedule WHERE id=?", (sid,))
        conn.commit()
    conn.close()
    return redir("/shows", "Show cancelled.")


@app.get("/top", response_class=HTMLResponse)
async def top_page(request: Request):
    """The Top Boards: this week's (Chicago Mon-Sun) top earners,
    top gifters and top live earners, from the ticket ledger."""
    user, err = require_login(request)
    if err:
        return err
    start, end = _week_window_utc()
    s0, s1 = start.isoformat(), end.isoformat()
    conn = db()

    def board(where, expr):
        rows = conn.execute(
            "SELECT t.user_id, SUM(" + expr + ") AS tot, u.name, "
            "u.avatar, u.founding_no, u.plus_until FROM ticket_txns t "
            "JOIN users u ON u.id = t.user_id WHERE " + where +
            " AND t.created_at>=? AND t.created_at<=? "
            "GROUP BY t.user_id ORDER BY tot DESC LIMIT 10",
            (s0, s1)).fetchall()
        return [{"id": r["user_id"], "name": r["name"],
                 "avatar": avatar_url(r["avatar"]),
                 "founding_no": r["founding_no"],
                 "is_plus": _is_plus(r), "tot": int(r["tot"] or 0)}
                for r in rows]

    earners = board("t.kind IN ('gift_receive','entry_receive',"
                    "'unlock_receive')", "t.tickets")
    gifters = board("t.kind='gift_send'", "-t.tickets")
    streamers = board("t.kind='gift_receive' AND t.live_id IS NOT "
                      "NULL", "t.tickets")
    conn.close()
    return render(request, "top.html",
                  {"earners": earners, "gifters": gifters,
                   "streamers": streamers})


@app.get("/user/{uid}/shop", response_class=HTMLResponse)
async def user_shop_page(request: Request, uid: int):
    """A member's full Market shop: every active listing, boosted
    first — the profile's Selling section, opened all the way up."""
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    seller = conn.execute("SELECT * FROM users WHERE id=?",
                          (uid,)).fetchone()
    if not seller or blocked_either_way(user["id"], uid):
        conn.close()
        return redir("/market", "That shop isn't available.")
    rows = conn.execute(
        "SELECT l.*, u.name AS sname, u.avatar AS savatar, u.is_verified_vendor AS svvendor "
        "FROM market_listings l JOIN users u ON u.id=l.seller_id "
        "WHERE l.seller_id=? AND l.status='active' ORDER BY "
        "CASE WHEN l.boost_until > strftime('%s','now') THEN 0 "
        "ELSE 1 END, l.created_at DESC", (uid,)).fetchall()
    listings = [_listing_card(r) for r in rows]
    conn.close()
    return render(request, "shop.html",
                  {"shop_name": f"{seller['name']}'s Shop",
                   "shop_avatar": seller["avatar"],
                   "listings": listings})


@app.get("/page/{pid}/shop", response_class=HTMLResponse)
async def page_shop_page(request: Request, pid: int):
    """A Page's shop: the listings of the member who runs the Page,
    presented under the Page's name."""
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    pg = conn.execute("SELECT * FROM pages WHERE id=?",
                      (pid,)).fetchone()
    if not pg:
        conn.close()
        return redir("/pages", "Page not found.")
    rows = conn.execute(
        "SELECT l.*, u.name AS sname, u.avatar AS savatar, u.is_verified_vendor AS svvendor "
        "FROM market_listings l JOIN users u ON u.id=l.seller_id "
        "WHERE l.seller_id=? AND l.status='active' ORDER BY "
        "CASE WHEN l.boost_until > strftime('%s','now') THEN 0 "
        "ELSE 1 END, l.created_at DESC",
        (pg["owner_id"],)).fetchall()
    listings = [_listing_card(r) for r in rows]
    conn.close()
    return render(request, "shop.html",
                  {"shop_name": f"{pg['name']} — Shop",
                   "shop_avatar": pg["avatar"]
                   if "avatar" in pg.keys() else "",
                   "listings": listings,
                   "back_url": f"/page/{pid}"})


# ---------------- The $100 Invite Race (Ken, Oct 29) --------
# "Sometimes you gotta pay to play." Whoever brings the most
# REAL joins wins $100 cash; 2nd gets 1,000 tickets, 3rd 500 —
# but a join only counts when the invited person actually uses
# HERE (posts, joins a live, or touches tickets). Raw signups
# are displayed for honesty but never decide the money: that is
# what keeps the prize safe from fake-account farming. Minimum
# 20 qualified joins to take the cash. Ken reviews the winner
# in /owner/race before a dollar moves — his call, always.
RACE_MIN_QUALIFIED = 20


def _race_window():
    start = datetime(2026, 10, 30, 0, 0,
                     tzinfo=CHICAGO_TZ).astimezone(
                         timezone.utc).isoformat()
    end = datetime(2026, 11, 15, 23, 59, 59,
                   tzinfo=CHICAGO_TZ).astimezone(
                       timezone.utc).isoformat()
    return start, end


def _race_phase():
    s, e = _race_window()
    n = now_iso()
    return "before" if n < s else ("after" if n > e else "open")


_QUALIFIED_SQL = (
    "(EXISTS(SELECT 1 FROM posts p WHERE p.user_id=u.id) "
    "OR EXISTS(SELECT 1 FROM live_viewers v WHERE v.user_id=u.id) "
    "OR EXISTS(SELECT 1 FROM ticket_txns t WHERE t.user_id=u.id))")


def _race_board(conn, limit=20):
    s, e = _race_window()
    rows = conn.execute(
        "SELECT u.invited_by AS inv, COUNT(*) AS raw, "
        "SUM(CASE WHEN " + _QUALIFIED_SQL + " THEN 1 ELSE 0 END) "
        "AS qualified, MAX(u.created_at) AS last_join "
        "FROM users u WHERE u.invited_by IS NOT NULL "
        "AND u.invited_by != u.id AND u.created_at >= ? "
        "AND u.created_at <= ? GROUP BY u.invited_by "
        "ORDER BY qualified DESC, raw DESC, last_join ASC",
        (s, e)).fetchall()
    out = []
    for r in rows[:limit]:
        inv = conn.execute(
            "SELECT id, name, avatar, founding_no, plus_until "
            "FROM users WHERE id=?", (r["inv"],)).fetchone()
        if not inv:
            continue
        out.append({
            "user_id": inv["id"], "name": inv["name"],
            "avatar": inv["avatar"],
            "founding_no": inv["founding_no"],
            "is_plus": _is_plus(inv),
            "raw": r["raw"], "qualified": r["qualified"] or 0,
            "pct": min(100, int(100 * (r["qualified"] or 0)
                                / RACE_MIN_QUALIFIED))})
    return out


def _race_mine(conn, uid):
    s, e = _race_window()
    r = conn.execute(
        "SELECT COUNT(*) AS raw, SUM(CASE WHEN " + _QUALIFIED_SQL
        + " THEN 1 ELSE 0 END) AS q FROM users u "
        "WHERE u.invited_by=? AND u.created_at>=? "
        "AND u.created_at<=?", (uid, s, e)).fetchone()
    return {"raw": r["raw"] or 0, "qualified": r["q"] or 0}


def _race_invitees(conn, inviter_id):
    """The owner's truth view: who this inviter actually brought,
    what each one DID, and when they were last alive on HERE."""
    s, e = _race_window()
    rows = conn.execute(
        "SELECT id, name, avatar, created_at FROM users "
        "WHERE invited_by=? AND created_at>=? AND created_at<=? "
        "ORDER BY created_at", (inviter_id, s, e)).fetchall()
    out = []
    for u in rows:
        posts = conn.execute(
            "SELECT COUNT(*) c, MAX(created_at) m FROM posts "
            "WHERE user_id=?", (u["id"],)).fetchone()
        lv = conn.execute(
            "SELECT COUNT(*) c, MAX(last_seen_at) m FROM "
            "live_viewers WHERE user_id=?", (u["id"],)).fetchone()
        tx = conn.execute(
            "SELECT COUNT(*) c, MAX(created_at) m FROM ticket_txns "
            "WHERE user_id=?", (u["id"],)).fetchone()
        stamps = [x for x in (u["created_at"], posts["m"],
                              lv["m"], tx["m"]) if x]
        out.append({
            "id": u["id"], "name": u["name"],
            "avatar": avatar_url(u["avatar"]),
            "joined": u["created_at"],
            "posts": posts["c"], "lives": lv["c"],
            "txns": tx["c"],
            "qualified": bool(posts["c"] or lv["c"] or tx["c"]),
            "last_active": max(stamps) if stamps else ""})
    return out


@app.get("/invite-race", response_class=HTMLResponse)
async def invite_race_page(request: Request):
    user = current_user(request)
    conn = db()
    board = _race_board(conn)
    mine = _race_mine(conn, user["id"]) if user else None
    conn.close()
    return render(request, "race.html", {
        "board": board, "mine": mine,
        "phase": _race_phase(), "min_qualified": RACE_MIN_QUALIFIED,
        "user": user})


@app.get("/owner/race", response_class=HTMLResponse)
async def owner_race_page(request: Request):
    err = require_owner(request)
    if err:
        return err
    conn = db()
    board = _race_board(conn, limit=25)
    drill = [{"inviter": b, "invitees": _race_invitees(conn,
              b["user_id"])} for b in board[:10]]
    conn.close()
    return render(request, "owner/race.html", {
        "board": board, "drill": drill, "phase": _race_phase(),
        "min_qualified": RACE_MIN_QUALIFIED})


SUGGESTION_STATUS_LABELS = {
    "new": "⏳ In review",
    "approved": "✅ Approved — on the build list",
    "built": "🚀 Shipped!",
    "passed": "🙏 Passed for now",
}


@app.post("/settings/suggest")
async def settings_suggest(request: Request):
    user, err = require_login(request)
    if err:
        return err
    form = await request.form()
    text = str(form.get("text") or "").strip()
    if len(text) < 5 or len(text) > 600:
        return RedirectResponse(
            "/settings/profile?idea=bad#ideas", status_code=303)
    conn = db()
    today = now_iso()[:10]
    n = conn.execute(
        "SELECT COUNT(*) c FROM app_suggestions WHERE user_id=? "
        "AND substr(created_at,1,10)=?", (user["id"], today)
    ).fetchone()["c"]
    if n >= 5:
        conn.close()
        return RedirectResponse(
            "/settings/profile?idea=many#ideas", status_code=303)
    conn.execute(
        "INSERT INTO app_suggestions(user_id, text, status, "
        "created_at) VALUES(?,?,'new',?)",
        (user["id"], text, now_iso()))
    conn.commit()
    conn.close()
    return RedirectResponse(
        "/settings/profile?idea=sent#ideas", status_code=303)


@app.get("/owner/suggestions", response_class=HTMLResponse)
async def owner_suggestions_page(request: Request):
    err = require_owner(request)
    if err:
        return err
    conn = db()
    rows = conn.execute(
        "SELECT s.*, u.name AS uname, u.avatar AS uavatar "
        "FROM app_suggestions s JOIN users u ON u.id=s.user_id "
        "ORDER BY CASE s.status WHEN 'new' THEN 0 "
        "WHEN 'approved' THEN 1 WHEN 'built' THEN 2 ELSE 3 END, "
        "s.id DESC LIMIT 200").fetchall()
    conn.close()
    return render(request, "owner/suggestions.html",
                  {"suggestions": [dict(r) for r in rows],
                   "labels": SUGGESTION_STATUS_LABELS})


@app.post("/owner/suggestions/{sid}/status")
async def owner_suggestion_status(request: Request, sid: int):
    err = require_owner(request)
    if err:
        return err
    form = await request.form()
    status = str(form.get("status") or "")
    if status not in SUGGESTION_STATUS_LABELS:
        if _oa(request):
            return _oa_fail("That suggestion status isn't valid.")
        return RedirectResponse("/owner/suggestions",
                                status_code=303)
    conn = db()
    row = conn.execute(
        "SELECT * FROM app_suggestions WHERE id=?",
        (sid,)).fetchone()
    if row:
        conn.execute(
            "UPDATE app_suggestions SET status=?, decided_at=? "
            "WHERE id=?", (status, now_iso(), sid))
        conn.commit()
        if status == "built" and row["status"] != "built":
            notify(row["user_id"], "suggestion",
                   "💡 Your HERE idea shipped: “"
                   + row["text"][:80] + "” — you helped build "
                   "this place. Thank you!",
                   "/settings/profile#ideas")
    conn.close()
    if _oa(request):
        return _oa_json(
            "Suggestion marked: "
            + SUGGESTION_STATUS_LABELS[status],
            status=status, label=SUGGESTION_STATUS_LABELS[status])
    return RedirectResponse("/owner/suggestions", status_code=303)


@app.get("/founders", response_class=HTMLResponse)
async def founders_page(request: Request):
    """The Original 100 wall: the first hundred real accounts,
    numbered by join order, with the still-open seats showing —
    scarcity you can count, because every seat is a real person."""
    # Public window (deterrent fix): the wall is a showroom
    # piece — strangers count the seats before joining.
    user = current_user(request)
    conn = db()
    rows = conn.execute(
        "SELECT id, name, avatar, founding_no, created_at FROM "
        "users WHERE founding=1 ORDER BY founding_no ASC").fetchall()
    taken, left = founding_seat_counts(conn)
    conn.close()
    founders = [{"id": r["id"], "name": r["name"],
                 "avatar": avatar_url(r["avatar"] or ""),
                 "no": r["founding_no"],
                 "joined": fancy_time(r["created_at"])}
                for r in rows]
    return render(request, "founders.html",
                  {"founders": founders, "taken": taken,
                   "left": left})


@app.get("/lobby")
async def lobby_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    live = _lobby_live(conn)
    conn.close()
    if not live:
        return redir("/home", "The Lobby isn't open yet.")
    return redir(f"/live/{live['id']}")


# ---------------- platforms (Ken's order Oct 30, 2026) -------
# A live is the host's own PLATFORM: it carries a category and
# hashtags so people find where they fit, and a backdrop image
# that brands the room. Categories are a fixed shelf; '' is
# the uncategorized "Just Chilling" shelf.
PLATFORM_CATEGORIES = [
    ("games", "🎮 Games"),
    ("talk", "🎙️ Talk & Podcasts"),
    ("music", "🎵 Music"),
    ("money", "💰 Money & Business"),
    ("cars", "🚗 Cars"),
    ("beauty", "💇 Hair & Beauty"),
    ("sports", "⚽ Sports"),
    ("food", "🍳 Food"),
    ("comedy", "😂 Comedy"),
    ("learning", "📚 Learning"),
    ("faith", "🙏 Faith & Life"),
    ("news", "🌍 World & News"),
]
PLATFORM_CAT_LABELS = dict(PLATFORM_CATEGORIES)


def _platform_tags(raw):
    """Normalize host-typed platform tags: split on spaces and
    commas, strip '#', lowercase, keep [a-z0-9_], dedupe,
    max 5 tags of max 24 chars."""
    out = []
    for piece in re.split(r"[\s,]+", str(raw or "")):
        t = re.sub(r"[^a-z0-9_]", "", piece.strip().lstrip("#").lower())
        if t and t not in out:
            out.append(t[:24])
        if len(out) >= 5:
            break
    return out


def _live_tags(live):
    """A live row's stored tags (a JSON list) as a clean list."""
    try:
        tags = json.loads(live.get("tags") or "[]")
    except Exception:
        return []
    return [str(t) for t in tags if t] if isinstance(tags, list) else []


def _create_live(conn, user, title, entry_tickets=0,
                 category="", tags=None):
    """Create a live for user (commits, returns the new id).
    Carries the platform's two standing rules: starting your own
    live pulls you off everybody else's panel (Ken, Oct 27), and
    one live per host — a new one ends the old, EXCEPT the Lobby,
    which is not the host's personal live and never dies here."""
    seats = conn.execute(
        "SELECT DISTINCT live_id FROM live_members WHERE user_id=? "
        "AND left_at IS NULL", (user["id"],)).fetchall()
    for seat in seats:
        other = _live_get(conn, seat["live_id"])
        if other and other["status"] == "live" \
                and other["host_id"] != user["id"]:
            _drop_guest(conn, dict(other), user["id"])
    old = conn.execute(
        "SELECT * FROM live_sessions WHERE host_id=? AND "
        "status='live' AND COALESCE(is_lobby,0)=0",
        (user["id"],)).fetchone()
    if old:
        _end_live_row(conn, dict(old))
    stamp = now_iso()
    cur = conn.execute(
        "INSERT INTO live_sessions(host_id, title, status, "
        "created_at, host_seen_at, like_count, panel_order, "
        "entry_tickets, category, tags) "
        "VALUES(?,?,'live',?,?,0,?,?,?,?)",
        (user["id"], title, stamp, stamp, json.dumps([user["id"]]),
         int(entry_tickets or 0), category or "",
         json.dumps(list(tags or []))))
    lid = cur.lastrowid
    conn.commit()
    return lid


def _live_entered(conn, lid, uid):
    return bool(conn.execute(
        "SELECT 1 FROM live_entries WHERE live_id=? AND user_id=?",
        (lid, uid)).fetchone())


def _entry_gate(conn, live, user, panel_ids, owner_on=False):
    """True when this viewer still owes the door: the live has a
    ticket price, and the viewer is not the host, not seated, not
    an admin, not the owner context, and has not paid."""
    price = live["entry_tickets"] or 0
    if not price or owner_on:
        return False
    if user["id"] == live["host_id"] or user["id"] in panel_ids:
        return False
    if conn.execute(
            "SELECT 1 FROM live_admins WHERE live_id=? AND "
            "user_id=? AND revoked_at IS NULL",
            (live["id"], user["id"])).fetchone():
        return False
    return not _live_entered(conn, live["id"], user["id"])


@app.post("/live/{lid}/pay-entry")
async def live_pay_entry(request: Request, lid: int):
    """🎟 Pay the door: the viewer's tickets move to the HOST's
    earnings in one guarded transaction (twin ledger lines,
    note='entry'), and a live_entries row remembers the payment
    for the life of this live. Paying twice never charges twice —
    the entries row is the receipt."""
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    conn = db()
    _sweep_lives(conn)
    live = _live_get(conn, lid)
    if not live or live["status"] != "live":
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That live isn't running."},
            status_code=400)
    price = live["entry_tickets"] or 0
    if not price:
        conn.close()
        return JSONResponse({"ok": True, "free": True})
    if user["id"] == live["host_id"]:
        conn.close()
        return JSONResponse({"ok": True, "host": True})
    if _live_entered(conn, lid, user["id"]):
        conn.close()
        return JSONResponse({"ok": True, "already": True})
    cur = conn.execute(
        "UPDATE users SET tickets_balance = "
        "COALESCE(tickets_balance,0) - ? WHERE id=? AND "
        "COALESCE(tickets_balance,0) >= ?",
        (price, user["id"], price))
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Not enough tickets — grab a pack first 🎟️"},
            status_code=400)
    host_id = live["host_id"]
    conn.execute(
        "UPDATE users SET gift_earned = COALESCE(gift_earned,0) "
        "+ ?, gift_lifetime = COALESCE(gift_lifetime,0) + ? "
        "WHERE id=?", (price, price, host_id))
    stamp = now_iso()
    conn.execute(
        "INSERT OR IGNORE INTO live_entries(live_id, user_id, "
        "tickets, created_at) VALUES(?,?,?,?)",
        (lid, user["id"], price, stamp))
    conn.execute(
        "INSERT INTO ticket_txns(user_id, kind, tickets, live_id, "
        "gift_key, note, created_at) VALUES(?,?,?,?,?,?,?)",
        (user["id"], "entry_pay", -price, lid, "", "entry", stamp))
    conn.execute(
        "INSERT INTO ticket_txns(user_id, kind, tickets, live_id, "
        "gift_key, note, created_at) VALUES(?,?,?,?,?,?,?)",
        (host_id, "entry_receive", price, lid, "", "entry", stamp))
    _invite_bonus(conn, host_id, price, stamp)
    conn.commit()
    bal = conn.execute(
        "SELECT tickets_balance FROM users WHERE id=?",
        (user["id"],)).fetchone()["tickets_balance"]
    conn.close()
    host = get_user(host_id) or {}
    notify(host_id, "live_entry",
           f"{user['name']} paid 🎟 {price} to enter your live",
           f"/live/{lid}", actor_id=user["id"])
    return JSONResponse({"ok": True, "balance": bal or 0,
                         "host": host.get("name") or ""})


@app.post("/live/start")
async def live_start(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    try:
        data = await request.json()
    except Exception:
        data = {}
    title = str(data.get("title") or "").strip()[:LIVE_TITLE_MAX] \
        if isinstance(data, dict) else ""
    entry = 0
    if isinstance(data, dict):
        try:
            entry = int(data.get("entry_tickets") or 0)
        except (TypeError, ValueError):
            entry = 0
    entry = max(0, min(entry, 100000))
    if entry and not (get_user(user["id"]) or {}).get(
            "creator_mode"):
        return JSONResponse(
            {"ok": False,
             "error": "Turn on Creator Mode (Settings) to charge "
                      "a ticket door — that's what lets you earn."},
            status_code=400)
    category = ""
    tags = []
    if isinstance(data, dict):
        category = str(data.get("category") or "").strip().lower()
        if category not in PLATFORM_CAT_LABELS:
            category = ""
        tags = _platform_tags(data.get("tags"))
    conn = db()
    _sweep_lives(conn)
    lid = _create_live(conn, user, title, entry_tickets=entry,
                       category=category, tags=tags)
    conn.close()
    _link_schedule_for_live(user, lid)
    if title:
        flag_item("live_title", lid, user["id"], user["name"], title)
    # Tell the host's friends (bell + phone push). notify() and the
    # push gate both respect silence/block via notifications_allowed.
    text = f"🔴 {user['name']} is live" + (f": {title}" if title else "")
    for fid in friend_id_set(user["id"]):
        notify(fid, "live", text, f"/live/{lid}", actor_id=user["id"])
        if notifications_allowed(fid, user["id"]):
            send_push(fid, f"🔴 {user['name']} is live",
                      title or "Tap to watch now.", f"/live/{lid}",
                      tag=f"live-{lid}", ttl=3600)
    return JSONResponse({"ok": True, "id": lid,
                         "url": f"/live/{lid}",
                         "entry_tickets": entry})


_LIVE_CARD_CACHE = {}


def _live_card_bytes(lid, animated):
    """The share card for a live: host avatar, pulsing LIVE
    badge, title, who's watching — drawn fresh with Pillow and
    cached a minute. GIF frames pulse the badge dot (feeds
    that animate og:images get motion; the rest get a strong
    first frame). None when the live is gone or Pillow is."""
    import io as _io
    import time as _time
    key = (lid, bool(animated))
    hit = _LIVE_CARD_CACHE.get(key)
    if hit and _time.time() - hit[0] < 60:
        return hit[1]
    conn = db()
    live = _live_get(conn, lid)
    if not live:
        conn.close()
        return None
    watching = conn.execute(
        "SELECT COUNT(*) c FROM live_viewers WHERE live_id=? "
        "AND left_at IS NULL", (lid,)).fetchone()["c"]
    members = conn.execute(
        "SELECT COUNT(*) c FROM live_members WHERE live_id=? "
        "AND left_at IS NULL", (lid,)).fetchone()["c"]
    conn.close()
    host_row = get_user(live["host_id"]) or {}
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None

    def font(sz, bold=True):
        cands = ["/usr/share/fonts/truetype/dejavu/"
                 + ("DejaVuSans-Bold.ttf" if bold
                    else "DejaVuSans.ttf"),
                 "/usr/share/fonts/truetype/liberation/"
                 + ("LiberationSans-Bold.ttf" if bold
                    else "LiberationSans-Regular.ttf")]
        for p in cands:
            try:
                return ImageFont.truetype(p, sz)
            except Exception:
                pass
        return ImageFont.load_default()

    W, H = 1200, 630
    live_now = (live.get("status") == "live")
    title = ((live.get("title") or "").strip()
             or "Untitled live")

    def frame(pulse):
        img = Image.new("RGB", (W, H), (16, 16, 20))
        d = ImageDraw.Draw(img)
        d.rectangle([0, 0, W, 10], fill=(224, 30, 38))
        cx, cy, r = 215, 265, 128
        av = None
        rel = host_row.get("avatar") or ""
        if rel:
            try:
                raw = Image.open(
                    os.path.join(UPLOAD_DIR, rel)).convert("RGB")
                side = min(raw.size)
                raw = raw.crop((0, 0, side, side)).resize(
                    (r * 2, r * 2))
                av = raw
            except Exception:
                av = None
        if av is not None:
            mask = Image.new("L", (r * 2, r * 2), 0)
            ImageDraw.Draw(mask).ellipse(
                [0, 0, r * 2, r * 2], fill=255)
            img.paste(av, (cx - r, cy - r), mask)
        else:
            d.ellipse([cx - r, cy - r, cx + r, cy + r],
                      fill=(64, 22, 27))
            d.text((cx, cy),
                   (host_row.get("name") or "?")[:1].upper(),
                   font=font(110), fill=(255, 255, 255),
                   anchor="mm")
        d.ellipse([cx - r, cy - r, cx + r, cy + r],
                  outline=(224, 30, 38), width=6)
        label = "LIVE" if live_now else "ENDED"
        d.rounded_rectangle(
            [415, 118, 625, 190], radius=35,
            fill=(224, 30, 38) if live_now else (92, 92, 100))
        dot_r = 12 + (pulse if live_now else 0)
        d.ellipse([448 - dot_r, 154 - dot_r, 448 + dot_r,
                   154 + dot_r], fill=(255, 255, 255))
        d.text((478, 154), label, font=font(42),
               fill=(255, 255, 255), anchor="lm")
        words, lines, cur_l = title.split(), [], ""
        for w_ in words:
            t = (cur_l + " " + w_).strip()
            if d.textlength(t, font=font(56)) > 720 and cur_l:
                lines.append(cur_l)
                cur_l = w_
            else:
                cur_l = t
        if cur_l:
            lines.append(cur_l)
        y = 232
        for ln in lines[:2]:
            d.text((415, y), ln, font=font(56),
                   fill=(255, 255, 255), anchor="lm")
            y += 72
        d.text((415, y + 14),
               (host_row.get("name") or "Someone") + " on HERE",
               font=font(36, bold=False), fill=(198, 198, 206),
               anchor="lm")
        if live_now:
            d.text((415, y + 68),
                   f"{watching} watching · {members + 1} "
                   "on the panel",
                   font=font(32, bold=False),
                   fill=(255, 168, 173), anchor="lm")
        d.rectangle([0, H - 84, W, H], fill=(224, 30, 38))
        d.text((40, H - 42), "joincomehere.com", font=font(38),
               fill=(255, 255, 255), anchor="lm")
        d.text((W - 40, H - 42), "Watch free — join in seconds",
               font=font(32, bold=False), fill=(255, 255, 255),
               anchor="rm")
        return img

    buf = _io.BytesIO()
    if animated:
        frames = [frame(p) for p in (0, 6, 12)]
        frames[0].save(buf, format="GIF", save_all=True,
                       append_images=frames[1:], duration=520,
                       loop=0)
    else:
        frame(6).save(buf, format="PNG")
    data = buf.getvalue()
    _LIVE_CARD_CACHE[key] = (_time.time(), data)
    return data


@app.get("/live/{lid}/og.png")
async def live_og_png(lid: int):
    data = _live_card_bytes(lid, False)
    if not data:
        return redir("/static/og-logo.jpg")
    return Response(data, media_type="image/png",
                    headers={"Cache-Control":
                             "public, max-age=60"})


@app.get("/live/{lid}/og.gif")
async def live_og_gif(lid: int):
    data = _live_card_bytes(lid, True)
    if not data:
        return redir("/static/og-logo.jpg")
    return Response(data, media_type="image/gif",
                    headers={"Cache-Control":
                             "public, max-age=60"})


@app.get("/live/{lid}", response_class=HTMLResponse)
async def live_page(request: Request, lid: int):
    user = current_user(request)
    if not user:
        # Guests (and every social scraper) get the watch-gate:
        # the host, the panel faces, and one job — join and
        # you're inside this live. Scrapers read its og blocks.
        conn = db()
        _sweep_lives(conn)
        glive = _live_get(conn, lid)
        if not glive:
            conn.close()
            return redir("/login")
        ghost = _live_person(glive["host_id"])
        gpanel = [ghost]
        for m in conn.execute(
                "SELECT user_id FROM live_members WHERE "
                "live_id=? AND left_at IS NULL",
                (lid,)).fetchall():
            if m["user_id"] != glive["host_id"]:
                gpanel.append(_live_person(m["user_id"]))
        gwatch = conn.execute(
            "SELECT COUNT(*) c FROM live_viewers WHERE "
            "live_id=? AND left_at IS NULL",
            (lid,)).fetchone()["c"]
        conn.close()
        return render(request, "live_gate.html", {
            "live": glive, "host": ghost,
            "panel": gpanel[:8], "watching": gwatch,
            "og_img": _abs_url(request, f"/live/{lid}/og.gif"),
            "og_png": _abs_url(request, f"/live/{lid}/og.png")})
    conn = db()
    _sweep_lives(conn)
    live = _live_get(conn, lid)
    if not live:
        conn.close()
        return redir("/home", "That live isn't available anymore.")
    host = _live_person(live["host_id"])
    host["live_level"] = _streamer_level(
        (get_user(live["host_id"]) or {}).get("gift_lifetime"))
    owner_on = is_owner(request)
    # Build GLASS: ?embed=1 renders the chrome-less viewer used by
    # the floating mini player (same watch gate, same viewer join —
    # only the chrome differs).
    embed = request.query_params.get("embed") == "1"
    if live["status"] != "live":
        conn.close()
        return render(request, "live.html", {
            "mode": "ended", "live": live, "host": host,
            "embed": embed, "hide_golive": True})
    if not _live_can_watch(user, live, owner_on=owner_on):
        conn.close()
        # Same face as an ended live: nothing about the room leaks.
        return render(request, "live.html", {
            "mode": "unavailable", "live": live, "host": host,
            "embed": embed, "hide_golive": True})
    panel_ids = _live_panel_ids(conn, live)
    if _entry_gate(conn, live, user, panel_ids, owner_on):
        price = live["entry_tickets"] or 0
        bal = (get_user(user["id"]) or {}).get(
            "tickets_balance") or 0
        conn.close()
        return render(request, "live.html", {
            "mode": "entry_gate", "live": live, "host": host,
            "entry_tickets": price, "my_balance": bal,
            "embed": embed, "hide_golive": True})
    conn.close()
    # Boot compat: 'guest' stays the first panelist (oror
    # None); the watch page's state poll carries the full panel.
    guest = _live_person(panel_ids[1]) if len(panel_ids) > 1 else None
    if user["id"] == live["host_id"]:
        role = "host"
    elif user["id"] in panel_ids:
        role = "guest"
    else:
        role = "watcher"
    if embed and role == "host":
        # The mini player is a viewer feature: the host's own embed
        # gets a static card instead of a second broadcast page (no
        # live script at all — nothing that could end the real live
        # when the tile closes).
        return render(request, "live.html", {
            "mode": "embed_host", "live": live, "host": host,
            "embed": True, "hide_golive": True})
    boot = {"id": lid, "me": user["id"], "role": role,
            "host": host, "guest": guest, "title": live["title"],
            "created_at": live["created_at"], "embed": embed,
            # Live Tickets: my spendable balance for the chip/gift
            # sheet, and the gift catalog for the tray (prices are
            # still enforced server-side from GIFT_CATALOG).
            "tickets": (dict(user).get("tickets_balance") or 0),
            "gifts": [GIFT_CATALOG[k] for k in GIFT_ORDER],
            "shout_cost": SHOUT_COST_TICKETS,
            # Platforms branding: the host's backdrop + shelf.
            "backdrop": avatar_url(live.get("backdrop") or ""),
            "backdrop_fit": live.get("backdrop_fit") or "auto",
            "category": live.get("category") or "",
            "tags": _live_tags(live)}
    # Browse mode (fullscreen-live rework): the watch page renders a
    # browse section server-side at load — other current lives this
    # viewer may watch (live_now_for, excluding THIS live) and the
    # latest ~10 home-feed posts (the same post_card dicts /home
    # renders, so the post_card partial works unchanged). The
    # section stays hidden until the viewer shrinks the live into
    # its floating tile; following a card link navigates away and
    # ends this viewing session (the beforeunload leave beacon),
    # exactly like leaving a call page.
    more_lives = [r for r in live_now_for(user["id"], owner_on)
                  if r["id"] != lid]
    browse_posts = _home_posts(user, owner_on)[:10]
    # Build GLASS Part C: the live's external share sheet mirrors the
    # /l public gate exactly — a live shares off-platform freely
    # while it is live and its host's profile is public; a private
    # host's live exposes no external sheet to anyone here (the /l
    # face itself shows the generic teaser for it).
    host_private = bool(
        (get_user(live["host_id"]) or {}).get("private_profile"))
    live_share_text = ((live.get("title") or "").strip()
                       or f"{host['name']} is live on HERE")
    return render(request, "live.html", {
        "mode": "live", "live": live, "host": host, "guest": guest,
        "role": role, "more_lives": more_lives,
        "browse_posts": browse_posts,
        "boot": json.dumps(boot).replace("</", "<\\/"),
        "embed": embed,
        "platform_label": PLATFORM_CAT_LABELS.get(
            live.get("category") or "", ""),
        "platform_tags": _live_tags(live),
        "can_share_live": not host_private,
        "live_share_url": f"https://joincomehere.com/l/{lid}",
        "live_share_text": live_share_text,
        "hide_golive": True})


@app.post("/live/{lid}/end")
async def live_end(request: Request, lid: int):
    """Host ends the live. Takes no body so the page can fire it from
    beforeunload with sendBeacon. Idempotent."""
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    conn = db()
    _sweep_lives(conn)
    live = _live_get(conn, lid)
    if not live:
        conn.close()
        return JSONResponse({"ok": False, "error": "not found"},
                            status_code=404)
    if live["host_id"] != user["id"]:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "Only the host can end this live."},
            status_code=403)
    _end_live_row(conn, live)
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.post("/owner/live/{lid}/end")
async def owner_live_end(request: Request, lid: int):
    """Admin kill switch: the site owner ends ANY live (from the
    owner panel's Live now section)."""
    err = require_owner(request)
    if err:
        return err
    conn = db()
    _sweep_lives(conn)
    live = _live_get(conn, lid)
    if live:
        _end_live_row(conn, live)
        conn.commit()
    conn.close()
    if _oa(request):
        return _oa_json("Live ended for everyone.")
    return redir("/owner")


@app.post("/live/{lid}/heartbeat")
async def live_heartbeat(request: Request, lid: int):
    """Presence: the host's heartbeat keeps the live from being swept;
    a watcher's/guest's heartbeat is their live_viewers row (viewer
    counts read rows fresh within LIVE_VIEWER_WINDOW)."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    # Build W3: a panel member's client reports its own camera
    # health (video_ok) on this heartbeat. An ABSENT key is a
    # legacy client — the stored flag is left untouched (NULL
    # reads as True in the state poll), never clobbered.
    video_ok = None
    try:
        _hb = await request.json()
        if isinstance(_hb, dict) and "video_ok" in _hb:
            video_ok = 1 if _hb["video_ok"] else 0
    except Exception:
        video_ok = None
    conn = db()
    stamp = now_iso()
    if user["id"] == live["host_id"]:
        conn.execute(
            "UPDATE live_sessions SET host_seen_at=? WHERE id=?",
            (stamp, lid))
        if video_ok is not None:
            conn.execute(
                "UPDATE live_sessions SET host_video_ok=? "
                "WHERE id=?", (video_ok, lid))
    else:
        conn.execute(
            "INSERT INTO live_viewers(live_id, user_id, joined_at, "
            "last_seen_at) VALUES(?,?,?,?) ON CONFLICT(live_id, user_id) "
            "DO UPDATE SET last_seen_at=excluded.last_seen_at, "
            "left_at=NULL", (lid, user["id"], stamp, stamp))
        if video_ok is not None and _live_member_row(
                conn, lid, user["id"]):
            conn.execute(
                "UPDATE live_members SET video_ok=? WHERE "
                "live_id=? AND user_id=?",
                (video_ok, lid, user["id"]))
    count = _live_viewer_count(conn, lid)
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "viewers": count})


@app.post("/live/{lid}/leave")
async def live_leave(request: Request, lid: int):
    """A watcher/guest closes the page (beacon-friendly, no body
    needed): their viewer row is marked left; an on-camera guest is
    dropped back to watcher, freeing the guest slot."""
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    conn = db()
    _sweep_lives(conn)
    live = _live_get(conn, lid)
    if live and user["id"] != live["host_id"]:
        if _live_member_row(conn, lid, user["id"]):
            # An on-camera panelist closing the page leaves the panel.
            _drop_guest(conn, live, user["id"])
        else:
            # Legacy fallback: an approved guest with no member row.
            guest_req = _live_guest_req(conn, lid)
            if guest_req and guest_req["user_id"] == user["id"]:
                _drop_guest(conn, live, user["id"])
        conn.execute(
            "UPDATE live_viewers SET left_at=? WHERE live_id=? AND "
            "user_id=?", (now_iso(), lid, user["id"]))
        conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


# ---------------- Live Showdown battles (Build W2) ----------------
# A battle LINKS two live rooms — viewers never move rooms; both
# state polls carry the same shared scoreboard from their own
# room's point of view. Gifts are the score: the gift handler adds
# each ticket gift's cost to the receiving side inside the gift's
# own transaction, so money flow is exactly as it always was.
# Resolution is LAZY: _sweep_lives (which every live read/write
# passes through) finishes expired battles, and _end_live_row
# finishes a battle the moment a host's live ends (forfeit).
BATTLE_DURATIONS = (180, 300, 600)
BATTLE_BANNER_SECONDS = 60    # finished block lingers in both rooms
BATTLE_DECLINED_SECONDS = 30  # declined notice lingers for challenger


def _battle_get(conn, bid):
    r = conn.execute("SELECT * FROM live_battles WHERE id=?",
                     (bid,)).fetchone()
    return dict(r) if r else None


def _battle_busy(conn, lid):
    """The pending/active battle involving this live, either role."""
    r = conn.execute(
        "SELECT * FROM live_battles WHERE status IN ('pending',"
        "'active') AND (challenger_live_id=? OR opponent_live_id=?) "
        "ORDER BY id DESC LIMIT 1", (lid, lid)).fetchone()
    return dict(r) if r else None


def _battle_for_live(conn, lid):
    """The battle block a room's state poll should carry: the live
    pending/active battle, else a just-finished one still inside
    its 60s winner-banner window, else (challenger only) a fresh
    decline so the challenger hears about it."""
    b = _battle_busy(conn, lid)
    if b:
        return b
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=BATTLE_BANNER_SECONDS)).isoformat()
    r = conn.execute(
        "SELECT * FROM live_battles WHERE status='finished' AND "
        "decided_at>=? AND (challenger_live_id=? OR "
        "opponent_live_id=?) ORDER BY id DESC LIMIT 1",
        (cutoff, lid, lid)).fetchone()
    if r:
        return dict(r)
    cutoff = (datetime.now(timezone.utc)
              - timedelta(seconds=BATTLE_DECLINED_SECONDS)).isoformat()
    r = conn.execute(
        "SELECT * FROM live_battles WHERE status='declined' AND "
        "decided_at>=? AND challenger_live_id=? ORDER BY id DESC "
        "LIMIT 1", (cutoff, lid)).fetchone()
    return dict(r) if r else None


def _battle_finish(conn, battle, reason, winner_user_id=None):
    """Guarded active→finished flip (no commit). Timer finishes
    compute the winner from the scores (tie = NULL = draw);
    forfeits pass the surviving host explicitly. Returns True
    only for the caller that actually flipped it."""
    if battle["status"] != "active":
        return False
    if reason != "forfeit":
        cs, os_ = battle["challenger_score"], battle["opponent_score"]
        if cs > os_:
            winner_user_id = battle["challenger_user_id"]
        elif os_ > cs:
            winner_user_id = battle["opponent_user_id"]
        else:
            winner_user_id = None
    cur = conn.execute(
        "UPDATE live_battles SET status='finished', "
        "winner_user_id=?, finish_reason=?, decided_at=? "
        "WHERE id=? AND status='active'",
        (winner_user_id, reason, now_iso(), battle["id"]))
    return cur.rowcount == 1


def _battle_expire(conn):
    """Finish every active battle whose clock ran out (no commit).
    Returns how many flipped."""
    rows = conn.execute(
        "SELECT * FROM live_battles WHERE status='active' AND "
        "ends_at IS NOT NULL AND ends_at<=?", (now_iso(),)).fetchall()
    n = 0
    for r in rows:
        if _battle_finish(conn, dict(r), "timer"):
            n += 1
    return n


def _battle_result_text(b):
    cn = (get_user(b["challenger_user_id"]) or {}).get(
        "name", "Deleted user")
    on = (get_user(b["opponent_user_id"]) or {}).get(
        "name", "Deleted user")
    cs, os_ = b["challenger_score"], b["opponent_score"]
    if b["winner_user_id"] is None:
        return (f"⚔️ Live Showdown draw! {cn} and {on} tied "
                f"{cs}–{os_}. Nobody takes the crown — run it back!")
    if b["winner_user_id"] == b["challenger_user_id"]:
        wname, lname, ws, ls = cn, on, cs, os_
    else:
        wname, lname, ws, ls = on, cn, os_, cs
    text = f"⚔️ {wname} won the Live Showdown {ws}–{ls} vs {lname}!"
    if b.get("finish_reason") == "forfeit":
        text += f" ({lname} left the live — forfeit win.)"
    return text


def _battle_settle_effects(bid):
    """Exactly-once finish side effects, on its OWN connection:
    both hosts get the result as their own feed post (moderation-
    flagged like any post) and a notification. The effects_done
    claim makes concurrent settlers safe; _sweep_lives retries
    any finished battle that somehow missed out."""
    conn = db()
    b = conn.execute("SELECT * FROM live_battles WHERE id=?",
                     (bid,)).fetchone()
    if not b or b["status"] != "finished":
        conn.close()
        return
    cur = conn.execute(
        "UPDATE live_battles SET effects_done=1 WHERE id=? AND "
        "status='finished' AND effects_done=0", (bid,))
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        return
    b = dict(b)
    text = _battle_result_text(b)
    stamp = now_iso()
    post_ids = {}
    for uid in (b["challenger_user_id"], b["opponent_user_id"]):
        cur = conn.execute(
            "INSERT INTO posts(user_id, page_id, kind, body, "
            "feeling, link_url, video_url, media, privacy, "
            "music_video_id, music_title, music_artist, music_thumb, "
            "music_start, created_at) VALUES(?,?,?,?,?,?,?,?,?,"
            "?,?,?,?,?,?)",
            (uid, None, "text", text, "", "", "", "", "public",
             "", "", "", "", 0, stamp))
        post_ids[uid] = cur.lastrowid
    conn.commit()
    conn.close()
    for uid, pid in post_ids.items():
        other = (b["opponent_user_id"] if uid == b["challenger_user_id"]
                 else b["challenger_user_id"])
        notify(uid, "showdown", text, f"/post/{pid}", actor_id=other)
        uname = (get_user(uid) or {}).get("name", "")
        flag_item("post", pid, uid, uname, text)


def _battle_json(conn, battle, lid):
    """The state-poll battle block from THIS room's point of view:
    'me' is always this room's host, scores already swapped."""
    is_chal = battle["challenger_live_id"] == lid
    my_uid = (battle["challenger_user_id"] if is_chal
              else battle["opponent_user_id"])
    opp_uid = (battle["opponent_user_id"] if is_chal
               else battle["challenger_user_id"])
    opp_live = (battle["opponent_live_id"] if is_chal
                else battle["challenger_live_id"])
    me = _live_person(my_uid)
    opp = _live_person(opp_uid)
    seconds_left = None
    if battle["status"] == "active" and battle.get("ends_at"):
        seconds_left = max(0, int((
            datetime.fromisoformat(battle["ends_at"])
            - datetime.now(timezone.utc)).total_seconds()))
    winner = battle["winner_user_id"]
    return {
        "id": battle["id"], "status": battle["status"],
        "duration_sec": battle["duration_sec"],
        "seconds_left": seconds_left,
        "my_score": (battle["challenger_score"] if is_chal
                     else battle["opponent_score"]),
        "opp_score": (battle["opponent_score"] if is_chal
                      else battle["challenger_score"]),
        "me": {"user_id": my_uid, "name": me["name"],
               "avatar": me["avatar"]},
        "opponent": {"live_id": opp_live, "user_id": opp_uid,
                     "name": opp["name"], "avatar": opp["avatar"]},
        "direction": "outgoing" if is_chal else "incoming",
        "winner_user_id": winner,
        "winner_name": (me["name"] if winner == my_uid
                        else opp["name"]) if winner else None,
        "i_won": (winner == my_uid) if winner else None,
        "forfeit": battle.get("finish_reason") == "forfeit",
    }


def _battle_candidates(conn, user, live):
    """Other currently-live hosts this host could challenge: live
    now, not already in a battle, and watchable by the challenger
    (blocked pairs never battle)."""
    rows = conn.execute(
        "SELECT * FROM live_sessions WHERE status='live' AND id!=? "
        "ORDER BY created_at DESC LIMIT 20", (live["id"],)).fetchall()
    out = []
    for r in rows:
        other = dict(r)
        if _battle_busy(conn, other["id"]):
            continue
        if not _live_can_watch(user, other):
            continue
        p = _live_person(other["host_id"])
        out.append({"live_id": other["id"],
                    "user_id": other["host_id"], "name": p["name"],
                    "avatar": p["avatar"], "title": other["title"]})
    return out


def _showdown_record(conn, uid):
    """(wins, losses, draws) over finished battles for a profile."""
    w = conn.execute(
        "SELECT COUNT(*) c FROM live_battles WHERE status='finished' "
        "AND winner_user_id=?", (uid,)).fetchone()["c"]
    played = conn.execute(
        "SELECT COUNT(*) c FROM live_battles WHERE status='finished' "
        "AND (challenger_user_id=? OR opponent_user_id=?)",
        (uid, uid)).fetchone()["c"]
    d = conn.execute(
        "SELECT COUNT(*) c FROM live_battles WHERE status='finished' "
        "AND winner_user_id IS NULL AND (challenger_user_id=? OR "
        "opponent_user_id=?)", (uid, uid)).fetchone()["c"]
    return w, played - w - d, d


def _recent_showdowns(conn, limit=5):
    """Last finished battles for the /lives 🏆 strip."""
    rows = conn.execute(
        "SELECT * FROM live_battles WHERE status='finished' ORDER "
        "BY decided_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        b = dict(r)
        cn = _live_person(b["challenger_user_id"])["name"]
        on = _live_person(b["opponent_user_id"])["name"]
        winner_name = None
        if b["winner_user_id"] == b["challenger_user_id"]:
            winner_name = cn
        elif b["winner_user_id"] == b["opponent_user_id"]:
            winner_name = on
        out.append({
            "challenger_name": cn, "opponent_name": on,
            "challenger_score": b["challenger_score"],
            "opponent_score": b["opponent_score"],
            "winner_name": winner_name,
            "forfeit": b.get("finish_reason") == "forfeit"})
    return out


@app.get("/live/{lid}/state")
async def live_state(request: Request, lid: int):
    """The watch page's 3s poll: room status, viewer count, the
    panel (ordered tiles), likes, new comments since ?since_comment=,
    heart-burst like events since ?since_like=, pending requests
    for the host/admins, and my own standing (my_panel, my_join,
    pending_invites_for_me). Legacy keys (guest, viewer_list,
    my_role) keep their shapes — guest is now the first panelist."""
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    conn = db()
    _sweep_lives(conn)
    live = _live_get(conn, lid)
    if not live:
        conn.close()
        return JSONResponse({"ok": False, "error": "not found"},
                            status_code=404)
    host = _live_person(live["host_id"])
    if live["status"] != "live":
        conn.close()
        return JSONResponse({"ok": True, "status": "ended",
                             "host": host, "title": live["title"]})
    if not _live_can_watch(user, live, owner_on=is_owner(request)):
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "You can't watch this live."},
            status_code=403)
    _panel_ids_pre = _live_panel_ids(conn, live)
    if _entry_gate(conn, live, user, _panel_ids_pre,
                   is_owner(request)):
        price = live["entry_tickets"] or 0
        conn.close()
        return JSONResponse({
            "ok": True, "status": "entry_required",
            "entry_tickets": price, "host": host,
            "title": live["title"]})
    try:
        since = int(request.query_params.get("since_comment") or 0)
    except (TypeError, ValueError):
        since = 0
    try:
        since_like = int(request.query_params.get("since_like") or 0)
    except (TypeError, ValueError):
        since_like = 0
    is_host = user["id"] == live["host_id"]
    admin_ids = {r["user_id"] for r in conn.execute(
        "SELECT user_id FROM live_admins WHERE live_id=? AND "
        "revoked_at IS NULL", (lid,)).fetchall()}
    viewer_is_admin = user["id"] in admin_ids
    is_mod = is_host or viewer_is_admin
    members = _live_active_members(conn, lid)
    member_map = {m["user_id"]: m for m in members}
    # The host is ALWAYS on the panel (panelist #1 by role) but is
    # never a live_members row, so membership must include is_host —
    # reporting False here once demoted the host's own client to a
    # watcher on its first state poll, which tore down every peer
    # and made the host ignore all hellos: nobody could see or hear
    # anybody (Ken's 2026-10-23 live report).
    am_member = is_host or user["id"] in member_map
    rows = conn.execute(
        "SELECT lc.*, u.name, u.avatar, u.founding_no, u.plus_until "
        "FROM live_comments lc "
        "LEFT JOIN users u ON u.id=lc.user_id WHERE lc.live_id=? AND "
        "lc.id>? ORDER BY lc.id ASC LIMIT 100", (lid, since)).fetchall()
    # Gifter ranks for this poll's authors + gift senders (cached
    # per user — one ledger SUM each, not one per row).
    _rank_cache = {}

    def _rank_of(uid):
        if uid not in _rank_cache:
            _rank_cache[uid] = _gifter_rank(
                _gift_sent_total(conn, uid))
        return _rank_cache[uid]

    comments = [_live_comment_json(dict(r), user["id"], live["host_id"],
                                   viewer_is_admin=viewer_is_admin,
                                   rank=_rank_of(r["user_id"]))
                for r in rows]
    panel_ids = _live_panel_ids(conn, live)
    captain_id = _lobby_captain_id(conn, live)
    # Live selling (Ken's pick #4): listings riding this live.
    shop = []
    for srow in conn.execute(
            "SELECT * FROM market_listings WHERE live_id=? "
            "AND status!='deleted' ORDER BY created_at ASC",
            (lid,)).fetchall():
        _ph = _market_photos(srow)
        _claim = None
        if srow["claimed_by"] and srow["claim_expires_at"]:
            _exp = _parse_ts(srow["claim_expires_at"])
            if _exp and _exp > datetime.now(timezone.utc):
                _cu = get_user(srow["claimed_by"])
                _claim = {
                    "by": srow["claimed_by"],
                    "by_name": _cu["name"] if _cu else "Someone",
                    "mine": srow["claimed_by"] == user["id"],
                    "left_s": int((_exp - datetime.now(
                        timezone.utc)).total_seconds())}
            else:
                conn.execute(
                    "UPDATE market_listings SET claimed_by=NULL, "
                    "claim_expires_at=NULL WHERE id=?",
                    (srow["id"],))
        shop.append({
            "id": srow["id"], "title": srow["title"] or "",
            "price_cents": srow["price_cents"],
            "price": _money(srow["price_cents"]),
            "photo": ("/uploads/" + _ph[0]) if _ph else "",
            "status": srow["status"],
            "sold": srow["status"] == "sold",
            "seller_id": srow["seller_id"],
            "claim": _claim})
    panel = []
    for pid in panel_ids:
        person = _live_person(pid)
        mrow = member_map.get(pid)
        # Build W3: the member's self-reported camera health.
        # NULL (host flag or member row) = never reported — a
        # legacy client — and reads True: only an explicit report
        # of a dead/missing video track turns this off.
        if pid == live["host_id"]:
            _vok = live.get("host_video_ok")
            if live.get("is_lobby"):
                # The Lobby's host is only "on camera" while his
                # own client is actually in the room; otherwise
                # his tile honestly reads camera-off (avatar face)
                # instead of a black connecting box.
                _fresh = (datetime.now(timezone.utc)
                          - timedelta(seconds=120)).isoformat()
                _vok = 1 if (live.get("host_seen_at") or "") \
                    >= _fresh else 0
        else:
            _vok = mrow.get("video_ok") if mrow else None
        panel.append({
            "user_id": pid, "name": person["name"],
            "seat_label": (mrow.get("seat_label") if mrow else None)
            or "",
            "avatar": person["avatar"],
            "role": "host" if pid == live["host_id"] else "guest",
            "admin": pid in admin_ids,
            "muted": bool(mrow["muted"]) if mrow else False,
            "video_ok": True if _vok is None else bool(_vok),
            # Ken: every panel tile wears its owner's gift level
            # at the bottom center — tiles render this chip.
            "founding_no": person.get("founding_no"),
            "captain": pid == captain_id,
            "rank": "host" if pid == live["host_id"] else (
                (mrow.get("rank") if mrow else None) or "guest"),
            "level": _streamer_level(
                (get_user(pid) or {}).get("gift_lifetime"))})
    # Compat: the single 'guest' of the old protocol is the first
    # non-host panelist.
    guest = _live_person(panel_ids[1]) if len(panel_ids) > 1 else None
    viewer_rows = _live_viewer_rows(conn, lid)
    # Likes: count + cursor are read BEFORE the lazy prune, so a
    # poll never loses a burst it has not seen yet; events older
    # than ~2 minutes are heart-burst history and get pruned here.
    like_max = conn.execute(
        "SELECT COALESCE(MAX(id),0) m FROM live_like_events "
        "WHERE live_id=?", (lid,)).fetchone()["m"]
    new_like_count = conn.execute(
        "SELECT COUNT(*) c FROM live_like_events WHERE live_id=? "
        "AND id>?", (lid, since_like)).fetchone()["c"]
    like_cursor = max(like_max, since_like)
    prune_cutoff = (datetime.now(timezone.utc)
                    - timedelta(minutes=2)).isoformat()
    conn.execute(
        "DELETE FROM live_like_events WHERE live_id=? AND "
        "created_at<?", (lid, prune_cutoff))
    conn.commit()
    inv_rows = conn.execute(
        "SELECT * FROM live_join_requests WHERE live_id=? AND "
        "user_id=? AND kind='invite' AND status='pending' "
        "ORDER BY id ASC", (lid, user["id"])).fetchall()
    invites_for_me = []
    for r in inv_rows:
        inviter = dict(r).get("inviter_id")
        invites_for_me.append({
            "id": r["id"],
            "from_name": _live_person(inviter)["name"] if inviter
                         else host["name"]})
    _now_ep = datetime.now(timezone.utc).timestamp()
    _arow = conn.execute(
        "SELECT * FROM live_auctions WHERE live_id=? AND "
        "(status='running' OR (status='ended' AND ends_at>?)) "
        "ORDER BY id DESC LIMIT 1", (lid, _now_ep - 90)).fetchone()
    auction = _auction_json(conn, dict(_arow) if _arow else None,
                            user["id"])
    out = {
        "ok": True, "status": "live", "title": live["title"],
        "host": host, "guest": guest,
        "created_at": live["created_at"],
        "viewers": len(viewer_rows),
        "comments": comments,
        "my_role": "host" if is_host else ("guest" if am_member
                                           else "watcher"),
        "my_join": None, "pending_requests": [],
        "panel": panel,
        "spotlight_id": live.get("spotlight_id"),
        "is_lobby": bool(live.get("is_lobby")),
        "backdrop": avatar_url(live.get("backdrop") or ""),
        "backdrop_fit": live.get("backdrop_fit") or "auto",
        "category": live.get("category") or "",
        "entry_tickets": live["entry_tickets"] or 0,
        "captain_id": captain_id,
        "prompt": (daily_prompt() if live.get("is_lobby")
                   else None),
        "auction": auction,
        "shop": shop,
        "shop_stats": {"watching": len(viewer_rows),
                       "sold": sum(1 for s in shop if s["sold"])},
        # Build X: the panel grid's shared state — open or not,
        # and the host's guest-square allowance (1..8).
        "panel_open": bool(live.get("panel_open")),
        "panel_boxes": _live_panel_boxes(live),
        "spot_names": _live_spot_names(live),
        "like_count": live.get("like_count") or 0,
        "new_likes": min(new_like_count, 20),
        "like_cursor": like_cursor,
        "my_panel": {
            "am_member": am_member,
            "muted": bool(member_map[user["id"]]["muted"])
                     if user["id"] in member_map else False,
            "is_admin": viewer_is_admin,
            "is_host": is_host},
        "pending_invites_for_me": invites_for_me,
    }
    if is_host or am_member:
        # The people sending video need to know who is watching so
        # they can open a peer to each new watcher.
        out["viewer_list"] = [
            {"id": v["user_id"], "name": v["name"] or "Deleted user",
             "avatar": avatar_url(v["avatar"] or "")}
            for v in viewer_rows]
    if is_mod:
        prows = conn.execute(
            "SELECT jr.*, u.name, u.avatar FROM live_join_requests jr "
            "LEFT JOIN users u ON u.id=jr.user_id WHERE jr.live_id=? "
            "AND jr.status='pending' ORDER BY jr.id ASC",
            (lid,)).fetchall()
        _spot_names = _live_spot_names(live)
        out["pending_requests"] = [
            {"id": r["id"], "user_id": r["user_id"],
             "name": r["name"] or "Deleted user",
             "avatar": avatar_url(r["avatar"] or ""),
             "created_at": r["created_at"],
             "spot_idx": dict(r).get("spot_idx"),
             "spot_name": (
                 _spot_names[dict(r)["spot_idx"]]
                 if dict(r).get("spot_idx") is not None
                 and 0 <= dict(r)["spot_idx"] < len(_spot_names)
                 else ""),
             "suggested_by": dict(r).get("suggested_by"),
             "suggested_by_name": (
                 (get_user(dict(r)["suggested_by"]) or {}).get(
                     "name", "")
                 if dict(r).get("suggested_by") else ""),
             "kind": dict(r).get("kind") or "request"} for r in prows]
        # The full room roster, moderators only: who is watching,
        # and which of them are on the panel / admins / muted.
        out["watchers"] = [
            {"user_id": v["user_id"],
             "name": v["name"] or "Deleted user",
             "avatar": avatar_url(v["avatar"] or ""),
             "is_admin": v["user_id"] in admin_ids,
             "is_member": v["user_id"] in member_map,
             "muted": bool(member_map[v["user_id"]]["muted"])
                      if v["user_id"] in member_map else False}
            for v in viewer_rows]
        # "Invite others" candidates FOR THIS moderator (Ken: invite
        # people I'm following or friends with, not only people in
        # the room): my friends ∪ my following ∪ current watchers,
        # deduped, can-watch filtered. People already on the panel or
        # room-banned are excluded; people with a pending invite or
        # join request are flagged invited so the UI disables them.
        cand_ids = friend_id_set(user["id"]) | following_ids(user["id"]) \
            | {v["user_id"] for v in viewer_rows}
        cand_ids.discard(live["host_id"])
        cand_ids.discard(user["id"])
        pending_uids = {r["user_id"] for r in conn.execute(
            "SELECT user_id FROM live_join_requests WHERE live_id=? "
            "AND status='pending'", (lid,)).fetchall()}
        watching_uids = {v["user_id"] for v in viewer_rows}
        cands = []
        for cuid in cand_ids:
            if cuid in member_map or _live_banned(conn, lid, cuid):
                continue
            cuser = get_user(cuid)
            if not cuser or not _live_can_watch(
                    cuser, live, owner_on=is_owner(request)):
                continue
            person = _live_person(cuid)
            cands.append({"id": cuid, "name": person["name"],
                          "avatar": person["avatar"],
                          "invited": cuid in pending_uids,
                          "watching": cuid in watching_uids})
        cands.sort(key=lambda c: c["name"].lower())
        out["invite_candidates"] = cands
    mine = conn.execute(
        "SELECT * FROM live_join_requests WHERE live_id=? AND "
        "user_id=? ORDER BY id DESC LIMIT 1",
        (lid, user["id"])).fetchone()
    if mine:
        out["my_join"] = {"id": mine["id"], "status": mine["status"],
                          "decided_at": mine["decided_at"]}
    # Games pack: the room's poll standing. active_poll NEVER carries
    # the trivia answer while the poll is live. last_closed_poll is
    # the most recently closed poll — trivia answer + winners_count
    # revealed — and rides along only while no new poll is active: a
    # new poll starting replaces it (see ROUTE_CONTRACT.md).
    _active_poll = conn.execute(
        "SELECT * FROM live_polls WHERE live_id=? AND active=1 "
        "ORDER BY id DESC LIMIT 1", (lid,)).fetchone()
    out["active_poll"] = (
        _live_poll_json(conn, dict(_active_poll), user["id"])
        if _active_poll else None)
    if _active_poll:
        out["last_closed_poll"] = None
    else:
        _closed_poll = conn.execute(
            "SELECT * FROM live_polls WHERE live_id=? AND active=0 "
            "ORDER BY id DESC LIMIT 1", (lid,)).fetchone()
        out["last_closed_poll"] = (
            _live_poll_json(conn, dict(_closed_poll), user["id"],
                            reveal=True)
            if _closed_poll else None)
    # Games Pack 2: the room's game standing. active_game NEVER
    # carries the guess secret (no 'secret' key at all while a game
    # runs). last_game — winners + the revealed secret — rides along
    # only while no game is active, mirroring last_closed_poll.
    _active_game = conn.execute(
        "SELECT * FROM live_games WHERE live_id=? AND status='active' "
        "ORDER BY id DESC LIMIT 1", (lid,)).fetchone()
    out["active_game"] = (
        _live_game_json(conn, dict(_active_game), user["id"])
        if _active_game else None)
    if _active_game:
        out["last_game"] = None
    else:
        _ended_game = conn.execute(
            "SELECT * FROM live_games WHERE live_id=? AND "
            "status='ended' ORDER BY id DESC LIMIT 1", (lid,)).fetchone()
        out["last_game"] = (
            _live_game_result_json(conn, dict(_ended_game))
            if _ended_game else None)
    # Live Tickets: gifts sent in the last ~15s (clients animate the
    # ones past their gift cursor), this live's top gifters, the
    # pinned shout-out if one is still on the clock, and the
    # viewer's own spendable balance for the gift sheet + chip.
    gift_cutoff = (datetime.now(timezone.utc)
                   - timedelta(seconds=45)).isoformat()
    out["recent_gifts"] = [
        {"id": r["id"], "sender_id": r["sender_id"],
         "sender_name": r["sender_name"] or "Deleted user",
         "sender_rank": _rank_of(r["sender_id"]),
         # Build PANEL GIFTS: who the gift landed on (recipient_id
         # is NULL on pre-build rows — those went to the host).
         "ts": (_parse_ts(r["created_at"]).timestamp()
                if _parse_ts(r["created_at"]) else 0),
         "recipient_id": r["recipient_id"] or r["host_id"],
         "recipient_name": r["recipient_name"] or "Deleted user",
         "gift_key": r["gift_key"],
         **_gift_public(r["gift_key"], r["tickets"])}
        for r in conn.execute(
            "SELECT g.*, u.name AS sender_name, "
            "ru.name AS recipient_name FROM live_gifts g "
            "LEFT JOIN users u ON u.id=g.sender_id "
            "LEFT JOIN users ru ON "
            "ru.id=COALESCE(g.recipient_id, g.host_id) "
            "WHERE g.live_id=? "
            "AND g.created_at>=? ORDER BY g.id ASC",
            (lid, gift_cutoff)).fetchall()]
    out["top_gifters"] = [
        {"name": r["sender_name"] or "Deleted user",
         "tickets": r["total"]}
        for r in conn.execute(
            "SELECT g.sender_id, SUM(g.tickets) AS total, "
            "u.name AS sender_name FROM live_gifts g "
            "LEFT JOIN users u ON u.id=g.sender_id WHERE g.live_id=? "
            "GROUP BY g.sender_id ORDER BY total DESC LIMIT 5",
            (lid,)).fetchall()]
    shout_row = conn.execute(
        "SELECT s.*, u.name AS user_name FROM live_shouts s "
        "LEFT JOIN users u ON u.id=s.user_id WHERE s.live_id=? AND "
        "s.expires_at>? ORDER BY s.id DESC LIMIT 1",
        (lid, now_iso())).fetchone()
    out["active_shout"] = (
        {"id": shout_row["id"], "name": shout_row["user_name"]
         or "Deleted user", "body": shout_row["body"],
         "expires_at": shout_row["expires_at"]}
        if shout_row else None)
    _me = conn.execute(
        "SELECT tickets_balance FROM users WHERE id=?",
        (user["id"],)).fetchone()
    out["my_tickets"] = (_me["tickets_balance"] or 0) if _me else 0
    # Build W2: the Showdown block (shared scoreboard from this
    # room's point of view) + — host only — who they could challenge
    # right now. _sweep_lives above already finished any battle
    # whose clock ran out and settled its result posts.
    _battle = _battle_for_live(conn, lid)
    out["battle"] = (_battle_json(conn, _battle, lid)
                     if _battle else None)
    if is_host:
        out["battle_candidates"] = _battle_candidates(conn, user, live)
    conn.close()
    return JSONResponse(out)


# ---------------- live polls + trivia (games pack) ----------------
# Binary polls inside a live room. correct NULL = a plain poll;
# 'a'/'b' = a trivia poll — the answer is NEVER included in the
# active_poll state payload; it is revealed only on the closed poll
# (last_closed_poll) together with winners_count.
TRIVIA_BANK = [
    {"q": "How many sides does a stop sign have?",
     "a": "Eight", "b": "Six", "correct": "a"},
    {"q": "What color do you get when you mix blue and yellow paint?",
     "a": "Green", "b": "Purple", "correct": "a"},
    {"q": "Which animal is called man's best friend?",
     "a": "Cat", "b": "Dog", "correct": "b"},
    {"q": "How many days are in a leap year?",
     "a": "365", "b": "366", "correct": "b"},
    {"q": "What do bees make?",
     "a": "Honey", "b": "Jam", "correct": "a"},
    {"q": "Which season comes right after summer?",
     "a": "Spring", "b": "Fall", "correct": "b"},
    {"q": "How many legs does a spider have?",
     "a": "Eight", "b": "Ten", "correct": "a"},
    {"q": "What is the main ingredient in guacamole?",
     "a": "Tomato", "b": "Avocado", "correct": "b"},
    {"q": "Which planet is known as the Red Planet?",
     "a": "Mars", "b": "Venus", "correct": "a"},
    {"q": "How many wheels does a bicycle have?",
     "a": "Three", "b": "Two", "correct": "b"},
    {"q": "What do caterpillars turn into?",
     "a": "Butterflies", "b": "Bees", "correct": "a"},
    {"q": "Which meal do you eat in the morning?",
     "a": "Dinner", "b": "Breakfast", "correct": "b"},
    {"q": "What is H2O more commonly known as?",
     "a": "Salt", "b": "Water", "correct": "b"},
    {"q": "How many continents are there on Earth?",
     "a": "Seven", "b": "Five", "correct": "a"},
    {"q": "Which bird cannot fly but can swim?",
     "a": "Eagle", "b": "Penguin", "correct": "b"},
]


def _live_poll_json(conn, poll, viewer_id, reveal=False):
    """One live poll for the state payload / vote responses: tallies
    for both choices + the viewer's pick. `correct` is included ONLY
    when reveal=True (a closed trivia poll) — never while active."""
    counts = {r["choice"]: r["c"] for r in conn.execute(
        "SELECT choice, COUNT(*) c FROM live_poll_votes "
        "WHERE poll_id=? GROUP BY choice", (poll["id"],)).fetchall()}
    a_votes, b_votes = counts.get("a", 0), counts.get("b", 0)
    mine = conn.execute(
        "SELECT choice FROM live_poll_votes WHERE poll_id=? "
        "AND user_id=?", (poll["id"], viewer_id)).fetchone()
    out = {"id": poll["id"], "question": poll["question"],
           "a_text": poll["a_text"], "b_text": poll["b_text"],
           "a_votes": a_votes, "b_votes": b_votes,
           "total": a_votes + b_votes,
           "my_choice": mine["choice"] if mine else None,
           "is_trivia": poll["correct"] is not None,
           "active": bool(poll["active"])}
    if reveal and poll["correct"] is not None:
        correct = poll["correct"]
        out["correct"] = correct
        out["correct_text"] = (poll["a_text"] if correct == "a"
                               else poll["b_text"])
        out["winners_count"] = conn.execute(
            "SELECT COUNT(*) c FROM live_poll_votes WHERE poll_id=? "
            "AND choice=?", (poll["id"], correct)).fetchone()["c"]
    return out


def _live_poll_mod_gate(request, lid):
    """(user, live, err) for poll creation/end: the live-access gate
    plus the host-or-admin role (the live section's role helpers)."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return None, None, err
    conn = db()
    allowed = live["host_id"] == user["id"] or _live_is_admin(
        conn, lid, user["id"])
    conn.close()
    if not allowed:
        return None, None, JSONResponse(
            {"ok": False,
             "error": "Only the host or an admin can do that."},
            status_code=403)
    return user, live, None


async def _live_poll_body(request):
    """JSON body, falling back to form fields (the live comment
    endpoint's pattern)."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict) or not data:
        try:
            form = await request.form()
            data = {"question": form.get("question"),
                    "a": form.get("a"), "b": form.get("b"),
                    "correct": form.get("correct"),
                    "choice": form.get("choice")}
        except Exception:
            data = {}
    return data if isinstance(data, dict) else {}


@app.post("/live/{lid}/poll")
async def live_poll_create(request: Request, lid: int):
    """Start a poll (host/admin only). Starting one auto-closes the
    room's previous active poll — only ONE active poll per live.
    question ≤120 chars, options ≤40. correct 'a'/'b' makes it a
    trivia poll; absent/empty makes it a plain poll."""
    user, live, err = _live_poll_mod_gate(request, lid)
    if err:
        return err
    data = await _live_poll_body(request)
    question = str(data.get("question") or "").strip()[:120]
    a_text = str(data.get("a") or "").strip()[:40]
    b_text = str(data.get("b") or "").strip()[:40]
    correct = data.get("correct")
    if correct in ("", None):
        correct = None
    if correct not in (None, "a", "b"):
        return JSONResponse({"ok": False, "error": "Bad trivia answer."},
                            status_code=400)
    if not question or not a_text or not b_text:
        return JSONResponse(
            {"ok": False,
             "error": "A poll needs a question and two choices."},
            status_code=400)
    conn = db()
    conn.execute("UPDATE live_polls SET active=0 WHERE live_id=? "
                 "AND active=1", (lid,))
    cur = conn.execute(
        "INSERT INTO live_polls(live_id, question, a_text, b_text, "
        "correct, created_by, active, created_at) "
        "VALUES(?,?,?,?,?,?,1,?)",
        (lid, question, a_text, b_text, correct, user["id"], now_iso()))
    poll_id = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM live_polls WHERE id=?",
                       (poll_id,)).fetchone()
    out = _live_poll_json(conn, dict(row), user["id"])
    conn.close()
    return JSONResponse({"ok": True, "poll": out})


@app.post("/live/{lid}/poll/{poll_id}/end")
async def live_poll_end(request: Request, lid: int, poll_id: int):
    """Close a poll (host/admin only). The response + subsequent
    state polls carry it as last_closed_poll with the trivia answer
    (when any) revealed."""
    user, live, err = _live_poll_mod_gate(request, lid)
    if err:
        return err
    conn = db()
    row = conn.execute("SELECT * FROM live_polls WHERE id=? AND live_id=?",
                       (poll_id, lid)).fetchone()
    if not row:
        conn.close()
        return JSONResponse({"ok": False, "error": "not found"},
                            status_code=404)
    conn.execute("UPDATE live_polls SET active=0 WHERE id=?", (poll_id,))
    conn.commit()
    row = conn.execute("SELECT * FROM live_polls WHERE id=?",
                       (poll_id,)).fetchone()
    out = _live_poll_json(conn, dict(row), user["id"], reveal=True)
    conn.close()
    return JSONResponse({"ok": True, "poll": out})


@app.post("/live/{lid}/poll/{poll_id}/vote")
async def live_poll_vote(request: Request, lid: int, poll_id: int):
    """Vote in the room's active poll. The voter must be IN the room
    — the same gate as live commenting (_live_access: host / panel /
    watcher who may watch this live). One vote per user, changeable
    while the poll is active; a closed poll refuses with 409."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    data = await _live_poll_body(request)
    choice = str(data.get("choice") or "").strip()
    if choice not in ("a", "b"):
        return JSONResponse({"ok": False, "error": "Pick a side."},
                            status_code=400)
    conn = db()
    row = conn.execute("SELECT * FROM live_polls WHERE id=? AND live_id=?",
                       (poll_id, lid)).fetchone()
    if not row:
        conn.close()
        return JSONResponse({"ok": False, "error": "not found"},
                            status_code=404)
    if not row["active"]:
        conn.close()
        return JSONResponse({"ok": False,
                             "error": "This poll has ended."},
                            status_code=409)
    conn.execute(
        "INSERT INTO live_poll_votes(poll_id, user_id, choice) "
        "VALUES(?,?,?) ON CONFLICT(poll_id, user_id) DO UPDATE SET "
        "choice=excluded.choice", (poll_id, user["id"], choice))
    conn.commit()
    out = _live_poll_json(conn, dict(row), user["id"])
    conn.close()
    return JSONResponse({"ok": True, "poll": out, **out})


@app.get("/live/{lid}/trivia")
async def live_trivia(request: Request, lid: int):
    """A random question from the built-in trivia bank, for the
    manage sheet's 🎲 prefill button (host/admin only, like creating)."""
    user, live, err = _live_poll_mod_gate(request, lid)
    if err:
        return err
    q = secrets.choice(TRIVIA_BANK)
    return JSONResponse({"ok": True, "question": q["q"], "a": q["a"],
                         "b": q["b"], "correct": q["correct"]})


# ---------------- live room games (Games Pack 2) ----------------
# Host/admin-run games the whole room plays. 'guess': the server
# picks a secret number 1-100 at start; players set/replace their
# guess; the closest guess(es) win. 'dice': every play rolls a
# server-side die 1-6; a player's best roll is their score and the
# highest best wins. The guess secret lives ONLY in live_games.secret
# server-side: _live_game_json (the active payload) has no 'secret'
# key at all; the secret is revealed only in _live_game_result_json
# once the game has ended. Only ONE active game per live — starting
# a new one ends the previous, and ending the live ends its game.


async def _live_game_body(request):
    """JSON body, falling back to form fields (the poll pattern)."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict) or not data:
        try:
            form = await request.form()
            data = {"kind": form.get("kind"), "guess": form.get("guess")}
        except Exception:
            data = {}
    return data if isinstance(data, dict) else {}


def _live_game_players(conn, game):
    """The game's player rows for payloads: [{name, display}].
    guess display = the player's guess (a number, public once made);
    dice display = '🎲 best {best} ({plays} rolls)'. Dice standings
    sort by best (then most rolls); guesses sort low to high."""
    rows = conn.execute(
        "SELECT p.*, u.name FROM live_game_plays p LEFT JOIN users u "
        "ON u.id=p.user_id WHERE p.game_id=?", (game["id"],)).fetchall()
    out = []
    for r in rows:
        if game["kind"] == "dice":
            display = (f"🎲 best {r['best'] or 0} "
                       f"({r['plays']} roll{'s' if r['plays'] != 1 else ''})")
            key = (-(r["best"] or 0), -(r["plays"] or 0))
        else:
            display = r["value"]
            key = (r["value"] if r["value"] is not None else 101,)
        out.append({"name": r["name"] or "Deleted user",
                    "display": display, "_key": key})
    out.sort(key=lambda p: p["_key"])
    for p in out:
        del p["_key"]
    return out


def _live_game_json(conn, game, viewer_id):
    """The ACTIVE game for the state payload / play responses: who
    is playing and what they show, plus the viewer's own standing.
    NEVER includes the secret — there is no 'secret' key here."""
    mine = conn.execute(
        "SELECT * FROM live_game_plays WHERE game_id=? AND user_id=?",
        (game["id"], viewer_id)).fetchone()
    my_play = None
    if mine:
        my_play = (mine["best"] if game["kind"] == "dice"
                   else mine["value"])
    return {"id": game["id"], "kind": game["kind"],
            "players": _live_game_players(conn, game),
            "my_play": my_play}


def _live_game_winners(conn, game):
    """(winner names, winning value) for an ENDED game. guess: the
    smallest |guess - secret| wins, all ties share the win. dice:
    the highest best roll wins, ties share. No plays -> no winners."""
    rows = conn.execute(
        "SELECT p.*, u.name FROM live_game_plays p LEFT JOIN users u "
        "ON u.id=p.user_id WHERE p.game_id=?", (game["id"],)).fetchall()
    if not rows:
        return [], None
    if game["kind"] == "guess":
        secret = game["secret"]
        best_dist = min(abs(r["value"] - secret) for r in rows)
        return ([(r["name"] or "Deleted user") for r in rows
                 if abs(r["value"] - secret) == best_dist], best_dist)
    top = max(r["best"] or 0 for r in rows)
    return ([(r["name"] or "Deleted user") for r in rows
             if (r["best"] or 0) == top], top)


def _live_game_result_json(conn, game):
    """An ENDED game for the payload: winners + a summary; the guess
    secret is revealed here (and only here / the end response)."""
    winners, _val = _live_game_winners(conn, game)
    if game["kind"] == "guess":
        summary = f"The number was {game['secret']}."
    else:
        top = conn.execute(
            "SELECT MAX(best) m FROM live_game_plays WHERE game_id=?",
            (game["id"],)).fetchone()["m"]
        summary = (f"The best roll was {top}." if top is not None
                   else "Nobody rolled.")
    out = {"id": game["id"], "kind": game["kind"],
           "summary": summary, "winners": winners}
    if game["kind"] == "guess":
        out["secret"] = game["secret"]
    return out


def _live_active_game(conn, lid):
    r = conn.execute(
        "SELECT * FROM live_games WHERE live_id=? AND status='active' "
        "ORDER BY id DESC LIMIT 1", (lid,)).fetchone()
    return dict(r) if r else None


@app.post("/live/{lid}/game/start")
async def live_game_start(request: Request, lid: int):
    """Start a room game (host/admin only — the poll mod gate).
    kind 'guess' deals a fresh server-side secret 1-100; 'dice'
    needs none. Starting auto-ends the room's active game."""
    user, live, err = _live_poll_mod_gate(request, lid)
    if err:
        return err
    data = await _live_game_body(request)
    kind = str(data.get("kind") or "").strip()
    if kind not in ("guess", "dice"):
        return JSONResponse({"ok": False, "error": "Unknown game."},
                            status_code=400)
    conn = db()
    stamp = now_iso()
    conn.execute(
        "UPDATE live_games SET status='ended', ended_at=? "
        "WHERE live_id=? AND status='active'", (stamp, lid))
    secret = secrets.randbelow(100) + 1 if kind == "guess" else None
    cur = conn.execute(
        "INSERT INTO live_games(live_id, kind, secret, status, "
        "created_by, created_at) VALUES(?,?,?,'active',?,?)",
        (lid, kind, secret, user["id"], stamp))
    game_id = cur.lastrowid
    conn.commit()
    row = conn.execute("SELECT * FROM live_games WHERE id=?",
                       (game_id,)).fetchone()
    out = _live_game_json(conn, dict(row), user["id"])
    conn.close()
    return JSONResponse({"ok": True, "game": out})


@app.post("/live/{lid}/game/play")
async def live_game_play(request: Request, lid: int):
    """Play in the room's active game. The player must be IN the
    room — the same gate as poll voting (_live_access). guess:
    {guess} integer 1-100 sets/replaces my guess (else 400). dice:
    the body is ignored; the server rolls 1-6 and updates my row
    (value=last roll, best=max, plays+1). No active game -> 409."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    conn = db()
    game = _live_active_game(conn, lid)
    if not game:
        conn.close()
        return JSONResponse({"ok": False,
                             "error": "No game is running."},
                            status_code=409)
    roll = None
    if game["kind"] == "guess":
        data = await _live_game_body(request)
        raw = data.get("guess")
        if isinstance(raw, bool) or raw is None:
            conn.close()
            return JSONResponse(
                {"ok": False, "error": "Guess a number 1-100."},
                status_code=400)
        try:
            guess = int(raw)
        except (TypeError, ValueError):
            conn.close()
            return JSONResponse(
                {"ok": False, "error": "Guess a number 1-100."},
                status_code=400)
        if isinstance(raw, float) and not raw.is_integer():
            conn.close()
            return JSONResponse(
                {"ok": False, "error": "Guess a number 1-100."},
                status_code=400)
        if guess < 1 or guess > 100:
            conn.close()
            return JSONResponse(
                {"ok": False, "error": "Guess a number 1-100."},
                status_code=400)
        conn.execute(
            "INSERT INTO live_game_plays(game_id, user_id, value) "
            "VALUES(?,?,?) ON CONFLICT(game_id, user_id) DO UPDATE "
            "SET value=excluded.value", (game["id"], user["id"], guess))
    else:
        roll = secrets.randbelow(6) + 1
        conn.execute(
            "INSERT INTO live_game_plays(game_id, user_id, value, "
            "best, plays) VALUES(?,?,?, ?,1) ON CONFLICT(game_id, "
            "user_id) DO UPDATE SET value=excluded.value, "
            "best=MAX(COALESCE(live_game_plays.best, 0), "
            "excluded.best), plays=live_game_plays.plays+1",
            (game["id"], user["id"], roll, roll))
    conn.commit()
    out = _live_game_json(conn, game, user["id"])
    conn.close()
    resp = {"ok": True, "game": out}
    if roll is not None:
        resp["roll"] = roll
    return JSONResponse(resp)


@app.post("/live/{lid}/game/end")
async def live_game_end(request: Request, lid: int):
    """End the room's active game (host/admin only) and settle it:
    guess -> closest guess(es) win; dice -> highest best wins, ties
    share. The response is the result payload (secret revealed)."""
    user, live, err = _live_poll_mod_gate(request, lid)
    if err:
        return err
    conn = db()
    game = _live_active_game(conn, lid)
    if not game:
        conn.close()
        return JSONResponse({"ok": False,
                             "error": "No game is running."},
                            status_code=404)
    conn.execute(
        "UPDATE live_games SET status='ended', ended_at=? WHERE id=?",
        (now_iso(), game["id"]))
    conn.commit()
    row = conn.execute("SELECT * FROM live_games WHERE id=?",
                       (game["id"],)).fetchone()
    out = _live_game_result_json(conn, dict(row))
    conn.close()
    return JSONResponse({"ok": True, "game": out, **out})


@app.post("/live/{lid}/comment")
async def live_comment(request: Request, lid: int):
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    _cg = db()
    _gated = _entry_gate(_cg, live, user,
                         _live_panel_ids(_cg, live),
                         is_owner(request))
    _cg.close()
    if _gated:
        return JSONResponse(
            {"ok": False,
             "error": "🎟 This live has a ticket door — pay the "
                      "entry to join the chat."},
            status_code=403)
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict) or not data:
        try:
            form = await request.form()
            data = {"body": form.get("body")}
        except Exception:
            data = {}
    body = str(data.get("body") or "").strip()[:LIVE_COMMENT_MAX]
    if not body:
        return JSONResponse({"ok": False, "error": "Empty comment."},
                            status_code=400)
    conn = db()
    cur = conn.execute(
        "INSERT INTO live_comments(live_id, user_id, body, created_at) "
        "VALUES(?,?,?,?)", (lid, user["id"], body, now_iso()))
    cid = cur.lastrowid
    conn.commit()
    row = conn.execute(
        "SELECT lc.*, u.name, u.avatar, u.founding_no, u.plus_until "
        "FROM live_comments lc "
        "LEFT JOIN users u ON u.id=lc.user_id WHERE lc.id=?",
        (cid,)).fetchone()
    viewer_admin = _live_is_admin(conn, lid, user["id"])
    my_rank = _gifter_rank(_gift_sent_total(conn, user["id"]))
    conn.close()
    flag_item("live_comment", cid, user["id"], user["name"], body)
    return JSONResponse({"ok": True, "comment": _live_comment_json(
        dict(row), user["id"], live["host_id"],
        viewer_is_admin=viewer_admin, rank=my_rank)})


@app.post("/live/{lid}/comment/{cid}/delete")
async def live_comment_delete(request: Request, lid: int, cid: int):
    """The comment's author can delete it; so can the live's host
    and any admin of this live — they moderate the room's chat."""
    user, live, err = _live_access(request, lid)
    if err:
        return err
    conn = db()
    row = conn.execute(
        "SELECT * FROM live_comments WHERE id=? AND live_id=?",
        (cid, lid)).fetchone()
    if not row:
        conn.close()
        return JSONResponse({"ok": False, "error": "not found"},
                            status_code=404)
    if row["user_id"] != user["id"] and live["host_id"] != user["id"] \
            and not _live_is_admin(conn, lid, user["id"]):
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Only the author, the host, or an admin can "
                      "delete this."},
            status_code=403)
    conn.execute("DELETE FROM live_comments WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


# ---------------- Build PAGE GIFTS: gift a profile or a Page --------
# Ken's order: the gift economy leaves the live room. A 🎁 Gift
# button on every profile and every Page sends the SAME catalog
# gifts at the SAME prices; the recipient — a profile's owner, or
# a Page's owner_id, never anyone else — earns into the SAME
# gift_earned wallet as live money. One core (_page_gift_core) runs
# both routes: the same guarded atomic debit as the live handler,
# the recipient's gift_earned + gift_lifetime credited, one
# page_gifts row, and two ledger rows stamped note='page' (with
# live_id NULL) so /earnings shows a 🎁 Page gifts line and
# /tickets reads "Received … on your page". Rules mirror the live
# path: no self-gifts, a block in either direction refuses, the
# sender must be able to VIEW the target (a private profile is
# friends-only; a Page must exist), and the FREE neon_heart rides
# the same 1-per-2s guard — scoped per sender+recipient and checked
# against the page_gifts row the last free gift wrote (the live
# guard checks live_gifts; the two never share a table, and neither
# is weakened by the other).


def _page_gift_ctx(user, endpoint, recipient_name):
    """Template context for the 🎁 Gift button + sheet (the shared
    partials/page_gift.html): the viewer's balance and the full
    catalog copy (prices live server-side in GIFT_CATALOG; the
    sheet renders from this, sends keys only)."""
    me = get_user(user["id"]) or {}
    gifts = []
    for k in GIFT_ORDER:
        g = GIFT_CATALOG[k]
        gifts.append({"key": k, "name": g["name"],
                      "tickets": g["tickets"],
                      "category": g["category"], "img": g["img"],
                      "anim": g["anim"]})
    return {"endpoint": endpoint, "recipient_name": recipient_name,
            "balance": me.get("tickets_balance") or 0,
            "gifts": gifts}


def _recent_page_gifts(recipient_id, page_id, limit=6):
    """The Recent gifts strip rows for one profile (page_id NULL)
    or one Page: latest first, sender face + gift face, ready for
    templates/partials/recent_gifts.html."""
    conn = db()
    if page_id is None:
        rows = conn.execute(
            "SELECT g.*, u.name AS sender_name, "
            "u.avatar AS sender_avatar FROM page_gifts g "
            "LEFT JOIN users u ON u.id = g.sender_id "
            "WHERE g.recipient_id=? AND g.page_id IS NULL "
            "ORDER BY g.id DESC LIMIT ?",
            (recipient_id, limit)).fetchall()
    else:
        rows = conn.execute(
            "SELECT g.*, u.name AS sender_name, "
            "u.avatar AS sender_avatar FROM page_gifts g "
            "LEFT JOIN users u ON u.id = g.sender_id "
            "WHERE g.page_id=? ORDER BY g.id DESC LIMIT ?",
            (page_id, limit)).fetchall()
    conn.close()
    out = []
    for r in rows:
        out.append({
            "sender_id": r["sender_id"],
            "sender_name": r["sender_name"] or "Someone",
            "sender_avatar": avatar_url(r["sender_avatar"] or ""),
            "gift": _gift_public(r["gift_key"], r["tickets"]),
            "when": fancy_time(r["created_at"])})
    return out


def _invite_bonus(conn, earner_id, tickets, stamp):
    """Invite bonus: when a member EARNS tickets, whoever invited
    them earns 10% on top — minted by HERE out of HERE's side, never
    deducted from the earner. Rides the caller's transaction (the
    caller commits). Floor division: gifts under 10 tickets pay no
    bonus. Pays into gift_earned so it cashes out like any earning;
    deliberately NOT into gift_lifetime (streamer levels measure fan
    love, not recruiting)."""
    if not tickets or tickets < 10:
        return
    row = conn.execute("SELECT invited_by FROM users WHERE id=?",
                       (earner_id,)).fetchone()
    if not row or not row["invited_by"]:
        return
    inviter_id = row["invited_by"]
    if inviter_id == earner_id:
        return
    bonus = tickets // 10
    if bonus <= 0:
        return
    conn.execute(
        "UPDATE users SET gift_earned = gift_earned + ? WHERE id=?",
        (bonus, inviter_id))
    conn.execute(
        "INSERT INTO ticket_txns(user_id, kind, tickets, live_id, "
        "gift_key, note, created_at) VALUES(?,?,?,?,?,?,?)",
        (inviter_id, "invite_bonus", bonus, None, "",
         f"from user {earner_id}", stamp))


def _paid_total_cents(conn):
    """Every dollar HERE has actually paid a creator, ever."""
    row = conn.execute(
        "SELECT COALESCE(SUM(dollars_cents),0) AS s FROM "
        "payout_requests WHERE status='paid'").fetchone()
    return int(row["s"] or 0)


def _page_gift_core(user, recipient_id, recipient_name, page_id,
                    gift):
    """The one money-moving transaction for a page gift, entered
    only after every view/self/block gate has passed. Returns
    (status_code, payload) — the routes just JSON it."""
    cost = gift["tickets"]
    conn = db()
    if cost == 0:
        # FREE gift anti-spam: free gifts move no ledger, so the
        # balance guard can't slow a spam tap — cap them at one
        # per sender per recipient per 2 seconds instead (checked
        # against the page_gifts row the last one wrote).
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=2)).isoformat()
        recent = conn.execute(
            "SELECT COUNT(*) c FROM page_gifts WHERE sender_id=? "
            "AND recipient_id=? AND tickets=0 AND created_at>?",
            (user["id"], recipient_id, cutoff)).fetchone()["c"]
        if recent:
            conn.close()
            return 429, {"ok": False,
                         "error": "Easy — one free gift every "
                                  "couple of seconds 💜"}
    cur = conn.execute(
        "UPDATE users SET tickets_balance = "
        "COALESCE(tickets_balance,0) - ? WHERE id=? AND "
        "COALESCE(tickets_balance,0) >= ?", (cost, user["id"], cost))
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        return 400, {"ok": False,
                     "error": "Not enough tickets — grab a pack "
                              "first 🎟️"}
    conn.execute(
        "UPDATE users SET gift_earned = COALESCE(gift_earned,0) + ?, "
        "gift_lifetime = COALESCE(gift_lifetime,0) + ? WHERE id=?",
        (cost, cost, recipient_id))
    stamp = now_iso()
    cur = conn.execute(
        "INSERT INTO page_gifts(sender_id, recipient_id, page_id, "
        "gift_key, tickets, created_at) VALUES(?,?,?,?,?,?)",
        (user["id"], recipient_id, page_id, gift["key"], cost,
         stamp))
    gift_id = cur.lastrowid
    conn.execute(
        "INSERT INTO ticket_txns(user_id, kind, tickets, live_id, "
        "gift_key, note, created_at) VALUES(?,?,?,?,?,?,?)",
        (user["id"], "gift_send", -cost, None, gift["key"], "page",
         stamp))
    conn.execute(
        "INSERT INTO ticket_txns(user_id, kind, tickets, live_id, "
        "gift_key, note, created_at) VALUES(?,?,?,?,?,?,?)",
        (recipient_id, "gift_receive", cost, None, gift["key"],
         "page", stamp))
    _invite_bonus(conn, recipient_id, cost, stamp)
    conn.commit()
    bal = conn.execute(
        "SELECT tickets_balance FROM users WHERE id=?",
        (user["id"],)).fetchone()["tickets_balance"]
    conn.close()
    notify(recipient_id, "page_gift",
           f"{user['name']} sent you {gift['name']} 🎁",
           f"/user/{user['id']}", actor_id=user["id"])
    return 200, {"ok": True, "balance": bal or 0,
                 "gift_id": gift_id, "recipient_id": recipient_id,
                 "recipient_name": recipient_name}


async def _page_gift_parse(request):
    """(user, gift) or (None, error_response) — the shared front
    door of both page-gift routes: login + a known catalog key."""
    user = current_user(request)
    if not user:
        return None, None, JSONResponse(
            {"ok": False, "error": "Log in first."},
            status_code=401)
    try:
        data = await request.json()
    except Exception:
        data = {}
    gift_key = str((data or {}).get("gift_key") or "")
    gift = GIFT_CATALOG.get(gift_key)
    if not gift:
        return None, None, JSONResponse(
            {"ok": False, "error": "Unknown gift."},
            status_code=400)
    return user, gift, None


@app.post("/user/{uid}/gift")
async def user_page_gift(request: Request, uid: int):
    """🎁 Gift a profile: the profile's owner earns. Creator Mode
    gates it first — no creator flag, no gifts. The other gates
    mirror the profile itself — a blocked pair gets the
    unavailable answer, a private profile is friends-only (the
    owner context sees through both, like the steal-look
    route)."""
    user, gift, err = await _page_gift_parse(request)
    if err:
        return err
    prof = get_user(uid)
    if not prof:
        return JSONResponse(
            {"ok": False, "error": "That profile isn't available."},
            status_code=400)
    if not prof.get("creator_mode"):
        return JSONResponse(
            {"ok": False,
             "error": "That profile can't take gifts — Creator "
                      "Mode is off."},
            status_code=403)
    if uid == user["id"]:
        return JSONResponse(
            {"ok": False, "error": "You can't gift yourself."},
            status_code=400)
    owner_on = is_owner(request)
    if not owner_on and blocked_either_way(user["id"], uid):
        return JSONResponse(
            {"ok": False, "error": "That profile isn't available."},
            status_code=400)
    if prof.get("private_profile") and not owner_on \
            and not is_friend(user["id"], uid):
        return JSONResponse(
            {"ok": False,
             "error": "That profile is private — only their "
                      "friends can send them gifts."},
            status_code=403)
    code, payload = _page_gift_core(user, uid, prof["name"], None,
                                    gift)
    return JSONResponse(payload, status_code=code)


def _post_gift_ctx(user):
    """Sheet context for post/Peep gifts (partials/post_gift.html):
    the viewer's balance + the catalog. The endpoint is dynamic —
    the sheet posts to /post/{id}/gift for whichever post the
    reader tapped, so there is no fixed recipient here."""
    me = get_user(user["id"]) or {}
    gifts = []
    for k in GIFT_ORDER:
        g = GIFT_CATALOG[k]
        gifts.append({"key": k, "name": g["name"],
                      "tickets": g["tickets"],
                      "category": g["category"], "img": g["img"],
                      "anim": g["anim"]})
    return {"balance": me.get("tickets_balance") or 0,
            "gifts": gifts}


def _post_gift_core(user, post, gift):
    """The money move for a post/Peep gift — the page-gift
    transaction with a post attached: guarded debit, the AUTHOR
    earns into gift_earned (+lifetime), a post_gifts row, and the
    twin ledger lines stamped note='post'. Free gifts move no
    ledger and ride the same 1-per-2s anti-spam guard, scoped per
    sender+post."""
    cost = gift["tickets"]
    recipient_id = post["user_id"]
    conn = db()
    if cost == 0:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=2)).isoformat()
        recent = conn.execute(
            "SELECT COUNT(*) c FROM post_gifts WHERE sender_id=? "
            "AND post_id=? AND tickets=0 AND created_at>?",
            (user["id"], post["id"], cutoff)).fetchone()["c"]
        if recent:
            conn.close()
            return 429, {"ok": False,
                         "error": "Easy — one free gift every "
                                  "couple of seconds 💜"}
    cur = conn.execute(
        "UPDATE users SET tickets_balance = "
        "COALESCE(tickets_balance,0) - ? WHERE id=? AND "
        "COALESCE(tickets_balance,0) >= ?", (cost, user["id"], cost))
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        return 400, {"ok": False,
                     "error": "Not enough tickets — grab a pack "
                              "first 🎟️"}
    conn.execute(
        "UPDATE users SET gift_earned = COALESCE(gift_earned,0) "
        "+ ?, gift_lifetime = COALESCE(gift_lifetime,0) + ? "
        "WHERE id=?", (cost, cost, recipient_id))
    stamp = now_iso()
    cur = conn.execute(
        "INSERT INTO post_gifts(sender_id, recipient_id, post_id, "
        "gift_key, tickets, created_at) VALUES(?,?,?,?,?,?)",
        (user["id"], recipient_id, post["id"], gift["key"], cost,
         stamp))
    gift_id = cur.lastrowid
    conn.execute(
        "INSERT INTO ticket_txns(user_id, kind, tickets, live_id, "
        "gift_key, note, created_at) VALUES(?,?,?,?,?,?,?)",
        (user["id"], "gift_send", -cost, None, gift["key"], "post",
         stamp))
    conn.execute(
        "INSERT INTO ticket_txns(user_id, kind, tickets, live_id, "
        "gift_key, note, created_at) VALUES(?,?,?,?,?,?,?)",
        (recipient_id, "gift_receive", cost, None, gift["key"],
         "post", stamp))
    _invite_bonus(conn, recipient_id, cost, stamp)
    conn.commit()
    bal = conn.execute(
        "SELECT tickets_balance FROM users WHERE id=?",
        (user["id"],)).fetchone()["tickets_balance"]
    conn.close()
    author = get_user(recipient_id) or {}
    notify(recipient_id, "post_gift",
           f"{user['name']} sent you {gift['name']} on your post 🎁",
           f"/post/{post['id']}", actor_id=user["id"])
    return 200, {"ok": True, "balance": bal or 0,
                 "gift_id": gift_id,
                 "recipient_id": recipient_id,
                 "recipient_name": author.get("name") or "them"}


@app.post("/post/{pid}/gift")
async def post_gift(request: Request, pid: int):
    """🎁 Gift a post or a Peep: the AUTHOR earns. Gates mirror
    the post itself (can_view_post) plus the profile-gift rules:
    the author must run Creator Mode, no self-gifts, no gifts
    across a block either way."""
    user, gift, err = await _page_gift_parse(request)
    if err:
        return err
    conn = db()
    p = conn.execute("SELECT * FROM posts WHERE id=?",
                     (pid,)).fetchone()
    conn.close()
    if not p:
        return JSONResponse(
            {"ok": False, "error": "That post isn't available."},
            status_code=400)
    post = dict(p)
    if post.get("shared_post_id"):
        post_id = post["shared_post_id"]
        conn = db()
        p2 = conn.execute("SELECT * FROM posts WHERE id=?",
                          (post_id,)).fetchone()
        conn.close()
        if not p2:
            return JSONResponse(
                {"ok": False, "error": "That post isn't available."},
                status_code=400)
        post = dict(p2)
    author = get_user(post["user_id"])
    if not author:
        return JSONResponse(
            {"ok": False, "error": "That post isn't available."},
            status_code=400)
    if not author.get("creator_mode"):
        return JSONResponse(
            {"ok": False,
             "error": "That creator can't take gifts — Creator "
                      "Mode is off."},
            status_code=403)
    if post["user_id"] == user["id"]:
        return JSONResponse(
            {"ok": False, "error": "You can't gift your own post."},
            status_code=400)
    owner_on = is_owner(request)
    if not owner_on and blocked_either_way(user["id"],
                                            post["user_id"]):
        return JSONResponse(
            {"ok": False, "error": "That post isn't available."},
            status_code=400)
    if not can_view_post(user, post, owner_on):
        return JSONResponse(
            {"ok": False, "error": "That post isn't available."},
            status_code=400)
    code, payload = _post_gift_core(user, post, gift)
    return JSONResponse(payload, status_code=code)


# ---------------- Media HERE: profile music ----------------
MEDIA_FREE_TRACKS = 10
MEDIA_PLUS_TRACKS = 50
MEDIA_MAX_MB = 20
MEDIA_EXTS = {"mp3", "m4a", "ogg", "wav"}
MEDIA_MODES = ("", "music", "artist")


def _media_cap(user):
    return (MEDIA_PLUS_TRACKS if _is_plus(user)
            else MEDIA_FREE_TRACKS)


def _media_tracks_for(uid, viewer_id=None):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM media_tracks WHERE user_id=? ORDER BY id",
        (uid,)).fetchall()
    owner = get_user(uid) or {}
    giftable_all = bool(owner.get("artist_verified")
                        and owner.get("creator_mode"))
    out = []
    for r in rows:
        likes = conn.execute(
            "SELECT COUNT(*) c FROM media_track_likes "
            "WHERE track_id=?", (r["id"],)).fetchone()["c"]
        liked = bool(viewer_id and conn.execute(
            "SELECT 1 FROM media_track_likes WHERE track_id=? "
            "AND user_id=?", (r["id"], viewer_id)).fetchone())
        out.append({"id": r["id"], "title": r["title"],
                    "src": avatar_url(r["file"]),
                    "rights": r["rights"],
                    "plays": r["plays"] or 0,
                    "likes": likes, "liked": liked,
                    "giftable": bool(giftable_all
                                     and r["rights"] == "own")})
    conn.close()
    return out


@app.post("/settings/media-mode")
async def settings_media_mode(request: Request):
    user, err = require_login(request)
    if err:
        return err
    form = await request.form()
    mode = str(form.get("media_mode") or "")
    if mode not in MEDIA_MODES:
        mode = ""
    conn = db()
    conn.execute("UPDATE users SET media_mode=? WHERE id=?",
                 (mode, user["id"]))
    conn.commit()
    conn.close()
    return redir("/settings/profile?media=mode#media-here")


@app.post("/settings/media-skin")
async def settings_media_skin(request: Request):
    user, err = require_login(request)
    if err:
        return err
    form = await request.form()
    up = form.get("skin")
    try:
        rel = save_upload(up, "media-skins",
                          {"jpg", "jpeg", "png", "webp", "gif"},
                          max_mb=8)
    except ValueError:
        return redir("/settings/profile?media=err-skin"
                     "#media-here")
    if not rel:
        return redir("/settings/profile?media=err-skin"
                     "#media-here")
    conn = db()
    conn.execute("UPDATE users SET media_skin=? WHERE id=?",
                 (rel, user["id"]))
    conn.commit()
    conn.close()
    return redir("/settings/profile?media=skin#media-here")


@app.post("/media/upload")
async def media_upload(request: Request):
    user, err = require_login(request)
    if err:
        return err
    back = f"/user/{user['id']}"
    if not (user.get("media_mode") or ""):
        return redir(back + "?media=err-mode")
    form = await request.form()
    title = str(form.get("title") or "").strip()[:120]
    rights = str(form.get("rights") or "")
    if rights not in ("own", "notown"):
        return redir(back + "?media=err-rights")
    conn = db()
    n = conn.execute(
        "SELECT COUNT(*) c FROM media_tracks WHERE user_id=?",
        (user["id"],)).fetchone()["c"]
    conn.close()
    if n >= _media_cap(user):
        return redir(back + "?media=err-cap")
    up = form.get("file")
    try:
        rel = save_upload(up, "media", MEDIA_EXTS,
                          max_mb=MEDIA_MAX_MB)
    except ValueError:
        return redir(back + "?media=err-type")
    if not rel:
        return redir(back + "?media=err-type")
    if not title:
        title = (getattr(up, "filename", "") or "Untitled")
        title = title.rsplit(".", 1)[0][:120] or "Untitled"
    conn = db()
    conn.execute(
        "INSERT INTO media_tracks(user_id, title, file, rights, "
        "created_at) VALUES(?,?,?,?,?)",
        (user["id"], title, rel, rights, now_iso()))
    conn.commit()
    conn.close()
    return redir(back + "?media=ok")


@app.post("/media/{tid}/delete")
async def media_delete(request: Request, tid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    row = conn.execute("SELECT * FROM media_tracks WHERE id=?",
                       (tid,)).fetchone()
    if row and (row["user_id"] == user["id"]
                or is_owner(request)):
        conn.execute("DELETE FROM media_tracks WHERE id=?",
                     (tid,))
        conn.commit()
        try:
            os.remove(os.path.join(UPLOAD_DIR, row["file"]))
        except OSError:
            pass
    conn.close()
    return redir(f"/user/{user['id']}?media=deleted")


@app.post("/media/{tid}/play")
async def media_play(request: Request, tid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    conn.execute("UPDATE media_tracks SET plays=plays+1 "
                 "WHERE id=?", (tid,))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


# ---------------- THE LABEL: Verified Artists ----------------
ARTIST_PLAYS_TARGET = 40000
LABEL_FEE_PCT = 3

CONTRACT_TERMS_V1 = [
    ("1. Who this deal is between",
     "You (the Artist) and HERE (the platform, run by its "
     "owner). This agreement starts the day you sign it below "
     "and runs until either side ends it (section 7)."),
    ("2. What HERE gives you",
     "The Verified Artist badge on your profile; eligibility "
     "for sponsorship, promotion and marketing on HERE, at "
     "HERE's discretion; eligibility for support from the "
     "Artist Fund (section 6); and the platform's tools to "
     "release music, gather fans and get paid."),
    ("3. What you give HERE",
     "Your work. Bring your people: your fans joining HERE is "
     "how the money pool fills — for you and for every artist "
     "here. Keep releasing, keep showing up, and represent "
     "the label well while you are on the platform."),
    ("4. Your music stays YOUR music",
     "You keep 100% of the rights to your music. HERE claims "
     "no ownership of your songs, your name or your image "
     "beyond showing them on the platform to promote you. "
     "Only claim 'I own the rights' on music that is truly "
     "yours — claiming rights you do not hold is a breach."),
    ("5. The money",
     "Fans pay you through HERE's ticket gifts at the "
     "platform's standard rate: every ticket a fan gifts you "
     "earns you half a cent, cashed out from $10 up through "
     "Stripe, exactly like every creator on HERE. While this "
     "contract is signed, a label fee of 3% comes out of your "
     "cash-outs — it funds your marketing and the Artist "
     "Fund. Everything you earn is yours; see section 7 for "
     "what happens if the deal ends."),
    ("6. The Artist Fund",
     "HERE keeps a fund, fed by label fees, that it may use "
     "to sponsor artists — promotion, features, support — at "
     "HERE's sole discretion. Being signed makes you "
     "eligible; it does not promise any artist a payment "
     "from the fund."),
    ("7. Ending the deal — either side, any time",
     "You may end this contract whenever you like, from your "
     "contract page, no penalty and no questions. HERE may "
     "end it for a breach (section 8), or for any reason with "
     "14 days' notice in the app. WHEN IT ENDS, FROM EITHER "
     "SIDE: the badge's label perks and the 3% fee stop, and "
     "everything in your wallet stays yours — your earned "
     "tickets remain yours to cash out under the normal "
     "rules. Nobody can take your earnings back."),
    ("8. Breaches — what ends it fast",
     "Fake plays, fake likes, fake gifts or any other rigged "
     "numbers; claiming rights to music that is not yours; "
     "breaking HERE's rules; or using the badge to scam "
     "people. A breach lets HERE end the contract and the "
     "Verified Artist badge on the spot."),
    ("9. Plain words",
     "This is the whole deal, written plain on purpose. If "
     "HERE ever offers you new terms, they apply only if you "
     "sign the new version. Your signed version governs you "
     "until then."),
]
CONTRACT_VERSIONS = {"v1": CONTRACT_TERMS_V1}


def _artist_stats(uid):
    conn = db()
    plays = conn.execute(
        "SELECT COALESCE(SUM(plays),0) s FROM media_tracks "
        "WHERE user_id=?", (uid,)).fetchone()["s"]
    likes = conn.execute(
        "SELECT COUNT(*) c FROM media_track_likes l JOIN "
        "media_tracks t ON t.id = l.track_id WHERE t.user_id=?",
        (uid,)).fetchone()["c"]
    gifts = conn.execute(
        "SELECT COALESCE(SUM(tickets),0) s FROM track_gifts "
        "WHERE recipient_id=?", (uid,)).fetchone()["s"]
    tracks = conn.execute(
        "SELECT COUNT(*) c FROM media_tracks WHERE user_id=?",
        (uid,)).fetchone()["c"]
    conn.close()
    return {"plays": plays or 0, "likes": likes or 0,
            "gift_tickets": gifts or 0, "tracks": tracks or 0}


def _fund_cents(conn):
    row = conn.execute(
        "SELECT value FROM app_settings WHERE "
        "key='artist_fund_cents'").fetchone()
    try:
        return int(row["value"]) if row else 0
    except (TypeError, ValueError):
        return 0


def _fund_set(conn, cents):
    conn.execute(
        "INSERT OR REPLACE INTO app_settings(key, value) "
        "VALUES('artist_fund_cents', ?)",
        (str(max(0, int(cents))),))


def _open_contract(conn, uid):
    return conn.execute(
        "SELECT * FROM artist_contracts WHERE user_id=? AND "
        "status IN ('offered','signed') ORDER BY id DESC",
        (uid,)).fetchone()


def _pdf_escape(s):
    return (str(s).replace("\\", "\\\\")
            .replace("(", "\\(").replace(")", "\\)"))


def _contract_pdf_bytes(title, lines):
    """A tiny dependency-free text PDF: (text, size, bold)
    lines wrapped at 86 chars, paginated. Deterministic."""
    import textwrap as _tw
    flat = []
    for text, size, bold in lines:
        wrapped = _tw.wrap(text, width=86) or [""]
        for i, w in enumerate(wrapped):
            flat.append((w, size, bold))
        flat.append(("", 6, False))
    pages, cur = [], []
    for ln in flat:
        cur.append(ln)
        if len(cur) >= 44:
            pages.append(cur)
            cur = []
    if cur:
        pages.append(cur)
    objs = []
    objs.append("<< /Type /Catalog /Pages 2 0 R >>")
    kids = " ".join(f"{3 + i * 2} 0 R" for i in range(len(pages)))
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count "
                f"{len(pages)} >>")
    for pi, page in enumerate(pages):
        content = ["BT", "50 780 Td"]
        for text, size, bold in page:
            font = "F2" if bold else "F1"
            content.append(f"/{font} {size} Tf")
            content.append(f"0 -{size + 5} Td")
            if text:
                content.append(f"({_pdf_escape(text)}) Tj")
        content.append("ET")
        stream = "\n".join(content)
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox "
            f"[0 0 612 792] /Resources << /Font << /F1 "
            f"{3 + len(pages) * 2} 0 R /F2 "
            f"{4 + len(pages) * 2} 0 R >> >> /Contents "
            f"{4 + pi * 2} 0 R >>")
        objs.append(f"<< /Length {len(stream)} >>\nstream\n"
                    f"{stream}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 "
                "/BaseFont /Helvetica >>")
    objs.append("<< /Type /Font /Subtype /Type1 "
                "/BaseFont /Helvetica-Bold >>")
    out = ["%PDF-1.4"]
    offsets = []
    pos = len("%PDF-1.4\n")
    for i, body in enumerate(objs):
        offsets.append(pos)
        chunk = f"{i + 1} 0 obj\n{body}\nendobj\n"
        out.append(chunk)
        pos += len(chunk)
    xref = pos
    tail = ["xref", f"0 {len(objs) + 1}",
            "0000000000 65535 f "]
    for off in offsets:
        tail.append(f"{off:010d} 00000 n ")
    tail += ["trailer",
             f"<< /Size {len(objs) + 1} /Root 1 0 R >>",
             "startxref", str(xref), "%%EOF"]
    out.append("\n".join(tail))
    return "\n".join(out).encode("latin-1", "replace")



# ---------------- Label routes ----------------

@app.post("/settings/artist-apply")
async def artist_apply(request: Request):
    user, err = require_login(request)
    if err:
        return err
    back = "/settings/profile?artist={}#artist-card"
    if (user.get("media_mode") or "") != "artist":
        return redir(back.format("err-mode"))
    if user.get("artist_verified"):
        return redir(back.format("err-already"))
    form = await request.form()
    note = str(form.get("note") or "").strip()[:400]
    conn = db()
    pending = conn.execute(
        "SELECT id FROM artist_applications WHERE user_id=? "
        "AND status='new'", (user["id"],)).fetchone()
    if pending:
        conn.close()
        return redir(back.format("err-pending"))
    conn.execute(
        "INSERT INTO artist_applications(user_id, note, "
        "created_at) VALUES(?,?,?)",
        (user["id"], note, now_iso()))
    conn.commit()
    conn.close()
    return redir(back.format("applied"))



@app.get("/artist/start", response_class=HTMLResponse)
async def artist_start_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    pending = conn.execute(
        "SELECT id FROM artist_applications WHERE user_id=? "
        "AND status='new'", (user["id"],)).fetchone()
    conn.close()
    return render(request, "artist_start.html", {
        "done": request.query_params.get("done") or "",
        "err": request.query_params.get("err") or "",
        "is_artist_mode":
            (user.get("media_mode") or "") == "artist",
        "pending": bool(pending)})


@app.post("/artist/start")
async def artist_start_submit(request: Request):
    """The sign-off IS the application: pitch + typed name +
    both attestations (read the Playbook; understands the
    money) land on one artist_applications row, stamped
    read_playbook=1, and the artist lands on the CEO's
    'sign yourself' closer."""
    user, err = require_login(request)
    if err:
        return err
    if (user.get("media_mode") or "") != "artist":
        return redir("/artist/start?err=mode")
    if user.get("artist_verified"):
        return redir("/artist/start?err=already")
    form = await request.form()
    name = str(form.get("signed_name") or "").strip()
    note = str(form.get("note") or "").strip()[:400]
    if len(name) < 3 or not form.get("agree_read") \
            or not form.get("agree_money"):
        return redir("/artist/start?err=form")
    conn = db()
    if conn.execute(
            "SELECT 1 FROM artist_applications WHERE user_id=? "
            "AND status='new'", (user["id"],)).fetchone():
        conn.close()
        return redir("/artist/start?err=pending")
    conn.execute(
        "INSERT INTO artist_applications(user_id, note, "
        "signed_name, read_playbook, created_at) "
        "VALUES(?,?,?,?,?)",
        (user["id"], note, name[:80], 1, now_iso()))
    conn.commit()
    conn.close()
    return redir("/artist/start?done=1")


@app.get("/artists", response_class=HTMLResponse)
async def artists_page(request: Request):
    user = current_user(request)
    conn = db()
    rows = conn.execute(
        "SELECT id, name, avatar, founding_no FROM users "
        "WHERE artist_verified=1 ORDER BY id").fetchall()
    conn.close()
    artists = []
    for r in rows:
        st = _artist_stats(r["id"])
        artists.append({
            "id": r["id"], "name": r["name"],
            "avatar": avatar_url(r["avatar"]),
            "founding_no": r["founding_no"], **st})
    artists.sort(key=lambda a: -a["plays"])
    return render(request, "artists.html",
                  {"artists": artists,
                   "plays_target": ARTIST_PLAYS_TARGET})


@app.get("/artist/contract", response_class=HTMLResponse)
async def artist_contract_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    row = _open_contract(conn, user["id"])
    last = row or conn.execute(
        "SELECT * FROM artist_contracts WHERE user_id=? "
        "ORDER BY id DESC", (user["id"],)).fetchone()
    conn.close()
    state = (user.get("artist_contract") or "")
    terms = CONTRACT_VERSIONS[
        (last["terms_version"] if last else "v1")]
    return render(request, "artist_contract.html", {
        "state": state, "contract": dict(last) if last else None,
        "terms": terms,
        "err": request.query_params.get("err") or "",
        "stats": _artist_stats(user["id"])})


@app.get("/artist/contract.pdf")
async def artist_contract_pdf(request: Request):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    row = conn.execute(
        "SELECT * FROM artist_contracts WHERE user_id=? "
        "ORDER BY id DESC", (user["id"],)).fetchone()
    conn.close()
    if not row:
        return redir("/artist/contract")
    terms = CONTRACT_VERSIONS[row["terms_version"]]
    lines = [("HERE — VERIFIED ARTIST AGREEMENT", 17, True),
             (f"Contract #{row['id']} · Terms "
              f"{row['terms_version']} · Artist: "
              f"{user['name']} ({user['email']})", 10.5, False),
             ("", 8, False)]
    for head, body in terms:
        lines.append((head, 12.5, True))
        lines.append((body, 11, False))
    lines.append(("", 8, False))
    if row["signed_at"]:
        lines.append(
            (f"SIGNED by the Artist: {row['signed_name']}", 12,
             True))
        lines.append(
            (f"Signed at: {row['signed_at']} · Status: "
             f"{row['status']}", 11, False))
    else:
        lines.append((f"Status: {row['status']} — not yet "
                      "signed.", 11, False))
    if row["ended_at"]:
        lines.append(
            (f"Ended at: {row['ended_at']} (by "
             f"{row['ended_by']})", 11, False))
    data = _contract_pdf_bytes("HERE Verified Artist Agreement",
                               lines)
    return Response(
        data, media_type="application/pdf",
        headers={"Content-Disposition":
                 'attachment; filename="here-artist-'
                 'agreement.pdf"'})


@app.post("/artist/contract/sign")
async def artist_contract_sign(request: Request):
    user, err = require_login(request)
    if err:
        return err
    back = "/artist/contract"
    if (user.get("artist_contract") or "") != "offered":
        return redir(back + "?err=state")
    form = await request.form()
    name = str(form.get("signed_name") or "").strip()
    if len(name) < 3 or not form.get("agree_read") \
            or not form.get("agree_sign"):
        return redir(back + "?err=form")
    conn = db()
    row = _open_contract(conn, user["id"])
    if not row:
        conn.close()
        return redir(back + "?err=state")
    stamp = now_iso()
    conn.execute(
        "UPDATE artist_contracts SET status='signed', "
        "signed_at=?, signed_name=? WHERE id=?",
        (stamp, name[:80], row["id"]))
    conn.execute(
        "UPDATE users SET artist_contract='signed' WHERE id=?",
        (user["id"],))
    conn.commit()
    conn.close()
    notify(user["id"], "artist_contract",
           "🎉 You're signed to HERE. Your agreement is live — "
           "the 3% label fee rides your cash-outs from here, "
           "and everything you earn stays yours.",
           "/artist/contract")
    return redir(back)


@app.post("/artist/contract/terminate")
async def artist_contract_terminate(request: Request):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    row = _open_contract(conn, user["id"])
    if row:
        stamp = now_iso()
        conn.execute(
            "UPDATE artist_contracts SET status='ended', "
            "ended_at=?, ended_by='artist' WHERE id=?",
            (stamp, row["id"]))
        conn.execute(
            "UPDATE users SET artist_contract='ended' "
            "WHERE id=?", (user["id"],))
        conn.commit()
    conn.close()
    return redir("/artist/contract")


# ---------------- owner: the Label desk ----------------

@app.get("/owner/artists", response_class=HTMLResponse)
async def owner_artists(request: Request):
    err = require_owner(request)
    if err:
        return err
    conn = db()
    apps = []
    for r in conn.execute(
            "SELECT a.*, u.name, u.email FROM "
            "artist_applications a JOIN users u ON "
            "u.id = a.user_id WHERE a.status='new' "
            "ORDER BY a.id").fetchall():
        apps.append({**dict(r), "stats": _artist_stats(
            r["user_id"]),
            "tracks": _media_tracks_for(r["user_id"])})
    verified, hopefuls = [], []
    for r in conn.execute(
            "SELECT id, name, email, artist_verified, "
            "artist_contract, media_mode FROM users WHERE "
            "artist_verified=1 OR media_mode='artist' "
            "ORDER BY artist_verified DESC, id").fetchall():
        entry = {**dict(r), "stats": _artist_stats(r["id"]),
                 "tracks": _media_tracks_for(r["id"])}
        entry["contract_ready"] = bool(
            entry["stats"]["plays"] >= ARTIST_PLAYS_TARGET)
        (verified if r["artist_verified"] else hopefuls).append(
            entry)
    grants = [dict(g) for g in conn.execute(
        "SELECT g.*, u.name FROM artist_fund_grants g JOIN "
        "users u ON u.id = g.user_id ORDER BY g.id DESC "
        "LIMIT 20").fetchall()]
    fund = _fund_cents(conn)
    fees = conn.execute(
        "SELECT COALESCE(SUM(cents),0) s FROM label_fees"
        ).fetchone()["s"]
    conn.close()
    return render(request, "owner/artists.html", {
        "apps": apps, "verified": verified,
        "hopefuls": hopefuls, "grants": grants,
        "fund_cents": fund, "fund": _money(fund),
        "fees_total": _money(fees or 0),
        "plays_target": ARTIST_PLAYS_TARGET,
        "msg": request.query_params.get("msg") or ""})


@app.post("/owner/artists/app/{aid}/{decision}")
async def owner_artist_app(request: Request, aid: int,
                           decision: str):
    err = require_owner(request)
    if err:
        return err
    if decision not in ("approve", "pass"):
        return redir("/owner/artists")
    conn = db()
    app_row = conn.execute(
        "SELECT * FROM artist_applications WHERE id=? AND "
        "status='new'", (aid,)).fetchone()
    if app_row:
        stamp = now_iso()
        conn.execute(
            "UPDATE artist_applications SET status=?, "
            "decided_at=? WHERE id=?",
            ("approved" if decision == "approve" else "passed",
             stamp, aid))
        if decision == "approve":
            conn.execute(
                "UPDATE users SET artist_verified=1 WHERE id=?",
                (app_row["user_id"],))
        conn.commit()
        if decision == "approve":
            notify(app_row["user_id"], "artist_verified",
                   "🎤👑 You're a HERE Verified Artist! The "
                   "golden note is on your profile — fans can "
                   "now gift your tracks.",
                   f"/user/{app_row['user_id']}")
    conn.close()
    if _oa(request):
        if not app_row:
            return _oa_fail("That application is gone.")
        return _oa_json(
            "🎤 Artist approved — the plaque is live."
            if decision == "approve" else "Application passed.",
            status="approved" if decision == "approve"
            else "passed")
    return redir("/owner/artists?msg=done")


@app.post("/owner/artists/{uid}/verify")
async def owner_artist_verify(request: Request, uid: int):
    err = require_owner(request)
    if err:
        return err
    conn = db()
    conn.execute("UPDATE users SET artist_verified=1 "
                 "WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    notify(uid, "artist_verified",
           "🎤👑 You're a HERE Verified Artist! The golden "
           "note is on your profile — fans can now gift your "
           "tracks.", f"/user/{uid}")
    return redir("/owner/artists?msg=done")


@app.post("/owner/artists/{uid}/revoke")
async def owner_artist_revoke(request: Request, uid: int):
    err = require_owner(request)
    if err:
        return err
    conn = db()
    conn.execute("UPDATE users SET artist_verified=0 "
                 "WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    return redir("/owner/artists?msg=done")


@app.post("/owner/artists/{uid}/offer")
async def owner_artist_offer(request: Request, uid: int):
    err = require_owner(request)
    if err:
        return err
    conn = db()
    u = conn.execute("SELECT artist_verified, artist_contract "
                     "FROM users WHERE id=?", (uid,)).fetchone()
    if u and u["artist_verified"] and not _open_contract(
            conn, uid):
        stamp = now_iso()
        conn.execute(
            "INSERT INTO artist_contracts(user_id, status, "
            "terms_version, offered_at, created_at) VALUES"
            "(?, 'offered', 'v1', ?, ?)", (uid, stamp, stamp))
        conn.execute(
            "UPDATE users SET artist_contract='offered' "
            "WHERE id=?", (uid,))
        conn.commit()
        notify(uid, "artist_contract",
               "📄 HERE is offering you a record deal. Read "
               "the agreement and sign it on your contract "
               "page.", "/artist/contract")
    conn.close()
    return redir("/owner/artists?msg=done")


@app.post("/owner/artists/{uid}/contract-end")
async def owner_artist_contract_end(request: Request, uid: int):
    err = require_owner(request)
    if err:
        return err
    conn = db()
    row = _open_contract(conn, uid)
    if row:
        stamp = now_iso()
        conn.execute(
            "UPDATE artist_contracts SET status='ended', "
            "ended_at=?, ended_by='owner' WHERE id=?",
            (stamp, row["id"]))
        conn.execute(
            "UPDATE users SET artist_contract='ended' "
            "WHERE id=?", (uid,))
        conn.commit()
        notify(uid, "artist_contract",
               "Your HERE artist contract has ended. "
               "Everything in your wallet stays yours to cash "
               "out, as agreed.", "/artist/contract")
    conn.close()
    return redir("/owner/artists?msg=done")


@app.post("/owner/artists/fund-grant")
async def owner_artist_fund_grant(request: Request):
    err = require_owner(request)
    if err:
        return err
    form = await request.form()
    try:
        uid = int(form.get("user_id") or 0)
        cents = int(float(form.get("dollars") or 0) * 100)
    except (TypeError, ValueError):
        uid, cents = 0, 0
    note = str(form.get("note") or "").strip()[:200]
    conn = db()
    fund = _fund_cents(conn)
    if uid and cents > 0 and cents <= fund and get_user(uid):
        _fund_set(conn, fund - cents)
        conn.execute(
            "INSERT INTO artist_fund_grants(user_id, cents, "
            "note, created_at) VALUES(?,?,?,?)",
            (uid, cents, note, now_iso()))
        conn.commit()
        notify(uid, "artist_fund",
               f"🎤💰 The Artist Fund is backing you: "
               f"{_money(cents)} sponsorship from HERE. "
               + (note or ""), "/artists")
    conn.close()
    return redir("/owner/artists?msg=done")


# ---------------- track likes + track gifts ----------------

@app.post("/media/{tid}/like")
async def media_like(request: Request, tid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    t = conn.execute("SELECT user_id FROM media_tracks "
                     "WHERE id=?", (tid,)).fetchone()
    if not t:
        conn.close()
        return JSONResponse({"ok": False}, status_code=404)
    has = conn.execute(
        "SELECT 1 FROM media_track_likes WHERE track_id=? "
        "AND user_id=?", (tid, user["id"])).fetchone()
    if has:
        conn.execute("DELETE FROM media_track_likes WHERE "
                     "track_id=? AND user_id=?",
                     (tid, user["id"]))
        liked = False
    else:
        conn.execute("INSERT INTO media_track_likes(track_id, "
                     "user_id, created_at) VALUES(?,?,?)",
                     (tid, user["id"], now_iso()))
        liked = True
    n = conn.execute("SELECT COUNT(*) c FROM media_track_likes "
                     "WHERE track_id=?", (tid,)).fetchone()["c"]
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "liked": liked,
                         "likes": n})


def _track_gift_core(user, track, owner, gift):
    """The money move for a track gift — the post-gift
    transaction aimed at a song: guarded debit, the ARTIST
    earns, a track_gifts row, twin ledger lines note='track',
    and the inviter bonus riding along."""
    cost = gift["tickets"]
    recipient_id = owner["id"]
    conn = db()
    if cost == 0:
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=2)).isoformat()
        recent = conn.execute(
            "SELECT COUNT(*) c FROM track_gifts WHERE "
            "sender_id=? AND track_id=? AND tickets=0 AND "
            "created_at>?", (user["id"], track["id"], cutoff)
            ).fetchone()["c"]
        if recent:
            conn.close()
            return 429, {"ok": False,
                         "error": "Easy — one free gift every "
                                  "couple of seconds 💜"}
    cur = conn.execute(
        "UPDATE users SET tickets_balance = "
        "COALESCE(tickets_balance,0) - ? WHERE id=? AND "
        "COALESCE(tickets_balance,0) >= ?",
        (cost, user["id"], cost))
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        return 400, {"ok": False,
                     "error": "Not enough tickets — grab a pack "
                              "first 🎟️"}
    conn.execute(
        "UPDATE users SET gift_earned = COALESCE(gift_earned,0) "
        "+ ?, gift_lifetime = COALESCE(gift_lifetime,0) + ? "
        "WHERE id=?", (cost, cost, recipient_id))
    stamp = now_iso()
    cur = conn.execute(
        "INSERT INTO track_gifts(sender_id, recipient_id, "
        "track_id, gift_key, tickets, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (user["id"], recipient_id, track["id"], gift["key"],
         cost, stamp))
    gift_id = cur.lastrowid
    conn.execute(
        "INSERT INTO ticket_txns(user_id, kind, tickets, "
        "live_id, gift_key, note, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (user["id"], "gift_send", -cost, None, gift["key"],
         "track", stamp))
    conn.execute(
        "INSERT INTO ticket_txns(user_id, kind, tickets, "
        "live_id, gift_key, note, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (recipient_id, "gift_receive", cost, None, gift["key"],
         "track", stamp))
    if cost > 0:
        _invite_bonus(conn, recipient_id, cost, stamp)
    conn.commit()
    conn.close()
    bal = (get_user(user["id"]) or {}).get("tickets_balance", 0)
    if cost > 0:
        notify(recipient_id, "track_gift",
               f"{user['name']} gifted your track "
               f"“{track['title']}” (+{cost} 🎟️)",
               f"/user/{recipient_id}", user["id"])
    return 200, {"ok": True, "balance": bal,
                 "gift_id": gift_id,
                 "recipient_id": recipient_id,
                 "recipient_name": track["title"]}


@app.post("/media/{tid}/gift")
async def media_gift(request: Request, tid: int):
    user, gift, err = await _page_gift_parse(request)
    if err:
        return err
    conn = db()
    track = conn.execute("SELECT * FROM media_tracks "
                         "WHERE id=?", (tid,)).fetchone()
    conn.close()
    if not track:
        return JSONResponse({"ok": False,
                             "error": "That track is gone."},
                            status_code=400)
    owner = get_user(track["user_id"]) or {}
    if track["user_id"] == user["id"]:
        return JSONResponse(
            {"ok": False, "error": "You can't gift yourself."},
            status_code=400)
    if track["rights"] != "own":
        return JSONResponse(
            {"ok": False,
             "error": "That track is shared for listening only "
                      "— it can't take gifts."},
            status_code=403)
    if not owner.get("artist_verified"):
        return JSONResponse(
            {"ok": False,
             "error": "Track gifts are for Verified Artists."},
            status_code=403)
    if not owner.get("creator_mode"):
        return JSONResponse(
            {"ok": False,
             "error": "That artist can't take gifts yet — "
                      "Creator Mode is off."},
            status_code=403)
    if not is_owner(request) and blocked_either_way(
            user["id"], track["user_id"]):
        return JSONResponse({"ok": False,
                             "error": "Not available."},
                            status_code=400)
    code, payload = _track_gift_core(
        user, dict(track), owner, gift)
    return JSONResponse(payload, status_code=code)


@app.post("/gift-nudge")
async def gift_nudge(request: Request):
    """One tap from the locked gift sheet: tell a non-creator
    owner that somebody tried to gift them. One nudge per
    sender per target per day — encouragement, never spam."""
    user, err = require_login(request)
    if err:
        return err
    try:
        data = await request.json()
    except Exception:
        data = {}
    kind = str((data or {}).get("kind") or "")
    try:
        tid = int((data or {}).get("id") or 0)
    except (TypeError, ValueError):
        tid = 0
    conn = db()
    owner_id, target_name = None, ""
    if kind == "page":
        pg = conn.execute("SELECT owner_id, name FROM pages "
                          "WHERE id=?", (tid,)).fetchone()
        if pg:
            owner_id, target_name = pg["owner_id"], pg["name"]
    elif kind == "profile":
        u = conn.execute("SELECT id, name FROM users WHERE id=?",
                         (tid,)).fetchone()
        if u:
            owner_id, target_name = u["id"], u["name"]
    if not owner_id:
        conn.close()
        return JSONResponse({"ok": False,
                             "error": "Not available."},
                            status_code=400)
    if owner_id == user["id"]:
        conn.close()
        return JSONResponse({"ok": False,
                             "error": "That's you."},
                            status_code=400)
    owner = get_user(owner_id) or {}
    if owner.get("creator_mode"):
        conn.close()
        return JSONResponse(
            {"ok": True, "already_creator": True})
    day = now_iso()[:10]
    dupe = conn.execute(
        "SELECT id FROM gift_nudges WHERE sender_id=? AND kind=? "
        "AND target_id=? AND substr(created_at,1,10)=?",
        (user["id"], kind, tid, day)).fetchone()
    if dupe:
        conn.close()
        return JSONResponse({"ok": True, "already": True})
    conn.execute(
        "INSERT INTO gift_nudges(sender_id, owner_id, kind, "
        "target_id, created_at) VALUES(?,?,?,?,?)",
        (user["id"], owner_id, kind, tid, now_iso()))
    conn.commit()
    conn.close()
    notify(owner_id, "gift_nudge",
           "🎁 " + user["name"] + " wanted to send “"
           + target_name + "” a gift — turn on Creator Mode "
           "to start earning from gifts.",
           "/settings/profile#creator-mode")
    return JSONResponse({"ok": True})


@app.post("/page/{pid}/gift")
async def page_page_gift(request: Request, pid: int):
    """🎁 Gift a Page: the PAGE'S OWNER earns — never an admin,
    a member, or anyone else. Creator Mode gates it on the PAGE
    OWNER's flag. A Page has no privacy gate (anyone signed in
    can view one), so the view gate is existence, plus the
    shared block rule against the owner."""
    user, gift, err = await _page_gift_parse(request)
    if err:
        return err
    conn = db()
    pg = conn.execute("SELECT * FROM pages WHERE id=?",
                      (pid,)).fetchone()
    conn.close()
    if not pg:
        return JSONResponse(
            {"ok": False, "error": "That page isn't available."},
            status_code=400)
    owner_id = pg["owner_id"]
    _pg_owner = get_user(owner_id) or {}
    if not _pg_owner.get("creator_mode"):
        return JSONResponse(
            {"ok": False,
             "error": "That Page can't take gifts — its owner "
                      "hasn't turned on Creator Mode."},
            status_code=403)
    if owner_id == user["id"]:
        return JSONResponse(
            {"ok": False, "error": "You can't gift yourself."},
            status_code=400)
    if not is_owner(request) \
            and blocked_either_way(user["id"], owner_id):
        return JSONResponse(
            {"ok": False, "error": "That page isn't available."},
            status_code=400)
    code, payload = _page_gift_core(user, owner_id, pg["name"], pid,
                                    gift)
    return JSONResponse(payload, status_code=code)


# ---------------- live tickets: gifts + shout-out pins ----------------
# The money-moving pair of the gift economy. Both endpoints are
# single-transaction and guarded: the spendable balance is debited
# with a WHERE balance>=cost UPDATE whose rowcount is checked, so a
# balance can never go negative and a double-tap can't double-spend.
# The recipient's side lands in gift_earned (the earned ledger);
# every move writes its ticket_txns rows in the same transaction.


def _live_present(conn, lid, uid, host_id):
    """Presence for gifting/shouting: an on-camera member row, or a
    watcher row that hasn't left (heartbeats keep it fresh). The
    host is 'present' by definition but is handled by the caller
    (hosts can't gift/shout on their own live)."""
    if conn.execute(
            "SELECT 1 FROM live_members WHERE live_id=? AND "
            "user_id=? AND left_at IS NULL",
            (lid, uid)).fetchone():
        return True
    return bool(conn.execute(
        "SELECT 1 FROM live_viewers WHERE live_id=? AND user_id=? "
        "AND left_at IS NULL", (lid, uid)).fetchone())


@app.post("/live/{lid}/gift")
async def live_gift(request: Request, lid: int):
    """Send a character gift on a live. Body: {gift_key} plus an
    optional {target_user_id} (Build PANEL GIFTS): absent/null aims
    at the host — the historical behavior; a target aims at that
    PANELIST (host, admin, or on-stage guest) and pays THEM. Prices
    come ONLY from GIFT_CATALOG (server-side). Atomic: sender
    spendable -= cost (guarded), recipient earned += cost, one
    live_gifts row + two ledger rows, all in one commit."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    try:
        data = await request.json()
    except Exception:
        data = {}
    gift_key = str((data or {}).get("gift_key") or "")
    gift = GIFT_CATALOG.get(gift_key)
    if not gift:
        return JSONResponse({"ok": False, "error": "Unknown gift."},
                            status_code=400)
    host_id = live["host_id"]
    if user["id"] == host_id:
        return JSONResponse(
            {"ok": False, "error": "You can't gift your own live."},
            status_code=400)
    # Build PANEL GIFTS: resolve the recipient. Panel membership
    # itself is validated below, once the conn is open.
    raw_target = (data or {}).get("target_user_id")
    recipient_id = host_id
    if raw_target is not None:
        try:
            recipient_id = int(raw_target)
        except (TypeError, ValueError):
            return JSONResponse(
                {"ok": False, "error": "Unknown gift target."},
                status_code=400)
    cost = gift["tickets"]
    conn = db()
    # Build W2: a battle whose clock already ran out finishes
    # BEFORE this gift lands, so a late gift never scores.
    if _battle_expire(conn):
        conn.commit()
    if not _live_present(conn, lid, user["id"], host_id):
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Join the live before sending gifts."},
            status_code=403)
    # Build PANEL GIFTS: the recipient must be ON THE PANEL right
    # now — the host always qualifies; anyone else needs an ACTIVE
    # live_members row (left_at NULL), which is the roster the
    # panel itself renders from. A plain watcher, a guest who
    # already left, or a stranger is refused here, before any
    # money moves — commenters can never receive gifts.
    if recipient_id == user["id"]:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "You can't gift yourself."},
            status_code=400)
    _rrow = conn.execute("SELECT name FROM users WHERE id=?",
                         (recipient_id,)).fetchone()
    if _rrow is None or (recipient_id != host_id
                         and not _live_member_row(conn, lid,
                                                  recipient_id)):
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "That person isn't on the panel anymore."},
            status_code=400)
    recipient_name = _rrow["name"]
    if cost == 0:
        # FREE gift anti-spam: free gifts move no ledger, so the
        # balance guard can't slow a spam tap — cap them at one
        # per sender per live per 2 seconds instead (checked
        # against the live_gifts row the last one wrote).
        cutoff = (datetime.now(timezone.utc)
                  - timedelta(seconds=2)).isoformat()
        recent = conn.execute(
            "SELECT COUNT(*) c FROM live_gifts WHERE live_id=? "
            "AND sender_id=? AND tickets=0 AND created_at>?",
            (lid, user["id"], cutoff)).fetchone()["c"]
        if recent:
            conn.close()
            return JSONResponse(
                {"ok": False,
                 "error": "Easy — one free gift every couple of "
                          "seconds 💜"},
                status_code=429)
    cur = conn.execute(
        "UPDATE users SET tickets_balance = "
        "COALESCE(tickets_balance,0) - ? WHERE id=? AND "
        "COALESCE(tickets_balance,0) >= ?", (cost, user["id"], cost))
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Not enough tickets — grab a pack first 🎟️"},
            status_code=400)
    conn.execute(
        "UPDATE users SET gift_earned = COALESCE(gift_earned,0) + ?, "
        "gift_lifetime = COALESCE(gift_lifetime,0) + ? WHERE id=?",
        (cost, cost, recipient_id))
    stamp = now_iso()
    cur = conn.execute(
        "INSERT INTO live_gifts(live_id, sender_id, host_id, "
        "recipient_id, gift_key, tickets, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (lid, user["id"], host_id, recipient_id, gift_key, cost,
         stamp))
    gift_id = cur.lastrowid
    conn.execute(
        "INSERT INTO ticket_txns(user_id, kind, tickets, live_id, "
        "gift_key, created_at) VALUES(?,?,?,?,?,?)",
        (user["id"], "gift_send", -cost, lid, gift_key, stamp))
    conn.execute(
        "INSERT INTO ticket_txns(user_id, kind, tickets, live_id, "
        "gift_key, created_at) VALUES(?,?,?,?,?,?)",
        (recipient_id, "gift_receive", cost, lid, gift_key, stamp))
    _invite_bonus(conn, recipient_id, cost, stamp)
    # Build W2: Showdown scoring — inside THIS gift's transaction,
    # so a gift scores exactly once, by exactly its ticket cost
    # (the free gift costs 0 = cheers only, scores nothing). The
    # money above never changes: battles only read the cost.
    _bb = conn.execute(
        "SELECT * FROM live_battles WHERE status='active' AND "
        "(challenger_live_id=? OR opponent_live_id=?)",
        (lid, lid)).fetchone()
    if _bb and cost:
        # Build PANEL GIFTS: points follow the RECIPIENT. A gift
        # scores only when it lands on one of the two battle hosts
        # — for THEIR side, whichever room the sender is standing
        # in; gifts to other panelists pay them but score zero.
        # The default target (this room's host) reduces to exactly
        # the room-side rule this replaces.
        _col = None
        if recipient_id == _bb["challenger_user_id"]:
            _col = "challenger_score"
        elif recipient_id == _bb["opponent_user_id"]:
            _col = "opponent_score"
        if _col:
            conn.execute(
                f"UPDATE live_battles SET {_col}={_col}+? WHERE id=? "
                "AND status='active'", (cost, _bb["id"]))
    conn.commit()
    bal = conn.execute(
        "SELECT tickets_balance FROM users WHERE id=?",
        (user["id"],)).fetchone()["tickets_balance"]
    conn.close()
    return JSONResponse({"ok": True, "balance": bal or 0,
                         "gift_id": gift_id,
                         "recipient_id": recipient_id,
                         "recipient_name": recipient_name})


@app.post("/live/{lid}/shout")
async def live_shout(request: Request, lid: int):
    """Buy a 60-second pinned shout-out: the sender's text pins to
    the top of the live's comments zone. Costs SHOUT_COST_TICKETS;
    the host earns them like a gift. The text gets the same length
    cap + moderation flag scan as a live comment (flag only)."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict) or not data:
        try:
            form = await request.form()
            data = {"body": form.get("body")}
        except Exception:
            data = {}
    body = str(data.get("body") or "").strip()[:LIVE_COMMENT_MAX]
    if not body:
        return JSONResponse({"ok": False, "error": "Empty shout."},
                            status_code=400)
    host_id = live["host_id"]
    if user["id"] == host_id:
        return JSONResponse(
            {"ok": False,
             "error": "Hosts can't pin a shout on their own live."},
            status_code=400)
    cost = SHOUT_COST_TICKETS
    conn = db()
    if not _live_present(conn, lid, user["id"], host_id):
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Join the live before pinning a shout."},
            status_code=403)
    cur = conn.execute(
        "UPDATE users SET tickets_balance = "
        "COALESCE(tickets_balance,0) - ? WHERE id=? AND "
        "COALESCE(tickets_balance,0) >= ?", (cost, user["id"], cost))
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Not enough tickets — a shout costs "
                      f"{cost} 🎟️"},
            status_code=400)
    conn.execute(
        "UPDATE users SET gift_earned = COALESCE(gift_earned,0) + ?, "
        "gift_lifetime = COALESCE(gift_lifetime,0) + ? WHERE id=?",
        (cost, cost, host_id))
    stamp = now_iso()
    expires = (datetime.now(timezone.utc)
               + timedelta(seconds=SHOUT_SECONDS)).isoformat()
    cur = conn.execute(
        "INSERT INTO live_shouts(live_id, user_id, body, tickets, "
        "expires_at, created_at) VALUES(?,?,?,?,?,?)",
        (lid, user["id"], body, cost, expires, stamp))
    shout_id = cur.lastrowid
    conn.execute(
        "INSERT INTO ticket_txns(user_id, kind, tickets, live_id, "
        "created_at) VALUES(?,?,?,?,?)",
        (user["id"], "shout", -cost, lid, stamp))
    conn.execute(
        "INSERT INTO ticket_txns(user_id, kind, tickets, live_id, "
        "note, created_at) VALUES(?,?,?,?,?,?)",
        (host_id, "gift_receive", cost, lid, "shout", stamp))
    _invite_bonus(conn, host_id, cost, stamp)
    conn.commit()
    bal = conn.execute(
        "SELECT tickets_balance FROM users WHERE id=?",
        (user["id"],)).fetchone()["tickets_balance"]
    conn.close()
    flag_item("live_shout", shout_id, user["id"], user["name"], body)
    return JSONResponse({"ok": True, "balance": bal or 0,
                         "shout_id": shout_id,
                         "expires_at": expires})


def _battle_host_ctx(request, lid):
    """(user, conn, live, err) for Showdown actions: signed in,
    hosting THIS room, and the room still live. Caller closes the
    conn on every path (err paths already closed it)."""
    user = current_user(request)
    if not user:
        return None, None, None, JSONResponse(
            {"ok": False, "error": "login"}, status_code=401)
    conn = db()
    _sweep_lives(conn)
    live = _live_get(conn, lid)
    if not live:
        conn.close()
        return None, None, None, JSONResponse(
            {"ok": False, "error": "not found"}, status_code=404)
    if live["host_id"] != user["id"]:
        conn.close()
        return None, None, None, JSONResponse(
            {"ok": False, "error": "Only the host can do that."},
            status_code=403)
    if live["status"] != "live":
        conn.close()
        return None, None, None, JSONResponse(
            {"ok": False, "error": "This live has ended."},
            status_code=409)
    return user, conn, live, None


def _battle_insert_pending(conn, my_live, opp_live, duration):
    """Insert a pending challenge row; the partial unique indexes
    backstop the busy checks. Returns battle id or None on a
    lost race (treated as 'already in a Showdown')."""
    try:
        cur = conn.execute(
            "INSERT INTO live_battles(challenger_live_id, "
            "challenger_user_id, opponent_live_id, opponent_user_id, "
            "status, duration_sec, created_at) VALUES(?,?,?,?,"
            "'pending',?,?)",
            (my_live["id"], my_live["host_id"], opp_live["id"],
             opp_live["host_id"], duration, now_iso()))
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        conn.rollback()
        return None


@app.post("/live/{lid}/battle/challenge")
async def live_battle_challenge(request: Request, lid: int):
    """Host (while live) challenges another live host to a
    ⚔️ LIVE SHOWDOWN of 3, 5 or 10 minutes."""
    user, conn, live, err = _battle_host_ctx(request, lid)
    if err:
        return err
    try:
        data = await request.json()
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    try:
        opp_lid = int(data.get("opponent_live_id") or 0)
    except (TypeError, ValueError):
        opp_lid = 0
    try:
        duration = int(data.get("duration_sec") or 300)
    except (TypeError, ValueError):
        duration = 300
    if duration not in BATTLE_DURATIONS:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "Pick 3, 5 or 10 minutes."},
            status_code=400)
    if opp_lid == lid:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "You can't battle yourself."},
            status_code=400)
    opp = _live_get(conn, opp_lid)
    if not opp or opp["status"] != "live":
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That host isn't live right now."},
            status_code=400)
    if opp["host_id"] == user["id"]:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "You can't battle yourself."},
            status_code=400)
    if not _live_can_watch(user, opp, owner_on=is_owner(request)):
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "You can't battle that host."},
            status_code=400)
    if _battle_busy(conn, lid):
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "You're already in a Showdown — "
                                   "finish that one first."},
            status_code=400)
    if _battle_busy(conn, opp_lid):
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": f"{_live_person(opp['host_id'])['name']} is "
                      "already in a Showdown."},
            status_code=400)
    bid = _battle_insert_pending(conn, live, opp, duration)
    opp_name = _live_person(opp["host_id"])["name"]
    conn.close()
    if not bid:
        return JSONResponse(
            {"ok": False, "error": "One of you is already in a "
                                   "Showdown."},
            status_code=400)
    notify(opp["host_id"], "showdown",
           f"⚔️ {user['name']} challenged you to a LIVE SHOWDOWN "
           f"({duration // 60} min)! Open your live to accept.",
           f"/live/{opp_lid}", actor_id=user["id"])
    return JSONResponse({"ok": True, "battle_id": bid})


@app.post("/live/{lid}/battle/{bid}/accept")
async def live_battle_accept(request: Request, lid: int, bid: int):
    """The challenged host accepts: the clock starts NOW and both
    rooms' state polls pick up the shared scoreboard."""
    user, conn, live, err = _battle_host_ctx(request, lid)
    if err:
        return err
    b = _battle_get(conn, bid)
    if not b or b["opponent_live_id"] != lid:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That challenge isn't yours."},
            status_code=404)
    if b["status"] != "pending":
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That challenge is gone."},
            status_code=400)
    chal = _live_get(conn, b["challenger_live_id"])
    if not chal or chal["status"] != "live":
        conn.execute(
            "UPDATE live_battles SET status='cancelled', "
            "decided_at=? WHERE id=? AND status='pending'",
            (now_iso(), bid))
        conn.commit()
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That live already ended — "
                                   "challenge called off."},
            status_code=400)
    started = datetime.now(timezone.utc)
    ends = started + timedelta(seconds=b["duration_sec"])
    cur = conn.execute(
        "UPDATE live_battles SET status='active', started_at=?, "
        "ends_at=? WHERE id=? AND status='pending'",
        (started.isoformat(), ends.isoformat(), bid))
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That challenge is gone."},
            status_code=400)
    b2 = _battle_get(conn, bid)
    out = _battle_json(conn, b2, lid)
    conn.commit()
    conn.close()
    notify(b["challenger_user_id"], "showdown",
           f"⚔️ {user['name']} accepted your Showdown — it's ON! "
           "Gifts are points. Good luck!",
           f"/live/{b['challenger_live_id']}", actor_id=user["id"])
    return JSONResponse({"ok": True, "battle": out})


@app.post("/live/{lid}/battle/{bid}/decline")
async def live_battle_decline(request: Request, lid: int, bid: int):
    """The challenged host passes; the challenger hears about it
    in their own state poll (and by notification)."""
    user, conn, live, err = _battle_host_ctx(request, lid)
    if err:
        return err
    b = _battle_get(conn, bid)
    if not b or b["opponent_live_id"] != lid:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That challenge isn't yours."},
            status_code=404)
    cur = conn.execute(
        "UPDATE live_battles SET status='declined', decided_at=? "
        "WHERE id=? AND status='pending'", (now_iso(), bid))
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That challenge is gone."},
            status_code=400)
    conn.commit()
    conn.close()
    notify(b["challenger_user_id"], "showdown",
           f"⚔️ {user['name']} passed on the Showdown this time.",
           f"/live/{b['challenger_live_id']}", actor_id=user["id"])
    return JSONResponse({"ok": True})


@app.post("/live/{lid}/battle/{bid}/cancel")
async def live_battle_cancel(request: Request, lid: int, bid: int):
    """The challenger calls off their own pending challenge."""
    user, conn, live, err = _battle_host_ctx(request, lid)
    if err:
        return err
    b = _battle_get(conn, bid)
    if not b or b["challenger_live_id"] != lid:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That challenge isn't yours."},
            status_code=404)
    cur = conn.execute(
        "UPDATE live_battles SET status='cancelled', decided_at=? "
        "WHERE id=? AND status='pending'", (now_iso(), bid))
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That challenge is gone."},
            status_code=400)
    conn.commit()
    conn.close()
    notify(b["opponent_user_id"], "showdown",
           f"⚔️ {user['name']} called off the Showdown challenge.",
           f"/live/{b['opponent_live_id']}", actor_id=user["id"])
    return JSONResponse({"ok": True})


@app.post("/live/{lid}/battle/{bid}/rematch")
async def live_battle_rematch(request: Request, lid: int, bid: int):
    """One-tap rematch from the result banner: a FRESH challenge
    from this room to the other host (same length as last time),
    through the exact same busy guards as a normal challenge."""
    user, conn, live, err = _battle_host_ctx(request, lid)
    if err:
        return err
    b = _battle_get(conn, bid)
    if not b or lid not in (b["challenger_live_id"],
                            b["opponent_live_id"]):
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That Showdown isn't yours."},
            status_code=404)
    if b["status"] != "finished":
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That Showdown isn't over yet."},
            status_code=400)
    opp_lid = (b["opponent_live_id"] if b["challenger_live_id"] == lid
               else b["challenger_live_id"])
    opp = _live_get(conn, opp_lid)
    if not opp or opp["status"] != "live":
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That host isn't live anymore."},
            status_code=400)
    if _battle_busy(conn, lid) or _battle_busy(conn, opp_lid):
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "One of you is already in a "
                                   "Showdown."},
            status_code=400)
    new_id = _battle_insert_pending(conn, live, opp, b["duration_sec"])
    conn.close()
    if not new_id:
        return JSONResponse(
            {"ok": False, "error": "One of you is already in a "
                                   "Showdown."},
            status_code=400)
    notify(opp["host_id"], "showdown",
           f"⚔️ {user['name']} wants a REMATCH! Open your live "
           "to accept the Showdown.",
           f"/live/{opp_lid}", actor_id=user["id"])
    return JSONResponse({"ok": True, "battle_id": new_id})


@app.post("/live/{lid}/signal")
async def live_signal_send(request: Request, lid: int):
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    try:
        data = await request.json()
    except Exception:
        data = {}
    kind = data.get("kind") if isinstance(data, dict) else None
    if kind not in LIVE_SIGNAL_TYPES:
        return JSONResponse({"ok": False, "error": "Bad signal kind."},
                            status_code=400)
    try:
        to_id = int(data.get("to_id") or 0)
    except (TypeError, ValueError):
        to_id = 0
    payload = data.get("payload")
    if not isinstance(payload, str):
        payload = json.dumps(payload or {})
    if len(payload) > LIVE_MAX_PAYLOAD:
        return JSONResponse({"ok": False, "error": "Signal too big."},
                            status_code=400)
    # Signals only travel between people in this room: the host, the
    # on-camera panelists, current watchers, and people with a join
    # request in flight — never to an outsider, never to a banned
    # user, and never across a block. (The sender already passed
    # _live_can_watch, which refuses banned senders with a 403.)
    conn = db()
    allowed = {live["host_id"]}
    for m in _live_active_members(conn, lid):
        allowed.add(m["user_id"])
    guest_req = _live_guest_req(conn, lid)
    if guest_req:
        allowed.add(guest_req["user_id"])
    for v in conn.execute(
            "SELECT user_id FROM live_viewers WHERE live_id=? AND "
            "left_at IS NULL", (lid,)).fetchall():
        allowed.add(v["user_id"])
    for v in conn.execute(
            "SELECT user_id FROM live_join_requests WHERE live_id=? "
            "AND status IN ('pending','approved')", (lid,)).fetchall():
        allowed.add(v["user_id"])
    if to_id == user["id"] or to_id not in allowed \
            or _live_banned(conn, lid, to_id):
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That person isn't in this live."},
            status_code=403)
    if blocked_either_way(user["id"], to_id):
        conn.close()
        return JSONResponse({"ok": False, "error": "Blocked."},
                            status_code=403)
    cur = conn.execute(
        "INSERT INTO live_signals(live_id, from_id, to_id, kind, "
        "payload, created_at) VALUES(?,?,?,?,?,?)",
        (lid, user["id"], to_id, kind, payload, now_iso()))
    sid = cur.lastrowid
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "id": sid})


@app.get("/live/{lid}/signals")
async def live_signals_poll(request: Request, lid: int):
    """Deliver signals addressed to ME after ?since=<last id seen>,
    in order, marked consumed on delivery — the calls pattern, made
    point-to-point because a live room has many peers."""
    user, live, err = _live_access(request, lid)
    if err:
        return err
    try:
        since = int(request.query_params.get("since") or 0)
    except (TypeError, ValueError):
        since = 0
    conn = db()
    rows = conn.execute(
        "SELECT * FROM live_signals WHERE live_id=? AND to_id=? AND "
        "id>? ORDER BY id ASC LIMIT 200", (lid, user["id"], since)
    ).fetchall()
    if rows:
        ids = [r["id"] for r in rows]
        conn.execute(
            "UPDATE live_signals SET consumed=1 WHERE id IN (%s)"
            % ",".join("?" * len(ids)), ids)
        conn.commit()
    fresh = conn.execute(
        "SELECT status FROM live_sessions WHERE id=?", (lid,)).fetchone()
    conn.close()
    sigs = []
    for r in rows:
        payload = r["payload"]
        try:
            payload = json.loads(payload)
        except Exception:
            pass
        sigs.append({"id": r["id"], "from_id": r["from_id"],
                     "kind": r["kind"], "payload": payload,
                     "created_at": r["created_at"]})
    return JSONResponse({
        "ok": True,
        "status": fresh["status"] if fresh else live["status"],
        "signals": sigs})


@app.post("/live/{lid}/join-request")
async def live_join_request(request: Request, lid: int):
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    if user["id"] == live["host_id"]:
        return JSONResponse(
            {"ok": False, "error": "You're the host of this live."},
            status_code=400)
    try:
        data = await request.json()
    except Exception:
        data = {}
    _body = _live_parse_body(data)
    # _live_body_int defaults a MISSING key to 0 — but "no pick"
    # must stay None (any spot), distinct from picking spot 0.
    spot = _live_body_int(_body, "spot") if "spot" in _body \
        else None
    if spot is not None and not (0 <= spot < 8):
        spot = None
    conn = db()
    guest_req = _live_guest_req(conn, lid)
    if _live_member_row(conn, lid, user["id"]) or (
            guest_req and guest_req["user_id"] == user["id"]):
        conn.close()
        return JSONResponse({"ok": True, "already_guest": True})
    pending = conn.execute(
        "SELECT * FROM live_join_requests WHERE live_id=? AND "
        "user_id=? AND status='pending' AND kind='request' "
        "ORDER BY id DESC LIMIT 1",
        (lid, user["id"])).fetchone()
    if pending:
        if spot is not None:
            conn.execute(
                "UPDATE live_join_requests SET spot_idx=? "
                "WHERE id=?", (spot, pending["id"]))
            conn.commit()
        conn.close()
        return JSONResponse({"ok": True, "request": {
            "id": pending["id"], "status": "pending"}})
    last = conn.execute(
        "SELECT * FROM live_join_requests WHERE live_id=? AND "
        "user_id=? AND kind='request' ORDER BY id DESC LIMIT 1",
        (lid, user["id"])).fetchone()
    if last and last["status"] == "declined" and last["decided_at"]:
        decided = _parse_ts(last["decided_at"])
        if decided:
            left = LIVE_JOIN_COOLDOWN - int(
                (datetime.now(timezone.utc) - decided).total_seconds())
            if left > 0:
                conn.close()
                return JSONResponse(
                    {"ok": False, "cooldown": True,
                     "error": f"Take a breather — you can apply "
                              f"again in {left}s."}, status_code=429)
    if live.get("is_lobby"):
        # The Lobby's door is open: a join request seats you on
        # the spot while the panel has room. A full panel falls
        # back to the normal pending request below.
        cur = conn.execute(
            "INSERT INTO live_join_requests(live_id, user_id, "
            "status, created_at, kind, decided_at, spot_idx) "
            "VALUES(?,?,'approved',?,'request',?,?)",
            (lid, user["id"], now_iso(), now_iso(), spot))
        rid = cur.lastrowid
        if _live_add_member(conn, live, user["id"]):
            _apply_spot_name(conn, live, user["id"], spot)
            conn.commit()
            conn.close()
            return JSONResponse({"ok": True, "auto": True,
                                 "request": {"id": rid,
                                             "status": "approved"}})
        conn.execute(
            "UPDATE live_join_requests SET status='pending', "
            "decided_at=NULL WHERE id=?", (rid,))
        conn.commit()
        conn.close()
        return JSONResponse({"ok": True, "request": {
            "id": rid, "status": "pending"}})
    cur = conn.execute(
        "INSERT INTO live_join_requests(live_id, user_id, status, "
        "created_at, kind, spot_idx) "
        "VALUES(?,?,'pending',?,'request',?)",
        (lid, user["id"], now_iso(), spot))
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "request": {"id": rid,
                                                 "status": "pending"}})


@app.post("/live/{lid}/spot-names")
async def live_spot_names(request: Request, lid: int):
    """Host only: name the cast spots in order ('DJ, Co-Host,
    ...'). The names wait on the spots — applicants apply for
    them by name, and whoever the host seats there wears the
    name on their box."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    if live["host_id"] != user["id"]:
        return JSONResponse(
            {"ok": False,
             "error": "Only the host names the spots on their "
                      "platform."},
            status_code=403)
    try:
        data = await request.json()
    except Exception:
        data = {}
    raw = _live_parse_body(data).get("names")
    if isinstance(raw, str):
        parts = raw.split(",")
    elif isinstance(raw, list):
        parts = raw
    else:
        parts = []
    names = [" ".join(str(p).split())[:24] for p in parts][:8]
    while names and names[-1] == "":
        names.pop()
    conn = db()
    conn.execute("UPDATE live_sessions SET spot_names=? "
                 "WHERE id=?", (json.dumps(names), lid))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "names": names})


@app.post("/live/{lid}/join-suggest")
async def live_join_suggest(request: Request, lid: int):
    """⭐ Suggest an applicant to the host (Ken's panel rule:
    accepting is the HOST's alone — admins advise). Allowed
    for the live's admins and the Lobby's captain. The
    suggestion rides the request row into the host's queue."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    if live["host_id"] == user["id"]:
        return JSONResponse(
            {"ok": False,
             "error": "You're the host — just let them in."},
            status_code=400)
    try:
        data = await request.json()
    except Exception:
        data = {}
    rid = _live_body_int(_live_parse_body(data), "request_id")
    conn = db()
    captain = bool(live.get("is_lobby")) and \
        _lobby_captain_id(conn, live) == user["id"]
    if not _live_is_admin(conn, lid, user["id"]) and not captain:
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Only the host's admins can suggest."},
            status_code=403)
    req = conn.execute(
        "SELECT * FROM live_join_requests WHERE id=? AND live_id=?",
        (rid, lid)).fetchone()
    if not req or req["status"] != "pending" \
            or (dict(req).get("kind") or "request") != "request":
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "That application isn't waiting anymore."},
            status_code=400)
    conn.execute("UPDATE live_join_requests SET suggested_by=? "
                 "WHERE id=?", (user["id"], rid))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.post("/live/{lid}/join-decide")
async def live_join_decide(request: Request, lid: int):
    """The HOST alone settles a kind='request' application
    (Ken's panel rule, Oct 30: admins ⭐ suggest, they don't
    accept). Approving puts the applicant on the panel
    (live_members + panel_order) unless the cast is full, and
    seats them under their spot's name."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    conn = db()
    if live["host_id"] != user["id"]:
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Only the host accepts applications — "
                      "admins can ⭐ suggest instead."},
            status_code=403)
    try:
        data = await request.json()
    except Exception:
        data = {}
    data = _live_parse_body(data)
    rid = _live_body_int(data, "request_id")
    approve = bool(data.get("approve"))
    req = conn.execute(
        "SELECT * FROM live_join_requests WHERE id=? AND live_id=?",
        (rid, lid)).fetchone()
    if not req or req["status"] != "pending":
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That request is already settled."},
            status_code=409)
    if (dict(req).get("kind") or "request") != "request":
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "That's an invite, not a join request."},
            status_code=400)
    if approve:
        # Build X: two fullness gates, hard cap FIRST so its exact
        # long-standing message wins at the absolute limit, then
        # the host's own boxes allowance.
        if _live_panel_size(conn, lid) >= LIVE_PANEL_MAX:
            conn.close()
            return JSONResponse(
                {"ok": False,
                 "error": "Panel is full (8 guests max)."},
                status_code=409)
        if len(_live_active_members(conn, lid)) \
                >= _live_panel_boxes(live):
            conn.close()
            return JSONResponse(
                {"ok": False,
                 "error": "All spots are full — raise your "
                          "cast size or remove someone."},
                status_code=409)
        if not _live_add_member(conn, live, req["user_id"]):
            conn.close()
            return JSONResponse(
                {"ok": False,
                 "error": "Panel is full (8 guests max)."},
                status_code=409)
        _apply_spot_name(conn, live, req["user_id"],
                         dict(req).get("spot_idx"))
    conn.execute(
        "UPDATE live_join_requests SET status=?, decided_at=? "
        "WHERE id=?",
        ("approved" if approve else "declined", now_iso(), rid))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.post("/live/{lid}/panel-open")
async def live_panel_open(request: Request, lid: int):
    """Build X, host only: open or close the panel grid. Opening
    shows the grid + empty Request squares to the whole room.
    Closing is refused while guests are on the panel (nobody is
    ever kicked by a layout change); the first approved guest
    opens the panel on its own (see _live_add_member)."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    if live["host_id"] != user["id"]:
        return JSONResponse(
            {"ok": False,
             "error": "Only the host can open or close the panel."},
            status_code=403)
    try:
        data = await request.json()
    except Exception:
        data = {}
    data = _live_parse_body(data)
    want_open = bool(data.get("open"))
    conn = db()
    if not want_open and _live_active_members(conn, lid):
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "The panel can't close while guests are on "
                      "it — remove them first, or let them step "
                      "down."},
            status_code=400)
    conn.execute(
        "UPDATE live_sessions SET panel_open=? WHERE id=?",
        (1 if want_open else 0, lid))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "panel_open": want_open})


@app.post("/live/{lid}/panel-exit")
async def live_panel_exit(request: Request, lid: int):
    """Host only: EXIT PANEL — every guest steps down to watcher
    (nobody leaves the room), the panel closes, and the host is
    full screen alone with whatever they are streaming. Ken's
    ask (Oct 28): one tap when the panel is done, instead of
    removing guests one by one. Reversible: reopen the panel
    and invite again."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    if live["host_id"] != user["id"]:
        return JSONResponse(
            {"ok": False,
             "error": "Only the host can exit the panel."},
            status_code=403)
    conn = db()
    members = _live_active_members(conn, lid)
    for m in members:
        _drop_guest(conn, live, m["user_id"])
    conn.execute(
        "UPDATE live_sessions SET panel_open=0, spotlight_id=NULL "
        "WHERE id=?", (lid,))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "dropped": len(members)})


@app.post("/live/{lid}/panel-boxes")
async def live_panel_boxes(request: Request, lid: int):
    """Build X, host only: set how many GUEST squares the panel
    shows (1..8, clamped; default 5). Lowering the count never
    kicks anyone — it only stops further approvals until the
    panel thins out or the host raises it again."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    if live["host_id"] != user["id"]:
        return JSONResponse(
            {"ok": False,
             "error": "Only the host can set the panel boxes."},
            status_code=403)
    try:
        data = await request.json()
    except Exception:
        data = {}
    data = _live_parse_body(data)
    n = _live_body_int(data, "boxes")
    n = max(1, min(8, n or 5))
    conn = db()
    conn.execute(
        "UPDATE live_sessions SET panel_boxes=? WHERE id=?",
        (n, lid))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "panel_boxes": n})


@app.post("/live/{lid}/guest-leave")
async def live_guest_leave(request: Request, lid: int):
    """A panelist steps down (their tile frees, peers tear down via
    'bye' signals). Generalized from the single-guest flow: ANY
    active member may leave, and the host may still remove one
    panelist by naming user_id (the legacy host-removes-guest
    call). The host calling it bare is a no-op — the host ends
    the live instead of leaving the panel."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    try:
        data = await request.json()
    except Exception:
        data = {}
    data = _live_parse_body(data)
    target = _live_body_int(data, "user_id") or None
    conn = db()
    if user["id"] == live["host_id"]:
        if target is None or target == user["id"]:
            conn.close()
            return JSONResponse({"ok": True})
        if _live_member_row(conn, lid, target):
            _drop_guest(conn, live, target)
            conn.commit()
            conn.close()
            return JSONResponse({"ok": True})
        guest_req = _live_guest_req(conn, lid)
        if guest_req and guest_req["user_id"] == target:
            _drop_guest(conn, live, target)
            conn.commit()
            conn.close()
            return JSONResponse({"ok": True})
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That person isn't on the panel."},
            status_code=400)
    if target is not None and target != user["id"]:
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "You can only step down yourself."},
            status_code=403)
    if _live_member_row(conn, lid, user["id"]):
        _drop_guest(conn, live, user["id"])
        conn.commit()
        conn.close()
        return JSONResponse({"ok": True})
    guest_req = _live_guest_req(conn, lid)
    if guest_req and guest_req["user_id"] == user["id"]:
        _drop_guest(conn, live, user["id"])
        conn.commit()
        conn.close()
        return JSONResponse({"ok": True})
    panel_empty = not _live_active_members(conn, lid)
    conn.close()
    return JSONResponse(
        {"ok": False,
         "error": "Nobody is on camera right now." if panel_empty
                  else "You're not on the panel."},
        status_code=409)


# ---------------- live panel: admins, invites, moderation, likes ----
@app.post("/live/{lid}/backdrop")
async def live_backdrop(request: Request, lid: int):
    """Platform branding (host only): set or clear the backdrop
    image — the host's logo — that covers the stage behind the
    tiles for everybody in the room."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    if live["host_id"] != user["id"]:
        return JSONResponse(
            {"ok": False,
             "error": "Only the host brands their platform."},
            status_code=403)
    form = await request.form()
    conn = db()
    old = live.get("backdrop") or ""
    if (form.get("clear") or "") == "1":
        conn.execute("UPDATE live_sessions SET backdrop='' "
                     "WHERE id=?", (lid,))
        conn.commit()
        conn.close()
        if old:
            _delete_upload(old)
        return JSONResponse({"ok": True, "url": ""})
    try:
        key = save_upload(form.get("backdrop"), "backdrops",
                          IMG_EXTS, max_mb=5)
    except ValueError as e:
        conn.close()
        return JSONResponse({"ok": False, "error": str(e)},
                            status_code=400)
    if not key:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "Pick an image first."},
            status_code=400)
    conn.execute("UPDATE live_sessions SET backdrop=? WHERE id=?",
                 (key, lid))
    conn.commit()
    conn.close()
    if old and old != key:
        _delete_upload(old)
    return JSONResponse({"ok": True, "url": avatar_url(key)})


@app.post("/live/{lid}/backdrop-fit")
async def live_backdrop_fit(request: Request, lid: int):
    """Backdrop fit (host only, Ken Oct 30): his backdrop art
    is portrait while his capture-card stream runs landscape,
    so 'cover' chops the art. 'fit' shows the whole picture
    (blur-filled sides); 'auto' picks per screen shape on
    every client; 'cover' is the original full-bleed."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    if live["host_id"] != user["id"]:
        return JSONResponse(
            {"ok": False,
             "error": "Only the host brands their platform."},
            status_code=403)
    try:
        data = await request.json()
    except Exception:
        data = {}
    fit = str(_live_parse_body(data).get("fit") or "").strip()
    if fit not in ("auto", "cover", "fit"):
        return JSONResponse(
            {"ok": False, "error": "Pick Auto, Fill or Fit."},
            status_code=400)
    conn = db()
    conn.execute("UPDATE live_sessions SET backdrop_fit=? "
                 "WHERE id=?", (fit, lid))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "fit": fit})


@app.post("/live/{lid}/seat-label")
async def live_seat_label(request: Request, lid: int):
    """Name a panel seat (Ken's Platforms order: the blocks are
    custom-named — a co-host chair, a DJ booth, Caller #1).
    The host or an admin may name any seat; a panelist may
    name their own. Blank clears back to the person's name."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    try:
        data = await request.json()
    except Exception:
        data = {}
    target = _live_body_int(_live_parse_body(data), "user_id")
    label = " ".join(str(
        _live_parse_body(data).get("label") or "").split())[:24]
    if not target:
        return JSONResponse({"ok": False, "error": "Which seat?"},
                            status_code=400)
    conn = db()
    allowed = (live["host_id"] == user["id"]
               or target == user["id"]
               or _live_is_admin(conn, lid, user["id"]))
    row = _live_member_row(conn, lid, target)
    if not allowed:
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "The host names the seats on their platform."},
            status_code=403)
    if not row or row.get("left_at"):
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That seat is empty right now."},
            status_code=400)
    conn.execute("UPDATE live_members SET seat_label=? "
                 "WHERE live_id=? AND user_id=?",
                 (label, lid, target))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "label": label})


@app.post("/live/{lid}/appoint-admin")
async def live_appoint_admin(request: Request, lid: int):
    """Host only: appoint a person IN the room (active watcher or
    panel member) as an admin of this live. Admins can decide join
    requests, invite, kick non-admins, mute members, reorder
    tiles, and delete comments in this room only."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    if live["host_id"] != user["id"]:
        return JSONResponse(
            {"ok": False, "error": "Only the host can appoint admins."},
            status_code=403)
    try:
        data = await request.json()
    except Exception:
        data = {}
    target = _live_body_int(_live_parse_body(data), "user_id")
    if not target or target == live["host_id"]:
        return JSONResponse(
            {"ok": False, "error": "The host runs this live already."},
            status_code=400)
    conn = db()
    if _live_is_admin(conn, lid, target):
        conn.close()
        return JSONResponse({"ok": True, "already_admin": True})
    watcher_ids = {v["user_id"] for v in _live_viewer_rows(conn, lid)}
    if target not in watcher_ids \
            and not _live_member_row(conn, lid, target):
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That person isn't in this live."},
            status_code=400)
    stamp = now_iso()
    existing = conn.execute(
        "SELECT * FROM live_admins WHERE live_id=? AND user_id=?",
        (lid, target)).fetchone()
    if existing:
        conn.execute(
            "UPDATE live_admins SET revoked_at=NULL, appointed_by=?, "
            "created_at=? WHERE live_id=? AND user_id=?",
            (user["id"], stamp, lid, target))
    else:
        conn.execute(
            "INSERT INTO live_admins(live_id, user_id, appointed_by, "
            "created_at) VALUES(?,?,?,?)",
            (lid, target, user["id"], stamp))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.post("/live/{lid}/remove-admin")
async def live_remove_admin(request: Request, lid: int):
    """Host only: revoke a live admin appointment."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    if live["host_id"] != user["id"]:
        return JSONResponse(
            {"ok": False, "error": "Only the host can remove admins."},
            status_code=403)
    try:
        data = await request.json()
    except Exception:
        data = {}
    target = _live_body_int(_live_parse_body(data), "user_id")
    conn = db()
    cur = conn.execute(
        "UPDATE live_admins SET revoked_at=? WHERE live_id=? AND "
        "user_id=? AND revoked_at IS NULL", (now_iso(), lid, target))
    changed = cur.rowcount
    conn.commit()
    conn.close()
    if not changed:
        return JSONResponse(
            {"ok": False, "error": "That person isn't an admin."},
            status_code=404)
    return JSONResponse({"ok": True})


@app.post("/live/{lid}/invite")
async def live_invite(request: Request, lid: int):
    """Host or admin invites someone onto the panel: a current
    watcher of this live, OR a friend of the inviter, OR someone the
    inviter follows (Ken's "Invite others" — you shouldn't have to
    already be in the room to be invited). The target must also be
    able to watch this live at all (_live_can_watch: no room ban, no
    block with the host, private host = friends only). The invite is
    a live_join_requests row with kind='invite' whose user_id is the
    INVITEE and inviter_id the caller (kind is how invite-decide
    tells it apart from a watcher-initiated request). One pending
    invite per target per live."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    try:
        data = await request.json()
    except Exception:
        data = {}
    target = _live_body_int(_live_parse_body(data), "user_id")
    conn = db()
    if live["host_id"] != user["id"] \
            and not _live_is_admin(conn, lid, user["id"]):
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Only the host or an admin can invite."},
            status_code=403)
    if not target or target == live["host_id"]:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "You can't invite the host."},
            status_code=400)
    if _live_banned(conn, lid, target):
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "That person is banned from this live."},
            status_code=400)
    if _live_member_row(conn, lid, target):
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "They're already on the panel."},
            status_code=400)
    target_user = get_user(target)
    if not target_user:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "User not found."},
            status_code=404)
    if not _live_can_watch(target_user, live,
                           owner_on=is_owner(request)):
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That person can't join this live."},
            status_code=400)
    watcher_ids = {v["user_id"] for v in _live_viewer_rows(conn, lid)}
    eligible = target in watcher_ids \
        or target in friend_id_set(user["id"]) \
        or is_following(user["id"], target)
    if not eligible:
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "You can only invite friends, people you follow, "
                      "or people watching this live."},
            status_code=400)
    existing = conn.execute(
        "SELECT * FROM live_join_requests WHERE live_id=? AND "
        "user_id=? AND kind='invite' AND status='pending' "
        "ORDER BY id DESC LIMIT 1", (lid, target)).fetchone()
    if existing:
        eid = existing["id"]
        conn.close()
        return JSONResponse({"ok": True, "invite": {
            "id": eid, "status": "pending"}})
    cur = conn.execute(
        "INSERT INTO live_join_requests(live_id, user_id, status, "
        "created_at, kind, inviter_id) "
        "VALUES(?,?,'pending',?,'invite',?)",
        (lid, target, now_iso(), user["id"]))
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    notify(target, "live_invite",
           f"🔴 {user['name']} invited you to join a live panel",
           f"/live/{lid}", actor_id=user["id"])
    if notifications_allowed(target, user["id"]):
        send_push(target,
                  f"🔴 {user['name']} invited you to join a live panel",
                  "Open HERE to join the live.",
                  f"/live/{lid}", tag=f"live-invite-{lid}")
    return JSONResponse({"ok": True, "invite": {"id": rid,
                                                 "status": "pending"}})


@app.post("/live/{lid}/invite-decide")
async def live_invite_decide(request: Request, lid: int):
    """The INVITEE settles their own kind='invite' row: accept puts
    them on the panel (host + 8 guests cap, same message as
    join-decide); decline just settles the row."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    try:
        data = await request.json()
    except Exception:
        data = {}
    data = _live_parse_body(data)
    rid = _live_body_int(data, "request_id")
    accept = bool(data.get("accept"))
    conn = db()
    req = conn.execute(
        "SELECT * FROM live_join_requests WHERE id=? AND live_id=?",
        (rid, lid)).fetchone()
    if not req:
        conn.close()
        return JSONResponse({"ok": False, "error": "not found"},
                            status_code=404)
    if req["user_id"] != user["id"]:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That invite isn't for you."},
            status_code=403)
    if (dict(req).get("kind") or "request") != "invite":
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That's not an invite."},
            status_code=400)
    if req["status"] != "pending":
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That invite is already settled."},
            status_code=409)
    if accept:
        # An invitee who was invited from outside the room (a friend /
        # someone the inviter follows) may have no live_viewers row yet
        # — accepting IS their arrival, so give them the heartbeat
        # upsert a watcher gets, then put them on the panel.
        stamp = now_iso()
        conn.execute(
            "INSERT INTO live_viewers(live_id, user_id, joined_at, "
            "last_seen_at) VALUES(?,?,?,?) ON CONFLICT(live_id, user_id) "
            "DO UPDATE SET last_seen_at=excluded.last_seen_at, "
            "left_at=NULL", (lid, user["id"], stamp, stamp))
    if accept and not _live_add_member(conn, live, user["id"]):
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "Panel is full (8 guests max)."},
            status_code=409)
    conn.execute(
        "UPDATE live_join_requests SET status=?, decided_at=? "
        "WHERE id=?",
        ("approved" if accept else "declined", now_iso(), rid))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.post("/live/{lid}/kick")
async def live_kick(request: Request, lid: int):
    """Remove someone from the panel ('panel') or from the room
    entirely ('room' = panel effects + a live_bans row + their
    viewer row closed). Hierarchy: the host may kick anyone but
    themselves; an admin may kick panel guests (non-admin members)
    and watchers only — never the host, never another admin."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    try:
        data = await request.json()
    except Exception:
        data = {}
    data = _live_parse_body(data)
    target = _live_body_int(data, "user_id")
    scope = str(data.get("scope") or "panel")
    if scope not in ("panel", "room"):
        return JSONResponse({"ok": False, "error": "Bad scope."},
                            status_code=400)
    if not target:
        return JSONResponse(
            {"ok": False, "error": "Missing user_id."},
            status_code=400)
    conn = db()
    is_host = user["id"] == live["host_id"]
    caller_admin = _live_is_admin(conn, lid, user["id"]) \
        or _is_lobby_captain(conn, live, user["id"])
    if not is_host and not caller_admin:
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Only the host or an admin can kick."},
            status_code=403)
    if target == user["id"]:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "You can't kick yourself."},
            status_code=400)
    if not is_host:
        if target == live["host_id"]:
            conn.close()
            return JSONResponse(
                {"ok": False, "error": "Admins can't kick the host."},
                status_code=403)
        if _live_is_admin(conn, lid, target):
            conn.close()
            return JSONResponse(
                {"ok": False,
                 "error": "Admins can't kick other admins."},
                status_code=403)
    member = _live_member_row(conn, lid, target)
    watcher_ids = {v["user_id"] for v in _live_viewer_rows(conn, lid)}
    target_admin = _live_is_admin(conn, lid, target)
    if not member and target not in watcher_ids and not target_admin:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That person isn't in this live."},
            status_code=400)
    # Teardown first, while the roster still names the target, so
    # every peer drops the target's connection at once.
    _live_signal_teardown(conn, live, target)
    if member:
        _live_remove_member(conn, live, target)
    if target_admin:
        # Losing your seat loses the badge too: a kicked admin's
        # appointment is revoked in the same pass.
        conn.execute(
            "UPDATE live_admins SET revoked_at=? WHERE live_id=? "
            "AND user_id=? AND revoked_at IS NULL",
            (now_iso(), lid, target))
    if scope == "room":
        conn.execute(
            "INSERT OR IGNORE INTO live_bans(live_id, user_id, "
            "by_id, created_at) VALUES(?,?,?,?)",
            (lid, target, user["id"], now_iso()))
        conn.execute(
            "UPDATE live_viewers SET left_at=? WHERE live_id=? AND "
            "user_id=?", (now_iso(), lid, target))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.post("/live/{lid}/mute")
async def live_mute(request: Request, lid: int):
    """Moderation mute for a panel member's mic (a roster flag —
    the member's client honors it, and the tile shows it). Host:
    any member. Admin: only members who are not admins and not
    the host. Nobody can mute themselves, not even to unmute —
    a muted member asks a moderator."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    try:
        data = await request.json()
    except Exception:
        data = {}
    data = _live_parse_body(data)
    target = _live_body_int(data, "user_id")
    mute_val = 1 if data.get("mute") else 0
    conn = db()
    is_host = user["id"] == live["host_id"]
    caller_admin = _live_is_admin(conn, lid, user["id"]) \
        or _is_lobby_captain(conn, live, user["id"])
    if not is_host and not caller_admin:
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Only the host or an admin can mute."},
            status_code=403)
    if target == user["id"]:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "You can't mute yourself."},
            status_code=403)
    if target == live["host_id"]:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "The host can't be muted."},
            status_code=403)
    if caller_admin and not is_host \
            and _live_is_admin(conn, lid, target):
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Admins can't mute other admins."},
            status_code=403)
    if not _live_member_row(conn, lid, target):
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That person isn't on the panel."},
            status_code=400)
    conn.execute(
        "UPDATE live_members SET muted=? WHERE live_id=? AND "
        "user_id=? AND left_at IS NULL", (mute_val, lid, target))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "muted": mute_val})


@app.post("/live/{lid}/move")
async def live_move(request: Request, lid: int):
    """Host or admin reorders a tile: swap user_id with its neighbor
    in panel_order (dir -1 = earlier, +1 = later). Out-of-range is
    a no-op that still returns the current order."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    try:
        data = await request.json()
    except Exception:
        data = {}
    data = _live_parse_body(data)
    target = _live_body_int(data, "user_id")
    step_raw = _live_body_int(data, "dir")
    step = 1 if step_raw > 0 else (-1 if step_raw < 0 else 0)
    conn = db()
    if live["host_id"] != user["id"] \
            and not _live_is_admin(conn, lid, user["id"]) \
            and not _is_lobby_captain(conn, live, user["id"]):
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Only the host or an admin can reorder tiles."},
            status_code=403)
    ids = _live_panel_ids(conn, live)
    if target not in ids:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That person isn't on the panel."},
            status_code=400)
    i = ids.index(target)
    j = i + step
    if step != 0 and 0 <= j < len(ids):
        ids[i], ids[j] = ids[j], ids[i]
        _live_set_panel_order(conn, lid, ids)
        conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "panel_order": ids})


@app.post("/live/{lid}/auction/start")
async def live_auction_start(request: Request, lid: int):
    """Start a live auction (host only — they run the show). The
    item is one of the live's shop listings, or a custom title.
    The listing's SELLER is the deal counterparty when a listing
    is used. Bids are claims to buy under HERE's Market rules —
    no money moves in-app; winner and seller settle directly."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    if user["id"] != live["host_id"]:
        return JSONResponse(
            {"ok": False,
             "error": "Only the host can start an auction."},
            status_code=403)
    try:
        data = await request.json()
    except Exception:
        data = {}
    data = _live_parse_body(data)
    import time as _time
    seconds = _live_body_int(data, "seconds") or 120
    seconds = max(30, min(900, seconds))
    start_cents = _live_body_int(data, "start_cents")
    listing_id = _live_body_int(data, "listing_id")
    conn = db()
    running = conn.execute(
        "SELECT id FROM live_auctions WHERE live_id=? AND "
        "status='running'", (lid,)).fetchone()
    if running:
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "An auction is already running — let it "
                      "finish first."}, status_code=400)
    title = ""
    photo = ""
    seller_id = user["id"]
    if listing_id:
        row = conn.execute(
            "SELECT * FROM market_listings WHERE id=? AND "
            "live_id=? AND status='active'",
            (listing_id, lid)).fetchone()
        if not row:
            conn.close()
            return JSONResponse(
                {"ok": False,
                 "error": "That item isn't on this live's shop."},
                status_code=400)
        title = row["title"] or "Item"
        _ph = _market_photos(row)
        photo = ("/uploads/" + _ph[0]) if _ph else ""
        seller_id = row["seller_id"]
        if not start_cents:
            start_cents = row["price_cents"]
    else:
        title = str(data.get("title") or "").strip()[:80]
        if not title:
            conn.close()
            return JSONResponse(
                {"ok": False, "error": "Name the item first."},
                status_code=400)
    start_cents = max(100, start_cents or 0)
    cur = conn.execute(
        "INSERT INTO live_auctions(live_id, listing_id, title, "
        "photo, start_cents, current_cents, leader_id, ends_at, "
        "status, created_by, created_at) VALUES(?,?,?,?,?,?,NULL,"
        "?,'running',?,?)",
        (lid, listing_id, title, photo, start_cents, start_cents,
         _time.time() + seconds, seller_id, now_iso()))
    conn.commit()
    a = conn.execute("SELECT * FROM live_auctions WHERE id=?",
                     (cur.lastrowid,)).fetchone()
    out = _auction_json(conn, dict(a), user["id"])
    conn.close()
    return JSONResponse({"ok": True, "auction": out})


@app.post("/live/{lid}/auction/bid")
async def live_auction_bid(request: Request, lid: int):
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    try:
        data = await request.json()
    except Exception:
        data = {}
    data = _live_parse_body(data)
    amount = _live_body_int(data, "amount_cents")
    import time as _time
    conn = db()
    a = conn.execute(
        "SELECT * FROM live_auctions WHERE live_id=? AND "
        "status='running' ORDER BY id DESC LIMIT 1",
        (lid,)).fetchone()
    if not a:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "No auction is running."},
            status_code=400)
    if user["id"] == a["created_by"]:
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "The seller can't bid on their own item."},
            status_code=400)
    need = (a["current_cents"] if not a["leader_id"] else
            a["current_cents"] + AUCTION_MIN_STEP_CENTS)
    if not amount or amount < need:
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": f"Bid at least {_money(need)}."},
            status_code=400)
    prev = a["leader_id"]
    ends = a["ends_at"]
    now = _time.time()
    if ends - now < AUCTION_SNIPE_WINDOW:
        ends = now + AUCTION_SNIPE_EXTEND
    conn.execute(
        "UPDATE live_auctions SET current_cents=?, leader_id=?, "
        "ends_at=? WHERE id=?", (amount, user["id"], ends, a["id"]))
    conn.commit()
    a2 = conn.execute("SELECT * FROM live_auctions WHERE id=?",
                      (a["id"],)).fetchone()
    out = _auction_json(conn, dict(a2), user["id"])
    conn.close()
    if prev and prev != user["id"]:
        notify(prev, "auction_outbid",
               f"You've been outbid on “{a['title']}” — it's at "
               f"{_money(amount)} now. Jump back in!",
               f"/live/{lid}", actor_id=user["id"])
    return JSONResponse({"ok": True, "auction": out})


@app.post("/live/{lid}/auction/cancel")
async def live_auction_cancel(request: Request, lid: int):
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    conn = db()
    a = conn.execute(
        "SELECT * FROM live_auctions WHERE live_id=? AND "
        "status='running' ORDER BY id DESC LIMIT 1",
        (lid,)).fetchone()
    if not a:
        conn.close()
        return JSONResponse({"ok": True})
    if user["id"] != live["host_id"] \
            and user["id"] != a["created_by"]:
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Only the host or the seller can cancel."},
            status_code=403)
    conn.execute(
        "UPDATE live_auctions SET status='cancelled', settled=1 "
        "WHERE id=?", (a["id"],))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.post("/live/{lid}/rank")
async def live_rank(request: Request, lid: int):
    """Talk-show ranks (Ken, Oct 28): a member steps UP by being
    great on the mic. The host may set any member's rank; an
    admin may set a non-admin member's. Ranks: 'guest' (just
    joined the panel) and 'regular' (earned their spot). Admin
    itself is the live_admins appointment, not a rank."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    try:
        data = await request.json()
    except Exception:
        data = {}
    data = _live_parse_body(data)
    target = _live_body_int(data, "user_id")
    rank = str(data.get("rank") or "").strip().lower()
    if rank not in ("guest", "regular"):
        return JSONResponse(
            {"ok": False, "error": "Unknown rank."}, status_code=400)
    conn = db()
    is_host = live["host_id"] == user["id"]
    if not is_host and not _live_is_admin(conn, lid, user["id"]):
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Only the host or an admin can set ranks."},
            status_code=403)
    if target == live["host_id"] or not _live_member_row(
            conn, lid, target):
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That person isn't on the panel."},
            status_code=400)
    if not is_host and _live_is_admin(conn, lid, target):
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Admins can't change another admin's rank."},
            status_code=403)
    conn.execute(
        "UPDATE live_members SET rank=? WHERE live_id=? AND "
        "user_id=? AND left_at IS NULL", (rank, lid, target))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "rank": rank})


@app.post("/live/{lid}/panel-order")
async def live_panel_order(request: Request, lid: int):
    """Host or admin sets the WHOLE tile order at once (the
    drag-a-block path; /move remains the one-step swap). The
    submitted order must be exactly the current panel — a
    permutation, never an edit of who's on it."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    try:
        data = await request.json()
    except Exception:
        data = {}
    data = _live_parse_body(data)
    raw = data.get("order")
    if not isinstance(raw, list):
        return JSONResponse(
            {"ok": False, "error": "Send the full tile order."},
            status_code=400)
    order = []
    for v in raw:
        try:
            order.append(int(v))
        except (TypeError, ValueError):
            return JSONResponse(
                {"ok": False, "error": "Bad tile order."},
                status_code=400)
    conn = db()
    if live["host_id"] != user["id"] \
            and not _live_is_admin(conn, lid, user["id"]) \
            and not _is_lobby_captain(conn, live, user["id"]):
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Only the host or an admin can move tiles."},
            status_code=403)
    ids = _live_panel_ids(conn, live)
    if sorted(order) != sorted(ids) or len(order) != len(ids):
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "That order doesn't match the panel."},
            status_code=400)
    _live_set_panel_order(conn, lid, order)
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "panel_order": order})


@app.post("/live/{lid}/spotlight")
async def live_spotlight(request: Request, lid: int):
    """Host or admin puts one panelist in the SPOTLIGHT — their
    tile takes the big stage spot on every screen (the person
    talking, the co-streamer's cast). user_id null clears it."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    try:
        data = await request.json()
    except Exception:
        data = {}
    data = _live_parse_body(data)
    target = data.get("user_id")
    if target is not None:
        try:
            target = int(target)
        except (TypeError, ValueError):
            return JSONResponse(
                {"ok": False, "error": "Bad user."}, status_code=400)
    conn = db()
    if live["host_id"] != user["id"] \
            and not _live_is_admin(conn, lid, user["id"]) \
            and not _is_lobby_captain(conn, live, user["id"]):
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Only the host or an admin can spotlight."},
            status_code=403)
    if target is not None and target not in _live_panel_ids(
            conn, live):
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That person isn't on the panel."},
            status_code=400)
    conn.execute(
        "UPDATE live_sessions SET spotlight_id=? WHERE id=?",
        (target, lid))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "spotlight_id": target})


@app.post("/live/{lid}/like")
async def live_like(request: Request, lid: int):
    """A heart: one tap = one like_count tick plus one like EVENT
    row — the event stream is what other clients turn into heart
    bursts off the state poll's new_likes/since_like cursor (the
    count alone would only ever show a number going up)."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    conn = db()
    conn.execute(
        "INSERT INTO live_like_events(live_id, user_id, created_at) "
        "VALUES(?,?,?)", (lid, user["id"], now_iso()))
    conn.execute(
        "UPDATE live_sessions SET like_count=COALESCE(like_count,0)+1 "
        "WHERE id=?", (lid,))
    row = conn.execute(
        "SELECT like_count FROM live_sessions WHERE id=?",
        (lid,)).fetchone()
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True,
                         "like_count": row["like_count"] if row else 0})



# ---------------- notifications ----------------
@app.get("/notifications", response_class=HTMLResponse)
async def notifications_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    rows = conn.execute(
        "SELECT * FROM notifications WHERE user_id=? ORDER BY created_at DESC LIMIT 100",
        (user["id"],)).fetchall()
    conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=?", (user["id"],))
    conn.commit()
    conn.close()
    notes = [{"id": r["id"], "kind": r["kind"], "text": r["text"],
              "link": r["link"] or "", "created_at": fancy_time(r["created_at"]),
              "is_read": bool(r["is_read"])} for r in rows]
    return render(request, "notifications.html", {"notes": notes})


@app.post("/notifications/clear")
async def notifications_clear(request: Request):
    """Clear ALL of the caller's notifications — the bell starts over
    from empty. Only the caller's own rows are touched."""
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    conn.execute("DELETE FROM notifications WHERE user_id=?",
                 (user["id"],))
    conn.commit()
    conn.close()
    return redir("/notifications", "All notifications cleared.")


# ---------------- discover ----------------
@app.get("/tag/{tag}", response_class=HTMLResponse)
async def tag_page(request: Request, tag: str):
    """Build TAGS: the hashtag page — every post carrying this tag
    that the viewer may see, newest first. Visibility is EXACTLY the
    standard rules: the post_hashtags index picks the candidates,
    then post_card/can_view_post decide (post privacy, private
    profiles, blocks, pending wall posts), posts the viewer hid stay
    hidden, and silenced authors stay out — no friends-only leaks."""
    user, err = require_login(request)
    if err:
        return err
    t = (tag or "").strip().lstrip("#").lower()
    if not re.fullmatch(r"[a-z0-9_]{1,50}", t or ""):
        return redir("/discover", "That hashtag isn't valid.")
    conn = db()
    rows = conn.execute(
        "SELECT p.* FROM post_hashtags ph JOIN posts p ON p.id=ph.post_id "
        "WHERE ph.tag=? ORDER BY p.created_at DESC LIMIT 100",
        (t,)).fetchall()
    hidden = {r["post_id"] for r in conn.execute(
        "SELECT post_id FROM hidden_posts WHERE user_id=?",
        (user["id"],)).fetchall()}
    conn.close()
    owner_on = is_owner(request)
    muted = muted_id_set(user["id"])
    cards = []
    for r in rows:
        d = dict(r)
        if d["user_id"] in muted:
            continue
        if (d.get("shared_post_id") or d["id"]) in hidden:
            continue
        card = post_card(d, user, owner_on)
        if card:
            cards.append(card)
    return render(request, "tag.html",
                  {"tag": t, "posts": cards, "count": len(cards)})


@app.get("/discover", response_class=HTMLResponse)
async def discover(request: Request):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    irows = conn.execute(
        "SELECT invited_by AS uid, COUNT(*) AS c FROM users "
        "WHERE invited_by IS NOT NULL GROUP BY invited_by "
        "HAVING COUNT(*) >= 10 ORDER BY COUNT(*) DESC").fetchall()
    conn.close()
    insiders = []
    for r in irows[:12]:
        iu = get_user(r["uid"])
        if iu:
            insiders.append({"id": iu["id"], "name": iu["name"],
                             "avatar": avatar_url(iu.get("avatar", ""))})
    return render(request, "discover.html",
                  {"people": discover_people(user["id"], 20),
                   "trends": trending_topics(15, user["id"]),
                   "tags": trending_tags(10, user),
                   "insiders": insiders})


# ---------------- search ----------------
@app.get("/search", response_class=HTMLResponse)
async def search(request: Request):
    user, err = require_login(request)
    if err:
        return err
    q = (request.query_params.get("q") or "").strip()
    users, pages, posts = [], [], []
    if q:
        # Token-based people search: split the query into tokens and match
        # ANY token against first/last/display name (the name field holds the
        # full name, so "ken" and "cooley" both find "Kendrick Cooley"),
        # case-insensitive partial match. Exact-phrase hits rank first.
        tokens = [t for t in q.split() if t]
        conn = db()
        ors = " OR ".join(["name LIKE ?"] * len(tokens))
        params = [f"%{t}%" for t in tokens]
        urows = conn.execute(
            f"SELECT id,name,avatar,last_seen_at,status_mode,inbox_status,"
            f"private_profile,is_verified_user,is_verified_vendor "
            f"FROM users WHERE ({ors}) OR email LIKE ? "
            f"ORDER BY CASE WHEN name LIKE ? THEN 0 ELSE 1 END, name LIMIT 50",
            params + [f"%{q}%", f"%{q}%"]).fetchall()
        prows = conn.execute(
            "SELECT id,name,ptype FROM pages WHERE name LIKE ? ORDER BY name LIMIT 50",
            (f"%{q}%",)).fetchall()
        # People search still FINDS private users (with a 🔒 marker, so
        # they can be friended) — but anyone in a block relationship
        # with the viewer (either direction) is excluded entirely.
        _blocked_search = blocked_id_set(user["id"])
        users = [{"id": r["id"], "name": r["name"],
                  "avatar": avatar_url(r["avatar"]),
                  "presence": presence_of(r),
                  "is_self": r["id"] == user["id"],
                  "is_private": bool(r["private_profile"]),
                  "vuser": bool(r["is_verified_user"]),
                  "vvendor": bool(r["is_verified_vendor"]),
                  "req": req_status(user["id"], r["id"])}
                 for r in urows if r["id"] not in _blocked_search]
        pages = []
        for r in prows:
            joined = bool(conn.execute(
                "SELECT 1 FROM page_members WHERE page_id=? AND user_id=?",
                (r["id"], user["id"])).fetchone())
            pages.append({"id": r["id"], "name": r["name"],
                          "ptype": r["ptype"], "joined": joined})
        # Post text search: match ANY token against body (privacy-respecting
        # via post_card — non-viewable posts are dropped).
        pors = " OR ".join(["body LIKE ?"] * len(tokens))
        prow2 = conn.execute(
            f"SELECT * FROM posts WHERE ({pors}) ORDER BY created_at DESC LIMIT 40",
            params).fetchall()
        conn.close()
        owner_on = is_owner(request)
        posts = []
        for r in prow2:
            card = post_card(dict(r), user, owner_on)
            if card:
                posts.append(card)
                if len(posts) >= 20:
                    break
    return render(request, "search.html",
                  {"q": q, "users": users, "pages": pages, "posts": posts})


# ---------------- pages ----------------
def page_ctx(pg, user):
    conn = db()
    members = conn.execute("SELECT COUNT(*) c FROM page_members WHERE page_id=?",
                           (pg["id"],)).fetchone()["c"]
    owner = conn.execute("SELECT name FROM users WHERE id=?", (pg["owner_id"],)).fetchone()
    joined = bool(conn.execute(
        "SELECT 1 FROM page_members WHERE page_id=? AND user_id=?",
        (pg["id"], user["id"])).fetchone())
    conn.close()
    return {
        "id": pg["id"], "name": pg["name"], "ptype": pg["ptype"],
        "description": pg.get("description", ""),
        "cover": avatar_url(pg.get("cover", "")),
        "owner_name": owner["name"] if owner else "Deleted user",
        "owner_id": pg["owner_id"], "members": members,
        "is_owner": user["id"] == pg["owner_id"], "joined": joined,
        "is_verified": bool(pg.get("is_verified")),
        "verification_status": pg.get("verification_status") or "none",
    }


@app.get("/pages", response_class=HTMLResponse)
async def pages_list(request: Request):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    rows = conn.execute("SELECT * FROM pages ORDER BY created_at DESC").fetchall()
    pages = []
    for r in rows:
        pg = dict(r)
        members = conn.execute("SELECT COUNT(*) c FROM page_members WHERE page_id=?",
                               (pg["id"],)).fetchone()["c"]
        owner = conn.execute("SELECT name FROM users WHERE id=?",
                             (pg["owner_id"],)).fetchone()
        joined = bool(conn.execute(
            "SELECT 1 FROM page_members WHERE page_id=? AND user_id=?",
            (pg["id"], user["id"])).fetchone())
        pages.append({
            "id": pg["id"], "name": pg["name"], "ptype": pg["ptype"],
            "description": pg.get("description", ""),
            "cover": avatar_url(pg.get("cover", "")),
            "members": members,
            "owner_name": owner["name"] if owner else "Deleted user",
            "joined": joined,
        })
    conn.close()
    return render(request, "pages.html", {"pages": pages})


@app.get("/pages/new", response_class=HTMLResponse)
async def page_new(request: Request):
    user, err = require_login(request)
    if err:
        return err
    return render(request, "page_form.html", {"ptypes": list(PTYPES)})


@app.post("/pages")
async def page_create(request: Request):
    user, err = require_login(request)
    if err:
        return err
    form = await request.form()
    name = (form.get("name") or "").strip()
    ptype = (form.get("ptype") or "community").strip()
    description = (form.get("description") or "").strip()
    if not name:
        return redir("/pages/new", "Page name is required.")
    if ptype not in PTYPES:
        return redir("/pages/new", "Invalid page type.")
    try:
        cover = save_upload(form.get("cover"), "pages", IMG_EXTS)
    except ValueError as e:
        return redir("/pages/new", str(e))
    conn = db()
    cur = conn.execute(
        "INSERT INTO pages(owner_id, name, ptype, description, cover, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (user["id"], name, ptype, description, cover, now_iso()))
    pid = cur.lastrowid
    conn.execute("INSERT OR IGNORE INTO page_members(page_id, user_id, created_at) "
                 "VALUES(?,?,?)", (pid, user["id"], now_iso()))
    conn.commit()
    conn.close()
    return redir(f"/page/{pid}")


@app.get("/page/{pid}", response_class=HTMLResponse)
async def page_view(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    owner_on = is_owner(request)
    conn = db()
    pg = conn.execute("SELECT * FROM pages WHERE id=?", (pid,)).fetchone()
    if not pg:
        conn.close()
        return redir("/pages", "Page not found.")
    prows = conn.execute(
        "SELECT * FROM posts WHERE page_id=? ORDER BY created_at DESC LIMIT 200",
        (pid,)).fetchall()
    conn.close()
    posts = []
    for r in prows:
        card = post_card(dict(r), user, owner_on)
        if card:
            posts.append(card)
    cfriends, cpages = composer_choices(user)
    # Build PAGE GIFTS: the 🎁 Gift button + sheet ride on every
    # Page except for the Page's owner (a gift to your own page
    # would be a gift to yourself); Creator Mode gates it on the
    # PAGE OWNER's flag — a member's flag never opens a Page. The
    # strip shows the PAGE's recent gifts (credited to the owner,
    # displayed here).
    pg_ctx = page_ctx(dict(pg), user)
    page_gift = None
    gift_locked = None
    _pg_owner = get_user(pg["owner_id"]) or {}
    if (pg["owner_id"] != user["id"]
            and _pg_owner.get("creator_mode")):
        page_gift = _page_gift_ctx(user, f"/page/{pid}/gift",
                                   pg["name"])
    elif pg["owner_id"] != user["id"]:
        gift_locked = {"name": pg["name"], "kind": "page",
                       "id": pid}
    recent_page_gifts = _recent_page_gifts(pg["owner_id"], pid)
    _sc = db()
    owner_selling = _sc.execute(
        "SELECT COUNT(*) c FROM market_listings WHERE seller_id=? "
        "AND status='active'", (pg["owner_id"],)).fetchone()["c"]
    _sc.close()
    return render(request, "page.html",
                  {"page": pg_ctx, "posts": posts,
                   "owner_selling": owner_selling,
                   "page_gift": page_gift,
                   "gift_locked": gift_locked,
                   "recent_page_gifts": recent_page_gifts,
                   "composer_friends": cfriends, "composer_pages": cpages})


@app.post("/page/{pid}/join")
async def page_join(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    pg = conn.execute("SELECT id FROM pages WHERE id=?", (pid,)).fetchone()
    if pg:
        conn.execute("INSERT OR IGNORE INTO page_members(page_id, user_id, created_at) "
                     "VALUES(?,?,?)", (pid, user["id"], now_iso()))
        conn.commit()
    conn.close()
    return redir(f"/page/{pid}")


@app.post("/page/{pid}/leave")
async def page_leave(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    pg = conn.execute("SELECT owner_id FROM pages WHERE id=?", (pid,)).fetchone()
    if pg and pg["owner_id"] != user["id"]:
        conn.execute("DELETE FROM page_members WHERE page_id=? AND user_id=?",
                     (pid, user["id"]))
        conn.commit()
    conn.close()
    return redir(f"/page/{pid}")


@app.post("/page/{pid}/post")
async def page_post(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    pg = conn.execute("SELECT id FROM pages WHERE id=?", (pid,)).fetchone()
    member = conn.execute("SELECT 1 FROM page_members WHERE page_id=? AND user_id=?",
                          (pid, user["id"])).fetchone()
    conn.close()
    if not pg:
        return redir("/pages", "Page not found.")
    if not member:
        return redir(f"/page/{pid}", "Join the page to post.")
    form = await request.form()
    _, ferr = create_post_from_form(user, form, page_id=pid)
    if ferr:
        return redir(f"/page/{pid}", ferr)
    return redir(f"/page/{pid}")


@app.get("/page/{pid}/edit", response_class=HTMLResponse)
async def page_edit_page(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    pg = conn.execute("SELECT * FROM pages WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not pg:
        return redir("/pages", "Page not found.")
    if pg["owner_id"] != user["id"]:
        return redir(f"/page/{pid}", "Only the page owner can edit.")
    return render(request, "page_edit.html", {"page": page_ctx(dict(pg), user)})


@app.post("/page/{pid}/edit")
async def page_edit(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    pg = conn.execute("SELECT * FROM pages WHERE id=?", (pid,)).fetchone()
    if not pg:
        conn.close()
        return redir("/pages", "Page not found.")
    if pg["owner_id"] != user["id"]:
        conn.close()
        return redir(f"/page/{pid}", "Only the page owner can edit.")
    form = await request.form()
    name = (form.get("name") or "").strip() or pg["name"]
    description = (form.get("description") or "").strip()
    try:
        cover = save_upload(form.get("cover"), "pages", IMG_EXTS)
    except ValueError as e:
        conn.close()
        return redir(f"/page/{pid}/edit", str(e))
    if cover:
        conn.execute("UPDATE pages SET name=?, description=?, cover=? WHERE id=?",
                     (name, description, cover, pid))
    else:
        conn.execute("UPDATE pages SET name=?, description=? WHERE id=?",
                     (name, description, pid))
    conn.commit()
    conn.close()
    return redir(f"/page/{pid}")


# ---------------- page ads: "📣 Run as ad" (Build ADS) ----------------
# A page owner promotes their page — itself, one of its posts, one of
# their Market listings, or a fully custom ad — into the same rotation
# the house ads run in. Phase 1: free to run. Only the page owner can
# create or manage a page's ads (pages have no separate admin role).

def _page_ad_guard(user, pid):
    """(page_row, error_response). 403 for anyone but the page owner —
    ad power follows page ownership exactly."""
    conn = db()
    pg = conn.execute("SELECT * FROM pages WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not pg:
        return None, redir("/pages", "Page not found.")
    if pg["owner_id"] != user["id"]:
        return None, HTMLResponse(
            "Only the page owner can run ads for this page.",
            status_code=403)
    return pg, None


@app.get("/page/{pid}/promote", response_class=HTMLResponse)
async def page_promote(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    pg, gerr = _page_ad_guard(user, pid)
    if gerr:
        return gerr
    conn = db()
    posts = conn.execute(
        "SELECT id, body, media FROM posts WHERE page_id=? "
        "ORDER BY created_at DESC LIMIT 20", (pid,)).fetchall()
    listings = conn.execute(
        "SELECT id, title FROM market_listings WHERE seller_id=? "
        "AND status='active' ORDER BY created_at DESC LIMIT 20",
        (user["id"],)).fetchall()
    my_ads = []
    for r in conn.execute(
            "SELECT * FROM ads WHERE page_id=? ORDER BY id DESC",
            (pid,)).fetchall():
        d = dict(r)
        d["ctr"] = ads.ctr(d)
        d["schedule_label"] = _ad_schedule_label(d)
        my_ads.append(d)
    conn.close()
    return render(request, "page_promote.html", {
        "page": page_ctx(dict(pg), user),
        "page_posts": [{"id": p["id"],
                        "label": (p["body"] or "📷 Media post")[:60]}
                       for p in posts],
        "my_listings": [{"id": l["id"], "title": l["title"]}
                        for l in listings],
        "my_ads": my_ads})


@app.post("/page/{pid}/promote")
async def page_promote_create(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    pg, gerr = _page_ad_guard(user, pid)
    if gerr:
        return gerr
    form = await request.form()
    fields = _ad_fields_from_form(form)
    what = (form.get("promote_what") or "page").strip()
    media_path, media_kind = "", "none"
    conn = db()
    if what == "post":
        try:
            post_id = int(form.get("post_id") or 0)
        except (TypeError, ValueError):
            post_id = 0
        post = conn.execute(
            "SELECT * FROM posts WHERE id=? AND page_id=?",
            (post_id, pid)).fetchone()
        if post:
            fields["title"] = fields["title"] or (
                (post["body"] or "").strip()[:70] or pg["name"])
            fields["body"] = fields["body"] or (post["body"] or "")[:300]
            fields["link_url"] = f"/post/{post_id}"
            media_path = post["media"] or ""
            if media_path:
                ext = media_path.rsplit(".", 1)[-1].lower()
                media_kind = ("video" if ext in VID_EXTS else "image")
    elif what == "listing":
        try:
            lid = int(form.get("listing_id") or 0)
        except (TypeError, ValueError):
            lid = 0
        lst = conn.execute(
            "SELECT * FROM market_listings WHERE id=? AND seller_id=?",
            (lid, user["id"])).fetchone()
        if lst:
            fields["title"] = fields["title"] or lst["title"]
            fields["body"] = fields["body"] or (lst["description"] or "")[:300]
            fields["link_url"] = f"/market/{lid}"
            try:
                photos = json.loads(lst["photos"] or "[]")
            except ValueError:
                photos = []
            if photos:
                media_path, media_kind = photos[0], "image"
    elif what == "custom":
        pass  # everything comes from the form itself
    else:  # the page itself
        what = "page"
        fields["title"] = fields["title"] or pg["name"]
        fields["body"] = fields["body"] or (pg["description"] or "")[:300]
        fields["link_url"] = f"/page/{pid}"
        media_path = pg["cover"] or ""
        if media_path:
            media_kind = "image"
    conn.close()
    if not fields["title"]:
        return redir(f"/page/{pid}/promote", "Give the ad a headline.")
    # A fresh upload on the form beats the promoted item's own media.
    media_path, media_kind, merr = _ad_media_from_form(
        form, media_path, media_kind)
    if merr:
        return redir(f"/page/{pid}/promote", merr)
    if not fields["link_url"]:
        fields["link_url"] = f"/page/{pid}"
    _insert_ad(user, "page", pid, fields, media_path, media_kind)
    return redir(f"/page/{pid}/promote",
                "📣 Your ad is live in the rotation — free in phase 1.")


@app.post("/page/{pid}/promote/{aid}/status")
async def page_promote_status(request: Request, pid: int, aid: int):
    user, err = require_login(request)
    if err:
        return err
    pg, gerr = _page_ad_guard(user, pid)
    if gerr:
        return gerr
    form = await request.form()
    status = (form.get("status") or "").strip()
    if status in ("active", "paused", "ended"):
        conn = db()
        conn.execute(
            "UPDATE ads SET status=? WHERE id=? AND page_id=?",
            (status, aid, pid))
        conn.commit()
        conn.close()
    return redir(f"/page/{pid}/promote")


# ---------------- Live Tickets: the live gift economy ----------------
# Viewers buy 🎟️ Live Tickets (packs via Stripe Checkout) and spend
# them on character gifts during lives; the host earns tickets on a
# SECOND ledger (gift_earned) and cashes them out at 0.5¢ each
# (2000-ticket / $10 minimum) through an owner-approved payout.
# HERE's cut is the spread between the ~1¢ buy price and the 0.5¢
# cash-out value — the same shape as TikTok's gift cut, but the
# cash-out path is deliberately hand-approved by the owner (like
# Drop's early manual payouts), never automatic.
#
# TWO ledgers, one audit table (see the init_db comment): spendable
# tickets_balance is debited by 'buy' (+) and 'gift_send'/'shout'
# (−); gift_earned is credited by 'gift_receive' (+) and moved by
# 'cashout_hold' (−)/'cashout_return' (+). 'cashout_paid' rows are
# 0-delta audit markers — the tickets left at hold time.
# Invariant (asserted by tests/test_gifts.py):
#   tickets_balance == SUM(tickets) WHERE kind IN
#       ('buy','gift_send','shout')
#   gift_earned == SUM(tickets) WHERE kind IN
#       ('gift_receive','cashout_hold','cashout_return')

def _gift(key, name, tickets, category, anim):
    return {"key": key, "name": name, "tickets": tickets,
            "category": category, "img": f"/static/gifts/{key}.webp",
            "anim": anim}


# Build RICH: every gift carries its OWN send animation name — the
# live page's animation engine (templates/live.html, @keyframes
# giftfx-*) implements exactly this set. Tiny/free gifts stay light
# (rise/pulse/flyby), mids get spin/bounce/burst, and the big gifts
# (>=100) get the showpieces (zoomin/burst/shower — themed vehicles
# and the jet keep flyby, they DRIVE/FLY across the stage).
GIFT_ANIMS = ("rise", "burst", "flyby", "spin", "shower", "zoomin",
              "bounce", "pulse")


def _gift_anim_name_for(tickets):
    """Fallback animation name by price (legacy/unknown keys and
    any catalog entry missing one): the tier ladder in one name."""
    t = tickets or 0
    if t >= 5000:
        return "shower"
    if t >= 500:
        return "burst"
    if t >= 100:
        return "zoomin"
    if t >= 50:
        return "spin"
    return "rise"


# Build P-GIFTS: the emoji cast is retired. The catalog is Ken's own
# artwork now — 67 gifts cut from his three sheets into
# static/gifts/<key>.webp (💰 Money ×32, ❤️ Love ×24, 👑 Luxury
# ×11; the Luxury sheet contributes only its no-brand-logo designs,
# Ken's call). Prices are server-side ONLY, as ever. neon_heart is
# the one FREE gift (0 tickets): it records a txn but moves no
# ledger and earns the host nothing.
_GIFT_ROWS = [
    # --- 💰 Money (sheet 1) ---
    ("neon_heart", "Neon Heart", 0, "money", "rise"),
    ("money_bag", "Money Bag", 1, "money", "rise"),
    ("flying_cash", "Flying Cash", 5, "money", "flyby"),
    ("gift_stack", "Gift Stack", 10, "money", "bounce"),
    ("here_shades", "HERE Shades", 15, "money", "pulse"),
    ("here_rocket", "HERE Rocket", 25, "money", "flyby"),
    ("money_duck", "Money Duck", 35, "money", "bounce"),
    ("money_bear", "Money Bear", 50, "money", "spin"),
    ("money_buddy", "Money Buddy", 65, "money", "bounce"),
    ("cash_gremlin", "Cash Gremlin", 75, "money", "spin"),
    ("money_heart", "Money Heart", 100, "money", "zoomin"),
    ("money_tree", "Money Tree", 125, "money", "zoomin"),
    ("golden_mic", "Golden Mic", 150, "money", "pulse"),
    ("megaphone", "HERE Megaphone", 200, "money", "burst"),
    ("boss_face", "Boss Face", 250, "money", "zoomin"),
    ("heart_hustler", "Heart Hustler", 300, "money", "spin"),
    ("gold_kicks", "Gold Kicks", 400, "money", "spin"),
    ("money_panda", "Money Panda", 500, "money", "shower"),
    ("angel_bag", "Angel Bag", 600, "money", "zoomin"),
    ("champagne_bucket", "Champagne Bucket", 750, "money", "burst"),
    ("dab_king", "Dab King", 900, "money", "burst"),
    ("here_diamond", "HERE Diamond", 1000, "money", "zoomin"),
    ("diamond_dollar", "Diamond Dollar", 1200, "money", "burst"),
    ("here_crown", "HERE Crown", 1500, "money", "zoomin"),
    ("burning_cash", "Burning Cash", 1800, "money", "burst"),
    ("the_vault", "The Vault", 2000, "money", "zoomin"),
    ("crown_jewel_bag", "Crown Jewel Bag", 2500, "money", "shower"),
    ("here_juice", "HERE Juice", 3000, "money", "pulse"),
    ("g_wagon", "G-Wagon", 4000, "money", "flyby"),
    ("midnight_rider", "Midnight Rider", 5000, "money", "flyby"),
    ("the_cutlass", "The Cutlass", 7500, "money", "flyby"),
    ("royal_fortune", "Royal Fortune", 10000, "money", "shower"),
    # --- ❤️ Love (sheet 3) ---
    ("single_rose", "Single Rose", 1, "love", "rise"),
    ("candle_glow", "Candle Glow", 5, "love", "pulse"),
    ("heart_balloons", "Heart Balloons", 10, "love", "rise"),
    ("love_letter", "Love Letter", 15, "love", "flyby"),
    ("hug_heart", "Hug Heart", 20, "love", "pulse"),
    ("i_love_you", "I Love You", 25, "love", "pulse"),
    ("infinity_hearts", "Infinity Hearts", 35, "love", "spin"),
    ("gift_boxes", "Gift Boxes", 50, "love", "bounce"),
    ("hoodie_teddy", "Hoodie Teddy", 65, "love", "bounce"),
    ("white_teddy", "White Teddy", 75, "love", "spin"),
    ("mystery_gift", "Mystery Gift", 90, "love", "burst"),
    ("rose_dome", "Rose Dome", 100, "love", "zoomin"),
    ("panda_love", "Panda Love", 125, "love", "zoomin"),
    ("crowned_bears", "Crowned Bears", 150, "love", "zoomin"),
    ("rose_box", "Rose Box", 180, "love", "burst"),
    ("forever_roses_bucket", "Forever Roses", 200, "love", "shower"),
    ("heart_bouquet_box", "Heart Bouquet Box", 250, "love", "zoomin"),
    ("love_script", "Love Script", 300, "love", "flyby"),
    ("dripping_heart", "Dripping Crown Heart", 400, "love", "zoomin"),
    ("couple_goals", "Couple Goals", 500, "love", "zoomin"),
    ("crowned_heart", "Crowned Heart", 750, "love", "pulse"),
    ("rose_heart_wreath", "Rose Heart Wreath", 1000, "love", "spin"),
    ("teddy_king", "Teddy King", 1250, "love", "zoomin"),
    ("heart_of_roses", "Heart of Roses", 1500, "love", "shower"),
    # --- 👑 Luxury (sheet 2, no-logo keepers only) ---
    ("forever_roses", "Forever Roses Box", 1000, "luxury", "shower"),
    ("cigar_box", "Cigar Box", 1500, "luxury", "zoomin"),
    ("diamond_rings", "Diamond Rings", 2500, "luxury", "burst"),
    ("gift_card", "Gift Card", 3000, "luxury", "flyby"),
    ("ceo_chain", "CEO Chain", 3500, "luxury", "spin"),
    ("grillz_case", "Grillz Case", 4000, "luxury", "burst"),
    ("bulldog_boss", "Bulldog Boss", 5000, "luxury", "burst"),
    ("luxury_lounge", "Luxury Lounge", 6000, "luxury", "zoomin"),
    ("private_jet", "Private Jet", 12000, "luxury", "flyby"),
    ("yacht_night", "Yacht Night", 15000, "luxury", "zoomin"),
    ("mansion_sunset", "Mansion Sunset", 20000, "luxury", "shower"),
]
GIFT_CATALOG = {k: _gift(k, n, t, c, a) for k, n, t, c, a in _GIFT_ROWS}
GIFT_ORDER = [k for k, _n, _t, _c, _a in _GIFT_ROWS]
GIFT_CATEGORIES = [("money", "💰 Money"), ("love", "❤️ Love"),
                   ("luxury", "👑 Luxury")]

# Retired emoji gifts (pre-P-GIFTS catalog): no longer sendable —
# POSTing one returns the same friendly "Unknown gift." error as
# any bad key — but historical ticket_txns rows still render with
# their old face via _gift_public's fallback. Never crash on them.
LEGACY_GIFT_LABELS = {
    "ant": ("🐜", "Lil' Red Ant"), "bolt": ("⚡", "Bolt Buddy"),
    "moon": ("🌙", "Moonie"), "rocket": ("🚀", "Red Rocket"),
    "diamond": ("💎", "Diamond Hands"),
    "crown": ("👑", "King's Crown"), "cutlass": ("🚗", "The Cutlass"),
    "globe": ("🌍", "Planet HERE"),
}
TICKET_PACKS = {
    "pack100": {"key": "pack100", "tickets": 100, "price_cents": 99},
    "pack500": {"key": "pack500", "tickets": 500, "price_cents": 499},
    "pack1500": {"key": "pack1500", "tickets": 1500, "price_cents": 1399},
}
# Custom-amount buys: pack_key "custom" + a qty. The price is
# computed SERVER-SIDE only — 1 ticket = 1 cent, so price_cents ==
# qty. The 50 floor is Stripe's $0.50 minimum charge; the cap
# keeps a typo from becoming a second mortgage.
CUSTOM_PACK_KEY = "custom"
CUSTOM_MIN_QTY = 50
CUSTOM_MAX_QTY = 1_000_000
CUSTOM_CENTS_PER_TICKET = 1


def _buy_tickets_for(meta):
    """Tickets a paid checkout session credits, derived ONLY from
    the server-set session metadata. Packs resolve through
    TICKET_PACKS (the canonical count for the pack key — metadata
    alone can't inflate a pack); a custom buy carries its qty in
    metadata but is re-validated against the custom bounds before
    anyone trusts it. Returns 0 for anything unrecognized, which
    both crediting paths treat as 'not a buy session'."""
    meta = meta or {}
    pk = meta.get("pack_key")
    if pk in TICKET_PACKS:
        return TICKET_PACKS[pk]["tickets"]
    if pk == CUSTOM_PACK_KEY:
        try:
            qty = int(meta.get("tickets") or 0)
        except (TypeError, ValueError):
            return 0
        if CUSTOM_MIN_QTY <= qty <= CUSTOM_MAX_QTY:
            return qty
    return 0


SHOUT_COST_TICKETS = 20
SHOUT_SECONDS = 60
CASHOUT_MIN_TICKETS = 2000  # = $10.00 at 0.5¢ per earned ticket


def _gift_anim_tier(tickets):
    """Send-animation tier from price: 1 = float up the stage
    (<50), 2 = pop + glow (50–499), 3 = center-stage banner
    (500–4,999), 4 = full-stage takeover (~5,000+)."""
    t = tickets or 0
    if t >= 5000:
        return 4
    if t >= 500:
        return 3
    if t >= 50:
        return 2
    return 1


def _gift_public(gift_key, tickets=None):
    """Catalog entry for state payloads / gift displays. Current
    art gifts carry key/name/tickets/category/img/anim (emoji is
    empty — the art replaces it); retired legacy keys fall back to
    their old emoji label; unknown keys render as a plain gift —
    never crash a poll on bad data."""
    g = GIFT_CATALOG.get(gift_key)
    if g:
        t = tickets if tickets is not None else g["tickets"]
        return {"key": gift_key, "emoji": "", "name": g["name"],
                "tickets": t, "category": g["category"],
                "img": g["img"], "anim": _gift_anim_tier(t),
                "anim_name": g["anim"]}
    emoji, name = LEGACY_GIFT_LABELS.get(gift_key, ("🎁", "Gift"))
    t = tickets if tickets is not None else 0
    return {"key": gift_key, "emoji": emoji, "name": name,
            "tickets": t, "category": "", "img": "",
            "anim": _gift_anim_tier(t),
            "anim_name": _gift_anim_name_for(t)}


# ---------------- gifter ranks + streamer levels (Build P-GIFTS) ----
# GIFTER RANK rides on lifetime tickets SENT (the gift_send side of
# the ledger, whose rows store negative deltas). STREAMER LEVEL
# rides on users.gift_lifetime (lifetime earned; see the init_db
# note). Both are pure functions of a total — thresholds live here
# and nowhere else.
GIFTER_RANKS = [
    (0, "", "Newcomer"), (100, "💧", "Supporter"),
    (500, "🔥", "Backer"), (2000, "💎", "Patron"),
    (10000, "🐋", "Whale"), (50000, "👑", "Legend"),
]


def _gifter_rank(lifetime_sent):
    rank = 0
    for i, (floor, _b, _n) in enumerate(GIFTER_RANKS):
        if (lifetime_sent or 0) >= floor:
            rank = i
    _floor, badge, name = GIFTER_RANKS[rank]
    return {"rank": rank, "badge": badge, "name": name,
            "label": (badge + " " + name).strip(),
            "lifetime_sent": lifetime_sent or 0}


def _gift_sent_total(conn, uid):
    r = conn.execute(
        "SELECT COALESCE(SUM(-tickets), 0) s FROM ticket_txns "
        "WHERE user_id=? AND kind='gift_send'", (uid,)).fetchone()
    return (r["s"] or 0) if r else 0


STREAMER_LEVELS = [
    (0, "🌱", "New Voice"), (1000, "⭐", "Rising"),
    (10000, "🔥", "Star"), (50000, "💎", "Pro"),
    (200000, "👑", "VIP Live"),
]


def _streamer_level(lifetime_earned):
    lvl = 1
    for i, (floor, _e, _n) in enumerate(STREAMER_LEVELS):
        if (lifetime_earned or 0) >= floor:
            lvl = i + 1
    _floor, emoji, name = STREAMER_LEVELS[lvl - 1]
    return {"level": lvl, "emoji": emoji, "name": name,
            "label": f"{emoji} {name}",
            "lifetime": lifetime_earned or 0}


def _earned_dollars(tickets):
    """Earned tickets -> whole cents at 0.5¢ each (floor: a stray
    half-cent stays with the house)."""
    return int(tickets or 0) // 2


# ---------------- HERE Money (Build T): owner profit panel ---------
# A 'buy' ledger row stores TICKETS, not dollars (credit_purchase
# records the ticket delta + the Stripe session id; the charged
# amount itself lives only in Stripe). Gross is reconstructed
# from the SAME price sources the charge used: TICKET_PACKS for
# pack buys, the custom rate (CUSTOM_CENTS_PER_TICKET per ticket)
# for every other count. A custom buy of exactly a pack size is
# indistinguishable in the ledger and prices as the pack — the
# widest possible gap is $1.01 (a 1500-ticket custom buy) — the
# accepted tradeoff, documented in ROUTE_CONTRACT.md. Stripe's
# cut is an ESTIMATE (Stripe reports no per-charge fee to this
# app): the standard card rate, 2.9% + 30¢ per purchase, rounded
# per purchase the way Stripe rounds. All figures are read-only
# SQL aggregates — see _buy_aggregates / owner_dashboard.


def _money2(cents):
    """Ledger money format: always $X,XXX.XX, honest negatives.
    (_money() is listing-display sugar — it drops .00 and calls
    0 'FREE'; neither is true on a ledger.)"""
    cents = int(cents or 0)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def _buy_amount_sql():
    """SQL expression: cents charged for one 'buy' row, from its
    ticket count (see the Build T note above). Generated from
    TICKET_PACKS so the pack prices live in exactly one place."""
    whens = " ".join(
        f"WHEN {p['tickets']} THEN {p['price_cents']}"
        for p in TICKET_PACKS.values())
    return (f"CASE tickets {whens} "
            f"ELSE tickets * {CUSTOM_CENTS_PER_TICKET} END")


def _buy_aggregates(conn, start=None, end=None):
    """(count, gross_cents, stripe_fee_est_cents) over 'buy' rows,
    optionally inside a [start, end) created_at window. One
    aggregate SQL pass — no per-row Python at any ledger size."""
    amt = _buy_amount_sql()
    sql = ("SELECT COUNT(*) c, COALESCE(SUM(" + amt + "),0) gross, "
           "COALESCE(SUM(CAST(ROUND((" + amt + ") * 0.029 + 30) "
           "AS INTEGER)),0) fee FROM ticket_txns WHERE kind='buy'")
    args = []
    if start is not None:
        sql += " AND created_at>=?"
        args.append(start)
    if end is not None:
        sql += " AND created_at<?"
        args.append(end)
    r = conn.execute(sql, args).fetchone()
    return r["c"], r["gross"], r["fee"]


def _chicago_day_start_utc(day):
    """UTC ISO instant the Chicago calendar day `day` begins —
    the range bound for created_at filters. zoneinfo is exact
    (CDT/CST); the fallback applies _chicago_now's DST rule as a
    fixed offset for that date (Chicago transitions happen at
    2am, never at midnight, so a per-day offset is exact too)."""
    if CHICAGO_TZ is not None:
        local = datetime(day.year, day.month, day.day,
                         tzinfo=CHICAGO_TZ)
    else:
        dst_start = _nth_weekday(day.year, 3, 6, 2)
        dst_end = _nth_weekday(day.year, 11, 6, 1)
        off = timedelta(hours=-5 if dst_start <= day < dst_end
                        else -6)
        local = datetime(day.year, day.month, day.day,
                         tzinfo=timezone(off))
    return local.astimezone(timezone.utc).isoformat()


def stripe_enabled():
    return bool(os.environ.get("STRIPE_SECRET_KEY", "").strip())


def _stripe_api(method, path, params=None):
    """ONE raw-HTTPS call to api.stripe.com (form-encoded, no
    dependency). Tests monkeypatch this instead of touching Stripe.
    Raises on HTTP errors so callers can degrade honestly."""
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    body = None
    url = "https://api.stripe.com" + path
    if method == "GET" and params:
        url += "?" + urlencode(params)
    elif params:
        body = urlencode(params).encode()
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", "Bearer " + key)
    if body:
        req.add_header("Content-Type",
                       "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode())
        except Exception:
            detail = {}
        raise RuntimeError(
            detail.get("error", {}).get("message") or
            f"Stripe error {e.code}")


def credit_purchase(user_id, tickets, session_id):
    """Idempotent purchase crediting — the ONLY buy path. Inserts
    the buy txn first (partial unique index on stripe_session), and
    on conflict reports 'already credited' without double-adding.
    Called from the webhook AND the return-verify path."""
    if not session_id:
        return False
    conn = db()
    try:
        conn.execute(
            "INSERT INTO ticket_txns(user_id, kind, tickets, "
            "stripe_session, created_at) VALUES(?,?,?,?,?)",
            (user_id, "buy", tickets, session_id, now_iso()))
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        return False
    conn.execute(
        "UPDATE users SET tickets_balance = "
        "COALESCE(tickets_balance,0) + ? WHERE id=?",
        (tickets, user_id))
    conn.commit()
    conn.close()
    return True


# ---------------------------------------------------------------------------
# Build LOCKED CONTENT — purchases. Creators (Creator Mode ON) sell
# locked posts and locked DM attachments for tickets; the money lands
# in the seller's gift_earned wallet (the same cash-out as gifts).
# The ledger marker is note='unlock' with kinds 'unlock_spend'
# (buyer) / 'unlock_receive' (seller) and gift_key 'post:{id}' /
# 'dm:{id}' — deliberately NOT gift_receive, so the live-gift
# "Earlier streams" bucket and the note='page' Page-gifts bucket
# stay bit-for-bit what they were. Unlocks are forever.
# ---------------------------------------------------------------------------

def _unlock_core(user, ctype, cid, confirm_adult):
    """The one guarded unlock transaction, shared by posts and DMs.
    Returns (ok, message). Check order is deliberate: existence +
    visibility FIRST (post privacy runs before a lock can even be
    attempted), then own-content, then the existing-unlock
    short-circuit (never a second charge), then the 18+ confirm,
    then the money: guarded debit (rowcount checked) → seller
    gift_earned AND gift_lifetime credit → content_unlocks row
    (UNIQUE(content_type, content_id, buyer_id) is the hard
    double-charge backstop) → both ticket_txns rows sharing ONE
    timestamp so _txn_line can pair them back up."""
    conn = db()
    if ctype == "post":
        row = conn.execute("SELECT * FROM posts WHERE id=?",
                           (cid,)).fetchone()
        if not row:
            conn.close()
            return False, "That content isn't available."
        row = dict(row)
        if not can_view_post(user, row, False):
            conn.close()
            return False, "That content isn't available."
        seller_id = row["user_id"]
        price = row.get("lock_price") or 0
        adult = bool(row.get("lock_adult"))
        noun = "post"
    else:
        row = conn.execute("SELECT * FROM messages WHERE id=?",
                           (cid,)).fetchone()
        if not row:
            conn.close()
            return False, "That content isn't available."
        row = dict(row)
        # Only the message's RECIPIENT can unlock it. The sender's
        # own content is always open to them; a third party gets the
        # same plain refusal as a missing message.
        if row["receiver_id"] != user["id"]:
            conn.close()
            if row["sender_id"] == user["id"]:
                return False, "Your own content is always open to you."
            return False, "That content isn't available."
        seller_id = row["sender_id"]
        price = row.get("lock_price") or 0
        adult = bool(row.get("lock_adult"))
        noun = "message"
    if not price:
        conn.close()
        return False, "That content isn't locked."
    if seller_id == user["id"]:
        conn.close()
        return False, "Your own content is always open to you."
    if _has_unlock(conn, user["id"], ctype, cid):
        conn.close()
        return True, "You've already unlocked this."
    if adult and not confirm_adult:
        conn.close()
        return False, "Confirm you're 18+ to unlock this."
    stamp = now_iso()
    cur = conn.execute(
        "UPDATE users SET tickets_balance = "
        "COALESCE(tickets_balance,0) - ? WHERE id=? "
        "AND COALESCE(tickets_balance,0) >= ?",
        (price, user["id"], price))
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        return False, "Not enough tickets — grab a pack first 🎟️"
    conn.execute(
        "UPDATE users SET gift_earned = COALESCE(gift_earned,0) + ?, "
        "gift_lifetime = COALESCE(gift_lifetime,0) + ? WHERE id=?",
        (price, price, seller_id))
    try:
        conn.execute(
            "INSERT INTO content_unlocks(content_type, content_id, "
            "buyer_id, seller_id, price, created_at) "
            "VALUES(?,?,?,?,?,?)",
            (ctype, cid, user["id"], seller_id, price, stamp))
    except sqlite3.IntegrityError:
        # A twin tap won the race: the unlock row already exists, so
        # this buyer already owns it — roll the money back whole.
        conn.rollback()
        conn.close()
        return True, "You've already unlocked this."
    gkey = f"{ctype}:{cid}"
    conn.execute(
        "INSERT INTO ticket_txns(user_id, kind, tickets, live_id, "
        "gift_key, stripe_session, note, created_at) "
        "VALUES(?, 'unlock_spend', ?, NULL, ?, NULL, 'unlock', ?)",
        (user["id"], -price, gkey, stamp))
    conn.execute(
        "INSERT INTO ticket_txns(user_id, kind, tickets, live_id, "
        "gift_key, stripe_session, note, created_at) "
        "VALUES(?, 'unlock_receive', ?, NULL, ?, NULL, 'unlock', ?)",
        (seller_id, price, gkey, stamp))
    _invite_bonus(conn, seller_id, price, stamp)
    conn.commit()
    conn.close()
    notify(seller_id, "content_unlock",
           f"{user['name']} unlocked your {noun} (+{price} 🎟️)",
           f"/post/{cid}" if ctype == "post" else f"/messages/{user['id']}",
           user["id"])
    return True, "Unlocked — it's yours forever 🔓"


async def _unlock_confirm(request):
    """The buyer's explicit 18+ confirmation, from a JSON body or a
    classic form field (confirm_adult=1)."""
    if "application/json" in (request.headers.get("content-type")
                               or ""):
        try:
            data = await request.json()
        except Exception:
            data = {}
        return data.get("confirm_adult") in (1, True, "1", "true", "on")
    form = await request.form()
    return (form.get("confirm_adult") or "") in ("1", "on", "true")


def _unlock_reply(request, ok, msg, fallback):
    """JSON for API callers, redirect-with-flash for the classic
    lock-card forms (the gift routes' dual-shape precedent)."""
    if "application/json" in (request.headers.get("content-type")
                              or ""):
        return JSONResponse(
            {"ok": ok, "error": "" if ok else msg, "message": msg},
            status_code=200 if ok else 400)
    # Same same-origin rule as back(): a relative Referer wins, the
    # fallback otherwise; the message rides as the flash.
    ref = request.headers.get("referer", "")
    return redir(ref if ref.startswith("/") else fallback, msg)


@app.post("/post/{pid}/unlock")
async def unlock_post(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    confirm = await _unlock_confirm(request)
    ok, msg = _unlock_core(user, "post", pid, confirm)
    return _unlock_reply(request, ok, msg, f"/post/{pid}")


@app.post("/messages/{mid}/unlock")
async def unlock_message(request: Request, mid: int):
    user, err = require_login(request)
    if err:
        return err
    confirm = await _unlock_confirm(request)
    ok, msg = _unlock_core(user, "dm", mid, confirm)
    conn = db()
    m = conn.execute("SELECT sender_id FROM messages WHERE id=?",
                     (mid,)).fetchone()
    conn.close()
    fallback = f"/messages/{m['sender_id']}" if m else "/messages"
    return _unlock_reply(request, ok, msg, fallback)


@app.get("/locked/post/{pid}")
async def locked_post_media(request: Request, pid: int):
    """The ONLY route that serves a locked post's media. Login
    required; post privacy runs first (can_view_post); then the
    viewer must be the author, the owner context, or a buyer.
    Everyone else: 404 (can't view / doesn't exist) or 403 (can see
    the post exists, hasn't paid)."""
    user = current_user(request)
    if not user:
        return HTMLResponse("Not found", status_code=404)
    owner_on = is_owner(request)
    conn = db()
    row = conn.execute("SELECT * FROM posts WHERE id=?",
                       (pid,)).fetchone()
    if not row:
        conn.close()
        return HTMLResponse("Not found", status_code=404)
    p = dict(row)
    media = p.get("media") or ""
    if not (p.get("lock_price") and media.startswith("locked/")):
        conn.close()
        return HTMLResponse("Not found", status_code=404)
    if not can_view_post(user, p, owner_on):
        conn.close()
        return HTMLResponse("Not found", status_code=404)
    entitled = bool(owner_on or user["id"] == p["user_id"]
                    or _has_unlock(conn, user["id"], "post", pid))
    conn.close()
    if not entitled:
        return HTMLResponse("Locked content", status_code=403)
    return _serve_locked_media(media)


@app.get("/locked/dm/{mid}")
async def locked_dm_media(request: Request, mid: int):
    """The ONLY route that serves a locked DM attachment. Only the
    two conversation participants can ever reach it: the sender
    always, the recipient after unlocking. Non-participants get
    404 (the message may as well not exist); an un-unlocked
    recipient gets 403."""
    user = current_user(request)
    if not user:
        return HTMLResponse("Not found", status_code=404)
    conn = db()
    row = conn.execute("SELECT * FROM messages WHERE id=?",
                       (mid,)).fetchone()
    if not row or not row["lock_media"]:
        conn.close()
        return HTMLResponse("Not found", status_code=404)
    m = dict(row)
    if user["id"] not in (m["sender_id"], m["receiver_id"]):
        conn.close()
        return HTMLResponse("Not found", status_code=404)
    entitled = bool(user["id"] == m["sender_id"]
                    or _has_unlock(conn, user["id"], "dm", mid))
    conn.close()
    if not entitled:
        return HTMLResponse("Locked content", status_code=403)
    return _serve_locked_media(m["lock_media"])


def _txn_line(t):
    """One ticket history row -> plain-English text."""
    k = t["kind"]
    if k == "buy":
        return f"Bought {t['tickets']} 🎟️"
    if k == "gift_send":
        g = _gift_public(t["gift_key"])
        face = (g["emoji"] + " ") if g["emoji"] else ""
        return f"Sent {face}{g['name']}"
    if k == "gift_receive":
        note = t["note"] or ""
        if note == "shout":
            return f"Received a shout-out pin (+{t['tickets']} 🎟️)"
        g = _gift_public(t["gift_key"])
        if note == "page":
            face = (g["emoji"] + " ") if g["emoji"] else ""
            return (f"Received {face}{g['name']} on your page "
                    f"(+{t['tickets']} 🎟️)")
        if note == "track":
            face = (g["emoji"] + " ") if g["emoji"] else ""
            return (f"Received {face}{g['name']} on your track "
                    f"(+{t['tickets']} 🎟️)")
        face = (g["emoji"] + " ") if g["emoji"] else ""
        return f"Received {face}{g['name']} (+{t['tickets']} 🎟️)"
    if k == "invite_bonus":
        return (f"Invite bonus (+{t['tickets']} 🎟️) — "
                f"someone you invited is earning")
    if k in ("unlock_spend", "unlock_receive"):
        # Build LOCKED CONTENT lines. gift_key is 'post:{id}' /
        # 'dm:{id}'; the spend side names the seller (the content's
        # author/sender — a fixed fact), the receive side names the
        # buyer via the content_unlocks row written in the SAME
        # transaction (matched on the shared created_at stamp).
        ctype, _, cid = (t["gift_key"] or "").partition(":")
        noun = "post" if ctype == "post" else "message"
        n = abs(t["tickets"])
        conn = db()
        if k == "unlock_spend":
            other = None
            if ctype == "post":
                r = conn.execute(
                    "SELECT user_id FROM posts WHERE id=?",
                    (cid,)).fetchone()
                other = r["user_id"] if r else None
            elif ctype == "dm":
                r = conn.execute(
                    "SELECT sender_id FROM messages WHERE id=?",
                    (cid,)).fetchone()
                other = r["sender_id"] if r else None
            nm = ""
            if other:
                u = conn.execute("SELECT name FROM users WHERE id=?",
                                 (other,)).fetchone()
                nm = u["name"] if u else ""
            conn.close()
            if nm:
                return f"Unlocked {nm}'s {noun} (−{n} 🎟️)"
            return f"Unlocked locked content (−{n} 🎟️)"
        r = conn.execute(
            "SELECT buyer_id FROM content_unlocks WHERE "
            "content_type=? AND content_id=? AND created_at=?",
            (ctype, cid, t["created_at"])).fetchone()
        nm = ""
        if r:
            u = conn.execute("SELECT name FROM users WHERE id=?",
                             (r["buyer_id"],)).fetchone()
            nm = u["name"] if u else ""
        conn.close()
        if nm:
            return f"{nm} unlocked your {noun} (+{n} 🎟️)"
        return f"Received unlock payment (+{n} 🎟️)"
    if k == "shout":
        return f"Pinned a shout-out (−{abs(t['tickets'])} 🎟️)"
    if k == "cashout_hold":
        return (f"Cash-out requested — {abs(t['tickets'])} 🎟️ held "
                "(sends automatically)")
    if k == "cashout_paid":
        return f"Cash-out paid 💰 {t['note'] or ''}".strip()
    if k == "cashout_return":
        return (f"Cash-out declined — {t['tickets']} 🎟️ returned "
                "to your earnings")
    return f"{k} {t['tickets']} 🎟️"


@app.get("/tickets", response_class=HTMLResponse)
async def tickets_page(request: Request):
    """Wallet page: spendable balance, earnings + $ value, pack
    cards, the cash-out card, and the last 30 ledger lines. When a
    Stripe checkout returns with ?session_id=, this page ALSO
    verifies the session against Stripe and credits the buyer —
    the path that works even before the webhook is registered."""
    user, err = require_login(request)
    if err:
        return err
    session_id = (request.query_params.get("session_id") or "").strip()
    bought_msg = ""
    if session_id and stripe_enabled():
        try:
            sess = _stripe_api(
                "GET", f"/v1/checkout/sessions/{session_id}")
            meta = sess.get("metadata") or {}
            n_tickets = _buy_tickets_for(meta)
            if (sess.get("payment_status") == "paid"
                    and str(meta.get("user_id")) == str(user["id"])
                    and n_tickets):
                if credit_purchase(user["id"], n_tickets,
                                   session_id):
                    bought_msg = (f"🎟️ {n_tickets} tickets "
                                  "added to your wallet!")
        except Exception:
            # Never let a verification failure break the wallet page;
            # the webhook path still credits the purchase when it
            # lands (and the buyer can refresh this page to retry).
            pass
    me = get_user(user["id"]) or {}
    # Build R: returning from Stripe onboarding (?setup=done) — or
    # holding an unfinished setup — refresh the cached payout-ready
    # flag from Stripe itself (at most one API call, only in those
    # cases; a Stripe failure keeps the cached value).
    setup_q = (request.query_params.get("setup") or "").strip()
    if (me.get("stripe_acct_id") and stripe_enabled()
            and (not me.get("payout_ready") or setup_q == "done")):
        me["payout_ready"] = _refresh_payout_ready(me)
    # Auto-pay: a bank-ready user landing here with a cash-out
    # queued fires the sweep before the page reads its state.
    if me.get("payout_ready"):
        _payout_sweep()
    balance = me.get("tickets_balance") or 0
    earned = me.get("gift_earned") or 0
    conn = db()
    pending = conn.execute(
        "SELECT * FROM payout_requests WHERE user_id=? AND "
        "status IN ('pending','sending') ORDER BY id DESC LIMIT 1",
        (user["id"],)).fetchone()
    my_payouts = conn.execute(
        "SELECT * FROM payout_requests WHERE user_id=? ORDER BY id "
        "DESC LIMIT 10", (user["id"],)).fetchall()
    txns = conn.execute(
        "SELECT * FROM ticket_txns WHERE user_id=? ORDER BY id DESC "
        "LIMIT 30", (user["id"],)).fetchall()
    conn.close()
    payouts_history = [{
        "id": p["id"], "paid": p["status"] == "paid",
        "tickets": p["tickets"],
        "dollars": _money(p["dollars_cents"]),
        "when": fancy_time(p["created_at"]),
        "status_text": _payout_status_text(dict(p)),
        "decided": (fancy_time(p["decided_at"])
                    if p["decided_at"] else "")}
        for p in my_payouts]
    bank_state = ("ready" if me.get("payout_ready")
                  else "started" if me.get("stripe_acct_id")
                  else "none")
    history = [{"text": _txn_line(dict(t)),
                "when": fancy_time(t["created_at"])} for t in txns]
    _tc = db()
    quest = _creator_quest(_tc, user)
    _tc.close()
    return render(request, "tickets.html", {
        "balance": balance, "earned": earned,
        "earned_dollars": _money(_earned_dollars(earned)),
        "earned_worth": _money2(_earned_dollars(earned)),
        "cashout_min": CASHOUT_MIN_TICKETS,
        "cashout_min_dollars": _money(
            _earned_dollars(CASHOUT_MIN_TICKETS)),
        "can_cashout": earned >= CASHOUT_MIN_TICKETS and not pending,
        "pending": ({"tickets": pending["tickets"],
                     "dollars": _money(pending["dollars_cents"]),
                     "when": fancy_time(pending["created_at"]),
                     "hand_note": pending["hand_note"] or ""}
                    if pending else None),
        "packs": [TICKET_PACKS[k] for k in
                  ("pack100", "pack500", "pack1500")],
        "pack_prices": {k: _money(v["price_cents"])
                        for k, v in TICKET_PACKS.items()},
        "custom_min": CUSTOM_MIN_QTY,
        "custom_max": CUSTOM_MAX_QTY,
        "stripe_on": stripe_enabled(),
        "history": history,
        "bought_msg": bought_msg,
        "bank_state": bank_state,
        "setup_q": setup_q,
        "payouts_history": payouts_history,
        "quest": quest})


@app.post("/tickets/buy")
async def tickets_buy(request: Request):
    """Create a Stripe Checkout session for one pack — or for a
    custom ticket amount (pack_key 'custom' + qty). Amounts are
    computed HERE, server-side, always: packs come from
    TICKET_PACKS; a custom qty is priced at 1 ticket = 1 cent and
    bounded (50..1,000,000) before a session exists. The client
    never sends a price. Disabled (no STRIPE_SECRET_KEY) = honest
    503, nothing half-sold."""
    user, err = require_login(request)
    if err:
        return err
    if not stripe_enabled():
        return JSONResponse(
            {"ok": False, "error": "Ticket sales open soon 🎟️"},
            status_code=503)
    try:
        form = await request.form()
    except Exception:
        form = {}
    pack_key = (form.get("pack_key") or "").strip()
    if pack_key == CUSTOM_PACK_KEY:
        raw_qty = (form.get("qty") or "").strip()
        try:
            qty = int(raw_qty)
        except (TypeError, ValueError):
            qty = 0
        if not (CUSTOM_MIN_QTY <= qty <= CUSTOM_MAX_QTY):
            return JSONResponse(
                {"ok": False, "error":
                 f"Custom amounts run from {CUSTOM_MIN_QTY} to "
                 f"{CUSTOM_MAX_QTY:,} tickets."},
                status_code=400)
        tickets_n = qty
        price_cents = qty * CUSTOM_CENTS_PER_TICKET
        product_name = f"HERE Tickets ×{qty}"
    else:
        pack = TICKET_PACKS.get(pack_key)
        if not pack:
            return JSONResponse({"ok": False, "error": "Unknown pack."},
                                status_code=400)
        tickets_n = pack["tickets"]
        price_cents = pack["price_cents"]
        product_name = f"HERE Live Tickets — {pack['tickets']} 🎟️"
    base = str(request.base_url).rstrip("/")
    params = {
        "mode": "payment",
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]":
            str(price_cents),
        "line_items[0][price_data][product_data][name]":
            product_name,
        "line_items[0][quantity]": "1",
        "metadata[user_id]": str(user["id"]),
        "metadata[pack_key]": pack_key,
        "metadata[tickets]": str(tickets_n),
        "success_url": base + "/tickets?session_id="
                        "{CHECKOUT_SESSION_ID}&bought=1",
        "cancel_url": base + "/tickets",
    }
    try:
        sess = _stripe_api("POST", "/v1/checkout/sessions", params)
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)},
                            status_code=502)
    url = sess.get("url")
    if not url:
        return JSONResponse(
            {"ok": False, "error": "Stripe didn't return a checkout "
                                   "link."},
            status_code=502)
    return RedirectResponse(url, status_code=303)


@app.post("/tickets/cashout")
async def tickets_cashout(request: Request):
    """Cash out ALL earned tickets (min 2000). Tickets move to a
    hold immediately (gift_earned -= n, cashout_hold txn) and the
    money SENDS ITSELF — a bank-ready streamer's transfer fires
    before this handler returns (see _payout_attempt); the owner
    desk is oversight/backstop only. Declining a still-pending
    request returns the tickets (gift_earned += n,
    cashout_return)."""
    user, err = require_login(request)
    if err:
        return err
    # Stripe ONLY (Ken's ruling, Oct 30): a cash-out pays
    # the bank the streamer linked through Stripe — there is
    # no destination to type and no other rail. A legacy note
    # field is still accepted and stored, never required.
    try:
        form = await request.form()
        note = (form.get("payout_note") or "").strip()[:300]
    except Exception:
        note = ""
    conn = db()
    me = conn.execute(
        "SELECT gift_earned FROM users WHERE id=?",
        (user["id"],)).fetchone()
    earned = (me["gift_earned"] or 0) if me else 0
    if earned < CASHOUT_MIN_TICKETS:
        conn.close()
        return redir("/tickets",
                     f"You need {CASHOUT_MIN_TICKETS} earned tickets "
                     "to cash out ($10 minimum).")
    if conn.execute(
            "SELECT 1 FROM payout_requests WHERE user_id=? AND "
            "status IN ('pending','sending')",
            (user["id"],)).fetchone():
        conn.close()
        return redir("/tickets",
                     "You already have a cash-out on its way.")
    n = earned
    fee_tickets = 0
    if (get_user(user["id"]) or {}).get(
            "artist_contract") == "signed":
        fee_tickets = (n * LABEL_FEE_PCT) // 100
    pay_tickets = n - fee_tickets
    cur = conn.execute(
        "UPDATE users SET gift_earned = gift_earned - ? WHERE id=? "
        "AND gift_earned=?", (n, user["id"], n))
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        return redir("/tickets",
                     "Your earnings changed — try again.")
    stamp = now_iso()
    cur2 = conn.execute(
        "INSERT INTO payout_requests(user_id, tickets, "
        "dollars_cents, payout_note, fee_tickets, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (user["id"], pay_tickets, _earned_dollars(pay_tickets),
         note, fee_tickets, stamp))
    req_id = cur2.lastrowid
    if fee_tickets:
        fee_cents = _earned_dollars(fee_tickets)
        conn.execute(
            "INSERT INTO label_fees(user_id, payout_request_id, "
            "tickets, cents, created_at) VALUES(?,?,?,?,?)",
            (user["id"], req_id, fee_tickets, fee_cents,
             stamp))
        _fund_set(conn, _fund_cents(conn) + fee_cents)
    conn.execute(
        "INSERT INTO ticket_txns(user_id, kind, tickets, created_at) "
        "VALUES(?,?,?,?)", (user["id"], "cashout_hold", -n, stamp))
    conn.commit()
    conn.close()
    # Auto-pay (Ken's order Oct 30): it's the creator's money —
    # no approval gate. The sweep attempts this request (and any
    # older queued ones) right now.
    _payout_sweep()
    conn = db()
    row = conn.execute(
        "SELECT status FROM payout_requests WHERE id=?",
        (req_id,)).fetchone()
    me2 = conn.execute(
        "SELECT payout_ready FROM users WHERE id=?",
        (user["id"],)).fetchone()
    conn.close()
    status = row["status"] if row else "pending"
    dollars = _money(_earned_dollars(pay_tickets))
    if status == "paid":
        return redir("/tickets",
                     f"💸 Sent! Your {dollars} is on its way to "
                     "your bank (lands in about 2 business days).")
    if status == "sending":
        return redir("/tickets",
                     "💸 Your cash-out is sending — it completes "
                     "on its own. No approval needed.")
    if me2 and me2["payout_ready"]:
        return redir("/tickets",
                     "Cash-out requested 💰 — it's queued and "
                     "sends by itself as soon as HERE's payout "
                     "balance covers it. It's your money: no "
                     "approval needed, ever.")
    return redir("/tickets",
                 "Cash-out requested 💰 — finish your bank setup "
                 "(Bank payouts below) and it sends automatically. "
                 "No approval needed — it's your money.")


@app.get("/earnings", response_class=HTMLResponse)
async def earnings_page(request: Request):
    """Build MONEY: the streamer's private earnings dashboard —
    what they earned, stream by stream, plus the cash-out center.

    Pure presentation over the existing ledgers: per-stream sums
    group ticket_txns 'gift_receive' credits by their live_id
    (the gift + shout writers have always set it); credits with a
    NULL live_id predate attribution and aggregate into one honest
    "Earlier streams" bucket. NOTHING here moves money — the
    cash-out form posts to the same POST /tickets/cashout handler
    as the wallet page (same min, same hold, one pending at a
    time), and bank setup/update are the existing /payouts routes.

    Money display discipline (Build V, as amended by Build RICH —
    Ken's direct order): on THIS private page the earned-ticket
    figures carry their dollar worth beside them and the cash-out
    center states plainly whether the user can cash out now or
    exactly how many more tickets (and dollars) they need. The
    worth math is the cash-out math itself (_earned_dollars), so
    the page can never disagree with a payout. Public profiles
    still show no money anywhere."""
    user, err = require_login(request)
    if err:
        return err
    me = get_user(user["id"]) or {}
    earned = me.get("gift_earned") or 0
    lifetime = me.get("gift_lifetime") or 0
    conn = db()
    stream_rows = conn.execute(
        "SELECT t.live_id AS live_id, "
        "SUM(CASE WHEN COALESCE(t.note,'')='shout' THEN 0 ELSE 1 "
        "END) AS gifts, COALESCE(SUM(t.tickets),0) AS tickets, "
        "MAX(t.created_at) AS last_at, s.title AS live_title, "
        "s.created_at AS live_created FROM ticket_txns t "
        "LEFT JOIN live_sessions s ON s.id = t.live_id "
        "WHERE t.user_id=? AND t.kind='gift_receive' "
        "AND t.live_id IS NOT NULL GROUP BY t.live_id "
        "ORDER BY COALESCE(s.created_at, MAX(t.created_at)) DESC",
        (user["id"],)).fetchall()
    streams = [{
        "title": (r["live_title"] or "").strip()
                 or f"Live #{r['live_id']}",
        "when": fancy_time(r["live_created"] or r["last_at"]),
        "gifts": r["gifts"] or 0,
        "tickets": r["tickets"] or 0}
        for r in stream_rows]
    legacy = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(tickets),0) s, "
        "SUM(CASE WHEN COALESCE(note,'')='shout' THEN 0 ELSE 1 "
        "END) g FROM ticket_txns WHERE user_id=? AND "
        "kind='gift_receive' AND live_id IS NULL AND "
        "COALESCE(note,'') NOT IN ('page','post','entry')",
        (user["id"],)).fetchone()
    earlier = ({"gifts": (legacy["g"] or 0) if legacy else 0,
                "tickets": (legacy["s"] or 0) if legacy else 0}
               if legacy and legacy["c"] else None)
    # Build PAGE GIFTS: page/profile gift credits also carry a
    # NULL live_id, but they are NOT "Earlier streams" — they get
    # their own line (the note='page' marker the page-gift writer
    # stamps), so a creator who never goes live still sees exactly
    # what their page earned.
    page_g = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(tickets),0) s FROM "
        "ticket_txns WHERE user_id=? AND kind='gift_receive' AND "
        "COALESCE(note,'') = 'page'", (user["id"],)).fetchone()
    page_gifts = ({"gifts": page_g["c"] or 0,
                   "tickets": page_g["s"] or 0}
                  if page_g and page_g["c"] else None)
    # Money build: post/Peep gifts (note='post') and ticket-door
    # earnings (kind='entry_receive') get their own honest lines
    # too — a creator sees exactly which door the money came
    # through.
    post_g = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(tickets),0) s FROM "
        "ticket_txns WHERE user_id=? AND kind='gift_receive' AND "
        "COALESCE(note,'') = 'post'", (user["id"],)).fetchone()
    post_gifts_earn = ({"gifts": post_g["c"] or 0,
                        "tickets": post_g["s"] or 0}
                       if post_g and post_g["c"] else None)
    track_g = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(tickets),0) s FROM "
        "ticket_txns WHERE user_id=? AND kind='gift_receive' AND "
        "COALESCE(note,'') = 'track'", (user["id"],)).fetchone()
    track_gifts_earn = ({"gifts": track_g["c"] or 0,
                         "tickets": track_g["s"] or 0}
                        if track_g and track_g["c"] else None)
    door_r = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(tickets),0) s FROM "
        "ticket_txns WHERE user_id=? AND "
        "kind='entry_receive'", (user["id"],)).fetchone()
    door_earn = ({"gifts": door_r["c"] or 0,
                  "tickets": door_r["s"] or 0}
                 if door_r and door_r["c"] else None)
    # Build LOCKED CONTENT: unlock credits are their own kind
    # ('unlock_receive', note='unlock') — never gift_receive — so
    # they can't leak into the stream buckets above; they get their
    # own line here, counted straight off content_unlocks' twin
    # ledger rows.
    lock_r = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(tickets),0) s FROM "
        "ticket_txns WHERE user_id=? AND kind='unlock_receive' AND "
        "COALESCE(note,'') = 'unlock'", (user["id"],)).fetchone()
    locked_content = ({"sales": lock_r["c"] or 0,
                       "tickets": lock_r["s"] or 0}
                      if lock_r and lock_r["c"] else None)
    pending = conn.execute(
        "SELECT * FROM payout_requests WHERE user_id=? AND "
        "status IN ('pending','sending') ORDER BY id DESC LIMIT 1",
        (user["id"],)).fetchone()
    my_payouts = conn.execute(
        "SELECT * FROM payout_requests WHERE user_id=? ORDER BY id "
        "DESC LIMIT 20", (user["id"],)).fetchall()
    paid_total = conn.execute(
        "SELECT COALESCE(SUM(dollars_cents),0) s FROM "
        "payout_requests WHERE user_id=? AND status='paid'",
        (user["id"],)).fetchone()["s"]
    conn.close()
    payouts_history = [{
        "id": p["id"], "paid": p["status"] == "paid",
        "tickets": p["tickets"],
        "dollars": _money2(p["dollars_cents"]),
        "when": fancy_time(p["created_at"]),
        "status_text": _payout_status_text(dict(p)),
        "decided": (fancy_time(p["decided_at"])
                    if p["decided_at"] else "")}
        for p in my_payouts]
    bank_state = ("ready" if me.get("payout_ready")
                  else "started" if me.get("stripe_acct_id")
                  else "none")
    cashout_remaining = max(0, CASHOUT_MIN_TICKETS - earned)
    _ec = db()
    quest = _creator_quest(_ec, user)
    invite_bonus_total = _ec.execute(
        "SELECT COALESCE(SUM(tickets),0) AS s FROM ticket_txns "
        "WHERE user_id=? AND kind='invite_bonus'",
        (user["id"],)).fetchone()["s"]
    _ec.close()
    return render(request, "earnings.html", {
        "lifetime": lifetime, "earned": earned,
        "lifetime_worth": _money2(_earned_dollars(lifetime)),
        "earned_worth": _money2(_earned_dollars(earned)),
        "cashout_remaining": cashout_remaining,
        "cashout_remaining_dollars": _money2(max(
            0, _earned_dollars(CASHOUT_MIN_TICKETS)
            - _earned_dollars(earned))),
        "cashout_min_dollars": _money2(
            _earned_dollars(CASHOUT_MIN_TICKETS)),
        "streams": streams, "earlier": earlier,
        "page_gifts": page_gifts,
        "post_gifts_earn": post_gifts_earn,
        "track_gifts_earn": track_gifts_earn,
        "door_earn": door_earn,
        "locked_content": locked_content,
        "pending": ({"tickets": pending["tickets"],
                     "dollars": _money2(pending["dollars_cents"]),
                     "when": fancy_time(pending["created_at"]),
                     "hand_note": pending["hand_note"] or ""}
                    if pending else None),
        "can_cashout": earned >= CASHOUT_MIN_TICKETS and not pending,
        "cashout_min": CASHOUT_MIN_TICKETS,
        "cashout_dollars": _money2(_earned_dollars(earned)),
        "paid_total": _money2(paid_total),
        "payouts_history": payouts_history,
        "quest": quest,
        "invite_bonus_total": invite_bonus_total,
        "bank_state": bank_state})


# ---------------- Build R: automatic bank payouts (Stripe Connect) ----
# A streamer can link a Stripe Express account ONCE (POST
# /payouts/setup — Stripe hosts the onboarding and holds the bank
# details; HERE stores only the account id). After that, the owner's
# approve action on a cash-out can SEND the money straight to the
# streamer's bank (POST /owner/payouts/{id}/send → a Stripe
# Transfer). The Build M manual flow (Mark paid / Decline) remains
# the fallback for anyone who hasn't set up — and for any send
# Stripe refuses.
#
# Capability honesty: live Connect needs one owner-side Stripe
# identity verification. Until Stripe lets account creation
# through, creation fails — that failure (or a creation success) is
# cached in app_settings('stripe_connect_state') and shown on the
# owner panel, so the state we present is always the last thing
# Stripe itself proved, never a guess.


def _app_setting(key):
    conn = db()
    row = conn.execute("SELECT value FROM app_settings WHERE key=?",
                       (key,)).fetchone()
    conn.close()
    return row["value"] if row else None


def _set_app_setting(key, value):
    conn = db()
    conn.execute("INSERT OR REPLACE INTO app_settings(key, value) "
                 "VALUES(?,?)", (key, value))
    conn.commit()
    conn.close()


def _connect_state():
    """Cached bank-payout capability: {'state': 'ready' | 'blocked'
    | 'unknown', 'error': short class, 'at': iso}. 'ready' = a
    connected-account creation has succeeded; 'blocked' = the last
    creation attempt failed (error holds the reason class);
    'unknown' = never proven either way."""
    raw = _app_setting("stripe_connect_state")
    if raw:
        try:
            d = json.loads(raw)
            if d.get("state") in ("ready", "blocked"):
                return d
        except Exception:
            pass
    return {"state": "unknown", "error": "", "at": ""}


def _connect_mark(state, error=""):
    _set_app_setting("stripe_connect_state", json.dumps(
        {"state": state, "error": error, "at": now_iso()}))


def _stripe_err_class(e):
    """Short, safe reason class for a Stripe failure — the ONLY part
    of a Stripe error that ever reaches a page. Keyword-classified;
    never the raw Stripe body."""
    msg = str(e).lower()
    if "insufficien" in msg:
        return "not enough money in HERE's Stripe balance yet"
    if ("connect" in msg or "capabilit" in msg or "verif" in msg
            or "not enabled" in msg or "not activated" in msg):
        return ("bank payouts aren't switched on at Stripe yet "
                "(owner verification step)")
    if "account" in msg:
        return "the streamer's Stripe account has a problem"
    return "Stripe refused it"


def _refresh_payout_ready(me):
    """One Stripe GET against the user's Express account: ready =
    payouts_enabled AND details_submitted. Caches the result on
    users.payout_ready and returns it. Any Stripe failure keeps the
    cached value — this never raises."""
    acct_id = me.get("stripe_acct_id")
    if not acct_id or not stripe_enabled():
        return me.get("payout_ready") or 0
    try:
        acct = _stripe_api("GET", f"/v1/accounts/{acct_id}")
    except Exception:
        return me.get("payout_ready") or 0
    ready = 1 if (acct.get("payouts_enabled")
                  and acct.get("details_submitted")) else 0
    if ready != (me.get("payout_ready") or 0):
        conn = db()
        conn.execute("UPDATE users SET payout_ready=? WHERE id=?",
                     (ready, me["id"]))
        conn.commit()
        conn.close()
    if ready:
        _connect_mark("ready")
    return ready


def _payout_status_text(pr):
    """One payout_requests row -> the streamer's plain-English
    status (wallet page 'Your cash-outs' list)."""
    st = pr["status"]
    if st == "pending":
        if (pr.get("hand_note") or "").strip():
            return "✅ Paid by hand — bank send still coming"
        return "⏳ Queued — sends automatically"
    if st == "sending":
        return "💸 Sending to your bank…"
    if st == "declined":
        return "↩️ Declined — tickets returned"
    if st == "paid":
        if pr.get("method") == "stripe":
            return ("💸 Sent to your bank — lands in ~2 business "
                    "days")
        return "✅ Paid"
    return st


@app.post("/payouts/setup")
async def payouts_setup(request: Request):
    """Start (or resume) the streamer's Stripe Express onboarding:
    create the connected account on first use, then hand the user a
    fresh Stripe-hosted onboarding link. ANY Stripe failure lands
    back on /tickets?setup=unavailable with a friendly state — the
    raw error is logged server-side only, never leaked."""
    user, err = require_login(request)
    if err:
        return err
    if not stripe_enabled():
        return redir("/tickets?setup=unavailable")
    me = get_user(user["id"]) or {}
    acct_id = me.get("stripe_acct_id")
    base = str(request.base_url).rstrip("/")
    try:
        if not acct_id:
            resp = _stripe_api("POST", "/v1/accounts", {
                "type": "express",
                "country": "US",
                "email": me.get("email") or "",
                "capabilities[transfers][requested]": "true",
                "business_type": "individual",
            })
            acct_id = resp.get("id")
            if not acct_id:
                raise RuntimeError("Stripe returned no account id")
            conn = db()
            conn.execute(
                "UPDATE users SET stripe_acct_id=? WHERE id=?",
                (acct_id, user["id"]))
            conn.commit()
            conn.close()
            # A creation success is the proof Connect is live for
            # us — cache it for the owner panel's capability line.
            _connect_mark("ready")
        link = _stripe_api("POST", "/v1/account_links", {
            "account": acct_id,
            "type": "account_onboarding",
            "return_url": base + "/tickets?setup=done",
            "refresh_url": base + "/tickets?setup=retry",
        })
        url = link.get("url")
        if not url:
            raise RuntimeError("Stripe returned no onboarding link")
    except Exception as e:
        print(f"[payouts] setup failed for user {user['id']}: {e}")
        if not acct_id:
            # Creation itself failed — that's the capability signal
            # (until the owner's verification lands, this is the
            # expected Connect-not-enabled refusal).
            _connect_mark("blocked", _stripe_err_class(e))
        return redir("/tickets?setup=unavailable")
    return RedirectResponse(url, status_code=303)


@app.post("/payouts/update")
async def payouts_update(request: Request):
    """Send a payout-ready streamer to their Stripe Express
    dashboard — bank details live at Stripe, never here."""
    user, err = require_login(request)
    if err:
        return err
    me = get_user(user["id"]) or {}
    if not (me.get("stripe_acct_id") and me.get("payout_ready")):
        return redir("/tickets",
                     "Bank payouts aren't ready yet — finish the "
                     "setup first.")
    try:
        link = _stripe_api(
            "POST",
            f"/v1/accounts/{me['stripe_acct_id']}/login_links")
        url = link.get("url")
        if not url:
            raise RuntimeError("Stripe returned no login link")
    except Exception as e:
        print(f"[payouts] login link failed for user "
              f"{user['id']}: {e}")
        return redir("/tickets",
                     "Couldn't open your Stripe bank page just "
                     "now — try again in a bit.")
    return RedirectResponse(url, status_code=303)


def _stripe_sig_ok(raw_body, sig_header, secret):
    """Verify a Stripe-Signature header (t + v1 HMAC-SHA256, 5-min
    tolerance, constant-time compare). Multiple v1 values are all
    tried (key rotation)."""
    if not sig_header or not secret:
        return False
    ts = None
    v1s = []
    for part in sig_header.split(","):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        if k == "t":
            ts = v
        elif k == "v1":
            v1s.append(v)
    if not ts or not v1s:
        return False
    try:
        if abs(time.time() - int(ts)) > 300:
            return False
    except (TypeError, ValueError):
        return False
    signed = f"{ts}.".encode() + raw_body
    expect = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expect, v) for v in v1s)


@app.post("/api/stripe/webhook")
async def stripe_webhook(request: Request):
    """Stripe webhook for ticket purchases: verify the signature,
    and on checkout.session.completed credit the buyer via the
    idempotent credit_purchase path. Unconfigured (no webhook
    secret) = 400 so Stripe shows it as unregistered, honestly."""
    secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        return JSONResponse({"ok": False,
                             "error": "Webhook not configured."},
                            status_code=400)
    raw = await request.body()
    if not _stripe_sig_ok(raw, request.headers.get("Stripe-Signature",
                                                  ""), secret):
        return JSONResponse({"ok": False, "error": "Bad signature."},
                            status_code=400)
    try:
        event = json.loads(raw.decode())
    except Exception:
        return JSONResponse({"ok": False, "error": "Bad JSON."},
                            status_code=400)
    if event.get("type") != "checkout.session.completed":
        return JSONResponse({"ok": True, "ignored": True})
    obj = (event.get("data") or {}).get("object") or {}
    meta = obj.get("metadata") or {}
    session_id = obj.get("id") or ""
    try:
        uid = int(meta.get("user_id") or 0)
    except (TypeError, ValueError):
        uid = 0
    tickets = _buy_tickets_for(meta)
    if uid and tickets:
        credit_purchase(uid, tickets, session_id)
    return JSONResponse({"ok": True})


# ---------------- HERE Market: local buy/sell ----------------
# In-app local buy/sell. A listing is a PUBLIC OFFER: it shows in
# browse, on its detail page, and on the logged-out /m/{id} share page
# even when the seller's profile is private -- posting the listing is
# the seller's own public act (like a public post). Blocks still apply
# both ways in-app: a blocked pair can't see, save, or message about
# each other's listings. HERE never handles money: no payments, no
# checkout -- buyers and sellers meet and pay directly (the safety
# note on the detail + public pages says exactly that).


def _money(cents):
    """cents -> '$25' when whole dollars, '$25.50' otherwise, 0 -> 'FREE'."""
    cents = int(cents or 0)
    if cents <= 0:
        return "FREE"
    if cents % 100 == 0:
        return f"${cents // 100:,}"
    return f"${cents // 100:,}.{cents % 100:02d}"


def _parse_price(raw):
    """Strict dollars string -> cents int. Accepts '25', '25.5',
    '25.50', '0' (= FREE). Raises ValueError on anything else:
    negatives, garbage, >2 decimal places, over the $999,999 cap."""
    s = (raw or "").strip()
    if not s:
        raise ValueError("Enter a price -- 0 means FREE.")
    if s[0] in "-+":
        raise ValueError("Price can't be negative.")
    if s.count(".") > 1:
        raise ValueError("Enter a price like 25 or 25.50.")
    if "." in s:
        d, c = s.split(".")
        if not c.isdigit() or len(c) > 2:
            raise ValueError("Enter a price like 25 or 25.50.")
        c = (c + "0")[:2]
    else:
        d, c = s, "00"
    if not d.isdigit():
        raise ValueError("Enter a price like 25 or 25.50.")
    cents = int(d) * 100 + int(c)
    if cents > MARKET_MAX_PRICE_CENTS:
        raise ValueError("Price is too high (max $999,999).")
    return cents


def _market_photos(row):
    """A listing row's photos JSON -> list of uploads-relative paths."""
    try:
        ph = json.loads(row["photos"] or "[]")
    except Exception:
        ph = []
    return [p for p in ph if isinstance(p, str) and p]


def _listing_card(row, saved=False):
    """Template-ready listing dict. `row` must carry the seller join
    aliases sname/savatar (every market query joins users)."""
    raw = _market_photos(row)
    keys = row.keys()
    return {
        "id": row["id"], "seller_id": row["seller_id"],
        "title": row["title"] or "",
        "price": _money(row["price_cents"]),
        "price_cents": row["price_cents"],
        "price_input": (f"{row['price_cents'] // 100}"
                        if row["price_cents"] % 100 == 0
                        else f"{row['price_cents'] / 100:.2f}"),
        "category": row["category"] or "",
        "condition": row["condition"] or "",
        "description": row["description"] or "",
        "location": row["location"] or "",
        "photos": ["/uploads/" + p for p in raw],
        "photo_paths": raw,
        "first_photo": ("/uploads/" + raw[0]) if raw else "",
        "status": row["status"], "sold": row["status"] == "sold",
        "boosted": bool("boost_until" in keys
                        and row["boost_until"]
                        and row["boost_until"] > time.time()),
        "created": fancy_time(row["created_at"]),
        "seller_name": row["sname"] if "sname" in keys else "",
        "seller_vvendor": bool(row["svvendor"]) if "svvendor" in keys and row["svvendor"] else False,
        "seller_avatar": (avatar_url(row["savatar"])
                          if "savatar" in keys and row["savatar"] else ""),
        "saved": bool(saved),
    }


def _get_listing(conn, lid):
    return conn.execute(
        "SELECT l.*, u.name AS sname, u.avatar AS savatar, u.is_verified_vendor AS svvendor "
        "FROM market_listings l JOIN users u ON u.id=l.seller_id "
        "WHERE l.id=?", (lid,)).fetchone()


def _market_form_fields(form):
    """Validate a sell/edit form's TEXT fields -> (fields, error).
    Photos are the caller's problem (create and edit treat them
    differently). A bad price/category/condition refuses the whole
    save, like a bad poll refuses the whole post."""
    title = (form.get("title") or "").strip()
    if not title:
        return None, "Give your listing a title."
    try:
        price_cents = _parse_price(form.get("price"))
    except ValueError as e:
        return None, str(e)
    category = (form.get("category") or "").strip()
    if category not in MARKET_CATEGORIES:
        return None, "Pick a category."
    condition = (form.get("condition") or "").strip()
    if condition not in MARKET_CONDITIONS:
        return None, "Pick a condition."
    return {"title": title[:120], "price_cents": price_cents,
            "category": category, "condition": condition,
            "description": (form.get("description") or "").strip()[:2000],
            "location": (form.get("location") or "").strip()[:80]}, None


def _market_save_photos(form, existing=()):
    """Save the form's NEW photo uploads (image-only through
    save_upload with IMG_EXTS + the app's standard image cap).
    Returns (paths, error); the 4-photo cap counts existing + new."""
    ups = [u for u in form.getlist("photos")
           if getattr(u, "filename", "")]
    if len(existing) + len(ups) > 4:
        return None, "Listings can have at most 4 photos."
    paths = list(existing)
    for up in ups:
        try:
            p = save_upload(up, "market", IMG_EXTS)
        except ValueError as e:
            return None, str(e)
        if p:
            paths.append(p)
    return paths, None


def _market_feed_post(user, listing_id, fields, photos):
    """The optional share-to-feed cross-post for a new listing: a
    completely normal post (photo kind with the first photo, else
    text) whose body carries the price line + the /market link.
    Deleting the listing later leaves the post standing; its link
    lands on the listing's friendly 'gone' page."""
    body = (f"🛒 For sale: {fields['title']} — "
            f"{_money(fields['price_cents'])}")
    desc = fields["description"]
    if desc:
        body += "\n" + (desc[:140] + ("..." if len(desc) > 140 else ""))
    body += f"\n👉 See it in the Market: /market/{listing_id}"
    conn = db()
    cur = conn.execute(
        "INSERT INTO posts(user_id, page_id, kind, body, feeling, "
        "link_url, video_url, media, privacy, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (user["id"], None, "photo" if photos else "text", body, "",
         "", "", photos[0] if photos else "", "public", now_iso()))
    pid = cur.lastrowid
    for t in parse_hashtags(body):
        conn.execute(
            "INSERT OR IGNORE INTO post_hashtags(post_id, tag) "
            "VALUES(?,?)", (pid, t))
    conn.commit()
    conn.close()
    record_activity(user["id"])
    return pid


def _market_forbidden():
    return JSONResponse({"ok": False, "error": "forbidden"},
                        status_code=403)


@app.get("/market", response_class=HTMLResponse)
async def market_browse(request: Request):
    user, err = require_login(request)
    if err:
        return err
    qp = request.query_params
    q = (qp.get("q") or "").strip()
    category = (qp.get("category") or "").strip()
    if category not in MARKET_CATEGORIES:
        category = ""
    sort = (qp.get("sort") or "new").strip()
    if sort not in ("new", "price_asc", "price_desc"):
        sort = "new"
    include_sold = (qp.get("sold") or "") in ("1", "true", "on")
    seller_raw = (qp.get("seller") or "").strip()
    seller_id = int(seller_raw) if seller_raw.isdigit() else None
    where = ["l.status IN ('active','sold')" if include_sold
             else "l.status='active'"]
    args = []
    if q:
        where.append("(l.title LIKE ? OR l.description LIKE ?)")
        args += [f"%{q}%", f"%{q}%"]
    if category:
        where.append("l.category=?")
        args.append(category)
    if seller_id:
        where.append("l.seller_id=?")
        args.append(seller_id)
    blocked = blocked_id_set(user["id"])
    if blocked:
        where.append("l.seller_id NOT IN (%s)"
                     % ",".join("?" * len(blocked)))
        args += sorted(blocked)
    _boost_first = ("CASE WHEN COALESCE(l.boost_until,0) > "
                    "CAST(strftime('%s','now') AS REAL) "
                    "THEN 0 ELSE 1 END, ")
    order = {"new": _boost_first + "l.created_at DESC",
             "price_asc": _boost_first
             + "l.price_cents ASC, l.created_at DESC",
             "price_desc": _boost_first
             + "l.price_cents DESC, l.created_at DESC"}[sort]
    conn = db()
    rows = conn.execute(
        "SELECT l.*, u.name AS sname, u.avatar AS savatar, u.is_verified_vendor AS svvendor "
        "FROM market_listings l JOIN users u ON u.id=l.seller_id WHERE "
        + " AND ".join(where) + f" ORDER BY {order} LIMIT 200",
        args).fetchall()
    saved_ids = {r["listing_id"] for r in conn.execute(
        "SELECT listing_id FROM market_saves WHERE user_id=?",
        (user["id"],)).fetchall()}
    conn.close()
    seller = get_user(seller_id) if seller_id else None
    return render(request, "market.html", {
        "listings": [_listing_card(r, saved=r["id"] in saved_ids)
                     for r in rows],
        "q": q, "category": category, "sort": sort,
        "include_sold": include_sold, "categories": MARKET_CATEGORIES,
        "seller": ({"id": seller["id"], "name": seller["name"]}
                   if seller else None),
        "saved_page": False, "mine_page": False})


@app.get("/market/mine", response_class=HTMLResponse)
async def market_mine(request: Request):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    rows = conn.execute(
        "SELECT l.*, u.name AS sname, u.avatar AS savatar, u.is_verified_vendor AS svvendor "
        "FROM market_listings l JOIN users u ON u.id=l.seller_id "
        "WHERE l.seller_id=? AND l.status!='deleted' "
        "ORDER BY (l.status='sold'), l.created_at DESC",
        (user["id"],)).fetchall()
    conn.close()
    return render(request, "market.html", {
        "listings": [_listing_card(r) for r in rows],
        "q": "", "category": "", "sort": "new", "include_sold": True,
        "categories": MARKET_CATEGORIES, "seller": None,
        "saved_page": False, "mine_page": True})


@app.get("/market/new", response_class=HTMLResponse)
async def market_new_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    return render(request, "market_form.html", {
        "editing": False, "listing": None, "action": "/market",
        "categories": MARKET_CATEGORIES, "conditions": MARKET_CONDITIONS})


@app.get("/market/saved", response_class=HTMLResponse)
async def market_saved(request: Request):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    rows = conn.execute(
        "SELECT l.*, u.name AS sname, u.avatar AS savatar, u.is_verified_vendor AS svvendor "
        "FROM market_saves s JOIN market_listings l ON l.id=s.listing_id "
        "JOIN users u ON u.id=l.seller_id "
        "WHERE s.user_id=? AND l.status!='deleted' "
        "ORDER BY (l.status='sold'), s.created_at DESC",
        (user["id"],)).fetchall()
    # Lazy cleanup: saves pointing at deleted listings are dropped on
    # read, so a deleted listing never lingers here as a dead row.
    conn.execute(
        "DELETE FROM market_saves WHERE user_id=? AND listing_id IN "
        "(SELECT id FROM market_listings WHERE status='deleted')",
        (user["id"],))
    conn.commit()
    conn.close()
    return render(request, "market.html", {
        "listings": [_listing_card(r, saved=True) for r in rows],
        "q": "", "category": "", "sort": "new", "include_sold": True,
        "categories": MARKET_CATEGORIES, "seller": None,
        "saved_page": True, "mine_page": False})


@app.post("/market")
async def market_create(request: Request):
    user, err = require_login(request)
    if err:
        return err
    form = await request.form()
    fields, ferr = _market_form_fields(form)
    if ferr:
        return redir("/market/new", ferr)
    photos, perr = _market_save_photos(form)
    if perr:
        return redir("/market/new", perr)
    share = (form.get("share_to_feed") or "") in ("1", "on", "true")
    conn = db()
    cur = conn.execute(
        "INSERT INTO market_listings(seller_id, title, price_cents, "
        "category, condition, description, location, photos, status, "
        "share_to_feed, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (user["id"], fields["title"], fields["price_cents"],
         fields["category"], fields["condition"], fields["description"],
         fields["location"], json.dumps(photos), "active",
         1 if share else 0, now_iso()))
    lid = cur.lastrowid
    conn.commit()
    conn.close()
    # Moderation: title + description are scanned; a hit flags the
    # listing into the owner queue -- it NEVER blocks the listing.
    flag_item("listing", lid, user["id"], user["name"],
              f"{fields['title']}\n{fields['description']}")
    if share:
        _market_feed_post(user, lid, fields, photos)
    return redir(f"/market/{lid}")


@app.get("/market/{lid}", response_class=HTMLResponse)
async def market_detail(request: Request, lid: int):
    user, err = require_login(request)
    if err:
        return err
    owner_on = is_owner(request)
    conn = db()
    row = _get_listing(conn, lid)
    if not row:
        conn.close()
        return redir("/market", "Listing not found.")
    if row["status"] == "deleted":
        conn.close()
        return render(request, "market_detail.html", {
            "gone": True, "listing": None, "seller": None,
            "is_seller": False, "saved": False, "more_count": 0,
            "safety_note": MARKET_SAFETY_NOTE})
    if row["seller_id"] != user["id"] and not owner_on \
            and blocked_either_way(user["id"], row["seller_id"]):
        conn.close()
        return render(request, "profile_unavailable.html", {})
    saved = bool(conn.execute(
        "SELECT 1 FROM market_saves WHERE user_id=? AND listing_id=?",
        (user["id"], lid)).fetchone())
    more_count = conn.execute(
        "SELECT COUNT(*) c FROM market_listings WHERE seller_id=? "
        "AND id!=? AND status!='deleted'",
        (row["seller_id"], lid)).fetchone()["c"]
    listing_live_id = None
    if row["live_id"]:
        _lv = conn.execute(
            "SELECT status FROM live_sessions WHERE id=?",
            (row["live_id"],)).fetchone()
        if _lv and _lv["status"] == "live":
            listing_live_id = row["live_id"]
    conn.close()
    seller = get_user(row["seller_id"])
    return render(request, "market_detail.html", {
        "listing_live_id": listing_live_id,
        "gone": False, "listing": _listing_card(row, saved=saved),
        "seller": ({"id": seller["id"], "name": seller["name"],
                    "avatar": avatar_url(seller.get("avatar", "")),
                    "is_verified_vendor": bool(seller.get("is_verified_vendor")),
                    "joined": fancy_time(seller["created_at"])}
                   if seller else None),
        "is_seller": row["seller_id"] == user["id"],
        "saved": saved, "more_count": more_count,
        "safety_note": MARKET_SAFETY_NOTE})


def _claim_state(row):
    """(active_claim_dict_or_None) — an expired hold is no hold."""
    if not row["claimed_by"] or not row["claim_expires_at"]:
        return None
    exp = _parse_ts(row["claim_expires_at"])
    if not exp or exp <= datetime.now(timezone.utc):
        return None
    cu = get_user(row["claimed_by"])
    return {"by": row["claimed_by"],
            "by_name": cu["name"] if cu else "Someone",
            "left_s": int((exp - datetime.now(
                timezone.utc)).total_seconds())}


@app.post("/market/{lid}/boost")
async def market_boost(request: Request, lid: int):
    """⭐ Boost a listing: BOOST_TICKETS buys 7 days at the top
    of the Market with a Featured tag. The tickets are spent —
    burned out of the seller's balance in one guarded update
    with a 'boost_spend' ledger line; HERE already collected
    their dollar value when the pack was bought. Stacking adds
    7 more days to whatever boost is left."""
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    conn = db()
    row = conn.execute(
        "SELECT * FROM market_listings WHERE id=?",
        (lid,)).fetchone()
    if not row or row["seller_id"] != user["id"]:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That listing isn't yours."},
            status_code=400)
    if row["status"] != "active":
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "Only a live listing can be "
                                   "boosted."},
            status_code=400)
    cur = conn.execute(
        "UPDATE users SET tickets_balance = "
        "COALESCE(tickets_balance,0) - ? WHERE id=? AND "
        "COALESCE(tickets_balance,0) >= ?",
        (BOOST_TICKETS, user["id"], BOOST_TICKETS))
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": f"Boosting takes {BOOST_TICKETS} tickets — "
                      "grab a pack first 🎟️"},
            status_code=400)
    now_e = time.time()
    until = max(now_e, row["boost_until"] or 0) + BOOST_SECONDS
    conn.execute(
        "UPDATE market_listings SET boost_until=? WHERE id=?",
        (until, lid))
    conn.execute(
        "INSERT INTO ticket_txns(user_id, kind, tickets, note, "
        "created_at) VALUES(?,?,?,?,?)",
        (user["id"], "boost_spend", -BOOST_TICKETS,
         f"market {lid}", now_iso()))
    conn.commit()
    bal = conn.execute(
        "SELECT tickets_balance FROM users WHERE id=?",
        (user["id"],)).fetchone()["tickets_balance"]
    conn.close()
    return JSONResponse({"ok": True, "boost_until": until,
                         "balance": bal or 0})


@app.post("/market/{lid}/claim")
async def market_claim(request: Request, lid: int):
    """✋ Claim it (live selling): while an item rides a live, a
    watcher can hold it for 10 minutes — first come, one holder.
    The seller then accepts (item goes SOLD to the claimer) or
    releases it back to the floor."""
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "Log in first."},
                            status_code=401)
    conn = db()
    row = _get_listing(conn, lid)
    if not row or row["status"] != "active":
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That item isn't available."},
            status_code=400)
    if row["seller_id"] == user["id"]:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "It's your item."},
            status_code=400)
    if not row["live_id"]:
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Claims open while the item is live."},
            status_code=400)
    lv = conn.execute(
        "SELECT status FROM live_sessions WHERE id=?",
        (row["live_id"],)).fetchone()
    if not lv or lv["status"] != "live":
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "That live is over — message the seller."},
            status_code=400)
    cur = _claim_state(row)
    if cur and cur["by"] != user["id"]:
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": f"{cur['by_name']} is holding this one."},
            status_code=400)
    exp = (datetime.now(timezone.utc)
           + timedelta(minutes=10)).isoformat()
    conn.execute(
        "UPDATE market_listings SET claimed_by=?, "
        "claim_expires_at=? WHERE id=?", (user["id"], exp, lid))
    conn.commit()
    conn.close()
    notify(row["seller_id"], "market_claim",
           f"✋ {user['name']} claimed “{row['title']}” on your "
           f"live — 10 minutes to accept in the shop strip.",
           f"/live/{row['live_id']}", actor_id=user["id"])
    return JSONResponse({"ok": True, "claim": {
        "by": user["id"], "by_name": user["name"], "left_s": 600}})


@app.post("/market/{lid}/claim-release")
async def market_claim_release(request: Request, lid: int):
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False}, status_code=401)
    conn = db()
    row = _get_listing(conn, lid)
    if row and (row["claimed_by"] == user["id"]
                or row["seller_id"] == user["id"]):
        conn.execute(
            "UPDATE market_listings SET claimed_by=NULL, "
            "claim_expires_at=NULL WHERE id=?", (lid,))
        conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.post("/market/{lid}/claim-accept")
async def market_claim_accept(request: Request, lid: int):
    """Seller accepts the hold: the item goes SOLD to the
    claimer (savers get the usual SOLD notice; the claimer gets
    their own — message the seller to pay and pick up)."""
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False}, status_code=401)
    conn = db()
    row = _get_listing(conn, lid)
    if not row or row["seller_id"] != user["id"]:
        conn.close()
        return JSONResponse({"ok": False}, status_code=403)
    cur = _claim_state(row)
    if not cur:
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "No live hold on this item."},
            status_code=400)
    claimer = cur["by"]
    savers = [r["user_id"] for r in conn.execute(
        "SELECT user_id FROM market_saves WHERE listing_id=?",
        (lid,)).fetchall()]
    conn.execute(
        "UPDATE market_listings SET status='sold', sold_at=?, "
        "claimed_by=NULL, claim_expires_at=NULL WHERE id=?",
        (now_iso(), lid))
    conn.commit()
    conn.close()
    for sid in savers:
        if sid != user["id"] and sid != claimer:
            notify(sid, "market_sold",
                   f"An item you saved was marked SOLD: "
                   f"{row['title']}", f"/market/{lid}",
                   actor_id=user["id"])
    notify(claimer, "market_claim_won",
           f"✅ {user['name']} accepted your claim on "
           f"“{row['title']}”! Message them to pay and pick up.",
           f"/messages/{user['id']}", actor_id=user["id"])
    return JSONResponse({"ok": True})


@app.post("/market/{lid}/go-live")
async def market_go_live(request: Request, lid: int):
    """Sell it LIVE (Ken's pick #4): attach this listing to the
    seller's live — starting one on the spot when they aren't
    already broadcasting — and land them in the room. The live
    page's shop strip carries the listing to every watcher."""
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    row = _get_listing(conn, lid)
    if not row or row["status"] == "deleted":
        conn.close()
        return redir("/market", "Listing not found.")
    if row["seller_id"] != user["id"]:
        conn.close()
        return redir(f"/market/{lid}",
                      "Only the seller can put this on a live.")
    _sweep_lives(conn)
    mine = conn.execute(
        "SELECT * FROM live_sessions WHERE host_id=? AND "
        "status='live' AND COALESCE(is_lobby,0)=0",
        (user["id"],)).fetchone()
    if mine:
        live_id = mine["id"]
    else:
        live_id = _create_live(
            conn, user, ("\U0001F6CD " + (row["title"] or "Live sale"))
            [:LIVE_TITLE_MAX])
        flag_item("live_title", live_id, user["id"], user["name"],
                  row["title"] or "")
    conn.execute(
        "UPDATE market_listings SET live_id=? WHERE id=?",
        (live_id, lid))
    conn.commit()
    conn.close()
    return redir(f"/live/{live_id}")


@app.post("/market/{lid}/detach-live")
async def market_detach_live(request: Request, lid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    row = _get_listing(conn, lid)
    if row and row["seller_id"] == user["id"]:
        conn.execute(
            "UPDATE market_listings SET live_id=NULL WHERE id=?",
            (lid,))
        conn.commit()
    conn.close()
    return redir(f"/market/{lid}")


@app.get("/market/{lid}/edit", response_class=HTMLResponse)
async def market_edit_page(request: Request, lid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    row = _get_listing(conn, lid)
    conn.close()
    if not row or row["status"] == "deleted":
        return redir("/market", "Listing not found.")
    if row["seller_id"] != user["id"]:
        return _market_forbidden()
    return render(request, "market_form.html", {
        "editing": True, "listing": _listing_card(row),
        "action": f"/market/{lid}/edit",
        "categories": MARKET_CATEGORIES, "conditions": MARKET_CONDITIONS})


@app.post("/market/{lid}/edit")
async def market_edit(request: Request, lid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    row = _get_listing(conn, lid)
    conn.close()
    if not row or row["status"] == "deleted":
        return redir("/market", "Listing not found.")
    if row["seller_id"] != user["id"]:
        return _market_forbidden()
    form = await request.form()
    fields, ferr = _market_form_fields(form)
    if ferr:
        return redir(f"/market/{lid}/edit", ferr)
    remove = set(form.getlist("remove_photo"))
    kept = [p for p in _market_photos(row) if p not in remove]
    photos, perr = _market_save_photos(form, existing=kept)
    if perr:
        return redir(f"/market/{lid}/edit", perr)
    conn = db()
    conn.execute(
        "UPDATE market_listings SET title=?, price_cents=?, category=?, "
        "condition=?, description=?, location=?, photos=? WHERE id=?",
        (fields["title"], fields["price_cents"], fields["category"],
         fields["condition"], fields["description"], fields["location"],
         json.dumps(photos), lid))
    conn.commit()
    conn.close()
    # Edited text is re-scanned like new text (flags only, never blocks).
    flag_item("listing", lid, user["id"], user["name"],
              f"{fields['title']}\n{fields['description']}")
    return redir(f"/market/{lid}", "Listing updated.")


@app.post("/market/{lid}/sold")
async def market_sold(request: Request, lid: int):
    """Toggle active <-> sold. Marking SOLD notifies everyone who
    saved the listing; re-listing is silent. Seller only."""
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    row = _get_listing(conn, lid)
    if not row or row["status"] == "deleted":
        conn.close()
        return redir("/market", "Listing not found.")
    if row["seller_id"] != user["id"]:
        conn.close()
        return _market_forbidden()
    if row["status"] == "active":
        savers = [r["user_id"] for r in conn.execute(
            "SELECT user_id FROM market_saves WHERE listing_id=?",
            (lid,)).fetchall()]
        conn.execute(
            "UPDATE market_listings SET status='sold', sold_at=? "
            "WHERE id=?", (now_iso(), lid))
        conn.commit()
        conn.close()
        for sid in savers:
            if sid != user["id"]:
                notify(sid, "market_sold",
                       f"An item you saved was marked SOLD: {row['title']}",
                       f"/market/{lid}", actor_id=user["id"])
        return redir(f"/market/{lid}", "Marked as SOLD.")
    conn.execute(
        "UPDATE market_listings SET status='active', sold_at=NULL "
        "WHERE id=?", (lid,))
    conn.commit()
    conn.close()
    return redir(f"/market/{lid}", "Listing is available again.")


@app.post("/market/{lid}/delete")
async def market_delete(request: Request, lid: int):
    """Soft delete: the row stays (status='deleted') so thread
    snapshot cards and feed posts keep a target -- their links land
    on the friendly 'gone' page. Seller only."""
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    row = _get_listing(conn, lid)
    if not row or row["status"] == "deleted":
        conn.close()
        return redir("/market", "Listing not found.")
    if row["seller_id"] != user["id"]:
        conn.close()
        return _market_forbidden()
    conn.execute("UPDATE market_listings SET status='deleted' WHERE id=?",
                 (lid,))
    conn.commit()
    conn.close()
    return redir("/market/mine", "Listing deleted.")


@app.post("/market/{lid}/save")
async def market_save(request: Request, lid: int):
    """Toggle a save (bookmark). Idempotent per tap, like follow;
    a seller can never save their own listing."""
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    row = _get_listing(conn, lid)
    if not row or row["status"] == "deleted":
        conn.close()
        return redir("/market", "Listing not found.")
    if row["seller_id"] == user["id"]:
        conn.close()
        return _market_forbidden()
    if blocked_either_way(user["id"], row["seller_id"]):
        conn.close()
        return redir("/market", "You can't save this listing.")
    has = conn.execute(
        "SELECT 1 FROM market_saves WHERE user_id=? AND listing_id=?",
        (user["id"], lid)).fetchone()
    if has:
        conn.execute(
            "DELETE FROM market_saves WHERE user_id=? AND listing_id=?",
            (user["id"], lid))
    else:
        conn.execute(
            "INSERT OR IGNORE INTO market_saves(user_id, listing_id, "
            "created_at) VALUES(?,?,?)", (user["id"], lid, now_iso()))
    conn.commit()
    conn.close()
    return back(request, f"/market/{lid}")


@app.post("/market/{lid}/message")
async def market_message(request: Request, lid: int):
    """Message the seller about this listing: delivers ONE message
    into the pair's existing 1:1 thread (or starts it) through
    _deliver_message, carrying the listing snapshot so the thread
    renders a listing card above the bubble. Same gates as normal
    messaging: a block in either direction refuses."""
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    row = _get_listing(conn, lid)
    conn.close()
    if not row or row["status"] == "deleted":
        return redir("/market", "Listing not found.")
    if row["seller_id"] == user["id"]:
        return _market_forbidden()
    if blocked_either_way(user["id"], row["seller_id"]):
        return redir("/messages", "You can't message this person.")
    form = await request.form()
    body = (form.get("body") or "").strip() \
        or "Hi! Is this still available?"
    _deliver_message(user, row["seller_id"], body[:2000],
                     listing_id=lid, listing_title=row["title"],
                     listing_price_cents=row["price_cents"])
    return redir(f"/messages/{row['seller_id']}")


@app.get("/m/{lid}", response_class=HTMLResponse)
async def public_listing(request: Request, lid: int):
    """Public share page for a listing (growth door): logged-out OK.
    Listings are public offers, so an active/sold listing shows in
    full -- even when the seller's profile is private -- but the page
    exposes only the seller's display name + avatar, and links nowhere
    into the private profile. A deleted (or missing) listing gets the
    generic teaser with zero listing/seller data."""
    viewer = current_user(request)
    conn = db()
    row = _get_listing(conn, lid)
    conn.close()
    if not row or row["status"] == "deleted":
        return _public_teaser(request, viewer)
    card = _listing_card(row)
    og_image = ""
    if card["first_photo"]:
        og_image = _abs_url(request, card["first_photo"])
    # No seller-avatar fallback: share previews use the HERE logo.
    return render(request, "public_listing.html", {
        "listing": card,
        "seller": {"name": row["sname"],
                   "is_verified_vendor": bool(row["svvendor"]) if "svvendor" in row.keys() and row["svvendor"] else False,
                   "avatar": avatar_url(row["savatar"] or "")},
        "og_image": og_image,
        "excerpt": (row["description"] or row["title"] or "")[:200],
        "safety_note": MARKET_SAFETY_NOTE,
        "logged_in": bool(viewer)})


# ---------------- ads (Build ADS) ----------------
# One engine, two doors: house ads are authored in the owner panel
# (kind='house'); page ads come from a Page's "📣 Run as ad" flow
# (kind='page'). Phase 1: page ads are FREE to run. The engine lives
# in ads.py (schedule/targeting/rotation/composition); these are the
# HTTP surfaces: click + hide for viewers, the owner panel CRUD +
# oversight, and the page promote flow with its mini-dashboard.

_AD_WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def _ad_schedule_label(ad):
    """One-line human summary of an ad's run rules, for dashboards."""
    s = ad.get("schedule") or "always"
    if s == "weekly":
        days = sorted(ads._csv_ints(ad.get("weekly_days")))
        base = "Weekly (" + (", ".join(_AD_WEEKDAYS[d] for d in days
                                        if 0 <= d <= 6) or "no days") + ")"
    elif s == "monthly":
        days = sorted(ads._csv_ints(ad.get("monthly_days")))
        base = "Monthly (days " + (", ".join(str(d) for d in days)
                                   or "—") + ")"
    else:
        base = "Always"
    hs, he = int(ad.get("hour_start") or 0), int(ad.get("hour_end") or 24)
    if (hs, he) != (0, 24):
        base += f" · {hs:02d}:00–{he:02d}:00"
    if ad.get("smart_hours"):
        base += " · ⚡ smart hours"
    if ad.get("starts_on") or ad.get("ends_on"):
        base += f" · {ad.get('starts_on') or '…'} → {ad.get('ends_on') or '…'}"
    return base


def _ad_fields_from_form(form, allow_weight=False):
    """Parse the shared ad form (owner panel + page promote) into ads
    column values. Media is resolved separately (uploads are stateful)."""
    def _int(name, default, lo, hi):
        try:
            return max(lo, min(hi, int(form.get(name) or default)))
        except (TypeError, ValueError):
            return default

    def _date(name):
        v = (form.get(name) or "").strip()
        if not v:
            return None
        try:
            datetime.strptime(v, "%Y-%m-%d")
            return v
        except ValueError:
            return None

    schedule = (form.get("schedule") or "always").strip()
    if schedule not in ("always", "weekly", "monthly"):
        schedule = "always"
    weekly = set()
    for v in form.getlist("weekly_days"):
        try:
            d = int(v)
        except (TypeError, ValueError):
            continue
        if 0 <= d <= 6:
            weekly.add(d)
    monthly = sorted(d for d in ads._csv_ints(form.get("monthly_days"))
                     if 1 <= d <= 31)
    return {
        "title": (form.get("title") or "").strip()[:120],
        "body": (form.get("body") or "").strip()[:1000],
        "link_url": (form.get("link_url") or "").strip()[:500],
        "cta_label": ((form.get("cta_label") or "").strip()[:30]
                      or "Learn more"),
        "tags": ",".join(ads.parse_tags(form.get("tags") or ""))[:200],
        "schedule": schedule,
        "weekly_days": ",".join(str(d) for d in sorted(weekly)),
        "monthly_days": ",".join(str(d) for d in monthly),
        "hour_start": _int("hour_start", 0, 0, 24),
        "hour_end": _int("hour_end", 24, 0, 24),
        "smart_hours": 1 if (form.get("smart_hours") or "") in (
            "1", "on", "true") else 0,
        "weight": _int("weight", 1, 1, 5) if allow_weight else 1,
        "starts_on": _date("starts_on"),
        "ends_on": _date("ends_on"),
    }


def _ad_media_from_form(form, existing_path="", existing_kind="none"):
    """Resolve an ad's media: a fresh upload wins, then an existing
    uploads path named by the form (promote-from-post/listing), then a
    clear request, else whatever the ad already had.
    Returns (media_path, media_kind, error)."""
    up = form.get("media")
    if up is not None and getattr(up, "filename", ""):
        try:
            path = save_upload(up, "ads", IMG_EXTS | VID_EXTS)
        except ValueError as e:
            return "", "none", str(e)
        ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
        return path, ("video" if ext in VID_EXTS else "image"), None
    mp = (form.get("media_path") or "").strip()
    if mp:
        if re.fullmatch(r"(posts|market|pages|ads)/[A-Za-z0-9._\-]+", mp):
            ext = mp.rsplit(".", 1)[-1].lower() if "." in mp else ""
            if ext in VID_EXTS:
                return mp, "video", None
            if ext in IMG_EXTS:
                return mp, "image", None
        return "", "none", "That media couldn't be used."
    if (form.get("media_clear") or "") in ("1", "on", "true"):
        return "", "none", None
    return existing_path, existing_kind, None


def _insert_ad(creator, kind, page_id, fields, media_path, media_kind):
    """Insert an ad row and run the standard moderation scan over its
    words (a hit queues a flag for the owner; the ad still runs —
    flagging never blocks, like every other content type)."""
    conn = db()
    cur = conn.execute(
        "INSERT INTO ads(creator_id, page_id, kind, title, body, "
        "media_kind, media_path, link_url, cta_label, tags, status, "
        "schedule, weekly_days, monthly_days, hour_start, hour_end, "
        "smart_hours, weight, starts_on, ends_on, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?, 'active', ?,?,?,?,?,?,?,?,?,?)",
        (creator["id"], page_id, kind, fields["title"], fields["body"],
         media_kind, media_path, fields["link_url"], fields["cta_label"],
         fields["tags"], fields["schedule"], fields["weekly_days"],
         fields["monthly_days"], fields["hour_start"], fields["hour_end"],
         fields["smart_hours"], fields["weight"], fields["starts_on"],
         fields["ends_on"], now_iso()))
    ad_id = cur.lastrowid
    conn.commit()
    conn.close()
    flag_item("ad", ad_id, creator["id"], creator["name"],
              f"{fields['title']}\n{fields['body']}")
    return ad_id


def _update_ad(ad_id, fields, media_path, media_kind):
    conn = db()
    conn.execute(
        "UPDATE ads SET title=?, body=?, media_kind=?, media_path=?, "
        "link_url=?, cta_label=?, tags=?, schedule=?, weekly_days=?, "
        "monthly_days=?, hour_start=?, hour_end=?, smart_hours=?, "
        "weight=?, starts_on=?, ends_on=? WHERE id=?",
        (fields["title"], fields["body"], media_kind, media_path,
         fields["link_url"], fields["cta_label"], fields["tags"],
         fields["schedule"], fields["weekly_days"],
         fields["monthly_days"], fields["hour_start"], fields["hour_end"],
         fields["smart_hours"], fields["weight"], fields["starts_on"],
         fields["ends_on"], ad_id))
    conn.commit()
    conn.close()


def _house_creator(request, conn):
    """The user account house ads are attributed to: the owner's own
    signed-in account when the panel is used alongside a user session
    (the normal case), else the platform's first account."""
    u = current_user(request)
    if u:
        return u
    row = conn.execute("SELECT * FROM users ORDER BY id ASC LIMIT 1").fetchone()
    return dict(row) if row else None


@app.get("/a/{aid}", response_class=HTMLResponse)
async def public_ad(request: Request, aid: int):
    """Public landing page for one ad (Build SHARE): the destination
    of every copied/shared ad link, open to logged-out visitors like
    the other public pages. An ACTIVE ad renders large — Sponsored
    label, media, full words, and the CTA routed through the logging
    click route. A paused, ended or missing ad gets the generic join
    teaser: never a 404, and no ad copy leaks once an ad stops."""
    viewer = current_user(request)
    conn = db()
    row = conn.execute("SELECT * FROM ads WHERE id=?", (aid,)).fetchone()
    ad = dict(row) if row else None
    sponsor_name, sponsor_avatar = "HERE", ""
    if ad and ad.get("kind") == "page" and ad.get("page_id"):
        pg = conn.execute("SELECT name, cover FROM pages WHERE id=?",
                          (ad["page_id"],)).fetchone()
        if pg:
            sponsor_name = pg["name"]
            sponsor_avatar = avatar_url(pg["cover"] or "")
    conn.close()
    if not ad or (ad.get("status") or "") != "active":
        return _public_teaser(request, viewer)
    card = ads.compose_card(ad, sponsor_name, sponsor_avatar)
    # The landing shows the advertiser's full words (the feed card's
    # 70/160 trims are feed-only), over the composed layout/CTA/links.
    card["title"] = (ad.get("title") or "").strip() or card["title"]
    card["body"] = (ad.get("body") or "").strip()
    og_image = ""
    og_video = ""
    if card["layout"] == "media" and card["media_path"]:
        og_image = _abs_url(request, "/uploads/" + card["media_path"])
    elif card["layout"] == "video" and card["media_path"]:
        og_video = _abs_url(request, "/uploads/" + card["media_path"])
    return render(request, "public_ad.html", {
        "ad": card,
        "excerpt": card["body"] or card["title"],
        "og_image": og_image, "og_video": og_video,
        "logged_in": bool(viewer),
    })


@app.get("/ads/{aid}/click")
async def ad_click(request: Request, aid: int):
    """Log the click, then 302 to the ad's link. Only http(s) URLs and
    internal paths are honored; anything else lands on '#'. Logged-in
    clicks log a full event; a logged-out click (the shared-link case
    from /a/{id}) still counts — ad_events.user_id is NOT NULL, so it
    bumps the ads.clicks counter without an event row."""
    user = current_user(request)
    target = "#"
    conn = db()
    ad = conn.execute("SELECT link_url FROM ads WHERE id=?",
                      (aid,)).fetchone()
    if ad:
        if user:
            ads.record_click(conn, aid, user["id"])
        else:
            conn.execute("UPDATE ads SET clicks=clicks+1 WHERE id=?",
                         (aid,))
        conn.commit()
        target = ads.safe_link(ad["link_url"])
    conn.close()
    return RedirectResponse(target, status_code=302)


@app.post("/ads/{aid}/hide")
async def ad_hide(request: Request, aid: int):
    """The card's ✕: this ad never shows for this viewer again."""
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    if conn.execute("SELECT id FROM ads WHERE id=?", (aid,)).fetchone():
        ads.record_hide(conn, aid, user["id"])
        conn.commit()
    conn.close()
    return redir("/home", "Got it — that ad won't show for you again.")


@app.get("/owner/ads/new", response_class=HTMLResponse)
async def owner_ad_new(request: Request):
    err = require_owner(request)
    if err:
        return err
    return render(request, "owner/ads_form.html",
                  {"ad": None, "action": "/owner/ads",
                   "heading": "➕ New house ad", "error": ""})


@app.post("/owner/ads")
async def owner_ad_create(request: Request):
    err = require_owner(request)
    if err:
        return err
    form = await request.form()
    fields = _ad_fields_from_form(form, allow_weight=True)
    if not fields["title"]:
        return render(request, "owner/ads_form.html",
                      {"ad": None, "action": "/owner/ads",
                       "heading": "➕ New house ad",
                       "error": "Give the ad a headline."})
    media_path, media_kind, merr = _ad_media_from_form(form)
    if merr:
        return render(request, "owner/ads_form.html",
                      {"ad": None, "action": "/owner/ads",
                       "heading": "➕ New house ad", "error": merr})
    conn = db()
    creator = _house_creator(request, conn)
    conn.close()
    if not creator:
        return redir("/owner", "Create a user account first.")
    _insert_ad(creator, "house", None, fields, media_path, media_kind)
    return redir("/owner", "📣 House ad created — it's in the rotation.")


@app.get("/owner/ads/{aid}/edit", response_class=HTMLResponse)
async def owner_ad_edit(request: Request, aid: int):
    err = require_owner(request)
    if err:
        return err
    conn = db()
    ad = conn.execute("SELECT * FROM ads WHERE id=?", (aid,)).fetchone()
    conn.close()
    if not ad:
        return redir("/owner", "Ad not found.")
    return render(request, "owner/ads_form.html",
                  {"ad": dict(ad), "action": f"/owner/ads/{aid}",
                   "heading": "✏️ Edit ad", "error": ""})


@app.post("/owner/ads/{aid}")
async def owner_ad_update(request: Request, aid: int):
    err = require_owner(request)
    if err:
        return err
    conn = db()
    ad = conn.execute("SELECT * FROM ads WHERE id=?", (aid,)).fetchone()
    conn.close()
    if not ad:
        return redir("/owner", "Ad not found.")
    ad = dict(ad)
    form = await request.form()
    fields = _ad_fields_from_form(form, allow_weight=True)
    if not fields["title"]:
        return render(request, "owner/ads_form.html",
                      {"ad": ad, "action": f"/owner/ads/{aid}",
                       "heading": "✏️ Edit ad",
                       "error": "Give the ad a headline."})
    media_path, media_kind, merr = _ad_media_from_form(
        form, ad["media_path"], ad["media_kind"])
    if merr:
        return render(request, "owner/ads_form.html",
                      {"ad": ad, "action": f"/owner/ads/{aid}",
                       "heading": "✏️ Edit ad", "error": merr})
    _update_ad(aid, fields, media_path, media_kind)
    return redir("/owner", "📣 Ad updated.")


@app.post("/owner/ads/{aid}/status")
async def owner_ad_status(request: Request, aid: int):
    """Owner controls for ANY ad (house or page): pause / resume /
    end. Oversight lives here; page owners have their own switch on
    their promote page for their own ads."""
    err = require_owner(request)
    if err:
        return err
    form = await request.form()
    status = (form.get("status") or "").strip()
    if status in ("active", "paused", "ended"):
        conn = db()
        conn.execute("UPDATE ads SET status=? WHERE id=?", (status, aid))
        conn.commit()
        conn.close()
        if _oa(request):
            label = {"active": "▶ Ad is running.",
                     "paused": "⏸ Ad paused.",
                     "ended": "⏹ Ad ended."}[status]
            return _oa_json(label, status=status)
    elif _oa(request):
        return _oa_fail("That ad status isn't one I can set.")
    return redir("/owner")


@app.post("/owner/ads/{aid}/delete")
async def owner_ad_delete(request: Request, aid: int):
    """Build MONEY: REMOVE an ad completely (owner only) — the row
    and its ad_events go away, so an active ad leaves the rotation
    at once (this is not a pause). The ad's media file is deleted
    best-effort through the storage layer ONLY when the path lives
    under the ads/ uploads prefix and no other ad still references
    the same path — media borrowed from a post/listing/page (or
    shared by another ad) is never touched."""
    err = require_owner(request)
    if err:
        return err
    conn = db()
    ad = conn.execute("SELECT * FROM ads WHERE id=?", (aid,)).fetchone()
    media_path = ""
    if ad:
        media_path = ad["media_path"] or ""
        conn.execute("DELETE FROM ad_events WHERE ad_id=?", (aid,))
        conn.execute("DELETE FROM ads WHERE id=?", (aid,))
        conn.commit()
        if not media_path.startswith("ads/"):
            media_path = ""
        elif conn.execute(
                "SELECT 1 FROM ads WHERE media_path=? LIMIT 1",
                (media_path,)).fetchone():
            media_path = ""
    conn.close()
    if media_path:
        _delete_upload(media_path)
    if _oa(request):
        return (_oa_json("🗑 Ad removed completely.") if ad
                else _oa_fail("Ad not found."))
    return redir("/owner", "🗑 Ad removed completely."
                 if ad else "Ad not found.")


def _owner_ads_ctx(conn):
    """Dashboard data for the 📣 Ads section: every ad with sponsor +
    stats, split house vs page, plus totals."""
    rows = conn.execute(
        "SELECT a.*, u.name AS creator_name, p.name AS page_name "
        "FROM ads a LEFT JOIN users u ON u.id=a.creator_id "
        "LEFT JOIN pages p ON p.id=a.page_id ORDER BY a.id DESC").fetchall()

    def _row(r):
        d = dict(r)
        d["ctr"] = ads.ctr(d)
        d["schedule_label"] = _ad_schedule_label(d)
        d["sponsor"] = (d.get("page_name") if d["kind"] == "page"
                        else "HERE (house)")
        return d

    house = [_row(r) for r in rows if r["kind"] == "house"]
    page_ads = [_row(r) for r in rows if r["kind"] != "house"]
    totals = {
        "ads": len(rows),
        "active": sum(1 for r in rows if r["status"] == "active"),
        "impressions": sum(int(r["impressions"] or 0) for r in rows),
        "clicks": sum(int(r["clicks"] or 0) for r in rows),
    }
    totals["ctr"] = ads.ctr(totals)
    return house, page_ads, totals


# ---------------- owner ----------------
@app.get("/owner/login", response_class=HTMLResponse)
async def owner_login_page(request: Request):
    if is_owner(request):
        return redir("/owner")
    return render(request, "owner/login.html",
                  {"error": request.query_params.get("error", "")})


@app.post("/owner/login")
async def owner_login(request: Request):
    form = await request.form()
    password = form.get("password") or ""
    if not bcrypt.checkpw(password.encode(), OWNER_PW_HASH.encode()):
        return redir("/owner/login?error=" + "Wrong password.")
    token = new_owner_session()
    resp = redir("/owner")
    return set_session_cookie(resp, token, OWNER_COOKIE)


@app.get("/owner/logout")
async def owner_logout(request: Request):
    token = request.cookies.get(OWNER_COOKIE, "")
    if token:
        conn = db()
        conn.execute("DELETE FROM owner_sessions WHERE token=?", (token,))
        conn.commit()
        conn.close()
    resp = redir("/login")
    resp.delete_cookie(OWNER_COOKIE, path="/")
    return resp


@app.get("/owner", response_class=HTMLResponse)
async def owner_dashboard(request: Request):
    err = require_owner(request)
    if err:
        return err
    conn = db()
    users = []
    for u in conn.execute("SELECT * FROM users ORDER BY created_at DESC").fetchall():
        posts_n = conn.execute("SELECT COUNT(*) c FROM posts WHERE user_id=?",
                               (u["id"],)).fetchone()["c"]
        friends_n = conn.execute(
            "SELECT COUNT(*) c FROM friendships WHERE status='accepted' AND "
            "(user_id=? OR friend_id=?)", (u["id"], u["id"])).fetchone()["c"]
        users.append({"id": u["id"], "name": u["name"], "email": u["email"],
                      "created_at": fancy_time(u["created_at"]),
                      "posts": posts_n, "friends": friends_n,
                      "is_verified_user": bool(u["is_verified_user"]),
                      "is_verified_vendor": bool(u["is_verified_vendor"])})

    fact = conn.execute(
        "SELECT f.*, a.name AS req_name, b.name AS tgt_name FROM friendships f "
        "LEFT JOIN users a ON a.id=f.user_id LEFT JOIN users b ON b.id=f.friend_id "
        "ORDER BY f.created_at DESC LIMIT 50").fetchall()
    friend_activity = [{
        "id": r["id"], "status": r["status"],
        "requester": r["req_name"] or "?", "target": r["tgt_name"] or "?",
        "created_at": fancy_time(r["created_at"])} for r in fact]

    frows = conn.execute(
        "SELECT mf.*, u.name AS author_name FROM moderation_flags mf "
        "LEFT JOIN users u ON u.id=mf.author_id "
        "ORDER BY CASE mf.status WHEN 'pending' THEN 0 ELSE 1 END, "
        "mf.created_at DESC LIMIT 100").fetchall()
    flags = [{
        "id": r["id"], "item_type": r["item_type"], "item_id": r["item_id"],
        "author_name": r["author_name"] or "Deleted user",
        "excerpt": r["excerpt"], "reasons": r["reasons"],
        "status": r["status"], "created_at": fancy_time(r["created_at"])}
        for r in frows]

    pages = []
    for p in conn.execute("SELECT * FROM pages ORDER BY created_at DESC").fetchall():
        members = conn.execute("SELECT COUNT(*) c FROM page_members WHERE page_id=?",
                               (p["id"],)).fetchone()["c"]
        pids = [x["id"] for x in conn.execute(
            "SELECT id FROM posts WHERE page_id=?", (p["id"],)).fetchall()]
        posts_n = len(pids)
        interactions = posts_n
        if pids:
            ph = ",".join("?" * len(pids))
            interactions += conn.execute(
                f"SELECT COUNT(*) c FROM comments WHERE post_id IN ({ph})",
                pids).fetchone()["c"]
            interactions += conn.execute(
                f"SELECT COUNT(*) c FROM likes WHERE post_id IN ({ph})",
                pids).fetchone()["c"]
        pages.append({"id": p["id"], "name": p["name"], "ptype": p["ptype"],
                      "members": members, "posts": posts_n,
                      "interactions": interactions})

    # --- verification requests: pending first, with review context ---
    vrows = conn.execute(
        "SELECT vr.*, ru.name AS requester_name FROM verification_requests vr "
        "LEFT JOIN users ru ON ru.id=vr.requester_id "
        "ORDER BY CASE vr.status WHEN 'pending' THEN 0 ELSE 1 END, "
        "vr.created_at DESC LIMIT 100").fetchall()
    verifications = []
    for r in vrows:
        if r["kind"] in ("user", "vuser", "vvendor"):
            tgt = conn.execute("SELECT id,name FROM users WHERE id=?",
                               (r["target_id"],)).fetchone()
            posts_n = conn.execute("SELECT COUNT(*) c FROM posts WHERE user_id=?",
                                   (r["target_id"],)).fetchone()["c"]
            extra_n = conn.execute(
                "SELECT COUNT(*) c FROM friendships WHERE status='accepted' AND "
                "(user_id=? OR friend_id=?)", (r["target_id"], r["target_id"])).fetchone()["c"]
            extra_lbl, link = "friends", f"/user/{r['target_id']}"
        else:
            tgt = conn.execute("SELECT id,name FROM pages WHERE id=?",
                               (r["target_id"],)).fetchone()
            posts_n = conn.execute("SELECT COUNT(*) c FROM posts WHERE page_id=?",
                                   (r["target_id"],)).fetchone()["c"]
            extra_n = conn.execute("SELECT COUNT(*) c FROM page_members WHERE page_id=?",
                                   (r["target_id"],)).fetchone()["c"]
            extra_lbl, link = "members", f"/page/{r['target_id']}"
        verifications.append({
            "id": r["id"], "kind": r["kind"],
            "kind_label": {"user": "star", "page": "page star",
                           "vuser": "VERIFIED USER sticker",
                           "vvendor": "VERIFIED VENDOR sticker"}.get(
                               r["kind"], r["kind"]),
            "name": tgt["name"] if tgt else "Deleted",
            "link": link,
            "requester_name": r["requester_name"] or "Deleted user",
            "posts": posts_n, "extra": extra_n, "extra_label": extra_lbl,
            "status": r["status"], "created_at": fancy_time(r["created_at"]),
        })

    # --- go live: rooms on air right now + the recently ended ---
    _sweep_lives(conn)
    live_now = []
    for r in conn.execute(
            "SELECT ls.*, u.name AS host_name FROM live_sessions ls "
            "LEFT JOIN users u ON u.id=ls.host_id WHERE ls.status='live' "
            "ORDER BY ls.created_at DESC").fetchall():
        live_now.append({
            "id": r["id"], "host_id": r["host_id"],
            "host_name": r["host_name"] or "Deleted user",
            "title": r["title"],
            "created_at": fancy_time(r["created_at"]),
            "viewers": _live_viewer_count(conn, r["id"])})
    live_recent = []
    for r in conn.execute(
            "SELECT ls.*, u.name AS host_name FROM live_sessions ls "
            "LEFT JOIN users u ON u.id=ls.host_id WHERE ls.status='ended' "
            "ORDER BY ls.ended_at DESC LIMIT 10").fetchall():
        live_recent.append({
            "id": r["id"], "host_id": r["host_id"],
            "host_name": r["host_name"] or "Deleted user",
            "title": r["title"],
            "created_at": fancy_time(r["created_at"]),
            "ended_at": fancy_time(r["ended_at"] or "")})

    today = datetime.now(timezone.utc).date().isoformat()
    stats = {
        "users": conn.execute("SELECT COUNT(*) c FROM users").fetchone()["c"],
        "posts_today": conn.execute(
            "SELECT COUNT(*) c FROM posts WHERE date(created_at)=date(?)",
            (today,)).fetchone()["c"],
        "msgs_today": conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE date(created_at)=date(?)",
            (today,)).fetchone()["c"],
        "taps_today": conn.execute(
            "SELECT COUNT(*) c FROM taps WHERE date(created_at)=date(?)",
            (today,)).fetchone()["c"],
        "flags_pending": conn.execute(
            "SELECT COUNT(*) c FROM moderation_flags WHERE status='pending'"
        ).fetchone()["c"],
        "verify_pending": conn.execute(
            "SELECT COUNT(*) c FROM verification_requests WHERE status='pending'"
        ).fetchone()["c"],
    }
    # --- live tickets: payout queue (pending first) + money row ---
    prow_orders = conn.execute(
        "SELECT pr.*, u.name AS user_name, u.email AS user_email, "
        "u.payout_ready AS user_payout_ready "
        "FROM payout_requests pr LEFT JOIN users u ON u.id=pr.user_id "
        "ORDER BY CASE WHEN pr.status IN ('pending','sending') "
        "THEN 0 ELSE 1 END, "
        "pr.created_at DESC LIMIT 50").fetchall()
    payouts = [{
        "id": r["id"], "user_id": r["user_id"],
        "user_name": r["user_name"] or "Deleted user",
        "user_email": r["user_email"] or "",
        "tickets": r["tickets"],
        "dollars": _money(r["dollars_cents"]),
        "payout_note": r["payout_note"], "status": r["status"],
        "method": r["method"],
        "stripe_transfer_id": r["stripe_transfer_id"],
        "user_payout_ready": bool(r["user_payout_ready"]),
        "hand_note": r["hand_note"] or "",
        "created_at": fancy_time(r["created_at"])}
        for r in prow_orders]
    tickets_sold = conn.execute(
        "SELECT COALESCE(SUM(tickets),0) s FROM ticket_txns "
        "WHERE kind='buy'").fetchone()["s"]
    earned_out = conn.execute(
        "SELECT COALESCE(SUM(gift_earned),0) s FROM users"
    ).fetchone()["s"]
    held = conn.execute(
        "SELECT COALESCE(SUM(tickets),0) s FROM payout_requests "
        "WHERE status IN ('pending','sending')").fetchone()["s"]
    money_stats = {
        "tickets_sold": tickets_sold,
        "liability": _money(_earned_dollars(earned_out + held)),
        "payouts_pending": sum(1 for p in payouts
                               if p["status"] in ("pending", "sending"))}

    # --- HERE Money (Build T): what the site itself has made ---
    # Profit = gross ticket sales - Stripe's estimated cut -
    # paid out to streamers - still owed to streamers. "Still
    # owed" counts held cash-outs at the dollars locked in when
    # the streamer asked (payout_requests.dollars_cents — the
    # exact amount that will leave), so an earned ticket is owed
    # exactly once at every stage: earnings -> request (hold)
    # -> paid. Definitions spelled out in ROUTE_CONTRACT.md.
    mp_today = _today_chicago()
    mp_d0 = _chicago_day_start_utc(mp_today)
    mp_d1 = _chicago_day_start_utc(mp_today + timedelta(days=1))
    mp_sales, gross_all, fee_est = _buy_aggregates(conn)
    _tc, gross_today_c, _tf = _buy_aggregates(conn, mp_d0, mp_d1)
    mp_m0 = _chicago_day_start_utc(mp_today.replace(day=1))
    mp_next = (mp_today.replace(day=28)
               + timedelta(days=4)).replace(day=1)
    mp_m1 = _chicago_day_start_utc(mp_next)
    _mc, gross_month_c, _mf = _buy_aggregates(conn, mp_m0, mp_m1)
    prow_paid = conn.execute(
        "SELECT COUNT(*) c, COALESCE(SUM(dollars_cents),0) s "
        "FROM payout_requests WHERE status='paid'").fetchone()
    held_dollars = conn.execute(
        "SELECT COALESCE(SUM(dollars_cents),0) s FROM "
        "payout_requests WHERE status IN ('pending','sending')"
    ).fetchone()["s"]
    owed_earned_c = _earned_dollars(earned_out)
    owed_c = owed_earned_c + held_dollars
    floating = conn.execute(
        "SELECT COALESCE(SUM(tickets_balance),0) s FROM users"
    ).fetchone()["s"]
    profit_c = (gross_all - fee_est - prow_paid["s"] - owed_c)
    mp_days = []
    for back in range(6, -1, -1):
        d = mp_today - timedelta(days=back)
        dc, dg, _df = _buy_aggregates(
            conn, _chicago_day_start_utc(d),
            _chicago_day_start_utc(d + timedelta(days=1)))
        mp_days.append({"label": f"{d.strftime('%a %b')} {d.day}",
                        "today": back == 0, "count": dc,
                        "gross": _money2(dg)})
    money_panel = {
        "profit": _money2(profit_c), "sales": mp_sales,
        "gross": _money2(gross_all),
        "gross_today": _money2(gross_today_c),
        "gross_month": _money2(gross_month_c),
        "fee_est": _money2(fee_est),
        "paid_out": _money2(prow_paid["s"]),
        "paid_count": prow_paid["c"],
        "owed": _money2(owed_c),
        "owed_earned": _money2(owed_earned_c),
        "owed_held": _money2(held_dollars),
        "floating": floating, "sold": tickets_sold,
        "days": mp_days}

    # --- password help queue (Build O): queued resets first, then
    # the recent history. State is derived, never stored: used /
    # expired win over the delivery label.
    _utcnow = datetime.now(timezone.utc)
    pwrows = conn.execute(
        "SELECT pr.*, u.name AS user_name, u.email AS user_email "
        "FROM password_resets pr LEFT JOIN users u ON u.id=pr.user_id "
        "ORDER BY CASE WHEN pr.delivery='queued' "
        "AND pr.used_at IS NULL THEN 0 ELSE 1 END, "
        "pr.id DESC LIMIT 50").fetchall()
    pw_resets = []
    for r in pwrows:
        exp = _parse_ts(r["expires_at"])
        expired = bool(exp and exp <= _utcnow)
        if r["used_at"]:
            state = "used"
        elif expired:
            state = "expired"
        elif r["delivery"] == "queued":
            state = "queued \u2014 needs hand-send"
        elif r["delivery"] == "hand_sent":
            state = "hand-sent"
        else:
            state = "emailed"
        pw_resets.append({
            "id": r["id"],
            "user_name": r["user_name"] or "Deleted user",
            "user_email": r["user_email"] or "",
            "state": state,
            "can_act": bool(r["delivery"] == "queued"
                            and not r["used_at"] and not expired
                            and r["token_plain"]),
            "created_at": fancy_time(r["created_at"]),
            "expires_at": fancy_time(r["expires_at"])})
    pw_help_pending = conn.execute(
        "SELECT COUNT(*) c FROM password_resets WHERE "
        "delivery='queued' AND used_at IS NULL AND expires_at>?",
        (_utcnow.isoformat(),)).fetchone()["c"]

    # --- ads (Build ADS): house rotation + page-ad oversight ---
    ads_house, ads_user, ads_totals = _owner_ads_ctx(conn)

    _rb_all = _race_board(conn, limit=100)
    _by_raw = sorted(_rb_all, key=lambda b: -b["raw"])
    race_summary = {
        "phase": _race_phase(),
        "racers": len(_rb_all),
        "total_real": sum(b["qualified"] for b in _rb_all),
        "leader_real": _rb_all[0]["qualified"] if _rb_all else 0,
        "leader_real_name": (_rb_all[0]["name"]
                             if _rb_all else "—"),
        "leader_raw": _by_raw[0]["raw"] if _by_raw else 0,
        "leader_raw_name": (_by_raw[0]["name"]
                            if _by_raw else "—"),
    }
    money_users = []
    for r in conn.execute(
            "SELECT id, name, gift_earned, gift_lifetime, "
            "payout_ready, creator_mode FROM users "
            "WHERE gift_earned > 0 OR id IN (SELECT user_id "
            "FROM payout_requests) "
            "ORDER BY gift_earned DESC LIMIT 50").fetchall():
        ge = r["gift_earned"] or 0
        money_users.append({
            "id": r["id"], "name": r["name"], "earned": ge,
            "lifetime": r["gift_lifetime"] or 0,
            "dollars": _money(_earned_dollars(ge)),
            "bank": bool(r["payout_ready"]),
            "creator": bool(r["creator_mode"]),
            "has10": ge >= CASHOUT_MIN_TICKETS,
            "eligible": (ge >= CASHOUT_MIN_TICKETS
                         and bool(r["payout_ready"]))})
    conn.close()
    return render(request, "owner/dashboard.html", {
        "race_summary": race_summary,
        "money_users": money_users,
        "users": users, "friend_activity": friend_activity,
        "flags": flags, "pages": pages, "stats": stats,
        "verifications": verifications,
        "live_now": live_now, "live_recent": live_recent,
        "payouts": payouts, "money_stats": money_stats,
        "money_panel": money_panel,
        "pw_resets": pw_resets, "pw_help_pending": pw_help_pending,
        "ads_house": ads_house, "ads_user": ads_user,
        "ads_totals": ads_totals,
        "connect_state": _connect_state()})


@app.post("/owner/flag/{fid}/dismiss")
async def owner_flag_dismiss(request: Request, fid: int):
    err = require_owner(request)
    if err:
        return err
    conn = db()
    conn.execute("UPDATE moderation_flags SET status='dismissed' WHERE id=?", (fid,))
    conn.commit()
    conn.close()
    if _oa(request):
        return _oa_json("Flag dismissed — the content stays.",
                        status="dismissed")
    return redir("/owner")


@app.post("/owner/flag/{fid}/remove")
async def owner_flag_remove(request: Request, fid: int):
    err = require_owner(request)
    if err:
        return err
    conn = db()
    f = conn.execute("SELECT * FROM moderation_flags WHERE id=?", (fid,)).fetchone()
    conn.close()
    if f:
        if f["item_type"] == "post":
            _delete_post(f["item_id"])
        elif f["item_type"] == "comment":
            conn = db()
            conn.execute("DELETE FROM comments WHERE id=?", (f["item_id"],))
            conn.commit()
            conn.close()
        elif f["item_type"] == "message":
            conn = db()
            conn.execute("DELETE FROM messages WHERE id=?", (f["item_id"],))
            conn.commit()
            conn.close()
        elif f["item_type"] == "listing":
            conn = db()
            conn.execute("UPDATE market_listings SET status='deleted' "
                         "WHERE id=?", (f["item_id"],))
            conn.commit()
            conn.close()
        elif f["item_type"] == "story":
            conn = db()
            conn.execute("DELETE FROM story_views WHERE story_id=?", (f["item_id"],))
            conn.execute("DELETE FROM stories WHERE id=?", (f["item_id"],))
            conn.commit()
            conn.close()
        conn = db()
        conn.execute("UPDATE moderation_flags SET status='removed' WHERE id=?", (fid,))
        conn.commit()
        conn.close()
    if _oa(request):
        return _oa_json("Content removed.", status="removed")
    return redir("/owner")


@app.post("/owner/post/{pid}/delete")
async def owner_post_delete(request: Request, pid: int):
    err = require_owner(request)
    if err:
        return err
    _delete_post(pid)
    return redir("/owner")


@app.post("/owner/user/{uid}/remove")
async def owner_user_remove(request: Request, uid: int):
    err = require_owner(request)
    if err:
        return err
    conn = db()
    pids = [r["id"] for r in conn.execute("SELECT id FROM posts WHERE user_id=?",
                                          (uid,)).fetchall()]
    conn.close()
    for pid in pids:
        _delete_post(pid)
    conn = db()
    conn.execute("DELETE FROM comments WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM likes WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM post_hidden_from WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM post_tags WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM messages WHERE sender_id=? OR receiver_id=?", (uid, uid))
    conn.execute("DELETE FROM friendships WHERE user_id=? OR friend_id=?", (uid, uid))
    conn.execute("DELETE FROM taps WHERE tapper_id=? OR tapped_id=?", (uid, uid))
    conn.execute("DELETE FROM notifications WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM page_members WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    conn.execute("DELETE FROM moderation_flags WHERE author_id=?", (uid,))
    conn.execute("DELETE FROM users WHERE id=?", (uid,))
    conn.commit()
    conn.close()
    if _oa(request):
        return _oa_json("User removed, with all their content.")
    return redir("/owner")


# ---------------- verification ----------------
@app.post("/verify/user/request")
async def verify_user_request(request: Request):
    user, err = require_login(request)
    if err:
        return err
    fresh = get_user(user["id"])
    if fresh.get("is_verified") or (fresh.get("verification_status") or "none") == "pending":
        return redir(f"/user/{user['id']}", "Your verification request is already pending.")
    conn = db()
    conn.execute("UPDATE users SET verification_status='pending' WHERE id=?", (user["id"],))
    conn.execute(
        "INSERT INTO verification_requests(kind, target_id, requester_id, status, created_at) "
        "VALUES(?,?,?,?,?)",
        ("user", user["id"], user["id"], "pending", now_iso()))
    conn.commit()
    conn.close()
    return redir(f"/user/{user['id']}", "Verification request submitted — the owner will review your page.")


@app.post("/verify/page/{pid}/request")
async def verify_page_request(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    pg = conn.execute("SELECT * FROM pages WHERE id=?", (pid,)).fetchone()
    if not pg or pg["owner_id"] != user["id"]:
        conn.close()
        return redir("/pages", "Only the page owner can request verification.")
    if pg["is_verified"] or (pg["verification_status"] or "none") == "pending":
        conn.close()
        return redir(f"/page/{pid}", "Verification request already pending.")
    conn.execute("UPDATE pages SET verification_status='pending' WHERE id=?", (pid,))
    conn.execute(
        "INSERT INTO verification_requests(kind, target_id, requester_id, status, created_at) "
        "VALUES(?,?,?,?,?)",
        ("page", pid, user["id"], "pending", now_iso()))
    conn.commit()
    conn.close()
    return redir(f"/page/{pid}", "Verification request submitted — the owner will review your page.")


@app.post("/owner/verify/{rid}/approve")
async def owner_verify_approve(request: Request, rid: int):
    err = require_owner(request)
    if err:
        return err
    conn = db()
    vr = conn.execute("SELECT * FROM verification_requests WHERE id=?", (rid,)).fetchone()
    if vr and vr["status"] == "pending":
        if vr["kind"] == "vuser":
            conn.execute("UPDATE users SET is_verified_user=1, "
                         "vuser_status='approved' WHERE id=?",
                         (vr["target_id"],))
        elif vr["kind"] == "vvendor":
            conn.execute("UPDATE users SET is_verified_vendor=1, "
                         "vvendor_status='approved' WHERE id=?",
                         (vr["target_id"],))
        else:
            table = "users" if vr["kind"] == "user" else "pages"
            conn.execute(
                f"UPDATE {table} SET is_verified=1, verification_status='approved' WHERE id=?",
                (vr["target_id"],))
        conn.execute("UPDATE verification_requests SET status='approved', reviewed_at=? WHERE id=?",
                     (now_iso(), rid))
        conn.commit()
    conn.close()
    if _oa(request):
        return _oa_json("Approved — the badge is live.",
                        status="approved")
    return redir("/owner")


@app.post("/owner/verify/{rid}/decline")
async def owner_verify_decline(request: Request, rid: int):
    err = require_owner(request)
    if err:
        return err
    conn = db()
    vr = conn.execute("SELECT * FROM verification_requests WHERE id=?", (rid,)).fetchone()
    if vr and vr["status"] == "pending":
        if vr["kind"] == "vuser":
            conn.execute("UPDATE users SET vuser_status='declined' WHERE id=?",
                         (vr["target_id"],))
        elif vr["kind"] == "vvendor":
            conn.execute("UPDATE users SET vvendor_status='declined' WHERE id=?",
                         (vr["target_id"],))
        else:
            table = "users" if vr["kind"] == "user" else "pages"
            conn.execute(f"UPDATE {table} SET verification_status='declined' WHERE id=?",
                         (vr["target_id"],))
        conn.execute("UPDATE verification_requests SET status='declined', reviewed_at=? WHERE id=?",
                     (now_iso(), rid))
        conn.commit()
    conn.close()
    if _oa(request):
        return _oa_json("Declined.", status="declined")
    return redir("/owner")


# ---------------- Verified User / Verified Vendor stickers ----------------
# Ken's order Oct 30, 2026: two more badges beside the name. The
# VERIFIED USER sticker (red seal) says "a real, stood-up member of
# HERE"; the VERIFIED VENDOR sticker (gold seal) says "a stood-up
# seller — safe to buy from". Only the owner grants them: from the
# owner panel's per-user switches, or by approving a request. The
# request door is buried deep in Settings on purpose — a quiet
# word-of-mouth door, not a menu item. Requests share the
# verification_requests table (kind 'vuser' / 'vvendor').

def _badge_request(request, kind, flag_col, status_col, label):
    user, err = require_login(request)
    if err:
        return err
    fresh = get_user(user["id"])
    if fresh.get(flag_col):
        return redir("/settings/profile",
                     f"You already wear the {label}. It shows beside your name.")
    if (fresh.get(status_col) or "none") == "pending":
        return redir("/settings/profile",
                     f"Your {label} request is already in — the owner will review it.")
    conn = db()
    conn.execute(f"UPDATE users SET {status_col}='pending' WHERE id=?",
                 (user["id"],))
    conn.execute(
        "INSERT INTO verification_requests(kind, target_id, requester_id, status, created_at) "
        "VALUES(?,?,?,?,?)",
        (kind, user["id"], user["id"], "pending", now_iso()))
    conn.commit()
    conn.close()
    return redir("/settings/profile",
                 f"{label} request sent — the owner reviews every one by hand.")


@app.post("/verify/vuser/request")
async def verify_vuser_request(request: Request):
    return _badge_request(request, "vuser", "is_verified_user",
                          "vuser_status", "Verified User sticker")


@app.post("/verify/vvendor/request")
async def verify_vvendor_request(request: Request):
    return _badge_request(request, "vvendor", "is_verified_vendor",
                          "vvendor_status", "Verified Vendor sticker")


@app.post("/owner/users/{uid}/badge/{kind}")
async def owner_user_badge_toggle(request: Request, uid: int, kind: str):
    """Owner panel switch: flip a user's Verified User / Verified
    Vendor sticker on or off. Turning one ON also settles any
    pending request for that badge (it was just granted by hand)."""
    err = require_owner(request)
    if err:
        return err
    cols = {"vuser": ("is_verified_user", "vuser_status"),
            "vvendor": ("is_verified_vendor", "vvendor_status")}
    if kind not in cols:
        return redir("/owner")
    flag_col, status_col = cols[kind]
    conn = db()
    row = conn.execute(f"SELECT {flag_col} AS f FROM users WHERE id=?",
                       (uid,)).fetchone()
    if row:
        newv = 0 if row["f"] else 1
        conn.execute(
            f"UPDATE users SET {flag_col}=?, {status_col}=? WHERE id=?",
            (newv, "approved" if newv else "none", uid))
        if newv:
            conn.execute(
                "UPDATE verification_requests SET status='approved', reviewed_at=? "
                "WHERE kind=? AND target_id=? AND status='pending'",
                (now_iso(), kind, uid))
        conn.commit()
    conn.close()
    if _oa(request):
        label = ("Verified User" if kind == "vuser"
                 else "Verified Vendor")
        if not row:
            return _oa_fail("That user is gone.")
        return _oa_json(
            f"{label} turned {'ON' if newv else 'OFF'}.",
            on=bool(newv))
    return redir("/owner")


# ---------------- Help: FAQ + Hearley the help bot ----------------
# Ken's order Oct 30, 2026: a FAQ tab that teaches people how to
# run HERE, plus an in-app helper named Hearley who answers real
# questions with specific answers. Hearley's brain is a curated
# knowledge base about HERE itself (accurate by construction — she
# only says what the site actually does); anything outside it gets
# an honest "that's one for the owner" plus the support door.

@app.get("/faq", response_class=HTMLResponse)
async def faq_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    return render(request, "faq.html", {})


@app.get("/hearley", response_class=HTMLResponse)
async def hearley_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    return render(request, "hearley.html", {})


# (keyword phrases/words, answer, optional link path, optional link label)
HEARLEY_KB = [
    (["how do i post", "make a post", "write a post", "post something", "composer"],
     "Tap the ➕ Create button up top (or the composer at the top of your feed), write what's on your mind, pick who can see it — Public, Friends, or Only me — and hit Post. You can add a photo, a video, a feeling, or a song. Your post lands in the feed and on your profile.",
     "/home", "Open the feed"),
    (["delete a post", "delete my post", "remove a post"],
     "Open your post, tap the ⋯ menu on it, and choose Delete. It's gone for everybody, right away. There's no undo, so be sure before you tap.",
     None, None),
    (["hide a post", "dont want to see", "don't want to see", "unhide"],
     "On any post, tap the ⋯ menu and choose Hide — it disappears from YOUR feed only; everybody else still sees it. You can unhide from the ⋯ menu on your profile's hidden list.",
     None, None),
    (["story", "stories", "add a story"],
     "Stories live at the top of the feed and last 24 hours. Tap the + on your own story circle to add a photo or video — friends see it, then it disappears on its own.",
     "/home", "See stories"),
    (["peep", "peeps"],
     "Peeps are HERE's short posts — quick hits that ride the feed. Same composer, keep it short and loud. Friends can react, comment, and gift a Peep just like a post.",
     "/home", "Open the feed"),
    (["add friend", "friend request", "find friends", "find people", "search people", "search for"],
     "Use the search bar up top to find people by name — or by job, city, or hometown on their profiles. Open their profile and tap Add Friend. When they accept, you're friends: you see each other's friends-only posts and can call each other.",
     "/search", "Search people"),
    (["message", "inbox", "dm", "send a message", "chat with"],
     "Tap the messages icon up top to open your Inbox. Pick a friend and write — you can send text, photos, and voice. The Sent box keeps what you've sent, and you can delete a conversation for yourself anytime.",
     "/messages", "Open messages"),
    (["go live", "start a live", "start live", "livestream", "stream"],
     "Tap the red Platforms button up top, then Go Live. Give your Platform a title, pick a category, and you're on air. People can watch, comment live, gift you tickets, and apply for a spot on your stage.",
     "/lives", "Open Platforms"),
    (["platform", "platforms", "lobby"],
     "Platforms are HERE's live rooms — every live is a Platform with its own stage. The Lobby is the always-open one: walk in anytime, day or night. Hosts run their room; guests apply for a spot and the host lets them in.",
     "/lives", "See what's live"),
    (["spot", "apply", "join the stage", "join a live", "get on stage", "panel"],
     "Watching a Platform? Tap 'Apply for a spot'. The host sees your name (and what spot you want, if the spots are named) and taps Let in. Once you're on stage, everybody can see and hear you — mute and camera buttons are yours to control.",
     "/lives", "See what's live"),
    (["ticket", "tickets", "buy tickets", "ticket packs", "how much are tickets"],
     "Live Tickets are HERE's gift money. Packs: 100 tickets for $0.99, 500 for $4.99, 1,500 for $13.99. Buy them on the Tickets page — payment runs through Stripe, and your balance shows the second it clears.",
     "/tickets", "Open Tickets"),
    (["gift", "send a gift", "gifting", "send tickets"],
     "See the gift button on a live, a post, a Peep, or a music track? Pick a gift and send it — the tickets leave your balance and land in the creator's EARNED balance. Earned tickets are the ones that turn into real money at cash-out.",
     "/tickets", "Open Tickets"),
    (["cash out", "cashout", "get paid", "withdraw", "my money", "payout"],
     "When your EARNED tickets reach 2,000, that's $10 — the cash-out door opens on the Tickets page. Every earned ticket is worth half a cent, guaranteed: the $13.99 pack, gifted in full, pays the creator $7.50. Bank-ready cash-outs send themselves; there's no follower minimum and no approval line for your own money.",
     "/tickets", "Open Tickets"),
    (["bank", "stripe", "connect bank", "bank account", "get my money to my bank"],
     "On the Tickets page tap 'Set up bank payouts'. That takes you to Stripe (the same company the big apps use) for a one-time, five-minute setup — ID and bank details go to Stripe, never to HERE. After that, cash-outs land in your bank in about two business days.",
     "/tickets", "Open Tickets"),
    (["market", "sell something", "selling", "list an item", "buy something", "buying"],
     "The Market is HERE's buy-and-sell board. Tap Market, then 'Sell something': add photos (up to 4), a price (0 means FREE), and a category. Buyers message you right here in your Inbox. HERE doesn't take payments — meet in a public place and pay the seller directly, like any good marketplace.",
     "/market", "Open the Market"),
    (["boost", "boost my listing", "promote my listing"],
     "A Market boost costs 100 tickets and pushes your listing up where buyers see it first. Open your listing and tap Boost.",
     "/market", "Open the Market"),
    (["verified vendor", "vendor badge", "vendor sticker", "trusted seller"],
     "The gold VERIFIED VENDOR sticker means the owner has stood this seller up as a real, trusted seller on HERE — it shows beside their name and on their Market listings. Sellers can request it in Settings (it's tucked deep, near the very bottom — the quiet door), and the owner reviews every request by hand. The owner can also stamp it directly from his panel.",
     "/market", "Open the Market"),
    (["verified user", "user badge", "user sticker"],
     "The red VERIFIED USER sticker means the owner has stood this person up as a real member of HERE — it rides beside their name on profiles and posts. You can request yours in Settings, tucked deep near the very bottom (most people never find it — now you know). The owner reviews every request himself.",
     "/settings/profile", "Open Settings"),
    (["verified star", "gold star", "get verified", "verification", "verify me"],
     "The golden ★ star is HERE's 'important page' mark. Apply from your profile or the Verification section in Settings — the owner reviews your page and your posts himself, and the bar is high: very interesting, very eye-catching, attention-grabbing. Make your page worth staring at first, then apply.",
     "/settings/profile", "Open Settings"),
    (["artist", "verified artist", "label", "record deal", "contract"],
     "HERE has its own record label. Switch on Artist mode in Settings and walk the artist door: watch the films, read the Playbook, sign that you understand the money comes from your fans and your work. Verified Artists get the gold plaque badge, track gifts and likes, and a scoreboard — 40,000 plays flags you CONTRACT-READY for a real deal where you keep 100% of your music rights.",
     "/artists", "See the artists"),
    (["upload music", "upload a song", "upload track", "my music", "profile music"],
     "Turn on Media HERE in Settings, pick Profile Music or Artist mode, and upload your tracks (MP3, M4A, OGG or WAV, up to 20 MB). Every upload asks whose music it is: yours (fans can gift it if you're a Verified Artist) or not yours (listening only — respect the artists). 10 tracks free, 50 with HERE Plus.",
     "/settings/profile", "Open Settings"),
    (["plus", "here plus", "membership", "subscription"],
     "HERE Plus is $4.99 a month: the ⭐ PLUS badge beside your name, 50 music tracks instead of 10, and more room to shine. Manage it on the Plus page.",
     "/plus", "See HERE Plus"),
    (["invite race", "100 dollars", "$100", "race", "prize"],
     "The $100 Invite Race runs October 30 to November 15, 2026. First place: $100 cash. Second: 1,000 tickets. Third: 500. But a join only counts when your invited person actually USES HERE — posts, joins a live, or uses tickets. Ghost signups don't count, and the winner is verified by the owner before anybody gets paid. Minimum 20 real joins for the money to pay out.",
     "/invite-race", "See the race board"),
    (["invite", "invite link", "referral", "invite friends"],
     "Your personal invite link is on the Invite page — share it anywhere. When somebody joins from your link, HERE knows they came through you (that's how the Invite Race counts you). Invited friends also earn you milestone rewards.",
     "/invite", "Get my invite link"),
    (["private", "privacy", "who can see", "only me", "friends only"],
     "Every post picks its own audience: Public, Friends, or Only me. Want the whole profile locked down? Settings → Privacy makes your profile friends-only — strangers see the door, not the room. The owner can always see his own house.",
     "/settings/profile", "Open Settings"),
    (["block", "silence", "mute someone", "report", "flag"],
     "Somebody bothering you? On their profile: 🚫 Block ends it completely — no friendship, no seeing each other, either way. 🔇 Silence just quiets them for you. And any post or comment can be flagged — flags go straight to the owner's desk; he alone decides remove, keep, or warn.",
     "/settings/profile", "Open Settings"),
    (["password", "forgot", "locked out", "reset", "cant log in", "can't log in", "login help"],
     "Locked out? Use the forgot-password door on the login page — a one-time reset link goes to the email on your account (check spam, links last one hour). Never got it? The support inbox is wearehereyouarehere@gmail.com — a human house, run by the owner himself.",
     None, None),
    (["background", "your spot", "profile background", "customize", "my look", "colors"],
     "Your Spot is your corner of HERE: Settings lets you set a full profile background, your own colors that follow you around the app, and how clear the glass over it sits. Make it yours — your background shows through the glass on your profile.",
     "/settings/profile", "Open Settings"),
    (["games", "showdown", "fortnite", "tournament"],
     "The Games corner runs showdowns and events — including Fortnite for Baddies, Friday October 30 at 11 PM. Games ride on Platforms: watch, join the room, and get in where you fit in.",
     "/games", "Open Games"),
    (["ads", "advertise", "run an ad", "promote my page", "promote"],
     "Ads on HERE cost $10 for 7 days and ride the feed where real people scroll. Pages can also run ads from their page menu. Your ad, your audience — no auction games.",
     None, None),
    (["earnings", "how much have i made", "how much did i earn", "scoreboard", "top earners"],
     "Your Earnings page shows every ticket you've earned, gift by gift, and where you stand. The scoreboard ranks creators by plays, likes, and gifts earned.",
     "/earnings", "See my earnings"),
    (["hearley", "who are you", "your name", "help bot", "robot"],
     "I'm Hearley — HERE's house helper. I live in the Help corner and I answer questions about how HERE works: posting, lives, tickets, cashing out, the Market, badges, all of it. Ask me anything about HERE and I'll give you the straight answer. For account trouble I can't fix, the support inbox is wearehereyouarehere@gmail.com.",
     "/faq", "Read the FAQ"),
    (["hello", "hi", "hey", "yo"],
     "Hey! 👋 I'm Hearley. Ask me how anything on HERE works — try 'how do I cash out?', 'how do I go live?', or 'what is a Verified Vendor?'",
     None, None),
    (["support", "contact", "email", "human", "real person", "owner", "help me"],
     "For anything I can't answer, the house support inbox is wearehereyouarehere@gmail.com — run by the owner himself. Tell him your HERE name and what's wrong, plain and simple, and he'll take it from there.",
     None, None),
]

HEARLEY_FALLBACK = (
    "That one's outside my map of HERE — and I won't guess at you. "
    "Try asking about posting, going live, tickets, cashing out, the Market, "
    "badges, or the Invite Race. Still stuck? The support inbox is "
    "wearehereyouarehere@gmail.com — the owner runs it himself.")


def hearley_answer(q):
    """Score the question against the knowledge base: a phrase hit
    weighs 3, a single-word hit weighs 1; best entry at score >= 2
    wins. Returns (answer, link, link_label)."""
    import re as _re
    ql = " " + _re.sub(r"[^a-z0-9$ ]", " ", (q or "").lower()) + " "
    ql = " " + " ".join(ql.split()) + " "
    best, best_score = None, 0
    for keys, ans, link, label in HEARLEY_KB:
        score = 0
        for k in keys:
            if " " in k or "$" in k:
                if k in ql:
                    score += 3
            elif f" {k} " in ql:
                score += 1
        if score > best_score:
            best, best_score = (ans, link, label), score
    if best and best_score >= 2:
        return best
    return (HEARLEY_FALLBACK, "/faq", "Read the FAQ")


@app.post("/api/hearley/ask")
async def hearley_ask(request: Request):
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    try:
        data = await request.json()
    except Exception:
        data = {}
    q = (data.get("q") or "").strip()[:500]
    if not q:
        return JSONResponse({"ok": False, "error": "empty"}, status_code=400)
    ans, link, label = hearley_answer(q)
    return JSONResponse({"ok": True, "answer": ans, "link": link,
                         "link_label": label})


def _payout_attempt(pid):
    """One guarded attempt to send payout request pid to the
    streamer's bank via Stripe Connect — the single money-out
    path behind BOTH the owner's Send button and the automatic
    cash-out flow (Ken's order Oct 30, 2026: a cash-out is the
    creator's own money, so there is no approval gate — a
    bank-ready request sends itself).

    Idempotency (the double-send guard): the request is claimed
    'pending' -> 'sending' in ONE guarded UPDATE before Stripe
    is called, and every settle path only acts on the claimed
    row, so no caller — button, sweep, or retry — can ever
    create two transfers for one request. Returns
    (outcome, info):
    - 'paid':     transfer created; status 'paid', method
                  'stripe', transfer id stored, the usual
                  0-delta cashout_paid audit row. Balances do
                  NOT move (tickets were held at request time).
    - 'refused':  Stripe definitively refused (HTTP error): the
                  claim reverts to 'pending' — no transfer
                  exists, so a later attempt is safe.
                  info['cls'] is the safe reason class.
    - 'unknown':  Stripe didn't answer (network/timeout): the
                  request stays 'sending' — stuck VISIBLE rather
                  than silently re-sendable.
    - 'gone':     not pending (already decided / missing).
    - 'notready': the streamer hasn't finished bank setup."""
    conn = db()
    pr = conn.execute(
        "SELECT pr.*, u.stripe_acct_id AS acct, "
        "u.payout_ready AS ready FROM payout_requests pr "
        "LEFT JOIN users u ON u.id=pr.user_id WHERE pr.id=?",
        (pid,)).fetchone()
    if not pr or pr["status"] != "pending":
        conn.close()
        return "gone", {}
    if not pr["ready"] or not pr["acct"]:
        conn.close()
        return "notready", {}
    cur = conn.execute(
        "UPDATE payout_requests SET status='sending' WHERE id=? "
        "AND status='pending'", (pid,))
    claimed = cur.rowcount == 1
    conn.commit()
    conn.close()
    if not claimed:
        return "gone", {}
    info = {"dollars_cents": pr["dollars_cents"]}
    transfer_id = None
    refused = None
    try:
        tr = _stripe_api("POST", "/v1/transfers", {
            "amount": str(pr["dollars_cents"]),
            "currency": "usd",
            "destination": pr["acct"],
            "description": f"HERE payout request {pid}",
        })
        transfer_id = tr.get("id")
    except RuntimeError as e:
        # _stripe_api only raises RuntimeError when Stripe ANSWERED
        # with an error — the refusal is definitive, no transfer.
        refused = e
    except Exception as e:
        # Network/timeout: no definitive answer (handled below).
        print(f"[payouts] transfer outcome UNKNOWN for request "
              f"{pid}: {e!r}")
    if refused is not None:
        conn = db()
        conn.execute(
            "UPDATE payout_requests SET status='pending' WHERE "
            "id=? AND status='sending'", (pid,))
        conn.commit()
        conn.close()
        cls = _stripe_err_class(refused)
        if "switched on" in cls:
            _connect_mark("blocked", cls)
        print(f"[payouts] transfer for request {pid} refused: "
              f"{refused}")
        info["cls"] = cls
        return "refused", info
    if not transfer_id:
        return "unknown", info
    stamp = now_iso()
    conn = db()
    conn.execute(
        "UPDATE payout_requests SET status='paid', method='stripe', "
        "stripe_transfer_id=?, decided_at=? WHERE id=? AND "
        "status='sending'", (transfer_id, stamp, pid))
    conn.execute(
        "INSERT INTO ticket_txns(user_id, kind, tickets, note, "
        "created_at) VALUES(?,?,?,?,?)",
        (pr["user_id"], "cashout_paid", 0,
         _money(pr["dollars_cents"]), stamp))
    conn.commit()
    conn.close()
    _connect_mark("ready")
    return "paid", info


def _payout_sweep():
    """Auto-pay pass: attempt every pending cash-out whose
    streamer is bank-ready, oldest first. Stops at the first
    balance refusal — if HERE's Stripe balance can't cover one,
    it can't cover the rest; the remainder retry on the next
    pass. Returns how many requests were paid."""
    conn = db()
    rows = conn.execute(
        "SELECT pr.id FROM payout_requests pr JOIN users u "
        "ON u.id = pr.user_id WHERE pr.status='pending' "
        "AND u.payout_ready AND u.stripe_acct_id IS NOT NULL "
        "AND u.stripe_acct_id != '' ORDER BY pr.id ASC").fetchall()
    conn.close()
    paid = 0
    for r in rows:
        outcome, info = _payout_attempt(r["id"])
        if outcome == "paid":
            paid += 1
        elif (outcome == "refused"
              and "not enough money" in (info.get("cls") or "")):
            break
    return paid


@app.post("/owner/payouts/{pid}/send")
async def owner_payout_send(request: Request, pid: int):
    """Owner backstop send: cash-outs normally send THEMSELVES
    automatically (see _payout_attempt); this button retries one
    by hand from the desk. Same guarded path, same outcomes."""
    err = require_owner(request)
    if err:
        return err
    outcome, info = _payout_attempt(pid)
    ajax = _oa(request)
    if outcome == "gone":
        if ajax:
            return _oa_fail("That cash-out was already decided.")
        return redir("/owner", "That cash-out was already decided.")
    if outcome == "notready":
        if ajax:
            return _oa_fail("That streamer hasn't finished bank "
                            "setup — pay this one by hand instead.")
        return redir("/owner",
                     "That streamer hasn't finished bank setup — "
                     "pay this one by hand instead.")
    if outcome == "refused":
        if ajax:
            return _oa_fail(f"Stripe refused the send: "
                            f"{info.get('cls')} — request {pid} is "
                            "still pending; you can pay by hand "
                            "instead.")
        return redir("/owner",
                     f"Stripe refused the send: {info.get('cls')} "
                     f"— request {pid} is still pending; you can "
                     "pay by hand instead.")
    if outcome == "unknown":
        if ajax:
            return _oa_fail(f"The send for request {pid} was "
                            "attempted but Stripe didn't answer — "
                            "check Stripe before doing anything "
                            "else. It's marked 'sending' now.")
        return redir("/owner",
                     f"The send for request {pid} was attempted but "
                     "Stripe didn't answer — check Stripe before "
                     "doing anything else. The request is marked "
                     "'sending' and can't be sent twice.")
    if ajax:
        return _oa_json(
            f"💸 Sent {_money(info['dollars_cents'])} to the "
            f"streamer's bank (request {pid}).",
            status="paid", note="💸 Stripe → bank")
    return redir("/owner",
                 f"💸 Sent {_money(info['dollars_cents'])} to the "
                 f"streamer's bank (request {pid}).")


@app.post("/owner/payouts/{pid}/hand-note")
async def owner_payout_hand_note(request: Request, pid: int):
    """Annotate a still-pending cash-out with a by-hand payment
    the owner made OUTSIDE the ticket rails (Ken paid Sunflwr
    $20 by hand on top of her queued $13, Oct 30). The request
    is NOT settled by this — its bank send still completes it —
    but the creator's pages show 'Paid by hand' instead of
    'queued', and the desk carries the note."""
    err = require_owner(request)
    if err:
        return err
    try:
        form = await request.form()
        note = (form.get("note") or "").strip()[:200]
    except Exception:
        note = ""
    conn = db()
    cur = conn.execute(
        "UPDATE payout_requests SET hand_note=? WHERE id=? AND "
        "status IN ('pending','sending')", (note, pid))
    conn.commit()
    conn.close()
    if cur.rowcount != 1:
        if _oa(request):
            return _oa_fail("That cash-out isn't waiting anymore "
                            "— no note added.")
        return redir("/owner",
                     "That cash-out isn't waiting anymore — "
                     "no note added.")
    if _oa(request):
        return _oa_json(
            f"💵 Noted on request {pid} — it shows as paid by "
            "hand, and its bank send still goes through.",
            note=note)
    return redir("/owner",
                 f"💵 Noted on request {pid} — it shows as paid "
                 "by hand, and its bank send still goes through.")


@app.post("/owner/payouts/{pid}/paid")
async def owner_payout_paid(request: Request, pid: int):
    """Owner marks a cash-out paid (he sent the money by hand). The
    tickets were already held at request time, so balances don't
    move here — only the audit marker is written."""
    err = require_owner(request)
    if err:
        return err
    conn = db()
    pr = conn.execute(
        "SELECT * FROM payout_requests WHERE id=?", (pid,)).fetchone()
    flipped = bool(pr and pr["status"] == "pending")
    if flipped:
        stamp = now_iso()
        conn.execute(
            "UPDATE payout_requests SET status='paid', "
            "method='manual', decided_at=? WHERE id=?", (stamp, pid))
        conn.execute(
            "INSERT INTO ticket_txns(user_id, kind, tickets, note, "
            "created_at) VALUES(?,?,?,?,?)",
            (pr["user_id"], "cashout_paid", 0,
             _money(pr["dollars_cents"]), stamp))
        conn.commit()
    conn.close()
    if _oa(request):
        if not flipped:
            return _oa_fail("That cash-out was already decided.")
        return _oa_json("Marked paid — by hand.",
                        status="paid", note="✋ paid by hand")
    return redir("/owner")


@app.post("/owner/payouts/{pid}/declined")
async def owner_payout_declined(request: Request, pid: int):
    """Owner declines a cash-out: the held tickets go straight back
    to the user's gift_earned (cashout_return), ready to re-request."""
    err = require_owner(request)
    if err:
        return err
    conn = db()
    pr = conn.execute(
        "SELECT * FROM payout_requests WHERE id=?", (pid,)).fetchone()
    flipped = bool(pr and pr["status"] == "pending")
    if flipped:
        stamp = now_iso()
        conn.execute(
            "UPDATE payout_requests SET status='declined', "
            "decided_at=? WHERE id=?", (stamp, pid))
        back = pr["tickets"] + (pr["fee_tickets"] or 0)
        conn.execute(
            "UPDATE users SET gift_earned = "
            "COALESCE(gift_earned,0) + ? WHERE id=?",
            (back, pr["user_id"]))
        conn.execute(
            "INSERT INTO ticket_txns(user_id, kind, tickets, "
            "created_at) VALUES(?,?,?,?)",
            (pr["user_id"], "cashout_return", back, stamp))
        if pr["fee_tickets"]:
            fee_row = conn.execute(
                "SELECT cents FROM label_fees WHERE "
                "payout_request_id=?", (pid,)).fetchone()
            if fee_row:
                _fund_set(conn, _fund_cents(conn)
                          - fee_row["cents"])
                conn.execute(
                    "DELETE FROM label_fees WHERE "
                    "payout_request_id=?", (pid,))
        conn.commit()
    conn.close()
    if _oa(request):
        if not flipped:
            return _oa_fail("That cash-out was already decided.")
        return _oa_json("Declined — the tickets went back to "
                        "their earnings.", status="declined")
    return redir("/owner")


@app.post("/owner/pwreset/{rid}/link", response_class=HTMLResponse)
async def owner_pwreset_link(request: Request, rid: int):
    """Owner-only: reveal the full reset URL for a QUEUED reset so
    it can be hand-sent to the address on the account. The raw token
    exists (token_plain) only while the row is queued; an emailed,
    hand-sent, used or expired row has nothing to reveal."""
    err = require_owner(request)
    if err:
        return err
    conn = db()
    r = conn.execute(
        "SELECT pr.*, u.email AS user_email FROM password_resets pr "
        "LEFT JOIN users u ON u.id=pr.user_id WHERE pr.id=?",
        (rid,)).fetchone()
    conn.close()
    if (not r or r["delivery"] != "queued" or r["used_at"]
            or not r["token_plain"]):
        if _oa(request):
            return _oa_fail("Nothing to show — that reset link is "
                            "gone (already sent, used, or expired).")
        return redir("/owner")
    exp = _parse_ts(r["expires_at"])
    if not exp or exp <= datetime.now(timezone.utc):
        if _oa(request):
            return _oa_fail("That reset link has expired.")
        return redir("/owner")
    if _oa(request):
        return _oa_json(
            "Reset link ready — email it to the address on the "
            "account, then tap Mark hand-sent.",
            link=_abs_url(request, f"/reset/{r['token_plain']}"),
            email=r["user_email"] or "",
            expires=fancy_time(r["expires_at"]))
    return render(request, "owner/pwreset_link.html", {
        "link": _abs_url(request, f"/reset/{r['token_plain']}"),
        "user_email": r["user_email"] or "",
        "expires_at": fancy_time(r["expires_at"])})


@app.post("/owner/pwreset/{rid}/hand-sent")
async def owner_pwreset_hand_sent(request: Request, rid: int):
    """Mark a queued reset as hand-sent (the owner emailed the link
    to the account address themselves). Flipping the state wipes
    the stored raw token \u2014 the link itself keeps working, since
    validation is by hash."""
    err = require_owner(request)
    if err:
        return err
    conn = db()
    conn.execute(
        "UPDATE password_resets SET delivery='hand_sent', sent_at=?, "
        "token_plain=NULL WHERE id=? AND delivery='queued' "
        "AND used_at IS NULL", (now_iso(), rid))
    conn.commit()
    conn.close()
    if _oa(request):
        return _oa_json("Marked hand-sent.", status="hand-sent")
    return redir("/owner")


# ---------------- health ----------------
@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True})
