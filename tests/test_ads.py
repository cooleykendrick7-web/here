"""Build ADS tests: the smart ad system — schema, owner house ads,
feed Sponsored interleave + impressions, schedule gates, rotation
caps, hide, targeting + smart-hours scoring, click safety, page
"Run as ad" flow + 403s, owner oversight, stats, moderation flags.

Run:  ./venv/bin/python tests/test_ads.py
Uses a throwaway DB/uploads dir; no network.
"""
import os
import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-ads-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import ads  # noqa: E402
import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{extra}]" if extra and not cond else ""))


def dbq(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"])
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def dbx(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"])
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


def owner_client():
    dbx("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
        ("ads-owner-token", here.now_iso()))
    c = TestClient(here.app, follow_redirects=False)
    c.cookies.set("here_owner", "ads-owner-token")
    return c


OWN = owner_client()

# First user on the platform (house ads fall back to this account when
# the owner panel is used without a user session).
FIRST, FIRST_ID = signup("First User", "first@x.io")

# ---------------- 1. schema / boot ----------------
tabs = {r["name"] for r in dbq(
    "SELECT name FROM sqlite_master WHERE type='table'")}
check("schema: ads + ad_events tables exist",
      {"ads", "ad_events"} <= tabs)
ad_cols = {r["name"] for r in dbq("PRAGMA table_info(ads)")}
check("schema: ads columns complete",
      {"creator_id", "page_id", "kind", "title", "body", "media_kind",
       "media_path", "link_url", "cta_label", "tags", "status",
       "schedule", "weekly_days", "monthly_days", "hour_start",
       "hour_end", "smart_hours", "weight", "starts_on", "ends_on",
       "impressions", "clicks", "created_at"} <= ad_cols)

# ---------------- 2. owner creates a house ad ----------------
r = OWN.post("/owner/ads", data={
    "title": "Friday Night Showdown", "body": "Live at 8pm — be there.",
    "link_url": "https://joincomehere.com/lives", "cta_label": "Watch",
    "tags": "live, gaming", "schedule": "always", "weight": "3",
    "hour_start": "0", "hour_end": "24"})
check("owner POST /owner/ads -> 303", r.status_code == 303,
      str(r.status_code))
house = dbq("SELECT * FROM ads WHERE kind='house'")
check("house ad row created, active", len(house) == 1
      and house[0]["status"] == "active")
HA = house[0]["id"]
check("house ad fields stored (weight 3, tags, cta)",
      house[0]["weight"] == 3 and house[0]["tags"] == "live,gaming"
      and house[0]["cta_label"] == "Watch")
check("house ad attributed to a real user account",
      house[0]["creator_id"] == FIRST_ID)

r = OWN.post("/owner/ads", data={"title": "Too heavy", "weight": "9"})
hw = dbq("SELECT weight FROM ads WHERE title='Too heavy'")
check("weight clamps to 5", hw and hw[0]["weight"] == 5)
dbx("DELETE FROM ads WHERE title='Too heavy'")

# ---------------- 3. owner-only gates ----------------
U1, U1_ID = signup("Plain User", "plain@x.io")
r = U1.get("/owner/ads/new")
check("user GET /owner/ads/new -> bounced to owner login",
      r.status_code == 303
      and r.headers.get("location", "").startswith("/owner/login"),
      str(r.status_code))
before = dbq("SELECT COUNT(*) c FROM ads")[0]["c"]
r = U1.post("/owner/ads", data={"title": "Sneaky"})
check("user POST /owner/ads creates nothing",
      dbq("SELECT COUNT(*) c FROM ads")[0]["c"] == before)

# ---------------- 4. fresh user's feed shows it, labeled ----------------
FRESH, FRESH_ID = signup("Fresh Face", "fresh@x.io")
home = FRESH.get("/home")
check("fresh feed 200", home.status_code == 200)
check("fresh feed carries the Sponsored label",
      "Sponsored" in home.text)
check("fresh feed carries the ad headline",
      "Friday Night Showdown" in home.text)
row = dbq("SELECT impressions FROM ads WHERE id=?", (HA,))[0]
check("impression counter incremented at render", row["impressions"] == 1,
      str(row["impressions"]))
ev = dbq("SELECT * FROM ad_events WHERE ad_id=? AND user_id=? AND kind='imp'",
         (HA, FRESH_ID))
check("impression event logged for the viewer", len(ev) == 1)

# ---------------- 5. schedule gates (engine level, pinned clock) ----
NOW = datetime(2026, 10, 26, 12, 0)          # a Monday, midday
assert NOW.weekday() == 0


def mkad(**kw):
    d = {"creator_id": FIRST_ID, "page_id": None, "kind": "house",
         "title": "Gate", "body": "", "media_kind": "none",
         "media_path": "", "link_url": "/home", "cta_label": "Go",
         "tags": "", "status": "active", "schedule": "always",
         "weekly_days": "", "monthly_days": "", "hour_start": 0,
         "hour_end": 24, "smart_hours": 0, "weight": 1,
         "starts_on": None, "ends_on": None,
         "created_at": here.now_iso()}
    d.update(kw)
    cols = ",".join(d.keys())
    conn = sqlite3.connect(os.environ["HERE_DB"])
    cur = conn.execute(
        f"INSERT INTO ads({cols}) VALUES({','.join('?' * len(d))})",
        tuple(d.values()))
    conn.commit()
    aid = cur.lastrowid
    conn.close()
    return aid


def elig_ids(uid, now=NOW):
    conn = here.db()
    out = {a["id"] for a in ads.eligible_ads(conn, uid, now)}
    conn.close()
    return out


g_week_ok = mkad(title="W-ok", schedule="weekly",
                 weekly_days=str(NOW.weekday()))
g_week_no = mkad(title="W-no", schedule="weekly",
                 weekly_days=str((NOW.weekday() + 1) % 7))
g_mon_ok = mkad(title="M-ok", schedule="monthly",
                monthly_days=str(NOW.day))
g_mon_no = mkad(title="M-no", schedule="monthly",
                monthly_days=str(NOW.day % 28 + 1))
g_hour_no = mkad(title="H-no", hour_start=18, hour_end=20)
g_hour_ok = mkad(title="H-ok", hour_start=9, hour_end=17)
g_future = mkad(title="D-future",
                starts_on=(NOW.date() + timedelta(days=1)).isoformat())
g_past = mkad(title="D-past",
              ends_on=(NOW.date() - timedelta(days=1)).isoformat())
g_paused = mkad(title="S-paused", status="paused")
g_ended = mkad(title="S-ended", status="ended")
ids = elig_ids(FRESH_ID)
check("gate: weekly matching day eligible", g_week_ok in ids)
check("gate: weekly wrong day excluded", g_week_no not in ids)
check("gate: monthly matching day eligible", g_mon_ok in ids)
check("gate: monthly wrong day excluded", g_mon_no not in ids)
check("gate: outside hour window excluded", g_hour_no not in ids)
check("gate: inside hour window eligible", g_hour_ok in ids)
check("gate: starts tomorrow excluded", g_future not in ids)
check("gate: ended yesterday excluded", g_past not in ids)
check("gate: paused excluded", g_paused not in ids)
check("gate: ended excluded", g_ended not in ids)

# ---------------- 6. targeting + smart-hours scoring -----------------
dbx("INSERT OR IGNORE INTO user_topics(user_id, topic) VALUES(?,?)",
    (FRESH_ID, "cars"))
g_tag = mkad(title="T-cars", tags="cars")
conn = here.db()
scored = {a["id"]: a["score"]
          for a in ads.eligible_ads(conn, FRESH_ID, NOW)}
conn.close()
check("targeting: matching tag triples the score",
      scored.get(g_tag) == 3, str(scored.get(g_tag)))
conn = here.db()
scored_u1 = {a["id"]: a["score"]
             for a in ads.eligible_ads(conn, U1_ID, NOW)}
conn.close()
check("targeting: no match keeps base weight for other users",
      scored_u1.get(g_tag) == 1, str(scored_u1.get(g_tag)))
g_smart = mkad(title="T-smart", smart_hours=1)
conn = here.db()
peak = {a["id"]: a["score"]
        for a in ads.eligible_ads(conn, U1_ID, datetime(2026, 10, 26, 12, 0))}
off = {a["id"]: a["score"]
       for a in ads.eligible_ads(conn, U1_ID, datetime(2026, 10, 26, 3, 0))}
conn.close()
check("smart hours: x2 in the midday peak", peak.get(g_smart) == 2,
      str(peak.get(g_smart)))
check("smart hours: no boost at 3am", off.get(g_smart) == 1,
      str(off.get(g_smart)))

# ---------------- 7. daily cap: 3 impressions per user per day --------
dbx("UPDATE ads SET status='paused'")
CAP = mkad(title="Cap Ad", link_url="/market")
U2, U2_ID = signup("Cap User", "cap@x.io")
seen = 0
for i in range(3):
    h = U2.get("/home").text
    if "Cap Ad" in h:
        seen += 1
check("cap: ad renders on the first 3 feed loads", seen == 3, str(seen))
imps = dbq("SELECT impressions FROM ads WHERE id=?", (CAP,))[0]["impressions"]
check("cap: counter stops at 3 for that user", imps == 3, str(imps))
h4 = U2.get("/home").text
check("cap: 4th load shows no ad (cap hit)", "Cap Ad" not in h4)
U3, U3_ID = signup("Cap Two", "cap2@x.io")
check("cap: a different user still sees it", "Cap Ad" in U3.get("/home").text)

# ---------------- 8. hide: gone for that user only -------------------
r = U3.post(f"/ads/{CAP}/hide")
check("hide -> 303", r.status_code == 303, str(r.status_code))
check("hide: hidden for the hider", "Cap Ad" not in U3.get("/home").text)
U4, U4_ID = signup("Cap Three", "cap3@x.io")
check("hide: still shows for everyone else",
      "Cap Ad" in U4.get("/home").text)

# ---------------- 9. click: logged, 302, safe targets only -----------
clicks0 = dbq("SELECT clicks FROM ads WHERE id=?", (CAP,))[0]["clicks"]
r = U4.get(f"/ads/{CAP}/click")
check("click -> 302", r.status_code == 302, str(r.status_code))
check("click lands on the ad's link",
      r.headers.get("location") == "/market",
      str(r.headers.get("location")))
clicks1 = dbq("SELECT clicks FROM ads WHERE id=?", (CAP,))[0]["clicks"]
check("click counter incremented", clicks1 == clicks0 + 1)
cev = dbq("SELECT * FROM ad_events WHERE ad_id=? AND kind='click'",
          (CAP,))
check("click event logged", len(cev) >= 1)
JS = mkad(title="JS Ad", link_url="javascript:alert(1)", status="paused")
dbx("UPDATE ads SET status='active' WHERE id=?", (JS,))
r = U4.get(f"/ads/{JS}/click")
check("javascript: link neutralized to '#'",
      r.status_code == 302 and r.headers.get("location") == "#",
      str(r.headers.get("location")))
EXT = mkad(title="Ext Ad", link_url="https://example.com/x", status="paused")
dbx("UPDATE ads SET status='active' WHERE id=?", (EXT,))
r = U4.get(f"/ads/{EXT}/click")
check("https link passes through",
      r.headers.get("location") == "https://example.com/x")

# ---------------- 10. page "Run as ad" --------------------------------
dbx("UPDATE ads SET status='paused'")
POWN, POWN_ID = signup("Page Owner", "pown@x.io")
r = POWN.post("/pages", data={"name": "Taco Truck", "ptype": "seller",
                              "description": "Best tacos in town"})
check("page created", r.status_code == 303, str(r.status_code))
PID = dbq("SELECT id FROM pages WHERE owner_id=?", (POWN_ID,))[0]["id"]
page_html = POWN.get(f"/page/{PID}").text
check("page shows the 📣 Run as ad button", "Run as ad" in page_html)

r = POWN.get(f"/page/{PID}/promote")
check("promote page 200 for the owner", r.status_code == 200)
r = POWN.post(f"/page/{PID}/promote", data={"promote_what": "page"})
check("page ad create -> 303", r.status_code == 303, str(r.status_code))
pads = dbq("SELECT * FROM ads WHERE page_id=? AND kind='page'", (PID,))
check("page ad row: kind/page/link/title from the page",
      len(pads) == 1 and pads[0]["link_url"] == f"/page/{PID}"
      and pads[0]["title"] == "Taco Truck")
PAD = pads[0]["id"]
check("page ad active + free (no charge fields touched)",
      pads[0]["status"] == "active")

promo_html = POWN.get(f"/page/{PID}/promote").text
check("mini-dashboard lists the ad with stats",
      "Taco Truck" in promo_html and "Your ads" in promo_html)

Q, Q_ID = signup("Outsider", "out@x.io")
r = Q.get(f"/page/{PID}/promote")
check("non-owner GET promote -> 403", r.status_code == 403,
      str(r.status_code))
n_before = dbq("SELECT COUNT(*) c FROM ads WHERE page_id=?", (PID,))[0]["c"]
r = Q.post(f"/page/{PID}/promote", data={"promote_what": "page"})
check("non-owner POST promote -> 403, nothing created",
      r.status_code == 403
      and dbq("SELECT COUNT(*) c FROM ads WHERE page_id=?",
              (PID,))[0]["c"] == n_before)
r = Q.post(f"/page/{PID}/promote/{PAD}/status", data={"status": "paused"})
check("non-owner status switch -> 403, ad untouched",
      r.status_code == 403
      and dbq("SELECT status FROM ads WHERE id=?", (PAD,))[0]["status"]
      == "active")

# the page ad reaches feeds under the page's name
V, V_ID = signup("Viewer", "viewer@x.io")
vh = V.get("/home").text
check("page ad appears in a feed as Sponsored",
      "Sponsored" in vh and "Taco Truck" in vh)

# page owner pauses their own ad -> drops out of the engine
r = POWN.post(f"/page/{PID}/promote/{PAD}/status",
              data={"status": "paused"})
check("owner pause -> 303", r.status_code == 303)
conn = here.db()
still = {a["id"] for a in ads.eligible_ads(conn, V_ID, ads.local_now())}
conn.close()
check("paused page ad out of rotation", PAD not in still)

# owner oversight: pause from the panel takes it out of feeds too
dbx("UPDATE ads SET status='active' WHERE id=?", (PAD,))
r = OWN.post(f"/owner/ads/{PAD}/status", data={"status": "paused"})
check("owner oversight pause -> 303", r.status_code == 303)
check("owner oversight pause applied",
      dbq("SELECT status FROM ads WHERE id=?", (PAD,))[0]["status"]
      == "paused")
conn = here.db()
still = {a["id"] for a in ads.eligible_ads(conn, V_ID, ads.local_now())}
conn.close()
check("oversight-paused ad out of rotation", PAD not in still)

# ---------------- 11. moderation: slur in ad copy flags, never blocks -
r = OWN.post("/owner/ads", data={
    "title": "Bad Words Ad", "body": "this copy has a nigger slur in it"})
check("flagged ad still created", r.status_code == 303)
bad = dbq("SELECT * FROM ads WHERE title='Bad Words Ad'")
check("flagged ad row exists + active",
      len(bad) == 1 and bad[0]["status"] == "active")
flags = dbq("SELECT * FROM moderation_flags WHERE item_type='ad' "
            "AND item_id=? AND status='pending'", (bad[0]["id"],))
check("moderation flag queued for the ad", len(flags) == 1)

# ---------------- 12. owner dashboard: section + stats ---------------
dash = OWN.get("/owner").text
check("dashboard has the 📣 Ads section", "📣 Ads" in dash)
check("dashboard lists the house ad", "Friday Night Showdown" in dash)
check("dashboard lists the page ad for oversight", "Taco Truck" in dash)
check("dashboard shows impression totals",
      "impressions" in dash)

# owner edit round-trip
r = OWN.post(f"/owner/ads/{HA}", data={
    "title": "Friday Night Showdown II", "body": "Updated copy.",
    "link_url": "/lives", "cta_label": "Watch", "weight": "2",
    "schedule": "always", "hour_start": "0", "hour_end": "24"})
check("owner edit -> 303", r.status_code == 303)
row = dbq("SELECT title, weight FROM ads WHERE id=?", (HA,))[0]
check("owner edit applied", row["title"] == "Friday Night Showdown II"
      and row["weight"] == 2)

# ---------------- 13. composer units: trim + link safety --------------
card = ads.compose_card({"id": 9, "kind": "house", "title": "x" * 200,
                         "body": "y" * 400, "media_kind": "image",
                         "media_path": "ads/a.jpg", "cta_label": "Go"})
check("compose: headline trimmed to 70", len(card["title"]) == 70)
check("compose: body trimmed to 160", len(card["body"]) == 160)
check("compose: image -> media layout", card["layout"] == "media")
card = ads.compose_card({"id": 9, "kind": "house", "title": "t",
                         "body": "", "media_kind": "video",
                         "media_path": "ads/a.mp4", "cta_label": ""})
check("compose: video -> video layout", card["layout"] == "video")
card = ads.compose_card({"id": 9, "kind": "house", "title": "t",
                         "body": "", "media_kind": "none",
                         "media_path": "", "cta_label": ""})
check("compose: no media -> text layout + default CTA",
      card["layout"] == "text" and card["cta_label"] == "Learn more")
check("safe_link: javascript: blocked",
      ads.safe_link("javascript:alert(1)") == "#")
check("safe_link: protocol-relative blocked",
      ads.safe_link("//evil.example/x") == "#")
check("safe_link: internal path ok", ads.safe_link("/page/3") == "/page/3")
check("safe_link: https ok",
      ads.safe_link("https://x.io/a") == "https://x.io/a")
check("safe_link: empty -> '#'", ads.safe_link("") == "#")

# ---------------- summary ----------------
fails = [n for n, okk in RESULTS if not okk]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
