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
import os
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
from fastapi import FastAPI, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import moderation

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
PTYPES = ("community", "market", "fan_club", "seller")
IMG_EXTS = {"jpg", "jpeg", "png", "gif", "webp"}
AUD_EXTS = {"mp3", "wav", "ogg", "m4a"}
MAX_UPLOAD_MB = 25
ONLINE_MINUTES = 5

for _d in ("avatars", "posts", "pages"):
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
        last_seen TEXT, created_at TEXT NOT NULL)""")
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
    return get_user(s["user_id"])


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


def render(request: Request, name, ctx=None):
    """Every page view gets the global template context; authenticated views
    also refresh last_seen."""
    ctx = dict(ctx or {})
    u = current_user(request)
    if u:
        touch_seen(u["id"])
        u = get_user(u["id"])  # refresh (last_seen changed)
        ctx["user"] = {"id": u["id"], "name": u["name"], "email": u["email"],
                       "bio": u.get("bio", ""), "avatar": u.get("avatar", "")}
        conn = db()
        ctx["pending_count"] = conn.execute(
            "SELECT COUNT(*) c FROM friendships WHERE friend_id=? AND status='pending'",
            (u["id"],)).fetchone()["c"]
        ctx["unread_count"] = conn.execute(
            "SELECT COUNT(*) c FROM notifications WHERE user_id=? AND is_read=0",
            (u["id"],)).fetchone()["c"]
        ctx["unread_msgs"] = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE receiver_id=? AND is_read=0",
            (u["id"],)).fetchone()["c"]
        conn.close()
    else:
        ctx["user"] = None
        ctx["pending_count"] = 0
        ctx["unread_count"] = 0
        ctx["unread_msgs"] = 0
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
def notify(user_id, kind, text, link=""):
    if not user_id:
        return
    conn = db()
    conn.execute(
        "INSERT INTO notifications(user_id, kind, text, link, created_at) "
        "VALUES(?,?,?,?,?)", (user_id, kind, text, link, now_iso()))
    conn.commit()
    conn.close()


def save_upload(up, subdir, allowed):
    """Save an uploaded file under UPLOAD_DIR/subdir with a uuid name.
    Returns the relative path (e.g. 'avatars/abc123.png') or '' if no file.
    Raises ValueError on bad extension or oversize."""
    if up is None or not getattr(up, "filename", ""):
        return ""
    fname = up.filename or ""
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    if ext not in allowed:
        raise ValueError(f"File type .{ext} not allowed.")
    data = up.file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise ValueError(f"File too large (max {MAX_UPLOAD_MB}MB).")
    name = f"{uuid.uuid4().hex}.{ext}"
    dest = os.path.join(UPLOAD_DIR, subdir, name)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as f:
        f.write(data)
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


def post_card(p, viewer, owner_on=False):
    """Build the post-card dict the partials/post_card.html template expects."""
    conn = db()
    a = conn.execute("SELECT id,name,avatar FROM users WHERE id=?",
                     (p["user_id"],)).fetchone()
    likes = conn.execute("SELECT COUNT(*) c FROM likes WHERE post_id=?",
                         (p["id"],)).fetchone()["c"]
    liked = False
    if viewer:
        liked = bool(conn.execute(
            "SELECT 1 FROM likes WHERE post_id=? AND user_id=?",
            (p["id"], viewer["id"])).fetchone())
    cc = conn.execute("SELECT COUNT(*) c FROM comments WHERE post_id=?",
                      (p["id"],)).fetchone()["c"]
    page_name, page_id, is_page = "", 0, False
    if p["page_id"]:
        pg = conn.execute("SELECT id,name FROM pages WHERE id=?",
                          (p["page_id"],)).fetchone()
        if pg:
            page_name, page_id, is_page = pg["name"], pg["id"], True
    conn.close()
    media = p["media"] or ""
    avatar = ""
    if a and a["avatar"]:
        avatar = "/uploads/" + a["avatar"]
    return {
        "id": p["id"], "user_id": p["user_id"],
        "author_name": a["name"] if a else "Deleted user",
        "author_avatar": avatar,
        "kind": p["kind"], "body": p["body"] or "",
        "feeling": p["feeling"] or "", "link_url": p["link_url"] or "",
        "video_url": p["video_url"] or "",
        "media": "/uploads/" + media if media else "",
        "embed_url": video_embed_url(p["video_url"]) if p["kind"] == "video" else "",
        "created_at": fancy_time(p["created_at"]),
        "likes": likes, "liked": liked, "comments_count": cc,
        "can_delete": bool(viewer and (viewer["id"] == p["user_id"] or owner_on)),
        "is_page_post": is_page, "page_name": page_name, "page_id": page_id,
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
    return "/uploads/" + path if path else ""


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
    """Own posts + friends' posts + posts on joined pages, newest first."""
    fids = friend_id_set(user["id"])
    conn = db()
    page_ids = [r["page_id"] for r in conn.execute(
        "SELECT page_id FROM page_members WHERE user_id=?", (user["id"],)).fetchall()]
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
    return [post_card(dict(r), user, owner_on) for r in rows]


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
            f = conn.execute("SELECT id,name,avatar,last_seen FROM users WHERE id=?",
                             (fid,)).fetchone()
            if f:
                contacts.append({"id": f["id"], "name": f["name"],
                                 "avatar": avatar_url(f["avatar"]),
                                 "online": is_online(f["last_seen"])})
        conn.close()
        contacts.sort(key=lambda c: (not c["online"], c["name"].lower()))
    return render(request, "home.html", {"posts": posts, "contacts": contacts})


def create_post_from_form(user, form, page_id=None):
    """Validate + insert a post. Returns (post_id, error_flash)."""
    kind = (form.get("kind") or "text").strip()
    if kind not in KINDS:
        return None, "Invalid post kind."
    body = (form.get("body") or "").strip()
    feeling = (form.get("feeling") or "").strip()
    link_url = (form.get("link_url") or "").strip()
    video_url = (form.get("video_url") or "").strip()

    if link_url and not (link_url.startswith("http://") or
                         link_url.startswith("https://")):
        return None, "Link must start with http:// or https://."
    if kind == "video":
        if not video_url or ("youtube" not in video_url and "youtu.be" not in video_url
                             and "vimeo" not in video_url):
            return None, "Video URL must be a YouTube or Vimeo link."
    elif video_url and ("youtube" not in video_url and "youtu.be" not in video_url
                        and "vimeo" not in video_url):
        return None, "Video URL must be a YouTube or Vimeo link."

    media_file = form.get("media")
    allowed = IMG_EXTS | AUD_EXTS
    if kind in ("photo", "clothes"):
        allowed = IMG_EXTS
    elif kind == "audio":
        allowed = AUD_EXTS
    try:
        media = save_upload(media_file, "posts", allowed)
    except ValueError as e:
        return None, str(e)

    conn = db()
    cur = conn.execute(
        "INSERT INTO posts(user_id, page_id, kind, body, feeling, link_url, "
        "video_url, media, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (user["id"], page_id, kind, body, feeling, link_url, video_url,
         media, now_iso()))
    pid = cur.lastrowid
    conn.commit()
    conn.close()
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
    crows = conn.execute(
        "SELECT c.* FROM comments c WHERE c.post_id=? ORDER BY c.created_at ASC",
        (pid,)).fetchall()
    comments = []
    for c in crows:
        a = conn.execute("SELECT id,name,avatar FROM users WHERE id=?",
                         (c["user_id"],)).fetchone()
        comments.append({
            "id": c["id"], "user_id": c["user_id"],
            "author_name": a["name"] if a else "Deleted user",
            "author_avatar": avatar_url(a["avatar"]) if a and a["avatar"] else "",
            "body": c["body"], "created_at": fancy_time(c["created_at"]),
            "can_delete": bool(user["id"] == c["user_id"] or
                               user["id"] == p["user_id"] or owner_on),
        })
    conn.close()
    return render(request, "post.html",
                  {"post": post_card(dict(p), user, owner_on), "comments": comments})


@app.post("/post/{pid}/comment")
async def add_comment(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    p = conn.execute("SELECT id FROM posts WHERE id=?", (pid,)).fetchone()
    conn.close()
    if not p:
        return redir("/home", "Post not found.")
    form = await request.form()
    body = (form.get("body") or "").strip()
    if not body:
        return redir(f"/post/{pid}", "Comment can't be empty.")
    conn = db()
    cur = conn.execute(
        "INSERT INTO comments(post_id, user_id, body, created_at) VALUES(?,?,?,?)",
        (pid, user["id"], body, now_iso()))
    cid = cur.lastrowid
    conn.commit()
    conn.close()
    flag_item("comment", cid, user["id"], user["name"], body)
    return redir(f"/post/{pid}")


@app.post("/post/{pid}/like")
async def toggle_like(request: Request, pid: int):
    user, err = require_login(request)
    if err:
        return JSONResponse({"ok": False, "error": "login"}, status_code=401)
    conn = db()
    p = conn.execute("SELECT id FROM posts WHERE id=?", (pid,)).fetchone()
    if not p:
        conn.close()
        return JSONResponse({"ok": False, "error": "not found"}, status_code=404)
    existing = conn.execute("SELECT 1 FROM likes WHERE post_id=? AND user_id=?",
                            (pid, user["id"])).fetchone()
    if existing:
        conn.execute("DELETE FROM likes WHERE post_id=? AND user_id=?",
                     (pid, user["id"]))
        liked = False
    else:
        conn.execute("INSERT INTO likes(post_id, user_id, created_at) VALUES(?,?,?)",
                     (pid, user["id"], now_iso()))
        liked = True
    likes = conn.execute("SELECT COUNT(*) c FROM likes WHERE post_id=?",
                         (pid,)).fetchone()["c"]
    conn.commit()
    conn.close()
    return JSONResponse({"ok": True, "likes": likes, "liked": liked})


def _delete_post(pid):
    conn = db()
    conn.execute("DELETE FROM likes WHERE post_id=?", (pid,))
    conn.execute("DELETE FROM comments WHERE post_id=?", (pid,))
    conn.execute("DELETE FROM moderation_flags WHERE item_type='post' AND item_id=?",
                 (pid,))
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
    conn = db()
    fcount = conn.execute(
        "SELECT COUNT(*) c FROM friendships WHERE status='accepted' AND "
        "(user_id=? OR friend_id=?)", (uid, uid)).fetchone()["c"]
    tcount = conn.execute("SELECT COUNT(*) c FROM taps WHERE tapped_id=?",
                          (uid,)).fetchone()["c"]
    prows = conn.execute(
        "SELECT * FROM posts WHERE user_id=? AND page_id IS NULL "
        "ORDER BY created_at DESC LIMIT 100", (uid,)).fetchall()
    conn.close()
    profile_ctx = {
        "id": prof["id"], "name": prof["name"], "bio": prof.get("bio", ""),
        "avatar": avatar_url(prof.get("avatar", "")),
        "created_at": fancy_time(prof["created_at"]),
        "friend_count": fcount,
        "is_self": user["id"] == uid,
        "req_status": req_status(user["id"], uid),
        "tap_count": tcount,
    }
    posts = [post_card(dict(r), user, owner_on) for r in prows]
    return render(request, "profile.html", {"profile": profile_ctx, "posts": posts})


@app.get("/settings/profile", response_class=HTMLResponse)
async def profile_edit_page(request: Request):
    user, err = require_login(request)
    if err:
        return err
    return render(request, "profile_edit.html", {
        "profile": {"id": user["id"], "name": user["name"],
                    "bio": user.get("bio", ""),
                    "avatar": avatar_url(user.get("avatar", ""))}})


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
    except ValueError as e:
        return redir("/settings/profile", str(e))
    conn = db()
    if avatar:
        conn.execute("UPDATE users SET name=?, bio=?, avatar=? WHERE id=?",
                     (name, bio, avatar, user["id"]))
    else:
        conn.execute("UPDATE users SET name=?, bio=? WHERE id=?",
                     (name, bio, user["id"]))
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
        "SELECT u.id,u.name,u.avatar,f.created_at FROM friendships f "
        "JOIN users u ON u.id=f.user_id WHERE f.friend_id=? AND f.status='pending' "
        "ORDER BY f.created_at DESC", (user["id"],)).fetchall()
    sent = conn.execute(
        "SELECT u.id,u.name,u.avatar FROM friendships f "
        "JOIN users u ON u.id=f.friend_id WHERE f.user_id=? AND f.status='pending' "
        "ORDER BY f.created_at DESC", (user["id"],)).fetchall()
    acc = conn.execute(
        "SELECT u.id,u.name,u.avatar,u.last_seen FROM friendships f "
        "JOIN users u ON u.id = CASE WHEN f.user_id=? THEN f.friend_id ELSE f.user_id END "
        "WHERE f.status='accepted' AND (f.user_id=? OR f.friend_id=?) "
        "ORDER BY u.name", (user["id"], user["id"], user["id"])).fetchall()
    conn.close()
    return render(request, "friends.html", {
        "incoming": [{"id": r["id"], "name": r["name"],
                      "avatar": avatar_url(r["avatar"]),
                      "created_at": fancy_time(r["created_at"])} for r in incoming],
        "sent": [{"id": r["id"], "name": r["name"],
                  "avatar": avatar_url(r["avatar"])} for r in sent],
        "friends": [{"id": r["id"], "name": r["name"],
                     "avatar": avatar_url(r["avatar"]),
                     "online": is_online(r["last_seen"])} for r in acc],
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
               f"{user['name']} sent you a friend request.", "/friends")
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
    notify(uid, "tap", f"{user['name']} tapped you. 👆", f"/user/{user['id']}")
    return back(request)


# ---------------- messages ----------------
@app.get("/messages", response_class=HTMLResponse)
async def inbox(request: Request):
    user, err = require_login(request)
    if err:
        return err
    conn = db()
    rows = conn.execute(
        "SELECT CASE WHEN sender_id=? THEN receiver_id ELSE sender_id END AS fid, "
        "MAX(created_at) AS last_at FROM messages "
        "WHERE sender_id=? OR receiver_id=? GROUP BY fid "
        "ORDER BY last_at DESC", (user["id"], user["id"], user["id"])).fetchall()
    threads = []
    for r in rows:
        fid = r["fid"]
        f = conn.execute("SELECT id,name,avatar FROM users WHERE id=?", (fid,)).fetchone()
        last = conn.execute(
            "SELECT body, created_at FROM messages WHERE "
            "((sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?)) "
            "ORDER BY created_at DESC LIMIT 1",
            (user["id"], fid, fid, user["id"])).fetchone()
        unread = conn.execute(
            "SELECT COUNT(*) c FROM messages WHERE sender_id=? AND receiver_id=? "
            "AND is_read=0", (fid, user["id"])).fetchone()["c"]
        threads.append({
            "friend_id": fid,
            "friend_name": f["name"] if f else "Deleted user",
            "friend_avatar": avatar_url(f["avatar"]) if f and f["avatar"] else "",
            "last_body": last["body"] if last else "",
            "last_at": fancy_time(last["created_at"]) if last else "",
            "unread": unread,
        })
    conn.close()
    return render(request, "inbox.html", {"threads": threads})


@app.get("/messages/{fid}", response_class=HTMLResponse)
async def thread(request: Request, fid: int):
    user, err = require_login(request)
    if err:
        return err
    friend = get_user(fid)
    if not friend:
        return redir("/messages", "User not found.")
    conn = db()
    conn.execute(
        "UPDATE messages SET is_read=1 WHERE sender_id=? AND receiver_id=?",
        (fid, user["id"]))
    conn.commit()
    rows = conn.execute(
        "SELECT * FROM messages WHERE "
        "((sender_id=? AND receiver_id=?) OR (sender_id=? AND receiver_id=?)) "
        "ORDER BY created_at ASC",
        (user["id"], fid, fid, user["id"])).fetchall()
    conn.close()
    msgs = [{"id": r["id"], "sender_id": r["sender_id"], "body": r["body"],
             "created_at": fancy_time(r["created_at"]),
             "mine": r["sender_id"] == user["id"]} for r in rows]
    return render(request, "thread.html", {
        "friend": {"id": friend["id"], "name": friend["name"],
                   "avatar": avatar_url(friend.get("avatar", ""))},
        "msgs": msgs})


@app.post("/messages/{fid}")
async def send_message(request: Request, fid: int):
    user, err = require_login(request)
    if err:
        return err
    friend = get_user(fid)
    if not friend:
        return redir("/messages", "User not found.")
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
           f"/messages/{user['id']}")
    return redir(f"/messages/{fid}")


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


# ---------------- search ----------------
@app.get("/search", response_class=HTMLResponse)
async def search(request: Request):
    user, err = require_login(request)
    if err:
        return err
    q = (request.query_params.get("q") or "").strip()
    users, pages = [], []
    if q:
        like = f"%{q}%"
        conn = db()
        urows = conn.execute(
            "SELECT id,name,avatar FROM users WHERE name LIKE ? OR email LIKE ? "
            "ORDER BY name LIMIT 50", (like, like)).fetchall()
        prows = conn.execute(
            "SELECT id,name,ptype FROM pages WHERE name LIKE ? ORDER BY name LIMIT 50",
            (like,)).fetchall()
        conn.close()
        users = [{"id": r["id"], "name": r["name"],
                  "avatar": avatar_url(r["avatar"])} for r in urows]
        pages = [{"id": r["id"], "name": r["name"], "ptype": r["ptype"]}
                 for r in prows]
    return render(request, "search.html", {"q": q, "users": users, "pages": pages})


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
    posts = [post_card(dict(r), user, owner_on) for r in prows]
    return render(request, "page.html",
                  {"page": page_ctx(dict(pg), user), "posts": posts})


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
    }
    conn.close()
    return render(request, "owner/dashboard.html", {
        "users": users, "friend_activity": friend_activity,
        "flags": flags, "pages": pages, "stats": stats})


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


# ---------------- health ----------------
@app.get("/healthz")
async def healthz():
    return JSONResponse({"ok": True})
