"""Live Tickets tests: the gift economy — catalog/packs, Stripe
crediting (webhook + return-verify, idempotent), the wallet page,
gift + shout flows on a live, cash-out holds, owner payouts, the
two-ledger invariant, node --check, and migrations on a dev-DB copy.

Run:  ./venv/bin/python tests/test_gifts.py
Uses a throwaway DB/uploads dir; Stripe is ALWAYS monkeypatched or
signed with a test secret — no network.
"""
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-gift-")
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
    conn = sqlite3.connect(os.environ["HERE_DB"])
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def dbx(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"])
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


def bal(uid):
    r = dbq("SELECT tickets_balance, gift_earned FROM users WHERE id=?",
            (uid,))[0]
    return (r["tickets_balance"] or 0), (r["gift_earned"] or 0)


def grant_spendable(uid, n):
    """Test-only credit that keeps the ledger invariant honest."""
    dbx("UPDATE users SET tickets_balance = COALESCE(tickets_balance,0)"
        " + ? WHERE id=?", (n, uid))
    dbx("INSERT INTO ticket_txns(user_id, kind, tickets, created_at) "
        "VALUES(?,?,?,?)", (uid, "buy", n, here.now_iso()))


def ledger_ok(uid):
    spend, earned = bal(uid)
    s = dbq("SELECT COALESCE(SUM(tickets),0) s FROM ticket_txns WHERE "
            "user_id=? AND kind IN ('buy','gift_send','shout')",
            (uid,))[0]["s"]
    e = dbq("SELECT COALESCE(SUM(tickets),0) s FROM ticket_txns WHERE "
            "user_id=? AND kind IN ('gift_receive','cashout_hold',"
            "'cashout_return')", (uid,))[0]["s"]
    return spend == s and earned == e, (spend, s, earned, e)


# ---------------- catalog / packs / money math (units) ----------------
# Build P-GIFTS replaced the 8-emoji catalog with Ken's 67-gift art
# catalog; full integrity (all 67 keys/prices/art files) is asserted
# in tests/test_gifts_art.py — here just the shape this suite needs.
check("catalog is the 67-gift art catalog with the free Neon Heart",
      len(here.GIFT_CATALOG) == 67 and len(here.GIFT_ORDER) == 67
      and here.GIFT_CATALOG["neon_heart"]["tickets"] == 0
      and here.GIFT_CATALOG["here_crown"]["tickets"] == 1500
      and here.GIFT_CATALOG["royal_fortune"]["tickets"] == 10000
      and here.GIFT_CATALOG["mansion_sunset"]["tickets"] == 20000)
check("retired emoji gifts are NOT sendable catalog entries",
      all(k not in here.GIFT_CATALOG for k in
          ("ant", "bolt", "moon", "rocket", "diamond", "crown",
           "cutlass", "globe"))
      and here.LEGACY_GIFT_LABELS["globe"] == ("🌍", "Planet HERE"))
check("packs: 100/$0.99, 500/$4.99, 1500/$13.99",
      [(here.TICKET_PACKS[k]["tickets"],
        here.TICKET_PACKS[k]["price_cents"])
       for k in ("pack100", "pack500", "pack1500")] ==
      [(100, 99), (500, 499), (1500, 1399)])
check("cashout min 2000 -> $10.00 (1000 cents)",
      here.CASHOUT_MIN_TICKETS == 2000
      and here._earned_dollars(2000) == 1000)
check("earned dollars floors a stray half-cent",
      here._earned_dollars(2001) == 1000
      and here._earned_dollars(2500) == 1250)
check("shout costs 20 for 60s",
      here.SHOUT_COST_TICKETS == 20 and here.SHOUT_SECONDS == 60)

H, HID = signup("Host Hana", "host@g.io")
V, VID = signup("Viewer Vic", "vic@g.io")
R, RID = signup("Rich Rita", "rita@g.io")
A, AID = signup("Absent Abe", "abe@g.io")
M, MID = signup("Member Mia", "mia@g.io")
E, EID = signup("Earner Eli", "eli@g.io")
D, DID = signup("Declined Dee", "dee@g.io")
ANON = TestClient(here.app, follow_redirects=False)

# ---------------- stripe disabled ----------------
os.environ.pop("STRIPE_SECRET_KEY", None)
os.environ.pop("STRIPE_WEBHOOK_SECRET", None)
check("stripe_enabled() False with no key", here.stripe_enabled() is False)
r = V.get("/tickets")
check("wallet page renders with stripe off (200)",
      r.status_code == 200, str(r.status_code))
check("wallet shows the open-soon state",
      "Ticket sales open soon" in r.text)
r = V.post("/tickets/buy", data={"pack_key": "pack100"})
check("buy with stripe off -> 503", r.status_code == 503, str(r.status_code))
r = ANON.post("/api/stripe/webhook", content=b"{}",
              headers={"Stripe-Signature": "t=1,v1=x"})
check("webhook with no secret configured -> 400",
      r.status_code == 400, str(r.status_code))

# ---------------- webhook crediting (test secret) ----------------
TEST_SECRET = "<MASKED_SECRET_001>"
os.environ["STRIPE_WEBHOOK_SECRET"] = TEST_SECRET
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake"
check("stripe_enabled() True with a key", here.stripe_enabled() is True)


def webhook_event(payload_obj, secret=TEST_SECRET, event_type="checkout.session.completed"):
    body = json.dumps({"type": event_type,
                       "data": {"object": payload_obj}}).encode()
    t = int(time.time())
    sig = hmac.new(secret.encode(), f"{t}.".encode() + body,
                   hashlib.sha256).hexdigest()
    return ANON.post("/api/stripe/webhook", content=body,
                     headers={"Stripe-Signature": f"t={t},v1={sig}"})


sess1 = {"id": "cs_test_1", "metadata": {"user_id": str(VID),
         "pack_key": "pack500", "tickets": "500"}}
b0, _ = bal(VID)
r = webhook_event(sess1)
check("signed webhook accepted", r.status_code == 200, r.text[:120])
b1, _ = bal(VID)
check("webhook credited 500 tickets", b1 - b0 == 500, f"{b0}->{b1}")
r = webhook_event(sess1)
b2, _ = bal(VID)
check("webhook replay is idempotent (still 500 total)", b2 == b1)
rows = dbq("SELECT * FROM ticket_txns WHERE stripe_session='cs_test_1'")
check("exactly one buy txn for the session", len(rows) == 1
      and rows[0]["kind"] == "buy" and rows[0]["tickets"] == 500)
body = json.dumps({"type": "checkout.session.completed",
                    "data": {"object": sess1}}).encode()
t = int(time.time())
r = ANON.post("/api/stripe/webhook", content=body,
              headers={"Stripe-Signature": f"t={t},v1=deadbeef"})
check("bad signature -> 400", r.status_code == 400, str(r.status_code))
r = webhook_event({"id": "cs_other"}, event_type="payment_intent.created")
check("other event types ignored (200, no credit)",
      r.status_code == 200 and r.json().get("ignored") is True)
r = webhook_event({"id": "cs_test_2", "metadata": {
    "user_id": str(VID), "pack_key": "pack100", "tickets": "100"}},
    secret=TEST_SECRET, event_type="checkout.session.completed")
b3, _ = bal(VID)
check("second session credits its own 100", b3 - b2 == 100)

# ---------------- return-verify crediting (monkeypatched API) --------
real_api = here._stripe_api
VERIFY = {}


def fake_api_get(method, path, params=None):
    sid = path.rsplit("/", 1)[-1]
    return VERIFY[sid]


here._stripe_api = fake_api_get
VERIFY["cs_paid"] = {"payment_status": "paid", "metadata": {
    "user_id": str(RID), "pack_key": "pack1500", "tickets": "1500"}}
rb0, _ = bal(RID)
r = R.get("/tickets?session_id=cs_paid&bought=1")
rb1, _ = bal(RID)
check("return-verify credits a paid session", rb1 - rb0 == 1500,
      f"{rb0}->{rb1}")
check("wallet shows the bought banner", "1500 tickets added" in r.text)
r = R.get("/tickets?session_id=cs_paid&bought=1")
rb2, _ = bal(RID)
check("return-verify replay is idempotent", rb2 == rb1)
VERIFY["cs_unpaid"] = {"payment_status": "unpaid", "metadata": {
    "user_id": str(RID), "pack_key": "pack100", "tickets": "100"}}
r = R.get("/tickets?session_id=cs_unpaid")
rb3, _ = bal(RID)
check("unpaid session credits nothing", rb3 == rb2)
VERIFY["cs_theirs"] = {"payment_status": "paid", "metadata": {
    "user_id": str(VID), "pack_key": "pack100", "tickets": "100"}}
r = R.get("/tickets?session_id=cs_theirs")
rb4, _ = bal(RID)
vb4, _ = bal(VID)
check("someone else's session credits nobody via my page",
      rb4 == rb3 and vb4 == b3)

# ---------------- buy endpoint (monkeypatched session create) --------
CAPTURED = {}


def fake_api_post(method, path, params=None):
    CAPTURED["method"] = method
    CAPTURED["path"] = path
    CAPTURED["params"] = params
    return {"id": "cs_new", "url": "https://checkout.stripe.com/pay/cs_new"}


here._stripe_api = fake_api_post
r = R.post("/tickets/buy", data={"pack_key": "pack500"})
check("buy redirects 303 to the Stripe checkout URL",
      r.status_code == 303
      and r.headers.get("location") ==
      "https://checkout.stripe.com/pay/cs_new",
      f"{r.status_code} {r.headers.get('location')}")
p = CAPTURED["params"]
check("buy charges the PACK price (499) not client input",
      p["line_items[0][price_data][unit_amount]"] == "499"
      and p["metadata[tickets]"] == "500"
      and p["metadata[pack_key]"] == "pack500"
      and p["metadata[user_id]"] == str(RID))
check("buy success_url carries the session id back to /tickets",
      "{CHECKOUT_SESSION_ID}" in p["success_url"]
      and p["success_url"].endswith("&bought=1"))
r = R.post("/tickets/buy", data={"pack_key": "pack9999"})
check("unknown pack_key -> 400", r.status_code == 400)
here._stripe_api = real_api

# ---------------- gift flow on a live ----------------
r = H.post("/live/start", json={"title": "Gift night"})
LID = r.json()["id"]
for c in (V, R, M):
    assert c.post(f"/live/{LID}/heartbeat").status_code == 200
# Mia joins the panel (member presence path)
r = M.post(f"/live/{LID}/join-request")
req_id = r.json()["request"]["id"]
r = H.post(f"/live/{LID}/join-decide",
           json={"request_id": req_id, "approve": True})
assert r.json().get("ok"), r.text[:200]
grant_spendable(VID, 100)
grant_spendable(MID, 50)
grant_spendable(AID, 100)
vic_start = bal(VID)[0]  # 500+100 webhook buys + 100 grant = 700

vb0, _ = bal(VID)
_, he0 = bal(HID)
r = V.post(f"/live/{LID}/gift", json={"gift_key": "here_rocket"})
j = r.json()
check("viewer gifts HERE Rocket (25)", r.status_code == 200 and j.get("ok"),
      r.text[:160])
vb1, ve1 = bal(VID)
_, he1 = bal(HID)
check("gift moves exactly 25: sender spendable -25, host earned +25",
      vb1 == vb0 - 25 and he1 == he0 + 25, f"V {vb0}->{vb1} H {he0}->{he1}")
check("gift returns the new balance + gift id",
      j.get("balance") == vb1 and isinstance(j.get("gift_id"), int))
grows = dbq("SELECT * FROM live_gifts WHERE live_id=?", (LID,))
check("live_gifts row written", len(grows) == 1
      and grows[0]["gift_key"] == "here_rocket"
      and grows[0]["tickets"] == 25
      and grows[0]["sender_id"] == VID
      and grows[0]["host_id"] == HID)
txn_kinds = sorted((t["kind"], t["tickets"]) for t in dbq(
    "SELECT kind, tickets FROM ticket_txns WHERE live_id=? "
    "AND kind IN ('gift_send','gift_receive')", (LID,)))
check("gift ledger rows: gift_send -25 / gift_receive +25",
      txn_kinds == [("gift_receive", 25), ("gift_send", -25)],
      str(txn_kinds))
r = M.post(f"/live/{LID}/gift", json={"gift_key": "flying_cash"})
check("panel member can gift too", r.status_code == 200 and r.json().get("ok"))
r = V.post(f"/live/{LID}/gift", json={"gift_key": "mansion_sunset"})
check("insufficient balance gift refused",
      r.status_code == 400 and "Not enough tickets" in r.json().get("error", ""),
      r.text[:160])
vb2, _ = bal(VID)
check("refused gift moved nothing", vb2 == vb1)
r = H.post(f"/live/{LID}/gift", json={"gift_key": "money_bag"})
check("host self-gift refused", r.status_code == 400, str(r.status_code))
r = A.post(f"/live/{LID}/gift", json={"gift_key": "money_bag"})
check("absent user (no presence row) gift -> 403",
      r.status_code == 403, str(r.status_code))
r = V.post(f"/live/{LID}/gift", json={"gift_key": "nope"})
check("unknown gift_key -> 400", r.status_code == 400)
r = ANON.post(f"/live/{LID}/gift", json={"gift_key": "money_bag"})
check("logged-out gift -> 401", r.status_code == 401)

st = V.get(f"/live/{LID}/state").json()
check("state my_tickets = my spendable balance", st["my_tickets"] == vb2)
rg = st["recent_gifts"]
check("state recent_gifts carries gift with catalog art face",
      len(rg) == 2 and rg[0]["img"] == "/static/gifts/here_rocket.webp"
      and rg[0]["name"] == "HERE Rocket" and rg[0]["tickets"] == 25
      and rg[0]["anim"] == 1
      and rg[0]["sender_name"] == "Viewer Vic", str(rg))
tg = {t["name"]: t["tickets"] for t in st["top_gifters"]}
check("state top_gifters aggregates (Vic 25, Mia 5)",
      tg.get("Viewer Vic") == 25 and tg.get("Member Mia") == 5, str(tg))

# ---------------- shout-out pins ----------------
vb0, _ = bal(VID)
_, he0 = bal(HID)
r = V.post(f"/live/{LID}/shout", json={"body": "First shout hello!"})
check("shout bought (20)", r.status_code == 200 and r.json().get("ok"),
      r.text[:160])
vb1, _ = bal(VID)
_, he1 = bal(HID)
check("shout charges 20 and the host earns 20",
      vb1 == vb0 - 20 and he1 == he0 + 20, f"V {vb0}->{vb1} H {he0}->{he1}")
st = V.get(f"/live/{LID}/state").json()
check("state active_shout shows the pinned text",
      st["active_shout"] and st["active_shout"]["body"] == "First shout hello!"
      and st["active_shout"]["name"] == "Viewer Vic",
      str(st.get("active_shout")))
r = R.post(f"/live/{LID}/shout", json={"body": "Rita takes the pin"})
grant_spendable(RID, 0)  # no-op: Rita was credited 1500 via return-verify
check("second shout bought", r.status_code == 200, r.text[:160])
st = V.get(f"/live/{LID}/state").json()
check("newer shout replaces the older pin",
      st["active_shout"]["body"] == "Rita takes the pin")
dbx("UPDATE live_shouts SET expires_at='2000-01-01T00:00:00+00:00' "
    "WHERE live_id=?", (LID,))
st = V.get(f"/live/{LID}/state").json()
check("expired shout leaves the state", st["active_shout"] is None)
r = H.post(f"/live/{LID}/shout", json={"body": "host shout"})
check("host shout on own live refused", r.status_code == 400)
r = V.post(f"/live/{LID}/shout", json={"body": "   "})
check("empty shout refused", r.status_code == 400)
vb2, _ = bal(VID)
check("viewer balance exact after gifts+shout (start-25-20)",
      vb2 == vic_start - 45, f"{vic_start}->{vb2}")
# moderation: a profane shout still posts + charges, but flags (never blocks)
r = V.post(f"/live/{LID}/shout", json={"body": "this is fucked up"})
check("flagged-word shout still posts (flag-only rule)",
      r.status_code == 200, r.text[:160])
flags = dbq("SELECT * FROM moderation_flags WHERE item_type='live_shout'")
check("shout was moderation-flagged for the owner queue",
      len(flags) == 1 and flags[0]["status"] == "pending", str(len(flags)))
st = V.get(f"/live/{LID}/state").json()
check("flagged shout is the active pin (flags never block)",
      st["active_shout"]["body"] == "this is fucked up")

# ---------------- live page: gift UI + node --check ----------------
# (Checked while LID is still on air: starting another live would
# end this one — one live per host.)
page = H.get(f"/live/{LID}").text
for needle, label in [
        ("ctl-gift", "gift button in the bar"),
        ("Send a gift", "gift sheet title"),
        ("live-gift-banner", "big-gift banner"),
        ("live-top-gifters", "top gifters line"),
        ("live-shout", "shout pin element"),
        ("Pin a shout-out", "shout section in gift sheet"),
        ("recent_gifts", "client consumes recent_gifts"),
        ("active_shout", "client consumes active_shout"),
        ("handleGiftEvent", "gift animation handler"),
        ("spawnGiftFloat", "small-gift float animation"),
        ("showGiftBanner", "banner animation"),
        ("/gift", "gift POST path"),
        ("/shout", "shout POST path")]:
    check(f"live page: {label} present", needle in page)
blocks = re.findall(r"<script>(.*?)</script>", page, re.S)
node = shutil.which("node")
if node:
    all_ok, bad = True, []
    for i, block in enumerate(blocks):
        path = os.path.join(_tmp, f"chk_gift_{i}.js")
        with open(path, "w") as f:
            f.write(block)
        p = subprocess.run([node, "--check", path],
                           capture_output=True, text=True)
        if p.returncode != 0:
            all_ok = False
            bad.append(f"#{i}: {p.stderr.strip()[:160]}")
    check("node --check: live page inline JS parses", all_ok,
          "; ".join(bad))
else:
    check("node --check skipped (node unavailable)", True)

# gifts on an ended live -> 409
r = H.post("/live/start", json={"title": "Short live"})
LID2 = r.json()["id"]
V.post(f"/live/{LID2}/heartbeat")
H.post(f"/live/{LID2}/end")
r = V.post(f"/live/{LID2}/gift", json={"gift_key": "money_bag"})
check("gift on an ended live -> 409", r.status_code == 409, str(r.status_code))
r = V.post(f"/live/{LID2}/shout", json={"body": "too late"})
check("shout on an ended live -> 409", r.status_code == 409,
      str(r.status_code))

# ---------------- cash-out flow ----------------
# Eli earns exactly 2500 the honest ledger way (gift_receive rows).
for amt in (2000, 500):
    dbx("INSERT INTO ticket_txns(user_id, kind, tickets, created_at) "
        "VALUES(?,?,?,?)", (EID, "gift_receive", amt, here.now_iso()))
dbx("UPDATE users SET gift_earned=2500 WHERE id=?", (EID,))
# Dee earns 1500 (below min) + later tops to 2100 for the decline test.
dbx("INSERT INTO ticket_txns(user_id, kind, tickets, created_at) "
    "VALUES(?,?,?,?)", (DID, "gift_receive", 1500, here.now_iso()))
dbx("UPDATE users SET gift_earned=1500 WHERE id=?", (DID,))

r = D.post("/tickets/cashout", data={"payout_note": "Cash App $dee"})
check("below-min cash-out refused (303 + no request)",
      r.status_code == 303
      and not dbq("SELECT id FROM payout_requests WHERE user_id=?",
                  (DID,)), str(r.status_code))
r = E.post("/tickets/cashout", data={"payout_note": ""})
check("cash-out without a payout note refused",
      r.status_code == 303
      and not dbq("SELECT id FROM payout_requests WHERE user_id=?",
                  (EID,)))
r = E.post("/tickets/cashout", data={"payout_note": "Cash App $eli"})
check("cash-out filed (303)", r.status_code == 303)
_, ee = bal(EID)
check("cash-out holds ALL earned (2500 -> 0)", ee == 0, str(ee))
prs = dbq("SELECT * FROM payout_requests WHERE user_id=?", (EID,))
check("payout request: 2500 tickets = $12.50 pending",
      len(prs) == 1 and prs[0]["tickets"] == 2500
      and prs[0]["dollars_cents"] == 1250
      and prs[0]["status"] == "pending"
      and prs[0]["payout_note"] == "Cash App $eli", str([dict(x) for x in prs]))
PRID = prs[0]["id"]
check("cashout_hold txn -2500 on the earned ledger",
      dbq("SELECT tickets FROM ticket_txns WHERE user_id=? AND "
          "kind='cashout_hold'", (EID,))[0]["tickets"] == -2500)
r = E.get("/tickets")
check("wallet shows the pending cash-out (no second request offered)",
      "sends automatically" in r.text and 'action="/tickets/cashout"' not in r.text)
r = E.post("/tickets/cashout", data={"payout_note": "again"})
check("second cash-out while pending refused",
      len(dbq("SELECT id FROM payout_requests WHERE user_id=?",
              (EID,))) == 1)

# top Dee to 2100 and file hers too
dbx("INSERT INTO ticket_txns(user_id, kind, tickets, created_at) "
    "VALUES(?,?,?,?)", (DID, "gift_receive", 600, here.now_iso()))
dbx("UPDATE users SET gift_earned=2100 WHERE id=?", (DID,))
r = D.post("/tickets/cashout", data={"payout_note": "Bank: Dee"})
assert r.status_code == 303
DPRID = dbq("SELECT id FROM payout_requests WHERE user_id=?",
            (DID,))[0]["id"]

# ---------------- owner payout decisions ----------------
dbx("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
    ("gift-owner-token", here.now_iso()))
OWN = TestClient(here.app, follow_redirects=False)
OWN.cookies.set("here_owner", "gift-owner-token")
r = OWN.get("/owner")
check("owner dashboard renders with payout queue",
      r.status_code == 200 and "Cash App $eli" in r.text
      and "Payouts" in r.text, str(r.status_code))
check("owner money row: tickets sold = all buy txns (500+100 webhook,"
      " 1500 verify, 250 test grants = 2350)",
      "2350 🎟️" in r.text)
check("owner money row: liability = held+earned x 0.5c rendered as $",
      "Owed to streamers" in r.text)
r = V.post(f"/owner/payouts/{PRID}/paid")
check("non-owner cannot mark paid",
      r.status_code in (303, 401, 403)
      and dbq("SELECT status FROM payout_requests WHERE id=?",
              (PRID,))[0]["status"] == "pending", str(r.status_code))
r = OWN.post(f"/owner/payouts/{PRID}/paid")
check("owner marks paid (303)", r.status_code == 303)
check("request now paid with decided_at",
      dbq("SELECT status, decided_at FROM payout_requests WHERE id=?",
          (PRID,))[0]["status"] == "paid")
_, ee = bal(EID)
check("paid cash-out does NOT move balances again (hold already took them)",
      ee == 0)
check("cashout_paid audit txn written (0 delta)",
      dbq("SELECT tickets FROM ticket_txns WHERE user_id=? AND "
          "kind='cashout_paid'", (EID,))[0]["tickets"] == 0)
r = OWN.post(f"/owner/payouts/{PRID}/paid")
check("re-deciding a paid request is a no-op",
      dbq("SELECT COUNT(*) c FROM ticket_txns WHERE user_id=? AND "
          "kind='cashout_paid'", (EID,))[0]["c"] == 1)
r = OWN.post(f"/owner/payouts/{DPRID}/declined")
check("owner declines Dee's cash-out (303)", r.status_code == 303)
_, de = bal(DID)
check("decline returns the held tickets to earnings (2100)",
      de == 2100, str(de))
check("cashout_return txn +2100",
      dbq("SELECT tickets FROM ticket_txns WHERE user_id=? AND "
          "kind='cashout_return'", (DID,))[0]["tickets"] == 2100)
check("Dee can file again after a decline",
      dbq("SELECT status FROM payout_requests WHERE id=?",
          (DPRID,))[0]["status"] == "declined")

# ---------------- two-ledger invariant after the mixed run ----------------
for uid, nm in ((VID, "Vic"), (RID, "Rita"), (HID, "Hana"), (MID, "Mia"),
                (EID, "Eli"), (DID, "Dee"), (AID, "Abe")):
    ok, detail = ledger_ok(uid)
    check(f"ledger invariant holds for {nm}", ok, str(detail))

# ---------------- wallet page rendering of history ----------------
r = V.get("/tickets")
check("wallet history lines render (bought, sent, cash-out-less)",
      "Bought 100 🎟️" in r.text and "Sent HERE Rocket" in r.text,
      r.text[:0])
r = E.get("/tickets")
check("earner wallet history shows hold + paid lines",
      "Cash-out requested" in r.text and "Cash-out paid" in r.text)

# ---------------- migration: boot on a COPY of the dev DB ----------------
dev_db = os.path.join(BASE, "here.db")
if os.path.exists(dev_db):
    copy_db = os.path.join(_tmp, "devcopy.db")
    shutil.copyfile(dev_db, copy_db)
    env2 = dict(os.environ, HERE_DB=copy_db,
                HERE_UPLOADS=os.path.join(_tmp, "uploads2"))
    boot = subprocess.run(
        [sys.executable, "-c",
         "import app; from fastapi.testclient import TestClient;"
         "print(TestClient(app.app).get('/login').status_code)"],
        cwd=BASE, env=env2, capture_output=True, text=True)
    check("dev-DB copy boots (migrations apply)", boot.stdout.strip() == "200",
          boot.stderr[-400:])
    tabs = [r[0] for r in sqlite3.connect(copy_db).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    check("dev copy has the ticket tables",
          all(t in tabs for t in ("ticket_txns", "live_gifts",
                                 "live_shouts", "payout_requests")),
          str(tabs))
    ucols = [r[1] for r in sqlite3.connect(copy_db).execute(
        "PRAGMA table_info(users)")]
    check("dev copy users gained tickets_balance + gift_earned",
          "tickets_balance" in ucols and "gift_earned" in ucols)
else:
    check("dev-DB copy check skipped (no dev DB)", True)

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
