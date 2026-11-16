"""CEO — Creative Engineering Operations on HERE (Phase A).

The /ceo link room plus the desktop token API. Modelled on
test_twox.py's harness. The gate is the 2X gate itself
(creator_mode + artist_verified); the difference from 2X is the
credential: the hand-typed code is only the bootstrap, exchanged
once for a scoped desktop token (SHA-256 hash at rest, scopes
exactly post:create + media:upload + status:read, 180-day
expiry), and posts land through create_post_from_form itself.

Covers:
- strangers -> /login; gate matrix gets the locked room and NO
  code row is minted
- entitled room shows a stored CEO-XXXXX-XXXXX code + token list
- link exchange: bad code 401, missing 400, non-entitled 403 with
  reasons; entitled 200 returns the token ONCE, DB holds only its
  hash, ceo_links is stamped
- entitlement by token; scope limits (a read-only-scope token
  cannot post; the token is not a credential anywhere else)
- text post lands with the right author/kind/privacy; the gate
  re-check pauses posting when a door closes
- photo post through the multipart upload fallback (disk mode):
  minted key shape, file stored, post media set; ANOTHER member's
  key is refused; a missing key is refused
- presign: disk mode answers direct:false for image AND video;
  the design's surface names (posts/stories) are accepted
- R2 mode (fake S3 wire): presign mints a direct key for video,
  the file PUTs, and a post with that key lands with media == key
- revocation: unlink, per-token room revoke, revoke-all, code
  regeneration (kills tokens + old code), password change
- expired token -> 401 token_expired
"""
import hashlib, os, re, sys, sqlite3, tempfile
from datetime import datetime, timedelta, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
TMP = tempfile.mkdtemp(prefix="here_ceo_")
os.environ["HERE_DB"] = os.path.join(TMP, "t.db")
os.environ["HERE_UPLOADS"] = os.path.join(TMP, "up")
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake"
import app as here
import storage
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
                                 "password": "testpass123",
                                 "birthdate": "1990-05-01"})
    assert r.status_code == 303, (name, r.status_code)
    return cl, dbq("SELECT MAX(id) m FROM users")[0]["m"]

def make_creator(cl):
    r = cl.post("/settings/creator", data={"creator_mode": "1"})
    assert r.status_code == 303, r.status_code

def stamp_artist(uid):
    dbx("UPDATE users SET artist_verified=1 WHERE id=?", (uid,))

def bearer(tok):
    return {"Authorization": "Bearer " + tok}

CODE_RE = re.compile(r"CEO-[A-Z2-9]{5}-[A-Z2-9]{5}")
KEY_RE = re.compile(r"posts/u(\d+)-[0-9a-f]{32}\.(png|jpg|jpeg|webp|gif)")
LOCKED = "The door is locked"
HUB = "Your CEO link code"
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082")

api = TestClient(here.app, follow_redirects=False)

print("== strangers + gate matrix ==")
r = TestClient(here.app, follow_redirects=False).get("/ceo")
check("logged-out /ceo redirects to login",
      r.status_code == 303 and "/login" in
      r.headers.get("location", ""), str(r.status_code))
cl_n, uid_n = signup("Ceo Neither", "ceo_neither@test.dev")
r = cl_n.get("/ceo")
check("neither sees locked room", r.status_code == 200
      and LOCKED in r.text and HUB not in r.text)
check("no ceo_links row for locked member",
      dbq("SELECT * FROM ceo_links WHERE user_id=?", (uid_n,)) == [])
cl_c, uid_c = signup("Ceo Creator", "ceo_creator@test.dev")
make_creator(cl_c)
r = cl_c.get("/ceo")
check("creator-only still locked, no code row",
      LOCKED in r.text and dbq(
          "SELECT * FROM ceo_links WHERE user_id=?", (uid_c,)) == [])
cl_a, uid_a = signup("Ceo Artist", "ceo_artist@test.dev")
stamp_artist(uid_a)
r = cl_a.get("/ceo")
check("artist-only still locked", LOCKED in r.text
      and HUB not in r.text)

print("== entitled room + code ==")
make_creator(cl_a)
r = cl_a.get("/ceo")
m = CODE_RE.search(r.text)
check("entitled sees room with a link code",
      r.status_code == 200 and HUB in r.text and bool(m))
code_a = m.group(0)
rows = dbq("SELECT * FROM ceo_links WHERE user_id=?", (uid_a,))
check("code stored on ceo_links, matches page",
      len(rows) == 1 and rows[0]["code"] == code_a)
r = cl_a.get("/ceo")
check("code is stable across visits",
      CODE_RE.search(r.text).group(0) == code_a)
check("room shows empty linked-computers state",
      "No computers are linked" in r.text)

print("== link exchange: failures ==")
r = api.post("/api/ceo/link", json={"code": "CEO-ZZZZZ-ZZZZZ"})
check("unknown code -> 401 invalid_code",
      r.status_code == 401
      and r.json()["error"] == "invalid_code", r.text[:120])
r = api.post("/api/ceo/link", json={})
check("missing code -> 400", r.status_code == 400,
      str(r.status_code))
dbx("INSERT INTO ceo_links(user_id, code, created_at, updated_at) "
    "VALUES(?, 'CEO-AAAAA-AAAAA', '', '')", (uid_c,))
r = api.post("/api/ceo/link", json={"code": "CEO-AAAAA-AAAAA"})
j = r.json()
check("non-entitled code holder -> 403 with reasons",
      r.status_code == 403 and j["entitled"] is False
      and j["reasons"] == ["artist_not_applied"], r.text[:200])
check("no token minted for the non-entitled",
      dbq("SELECT * FROM ceo_tokens WHERE user_id=?",
          (uid_c,)) == [])

print("== link exchange: success ==")
r = api.post("/api/ceo/link", json={"code": code_a,
                                    "app_version": "1.1.0",
                                    "platform": "windows"})
j = r.json()
check("exchange answers the doc shape",
      r.status_code == 200 and j["linked"] is True
      and j["entitled"] is True and j["username"] == "Ceo Artist"
      and j["reasons"] == [] and bool(j["token"]), r.text[:200])
tok_a = j["token"]
exp = datetime.fromisoformat(j["token_expires_at"])
check("expiry is ~180 days out",
      timedelta(days=179) < exp - datetime.now(timezone.utc)
      < timedelta(days=181))
trows = dbq("SELECT * FROM ceo_tokens WHERE user_id=?", (uid_a,))
check("exactly one token row", len(trows) == 1)
trow = trows[0]
check("DB holds only the SHA-256 hash",
      trow["token_hash"] == hashlib.sha256(
          tok_a.encode()).hexdigest()
      and tok_a not in str(trow))
check("scope is exactly the three grants",
      trow["scope"] == "post:create media:upload status:read")
lrow = dbq("SELECT * FROM ceo_links WHERE user_id=?", (uid_a,))[0]
check("ceo_links stamped with app info",
      bool(lrow["linked_at"]) and lrow["app_version"] == "1.1.0"
      and lrow["platform"] == "windows")
r = cl_a.get("/ceo")
check("room lists the linked computer",
      "Linked computers" in r.text and "windows" in r.text)

print("== entitlement by token ==")
r = api.get("/api/ceo/entitlement", headers=bearer(tok_a))
j = r.json()
check("entitlement answers entitled",
      r.status_code == 200 and j["linked"] is True
      and j["entitled"] is True and j["reasons"] == []
      and "checked_at" in j, r.text[:200])
check("entitlement leaks no member data",
      "ceo_artist@test.dev" not in r.text)
r = api.get("/api/ceo/entitlement")
check("no token -> 401 invalid_token",
      r.status_code == 401
      and r.json()["error"] == "invalid_token")
r = api.get("/api/ceo/entitlement", headers=bearer("nope-nope"))
check("garbage token -> 401 invalid_token",
      r.status_code == 401
      and r.json()["error"] == "invalid_token")
check("last_used_at stamped",
      bool(dbq("SELECT last_used_at FROM ceo_tokens WHERE "
               "id=?", (trow["id"],))[0]["last_used_at"]))

print("== scope limits ==")
ro_tok = "ceo-readonly-token"
dbx("INSERT INTO ceo_tokens(user_id, token_hash, scope, "
    "created_at, expires_at) VALUES(?,?,?,?,?)",
    (uid_a, hashlib.sha256(ro_tok.encode()).hexdigest(),
     "status:read", here.now_iso(),
     (datetime.now(timezone.utc)
      + timedelta(days=30)).isoformat()))
r = api.get("/api/ceo/entitlement", headers=bearer(ro_tok))
check("read-only token can read status", r.status_code == 200)
r = api.post("/api/ceo/post", headers=bearer(ro_tok),
             json={"kind": "text", "text": "should not land"})
check("read-only token cannot post -> 403 scope",
      r.status_code == 403 and r.json()["error"] == "scope",
      r.text[:120])
r = api.post("/api/ceo/upload", headers=bearer(ro_tok),
             files={"media": ("x.png", PNG_1PX, "image/png")})
check("read-only token cannot upload -> 403 scope",
      r.status_code == 403)
r = api.post("/api/uploads/presign", headers=bearer(tok_a),
             json={"surface": "post", "content_type": "video/mp4"})
check("ceo token is not a credential on cookie routes -> 401",
      r.status_code == 401, str(r.status_code))
r = api.post("/api/2x/link-status", json={"code": tok_a})
check("ceo token is not a 2x code -> 404",
      r.status_code == 404, str(r.status_code))

print("== text post ==")
r = api.post("/api/ceo/post", headers=bearer(tok_a),
             json={"kind": "text", "text": "First CEO post",
                   "privacy": "onlyme"})
j = r.json()
check("text post lands", r.status_code == 200 and j["ok"] is True
      and j["url"] == f"/post/{j['post_id']}", r.text[:200])
prow = dbq("SELECT * FROM posts WHERE id=?", (j["post_id"],))[0]
check("post row: author/kind/body/privacy",
      prow["user_id"] == uid_a and prow["kind"] == "text"
      and prow["body"] == "First CEO post"
      and prow["privacy"] == "onlyme")
r = api.post("/api/ceo/post", headers=bearer(tok_a),
             json={"kind": "photo"})
check("photo without media -> 400", r.status_code == 400)
r = api.post("/api/ceo/post", headers=bearer(tok_a),
             json={"kind": "text", "text": "  "})
check("empty text post -> 400", r.status_code == 400)
r = api.post("/api/ceo/post", headers=bearer(tok_a),
             json={"kind": "poll", "text": "nope"})
check("kind outside the API set -> 400", r.status_code == 400)

print("== presign (disk mode) ==")
r = api.post("/api/ceo/presign", headers=bearer(tok_a),
             json={"surface": "post",
                   "content_type": "image/png"})
check("image presign -> direct:false (why the fallback exists)",
      r.status_code == 200 and r.json()["direct"] is False,
      r.text[:120])
r = api.post("/api/ceo/presign", headers=bearer(tok_a),
             json={"surface": "posts",
                   "content_type": "video/mp4"})
check("design surface name 'posts' accepted, disk -> false",
      r.status_code == 200 and r.json()["direct"] is False)
r = api.post("/api/ceo/presign", headers=bearer(tok_a),
             json={"surface": "bogus", "content_type": "video/mp4"})
check("bad surface -> 400", r.status_code == 400)

print("== photo post via upload fallback (disk mode) ==")
r = api.post("/api/ceo/upload", headers=bearer(tok_a),
             files={"media": ("studio.png", PNG_1PX, "image/png")},
             data={"surface": "post"})
j = r.json()
check("upload answers a media key", r.status_code == 200
      and j["ok"] is True and bool(j.get("media_key")),
      r.text[:160])
key_a = j["media_key"]
km = KEY_RE.fullmatch(key_a)
check("key shape is posts/u<uid>-<hex>.<img ext>, owner = artist",
      bool(km) and int(km.group(1)) == uid_a, key_a)
check("file stored under the uploads tree",
      os.path.isfile(os.path.join(os.environ["HERE_UPLOADS"],
                                  key_a)))
r = api.post("/api/ceo/post", headers=bearer(tok_a),
             json={"kind": "photo", "text": "studio shot",
                   "media_key": key_a})
j = r.json()
check("photo post lands", r.status_code == 200 and j["ok"] is True,
      r.text[:200])
prow = dbq("SELECT * FROM posts WHERE id=?", (j["post_id"],))[0]
check("photo post row carries stored media",
      prow["kind"] == "photo" and bool(prow["media"])
      and prow["body"] == "studio shot")
check("posted media file exists",
      os.path.isfile(os.path.join(os.environ["HERE_UPLOADS"],
                                  prow["media"])))
check("minted temp object cleaned up after the post",
      not os.path.exists(os.path.join(os.environ["HERE_UPLOADS"],
                                      key_a)))

print("== another member's key is refused ==")
cl_b, uid_b = signup("Ceo Other", "ceo_other@test.dev")
make_creator(cl_b); stamp_artist(uid_b)
r = cl_b.get("/ceo")
code_b = CODE_RE.search(r.text).group(0)
r = api.post("/api/ceo/link", json={"code": code_b,
                                    "app_version": "1.1.0",
                                    "platform": "mac"})
tok_b = r.json()["token"]
r = api.post("/api/ceo/upload", headers=bearer(tok_a),
             files={"media": ("mine.png", PNG_1PX, "image/png")})
key_a2 = r.json()["media_key"]
r = api.post("/api/ceo/post", headers=bearer(tok_b),
             json={"kind": "photo", "media_key": key_a2})
check("foreign media key refused -> 400",
      r.status_code == 400 and r.json()["ok"] is False,
      r.text[:160])
r = api.post("/api/ceo/post", headers=bearer(tok_b),
             json={"kind": "photo",
                   "media_key": f"posts/u{uid_b}-" + "0" * 32
                                + ".png"})
check("minted-shape but missing object refused -> 400",
      r.status_code == 400)

print("== gate re-check: a closed door pauses posting ==")
dbx("UPDATE users SET creator_mode=0 WHERE id=?", (uid_a,))
r = api.post("/api/ceo/post", headers=bearer(tok_a),
             json={"kind": "text", "text": "should not land"})
check("creator off -> post 403 creator_mode_off",
      r.status_code == 403
      and "creator_mode_off" in r.json()["reasons"], r.text[:160])
r = api.get("/api/ceo/entitlement", headers=bearer(tok_a))
check("entitlement still answers, entitled:false",
      r.status_code == 200 and r.json()["entitled"] is False)
dbx("UPDATE users SET creator_mode=1 WHERE id=?", (uid_a,))

print("== revocation: unlink from the app ==")
r = api.post("/api/ceo/unlink", headers=bearer(tok_a))
check("unlink answers ok", r.status_code == 200
      and r.json()["linked"] is False)
r = api.get("/api/ceo/entitlement", headers=bearer(tok_a))
check("unlinked token -> 401 token_revoked",
      r.status_code == 401
      and r.json()["error"] == "token_revoked")

print("== revocation: room doors ==")
r = api.post("/api/ceo/link", json={"code": code_a,
                                    "app_version": "1.1.0",
                                    "platform": "windows"})
tok2 = r.json()["token"]
tid2 = dbq("SELECT id FROM ceo_tokens WHERE token_hash=?",
           (hashlib.sha256(tok2.encode()).hexdigest(),))[0]["id"]
r = cl_a.post(f"/ceo/tokens/{tid2}/revoke")
check("per-token revoke redirects to the room",
      r.status_code == 303)
r = api.get("/api/ceo/entitlement", headers=bearer(tok2))
check("room-revoked token -> 401", r.status_code == 401)
r = api.post("/api/ceo/link", json={"code": code_a})
tok3 = r.json()["token"]
r = cl_a.post("/ceo/tokens/revoke-all")
check("revoke-all redirects", r.status_code == 303)
r = api.get("/api/ceo/entitlement", headers=bearer(tok3))
check("revoke-all killed the token", r.status_code == 401)

print("== revocation: code regeneration ==")
r = api.post("/api/ceo/link", json={"code": code_a})
tok4 = r.json()["token"]
r = cl_a.post("/ceo/code/regenerate")
check("regenerate redirects", r.status_code == 303)
r = api.get("/api/ceo/entitlement", headers=bearer(tok4))
check("regeneration revoked the live token",
      r.status_code == 401)
r = api.post("/api/ceo/link", json={"code": code_a})
check("old code is dead after regeneration",
      r.status_code == 401)
r = cl_a.get("/ceo")
code_a2 = CODE_RE.search(r.text).group(0)
check("room shows a fresh code", code_a2 != code_a)

print("== revocation: password change ==")
r = api.post("/api/ceo/link", json={"code": code_a2})
tok5 = r.json()["token"]
raw_reset = "ceo-reset-raw-token"
dbx("INSERT INTO password_resets(user_id, token_hash, "
    "token_plain, delivery, created_at, expires_at) "
    "VALUES(?,?,?,?,?,?)",
    (uid_a, here._reset_token_hash(raw_reset), raw_reset,
     "queued", here.now_iso(),
     (datetime.now(timezone.utc)
      + timedelta(hours=1)).isoformat()))
r = cl_a.post(f"/reset/{raw_reset}",
              data={"password": "brandnewpass456",
                    "confirm": "brandnewpass456"})
check("reset submit lands", r.status_code == 303,
      str(r.status_code))
r = api.get("/api/ceo/entitlement", headers=bearer(tok5))
check("password change revoked the token -> 401 token_revoked",
      r.status_code == 401
      and r.json()["error"] == "token_revoked", r.text[:120])

print("== expired token ==")
old_tok = "ceo-expired-token"
dbx("INSERT INTO ceo_tokens(user_id, token_hash, scope, "
    "created_at, expires_at) VALUES(?,?,?,?,?)",
    (uid_a, hashlib.sha256(old_tok.encode()).hexdigest(),
     "post:create media:upload status:read", here.now_iso(),
     (datetime.now(timezone.utc)
      - timedelta(days=1)).isoformat()))
r = api.get("/api/ceo/entitlement", headers=bearer(old_tok))
check("expired token -> 401 token_expired",
      r.status_code == 401
      and r.json()["error"] == "token_expired")

print("== R2 mode (fake S3 wire) ==")
S3 = {}
def fake_wire(method, url, headers=None, body=None, timeout=120):
    key = url.split("/", 4)[4]
    if method == "PUT":
        data = body.read() if hasattr(body, "read") else body
        S3[key] = data or b""
        return 200, {"content-length": str(len(S3[key]))}, b""
    if method == "HEAD":
        if key in S3:
            return 200, {"content-length": str(len(S3[key]))}, b""
        return 404, {}, b""
    if method == "GET":
        if key in S3:
            return 200, {}, S3[key]
        return 404, {}, b""
    return 405, {}, b""
os.environ["R2_ACCOUNT_ID"] = "acct-test"
os.environ["R2_ACCESS_KEY_ID"] = "akid-test"
os.environ["R2_SECRET_ACCESS_KEY"] = "secret-test"
os.environ["R2_BUCKET"] = "bucket-test"
storage._http_request = fake_wire
check("r2 mode on for this section", storage.r2_enabled())
r = api.post("/api/ceo/link", json={"code": code_a2,
                                    "app_version": "1.1.0",
                                    "platform": "mac"})
tok6 = r.json()["token"]
r = api.post("/api/ceo/presign", headers=bearer(tok6),
             json={"surface": "post",
                   "content_type": "video/mp4"})
j = r.json()
check("video presign -> direct with key + url",
      r.status_code == 200 and j["direct"] is True
      and j["key"].startswith(f"posts/u{uid_a}-")
      and j["key"].endswith(".mp4") and bool(j["url"]),
      r.text[:200])
vkey = j["key"]
storage.put_object(vkey, b"fake-video-bytes", "video/mp4",
                   size=16)
r = api.post("/api/ceo/post", headers=bearer(tok6),
             json={"kind": "video", "text": "session clip",
                   "media_key": vkey})
j = r.json()
check("video post with presigned key lands",
      r.status_code == 200 and j["ok"] is True, r.text[:200])
prow = dbq("SELECT * FROM posts WHERE id=?", (j["post_id"],))[0]
check("post media IS the presigned key (web R2 path)",
      prow["media"] == vkey)
r = api.post("/api/ceo/upload", headers=bearer(tok6),
             files={"media": ("shot.png", PNG_1PX, "image/png")})
j = r.json()
check("R2-mode upload fallback stores + answers a key",
      r.status_code == 200 and j["ok"] is True
      and j["media_key"] in S3, r.text[:160])
r = api.post("/api/ceo/post", headers=bearer(tok6),
             json={"kind": "photo", "media_key": j["media_key"]})
check("R2-mode photo post with fallback key lands",
      r.status_code == 200 and r.json()["ok"] is True,
      r.text[:160])
for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID",
          "R2_SECRET_ACCESS_KEY", "R2_BUCKET"):
    del os.environ[k]

print(f"\nPASS {PASS[0]} FAIL {FAIL[0]}")
sys.exit(1 if FAIL[0] else 0)
