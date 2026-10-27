"""Feed system (Ken's order Oct 27): Who's HERE? rename, discovery
posts woven into the home feed, the This-or-Not trainer
(user_topic_prefs + /feed/interest), and pure profiles.

Run:  ./venv/bin/python tests/test_feed_system.py
Uses a throwaway DB/uploads dir; no network.
"""
import os
import sqlite3
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-feed-")
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
                                "password": "test-pass-123"})
    assert r.status_code in (200, 303), r.status_code
    uid = dbq("SELECT id FROM users WHERE email=?", (email,))[0]["id"]
    return c, uid


def befriend(c1, u1, c2, u2):
    assert c2.post(f"/friends/request/{u1}").status_code in (200, 303)
    assert c1.post(f"/friends/accept/{u2}").status_code in (200, 303)


PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
       b"\x00\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64

A, AID = signup("Feed Ann", "ann@feed.io")      # the viewer
B, BID = signup("Feed Ben", "ben@feed.io")      # friend
C, CID = signup("Feed Cam", "cam@feed.io")      # stranger
D, DID = signup("Feed Dan", "dan@feed.io")      # stranger
befriend(A, AID, B, BID)

B.post("/posts", data={"kind": "text", "body": "ben main post",
                       "privacy": "public"})
C.post("/posts", data={"kind": "text", "body": "cam says hi #dogs",
                       "privacy": "public"})
D.post("/posts", data={"kind": "photo", "body": "dan photo #cats",
                       "privacy": "public"},
       files={"media": ("cat.png", PNG, "image/png")})
D.post("/posts", data={"kind": "video", "body": "dan video",
                       "privacy": "public"},
       files={"media": ("clip.mp4", MP4, "video/mp4")})
C.post("/posts", data={"kind": "text", "body": "cam secret",
                       "privacy": "friends"})

ann = here.get_user(AID)
main_cards = here._home_posts(ann, False)
items = here._home_feed_items(ann, main_cards)
posts_in_feed = [i["post"] for i in items if i["kind"] == "post"]
disco = [p for p in posts_in_feed if p.get("discovery")]
main_ids = {p["id"] for p in main_cards}

check("rename: home page says Who's HERE?",
      "Who's HERE?" in A.get("/home").text)
check("feed: friend's post is in the main list",
      any(p["body"] == "ben main post" for p in main_cards))
check("discovery: stranger public posts are woven in",
      {p["user_id"] for p in disco} >= {CID, DID},
      str([(p["user_id"], p["body"][:20]) for p in disco]))
check("discovery: no duplicates of main-list posts",
      all(p["id"] not in main_ids for p in disco))
check("discovery: friends-only stranger post never surfaces",
      all(p["body"] != "cam secret" for p in posts_in_feed))
check("discovery: cards carry the discovery flag for the banner",
      all(p.get("discovery") for p in disco))
home_html = A.get("/home").text
check("feed page: discovery banner + follow/add actions render",
      "From around HERE" in home_html
      and f'action="/follow/{DID}"' in home_html
      and f'action="/friends/request/{DID}"' in home_html)
check("feed page: trainer card shows while prompts remain",
      "Train your feed" in home_html)

# ---------------- trainer endpoint ----------------
r = A.post("/feed/interest", json={"key": "kind:video", "pref": "not"})
check("interest: kind answer saves", r.status_code == 200
      and r.json().get("ok"))
check("interest: pref row written",
      dbq("SELECT pref FROM user_topic_prefs WHERE user_id=? AND key=?",
          (AID, "kind:video"))[0]["pref"] == "not")
disco2 = here._discovery_posts(ann, set())
check("trainer effect: 'not video' hides discovery videos only",
      all(p["kind"] != "video" for p in disco2)
      and any(p["kind"] == "photo" for p in disco2))
r = A.post("/feed/interest", json={"key": "tag:cats", "pref": "this"})
check("interest: hashtag answer saves (tag exists)", r.status_code == 200)
disco3 = here._discovery_posts(ann, set())
check("trainer effect: 'this #cats' floats the cats photo first",
      disco3 and disco3[0]["body"].startswith("dan photo"))
r = A.post("/feed/interest", json={"key": "kind:evil", "pref": "this"})
check("interest: unknown kind refused", r.status_code == 400)
r = A.post("/feed/interest", json={"key": "tag:nosuchtag", "pref": "this"})
check("interest: unknown hashtag refused", r.status_code == 400)
r = A.post("/feed/interest", json={"key": "kind:photo", "pref": "maybe"})
check("interest: bad pref refused", r.status_code == 400)
r = A.post("/feed/interest", json={"key": "tag:dogs", "pref": "not"})
assert r.status_code == 200
disco4 = here._discovery_posts(ann, set())
check("trainer effect: 'not #dogs' hides the dogs post",
      all("#dogs" not in (p["body"] or "") for p in disco4))

# friends are NEVER filtered by the trainer
B.post("/posts", data={"kind": "video", "body": "ben video",
                       "privacy": "public"},
       files={"media": ("b.mp4", MP4, "video/mp4")})
main2 = here._home_posts(ann, False)
check("trainer never touches friends: ben's video still in main feed",
      any(p["body"] == "ben video" for p in main2))

# once everything is answered the card goes away
prefs = here._topic_prefs(AID)
cands = here._interest_candidates(AID)
for c in list(cands):
    A.post("/feed/interest", json={"key": c["key"], "pref": "this"})
check("trainer: no prompt left once all answered",
      here._next_interest_prompt(AID) is None)
check("trainer: answered keys are never re-asked",
      here._interest_candidates(AID) == [])

# ---------------- profiles stay pure ----------------
prof = A.get(f"/user/{CID}").text
check("profile purity: no discovery banner on a profile",
      "From around HERE" not in prof)
check("profile purity: no Who's HERE? module on a profile",
      "Who's HERE?" not in prof)
check("profile purity: no trainer card on a profile",
      "Train your feed" not in prof)
check("profile purity: no Sponsored ad card on a profile",
      "Sponsored" not in prof)
check("profile: the person's own public post shows",
      "cam says hi" in prof)

# discover page renamed too
disc = A.get("/discover").text
check("rename: discover page says Who's HERE?",
      "Who's HERE?" in disc and "People you may know" not in disc)

failed = [n for n, ok in RESULTS if not ok]
print()
print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
if failed:
    print("FAILURES:", failed)
    sys.exit(1)
