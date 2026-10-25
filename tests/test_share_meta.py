"""Build OG tests for HERE: link-share previews (Open Graph / Twitter
cards / meta description) — site-wide defaults from base.html + auth
heads, per-content overrides on the public share pages (/p, /l, /u, /m)
and /market, and the bundled default share image.

Run:  ./venv/bin/python tests/test_share_meta.py
Uses a throwaway DB/uploads dir; no network.
"""
import os
import re
import sqlite3
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-og-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []

DEFAULT_IMG = "https://joincomehere.com/static/og-share.jpg"
PITCH = "HERE is where the fun lives"


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


def meta(html, attr, name):
    m = re.search(attr + r'="' + re.escape(name) + r'" content="([^"]*)"',
                  html)
    return m.group(1) if m else None


def og(html, prop):
    return meta(html, "property", "og:" + prop)


def tw(html, name):
    return meta(html, "name", "twitter:" + name)


def full_default_set(name, html):
    """The site-wide default card: every tag present, image absolute."""
    check(f"{name}: meta description present",
          (meta(html, "name", "description") or "").startswith(PITCH))
    check(f"{name}: og:type website", og(html, "type") == "website")
    check(f"{name}: og:site_name HERE", og(html, "site_name") == "HERE")
    check(f"{name}: og:title default",
          og(html, "title") == "HERE — Come Get HERE")
    check(f"{name}: og:description is the pitch",
          (og(html, "description") or "").startswith(PITCH))
    check(f"{name}: og:image is the absolute default card",
          og(html, "image") == DEFAULT_IMG)
    check(f"{name}: og:image hints (w/h/type)",
          og(html, "image:width") == "1320"
          and og(html, "image:height") == "2417"
          and og(html, "image:type") == "image/jpeg")
    check(f"{name}: twitter card summary_large_image",
          tw(html, "card") == "summary_large_image")
    check(f"{name}: twitter title/description mirror og",
          tw(html, "title") == og(html, "title")
          and tw(html, "description") == og(html, "description"))
    check(f"{name}: twitter:image absolute default",
          tw(html, "image") == DEFAULT_IMG)


def signup(name, email):
    c = TestClient(here.app, follow_redirects=False)
    r = c.post("/signup", data={"name": name, "email": email,
                                "password": "pw123456"})
    assert r.status_code == 303, (name, r.status_code)
    uid = dbq("SELECT id FROM users WHERE email=?", (email,))[0]["id"]
    return c, uid


JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64 + b"\xff\xd9"

SUE, SUEID = signup("Share Sue", "sue@og.io")

# public photo post (content image wins) + public text post (no media ->
# default card) + a post whose body needs HTML-escaping in the meta tags
r = SUE.post("/posts", data={"kind": "photo", "privacy": "public",
                             "body": "og photo post body"},
             files={"media": ("pub.jpg", JPEG, "image/jpeg")})
assert r.status_code == 303, r.status_code
r = SUE.post("/posts", data={"kind": "text", "privacy": "public",
                             "body": "og text only post body"})
assert r.status_code == 303, r.status_code
r = SUE.post("/posts", data={"kind": "text", "privacy": "public",
                             "body": 'meta <escape> "test" & stuff'})
assert r.status_code == 303, r.status_code

photo_post = dbq("SELECT * FROM posts WHERE body=?",
                 ("og photo post body",))[0]
text_post = dbq("SELECT * FROM posts WHERE body=?",
                ("og text only post body",))[0]
esc_post = dbq("SELECT * FROM posts WHERE body LIKE 'meta %'")[0]

# listing with a photo + listing without one
r = SUE.post("/market",
             data={"title": "OG Test Bike", "price": "25",
                   "category": "Other", "condition": "Good",
                   "description": "a very shareable bike",
                   "location": ""},
             files={"photos": ("bike.jpg", JPEG, "image/jpeg")})
assert r.status_code == 303, r.status_code
lid_photo = int(r.headers["location"].split("/")[2].split("?")[0])
r = SUE.post("/market",
             data={"title": "OG Plain Chair", "price": "0",
                   "category": "Other", "condition": "Good",
                   "description": "", "location": ""})
assert r.status_code == 303, r.status_code
lid_plain = int(r.headers["location"].split("/")[2].split("?")[0])

anon = TestClient(here.app, follow_redirects=False)

# ---------------- bundled share image ----------------
r = anon.get("/static/og-share.jpg")
check("static og-share.jpg serves 200", r.status_code == 200)
check("static og-share.jpg is the bundled portrait (645473 bytes)",
      len(r.content) == 645473, str(len(r.content)))
check("static og-share.jpg is a JPEG",
      r.content[:2] == b"\xff\xd8")

# ---------------- site-wide defaults: login / signup / teaser ----------
for path in ("/login", "/signup"):
    r = anon.get(path)
    check(f"anon {path} -> 200", r.status_code == 200)
    full_default_set(f"anon {path}", r.text)

r = anon.get("/p/999999")  # missing post -> join teaser
check("anon /p missing -> 200 teaser", r.status_code == 200)
full_default_set("teaser", r.text)

# ---------------- base.html defaults + overrides (logged in) ------------
r = SUE.get("/home")
check("logged-in /home -> 200", r.status_code == 200)
full_default_set("/home (base defaults)", r.text)

r = SUE.get("/market")
check("logged-in /market -> 200", r.status_code == 200)
check("/market: og:title overridden for the Market",
      og(r.text, "title") == "HERE Market — Buy &amp; Sell on HERE")
check("/market: og:description overridden",
      (og(r.text, "description") or "").startswith("Buy, sell and find"))
check("/market: twitter:title mirrors the override",
      tw(r.text, "title") == og(r.text, "title"))
check("/market: og:image still the absolute default card",
      og(r.text, "image") == DEFAULT_IMG)

# ---------------- /p/{post}: content wins, else default card -----------
r = anon.get(f"/p/{photo_post['id']}")
t = r.text
check("anon /p photo post -> 200", r.status_code == 200)
check("/p photo: og:title carries the author",
      og(t, "title") == "Share Sue on HERE")
check("/p photo: og:description is the post body snippet",
      og(t, "description") == "og photo post body")
check("/p photo: og:image is the post photo, absolute",
      (og(t, "image") or "").startswith("http")
      and (og(t, "image") or "").endswith("/uploads/" + photo_post["media"]))
check("/p photo: twitter:image matches og:image",
      tw(t, "image") == og(t, "image"))
check("/p photo: twitter card present",
      tw(t, "card") == "summary_large_image")
check("/p photo: og:site_name + type present",
      og(t, "site_name") == "HERE" and og(t, "type") == "website")

r = anon.get(f"/p/{text_post['id']}")
t = r.text
check("anon /p text post -> 200", r.status_code == 200)
check("/p text: og:description is the post body snippet",
      og(t, "description") == "og text only post body")
check("/p text: og:image falls back to the default card",
      og(t, "image") == DEFAULT_IMG)
check("/p text: fallback image carries w/h/type hints",
      og(t, "image:width") == "1320" and og(t, "image:type") == "image/jpeg")
check("/p text: twitter:image falls back too",
      tw(t, "image") == DEFAULT_IMG)

r = anon.get(f"/p/{esc_post['id']}")
t = r.text
check("/p escape: og:description is HTML-escaped",
      og(t, "description") is not None
      and "&lt;escape&gt;" in og(t, "description")
      and "<escape>" not in og(t, "description"))

# ---------------- /u/{user} --------------------------------------------
r = anon.get(f"/u/{SUEID}")
t = r.text
check("anon /u -> 200", r.status_code == 200)
check("/u: og:title carries the user name",
      og(t, "title") == "Share Sue on HERE")
check("/u: og:description names the user",
      (og(t, "description") or "").startswith("Share Sue is on HERE"))
check("/u: og:image absolute (avatar or default card)",
      (og(t, "image") or "").startswith("http"))
check("/u: twitter card present",
      tw(t, "card") == "summary_large_image"
      and tw(t, "title") == og(t, "title"))

# ---------------- /m/{listing} -----------------------------------------
r = anon.get(f"/m/{lid_photo}")
t = r.text
check("anon /m photo listing -> 200", r.status_code == 200)
check("/m photo: og:title carries the listing title + price",
      (og(t, "title") or "").startswith("OG Test Bike")
      and "$25" in (og(t, "title") or ""))
check("/m photo: og:description is the listing description",
      og(t, "description") == "a very shareable bike")
check("/m photo: og:image is the listing photo, absolute",
      (og(t, "image") or "").startswith("http")
      and "/uploads/market/" in (og(t, "image") or ""))
check("/m photo: twitter:image matches og:image",
      tw(t, "image") == og(t, "image"))

r = anon.get(f"/m/{lid_plain}")
t = r.text
check("anon /m plain listing -> 200", r.status_code == 200)
check("/m plain: og:title carries the listing title",
      (og(t, "title") or "").startswith("OG Plain Chair"))
check("/m plain: og:description falls back to the title",
      og(t, "description") == "OG Plain Chair")
check("/m plain: og:image falls back to the default card",
      og(t, "image") == DEFAULT_IMG
      or (og(t, "image") or "").startswith("http"))  # seller avatar ok too
check("/m plain: twitter card present",
      tw(t, "card") == "summary_large_image")

# ---------------- summary ----------------
fails = [n for n, okk in RESULTS if not okk]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
