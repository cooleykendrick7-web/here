"""HERE Market tests: local buy/sell listings — create validation,
browse/search/sort, block + privacy gates, sold flow + saver
notifications, saves, message-seller snapshot cards, feed cross-post,
moderation flags, the public /m door, edits, and migrations.

Run:  ./venv/bin/python tests/test_market.py
Uses a throwaway DB/uploads dir; no network.
"""
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-mkt-")
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
    conn = sqlite3.connect(os.environ["HERE_DB"])
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def signup(name, email):
    c = TestClient(here.app, follow_redirects=False)
    r = c.post("/signup", data={"name": name, "email": email,
                                "password": "pw123456"})
    assert r.status_code == 303, (name, r.status_code)
    uid = dbq("SELECT id FROM users WHERE email=?", (email,))[0]["id"]
    return c, uid


JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64 + b"\xff\xd9"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
TXT = b"definitely not an image"


def listing_data(**kw):
    d = {"title": "Thing", "price": "25", "category": "Other",
         "condition": "Good", "description": "", "location": ""}
    d.update(kw)
    return d


def make_listing(client, **kw):
    files = kw.pop("files", None)
    r = client.post("/market", data=listing_data(**kw), files=files)
    assert r.status_code == 303, r.status_code
    loc = r.headers.get("location", "")
    assert loc.startswith("/market/"), loc
    return int(loc.split("/")[2].split("?")[0])


def listing_row(lid):
    rows = dbq("SELECT * FROM market_listings WHERE id=?", (lid,))
    return rows[0] if rows else None


S, SID = signup("Seller Sam", "sam@mkt.io")
B, BID = signup("Buyer Beth", "beth@mkt.io")
C, CID = signup("Third Tom", "tom@mkt.io")
ANON = TestClient(here.app, follow_redirects=False)

# ---------------- price parsing + money formatting (units) ----------------
check("price '25' -> 2500", here._parse_price("25") == 2500)
check("price '25.5' -> 2550", here._parse_price("25.5") == 2550)
check("price '25.50' -> 2550", here._parse_price("25.50") == 2550)
check("price '0' -> 0 (FREE)", here._parse_price("0") == 0)
check("price at cap '999999' ok", here._parse_price("999999") == 99999900)
check("money 2500 -> $25", here._money(2500) == "$25")
check("money 2550 -> $25.50", here._money(2550) == "$25.50")
check("money 0 -> FREE", here._money(0) == "FREE")
check("money 5 -> $0.05", here._money(5) == "$0.05")
for bad in ("", "abc", "-5", "+5", "25.555", "1,000", "1000000", "25.",
            ".5", "2 5", "$25"):
    try:
        here._parse_price(bad)
        check(f"price {bad!r} rejected", False)
    except ValueError:
        check(f"price {bad!r} rejected", True)

# ---------------- create validation matrix ----------------
n0 = len(dbq("SELECT id FROM market_listings"))
r = S.post("/market", data=listing_data(title=""))
check("missing title refused (redirect back to form)",
      r.status_code == 303 and "/market/new" in r.headers["location"])
r = S.post("/market", data=listing_data(price="abc"))
check("garbage price refused", r.status_code == 303
      and "/market/new" in r.headers["location"])
r = S.post("/market", data=listing_data(price="-5"))
check("negative price refused", r.status_code == 303
      and "/market/new" in r.headers["location"])
r = S.post("/market", data=listing_data(price="1000000"))
check("over-cap price refused", r.status_code == 303
      and "/market/new" in r.headers["location"])
r = S.post("/market", data=listing_data(category="Bogus"))
check("bad category refused", r.status_code == 303
      and "/market/new" in r.headers["location"])
r = S.post("/market", data=listing_data(condition="Minty"))
check("bad condition refused", r.status_code == 303
      and "/market/new" in r.headers["location"])
five = [("photos", (f"p{i}.jpg", JPEG, "image/jpeg")) for i in range(5)]
r = S.post("/market", data=listing_data(), files=five)
check("5 photos refused", r.status_code == 303
      and "/market/new" in r.headers["location"])
r = S.post("/market", data=listing_data(),
           files=[("photos", ("note.txt", TXT, "text/plain"))])
check("non-image photo refused", r.status_code == 303
      and "/market/new" in r.headers["location"])
check("no listing rows leaked from refused creates",
      len(dbq("SELECT id FROM market_listings")) == n0)

lid_free = make_listing(S, title="Free Couch", price="0",
                        category="Free Stuff",
                        description="Come haul it away")
row = listing_row(lid_free)
check("'0' price listing stored as 0 cents", row["price_cents"] == 0)
r = B.get(f"/market/{lid_free}")
check("FREE listing shows FREE on detail", "FREE" in r.text)

# ---------------- browse: search / filter / sort ----------------
lid_bike = make_listing(S, title="Red Bike", price="25.50",
                        category="Vehicles",
                        description="Rides great, new tires",
                        location="Jackson, MS",
                        files=[("photos", ("bike.jpg", JPEG, "image/jpeg"))])
lid_lamp = make_listing(S, title="Desk Lamp", price="10",
                        category="Home & Garden", description="Warm light")
lid_shoes = make_listing(C, title="Fresh Kicks", price="80",
                         category="Shoes", description="Worn once")

r = B.get("/market")
check("browse 200 + shows active listings",
      r.status_code == 200 and "Red Bike" in r.text
      and "Desk Lamp" in r.text and "Fresh Kicks" in r.text)
check("browse price badge formatted", "$25.50" in r.text and "$10" in r.text)
check("browse seller chip links to profile",
      f'href="/user/{SID}"' in r.text and "Seller Sam" in r.text)
r = B.get("/market?q=Bike")
check("search matches title", "Red Bike" in r.text
      and "Desk Lamp" not in r.text)
r = B.get("/market?q=tires")
check("search matches description", "Red Bike" in r.text
      and "Desk Lamp" not in r.text)
r = B.get("/market?category=Shoes")
check("category filter", "Fresh Kicks" in r.text
      and "Red Bike" not in r.text)
r = B.get("/market?sort=price_asc")
i10, i2550, i80 = (r.text.find("$10"), r.text.find("$25.50"),
                   r.text.find("$80"))
check("sort price_asc orders $10 < $25.50 < $80",
      -1 < i10 < i2550 < i80)
r = B.get("/market?sort=price_desc")
i10, i2550, i80 = (r.text.find("$10"), r.text.find("$25.50"),
                   r.text.find("$80"))
check("sort price_desc orders $80 > $25.50 > $10",
      -1 < i80 < i2550 < i10)
r = B.get(f"/market?seller={SID}")
check("seller filter shows only that seller's listings",
      "Red Bike" in r.text and "Fresh Kicks" not in r.text)
r = S.get("/user/" + str(SID))
check("profile 🛒 Selling strip shows listings + count",
      "🛒 Selling" in r.text and "Red Bike" in r.text)
r = B.get("/market/new")
check("sell form 200 with categories + conditions",
      r.status_code == 200 and "Vehicles" in r.text
      and "Like new" in r.text)
r = B.get("/market/mine")
check("buyer's /market/mine is empty-state, not an error",
      r.status_code == 200 and "not selling anything" in r.text)
r = S.get("/market/mine")
check("/market/mine shows seller's listings + quick actions",
      "Red Bike" in r.text and "Mark SOLD" in r.text)

# ---------------- save toggle ----------------
r = B.post(f"/market/{lid_bike}/save")
check("save redirects back", r.status_code == 303)
check("save row exists", len(dbq(
    "SELECT * FROM market_saves WHERE user_id=? AND listing_id=?",
    (BID, lid_bike))) == 1)
r = B.post(f"/market/{lid_bike}/save")
check("second save tap unsaves (toggle)", len(dbq(
    "SELECT * FROM market_saves WHERE user_id=? AND listing_id=?",
    (BID, lid_bike))) == 0)
B.post(f"/market/{lid_bike}/save")
B.post(f"/market/{lid_bike}/save")
B.post(f"/market/{lid_bike}/save")
check("toggle is idempotent in effect (odd taps = saved once)",
      len(dbq("SELECT * FROM market_saves WHERE user_id=? AND listing_id=?",
              (BID, lid_bike))) == 1)
r = B.get("/market/saved")
check("/market/saved shows the save", "Red Bike" in r.text)
r = S.post(f"/market/{lid_bike}/save")
check("seller saving own listing -> 403", r.status_code == 403)
r = B.get(f"/market/{lid_bike}")
check("detail shows Saved state for the saver", "Saved" in r.text)

# ---------------- message seller ----------------
msgs0 = len(dbq("SELECT id FROM messages"))
r = B.post(f"/market/{lid_bike}/message", data={})
check("message seller redirects into the thread",
      r.status_code == 303
      and r.headers["location"].startswith(f"/messages/{SID}"))
mrows = dbq("SELECT * FROM messages WHERE listing_id=?", (lid_bike,))
check("exactly one listing message delivered", len(mrows) == 1)
m = mrows[0]
check("message snapshot fields set (title + price at send time)",
      m["listing_title"] == "Red Bike" and m["listing_price_cents"] == 2550)
check("message body is the prefilled availability question",
      m["body"] == "Hi! Is this still available?")
check("message goes buyer -> seller",
      m["sender_id"] == BID and m["receiver_id"] == SID)
r = B.post(f"/market/{lid_bike}/message",
           data={"body": "Would you take $20?"})
mrows = dbq("SELECT * FROM messages WHERE listing_id=? ORDER BY id",
            (lid_bike,))
check("second inquiry lands in the SAME thread (no new conversation)",
      len(mrows) == 2 and len(dbq(
          "SELECT id FROM messages WHERE (sender_id=? AND receiver_id=?) "
          "OR (sender_id=? AND receiver_id=?)",
          (BID, SID, SID, BID))) == 2)
check("total messages grew by exactly the two inquiries",
      len(dbq("SELECT id FROM messages")) == msgs0 + 2)
r = B.get(f"/messages/{SID}")
check("thread renders the listing card above the bubble",
      "listing-card" in r.text and "Red Bike" in r.text
      and "$25.50" in r.text and f'href="/market/{lid_bike}"' in r.text)
r = S.post(f"/market/{lid_bike}/message", data={})
check("seller messaging own listing -> 403", r.status_code == 403)
r = S.get("/messages")
check("inbox preview carries the 🛒 Listing prefix",
      "🛒 Listing:" in r.text)

# ---------------- sold flow ----------------
r = B.post(f"/market/{lid_bike}/sold")
check("buyer marking sold -> 403", r.status_code == 403)
r = B.post(f"/market/{lid_bike}/edit", data=listing_data(title="Hax"))
check("buyer editing -> 403", r.status_code == 403)
r = B.get(f"/market/{lid_bike}/edit")
check("buyer opening edit form -> 403", r.status_code == 403)
r = B.post(f"/market/{lid_bike}/delete")
check("buyer deleting -> 403", r.status_code == 403)
check("listing untouched by buyer attempts",
      listing_row(lid_bike)["status"] == "active")

C.post(f"/market/{lid_bike}/save")  # Tom saves it too
notif0 = len(dbq("SELECT id FROM notifications WHERE user_id=? "
                 "AND kind='market_sold'", (BID,)))
r = S.post(f"/market/{lid_bike}/sold")
check("seller marks sold -> redirect", r.status_code == 303)
row = listing_row(lid_bike)
check("status sold + sold_at set",
      row["status"] == "sold" and row["sold_at"])
n_b = dbq("SELECT * FROM notifications WHERE user_id=? AND kind='market_sold'",
          (BID,))
n_c = dbq("SELECT * FROM notifications WHERE user_id=? AND kind='market_sold'",
          (CID,))
check("saver Beth notified exactly once on sold",
      len(n_b) == notif0 + 1
      and "marked SOLD: Red Bike" in n_b[-1]["text"])
check("saver Tom notified on sold", len(n_c) == 1)
check("seller NOT notified about own sale", len(dbq(
    "SELECT id FROM notifications WHERE user_id=? AND kind='market_sold'",
    (SID,))) == 0)
r = B.get("/market")
check("sold hidden from browse by default", "Red Bike" not in r.text)
r = B.get("/market?sold=1")
check("sold shown with sold=1, badged SOLD",
      "Red Bike" in r.text and "SOLD" in r.text)
r = B.get(f"/market/{lid_bike}")
check("sold detail shows SOLD banner", "SOLD" in r.text)
r = S.post(f"/market/{lid_bike}/sold")
row = listing_row(lid_bike)
check("re-list clears sold_at, back to active",
      row["status"] == "active" and not row["sold_at"])
check("re-list sends NO notification", len(dbq(
    "SELECT id FROM notifications WHERE user_id=? AND kind='market_sold'",
    (BID,))) == notif0 + 1)
S.post(f"/market/{lid_bike}/sold")
check("second sold transition notifies again (once per transition)",
      len(dbq("SELECT id FROM notifications WHERE user_id=? "
              "AND kind='market_sold'", (BID,))) == notif0 + 2)
S.post(f"/market/{lid_bike}/sold")  # leave it active for later sections

# ---------------- feed cross-post ----------------
posts0 = len(dbq("SELECT id FROM posts WHERE user_id=?", (SID,)))
lid_post = make_listing(S, title="Oak Table", price="40",
                        category="Furniture",
                        description="Solid oak, seats six",
                        share_to_feed="1")
prows = dbq("SELECT * FROM posts WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (SID,))
check("share_to_feed=1 creates exactly one feed post",
      len(dbq("SELECT id FROM posts WHERE user_id=?", (SID,))) == posts0 + 1)
p = prows[0]
check("feed post body carries title, price and the market link",
      "For sale: Oak Table" in p["body"] and "$40" in p["body"]
      and f"/market/{lid_post}" in p["body"])
check("feed post is a text post when the listing has no photo",
      p["kind"] == "text")
lid_quiet = make_listing(S, title="Quiet Plant", price="5",
                         category="Home & Garden")
check("no share checkbox -> no feed post",
      len(dbq("SELECT id FROM posts WHERE user_id=?", (SID,))) == posts0 + 1)
lid_photo = make_listing(
    S, title="Photo Post Item", price="15", category="Other",
    share_to_feed="1",
    files=[("photos", ("x.jpg", JPEG, "image/jpeg"))])
p2 = dbq("SELECT * FROM posts WHERE user_id=? ORDER BY id DESC LIMIT 1",
         (SID,))[0]
check("photo listing cross-posts as a photo post with the photo",
      p2["kind"] == "photo" and p2["media"].startswith("market/"))

# ---------------- edit flow ----------------
lid_edit = make_listing(
    S, title="Edit Me", price="30", category="Electronics",
    description="before",
    files=[("photos", ("a.jpg", JPEG, "image/jpeg")),
           ("photos", ("b.png", PNG, "image/png"))])
row = listing_row(lid_edit)
photos_before = here._market_photos(row)
check("edit-target starts with 2 photos", len(photos_before) == 2)
r = S.get(f"/market/{lid_edit}/edit")
check("edit form prefilled", r.status_code == 200
      and "Edit Me" in r.text and 'value="30"' in r.text)
r = S.post(f"/market/{lid_edit}/edit",
           data=listing_data(title="Edited Title", price="12.75",
                             category="Electronics", description="after"),
           files=[("photos", ("c.jpg", JPEG, "image/jpeg"))])
# remove the first photo in a second pass (multipart remove + add are
# also exercised together below)
row = listing_row(lid_edit)
check("edit updates title + price",
      row["title"] == "Edited Title" and row["price_cents"] == 1275)
check("edit appends a photo (now 3)",
      len(here._market_photos(row)) == 3)
keep = here._market_photos(row)
r = S.post(f"/market/{lid_edit}/edit",
           data=listing_data(title="Edited Title", price="12.75",
                             category="Electronics", description="after")
           | {"remove_photo": keep[0]},
           files=[("photos", ("d.jpg", JPEG, "image/jpeg")),
                  ("photos", ("e.jpg", JPEG, "image/jpeg"))])
row = listing_row(lid_edit)
photos_now = here._market_photos(row)
check("remove-one + add-two in one edit lands at the 4 cap",
      len(photos_now) == 4 and keep[0] not in photos_now)
r = S.post(f"/market/{lid_edit}/edit",
           data=listing_data(title="Edited Title", price="12.75",
                             category="Electronics"),
           files=[("photos", ("f.jpg", JPEG, "image/jpeg"))])
check("5th photo on edit refused (back to edit form)",
      r.status_code == 303
      and f"/market/{lid_edit}/edit" in r.headers["location"])
check("photos unchanged after refused edit",
      len(here._market_photos(listing_row(lid_edit))) == 4)

# ---------------- block gates ----------------
lid_block = make_listing(C, title="Toms Guitar", price="120",
                         category="Other", description="Six string")
r = B.get("/market")
check("before block: Beth sees Tom's listing", "Toms Guitar" in r.text)
B.post(f"/block/{CID}")
r = B.get("/market")
check("blocked: listing hidden from browse", "Toms Guitar" not in r.text)
r = B.get(f"/market/{lid_block}")
check("blocked: detail is the unavailable page",
      r.status_code == 200 and "Toms Guitar" not in r.text)
r = B.post(f"/market/{lid_block}/message", data={})
check("blocked: message seller refused (no thread redirect)",
      r.status_code == 303 and "/messages" in r.headers["location"]
      and f"/messages/{CID}" not in r.headers["location"])
check("blocked: no message was delivered", len(dbq(
    "SELECT id FROM messages WHERE sender_id=? AND receiver_id=?",
    (BID, CID))) == 0)
r = C.get(f"/user/{BID}")
check("block is both ways: Tom gets the unavailable profile too",
      r.status_code == 200 and "Buyer Beth" not in r.text)
r = C.post(f"/market/{lid_block}/save")
check("seller-side save of own listing still 403 while blocked pair exists",
      r.status_code == 403)
B.post(f"/unblock/{CID}")
r = B.get(f"/market/{lid_block}")
check("after unblock the listing is visible again",
      "Toms Guitar" in r.text)

# ---------------- private-profile seller ----------------
P, PID = signup("Private Pam", "pam@mkt.io")
P.post("/settings/privacy", data={"private_profile": "1"})
lid_priv = make_listing(P, title="Pams Secret Sofa", price="60",
                        category="Furniture",
                        description="Comfy and quiet")
r = B.get("/market")
check("private seller's listing IS public in browse",
      "Pams Secret Sofa" in r.text)
r = B.get(f"/market/{lid_priv}")
check("private seller's listing detail opens",
      r.status_code == 200 and "Pams Secret Sofa" in r.text)
r = B.get(f"/user/{PID}")
check("...while her profile itself stays locked",
      "This profile is private" in r.text
      and "Pams Secret Sofa" not in r.text)
r = ANON.get(f"/m/{lid_priv}")
check("public /m shows the private seller's listing too",
      r.status_code == 200 and "Pams Secret Sofa" in r.text)

# ---------------- public /m door ----------------
r = ANON.get(f"/m/{lid_lamp}")
check("/m active listing 200 logged-out", r.status_code == 200)
check("/m shows price, title, description, seller name",
      "Desk Lamp" in r.text and "$10" in r.text
      and "Warm light" in r.text and "Seller Sam" in r.text)
check("/m has the free-join CTA + OG title tag",
      "Join HERE — it's free" in r.text and 'property="og:title"' in r.text)
r = ANON.get(f"/m/{lid_bike}")
check("/m OG image present when the listing has a photo",
      'property="og:image"' in r.text and "/uploads/market/" in r.text)
S.post(f"/market/{lid_lamp}/sold")
r = ANON.get(f"/m/{lid_lamp}")
check("/m sold listing still public, SOLD banner",
      r.status_code == 200 and "Desk Lamp" in r.text and "SOLD" in r.text)
S.post(f"/market/{lid_lamp}/sold")  # back to active

# ---------------- delete + gone discipline ----------------
lid_gone = make_listing(S, title="Doomed Trinket", price="3",
                        category="Other", description="bye soon",
                        share_to_feed="1")
B.post(f"/market/{lid_gone}/message", data={})
B.post(f"/market/{lid_gone}/save")
S.post(f"/market/{lid_gone}/delete")
check("delete is soft (row stays, status deleted)",
      listing_row(lid_gone)["status"] == "deleted")
r = B.get("/market")
check("deleted gone from browse", "Doomed Trinket" not in r.text)
r = B.get(f"/market/{lid_gone}")
check("deleted detail = friendly gone page (200)",
      r.status_code == 200 and "This listing is gone" in r.text)
r = ANON.get(f"/m/{lid_gone}")
check("deleted /m = generic teaser, zero leaks",
      r.status_code == 200 and "Doomed Trinket" not in r.text
      and "Seller Sam" not in r.text)
r = ANON.get("/m/999999")
check("missing /m id = generic teaser too",
      r.status_code == 200 and "Join HERE" in r.text)
r = B.get("/market/saved")
check("deleted listing excluded from saved page",
      "Doomed Trinket" not in r.text)
check("...and its save row was cleaned lazily", len(dbq(
    "SELECT * FROM market_saves WHERE listing_id=?", (lid_gone,))) == 0)
check("feed post survives the listing delete", len(dbq(
    "SELECT id FROM posts WHERE body LIKE ?", (f"%/market/{lid_gone}%",)))
    == 1)
r = B.get(f"/messages/{SID}")
check("thread card survives delete with SNAPSHOT title/price",
      "listing-card" in r.text and "Doomed Trinket" in r.text
      and "$3" in r.text)
check("snapshot card thumb falls back to placeholder after delete",
      "listing-thumb-ph" in r.text)
r = S.get("/market/mine")
check("deleted gone from /market/mine", "Doomed Trinket" not in r.text)
r = S.get(f"/user/{SID}")
check("deleted gone from profile strip (mini-card anchor removed; "
      "the surviving feed post may still mention it)",
      f'class="mkt-mini" href="/market/{lid_gone}"' not in r.text)

# ---------------- moderation ----------------
lid_flag = make_listing(S, title="This shit lamp", price="9",
                        category="Home & Garden",
                        description="totally normal lamp")
frow = dbq("SELECT * FROM moderation_flags WHERE item_type='listing' "
           "AND item_id=?", (lid_flag,))
check("flagged title creates a 'listing' flag row",
      len(frow) == 1 and "profanity" in frow[0]["reasons"])
r = B.get(f"/market/{lid_flag}")
check("flagged listing still posts (flags never block)",
      r.status_code == 200 and "This shit lamp" in r.text)
# the owner queue must render with a listing flag in it, and the
# owner's Remove on a listing flag must soft-delete the listing
rows = dbq("SELECT mf.*, u.name AS author_name FROM moderation_flags mf "
           "LEFT JOIN users u ON u.id=mf.author_id "
           "WHERE mf.item_type='listing'")
check("owner flag query returns listing flags without error",
      len(rows) >= 1 and rows[0]["author_name"] == "Seller Sam")
conn = sqlite3.connect(os.environ["HERE_DB"])
conn.execute("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
             ("mkt-owner-token", here.now_iso()))
conn.commit()
conn.close()
OWN = TestClient(here.app, follow_redirects=False)
OWN.cookies.set("here_owner", "mkt-owner-token")
r = OWN.get("/owner")
check("owner dashboard renders with a 'listing' flag in the queue",
      r.status_code == 200 and "listing" in r.text
      and "This shit lamp" in r.text)
flag_id = frow[0]["id"]
r = OWN.post(f"/owner/flag/{flag_id}/remove")
check("owner Remove on listing flag soft-deletes the listing",
      r.status_code == 303
      and listing_row(lid_flag)["status"] == "deleted")
check("flag row itself marked removed", dbq(
    "SELECT status FROM moderation_flags WHERE id=?",
    (flag_id,))[0]["status"] == "removed")

# ---------------- nav ----------------
r = B.get("/home")
check("Market in nav (topnav + sidebar + mobile menu)",
      r.text.count('href="/market"') >= 3)

# ---------------- inline JS syntax (detail gallery) ----------------
_node = shutil.which("node")
if _node:
    html = B.get(f"/market/{lid_edit}").text
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    ok = True
    for i, s in enumerate(scripts):
        p = os.path.join(_tmp, f"detail_{i}.js")
        open(p, "w").write(s)
        if subprocess.run([_node, "--check", p],
                          capture_output=True).returncode != 0:
            ok = False
    check("market detail inline JS passes node --check", ok and scripts)
else:
    check("node unavailable -- JS check skipped", True)

# ---------------- migration: dev-DB copy carries the new schema ----------
dev_db = os.path.join(BASE, "here.db")
if os.path.exists(dev_db):
    copy_db = os.path.join(_tmp, "devcopy.db")
    shutil.copyfile(dev_db, copy_db)
    env2 = dict(os.environ, HERE_DB=copy_db,
                HERE_UPLOADS=os.path.join(_tmp, "uploads2"))
    boot = subprocess.run(
        [sys.executable, "-c",
         "import app; from fastapi.testclient import TestClient;"
         "print(TestClient(app.app).get('/login').status_code)"],
        cwd=BASE, env=env2, capture_output=True, text=True)
    check("dev-DB copy boots (login 200)", boot.stdout.strip() == "200",
          boot.stderr[-300:])
    _c = sqlite3.connect(copy_db)
    tabs = [r[0] for r in _c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    mcols = [r[1] for r in _c.execute("PRAGMA table_info(messages)")]
    lcols = [r[1] for r in _c.execute("PRAGMA table_info(market_listings)")]
    _c.close()
    check("dev-DB copy migrated: market tables exist",
          "market_listings" in tabs and "market_saves" in tabs)
    check("dev-DB copy migrated: messages listing columns exist",
          all(c in mcols for c in
              ("listing_id", "listing_title", "listing_price_cents")))
    check("dev-DB copy migrated: listing columns complete",
          all(c in lcols for c in
              ("seller_id", "price_cents", "photos", "status", "sold_at",
               "share_to_feed")))
else:
    check("no dev DB present -- dev-copy migration check skipped", True)

# ---------------- summary ----------------
fails = [n for n, okk in RESULTS if not okk]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
