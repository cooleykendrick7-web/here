"""Auto-pay cash-outs (Ken's order Oct 30, 2026: 'I shouldn't
have to approve someone getting their own money').

A bank-ready streamer's cash-out fires its Stripe transfer
before the request handler returns; a balance refusal leaves
it honestly queued; landing on /tickets bank-ready retries
the queue; the owner's Send button remains as a backstop on
the same guarded path; the sweep stops at the first balance
refusal instead of hammering Stripe.

Run:  ./venv/bin/python tests/test_autopay.py
"""
import os
import sys
import tempfile
import urllib.parse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-autopay-")
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
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    conn.close()
    return rows


def dbx(sql, args=()):
    conn = here.db()
    conn.execute(sql, args)
    conn.commit()
    conn.close()


CALLS = {"n": 0, "mode": "ok"}


def fake_stripe(method, path, data=None):
    if path == "/v1/transfers":
        CALLS["n"] += 1
        if CALLS["mode"] == "broke":
            raise RuntimeError(
                "Stripe error: insufficient funds in account")
        return {"id": f"tr_test_{CALLS['n']}"}
    return {}


here._stripe_api = fake_stripe

OWN = TestClient(here.app, follow_redirects=False)
_tok = "autopay-owner-token"
dbx("INSERT INTO owner_sessions(token, created_at) "
    "VALUES(?, '2026-10-30T00:00:00')", (_tok,))
OWN.cookies.set("here_owner", _tok)


def make_user(name, email, earned=2600, ready=False):
    c = TestClient(here.app, follow_redirects=False)
    c.post("/signup", data={"name": name, "email": email,
                            "password": "pw123456"})
    uid = dbq("SELECT id FROM users WHERE email=?",
              (email,))[0]["id"]
    dbx("UPDATE users SET gift_earned=? WHERE id=?",
        (earned, uid))
    if ready:
        dbx("UPDATE users SET payout_ready=1, "
            "stripe_acct_id=? WHERE id=?",
            (f"acct_{uid}", uid))
    return c, uid


def flash_of(resp):
    return urllib.parse.unquote(
        resp.headers.get("location") or "")


def req_of(uid):
    rows = dbq("SELECT * FROM payout_requests WHERE user_id=? "
               "ORDER BY id DESC", (uid,))
    return rows[0] if rows else None


# ---- 1. bank-ready: the cash-out pays ITSELF instantly ----
RITA, RID = make_user("Ready Rita", "rita@auto.test", ready=True)
r = RITA.post("/tickets/cashout",
              data={"payout_note": "my bank"})
pr = req_of(RID)
check("bank-ready cash-out is PAID before the handler returns",
      pr and pr["status"] == "paid", str(pr))
check("paid via stripe with a transfer id",
      pr and pr["method"] == "stripe"
      and (pr["stripe_transfer_id"] or "").startswith("tr_test"))
check("flash says Sent (no approval language)",
      "Sent" in flash_of(r)
      and "approval" not in flash_of(r).lower())
check("cashout_paid audit row written",
      dbq("SELECT COUNT(*) c FROM ticket_txns WHERE user_id=? "
          "AND kind='cashout_paid'", (RID,))[0]["c"] == 1)

# ---- 2. balance refusal: honestly queued, then self-heals --
CALLS["mode"] = "broke"
BROOKE, BID = make_user("Brooke", "brooke@auto.test", ready=True)
r = BROOKE.post("/tickets/cashout",
                data={"payout_note": "my bank"})
pr = req_of(BID)
check("balance refusal leaves the request pending (not lost)",
      pr and pr["status"] == "pending", str(pr))
check("flash tells the truth: queued, sends by itself",
      "queued" in flash_of(r) and "approval" not in
      flash_of(r).lower().replace("no approval", ""))
page = BROOKE.get("/tickets").text
check("wallet page carries no owner-approval copy",
      "owner's approval" not in page
      and "sends automatically" in page)
CALLS["mode"] = "ok"
BROOKE.get("/tickets")
pr = req_of(BID)
check("landing on /tickets bank-ready retries and pays",
      pr and pr["status"] == "paid", str(pr))

# ---- 3. unlinked bank: queued for setup, owner send guarded -
NED, NID = make_user("NoBank Ned", "ned@auto.test", ready=False)
NED.post("/tickets/cashout", data={"payout_note": "Cash App $ned"})
pr = req_of(NID)
check("unlinked cash-out waits pending (no transfer fired)",
      pr and pr["status"] == "pending", str(pr))
before = CALLS["n"]
r = OWN.post(f"/owner/payouts/{pr['id']}/send")
check("owner send on unlinked request refuses with the reason",
      "bank setup" in flash_of(r) and CALLS["n"] == before)
dbx("UPDATE users SET payout_ready=1, stripe_acct_id=? "
    "WHERE id=?", (f"acct_{NID}", NID))
r = OWN.post(f"/owner/payouts/{pr['id']}/send")
pr = req_of(NID)
check("owner backstop send pays once the bank is ready",
      pr and pr["status"] == "paid", str(pr))

# ---- 4. sweep stops at the first balance refusal ------------
U1, U1ID = make_user("Queue One", "q1@auto.test", ready=True)
U2, U2ID = make_user("Queue Two", "q2@auto.test", ready=True)
CALLS["mode"] = "broke"
U1.post("/tickets/cashout", data={"payout_note": "bank"})
U2.post("/tickets/cashout", data={"payout_note": "bank"})
n_before = CALLS["n"]
U1.get("/tickets")
check("one balance refusal stops the sweep (no hammering)",
      CALLS["n"] - n_before == 1,
      f"attempts={CALLS['n'] - n_before}")
CALLS["mode"] = "ok"
U1.get("/tickets")
check("next pass pays the whole queue",
      req_of(U1ID)["status"] == "paid"
      and req_of(U2ID)["status"] == "paid")

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL GREEN")
