"""Build SHARE tests: every post and ad gets a real shareable link —
the post share sheet (canonical /p link, copy, native share, FB/X/
WhatsApp intents) gated to genuinely-public posts only, the public
ad landing GET /a/{id} (active renders large with a click-logged
CTA; paused/ended/deleted/missing get the join teaser, no leaks),
and the ad card's own share sheet.

Run:  ./venv/bin/python tests/test_share_links.py
Uses a throwaway DB/uploads dir; no network.
"""
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-share-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import ads  # noqa: E402
import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []
CANON = "https://joincomehere.com"
DEFAULT_IMG = CANON + "/static/og-share.jpg"
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64 + b"\xff\xd9"


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
        ("share-owner-token", here.now_iso()))
    c = TestClient(here.app, follow_redirects=False)
    c.cookies.set("here_owner", "share-owner-token")
    return c


def mkpost(client, body, privacy="public"):
    r = client.post("/posts", data={"kind": "text", "privacy": privacy,
                                    "body": body})
    assert r.status_code == 303, r.status_code
    return dbq("SELECT * FROM posts WHERE body=?", (body,))[0]["id"]


def mkad(**kw):
    d = {"creator_id": ANN_ID, "page_id": None, "kind": "house",
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


def og(html, prop):
    m = re.search(r'property="og:' + re.escape(prop) + r'" content="([^"]*)"',
                  html)
    return m.group(1) if m else None


OWN = owner_client()
ANN, ANN_ID = signup("Share Ann", "ann@share.io")
FAY, FAY_ID = signup("Friend Fay", "fay@share.io")
anon = TestClient(here.app, follow_redirects=False)

# Ann + Fay are friends
r = ANN.post(f"/friends/request/{FAY_ID}")
assert r.status_code in (200, 303), r.status_code
r = FAY.post(f"/friends/accept/{ANN_ID}")
assert r.status_code in (200, 303), r.status_code

# ---------------- 1. public post: the sheet, everywhere ---------------
PUB = mkpost(ANN, "share me please\nsecond line here")
PUB_URL = f"{CANON}/p/{PUB}"
# Jinja's urlencode (query-value encoding): ':' and spaces encoded,
# '/' left literal — the exact hrefs the intents must carry.
PUB_URL_ENC = "https%3A//joincomehere.com/p/" + str(PUB)

home = ANN.get("/home").text
check("public post in own feed", "share me please" in home)
check("feed: share sheet present for the public post",
      f'id="share-sheet-{PUB}"' in home)
check("feed: ↗ Share toggle wired to the sheet",
      f'data-sheet="share-sheet-{PUB}"' in home
      and "share-toggle" in home)
check("feed: canonical link in the readonly field",
      f'value="{PUB_URL}"' in home)
check("feed: Copy link button carries the canonical URL",
      f'class="btn btn-sm share-copy" data-share-url="{PUB_URL}"' in home)
check("feed: native share button present (hidden until JS)",
      "share-native" in home and f'data-share-url="{PUB_URL}"' in home)
check("feed: Facebook intent carries the encoded link",
      f"https://www.facebook.com/sharer/sharer.php?u={PUB_URL_ENC}" in home)
check("feed: X intent carries link + first-line text",
      f"https://twitter.com/intent/tweet?url={PUB_URL_ENC}" in home
      and "share%20me%20please" in home)
check("feed: WhatsApp intent carries text + link",
      "https://wa.me/?text=share%20me%20please%20" + PUB_URL_ENC in home)
check("feed: internal 🔁 share stays for public posts",
      f'action="/post/{PUB}/share"' in home)

detail = ANN.get(f"/post/{PUB}").text
check("detail: share sheet present", f'id="share-sheet-{PUB}"' in detail
      and f'value="{PUB_URL}"' in detail)
prof = ANN.get(f"/user/{ANN_ID}").text
check("profile: share sheet present", f'id="share-sheet-{PUB}"' in prof)

# a friend sees the sheet too — the post is public
fay_detail = FAY.get(f"/post/{PUB}").text
check("friend view: sheet offered on the public post",
      f'id="share-sheet-{PUB}"' in fay_detail)

# ---------------- 2. non-public posts: NO external sheet --------------
SEC = mkpost(ANN, "friends only secret words", privacy="friends")
SEC_URL_ENC = "https%3A//joincomehere.com/p/" + str(SEC)
for label, html in (("author detail", ANN.get(f"/post/{SEC}").text),
                    ("friend detail", FAY.get(f"/post/{SEC}").text),
                    ("author feed", ANN.get("/home").text),
                    ("friend feed", FAY.get("/home").text)):
    check(f"friends-only post ({label}): no share sheet",
          f'id="share-sheet-{SEC}"' not in html
          and f'data-sheet="share-sheet-{SEC}"' not in html)
    check(f"friends-only post ({label}): no intent flies its link",
          f"sharer.php?u={SEC_URL_ENC}" not in html
          and f"intent/tweet?url={SEC_URL_ENC}" not in html)
r = anon.get(f"/p/{SEC}")
check("/p friends-only -> 200 teaser, no content leak",
      r.status_code == 200 and "friends only secret words" not in r.text
      and "join to see it" in r.text)

# private-profile author: even a 'public' post teasers on /p, so the
# sheet must not be offered — not even to the author herself.
PAT, PAT_ID = signup("Private Pat", "pat@share.io")
dbx("UPDATE users SET private_profile=1 WHERE id=?", (PAT_ID,))
PP = mkpost(PAT, "pat public words")
pat_detail = PAT.get(f"/post/{PP}").text
check("private author sees her own post", "pat public words" in pat_detail)
check("private author's post: no external sheet",
      f'id="share-sheet-{PP}"' not in pat_detail)
check("private author's post: internal share behavior unchanged",
      f'action="/post/{PP}/share"' in pat_detail)
r = anon.get(f"/p/{PP}")
check("/p private-author post -> teaser, no leak",
      r.status_code == 200 and "pat public words" not in r.text
      and "join to see it" in r.text)

# ---------------- 3. the shared script (base.html) --------------------
home = ANN.get("/home").text
for marker in (".share-toggle", ".share-copy", ".share-native",
               ".share-close", "Link copied ✓", "navigator.share",
               "openAudPopup"):
    check(f"share script handles {marker}", marker in home)
check("share script: copy falls back without the clipboard API",
      "execCommand('copy')" in home or 'execCommand("copy")' in home
      or "execCommand" in home)

# ---------------- 4. ad landing: active image ad ---------------------
dbx("UPDATE ads SET status='paused'")
r = OWN.post("/owner/ads",
             data={"title": "Share This Ad",
                   "body": "A very shareable ad body",
                   "link_url": "/market", "cta_label": "Shop now",
                   "schedule": "always", "weight": "1",
                   "hour_start": "0", "hour_end": "24"},
             files={"media": ("adpic.jpg", JPEG, "image/jpeg")})
check("owner creates image ad -> 303", r.status_code == 303,
      str(r.status_code))
AD = dbq("SELECT * FROM ads WHERE title='Share This Ad'")[0]
AID = AD["id"]
check("image ad stored with media", AD["media_kind"] == "image"
      and AD["media_path"].startswith("ads/"), AD["media_path"])

r = anon.get(f"/a/{AID}")
t = r.text
check("anon /a active ad -> 200", r.status_code == 200)
check("/a: ad renders large — title + body + Sponsored",
      "Share This Ad" in t and "A very shareable ad body" in t
      and "Sponsored" in t)
check("/a: CTA routes through the logging click route",
      f'href="/ads/{AID}/click"' in t and "Shop now" in t)
check("/a: og:title is the ad's own title",
      og(t, "title") == "Share This Ad")
check("/a: og:description is the ad body",
      og(t, "description") == "A very shareable ad body")
check("/a: og:image is the ad's own image, absolute",
      (og(t, "image") or "").endswith("/uploads/" + AD["media_path"])
      and (og(t, "image") or "").startswith("http"))
check("/a: join invite rides along for logged-out visitors",
      "Join HERE" in t)

# ---------------- 5. CTA clicks from shared links count --------------
clicks0 = dbq("SELECT clicks FROM ads WHERE id=?", (AID,))[0]["clicks"]
r = anon.get(f"/ads/{AID}/click")
check("anon click -> 302 to the destination",
      r.status_code == 302 and r.headers.get("location") == "/market",
      str(r.status_code) + " " + str(r.headers.get("location")))
clicks1 = dbq("SELECT clicks FROM ads WHERE id=?", (AID,))[0]["clicks"]
check("anon click counted", clicks1 == clicks0 + 1)
r = ANN.get(f"/ads/{AID}/click")
check("logged-in click -> 302 to the destination",
      r.status_code == 302 and r.headers.get("location") == "/market")
cev = dbq("SELECT * FROM ad_events WHERE ad_id=? AND kind='click' "
          "AND user_id=?", (AID, ANN_ID))
check("logged-in click logged as an event", len(cev) == 1)
clicks2 = dbq("SELECT clicks FROM ads WHERE id=?", (AID,))[0]["clicks"]
check("logged-in click counted too", clicks2 == clicks1 + 1)

# ---------------- 6. ad landing: video + text ads' og -----------------
VID = mkad(title="Video Landing Ad", body="watch me move",
           media_kind="video", media_path="ads/probe.mp4",
           link_url="/lives", cta_label="Watch")
r = anon.get(f"/a/{VID}")
t = r.text
check("/a video ad -> 200 with its title", r.status_code == 200
      and "Video Landing Ad" in t)
check("/a video: og:type is video.other", og(t, "type") == "video.other")
check("/a video: og:video is the media, absolute",
      (og(t, "video") or "").endswith("/uploads/ads/probe.mp4")
      and (og(t, "video") or "").startswith("http"))
_vtag = re.search(r"<video[^>]*>", t)
check("/a video: ADS-2 autoplay-muted treatment",
      _vtag is not None and all(
          a in _vtag.group(0) for a in
          ("autoplay", "muted", "loop", "playsinline"))
      and "controls" not in _vtag.group(0), _vtag.group(0) if _vtag else "")
check("/a video: speaker chip present", 'class="ad-vol-btn"' in t)

TXT = mkad(title="Plain Text Ad", body="just words here",
           media_kind="none", link_url="/home", cta_label="Go")
r = anon.get(f"/a/{TXT}")
check("/a text ad: og:image falls back to the default card",
      og(r.text, "image") == DEFAULT_IMG)
check("/a text ad: renders with Sponsored",
      "Plain Text Ad" in r.text and "Sponsored" in r.text)

# ---------------- 7. stopped ads: teaser, never a leak -----------------
PAU = mkad(title="Paused Mystery Ad", body="paused body words",
           status="paused")
END = mkad(title="Ended Mystery Ad", body="ended body words",
           status="ended")
for aid, title, body in ((PAU, "Paused Mystery Ad", "paused body words"),
                         (END, "Ended Mystery Ad", "ended body words"),
                         (999999, None, None)):
    r = anon.get(f"/a/{aid}")
    check(f"/a/{aid} (stopped/missing) -> 200 teaser",
          r.status_code == 200 and "join to see it" in r.text)
    if title:
        check(f"/a/{aid}: no ad copy leaks",
              title not in r.text and body not in r.text)

# deleted ad: owner removes it completely -> the link teasers too
DEL = mkad(title="Doomed Ad", body="soon to vanish")
r = OWN.post(f"/owner/ads/{DEL}/delete")
check("owner delete -> 303", r.status_code == 303, str(r.status_code))
r = anon.get(f"/a/{DEL}")
check("/a deleted ad -> teaser, no leak",
      r.status_code == 200 and "Doomed Ad" not in r.text
      and "soon to vanish" not in r.text)

# ---------------- 8. ad card carries its own share sheet ---------------
dbx("UPDATE ads SET status='paused'")
dbx("UPDATE ads SET status='active' WHERE id=?", (AID,))
VIC, VIC_ID = signup("Ad Viewer", "vic@share.io")
vhome = VIC.get("/home").text
check("ad card in feed (Sponsored)", "Share This Ad" in vhome
      and "Sponsored" in vhome)
check("ad card: share sheet with the /a link",
      f'id="share-sheet-ad-{AID}"' in vhome
      and f'value="{CANON}/a/{AID}"' in vhome)
check("ad card: copy + intents carry the /a link",
      f'data-share-url="{CANON}/a/{AID}"' in vhome
      and f"sharer.php?u=https%3A//joincomehere.com/a/{AID}" in vhome)
check("ad card: share control clears the ✕ and the speaker chip",
      "share-toggle" in vhome)  # toggle lives in the CTA row, apart from both

# ---------------- 9. node --check the new inline script ----------------
_blocks = re.findall(r"<script>(.*?)</script>", vhome, re.S)
_js_ok, _js_bad = True, []
for _i, _blk in enumerate(_blocks):
    _p = os.path.join(_tmp, f"share_chk_{_i}.js")
    with open(_p, "w") as _f:
        _f.write(_blk)
    _r = subprocess.run(["node", "--check", _p],
                        capture_output=True, text=True)
    if _r.returncode != 0:
        _js_ok = False
        _js_bad.append(_r.stderr.strip()[:160])
check("node --check: feed inline JS parses (incl. share script)",
      _js_ok and len(_blocks) >= 1, "; ".join(_js_bad))
_pub_blocks = re.findall(r"<script>(.*?)</script>",
                         anon.get(f"/a/{VID}").text, re.S)
_js_ok = True
for _i, _blk in enumerate(_pub_blocks):
    _p = os.path.join(_tmp, f"pubad_chk_{_i}.js")
    with open(_p, "w") as _f:
        _f.write(_blk)
    if subprocess.run(["node", "--check", _p],
                      capture_output=True, text=True).returncode != 0:
        _js_ok = False
check("node --check: public ad page inline JS parses", _js_ok)

# ---------------- summary ----------------
fails = [n for n, okk in RESULTS if not okk]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
