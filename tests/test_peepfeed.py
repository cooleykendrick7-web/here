"""Peep Feed tests (Nov 9, 2026) — Ken's owner-run seed
channel for the Peeps algorithm:
- a labeled system account ('Peep Feed') exists from boot and
  never logs in; it is hidden from people search and Who's
  HERE, and it cannot be friended;
- only the owner can open the desk or upload; an upload becomes
  a public video post by the channel with hashtags parsed;
- the video shows up in a stranger's Peeps feed (GET
  /views.json) credited to 'Peep Feed';
- members can repost it to their timeline (once) and add it to
  their story (once while live);
- owner removal is a tombstone: the video leaves every feed
  but a member's existing share never breaks.

Run:  ./venv/bin/python tests/test_peepfeed.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-peepfeed-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 2048


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{extra}]" if extra and not cond else ""))


def dbq(sql, args=()):
    conn = here.db()
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def dbx(sql, args=()):
    conn = here.db()
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def signup(name, email):
    c = TestClient(here.app, follow_redirects=False)
    r = c.post("/signup", data={"name": name, "email": email,
                                "password": "pw123456"})
    assert r.status_code == 303, (name, r.status_code)
    uid = dbq("SELECT id FROM users WHERE email=?",
              (email,))[0]["id"]
    return c, uid


dbx("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
    ("pf-owner-token", here.now_iso()))
OWN = TestClient(here.app, follow_redirects=False)
OWN.cookies.set("here_owner", "pf-owner-token")

Mc, MIA = signup("Mia Member", "mia@pf.test")
Nc, NOAH = signup("Noah Newbie", "noah@pf.test")

# ---------- the channel exists and stays in its lane ----------
pf = dbq("SELECT * FROM users WHERE email='peepfeed@here.internal'")
check("Peep Feed channel account exists from boot", len(pf) == 1)
PF = pf[0]["id"]
check("channel is flagged system + named Peep Feed",
      pf[0]["is_system"] == 1 and pf[0]["name"] == "Peep Feed")
r = Mc.get("/search", params={"q": "Peep"})
check("people search never surfaces the channel",
      f"/user/{PF}" not in r.text)
ids = [p["id"] for p in here.discover_people(MIA)]
check("Who's HERE never suggests the channel", PF not in ids)
r = Mc.post(f"/friends/request/{PF}")
fr = dbq("SELECT * FROM friendships WHERE user_id=? OR "
         "friend_id=?", (PF, PF))
check("the channel cannot be friended", not fr)

# ---------- owner gates ----------
r = Mc.get("/owner/peepfeed")
check("the desk is owner-gated",
      r.status_code in (301, 302, 303, 307))
r = Mc.post("/owner/peepfeed", data={"caption": "sneaky"},
            files={"video": ("v.mp4", MP4, "video/mp4")})
n = dbq("SELECT COUNT(*) c FROM posts WHERE user_id=?",
        (PF,))[0]["c"]
check("a member cannot upload to the desk", n == 0)
r = OWN.get("/owner/peepfeed")
check("owner desk renders with the upload form",
      r.status_code == 200 and 'name="video"' in r.text
      and "Peep Feed" in r.text)

# ---------- upload -> post ----------
r = OWN.post("/owner/peepfeed",
             data={"caption": "First seed! #seedclip roll out"},
             files={"video": ("seed.mp4", MP4, "video/mp4")})
check("owner upload redirects back to the desk",
      r.status_code in (301, 302, 303, 307))
rows = dbq("SELECT * FROM posts WHERE user_id=?", (PF,))
check("the upload became a public video post by the channel",
      len(rows) == 1 and rows[0]["media"].endswith(".mp4")
      and rows[0]["privacy"] == "public"
      and rows[0]["kind"] == "video", str([dict(x) for x in rows]))
PID = rows[0]["id"]
tags = dbq("SELECT tag FROM post_hashtags WHERE post_id=?", (PID,))
check("caption hashtags are indexed", [t["tag"] for t in tags]
      == ["seedclip"], str(tags))
r = OWN.get("/owner/peepfeed")
check("the desk lists the seeded video",
      "First seed!" in r.text)

# ---------- it lands in a stranger's Peeps ----------
r = Nc.get("/views.json")
data = r.json()
views = data.get("views") or []
mine = [v for v in views if v["post_id"] == PID]
check("the seed shows in a brand-new member's Peeps feed",
      len(mine) == 1, str([v["post_id"] for v in views]))
check("the byline is the channel, nobody else",
      mine and mine[0]["author"]["name"] == "Peep Feed"
      and mine[0]["author"]["id"] == PF)
r = TestClient(here.app, follow_redirects=False).get("/views.json")
check("logged-out viewers get no feed", r.status_code == 401)

# ---------- repost to timeline ----------
r = Nc.post(f"/views/{PID}/repost")
check("repost answers ok", r.status_code == 200
      and r.json().get("ok") is True, r.text[:120])
sh = dbq("SELECT * FROM posts WHERE user_id=? AND "
         "shared_post_id=?", (NOAH, PID))
check("the repost is a share row on the member's timeline",
      len(sh) == 1)
r = Nc.post(f"/views/{PID}/repost")
check("a second repost is a no-op (already)",
      r.json().get("already") is True)
sh = dbq("SELECT * FROM posts WHERE user_id=? AND "
         "shared_post_id=?", (NOAH, PID))
check("still exactly one share row", len(sh) == 1)
n = dbq("SELECT COUNT(*) c FROM notifications WHERE user_id=? "
        "AND kind='share'", (PF,))[0]["c"]
check("the repost notifies the channel like any author "
      "(it just sits unread — the channel never logs in)", n == 1)

# ---------- add to story ----------
r = Nc.post(f"/views/{PID}/to-story")
check("add-to-story answers ok", r.status_code == 200
      and r.json().get("ok") is True, r.text[:120])
st = dbq("SELECT * FROM stories WHERE user_id=?", (NOAH,))
check("the story rides the same video file for 24h",
      len(st) == 1 and st[0]["media"] == rows[0]["media"]
      and st[0]["audience"] == "friends"
      and st[0]["expires_at"] > here.now_iso(),
      str([dict(x) for x in st]))
r = Nc.post(f"/views/{PID}/to-story")
check("adding the same video twice is a no-op",
      r.json().get("already") is True)
st = dbq("SELECT * FROM stories WHERE user_id=?", (NOAH,))
check("still exactly one story", len(st) == 1)

# ---------- removal is a tombstone ----------
r = OWN.post(f"/owner/peepfeed/{PID}/remove")
row = dbq("SELECT media, privacy FROM posts WHERE id=?",
          (PID,))[0]
check("removal tombstones the post (media blanked, privatized)",
      row["media"] == "" and row["privacy"] == "private")
r = Nc.get("/views.json")
views = r.json().get("views") or []
check("the removed video leaves the Peeps feed",
      all(v["post_id"] != PID for v in views))
r = Nc.get("/home")
check("the member's timeline still renders after removal",
      r.status_code == 200)
r = OWN.get("/owner/peepfeed")
check("the desk no longer lists the removed video",
      "First seed!" not in r.text)

# ---------- AJAX upload (the progress path) ----------
r = OWN.get("/owner/peepfeed")
check("desk carries the live-progress uploader",
      "onprogress" in r.text and 'id="pf-form"' in r.text
      and "multiple" in r.text)
r = OWN.post("/owner/peepfeed",
             data={"caption": "ajax seed"},
             files={"video": ("ajax.mp4", MP4, "video/mp4")},
             headers={"X-Peepfeed-Ajax": "1"})
j = {}
try:
    j = r.json()
except Exception:
    pass
check("AJAX upload answers JSON ok + post_id",
      r.status_code == 200 and j.get("ok") is True
      and bool(j.get("post_id")), r.text[:120])
rows = dbq("SELECT * FROM posts WHERE user_id=? AND body='ajax seed'",
           (PF,))
check("the AJAX upload still lands as a channel post",
      len(rows) == 1 and rows[0]["media"].endswith(".mp4"))
r = OWN.post("/owner/peepfeed",
             data={"caption": "bad file"},
             files={"video": ("notes.txt", b"hello", "text/plain")},
             headers={"X-Peepfeed-Ajax": "1"})
j = {}
try:
    j = r.json()
except Exception:
    pass
check("a bad file answers JSON error, not a redirect",
      r.status_code == 400 and j.get("ok") is False
      and bool(j.get("error")), r.text[:120])
r = OWN.post("/owner/peepfeed",
             data={"caption": "classic seed"},
             files={"video": ("classic.mp4", MP4, "video/mp4")})
check("the no-JS form path still redirects (fallback intact)",
      r.status_code in (301, 302, 303, 307))

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
