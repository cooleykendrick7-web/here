"""Build MONEY tests: owner ad DELETE + the streamer earnings
dashboard (/earnings).

Part 1 — POST /owner/ads/{id}/delete: owner-only hard delete (row +
ad_events gone, active ad leaves rotation at once), media file
removed only when it lives under ads/ and no other ad references
the same path, page-owner pause/end untouched.

Part 2 — GET /earnings: login-gated private dashboard. Per-stream
sums group 'gift_receive' credits by ticket_txns.live_id (the gift
+ shout writers set it); NULL-live_id credits form the honest
"Earlier streams" bucket. Dashboard sums must equal the ledgers
(gift_lifetime / gift_earned). The cash-out center posts to the
EXISTING POST /tickets/cashout handler — same min / note / hold /
one-pending rules — and paid history + bank state mirror the
wallet. Dollars appear only as transactional figures (in-flight
amount, exact cash-out amount, per-request history, paid total);
no rate copy anywhere. Build RICH (Ken's order) amends the page:
the earned figures also carry their dollar worth and the cash-out
center states the exact shortfall to the minimum — asserted
below at the old discipline check's slot, at greater strength.

Run:  ./venv/bin/python tests/test_earnings.py
Uses a throwaway DB/uploads dir; no network.
"""
import os
import re
import sqlite3
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-earnings-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import ads  # noqa: E402
import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


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


def owner_client():
    dbx("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
        ("money-owner-token", here.now_iso()))
    c = TestClient(here.app, follow_redirects=False)
    c.cookies.set("here_owner", "money-owner-token")
    return c


def grant_spendable(uid, n):
    dbx("UPDATE users SET tickets_balance = "
        "COALESCE(tickets_balance,0) + ? WHERE id=?", (n, uid))
    dbx("INSERT INTO ticket_txns(user_id, kind, tickets, created_at) "
        "VALUES(?,?,?,?)", (uid, "buy", n, here.now_iso()))


def seed_earned(uid, n, live_id=None):
    """A historical gift credit the honest ledger way (txn row +
    both earned counters move together)."""
    dbx("INSERT INTO ticket_txns(user_id, kind, tickets, live_id, "
        "created_at) VALUES(?,?,?,?,?)",
        (uid, "gift_receive", n, live_id, here.now_iso()))
    dbx("UPDATE users SET gift_earned = COALESCE(gift_earned,0) + ?, "
        "gift_lifetime = COALESCE(gift_lifetime,0) + ? WHERE id=?",
        (n, n, uid))


def seg(text, start, span=500):
    i = text.find(start)
    return text[i:i + span] if i >= 0 else ""


OWN = owner_client()
ANON = TestClient(here.app, follow_redirects=False)

# ---------------- 1. guards + schema --------------------------------
r = ANON.get("/earnings")
check("logged-out /earnings -> redirect to /login",
      r.status_code == 303 and "/login" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location')}")
cols = [row["name"] for row in dbq("PRAGMA table_info(ticket_txns)")]
check("ticket_txns carries live_id (attribution column)", "live_id" in cols)

# ---------------- 2. two streams of gifts ----------------------------
H, HID = signup("Host Holly", "holly@x.io")
V, VID = signup("Viewer Vic", "vic@x.io")
A, AID = signup("Viewer Val", "val@x.io")
N, NID = signup("New Ned", "ned@x.io")

r = H.post("/live/start", json={"title": "Sunrise Set"})
LID1 = r.json()["id"]
assert V.post(f"/live/{LID1}/heartbeat").status_code == 200
grant_spendable(VID, 500)
r = V.post(f"/live/{LID1}/gift", json={"gift_key": "here_rocket"})
assert r.json().get("ok"), r.text[:160]
r = V.post(f"/live/{LID1}/gift", json={"gift_key": "gift_stack"})
assert r.json().get("ok"), r.text[:160]
r = V.post(f"/live/{LID1}/shout", json={"body": "Great stream!"})
assert r.json().get("ok"), r.text[:160]
H.post(f"/live/{LID1}/end")

r = H.post("/live/start", json={"title": "Night Drive"})
LID2 = r.json()["id"]
assert A.post(f"/live/{LID2}/heartbeat").status_code == 200
grant_spendable(AID, 300)
r = A.post(f"/live/{LID2}/gift", json={"gift_key": "money_heart"})
assert r.json().get("ok"), r.text[:160]
H.post(f"/live/{LID2}/end")

# writers attributed every credit to its live
rows = dbq("SELECT live_id, COUNT(*) c FROM ticket_txns WHERE "
           "user_id=? AND kind='gift_receive' GROUP BY live_id",
           (HID,))
by_live = {row["live_id"]: row["c"] for row in rows}
check("gift+shout credits carry their live_id",
      by_live == {LID1: 3, LID2: 1}, str(by_live))

html = H.get("/earnings").text
check("earnings page 200 for the host", "My earnings" in html)
check("both streams listed, newest first",
      html.find("Night Drive") > -1
      and html.find("Night Drive") < html.find("Sunrise Set"))
s1 = seg(html, "Sunrise Set")
check("stream 1: 55 tickets (25 + 10 gifts + 20 shout)",
      "55 🎟️" in s1, s1[:300])
check("stream 1: gifts count is 2 (the shout is not a gift)",
      "2 gifts received" in s1, s1[:300])
s2 = seg(html, "Night Drive")
check("stream 2: 100 tickets, 1 gift",
      "100 🎟️" in s2 and "1 gift received" in s2, s2[:300])
check("no Earlier-streams bucket before any legacy credit",
      "Earlier streams" not in html)
u = dbq("SELECT gift_earned, gift_lifetime FROM users WHERE id=?",
        (HID,))[0]
check("dashboard sums equal the ledger (155 earned / lifetime)",
      u["gift_earned"] == 155 and u["gift_lifetime"] == 155
      and html.count(">155 🎟️<") >= 2,
      f"db={u['gift_earned']}/{u['gift_lifetime']}")
check("Build RICH dollar clarity: worth + shortfall math show "
      "(155 earned -> worth $0.77, 1845 more = $9.23 more, min "
      "2,000 = $10.00), still no cents sign / no rate copy",
      "¢" not in html and "per ticket" not in html
      and "worth $0.77" in html
      and "1845 more earned tickets" in html
      and "($9.23 more)" in html
      and "minimum 2,000 tickets = $10.00" in html)
check("paid-out total renders as an honest $0.00", "$0.00" in html)

# entry points: own surfaces only
check("own profile links My earnings",
      'href="/earnings"' in H.get(f"/user/{HID}").text)
check("another viewer never sees the earnings link on that profile",
      'href="/earnings"' not in V.get(f"/user/{HID}").text)
check("tickets page links My earnings",
      'href="/earnings"' in H.get("/tickets").text)

# ---------------- 3. legacy bucket -----------------------------------
seed_earned(HID, 40)  # live_id NULL: pre-attribution history
html = H.get("/earnings").text
eb = seg(html, "Earlier streams")
check("legacy credits aggregate into the Earlier streams bucket",
      "Earlier streams" in html and "40 🎟️" in eb, eb[:300])
check("bucket joins the totals (195 everywhere)",
      ">195 🎟️<" in html
      and dbq("SELECT gift_earned g FROM users WHERE id=?",
              (HID,))[0]["g"] == 195)

# ---------------- 4. zero state --------------------------------------
html = N.get("/earnings").text
check("non-earner sees a clean zero state",
      "No earnings yet" in html and "$0.00" in html)
check("non-earner gets no cash-out form",
      'action="/tickets/cashout"' not in html)

# ---------------- 5. cash-out center (the existing handler) ----------
seed_earned(HID, 2500)  # earned/lifetime now 2695
html = H.get("/earnings").text
check("cash-out form surfaces on /earnings at the min",
      'action="/tickets/cashout"' in html)
check("the exact cash-out amount shows in the cash-out area",
      "$13.47" in html)
r = H.post("/tickets/cashout", data={"payout_note": "Cash App $holly"})
check("cash-out POST -> 303", r.status_code == 303)
prs = dbq("SELECT * FROM payout_requests WHERE user_id=?", (HID,))
check("one pending request, ALL earned held, honest dollars",
      len(prs) == 1 and prs[0]["status"] == "pending"
      and prs[0]["tickets"] == 2695 and prs[0]["dollars_cents"] == 1347)
PRID = prs[0]["id"]
check("hold moved the earned ledger to zero + hold txn written",
      dbq("SELECT gift_earned g FROM users WHERE id=?",
          (HID,))[0]["g"] == 0
      and dbq("SELECT tickets FROM ticket_txns WHERE user_id=? AND "
              "kind='cashout_hold'", (HID,))[0]["tickets"] == -2695)
r = H.post("/tickets/cashout", data={"payout_note": "again"})
check("second request refused while one is pending",
      len(dbq("SELECT * FROM payout_requests WHERE user_id=?",
              (HID,))) == 1)
html = H.get("/earnings").text
check("dashboard shows the in-flight cash-out (tickets + $)",
      "2695 🎟️" in html and "$13.47" in html
      and "waiting for the owner's approval" in html)
check("history lists the pending request",
      "Waiting for approval" in html)
check("no cash-out form while a request is in flight",
      'action="/tickets/cashout"' not in html)

# min + note rules bite from any surface
dbx("UPDATE users SET gift_earned=100 WHERE id=?", (NID,))
r = N.post("/tickets/cashout", data={"payout_note": "Cash App $ned"})
check("below the 2000 min: no request, tickets unmoved",
      dbq("SELECT COUNT(*) c FROM payout_requests WHERE user_id=?",
          (NID,))[0]["c"] == 0
      and dbq("SELECT gift_earned g FROM users WHERE id=?",
              (NID,))[0]["g"] == 100)
dbx("UPDATE users SET gift_earned=2500 WHERE id=?", (AID,))
r = A.post("/tickets/cashout", data={"payout_note": ""})
check("empty payout note: no request, tickets unmoved",
      dbq("SELECT COUNT(*) c FROM payout_requests WHERE user_id=?",
          (AID,))[0]["c"] == 0
      and dbq("SELECT gift_earned g FROM users WHERE id=?",
              (AID,))[0]["g"] == 2500)

# owner marks it paid (existing owner flow) -> paid totals move
r = OWN.post(f"/owner/payouts/{PRID}/paid")
check("owner mark-paid -> 303", r.status_code == 303)
html = H.get("/earnings").text
check("paid total now reads $13.47 and history shows Paid",
      "Paid out to you so far" in html and "$13.47" in html
      and "✅ Paid" in html)
check("paid request leaves the in-flight slot",
      "waiting for the owner's approval" not in html)

# ---------------- 6. ad delete ---------------------------------------
# a lone house ad: refusals first, then rotation proof, then delete
r = OWN.post("/owner/ads", data={
    "title": "Delete Me House", "body": "going away",
    "link_url": "https://example.com/x", "cta_label": "Go",
    "schedule": "always"})
check("owner creates the house ad", r.status_code == 303)
AD1 = dbq("SELECT id FROM ads WHERE title='Delete Me House'")[0]["id"]
panel = OWN.get("/owner").text
check("owner panel shows the 🗑 Remove control for the ad",
      f'action="/owner/ads/{AD1}/delete"' in panel
      and "Remove this ad completely?" in panel)

r = V.post(f"/owner/ads/{AD1}/delete")
check("regular user delete refused (owner gate), ad intact",
      r.status_code == 303
      and dbq("SELECT COUNT(*) c FROM ads WHERE id=?", (AD1,))[0]["c"]
      == 1)
r = ANON.post(f"/owner/ads/{AD1}/delete")
check("logged-out delete refused, ad intact",
      dbq("SELECT COUNT(*) c FROM ads WHERE id=?", (AD1,))[0]["c"] == 1)

F1, _ = signup("Feed Fred", "fred@x.io")
fh = F1.get("/home").text
check("active house ad is in rotation", "Delete Me House" in fh)
r = F1.get(f"/ads/{AD1}/click")
check("click registers through the click route", r.status_code == 302)
ev = dbq("SELECT COUNT(*) c FROM ad_events WHERE ad_id=?",
         (AD1,))[0]["c"]
check("ad has events before the delete", ev >= 1, str(ev))

r = OWN.post(f"/owner/ads/{AD1}/delete")
check("owner delete -> 303", r.status_code == 303)
check("ad row AND its events are gone",
      dbq("SELECT COUNT(*) c FROM ads WHERE id=?", (AD1,))[0]["c"] == 0
      and dbq("SELECT COUNT(*) c FROM ad_events WHERE ad_id=?",
              (AD1,))[0]["c"] == 0)
F2, _ = signup("Feed Fran", "fran@x.io")
check("deleted ad leaves rotation immediately",
      "Delete Me House" not in F2.get("/home").text)

# media under ads/: deleted when unshared, kept while shared
r = OWN.post("/owner/ads",
             data={"title": "Media House Ad", "schedule": "always"},
             files={"media": ("probe.png", PNG, "image/png")})
check("owner creates a media house ad", r.status_code == 303)
AD2 = dbq("SELECT * FROM ads WHERE title='Media House Ad'")[0]
MPATH = AD2["media_path"]
fpath = os.path.join(os.environ["HERE_UPLOADS"], MPATH)
check("ad media stored under the ads/ prefix and on disk",
      MPATH.startswith("ads/") and os.path.isfile(fpath), MPATH)
dbx("INSERT INTO ads(creator_id, page_id, kind, title, body, "
    "media_kind, media_path, link_url, cta_label, tags, status, "
    "schedule, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (AD2["creator_id"], None, "house", "Clone Ad", "", "image",
     MPATH, "", "Learn more", "", "paused", "always",
     here.now_iso()))
AD3 = dbq("SELECT id FROM ads WHERE title='Clone Ad'")[0]["id"]
r = OWN.post(f"/owner/ads/{AD2['id']}/delete")
check("shared media survives the first delete",
      r.status_code == 303 and os.path.isfile(fpath))
r = OWN.post(f"/owner/ads/{AD3}/delete")
check("media file removed once the last reference is deleted",
      r.status_code == 303 and not os.path.exists(fpath))

# media borrowed from posts/ is never touched
os.makedirs(os.path.join(os.environ["HERE_UPLOADS"], "posts"),
            exist_ok=True)
keep = os.path.join(os.environ["HERE_UPLOADS"], "posts", "keep.png")
with open(keep, "wb") as fh2:
    fh2.write(PNG)
dbx("INSERT INTO ads(creator_id, page_id, kind, title, body, "
    "media_kind, media_path, link_url, cta_label, tags, status, "
    "schedule, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (HID, None, "house", "Borrowed Media Ad", "", "image",
     "posts/keep.png", "", "Learn more", "", "paused", "always",
     here.now_iso()))
AD4 = dbq("SELECT id FROM ads WHERE title='Borrowed Media Ad'")[0]["id"]
r = OWN.post(f"/owner/ads/{AD4}/delete")
check("borrowed posts/ media is NOT deleted with the ad",
      r.status_code == 303 and os.path.isfile(keep)
      and dbq("SELECT COUNT(*) c FROM ads WHERE id=?",
              (AD4,))[0]["c"] == 0)

# page ad: owner can remove it completely; page-owner switches intact
P, PID_USER = signup("Paige Page", "paige@x.io")
r = P.post("/pages", data={"name": "Taco Truck", "ptype": "seller",
                           "description": "Best tacos in town"})
PGID = dbq("SELECT id FROM pages WHERE owner_id=?",
           (PID_USER,))[0]["id"]
r = P.post(f"/page/{PGID}/promote", data={"promote_what": "page"})
check("page ad created", r.status_code == 303)
PAD = dbq("SELECT id FROM ads WHERE page_id=? AND kind='page'",
          (PGID,))[0]["id"]
F3, _ = signup("Feed Fern", "fern@x.io")
check("page ad is in rotation", "Taco Truck" in F3.get("/home").text)
r = OWN.post(f"/owner/ads/{PAD}/delete")
check("owner removes a page ad completely (row gone)",
      r.status_code == 303
      and dbq("SELECT COUNT(*) c FROM ads WHERE id=?",
              (PAD,))[0]["c"] == 0)
F4, _ = signup("Feed Finn", "finn@x.io")
check("removed page ad leaves rotation",
      "Taco Truck" not in F4.get("/home").text)
check("removed page ad leaves the owner's mini-dashboard",
      f"/page/{PGID}/promote/{PAD}/status"
      not in P.get(f"/page/{PGID}/promote").text)

r = P.post(f"/page/{PGID}/promote", data={"promote_what": "page"})
PAD2 = dbq("SELECT id FROM ads WHERE page_id=? AND kind='page'",
           (PGID,))[0]["id"]
r = P.post(f"/page/{PGID}/promote/{PAD2}/status",
           data={"status": "paused"})
check("page-owner pause still works after the delete build",
      r.status_code == 303
      and dbq("SELECT status FROM ads WHERE id=?",
              (PAD2,))[0]["status"] == "paused")
r = P.post(f"/page/{PGID}/promote/{PAD2}/status",
           data={"status": "ended"})
check("page-owner end still works",
      dbq("SELECT status FROM ads WHERE id=?",
          (PAD2,))[0]["status"] == "ended")
r = OWN.post(f"/owner/ads/{PAD2}/delete")
check("owner can remove even an ended ad",
      r.status_code == 303
      and dbq("SELECT COUNT(*) c FROM ads WHERE id=?",
              (PAD2,))[0]["c"] == 0)
conn = here.db()
left = {a["id"] for a in ads.eligible_ads(conn, VID, ads.local_now())}
conn.close()
check("no deleted ad id ever comes back from the engine",
      not ({AD1, AD2["id"], AD3, PAD, PAD2} & left))

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed")
if fails:
    print("FAILED:", *fails, sep="\n  ")
    sys.exit(1)
