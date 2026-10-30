"""Front-door deterrent fixes: /founders + /lives public for
strangers, the login peek strip, and the Wall of Paid's
money-in-motion empty state with true aggregates.

Run:  ./venv/bin/python tests/test_frontdoor.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-frontdoor-")
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
    conn = here.db()
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    conn.close()
    return rows


def dbx(sql, args=()):
    conn = here.db()
    conn.execute(sql, args)
    conn.commit()
    conn.close()


ANON = TestClient(here.app, follow_redirects=False)
C = TestClient(here.app, follow_redirects=False)
C.post("/signup", data={"name": "First Founder",
                        "email": "first@front.test",
                        "password": "pw123456"})
uid = dbq("SELECT id FROM users WHERE email='first@front.test'"
          )[0]["id"]

# ---- public windows ----
r = ANON.get("/founders")
check("strangers can see the Original 100 wall",
      r.status_code == 200 and "First Founder" in r.text)
r = ANON.get("/lives")
check("strangers can see who's live", r.status_code == 200)
r = ANON.get("/live/999999")
check("watching itself still passes the gate",
      r.status_code in (200, 302, 303)
      and ("/login" in (r.headers.get("location") or "")
           or "Create your free profile" in r.text))

# ---- login peek strip ----
r = ANON.get("/login")
i_form = r.text.find('action="/login"')
i_peek = r.text.find("Look around first")
check("login form still comes first", 0 < i_form < i_peek)
check("peek strip links the public windows",
      all(x in r.text for x in
          ("/lives", "/artists", "/paidwall", "/founders")))

# ---- paidwall money in motion ----
dbx("INSERT INTO ticket_txns(user_id, kind, tickets, "
    "stripe_session, created_at) VALUES(?, 'buy', 6100, "
    "'sess_test', '2026-10-29T00:00:00')", (uid,))
dbx("UPDATE users SET gift_earned=2600 WHERE id=?", (uid,))
r = ANON.get("/paidwall")
check("empty wall shows tickets sold", "6,100" in r.text
      or "6100" in r.text)
check("empty wall shows fan dollars in",
      "· $61 in fan money" in r.text, r.text[:400])
check("empty wall shows creator bank", ">$13<" in r.text)

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL GREEN")
