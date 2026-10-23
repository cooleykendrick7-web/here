"""Games pack (Build F) tests for HERE: the weekly My Views contest,
post polls, story polls + ask-me-a-question, and live polls/trivia.

Run:  ./venv/bin/python tests/test_games_pack.py
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
_tmp = tempfile.mkdtemp(prefix="here-test-games-")
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


MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
       b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def post_video(client, body, privacy="public"):
    r = client.post("/posts", data={"kind": "video", "body": body,
                                    "privacy": privacy},
                    files={"media": ("clip.mp4", MP4, "video/mp4")})
    assert r.status_code == 303, (body, r.status_code)
    return dbq("SELECT id FROM posts WHERE body=?", (body,))[0]["id"]


def fab_event(pid, uid, kind, ts):
    dbx("INSERT OR IGNORE INTO view_events(user_id, post_id, kind,"
        " created_at) VALUES(?,?,?,?)", (uid, pid, kind, ts))


def fab_like(pid, uid, ts):
    dbx("INSERT OR IGNORE INTO likes(post_id, user_id, kind, created_at)"
        " VALUES(?,?,'love',?)", (pid, uid, ts))


def fab_comment(pid, uid, body, ts):
    dbx("INSERT INTO comments(post_id, user_id, body, created_at)"
        " VALUES(?,?,?,?)", (pid, uid, body, ts))


# ---------------- 0. migrations ----------------
_tabs = {r[0] for r in dbq(
    "SELECT name FROM sqlite_master WHERE type='table'")}
check("migration: games tables exist",
      {"polls", "poll_options", "poll_votes", "story_polls",
       "story_poll_votes", "live_polls", "live_poll_votes"} <= _tabs,
      str(_tabs))
_scols = [r[1] for r in dbq("PRAGMA table_info(stories)")]
_mcols = [r[1] for r in dbq("PRAGMA table_info(messages)")]
check("migration: stories.ama + messages.ama columns exist",
      "ama" in _scols and "ama" in _mcols)

# ---------------- 1. contest: tag + week math ----------------
TAG = here.contest_tag()
_mon, _sun = here.week_bounds()
check("contest: tag is deterministic for the week",
      here.contest_tag() == TAG
      and here.contest_tag(_mon) == TAG
      and here.contest_tag(_sun) == TAG)
check("contest: next week's tag differs (rotation moves on)",
      here.contest_tag(_mon + timedelta(days=7)) != TAG)
check("contest: week_bounds is Mon..Sun (6 days apart)",
      _mon.weekday() == 0 and _sun.weekday() == 6
      and (_sun - _mon).days == 6)
_start, _end = here._week_window_utc()
check("contest: week window spans ~7 days in UTC",
      timedelta(days=6, hours=23) < (_end - _start) < timedelta(days=7))

CA, CAID = signup("Contest Ann", "ca@games.io")
CB, CBID = signup("Contest Ben", "cb@games.io")
CV, CVID = signup("Contest Viewer", "cv@games.io")
CS, CSID = signup("Contest Stranger", "cs@games.io")
CC, CCID = signup("Contest Cam", "cc@games.io")
befriend(CV, CVID, CC, CCID)

V1 = post_video(CA, f"contest alpha entry #{TAG}")
V2 = post_video(CB, f"contest beta entry #{TAG}")
TXT = CA.post("/posts", data={"kind": "text",
                              "body": f"contest textpost #{TAG}"})
assert TXT.status_code == 303
TXTID = dbq("SELECT id FROM posts WHERE body LIKE 'contest textpost%'"
            )[0]["id"]
WRONG = post_video(CB, "contest wrongtag entry #notthecontest")
VF = post_video(CC, f"contest friends entry #{TAG}", privacy="friends")

_ts_in = (_start + timedelta(hours=2)).isoformat()
_ts_out = (_start - timedelta(days=1)).isoformat()
# V1: 3 completed (in-window) + 1 stale completed (out of window),
# 2 reactions, 1 real comment via the endpoint -> 3 + 4 + 3 = 10.
for _u in (CVID, CSID, CBID):
    fab_event(V1, _u, "completed", _ts_in)
fab_event(V1, CAID, "completed", _ts_out)
fab_like(V1, CVID, _ts_in)
fab_like(V1, CSID, _ts_in)
r = CV.post(f"/post/{V1}/comment", data={"body": "alpha fan comment"})
assert r.status_code == 303, r.status_code
# V2: 1 completed + 1 fabricated comment -> 1 + 0 + 3 = 4.
fab_event(V2, CVID, "completed", _ts_in)
fab_comment(V2, CSID, "beta comment", _ts_in)

_entries = here._contest_entries_for_viewer(here.db(), here.get_user(CVID))
_eids = [e["post_id"] for e in _entries]
_e1 = [e for e in _entries if e["post_id"] == V1][0]
_e2 = [e for e in _entries if e["post_id"] == V2][0]
check("contest: scores follow views + 2x likes + 3x comments",
      _e1["score"] == 10 and _e1["views"] == 3 and _e1["likes"] == 2
      and _e1["comments"] == 1 and _e2["score"] == 4,
      str((_e1, _e2)))
check("contest: out-of-window completed view does NOT count",
      _e1["views"] == 3)
check("contest: ranking puts the higher score first",
      _eids.index(V1) < _eids.index(V2))
check("contest: text post with the tag is excluded", TXTID not in _eids)
check("contest: wrong-tag video is excluded", WRONG not in _eids)
check("contest: friend sees the friends-only entry", VF in _eids)
_entries_s = here._contest_entries_for_viewer(here.db(),
                                              here.get_user(CSID))
check("contest: stranger does NOT see the friends-only entry",
      VF not in [e["post_id"] for e in _entries_s])

page = CV.get("/contest")
check("contest page: 200 with this week's tag + ends Sunday",
      page.status_code == 200 and f"#{TAG}" in page.text
      and "Ends Sunday" in page.text)
check("contest page: leaderboard order matches the scores",
      page.text.index("contest alpha entry")
      < page.text.index("contest beta entry"))
check("contest page: NO winner block before last week has entries",
      "Last week's winner" not in page.text)
check("contest page: stranger's page hides the friends-only entry",
      "contest friends entry" not in CS.get("/contest").text)

# --- last week's winner + tie-break (earliest post wins) ---
LASTTAG = here.contest_tag(_mon - timedelta(days=1))
_lstart, _lend = here._week_window_utc(_mon - timedelta(days=1))
_lts = (_lstart + timedelta(hours=3)).isoformat()
LD, LDID = signup("Lastweek Dan", "ld@games.io")
LE, LEID = signup("Lastweek Eve", "le@games.io")
W_LATE = post_video(LD, f"lastweek late entry #{LASTTAG}")
W_EARLY = post_video(LE, f"lastweek early entry #{LASTTAG}")
_created_late = dbq("SELECT created_at FROM posts WHERE id=?",
                    (W_LATE,))[0]["created_at"]
_early_ts = (here._parse_ts(_created_late)
             - timedelta(hours=2)).isoformat()
dbx("UPDATE posts SET created_at=? WHERE id=?", (_early_ts, W_EARLY))
# both score exactly 4: late = 2 completed + 1 like; early = 1 + 1 comment
fab_event(W_LATE, CVID, "completed", _lts)
fab_event(W_LATE, CSID, "completed", _lts)
fab_like(W_LATE, CVID, _lts)
fab_event(W_EARLY, CVID, "completed", _lts)
fab_comment(W_EARLY, CSID, "early comment", _lts)
_win = here._last_week_winner()
check("contest: last-week winner resolves, tie -> earliest post",
      _win is not None and _win["user_id"] == LEID
      and _win["score"] == 4, str(_win))
page2 = CV.get("/contest")
check("contest page: winner block shows last week's winner",
      "Last week's winner" in page2.text
      and "lastweek early entry" in page2.text)
prof_w = CV.get(f"/user/{LEID}").text
prof_l = CV.get(f"/user/{LDID}").text
check("contest: winner's profile carries the 🏆 My Views Winner chip",
      "🏆 My Views Winner" in prof_w)
check("contest: the tied loser's profile has NO winner chip",
      "🏆 My Views Winner" not in prof_l)

_tr = CV.get("/views/trending").json()["tags"]
check("contest: /views trending pins the contest chip first",
      _tr[0]["tag"] == TAG and _tr[0].get("contest") is True,
      str(_tr[:2]))
_home = CV.get("/home").text
check("contest: nav links 🏆 Contest in the chrome",
      'href="/contest"' in _home)

# ---------------- 2. post polls ----------------
PA, PAID = signup("Poll Author", "pa@games.io")
PV, PVID = signup("Poll Voter", "pv@games.io")
PW, PWID = signup("Poll Second", "pw@games.io")
PS, PSID = signup("Poll Stranger", "ps@games.io")
PF, PFID = signup("Poll Friend", "pf@games.io")
befriend(PA, PAID, PF, PFID)

_nposts = len(dbq("SELECT id FROM posts"))
r = PA.post("/posts", data={"kind": "poll", "body": "One option poll?",
                            "option1": "Only"})
check("post poll: 1 option -> refused, NO post created",
      r.status_code == 303 and len(dbq("SELECT id FROM posts")) == _nposts
      and dbq("SELECT id FROM posts WHERE body='One option poll?'") == [])
r = PA.post("/posts", data={"kind": "poll", "body": "Best snack?",
                            "option1": " Chips ", "option2": "Salsa",
                            "option3": "", "option4": "Dip"})
assert r.status_code == 303, r.status_code
POLL = dbq("SELECT * FROM posts WHERE body='Best snack?'")[0]
check("post poll: created as a text post with no media",
      POLL["kind"] == "text" and (POLL["media"] or "") == "")
_opts = dbq("SELECT * FROM poll_options WHERE post_id=? ORDER BY idx",
            (POLL["id"],))
check("post poll: options trimmed, blanks dropped (3 stored)",
      [o["text"] for o in _opts] == ["Chips", "Salsa", "Dip"],
      str([o["text"] for o in _opts]))
check("post poll: polls row exists",
      dbq("SELECT post_id FROM polls WHERE post_id=?",
          (POLL["id"],)) != [])
OID = {o["text"]: o["id"] for o in _opts}

r = PA.post("/posts", data={"kind": "poll", "body": "Long option poll?",
                            "option1": "x" * 70, "option2": "short"})
_long = dbq("SELECT text FROM poll_options WHERE post_id IN "
            "(SELECT id FROM posts WHERE body='Long option poll?') "
            "ORDER BY idx")[0]
check("post poll: options capped at 60 chars", len(_long["text"]) == 60)

dbx("DELETE FROM user_activity_days WHERE user_id=?", (PVID,))
r = PV.post(f"/post/{POLL['id']}/poll/vote", json={"option_id": OID["Chips"]})
_j = r.json()
check("post poll: first vote lands (total 1, Chips 100%)",
      r.status_code == 200 and _j.get("ok") and _j["total"] == 1
      and [o for o in _j["options"] if o["text"] == "Chips"][0]["pct"] == 100
      and _j["my_option_id"] == OID["Chips"], r.text[:200])
check("post poll: voting records streak activity",
      dbq("SELECT day FROM user_activity_days WHERE user_id=?",
          (PVID,)) != [])
r = PW.post(f"/post/{POLL['id']}/poll/vote", json={"option_id": OID["Salsa"]})
_j = r.json()
_pcts = {o["text"]: o["pct"] for o in _j["options"]}
check("post poll: second voter splits 50/50",
      _j["total"] == 2 and _pcts == {"Chips": 50, "Salsa": 50, "Dip": 0},
      str(_pcts))
r = PV.post(f"/post/{POLL['id']}/poll/vote", json={"option_id": OID["Salsa"]})
_j = r.json()
check("post poll: re-vote CHANGES the pick, never doubles",
      _j["total"] == 2
      and {o["text"]: o["votes"] for o in _j["options"]}
      == {"Chips": 0, "Salsa": 2, "Dip": 0}
      and len(dbq("SELECT * FROM poll_votes WHERE post_id=? AND user_id=?",
                  (POLL["id"], PVID))) == 1, r.text[:200])
r = PV.post(f"/post/{POLL['id']}/poll/vote", json={"option_id": 999999})
check("post poll: unknown option -> 400", r.status_code == 400)
PLAIN = PA.post("/posts", data={"kind": "text", "body": "plain post here"})
PLAINID = dbq("SELECT id FROM posts WHERE body='plain post here'")[0]["id"]
r = PV.post(f"/post/{PLAINID}/poll/vote", json={"option_id": OID["Chips"]})
check("post poll: voting on a poll-less post -> 404",
      r.status_code == 404)

_apage = PA.get(f"/post/{POLL['id']}").text
check("post poll: author sees result bars without voting",
      'class="poll-row' in _apage and "100%" in _apage
      and "Salsa" in _apage)
_vpage = PW.get(f"/post/{POLL['id']}").text
check("post poll: a voter sees results + their pick highlighted",
      'class="poll-row' in _vpage and "your pick" in _vpage)
_spage = PS.get(f"/post/{POLL['id']}").text
check("post poll: a non-voter sees tappable option buttons",
      'class="poll-opt-btn"' in _spage and 'class="poll-row' not in _spage)

r = PA.post("/posts", data={"kind": "poll", "body": "Friends only poll?",
                            "privacy": "friends",
                            "option1": "Yes", "option2": "No"})
FPOLL = dbq("SELECT * FROM posts WHERE body='Friends only poll?'")[0]
_fopts = dbq("SELECT * FROM poll_options WHERE post_id=? ORDER BY idx",
             (FPOLL["id"],))
r = PS.post(f"/post/{FPOLL['id']}/poll/vote",
            json={"option_id": _fopts[0]["id"]})
check("post poll: stranger vote on friends-only poll post -> 404",
      r.status_code == 404
      and dbq("SELECT * FROM poll_votes WHERE post_id=?",
              (FPOLL["id"],)) == [])
r = PF.post(f"/post/{FPOLL['id']}/poll/vote",
            json={"option_id": _fopts[0]["id"]})
check("post poll: a friend CAN vote on the friends-only poll",
      r.status_code == 200 and r.json().get("ok") is True)

_home = PA.get("/home").text
check("post poll: home composer has the 📊 Poll tab + option fields",
      'data-kind="poll"' in _home and 'name="option1"' in _home
      and 'name="option4"' in _home)
r = PA.post("/pages", data={"name": "Poll Page", "ptype": "community",
                            "description": "polls here"})
assert r.status_code == 303, r.status_code
PGID = dbq("SELECT id FROM pages WHERE name='Poll Page'")[0]["id"]
_ppage = PA.get(f"/page/{PGID}").text
check("post poll: page composer has the 📊 Poll tab too",
      'data-kind="poll"' in _ppage and 'name="option1"' in _ppage)
r = PA.post(f"/page/{PGID}/post",
            data={"kind": "poll", "body": "Page poll question?",
                  "option1": "Aye", "option2": "Nay"})
assert r.status_code == 303, r.status_code
_pgpoll = dbq("SELECT id FROM posts WHERE body='Page poll question?'")[0]
check("post poll: page-posted poll stores its rows",
      dbq("SELECT * FROM poll_options WHERE post_id=?",
          (_pgpoll["id"],)) != [])

# ---------------- 3. story polls + AMA ----------------
SA, SAID = signup("Story Ann", "sa@games.io")
SB, SBID = signup("Story Bob", "sb@games.io")
SC, SCID = signup("Story Stranger", "sc@games.io")
SD, SDID = signup("Story Dan", "sd@games.io")
befriend(SA, SAID, SB, SBID)
befriend(SA, SAID, SD, SDID)


def post_story(client, **fields):
    return client.post("/stories/new", data=fields,
                       files={"media": ("x.png", PNG, "image/png")})


_nstories = len(dbq("SELECT id FROM stories WHERE user_id=?", (SAID,)))
r = post_story(SA, poll_question="Tea or coffee?", poll_a="Tea")
check("story poll: partial poll -> back to /stories/new, nothing created",
      r.status_code == 303
      and r.headers.get("location", "").startswith("/stories/new")
      and len(dbq("SELECT id FROM stories WHERE user_id=?",
                  (SAID,))) == _nstories,
      r.headers.get("location", ""))
r = post_story(SA, caption="poll story", poll_question="Tea or coffee?",
               poll_a="Tea", poll_b="Coffee", ama="1")
assert r.status_code == 303, r.status_code
SID = dbq("SELECT id FROM stories WHERE user_id=? ORDER BY id DESC",
          (SAID,))[0]["id"]
_srow = dbq("SELECT * FROM stories WHERE id=?", (SID,))[0]
_spoll = dbq("SELECT * FROM story_polls WHERE story_id=?", (SID,))
check("story poll: full poll + ama stored on the story",
      _srow["ama"] == 1 and len(_spoll) == 1
      and _spoll[0]["question"] == "Tea or coffee?"
      and _spoll[0]["a_text"] == "Tea" and _spoll[0]["b_text"] == "Coffee")


def queue_story(client, sid):
    d = client.get("/stories/queue").json()
    for g in d.get("groups") or []:
        for s in g.get("stories") or []:
            if s["id"] == sid:
                return s
    return None


_qs = queue_story(SB, SID)
check("story poll: queue payload carries poll + ama for a friend",
      _qs is not None and _qs["poll"]["question"] == "Tea or coffee?"
      and _qs["poll"]["a_text"] == "Tea" and _qs["poll"]["total"] == 0
      and _qs["poll"]["my_choice"] is None and _qs["ama"] is True,
      str(_qs))
r = SB.post(f"/story/{SID}/poll/vote", json={"choice": "a"})
_j = r.json()
check("story poll: friend votes A (100%)",
      r.status_code == 200 and _j.get("ok") and _j["a_votes"] == 1
      and _j["a_pct"] == 100 and _j["my_choice"] == "a", r.text[:200])
r = SB.post(f"/story/{SID}/poll/vote", json={"choice": "b"})
_j = r.json()
check("story poll: change-vote moves the tally, no double row",
      _j["a_votes"] == 0 and _j["b_votes"] == 1 and _j["total"] == 1
      and len(dbq("SELECT * FROM story_poll_votes WHERE story_id=? "
                  "AND user_id=?", (SID, SBID))) == 1)
_qs = queue_story(SA, SID)
check("story poll: the owner sees the results in their payload",
      _qs["poll"]["b_votes"] == 1 and _qs["poll"]["my_choice"] is None)
r = SA.post(f"/story/{SID}/poll/vote", json={"choice": "a"})
check("story poll: voting on your OWN story refused (400)",
      r.status_code == 400)
r = SC.post(f"/story/{SID}/poll/vote", json={"choice": "a"})
check("story poll: non-friend vote refused (404)", r.status_code == 404)
r = SB.post(f"/story/{SID}/poll/vote", json={"choice": "c"})
check("story poll: bad choice refused (400)", r.status_code == 400)

_viewer = SB.get(f"/story/{SID}").text
check("story viewer: poll card + AMA form shells render",
      'id="sv-poll"' in _viewer and 'id="sv-ama-form"' in _viewer
      and 'id="sv-ama-input"' in _viewer)

# AMA happy path
r = SB.post(f"/story/{SID}/ama", json={"text": "Why coffee though?"})
_j = r.json()
check("AMA: friend's question accepted", r.status_code == 200
      and _j.get("ok"), r.text[:200])
_m = dbq("SELECT * FROM messages WHERE id=?", (_j.get("id"),))[0]
check("AMA: message row is story-linked, clip attached, ama=1",
      _m["sender_id"] == SBID and _m["receiver_id"] == SAID
      and _m["story_id"] == SID and _m["ama"] == 1
      and (_m["story_media"] or "").startswith("story_replies/"),
      str(dict(_m)))
_inbox = SA.get("/messages").text
check("AMA: inbox preview labels it ❓ Question about your story:",
      "❓ Question about your story: Why coffee though?" in _inbox)
_thread = SA.get(f"/messages/{SBID}").text
check("AMA: thread labels it ❓ Question about your story",
      "❓ Question about your story" in _thread)
r = SB.post(f"/story/{SID}/ama", json={"text": "   "})
check("AMA: empty question refused (400)", r.status_code == 400)
r = SB.post(f"/story/{SID}/ama", json={"text": "x" * 2001})
check("AMA: overlong question refused (400)", r.status_code == 400)

# story WITHOUT the ama flag refuses questions
r = post_story(SA, caption="plain story")
SID2 = dbq("SELECT id FROM stories WHERE user_id=? ORDER BY id DESC",
           (SAID,))[0]["id"]
r = SB.post(f"/story/{SID2}/ama", json={"text": "anyone there?"})
check("AMA: a story without the AMA box refuses (400)",
      r.status_code == 400)
r = SB.post(f"/story/{SID2}/poll/vote", json={"choice": "a"})
check("story poll: voting on a poll-less story -> 404",
      r.status_code == 404)

# blocked sender: Dan is a friend; Ann blocks him
r = SA.post(f"/block/{SDID}")
assert r.status_code in (200, 303), r.status_code
r = SD.post(f"/story/{SID}/ama", json={"text": "let me ask"})
check("AMA: blocked sender refused with the message route's wording",
      r.status_code == 403
      and r.json().get("error") == "You can't message this person.",
      r.text[:200])
r = SD.post(f"/story/{SID}/poll/vote", json={"choice": "a"})
check("story poll: blocked sender vote refused (403)",
      r.status_code == 403)

# expired story refuses votes
r = post_story(SA, caption="short lived", poll_question="Quick?",
               poll_a="Yep", poll_b="Nope")
SID3 = dbq("SELECT id FROM stories WHERE user_id=? ORDER BY id DESC",
           (SAID,))[0]["id"]
dbx("UPDATE stories SET expires_at='2000-01-01T00:00:00+00:00' "
    "WHERE id=?", (SID3,))
r = SB.post(f"/story/{SID3}/poll/vote", json={"choice": "a"})
check("story poll: expired story vote refused (404)",
      r.status_code == 404 and not r.json().get("ok"))

# ---------------- 4. live polls + trivia ----------------
LH, LHID = signup("Live Host", "lh@games.io")
LW, LWID = signup("Live Watcher", "lw@games.io")
LA, LAID = signup("Live Admin", "la@games.io")
r = LH.post("/live/start", json={"title": "Poll night"})
assert r.status_code == 200 and r.json().get("ok"), r.text[:200]
LID = r.json()["id"]
for _c in (LW, LA):
    rr = _c.post(f"/live/{LID}/heartbeat")
    assert rr.status_code == 200, rr.text[:200]
r = LH.post(f"/live/{LID}/appoint-admin", json={"user_id": LAID})
check("live poll setup: host appoints an admin",
      r.json().get("ok") is True, r.text[:200])

r = LW.post(f"/live/{LID}/poll",
            json={"question": "Watcher poll?", "a": "Yes", "b": "No"})
check("live poll: a watcher CANNOT create (403)", r.status_code == 403)
r = LH.post(f"/live/{LID}/poll",
            json={"question": "Best snack?", "a": "Chips", "b": "Salsa"})
_j = r.json()
check("live poll: host creates the first poll",
      r.status_code == 200 and _j.get("ok"), r.text[:200])
P1 = _j["poll"]["id"]
st = LW.get(f"/live/{LID}/state").json()
_ap = st.get("active_poll")
check("live poll: state carries active_poll WITHOUT correct",
      _ap is not None and _ap["id"] == P1
      and _ap["question"] == "Best snack?" and _ap["a_votes"] == 0
      and _ap["my_choice"] is None and _ap["is_trivia"] is False
      and "correct" not in _ap and st.get("last_closed_poll") is None,
      str(_ap))
r = LW.post(f"/live/{LID}/poll/{P1}/vote", json={"choice": "a"})
_j = r.json()
check("live poll: watcher votes A",
      r.status_code == 200 and _j["a_votes"] == 1
      and _j["my_choice"] == "a", r.text[:200])
r = LW.post(f"/live/{LID}/poll/{P1}/vote", json={"choice": "b"})
_j = r.json()
check("live poll: change-vote moves the tally (one row)",
      _j["a_votes"] == 0 and _j["b_votes"] == 1
      and len(dbq("SELECT * FROM live_poll_votes WHERE poll_id=? "
                  "AND user_id=?", (P1, LWID))) == 1)

r = LA.post(f"/live/{LID}/poll",
            json={"question": "Capital of France?", "a": "Paris",
                  "b": "London", "correct": "a"})
_j = r.json()
check("live poll: an ADMIN can create (trivia)", r.status_code == 200
      and _j.get("ok") and _j["poll"]["is_trivia"] is True, r.text[:200])
P2 = _j["poll"]["id"]
check("live poll: the new poll auto-closes the previous one",
      dbq("SELECT active FROM live_polls WHERE id=?", (P1,))[0]["active"] == 0
      and dbq("SELECT active FROM live_polls WHERE id=?",
              (P2,))[0]["active"] == 1)
r = LW.post(f"/live/{LID}/poll/{P1}/vote", json={"choice": "a"})
check("live poll: voting on a closed poll -> 409", r.status_code == 409)
st = LW.get(f"/live/{LID}/state").json()
_ap = st.get("active_poll")
check("live poll: trivia active_poll hides the answer",
      _ap is not None and _ap["id"] == P2 and _ap["is_trivia"] is True
      and "correct" not in _ap and st.get("last_closed_poll") is None,
      str(_ap))
LW.post(f"/live/{LID}/poll/{P2}/vote", json={"choice": "a"})   # correct
LA.post(f"/live/{LID}/poll/{P2}/vote", json={"choice": "b"})   # wrong
r = LH.post(f"/live/{LID}/poll/{P2}/end")
_j = r.json()
check("live trivia: ending reveals correct + winners_count",
      r.status_code == 200 and _j["poll"].get("correct") == "a"
      and _j["poll"].get("correct_text") == "Paris"
      and _j["poll"].get("winners_count") == 1, r.text[:300])
st = LH.get(f"/live/{LID}/state").json()
_cp = st.get("last_closed_poll")
check("live trivia: state last_closed_poll reveals the answer",
      st.get("active_poll") is None and _cp is not None
      and _cp["correct"] == "a" and _cp["winners_count"] == 1
      and _cp["a_votes"] == 1 and _cp["b_votes"] == 1, str(_cp))
_lv = LH.get(f"/live/{LID}").text
check("live page: poll card + closed reveal + manage hooks render",
      'id="live-poll-card"' in _lv and 'id="live-poll-closed"' in _lv
      and 'id="ctl-poll"' in _lv and "Correct:" in _lv)

r = LH.get(f"/live/{LID}/trivia")
_j = r.json()
check("live trivia: bank endpoint returns a full question",
      r.status_code == 200 and _j.get("ok") and bool(_j.get("question"))
      and bool(_j.get("a")) and bool(_j.get("b"))
      and _j.get("correct") in ("a", "b"), r.text[:200])
r = LW.get(f"/live/{LID}/trivia")
check("live trivia: a watcher cannot pull trivia (403)",
      r.status_code == 403)
r = LH.post(f"/live/{LID}/poll",
            json={"question": "q" * 130, "a": "One", "b": "Two"})
_j = r.json()
P3 = _j["poll"]["id"]
check("live poll: question capped at 120 chars",
      len(_j["poll"]["question"]) == 120, str(len(_j["poll"]["question"])))
r = LW.post(f"/live/{LID}/poll/{P3}/end")
check("live poll: a watcher cannot end a poll (403)",
      r.status_code == 403)
r = LH.post(f"/live/{LID}/poll",
            json={"question": "Missing a side", "a": "Only", "b": " "})
check("live poll: a blank option refuses creation (400)",
      r.status_code == 400)
LH.post(f"/live/{LID}/poll/{P3}/end")

# non-room user: blocked by the host of a second live
LH2, LH2ID = signup("Live Host Two", "lh2@games.io")
LB, LBID = signup("Live Blocked", "lb@games.io")
r = LH2.post("/live/start", json={"title": "Second room"})
LID2 = r.json()["id"]
r = LH2.post(f"/live/{LID2}/poll",
             json={"question": "Room poll?", "a": "In", "b": "Out"})
P9 = r.json()["poll"]["id"]
LH2.post(f"/block/{LBID}")
r = LB.post(f"/live/{LID2}/poll/{P9}/vote", json={"choice": "a"})
check("live poll: a non-room (blocked) user's vote is refused (403)",
      r.status_code == 403, str(r.status_code))
_anon = TestClient(here.app, follow_redirects=False)
r = _anon.post(f"/live/{LID2}/poll/{P9}/vote", json={"choice": "a"})
check("live poll: anonymous vote refused (401)", r.status_code == 401)

# ---------------- 5. inline JS syntax (node --check) ----------------
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


for _label, _html in [
        ("home", PA.get("/home").text),
        ("post_page", PA.get(f"/post/{POLL['id']}").text),
        ("page", PA.get(f"/page/{PGID}").text),
        ("story_viewer", SB.get(f"/story/{SID}").text),
        ("story_new", SA.get("/stories/new").text),
        ("views", CV.get("/views").text),
        ("contest", CV.get("/contest").text),
        ("live", LH.get(f"/live/{LID}").text)]:
    _ok, _bad = js_ok(_html, _label)
    check(f"node --check: {_label} inline JS parses", _ok, _bad)

# ---------------- 6. dev-DB boot on a copy ----------------
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
    _tabs2 = {r[0] for r in _cc.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    _scols2 = [r[1] for r in _cc.execute("PRAGMA table_info(stories)")]
    _mcols2 = [r[1] for r in _cc.execute("PRAGMA table_info(messages)")]
    _cc.close()
    check("dev DB copy: games tables + ama columns migrated in",
          {"polls", "poll_options", "poll_votes", "story_polls",
           "story_poll_votes", "live_polls", "live_poll_votes"} <= _tabs2
          and "ama" in _scols2 and "ama" in _mcols2)
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
