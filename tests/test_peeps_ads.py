"""Peeps sponsors tests: the ads engine rides the /views scroll —
one Sponsored slide per 8 peeps crossed, impressions recorded,
the daily cap honored, and the peeps list itself staying pure
(the client's offset math counts peeps only).

Run:  ./venv/bin/python tests/test_peeps_ads.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-peepsads-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import app as here  # noqa: E402
import ads as ads_mod  # noqa: E402
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


MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" \
    + b"\x00" * 64

V, VID = signup("Peeps Viewer", "v@peeps.io")
W, WID = signup("Peeps Maker", "w@peeps.io")
for i in range(10):
    r = W.post("/posts", data={"kind": "video",
                               "body": f"peep {i}",
                               "privacy": "public"},
               files={"media": (f"clip{i}.mp4", MP4, "video/mp4")})
    assert r.status_code in (200, 303), r.status_code

# a running sponsor (house ad, active + always schedule)
conn = here.db()
ads_mod.ensure_schema(conn)
cur = conn.execute(
    "INSERT INTO ads(creator_id, kind, title, body, link_url, "
    "cta_label, status, schedule, created_at) VALUES(?,?,?,?,?,"
    "?,?,?,?)",
    (WID, "house", "ZZ Peeps Sponsor", "Sponsor body line",
     "https://example.com", "Go see it", "active", "always",
     here.now_iso()))
ADID = cur.lastrowid
conn.commit()
conn.close()

r = V.get("/views.json?offset=0&limit=10")
j = r.json()
check("peeps: the page of 10 peeps arrives pure",
      j.get("ok") and len(j.get("views") or []) == 10
      and all("post_id" in v for v in j["views"]),
      str(len(j.get("views") or [])))
ads_here = j.get("ads") or []
check("peeps: one Sponsored slide rides the first page",
      len(ads_here) == 1 and ads_here[0]["id"] == ADID
      and ads_here[0]["title"] == "ZZ Peeps Sponsor",
      str(ads_here)[:160])
check("peeps: the slide carries sponsor + click + hide plumbing",
      ads_here and ads_here[0].get("click_url")
      == f"/ads/{ADID}/click"
      and ads_here[0].get("hide_url") == f"/ads/{ADID}/hide"
      and ads_here[0].get("sponsor_name"))
imps = dbq("SELECT COUNT(*) c FROM ad_events WHERE ad_id=? AND "
           "user_id=? AND kind='imp'", (ADID, VID))[0]["c"]
check("peeps: the impression was recorded for the viewer",
      imps == 1, str(imps))

r = V.get("/views.json?offset=10&limit=10")
j = r.json()
check("peeps: no peeps left, no boundary crossed, no ad",
      len(j.get("views") or []) == 0 and not j.get("ads"))

# daily cap: three impressions a day, then the engine goes quiet
V.get("/views.json?offset=0&limit=10")
V.get("/views.json?offset=0&limit=10")
imps = dbq("SELECT COUNT(*) c FROM ad_events WHERE ad_id=? AND "
           "user_id=? AND kind='imp'", (ADID, VID))[0]["c"]
check("peeps: impressions accumulate to the cap", imps == 3,
      str(imps))
r = V.get("/views.json?offset=0&limit=10")
check("peeps: at the daily cap the sponsor stops appearing",
      not (r.json().get("ads")))

html = V.get("/views").text
check("peeps page: the ad-slide renderer is wired in",
      "appendAdCell" in html and "d.ads" in html
      and "mv-ad" in html)

fails = [n for n, ok in RESULTS if not ok]
print()
print(f"peeps-ads: {len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILED:", fails)
    sys.exit(1)
