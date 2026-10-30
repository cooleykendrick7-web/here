"""HELP + BADGES tests (Oct 30, 2026): the /faq page, Hearley the
help bot (page + ask API + knowledge base), and the Verified User /
Verified Vendor stickers — settings request door (buried deep),
owner queue approve/decline, owner panel per-user toggles, and the
sticker rendering on profiles, posts, search, and Market listings.

Run:  ./venv/bin/python tests/test_help_badges.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-help-")
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


def signup(name, email):
    c = TestClient(here.app, follow_redirects=False)
    r = c.post("/signup", data={"name": name, "email": email,
                                "password": "pw123456"})
    assert r.status_code == 303, (name, r.status_code)
    uid = dbq("SELECT id FROM users WHERE email=?", (email,))[0]["id"]
    return c, uid


Ac, ANN = signup("Ann Asker", "ann@help.test")
Bc, BOB = signup("Bob Buyer", "bob@help.test")

OWN = TestClient(here.app, follow_redirects=False)
dbx("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
    ("help-owner-token", here.now_iso()))
OWN.cookies.set("here_owner", "help-owner-token")

ANON = TestClient(here.app, follow_redirects=False)

# ---------- FAQ + Hearley pages ----------
r = Ac.get("/faq")
check("FAQ page renders for a member", r.status_code == 200
      and "Ask Hearley" in r.text and "How HERE works" in r.text)
r = ANON.get("/faq")
check("FAQ page requires login", r.status_code in (303, 401, 403))
r = Ac.get("/hearley")
check("Hearley page renders", r.status_code == 200
      and "Hearley" in r.text and "hl-input" in r.text)

# ---------- Hearley brain ----------
r = ANON.post("/api/hearley/ask", json={"q": "how do I cash out?"})
check("Hearley refuses logged-out", r.status_code == 401)
r = Ac.post("/api/hearley/ask", json={"q": "how do I cash out?"})
d = r.json()
check("Hearley answers cash-out specifically",
      d.get("ok") and "2,000" in d["answer"] and "$10" in d["answer"]
      and d.get("link") == "/tickets")
r = Ac.post("/api/hearley/ask", json={"q": "what is a verified vendor?"})
d = r.json()
check("Hearley answers Verified Vendor", d.get("ok")
      and "VERIFIED VENDOR" in d["answer"])
r = Ac.post("/api/hearley/ask", json={"q": "how do I go live"})
d = r.json()
check("Hearley answers going live", d.get("ok")
      and "Platform" in d["answer"])
r = Ac.post("/api/hearley/ask",
            json={"q": "zqxwkv blorp nonsense syllables"})
d = r.json()
check("Hearley fallback is honest + gives support door",
      d.get("ok") and "wearehereyouarehere@gmail.com" in d["answer"])
r = Ac.post("/api/hearley/ask", json={"q": "  "})
check("Hearley rejects empty question", r.status_code == 400)
ans, link, _lbl = here.hearley_answer("tell me about the invite race")
check("KB: race answer carries the terms",
      "$100" in ans and link == "/invite-race")
ans, _l, _b = here.hearley_answer("i forgot my password")
check("KB: password answer gives the reset door",
      "reset" in ans.lower())

# ---------- settings: the buried badge door ----------
r = Ac.get("/settings/profile")
check("settings carries the buried badge door",
      r.status_code == 200 and "badge door" in r.text
      and "/verify/vuser/request" in r.text
      and "/verify/vvendor/request" in r.text)

# ---------- request flow ----------
r = Ac.post("/verify/vuser/request", follow_redirects=False)
check("vuser request lands pending",
      r.status_code == 303
      and dbq("SELECT vuser_status FROM users WHERE id=?",
              (ANN,))[0]["vuser_status"] == "pending"
      and dbq("SELECT COUNT(*) c FROM verification_requests "
              "WHERE kind='vuser' AND target_id=? AND status='pending'",
              (ANN,))[0]["c"] == 1)
r = Ac.post("/verify/vuser/request", follow_redirects=False)
check("double vuser request refused (still one row)",
      dbq("SELECT COUNT(*) c FROM verification_requests "
          "WHERE kind='vuser' AND target_id=?", (ANN,))[0]["c"] == 1)
r = Ac.get("/settings/profile")
check("settings shows the pending state", "Request in" in r.text)

# ---------- owner queue ----------
r = OWN.get("/owner")
check("owner queue shows the sticker request",
      r.status_code == 200 and "VERIFIED USER sticker" in r.text
      and "Ann Asker" in r.text)
rid = dbq("SELECT id FROM verification_requests WHERE kind='vuser' "
          "AND target_id=?", (ANN,))[0]["id"]
r = OWN.post(f"/owner/verify/{rid}/approve", follow_redirects=False)
u = dbq("SELECT is_verified_user, vuser_status FROM users WHERE id=?",
        (ANN,))[0]
check("owner approve turns the sticker ON",
      r.status_code == 303 and u["is_verified_user"] == 1
      and u["vuser_status"] == "approved")

# ---------- owner toggle switches ----------
r = OWN.get("/owner")
check("owner users table carries both toggles",
      f"/owner/users/{ANN}/badge/vuser" in r.text
      and f"/owner/users/{ANN}/badge/vvendor" in r.text
      and "Verified Vendor: OFF" in r.text)
r = OWN.post(f"/owner/users/{ANN}/badge/vvendor", follow_redirects=False)
check("owner toggle turns vendor ON",
      dbq("SELECT is_verified_vendor FROM users WHERE id=?",
          (ANN,))[0]["is_verified_vendor"] == 1)
r = OWN.post(f"/owner/users/{ANN}/badge/vvendor", follow_redirects=False)
check("owner toggle turns vendor back OFF",
      dbq("SELECT is_verified_vendor FROM users WHERE id=?",
          (ANN,))[0]["is_verified_vendor"] == 0)
OWN.post(f"/owner/users/{ANN}/badge/vvendor", follow_redirects=False)

# ---------- decline path ----------
Bc.post("/verify/vuser/request", follow_redirects=False)
rid2 = dbq("SELECT id FROM verification_requests WHERE kind='vuser' "
           "AND target_id=?", (BOB,))[0]["id"]
OWN.post(f"/owner/verify/{rid2}/decline", follow_redirects=False)
u = dbq("SELECT is_verified_user, vuser_status FROM users WHERE id=?",
        (BOB,))[0]
check("owner decline leaves the sticker OFF + status declined",
      u["is_verified_user"] == 0 and u["vuser_status"] == "declined")

# ---------- the stickers render ----------
r = Bc.get(f"/user/{ANN}")
check("profile header shows BOTH full stickers",
      r.status_code == 200 and "VERIFIED USER" in r.text
      and "VERIFIED VENDOR" in r.text and "bv-sticker" in r.text)
Ac.post("/posts", data={"kind": "text", "body": "ann says hello",
                        "privacy": "public"})
r = Bc.get("/home")
check("post card carries the mini seals", "bv-seal" in r.text
      and "bv-user" in r.text and "bv-vendor" in r.text)
r = Bc.get("/search?q=Ann")
check("search people row carries the seals", "bv-seal" in r.text)

# ---------- vendor sticker on the Market ----------
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64 + b"\xff\xd9"
r = Ac.post("/market", data={"title": "Ann's Chair", "price": "25",
                             "category": "Other", "condition": "Good",
                             "description": "", "location": ""},
            files=[("photos", ("c.jpg", JPEG, "image/jpeg"))],
            follow_redirects=False)
loc = r.headers.get("location", "")
check("vendor listing created", r.status_code == 303
      and loc.startswith("/market/"), loc)
lid = int(loc.split("/")[2].split("?")[0])
r = Bc.get("/market")
check("market card shows the gold vendor seal", "bv-vendor" in r.text)
r = Bc.get(f"/market/{lid}")
check("market detail shows the gold vendor seal", "bv-vendor" in r.text)
r = ANON.get(f"/m/{lid}")
check("public listing shows the gold vendor seal",
      r.status_code == 200 and "bv-vendor" in r.text)

# ---------- base nav carries the help spot ----------
r = Ac.get("/home")
check("menus link Help & FAQ + Ask Hearley",
      'href="/faq"' in r.text and 'href="/hearley"' in r.text)

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
