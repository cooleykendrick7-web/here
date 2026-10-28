"""Top-tier round (Oct 28): numbered Original 100 seats + badge
surfaces + Founders wall, Lobby daily prompt + captain rotation +
voice-only hooks, live auctions, Claim-it holds, shop stats.

Run:  ./venv/bin/python tests/test_build_all.py
"""
import os
import sqlite3
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-all-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
os.environ["HERE_LOBBY_HOST"] = "host@example.com"
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


def state(c, lid):
    r = c.get(f"/live/{lid}/state")
    assert r.status_code == 200, r.status_code
    return r.json()


HC, host_id = signup("Lobby Host", "host@example.com")
A, a_id = signup("Member A", "a@example.com")
B, b_id = signup("Member B", "b@example.com")
C, c_id = signup("Member C", "c@example.com")

# ---------- Original 100: numbers + surfaces + wall ----------
nos = {r["email"]: r["founding_no"] for r in dbq(
    "SELECT email, founding_no FROM users")}
check("og: seats numbered in join order",
      nos["host@example.com"] == 1 and nos["a@example.com"] == 2
      and nos["c@example.com"] == 4, str(nos))
r = A.get(f"/user/{host_id}")
check("og: profile chip carries the number",
      "ORIGINAL" in r.text and "#1" in r.text)
r = TestClient(here.app, follow_redirects=False).get("/signup")
check("og: signup page counts the real seats left",
      "Only 96 of the 100 Original seats" in r.text)
r = A.get("/founders")
check("og: Founders wall lists members + open seats",
      "The Original 100" in r.text and "#1" in r.text
      and "still open" in r.text)
# badge on posts + comments
A.post("/posts", data={"kind": "text", "body": "first post from a founder", "privacy": "public"})
pid = dbq("SELECT id FROM posts WHERE user_id=?", (a_id,))[0]["id"]
r = B.get("/home")
check("og: feed post card wears the mini badge", "🏅 #2" in r.text)
B.post(f"/post/{pid}/comment", data={"body": "nice"})
r = A.get(f"/post/{pid}")
check("og: comment author wears the mini badge", "🏅 #3" in r.text)

# ---------- Lobby: prompt + captain ----------
r = A.get("/lobby")
lobby_id = int(r.headers["location"].rstrip("/").split("/")[-1])
st = state(A, lobby_id)
check("lobby: the daily question is on the wall",
      bool(st.get("prompt")) and st.get("is_lobby") is True,
      str(st.get("prompt")))
# A walks in first (captain), then B
A.post(f"/live/{lobby_id}/join-request")
B.post(f"/live/{lobby_id}/join-request")
st = state(C, lobby_id)
check("captain: earliest seated panelist holds the room",
      st.get("captain_id") == a_id, str(st.get("captain_id")))
row_a = [m for m in st["panel"] if m["user_id"] == a_id][0]
check("captain: the tile row is flagged", row_a.get("captain") is True)
r = A.post(f"/live/{lobby_id}/mute",
           json={"user_id": b_id, "mute": True})
check("captain: can mute like an admin", r.json().get("ok"),
      r.text[:120])
A.post(f"/live/{lobby_id}/mute", json={"user_id": b_id, "mute": False})
r = A.post(f"/live/{lobby_id}/spotlight", json={"user_id": b_id})
check("captain: can spotlight", r.json().get("ok"), r.text[:120])
r = C.post(f"/live/{lobby_id}/mute",
           json={"user_id": b_id, "mute": True})
check("captain: a plain watcher cannot mute", r.status_code == 403)
r = B.post(f"/live/{lobby_id}/mute",
           json={"user_id": a_id, "mute": True})
check("captain: a plain member cannot mute either",
      r.status_code == 403)
# the host walks in (heartbeat) -> captain stands down
HC.post(f"/live/{lobby_id}/heartbeat")
st = state(C, lobby_id)
check("captain: stands down when the host is in the room",
      st.get("captain_id") is None, str(st.get("captain_id")))
r = A.post(f"/live/{lobby_id}/mute",
           json={"user_id": b_id, "mute": True})
check("captain: ex-captain loses the power with the host home",
      r.status_code == 403)
# voice-only hooks exist in the client
live_html = open(os.path.join(BASE, "templates", "live.html")).read()
check("voice: controls + join choice shipped",
      'id="ctl-voice"' in live_html and 'id="join-voice"' in live_html
      and "applyVoiceOnly" in live_html)

# ---------- auctions + claims on a selling live ----------
S, s_id = signup("Seller Sam", "seller@example.com")
BY, by_id = signup("Buyer Bea", "buyer@example.com")
BY2, by2_id = signup("Buyer Bo", "bo@example.com")
S.post("/market", data={"title": "Vintage lamp", "price": "20",
                        "category": "Other", "condition": "Good",
                        "description": "", "location": ""})
lamp = dbq("SELECT id FROM market_listings WHERE seller_id=?",
           (s_id,))[0]["id"]
r = S.post(f"/market/{lamp}/go-live")
sale_live = int(r.headers["location"].rstrip("/").split("/")[-1])
# claims first: while no auction runs
r = BY.post(f"/market/{lamp}/claim")
check("claim: a watcher can hold the item", r.json().get("ok"),
      r.text[:120])
r = BY2.post(f"/market/{lamp}/claim")
check("claim: one holder only", r.status_code == 400
      and "holding" in r.json().get("error", ""), r.text[:120])
st = state(BY2, sale_live)
shop = st.get("shop") or []
check("claim: the strip shows the hold + stats",
      shop and shop[0]["claim"]["by_name"] == "Buyer Bea"
      and st["shop_stats"]["sold"] == 0, str(shop)[:200])
r = BY.post(f"/market/{lamp}/claim-release")
check("claim: the holder can release", r.json().get("ok"))
r = BY2.post(f"/market/{lamp}/claim")
check("claim: released item can be claimed again",
      r.json().get("ok"))
# auction on the same item
r = BY.post(f"/live/{sale_live}/auction/start",
            json={"listing_id": lamp, "seconds": 120})
check("auction: only the host starts one",
      r.status_code == 403, r.text[:100])
r = S.post(f"/live/{sale_live}/auction/start",
           json={"listing_id": lamp, "seconds": 120})
j = r.json()
check("auction: host starts on a shop item at its price",
      j.get("ok") and j["auction"]["current_cents"] == 2000
      and j["auction"]["seller_id"] == s_id, r.text[:200])
r = S.post(f"/live/{sale_live}/auction/start",
           json={"title": "Second", "start_cents": 100})
check("auction: one at a time", r.status_code == 400)
r = S.post(f"/live/{sale_live}/auction/bid",
           json={"amount_cents": 5000})
check("auction: the seller cannot bid", r.status_code == 400)
r = BY.post(f"/live/{sale_live}/auction/bid",
            json={"amount_cents": 1900})
check("auction: under the opening price is refused",
      r.status_code == 400, r.text[:120])
r = BY.post(f"/live/{sale_live}/auction/bid",
            json={"amount_cents": 2000})
j = r.json()
check("auction: opening bid leads",
      j.get("ok") and j["auction"]["leader_id"] == by_id)
r = BY2.post(f"/live/{sale_live}/auction/bid",
             json={"amount_cents": 2025})
check("auction: bids must clear the $0.50 step",
      r.status_code == 400)
r = BY2.post(f"/live/{sale_live}/auction/bid",
             json={"amount_cents": 2100})
j = r.json()
check("auction: outbid takes the lead",
      j.get("ok") and j["auction"]["leader_id"] == by2_id)
# anti-snipe: run the clock down, bid, clock extends
dbx("UPDATE live_auctions SET ends_at=? WHERE status='running'",
    (time.time() + 4,))
r = BY.post(f"/live/{sale_live}/auction/bid",
            json={"amount_cents": 2200})
j = r.json()
check("auction: a late bid extends the clock (anti-snipe)",
      j.get("ok") and j["auction"]["ends_at"] > time.time() + 8,
      str(j.get("auction", {}).get("ends_at")))
# end it: push the clock past, any state poll settles it
dbx("UPDATE live_auctions SET ends_at=? WHERE status='running'",
    (time.time() - 1,))
st = state(BY, sale_live)
au = st.get("auction") or {}
check("auction: settlement shows the winner",
      au.get("status") == "ended" and au.get("leader_id") == by_id,
      str(au)[:160])
n = dbq("SELECT COUNT(*) c FROM notifications WHERE user_id=? AND "
        "kind='auction_won'", (by_id,))
check("auction: winner notified", n[0]["c"] >= 1)
m = dbq("SELECT COUNT(*) c FROM messages WHERE sender_id=? AND "
        "receiver_id=?", (s_id, by_id))
check("auction: winner and seller connected by a thread",
      m[0]["c"] >= 1)
# claim-accept sells the lamp to its holder (Bo holds the claim)
r = S.post(f"/market/{lamp}/claim-accept")
check("claim: seller accepts the hold", r.json().get("ok"),
      r.text[:120])
row = dbq("SELECT status, claimed_by FROM market_listings "
          "WHERE id=?", (lamp,))[0]
check("claim: accepted claim marks SOLD and clears the hold",
      row["status"] == "sold" and row["claimed_by"] is None)
st = state(BY, sale_live)
check("stats: the strip counts the sale",
      st["shop_stats"]["sold"] == 1)
# custom auction + cancel
S.post("/live/start", json={"title": "Custom night"})
r = S.post(f"/live/{sale_live}/auction/start",
           json={"title": "Mystery box", "start_cents": 500,
                 "seconds": 60})
check("auction: the ended room rejects new starts (old live gone)",
      r.status_code in (400, 404) or not r.json().get("ok"),
      r.text[:100])
lst = dbq("SELECT id FROM live_sessions WHERE host_id=? AND "
          "status='live'", (s_id,))
new_live = lst[0]["id"]
r = S.post(f"/live/{new_live}/auction/start",
           json={"title": "Mystery box", "start_cents": 500,
                 "seconds": 60})
check("auction: custom item starts",
      r.json().get("ok")
      and r.json()["auction"]["title"] == "Mystery box")
r = S.post(f"/live/{new_live}/auction/cancel", json={})
check("auction: host can cancel", r.json().get("ok"))
st = state(BY, new_live)
check("auction: cancelled leaves the stage",
      st.get("auction") is None)

fails = [n for n, ok in RESULTS if not ok]
print()
print(f"build-all: {len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILED:", fails)
    sys.exit(1)
