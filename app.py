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
import os
import re
import secrets
import shutil
import smtplib
import sqlite3
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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import moderation

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
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))


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


def parse_hashtags(body):
    """Normalized hashtags from a post body, de-duplicated in
    first-seen order."""
    out = []
    for m in HASHTAG_RE.finditer(body or ""):
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
    c.execute("""CREATE TABLE IF NOT EXISTS live_shouts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        live_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
        body TEXT NOT NULL, tickets INTEGER NOT NULL,
        expires_at TEXT NOT NULL, created_at TEXT NOT NULL)""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_live_shouts_room "
              "ON live_shouts(live_id, id)")
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
    u = current_user(request)
    if u:
        touch_seen(u["id"])
        u = get_user(u["id"])  # refresh (last_seen changed)
        ctx["user"] = {"id": u["id"], "name": u["name"], "email": u["email"],
                       "bio": u.get("bio", ""),
                       "avatar": avatar_url(u.get("avatar", ""))}
        conn = db()
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
        ctx.setdefault("viewer_box_alpha", viewer_box_alpha_for(u))
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
    ctx.setdefault("flash", request.query_params.get("flash", ""))
    return templates.TemplateResponse(request, name, ctx)


def redir(url, flash=""):
    if flash:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}flash={flash}"
    return RedirectResponse(url, status_code=303)


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


_URL_RE = re.compile(r"https?://[^\s<>\"']+")
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
    bare URLs become clickable links and '@Name' mentions of this post's
    actual tags become profile/page links (Facebook-style). A single
    regex pass means inserted links are never re-matched, and a tag name
    inside a URL is never linkified separately. Longest tag names match
    first so 'Anna' wins over 'Ann'."""
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
    pat = re.compile("|".join(alts))

    def repl(m):
        s = m.group(0)
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
    """People you may know: non-friends ranked by profile views received in
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
        for m in re.findall(r"#(\w+)", r["body"] or ""):
            t = m.lower()
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
    if viewer["id"] == p["user_id"]:
        return True
    if not owner_on and blocked_either_way(viewer["id"], p["user_id"]):
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
    a = conn.execute("SELECT id,name,avatar,is_verified FROM users WHERE id=?",
                     (p["user_id"],)).fetchone()
    likes = conn.execute("SELECT COUNT(*) c FROM likes WHERE post_id=?",
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
        {"emoji": REACTION_EMOJI.get(r["kind"], "👍"), "count": r["c"]}
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
    return {
        "id": p["id"], "user_id": p["user_id"],
        "dom_id": ("post-share-%d" % share["id"]) if share else ("post-%d" % p["id"]),
        "author_name": a["name"] if a else "Deleted user",
        "author_avatar": avatar,
        "author_verified": bool(a["is_verified"]) if a else False,
        "kind": p["kind"], "body": p["body"] or "",
        "body_html": linkify_body(p["body"] or "", tagged_users, tagged_pages),
        "feeling": p["feeling"] or "", "link_url": p["link_url"] or "",
        "video_url": p["video_url"] or "",
        "media": "/uploads/" + media if media else "",
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
        "my_reaction": my_reaction, "reactions_top": reactions_top,
        "reaction_kinds": REACTIONS,
        "share_count": share_count,
        "privacy": priv,
        "pinned": bool(p.get("pinned")) and not share,
        "can_pin": bool(viewer and viewer["id"] == p["user_id"]
                         and not p.get("page_id") and not share),
        "can_share": bool(viewer and not share and priv == "public"),
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
                            (viewer["id"] == p["user_id"] or owner_on)),
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
    return render(request, "auth/signup.html",
                  {"error": request.query_params.get("error", ""),
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
    resp = redir("/home" if onboarded else "/welcome")
    if credited or request.cookies.get("here_invite"):
        resp.delete_cookie("here_invite", path="/")
    return set_session_cookie(resp, token, SESSION_COOKIE)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if current_user(request):
        return redir("/home")
    return render(request, "auth/login.html",
                  {"error": request.query_params.get("error", "")})


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
    resp = redir("/home")
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
    if not p or p.get("shared_post_id") or not author \
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
    conn.close()
    media = p.get("media") or ""
    media_url = "/uploads/" + media if media else ""
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
    if media_kind == "photo" and media_url:
        og_image = _abs_url(request, media_url)
    elif author_ctx["avatar"]:
        og_image = _abs_url(request, author_ctx["avatar"])
    return render(request, "public_post.html", {
        "author": author_ctx,
        "body": p.get("body") or "",
        "media_url": media_url, "media_kind": media_kind,
        "embed_url": embed_url, "music_embed": music_embed,
        "like_count": like_count, "comment_count": comment_count,
        "created": fancy_time(p.get("created_at")),
        "excerpt": (p.get("body") or "")[:160],
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
    og_image = (_abs_url(request, host_ctx["avatar"])
                if host_ctx["avatar"] else "")
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
                "following_count": following_count(user_id)}
    og_image = (_abs_url(request, prof_ctx["avatar"])
                if prof_ctx["avatar"] else "")
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
    count = len(invitees)
    link = _abs_url(request, "/join/" + code)
    return render(request, "invite.html", {
        "invite_link": link,
        "qr_src": ("https://api.qrserver.com/v1/create-qr-code/"
                   "?size=180x180&data=" + quote(link, safe="")),
        "invitees": invitees, "count": count,
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
        scored.append({"post_id": p["id"],
                       "video_url": "/uploads/" + p["media"],
                       "poster_url": "/uploads/" + p["media"],
                       "media": "/uploads/" + p["media"],
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
    })


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


@app.get("/home", response_class=HTMLResponse)
async def home(request: Request):
    user, err = require_login(request)
    if err:
        return err
    record_activity(user["id"])
    owner_on = is_owner(request)
    posts = _home_posts(user, owner_on)
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
    return render(request, "home.html",
                  {"posts": posts, "contacts": contacts,
                   "stories_tray": stories_tray(user["id"]),
                   "live_now": live_now_for(user["id"], owner_on),
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
    cap = POST_VIDEO_MAX_MB if _mext in VID_EXTS else 0
    if poll_options is not None:
        media = ""  # polls are text-only by design; any file is ignored
    else:
        try:
            media = save_upload(media_file, "posts", allowed, max_mb=cap)
        except ValueError as e:
            return None, str(e)

    conn = db()
    cur = conn.execute(
        "INSERT INTO posts(user_id, page_id, kind, body, feeling, link_url, "
        "video_url, media, privacy, music_video_id, music_title, "
        "music_artist, music_thumb, music_start, created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user["id"], page_id, kind, body, feeling, link_url, video_url,
         media, privacy,
         music["video_id"] if music else "",
         music["title"] if music else "",
         music["artist"] if music else "",
         music["thumb"] if music else "",
         music_start if music else 0,
         now_iso()))
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
                "SELECT id,name,avatar FROM users WHERE id=?",
                (uid,)).fetchone()
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
    conn.close()
    pc = post_card(dict(p), user, owner_on)
    pc["comments"] = comments
    return render(request, "post.html", {"post": pc})


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
        items.append({
            "post_id": pid,
            "video_url": "/uploads/" + p["media"],
            "poster_url": "/uploads/" + p["media"],
            "author": {
                "id": p["user_id"],
                "name": a["name"] if a else "Deleted user",
                "avatar": avatar_url(a["avatar"]) if a else "",
                "is_friend": p["user_id"] in friends,
                "following": p["user_id"] in following,
                "is_self": p["user_id"] == viewer["id"],
            },
            "body": p["body"] or "",
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
    return JSONResponse({"ok": True, "tag": tag,
                         "views": _views_feed(user, tag, offset, limit)})


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


def stories_tray(viewer_id):
    """Tray entries for viewer + friends with active stories. Self first, then newest."""
    prune_stories()
    conn = db()
    now = now_iso()
    tray, others = [], []
    for fid in friend_id_set(viewer_id) | {viewer_id}:
        u = conn.execute("SELECT id,name,avatar FROM users WHERE id=?", (fid,)).fetchone()
        if not u:
            continue
        ss = conn.execute(
            "SELECT id, created_at FROM stories WHERE user_id=? AND expires_at > ? "
            "ORDER BY created_at DESC", (fid, now)).fetchall()
        if not ss:
            continue
        sids = [s["id"] for s in ss]
        ph = ",".join("?" * len(sids))
        seen = {r["story_id"] for r in conn.execute(
            f"SELECT story_id FROM story_views WHERE user_id=? AND story_id IN ({ph})",
            (viewer_id, *sids)).fetchall()}
        entry = {
            "user_id": u["id"], "name": u["name"],
            "avatar": avatar_url(u["avatar"]),
            "is_self": fid == viewer_id,
            "first_id": ss[-1]["id"], "count": len(ss),
            "all_viewed": all(s in seen for s in sids),
            "latest": ss[0]["created_at"] or "",
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
    if not s or s["expires_at"] < now_iso():
        return False
    if s["user_id"] == user["id"]:
        return True
    return s["user_id"] in friend_id_set(user["id"])


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
    for fid in friend_id_set(viewer_id) | {viewer_id}:
        u = conn.execute("SELECT id,name,avatar FROM users WHERE id=?",
                         (fid,)).fetchone()
        if not u:
            continue
        ss = conn.execute(
            "SELECT * FROM stories WHERE user_id=? AND expires_at > ? "
            "ORDER BY created_at ASC", (fid, now)).fetchall()
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


@app.get("/stories/new", response_class=HTMLResponse)
async def story_new_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    return render(request, "story_new.html", {})


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
    try:
        media = save_upload(form.get("media"), "stories", STORY_EXTS)
    except ValueError as e:
        return redir("/stories/new", str(e))
    if not media:
        return redir("/stories/new", "Choose a photo or short video.")
    mins = _story_lifetime_minutes(form)
    conn = db()
    cur = conn.execute(
        "INSERT INTO stories(user_id, media, caption, created_at, expires_at, "
        "overlay_text, overlay_color, overlay_pos, music_kind, music_ref, "
        "music_title, music_artist, music_thumb, music_start, music_len, "
        "music_volume, music_mute_orig, ama) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user["id"], media, caption, now_iso(), story_expires_at(mins),
         overlay_text, overlay_color, overlay_pos,
         "youtube" if music else None,
         music["video_id"] if music else "",
         music["title"] if music else "",
         music["artist"] if music else "",
         music["thumb"] if music else "",
         music_start, music_len, music_volume, music_mute_orig,
         story_ama))
    sid = cur.lastrowid
    if story_poll:
        conn.execute(
            "INSERT INTO story_polls(story_id, question, a_text, b_text) "
            "VALUES(?,?,?,?)", (sid, story_poll[0], story_poll[1],
                                story_poll[2]))
    conn.commit()
    conn.close()
    flag_item("story", sid, user["id"], user["name"],
              " ".join(x for x in (caption, overlay_text) if x))
    return redir("/home", _lifetime_flash(mins))


@app.get("/stories/queue")
async def stories_queue(request: Request):
    """JSON queue for the full-screen viewer (own + friends' stories)."""
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    return JSONResponse({"ok": True, "groups": _story_queue(user["id"])})


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
    if row and os.path.isfile(os.path.join(UPLOAD_DIR, row["story_media"])):
        return row["story_media"]
    src_rel = s["media"] or ""
    if not src_rel:
        return ""
    src = os.path.join(UPLOAD_DIR, src_rel)
    if not os.path.isfile(src):
        return ""
    dest_rel = f"story_replies/{s['id']}-{os.path.basename(src_rel)}"
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
    conn.execute("DELETE FROM posts WHERE id=?", (pid,))
    conn.commit()
    conn.close()


@app.post("/post/{pid}/delete")
async def delete_post(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    p = conn.execute("SELECT user_id FROM posts WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not p:
        return back(request)
    if p["user_id"] != user["id"] and not is_owner(request):
        return back(request, "/home")
    _delete_post(pid)
    return back(request, "/home")


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
BOX_OPACITY_MIN, BOX_OPACITY_MAX = 30, 100
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

    users.box_opacity is a solidity percent (30-100). Returns "" when
    the viewer is at (or defaults to) 100 — fully solid, the default
    look with no glass overrides at all — otherwise the alpha as a
    compact decimal string (55 -> "0.55", 30 -> "0.3").
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
    prows = conn.execute(
        "SELECT * FROM posts WHERE user_id=? AND page_id IS NULL "
        "ORDER BY pinned DESC, created_at DESC LIMIT 100", (uid,)).fetchall()
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
        "streak": current_streak(uid),
        "contest_winner": contest_winner_user_id() == uid,
        "viewer_follows": bool(user["id"] != uid
                               and is_following(user["id"], uid)),
        "is_self": user["id"] == uid,
        "req_status": status,
        "tap_count": tcount,
        "is_verified": bool(prof.get("is_verified")),
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
    }
    posts = []
    if not locked:
        for r in prows:
            d = dict(r)
            if (d.get("shared_post_id") or d["id"]) in hidden:
                continue
            card = post_card(d, user, owner_on)
            if card:
                posts.append(card)
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
            "SELECT l.*, u.name AS sname, u.avatar AS savatar "
            "FROM market_listings l JOIN users u ON u.id=l.seller_id "
            "WHERE l.seller_id=? AND l.status!='deleted' "
            "ORDER BY (l.status='sold'), l.created_at DESC LIMIT 6",
            (uid,)).fetchall()
        selling_count = _mconn.execute(
            "SELECT COUNT(*) c FROM market_listings WHERE seller_id=? "
            "AND status!='deleted'", (uid,)).fetchone()["c"]
        _mconn.close()
        selling = [_listing_card(r) for r in _mrows]
    return render(request, "profile.html",
                  {"profile": profile_ctx, "posts": posts,
                   "album_photos": album_photos,
                   "album_count": album_count,
                   "selling": selling, "selling_count": selling_count,
                   "page_bg": profile_ctx["background"],
                   "viewer_spot_style": "", "viewer_spot_classes": ""})


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
           "spot_classes": spot_classes}
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
    conn.close()
    return render(request, "profile_edit.html", {
        "muted_people": muted_people,
        "blocked_people": blocked_people,
        "profile": {"id": user["id"], "name": user["name"],
                    "bio": user.get("bio", ""),
                    "avatar": avatar_url(user.get("avatar", "")),
                    "background": avatar_url(user.get("background", "")),
                    "is_verified": bool(user.get("is_verified")),
                    "verification_status": user.get("verification_status") or "none",
                    "status_mode": mode,
                    "private_profile": 1 if user.get("private_profile")
                    else 0,
                    "inbox_status": 1 if user.get("inbox_status") is None
                    else (1 if user.get("inbox_status") else 0),
                    "status_label": status_label,
                    "spot_name": user.get("spot_name") or "",
                    "spot_text_color": user.get("spot_text_color") or "",
                    "spot_card_color": user.get("spot_card_color") or "",
                    "spot_card_opacity": user.get("spot_card_opacity"),
                    "box_opacity": user.get("box_opacity")}})


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
    boxes are on THEIR screen, everywhere on HERE, and changes
    nothing for anybody else. A non-number changes nothing.
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
                     "Box transparency needs a number from 30 to 100.")
    op = max(BOX_OPACITY_MIN, min(BOX_OPACITY_MAX, op))
    conn = db()
    conn.execute("UPDATE users SET box_opacity=? WHERE id=?",
                 (op, user["id"]))
    conn.commit()
    conn.close()
    return redir("/settings/profile",
                 f"Boxes are now {op}% solid on your screen.")


def _delete_upload(rel_path):
    """Best-effort removal of a stored upload (e.g. replaced backgrounds)."""
    try:
        if rel_path and not rel_path.startswith(("http://", "https://", "/")):
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
                     listing_title=None, listing_price_cents=None):
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
        "listing_price_cents) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (sender["id"], receiver_id, body, now_iso(), story_id, story_media,
         1 if ama else 0, listing_id, listing_title, listing_price_cents))
    mid = cur.lastrowid
    conn.commit()
    conn.close()
    flag_item("message", mid, sender["id"], sender["name"], body)
    notify(receiver_id, "message", f"New message from {sender['name']}.",
           f"/messages/{sender['id']}", actor_id=sender["id"])
    preview = body if len(body) <= 80 else body[:79] + "…"
    if notifications_allowed(receiver_id, sender["id"]):
        send_push(receiver_id, f"💬 {sender['name']}", preview,
                  f"/messages/{sender['id']}", tag=f"msg-{sender['id']}")
    return mid


def _msg_preview(body, story_id, ama=0, listing_id=None):
    """Inbox/Sent preview text for one message row. Story-linked
    messages announce themselves: a reply reads "📖 Story reply: …"
    and a reaction (body is exactly one reaction emoji) reads
    "📖 {emoji} reacted to a story". A story AMA question reads
    "❓ Question about your story: …" (checked first — an AMA message
    is story-linked too, but the question label wins). Plain messages
    preview as-is."""
    b = (body or "").strip()
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
                     r["listing_id"] if "listing_id" in r.keys() else None),
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
            "SELECT m.body, m.created_at, m.story_id, m.ama, m.listing_id FROM messages m WHERE "
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
                last["listing_id"] if "listing_id" in last.keys() else None)
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
             "listing": None}
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
        "host_seen_at<?", (cutoff,)).fetchall()
    for row in stale:
        _end_live_row(conn, dict(row))
    if stale:
        conn.commit()


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
    return out


def _live_person(uid):
    u = get_user(uid) or {}
    return {"id": uid, "name": u.get("name", "Deleted user"),
            "avatar": avatar_url(u.get("avatar", ""))}


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
        out.append({"id": live["id"], "host_id": live["host_id"],
                    "name": host["name"], "avatar": host["avatar"],
                    "title": live["title"],
                    "viewers": _live_viewer_count(conn, live["id"]),
                    "panel_count": 1 + len(_live_active_members(conn, live["id"])),
                    "created_at": live["created_at"],
                    "host_level": lvl["level"],
                    "host_level_label": lvl["label"],
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
    {"urls": "turn:openrelay.metered.ca:80",
     "username": "openrelayproject", "credential": "openrelayproject"},
    {"urls": "turn:openrelay.metered.ca:443",
     "username": "openrelayproject", "credential": "openrelayproject"},
    {"urls": "turn:openrelay.metered.ca:443?transport=tcp",
     "username": "openrelayproject", "credential": "openrelayproject"},
]


def get_ice_servers():
    """ICE servers for /ice.json: the HERE_ICE_SERVERS env override
    (JSON) when set AND valid (a non-empty list of dicts each with a
    'urls'), else the built-in STUN + Open Relay defaults."""
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
    return [dict(e) for e in DEFAULT_ICE_SERVERS]


@app.get("/ice.json")
async def ice_json(request: Request):
    """Public on purpose: the STUN/TURN values are the same community
    credentials already shipped in the page templates; the live and
    call clients fetch this before creating peer connections."""
    return JSONResponse({"iceServers": get_ice_servers()})


def _lives_sections(rows):
    """Group /lives rows by host streamer level (Build P-GIFTS):
    👑 VIP Lives (L5), 🔥 Star Lives (L3–L4), 🌱 New & Rising
    (L1–L2). Rows keep their _lives_page_list order inside each
    section; every live lands in exactly one section; empty
    sections are omitted."""
    groups = [
        ("vip", "👑 VIP Lives",
         [r for r in rows if (r.get("host_level") or 1) >= 5]),
        ("star", "🔥 Star Lives",
         [r for r in rows if 3 <= (r.get("host_level") or 1) <= 4]),
        ("new", "🌱 New & Rising",
         [r for r in rows if (r.get("host_level") or 1) <= 2]),
    ]
    return [{"key": k, "title": t, "lives": rs}
            for k, t, rs in groups if rs]


@app.get("/lives", response_class=HTMLResponse)
async def lives_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    rows = _lives_page_list(user, is_owner(request))
    return render(request, "lives.html",
                  {"lives": rows, "sections": _lives_sections(rows)})


@app.get("/lives.json")
async def lives_json(request: Request):
    user = current_user(request)
    if not user:
        return JSONResponse({"ok": False, "error": "login"},
                            status_code=401)
    rows = _lives_page_list(user, is_owner(request))
    return JSONResponse({"ok": True, "lives": [
        {"id": r["id"], "host_id": r["host_id"],
         "host_name": r["name"], "host_avatar": r["avatar"],
         "title": r["title"], "viewers": r["viewers"],
         "panel_count": r["panel_count"], "following": r["following"],
         "host_level": r.get("host_level") or 1,
         "host_level_label": r.get("host_level_label") or "",
         "is_self": r["is_self"]} for r in rows]})


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
    conn = db()
    _sweep_lives(conn)
    # One live per host: starting a new one ends the old one first.
    old = conn.execute(
        "SELECT * FROM live_sessions WHERE host_id=? AND status='live'",
        (user["id"],)).fetchone()
    if old:
        _end_live_row(conn, dict(old))
    stamp = now_iso()
    cur = conn.execute(
        "INSERT INTO live_sessions(host_id, title, status, created_at, "
        "host_seen_at, like_count, panel_order) "
        "VALUES(?,?,'live',?,?,0,?)",
        (user["id"], title, stamp, stamp, json.dumps([user["id"]])))
    lid = cur.lastrowid
    conn.commit()
    conn.close()
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
    return JSONResponse({"ok": True, "id": lid, "url": f"/live/{lid}"})


@app.get("/live/{lid}", response_class=HTMLResponse)
async def live_page(request: Request, lid: int):
    user, err = require_login(request)
    if err:
        return err
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
    if live["status"] != "live":
        conn.close()
        return render(request, "live.html", {
            "mode": "ended", "live": live, "host": host,
            "hide_golive": True})
    if not _live_can_watch(user, live, owner_on=owner_on):
        conn.close()
        # Same face as an ended live: nothing about the room leaks.
        return render(request, "live.html", {
            "mode": "unavailable", "live": live, "host": host,
            "hide_golive": True})
    panel_ids = _live_panel_ids(conn, live)
    conn.close()
    # Boot compat: 'guest' stays the first non-host panelist (or
    # None); the watch page's state poll carries the full panel.
    guest = _live_person(panel_ids[1]) if len(panel_ids) > 1 else None
    if user["id"] == live["host_id"]:
        role = "host"
    elif user["id"] in panel_ids:
        role = "guest"
    else:
        role = "watcher"
    boot = {"id": lid, "me": user["id"], "role": role,
            "host": host, "guest": guest, "title": live["title"],
            "created_at": live["created_at"],
            # Live Tickets: my spendable balance for the chip/gift
            # sheet, and the gift catalog for the tray (prices are
            # still enforced server-side from GIFT_CATALOG).
            "tickets": (dict(user).get("tickets_balance") or 0),
            "gifts": [GIFT_CATALOG[k] for k in GIFT_ORDER],
            "shout_cost": SHOUT_COST_TICKETS}
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
    return render(request, "live.html", {
        "mode": "live", "live": live, "host": host, "guest": guest,
        "role": role, "more_lives": more_lives,
        "browse_posts": browse_posts,
        "boot": json.dumps(boot).replace("</", "<\\/"),
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
    return redir("/owner")


@app.post("/live/{lid}/heartbeat")
async def live_heartbeat(request: Request, lid: int):
    """Presence: the host's heartbeat keeps the live from being swept;
    a watcher's/guest's heartbeat is their live_viewers row (viewer
    counts read rows fresh within LIVE_VIEWER_WINDOW)."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    conn = db()
    stamp = now_iso()
    if user["id"] == live["host_id"]:
        conn.execute(
            "UPDATE live_sessions SET host_seen_at=? WHERE id=?",
            (stamp, lid))
    else:
        conn.execute(
            "INSERT INTO live_viewers(live_id, user_id, joined_at, "
            "last_seen_at) VALUES(?,?,?,?) ON CONFLICT(live_id, user_id) "
            "DO UPDATE SET last_seen_at=excluded.last_seen_at, "
            "left_at=NULL", (lid, user["id"], stamp, stamp))
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
        "SELECT lc.*, u.name, u.avatar FROM live_comments lc "
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
    panel = []
    for pid in panel_ids:
        person = _live_person(pid)
        mrow = member_map.get(pid)
        panel.append({
            "user_id": pid, "name": person["name"],
            "avatar": person["avatar"],
            "role": "host" if pid == live["host_id"] else "guest",
            "admin": pid in admin_ids,
            "muted": bool(mrow["muted"]) if mrow else False})
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
        out["pending_requests"] = [
            {"id": r["id"], "user_id": r["user_id"],
             "name": r["name"] or "Deleted user",
             "avatar": avatar_url(r["avatar"] or ""),
             "created_at": r["created_at"],
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
                   - timedelta(seconds=15)).isoformat()
    out["recent_gifts"] = [
        {"id": r["id"], "sender_id": r["sender_id"],
         "sender_name": r["sender_name"] or "Deleted user",
         "sender_rank": _rank_of(r["sender_id"]),
         "gift_key": r["gift_key"],
         **_gift_public(r["gift_key"], r["tickets"])}
        for r in conn.execute(
            "SELECT g.*, u.name AS sender_name FROM live_gifts g "
            "LEFT JOIN users u ON u.id=g.sender_id WHERE g.live_id=? "
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
        "SELECT lc.*, u.name, u.avatar FROM live_comments lc "
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


# ---------------- live tickets: gifts + shout-out pins ----------------
# The money-moving pair of the gift economy. Both endpoints are
# single-transaction and guarded: the spendable balance is debited
# with a WHERE balance>=cost UPDATE whose rowcount is checked, so a
# balance can never go negative and a double-tap can't double-spend.
# The host's side lands in gift_earned (the earned ledger); every
# move writes its ticket_txns rows in the same transaction.


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
    """Send a character gift to the host of a live. Body: {gift_key}.
    Prices come ONLY from GIFT_CATALOG (server-side). Atomic: sender
    spendable -= cost (guarded), host earned += cost, one live_gifts
    row + two ledger rows, all in one commit."""
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
    cost = gift["tickets"]
    conn = db()
    if not _live_present(conn, lid, user["id"], host_id):
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Join the live before sending gifts."},
            status_code=403)
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
        (cost, cost, host_id))
    stamp = now_iso()
    cur = conn.execute(
        "INSERT INTO live_gifts(live_id, sender_id, host_id, "
        "gift_key, tickets, created_at) VALUES(?,?,?,?,?,?)",
        (lid, user["id"], host_id, gift_key, cost, stamp))
    gift_id = cur.lastrowid
    conn.execute(
        "INSERT INTO ticket_txns(user_id, kind, tickets, live_id, "
        "gift_key, created_at) VALUES(?,?,?,?,?,?)",
        (user["id"], "gift_send", -cost, lid, gift_key, stamp))
    conn.execute(
        "INSERT INTO ticket_txns(user_id, kind, tickets, live_id, "
        "gift_key, created_at) VALUES(?,?,?,?,?,?)",
        (host_id, "gift_receive", cost, lid, gift_key, stamp))
    conn.commit()
    bal = conn.execute(
        "SELECT tickets_balance FROM users WHERE id=?",
        (user["id"],)).fetchone()["tickets_balance"]
    conn.close()
    return JSONResponse({"ok": True, "balance": bal or 0,
                         "gift_id": gift_id})


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
    conn.commit()
    bal = conn.execute(
        "SELECT tickets_balance FROM users WHERE id=?",
        (user["id"],)).fetchone()["tickets_balance"]
    conn.close()
    flag_item("live_shout", shout_id, user["id"], user["name"], body)
    return JSONResponse({"ok": True, "balance": bal or 0,
                         "shout_id": shout_id,
                         "expires_at": expires})


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
                     "error": f"Take a breather — you can ask again "
                              f"in {left}s."}, status_code=429)
    cur = conn.execute(
        "INSERT INTO live_join_requests(live_id, user_id, status, "
        "created_at, kind) VALUES(?,?,'pending',?,'request')",
        (lid, user["id"], now_iso()))
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "request": {"id": rid,
                                                 "status": "pending"}})


@app.post("/live/{lid}/join-decide")
async def live_join_decide(request: Request, lid: int):
    """Host OR an admin of this live settles a kind='request' join
    request. Approving puts the requester on the panel (live_members
    + panel_order) unless the panel already has 8 guests."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    conn = db()
    if live["host_id"] != user["id"] \
            and not _live_is_admin(conn, lid, user["id"]):
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Only the host or an admin can decide."},
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
    if approve and not _live_add_member(conn, live, req["user_id"]):
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "Panel is full (8 guests max)."},
            status_code=409)
    conn.execute(
        "UPDATE live_join_requests SET status=?, decided_at=? "
        "WHERE id=?",
        ("approved" if approve else "declined", now_iso(), rid))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


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
    caller_admin = _live_is_admin(conn, lid, user["id"])
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
    caller_admin = _live_is_admin(conn, lid, user["id"])
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
            and not _live_is_admin(conn, lid, user["id"]):
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
            f"private_profile "
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
    return render(request, "page.html",
                  {"page": page_ctx(dict(pg), user), "posts": posts,
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

def _gift(key, name, tickets, category):
    return {"key": key, "name": name, "tickets": tickets,
            "category": category, "img": f"/static/gifts/{key}.webp"}


# Build P-GIFTS: the emoji cast is retired. The catalog is Ken's own
# artwork now — 67 gifts cut from his three sheets into
# static/gifts/<key>.webp (💰 Money ×32, ❤️ Love ×24, 👑 Luxury
# ×11; the Luxury sheet contributes only its no-brand-logo designs,
# Ken's call). Prices are server-side ONLY, as ever. neon_heart is
# the one FREE gift (0 tickets): it records a txn but moves no
# ledger and earns the host nothing.
_GIFT_ROWS = [
    # --- 💰 Money (sheet 1) ---
    ("neon_heart", "Neon Heart", 0, "money"),
    ("money_bag", "Money Bag", 1, "money"),
    ("flying_cash", "Flying Cash", 5, "money"),
    ("gift_stack", "Gift Stack", 10, "money"),
    ("here_shades", "HERE Shades", 15, "money"),
    ("here_rocket", "HERE Rocket", 25, "money"),
    ("money_duck", "Money Duck", 35, "money"),
    ("money_bear", "Money Bear", 50, "money"),
    ("money_buddy", "Money Buddy", 65, "money"),
    ("cash_gremlin", "Cash Gremlin", 75, "money"),
    ("money_heart", "Money Heart", 100, "money"),
    ("money_tree", "Money Tree", 125, "money"),
    ("golden_mic", "Golden Mic", 150, "money"),
    ("megaphone", "HERE Megaphone", 200, "money"),
    ("boss_face", "Boss Face", 250, "money"),
    ("heart_hustler", "Heart Hustler", 300, "money"),
    ("gold_kicks", "Gold Kicks", 400, "money"),
    ("money_panda", "Money Panda", 500, "money"),
    ("angel_bag", "Angel Bag", 600, "money"),
    ("champagne_bucket", "Champagne Bucket", 750, "money"),
    ("dab_king", "Dab King", 900, "money"),
    ("here_diamond", "HERE Diamond", 1000, "money"),
    ("diamond_dollar", "Diamond Dollar", 1200, "money"),
    ("here_crown", "HERE Crown", 1500, "money"),
    ("burning_cash", "Burning Cash", 1800, "money"),
    ("the_vault", "The Vault", 2000, "money"),
    ("crown_jewel_bag", "Crown Jewel Bag", 2500, "money"),
    ("here_juice", "HERE Juice", 3000, "money"),
    ("g_wagon", "G-Wagon", 4000, "money"),
    ("midnight_rider", "Midnight Rider", 5000, "money"),
    ("the_cutlass", "The Cutlass", 7500, "money"),
    ("royal_fortune", "Royal Fortune", 10000, "money"),
    # --- ❤️ Love (sheet 3) ---
    ("single_rose", "Single Rose", 1, "love"),
    ("candle_glow", "Candle Glow", 5, "love"),
    ("heart_balloons", "Heart Balloons", 10, "love"),
    ("love_letter", "Love Letter", 15, "love"),
    ("hug_heart", "Hug Heart", 20, "love"),
    ("i_love_you", "I Love You", 25, "love"),
    ("infinity_hearts", "Infinity Hearts", 35, "love"),
    ("gift_boxes", "Gift Boxes", 50, "love"),
    ("hoodie_teddy", "Hoodie Teddy", 65, "love"),
    ("white_teddy", "White Teddy", 75, "love"),
    ("mystery_gift", "Mystery Gift", 90, "love"),
    ("rose_dome", "Rose Dome", 100, "love"),
    ("panda_love", "Panda Love", 125, "love"),
    ("crowned_bears", "Crowned Bears", 150, "love"),
    ("rose_box", "Rose Box", 180, "love"),
    ("forever_roses_bucket", "Forever Roses", 200, "love"),
    ("heart_bouquet_box", "Heart Bouquet Box", 250, "love"),
    ("love_script", "Love Script", 300, "love"),
    ("dripping_heart", "Dripping Crown Heart", 400, "love"),
    ("couple_goals", "Couple Goals", 500, "love"),
    ("crowned_heart", "Crowned Heart", 750, "love"),
    ("rose_heart_wreath", "Rose Heart Wreath", 1000, "love"),
    ("teddy_king", "Teddy King", 1250, "love"),
    ("heart_of_roses", "Heart of Roses", 1500, "love"),
    # --- 👑 Luxury (sheet 2, no-logo keepers only) ---
    ("forever_roses", "Forever Roses Box", 1000, "luxury"),
    ("cigar_box", "Cigar Box", 1500, "luxury"),
    ("diamond_rings", "Diamond Rings", 2500, "luxury"),
    ("gift_card", "Gift Card", 3000, "luxury"),
    ("ceo_chain", "CEO Chain", 3500, "luxury"),
    ("grillz_case", "Grillz Case", 4000, "luxury"),
    ("bulldog_boss", "Bulldog Boss", 5000, "luxury"),
    ("luxury_lounge", "Luxury Lounge", 6000, "luxury"),
    ("private_jet", "Private Jet", 12000, "luxury"),
    ("yacht_night", "Yacht Night", 15000, "luxury"),
    ("mansion_sunset", "Mansion Sunset", 20000, "luxury"),
]
GIFT_CATALOG = {k: _gift(k, n, t, c) for k, n, t, c in _GIFT_ROWS}
GIFT_ORDER = [k for k, _n, _t, _c in _GIFT_ROWS]
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
                "img": g["img"], "anim": _gift_anim_tier(t)}
    emoji, name = LEGACY_GIFT_LABELS.get(gift_key, ("🎁", "Gift"))
    t = tickets if tickets is not None else 0
    return {"key": gift_key, "emoji": emoji, "name": name,
            "tickets": t, "category": "", "img": "",
            "anim": _gift_anim_tier(t)}


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
        face = (g["emoji"] + " ") if g["emoji"] else ""
        return f"Received {face}{g['name']} (+{t['tickets']} 🎟️)"
    if k == "shout":
        return f"Pinned a shout-out (−{abs(t['tickets'])} 🎟️)"
    if k == "cashout_hold":
        return (f"Cash-out requested — {abs(t['tickets'])} 🎟️ held "
                "for approval")
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
    return render(request, "tickets.html", {
        "balance": balance, "earned": earned,
        "earned_dollars": _money(_earned_dollars(earned)),
        "cashout_min": CASHOUT_MIN_TICKETS,
        "cashout_min_dollars": _money(
            _earned_dollars(CASHOUT_MIN_TICKETS)),
        "can_cashout": earned >= CASHOUT_MIN_TICKETS and not pending,
        "pending": ({"tickets": pending["tickets"],
                     "dollars": _money(pending["dollars_cents"]),
                     "when": fancy_time(pending["created_at"])}
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
        "payouts_history": payouts_history})


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
    owner decides in the owner panel — paying changes nothing more;
    declining returns them (gift_earned += n, cashout_return)."""
    user, err = require_login(request)
    if err:
        return err
    try:
        form = await request.form()
        note = (form.get("payout_note") or "").strip()[:300]
    except Exception:
        note = ""
    if not note:
        return redir("/tickets",
                     "Tell us where to send it (Cash App, bank, etc.).")
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
                     "You already have a cash-out waiting for "
                     "approval.")
    n = earned
    cur = conn.execute(
        "UPDATE users SET gift_earned = gift_earned - ? WHERE id=? "
        "AND gift_earned=?", (n, user["id"], n))
    if cur.rowcount != 1:
        conn.rollback()
        conn.close()
        return redir("/tickets",
                     "Your earnings changed — try again.")
    stamp = now_iso()
    conn.execute(
        "INSERT INTO payout_requests(user_id, tickets, "
        "dollars_cents, payout_note, created_at) VALUES(?,?,?,?,?)",
        (user["id"], n, _earned_dollars(n), note, stamp))
    conn.execute(
        "INSERT INTO ticket_txns(user_id, kind, tickets, created_at) "
        "VALUES(?,?,?,?)", (user["id"], "cashout_hold", -n, stamp))
    conn.commit()
    conn.close()
    return redir("/tickets",
                 "Cash-out sent for approval 💰 — the owner sends it "
                 "and marks it paid.")


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
        return "⏳ Waiting for approval"
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
        "created": fancy_time(row["created_at"]),
        "seller_name": row["sname"] if "sname" in keys else "",
        "seller_avatar": (avatar_url(row["savatar"])
                          if "savatar" in keys and row["savatar"] else ""),
        "saved": bool(saved),
    }


def _get_listing(conn, lid):
    return conn.execute(
        "SELECT l.*, u.name AS sname, u.avatar AS savatar "
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
    order = {"new": "l.created_at DESC",
             "price_asc": "l.price_cents ASC, l.created_at DESC",
             "price_desc": "l.price_cents DESC, l.created_at DESC"}[sort]
    conn = db()
    rows = conn.execute(
        "SELECT l.*, u.name AS sname, u.avatar AS savatar "
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
        "SELECT l.*, u.name AS sname, u.avatar AS savatar "
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
        "SELECT l.*, u.name AS sname, u.avatar AS savatar "
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
    conn.close()
    seller = get_user(row["seller_id"])
    return render(request, "market_detail.html", {
        "gone": False, "listing": _listing_card(row, saved=saved),
        "seller": ({"id": seller["id"], "name": seller["name"],
                    "avatar": avatar_url(seller.get("avatar", "")),
                    "joined": fancy_time(seller["created_at"])}
                   if seller else None),
        "is_seller": row["seller_id"] == user["id"],
        "saved": saved, "more_count": more_count,
        "safety_note": MARKET_SAFETY_NOTE})


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
    elif card["seller_avatar"]:
        og_image = _abs_url(request, card["seller_avatar"])
    return render(request, "public_listing.html", {
        "listing": card,
        "seller": {"name": row["sname"],
                   "avatar": avatar_url(row["savatar"] or "")},
        "og_image": og_image,
        "excerpt": (row["description"] or row["title"] or "")[:160],
        "safety_note": MARKET_SAFETY_NOTE,
        "logged_in": bool(viewer)})


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
                      "posts": posts_n, "friends": friends_n})

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
        if r["kind"] == "user":
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

    conn.close()
    return render(request, "owner/dashboard.html", {
        "users": users, "friend_activity": friend_activity,
        "flags": flags, "pages": pages, "stats": stats,
        "verifications": verifications,
        "live_now": live_now, "live_recent": live_recent,
        "payouts": payouts, "money_stats": money_stats,
        "money_panel": money_panel,
        "pw_resets": pw_resets, "pw_help_pending": pw_help_pending,
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
        table = "users" if vr["kind"] == "user" else "pages"
        conn.execute(
            f"UPDATE {table} SET is_verified=1, verification_status='approved' WHERE id=?",
            (vr["target_id"],))
        conn.execute("UPDATE verification_requests SET status='approved', reviewed_at=? WHERE id=?",
                     (now_iso(), rid))
        conn.commit()
    conn.close()
    return redir("/owner")


@app.post("/owner/verify/{rid}/decline")
async def owner_verify_decline(request: Request, rid: int):
    err = require_owner(request)
    if err:
        return err
    conn = db()
    vr = conn.execute("SELECT * FROM verification_requests WHERE id=?", (rid,)).fetchone()
    if vr and vr["status"] == "pending":
        table = "users" if vr["kind"] == "user" else "pages"
        conn.execute(f"UPDATE {table} SET verification_status='declined' WHERE id=?",
                     (vr["target_id"],))
        conn.execute("UPDATE verification_requests SET status='declined', reviewed_at=? WHERE id=?",
                     (now_iso(), rid))
        conn.commit()
    conn.close()
    return redir("/owner")


@app.post("/owner/payouts/{pid}/send")
async def owner_payout_send(request: Request, pid: int):
    """Owner approves a cash-out AND sends it: a Stripe Transfer to
    the streamer's Express account (their bank). Only for pending
    requests whose streamer is payout_ready.

    Idempotency (the double-send guard): the request is claimed
    'pending' -> 'sending' in ONE guarded UPDATE before Stripe is
    called, so a double-click / second tab can never create two
    transfers — every decision path (paid / declined / send) only
    acts on 'pending'. Outcomes:
    - Transfer created: status 'paid', method 'stripe', transfer id
      stored, the usual 0-delta cashout_paid audit row. Balances do
      NOT move (tickets were held at request time).
    - Stripe definitively refuses (HTTP error): the claim reverts
      to 'pending' — no transfer exists, so the owner can safely
      decide again, including paying by hand.
    - Stripe doesn't answer (network/timeout): the outcome is
      UNKNOWN, so the request stays 'sending' — stuck VISIBLE rather
      than silently re-sendable; the owner checks Stripe first.
    """
    err = require_owner(request)
    if err:
        return err
    conn = db()
    pr = conn.execute(
        "SELECT pr.*, u.stripe_acct_id AS acct, "
        "u.payout_ready AS ready FROM payout_requests pr "
        "LEFT JOIN users u ON u.id=pr.user_id WHERE pr.id=?",
        (pid,)).fetchone()
    if not pr or pr["status"] != "pending":
        conn.close()
        return redir("/owner", "That cash-out was already decided.")
    if not pr["ready"] or not pr["acct"]:
        conn.close()
        return redir("/owner",
                     "That streamer hasn't finished bank setup — "
                     "pay this one by hand instead.")
    cur = conn.execute(
        "UPDATE payout_requests SET status='sending' WHERE id=? "
        "AND status='pending'", (pid,))
    claimed = cur.rowcount == 1
    conn.commit()
    conn.close()
    if not claimed:
        return redir("/owner", "That cash-out was already decided.")
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
        return redir("/owner",
                     f"Stripe refused the send: {cls} — request "
                     f"{pid} is still pending; you can pay by hand "
                     "instead.")
    if not transfer_id:
        return redir("/owner",
                     f"The send for request {pid} was attempted but "
                     "Stripe didn't answer — check Stripe before "
                     "doing anything else. The request is marked "
                     "'sending' and can't be sent twice.")
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
    return redir("/owner",
                 f"💸 Sent {_money(pr['dollars_cents'])} to the "
                 f"streamer's bank (request {pid}).")


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
    if pr and pr["status"] == "pending":
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
    if pr and pr["status"] == "pending":
        stamp = now_iso()
        conn.execute(
            "UPDATE payout_requests SET status='declined', "
            "decided_at=? WHERE id=?", (stamp, pid))
        conn.execute(
            "UPDATE users SET gift_earned = "
            "COALESCE(gift_earned,0) + ? WHERE id=?",
            (pr["tickets"], pr["user_id"]))
        conn.execute(
            "INSERT INTO ticket_txns(user_id, kind, tickets, "
            "created_at) VALUES(?,?,?,?)",
            (pr["user_id"], "cashout_return", pr["tickets"], stamp))
        conn.commit()
    conn.close()
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
        return redir("/owner")
    exp = _parse_ts(r["expires_at"])
    if not exp or exp <= datetime.now(timezone.utc):
        return redir("/owner")
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
    return redir("/owner")


# ---------------- health ----------------
@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True})
