"""Owner money board tests (Oct 29): the dashboard ranks
members by earned tickets with cash value, bank status and
cash-out eligibility — Ken's 'who has the most money and who
is eligible to get paid' question, answered in the panel.

Run:  ./venv/bin/python tests/test_moneyboard.py
"""
import os
import secrets
import sqlite3
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-moneyboard-")
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


def signup(name, email):
    c = TestClient(here.app, follow_redirects=False)
    r = c.post("/signup", data={"name": name, "email": email,
                                "password": "pw123456"})
    assert r.status_code == 303, (name, r.status_code)
    conn = here.db()
    uid = conn.execute("SELECT id FROM users WHERE email=?",
                       (email,)).fetchone()["id"]
    conn.close()
    return c, uid


_, RICH = signup("Rita Rich", "rita@moneyboard.test")
_, MID = signup("Mike Middle", "mike@moneyboard.test")
_, ZERO = signup("Zed Zero", "zed@moneyboard.test")

conn = sqlite3.connect(os.environ["HERE_DB"])
conn.execute("UPDATE users SET gift_earned=5000, gift_lifetime=6000, "
             "payout_ready=1, creator_mode=1 WHERE id=?", (RICH,))
conn.execute("UPDATE users SET gift_earned=2600, gift_lifetime=2600, "
             "creator_mode=1 WHERE id=?", (MID,))
conn.commit()
conn.close()

tok = secrets.token_urlsafe(24)
conn = here.db()
conn.execute("INSERT INTO owner_sessions(token, created_at) "
             "VALUES(?, ?)", (tok, here.now_iso()))
conn.commit()
conn.close()
OWN = TestClient(here.app, follow_redirects=False)
OWN.cookies.set("here_owner", tok)

r = OWN.get("/owner")
rich_i = r.text.find("Rita Rich")
mid_i = r.text.find("Mike Middle")
check("money board ranks the top earner first",
      r.status_code == 200 and 0 <= rich_i < mid_i)
check("top earner shows $25 and READY — can cash out now",
      "$25" in r.text and "READY — can cash out now" in r.text)
check("$10+ without a bank shows needs-bank-link",
      "Has $10+ — needs bank link" in r.text)
check("a member with zero earned stays off the board",
      "Zed Zero" not in r.text.split("Money by member")[1]
      .split("HERE Money")[0])

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILS:", fails)
    sys.exit(1)
