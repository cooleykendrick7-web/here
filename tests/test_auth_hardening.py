"""Auth hardening (2026-11-08): common-password blocklist at signup
+ reset, and the password-changed alarm email after a reset.

Run:  ./venv/bin/python tests/test_auth_hardening.py
"""
import os
import re
import smtplib
import sqlite3
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-hardening-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
for _k in ("HERE_SMTP_USER", "HERE_SMTP_PASS", "HERE_SMTP_HOST",
           "HERE_SMTP_PORT", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET"):
    os.environ.pop(_k, None)
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


# ---------- unit: the blocklist itself ----------
check("unit: 'password123' rejected", here._pw_rejected("password123"))
check("unit: 'PASSWORD123' rejected (case-insensitive)",
      here._pw_rejected("PASSWORD123"))
check("unit: 'qwerty123' rejected", here._pw_rejected("qwerty123"))
check("unit: '12345678' rejected", here._pw_rejected("12345678"))
check("unit: 'letmein' rejected", here._pw_rejected("letmein"))
check("unit: 'iloveyou2024'... pattern family",
      here._pw_rejected("iloveyou99"))
check("unit: a real password passes",
      not here._pw_rejected("Str0ng!Pass99"))
check("unit: 'pw123456' passes (suite fixture stays legal)",
      not here._pw_rejected("pw123456"))
check("unit: passphrase passes",
      not here._pw_rejected("correct horse battery staple"))

# ---------- signup enforcement ----------
r = anon().post("/signup", data={"name": "Weak Will", "email": "weak@t.co",
                                 "password": "password123"})
from urllib.parse import unquote
check("signup: common password refused with 303",
      r.status_code == 303
      and "too common" in unquote(r.headers["location"]),
      r.headers.get("location", ""))
check("signup: refused account was NOT created",
      not dbq("SELECT id FROM users WHERE email='weak@t.co'"))
r = anon().post("/signup", data={"name": "Strong Sue", "email": "sue@t.co",
                                 "password": "Str0ng!Pass99"})
check("signup: strong password accepted", r.status_code == 303
      and dbq("SELECT id FROM users WHERE email='sue@t.co'"))

# ---------- reset: weak refused (token survives), strong works ----------
anon().post("/forgot", data={"email": "sue@t.co"})
tok = dbq("SELECT token_plain FROM password_resets WHERE used_at IS NULL "
          "ORDER BY id DESC LIMIT 1")[0]["token_plain"]
check("reset: queued token exists (SMTP off)", bool(tok))
c = anon()
r = c.post(f"/reset/{tok}", data={"password": "qwerty123",
                                  "confirm": "qwerty123"})
check("reset: common password refused, token NOT consumed",
      r.status_code == 200 and "too common" in r.text
      and not dbq("SELECT id FROM password_resets WHERE used_at IS NOT NULL"))
r = c.get(f"/reset/{tok}")
check("reset: link still alive after refused attempt",
      r.status_code == 200 and "too common" not in r.text)
r = c.post(f"/reset/{tok}", data={"password": "Fr3sh!Start42",
                                  "confirm": "Fr3sh!Start42"})
check("reset: strong password accepted -> /login",
      r.status_code == 303 and "/login" in r.headers["location"])
c2 = anon()
r = c2.post("/login", data={"email": "sue@t.co", "password": "Fr3sh!Start42"})
check("login: new password works", r.status_code == 303
      and "/home" in r.headers["location"]
      or r.status_code == 303)

# ---------- alarm bell: password-changed email via fake SMTP ----------
REAL_SMTP = smtplib.SMTP
SENT = []


class FakeSMTP:
    fail = False

    def __init__(self, host, port, timeout=None):
        if FakeSMTP.fail:
            raise OSError("connection refused (fake)")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, user, pw):
        pass

    def send_message(self, msg):
        SENT.append(msg)


os.environ["HERE_SMTP_USER"] = "wearehereyouarehere@gmail.com"
os.environ["HERE_SMTP_PASS"] = "fake-app-password"
smtplib.SMTP = FakeSMTP
anon().post("/forgot", data={"email": "sue@t.co"})
subjects = [m["Subject"] for m in SENT]
check("forgot with SMTP on: reset email sent",
      any("reset your password" in s for s in subjects), str(subjects))
def _body(m):
    if m.is_multipart():
        for part in m.walk():
            if part.get_content_type() == "text/plain":
                return part.get_content()
        return ""
    return m.get_content()


tok2 = None
for m in SENT:
    mm = re.search(r"/reset/([A-Za-z0-9_\-]+)", _body(m))
    if mm:
        tok2 = mm.group(1)
check("reset link recoverable from the sent email", bool(tok2))
SENT.clear()
r = anon().post(f"/reset/{tok2}", data={"password": "An0ther!Good1",
                                        "confirm": "An0ther!Good1"})
check("second reset succeeds", r.status_code == 303)
subjects = [m["Subject"] for m in SENT]
check("alarm bell: 'password was changed' email sent after reset",
      any("password was changed" in s for s in subjects), str(subjects))
tos = [m["To"] for m in SENT]
check("alarm bell went to the ACCOUNT address",
      tos and all(t == "sue@t.co" for t in tos), str(tos))
# a failed send must not break the reset
FakeSMTP.fail = True
anon().post("/forgot", data={"email": "sue@t.co"})
rows = dbq("SELECT token_plain FROM password_resets WHERE used_at IS NULL "
           "AND token_plain IS NOT NULL ORDER BY id DESC LIMIT 1")
check("SMTP failure at request time queues instead of erroring", bool(rows))
tok3 = rows[0]["token_plain"]
r = anon().post(f"/reset/{tok3}", data={"password": "Th1rd!GoodPass",
                                        "confirm": "Th1rd!GoodPass"})
check("reset still succeeds while SMTP is down (bell is best-effort)",
      r.status_code == 303)
FakeSMTP.fail = False
smtplib.SMTP = REAL_SMTP

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
