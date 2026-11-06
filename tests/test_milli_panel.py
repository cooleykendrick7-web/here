"""Owner Pack P7 — Talk to Milli.

Covers:
- /owner/milli page (owner-gated) renders with the honest copy:
  'Milli checks this inbox regularly and replies here'
- owner JSON endpoints: send + history round-trip; empty body
  refused; stranger (no owner session) refused on page + APIs
- machine endpoints with MILLI_PANEL_TOKEN unset: BOTH 404
- with the token set: missing/wrong header 404; right header
  reads Ken's unhandled messages; reply inserts a Milli message
  and marks Ken's pending handled; inbox drains; owner history
  shows both sides of the thread
"""
import os, sys, sqlite3, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
TMP = tempfile.mkdtemp(prefix="here_milli_")
os.environ["HERE_DB"] = os.path.join(TMP, "t.db")
os.environ["HERE_UPLOADS"] = os.path.join(TMP, "up")
os.environ.pop("MILLI_PANEL_TOKEN", None)
import app as here
from fastapi.testclient import TestClient

PASS = [0]; FAIL = [0]

def check(name, cond, extra=""):
    if cond:
        PASS[0] += 1; print("  ok -", name)
    else:
        FAIL[0] += 1; print("  FAIL -", name, extra)

def dbq(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"])
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    conn.close(); return rows

def dbx(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"])
    conn.execute(sql, args); conn.commit(); conn.close()

dbx("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
    ("milli-owner-token", here.now_iso()))
own = TestClient(here.app, follow_redirects=False)
own.cookies.set("here_owner", "milli-owner-token")
stranger = TestClient(here.app, follow_redirects=False)

print("== 1. Owner page + honest copy ==")
r = own.get("/owner/milli")
check("owner page renders", r.status_code == 200, str(r.status_code))
check("page carries the honest line",
      "Milli checks this inbox regularly and replies here" in r.text
      and "build requests made here get picked up by Milli" in r.text)
check("page polls the history endpoint",
      "/api/owner/milli/history" in r.text and "10000" in r.text)

print("== 2. Stranger refused everywhere ==")
for path in ("/owner/milli", "/api/owner/milli/history"):
    r = stranger.get(path)
    check(f"stranger GET {path} refused",
          r.status_code in (303, 401, 403), str(r.status_code))
r = stranger.post("/api/owner/milli/send", json={"body": "hi"})
check("stranger send refused", r.status_code in (303, 401, 403),
      str(r.status_code))
check("stranger send wrote nothing", dbq("SELECT * FROM milli_msgs")
      == [])

print("== 3. Owner send + history round-trip ==")
r = own.post("/api/owner/milli/send",
             json={"body": "Milli — please add a dark mode toggle."})
check("owner send ok", r.status_code == 200 and r.json()["ok"],
      str(r.status_code))
r = own.post("/api/owner/milli/send", json={"body": "   "})
check("empty message refused 400", r.status_code == 400,
      str(r.status_code))
r = own.get("/api/owner/milli/history")
msgs = r.json()["msgs"]
check("history has the one message",
      len(msgs) == 1 and msgs[0]["from"] == "ken"
      and "dark mode" in msgs[0]["body"], str(msgs)[:160])
check("message starts unhandled", msgs[0]["handled"] is False)
r = own.get("/owner/milli")
check("page renders Ken's message in the thread",
      "dark mode toggle" in r.text)

print("== 4. Machine endpoints: token unset -> 404 ==")
r = stranger.get("/api/milli/inbox")
check("inbox 404 with env unset", r.status_code == 404,
      str(r.status_code))
r = stranger.post("/api/milli/reply", json={"body": "on it"})
check("reply 404 with env unset", r.status_code == 404,
      str(r.status_code))

print("== 5. Machine endpoints: token set ==")
os.environ["MILLI_PANEL_TOKEN"] = "test-milli-token"
try:
    r = stranger.get("/api/milli/inbox")
    check("inbox 404 without the header", r.status_code == 404)
    r = stranger.get("/api/milli/inbox",
                     headers={"X-Milli-Token": "wrong"})
    check("inbox 404 with the wrong token", r.status_code == 404)
    r = stranger.get("/api/milli/inbox",
                     headers={"X-Milli-Token": "test-milli-token"})
    check("inbox works with the token",
          r.status_code == 200 and r.json()["ok"],
          str(r.status_code))
    got = r.json()["msgs"]
    check("inbox returns Ken's unhandled message",
          len(got) == 1 and "dark mode" in got[0]["body"],
          str(got)[:160])
    r = stranger.post("/api/milli/reply",
                      json={"body": "Seen — building it tonight."},
                      headers={"X-Milli-Token": "test-milli-token"})
    check("reply ok + marked 1 handled",
          r.status_code == 200 and r.json()["ok"]
          and r.json()["marked_handled"] == 1,
          str(r.status_code) + " " + r.text[:120])
    r = stranger.get("/api/milli/inbox",
                     headers={"X-Milli-Token": "test-milli-token"})
    check("inbox drained after the reply",
          r.json()["msgs"] == [])
    r = stranger.post("/api/milli/reply", json={"body": ""},
                      headers={"X-Milli-Token": "test-milli-token"})
    check("empty reply refused 400", r.status_code == 400)
finally:
    os.environ.pop("MILLI_PANEL_TOKEN", None)

print("== 6. Thread state after the exchange ==")
r = own.get("/api/owner/milli/history")
msgs = r.json()["msgs"]
check("history shows both sides in order",
      len(msgs) == 2 and msgs[0]["from"] == "ken"
      and msgs[1]["from"] == "milli"
      and "building it tonight" in msgs[1]["body"],
      str(msgs)[:200])
check("Ken's message now handled", msgs[0]["handled"] is True)
r = own.get("/owner/milli")
check("page shows Milli's reply", "building it tonight" in r.text)

print(f"\n{PASS[0]} passed, {FAIL[0]} failed")
sys.exit(1 if FAIL[0] else 0)
