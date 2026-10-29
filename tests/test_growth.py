"""Growth round tests: payout proof (wall / share cards / counter),
invite bonuses (10% from HERE's side at every earn site), tip
goals, scheduled shows + reminders, Top Boards, profile/Page
shops, and the First $10 Quest. Invariant under test: bonuses are
MINTED for the inviter — the earner always keeps 100%.

Run:  ./venv/bin/python tests/test_growth.py
"""
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-growth-")
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


def bal(uid):
    r = dbq("SELECT tickets_balance, gift_earned, gift_lifetime "
            "FROM users WHERE id=?", (uid,))[0]
    return (r["tickets_balance"] or 0), (r["gift_earned"] or 0), \
        (r["gift_lifetime"] or 0)


def gift_key_for(tickets):
    for k, g in here.GIFT_CATALOG.items():
        if g["tickets"] == tickets:
            return k
    raise AssertionError(f"no {tickets}-ticket gift")


G100 = gift_key_for(100)

INVc, INV = signup("Ivy Inviter", "ivy@growth.test")
EARc, EAR = signup("Eli Earner", "eli@growth.test")
FANc, FAN = signup("Fay Fan", "fay@growth.test")
dbx("UPDATE users SET invited_by=? WHERE id=?", (INV, EAR))
EARc.post("/settings/creator", data={"creator_mode": "1"})
dbx("UPDATE users SET tickets_balance=100000 WHERE id=?", (FAN,))

# ---------------- invite bonus: helper unit ----------------
conn = here.db()
stamp = here.now_iso()
here._invite_bonus(conn, FAN, 500, stamp)          # no inviter
here._invite_bonus(conn, EAR, 9, stamp)            # under the floor
conn.commit()
conn.close()
check("bonus helper: no inviter / under 10 tickets = no mint",
      bal(INV)[1] == 0)

# ---------------- post gift: earner keeps 100%, inviter +10% ----
r = EARc.post("/posts", json={"kind": "text", "body": "eli post"})
PID = dbq("SELECT id FROM posts WHERE user_id=? ORDER BY id DESC",
          (EAR,))[0]["id"]
r = FANc.post(f"/post/{PID}/gift", json={"gift_key": G100})
check("post gift lands in full for the earner",
      r.status_code == 200 and bal(EAR)[1] == 100, str(bal(EAR)))
check("invite bonus minted on top (+10, earner untouched)",
      bal(INV)[1] == 10 and bal(EAR)[1] == 100)
rows = dbq("SELECT tickets FROM ticket_txns WHERE user_id=? AND "
           "kind='invite_bonus'", (INV,))
check("bonus is an auditable invite_bonus ledger row",
      len(rows) == 1 and rows[0]["tickets"] == 10)

# ---------------- profile gift hook ----------------
r = FANc.post(f"/user/{EAR}/gift", json={"gift_key": G100})
check("profile gift pays earner 100 + inviter 10 more",
      r.status_code == 200 and bal(EAR)[1] == 200
      and bal(INV)[1] == 20, str((bal(EAR), bal(INV))))

# ---------------- ticket door hook ----------------
r = EARc.post("/live/start", json={"title": "Door show",
                                   "entry_tickets": 200})
LID = r.json()["id"]
r = FANc.post(f"/live/{LID}/pay-entry")
check("ticket door pays host 200 + inviter 20 more",
      r.status_code == 200 and bal(EAR)[1] == 400
      and bal(INV)[1] == 40, str((bal(EAR), bal(INV))))

# ---------------- tip goal ----------------
r = EARc.post("/settings/tip-goal",
              data={"label": "New microphone", "tickets": "250"})
check("tip goal saves", r.status_code == 303)
base = bal(EAR)[2]
r = FANc.post(f"/post/{PID}/gift", json={"gift_key": G100})
r = FANc.get(f"/user/{EAR}")
check("tip goal bar shows on the profile (100 of 250, 40%)",
      "New microphone" in r.text and "100 of 250" in r.text
      and "width:40%" in r.text)
r = EARc.get(f"/user/{EAR}")
check("goal editor rides the owner's own profile",
      "Change my tip goal" in r.text)
check("bonus kept flowing during the goal gift (+10 = 50 total)",
      bal(INV)[1] == 50)

# ---------------- payout proof ----------------
dbx("UPDATE users SET gift_earned=5000 WHERE id=?", (EAR,))
r = EARc.post("/tickets/cashout", data={"payout_note": "Bank: Eli"})
pr = dbq("SELECT * FROM payout_requests WHERE user_id=? ORDER BY "
         "id DESC", (EAR,))[0]
dbx("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
    ("growth-owner-token", here.now_iso()))
OWN = TestClient(here.app, follow_redirects=False)
OWN.cookies.set("here_owner", "growth-owner-token")
r = OWN.post(f"/owner/payouts/{pr['id']}/paid")
check("cash-out completes (owner marks paid)",
      dbq("SELECT status FROM payout_requests WHERE id=?",
          (pr["id"],))[0]["status"] == "paid")

r = FANc.get("/paidwall")
check("Wall of Paid shows the total and the earner",
      r.status_code == 200 and "$25" in r.text
      and "Eli Earner" in r.text)
r = FANc.post(f"/tickets/payout/{pr['id']}/share")
check("a stranger cannot mint someone else's share card",
      r.status_code == 303
      and dbq("SELECT share_token FROM payout_requests WHERE id=?",
              (pr["id"],))[0]["share_token"] is None)
r = EARc.post(f"/tickets/payout/{pr['id']}/share")
loc = r.headers.get("location", "")
tok = dbq("SELECT share_token FROM payout_requests WHERE id=?",
          (pr["id"],))[0]["share_token"]
check("earner mints the card and lands on it",
      r.status_code == 303 and tok and loc == f"/paid/{tok}")
ANON = TestClient(here.app, follow_redirects=False)
r = ANON.get(f"/paid/{tok}")
check("the public card shows the money, logged out",
      r.status_code == 200 and "just got paid" in r.text
      and "$25" in r.text)
r = ANON.get("/paid/nope-not-a-token")
check("a bogus card token shows the unavailable card",
      r.status_code == 200 and "isn't available" in r.text)
r = ANON.get("/signup")
check("signup page carries the paid-out proof line",
      "paid out so far" in r.text)
r = FANc.get("/home")
check("home carries the Wall of Paid strip", "Wall of Paid" in r.text)

# ---------------- scheduled shows + reminders ----------------
chi = ZoneInfo("America/Chicago")
when = (datetime.now(chi) + timedelta(days=1)).replace(
    hour=20, minute=0, second=0, microsecond=0)
r = EARc.post("/live/schedule",
              data={"title": "Friday Talk",
                    "starts_at": when.strftime("%Y-%m-%dT%H:%M"),
                    "entry_tickets": "0"})
sched = dbq("SELECT * FROM live_schedule WHERE host_id=?",
            (EAR,))[0]
check("show schedules", r.status_code == 303
      and sched["title"] == "Friday Talk")
r = FANc.get("/shows")
check("shows page lists the upcoming show", "Friday Talk" in r.text)
r = FANc.post(f"/live/schedule/{sched['id']}/remind")
j = r.json()
check("remind subscribes", j["ok"] and j["reminded"]
      and j["count"] == 1, str(j))
r = FANc.post(f"/live/schedule/{sched['id']}/remind")
check("remind toggles back off", r.json()["reminded"] is False)
FANc.post(f"/live/schedule/{sched['id']}/remind")
r = EARc.post("/live/start", json={"title": "Too early"})
check("starting a day early does NOT link the show",
      dbq("SELECT live_id FROM live_schedule WHERE id=?",
          (sched["id"],))[0]["live_id"] is None)
dbx("UPDATE live_schedule SET starts_at=? WHERE id=?",
    (time.time(), sched["id"]))
r = EARc.post("/live/start", json={"title": "Friday Talk LIVE"})
lid2 = r.json()["id"]
row = dbq("SELECT live_id FROM live_schedule WHERE id=?",
          (sched["id"],))[0]
check("starting on time links the show to the live",
      row["live_id"] == lid2)
notes = dbq("SELECT * FROM notifications WHERE user_id=? AND "
            "kind='show_start'", (FAN,))
check("the subscriber got rung (show_start notification)",
      len(notes) == 1 and f"/live/{lid2}" in (notes[0]["link"] or ""))

# ---------------- Top Boards ----------------
r = FANc.get("/top")
check("Top Boards: earner tops earners, fan tops gifters",
      r.status_code == 200 and "Top Earners" in r.text
      and "Eli Earner" in r.text and "Fay Fan" in r.text)

# ---------------- shops ----------------
r = EARc.post("/market", data={"title": "Eli Keyboard",
                               "price": "40", "category": "Other",
                               "condition": "Good", "description": "",
                               "location": ""})
r = FANc.get(f"/user/{EAR}/shop")
check("profile shop lists the seller's listings",
      r.status_code == 200 and "Eli Keyboard" in r.text
      and "Eli Earner" in r.text)
dbx("INSERT INTO pages(owner_id, name, created_at) VALUES(?,?,?)",
    (EAR, "Eli's Page", here.now_iso()))
pgid = dbq("SELECT id FROM pages WHERE owner_id=?", (EAR,))[0]["id"]
r = FANc.get(f"/page/{pgid}/shop")
check("Page shop shows the page owner's listings",
      r.status_code == 200 and "Eli Keyboard" in r.text)
r = FANc.get(f"/page/{pgid}")
check("the Page itself carries the Shop chip",
      f"/page/{pgid}/shop" in r.text)

# ---------------- First $10 Quest ----------------
NEWc, NEW = signup("Ned New", "ned@growth.test")
r = NEWc.get("/earnings")
check("quest: a brand-new member sees the road (0 of 7)",
      "Your road to your first $10" in r.text
      and "0 of 7 steps done" in r.text)
r = EARc.get("/tickets")
check("quest: the earner's progress is honest (5 of 7)",
      "5 of 7 steps done" in r.text, "")
r = INVc.get("/earnings")
check("earnings shows the invite-bonus line (50 total)",
      "Invite bonuses" in r.text and "50 🎟️" in r.text)
r = INVc.get("/invite")
check("invite page explains the 10% and the running total",
      "10% of everything your people earn" in r.text
      and "50" in r.text)

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILS:", fails)
    sys.exit(1)
