"""Owner Key Card tests (Nov 4, 2026): swipe login, vault
gating, issue/revoke, and the typed-password door staying intact.

Run:  ./venv/bin/python tests/test_keycard.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-keycard-")
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


ANON = TestClient(here.app, follow_redirects=False)

# vault + keycard pages gate correctly
r = ANON.get("/owner/vault")
check("vault refuses logged-out visitors", r.status_code == 303
      and "/owner/login" in r.headers.get("location", ""))
r = ANON.get("/keycard")
check("keycard page renders for visitors", r.status_code == 200
      and "Swipe your Key Card" in r.text)
r = ANON.post("/owner/keycard/issue", data={"label": "Nope"})
check("issue refuses logged-out visitors", r.status_code == 303
      and "/owner/login" in r.headers.get("location", ""))
check("no card was issued by the refused call",
      not dbq("SELECT id FROM key_cards"))

# parser: round-trip the track strings
tracks = here._keycard_tracks("1234567890", "1111222233334444555566667777")
raw = tracks["track1"] + "\n" + tracks["track2"] + "\n" + tracks["track3"]
parsed = here._keycard_parse(raw)
check("parser reads a full swipe", parsed == (
    "1234567890", "1111222233334444555566667777"), str(parsed))
check("parser rejects junk", here._keycard_parse("hello world") is None)
check("parser rejects a half swipe",
      here._keycard_parse(tracks["track2"]) is None)

# owner (typed password session) issues a card
OWN = TestClient(here.app, follow_redirects=False)
tok = here.new_owner_session()
OWN.cookies.set(here.OWNER_COOKIE, tok)
r = OWN.post("/owner/keycard/issue", data={"label": "Wallet card"})
check("issue renders the one-time track strings", r.status_code == 200
      and "shown only once" in r.text)
row = dbq("SELECT * FROM key_cards")[0]
card_no, secret = None, None
# recover the issued values from the rendered page like Ken would
import re
m = re.search(r"Track 2</label><code>;(\d{10})=(\d{14})", r.text)
m3 = re.search(r"Track 3</label><code>;(\d{14})=", r.text)
card_no = m.group(1)
secret = m.group(2) + m3.group(1)
check("issued card matches its DB row", card_no == row["card_no"])
check("DB stores only the hash, never the secret",
      row["token_hash"] == here._keycard_hash(card_no, secret)
      and secret not in str(dict(row)))

# swipe it: build the raw swipe from the tracks
t = here._keycard_tracks(card_no, secret)
swipe = t["track1"] + t["track2"] + t["track3"]
r = ANON.post("/keycard", data={"swipe": swipe})
check("a good swipe opens the vault", r.status_code == 303
      and "/owner/vault" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location')}")
SWIPE = TestClient(here.app, follow_redirects=False)
SWIPE.cookies.set(here.OWNER_COOKIE,
                  r.headers.get("set-cookie", "").split(";")[0]
                  .split("=", 1)[1])
r = SWIPE.get("/owner/vault")
check("vault renders after a swipe", r.status_code == 200
      and "Owner Vault" in r.text)
check("last_used_at was stamped",
      dbq("SELECT last_used_at FROM key_cards WHERE id=?",
          (row["id"],))[0]["last_used_at"] is not None)

# hand-typed fallback works too
r = ANON.post("/keycard", data={"card_no": card_no, "secret": secret})
check("hand-typed card number + secret also opens",
      r.status_code == 303
      and "/owner/vault" in r.headers.get("location", ""))

# wrong secret fails, generically
bad = secret[:-1] + ("0" if secret[-1] != "0" else "1")
r = ANON.post("/keycard", data={"card_no": card_no, "secret": bad})
check("wrong secret is refused", r.status_code == 303
      and "didn" in r.headers.get("location", ""))

# revoke kills the card
OWN.post(f"/owner/keycard/{row['id']}/revoke")
r = ANON.post("/keycard", data={"swipe": swipe})
check("a revoked card no longer opens", r.status_code == 303
      and "/owner/vault" not in r.headers.get("location", ""))

# DB download door: no backup in a test env -> honest redirect msg
r = OWN.get("/owner/vault/db")
check("vault/db with no backup redirects with a message",
      r.status_code == 303 and "No database backup" in
      r.headers.get("location", "").replace("%20", " ")
      .replace("+", " "), r.headers.get("location", ""))

# the typed-password door itself is untouched by this build:
# /owner/login still renders and still rejects a wrong password
# (fresh client: ANON holds an owner cookie from the swipe above,
# and a logged-in owner is correctly bounced to /owner instead)
ANON = TestClient(here.app, follow_redirects=False)
r = ANON.get("/owner/login")
check("typed owner login page still renders", r.status_code == 200)
r = ANON.post("/owner/login", data={"password": "definitely-wrong"})
check("wrong typed password still refused", r.status_code == 303
      and "/owner/login" in r.headers.get("location", "")
      and "error" in r.headers.get("location", ""))

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
