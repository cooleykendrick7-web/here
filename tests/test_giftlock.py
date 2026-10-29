"""Gift dead-end tests (Oct 29): a Page/profile whose owner is
not a creator used to show NO gift button at all. Now the
button always shows; the locked sheet offers a nudge that
notifies the owner once a day per sender.

Run:  ./venv/bin/python tests/test_giftlock.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-giftlock-")
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
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def signup(name, email):
    c = TestClient(here.app, follow_redirects=False)
    r = c.post("/signup", data={"name": name, "email": email,
                                "password": "pw123456"})
    assert r.status_code == 303, (name, r.status_code)
    uid = dbq("SELECT id FROM users WHERE email=?",
              (email,))[0]["id"]
    return c, uid


Pc, PLAIN = signup("Paul Plain", "paul@giftlock.test")
Fc, FAN = signup("Fran Fan", "fran@giftlock.test")
Cc, CRE = signup("Cory Creator", "cory@giftlock.test")
Cc.post("/settings/creator", data={"creator_mode": "1"})
Pc.post("/pages", data={"name": "Paul's Page", "ptype": "community"})
Cc.post("/pages", data={"name": "Cory's Page", "ptype": "community"})
plain_pid = dbq("SELECT id FROM pages WHERE owner_id=?",
                (PLAIN,))[0]["id"]
cre_pid = dbq("SELECT id FROM pages WHERE owner_id=?",
              (CRE,))[0]["id"]

r = Fc.get(f"/page/{plain_pid}")
check("non-creator Page now SHOWS a gift button (locked sheet)",
      r.status_code == 200 and 'id="glock-open"' in r.text
      and "Creator Mode" in r.text)
r = Fc.get(f"/page/{cre_pid}")
check("creator Page keeps the real gift sheet",
      'id="pgift-open"' in r.text and 'id="glock-open"' not in r.text)
r = Fc.get(f"/user/{PLAIN}")
check("non-creator PROFILE shows the locked gift button too",
      'id="glock-open"' in r.text)

r = Fc.post("/gift-nudge", json={"kind": "page", "id": plain_pid})
notes = dbq("SELECT text FROM notifications WHERE user_id=? "
            "AND kind='gift_nudge'", (PLAIN,))
check("nudge lands in the owner's notifications",
      r.status_code == 200 and r.json().get("ok")
      and len(notes) == 1 and "Creator Mode" in notes[0]["text"],
      str(r.text)[:120])
r = Fc.post("/gift-nudge", json={"kind": "page", "id": plain_pid})
notes = dbq("SELECT text FROM notifications WHERE user_id=? "
            "AND kind='gift_nudge'", (PLAIN,))
check("a second nudge the same day does not spam",
      r.json().get("already") and len(notes) == 1)
r = Fc.post("/gift-nudge", json={"kind": "profile", "id": PLAIN})
notes = dbq("SELECT text FROM notifications WHERE user_id=? "
            "AND kind='gift_nudge'", (PLAIN,))
check("profile targets nudge too (separate target)",
      r.json().get("ok") and len(notes) == 2)
r = Pc.post("/gift-nudge", json={"kind": "page", "id": plain_pid})
check("you cannot nudge yourself", r.status_code == 400)
r = Fc.post("/gift-nudge", json={"kind": "page", "id": cre_pid})
check("nudging a creator is a friendly no-op",
      r.json().get("already_creator") is True)

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILS:", fails)
    sys.exit(1)
