"""Invite Race + suggestion box tests (Oct 29):
- a join counts as REAL only when the invited account uses HERE
  (post / live visit / ticket txn); ghosts inflate raw only;
- the public board, the invite-page card, and the owner monitor
  (with the per-person drill) all read the same truth;
- the Settings idea box files suggestions, the owner reviews
  them, and 'built' thanks the suggester with a notification.

Run:  ./venv/bin/python tests/test_race.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-race-")
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
    conn = here.db()
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def dbx(sql, args=()):
    conn = here.db()
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def signup(name, email, invite_code=None):
    c = TestClient(here.app, follow_redirects=False)
    if invite_code:
        c.cookies.set("here_invite", invite_code)
    r = c.post("/signup", data={"name": name, "email": email,
                                "password": "pw123456"})
    assert r.status_code == 303, (name, r.status_code)
    uid = dbq("SELECT id FROM users WHERE email=?",
              (email,))[0]["id"]
    return c, uid


Ic, IVY = signup("Ivy Inviter", "ivy@race.test")
code = dbq("SELECT invite_code FROM users WHERE id=?",
           (IVY,))[0]["invite_code"]
check("every user already carries an invite code", bool(code))

# three people join through Ivy inside the race window
Ac, ANN = signup("Ann Active", "ann@race.test", code)
Bc, BOB = signup("Bob Ghost", "bob@race.test", code)
Cc, CAM = signup("Cam Livefan", "cam@race.test", code)
for uid in (ANN, BOB, CAM):
    dbx("UPDATE users SET created_at=? WHERE id=?",
        ("2026-11-02T15:00:00+00:00", uid))
r = dbq("SELECT invited_by FROM users WHERE id=?", (ANN,))[0]
check("signup through the link records the inviter",
      r["invited_by"] == IVY)

# Ann uses the app (a post); Cam used it too (sat in a live);
# Bob never did anything.
Ac.post("/posts", data={"kind": "text", "body": "ann first post"})
dbx("INSERT INTO live_viewers(live_id, user_id, joined_at, "
    "last_seen_at) VALUES(1, ?, '2026-11-02T16:00:00+00:00', "
    "'2026-11-02T16:05:00+00:00')", (CAM,))

conn = here.db()
board = here._race_board(conn)
mine = here._race_mine(conn, IVY)
conn.close()
ivy_row = [b for b in board if b["user_id"] == IVY]
check("board: Ivy has 3 raw signups", ivy_row and ivy_row[0]["raw"] == 3,
      str(board))
check("board: only 2 REAL joins (post + live count, ghost does not)",
      ivy_row and ivy_row[0]["qualified"] == 2, str(ivy_row))
check("mine agrees with the board",
      mine == {"raw": 3, "qualified": 2}, str(mine))

r = TestClient(here.app).get("/invite-race")
check("public race page renders with Ivy's real count",
      r.status_code == 200 and "2 real" in r.text
      and "$100 Invite Race" in r.text)
r = Ic.get("/invite")
check("invite page carries the race card with her standing",
      "The $100 Invite Race" in r.text and "2 real joins" in r.text)

# ---- owner monitor ----
r = TestClient(here.app, follow_redirects=False).get("/owner/race")
check("owner race page is owner-gated", r.status_code in (302, 303, 401, 403),
      str(r.status_code))
dbx("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
    ("race-owner-token", here.now_iso()))
OWN = TestClient(here.app, follow_redirects=False)
OWN.cookies.set("here_owner", "race-owner-token")
r = OWN.get("/owner/race")
check("owner monitor shows the drill: Ann REAL, Bob GHOST",
      r.status_code == 200 and "Ann Active" in r.text
      and "Bob Ghost" in r.text and "GHOST" in r.text
      and "IN THE MONEY ZONE" not in r.text)

# ---- suggestion box ----
r = Ic.post("/settings/suggest",
            data={"text": "Let hosts pin a comment in lives"})
check("suggestion files from Settings", r.status_code == 303)
rows = dbq("SELECT * FROM app_suggestions WHERE user_id=?", (IVY,))
check("suggestion stored as new", len(rows) == 1
      and rows[0]["status"] == "new")
r = Ic.post("/settings/suggest", data={"text": "no"})
check("a too-short idea is refused", r.status_code == 303
      and len(dbq("SELECT id FROM app_suggestions")) == 1)
r = Ic.get("/settings/profile")
check("settings shows my idea with its review status",
      "Let hosts pin a comment in lives" in r.text
      and "In review" in r.text
      and 'id="ideas"' in r.text)
r = OWN.get("/owner/suggestions")
sid = rows[0]["id"]
check("owner suggestion queue lists it",
      r.status_code == 200 and "Let hosts pin" in r.text)
r = OWN.post(f"/owner/suggestions/{sid}/status",
             data={"status": "built"})
notes = dbq("SELECT text FROM notifications WHERE user_id=? "
            "AND kind='suggestion'", (IVY,))
check("marking it shipped thanks the suggester",
      r.status_code == 303 and len(notes) == 1
      and "shipped" in notes[0]["text"])
r = Ic.get("/settings/profile")
check("settings now shows the idea as shipped",
      "Shipped" in r.text)

# ---- dashboard summary + the random feed sighting ----
r = OWN.get("/owner")
check("owner DASHBOARD carries the race summary with both leaders",
      r.status_code == 200 and "Leading on REAL joins" in r.text
      and "Ivy Inviter" in r.text
      and "Most invites sent" in r.text)

import random as _random_mod  # noqa: E402

_orig_phase = here._race_phase
_orig_random = _random_mod.random
try:
    here._race_phase = lambda: "open"
    _random_mod.random = lambda: 0.0
    r = Ic.get("/home")
    check("feed shows the race leader on a lucky load",
          "who's winning" in r.text and "is leading with" in r.text
          and "Ivy Inviter" in r.text)
    _random_mod.random = lambda: 0.99
    r = Ic.get("/home")
    check("and on an unlucky load it stays out of the feed",
          "who's winning" not in r.text)
finally:
    here._race_phase = _orig_phase
    _random_mod.random = _orig_random

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILS:", fails)
    sys.exit(1)
