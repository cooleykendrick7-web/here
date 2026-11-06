"""Probe Janitor tests (Panel Upgrade P2 — Nov 6, 2026):
 * Signup with a probe_code matching HERE_PROBE_CODE stamps the
   account is_probe=1; without it (or a wrong code) it stays 0.
 * _janitor_sweep deletes an old, clean probe through the same
   cascade (its content goes too) and logs the deletion.
 * A probe holding tickets, or with a payout row, or with messages
   to a real user, is FROZEN (skipped_guard) — never auto-deleted.
 * A young probe and any normal user are untouched.
 * Owner probe toggle + /owner/janitor are owner-gated.

Run:  ./venv/bin/python tests/test_janitor.py
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-janitor-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
os.environ["HERE_PROBE_CODE"] = "sweep-secret-123"
sys.path.insert(0, BASE)

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{extra}]" if extra and not cond else ""))


def q(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"])
    conn.row_factory = sqlite3.Row
    r = conn.execute(sql, args).fetchall()
    conn.close()
    return r


def x(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"])
    cur = conn.execute(sql, args)
    conn.commit()
    last = cur.lastrowid
    conn.close()
    return last


def ago(minutes):
    return (datetime.now(timezone.utc)
            - timedelta(minutes=minutes)).isoformat()


def mkuser(name, email, minutes_old=30, probe=0, bal=0, earn=0):
    return x("INSERT INTO users(name, email, pw_hash, created_at, "
             "is_probe, tickets_balance, gift_earned) "
             "VALUES(?,?,?,?,?,?,?)",
             (name, email, "x", ago(minutes_old), probe, bal, earn))


# ---------- signup stamping ----------
c = TestClient(here.app, follow_redirects=False)
r = c.post("/signup", data={"name": "Probe Pat", "email": "pat@x.io",
                            "password": "longenough1",
                            "probe_code": "sweep-secret-123"})
check("signup with probe_code redirects (account created)",
      r.status_code in (303, 307), f"got {r.status_code}")
row = q("SELECT is_probe FROM users WHERE email='pat@x.io'")
check("probe_code signup is stamped is_probe=1",
      row and row[0]["is_probe"] == 1)

c2 = TestClient(here.app, follow_redirects=False)
c2.post("/signup", data={"name": "Real Rita", "email": "rita@x.io",
                         "password": "longenough1"})
row = q("SELECT is_probe FROM users WHERE email='rita@x.io'")
check("plain signup stays is_probe=0", row and row[0]["is_probe"] == 0)

c3 = TestClient(here.app, follow_redirects=False)
c3.post("/signup", data={"name": "Wrong Code", "email": "wc@x.io",
                         "password": "longenough1",
                         "probe_code": "nope"})
row = q("SELECT is_probe FROM users WHERE email='wc@x.io'")
check("wrong probe_code does NOT stamp", row and row[0]["is_probe"] == 0)

# ---------- sweep fixtures ----------
real = mkuser("Real Member", "real@x.io", probe=0)
clean_probe = mkuser("Clean Probe", "clean@x.io", probe=1)
rich_probe = mkuser("Rich Probe", "rich@x.io", probe=1, earn=50)
paid_probe = mkuser("Paid Probe", "paid@x.io", probe=1)
chat_probe = mkuser("Chat Probe", "chat@x.io", probe=1)
young_probe = mkuser("Young Probe", "young@x.io", minutes_old=2,
                     probe=1)
# clean probe owns content — the cascade must take it too
x("INSERT INTO posts(user_id, body, created_at) VALUES(?,?,?)",
  (clean_probe, "probe post", ago(30)))
# paid probe has a cash-out row
x("INSERT INTO payout_requests(user_id, tickets, dollars_cents, "
  "status, created_at) VALUES(?,?,?,?,?)",
  (paid_probe, 2000, 1000, "pending", ago(30)))
# chat probe messaged a REAL user
x("INSERT INTO messages(sender_id, receiver_id, body, created_at) "
  "VALUES(?,?,?,?)", (chat_probe, real, "hi human", ago(30)))
# and a probe<->probe message for the clean probe (must NOT guard)
buddy = mkuser("Buddy Probe", "buddy@x.io", minutes_old=30, probe=1)
x("INSERT INTO messages(sender_id, receiver_id, body, created_at) "
  "VALUES(?,?,?,?)", (clean_probe, buddy, "probe talk", ago(30)))

deleted, skipped = here._janitor_sweep()
check("sweep deletes the two clean old probes (clean + buddy)",
      deleted == 2, f"deleted={deleted}")
check("sweep skips the three guarded probes", skipped == 3,
      f"skipped={skipped}")
check("clean probe row is gone",
      not q("SELECT id FROM users WHERE id=?", (clean_probe,)))
check("clean probe's post was cascaded away",
      not q("SELECT id FROM posts WHERE user_id=?", (clean_probe,)))
check("rich probe (holds earned tickets) survives",
      bool(q("SELECT id FROM users WHERE id=?", (rich_probe,))))
check("paid probe (payout row) survives",
      bool(q("SELECT id FROM users WHERE id=?", (paid_probe,))))
check("chat probe (messages with a real user) survives",
      bool(q("SELECT id FROM users WHERE id=?", (chat_probe,))))
check("young probe survives (under 15 minutes)",
      bool(q("SELECT id FROM users WHERE id=?", (young_probe,))))
check("real user untouched",
      bool(q("SELECT id FROM users WHERE id=?", (real,))))
logs = q("SELECT * FROM janitor_log ORDER BY id")
check("janitor logged the deletions",
      sum(1 for l in logs if l["action"] == "deleted") == 2)
check("janitor logged the guard skips with reasons",
      sum(1 for l in logs if l["action"] == "skipped_guard") == 3
      and all(l["detail"] for l in logs
              if l["action"] == "skipped_guard"))
# second pass must not duplicate skip logs
here._janitor_sweep()
logs2 = q("SELECT * FROM janitor_log WHERE action='skipped_guard'")
check("repeat sweep does not spam skip logs", len(logs2) == 3)

# ---------- owner toggle + janitor page ----------
anon = TestClient(here.app, follow_redirects=False)
r = anon.post(f"/owner/users/{real}/probe")
check("stranger cannot toggle probe stamp",
      r.status_code in (303, 307)
      and "/owner/login" in r.headers.get("location", ""))
r = anon.get("/owner/janitor")
check("stranger cannot open /owner/janitor",
      r.status_code in (303, 307)
      and "/owner/login" in r.headers.get("location", ""))

x("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
  ("janitor-owner-token", here.now_iso()))
own = TestClient(here.app, follow_redirects=False)
own.cookies.set("here_owner", "janitor-owner-token")
r = own.post(f"/owner/users/{real}/probe")
check("owner toggle stamps the user",
      q("SELECT is_probe FROM users WHERE id=?",
        (real,))[0]["is_probe"] == 1)
r = own.post(f"/owner/users/{real}/probe")
check("owner toggle un-stamps the user",
      q("SELECT is_probe FROM users WHERE id=?",
        (real,))[0]["is_probe"] == 0)
r = own.get("/owner/janitor")
check("/owner/janitor renders for the owner with the log",
      r.status_code == 200 and "Probe Janitor" in r.text
      and "skipped_guard" in r.text and "Rich Probe" in r.text)

fails = [n for n, okk in RESULTS if not okk]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
