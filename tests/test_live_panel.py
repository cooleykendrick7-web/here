"""Panel test suite for the Go Live panel upgrade. Throwaway DB.

Run:  ./venv/bin/python tests/test_live_panel.py
"""
import os, sys, tempfile, sqlite3, json
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-live-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)
import app as here
from fastapi.testclient import TestClient

RESULTS = []
def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))

def dbq(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"]); conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, args).fetchall(); conn.close(); return rows

def dbx(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"]); conn.execute(sql, args); conn.commit(); conn.close()

def signup(name, email):
    c = TestClient(here.app, follow_redirects=False)
    r = c.post("/signup", data={"name": name, "email": email, "password": "pw123456"})
    assert r.status_code == 303, (name, r.status_code, r.text[:200])
    uid = dbq("SELECT id FROM users WHERE email=?", (email,))[0]["id"]
    return c, uid

H, HID = signup("Host H", "host@x.io")
G1, G1ID = signup("Guest One", "g1@x.io")
G2, G2ID = signup("Guest Two", "g2@x.io")
W, WID = signup("Watcher W", "w@x.io")
A, AID = signup("Admin A", "a@x.io")
A2, A2ID = signup("Admin Two", "a2@x.io")
NW, NWID = signup("Never Watches", "nw@x.io")
IV, IVID = signup("Invitee Cap", "iv@x.io")
R, RID = signup("Late R", "r@x.io")

r = H.post("/live/start", json={"title": "Panel night"})
check("live start ok", r.status_code == 200 and r.json().get("ok"), r.text[:200])
LID = r.json()["id"]
row = dbq("SELECT panel_order, like_count FROM live_sessions WHERE id=?", (LID,))[0]
check("start seeds panel_order=[host], like_count=0",
      json.loads(row["panel_order"]) == [HID] and row["like_count"] == 0, str(dict(row)))

for c in (G1, G2, W, A, A2, IV, R):
    rr = c.post(f"/live/{LID}/heartbeat")
    assert rr.status_code == 200, rr.text[:200]

# join-request + host approve
r = G1.post(f"/live/{LID}/join-request")
check("G1 join-request ok", r.json().get("ok"), r.text[:200])
req1 = r.json()["request"]["id"]
# --- permission matrix: a plain watcher is refused by EVERY mod tool ---
for _label, _r in [
    ("kick", W.post(f"/live/{LID}/kick", json={"user_id": G1ID, "scope": "panel"})),
    ("mute", W.post(f"/live/{LID}/mute", json={"user_id": G1ID, "mute": 1})),
    ("move", W.post(f"/live/{LID}/move", json={"user_id": G1ID, "dir": -1})),
    ("invite", W.post(f"/live/{LID}/invite", json={"user_id": AID})),
    ("appoint-admin", W.post(f"/live/{LID}/appoint-admin", json={"user_id": WID})),
    ("join-decide", W.post(f"/live/{LID}/join-decide", json={"request_id": req1, "approve": True})),
]:
    check(f"plain watcher {_label} -> 403", _r.status_code == 403, str(_r.status_code))
r = G2.post(f"/live/{LID}/join-decide", json={"request_id": req1, "approve": True})
check("watcher cannot join-decide (403)", r.status_code == 403, str(r.status_code))
r = H.post(f"/live/{LID}/join-decide", json={"request_id": req1, "approve": True})
check("host approves G1", r.json().get("ok"), r.text[:200])
st = G1.get(f"/live/{LID}/state").json()
check("G1 my_panel.am_member", st["my_panel"]["am_member"] is True, str(st.get("my_panel")))
check("panel has host+G1 in order", [p["user_id"] for p in st["panel"]] == [HID, G1ID], str(st.get("panel")))
check("legacy guest = first panelist", st["guest"] and st["guest"]["id"] == G1ID)
check("my_role guest", st["my_role"] == "guest")

# appoint admin (host only)
r = G1.post(f"/live/{LID}/appoint-admin", json={"user_id": AID})
check("member cannot appoint admin (403)", r.status_code == 403, str(r.status_code))
r = H.post(f"/live/{LID}/appoint-admin", json={"user_id": AID})
check("host appoints watcher A as admin", r.json().get("ok"), r.text[:200])
r = A.post(f"/live/{LID}/appoint-admin", json={"user_id": WID})
check("admin cannot appoint admin (403)", r.status_code == 403, str(r.status_code))
check("_live_role admin/member/host/watcher",
      here._live_role(here.db(), here._live_get(here.db(), LID), AID) == "admin")

# admin can join-decide
r = G2.post(f"/live/{LID}/join-request"); req2 = r.json()["request"]["id"]
r = A.post(f"/live/{LID}/join-decide", json={"request_id": req2, "approve": True})
check("admin approves G2", r.json().get("ok"), r.text[:200])
st = H.get(f"/live/{LID}/state").json()
check("panel now 3", [p["user_id"] for p in st["panel"]] == [HID, G1ID, G2ID], str(st.get("panel")))
check("host watchers list present w/ flags", "watchers" in st and any(w["user_id"] == AID and w["is_admin"] for w in st["watchers"]), str(st.get("watchers")))
stw = W.get(f"/live/{LID}/state").json()
check("watcher gets NO watchers key", "watchers" not in stw)

# --- admin-vs-admin hierarchy: a second admin is untouchable by A ---
r = H.post(f"/live/{LID}/appoint-admin", json={"user_id": A2ID})
check("host appoints second admin A2", r.json().get("ok"), r.text[:200])
r = A.post(f"/live/{LID}/kick", json={"user_id": A2ID, "scope": "panel"})
check("admin kicks another admin -> 403", r.status_code == 403, str(r.status_code))
r = A.post(f"/live/{LID}/mute", json={"user_id": A2ID, "mute": 1})
check("admin mutes an admin -> 403", r.status_code == 403, str(r.status_code))
r = H.post(f"/live/{LID}/remove-admin", json={"user_id": A2ID})
check("host remove-admin works (A2)", r.json().get("ok"), r.text[:200])
check("A2 no longer admin", here._live_is_admin(here.db(), LID, A2ID) is False)

# invite flow
r = H.post(f"/live/{LID}/invite", json={"user_id": NWID})
check("invite a non-watcher -> refused (400)", r.status_code == 400 and not r.json().get("ok"), r.text[:200])
r = H.post(f"/live/{LID}/invite", json={"user_id": G1ID})
check("invite a panel member -> refused (400)", r.status_code == 400 and not r.json().get("ok"), r.text[:200])
r = H.post(f"/live/{LID}/invite", json={"user_id": WID})
check("host invites W", r.json().get("ok"), r.text[:200])
inv_id = r.json()["invite"]["id"]
nrows = dbq("SELECT * FROM notifications WHERE user_id=? AND kind='live_invite'", (WID,))
check("invite creates a live_invite bell notification", len(nrows) >= 1, str(len(nrows)))
check("invitee's /notifications page shows the invite",
      "invited you to join a live panel" in W.get("/notifications").text)
r = H.post(f"/live/{LID}/invite", json={"user_id": WID})
check("duplicate invite returns same pending invite", r.json().get("ok") and r.json()["invite"]["id"] == inv_id, r.text[:200])
stw = W.get(f"/live/{LID}/state").json()
check("W pending_invites_for_me", stw["pending_invites_for_me"] and stw["pending_invites_for_me"][0]["id"] == inv_id and stw["pending_invites_for_me"][0]["from_name"] == "Host H", str(stw.get("pending_invites_for_me")))
r = G1.post(f"/live/{LID}/invite-decide", json={"request_id": inv_id, "accept": True})
check("non-invitee cannot invite-decide (403)", r.status_code == 403, str(r.status_code))
r = W.post(f"/live/{LID}/invite-decide", json={"request_id": inv_id, "accept": True})
check("W accepts invite -> member", r.json().get("ok"), r.text[:200])
stw = W.get(f"/live/{LID}/state").json()
check("W am_member after accept", stw["my_panel"]["am_member"] is True)

# invitee decline: the pending invite clears and no seat is taken
r = H.post(f"/live/{LID}/invite", json={"user_id": AID})
check("host invites admin A (watcher) ", r.json().get("ok"), r.text[:200])
inv_a = r.json()["invite"]["id"]
sta = A.get(f"/live/{LID}/state").json()
check("A sees the pending invite", any(i["id"] == inv_a for i in sta["pending_invites_for_me"]), str(sta.get("pending_invites_for_me")))
r = A.post(f"/live/{LID}/invite-decide", json={"request_id": inv_a, "accept": False})
check("A declines the invite", r.json().get("ok"), r.text[:200])
sta = A.get(f"/live/{LID}/state").json()
check("decline clears the pending invite", sta["pending_invites_for_me"] == [], str(sta.get("pending_invites_for_me")))
check("declined invitee is not a member", sta["my_panel"]["am_member"] is False)
# pending invite for the cap test: IV is invited NOW (while a watcher,
# panel not full); IV only tries to accept AFTER the panel fills.
r = H.post(f"/live/{LID}/invite", json={"user_id": IVID})
check("host invites IV (cap-test invitee)", r.json().get("ok"), r.text[:200])
inv_iv = r.json()["invite"]["id"]

# likes
for _ in range(3):
    r = W.post(f"/live/{LID}/like")
check("like returns count 3", r.json().get("like_count") == 3, r.text[:200])
st = G1.get(f"/live/{LID}/state?since_like=0").json()
check("new_likes=3, like_count=3", st["new_likes"] == 3 and st["like_count"] == 3, str({k: st.get(k) for k in ("new_likes","like_count","like_cursor")}))
cur = st["like_cursor"]
st = G1.get(f"/live/{LID}/state?since_like={cur}").json()
check("new_likes=0 after cursor advance", st["new_likes"] == 0, str(st.get("new_likes")))
# second poll delta: two fresh likes are counted exactly once
W.post(f"/live/{LID}/like"); W.post(f"/live/{LID}/like")
st = G1.get(f"/live/{LID}/state?since_like={cur}").json()
check("second poll delta new_likes=2, like_count=5",
      st["new_likes"] == 2 and st["like_count"] == 5,
      str({k: st.get(k) for k in ("new_likes", "like_count", "like_cursor")}))
cur2 = st["like_cursor"]
st = G1.get(f"/live/{LID}/state?since_like={cur2}").json()
check("delta is empty once the cursor advances again", st["new_likes"] == 0, str(st.get("new_likes")))

# mute hierarchy
r = G1.post(f"/live/{LID}/mute", json={"user_id": G2ID, "mute": 1})
check("member cannot mute (403)", r.status_code == 403, str(r.status_code))
r = A.post(f"/live/{LID}/mute", json={"user_id": HID, "mute": 1})
check("admin cannot mute host (403)", r.status_code == 403, str(r.status_code))
r = A.post(f"/live/{LID}/mute", json={"user_id": G2ID, "mute": 1})
check("admin mutes G2", r.json().get("ok") and r.json().get("muted") == 1, r.text[:200])
st = H.get(f"/live/{LID}/state").json()
g2p = [p for p in st["panel"] if p["user_id"] == G2ID][0]
check("state panel shows G2 muted", g2p["muted"] is True, str(g2p))
r = G2.post(f"/live/{LID}/mute", json={"user_id": G2ID, "mute": 0})
check("member self-unmute refused (403)", r.status_code == 403, str(r.status_code))
r = H.post(f"/live/{LID}/mute", json={"user_id": WID, "mute": 0})
check("host unmutes W (member) ok", r.json().get("ok"), r.text[:200])
r = H.post(f"/live/{LID}/mute", json={"user_id": RID, "mute": 1})
check("mute non-member -> 400", r.status_code == 400, str(r.status_code))

# move / reorder
r = H.post(f"/live/{LID}/move", json={"user_id": G2ID, "dir": -1})
order = r.json().get("panel_order")
check("move swaps G2 earlier", order == [HID, G2ID, G1ID, WID], str(order))
st = H.get(f"/live/{LID}/state").json()
check("move swap persists in state panel order",
      [p["user_id"] for p in st["panel"]] == [HID, G2ID, G1ID, WID],
      str([p["user_id"] for p in st.get("panel", [])]))
r = H.post(f"/live/{LID}/move", json={"user_id": HID, "dir": -1})
check("move out-of-range no-op ok", r.json().get("ok") and r.json()["panel_order"][0] == HID, r.text[:200])

# comment delete: author / host / admin
r = G1.post(f"/live/{LID}/comment", json={"body": "hello panel"})
cid = r.json()["comment"]["id"]
check("comment can_delete for author", r.json()["comment"]["can_delete"] is True)
r = G2.post(f"/live/{LID}/comment/{cid}/delete")
check("random member cannot delete other's comment (403)", r.status_code == 403, str(r.status_code))
r = A.post(f"/live/{LID}/comment/{cid}/delete")
check("admin deletes comment", r.json().get("ok"), r.text[:200])

# admin kicks a member (panel scope): seat lost, room kept
r = A.post(f"/live/{LID}/kick", json={"user_id": WID, "scope": "panel"})
check("admin panel-kicks member W", r.json().get("ok"), r.text[:200])
stw = W.get(f"/live/{LID}/state")
check("panel-kicked W can still watch", stw.status_code == 200 and stw.json()["my_panel"]["am_member"] is False, str(stw.status_code))
r = A.post(f"/live/{LID}/kick", json={"user_id": HID, "scope": "panel"})
check("admin cannot kick host (403)", r.status_code == 403, str(r.status_code))

# room kick by host: ban lands everywhere
r = H.post(f"/live/{LID}/kick", json={"user_id": G1ID, "scope": "room"})
check("host room-kicks G1", r.json().get("ok"), r.text[:200])
check("G1 state refused 403", G1.get(f"/live/{LID}/state").status_code == 403)
check("G1 heartbeat refused 403", G1.post(f"/live/{LID}/heartbeat").status_code == 403)
check("G1 signal refused 403", G1.post(f"/live/{LID}/signal", json={"to_id": HID, "kind": "hello", "payload": "{}"}).status_code == 403)
check("G1 signals poll refused 403", G1.get(f"/live/{LID}/signals").status_code == 403)
check("G1 like refused 403", G1.post(f"/live/{LID}/like").status_code == 403)
check("G1 comment refused 403", G1.post(f"/live/{LID}/comment", json={"body": "still here?"}).status_code == 403)
check("G1 join-request refused 403", G1.post(f"/live/{LID}/join-request").status_code == 403)
r = H.post(f"/live/{LID}/invite", json={"user_id": G1ID})
check("invite a banned user -> refused (400)", r.status_code == 400 and not r.json().get("ok"), r.text[:200])
check("banned excluded from live_now_for", all(x["id"] != LID for x in here.live_now_for(G1ID)))
check("banned user's /home HTML excludes the live",
      f"/live/{LID}" not in G1.get("/home").text)
check("G1 page renders (ended/unavailable face, 200)", G1.get(f"/live/{LID}").status_code == 200)
# admin appointment survives panel kick of W? W was not admin. Check A still admin:
check("A still admin", here._live_is_admin(here.db(), LID, AID) is True)

# panel cap: members now = {G2}. Fill to host + 8 guests (panel size 9)
# via direct member rows for 7 filler users.
conn = sqlite3.connect(os.environ["HERE_DB"])
for i in range(7):
    conn.execute("INSERT INTO users(name,email,pw_hash,created_at) VALUES(?,?,?,?)",
                 (f"Fill {i}", f"fill{i}@x.io", "x", "2026-01-01T00:00:00+00:00"))
    fid = conn.execute("SELECT id FROM users WHERE email=?", (f"fill{i}@x.io",)).fetchone()[0]
    conn.execute("INSERT INTO live_members(live_id,user_id,muted,joined_at) VALUES(?,?,0,?)",
                 (LID, fid, "2026-01-01T00:00:00+00:00"))
conn.commit(); conn.close()
check("panel size is 9 (host + 8 guests)", here._live_panel_size(here.db(), LID) == 9)
R.post(f"/live/{LID}/heartbeat")
r = R.post(f"/live/{LID}/join-request"); reqR = r.json()["request"]["id"]
r = H.post(f"/live/{LID}/join-decide", json={"request_id": reqR, "approve": True})
check("approve at cap -> 409 Panel is full", r.status_code == 409 and r.json().get("error") == "Panel is full (8 guests max).", r.text[:200])
r = IV.post(f"/live/{LID}/invite-decide", json={"request_id": inv_iv, "accept": True})
check("invite-accept at cap -> 409 Panel is full", r.status_code == 409 and r.json().get("error") == "Panel is full (8 guests max).", r.text[:200])
check("invite-accept at cap took no seat", here._live_panel_size(here.db(), LID) == 9)

# remove-admin + end-of-live settling
r = H.post(f"/live/{LID}/remove-admin", json={"user_id": AID})
check("host removes admin", r.json().get("ok"), r.text[:200])
check("A no longer admin", here._live_is_admin(here.db(), LID, AID) is False)
r = H.post(f"/live/{LID}/end")
check("host ends live", r.json().get("ok"))
rows = dbq("SELECT COUNT(*) c FROM live_members WHERE live_id=? AND left_at IS NULL", (LID,))
check("ending settles members (all left)", rows[0]["c"] == 0, str(rows[0]["c"]))

# legacy single-guest flow on a fresh live: request/approve/guest-leave
r = H.post("/live/start", json={"title": "legacy"})
L2 = r.json()["id"]
G1.post(f"/live/{L2}/heartbeat")
r = G1.post(f"/live/{L2}/join-request"); rq = r.json()["request"]["id"]
H.post(f"/live/{L2}/join-decide", json={"request_id": rq, "approve": True})
st = G1.get(f"/live/{L2}/state").json()
check("legacy flow: guest on panel", st["my_panel"]["am_member"] is True and st["my_role"] == "guest")
r = G1.post(f"/live/{L2}/guest-leave", json={})
check("legacy guest-leave ok", r.json().get("ok"), r.text[:200])
st = G1.get(f"/live/{L2}/state").json()
check("guest-leave frees the seat", st["my_panel"]["am_member"] is False and len(st["panel"]) == 1, str(st.get("panel")))
r = H.post(f"/live/{L2}/guest-leave", json={})
check("host bare guest-leave is no-op ok", r.json().get("ok"), r.text[:200])

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails); sys.exit(1)
