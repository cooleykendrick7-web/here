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
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import bcrypt
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
STORY_MUSIC_MAX_MB = 10
STORY_MUSIC_LENS = (5, 10, 15, 30)
# Built-in story-music catalog seed. Every track is CC0, public domain,
# or CC-BY with the attribution stored alongside; files live in
# static/music/ and a track is only seeded when its file is present.
# Kevin MacLeod tracks: CC BY 4.0 (incompetech.com).
_KM_ATTR = ("Kevin MacLeod (incompetech.com) — Licensed under Creative "
            "Commons: By Attribution 4.0 License "
            "http://creativecommons.org/licenses/by/4.0/")
MUSIC_SEED = [
    {"file_path": "music/carefree.mp3", "title": "Carefree",
     "artist": "Kevin MacLeod", "genre": "Happy",
     "license": "CC BY 4.0", "attribution": _KM_ATTR},
    {"file_path": "music/investigations.mp3", "title": "Investigations",
     "artist": "Kevin MacLeod", "genre": "Mystery",
     "license": "CC BY 4.0", "attribution": _KM_ATTR},
    {"file_path": "music/lobby-time.mp3", "title": "Lobby Time",
     "artist": "Kevin MacLeod", "genre": "Jazz",
     "license": "CC BY 4.0", "attribution": _KM_ATTR},
    {"file_path": "music/monkeys-spinning-monkeys.mp3",
     "title": "Monkeys Spinning Monkeys",
     "artist": "Kevin MacLeod", "genre": "Comedy",
     "license": "CC BY 4.0", "attribution": _KM_ATTR},
    {"file_path": "music/sneaky-snitch.mp3", "title": "Sneaky Snitch",
     "artist": "Kevin MacLeod", "genre": "Comedy",
     "license": "CC BY 4.0", "attribution": _KM_ATTR},
    {"file_path": "music/merry-go.mp3", "title": "Merry Go",
     "artist": "Kevin MacLeod", "genre": "Happy",
     "license": "CC BY 4.0", "attribution": _KM_ATTR},
    {"file_path": "music/quirky-dog.mp3", "title": "Quirky Dog",
     "artist": "Kevin MacLeod", "genre": "Comedy",
     "license": "CC BY 4.0", "attribution": _KM_ATTR},
    {"file_path": "music/local-forecast-elevator.mp3",
     "title": "Local Forecast – Elevator",
     "artist": "Kevin MacLeod", "genre": "Chill",
     "license": "CC BY 4.0", "attribution": _KM_ATTR},
]
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
            ("music_mute_orig", "ALTER TABLE stories ADD COLUMN music_mute_orig INTEGER DEFAULT 0")):
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
    # --- story music catalog + user music uploads (safe) ---
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
    # Seed the catalog per track: insert any MUSIC_SEED track whose file
    # actually shipped in static/music/ and that has no row yet (matched
    # by file_path). Safe to re-run on every boot — it tops up tracks
    # whose files landed after the first boot, never duplicates an
    # existing file_path, and never touches existing rows.
    for _t in MUSIC_SEED:
        if not os.path.isfile(os.path.join(BASE, "static", _t["file_path"])):
            continue
        if c.execute("SELECT 1 FROM music_tracks WHERE file_path=?",
                     (_t["file_path"],)).fetchone():
            continue
        c.execute(
            "INSERT INTO music_tracks(title, artist, genre, file_path, "
            "license_note, attribution, use_count, created_at) "
            "VALUES(?,?,?,?,?,?,0,?)",
            (_t["title"], _t["artist"], _t["genre"], _t["file_path"],
             _t["license"], _t["attribution"], now_iso()))
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
    else:
        ctx["user"] = None
        ctx["pending_count"] = 0
        ctx["unread_count"] = 0
        ctx["unread_msgs"] = 0
        ctx.setdefault("page_bg", "")
        ctx.setdefault("viewer_spot_style", "")
        ctx.setdefault("viewer_spot_classes", "")
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
                    raise ValueError(f"File too large (max {cap_mb}MB).")
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


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request):
    if current_user(request):
        return redir("/home")
    return render(request, "auth/signup.html",
                  {"error": request.query_params.get("error", "")})


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
            "INSERT INTO users(name, email, pw_hash, created_at) VALUES(?,?,?,?)",
            (name, email, pw_hash, now_iso()))
        uid = cur.lastrowid
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        return redir("/signup?error=" + "That email is already registered.")
    conn.close()
    token = new_session(uid)
    resp = redir("/home")
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
                   "composer_friends": cfriends, "composer_pages": cpages})


def create_post_from_form(user, form, page_id=None):
    """Validate + insert a post. Returns (post_id, error_flash)."""
    kind = (form.get("kind") or "text").strip()
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

    media_file = form.get("media")
    allowed = IMG_EXTS | AUD_EXTS | VID_EXTS
    if kind in ("photo", "clothes"):
        allowed = IMG_EXTS
    elif kind == "audio":
        allowed = AUD_EXTS
    elif kind == "video":
        allowed = VID_EXTS
    # Post videos get the big cap; photos/audio keep the default cap.
    _mname = getattr(media_file, "filename", "") or ""
    _mext = _mname.rsplit(".", 1)[-1].lower() if "." in _mname else ""
    cap = POST_VIDEO_MAX_MB if _mext in VID_EXTS else 0
    try:
        media = save_upload(media_file, "posts", allowed, max_mb=cap)
    except ValueError as e:
        return None, str(e)

    conn = db()
    cur = conn.execute(
        "INSERT INTO posts(user_id, page_id, kind, body, feeling, link_url, "
        "video_url, media, privacy, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (user["id"], page_id, kind, body, feeling, link_url, video_url,
         media, privacy, now_iso()))
    pid = cur.lastrowid
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
    return pid, None


@app.post("/posts")
async def create_post(request: Request):
    user, err = require_login(request)
    if err:
        return err
    form = await request.form()
    pid, ferr = create_post_from_form(user, form)
    if ferr:
        return redir("/home", ferr)
    return redir("/home")


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
    crows = conn.execute(
        "SELECT c.* FROM comments c WHERE c.post_id=? ORDER BY c.created_at ASC",
        (pid,)).fetchall()
    if not owner_on:
        # Comments from anyone in a block relationship with the viewer
        # (either direction) don't render for that viewer; everyone
        # else's view of the same thread is untouched.
        _blocked_view = blocked_id_set(user["id"])
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
            "can_delete": bool(user["id"] == c["user_id"] or
                               user["id"] == p["user_id"] or owner_on),
            "can_block": bool(user["id"] == p["user_id"] and
                              c["user_id"] != user["id"]),
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
    return JSONResponse({"ok": True, "likes": likes, "reacted": my_kind is not None,
                         "kind": my_kind, "top": top})


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


# ---- story music helpers ----
def _music_catalog():
    """Built-in licensed catalog, trending first (most used, then A-Z)."""
    conn = db()
    rows = conn.execute(
        "SELECT * FROM music_tracks ORDER BY use_count DESC, title ASC").fetchall()
    conn.close()
    return [{"id": r["id"], "title": r["title"], "artist": r["artist"],
             "genre": r["genre"], "url": "/static/" + r["file_path"],
             "attribution": r["attribution"],
             "license": r["license_note"]} for r in rows]


def _my_music(user_id):
    """Music a user may attach: their story-music uploads plus the audio
    files from their own audio posts. Each: {ref, title, url} where ref
    is the uploads-relative path stored on the story."""
    conn = db()
    out, seen = [], set()
    for r in conn.execute(
            "SELECT file_path, title FROM story_music_uploads "
            "WHERE user_id=? ORDER BY created_at DESC", (user_id,)).fetchall():
        if r["file_path"] in seen:
            continue
        seen.add(r["file_path"])
        out.append({"ref": r["file_path"], "title": r["title"] or "My music",
                    "url": "/uploads/" + r["file_path"]})
    for r in conn.execute(
            "SELECT media, body FROM posts WHERE user_id=? AND kind='audio' "
            "AND media != '' ORDER BY created_at DESC", (user_id,)).fetchall():
        if r["media"] in seen:
            continue
        seen.add(r["media"])
        out.append({"ref": r["media"],
                    "title": (r["body"] or "").strip()[:40] or "Audio post",
                    "url": "/uploads/" + r["media"]})
    conn.close()
    return out


def _owned_music_title(user_id, ref):
    """Title snapshot when ref is a music file user_id owns, else None.
    This is the server-side ownership gate for music_kind='upload': a
    story can never point at another user's file or an arbitrary path."""
    if not ref or ".." in ref:
        return None
    conn = db()
    r = conn.execute(
        "SELECT title FROM story_music_uploads WHERE user_id=? AND file_path=?",
        (user_id, ref)).fetchone()
    if r:
        conn.close()
        return r["title"] or "My music"
    r = conn.execute(
        "SELECT body FROM posts WHERE user_id=? AND kind='audio' AND media=?",
        (user_id, ref)).fetchone()
    conn.close()
    if r:
        return (r["body"] or "").strip()[:40] or "Audio post"
    return None


def _story_music_url(conn, s):
    if s["music_kind"] == "catalog":
        try:
            tid = int(s["music_ref"])
        except (TypeError, ValueError):
            return ""
        t = conn.execute("SELECT file_path FROM music_tracks WHERE id=?",
                         (tid,)).fetchone()
        return "/static/" + t["file_path"] if t else ""
    if s["music_kind"] == "upload" and s["music_ref"]:
        return "/uploads/" + s["music_ref"]
    return ""


def _story_payload(conn, s, viewer_id, seen):
    media_url = "/uploads/" + s["media"] if s["media"] else ""
    overlay = None
    if (s["overlay_text"] or "").strip():
        overlay = {"text": s["overlay_text"],
                   "color": s["overlay_color"] or "#ffffff",
                   "pos": s["overlay_pos"] or "bottom"}
    music = None
    murl = _story_music_url(conn, s)
    if murl:
        music = {"kind": s["music_kind"], "url": murl,
                 "title": s["music_title"] or "",
                 "artist": s["music_artist"] or "",
                 "start": s["music_start"] or 0,
                 "len": s["music_len"] or 15,
                 "volume": s["music_volume"] if s["music_volume"] is not None else 80,
                 "mute_orig": bool(s["music_mute_orig"])}
    return {
        "id": s["id"], "media": media_url,
        "is_video": (s["media"] or "").lower().endswith((".mp4", ".mov", ".webm")),
        "caption": s["caption"] or "",
        "created_at": s["created_at"], "expires_at": s["expires_at"],
        "mine": s["user_id"] == viewer_id, "seen": bool(seen),
        "overlay": overlay, "music": music,
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


@app.get("/stories/new", response_class=HTMLResponse)
async def story_new_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    return render(request, "story_new.html", {
        "catalog": _music_catalog(),
        "my_music": _my_music(user["id"]),
    })


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
    # --- media first; a bad story file rejects the whole post ---
    try:
        media = save_upload(form.get("media"), "stories", STORY_EXTS)
    except ValueError as e:
        return redir("/stories/new", str(e))
    if not media:
        return redir("/stories/new", "Choose a photo or short video.")
    # --- music selection: a fresh upload beats a picked track ---
    music_kind = music_ref = music_title = music_artist = None
    up = form.get("music_file")
    if up is not None and getattr(up, "filename", ""):
        try:
            ref = save_upload(up, "story_music", AUD_EXTS,
                              max_mb=STORY_MUSIC_MAX_MB)
        except ValueError as e:
            return redir("/stories/new", str(e))
        if ref:
            title = os.path.splitext(os.path.basename(up.filename))[0]
            title = re.sub(r"\s+", " ", title).strip()[:80] or "My music"
            conn = db()
            conn.execute(
                "INSERT INTO story_music_uploads(user_id, file_path, title, "
                "created_at) VALUES(?,?,?,?)",
                (user["id"], ref, title, now_iso()))
            conn.commit()
            conn.close()
            music_kind, music_ref = "upload", ref
            music_title, music_artist = title, user["name"]
    if music_kind is None:
        want = (form.get("music_kind") or "").strip()
        ref = (form.get("music_ref") or "").strip()
        if want == "catalog" and ref:
            try:
                tid = int(ref)
            except ValueError:
                tid = None
            if tid is not None:
                conn = db()
                t = conn.execute("SELECT * FROM music_tracks WHERE id=?",
                                 (tid,)).fetchone()
                conn.close()
                if t:
                    music_kind, music_ref = "catalog", str(tid)
                    music_title, music_artist = t["title"], t["artist"]
        elif want == "upload" and ref:
            owned = _owned_music_title(user["id"], ref)
            if owned is not None:
                music_kind, music_ref = "upload", ref
                music_title, music_artist = owned, user["name"]
    mins = _story_lifetime_minutes(form)
    conn = db()
    cur = conn.execute(
        "INSERT INTO stories(user_id, media, caption, created_at, expires_at, "
        "overlay_text, overlay_color, overlay_pos, music_kind, music_ref, "
        "music_title, music_artist, music_start, music_len, music_volume, "
        "music_mute_orig) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (user["id"], media, caption, now_iso(), story_expires_at(mins),
         overlay_text, overlay_color, overlay_pos, music_kind, music_ref or "",
         music_title or "", music_artist or "", music_start, music_len,
         music_volume, music_mute_orig))
    sid = cur.lastrowid
    if music_kind == "catalog":
        conn.execute("UPDATE music_tracks SET use_count=use_count+1 WHERE id=?",
                     (int(music_ref),))
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
        # profile), so a seen tap doesn't sit in the bell forever ---
        conn.execute(
            "DELETE FROM notifications WHERE user_id=? AND kind='tap' "
            "AND link=?", (user["id"], f"/user/{uid}"))
        conn.commit()
    fcount = conn.execute(
        "SELECT COUNT(*) c FROM friendships WHERE status='accepted' AND "
        "(user_id=? OR friend_id=?)", (uid, uid)).fetchone()["c"]
    tcount = conn.execute("SELECT COUNT(*) c FROM taps WHERE tapped_id=?",
                          (uid,)).fetchone()["c"]
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
    # On a profile page the PROFILE OWNER's background wins over the
    # viewer's own (render() only defaults page_bg when unset). Same for
    # the viewer's spot colors: blanked here, the owner's .spot-scope
    # vars rule the whole page.
    return render(request, "profile.html",
                  {"profile": profile_ctx, "posts": posts,
                   "page_bg": profile_ctx["background"],
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
                    "spot_card_opacity": user.get("spot_card_opacity")}})


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


# ---------------- tap ----------------
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
    # it has done its job.
    conn = db()
    conn.execute(
        "DELETE FROM notifications WHERE user_id=? AND kind='tap' "
        "AND link=?", (user["id"], f"/user/{uid}"))
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
                 "body": r["body"], "created_at": fancy_time(r["created_at"])}
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
            "SELECT m.body, m.created_at FROM messages m WHERE "
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
            "last_body": last["body"] if last else "",
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
    conn.close()
    call_req = None
    if pending_req:
        call_req = {"id": pending_req["id"], "kind": pending_req["kind"],
                    "direction": ("out" if pending_req["requester_id"] == user["id"]
                                  else "in")}
    msgs = [{"id": r["id"], "sender_id": r["sender_id"], "body": r["body"],
             "created_at": fancy_time(r["created_at"]),
             "mine": r["sender_id"] == user["id"]} for r in rows]
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
    conn = db()
    cur = conn.execute(
        "INSERT INTO messages(sender_id, receiver_id, body, created_at) "
        "VALUES(?,?,?,?)", (user["id"], fid, body, now_iso()))
    mid = cur.lastrowid
    conn.commit()
    conn.close()
    flag_item("message", mid, user["id"], user["name"], body)
    notify(fid, "message", f"New message from {user['name']}.",
           f"/messages/{user['id']}", actor_id=user["id"])
    preview = body if len(body) <= 80 else body[:79] + "…"
    if notifications_allowed(fid, user["id"]):
        send_push(fid, f"💬 {user['name']}", preview,
                  f"/messages/{user['id']}", tag=f"msg-{user['id']}")
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
# and one approved guest can join on camera. The architecture mirrors
# 1:1 calls exactly — media flows phone-to-phone via WebRTC, the DB
# only carries room state, chat, presence, join requests, and the
# small signaling messages (SDP/ICE) browsers poll for. No websockets,
# nothing is recorded or stored server-side.
#
# Topology: the HOST sends to every watcher (one send peer per
# watcher, host always offers). The GUEST (max one) is two-way with
# the host (host offers) and sends to every watcher (guest offers).
# Watchers are receive-only and answer. Fixed offerer roles per pair
# mean there is never negotiation glare to resolve.
LIVE_STALE_SECONDS = 90        # host heartbeat gap that auto-ends a live
LIVE_VIEWER_WINDOW = 45       # viewer counts if seen within this window
LIVE_TITLE_MAX = 80
LIVE_COMMENT_MAX = 300
LIVE_JOIN_COOLDOWN = 60       # seconds after a decline before re-asking
LIVE_SIGNAL_TYPES = ("hello", "offer", "answer", "ice", "bye")
LIVE_MAX_PAYLOAD = 65536      # same SDP headroom as calls


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


def _live_get(conn, lid):
    r = conn.execute("SELECT * FROM live_sessions WHERE id=?",
                     (lid,)).fetchone()
    return dict(r) if r else None


def _live_can_watch(viewer, live, owner_on=False):
    """Who may watch/comment/join a live: the host always; the site
    owner context always; anyone logged in EXCEPT a pair blocked
    either way with the host; and when the host's profile is private,
    only the host's friends (the privacy suite's rule)."""
    if not viewer:
        return False
    if viewer["id"] == live["host_id"] or owner_on:
        return True
    if blocked_either_way(viewer["id"], live["host_id"]):
        return False
    host = get_user(live["host_id"])
    if host and host.get("private_profile"):
        return viewer["id"] in friend_id_set(live["host_id"])
    return True


def _live_guest_req(conn, lid):
    """The currently approved join request (the on-camera guest)."""
    r = conn.execute(
        "SELECT * FROM live_join_requests WHERE live_id=? AND "
        "status='approved' ORDER BY id DESC LIMIT 1", (lid,)).fetchone()
    return dict(r) if r else None


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
    """Drop the on-camera guest back to watcher (no commit): settle
    the join request and leave 'bye' signals so the guest's client and
    every peer holding a guest connection tears it down at once."""
    stamp = now_iso()
    conn.execute(
        "UPDATE live_join_requests SET status='left', decided_at=? "
        "WHERE live_id=? AND user_id=? AND status='approved'",
        (stamp, live["id"], guest_id))
    _live_signal(conn, live["id"], guest_id, live["host_id"], "bye")
    _live_signal(conn, live["id"], live["host_id"], guest_id, "bye")
    for v in _live_viewer_rows(conn, live["id"]):
        if v["user_id"] not in (guest_id, live["host_id"]):
            _live_signal(conn, live["id"], guest_id, v["user_id"], "bye")


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


def _live_comment_json(row, viewer_id, host_id):
    return {
        "id": row["id"], "user_id": row["user_id"],
        "name": row["name"] or "Deleted user",
        "avatar": avatar_url(row["avatar"] or ""),
        "body": row["body"], "created_at": row["created_at"],
        "mine": row["user_id"] == viewer_id,
        "from_host": row["user_id"] == host_id,
        "can_delete": row["user_id"] == viewer_id
                      or viewer_id == host_id}


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
        out.append({"id": live["id"], "host_id": live["host_id"],
                    "name": host["name"], "avatar": host["avatar"],
                    "title": live["title"],
                    "viewers": _live_viewer_count(conn, live["id"]),
                    "is_self": live["host_id"] == viewer_id})
    conn.close()
    return out


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
        "host_seen_at) VALUES(?,?,'live',?,?)",
        (user["id"], title, stamp, stamp))
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
    guest_req = _live_guest_req(conn, lid)
    conn.close()
    guest = _live_person(guest_req["user_id"]) if guest_req else None
    if user["id"] == live["host_id"]:
        role = "host"
    elif guest_req and guest_req["user_id"] == user["id"]:
        role = "guest"
    else:
        role = "watcher"
    boot = {"id": lid, "me": user["id"], "role": role,
            "host": host, "guest": guest, "title": live["title"],
            "created_at": live["created_at"]}
    return render(request, "live.html", {
        "mode": "live", "live": live, "host": host, "guest": guest,
        "role": role,
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
    """The watch page's 3s poll: room status, viewer count, the guest,
    new comments since ?since_comment=, the host's pending join
    requests, and my own join-request standing."""
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
    rows = conn.execute(
        "SELECT lc.*, u.name, u.avatar FROM live_comments lc "
        "LEFT JOIN users u ON u.id=lc.user_id WHERE lc.live_id=? AND "
        "lc.id>? ORDER BY lc.id ASC LIMIT 100", (lid, since)).fetchall()
    comments = [_live_comment_json(dict(r), user["id"], live["host_id"])
                for r in rows]
    guest_req = _live_guest_req(conn, lid)
    guest = _live_person(guest_req["user_id"]) if guest_req else None
    viewer_rows = _live_viewer_rows(conn, lid)
    is_host = user["id"] == live["host_id"]
    is_guest = bool(guest_req and guest_req["user_id"] == user["id"])
    out = {
        "ok": True, "status": "live", "title": live["title"],
        "host": host, "guest": guest,
        "created_at": live["created_at"],
        "viewers": len(viewer_rows),
        "comments": comments,
        "my_role": "host" if is_host else ("guest" if is_guest
                                           else "watcher"),
        "my_join": None, "pending_requests": [],
    }
    if is_host or is_guest:
        # The people sending video need to know who is watching so
        # they can open a peer to each new watcher.
        out["viewer_list"] = [
            {"id": v["user_id"], "name": v["name"] or "Deleted user",
             "avatar": avatar_url(v["avatar"] or "")}
            for v in viewer_rows]
    if is_host:
        prows = conn.execute(
            "SELECT jr.*, u.name, u.avatar FROM live_join_requests jr "
            "LEFT JOIN users u ON u.id=jr.user_id WHERE jr.live_id=? "
            "AND jr.status='pending' ORDER BY jr.id ASC",
            (lid,)).fetchall()
        out["pending_requests"] = [
            {"id": r["id"], "user_id": r["user_id"],
             "name": r["name"] or "Deleted user",
             "avatar": avatar_url(r["avatar"] or ""),
             "created_at": r["created_at"]} for r in prows]
    mine = conn.execute(
        "SELECT * FROM live_join_requests WHERE live_id=? AND "
        "user_id=? ORDER BY id DESC LIMIT 1",
        (lid, user["id"])).fetchone()
    if mine:
        out["my_join"] = {"id": mine["id"], "status": mine["status"],
                          "decided_at": mine["decided_at"]}
    conn.close()
    return JSONResponse(out)


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
    conn.close()
    flag_item("live_comment", cid, user["id"], user["name"], body)
    return JSONResponse({"ok": True, "comment": _live_comment_json(
        dict(row), user["id"], live["host_id"])})


@app.post("/live/{lid}/comment/{cid}/delete")
async def live_comment_delete(request: Request, lid: int, cid: int):
    """The comment's author can delete it; so can the live's host —
    hosts moderate their own live chat."""
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
    if row["user_id"] != user["id"] and live["host_id"] != user["id"]:
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Only the author or the host can delete this."},
            status_code=403)
    conn.execute("DELETE FROM live_comments WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


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
    # on-camera guest, current watchers, and people with a join
    # request in flight — never to an outsider, and never across a
    # block. (The sender already passed _live_can_watch.)
    conn = db()
    allowed = {live["host_id"]}
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
    if to_id == user["id"] or to_id not in allowed:
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
    if guest_req and guest_req["user_id"] == user["id"]:
        conn.close()
        return JSONResponse({"ok": True, "already_guest": True})
    pending = conn.execute(
        "SELECT * FROM live_join_requests WHERE live_id=? AND "
        "user_id=? AND status='pending' ORDER BY id DESC LIMIT 1",
        (lid, user["id"])).fetchone()
    if pending:
        conn.close()
        return JSONResponse({"ok": True, "request": {
            "id": pending["id"], "status": "pending"}})
    last = conn.execute(
        "SELECT * FROM live_join_requests WHERE live_id=? AND "
        "user_id=? ORDER BY id DESC LIMIT 1",
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
        "created_at) VALUES(?,?,'pending',?)",
        (lid, user["id"], now_iso()))
    rid = cur.lastrowid
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "request": {"id": rid,
                                                 "status": "pending"}})


@app.post("/live/{lid}/join-decide")
async def live_join_decide(request: Request, lid: int):
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    if live["host_id"] != user["id"]:
        return JSONResponse(
            {"ok": False, "error": "Only the host can decide."},
            status_code=403)
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        rid = int(data.get("request_id") or 0)
    except (TypeError, ValueError):
        rid = 0
    approve = bool(data.get("approve")) if isinstance(data, dict) \
        else False
    conn = db()
    req = conn.execute(
        "SELECT * FROM live_join_requests WHERE id=? AND live_id=?",
        (rid, lid)).fetchone()
    if not req or req["status"] != "pending":
        conn.close()
        return JSONResponse(
            {"ok": False, "error": "That request is already settled."},
            status_code=409)
    if approve and _live_guest_req(conn, lid):
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Someone is already on camera — only one guest "
                      "at a time."}, status_code=409)
    conn.execute(
        "UPDATE live_join_requests SET status=?, decided_at=? "
        "WHERE id=?",
        ("approved" if approve else "declined", now_iso(), rid))
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


@app.post("/live/{lid}/guest-leave")
async def live_guest_leave(request: Request, lid: int):
    """The guest steps down (or the host removes them): the slot
    frees, peers tear the guest down via 'bye' signals."""
    user, live, err = _live_access(request, lid, need_live=True)
    if err:
        return err
    try:
        data = await request.json()
    except Exception:
        data = {}
    conn = db()
    guest_req = _live_guest_req(conn, lid)
    if not guest_req:
        conn.close()
        return JSONResponse({"ok": False,
                             "error": "Nobody is on camera right now."},
                            status_code=409)
    guest_id = guest_req["user_id"]
    if user["id"] == live["host_id"]:
        try:
            want = int(data.get("user_id") or guest_id) \
                if isinstance(data, dict) else guest_id
        except (TypeError, ValueError):
            want = guest_id
        if want != guest_id:
            conn.close()
            return JSONResponse(
                {"ok": False,
                 "error": "That person isn't the guest."},
                status_code=400)
    elif user["id"] != guest_id:
        conn.close()
        return JSONResponse(
            {"ok": False,
             "error": "Only the guest or the host can do that."},
            status_code=403)
    _drop_guest(conn, live, guest_id)
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True})


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
    return render(request, "discover.html",
                  {"people": discover_people(user["id"], 20),
                   "trends": trending_topics(15, user["id"])})


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
    conn.close()
    return render(request, "owner/dashboard.html", {
        "users": users, "friend_activity": friend_activity,
        "flags": flags, "pages": pages, "stats": stats,
        "verifications": verifications,
        "live_now": live_now, "live_recent": live_recent})


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


# ---------------- health ----------------
@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True})
