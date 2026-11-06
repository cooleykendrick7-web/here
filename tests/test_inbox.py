"""One Inbox tests (Panel Upgrade P4 — Nov 6, 2026):
 * /owner/inbox is owner-gated.
 * It aggregates exactly the PENDING rows of every real queue
   (verification requests, flags, cash-outs, IDM applications,
   new suggestions, Match face checks) with correct counts, and
   none of the already-decided noise.
 * Every section deep-links to the existing decision page.

Run:  ./venv/bin/python tests/test_inbox.py
"""
import os
import sqlite3
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-inbox-")
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


def x(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"])
    cur = conn.execute(sql, args)
    conn.commit()
    last = cur.lastrowid
    conn.close()
    return last


now = here.now_iso()
u1 = x("INSERT INTO users(name, email, pw_hash, created_at) "
       "VALUES(?,?,?,?)", ("Inbox Ivy", "ivy@x.io", "x", now))
u2 = x("INSERT INTO users(name, email, pw_hash, created_at) "
       "VALUES(?,?,?,?)", ("Queue Quincy", "q@x.io", "x", now))

# one PENDING item per queue
x("INSERT INTO verification_requests(kind, target_id, requester_id, "
  "status, created_at) VALUES(?,?,?,?,?)",
  ("user", u1, u1, "pending", now))
x("INSERT INTO moderation_flags(item_type, item_id, author_id, "
  "reasons, excerpt, status, created_at) VALUES(?,?,?,?,?,?,?)",
  ("post", 42, u2, "spam-suspect", "BUY FOLLOWERS CHEAP", "pending",
   now))
x("INSERT INTO payout_requests(user_id, tickets, dollars_cents, "
  "status, created_at) VALUES(?,?,?,?,?)",
  (u1, 2000, 1000, "pending", now))
x("INSERT INTO idm_requests(user_id, note, status, created_at) "
  "VALUES(?,?,?,?)", (u2, "I test everything", "pending", now))
x("INSERT INTO app_suggestions(user_id, text, status, created_at) "
  "VALUES(?,?,?,?)",
  (u1, "Add a karaoke night mode please", "new", now))
x("INSERT INTO match_profiles(user_id, face_status, city, state, "
  "created_at, updated_at) VALUES(?,?,?,?,?,?)",
  (u2, "pending", "Bismarck", "ND", now, now))

# decided/noise rows that must NOT appear
x("INSERT INTO verification_requests(kind, target_id, requester_id, "
  "status, created_at) VALUES(?,?,?,?,?)",
  ("user", u2, u2, "approved", now))
x("INSERT INTO moderation_flags(item_type, item_id, author_id, "
  "reasons, excerpt, status, created_at) VALUES(?,?,?,?,?,?,?)",
  ("post", 43, u1, "old-noise", "DISMISSED NOISE TEXT", "dismissed",
   now))
x("INSERT INTO payout_requests(user_id, tickets, dollars_cents, "
  "status, created_at) VALUES(?,?,?,?,?)",
  (u2, 2000, 1000, "paid", now))
x("INSERT INTO idm_requests(user_id, note, status, created_at) "
  "VALUES(?,?,?,?)", (u1, "already decided", "approved", now))
x("INSERT INTO app_suggestions(user_id, text, status, created_at) "
  "VALUES(?,?,?,?)", (u2, "OLD DECIDED SUGGESTION", "done", now))
x("INSERT INTO match_profiles(user_id, face_status, created_at, "
  "updated_at) VALUES(?,?,?,?)", (u1, "verified", now, now))

anon = TestClient(here.app, follow_redirects=False)
r = anon.get("/owner/inbox")
check("stranger refused at /owner/inbox",
      r.status_code in (303, 307)
      and "/owner/login" in r.headers.get("location", ""))

x("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
  ("inbox-owner-token", here.now_iso()))
own = TestClient(here.app, follow_redirects=False)
own.cookies.set("here_owner", "inbox-owner-token")
r = own.get("/owner/inbox")
check("One Inbox renders for the owner", r.status_code == 200,
      f"got {r.status_code}")
body = r.text
check("header total = 6 waiting", "6 things waiting" in body)
check("verification pending listed (target + requester)",
      "Inbox Ivy" in body and "Verification requests" in body)
check("flag pending listed with excerpt",
      "BUY FOLLOWERS CHEAP" in body and "Queue Quincy" in body)
check("cash-out pending listed with dollars",
      "$10.00" in body and "Cash-outs waiting" in body)
check("IDM application listed with note",
      "I test everything" in body and "IDM applications" in body)
check("new suggestion listed",
      "karaoke night mode" in body and "New suggestions" in body)
check("match face pending listed with city",
      "Bismarck" in body and "face checks" in body)
check("decided noise is NOT listed",
      "DISMISSED NOISE TEXT" not in body
      and "OLD DECIDED SUGGESTION" not in body
      and "already decided" not in body)
for door in ('href="/owner/idm"', 'href="/owner/suggestions"',
             'href="/owner/match"'):
    check(f"decision door present: {door}", door in body)

fails = [n for n, okk in RESULTS if not okk]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
