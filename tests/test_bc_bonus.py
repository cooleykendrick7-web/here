"""Owner Pack P6 — Black Coin Bonus on ticket pack purchases.

Covers:
- credit_purchase (the ONLY buy path: Stripe webhook + return
  verify both call it) grants the pack's BC bonus in the same
  transaction: pack100 -> 1,000 BC, pack500 -> 7,500, pack1500 ->
  25,000
- idempotent: a duplicate session (returns False) grants nothing
  twice — tickets and BC both credited exactly once
- custom / non-pack ticket counts grant no bonus
- bc_txns ledger rows carry delta, reason exactly 'Black Coin
  Bonus', ref = the Stripe session id
- wallet page (/tickets) shows the BC balance labeled 'Black Coin
  Bonus'; Money Desk shows the all-time granted total
- the words 'stock', 'shares', 'investment' never label the bonus
"""
import os, sys, sqlite3, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
TMP = tempfile.mkdtemp(prefix="here_bcbonus_")
os.environ["HERE_DB"] = os.path.join(TMP, "t.db")
os.environ["HERE_UPLOADS"] = os.path.join(TMP, "up")
import app as here
from fastapi.testclient import TestClient

PASS = [0]; FAIL = [0]

def check(name, cond, extra=""):
    if cond:
        PASS[0] += 1; print("  ok -", name)
    else:
        FAIL[0] += 1; print("  FAIL -", name, extra)

def dbq(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"])
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    conn.close(); return rows

def dbx(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"])
    conn.execute(sql, args); conn.commit(); conn.close()

def signup(name, email):
    cl = TestClient(here.app, follow_redirects=False)
    r = cl.post("/signup", data={"name": name, "email": email,
                                 "password": "testpass123"})
    assert r.status_code == 303, (name, r.status_code)
    return cl, dbq("SELECT MAX(id) m FROM users")[0]["m"]

def bc(uid):
    return here._bc_balance(uid)

def bc_rows(uid):
    return dbq("SELECT * FROM bc_txns WHERE user_id=? ORDER BY id",
               (uid,))

U, UID = signup("Bonus Buyer", "bonus@x.io")

print("== 1. Pack purchases grant the right bonus ==")
check("no BC before any purchase", bc(UID) == 0)
check("pack100 credit ok",
      here.credit_purchase(UID, 100, "cs_p6_100") is True)
check("pack100 -> 1,000 BC", bc(UID) == 1000, str(bc(UID)))
check("pack500 credit ok",
      here.credit_purchase(UID, 500, "cs_p6_500") is True)
check("pack500 -> +7,500 BC (8,500 total)", bc(UID) == 8500,
      str(bc(UID)))
check("pack1500 credit ok",
      here.credit_purchase(UID, 1500, "cs_p6_1500") is True)
check("pack1500 -> +25,000 BC (33,500 total)", bc(UID) == 33500,
      str(bc(UID)))
tb = dbq("SELECT tickets_balance b FROM users WHERE id=?",
         (UID,))[0]["b"]
check("tickets credited alongside (2,100)", tb == 2100, str(tb))

print("== 2. Idempotent: duplicate sessions grant nothing twice ==")
check("duplicate pack100 session refused",
      here.credit_purchase(UID, 100, "cs_p6_100") is False)
check("BC unchanged after duplicate", bc(UID) == 33500)
check("tickets unchanged after duplicate",
      dbq("SELECT tickets_balance b FROM users WHERE id=?",
          (UID,))[0]["b"] == 2100)
check("empty session refused",
      here.credit_purchase(UID, 100, "") is False)
check("BC unchanged after empty session", bc(UID) == 33500)

print("== 3. Custom buys earn no bonus ==")
check("custom 250-ticket credit ok",
      here.credit_purchase(UID, 250, "cs_p6_custom") is True)
check("no BC for a non-pack count", bc(UID) == 33500)
check("no bc_txns row for the custom buy",
      len(bc_rows(UID)) == 3, str(len(bc_rows(UID))))

print("== 4. Ledger rows are exact ==")
rows = bc_rows(UID)
check("three bonus ledger rows", len(rows) == 3)
check("deltas 1000 / 7500 / 25000",
      [r["delta"] for r in rows] == [1000, 7500, 25000],
      str([r["delta"] for r in rows]))
check("reason is exactly 'Black Coin Bonus' on every row",
      all(r["reason"] == "Black Coin Bonus" for r in rows))
check("ref carries the Stripe session id",
      [r["ref"] for r in rows] == ["cs_p6_100", "cs_p6_500",
                                   "cs_p6_1500"],
      str([r["ref"] for r in rows]))
check("bc_balances total equals the ledger sum",
      bc(UID) == sum(r["delta"] for r in rows))

print("== 5. Displays ==")
html = U.get("/tickets").text
check("wallet shows the Black Coin Bonus label",
      "Black Coin Bonus" in html)
check("wallet shows the formatted BC balance (33,500)",
      "33,500" in html)
check("bonus label block carries no stock/share/investment words",
      all(w not in html.split("Black Coin Bonus")[1][:120].lower()
          for w in ("stock", "share", "invest")))
dbx("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
    ("bc-owner-token", here.now_iso()))
own = TestClient(here.app, follow_redirects=False)
own.cookies.set("here_owner", "bc-owner-token")
r = own.get("/owner/money?range=all")
check("Money Desk renders with the BC total wired",
      r.status_code == 200 and "33,500" in r.text
      and "Black Coin Bonuses granted" in r.text,
      str(r.status_code))

print(f"\n{PASS[0]} passed, {FAIL[0]} failed")
sys.exit(1 if FAIL[0] else 0)
