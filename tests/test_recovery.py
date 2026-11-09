"""Build O tests: password + account recovery.

Covers the /forgot + /reset/{token} flow end to end: identical
no-enumeration responses, hashed single-use 1-hour tokens, session
kill on reset, supersede-on-new-request, expiry, per-email + per-IP
rate limits, the SMTP-absent owner queue (show link + hand-sent),
the SMTP-present path (monkeypatched smtplib — success AND failure),
owner-session gating, and recovery for blocked/private accounts.

Email sending is exercised ONLY through a fake smtplib.SMTP; the
HERE_SMTP_* env vars are set/unset inside this process only.

Run:  ./venv/bin/python tests/test_recovery.py
"""
import hashlib
import os
import re
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-recovery-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
# Deterministic starting point: NO SMTP configured (the live state).
for _k in ("HERE_SMTP_USER", "HERE_SMTP_PASS", "HERE_SMTP_HOST",
           "HERE_SMTP_PORT"):
    os.environ.pop(_k, None)
sys.path.insert(0, BASE)

import smtplib  # noqa: E402

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []
SUCCESS_LINE = ("If that email has an account, a reset link is on "
                "its way. It works for 1 hour.")


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


def anon():
    return TestClient(here.app, follow_redirects=False)


def signup(name, email, password="pw123456"):
    c = anon()
    r = c.post("/signup", data={"name": name, "email": email,
                                "password": password})
    assert r.status_code == 303, (name, r.status_code)
    uid = dbq("SELECT id FROM users WHERE email=?", (email,))[0]["id"]
    return c, uid


def login(email, password):
    c = anon()
    return c, c.post("/login", data={"email": email,
                                     "password": password})


def forgot(email):
    return anon().post("/forgot", data={"email": email})


def last_reset(uid):
    rows = dbq("SELECT * FROM password_resets WHERE user_id=? "
               "ORDER BY id DESC", (uid,))
    return rows[0] if rows else None


def sha(tok):
    return hashlib.sha256(tok.encode()).hexdigest()


# ---------------- owner client (session row inserted directly) ----
dbx("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
    ("recovery-owner-token", here.now_iso()))
OWN = anon()
OWN.cookies.set("here_owner", "recovery-owner-token")

# ---------------- pages: forgot form, helper, login link ----------
r = anon().get("/forgot")
check("GET /forgot renders", r.status_code == 200)
check("forgot page asks the question",
      "Forgot your password?" in r.text)
check("helper names the support mailbox",
      "wearehereyouarehere@gmail.com" in r.text)
check("helper is the fixed script (human named)",
      "a human (Milli) will walk you through it" in r.text)
check("helper covers the 1-hour expiry",
      "Links expire in 1 hour" in r.text)
r = anon().get("/login")
check("login page links Forgot password?",
      'href="/forgot"' in r.text and "Forgot password?" in r.text)

# ---------------- A: happy path + no-enumeration ------------------
_, uid_a = signup("Rec A", "reca@example.com")
r_unknown = forgot("ghost@example.com")
check("unknown email gets the success page",
      r_unknown.status_code == 200 and SUCCESS_LINE in r_unknown.text)
check("unknown email stores NO row",
      dbq("SELECT COUNT(*) c FROM password_resets")[0]["c"] == 0)
r_known = forgot("reca@example.com")
check("known email gets the success page",
      r_known.status_code == 200 and SUCCESS_LINE in r_known.text)
check("known/unknown responses are byte-identical",
      r_known.text == r_unknown.text)
row_a = last_reset(uid_a)
check("row created for the real account", row_a is not None)
tok_a = row_a["token_plain"]
check("SMTP absent -> delivery queued", row_a["delivery"] == "queued")
check("queued row holds the raw token (owner hand-send copy)",
      bool(tok_a) and len(tok_a) >= 40)
check("token_hash is NOT the raw token",
      row_a["token_hash"] != tok_a)
check("token_hash is sha256(raw token)",
      row_a["token_hash"] == sha(tok_a))
check("requester ip recorded", row_a["requester_ip"] == "testclient")
_exp = datetime.fromisoformat(row_a["expires_at"])
_cre = datetime.fromisoformat(row_a["created_at"])
check("expiry is ~1 hour out",
      timedelta(minutes=55) < (_exp - _cre) <= timedelta(hours=1))

r = anon().get(f"/reset/{tok_a}")
check("GET /reset/{token} shows the form",
      r.status_code == 200 and "Pick a new password" in r.text)
r = anon().post(f"/reset/{tok_a}",
                data={"password": "abc12345", "confirm": "zzz99999"})
check("mismatched confirm is refused, token NOT consumed",
      r.status_code == 200 and "don&#39;t match" in r.text
      and last_reset(uid_a)["used_at"] is None)
r = anon().post(f"/reset/{tok_a}",
                data={"password": "", "confirm": ""})
check("empty password is refused, token NOT consumed",
      r.status_code == 200 and "Enter a new password." in r.text
      and last_reset(uid_a)["used_at"] is None)
r = anon().post(f"/reset/{tok_a}",
                data={"password": "newpass-A1", "confirm": "newpass-A1"})
check("good reset redirects to /login with the notice",
      r.status_code == 303
      and r.headers["location"].startswith("/login")
      and "Password%20changed" in r.headers["location"])
row_a = last_reset(uid_a)
check("token marked used + raw copy wiped",
      row_a["used_at"] is not None and row_a["token_plain"] is None)
_, r_old = login("reca@example.com", "pw123456")
check("old password is dead",
      r_old.status_code == 303 and "error" in r_old.headers["location"])
_, r_new = login("reca@example.com", "newpass-A1")
check("new password logs in",
      r_new.status_code == 303
      and r_new.headers["location"] == "/home")
r = anon().get(f"/reset/{tok_a}")
check("used token shows the dead-link page",
      r.status_code == 200 and "This link is dead" in r.text)
r = anon().post(f"/reset/{tok_a}",
                data={"password": "second-try-1",
                      "confirm": "second-try-1"})
check("token can never work twice",
      r.status_code == 200 and "This link is dead" in r.text)
_, r_still = login("reca@example.com", "newpass-A1")
check("password unchanged after reuse attempt",
      r_still.headers["location"] == "/home")
r = anon().get("/reset/definitely-not-a-token")
check("garbage token -> friendly dead page (no 404/500)",
      r.status_code == 200 and "This link is dead" in r.text)

# ---------------- B: a reset kills every other session ------------
c_b1, uid_b = signup("Rec B", "recb@example.com")
c_b2, r_b2 = login("recb@example.com", "pw123456")
check("second login works before reset", r_b2.status_code == 303)
n_sess = dbq("SELECT COUNT(*) c FROM sessions WHERE user_id=?",
             (uid_b,))[0]["c"]
check("two live sessions before reset", n_sess == 2, str(n_sess))
forgot("recb@example.com")
tok_b = last_reset(uid_b)["token_plain"]
anon().post(f"/reset/{tok_b}",
            data={"password": "newpass-B1", "confirm": "newpass-B1"})
n_sess = dbq("SELECT COUNT(*) c FROM sessions WHERE user_id=?",
             (uid_b,))[0]["c"]
check("reset deleted ALL of the user's sessions", n_sess == 0)
r = c_b1.get("/home")
check("old session cookie #1 is bounced to /login",
      r.status_code == 303
      and r.headers["location"].startswith("/login"))
r = c_b2.get("/home")
check("old session cookie #2 is bounced to /login",
      r.status_code == 303
      and r.headers["location"].startswith("/login"))

# ---------------- D: expired token ---------------------------------
_, uid_d = signup("Rec D", "recd@example.com")
forgot("recd@example.com")
row_d = last_reset(uid_d)
tok_d = row_d["token_plain"]
past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
dbx("UPDATE password_resets SET expires_at=? WHERE id=?",
    (past, row_d["id"]))
r = anon().get(f"/reset/{tok_d}")
check("expired token shows the dead-link page",
      r.status_code == 200 and "This link is dead" in r.text)
r = anon().post(f"/reset/{tok_d}",
                data={"password": "newpass-D1",
                      "confirm": "newpass-D1"})
check("expired token cannot set a password",
      r.status_code == 200 and "This link is dead" in r.text)
check("expired attempt leaves the row unused",
      last_reset(uid_d)["used_at"] is None)
_, r_d = login("recd@example.com", "pw123456")
check("old password still works after expired attempt",
      r_d.headers["location"] == "/home")

# ---------------- E: a new request supersedes the old token -------
_, uid_e = signup("Rec E", "rece@example.com")
forgot("rece@example.com")
tok_e1 = last_reset(uid_e)["token_plain"]
forgot("RECE@EXAMPLE.COM")  # case-insensitive, like signup/login
row_e2 = last_reset(uid_e)
tok_e2 = row_e2["token_plain"]
rows_e = dbq("SELECT * FROM password_resets WHERE user_id=? "
             "ORDER BY id", (uid_e,))
check("mixed-case email resolved to the same account",
      len(rows_e) == 2 and tok_e2 and tok_e2 != tok_e1)
check("superseded row is used-stamped + raw copy wiped",
      rows_e[0]["used_at"] is not None
      and rows_e[0]["token_plain"] is None)
r = anon().get(f"/reset/{tok_e1}")
check("superseded token is dead",
      r.status_code == 200 and "This link is dead" in r.text)
r = anon().get(f"/reset/{tok_e2}")
check("the newest token is the live one",
      r.status_code == 200 and "Pick a new password" in r.text)

# ---------------- F: owner queue (SMTP absent) ---------------------
_, uid_q = signup("Rec Q", "recq@example.com")
forgot("recq@example.com")
row_q = last_reset(uid_q)
tok_q = row_q["token_plain"]
r = OWN.get("/owner")
check("owner panel has the Password help section",
      r.status_code == 200 and "🔐 Password help" in r.text)
check("queue lists the waiting user + account email",
      "recq@example.com" in r.text and "Rec Q" in r.text)
check("queue shows the queued state",
      "queued — needs hand-send" in r.text)
check("stat card counts the waiting reset",
      "Password help waiting" in r.text)
r = anon().post(f"/owner/pwreset/{row_q['id']}/link")
check("anonymous cannot reveal a reset link",
      r.status_code == 303
      and r.headers["location"].startswith("/owner/login"))
c_norm, _ = login("recq@example.com", "pw123456")
r = c_norm.post(f"/owner/pwreset/{row_q['id']}/link")
check("a normal user session cannot reveal a reset link",
      r.status_code == 303
      and r.headers["location"].startswith("/owner/login"))
r = c_norm.get("/owner")
check("a normal user session cannot open the owner panel",
      r.status_code == 303
      and r.headers["location"].startswith("/owner/login"))
r = OWN.post(f"/owner/pwreset/{row_q['id']}/link")
check("owner Show-link returns the full working URL",
      r.status_code == 200
      and f"http://testserver/reset/{tok_q}" in r.text)
check("link page warns: account address only",
      "Never send it to any other address" in r.text)
r = OWN.post(f"/owner/pwreset/{row_q['id']}/hand-sent")
check("Mark hand-sent redirects back to the panel",
      r.status_code == 303 and r.headers["location"] == "/owner")
row_q2 = last_reset(uid_q)
check("hand-sent flips delivery + stamps sent_at",
      row_q2["delivery"] == "hand_sent"
      and row_q2["sent_at"] is not None)
check("hand-sent wipes the stored raw token",
      row_q2["token_plain"] is None)
r = OWN.post(f"/owner/pwreset/{row_q['id']}/link")
check("a hand-sent row has nothing left to reveal",
      r.status_code == 303 and r.headers["location"] == "/owner")
r = anon().get(f"/reset/{tok_q}")
check("hand-sent link still WORKS (validation is by hash)",
      r.status_code == 200 and "Pick a new password" in r.text)
anon().post(f"/reset/{tok_q}",
            data={"password": "newpass-Q1", "confirm": "newpass-Q1"})
_, r_q = login("recq@example.com", "newpass-Q1")
check("hand-carried reset completes end to end",
      r_q.headers["location"] == "/home")
check("no notifications or flags were written by recovery",
      dbq("SELECT COUNT(*) c FROM notifications")[0]["c"] == 0
      and dbq("SELECT COUNT(*) c FROM moderation_flags")[0]["c"] == 0)

# ---------------- SMTP present: success + failure ------------------
REAL_SMTP = smtplib.SMTP
SENT = []


class FakeSMTP:
    fail = False

    def __init__(self, host, port, timeout=None):
        if FakeSMTP.fail:
            raise OSError("connection refused (fake)")
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, user, pw):
        self.creds = (user, pw)

    def send_message(self, msg):
        SENT.append(msg)


os.environ["HERE_SMTP_USER"] = "wearehereyouarehere@gmail.com"
os.environ["HERE_SMTP_PASS"] = "fake-app-password"
smtplib.SMTP = FakeSMTP
try:
    _, uid_s = signup("Rec S", "recs@example.com")
    r = forgot("recs@example.com")
    check("SMTP on: forgot still answers with the same success page",
          r.status_code == 200 and SUCCESS_LINE in r.text)
    row_s = last_reset(uid_s)
    check("SMTP on: delivery recorded as emailed",
          row_s["delivery"] == "emailed")
    check("SMTP on: raw token NOT kept once emailed",
          row_s["token_plain"] is None)
    check("SMTP on: sent_at stamped", row_s["sent_at"] is not None)
    check("exactly one email went out", len(SENT) == 1)
    msg = SENT[0]
    check("email addressed ONLY to the account address",
          msg["To"] == "recs@example.com")
    check("email comes from the support mailbox",
          msg["From"] == "wearehereyouarehere@gmail.com")
    check("subject is the reset subject",
          msg["Subject"] == "HERE — reset your password")
    body = msg.get_body(("plain",)).get_content()
    check("body carries the ignore-if-not-you line",
          "If you didn't ask for this, ignore this email" in body)
    m = re.search(r"/reset/([A-Za-z0-9_\-]+)", body)
    check("body carries a reset link", m is not None)
    tok_s = m.group(1)
    check("emailed token matches the stored hash",
          sha(tok_s) == row_s["token_hash"])
    r = anon().get(f"/reset/{tok_s}")
    check("emailed link opens the reset form",
          r.status_code == 200 and "Pick a new password" in r.text)

    FakeSMTP.fail = True
    _, uid_f = signup("Rec F", "recf@example.com")
    r = forgot("recf@example.com")
    check("SMTP failure does NOT 500 the request",
          r.status_code == 200 and SUCCESS_LINE in r.text)
    row_f = last_reset(uid_f)
    check("SMTP failure falls back to the owner queue",
          row_f["delivery"] == "queued"
          and row_f["token_plain"] is not None)
finally:
    smtplib.SMTP = REAL_SMTP
    os.environ.pop("HERE_SMTP_USER", None)
    os.environ.pop("HERE_SMTP_PASS", None)

# ---------------- G: blocked + private account still recovers ------
_, uid_g = signup("Rec G", "recg@example.com")
_, uid_h = signup("Rec H", "rech@example.com")
dbx("UPDATE users SET private_profile=1 WHERE id=?", (uid_g,))
dbx("INSERT INTO blocks(blocker_id, blocked_id, created_at) "
    "VALUES(?,?,?)", (uid_g, uid_h, here.now_iso()))
dbx("INSERT INTO blocks(blocker_id, blocked_id, created_at) "
    "VALUES(?,?,?)", (uid_h, uid_g, here.now_iso()))
forgot("recg@example.com")
tok_g = last_reset(uid_g)["token_plain"]
r = anon().post(f"/reset/{tok_g}",
                data={"password": "newpass-G1",
                      "confirm": "newpass-G1"})
check("private + blocked-both-ways account resets fine",
      r.status_code == 303)
_, r_g = login("recg@example.com", "newpass-G1")
check("...and logs in with the new password",
      r_g.headers["location"] == "/home")

# ---------------- rate limits --------------------------------------
# Age every existing row out of the 1-hour windows so both caps are
# measured from zero.
dbx("UPDATE password_resets SET created_at='2000-01-01T00:00:00+00:00'")

_, uid_r1 = signup("Rec R1", "recr1@example.com")
for _ in range(3):
    forgot("recr1@example.com")
n = dbq("SELECT COUNT(*) c FROM password_resets WHERE user_id=?",
        (uid_r1,))[0]["c"]
check("3 requests per email per hour are allowed", n == 3, str(n))
r = forgot("recr1@example.com")
n = dbq("SELECT COUNT(*) c FROM password_resets WHERE user_id=?",
        (uid_r1,))[0]["c"]
check("the 4th request inside the hour creates NOTHING",
      n == 3, str(n))
check("...but still answers with the same success page",
      r.status_code == 200 and SUCCESS_LINE in r.text)

# Per-IP cap: 3 (above) + 3 + 3 = 9 rows this hour; the 10th lands,
# the 11th is refused.
_, uid_r2 = signup("Rec R2", "recr2@example.com")
_, uid_r3 = signup("Rec R3", "recr3@example.com")
_, uid_r4 = signup("Rec R4", "recr4@example.com")
for _ in range(3):
    forgot("recr2@example.com")
    forgot("recr3@example.com")
forgot("recr4@example.com")
n4 = dbq("SELECT COUNT(*) c FROM password_resets WHERE user_id=?",
         (uid_r4,))[0]["c"]
check("the 10th request from one IP still lands", n4 == 1, str(n4))
r = forgot("recr4@example.com")
n4 = dbq("SELECT COUNT(*) c FROM password_resets WHERE user_id=?",
         (uid_r4,))[0]["c"]
check("the 11th request from one IP creates NOTHING", n4 == 1, str(n4))
check("IP-limited answer is the same success page",
      r.status_code == 200 and SUCCESS_LINE in r.text)

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
