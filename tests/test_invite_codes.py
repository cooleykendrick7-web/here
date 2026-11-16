"""Custom invite codes ("Got My 10") tests (Nov 15, 2026) —
Ken's order: members make their OWN invitation code; signups
type it in to credit the inviter; at 10 QUALIFIED joins the code
retires itself, pays 1,000 earned tickets + the Got My 10 badge,
and frees the words for reuse.

Covered:
- create a code (normalized upper), tracker page shows it;
- invalid formats, reserved words rejected (check endpoint);
- an ACTIVE code is unavailable to everyone else;
- impersonation guard: the owner account's name (user id 3) and
  a verified member's name cannot be claimed as codes;
- one active code per member;
- signup with a valid code: invited_by stamped, use row written,
  owner notified (kind 'invite', link '/my-code');
- signup with an unknown code still succeeds, no credit;
- a typed code beats the invite LINK (never double-credit), and
  the link alone still credits like before;
- a joiner counts only after a real action (a post);
- driving a code to 10 qualified joiners completes it: status,
  gift_earned +1000, ticket_txns kind 'invite_code_reward',
  completion notification, badge on the profile;
- a completed code takes no new signups, and its words can be
  claimed by someone else; retiring frees words immediately.

Run:  ./venv/bin/python tests/test_invite_codes.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-invitecodes-")
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


def dbx(sql, args=()):
    conn = here.db()
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def signup(name, email, extra=None, cookies=None):
    c = TestClient(here.app, follow_redirects=False)
    if cookies:
        for k, v in cookies.items():
            c.cookies.set(k, v)
    data = {"name": name, "email": email, "password": "pw123456"}
    if extra:
        data.update(extra)
    r = c.post("/signup", data=data)
    assert r.status_code == 303, (name, r.status_code,
                                  r.headers.get("location"))
    uid = dbq("SELECT id FROM users WHERE email=?",
              (email,))[0]["id"]
    return c, uid


def post_once(client, body="hello from a real joiner"):
    r = client.post("/posts", data={"kind": "text", "body": body,
                                    "privacy": "public"})
    assert r.status_code in (200, 303), r.status_code


def code_row(uid):
    rows = dbq("SELECT * FROM invite_codes WHERE user_id=? "
               "ORDER BY id DESC", (uid,))
    return rows[0] if rows else None


def check_ep(client, code):
    r = client.post("/invite-code/check", data={"code": code})
    assert r.status_code == 200, r.status_code
    return r.json()


# ---------------- phase 1: creation rules ----------------------
A, a_id = signup("Alice Owner", "alice@example.com")
r = A.post("/invite-code", data={"code": "alpha10"})
check("create code redirects ok",
      r.status_code == 303 and "ok=" in r.headers.get("location", ""),
      r.headers.get("location", ""))
row = code_row(a_id)
check("code stored active + normalized",
      row and row["status"] == "active" and row["code"] == "ALPHA10"
      and row["code_norm"] == "ALPHA10")
r = A.get("/my-code")
check("tracker shows code + 0 of 10",
      r.status_code == 200 and "ALPHA10" in r.text
      and "0 of 10" in r.text)
r = TestClient(here.app, follow_redirects=False).get("/my-code")
check("my-code requires login", r.status_code in (303, 401))

B, b_id = signup("Bee Member", "bee@example.com")
U3, u3_id = signup("Third Member", "third@example.com")
# The impersonation guard protects user id 3 (the owner account)
# by NAME, plus every verified member's name. Seed rows shift the
# ids real signups land on, so stand the two guard targets up
# explicitly: id 3 is an UNVERIFIED "Zebra One" (only the id-3
# rule can reject ZEBRAONE), and A becomes the verified member
# "Cool Cat" (only the verified rule can reject COOLCAT).
dbx("UPDATE users SET name='Zebra One', is_verified=0, "
    "is_verified_user=0, is_verified_vendor=0 WHERE id=3")
dbx("UPDATE users SET name='Cool Cat', is_verified_user=1 "
    "WHERE id=?", (a_id,))
check("guard targets in place",
      dbq("SELECT name FROM users WHERE id=3")[0]["name"]
      == "Zebra One")

j = check_ep(B, "ADMIN")
check("reserved word rejected", not j["available"]
      and "reserved" in j["reason"], str(j))
j = check_ep(B, "ab")
check("too-short code rejected", not j["available"], str(j))
j = check_ep(B, "HAS SPACE")
check("code with space rejected", not j["available"], str(j))
j = check_ep(B, "TOOLONGCODE123456")
check("too-long code rejected", not j["available"], str(j))
j = check_ep(B, "ALPHA10")
check("active code unavailable to others", not j["available"]
      and "Unavailable" in j["reason"], str(j))
j = check_ep(B, "COOLCAT")
check("verified member's name unclaimable", not j["available"],
      str(j))
j = check_ep(B, "ZEBRAONE")
check("owner account's name unclaimable", not j["available"],
      str(j))
j = check_ep(B, "FRESH99")
check("fresh word available", j["available"], str(j))

r = B.post("/invite-code", data={"code": "ALPHA10"})
check("create of a running code fails",
      r.status_code == 303 and "error=" in r.headers.get("location", "")
      and code_row(b_id) is None, r.headers.get("location", ""))

r = A.post("/invite-code", data={"code": "SECOND1"})
check("one active code per member",
      r.status_code == 303 and "error=" in r.headers.get("location", "")
      and len(dbq("SELECT * FROM invite_codes WHERE user_id=? "
                  "AND status='active'", (a_id,))) == 1)

# ---------------- phase 2: signup credit -----------------------
J1, j1_id = signup("Joiner One", "j1@example.com",
                   extra={"invite_code": "alpha10"})
u = dbq("SELECT invited_by FROM users WHERE id=?", (j1_id,))[0]
check("code signup stamps invited_by", u["invited_by"] == a_id)
uses = dbq("SELECT * FROM invite_code_uses WHERE user_id=?",
           (j1_id,))
check("code use row written",
      len(uses) == 1 and uses[0]["code_id"] == row["id"])
notes = dbq("SELECT * FROM notifications WHERE user_id=? "
            "AND kind='invite' AND link='/my-code'", (a_id,))
check("owner notified of code join", len(notes) >= 1)

J2, j2_id = signup("Joiner Two", "j2@example.com",
                   extra={"invite_code": "NOSUCH99"})
u = dbq("SELECT invited_by FROM users WHERE id=?", (j2_id,))[0]
check("unknown code: signup ok, no credit",
      u["invited_by"] is None
      and not dbq("SELECT * FROM invite_code_uses WHERE user_id=?",
                  (j2_id,)))

b_link = dbq("SELECT invite_code FROM users WHERE id=?",
             (b_id,))[0]["invite_code"]
J3, j3_id = signup("Joiner Three", "j3@example.com",
                   extra={"invite_code": "ALPHA10"},
                   cookies={"here_invite": b_link})
u = dbq("SELECT invited_by FROM users WHERE id=?", (j3_id,))[0]
check("typed code beats link (no double credit)",
      u["invited_by"] == a_id, str(u["invited_by"]))
J4, j4_id = signup("Joiner Four", "j4@example.com",
                   cookies={"here_invite": b_link})
u = dbq("SELECT invited_by FROM users WHERE id=?", (j4_id,))[0]
check("link alone still credits", u["invited_by"] == b_id)

r = A.get("/my-code")
check("joiners listed, none counted yet",
      "Joiner One" in r.text and "0 of 10" in r.text
      and "not active yet" in r.text)
post_once(J1)
r = A.get("/my-code")
check("joiner counts after a real post",
      "1 of 10" in r.text and "✓ counted" in r.text)

# ---------------- phase 3: completion --------------------------
C, c_id = signup("Club Owner", "club@example.com")
r = C.post("/invite-code", data={"code": "TENCLUB"})
check("second owner creates TENCLUB",
      r.status_code == 303 and "ok=" in r.headers.get("location", ""))
c_code = code_row(c_id)
before = dbq("SELECT gift_earned FROM users WHERE id=?",
             (c_id,))[0]["gift_earned"]
dbx("DELETE FROM auth_attempts")  # test-only: reset signup limit
for i in range(1, 11):
    jc, jid = signup(f"Ten Joiner {i}", f"ten{i}@example.com",
                     extra={"invite_code": "TENCLUB"})
    post_once(jc, f"ten joiner {i} checking in")
r = C.get("/my-code")
crow = dbq("SELECT * FROM invite_codes WHERE id=?",
           (c_code["id"],))[0]
check("code completes at 10 qualified",
      crow["status"] == "completed" and crow["completed_at"],
      crow["status"])
after = dbq("SELECT gift_earned FROM users WHERE id=?",
            (c_id,))[0]["gift_earned"]
check("owner paid 1,000 earned tickets", after - before == 1000,
      f"{before} -> {after}")
tx = dbq("SELECT * FROM ticket_txns WHERE user_id=? "
         "AND kind='invite_code_reward'", (c_id,))
check("reward txn row written",
      len(tx) == 1 and tx[0]["tickets"] == 1000)
notes = dbq("SELECT * FROM notifications WHERE user_id=? "
            "AND kind='invite_code_done'", (c_id,))
check("owner notified of completion", len(notes) == 1)
r = C.get(f"/user/{c_id}")
check("Got My 10 badge on profile", "Got My 10" in r.text)
r = A.get(f"/user/{a_id}")
check("no badge without a completion", "Got My 10" not in r.text)

JX, jx_id = signup("Late Joiner", "late@example.com",
                   extra={"invite_code": "TENCLUB"})
u = dbq("SELECT invited_by FROM users WHERE id=?", (jx_id,))[0]
check("completed code takes no new signups",
      u["invited_by"] is None)

D, d_id = signup("Dee Next", "dee@example.com")
r = D.post("/invite-code", data={"code": "TENCLUB"})
check("completed words can be claimed again",
      r.status_code == 303 and "ok=" in r.headers.get("location", "")
      and code_row(d_id)["status"] == "active",
      r.headers.get("location", ""))
r = D.post("/invite-code/retire")
drow = dbq("SELECT * FROM invite_codes WHERE user_id=?",
           (d_id,))[0]
check("retire marks the code retired",
      r.status_code == 303 and drow["status"] == "retired")
r = B.post("/invite-code", data={"code": "TENCLUB"})
check("retired words free up immediately",
      r.status_code == 303 and "ok=" in r.headers.get("location", "")
      and code_row(b_id)["code"] == "TENCLUB",
      r.headers.get("location", ""))
j = check_ep(J2, "TENCLUB")
check("check endpoint sees the new runner", not j["available"],
      str(j))
j = check_ep(J2, "BRANDNEW7")
check("check endpoint sees a free word", j["available"], str(j))

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL GREEN")
