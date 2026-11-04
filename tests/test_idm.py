"""IDM suite — Instant Developer Mode (Ken's build, Nov 4, 2026):
apply → owner approve → member marks (settings ❓ door, badge,
avatar-ring payload) → findings land in the owner desk tagged
'idm' + notify Ken instantly. Owner panel flip on/off.

Run:  ./venv/bin/python tests/test_idm.py
"""
import os
import sqlite3
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-idm-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{extra}]" if extra and not cond else ""), flush=True)


def dbq(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"], timeout=10)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def dbx(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"], timeout=10)
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def signup(name, email):
    c = TestClient(here.app, follow_redirects=False)
    r = c.post("/signup", data={"name": name, "email": email,
                                "password": "pw123456"})
    assert r.status_code == 303, (name, r.status_code)
    uid = dbq("SELECT id FROM users WHERE email=?", (email,))[0]["id"]
    return c, uid


KENc, KEN = signup("Ken Owner", "ken@idm.io")
dbx("UPDATE users SET founding_no=1 WHERE id=?", (KEN,))
A, AID = signup("Ida Insider", "ida@idm.io")
B, BID = signup("Bob Bystander", "bob@idm.io")

OWN = TestClient(here.app, follow_redirects=False)
dbx("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
    ("idm-owner-token", here.now_iso()))
OWN.cookies.set("here_owner", "idm-owner-token")

# ---------- doors before membership ----------
s = A.get("/settings/profile").text
check("non-member settings: Apply for IDM door, no ? button",
      "Apply for IDM" in s and 'id="idm-qbtn"' not in s)
r = B.post("/settings/idm-suggest",
           data={"text": "sneaky finding from a non-member"})
check("non-member finding refused (redirect to denied)",
      r.status_code == 303
      and "idm=denied" in (r.headers.get("location") or ""))
check("non-member finding created NO suggestion row",
      dbq("SELECT COUNT(*) c FROM app_suggestions")[0]["c"] == 0)

# ---------- apply ----------
r = A.post("/settings/idm-apply", data={"note": "I test lives nightly"})
check("apply redirects to applied flash",
      r.status_code == 303
      and "idm=applied" in (r.headers.get("location") or ""))
rows = dbq("SELECT * FROM idm_requests WHERE user_id=?", (AID,))
check("application row pending", len(rows) == 1
      and rows[0]["status"] == "pending")
A.post("/settings/idm-apply", data={"note": "again"})
check("second apply while pending creates no duplicate",
      len(dbq("SELECT * FROM idm_requests WHERE user_id=?",
              (AID,))) == 1)
s = A.get("/settings/profile").text
check("pending state shows in settings", "application is in" in s)
page = OWN.get("/owner/idm").text
check("owner IDM page lists the application",
      "Ida Insider" in page and "test lives nightly" in page)

# ---------- approve ----------
rid = rows[0]["id"]
r = OWN.post(f"/owner/idm/{rid}/decide", data={"act": "approve"})
u = dbq("SELECT idm_member FROM users WHERE id=?", (AID,))[0]
check("approve flips idm_member on", u["idm_member"] == 1)
n = dbq("SELECT * FROM notifications WHERE user_id=? AND kind='idm'",
        (AID,))
check("applicant notified she is in", len(n) == 1)
s = A.get("/settings/profile").text
check("member settings: the tiny ? door is live",
      'id="idm-qbtn"' in s and 'action="/settings/idm-suggest"' in s)

# ---------- the finding flow ----------
r = A.post("/settings/idm-suggest",
           data={"text": "The live chat scroll sticks on iPhone Safari"})
check("member finding accepted",
      r.status_code == 303
      and "idm=sent" in (r.headers.get("location") or ""))
rows = dbq("SELECT * FROM app_suggestions WHERE user_id=?", (AID,))
check("finding in the desk tagged source='idm'",
      len(rows) == 1 and rows[0]["source"] == "idm")
n = dbq("SELECT * FROM notifications WHERE user_id=? AND kind='idm'",
        (KEN,))
check("Ken notified instantly in his own notifications",
      len(n) == 1 and "live chat scroll" in n[0]["text"],
      str([dict(x) for x in n])[:120])
page = OWN.get("/owner/suggestions").text
check("owner suggestions desk shows the IDM tag",
      "💡 IDM" in page and "live chat scroll sticks" in page)
page = OWN.get("/owner/idm").text
check("owner IDM page shows crew + latest finding",
      "The crew (1 on IDM)" in page
      and "live chat scroll sticks" in page)

# ---------- the marks ----------
dbx("UPDATE users SET avatar='avatars/ringtest.jpg' WHERE id=?",
    (AID,))
home = B.get("/home").text
check("ring payload ships the member's avatar path on every page",
      '"avatars/ringtest.jpg"' in home and 'id="idm-avatars"' in home)
check("neon ring + badge CSS live in base",
      ".idm-ring{" in home and ".idm-badge{" in home)
prof = B.get(f"/user/{AID}").text
check("profile name carries the HERE IDM badge",
      'class="idm-badge"' in prof)
A.post("/posts", data={"body": "idm badge post", "kind": "text"})
home = B.get("/home").text
check("post card author line carries the HERE IDM badge",
      'class="idm-badge"' in home)

# ---------- owner panel flip ----------
dash = OWN.get("/owner").text
check("owner users table carries the IDM flip",
      f"/owner/users/{BID}/idm" in dash and "IDM: OFF" in dash)
OWN.post(f"/owner/users/{BID}/idm")
check("flip turns Bob ON + notifies him",
      dbq("SELECT idm_member FROM users WHERE id=?",
          (BID,))[0]["idm_member"] == 1
      and len(dbq("SELECT * FROM notifications WHERE user_id=? "
                  "AND kind='idm'", (BID,))) == 1)
OWN.post(f"/owner/users/{BID}/idm")
check("flip again turns Bob OFF",
      dbq("SELECT idm_member FROM users WHERE id=?",
          (BID,))[0]["idm_member"] == 0)

# ---------- deny path ----------
C, CID = signup("Cal Hopeful", "cal@idm.io")
C.post("/settings/idm-apply", data={"note": ""})
rid2 = dbq("SELECT id FROM idm_requests WHERE user_id=?",
           (CID,))[0]["id"]
OWN.post(f"/owner/idm/{rid2}/decide", data={"act": "deny"})
check("deny leaves the user off IDM, request settled",
      dbq("SELECT idm_member FROM users WHERE id=?",
          (CID,))[0]["idm_member"] == 0
      and dbq("SELECT status FROM idm_requests WHERE id=?",
              (rid2,))[0]["status"] == "denied")

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} IDM checks passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL IDM CHECKS PASSED")
