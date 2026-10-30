"""The founder card: the Original #1 profile carries the
designed FOUNDER & OWNER treatment (role line, Lobby host
invite, anti-impersonation line); no other profile does.

Run:  ./venv/bin/python tests/test_founder_card.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-founder-")
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


A = TestClient(here.app, follow_redirects=False)
A.post("/signup", data={"name": "Seat One",
                        "email": "one@founder.test",
                        "password": "pw123456"})
B = TestClient(here.app, follow_redirects=False)
B.post("/signup", data={"name": "Seat Two",
                        "email": "two@founder.test",
                        "password": "pw123456"})
one = dbq("SELECT id, founding_no FROM users "
          "WHERE email='one@founder.test'")[0]
two = dbq("SELECT id, founding_no FROM users "
          "WHERE email='two@founder.test'")[0]
check("first signup holds seat #1", one["founding_no"] == 1,
      str(one))

r = B.get(f"/user/{one['id']}")
check("founder card renders on seat #1's profile",
      r.status_code == 200 and "FOUNDER &amp; OWNER OF HERE"
      in r.text or "FOUNDER & OWNER OF HERE" in r.text)
check("card carries the Lobby invite",
      "The Lobby" in r.text and 'href="/lobby"' in r.text)
check("card carries the anti-impersonation line",
      "never ask for your password" in r.text)

r = A.get(f"/user/{two['id']}")
check("no founder card on a regular profile",
      "FOUNDER &" not in r.text
      and "never ask for your password" not in r.text)

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL GREEN")
