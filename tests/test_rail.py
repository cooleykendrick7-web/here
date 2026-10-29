"""Rail round tests (Oct 28 evening): host Exit Panel drops every
guest to watcher and closes the panel in one tap; the live page
carries the new rail / PiP / exit pieces.

Run:  ./venv/bin/python tests/test_rail.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-rail-")
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


Hc, H = signup("Hank Host", "hank@rail.test")
Gc, G = signup("Gail Guest", "gail@rail.test")
G2c, G2 = signup("Gus Guest", "gus@rail.test")

r = Hc.post("/live/start", json={"title": "Rail room"})
LID = r.json()["id"]

for c in (Gc, G2c):
    c.post(f"/live/{LID}/join-request")
st = Hc.get(f"/live/{LID}/state").json()
for req in st["pending_requests"]:
    Hc.post(f"/live/{LID}/join-decide",
            json={"request_id": req["id"], "approve": True})
st = Gc.get(f"/live/{LID}/state").json()
check("setup: guest seated", st["my_role"] == "guest")
st = Hc.get(f"/live/{LID}/state").json()
check("setup: panel holds host + 2 guests",
      len(st["panel"]) == 3, str(len(st["panel"])))

r = Gc.post(f"/live/{LID}/panel-exit", json={})
check("a guest cannot exit the host's panel",
      r.status_code == 403, str(r.status_code))

r = Hc.post(f"/live/{LID}/panel-exit", json={})
j = r.json()
check("host Exit Panel drops both guests in one tap",
      r.status_code == 200 and j.get("ok") and j.get("dropped") == 2,
      str(j))
st = Gc.get(f"/live/{LID}/state").json()
check("dropped guest is a watcher again (still in the room)",
      st["my_role"] == "watcher")
st = Hc.get(f"/live/{LID}/state").json()
check("host is solo and the panel is closed",
      len(st["panel"]) == 1 and st["panel_open"] is False)
row = dbq("SELECT panel_open, spotlight_id FROM live_sessions "
          "WHERE id=?", (LID,))[0]
check("panel_open=0 persisted", row["panel_open"] == 0)

r = Hc.get(f"/live/{LID}")
check("live page carries the rail + chips + exit + PiP pieces",
      r.status_code == 200 and 'id="ctl-rail-toggle"' in r.text
      and 'id="live-chips"' in r.text
      and 'id="ctl-panel-exit"' in r.text
      and 'id="ctl-pip"' in r.text
      and "togglePip" in r.text
      and "Game + my face" in r.text
      and "live-controls.collapsed" in r.text)

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILS:", fails)
    sys.exit(1)
