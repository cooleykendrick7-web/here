"""Invest suite — the /invest hub (Ken's build, Nov 4, 2026):
three honest ways in (creator / Playbook / Black Coin), the
Playbook's Stripe purchase + gated download, the free artist-door
copy, and Hearley's new invest answers.

Run:  ./venv/bin/python tests/test_invest.py
"""
import os
import sqlite3
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-invest-")
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


def signup(name, email):
    c = TestClient(here.app, follow_redirects=False)
    r = c.post("/signup", data={"name": name, "email": email,
                                "password": "pw123456"})
    assert r.status_code == 303, (name, r.status_code)
    uid = dbq("SELECT id FROM users WHERE email=?",
              (email,))[0]["id"]
    return c, uid


G = TestClient(here.app, follow_redirects=False)
page = G.get("/invest")
check("guest can open /invest", page.status_code == 200)
t = page.text
check("hub names all three ways in",
      "Become a creator" in t and "The Playbook" in t
      and "Black Coin (BC)" in t)
check("BC honesty box present (not stock, no ownership)",
      "not stock" in t and "no ownership" in t
      and "can fall to zero" in t)
check("real contract address on the page",
      here.BC_CONTRACT in t)
check("proof strip renders live numbers",
      "paid to creators so far" in t and "Wall of Paid" in t)
check("creator way states the guaranteed rate + Stripe cash-out",
      "half a cent" in t and "Stripe" in t)
check("guest buy door points at joining first",
      "Join free to get the Playbook" in t)

A, AID = signup("Ann Author", "ann@inv.io")
B, BID = signup("Bob Buyer", "bob@inv.io")
r = B.get("/playbook.pdf")
check("non-buyer is bounced from the storefront PDF",
      r.status_code == 303
      and "/invest" in (r.headers.get("location") or ""))

# artist door copy is free for signed-in members
r = A.get("/artist/playbook.pdf")
check("artist-door copy serves the PDF to a member",
      r.status_code == 200
      and "application/pdf" in (r.headers.get("content-type")
                                or ""))
check("artist entitlement recorded",
      len(dbq("SELECT * FROM playbook_access WHERE user_id=? "
              "AND kind='artist'", (AID,))) == 1)

# ---------- purchase flow (Stripe faked) ----------
real_api, real_en = here._stripe_api, here.stripe_enabled
here.stripe_enabled = lambda: True


def fake_api(method, path, params=None):
    if method == "POST":
        return {"url": "https://stripe.test/checkout"}
    return {"payment_status": "paid",
            "metadata": {"user_id": str(AID), "kind": "playbook"}}


here._stripe_api = fake_api
r = A.post("/invest/playbook/checkout")
check("checkout redirects to Stripe",
      r.status_code == 303
      and "stripe.test" in (r.headers.get("location") or ""))
r = A.get("/invest/playbook/return?session_id=cs_test_1")
rows = dbq("SELECT * FROM playbook_access WHERE user_id=? "
           "AND kind='paid'", (AID,))
check("paid return grants the download (idempotent row)",
      len(rows) == 1 and rows[0]["session_id"] == "cs_test_1")
r = A.get("/playbook.pdf")
check("buyer downloads the Playbook",
      r.status_code == 200
      and "application/pdf" in (r.headers.get("content-type")
                                or ""))
page = A.get("/invest").text
check("hub flips to owned state with download button",
      "You own the Playbook" in page)

# a session for somebody else grants nothing
r = B.get("/invest/playbook/return?session_id=cs_test_1")
check("another member's session grants Bob nothing",
      dbq("SELECT * FROM playbook_access WHERE user_id=? "
          "AND kind='paid'", (BID,)) == [])
here._stripe_api, here.stripe_enabled = real_api, real_en

# ---------- Hearley ----------
a1 = here.hearley_answer("how do I invest in here")
check("Hearley: invest -> /invest", a1 and a1[1] == "/invest",
      str(a1)[:80])
a2 = here.hearley_answer("what is black coin")
check("Hearley: black coin -> /invest + not-stock truth",
      a2 and a2[1] == "/invest" and "NOT stock" in a2[0])
a3 = here.hearley_answer("how can I make money here")
check("Hearley: make money -> /invest",
      a3 and a3[1] == "/invest", str(a3)[:80])
a4 = here.hearley_answer("how much is the playbook")
check("Hearley: playbook -> /invest", a4 and a4[1] == "/invest",
      str(a4)[:80])
a5 = here.hearley_answer("how do I cash out")
check("Hearley: cash out still routes to Tickets (no hijack)",
      a5 and a5[1] == "/tickets", str(a5)[:80])
a6 = here.hearley_answer("how do i post")
check("Hearley: post still routes home (no hijack)",
      a6 and a6[1] == "/home", str(a6)[:80])
a7 = here.hearley_answer("how do I stream my ps5 with a "
                         "capture card")
check("Hearley: capture card answer exists",
      a7 and "capture card" in a7[0].lower(), str(a7)[:80])

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} invest "
      "checks passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL INVEST CHECKS PASSED")
