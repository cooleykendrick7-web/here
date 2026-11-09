"""Google sign-in (2026-11-08): env-gated OAuth with link-by-email.

Google's two HTTP calls are monkeypatched seams
(_google_exchange_code / _google_tokeninfo); no network is touched.

Run:  ./venv/bin/python tests/test_google_auth.py
"""
import os
import sqlite3
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-google-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
os.environ.pop("GOOGLE_CLIENT_ID", None)
os.environ.pop("GOOGLE_CLIENT_SECRET", None)
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


def anon():
    return TestClient(here.app, follow_redirects=False)


# ---------- dark state: no keys, no button, route explains ----------
c = anon()
r = c.get("/login")
check("dark: login page has NO Google button",
      r.status_code == 200 and "Continue with Google" not in r.text)
r = c.get("/signup")
check("dark: signup page has NO Google button",
      r.status_code == 200 and "Continue with Google" not in r.text)
r = c.get("/auth/google")
check("dark: /auth/google redirects to /login with an explanation",
      r.status_code == 303 and "isn" in r.headers["location"])

# ---------- light it up (fake keys, fake Google) ----------
os.environ["GOOGLE_CLIENT_ID"] = "test-client-id"
os.environ["GOOGLE_CLIENT_SECRET"] = "test-secret"
PROFILE = {"sub": "g-sub-1", "email": "gina@example.com",
           "email_verified": True, "name": "Gina Google",
           "aud": "test-client-id"}
here._google_exchange_code = lambda code, uri: {"id_token": "fake-id-token"}
here._google_tokeninfo = lambda idt: dict(PROFILE)

c = anon()
r = c.get("/login")
check("lit: login page SHOWS the Google button",
      "Continue with Google" in r.text and 'href="/auth/google' in r.text)
r = c.get("/auth/google")
check("start: redirects to accounts.google.com with our client + state",
      r.status_code == 303
      and r.headers["location"].startswith(
          "https://accounts.google.com/o/oauth2/v2/auth?")
      and "client_id=test-client-id" in r.headers["location"]
      and "state=" in r.headers["location"])
state = c.cookies.get("here_gstate")
check("start: state cookie set", bool(state))

# state mismatch must fail closed
r = c.get("/auth/google/callback?code=x&state=WRONG")
check("callback: wrong state refused",
      r.status_code == 303 and "didn" in r.headers["location"])
check("callback: wrong state created NO account",
      not dbq("SELECT id FROM users WHERE email='gina@example.com'"))

# happy path: brand-new account
r = c.get(f"/auth/google/callback?code=ok&state={state}")
check("callback: new Google user lands on /welcome",
      r.status_code == 303 and "/welcome" in r.headers["location"],
      r.headers.get("location", ""))
rows = dbq("SELECT * FROM users WHERE email='gina@example.com'")
check("callback: account created with google_sub stored",
      len(rows) == 1 and rows[0]["google_sub"] == "g-sub-1")
check("callback: session cookie issued",
      "here_session" in r.headers.get("set-cookie", ""))
home = c.get("/home")
check("callback: the new session actually works",
      home.status_code == 200 and "Gina Google" in home.text)

# returning by sub: straight to /home, no duplicate
c2 = anon()
c2.get("/auth/google")
st2 = c2.cookies.get("here_gstate")
r = c2.get(f"/auth/google/callback?code=ok&state={st2}")
check("returning: known sub goes to /home",
      r.status_code == 303 and "/home" in r.headers["location"])
check("returning: still exactly ONE account for that email",
      len(dbq("SELECT id FROM users WHERE email='gina@example.com'")) == 1)

# link by email: an existing password account + Google same email
c3 = anon()
c3.post("/signup", data={"name": "Paul Pass", "email": "paul@example.com",
                         "password": "Str0ng!Pass99"})
paul_id = dbq("SELECT id FROM users WHERE email='paul@example.com'")[0]["id"]
PROFILE.update({"sub": "g-sub-2", "email": "paul@example.com",
                "name": "Paul Pass"})
c4 = anon()
c4.get("/auth/google")
st4 = c4.cookies.get("here_gstate")
r = c4.get(f"/auth/google/callback?code=ok&state={st4}")
rows = dbq("SELECT * FROM users WHERE email='paul@example.com'")
check("link: Google sign-in lands on the SAME account (no duplicate)",
      len(rows) == 1 and rows[0]["id"] == paul_id
      and rows[0]["google_sub"] == "g-sub-2")
c5 = anon()
r = c5.post("/login", data={"email": "paul@example.com",
                            "password": "Str0ng!Pass99"})
check("link: the old password still works after linking",
      r.status_code == 303)

# invite credit for a Google-created account
c6 = anon()
c6.post("/signup", data={"name": "Ivy Inviter", "email": "ivy@example.com",
                         "password": "Str0ng!Pass99"})
ivy = dbq("SELECT * FROM users WHERE email='ivy@example.com'")[0]
PROFILE.update({"sub": "g-sub-3", "email": "newbie@example.com",
                "name": "New Bie"})
c7 = anon()
c7.cookies.set("here_invite", ivy["invite_code"])
c7.get("/auth/google")
st7 = c7.cookies.get("here_gstate")
r = c7.get(f"/auth/google/callback?code=ok&state={st7}")
nb = dbq("SELECT * FROM users WHERE email='newbie@example.com'")
check("invite: Google signup credits the inviter",
      len(nb) == 1 and nb[0]["invited_by"] == ivy["id"])

# unverified email must never create or link
PROFILE.update({"sub": "g-sub-4", "email": "sneaky@example.com",
                "email_verified": False, "name": "Sneak"})
c8 = anon()
c8.get("/auth/google")
st8 = c8.cookies.get("here_gstate")
r = c8.get(f"/auth/google/callback?code=ok&state={st8}")
check("safety: unverified Google email refused",
      r.status_code == 303
      and not dbq("SELECT id FROM users WHERE email='sneaky@example.com'"))

# wrong audience (a token minted for another app) refused
PROFILE.update({"sub": "g-sub-5", "email": "aud@example.com",
                "email_verified": True, "aud": "someone-elses-client",
                "name": "Aud"})
c9 = anon()
c9.get("/auth/google")
st9 = c9.cookies.get("here_gstate")
r = c9.get(f"/auth/google/callback?code=ok&state={st9}")
check("safety: foreign-audience token refused",
      r.status_code == 303
      and not dbq("SELECT id FROM users WHERE email='aud@example.com'"))

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
