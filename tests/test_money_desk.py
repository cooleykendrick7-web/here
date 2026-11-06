"""Money Desk tests (Panel Upgrade P3 — Nov 6, 2026):
 * /owner/money and /owner/money.csv are owner-gated.
 * Range filters (presets + explicit from/to) select exactly the
   right 'buy' ledger rows; gross matches the pack-price rebuild
   the dashboard uses (pack100 $0.99, pack500 $4.99, custom 250 =
   $2.50).
 * Cash-out aggregates + the owed-right-now total read the real
   payout_requests / users ledgers.

Run:  ./venv/bin/python tests/test_money_desk.py
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-money-")
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


def ago(days, hours=0):
    return (datetime.now(timezone.utc)
            - timedelta(days=days, hours=hours)).isoformat()


buyer = x("INSERT INTO users(name, email, pw_hash, created_at, "
          "gift_earned) VALUES(?,?,?,?,?)",
          ("Money Mae", "mae@x.io", "x", ago(60), 4000))
other = x("INSERT INTO users(name, email, pw_hash, created_at) "
          "VALUES(?,?,?,?)", ("Second Sam", "sam@x.io", "x", ago(60)))

# three sales: today (pack100), 3 days ago (pack500), 40 days ago
# (custom 250)
x("INSERT INTO ticket_txns(user_id, kind, tickets, stripe_session, "
  "created_at) VALUES(?,?,?,?,?)",
  (buyer, "buy", 100, "cs_today", ago(0, 1)))
x("INSERT INTO ticket_txns(user_id, kind, tickets, stripe_session, "
  "created_at) VALUES(?,?,?,?,?)",
  (buyer, "buy", 500, "cs_3d", ago(3)))
x("INSERT INTO ticket_txns(user_id, kind, tickets, stripe_session, "
  "created_at) VALUES(?,?,?,?,?)",
  (other, "buy", 250, "cs_40d", ago(40)))
# noise that must never count as a sale
x("INSERT INTO ticket_txns(user_id, kind, tickets, created_at) "
  "VALUES(?,?,?,?)", (buyer, "gift_send", -100, ago(0, 2)))

# cash-outs: one paid (decided today), one pending from 2 days ago
x("INSERT INTO payout_requests(user_id, tickets, dollars_cents, "
  "status, created_at, decided_at) VALUES(?,?,?,?,?,?)",
  (buyer, 2000, 1000, "paid", ago(1), ago(0, 3)))
x("INSERT INTO payout_requests(user_id, tickets, dollars_cents, "
  "status, created_at) VALUES(?,?,?,?,?)",
  (buyer, 2000, 1000, "pending", ago(2)))

anon = TestClient(here.app, follow_redirects=False)
r = anon.get("/owner/money")
check("stranger refused at /owner/money",
      r.status_code in (303, 307)
      and "/owner/login" in r.headers.get("location", ""))
r = anon.get("/owner/money.csv?range=all")
check("stranger refused at /owner/money.csv",
      r.status_code in (303, 307)
      and "/owner/login" in r.headers.get("location", ""))

x("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
  ("money-owner-token", here.now_iso()))
own = TestClient(here.app, follow_redirects=False)
own.cookies.set("here_owner", "money-owner-token")

r = own.get("/owner/money?range=all")
check("Money Desk renders (all time)", r.status_code == 200,
      f"got {r.status_code}")
check("all-time gross = $8.48 (99+499+250 cents)",
      "$8.48" in r.text)
check("all-time paid out = $10.00", "$10.00" in r.text)
check("per-sale rows list the buyers + sessions",
      "Money Mae" in r.text and "Second Sam" in r.text
      and "cs_today" in r.text and "cs_40d" in r.text)
# owed right now: Mae's 4000 earned tickets = $20.00 + the pending
# cash-out's locked $10.00 = $30.00
check("owed right now = $30.00", "$30.00" in r.text)

r = own.get("/owner/money?range=today")
check("today range shows only today's $0.99 sale",
      "$0.99" in r.text and "cs_3d" not in r.text
      and "cs_40d" not in r.text)
r = own.get("/owner/money?range=7d")
check("7d range includes 3-day sale, excludes 40-day sale",
      "cs_3d" in r.text and "cs_40d" not in r.text
      and "$5.98" in r.text)
r = own.get("/owner/money?range=30d")
check("30d range still excludes the 40-day sale",
      "cs_40d" not in r.text and "cs_3d" in r.text)

# explicit backdate: a window covering only ~3 days ago (UTC dates
# are within a day of Chicago dates at this distance — use a
# 2..4-day-ago window so the pack500 sale is the only one inside)
d4 = (datetime.now(timezone.utc) - timedelta(days=4)).date()
d2 = (datetime.now(timezone.utc) - timedelta(days=2)).date()
r = own.get(f"/owner/money?from={d4}&to={d2}")
check("explicit from/to isolates the pack500 sale",
      "cs_3d" in r.text and "cs_today" not in r.text
      and "cs_40d" not in r.text and "$4.99" in r.text)

r = own.get("/owner/money.csv?range=all")
check("CSV downloads", r.status_code == 200
      and "text/csv" in r.headers.get("content-type", ""))
lines = [ln for ln in r.text.splitlines() if ln.strip()]
check("CSV has header + exactly the 3 sales",
      lines[0].startswith("created_at,buyer,tickets")
      and len(lines) == 4, f"lines={len(lines)}")
check("CSV rows carry buyer, amount, session",
      "Money Mae,100,0.99,cs_today" in r.text
      and "Second Sam,250,2.50,cs_40d" in r.text)
r = own.get("/owner/money.csv?range=today")
lines = [ln for ln in r.text.splitlines() if ln.strip()]
check("CSV respects the range (today = 1 sale)", len(lines) == 2)

fails = [n for n, okk in RESULTS if not okk]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
