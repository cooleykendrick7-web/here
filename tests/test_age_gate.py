"""The 18+ age gate (Nov 15, 2026) — Ken's call: HERE is 18+
at the door. Every NEW signup, email or Google, proves an
adult birthdate before any account exists. Current members
are untouched; Match doesn't change.

Covered:
- email signup: under-18 rejected with the EXACT message and
  NO user row; exactly-18-today accepted; 18-minus-one-day
  rejected; missing DOB rejected; future DOB rejected;
  absurd (age > 120) DOB rejected;
- a valid adult signup stores users.birthdate, and the public
  profile shows "Age N" — never the raw birthdate string;
- invite-code signup still credits with a DOB present;
- Google NEW identity: the callback parks it (redirect to
  /auth/google/dob, NO account, pending row written); an
  adult DOB completes it (account + google_sub + birthdate +
  session + /welcome); an under-18 DOB completes nothing
  (no account, pending row gone, exact message shown);
- Google EXISTING user: straight in, no birthday page;
- the dob page refuses browsers with no/expired pending row.

Google's two HTTP calls are monkeypatched seams, mirroring
test_google_auth.py. Run:  ./venv/bin/python tests/test_age_gate.py
"""
import os
import sys
import tempfile
from datetime import date, timedelta
from urllib.parse import unquote

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-agegate-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
os.environ.pop("GOOGLE_CLIENT_ID", None)
os.environ.pop("GOOGLE_CLIENT_SECRET", None)
sys.path.insert(0, BASE)

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []

UNDER18 = ("HERE is for 18 and up \u2014 come back and see "
           "us when you're 18.")
INVALID = "Please enter your real birthday"
PW = "Str0ng!Pass99"


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{extra}]" if extra and not cond else ""))


def dbq(sql, args=()):
    conn = here.db()
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def dbx(sql, args=()):
    conn = here.db()
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def anon():
    return TestClient(here.app, follow_redirects=False)


def signup(c, name, email, dob=None, extra=None):
    data = {"name": name, "email": email, "password": PW}
    if dob is not None:
        data["birthdate"] = dob
    if extra:
        data.update(extra)
    return c.post("/signup", data=data)


def loc(r):
    return unquote(r.headers.get("location", ""))


TODAY = here._today_chicago()


def years_ago(n, plus_days=0):
    try:
        d = TODAY.replace(year=TODAY.year - n)
    except ValueError:  # Feb 29 -> Mar 1
        d = TODAY.replace(year=TODAY.year - n, day=1, month=3)
    return (d + timedelta(days=plus_days)).isoformat()


# ---------- email signup: the gate itself ----------
c = anon()
r = signup(c, "Young One", "young@age.test", dob=years_ago(17))
check("under-18 email signup -> 303 back to /signup",
      r.status_code == 303 and "/signup?error=" in loc(r), loc(r))
check("under-18 email signup -> the EXACT message",
      UNDER18 in loc(r), loc(r))
check("under-18 email signup -> NO user row",
      not dbq("SELECT id FROM users WHERE email='young@age.test'"))

r = signup(c, "Just Legal", "legal@age.test", dob=years_ago(18))
check("exactly-18-today signup -> accepted",
      r.status_code == 303 and "/welcome" in loc(r), loc(r))
rows = dbq("SELECT * FROM users WHERE email='legal@age.test'")
check("exactly-18 account exists", len(rows) == 1)
check("birthdate stored on the account",
      rows and rows[0]["birthdate"] == years_ago(18))

r = signup(c, "Almost There", "almost@age.test",
           dob=years_ago(18, plus_days=1))
check("18-minus-one-day signup -> rejected with the exact message",
      r.status_code == 303 and UNDER18 in loc(r), loc(r))
check("18-minus-one-day -> NO user row",
      not dbq("SELECT id FROM users WHERE email='almost@age.test'"))

r = signup(c, "No Birthday", "nodob@age.test")
check("missing DOB -> rejected (real-birthday message)",
      r.status_code == 303 and INVALID in loc(r), loc(r))
check("missing DOB -> NO user row",
      not dbq("SELECT id FROM users WHERE email='nodob@age.test'"))

future = (TODAY + timedelta(days=30)).isoformat()
r = signup(c, "Time Traveler", "future@age.test", dob=future)
check("future DOB -> rejected (real-birthday message)",
      r.status_code == 303 and INVALID in loc(r), loc(r))
check("future DOB -> NO user row",
      not dbq("SELECT id FROM users WHERE email='future@age.test'"))

r = signup(c, "Methuselah", "old@age.test", dob=years_ago(121))
check("age > 120 DOB -> rejected (real-birthday message)",
      r.status_code == 303 and INVALID in loc(r), loc(r))
check("age > 120 -> NO user row",
      not dbq("SELECT id FROM users WHERE email='old@age.test'"))

r = signup(c, "Gibberish", "gib@age.test", dob="not-a-date")
check("garbage DOB -> rejected (real-birthday message)",
      r.status_code == 303 and INVALID in loc(r), loc(r))
check("garbage DOB -> NO user row",
      not dbq("SELECT id FROM users WHERE email='gib@age.test'"))

# ---------- adult profile: Age shows, birthday never does ----------
c = anon()
r = signup(c, "Dana Adult", "dana@age.test", dob="1990-06-15")
dana = dbq("SELECT * FROM users WHERE email='dana@age.test'")[0]
dbx("UPDATE users SET city='Fargo', state='ND' WHERE id=?",
    (dana["id"],))
expected_age = here._age_on("1990-06-15")
viewer = anon()
signup(viewer, "Vic Viewer", "vic@age.test", dob="1985-01-01")
r = viewer.get(f"/user/{dana['id']}")
check("public profile renders", r.status_code == 200)
check(f"profile shows Age {expected_age}",
      f"Age {expected_age}" in r.text)
check("profile NEVER shows the raw birthdate",
      "1990-06-15" not in r.text and "06/15/1990" not in r.text)

# ---------- invite code still credits with a DOB present ----------
inv = anon()
signup(inv, "Ivy Inviter", "ivy@age.test", dob="1990-01-01")
inv_row = dbq("SELECT * FROM users WHERE email='ivy@age.test'")[0]
inv.post("/invite-code", data={"code": "AGEGATE10"})
joiner = anon()
r = signup(joiner, "Jay Joiner", "jay@age.test", dob="1995-03-03",
           extra={"invite_code": "AGEGATE10"})
jrow = dbq("SELECT * FROM users WHERE email='jay@age.test'")
check("invite-code signup with DOB -> account created",
      r.status_code == 303 and len(jrow) == 1, loc(r))
check("invite-code signup with DOB -> inviter credited",
      jrow and jrow[0]["invited_by"] == inv_row["id"])

# ---------- Google: new identity parks at the birthday page ----------
os.environ["GOOGLE_CLIENT_ID"] = "test-client-id"
os.environ["GOOGLE_CLIENT_SECRET"] = "test-secret"
PROFILE = {"sub": "g-age-1", "email": "gina@age.test",
           "email_verified": True, "name": "Gina Google",
           "aud": "test-client-id"}
here._google_exchange_code = lambda code, uri: {"id_token": "tok"}
here._google_tokeninfo = lambda idt: dict(PROFILE)


def google_flow(client):
    client.get("/auth/google")
    state = client.cookies.get("here_gstate")
    return client.get(f"/auth/google/callback?code=ok&state={state}")


g = anon()
r = google_flow(g)
check("google new identity -> redirected to the birthday page",
      r.status_code == 303
      and r.headers["location"].endswith("/auth/google/dob"),
      r.headers.get("location", ""))
check("google new identity -> NO account yet",
      not dbq("SELECT id FROM users WHERE email='gina@age.test'"))
check("google new identity -> pending row parked",
      len(dbq("SELECT token FROM google_signups "
              "WHERE email='gina@age.test'")) == 1)
check("google new identity -> NO session issued",
      "here_session" not in r.headers.get("set-cookie", ""))

r = g.get("/auth/google/dob")
check("dob page renders for the pending browser",
      r.status_code == 200 and "birthday" in r.text
      and 'name="birthdate"' in r.text)

r = g.post("/auth/google/dob", data={"birthdate": "1992-05-05"})
check("google adult DOB -> lands on /welcome",
      r.status_code == 303 and "/welcome" in loc(r), loc(r))
rows = dbq("SELECT * FROM users WHERE email='gina@age.test'")
check("google adult DOB -> account created, sub stamped",
      len(rows) == 1 and rows[0]["google_sub"] == "g-age-1")
check("google adult DOB -> birthdate stored",
      rows and rows[0]["birthdate"] == "1992-05-05")
check("google adult DOB -> pending row consumed",
      not dbq("SELECT token FROM google_signups "
              "WHERE email='gina@age.test'"))
home = g.get("/home")
check("google adult DOB -> the session works",
      home.status_code == 200 and "Gina Google" in home.text)

# existing Google user: no birthday page, straight in
g2 = anon()
r = google_flow(g2)
check("google EXISTING user -> /home, no birthday page",
      r.status_code == 303 and "/home" in r.headers["location"]
      and "dob" not in r.headers["location"],
      r.headers.get("location", ""))
check("google existing user -> still exactly one account",
      len(dbq("SELECT id FROM users WHERE email='gina@age.test'")) == 1)

# under-18 Google completion: nothing is created
PROFILE.update({"sub": "g-age-2", "email": "kid@age.test",
                "name": "Kid Google"})
g3 = anon()
r = google_flow(g3)
check("google minor identity -> parked at the birthday page",
      r.status_code == 303
      and r.headers["location"].endswith("/auth/google/dob"))
r = g3.post("/auth/google/dob", data={"birthdate": years_ago(16)})
from markupsafe import escape as _esc  # the template |e filter
check("google under-18 DOB -> page shows the EXACT message",
      r.status_code == 200 and str(_esc(UNDER18)) in r.text)
check("google under-18 DOB -> NO account created",
      not dbq("SELECT id FROM users WHERE email='kid@age.test'"))
check("google under-18 DOB -> pending row deleted",
      not dbq("SELECT token FROM google_signups "
              "WHERE email='kid@age.test'"))
r = g3.get("/auth/google/dob")
check("google under-18 DOB -> pending cookie is dead (to /login)",
      r.status_code == 303
      and r.headers["location"].startswith("/login"))

# no pending row / expired pending row: the page refuses
g4 = anon()
r = g4.get("/auth/google/dob")
check("dob page with NO pending signup -> /login",
      r.status_code == 303
      and r.headers["location"].startswith("/login"))
dbx("INSERT INTO google_signups(token, sub, email, name, "
    "created_at) VALUES('stale-tok', 'g-age-9', 'stale@age.test', "
    "'Stale', ?)",
    ((here.datetime.now(here.timezone.utc)
      - here.timedelta(minutes=31)).isoformat(),))
g5 = anon()
g5.cookies.set("here_google_pending", "stale-tok")
r = g5.get("/auth/google/dob")
check("dob page with an EXPIRED pending row -> /login",
      r.status_code == 303
      and r.headers["location"].startswith("/login"))
check("expired pending row is deleted on sight",
      not dbq("SELECT token FROM google_signups "
              "WHERE token='stale-tok'"))

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
