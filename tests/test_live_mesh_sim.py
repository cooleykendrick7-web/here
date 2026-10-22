"""Signaling simulation for the Go Live panel mesh.

Run:  ./venv/bin/python tests/test_live_mesh_sim.py

Builds a live with host + 2 members (panel_order [H, A, B]), one
watcher W and one admin Ad through the REAL HTTP endpoints
(TestClient, one session per user, heartbeats registering presence),
then drives the documented offer rule purely through
POST /live/{id}/signal and GET /live/{id}/signals:

  * a watcher hellos each panelist; each panelist offers to the
    watcher (panelists always offer to watchers);
  * panelist pairs: whoever sits EARLIER in panel_order offers
    (H->A, H->B, A->B); the later panelist hellos instead;
  * answers route back along the opposite direction;
  * ICE signals pass through untouched;
  * kicking B off the panel produces teardown 'bye' signals between
    B and every other room participant, in both directions.

The signals poll is cursor-based (?since=<last id seen>, ordered by
id): each scripted client keeps its own cursor and advances it to
the newest id it received, exactly like the browser client does.
This tests the signaling layer + kick teardown, not real RTC.
"""
import os, sys, tempfile, sqlite3, json

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-mesh-")
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


def signup(name, email):
    c = TestClient(here.app, follow_redirects=False)
    r = c.post("/signup", data={"name": name, "email": email,
                                "password": "pw123456"})
    assert r.status_code == 303, (name, r.status_code, r.text[:200])
    uid = dbq("SELECT id FROM users WHERE email=?", (email,))[0]["id"]
    return c, uid


H, HID = signup("Mesh Host", "mh@x.io")
A, AID = signup("Mesh A", "ma@x.io")
B, BID = signup("Mesh B", "mb@x.io")
W, WID = signup("Mesh Watcher", "mw@x.io")
AD, ADID = signup("Mesh Admin", "mad@x.io")
CLIENTS = {HID: H, AID: A, BID: B, WID: W, ADID: AD}

r = H.post("/live/start", json={"title": "Mesh night"})
assert r.status_code == 200 and r.json().get("ok"), r.text[:200]
LID = r.json()["id"]

# Heartbeats register watcher presence for everyone but the host.
for c in (A, B, W, AD):
    rr = c.post(f"/live/{LID}/heartbeat")
    assert rr.status_code == 200, rr.text[:200]

# A and B join the panel through the real request/decide flow.
for c in (A, B):
    rr = c.post(f"/live/{LID}/join-request")
    assert rr.json().get("ok"), rr.text[:200]
    rid = rr.json()["request"]["id"]
    rr = H.post(f"/live/{LID}/join-decide",
                json={"request_id": rid, "approve": True})
    assert rr.json().get("ok"), rr.text[:200]

st = H.get(f"/live/{LID}/state").json()
check("panel_order is [H, A, B]",
      [p["user_id"] for p in st["panel"]] == [HID, AID, BID],
      str([p["user_id"] for p in st.get("panel", [])]))

r = H.post(f"/live/{LID}/appoint-admin", json={"user_id": ADID})
check("host appoints Ad as admin", r.json().get("ok"), r.text[:200])

# ---------------- page smoke (step 5 of the verification brief) ----
for label, c in (("host", H), ("member", A), ("watcher", W),
                 ("admin", AD)):
    page = c.get(f"/live/{LID}")
    check(f"live page renders 200 for {label}", page.status_code == 200,
          str(page.status_code))
    check(f"live page for {label} has panel grid + like button hooks",
          'id="live-grid"' in page.text and 'id="ctl-like"' in page.text)

# ---------------- scripted signaling clients ----------------------
cursors = {uid: 0 for uid in CLIENTS}
OFFER_PAIRS = set()          # every (from_id, to_id) offer observed


def send(uid, to_id, kind, payload=None):
    c = CLIENTS[uid]
    r = c.post(f"/live/{LID}/signal",
               json={"to_id": to_id, "kind": kind,
                     "payload": payload if payload is not None else {}})
    check(f"signal {kind} {uid}->{to_id} accepted",
          r.status_code == 200 and r.json().get("ok"), r.text[:160])
    return r


def poll(uid):
    """One signals poll for uid; advances uid's cursor like the
    browser does and returns [(from_id, kind, payload), ...]."""
    c = CLIENTS[uid]
    r = c.get(f"/live/{LID}/signals?since={cursors[uid]}")
    assert r.status_code == 200, (uid, r.status_code, r.text[:160])
    sigs = r.json()["signals"]
    if sigs:
        cursors[uid] = max(s["id"] for s in sigs)
    out = []
    for s in sigs:
        if s["kind"] == "offer":
            OFFER_PAIRS.add((s["from_id"], uid))
        out.append((s["from_id"], s["kind"], s["payload"]))
    return out


def pairs(sigs):
    return [(f, k) for f, k, _ in sigs]


# Phase 1: the watcher hellos every panelist.
for pid in (HID, AID, BID):
    send(WID, pid, "hello", {"hello": "watcher"})
for pid in (HID, AID, BID):
    got = poll(pid)
    check(f"panelist {pid} receives exactly W's hello",
          pairs(got) == [(WID, "hello")], str(got))
check("watcher receives nothing in phase 1", poll(WID) == [])

# Phase 2: every panelist offers to the watcher.
for pid in (HID, AID, BID):
    send(pid, WID, "offer", {"type": "offer", "sdp": f"sdp-from-{pid}"})
got = poll(WID)
check("watcher receives exactly one offer per panelist, in order",
      pairs(got) == [(HID, "offer"), (AID, "offer"), (BID, "offer")],
      str(got))
check("offer payloads pass through untouched",
      [p.get("sdp") for _, _, p in got]
      == [f"sdp-from-{pid}" for pid in (HID, AID, BID)], str(got))

# Phase 3: panelist pairs — the earlier panelist offers.
send(HID, AID, "offer", {"type": "offer", "sdp": "h-to-a"})
send(HID, BID, "offer", {"type": "offer", "sdp": "h-to-b"})
send(AID, BID, "offer", {"type": "offer", "sdp": "a-to-b"})
check("A receives exactly H's offer", pairs(poll(AID)) == [(HID, "offer")])
check("B receives exactly H's and A's offers",
      pairs(poll(BID)) == [(HID, "offer"), (AID, "offer")])
check("H (earliest) receives no panelist offers", poll(HID) == [])

# Phase 4: the later panelist hellos the earlier ones instead.
send(BID, HID, "hello", {"hello": "later-panelist"})
send(BID, AID, "hello", {"hello": "later-panelist"})
check("H receives exactly B's hello", pairs(poll(HID)) == [(BID, "hello")])
check("A receives exactly B's hello", pairs(poll(AID)) == [(BID, "hello")])
check("B receives nothing in phase 4", poll(BID) == [])
check("watcher receives nothing in phase 4", poll(WID) == [])

# No pair may ever have offers from BOTH directions.
expected_offers = {(HID, WID), (AID, WID), (BID, WID),
                   (HID, AID), (HID, BID), (AID, BID)}
check("observed offer pairs are exactly the documented rule's pairs",
      OFFER_PAIRS == expected_offers, str(OFFER_PAIRS))
check("no offer pair runs in both directions",
      all((b, a) not in OFFER_PAIRS for a, b in OFFER_PAIRS))

# Phase 5: answers route back opposite the offers.
send(WID, HID, "answer", {"type": "answer", "sdp": "w-to-h"})
send(AID, HID, "answer", {"type": "answer", "sdp": "a-to-h"})
send(BID, HID, "answer", {"type": "answer", "sdp": "b-to-h"})
send(WID, AID, "answer", {"type": "answer", "sdp": "w-to-a"})
send(BID, AID, "answer", {"type": "answer", "sdp": "b-to-a"})
send(WID, BID, "answer", {"type": "answer", "sdp": "w-to-b"})
check("H receives answers from W, A and B",
      pairs(poll(HID)) == [(WID, "answer"), (AID, "answer"),
                           (BID, "answer")])
check("A receives answers from W and B",
      pairs(poll(AID)) == [(WID, "answer"), (BID, "answer")])
check("B receives the answer from W",
      pairs(poll(BID)) == [(WID, "answer")])
check("W receives no answers (watchers only answer)",
      poll(WID) == [])

# Phase 6: ICE passes through, payload intact.
ice_h = {"candidate": "candidate:host 1 udp 1.1.1.1 9 typ host",
         "sdpMid": "0", "sdpMLineIndex": 0}
ice_w = {"candidate": "candidate:watch 1 udp 2.2.2.2 9 typ host",
         "sdpMid": "0", "sdpMLineIndex": 0}
ice_a = {"candidate": "candidate:a 1 udp 3.3.3.3 9 typ host",
         "sdpMid": "1", "sdpMLineIndex": 1}
send(HID, WID, "ice", ice_h)
send(WID, HID, "ice", ice_w)
send(AID, BID, "ice", ice_a)
got_w, got_h, got_b = poll(WID), poll(HID), poll(BID)
check("W receives H's ICE with payload intact",
      pairs(got_w) == [(HID, "ice")] and got_w[0][2] == ice_h, str(got_w))
check("H receives W's ICE with payload intact",
      pairs(got_h) == [(WID, "ice")] and got_h[0][2] == ice_w, str(got_h))
check("B receives A's ICE with payload intact",
      pairs(got_b) == [(AID, "ice")] and got_b[0][2] == ice_a, str(got_b))
check("A receives nothing in the ICE phase", poll(AID) == [])

# Phase 7: panel-kicking B tears down B's peers in both directions.
r = H.post(f"/live/{LID}/kick", json={"user_id": BID, "scope": "panel"})
check("host panel-kicks B", r.json().get("ok"), r.text[:200])
check("H receives exactly B's bye", pairs(poll(HID)) == [(BID, "bye")])
check("A receives exactly B's bye", pairs(poll(AID)) == [(BID, "bye")])
check("watcher W receives exactly B's bye",
      pairs(poll(WID)) == [(BID, "bye")])
check("admin Ad receives exactly B's bye",
      pairs(poll(ADID)) == [(BID, "bye")])
got_b = poll(BID)
check("B receives a bye from every other participant",
      sorted(pairs(got_b)) == sorted([(HID, "bye"), (AID, "bye"),
                                       (WID, "bye"), (ADID, "bye")]),
      str(got_b))
st = B.get(f"/live/{LID}/state").json()
check("kicked B is off the panel, room intact",
      st["my_panel"]["am_member"] is False
      and [p["user_id"] for p in st["panel"]] == [HID, AID],
      str(st.get("panel")))

r = H.post(f"/live/{LID}/end")
check("host ends the mesh live", r.json().get("ok"), r.text[:200])

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL GREEN")
