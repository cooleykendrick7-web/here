"""Live share tests (Oct 29): a shared live link shows an
animated OG card to feeds, lands guests on a watch-gate (not
a bare login wall), and login/signup ?next= drops the member
inside the live they came for.

Run:  ./venv/bin/python tests/test_live_share.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-liveshare-")
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


Hc, HOST = signup("Hank Host", "hank@liveshare.test")
r = Hc.post("/live/start", json={"title": "Probe Night Live"})
lid = r.json().get("live_id") or r.json().get("id")

G = TestClient(here.app, follow_redirects=False)
r = G.get(f"/live/{lid}")
check("guest clicking a live link lands on the watch-gate",
      r.status_code == 200 and "Create your free profile" in r.text
      and "Probe Night Live" in r.text
      and "Hank Host" in r.text)
check("gate carries the live's own og card tags",
      f"/live/{lid}/og.gif" in r.text
      and 'property="og:image"' in r.text)
check("gate CTAs carry the return path",
      f"/signup?next=/live/{lid}" in r.text
      and f"/login?next=/live/{lid}" in r.text)

r = G.get(f"/live/{lid}/og.png")
check("og.png is a real PNG",
      r.status_code == 200
      and r.headers["content-type"].startswith("image/png")
      and r.content[:4] == b"\x89PNG" and len(r.content) > 5000,
      str(r.status_code) + " " + str(len(r.content)))
r = G.get(f"/live/{lid}/og.gif")
check("og.gif is a real animated GIF",
      r.status_code == 200
      and r.headers["content-type"].startswith("image/gif")
      and r.content[:3] == b"GIF" and len(r.content) > 5000,
      str(r.status_code) + " " + str(len(r.content)))
r = G.get("/live/999999/og.png")
check("a dead live's card falls back, never 500s",
      r.status_code in (200, 303, 307))

# next: login lands inside the live
Lc = TestClient(here.app, follow_redirects=False)
r = Lc.post("/login", data={"email": "hank@liveshare.test",
                            "password": "pw123456",
                            "next": f"/live/{lid}"})
check("login with next lands on the live",
      r.headers.get("location") == f"/live/{lid}",
      r.headers.get("location", ""))
r = Lc.post("/login", data={"email": "hank@liveshare.test",
                            "password": "pw123456",
                            "next": "https://evil.example/x"})
check("an off-site next is refused (home instead)",
      r.headers.get("location") == "/home",
      r.headers.get("location", ""))
# next: signup lands inside the live (skips welcome)
Sc = TestClient(here.app, follow_redirects=False)
r = Sc.post("/signup", data={"name": "New Nate",
                             "email": "nate@liveshare.test",
                             "password": "pw123456",
                             "next": f"/live/{lid}"})
check("signup with next lands on the live",
      r.headers.get("location") == f"/live/{lid}",
      r.headers.get("location", ""))
# logged-in member still gets the real live page
r = Hc.get(f"/live/{lid}")
check("members still get the live itself, not the gate",
      r.status_code == 200
      and "Create your free profile" not in r.text)

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILS:", fails)
    sys.exit(1)
