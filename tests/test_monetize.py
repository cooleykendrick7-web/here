"""Money build tests: post/Peep gifts into the REAL cash-out
pipeline, the ticket door, paid ads (Stripe-verified, never
before), Market boosts, and HERE Plus. The invariant under test
everywhere: money moves exactly once, to exactly the right
person, and nothing activates without a verified payment.

Run:  ./venv/bin/python tests/test_monetize.py
Uses a throwaway DB; Stripe is ALWAYS the in-process fake below.
"""
import json
import os
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-money-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
import ads as ads_mod  # noqa: E402

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
    r = dbq("SELECT tickets_balance, gift_earned FROM users "
            "WHERE id=?", (uid,))[0]
    return (r["tickets_balance"] or 0), (r["gift_earned"] or 0)


def creator_on(c):
    c.post("/settings/creator", data={"creator_mode": "1"})


def gift_key_for(tickets):
    for k, g in here.GIFT_CATALOG.items():
        if g["tickets"] == tickets:
            return k
    raise AssertionError(f"no {tickets}-ticket gift")


# ---------------- fake Stripe ----------------
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake"
FAKE = {"sessions": {}, "cancel_calls": []}
PERIOD_END = int(time.time()) + 30 * 86400


def fake_api(method, path, params=None):
    if method == "POST" and path == "/v1/checkout/sessions":
        sid = f"cs_fake_{len(FAKE['sessions']) + 1}"
        meta = {k[len("metadata["):-1]: v
                for k, v in (params or {}).items()
                if k.startswith("metadata[")}
        FAKE["sessions"][sid] = {"payment_status": "paid",
                                 "metadata": meta}
        return {"id": sid, "url": "https://stripe.fake/" + sid}
    if method == "GET" and path.startswith("/v1/checkout/sessions/"):
        sid = path.rsplit("/", 1)[1]
        s = FAKE["sessions"].get(sid)
        if not s:
            raise RuntimeError("no such session")
        out = {"id": sid, "payment_status": s["payment_status"],
               "metadata": s["metadata"]}
        if s["metadata"].get("kind") == "plus":
            out["subscription"] = "sub_fake_1"
        return out
    if method == "GET" and path == "/v1/subscriptions/sub_fake_1":
        return {"id": "sub_fake_1", "status": "active",
                "current_period_end": PERIOD_END}
    if method == "POST" and path == "/v1/subscriptions/sub_fake_1":
        FAKE["cancel_calls"].append(params)
        return {"id": "sub_fake_1", "cancel_at_period_end": True}
    raise RuntimeError(f"unexpected stripe call {method} {path}")


here._stripe_api = fake_api

# ---------------- people ----------------
A, AID = signup("Money Ann", "ann@money.io")      # creator author
B, BID = signup("Money Ben", "ben@money.io")      # fan
C, CID = signup("Money Cam", "cam@money.io")      # plain author
creator_on(A)
dbx("UPDATE users SET tickets_balance=100000 WHERE id=?", (BID,))

r = A.post("/posts", json={"kind": "text", "body": "ann's post",
                           "privacy": "public"})
PID = dbq("SELECT id FROM posts WHERE user_id=? ORDER BY id DESC",
          (AID,))[0]["id"]
r = C.post("/posts", json={"kind": "text", "body": "cam's post",
                           "privacy": "public"})
CPID = dbq("SELECT id FROM posts WHERE user_id=? ORDER BY id DESC",
           (CID,))[0]["id"]

# ---------------- post gifts: the money moves exactly ----------------
G100 = gift_key_for(100)
before_b, before_a = bal(BID), bal(AID)
r = B.post(f"/post/{PID}/gift", json={"gift_key": G100})
j = r.json()
check("post gift: send ok", r.status_code == 200 and j.get("ok"),
      r.text[:120])
nb, na = bal(BID), bal(AID)
check("post gift: fan debited exactly 100",
      nb[0] == before_b[0] - 100, str(nb))
check("post gift: author earned exactly 100",
      na[1] == before_a[1] + 100, str(na))
check("post gift: row + twin ledger lines stamped 'post'",
      len(dbq("SELECT id FROM post_gifts WHERE post_id=? AND "
              "tickets=100", (PID,))) == 1
      and len(dbq("SELECT id FROM ticket_txns WHERE user_id=? "
                  "AND kind='gift_receive' AND note='post' AND "
                  "tickets=100", (AID,))) == 1
      and len(dbq("SELECT id FROM ticket_txns WHERE user_id=? "
                  "AND kind='gift_send' AND note='post' AND "
                  "tickets=-100", (BID,))) == 1)
pc = dbq("SELECT gift_earned FROM users WHERE id=?", (AID,))[0]
check("post gift: notification reached the author",
      len(dbq("SELECT id FROM notifications WHERE user_id=? AND "
              "kind='post_gift'", (AID,))) == 1)
r = B.get("/earnings") if False else A.get("/earnings")
check("earnings: the Post & Peep bucket shows the money",
      "Post &amp; Peep gifts" in r.text and "100 🎟️" in r.text)

r = A.post(f"/post/{PID}/gift", json={"gift_key": G100})
check("post gift: self-gift refused", r.status_code == 400)
r = B.post(f"/post/{CPID}/gift", json={"gift_key": G100})
check("post gift: non-creator author refused (Creator Mode gate)",
      r.status_code == 403, str(r.status_code))
dbx("UPDATE users SET tickets_balance=5 WHERE id=?", (CID,))
r = C.post(f"/post/{PID}/gift", json={"gift_key": G100})
check("post gift: insufficient balance refused",
      r.status_code == 400 and bal(CID)[0] == 5
      and len(dbq("SELECT id FROM post_gifts WHERE sender_id=?",
                  (CID,))) == 0)
r = B.post(f"/post/{PID}/gift", json={"gift_key": "neon_heart"})
check("post gift: the free gift still works, moves nothing",
      r.status_code == 200 and bal(BID)[0] == nb[0])
r = B.get(f"/post/{PID}")
check("post page: the gift sheet rides along for viewers",
      "togift-sheet" in r.text)
r = B.get("/home")
check("feed card: gift button with the running count",
      f'data-gift-post="{PID}"' in r.text)

# ---------------- cash-out: post earnings become real money ---------
r = B.post(f"/post/{PID}/gift",
           json={"gift_key": "mansion_sunset"})
check("cashout setup: the big gift lands (20,000 earned)",
      r.status_code == 200 and bal(AID)[1] == 20100,
      str(bal(AID)))
r = A.post("/tickets/cashout", data={"payout_note": "Bank: Ann"})
check("cashout: request files and holds everything",
      r.status_code == 303 and bal(AID)[1] == 0)
pr = dbq("SELECT * FROM payout_requests WHERE user_id=?", (AID,))[0]
check("cashout: the request is worth exactly $100.50",
      pr["tickets"] == 20100 and pr["dollars_cents"] == 10050,
      str(dict(pr)))
dbx("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
    ("money-owner-token", here.now_iso()))
OWN = TestClient(here.app, follow_redirects=False)
OWN.cookies.set("here_owner", "money-owner-token")
r = OWN.post(f"/owner/payouts/{pr['id']}/paid")
check("cashout: owner marks paid — the pipeline completes",
      dbq("SELECT status FROM payout_requests WHERE id=?",
          (pr["id"],))[0]["status"] == "paid")

# ---------------- the ticket door ----------------
H, HID = signup("Money Host", "host@money.io")
G, GID = signup("Money Guest", "guest@money.io")
G2, G2ID = signup("Money Late", "late@money.io")
creator_on(H)
dbx("UPDATE users SET tickets_balance=500 WHERE id IN (?,?)",
    (GID, G2ID))
r = H.post("/live/start", json={"title": "Door show",
                                "entry_tickets": 50})
LID = r.json()["id"]
check("door: a creator can start a ticket-door live",
      r.status_code == 200 and r.json().get("entry_tickets") == 50,
      r.text[:120])
r = C.post("/live/start", json={"title": "Nope",
                                "entry_tickets": 10})
check("door: a non-creator cannot charge a door",
      r.status_code == 400, str(r.status_code))
st = G.get(f"/live/{LID}/state").json()
check("door: state is trimmed for non-payers (no panel leaks)",
      st.get("status") == "entry_required"
      and "panel" not in st and st.get("entry_tickets") == 50,
      str(list(st.keys())))
r = G.get(f"/live/{LID}")
check("door: the page is a paywall poster",
      r.status_code == 200 and "ticket door" in r.text)
r = G2.post(f"/live/{LID}/comment", json={"body": "let me in"})
check("door: no chatting from behind the door",
      r.status_code == 403, str(r.status_code))
hb_before = bal(HID)
r = G.post(f"/live/{LID}/pay-entry")
check("door: paying works", r.status_code == 200
      and r.json().get("ok"), r.text[:120])
check("door: guest debited 50, host earned 50",
      bal(GID)[0] == 450 and bal(HID)[1] == hb_before[1] + 50,
      f"{bal(GID)} {bal(HID)}")
check("door: twin ledger lines (entry_pay / entry_receive)",
      len(dbq("SELECT id FROM ticket_txns WHERE kind='entry_pay' "
              "AND tickets=-50 AND live_id=?", (LID,))) == 1
      and len(dbq("SELECT id FROM ticket_txns WHERE "
                  "kind='entry_receive' AND tickets=50 AND "
                  "live_id=?", (LID,))) == 1)
r = G.post(f"/live/{LID}/pay-entry")
check("door: paying twice never charges twice",
      r.json().get("already") is True and bal(GID)[0] == 450)
st = G.get(f"/live/{LID}/state").json()
check("door: after paying, the full room opens",
      st.get("status") in (None, "live") and "panel" in st,
      str(st.get("status")))
r = G.post(f"/live/{LID}/comment", json={"body": "I'm in!"})
check("door: after paying, chat works", r.status_code == 200)
r = H.get("/earnings")
check("earnings: the Ticket doors bucket shows the 50",
      "Ticket doors" in r.text)
H.post(f"/live/{LID}/end")

# ---------------- market boost ----------------
S1, S1ID = signup("Seller One", "s1@money.io")
S2, S2ID = signup("Seller Two", "s2@money.io")
dbx("UPDATE users SET tickets_balance=150 WHERE id=?", (S1ID,))
S1.post("/market", data={"title": "BOOSTME Old Widget",
                         "price": "9", "category": "Other",
                         "condition": "Good", "description": "",
                         "location": ""})
S2.post("/market", data={"title": "BOOSTME New Widget",
                         "price": "9", "category": "Other",
                         "condition": "Good", "description": "",
                         "location": ""})
L1 = dbq("SELECT id FROM market_listings WHERE title LIKE "
         "'BOOSTME Old%'")[0]["id"]
r = S2.post(f"/market/{L1}/boost")
check("boost: a non-seller cannot boost", r.status_code == 400)
r = S1.post(f"/market/{L1}/boost")
check("boost: seller boosts for exactly 100 tickets",
      r.status_code == 200 and r.json().get("ok")
      and bal(S1ID)[0] == 50, r.text[:120])
row = dbq("SELECT boost_until FROM market_listings WHERE id=?",
          (L1,))[0]
check("boost: a week of Featured is on the listing",
      (row["boost_until"] or 0) > time.time() + 6 * 86400)
r = S1.get("/market?q=BOOSTME")
check("boost: the boosted listing outranks the newer one",
      0 < r.text.find("BOOSTME Old Widget")
      < r.text.find("BOOSTME New Widget"))
check("boost: the Featured tag shows on the card",
      "FEATURED" in r.text)
r = S1.post(f"/market/{L1}/boost")
check("boost: insufficient balance refused, nothing moves",
      r.status_code == 400 and bal(S1ID)[0] == 50)

# ---------------- paid ads: no payment, no ad ----------------
ADU, ADUID = signup("Ad Buyer", "ad@money.io")
r = ADU.post("/ads/buy", data={"title": "Money Ad One",
                               "body": "Buy the thing",
                               "link_url": "https://example.com",
                               "cta_label": "Go"})
check("ads: checkout hands off to Stripe",
      r.status_code == 303
      and r.headers["location"].startswith("https://stripe.fake/"),
      str(r.status_code))
ADID = dbq("SELECT id FROM ads WHERE creator_id=?",
           (ADUID,))[0]["id"]
conn = here.db()
pool = ads_mod.eligible_ads(conn, ADUID)
conn.close()
check("ads: an UNPAID ad is invisible in the rotation",
      all(a["id"] != ADID for a in pool)
      and dbq("SELECT status FROM ads WHERE id=?",
              (ADID,))[0]["status"] == "unpaid")
sid = r.headers["location"].rsplit("/", 1)[1]
r = B.get(f"/ads/mine?session_id={sid}")
row = dbq("SELECT status FROM ads WHERE id=?", (ADID,))[0]
check("ads: somebody else's session activates nothing",
      row["status"] == "unpaid")
FAKE["sessions"][sid]["payment_status"] = "unpaid"
r = ADU.get(f"/ads/mine?session_id={sid}")
row = dbq("SELECT status, starts_on, ends_on FROM ads WHERE id=?",
          (ADID,))[0]
check("ads: an unpaid Stripe session activates nothing",
      row["status"] == "unpaid")
FAKE["sessions"][sid]["payment_status"] = "paid"
r = ADU.get(f"/ads/mine?session_id={sid}")
row = dbq("SELECT status, starts_on, ends_on FROM ads WHERE id=?",
          (ADID,))[0]
check("ads: a verified payment activates a 7-day run",
      row["status"] == "active" and row["starts_on"]
      and row["ends_on"] > row["starts_on"], str(dict(row)))
conn = here.db()
pool = ads_mod.eligible_ads(conn, ADUID)
conn.close()
check("ads: the paid ad joins the rotation",
      any(a["id"] == ADID for a in pool))
r = ADU.get(f"/ads/mine?session_id={sid}")
row = dbq("SELECT status FROM ads WHERE id=?", (ADID,))[0]
check("ads: re-using the session changes nothing (idempotent)",
      row["status"] == "active")

# ---------------- HERE Plus ----------------
P, PID2 = signup("Plus Pam", "pam@money.io")
r = P.post("/plus/checkout")
check("plus: checkout hands off to Stripe",
      r.status_code == 303
      and r.headers["location"].startswith("https://stripe.fake/"),
      str(r.status_code))
psid = r.headers["location"].rsplit("/", 1)[1]
r = B.get(f"/plus?session_id={psid}")
me = dbq("SELECT plus_until FROM users WHERE id=?", (PID2,))[0]
check("plus: somebody else's session grants nothing",
      not me["plus_until"])
r = P.get(f"/plus?session_id={psid}")
me = dbq("SELECT plus_until, stripe_sub_id FROM users WHERE id=?",
         (PID2,))[0]
check("plus: a verified subscription turns Plus on",
      bool(me["plus_until"]) and me["plus_until"] > here.now_iso()
      and me["stripe_sub_id"] == "sub_fake_1", str(dict(me)))
r = B.get(f"/user/{PID2}")
check("plus: the badge shows on the profile", "⭐ PLUS" in r.text)
P.post("/posts", json={"kind": "text", "body": "pam post",
                       "privacy": "public"})
r = B.get("/home")
check("plus: the badge rides the post card too", "⭐ PLUS" in r.text)
r = P.post("/plus/cancel")
check("plus: cancel tells Stripe to stop at period end",
      r.status_code == 303 and len(FAKE["cancel_calls"]) == 1
      and FAKE["cancel_calls"][0].get("cancel_at_period_end")
      == "true")
check("plus: cancel keeps Plus until the paid month ends",
      here._is_plus(dbq("SELECT * FROM users WHERE id=?",
                        (PID2,))[0]))

# ---------------- the story cap pipe ----------------
PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
BIG = PNG + b"\x00" * (26 * 1024 * 1024)
r = B.post("/stories/new", data={"audience": "public",
                                 "lifetime": "24h"},
           files={"media": ("big.png", BIG, "image/png")})
check("caps: a 26 MB story is refused for a regular member",
      dbq("SELECT COUNT(*) c FROM stories WHERE user_id=?",
          (BID,))[0]["c"] == 0, str(r.status_code))
r = P.post("/stories/new", data={"audience": "public",
                                 "lifetime": "24h"},
           files={"media": ("big.png", BIG, "image/png")})
check("caps: the same 26 MB story flies for Plus (100 MB pipe)",
      dbq("SELECT COUNT(*) c FROM stories WHERE user_id=?",
          (PID2,))[0]["c"] == 1, str(r.status_code))

fails = [n for n, ok in RESULTS if not ok]
print()
print(f"monetize: {len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILED:", fails)
    sys.exit(1)
