"""OWNER IN-PLACE ACTIONS + UPLOAD PROGRESS tests (Oct 30, 2026).

Ken: "I hate when I click on something and make a change it jumps
to another page — let me make the change and be able to make that
change again on someone else." Owner POST routes now answer JSON
to the panel's X-Owner-Ajax calls (the row updates in place) and
keep the classic 303 for a plain submit. Also: "when people are
posting their stories and making posts they need to see the
progression — a loading bar to 100%, then it says posted" — the
shared upload-progress bar ships in base.html and wires the post
and story forms.

Run:  ./venv/bin/python tests/test_owner_ajax.py
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-oa-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []
H = {"X-Owner-Ajax": "1"}


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
    cur = conn.execute(sql, args)
    conn.commit()
    conn.close()
    return cur


def signup(name, email):
    c = TestClient(here.app, follow_redirects=False)
    r = c.post("/signup", data={"name": name, "email": email,
                                "password": "pw123456"})
    assert r.status_code == 303, (name, r.status_code)
    uid = dbq("SELECT id FROM users WHERE email=?", (email,))[0]["id"]
    return c, uid


Ac, ANN = signup("Ann OwnerTarget", "ann@oa.test")
Bc, BOB = signup("Bob Second", "bob@oa.test")

OWN = TestClient(here.app, follow_redirects=False)
dbx("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
    ("oa-owner-token", here.now_iso()))
OWN.cookies.set("here_owner", "oa-owner-token")
ANON = TestClient(here.app, follow_redirects=False)

# ---------- the panel ships the in-place script ----------
r = OWN.get("/owner")
check("dashboard ships the in-place script",
      r.status_code == 200 and "X-Owner-Ajax" in r.text
      and "oa-toast" in r.text)
r = OWN.get("/owner/suggestions")
check("suggestions desk ships the in-place script",
      r.status_code == 200 and "X-Owner-Ajax" in r.text)
r = OWN.get("/owner/artists")
check("artists desk ships the in-place script",
      r.status_code == 200 and "X-Owner-Ajax" in r.text)

# ---------- badge toggle: in place + classic ----------
r = OWN.post(f"/owner/users/{BOB}/badge/vuser", headers=H)
d = r.json()
check("badge toggle answers JSON on=true",
      r.status_code == 200 and d.get("ok") and d.get("on") is True,
      str(d))
check("badge flag landed",
      dbq("SELECT is_verified_user f FROM users WHERE id=?",
          (BOB,))[0]["f"] == 1)
r = OWN.post(f"/owner/users/{BOB}/badge/vuser", headers=H)
check("badge toggle flips back off", r.json().get("on") is False)
r = OWN.post(f"/owner/users/{BOB}/badge/vuser")
check("badge toggle classic submit still 303s",
      r.status_code == 303)
dbx("UPDATE users SET is_verified_user=0 WHERE id=?", (BOB,))
r = Ac.post(f"/owner/users/{BOB}/badge/vuser", headers=H)
check("non-owner gets no JSON action",
      not (r.status_code == 200 and r.headers.get("content-type", "")
           .startswith("application/json")))

# ---------- ad status / delete ----------
dbx("INSERT INTO ads(creator_id, page_id, kind, title, body, "
    "media_kind, media_path, link_url, cta_label, tags, status, "
    "schedule, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
    (ANN, None, "house", "OA Probe Ad", "", "none", "", "",
     "Learn more", "", "active", "always", here.now_iso()))
AD = dbq("SELECT id FROM ads WHERE title='OA Probe Ad'")[0]["id"]
r = OWN.post(f"/owner/ads/{AD}/status", data={"status": "paused"},
             headers=H)
d = r.json()
check("ad pause answers JSON with the new status",
      d.get("ok") and d.get("status") == "paused", str(d))
check("ad pause landed",
      dbq("SELECT status FROM ads WHERE id=?", (AD,))[0]["status"]
      == "paused")
r = OWN.post(f"/owner/ads/{AD}/status", data={"status": "bogus"},
             headers=H)
check("bad ad status is a JSON no",
      r.json().get("ok") is False)
check("bad ad status changed nothing",
      dbq("SELECT status FROM ads WHERE id=?", (AD,))[0]["status"]
      == "paused")
r = OWN.post(f"/owner/ads/{AD}/delete", headers=H)
check("ad delete answers JSON", r.json().get("ok") is True)
check("ad delete landed",
      not dbq("SELECT id FROM ads WHERE id=?", (AD,)))

# ---------- verification approve / decline ----------
r = Bc.post("/verify/vuser/request")
check("vendor... user badge request files", r.status_code == 303)
VR = dbq("SELECT id FROM verification_requests WHERE kind='vuser' "
         "AND target_id=?", (BOB,))[0]["id"]
r = OWN.post(f"/owner/verify/{VR}/approve", headers=H)
d = r.json()
check("verify approve answers JSON approved",
      d.get("ok") and d.get("status") == "approved", str(d))
check("verify approve granted the sticker",
      dbq("SELECT is_verified_user f FROM users WHERE id=?",
          (BOB,))[0]["f"] == 1)
r = Bc.post("/verify/vvendor/request")
VR2 = dbq("SELECT id FROM verification_requests WHERE "
          "kind='vvendor' AND target_id=?", (BOB,))[0]["id"]
r = OWN.post(f"/owner/verify/{VR2}/decline", headers=H)
check("verify decline answers JSON declined",
      r.json().get("status") == "declined")
check("verify decline granted nothing",
      dbq("SELECT is_verified_vendor f FROM users WHERE id=?",
          (BOB,))[0]["f"] == 0)

# ---------- flags ----------
dbx("INSERT INTO moderation_flags(item_type, item_id, author_id, "
    "reasons, excerpt, status, created_at) VALUES(?,?,?,?,?,?,?)",
    ("post", 1, BOB, "spam: test", "hello", "pending",
     here.now_iso()))
FID = dbq("SELECT id FROM moderation_flags WHERE author_id=?",
          (BOB,))[0]["id"]
r = OWN.post(f"/owner/flag/{FID}/dismiss", headers=H)
check("flag dismiss answers JSON dismissed",
      r.json().get("status") == "dismissed")
check("flag dismiss landed",
      dbq("SELECT status FROM moderation_flags WHERE id=?",
          (FID,))[0]["status"] == "dismissed")

# ---------- payouts: note, paid, declined, send-guard ----------
def mk_payout(uid, tickets=2600):
    dbx("INSERT INTO payout_requests(user_id, tickets, "
        "dollars_cents, payout_note, status, created_at) "
        "VALUES(?,?,?,?,?,?)",
        (uid, tickets, tickets // 2, "Cash App $oa", "pending",
         here.now_iso()))
    return dbq("SELECT id FROM payout_requests WHERE user_id=? "
               "ORDER BY id DESC", (uid,))[0]["id"]


P1 = mk_payout(BOB)
r = OWN.post(f"/owner/payouts/{P1}/hand-note",
             data={"note": "Paid $5 by hand"}, headers=H)
d = r.json()
check("hand-note answers JSON with the note",
      d.get("ok") and d.get("note") == "Paid $5 by hand", str(d))
check("hand-note landed",
      dbq("SELECT hand_note FROM payout_requests WHERE id=?",
          (P1,))[0]["hand_note"] == "Paid $5 by hand")
r = OWN.post(f"/owner/payouts/{P1}/paid", headers=H)
d = r.json()
check("payout paid answers JSON paid",
      d.get("ok") and d.get("status") == "paid", str(d))
r = OWN.post(f"/owner/payouts/{P1}/paid", headers=H)
check("second paid is a JSON no (already decided)",
      r.json().get("ok") is False)
before = dbq("SELECT gift_earned g FROM users WHERE id=?",
             (BOB,))[0]["g"] or 0
P2 = mk_payout(BOB, tickets=1000)
r = OWN.post(f"/owner/payouts/{P2}/declined", headers=H)
check("payout declined answers JSON declined",
      r.json().get("status") == "declined")
after = dbq("SELECT gift_earned g FROM users WHERE id=?",
            (BOB,))[0]["g"] or 0
check("decline returned the tickets", after - before == 1000,
      f"{before} -> {after}")
P3 = mk_payout(BOB)
r = OWN.post(f"/owner/payouts/{P3}/send", headers=H)
check("send to a bank-less streamer is a JSON no, row stays",
      r.json().get("ok") is False
      and dbq("SELECT status FROM payout_requests WHERE id=?",
              (P3,))[0]["status"] == "pending")

# ---------- password help: link + hand-sent ----------
exp = (datetime.now(timezone.utc)
       + timedelta(hours=1)).isoformat()
dbx("INSERT INTO password_resets(user_id, token_hash, token_plain, "
    "delivery, created_at, expires_at) VALUES(?,?,?,?,?,?)",
    (BOB, "oa-hash-1", "oa-token-1", "queued", here.now_iso(), exp))
RID = dbq("SELECT id FROM password_resets WHERE token_hash="
          "'oa-hash-1'")[0]["id"]
r = OWN.post(f"/owner/pwreset/{RID}/link", headers=H)
d = r.json()
check("pwreset link answers JSON with the link",
      d.get("ok") and "/reset/oa-token-1" in (d.get("link") or ""),
      str(d))
r = OWN.post(f"/owner/pwreset/{RID}/hand-sent", headers=H)
check("pwreset hand-sent answers JSON",
      r.json().get("status") == "hand-sent")
row = dbq("SELECT delivery, token_plain FROM password_resets "
          "WHERE id=?", (RID,))[0]
check("hand-sent landed and wiped the stored token",
      row["delivery"] == "hand_sent" and row["token_plain"] is None)
r = OWN.post(f"/owner/pwreset/{RID}/link", headers=H)
check("link after hand-sent is a JSON no",
      r.json().get("ok") is False)
r = OWN.post(f"/owner/pwreset/{RID}/link")
check("pwreset link classic submit still renders the page flow",
      r.status_code in (200, 303))

# ---------- live end ----------
r = Bc.post("/live/start", data={"title": "OA probe live"})
check("probe live starts", r.status_code in (200, 303),
      str(r.status_code))
lives = dbq("SELECT id FROM live_sessions WHERE host_id=? AND "
            "ended_at IS NULL", (BOB,))
if lives:
    LID = lives[0]["id"]
    r = OWN.post(f"/owner/live/{LID}/end", headers=H)
    check("live end answers JSON", r.json().get("ok") is True)
    check("live end landed",
          dbq("SELECT ended_at FROM live_sessions WHERE id=?",
              (LID,))[0]["ended_at"] is not None)
else:
    check("live end answers JSON", False, "no live row to end")

# ---------- suggestions ----------
r = Bc.post("/settings/suggest", data={"text": "OA probe idea"})
check("suggestion files", r.status_code == 303)
SID = dbq("SELECT id FROM app_suggestions WHERE user_id=?",
          (BOB,))[0]["id"]
r = OWN.post(f"/owner/suggestions/{SID}/status",
             data={"status": "built"}, headers=H)
d = r.json()
check("suggestion status answers JSON with label",
      d.get("ok") and d.get("status") == "built"
      and "Shipped" in (d.get("label") or ""), str(d))
check("suggestion status landed",
      dbq("SELECT status FROM app_suggestions WHERE id=?",
          (SID,))[0]["status"] == "built")
r = OWN.post(f"/owner/suggestions/{SID}/status",
             data={"status": "bogus"}, headers=H)
check("bad suggestion status is a JSON no",
      r.json().get("ok") is False)

# ---------- user remove ----------
Cc, CARA = signup("Cara Gone", "cara@oa.test")
r = OWN.post(f"/owner/user/{CARA}/remove", headers=H)
check("user remove answers JSON", r.json().get("ok") is True)
check("user remove landed",
      not dbq("SELECT id FROM users WHERE id=?", (CARA,)))

# ---------- upload progress bar ----------
r = Ac.get("/home")
check("home ships the upload progress bar",
      r.status_code == 200 and 'id="hup"' in r.text
      and "hereUploadProgress" in r.text
      and "Uploading…" in r.text)
check("progress script wires posts + page posts + stories",
      "'/posts'" in r.text and "'/stories/new'" in r.text
      and "\\/page\\/" in r.text)
r = Ac.get("/stories/new")
check("story editor drives the bar for multi-posts",
      r.status_code == 200 and "hereUploadProgress.set" in r.text
      and "hereUploadProgress.done" in r.text)
r = Ac.post("/posts", data={"body": "oa progress sanity post"})
check("classic post submit still works (303)",
      r.status_code == 303)

failed = [n for n, ok in RESULTS if not ok]
print()
print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
if failed:
    print("FAILED:", "; ".join(failed))
    sys.exit(1)
