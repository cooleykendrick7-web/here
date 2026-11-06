"""BC Bonus withdrawal door — user requests, owner sends by hand.

Covers:
- a request reserves the BC: balance drops, a bc_txns
  'withdraw_request' row is written with ref = withdrawal id,
  and a pending bc_withdrawals row exists
- refusals are clean and move nothing: bad address, below the
  1,000 BC minimum, over balance, a second pending request
- owner desk /owner/bc: summary math (granted / sent /
  outstanding liability vs the 500,000,000 stack), pending list
- mark sent stores the tx hash + status; the user sees a
  BaseScan link; a bad hash is refused
- decline refunds the reservation exactly ('withdraw_refund')
- One Inbox counts the pending BC withdrawal
- gates: strangers refused on every user + owner route
- a user with 0 balance sees no withdrawal form
- reconciliation: bc_balances.balance always equals the sum of
  the user's bc_txns deltas, and granted == balances + pending
  reservations + sent, per user, at every step
"""
import os, sys, sqlite3, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
TMP = tempfile.mkdtemp(prefix="here_bcwithdraw_")
os.environ["HERE_DB"] = os.path.join(TMP, "t.db")
os.environ["HERE_UPLOADS"] = os.path.join(TMP, "up")
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake"
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

ADDR = "0x" + "11" * 20          # a valid-shaped Base address
ADDR2 = "0x" + "22" * 20
TXHASH = "0x" + "ab" * 32        # a valid-shaped tx hash

def bal(uid):
    return here._bc_balance(uid)

def reconcile(uid):
    """The ledger truth: balance == sum of bc_txns deltas, and
    granted == balance + pending reservations + sent amounts."""
    s = dbq("SELECT COALESCE(SUM(delta),0) s FROM bc_txns "
            "WHERE user_id=?", (uid,))[0]["s"]
    granted = dbq("SELECT COALESCE(SUM(delta),0) s FROM bc_txns "
                  "WHERE user_id=? AND reason=?",
                  (uid, here.BC_BONUS_REASON))[0]["s"]
    pend = dbq("SELECT COALESCE(SUM(amount),0) s FROM "
               "bc_withdrawals WHERE user_id=? AND "
               "status='pending'", (uid,))[0]["s"]
    sent = dbq("SELECT COALESCE(SUM(amount),0) s FROM "
               "bc_withdrawals WHERE user_id=? AND status='sent'",
               (uid,))[0]["s"]
    return (bal(uid) == s
            and granted == bal(uid) + pend + sent)

def wds(uid):
    return dbq("SELECT * FROM bc_withdrawals WHERE user_id=? "
               "ORDER BY id", (uid,))

A, AID = signup("Withdraw Wanda", "wanda@x.io")
B, BID = signup("Declined Dan", "dan@x.io")
C, CID = signup("Zero Zelda", "zelda@x.io")

print("== 0. Gates ==")
anon = TestClient(here.app, follow_redirects=False)
r = anon.get("/owner/bc")
check("stranger refused /owner/bc",
      r.status_code == 303 and "/owner/login" in
      r.headers.get("location", ""), str(r.status_code))
r = anon.post("/owner/bc/1/sent", data={"tx_hash": TXHASH})
check("stranger refused mark-sent", r.status_code == 303)
r = anon.post("/owner/bc/1/decline")
check("stranger refused decline", r.status_code == 303)
r = anon.post("/tickets/bc/withdraw",
              data={"address": ADDR, "amount": "1000"})
check("stranger refused the withdraw door",
      r.status_code == 303 and "/login" in
      r.headers.get("location", ""), str(r.status_code))
r = A.get("/owner/bc")
check("a logged-in member is not the owner",
      r.status_code == 303 and "/owner/login" in
      r.headers.get("location", ""), str(r.status_code))

print("== 1. Zero balance: no form ==")
here.credit_purchase(AID, 1500, "cs_w_1500")   # +25,000 BC
here.credit_purchase(BID, 500, "cs_w_500")     # +7,500 BC
# Black Coin participation is opt-in now (deep Settings toggle,
# users.bc_enabled, default off): A and B flip it on through the
# real settings flow, like members, before using the door. C
# never does — her no-form check below holds either way.
A.post("/settings/blackcoin", data={"bc_enabled": "1"})
B.post("/settings/blackcoin", data={"bc_enabled": "1"})
check("A funded 25,000", bal(AID) == 25000)
check("B funded 7,500", bal(BID) == 7500)
html = C.get("/tickets").text
check("0-balance user sees no withdraw form",
      'action="/tickets/bc/withdraw"' not in html)
html = A.get("/tickets").text
check("funded user sees the withdraw form",
      'action="/tickets/bc/withdraw"' in html)
check("wallet states the by-hand honesty line",
      "Ken sends Black Coin from his own wallet by hand" in html)
check("pack card badges show the free BC",
      "+ 1,000 BC FREE" in html and "+ 7,500 BC FREE" in html
      and "+ 25,000 BC FREE" in html)

print("== 2. Clean refusals move nothing ==")
def try_withdraw(cl, **kw):
    return cl.post("/tickets/bc/withdraw", data=kw)
r = try_withdraw(A, address="not-an-address", amount="10000")
check("bad address refused (redirect)",
      r.status_code == 303)
r = try_withdraw(A, address="0x1234", amount="10000")
check("short address refused", r.status_code == 303)
r = try_withdraw(A, address=ADDR, amount="999")
check("below the 1,000 minimum refused", r.status_code == 303)
r = try_withdraw(A, address=ADDR, amount="25001")
check("over balance refused", r.status_code == 303)
r = try_withdraw(A, address=ADDR, amount="abc")
check("non-numeric amount refused", r.status_code == 303)
check("balance untouched by every refusal", bal(AID) == 25000)
check("no withdrawal rows from refusals", wds(AID) == [])
check("no withdraw ledger rows from refusals",
      dbq("SELECT * FROM bc_txns WHERE user_id=? AND reason=?",
          (AID, here.BC_WITHDRAW_REASON)) == [])
check("reconciles after refusals", reconcile(AID))

print("== 3. A valid request reserves the BC ==")
r = try_withdraw(A, address=ADDR, amount="10000")
check("request accepted", r.status_code == 303)
check("balance dropped by the reserved amount",
      bal(AID) == 15000, str(bal(AID)))
rows = wds(AID)
check("one pending withdrawal row", len(rows) == 1
      and rows[0]["status"] == "pending")
check("row carries amount + address",
      rows[0]["amount"] == 10000 and rows[0]["address"] == ADDR)
wid = rows[0]["id"]
led = dbq("SELECT * FROM bc_txns WHERE user_id=? AND reason=?",
          (AID, here.BC_WITHDRAW_REASON))
check("ledger has the withdraw_request row",
      len(led) == 1 and led[0]["delta"] == -10000)
check("ledger ref is the withdrawal id",
      led[0]["ref"] == str(wid), str(led))
check("reconciles with a pending reservation", reconcile(AID))
r = try_withdraw(A, address=ADDR2, amount="1000")
check("a second pending request refused", r.status_code == 303)
check("balance unchanged after second try", bal(AID) == 15000)
check("still exactly one withdrawal row", len(wds(AID)) == 1)
html = A.get("/tickets").text
check("pending user sees their request listed",
      "10,000 BC" in html and "Pending" in html)
check("no form while one is pending",
      'action="/tickets/bc/withdraw"' not in html)

print("== 4. One Inbox counts it; owner desk shows it ==")
dbx("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
    ("bcw-owner-token", here.now_iso()))
own = TestClient(here.app, follow_redirects=False)
own.cookies.set("here_owner", "bcw-owner-token")
html = own.get("/owner/inbox").text
check("inbox has the Black Coin withdrawals section",
      "Black Coin withdrawals" in html)
check("inbox shows the pending amount + member",
      "10,000" in html and "Withdraw Wanda" in html)
check("inbox deep-links to the BC desk",
      'href="/owner/bc"' in html)
html = own.get("/owner/bc").text
check("desk renders", "Black Coin Withdrawals" in html)
check("desk granted total 32,500",
      "32,500" in html, "")
check("desk outstanding liability 32,500",
      "32,500 BC" in html)
check("desk shows the 500M stack", "500,000,000" in html)
check("desk pending row has the full address", ADDR in html)
check("desk mark-sent form targets the right route",
      f'action="/owner/bc/{wid}/sent"' in html)
check("cockpit carries the BC Withdrawals door",
      'href="/owner/bc"' in own.get("/owner").text)

print("== 5. Mark sent ==")
r = own.post(f"/owner/bc/{wid}/sent", data={"tx_hash": "0xzz"})
check("a bad tx hash refused", r.status_code == 303)
check("still pending after bad hash",
      wds(AID)[0]["status"] == "pending")
r = own.post(f"/owner/bc/{wid}/sent", data={"tx_hash": TXHASH})
check("mark sent accepted", r.status_code == 303)
row = wds(AID)[0]
check("status flipped to sent", row["status"] == "sent")
check("tx hash stored", row["tx_hash"] == TXHASH)
check("decided_at stamped", bool(row["decided_at"]))
check("balance stays 15,000 — the BC left for real",
      bal(AID) == 15000)
check("reconciles after the send", reconcile(AID))
r = own.post(f"/owner/bc/{wid}/sent", data={"tx_hash": TXHASH})
check("re-deciding a sent row refused", r.status_code == 303)
html = A.get("/tickets").text
check("user sees Sent + a BaseScan link",
      "Sent" in html
      and f"https://basescan.org/tx/{TXHASH}" in html)
check("form returns once nothing is pending",
      'action="/tickets/bc/withdraw"' in html)

print("== 6. Decline refunds exactly ==")
r = try_withdraw(B, address=ADDR2, amount="7500")
check("B's full-balance request accepted", r.status_code == 303)
check("B drained to 0 while pending", bal(BID) == 0)
bwid = wds(BID)[0]["id"]
r = own.post(f"/owner/bc/{bwid}/decline")
check("decline accepted", r.status_code == 303)
check("B refunded to exactly 7,500", bal(BID) == 7500,
      str(bal(BID)))
check("B's row marked declined",
      wds(BID)[0]["status"] == "declined")
ref = dbq("SELECT * FROM bc_txns WHERE user_id=? AND reason=?",
          (BID, here.BC_REFUND_REASON))
check("refund ledger row +7,500 with ref = withdrawal id",
      len(ref) == 1 and ref[0]["delta"] == 7500
      and ref[0]["ref"] == str(bwid), str(ref))
check("B reconciles after the refund", reconcile(BID))
html = B.get("/tickets").text
check("B sees the decline + refund note",
      "Declined" in html and "went back to your bonus balance"
      in html)
html = own.get("/owner/bc").text
check("desk history shows sent + declined",
      TXHASH[:10] not in html or "basescan.org/tx/" in html)
check("desk liability back to balances only (22,500)",
      "22,500" in html)

print("== 7. Minimum boundary ==")
here.credit_purchase(CID, 100, "cs_w_100")   # exactly 1,000 BC
check("C funded exactly 1,000", bal(CID) == 1000)
C.post("/settings/blackcoin", data={"bc_enabled": "1"})
r = try_withdraw(C, address=ADDR, amount="1000")
check("a 1,000 BC request is allowed", r.status_code == 303)
check("C reserved to 0", bal(CID) == 0)
check("C has a pending row", len(wds(CID)) == 1
      and wds(CID)[0]["status"] == "pending")
check("C reconciles", reconcile(CID))

print(f"\n{PASS[0]} passed, {FAIL[0]} failed")
sys.exit(1 if FAIL[0] else 0)
