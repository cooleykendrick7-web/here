"""My Views (🎬 vertical video feed) tests for HERE: hashtags,
feed candidates, the ranking formula, learning events, interest
teaching, comments JSON, trending, the /views composer flow,
pagination, and a dev-DB boot check.

Run:  ./venv/bin/python tests/test_my_views.py
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
_tmp = tempfile.mkdtemp(prefix="here-test-mv-")
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


def befriend(c1, u1, c2, u2):
    r = c2.post(f"/friends/request/{u1}")
    assert r.status_code in (200, 303), r.status_code
    r = c1.post(f"/friends/accept/{u2}")
    assert r.status_code in (200, 303), r.status_code


MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64 + b"\xff\xd9"


def post_video(client, body, privacy="public", fname="clip.mp4"):
    r = client.post("/posts", data={"kind": "video", "body": body,
                                    "privacy": privacy},
                    files={"media": (fname, MP4, "video/mp4")})
    assert r.status_code == 303, (body, r.status_code)
    return dbq("SELECT id FROM posts WHERE body=?", (body,))[0]["id"]


def feed(client, **kw):
    r = client.get("/views.json", params=kw)
    assert r.status_code == 200, r.status_code
    return r.json()["views"]


def feed_ids(client, **kw):
    return [v["post_id"] for v in feed(client, **kw)]


def feed_item(client, pid, **kw):
    for v in feed(client, limit=20, **kw):
        if v["post_id"] == pid:
            return v
    return None


def aff(uid, kind, key):
    rows = dbq("SELECT score FROM user_affinity WHERE user_id=? "
               "AND kind=? AND key=?", (uid, kind, key))
    return rows[0]["score"] if rows else 0.0


# ---------------- users ----------------
V, VID = signup("Viewer Vee", "v@mv.io")
F, FID = signup("Friend Fay", "f@mv.io")
S1, S1ID = signup("Stranger One", "s1@mv.io")
S2, S2ID = signup("Stranger Two", "s2@mv.io")
FO, FOID = signup("Followed Ola", "fo@mv.io")
S3, S3ID = signup("Stranger Three", "s3@mv.io")
MU, MUID = signup("Muted Mel", "mu@mv.io")
BL, BLID = signup("Blocked Bob", "bl@mv.io")
befriend(V, VID, F, FID)
r = V.post(f"/follow/{FOID}")
assert r.status_code in (200, 303), r.status_code

# ---------------- 1. hashtag parsing ----------------
check("parse_hashtags: '#Funny #cats_and Dogs!' -> funny, cats_and",
      here.parse_hashtags("#Funny #cats_and Dogs!") == ["funny", "cats_and"],
      str(here.parse_hashtags("#Funny #cats_and Dogs!")))
check("parse_hashtags: dedupe + first-seen order + case folding",
      here.parse_hashtags("#One #TWO #one #two #Three")
      == ["one", "two", "three"])

tp = V.post("/posts", data={"kind": "text",
                            "body": "Loving this #Funny day #cats_and "
                                    "#Dogs! #funny"})
assert tp.status_code == 303
tpid = dbq("SELECT id FROM posts WHERE body LIKE 'Loving this%'")[0]["id"]
tags = {r["tag"] for r in dbq(
    "SELECT tag FROM post_hashtags WHERE post_id=?", (tpid,))}
check("create hook: a TEXT post's hashtags are stored normalized",
      tags == {"funny", "cats_and", "dogs"}, str(tags))

# ---------------- 2. backfill (existing video posts) ----------------
dbx("INSERT INTO posts(user_id, kind, body, media, privacy, created_at) "
    "VALUES(?, 'video', 'old clip #retro #Vibes', 'posts/old.mp4', "
    "'public', ?)", (S1ID, "2020-01-01T00:00:00+00:00"))
old_vid = dbq("SELECT id FROM posts WHERE body LIKE 'old clip%'")[0]["id"]
dbx("INSERT INTO posts(user_id, kind, body, media, privacy, created_at) "
    "VALUES(?, 'text', 'old text #nope', '', 'public', ?)",
    (S1ID, "2020-01-01T00:00:00+00:00"))
old_txt = dbq("SELECT id FROM posts WHERE body LIKE 'old text%'")[0]["id"]
here.init_db()  # re-run: migrations + backfill must be safe to repeat
btags = {r["tag"] for r in dbq(
    "SELECT tag FROM post_hashtags WHERE post_id=?", (old_vid,))}
check("backfill: pre-hook video post body parsed at migration",
      btags == {"retro", "vibes"}, str(btags))
check("backfill: text posts are NOT backfilled (video posts only)",
      dbq("SELECT tag FROM post_hashtags WHERE post_id=?",
          (old_txt,)) == [])
check("backfill: re-running init_db doesn't duplicate rows",
      len(dbq("SELECT tag FROM post_hashtags WHERE post_id=?",
              (old_vid,))) == 2)
# keep the ancient rows out of the ranking fixtures below
dbx("DELETE FROM posts WHERE id IN (?,?)", (old_vid, old_txt))
dbx("DELETE FROM post_hashtags WHERE post_id IN (?,?)", (old_vid, old_txt))

# ---------------- 3. feed candidates ----------------
fv = post_video(F, "friend public video cand1")
ph = F.post("/posts", data={"kind": "photo", "body": "a photo post"},
            files={"media": ("p.jpg", JPEG, "image/jpeg")})
assert ph.status_code == 303
phid = dbq("SELECT id FROM posts WHERE body='a photo post'")[0]["id"]
tx = post_video(V, "should not appear textvid", privacy="public")
dbx("UPDATE posts SET kind='text', media='' WHERE id=?", (tx,))
lk = F.post("/posts", data={"kind": "video", "body": "a link video",
                            "video_url": "https://www.youtube.com/"
                                         "watch?v=abc12345678"})
assert lk.status_code == 303
lkid = dbq("SELECT id FROM posts WHERE body='a link video'")[0]["id"]
ff = post_video(F, "friend friends-only video cand2", privacy="friends")
muv = post_video(MU, "muted author video cand3")
blv = post_video(BL, "blocked author video cand4")
V.post(f"/mute/{MUID}")
V.post(f"/block/{BLID}")
ids_v = feed_ids(V, limit=20)
check("candidates: friend's uploaded-video post is in the feed",
      fv in ids_v)
check("candidates: photo post is NOT in the feed", phid not in ids_v)
check("candidates: text post is NOT in the feed", tx not in ids_v)
check("candidates: YouTube link post (no file) is NOT in the feed",
      lkid not in ids_v)
check("candidates: friends-only video visible to a friend", ff in ids_v)
check("candidates: friends-only video hidden from a stranger",
      ff not in feed_ids(S1, limit=20))
check("candidates: muted author's video is excluded", muv not in ids_v)
check("candidates: blocked author's video is excluded", blv not in ids_v)
check("candidates: own posts appear in own feed",
      feed_item(V, fv) is not None and
      feed_item(F, fv)["author"]["is_self"] is True)

# ---------------- 4. ranking ----------------
# All posts below are fresh (recency ~equal) with no popularity, so the
# structural weights decide: friend(+30) > followed(+25) > stranger(0).
rv_friend = post_video(F, "rank friend video")
rv_follow = post_video(FO, "rank followed video")
rv_stranger = post_video(S2, "rank stranger video")
order = feed_ids(V, limit=20)
check("ranking: friend's video outranks followed + stranger",
      order.index(rv_friend) < order.index(rv_follow)
      and order.index(rv_friend) < order.index(rv_stranger))
check("ranking: followed author outranks a stranger",
      order.index(rv_follow) < order.index(rv_stranger))
it = feed_item(V, rv_friend)
check("payload: author flags + counts + score present",
      it["author"]["is_friend"] is True
      and it["author"]["following"] is False
      and it["reaction_count"] == 0 and it["comment_count"] == 0
      and it["my_reaction"] is None and isinstance(it["score"], float)
      and it["video_url"].startswith("/uploads/posts/")
      and it["poster_url"] == it["video_url"])

# Tag teaching: interest on an #x video lifts OTHER #x videos.
x1 = post_video(S1, "teach source #x")
x2 = post_video(S2, "tag mate #x")
x3 = post_video(S3, "no tags here plain")
r = V.post(f"/views/{x1}/interest", json={"interested": True})
assert r.status_code == 200 and r.json()["ok"]
check("ranking: after +interest on #x, an #x stranger beats a no-tag one",
      feed_ids(V, limit=20).index(x2) < feed_ids(V, limit=20).index(x3))
check("interest=true does NOT hide the source video",
      feed_item(V, x1) is not None)
w_before = feed_item(V, x2)["score"]
r = V.post(f"/views/{x2}/interest", json={"interested": False})
assert r.status_code == 200
check("not-interested: the video leaves the feed entirely",
      feed_item(V, x2) is None)
w_after = feed_item(V, x3)
x2b = dbq("SELECT score FROM user_affinity WHERE user_id=? AND kind='tag' "
          "AND key='x'", (VID,))
check("not-interested: tag affinity went +10 then -10 (net ~0)",
      abs((x2b[0]["score"] if x2b else 0.0)) < 0.51,
      str(x2b))
mate = post_video(S2, "second tag mate #x")
check("not-interested: a later #x video scores ~10 below its pre-drop kin",
      feed_item(V, mate)["score"] < w_before - 9.0,
      f"{feed_item(V, mate)['score']} vs {w_before}")

# Seen demotion, exactly: V's OWN no-tag videos (own posts teach no
# author affinity; no hashtags -> no tag affinity), so a first 'seen'
# changes the score by exactly the -35 penalty.
sv1 = post_video(V, "seen demo one")
sv2 = post_video(V, "seen demo two")
s_before = feed_item(V, sv1)["score"]
r = V.post(f"/views/{sv1}/seen")
assert r.status_code == 200 and r.json()["first"] is True
s_after = feed_item(V, sv1)["score"]
check("ranking: 'seen' demotes by exactly the seen penalty (35)",
      abs((s_before - s_after) - 35.0) < 0.011,
      f"{s_before} -> {s_after}")
check("ranking: seen video sinks below its identical unseen twin",
      feed_ids(V, limit=20).index(sv1) > feed_ids(V, limit=20).index(sv2))

# Popularity + recency directions via the score field.
pop = post_video(S2, "popularity probe")
p0 = feed_item(V, pop)["score"]
S1.post(f"/post/{pop}/react", data={"kind": "love"})
F.post(f"/post/{pop}/comment", data={"body": "nice one"})
p1 = feed_item(V, pop)["score"]
check("ranking: +1 reaction +1 comment lifts score by 2+3=5",
      abs((p1 - p0) - 5.0) < 0.011, f"{p0} -> {p1}")
check("payload: reaction_count/comment_count/my_reaction track reality",
      feed_item(V, pop)["reaction_count"] == 1
      and feed_item(V, pop)["comment_count"] == 1
      and feed_item(S1, pop)["my_reaction"] == "love")
dbx("UPDATE posts SET created_at='2020-01-01T00:00:00+00:00' WHERE id=?",
    (pop,))
check("ranking: an ancient video loses the whole recency bonus",
      feed_item(V, pop)["score"] < p1 - 19.0)

# ---------------- 5. learning events ----------------
ev = post_video(S3, "event probe #learn")
r1 = V.post(f"/views/{ev}/seen")
r2 = V.post(f"/views/{ev}/seen")
check("events: seen is idempotent (second call not 'first')",
      r1.json()["first"] is True and r2.json()["first"] is False)
check("events: first seen teaches tag +0.5 and author +0.5",
      abs(aff(VID, "tag", "learn") - 0.5) < 1e-9
      and abs(aff(VID, "author", str(S3ID)) - 0.5) < 1e-9,
      f"tag={aff(VID, 'tag', 'learn')} author={aff(VID, 'author', str(S3ID))}")
V.post(f"/views/{ev}/completed")
V.post(f"/views/{ev}/completed")
check("events: completed teaches +1/+1 and is idempotent",
      abs(aff(VID, "tag", "learn") - 1.5) < 1e-9
      and abs(aff(VID, "author", str(S3ID)) - 1.5) < 1e-9)
r = V.post(f"/views/{ev}/engage", json={"kind": "react"})
assert r.status_code == 200
check("events: engage react teaches +2/+2",
      abs(aff(VID, "tag", "learn") - 3.5) < 1e-9
      and abs(aff(VID, "author", str(S3ID)) - 3.5) < 1e-9)
V.post(f"/views/{ev}/engage", json={"kind": "comment"})
check("events: engage comment teaches +3/+3",
      abs(aff(VID, "tag", "learn") - 6.5) < 1e-9
      and abs(aff(VID, "author", str(S3ID)) - 6.5) < 1e-9)
r = V.post(f"/views/{ev}/engage", json={"kind": "bogus"})
check("events: engage with a bad kind is a 400", r.status_code == 400)
r = V.post(f"/views/{ff}/seen")  # friends-only post, V is a friend: ok
check("events: seen allowed on a viewable friends-only post",
      r.status_code == 200)
r = S2.post(f"/views/{ff}/seen")  # stranger cannot see it
check("events: seen on an unviewable post is refused (404)",
      r.status_code == 404)
r = V.post(f"/views/{ev}/interest", json={"interested": False})
check("events: not-interested writes the 'hidden' view event",
      dbq("SELECT kind FROM view_events WHERE user_id=? AND post_id=? "
          "AND kind='hidden'", (VID, ev)) != [])

# ---------------- 6. comments JSON ----------------
cv = post_video(F, "comment stage video")
S1.post(f"/post/{cv}/comment", data={"body": "first from s1"})
V.post(f"/post/{cv}/comment", data={"body": "second from v"})
r = V.get(f"/views/{cv}/comments")
cj = r.json()
flat = cj["comments"]
bodies = [c["body"] for c in flat]
check("comments JSON: lists existing comments with authors",
      bodies == ["first from s1", "second from v"]
      and flat[0]["author_name"] == "Stranger One"
      and cj["comment_blocked"] is False, str(bodies))
top_id = flat[0]["id"]
V.post(f"/post/{cv}/comment",
       data={"body": "a reply", "parent_id": str(top_id)})
cj = V.get(f"/views/{cv}/comments").json()
check("comments JSON: a reply via the existing endpoint nests under "
      "its top-level comment",
      len(cj["comments"]) == 2
      and [x["body"] for x in cj["comments"][0]["replies"]] == ["a reply"])
check("comments JSON: can_delete follows post-page rules "
      "(mine + post-author's rights)",
      V.get(f"/views/{cv}/comments").json()["comments"][1]["can_delete"]
      is True
      and S2.get(f"/views/{cv}/comments").json()["comments"][0]
      ["can_delete"] is False
      and F.get(f"/views/{cv}/comments").json()["comments"][0]
      ["can_delete"] is True)
F.post(f"/post/{cv}/comment-block", data={"user_id": str(S1ID)})
cj = S1.get(f"/views/{cv}/comments").json()
check("comments JSON: comment_blocked flag mirrors the post page",
      cj["comment_blocked"] is True)
S1.post(f"/post/{cv}/comment", data={"body": "should not land"})
check("comments JSON: a comment-blocked user's POST still doesn't land",
      "should not land" not in
      [c["body"] for c in V.get(f"/views/{cv}/comments")
       .json()["comments"]])
r = S2.get(f"/views/{ff}/comments")
check("comments JSON: unviewable post -> 404", r.status_code == 404)

# ---------------- 7. trending ----------------
post_video(F, "trend one #alpha")
post_video(S1, "trend two #alpha #beta")
post_video(MU, "trend muted #gamma")
tr = V.get("/views/trending").json()["tags"]
tmap = {t["tag"]: t["count"] for t in tr}
check("trending: counts only videos the viewer can see (muted out)",
      tmap.get("alpha") == 2 and tmap.get("beta") == 1
      and "gamma" not in tmap, str(tmap))
tr2 = S2.get("/views/trending").json()["tags"]
check("trending: another viewer (no mute) does see #gamma",
      {t["tag"]: t["count"] for t in tr2}.get("gamma") == 1)

# ---------------- 8. composer flow + pagination ----------------
r = V.post("/posts", data={"kind": "video", "body": "my fresh view #fresh",
                           "next": "/views"},
           files={"media": ("v.mp4", MP4, "video/mp4")},
           follow_redirects=False)
check("composer: next=/views is honored on success",
      r.status_code == 303
      and r.headers.get("location", "").startswith("/views"),
      r.headers.get("location", ""))
fresh = dbq("SELECT id FROM posts WHERE body LIKE 'my fresh view%'")[0]["id"]
check("composer: the posted View lands in the feed with its hashtags",
      feed_item(V, fresh, tag="fresh") is not None
      and feed_item(V, fresh)["hashtags"] == ["fresh"])
r = V.post("/posts", data={"kind": "text", "body": "evil next probe",
                           "next": "//evil.example.com"},
           follow_redirects=False)
check("composer: a protocol-relative 'next' falls back to /home",
      r.headers.get("location", "").startswith("/home"),
      r.headers.get("location", ""))
r = V.post("/posts", data={"kind": "text", "body": "plain next probe"},
           follow_redirects=False)
check("composer: no 'next' keeps the old /home redirect",
      r.headers.get("location", "").startswith("/home"))

for i in range(12):
    post_video(S1, f"page probe {i} #pageprobe")
page1 = feed(V, tag="pageprobe", limit=5, offset=0)
page2 = feed(V, tag="pageprobe", limit=5, offset=5)
page3 = feed(V, tag="pageprobe", limit=5, offset=10)
s1ids = {v["post_id"] for v in page1}
s2ids = {v["post_id"] for v in page2}
s3ids = {v["post_id"] for v in page3}
check("pagination: tag-filtered pages of 5/5/2, disjoint",
      len(page1) == 5 and len(page2) == 5 and len(page3) == 2
      and not (s1ids & s2ids) and not (s1ids & s3ids)
      and not (s2ids & s3ids),
      f"{len(page1)}/{len(page2)}/{len(page3)}")
allp = feed(V, limit=100)
check("pagination: limit is capped at 20", len(allp) <= 20)

# ---------------- 9. page + JS ----------------
r = V.get("/views")
check("page: GET /views renders for a logged-in user",
      r.status_code == 200 and "My Views" in r.text)
r = V.get("/views?tag=fresh")
check("page: GET /views?tag= renders (filter chip state)",
      r.status_code == 200 and "fresh" in r.text)
html = V.get("/views").text
blocks = re.findall(r"<script>(.*?)</script>", html, re.S)
all_ok, bad = True, []
for i, b in enumerate(blocks):
    if not b.strip():
        continue
    path = os.path.join(_tmp, f"mv_{i}.js")
    with open(path, "w") as fh:
        fh.write(b)
    p = subprocess.run(["node", "--check", path],
                       capture_output=True, text=True)
    if p.returncode != 0:
        all_ok = False
        bad.append(p.stderr.strip()[:200])
check("node --check: /views page inline JS parses", all_ok,
      "; ".join(bad))
home_html = V.get("/home").text
check("nav: My Views is linked from the site chrome",
      'href="/views"' in home_html)

# ---------------- 10. dev-DB boot (copy; original untouched) ----------------
dev_db = os.path.join(BASE, "here.db")
if os.path.exists(dev_db):
    before = os.path.getmtime(dev_db)
    copy_db = os.path.join(_tmp, "devcopy.db")
    shutil.copyfile(dev_db, copy_db)
    env2 = dict(os.environ, HERE_DB=copy_db,
                HERE_UPLOADS=os.path.join(_tmp, "devup"))
    code = (
        "import sys; sys.path.insert(0, r'%s');"
        "import app;"
        "from fastapi.testclient import TestClient;"
        "c = TestClient(app.app, follow_redirects=False);"
        "r = c.get('/views.json');"
        "assert r.status_code == 401, r.status_code;"
        "print('dev boot ok')" % BASE)
    p = subprocess.run([sys.executable, "-c", code], env=env2,
                       capture_output=True, text=True)
    check("dev DB copy: boots with the new tables + backfill",
          p.returncode == 0 and "dev boot ok" in p.stdout,
          (p.stdout + p.stderr)[-300:])
    tbls = {r[0] for r in sqlite3.connect(copy_db).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    check("dev DB copy: post_hashtags / user_affinity / view_events exist",
          {"post_hashtags", "user_affinity", "view_events"} <= tbls)
    check("dev DB copy: the original dev DB file was not modified",
          os.path.getmtime(dev_db) == before)
else:
    check("dev DB copy: skipped (no here.db in repo)", True)

print()
fails = [n for n, ok in RESULTS if not ok]
print(f"{len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed")
if fails:
    print("FAILURES:", *fails, sep="\n  ")
    sys.exit(1)
