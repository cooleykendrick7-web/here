"""EVERYBODY EATS suite (Ken's order, Nov 8, 2026): the opt-in
daily pool inside Creator Mode — switch gating, pool math from
recorded sales (ticket packs + Playbook), the equal midnight
split, rollovers, idempotent settlement, the /eats page, the
🍽️ EATS badge, the /earnings line, and Hearley.

Run:  ./venv/bin/python tests/test_eats.py
"""
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-eats-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{extra}]" if extra and not cond else ""), flush=True)


def dbq(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"], timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def dbx(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"], timeout=10)
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


def stamp_for(day):
    start = here._chicago_day_start_utc(day)
    return (datetime.fromisoformat(start)
            + timedelta(hours=15)).isoformat()


def txn(uid, kind, tickets, day, note=""):
    dbx("INSERT INTO ticket_txns(user_id, kind, tickets, live_id,"
        " gift_key, note, created_at) VALUES(?,?,?,?,?,?,?)",
        (uid, kind, tickets, None, "", note, stamp_for(day)))


def earned(uid):
    return dbq("SELECT gift_earned, gift_lifetime FROM users "
               "WHERE id=?", (uid,))[0]


A, AID = signup("Ana Eats", "ana@eats.io")      # qualifier
B, BID = signup("Ben Eats", "ben@eats.io")      # qualifier
C, CID = signup("Cal Off", "cal@eats.io")       # earns, switch OFF
D, DID = signup("Dee Eats", "dee@eats.io")      # qualifier (day Y)
E, EID = signup("Eli NoCreator", "eli@eats.io")  # switch on, no CM
F, FID = signup("Fay Fan", "fay@eats.io")       # buyer / bystander

W = date(2026, 11, 4)   # pool, NO qualifiers -> all rolls forward
X = date(2026, 11, 5)   # A alone
Y = date(2026, 11, 6)   # A + B + D, remainder 2 rolls forward
Z = date(2026, 11, 7)   # A + B split the roll-in + Playbook money

# ---- the switch ------------------------------------------------
r = A.post("/settings/eats", data={"eats": "1"})
u = dbq("SELECT eats_enabled FROM users WHERE id=?", (AID,))[0]
check("switch refuses to turn on without Creator Mode",
      u["eats_enabled"] == 0)
A.post("/settings/creator", data={"creator_mode": "1"})
A.post("/settings/eats", data={"eats": "1"})
u = dbq("SELECT eats_enabled, eats_since FROM users WHERE id=?",
        (AID,))[0]
check("switch turns on inside Creator Mode (+ eats_since set)",
      u["eats_enabled"] == 1 and bool(u["eats_since"]))
for c in (B, D):
    c.post("/settings/creator", data={"creator_mode": "1"})
    c.post("/settings/eats", data={"eats": "1"})
C.post("/settings/creator", data={"creator_mode": "1"})
dbx("UPDATE users SET eats_enabled=1 WHERE id=?", (EID,))
A.post("/settings/creator", data={"creator_mode": "0"})
u = dbq("SELECT creator_mode, eats_enabled FROM users WHERE id=?",
        (AID,))[0]
check("Creator Mode OFF also leaves the pool",
      u["creator_mode"] == 0 and u["eats_enabled"] == 0)
A.post("/settings/creator", data={"creator_mode": "1"})
A.post("/settings/eats", data={"eats": "1"})

# ---- the money days ---------------------------------------------
txn(FID, "buy", 100, W)                       # 99c gross
txn(FID, "buy", 100, X)                       # 99c gross
txn(AID, "gift_receive", 25, X)
txn(FID, "buy", 500, Y)                       # 499c gross
txn(AID, "gift_receive", 25, Y)
txn(BID, "gift_receive", 10, Y)
txn(DID, "unlock_receive", 5, Y)
dbx("INSERT INTO playbook_access(user_id, kind, session_id,"
    " created_at) VALUES(?,?,?,?)",
    (FID, "paid", "sess_eats", stamp_for(Z)))  # 1999c gross
txn(AID, "gift_receive", 25, Z)
txn(BID, "entry_receive", 40, Z)
txn(CID, "gift_receive", 10, Z)               # eats OFF: excluded
txn(EID, "gift_receive", 10, Z)               # creator OFF: excluded

conn = here.db()
g = here._eats_day_gross_cents(conn, Z)
check("day gross = Playbook sale only on Z", g == 1999, str(g))
g = here._eats_day_gross_cents(conn, Y)
check("day gross = 500-pack on Y", g == 499, str(g))
q = sorted(here._eats_qualifiers(conn, Z))
check("qualifiers on Z are A+B only", q == sorted([AID, BID]),
      str(q))

# ---- settlement, ascending, with the roll chain ------------------
c1 = here._eats_settle_day(conn, W)
row = dbq("SELECT * FROM eats_days WHERE day=?", (W.isoformat(),))[0]
check("W: no qualifiers, whole pool (18) rolls forward",
      c1 == [] and row["settled"] == 1 and row["carried_out"] == 18
      and row["pool_tickets"] == 18, str(dict(row)))
c2 = here._eats_settle_day(conn, X)
row = dbq("SELECT * FROM eats_days WHERE day=?", (X.isoformat(),))[0]
check("X: carried 18 + own 18 = 36, all to A",
      c2 == [(AID, 36)] and row["carried_in"] == 18
      and row["carried_out"] == 0, str((c2, dict(row))))
c3 = here._eats_settle_day(conn, Y)
row = dbq("SELECT * FROM eats_days WHERE day=?", (Y.isoformat(),))[0]
check("Y: 98 split 3 ways = 32 each, 2 roll forward",
      sorted(c3) == sorted([(AID, 32), (BID, 32), (DID, 32)])
      and row["per_share"] == 32 and row["carried_out"] == 2,
      str((c3, dict(row))))
c4 = here._eats_settle_day(conn, Z)
row = dbq("SELECT * FROM eats_days WHERE day=?", (Z.isoformat(),))[0]
check("Z: 398 + 2 carried = 400, 200 each to A+B",
      sorted(c4) == sorted([(AID, 200), (BID, 200)])
      and row["carried_in"] == 2 and row["carried_out"] == 0,
      str((c4, dict(row))))
conn.close()

check("A earned 268 pool tickets total", earned(AID)["gift_earned"] == 268,
      str(dict(earned(AID))))
check("B earned 232 pool tickets total", earned(BID)["gift_earned"] == 232)
check("D earned 32 pool tickets", earned(DID)["gift_earned"] == 32)
check("C (switch off) got nothing", earned(CID)["gift_earned"] == 0)
check("E (no Creator Mode) got nothing", earned(EID)["gift_earned"] == 0)
check("pool never touches gift_lifetime",
      earned(AID)["gift_lifetime"] == 0
      and earned(BID)["gift_lifetime"] == 0)
n = dbq("SELECT COUNT(*) c FROM ticket_txns WHERE kind='eats_pool'"
        )[0]["c"]
check("6 eats_pool ledger rows written", n == 6, str(n))

# ---- idempotency + settle_due ------------------------------------
conn = here.db()
again = here._eats_settle_day(conn, Z)
here._eats_settle_due(conn)
conn.close()
check("re-settling a settled day credits nobody", again == [])
check("settle_due changes nothing twice",
      earned(AID)["gift_earned"] == 268)
rows = dbq("SELECT COUNT(*) c FROM eats_days WHERE settled=1")[0]["c"]
check("exactly the 4 simulated days are settled", rows == 4, str(rows))

# ---- the pages ----------------------------------------------------
guest = TestClient(here.app, follow_redirects=False)
r = guest.get("/eats")
check("/eats is public and renders",
      r.status_code == 200 and "Everybody Eats" in r.text
      and "Today's pot so far" in r.text)
check("/eats shows yesterday's split + history",
      "Yesterday's split" in r.text and "200" in r.text
      and "Ana Eats" in r.text)
r = A.get("/eats")
check("/eats tells a member they're in", "You're in the pool" in r.text)
r = A.get("/settings/profile")
check("settings render the Eats card, switch on",
      'id="eats"' in r.text and "Everybody Eats" in r.text)
r = C.get("/settings/profile")
check("creator without the switch sees the card too",
      'id="eats"' in r.text)
r = F.get("/settings/profile")
check("non-creator is told to turn Creator Mode on first",
      "Turn on Creator Mode above first" in r.text)
r = A.get("/user/%d" % AID)
check("profile wears the EATS badge", "🍽️ EATS" in r.text)
A.post("/posts", data={"body": "eats badge post", "kind": "text"})
r = B.get("/home")
check("post cards wear the EATS chip",
      "🍽️ EATS" in r.text and "eats badge post" in r.text)
r = A.get("/earnings")
check("/earnings has the Everybody Eats line",
      "Everybody Eats" in r.text and "268" in r.text)
r = A.post("/api/hearley/ask", json={"q": "what is everybody eats?"})
j = r.json()
check("Hearley explains the pool and links /eats",
      j.get("link") == "/eats" and "pool" in (j.get("answer") or ""),
      str(j)[:120])

# ---- switch back off ----------------------------------------------
A.post("/settings/eats", data={"eats": "0"})
u = dbq("SELECT eats_enabled FROM users WHERE id=?", (AID,))[0]
check("switch turns back off", u["eats_enabled"] == 0)

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL EATS CHECKS PASSED")
