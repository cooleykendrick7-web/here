"""Welcome flow + Daily Challenge tests for HERE (growth pack E):
onboarded migration/backfill, signup -> /welcome, topics + affinity
seeding, follow suggestions ranking, trending step, daily prompt
determinism, challenge answers, streak math, activity hooks, profile
chip, home banner, inline-JS syntax, dev-DB boot-on-copy.

Run:  ./venv/bin/python tests/test_welcome_daily.py
Uses a throwaway DB/uploads dir; no network.
"""
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-wd-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{extra}]" if extra and not cond else ""))


def dbq(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"])
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def dbx(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"])
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def signup(name, email):
    c = TestClient(here.app, follow_redirects=False)
    r = c.post("/signup", data={"name": name, "email": email,
                                "password": "pw123456"})
    assert r.status_code == 303, (name, r.status_code)
    uid = dbq("SELECT id FROM users WHERE email=?", (email,))[0]["id"]
    return c, uid


def befriend(c1, u1, c2, u2):
    r = c2.post(f"/friends/request/{u1}")
    assert r.status_code in (200, 303), r.status_code
    r = c1.post(f"/friends/accept/{u2}")
    assert r.status_code in (200, 303), r.status_code


def aff(uid, kind, key):
    rows = dbq("SELECT score FROM user_affinity WHERE user_id=? "
               "AND kind=? AND key=?", (uid, kind, key))
    return rows[0]["score"] if rows else 0.0


JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64 + b"\xff\xd9"
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64

# ---------------- 1. migration: backfill proof ----------------
# Mirror test_live_migrate.py: build an OLD-shape DB (users table
# WITHOUT onboarded, one pre-existing row) and boot the app against
# it in a subprocess — the migration must mark that row onboarded=1
# while a row inserted afterwards (no onboarded named) defaults to 0.
old_db = os.path.join(_tmp, "old.db")
_oc = sqlite3.connect(old_db)
_oc.executescript("""
CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, email TEXT NOT NULL UNIQUE, pw_hash TEXT NOT NULL,
  bio TEXT DEFAULT '', avatar TEXT DEFAULT '', last_seen TEXT,
  last_seen_at TEXT, status_mode TEXT DEFAULT 'auto',
  inbox_status INTEGER DEFAULT 1, created_at TEXT NOT NULL);
INSERT INTO users(id,name,email,pw_hash,created_at) VALUES
  (1,'Old Timer','old@wd.io','x','2026-01-01T00:00:00+00:00');
""")
_oc.commit()
_oc.close()
_env = dict(os.environ, HERE_DB=old_db,
            HERE_UPLOADS=os.path.join(_tmp, "oldup"))
_code = (
    "import sys; sys.path.insert(0, " + repr(BASE) + ");"
    "import app as here;"
    "c = here.db();"
    "r = c.execute('SELECT onboarded FROM users WHERE id=1').fetchone();"
    "print('old', r['onboarded']);"
    "c.execute(\"INSERT INTO users(name,email,pw_hash,created_at) "
    "VALUES('Newbie','new@wd.io','x','2026-10-22T00:00:00+00:00')\");"
    "c.commit();"
    "r = c.execute('SELECT onboarded FROM users WHERE id=2').fetchone();"
    "print('new', r['onboarded']);"
)
_r = subprocess.run([sys.executable, "-c", _code], cwd=BASE,
                    capture_output=True, text=True, env=_env)
check("migration: old-DB boot has no exception", _r.returncode == 0,
      _r.stderr[-400:])
check("migration: pre-existing user backfilled onboarded=1",
      "old 1" in _r.stdout, _r.stdout)
check("migration: row inserted after migration defaults onboarded=0",
      "new 0" in _r.stdout, _r.stdout)
_ocol = sqlite3.connect(old_db)
_tabs = {r[0] for r in _ocol.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}
_ucols = [r[1] for r in _ocol.execute("PRAGMA table_info(users)")]
_pcols = [r[1] for r in _ocol.execute("PRAGMA table_info(posts)")]
_ocol.close()
check("migration: user_topics + user_activity_days tables created",
      {"user_topics", "user_activity_days"} <= _tabs, str(_tabs))
check("migration: users.onboarded + posts.challenge_date columns exist",
      "onboarded" in _ucols and "challenge_date" in _pcols)
# (a) on the live test DB: the column exists and a raw INSERT that
# doesn't name it gets 0.
dbx("INSERT INTO users(name, email, pw_hash, created_at) "
    "VALUES('Raw Row', 'raw@wd.io', 'x', '2026-10-22T00:00:00+00:00')")
check("migration: raw INSERT without onboarded gets 0 on the test DB",
      dbq("SELECT onboarded FROM users WHERE email='raw@wd.io'")
      [0]["onboarded"] == 0)
check("chicago helpers: _today_str matches _today_chicago",
      here._today_str() == here._today_chicago().isoformat()
      and here._day_ordinal() == here._today_chicago().toordinal())

# ---------------- 2. signup / login / done ----------------
L, LID = signup("Login Lisa", "lisa@wd.io")
# signup() already asserted 303; do an explicit fresh signup to see
# the Location header.
_c2 = TestClient(here.app, follow_redirects=False)
_r2 = _c2.post("/signup", data={"name": "Fresh Fred",
                                "email": "fred@wd.io",
                                "password": "pw123456"})
check("new signup -> Location /welcome",
      _r2.status_code == 303
      and _r2.headers.get("location") == "/welcome",
      _r2.headers.get("location", ""))
FREDID = dbq("SELECT id FROM users WHERE email='fred@wd.io'")[0]["id"]
check("new signup row has onboarded=0",
      dbq("SELECT onboarded FROM users WHERE id=?", (FREDID,))
      [0]["onboarded"] == 0)
_login = TestClient(here.app, follow_redirects=False)
_rl = _login.post("/login", data={"email": "lisa@wd.io",
                                  "password": "pw123456"})
check("login -> Location /home (NOT /welcome)",
      _rl.status_code == 303
      and _rl.headers.get("location") == "/home",
      _rl.headers.get("location", ""))
_anon = TestClient(here.app, follow_redirects=False)
_ra = _anon.get("/welcome")
check("anon GET /welcome -> login redirect",
      _ra.status_code == 303 and "/login" in _ra.headers.get("location", ""))
_r = _c2.post("/welcome/done")
check("POST /welcome/done -> /home and onboarded=1 in DB",
      _r.status_code == 303 and _r.headers.get("location") == "/home"
      and dbq("SELECT onboarded FROM users WHERE id=?", (FREDID,))
      [0]["onboarded"] == 1)
_r = _c2.get("/welcome")
check("GET /welcome as an onboarded user -> 200 (re-pick allowed)",
      _r.status_code == 200 and "Welcome to HERE" in _r.text)

# ---------------- 3. topics ----------------
W, WID = signup("Wel Wendy", "wendy@wd.io")
_r = W.post("/welcome/topics",
            json={"topics": ["music", "cars", "bogus", "music"]})
_j = _r.json()
_saved = {row["topic"] for row in dbq(
    "SELECT topic FROM user_topics WHERE user_id=?", (WID,))}
check("topics: bogus dropped, deduped -> {music, cars}",
      _r.status_code == 200 and _j.get("ok") is True
      and set(_j.get("topics") or []) == {"music", "cars"}
      and _saved == {"music", "cars"}, str(_j) + " " + str(_saved))
check("topics: affinity seeded +4.0 for music and cars",
      abs(aff(WID, "tag", "music") - 4.0) < 1e-9
      and abs(aff(WID, "tag", "cars") - 4.0) < 1e-9,
      f"music={aff(WID, 'tag', 'music')} cars={aff(WID, 'tag', 'cars')}")
_r = W.post("/welcome/topics", json={"topics": ["faith"]})
_saved = {row["topic"] for row in dbq(
    "SELECT topic FROM user_topics WHERE user_id=?", (WID,))}
check("topics: re-save replaces the set -> {faith} only",
      _saved == {"faith"}, str(_saved))
_r = W.post("/welcome/topics", json={
    "topics": ["music", "cars", "comedy", "faith", "sports", "food",
               "gaming"]})
_saved = [row["topic"] for row in dbq(
    "SELECT topic FROM user_topics WHERE user_id=?", (WID,))]
check("topics: 7 valid picks capped at exactly 5 (first 5 win)",
      _r.json().get("topics") == ["music", "cars", "comedy", "faith",
                                  "sports"]
      and len(_saved) == 5, str(_r.json()))

# ---------------- 4. suggestions ranking ----------------
X, XID = signup("Xavier Tags", "xavier@wd.io")
Y, YID = signup("Yara Plain", "yara@wd.io")
Z, ZID = signup("Zed Blocked", "zed@wd.io")
F1, F1ID = signup("Follower Fred", "ffred@wd.io")
X.post("/posts", data={"kind": "text",
                       "body": "my garage jams #music #cars"})
Y.post("/posts", data={"kind": "text",
                       "body": "kitchen talk #cooking #random"})
Z.post("/posts", data={"kind": "text",
                       "body": "zed tunes #music #cars"})
# equal follower counts for X and Y (W + F1 follow both)
for _f in (WID, F1ID):
    for _t in (XID, YID):
        dbx("INSERT OR IGNORE INTO follows(follower_id, followee_id, "
            "created_at) VALUES(?,?,?)",
            (_f, _t, "2026-10-22T00:00:00+00:00"))
W.post("/welcome/topics", json={"topics": ["music", "cars"]})
_sugg = W.get("/welcome/suggestions.json").json()["suggestions"]
_ids = [s["id"] for s in _sugg]
check("suggestions: X (matching tags) ranks above Y (unmatched)",
      XID in _ids and YID in _ids and _ids.index(XID) < _ids.index(YID),
      str(_ids))
_xrow = [s for s in _sugg if s["id"] == XID][0]
check("suggestions: X.matched_topics includes music + cars",
      set(_xrow["matched_topics"]) == {"music", "cars"},
      str(_xrow["matched_topics"]))
check("suggestions: follower_count is real (2 for X and Y)",
      _xrow["follower_count"] == 2
      and [s for s in _sugg if s["id"] == YID][0]["follower_count"] == 2)
_r = W.post(f"/follow/{XID}")
_sugg = W.get("/welcome/suggestions.json").json()["suggestions"]
_xrow = [s for s in _sugg if s["id"] == XID][0]
check("follow toggle: POST /follow/{X} -> 303, DB row, following=true",
      _r.status_code == 303
      and dbq("SELECT 1 FROM follows WHERE follower_id=? AND "
              "followee_id=?", (WID, XID)) != []
      and _xrow["following"] is True)
W.post(f"/block/{ZID}")
_sugg = W.get("/welcome/suggestions.json").json()["suggestions"]
check("suggestions: blocked-either-way author is excluded",
      ZID not in [s["id"] for s in _sugg])

# ---------------- 5. trending step ----------------
A, AID = signup("Author Ann", "ann@wd.io")
B, BID = signup("Buddy Ben", "ben@wd.io")
C, CID = signup("Carol Stranger", "carol@wd.io")
A.post("/posts", data={"kind": "video", "body": "ann public view",
                       "privacy": "public"},
       files={"media": ("ann.mp4", MP4, "video/mp4")})
C.post("/posts", data={"kind": "video", "body": "carol friends view",
                       "privacy": "friends"},
       files={"media": ("carol.mp4", MP4, "video/mp4")})
_amedia = dbq("SELECT media FROM posts WHERE body='ann public view'")[0]["media"]
_cmedia = dbq("SELECT media FROM posts WHERE body='carol friends view'")[0]["media"]
_wel = B.get("/welcome").text
check("trending: public video appears in another user's /welcome",
      ("/uploads/" + _amedia) in _wel)
check("trending: stranger's friends-only video NOT in /welcome",
      ("/uploads/" + _cmedia) not in _wel)

# ---------------- 6. daily prompt determinism ----------------
_d1 = A.get("/daily").text
_d2 = B.get("/daily").text
check("daily: two users see the same prompt, == daily_prompt()",
      here.daily_prompt() in _d1 and here.daily_prompt() in _d2
      and here.daily_prompt() in _d1)

# ---------------- 7. answering the challenge ----------------
befriend(A, AID, B, BID)
_r = A.post("/posts", data={"kind": "photo", "body": "My porch",
                            "challenge": "1", "next": "/daily"},
            files={"media": ("porch.jpg", JPEG, "image/jpeg")})
_prow = dbq("SELECT * FROM posts WHERE body LIKE 'My porch%'")[0]
check("answer: POST /posts challenge=1 -> next /daily honored",
      _r.status_code == 303
      and _r.headers.get("location", "").startswith("/daily"),
      _r.headers.get("location", ""))
check("answer: post row challenge_date == today (Chicago)",
      _prow["challenge_date"] == here._today_str(),
      str(_prow["challenge_date"]) + " vs " + here._today_str())
check("answer: #HereDaily stored in post_hashtags as 'heredaily'",
      "heredaily" in {r["tag"] for r in dbq(
          "SELECT tag FROM post_hashtags WHERE post_id=?",
          (_prow["id"],))})
A.post("/posts", data={"kind": "text", "body": "an ordinary post"})
_orow = dbq("SELECT * FROM posts WHERE body='an ordinary post'")[0]
check("answer: normal post -> challenge_date NULL, no heredaily tag",
      _orow["challenge_date"] is None
      and dbq("SELECT tag FROM post_hashtags WHERE post_id=?",
              (_orow["id"],)) == [])
check("answer: friend B sees A's answer on /daily",
      "My porch" in B.get("/daily").text)
D, DID = signup("Blocker Dan", "dan@wd.io")
D.post(f"/block/{AID}")
check("answer: a user who blocked the author does NOT see it",
      "My porch" not in D.get("/daily").text)

# ---------------- 8. yesterday line ----------------
_yday = here._today_chicago() - timedelta(days=1)
_src = dbq("SELECT * FROM posts WHERE id=?", (_prow["id"],))[0]
dbx("INSERT INTO posts(user_id, page_id, kind, body, feeling, link_url,"
    " video_url, media, privacy, music_video_id, music_title,"
    " music_artist, music_thumb, music_start, created_at,"
    " challenge_date) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (_src["user_id"], None, "text", "yesterday unique answer", "", "",
     "", "", "public", "", "", "", "", 0,
     "2026-10-21T12:00:00+00:00", _yday.isoformat()))
_dt = B.get("/daily").text
_m = re.search(r"Yesterday: .*? — (\d+) answer", _dt)
check("yesterday: /daily shows yesterday's prompt + count >= 1",
      here.daily_prompt(_yday) in _dt and _m is not None
      and int(_m.group(1)) >= 1,
      (_m.group(0) if _m else "no yesterday line found"))

# ---------------- 9. streak math (unit) ----------------
def fab_days(uid, offsets):
    dbx("DELETE FROM user_activity_days WHERE user_id=?", (uid,))
    today = here._today_chicago()
    for off in offsets:
        dbx("INSERT OR IGNORE INTO user_activity_days(user_id, day) "
            "VALUES(?,?)", (uid, (today - timedelta(days=off)).isoformat()))

SA, SAID = signup("Streak Ann", "sa@wd.io")
SB, SBID = signup("Streak Bob", "sb@wd.io")
SC, SCID = signup("Streak Cat", "sc@wd.io")
SD, SDID = signup("Streak Dot", "sd@wd.io")
fab_days(SAID, [0, 1, 2])
check("streak: [today, -1, -2] == 3", here.current_streak(SAID) == 3,
      str(here.current_streak(SAID)))
fab_days(SBID, [0, 1, 3])
check("streak: [today, -1, -3] == 2 (gap stops the count)",
      here.current_streak(SBID) == 2, str(here.current_streak(SBID)))
fab_days(SCID, [1, 2])
check("streak: [yesterday, -2] == 2 (still alive without today)",
      here.current_streak(SCID) == 2, str(here.current_streak(SCID)))
fab_days(SDID, [3])
check("streak: [3 days ago] only == 0", here.current_streak(SDID) == 0,
      str(here.current_streak(SDID)))

# ---------------- 10. activity hooks (endpoint level) ----------------
P1, P1ID = signup("Poster Pam", "pam@wd.io")
dbx("DELETE FROM user_activity_days WHERE user_id=?", (P1ID,))
P1.post("/posts", data={"kind": "text", "body": "pam activity post"})
check("activity: posting records today",
      dbq("SELECT day FROM user_activity_days WHERE user_id=?",
          (P1ID,))[0]["day"] == here._today_str())
P2, P2ID = signup("Reactor Rob", "rob@wd.io")
dbx("DELETE FROM user_activity_days WHERE user_id=?", (P2ID,))
P2.post(f"/post/{_prow['id']}/react", data={"kind": "love"})
check("activity: a reaction that lands records today",
      len(dbq("SELECT day FROM user_activity_days WHERE user_id=?",
              (P2ID,))) == 1)
dbx("DELETE FROM user_activity_days WHERE user_id=?", (P2ID,))
P2.post(f"/post/{_prow['id']}/react", data={"kind": "love"})
check("activity: toggling a reaction OFF records nothing",
      dbq("SELECT day FROM user_activity_days WHERE user_id=?",
          (P2ID,)) == [])
P3, P3ID = signup("Commenter Cam", "cam@wd.io")
dbx("DELETE FROM user_activity_days WHERE user_id=?", (P3ID,))
P3.post(f"/post/{_prow['id']}/comment", data={"body": "nice porch"})
check("activity: commenting records today",
      len(dbq("SELECT day FROM user_activity_days WHERE user_id=?",
              (P3ID,))) == 1)
P4, P4ID = signup("Homey Hal", "hal@wd.io")
dbx("DELETE FROM user_activity_days WHERE user_id=?", (P4ID,))
P4.get("/home")
check("activity: visiting /home records today",
      len(dbq("SELECT day FROM user_activity_days WHERE user_id=?",
              (P4ID,))) == 1)

# ---------------- 11. profile streak chip ----------------
Q3, Q3ID = signup("Chip Three", "chip3@wd.io")
fab_days(Q3ID, [0, 1, 2])
_prof = W.get(f"/user/{Q3ID}").text
check("profile chip: 3-day streak shows '🔥 3 day streak'",
      "🔥 3 day streak" in _prof)
Q1, Q1ID = signup("Chip One", "chip1@wd.io")
fab_days(Q1ID, [0])
_prof1 = W.get(f"/user/{Q1ID}").text
check("profile chip: 1-day streak shows NO chip",
      "day streak" not in _prof1)

# ---------------- 12. home banner ----------------
_home = B.get("/home").text
check("home banner: 'Today's Challenge' + today's prompt on /home",
      "Today's Challenge" in _home and here.daily_prompt() in _home
      and 'id="daily-banner"' in _home)

# ---------------- 13. inline JS syntax (node --check) ----------------
def js_ok(html_text, tag):
    blocks = re.findall(r"<script>(.*?)</script>", html_text, re.S)
    ok, bad = True, []
    for i, b in enumerate(blocks):
        if not b.strip():
            continue
        path = os.path.join(_tmp, f"{tag}_{i}.js")
        with open(path, "w") as fh:
            fh.write(b)
        p = subprocess.run(["node", "--check", path],
                           capture_output=True, text=True)
        if p.returncode != 0:
            ok = False
            bad.append(p.stderr.strip()[:160])
    return ok, "; ".join(bad)

_ok, _bad = js_ok(B.get("/welcome").text, "wel")
check("node --check: /welcome inline JS parses", _ok, _bad)
_ok, _bad = js_ok(B.get("/daily").text, "day")
check("node --check: /daily inline JS parses", _ok, _bad)
_ok, _bad = js_ok(B.get("/home").text, "home")
check("node --check: /home (incl. daily banner) inline JS parses",
      _ok, _bad)
check("nav: base chrome links 🔥 Daily -> /daily",
      'href="/daily"' in B.get("/home").text)

# ---------------- 14. dev-DB boot on a copy ----------------
dev_db = os.path.join(BASE, "here.db")
if os.path.exists(dev_db):
    before = os.path.getmtime(dev_db)
    copy_db = os.path.join(_tmp, "devcopy.db")
    shutil.copyfile(dev_db, copy_db)
    env2 = dict(os.environ, HERE_DB=copy_db,
                HERE_UPLOADS=os.path.join(_tmp, "devup"))
    boot = subprocess.run(
        [sys.executable, "-c",
         "import app; from fastapi.testclient import TestClient;"
         "print(TestClient(app.app).get('/login').status_code)"],
        cwd=BASE, env=env2, capture_output=True, text=True)
    check("dev DB copy: boots without exception (login 200)",
          boot.returncode == 0 and boot.stdout.strip() == "200",
          (boot.stdout + boot.stderr)[-300:])
    _cc = sqlite3.connect(copy_db)
    _tabs = {r[0] for r in _cc.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    _ucols = [r[1] for r in _cc.execute("PRAGMA table_info(users)")]
    _pcols = [r[1] for r in _cc.execute("PRAGMA table_info(posts)")]
    _cc.close()
    check("dev DB copy: new tables + columns migrated in",
          {"user_topics", "user_activity_days"} <= _tabs
          and "onboarded" in _ucols and "challenge_date" in _pcols)
    check("dev DB copy: the original dev DB file was not modified",
          os.path.getmtime(dev_db) == before)
else:
    check("dev DB copy: skipped (no here.db in repo)", True)

# ---------------- summary ----------------
fails = [n for n, okk in RESULTS if not okk]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
