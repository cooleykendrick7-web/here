"""Ken's 2-4-7 build (Oct 28): the Lobby (always-open room), live
selling (Market listings riding a live), and Original 100
founding seats.

Run:  ./venv/bin/python tests/test_build247.py
"""
import os
import sqlite3
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-247-")
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
    assert r.status_code == 303, (name, r.status_code, r.text[:200])
    uid = dbq("SELECT id FROM users WHERE email=?", (email,))[0]["id"]
    return c, uid


def state(c, lid):
    r = c.get(f"/live/{lid}/state")
    assert r.status_code == 200, r.status_code
    return r.json()


# ---------- Original 100 ----------
HC, host_id = signup("Lobby Host", "host@example.com")
A, a_id = signup("Member A", "a@example.com")
check("founding: first accounts hold seats",
      dbq("SELECT founding FROM users WHERE id=?",
          (host_id,))[0]["founding"] == 1
      and dbq("SELECT founding FROM users WHERE id=?",
              (a_id,))[0]["founding"] == 1)
r = A.get(f"/user/{host_id}")
check("founding: profile wears the ORIGINAL chip",
      "ORIGINAL" in r.text)
# Fill seats to 99, then the next TWO signups race the boundary.
dbx("INSERT INTO users(name, email, pw_hash, created_at, founding) "
    "SELECT 'Filler ' || n, 'f' || n || '@example.com', 'x', "
    "'2026-01-01T00:00:00+00:00', 1 FROM ("
    "WITH RECURSIVE s(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM s "
    "WHERE n<97) SELECT n FROM s)")
n = dbq("SELECT COUNT(*) c FROM users WHERE founding=1")[0]["c"]
check("founding: seats filled to 99 for the boundary test", n == 99)
B, b_id = signup("Member B", "b@example.com")
C, c_id = signup("Member C", "c@example.com")
check("founding: the 100th seat goes to the 100th real signup",
      dbq("SELECT founding FROM users WHERE id=?",
          (b_id,))[0]["founding"] == 1)
check("founding: seat 101 does not exist",
      dbq("SELECT founding FROM users WHERE id=?",
          (c_id,))[0]["founding"] == 0)
r = A.get(f"/user/{c_id}")
check("founding: no chip past the hundred", "ORIGINAL" not in r.text)

# ---------- The Lobby ----------
r = A.get("/lobby")
loc = r.headers.get("location", "")
check("lobby: /lobby redirects into the room",
      r.status_code in (302, 303, 307) and "/live/" in loc, loc)
lobby_id = int(loc.rstrip("/").split("/")[-1])
lrow = dbq("SELECT * FROM live_sessions WHERE id=?", (lobby_id,))[0]
check("lobby: hosted by the owner account, flagged is_lobby",
      lrow["host_id"] == host_id and lrow["is_lobby"] == 1)
r2 = B.get("/lobby")
check("lobby: one room only (get-or-create)",
      r2.headers.get("location", "").endswith(f"/live/{lobby_id}"))
r = A.get("/home")
check("lobby: pinned card on home", "The Lobby — always open" in r.text)
# the host is not in the room: his tile must read camera-off
st = state(A, lobby_id)
hrow = [m for m in st["panel"] if m["user_id"] == host_id][0]
check("lobby: absent host tile is camera-off, never fake-live",
      hrow["video_ok"] is False)
# walk-in seating
r = B.post(f"/live/{lobby_id}/join-request")
j = r.json()
check("lobby: join request auto-seats (door is open)",
      j.get("ok") and j.get("auto") is True, str(j)[:160])
st = state(A, lobby_id)
check("lobby: the walker-in is on the panel",
      any(m["user_id"] == b_id for m in st["panel"]))
# the sweep must never kill it, and the host starting his OWN
# live must not kill it either
dbx("UPDATE live_sessions SET host_seen_at="
    "'2020-01-01T00:00:00+00:00' WHERE id=?", (lobby_id,))
r = HC.post("/live/start", json={"title": "Host personal show"})
personal_id = r.json().get("live_id") or r.json().get("id")
lrow = dbq("SELECT status FROM live_sessions WHERE id=?",
           (lobby_id,))[0]
check("lobby: survives a stale sweep + the host's own live start",
      lrow["status"] == "live")
check("lobby: the host's personal live is a SEPARATE room",
      personal_id and personal_id != lobby_id)
# B is seated in the lobby; starting B's own live pulls B off it
r = B.post("/live/start", json={"title": "B show"})
st = state(A, lobby_id)
check("lobby: starting your own live still pulls you off the panel",
      not any(m["user_id"] == b_id for m in st["panel"]))

# ---------- Live selling ----------
S, s_id = signup("Seller Sam", "seller@example.com")
BY, by_id = signup("Buyer Bea", "buyer@example.com")
r = S.post("/market", data={"title": "Fresh eggs dozen",
                            "price": "6", "category": "Other",
                            "condition": "Good", "description": "",
                            "location": ""})
lst = dbq("SELECT id FROM market_listings WHERE seller_id=?",
          (s_id,))
check("sell: listing created", len(lst) == 1)
lid_listing = lst[0]["id"]
# a stranger cannot put Sam's listing on a live
r = BY.post(f"/market/{lid_listing}/go-live")
row = dbq("SELECT live_id FROM market_listings WHERE id=?",
          (lid_listing,))[0]
check("sell: only the seller can go live with it",
      row["live_id"] is None)
# the seller goes live with it: creates a live + attaches
r = S.post(f"/market/{lid_listing}/go-live")
loc = r.headers.get("location", "")
check("sell: go-live lands the seller in the room",
      r.status_code in (302, 303, 307) and "/live/" in loc, loc)
sale_live = int(loc.rstrip("/").split("/")[-1])
row = dbq("SELECT live_id FROM market_listings WHERE id=?",
          (lid_listing,))[0]
check("sell: listing rides the live", row["live_id"] == sale_live)
st = state(BY, sale_live)
shop = st.get("shop") or []
check("sell: the shop strip carries the listing to watchers",
      len(shop) == 1 and shop[0]["title"] == "Fresh eggs dozen"
      and shop[0]["price"] == "$6" and shop[0]["sold"] is False,
      str(shop)[:200])
r = BY.get(f"/market/{lid_listing}")
check("sell: the listing page flies the LIVE banner",
      "LIVE NOW" in r.text)
r = S.get(f"/market/{lid_listing}")
check("sell: the seller sees it is on their live",
      "on your live right now" in r.text)
# sell it mid-show: the strip flips to SOLD for every watcher
S.post(f"/market/{lid_listing}/sold")
st = state(BY, sale_live)
check("sell: marked sold mid-live flips the strip",
      (st.get("shop") or [{}])[0].get("sold") is True)
# a second listing joins the SAME live (no new room created)
S.post(f"/market/{lid_listing}/sold")  # back to available
r = S.post("/market", data={"title": "Honey jar", "price": "12",
                            "category": "Other", "condition": "Good",
                            "description": "", "location": ""})
lst2 = dbq("SELECT id FROM market_listings WHERE seller_id=? AND "
           "title='Honey jar'", (s_id,))[0]["id"]
r = S.post(f"/market/{lst2}/go-live")
check("sell: a second item joins the seller's running live",
      r.headers.get("location", "").endswith(f"/live/{sale_live}"))
st = state(BY, sale_live)
check("sell: two items on the strip",
      len(st.get("shop") or []) == 2)
# detach takes it off the strip
S.post(f"/market/{lst2}/detach-live")
st = state(BY, sale_live)
check("sell: detach clears the item from the strip",
      len(st.get("shop") or []) == 1)

fails = [n for n, ok in RESULTS if not ok]
print()
print(f"build247: {len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILED:", fails)
    sys.exit(1)
