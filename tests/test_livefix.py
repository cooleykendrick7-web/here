"""Live-fix tests (Oct 28, Ken's stuck-panel report):
- join requests expire (3 min) and invites expire (10 min) in the
  sweep; 'expired' carries NO re-ask cooldown;
- recent_gifts rides a 45s window with a ts (a viewer returning
  from a minimized tab still sees the gift fly).

Run:  ./venv/bin/python tests/test_livefix.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-livefix-")
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


def signup(name, email):
    c = TestClient(here.app, follow_redirects=False)
    r = c.post("/signup", data={"name": name, "email": email,
                                "password": "pw123456"})
    assert r.status_code == 303, (name, r.status_code)
    uid = dbq("SELECT id FROM users WHERE email=?",
              (email,))[0]["id"]
    return c, uid


def ago(seconds):
    return (datetime.now(timezone.utc)
            - timedelta(seconds=seconds)).isoformat()


Hc, H = signup("Hank Host", "hank@livefix.test")
Gc, G = signup("Gail Guest", "gail@livefix.test")
Wc, W = signup("Wes Watcher", "wes@livefix.test")

r = Hc.post("/live/start", json={"title": "Fix room"})
LID = r.json()["id"]

# ---- join request: pending, then expiry, then instant re-ask ----
r = Gc.post(f"/live/{LID}/join-request")
check("request files as pending", r.status_code == 200
      and r.json()["request"]["status"] == "pending")
dbx("UPDATE live_join_requests SET created_at=? WHERE live_id=? "
    "AND user_id=?", (ago(200), LID, G))
conn = here.db()
here._sweep_lives(conn)
conn.commit()
conn.close()
row = dbq("SELECT status FROM live_join_requests WHERE live_id=? "
          "AND user_id=?", (LID, G))[0]
check("a 200s-old request is expired by the sweep",
      row["status"] == "expired", row["status"])
r = Gc.get(f"/live/{LID}/state")
check("the asker's state says expired (boxes unstick)",
      r.json()["my_join"]["status"] == "expired")
r = Gc.post(f"/live/{LID}/join-request")
check("expired carries no cooldown — instant re-ask",
      r.status_code == 200
      and r.json()["request"]["status"] == "pending",
      str(r.status_code))

# ---- invite expiry at 10 minutes ----
dbx("INSERT INTO live_join_requests(live_id, user_id, status, "
    "created_at, kind, inviter_id) VALUES(?,?, 'pending', ?, "
    "'invite', ?)", (LID, W, ago(700), H))
conn = here.db()
here._sweep_lives(conn)
conn.commit()
conn.close()
row = dbq("SELECT status FROM live_join_requests WHERE live_id=? "
          "AND user_id=? AND kind='invite'", (LID, W))[0]
check("a 700s-old invite is expired by the sweep",
      row["status"] == "expired", row["status"])

# ---- recent_gifts: ts + the 45s window ----
Hc.post("/settings/creator", data={"creator_mode": "1"})
dbx("UPDATE users SET tickets_balance=5000 WHERE id=?", (W,))
key = None
for k, g in here.GIFT_CATALOG.items():
    if g["tickets"] == 100:
        key = k
        break
Wc.post(f"/live/{LID}/heartbeat")   # presence: watcher enters
r = Wc.post(f"/live/{LID}/gift", json={"gift_key": key})
check("gift sends", r.status_code == 200, str(r.status_code))
r = Wc.get(f"/live/{LID}/state")
gifts = r.json()["recent_gifts"]
check("the gift rides state with a ts",
      len(gifts) == 1 and gifts[0]["ts"] > 0, str(gifts))
dbx("UPDATE live_gifts SET created_at=? WHERE live_id=?",
    (ago(30), LID))
r = Wc.get(f"/live/{LID}/state")
check("a 30s-old gift still flies (returning viewer's screen)",
      len(r.json()["recent_gifts"]) == 1)
dbx("UPDATE live_gifts SET created_at=? WHERE live_id=?",
    (ago(60), LID))
r = Wc.get(f"/live/{LID}/state")
check("a 60s-old gift is history", len(r.json()["recent_gifts"]) == 0)

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILS:", fails)
    sys.exit(1)
