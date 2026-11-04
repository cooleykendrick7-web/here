"""HERE Play Book suite — THE HERE PLAY BOOK ($35.99, 37-page
book): public landing, Stripe purchase + gated download, and
strict separation from the $19.99 invest Playbook (owning one
never unlocks the other).

Run:  ./venv/bin/python tests/test_here_playbook.py
"""
import os
import sqlite3
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-hereplaybook-")
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


# ---------- public landing ----------
G = TestClient(here.app, follow_redirects=False)
page = G.get("/here-playbook")
check("guest can open /here-playbook", page.status_code == 200)
t = page.text
check("landing shows the price $35.99", "$35.99" in t)
check("landing shows the cover image",
      "/static/here_play_book_cover.jpg" in t)
check("landing links Gumroad as the second door",
      "https://cooley11.gumroad.com/l/here-play-book" in t)
check("landing is honest: no capture card included",
      "No capture card is included" in t)
check("landing is honest: rate guaranteed, amount up to you",
      "rate" in t and "amount" in t and "up to your work" in t)
check("landing teaches creators/artists/pages/users",
      "7 ways to get paid" in t and "Artists" in t
      and "Pages" in t and "Regular users" in t)
check("guest buy door points at joining first",
      "Join free to get the book" in t)

# ---------- gates before purchase ----------
r = G.get("/here-playbook.pdf")
check("logged-out PDF request is refused (no PDF served)",
      r.status_code == 303
      and "/here-playbook" in (r.headers.get("location") or "")
      and "application/pdf" not in (r.headers.get("content-type")
                                    or ""))
r = G.post("/here-playbook/checkout")
check("guest checkout bounces to login",
      r.status_code == 303
      and "/login" in (r.headers.get("location") or ""))

A, AID = signup("Amy Buyer", "amy@hp.io")
B, BID = signup("Ben Bystander", "ben@hp.io")
C, CID = signup("Cora Crosscheck", "cora@hp.io")

r = B.get("/here-playbook.pdf")
check("logged-in non-buyer is bounced from the PDF",
      r.status_code == 303
      and "/here-playbook" in (r.headers.get("location") or ""))

# ---------- purchase flow (Stripe faked) ----------
real_api, real_en = here._stripe_api, here.stripe_enabled
here.stripe_enabled = lambda: True
posted = {}


def fake_api(method, path, params=None):
    if method == "POST":
        posted.update(params or {})
        return {"url": "https://stripe.test/checkout"}
    # GET session: whoever asks, the session belongs to Amy and
    # is for THIS book — user/kind checks in the route decide.
    return {"payment_status": "paid",
            "metadata": {"user_id": str(AID),
                         "kind": "here_playbook"}}


here._stripe_api = fake_api
r = A.post("/here-playbook/checkout")
check("checkout redirects to Stripe",
      r.status_code == 303
      and "stripe.test" in (r.headers.get("location") or ""))
check("checkout charges exactly 3599 cents for this book",
      posted.get("line_items[0][price_data][unit_amount]")
      == "3599" and posted.get("metadata[kind]")
      == "here_playbook")
r = A.get("/here-playbook/return?session_id=cs_hp_1")
rows = dbq("SELECT * FROM here_playbook_access WHERE user_id=? "
           "AND kind='paid'", (AID,))
check("paid return grants access (row recorded)",
      len(rows) == 1 and rows[0]["session_id"] == "cs_hp_1")
r = A.get("/here-playbook/return?session_id=cs_hp_1")
check("grant is idempotent — second return adds no row",
      len(dbq("SELECT * FROM here_playbook_access WHERE "
              "user_id=? AND kind='paid'", (AID,))) == 1)
r = A.get("/here-playbook.pdf")
check("buyer downloads THE HERE PLAY BOOK",
      r.status_code == 200
      and "application/pdf" in (r.headers.get("content-type")
                                or "")
      and r.content[:4] == b"%PDF")
page = A.get("/here-playbook").text
check("landing flips to owned state with download button",
      "You own THE HERE PLAY BOOK" in page)

# a session in Amy's name grants Ben nothing
r = B.get("/here-playbook/return?session_id=cs_hp_1")
check("another member's session grants Ben nothing",
      dbq("SELECT * FROM here_playbook_access WHERE user_id=?",
          (BID,)) == [])

# ---------- cross-book separation ----------
# Dana buys ONLY the $35.99 book (she is not the owner), so the
# cross-checks below are not polluted by any owner bypass.
D, DID = signup("Dana Buyer", "dana@hp.io")


def fake_api_dana(method, path, params=None):
    if method == "POST":
        return {"url": "https://stripe.test/checkout"}
    return {"payment_status": "paid",
            "metadata": {"user_id": str(DID),
                         "kind": "here_playbook"}}


here._stripe_api = fake_api_dana
r = D.get("/here-playbook/return?session_id=cs_hp_dana")
check("Dana's $35.99 grant lands",
      len(dbq("SELECT * FROM here_playbook_access WHERE "
              "user_id=? AND kind='paid'", (DID,))) == 1)
r = D.get("/playbook.pdf")
check("$35.99 book access does NOT unlock the $19.99 Playbook",
      r.status_code == 303
      and "/invest" in (r.headers.get("location") or ""))

# Cora buys the $19.99 invest Playbook through its own door.
def fake_api_invest(method, path, params=None):
    if method == "POST":
        return {"url": "https://stripe.test/checkout"}
    return {"payment_status": "paid",
            "metadata": {"user_id": str(CID), "kind": "playbook"}}


here._stripe_api = fake_api_invest
r = C.get("/invest/playbook/return?session_id=cs_inv_1")
check("Cora's $19.99 playbook grant lands in its own table",
      len(dbq("SELECT * FROM playbook_access WHERE user_id=? "
              "AND kind='paid'", (CID,))) == 1)
r = C.get("/playbook.pdf")
check("Cora can download the $19.99 Playbook she bought",
      r.status_code == 200
      and "application/pdf" in (r.headers.get("content-type")
                                or ""))
r = C.get("/here-playbook.pdf")
check("$19.99 Playbook access does NOT unlock the $35.99 book",
      r.status_code == 303
      and "/here-playbook" in (r.headers.get("location") or ""))
here._stripe_api, here.stripe_enabled = real_api, real_en

# ---------- the owner always gets in ----------
# Make Ben the owner explicitly (founding_no=1) in the test DB;
# he holds no purchase rows, so only the owner path can let him in.
_conn = sqlite3.connect(os.environ["HERE_DB"], timeout=10)
_conn.execute("UPDATE users SET founding_no=NULL")
_conn.execute("UPDATE users SET founding_no=1 WHERE id=?", (BID,))
_conn.commit()
_conn.close()
owner_row = dbq("SELECT id FROM users WHERE founding_no=1")
check("owner reassigned to Ben (founding_no=1)",
      len(owner_row) == 1 and owner_row[0]["id"] == BID)
check("Ben holds no purchase row for the $35.99 book",
      dbq("SELECT * FROM here_playbook_access WHERE user_id=?",
          (BID,)) == [])
r = B.get("/here-playbook.pdf")
check("owner downloads without a purchase row",
      r.status_code == 200
      and "application/pdf" in (r.headers.get("content-type")
                                or ""))

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} "
      "here-playbook checks passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL HERE PLAY BOOK CHECKS PASSED")
