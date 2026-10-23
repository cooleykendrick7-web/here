"""Growth door tests for HERE: public pages (/p, /l, /u), share-link
hooks, and the invite system (/join/{code}, /invite, invite codes,
gold/insider milestones).

Run:  ./venv/bin/python tests/test_growth_door.py
Uses a throwaway DB/uploads dir; no network.
"""
import os
import sqlite3
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-gd-")
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


def set_cookies(r):
    try:
        return r.headers.get_list("set-cookie")
    except Exception:
        v = r.headers.get("set-cookie")
        return [v] if v else []


def fab_invitee(name, email, inviter_id):
    """Direct DB insert of a user credited to inviter_id, with a real
    unique invite code (via the app's own generator)."""
    conn = sqlite3.connect(os.environ["HERE_DB"])
    code = here.gen_invite_code(conn)
    conn.execute(
        "INSERT INTO users(name, email, pw_hash, created_at, invite_code,"
        " invited_by) VALUES(?,?,?,?,?,?)",
        (name, email, "dummy-hash", "2026-10-22T00:00:00+00:00", code,
         inviter_id))
    conn.commit()
    conn.close()


JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64 + b"\xff\xd9"

# ---------------- users + posts ----------------
A, AID = signup("Inviter Amy", "amy@gd.io")
PAM, PAMID = signup("Public Pam", "pam@gd.io")
PETE, PETEID = signup("Private Pete", "pete@gd.io")
LARRY, LARRYID = signup("Live Larry", "larry@gd.io")

r = PAM.post("/posts", data={"kind": "photo",
                             "body": "pam public hello world",
                             "privacy": "public"},
             files={"media": ("pub.jpg", JPEG, "image/jpeg")})
assert r.status_code == 303, r.status_code
r = PAM.post("/posts", data={"kind": "photo",
                             "body": "pam friends secret body",
                             "privacy": "friends"},
             files={"media": ("fri.jpg", JPEG, "image/jpeg")})
assert r.status_code == 303, r.status_code
r = PETE.post("/posts", data={"kind": "photo",
                              "body": "pete public but private profile",
                              "privacy": "public"},
              files={"media": ("pete.jpg", JPEG, "image/jpeg")})
assert r.status_code == 303, r.status_code


def post_row(body):
    rows = dbq("SELECT * FROM posts WHERE body=?", (body,))
    return rows[0] if rows else None


pub = post_row("pam public hello world")
fri = post_row("pam friends secret body")
pete_post = post_row("pete public but private profile")
pub_url = "/uploads/" + pub["media"]
fri_url = "/uploads/" + fri["media"]
check("test posts captured from DB", bool(pub and fri and pete_post))

dbx("UPDATE users SET private_profile=1 WHERE id=?", (PETEID,))

anon = TestClient(here.app, follow_redirects=False)

# ---------------- anon: public post ----------------
r = anon.get(f"/p/{pub['id']}")
t = r.text
check("anon /p public post -> 200", r.status_code == 200)
check("anon /p shows body text", "pam public hello world" in t)
check("anon /p shows media URL", pub_url in t)
check("anon /p shows author name", "Public Pam" in t)
check("anon /p shows Join HERE CTA", "Join HERE" in t)
check("anon /p has og:title", 'property="og:title"' in t
      and "Public Pam on HERE" in t)
check("anon /p has og:image (photo, absolute)",
      'property="og:image"' in t and ("http" in t.split(
          'property="og:image"')[1].split(">")[0]))

# friends-only post -> teaser, leaks nothing
r = anon.get(f"/p/{fri['id']}")
t = r.text
check("anon /p friends post -> 200 teaser", r.status_code == 200
      and "join to see it" in t)
check("teaser: friends body ABSENT", "pam friends secret body" not in t)
check("teaser: author name ABSENT", "Public Pam" not in t)
check("teaser: friends media ABSENT", fri_url not in t)

# public post by a private-profile author -> teaser
r = anon.get(f"/p/{pete_post['id']}")
t = r.text
check("anon /p private author's public post -> teaser",
      r.status_code == 200 and "join to see it" in t
      and "pete public but private profile" not in t
      and "Private Pete" not in t)

# nonexistent post -> teaser
r = anon.get("/p/999999")
check("anon /p nonexistent -> 200 teaser", r.status_code == 200
      and "join to see it" in r.text)

# share rows are not public faces either
BOBC, BOBID = signup("Sharer Sam", "sam@gd.io")
BOBC.post(f"/post/{pub['id']}/share")
share_rows = dbq("SELECT id FROM posts WHERE shared_post_id=?",
                 (pub["id"],))
if share_rows:
    r = anon.get(f"/p/{share_rows[0]['id']}")
    check("anon /p share row -> teaser",
          r.status_code == 200 and "join to see it" in r.text
          and "pam public hello world" not in r.text)
else:
    check("anon /p share row -> teaser", False, "no share row created")

# logged-in users can open /p too
r = PAM.get(f"/p/{pub['id']}")
check("logged-in /p public post -> 200 with content",
      r.status_code == 200 and "pam public hello world" in r.text)

# ---------------- anon: live ----------------
r = LARRY.post("/live/start", json={"title": "Larry live show"})
assert r.status_code == 200 and r.json().get("ok"), r.text[:200]
lid = r.json()["id"]
r = anon.get(f"/l/{lid}")
t = r.text
check("anon /l live -> 200 with title + host",
      r.status_code == 200 and "Larry live show" in t
      and "Live Larry" in t and "LIVE" in t)
dbx("UPDATE users SET private_profile=1 WHERE id=?", (LARRYID,))
r = anon.get(f"/l/{lid}")
check("anon /l private host -> teaser without title",
      r.status_code == 200 and "join to see it" in r.text
      and "Larry live show" not in r.text)
dbx("UPDATE users SET private_profile=0 WHERE id=?", (LARRYID,))
LARRY.post(f"/live/{lid}/end")
r = anon.get(f"/l/{lid}")
check("anon /l ended live -> teaser",
      r.status_code == 200 and "join to see it" in r.text
      and "Larry live show" not in r.text)

# ---------------- anon: profile ----------------
r = anon.get(f"/u/{PAMID}")
t = r.text
check("anon /u public user -> 200 with name + public media",
      r.status_code == 200 and "Public Pam" in t and pub_url in t)
check("anon /u: friends-only media ABSENT", fri_url not in t)
check("anon /u photo cell links /p/{id}", f'href="/p/{pub["id"]}"' in t)
r = anon.get(f"/u/{PETEID}")
check("anon /u private user -> teaser, name absent",
      r.status_code == 200 and "join to see it" in r.text
      and "Private Pete" not in r.text)
r = anon.get("/u/999999")
check("anon /u nonexistent -> teaser", r.status_code == 200
      and "join to see it" in r.text)

# ---------------- template share hooks ----------------
def read_tpl(rel):
    with open(os.path.join(BASE, "templates", rel)) as fh:
        return fh.read()

check("post_card has /p/ copy-share hook",
      'data-copy-path="/p/' in read_tpl("partials/post_card.html"))
check("live.html has /l/ share button",
      'data-copy-path="/l/' in read_tpl("live.html")
      and "Share this live" in read_tpl("live.html"))
check("profile.html has /u/ share button",
      'data-copy-path="/u/' in read_tpl("profile.html"))
views_src = read_tpl("views.html")
check("views.html rail creates a 🔗 share item for /p/",
      "🔗" in views_src and "'/p/'" in views_src and "Share" in views_src)
check("base.html has the global data-copy-path handler + av-gold CSS",
      "data-copy-path" in read_tpl("base.html")
      and ".av-gold" in read_tpl("base.html"))

# views page render smoke (Jinja + inline JS page loads for a user)
r = PAM.get("/views")
check("GET /views as a user -> 200", r.status_code == 200)

# ---------------- invite codes: backfill / shape ----------------
arow = dbq("SELECT invite_code FROM users WHERE id=?", (AID,))[0]
acode = arow["invite_code"]
check("A's invite_code is 8 chars, alphabet-only",
      bool(acode) and len(acode) == 8
      and all(ch in here.INVITE_ALPHABET for ch in acode), str(acode))
all_codes = [row["invite_code"] for row in dbq("SELECT invite_code FROM users")]
check("every user has a non-empty unique invite_code",
      all(all_codes) and len(set(all_codes)) == len(all_codes))

# ---------------- join flow ----------------
B = TestClient(here.app, follow_redirects=False)
r = B.get(f"/join/{acode}")
check("GET /join/{code} -> 303 /signup",
      r.status_code == 303 and r.headers.get("location") == "/signup",
      str(r.status_code) + " " + r.headers.get("location", ""))
check("GET /join/{code} sets here_invite cookie",
      any("here_invite=" in c for c in set_cookies(r)), str(set_cookies(r)))
r = B.get("/signup")
check("GET /signup with invite cookie shows inviter banner",
      r.status_code == 200 and "Inviter Amy" in r.text
      and "invited you to HERE" in r.text)
r = B.post("/signup", data={"name": "Invited Bob", "email": "bob@gd.io",
                            "password": "pw123456"})
check("invited signup -> 303 /home", r.status_code == 303
      and r.headers.get("location") == "/welcome")
bob = dbq("SELECT * FROM users WHERE email='bob@gd.io'")[0]
check("DB: Bob.invited_by == A.id", bob["invited_by"] == AID)
notes = dbq("SELECT * FROM notifications WHERE user_id=? AND kind='invite'",
            (AID,))
check("A got an 'invite' notification", len(notes) == 1, str(len(notes)))
deleting = [c for c in set_cookies(r) if "here_invite=" in c]
check("signup response clears here_invite cookie",
      any(("Max-Age=0" in c) or ("expires=Thu, 01 Jan 1970" in c)
          for c in deleting), str(deleting))

# duplicate-email attempt: no credit, no second notification
count_before = here.invite_count(AID)
notes_before = len(dbq(
    "SELECT * FROM notifications WHERE user_id=? AND kind='invite'", (AID,)))
C = TestClient(here.app, follow_redirects=False)
C.get(f"/join/{acode}")
r = C.post("/signup", data={"name": "Copy Cat", "email": "bob@gd.io",
                            "password": "pw123456"})
check("duplicate-email signup -> error redirect",
      r.status_code == 303
      and r.headers.get("location", "").startswith("/signup?error="),
      r.headers.get("location", ""))
check("duplicate attempt: invite_count(A) unchanged",
      here.invite_count(AID) == count_before)
check("duplicate attempt: no second invite notification",
      len(dbq("SELECT * FROM notifications WHERE user_id=? AND kind='invite'",
              (AID,))) == notes_before)

# invalid code: redirect, NO cookie
existing = {row["invite_code"] for row in dbq("SELECT invite_code FROM users")}
bad = None
for cand in ("ZZZZZZZZ", "YYYYYYYY", "XXXXXXXX", "WWWWWWWW"):
    if cand not in existing:
        bad = cand
        break
D = TestClient(here.app, follow_redirects=False)
r = D.get(f"/join/{bad}")
check("GET /join/{invalid} -> 303 /signup, NO here_invite cookie",
      r.status_code == 303 and r.headers.get("location") == "/signup"
      and not any("here_invite=" in c for c in set_cookies(r)),
      str(set_cookies(r)))

# ---------------- /invite page ----------------
r = A.get("/invite")
t = r.text
check("/invite as A -> 200", r.status_code == 200)
check("/invite contains the invite link", f"/join/{acode}" in t)
check("/invite lists the invitee (Bob)", "Invited Bob" in t)
check("/invite shows the count (1)", ">1<" in t or "1 person" in t
      or "<strong>1</strong>" in t)
check("/invite has a QR image for the link", "api.qrserver.com" in t)

# ---------------- milestones: gold at 3 ----------------
fab_invitee("Fab Two", "fab2@gd.io", AID)
fab_invitee("Fab Three", "fab3@gd.io", AID)
check("invite_count(A) == 3 after fabricating 2 more",
      here.invite_count(AID) == 3)
prof = A.get(f"/user/{AID}").text
check("profile at 3 invites shows 🥇 Gold Inviter chip",
      "🥇 Gold Inviter" in prof)
check("profile at 3 invites has av-gold on the avatar", "av-gold" in prof)
home = A.get("/home").text
check("nav at 3 invites has av-gold", "av-gold" in home)

# ---------------- milestones: insider at 10 ----------------
for i in range(4, 11):
    fab_invitee(f"Fab {i}", f"fab{i}@gd.io", AID)
check("invite_count(A) == 10", here.invite_count(AID) == 10)
prof = A.get(f"/user/{AID}").text
check("profile at 10 invites shows ⭐ HERE Insider chip",
      "⭐ HERE Insider" in prof)
check("profile at 10 invites no longer shows the Gold chip",
      "🥇 Gold Inviter" not in prof)
disc = A.get("/discover").text
idx = disc.find("HERE Insiders")
check("/discover shows the ⭐ HERE Insiders card", idx >= 0)
check("/discover insiders strip contains A's name",
      idx >= 0 and "Inviter Amy" in disc[idx:])

# final invariant: codes still unique + non-empty for EVERY user
all_codes = [row["invite_code"] for row in dbq("SELECT invite_code FROM users")]
check("final: every user row has a non-empty unique invite_code",
      all(all_codes) and len(set(all_codes)) == len(all_codes))

# ---------------- summary ----------------
fails = [n for n, okk in RESULTS if not okk]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
