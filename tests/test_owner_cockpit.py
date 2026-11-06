"""Owner's Cockpit tests (Panel Upgrade P1 — Nov 6, 2026):
 * /owner still refuses strangers.
 * For the owner, the dashboard renders the new digital-dash
   cockpit: time-of-day greeting for Kendrick Cooley, the gauge
   cluster, the digital readouts, and the new doors (Money Desk,
   One Inbox, Talk to Milli, Black Coin).
 * EVERY pre-redesign href / form action is still present.

Run:  ./venv/bin/python tests/test_owner_cockpit.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-cockpit-")
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


def dbx(sql, args=()):
    import sqlite3
    conn = sqlite3.connect(os.environ["HERE_DB"])
    conn.execute(sql, args)
    conn.commit()
    conn.close()


anon = TestClient(here.app, follow_redirects=False)
r = anon.get("/owner")
check("GET /owner still refuses strangers",
      r.status_code in (303, 307)
      and "/owner/login" in r.headers.get("location", ""),
      f"got {r.status_code}")

# one real user so the Users table (and its row actions) renders
dbx("INSERT INTO users(name, email, pw_hash, created_at) "
    "VALUES(?,?,?,?)",
    ("Dash Member", "dash@example.com", "x", here.now_iso()))

dbx("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
    ("cockpit-owner-token", here.now_iso()))
own = TestClient(here.app, follow_redirects=False)
own.cookies.set("here_owner", "cockpit-owner-token")
r = own.get("/owner")
check("GET /owner renders for the owner", r.status_code == 200,
      f"got {r.status_code}")
body = r.text

check("cockpit greeting names Kendrick Cooley",
      "Kendrick Cooley" in body)
check("cockpit greeting is time-of-day aware",
      any(g in body for g in ("Good morning", "Good afternoon",
                              "Good evening", "Good night",
                              "Up late")))
check("cockpit welcome line present",
      "Owner's Cockpit" in body and "your empire is live" in body)
check("gauge cluster present with real values",
      'class="gauge"' in body and 'data-value="1"' in body
      and "Members" in body and "Tickets sold" in body)
check("digital readouts present",
      "HERE profit (all time)" in body
      and "Owed to streamers if all cashed out" in body
      and "Money in today" in body)
check("new doors present",
      'href="/owner/money"' in body and 'href="/owner/inbox"' in body
      and 'href="/owner/milli"' in body and 'href="/blackcoin"' in body)
check("money readouts are doors into the Money Desk",
      'href="/owner/money?range=today"' in body
      and 'href="/owner/money?range=all"' in body)

# --- every pre-redesign static href survives ---
for href in ("/owner/race", "/owner/suggestions", "/owner/match",
             "/owner/idm", "/owner/artists", "/owner/logout",
             "/owner/ads/new"):
    check(f"legacy href kept: {href}", f'href="{href}"' in body)

# --- every form action family survives (row actions need rows;
# the seeded user guarantees the Users-table actions render) ---
for action_bit in ("/owner/users/", "/badge/vuser", "/badge/vvendor",
                   "/idm", "/owner/user/", "/remove"):
    check(f"legacy action kept: {action_bit}", action_bit in body)
for section in ("Invite Race", "Money by member", "HERE Money",
                "Payouts", "Password help", "Live Now",
                "Verification Requests", "Flagged Content Queue",
                "Users", "Pages"):
    check(f"legacy section kept: {section}", section in body)

fails = [n for n, okk in RESULTS if not okk]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
