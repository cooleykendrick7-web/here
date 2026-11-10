"""2X STUDIO GO on HERE (Ken's order 2026-11-09).

The gated hub at /2x plus the desktop app's link API. The gate
reads the REAL flags — users.creator_mode (the /settings/creator
switch) and users.artist_verified (set by the owner approving an
artist_applications row or stamping the member on the Label
desk). No parallel flags.

Covers:
- strangers are sent to /login; the hub never renders for them
- the gate matrix on the page: neither / creator-only /
  artist-only all get the LOCKED page with working step links and
  NO link code (and no twox_links row is minted for them);
  creator+artist gets the hub with a stored, shown link code
- steps reflect progress (creator done, artist pending/rejected)
- owner session bypasses the gate on the page (owner context),
  like everywhere else on HERE
- POST /2x/code/regenerate rotates the code (entitled only);
  the old code dies on the API
- POST /api/2x/link-status + GET /api/2x/entitlement: gate fields
  + the spec's reason vocabulary, and NOTHING else about the
  member (no name, no email); invalid code 404, missing code 400
- POST /api/2x/link stamps linked_at/app info and answers in the
  integration doc's shape
"""
import os, re, sys, sqlite3, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
TMP = tempfile.mkdtemp(prefix="here_twox_")
os.environ["HERE_DB"] = os.path.join(TMP, "t.db")
os.environ["HERE_UPLOADS"] = os.path.join(TMP, "up")
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake"
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

def signup(name, email):
    cl = TestClient(here.app, follow_redirects=False)
    r = cl.post("/signup", data={"name": name, "email": email,
                                 "password": "testpass123"})
    assert r.status_code == 303, (name, r.status_code)
    return cl, dbq("SELECT MAX(id) m FROM users")[0]["m"]

def make_creator(cl):
    r = cl.post("/settings/creator", data={"creator_mode": "1"})
    assert r.status_code == 303, r.status_code

def stamp_artist(uid):
    # Exactly what the Label desk does (owner approve / stamp):
    dbx("UPDATE users SET artist_verified=1 WHERE id=?", (uid,))

CODE_RE = re.compile(r"2X-[A-Z2-9]{5}-[A-Z2-9]{5}")
LOCKED = "The door is locked"
HUB = "Your 2X link code"

api = TestClient(here.app, follow_redirects=False)

print("== strangers ==")
r = TestClient(here.app, follow_redirects=False).get("/2x")
check("logged-out /2x redirects to login",
      r.status_code == 303 and "/login" in
      r.headers.get("location", ""), str(r.status_code))
r = api.post("/api/2x/link-status", json={"code": "2X-ZZZZZ-ZZZZZ"})
check("unknown code -> 404 linked:false",
      r.status_code == 404 and r.json()["linked"] is False
      and r.json()["reasons"] == ["invalid_code"], r.text[:120])
r = api.post("/api/2x/link-status", json={})
check("missing code -> 400", r.status_code == 400, str(r.status_code))

print("== neither (locked, no code minted) ==")
cl_n, uid_n = signup("Twox Neither", "twox_neither@test.dev")
r = cl_n.get("/2x")
check("neither sees locked page", r.status_code == 200
      and LOCKED in r.text and HUB not in r.text)
check("locked page links Creator Mode settings",
      "/settings/profile#creator-mode" in r.text)
check("locked page links the artist door", "/artist/start" in r.text)
check("no twox_links row for locked member",
      dbq("SELECT * FROM twox_links WHERE user_id=?",
          (uid_n,)) == [])
r = cl_n.post("/2x/code/regenerate")
check("locked member cannot regenerate (no row appears)",
      dbq("SELECT * FROM twox_links WHERE user_id=?",
          (uid_n,)) == [])

print("== creator only ==")
cl_c, uid_c = signup("Twox Creator", "twox_creator@test.dev")
make_creator(cl_c)
r = cl_c.get("/2x")
check("creator-only still locked", LOCKED in r.text
      and HUB not in r.text)
check("creator step reads done", "On — done." in r.text)
check("artist step still open (apply links present)",
      "/artist/start" in r.text)
check("no code row for creator-only",
      dbq("SELECT * FROM twox_links WHERE user_id=?",
          (uid_c,)) == [])

print("== artist only ==")
cl_a, uid_a = signup("Twox Artist", "twox_artist@test.dev")
stamp_artist(uid_a)
r = cl_a.get("/2x")
check("artist-only still locked", LOCKED in r.text
      and HUB not in r.text)
check("artist step reads done", "Approved — done." in r.text)
check("creator step still open",
      "/settings/profile#creator-mode" in r.text)

print("== artist pending / rejected copy ==")
cl_p, uid_p = signup("Twox Pending", "twox_pending@test.dev")
dbx("INSERT INTO artist_applications(user_id, note, status, "
    "created_at) VALUES(?,?, 'new', '')", (uid_p, ""))
r = cl_p.get("/2x")
check("pending artist reads pending", "waiting on the owner"
      in r.text)
dbx("UPDATE artist_applications SET status='passed' "
    "WHERE user_id=?", (uid_p,))
r = cl_p.get("/2x")
check("passed artist reads not-approved + redo door",
      "wasn't approved" in r.text and "/artist/start" in r.text)

print("== entitled: hub + code + regenerate ==")
make_creator(cl_a)
r = cl_a.get("/2x")
m = CODE_RE.search(r.text)
check("entitled sees hub with a link code",
      r.status_code == 200 and HUB in r.text and bool(m))
code1 = m.group(0)
rows = dbq("SELECT * FROM twox_links WHERE user_id=?", (uid_a,))
check("code stored on twox_links, matches page",
      len(rows) == 1 and rows[0]["code"] == code1)
r = cl_a.get("/2x")
check("code is stable across visits",
      CODE_RE.search(r.text).group(0) == code1)
r = cl_a.post("/2x/code/regenerate")
check("regenerate redirects back to /2x", r.status_code == 303)
r = cl_a.get("/2x")
code2 = CODE_RE.search(r.text).group(0)
check("regenerated code differs", code2 != code1)
r = api.post("/api/2x/link-status", json={"code": code1})
check("old code is dead after regenerate",
      r.status_code == 404, str(r.status_code))

print("== api: link-status / entitlement ==")
r = api.post("/api/2x/link-status", json={"code": code2})
j = r.json()
check("entitled status payload",
      r.status_code == 200 and j["linked"] is True
      and j["entitled"] is True and j["creator_mode"] is True
      and j["artist_approved"] is True and j["reasons"] == [],
      r.text[:200])
check("status payload leaks no member data",
      "twox_artist@test.dev" not in r.text
      and "Twox Artist" not in r.text)
check("last_checked_at stamped",
      bool(dbq("SELECT last_checked_at FROM twox_links WHERE "
               "user_id=?", (uid_a,))[0]["last_checked_at"]))
r = api.get("/api/2x/entitlement", params={"code": code2})
check("GET entitlement twin agrees",
      r.status_code == 200 and r.json()["entitled"] is True)

print("== api: reasons for a non-entitled code holder ==")
dbx("INSERT INTO twox_links(user_id, code, created_at, "
    "updated_at) VALUES(?, '2X-AAAAA-AAAAA', '', '')", (uid_c,))
r = api.post("/api/2x/link-status",
             json={"code": "2X-AAAAA-AAAAA"})
j = r.json()
check("creator-only: artist_not_applied reason",
      j["linked"] is True and j["entitled"] is False
      and j["creator_mode"] is True
      and j["artist_approved"] is False
      and j["reasons"] == ["artist_not_applied"], r.text[:200])
dbx("INSERT INTO artist_applications(user_id, note, status, "
    "created_at) VALUES(?,?, 'new', '')", (uid_c, ""))
r = api.post("/api/2x/link-status",
             json={"code": "2X-AAAAA-AAAAA"})
check("pending application -> artist_pending",
      r.json()["reasons"] == ["artist_pending"], r.text[:200])
dbx("UPDATE artist_applications SET status='passed' "
    "WHERE user_id=?", (uid_c,))
r = api.post("/api/2x/link-status",
             json={"code": "2X-AAAAA-AAAAA"})
check("passed application -> artist_rejected",
      r.json()["reasons"] == ["artist_rejected"], r.text[:200])
dbx("UPDATE users SET creator_mode=0 WHERE id=?", (uid_c,))
r = api.post("/api/2x/link-status",
             json={"code": "2X-AAAAA-AAAAA"})
check("creator off adds creator_mode_off first",
      r.json()["reasons"] == ["creator_mode_off",
                              "artist_rejected"], r.text[:200])

print("== api: link confirm ==")
r = api.post("/api/2x/link", json={"code": code2,
                                   "app_version": "0.3.0",
                                   "platform": "mac"})
j = r.json()
check("confirm answers doc shape",
      r.status_code == 200 and j["linked"] is True
      and j["entitled"] is True and j["username"] == "Twox Artist"
      and j["reason"] is None, r.text[:200])
row = dbq("SELECT * FROM twox_links WHERE user_id=?",
          (uid_a,))[0]
check("confirm stamps linked_at + app info",
      bool(row["linked_at"]) and row["app_version"] == "0.3.0"
      and row["platform"] == "mac")
r = api.post("/api/2x/link", json={"code": "2X-ZZZZZ-ZZZZZ"})
check("confirm with bad code -> 401",
      r.status_code == 401 and r.json()["linked"] is False)
r = cl_a.get("/2x")
check("hub shows the app as linked", "Studio app linked" in r.text)

print("== owner bypass ==")
from datetime import datetime, timezone
tok = "twox-owner-token"
dbx("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
    (tok, datetime.now(timezone.utc).isoformat()))
cl_n.cookies.set("here_owner", tok)
r = cl_n.get("/2x")
check("owner session sees the hub for a non-entitled member",
      HUB in r.text and "owner access" in r.text)

print(f"\nPASS {PASS[0]} FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)
