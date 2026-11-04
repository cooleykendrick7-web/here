"""Killer-fix tests (Nov 4, 2026): the platform-killing holes
closed in one pass —
 * WAL + busy_timeout on every DB connection
 * a block expires pending call requests / live join requests,
   and approval endpoints re-check the block at decide time
 * the Stripe webhook only credits PAID sessions
 * login / owner-login / signup throttles + 8-char password floor
 * sessions expire server-side after 30 days
 * /healthz actually checks the DB

Run:  ./venv/bin/python tests/test_killer_fixes.py
"""
import hashlib
import hmac
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-killer-")
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


def signup(name, email, password="pw123456"):
    c = TestClient(here.app, follow_redirects=False)
    r = c.post("/signup", data={"name": name, "email": email,
                                "password": password})
    assert r.status_code == 303, (name, r.status_code, r.text[:200])
    uid = dbq("SELECT id FROM users WHERE email=?", (email,))[0]["id"]
    return c, uid


# ---------------- WAL / busy timeout ----------------
conn = here.db()
mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
conn.close()
check("DB runs in WAL mode", str(mode).lower() == "wal", str(mode))
check("DB busy_timeout is 10000", busy == 10000, str(busy))

# ---------------- healthz ----------------
ANON = TestClient(here.app, follow_redirects=False)
r = ANON.get("/healthz")
check("healthz 200 with DB up", r.status_code == 200
      and r.json().get("ok") is True)

# ---------------- password floor ----------------
r = ANON.post("/signup", data={"name": "Short Pw", "email": "short@k.test",
                               "password": "abc123"})
from urllib.parse import unquote_plus
check("signup with 6-char password refused", r.status_code == 303
      and "at least 8" in unquote_plus(r.headers.get("location", "")))
check("short-password user was not created",
      not dbq("SELECT id FROM users WHERE email='short@k.test'"))

# ---------------- block kills pending call requests ----------------
Ac, AID = signup("Ann Host", "ann@k.test")
Bc, BID = signup("Bob Caller", "bob@k.test")
r = Bc.post("/call-requests", json={"user_id": AID, "kind": "voice"})
check("call request created", r.status_code == 200
      and r.json().get("ok"), r.text[:160])
rid = r.json()["request"]["id"]
r = Ac.get("/call-requests/incoming")
check("incoming lists the pending request",
      any(x["id"] == rid for x in r.json().get("requests", [])))
Ac.post(f"/block/{BID}")
st = dbq("SELECT status FROM call_requests WHERE id=?", (rid,))[0]["status"]
check("blocking expires the pending call request", st == "expired", st)
r = Ac.get("/call-requests/incoming")
check("incoming no longer lists it",
      all(x["id"] != rid for x in r.json().get("requests", [])))
r = Ac.post(f"/call-requests/{rid}/approve")
check("approving the expired request fails", r.status_code == 409,
      str(r.status_code))

# approve-time gate: a pending row that exists DESPITE a block
# (e.g. created before this fix shipped) still cannot be approved.
dbx("INSERT INTO call_requests(requester_id, target_id, kind, status, "
    "created_at) VALUES(?,?,?,'pending',?)",
    (BID, AID, "voice", here.now_iso()))
rid2 = dbq("SELECT id FROM call_requests WHERE requester_id=? AND "
           "status='pending'", (BID,))[0]["id"]
r = Ac.get("/call-requests/incoming")
check("incoming hides blocked requester even for legacy rows",
      all(x["id"] != rid2 for x in r.json().get("requests", [])))
r = Ac.post(f"/call-requests/{rid2}/approve")
check("approve after block -> 403", r.status_code == 403,
      str(r.status_code))
st = dbq("SELECT status FROM call_requests WHERE id=?",
         (rid2,))[0]["status"]
check("refused approval expires the request", st == "expired", st)
Ac.post(f"/unblock/{BID}")

# ---------------- block kills pending live join requests ----------------
Hc, HID = signup("Hattie Host", "hattie@k.test")
Gc, GID = signup("Gus Guest", "gus@k.test")
r = Hc.post("/live/start", json={"title": "Killer-fix stage"})
LID = r.json()["id"]
Gc.post(f"/live/{LID}/heartbeat")
r = Gc.post(f"/live/{LID}/join-request")
jr = r.json()["request"]["id"]
Hc.post(f"/block/{GID}")
st = dbq("SELECT status FROM live_join_requests WHERE id=?",
         (jr,))[0]["status"]
check("blocking declines the pending live join request",
      st == "declined", st)
# legacy-row gate at decide time
dbx("INSERT INTO live_join_requests(live_id, user_id, status, "
    "created_at, kind) VALUES(?,?,'pending',?,'request')",
    (LID, GID, here.now_iso()))
jr2 = dbq("SELECT id FROM live_join_requests WHERE live_id=? AND "
          "status='pending'", (LID,))[0]["id"]
r = Hc.post(f"/live/{LID}/join-decide",
            json={"request_id": jr2, "approve": True})
check("join-decide refuses a blocked applicant", r.status_code == 403,
      str(r.status_code))
check("blocked applicant was not seated",
      not dbq("SELECT user_id FROM live_members WHERE live_id=? AND "
              "user_id=? AND left_at IS NULL", (LID, GID)))
Hc.post(f"/unblock/{GID}")

# ---------------- webhook only credits PAID ----------------
TEST_SECRET = "whsec_killertest"
os.environ["STRIPE_WEBHOOK_SECRET"] = TEST_SECRET
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake"


def webhook_event(obj):
    body = json.dumps({"type": "checkout.session.completed",
                       "data": {"object": obj}}).encode()
    t = int(time.time())
    sig = hmac.new(TEST_SECRET.encode(), f"{t}.".encode() + body,
                   hashlib.sha256).hexdigest()
    return ANON.post("/api/stripe/webhook", content=body,
                     headers={"Stripe-Signature": f"t={t},v1={sig}"})


bal0 = dbq("SELECT tickets_balance FROM users WHERE id=?",
           (AID,))[0]["tickets_balance"]
r = webhook_event({"id": "cs_unpaid_1", "payment_status": "unpaid",
                   "metadata": {"user_id": str(AID),
                                "pack_key": "pack100", "tickets": "100"}})
bal1 = dbq("SELECT tickets_balance FROM users WHERE id=?",
           (AID,))[0]["tickets_balance"]
check("webhook does NOT credit an unpaid session",
      r.status_code == 200 and bal1 == bal0, f"{bal0}->{bal1}")
r = webhook_event({"id": "cs_paid_1", "payment_status": "paid",
                   "metadata": {"user_id": str(AID),
                                "pack_key": "pack100", "tickets": "100"}})
bal2 = dbq("SELECT tickets_balance FROM users WHERE id=?",
           (AID,))[0]["tickets_balance"]
check("webhook credits a paid session", bal2 - bal1 == 100,
      f"{bal1}->{bal2}")

# ---------------- login throttle ----------------
Lc, LIDU = signup("Lock Lisa", "lisa@k.test")
AN2 = TestClient(here.app, follow_redirects=False)
for i in range(10):
    AN2.post("/login", data={"email": "lisa@k.test",
                             "password": "wrong-pass"})
r = AN2.post("/login", data={"email": "lisa@k.test",
                             "password": "pw123456"})
loc = r.headers.get("location", "")
check("11th login try is throttled even with the right password",
      "Too many tries" in loc.replace("%20", " ").replace("+", " ")
      or "Too%20many" in loc, loc)
r = Lc.get("/home")
check("the real session still works (throttle is per login door)",
      r.status_code == 200)

# ---------------- session expiry ----------------
old = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
dbx("INSERT INTO sessions(token, user_id, created_at) VALUES(?,?,?)",
    ("stale-token-killer", LIDU, old))
Sc = TestClient(here.app, follow_redirects=False)
Sc.cookies.set(here.SESSION_COOKIE, "stale-token-killer")
r = Sc.get("/home")
check("a 40-day-old session is dead server-side",
      r.status_code in (303, 307)
      and "/login" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location')}")
check("the stale session row was deleted",
      not dbq("SELECT token FROM sessions WHERE token='stale-token-killer'"))

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
