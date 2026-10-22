"""YouTube music + story rewatch + regression tests for HERE.

Run:  ./venv/bin/python tests/test_music_youtube.py
Uses a throwaway DB/uploads dir; the oEmbed fetcher is stubbed — no
network access happens in this suite.
"""
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

GOOD = "dQw4w9WgXcQ"        # embeddable per the stub
BAD = "ZZZZZZZZZZZ"         # well-shaped id, oEmbed says no
GOOD_THUMB = "https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg"


def _fake_fetch(vid):
    if vid == GOOD:
        return {"title": "Test Song", "author_name": "Test Artist",
                "thumbnail_url": GOOD_THUMB}
    return None


here._oembed_fetch = _fake_fetch

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{extra}]" if extra and not cond else ""))


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


def befriend(c1, u1, c2, u2):
    r = c2.post(f"/friends/request/{u1}")
    assert r.status_code in (200, 303), r.status_code
    r = c1.post(f"/friends/accept/{u2}")
    assert r.status_code in (200, 303), r.status_code


PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
       b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")


def post_story(client, **fields):
    files = {"media": ("x.png", PNG, "image/png")}
    return client.post("/stories/new", data=fields, files=files)


# ---------------- users ----------------
anon = TestClient(here.app, follow_redirects=False)
A, AID = signup("Alice A", "alice@test.io")
B, BID = signup("Bob B", "bob@test.io")
C, CID = signup("Carol C", "carol@test.io")
D, DID = signup("Dave D", "dave@test.io")
befriend(A, AID, B, BID)   # A <-> B friends; C, D strangers

# ---------------- 1. youtube_id_from ----------------
cases = [
    (GOOD, GOOD),
    (f"https://www.youtube.com/watch?v={GOOD}", GOOD),
    (f"https://www.youtube.com/watch?v={GOOD}&list=PL123&t=5", GOOD),
    (f"https://youtu.be/{GOOD}", GOOD),
    (f"https://youtu.be/{GOOD}?si=abc", GOOD),
    (f"https://music.youtube.com/watch?v={GOOD}", GOOD),
    (f"https://www.youtube.com/shorts/{GOOD}", GOOD),
    (f"https://www.youtube.com/embed/{GOOD}", GOOD),
    (f"https://www.youtube.com/live/{GOOD}", GOOD),
    (f"https://m.youtube.com/watch?v={GOOD}", GOOD),
    ("https://example.com/watch?v=" + GOOD, None),
    ("https://vimeo.com/12345678901", None),
    ("dQw4w9WgXc", None),          # 10 chars
    (GOOD + "Q", None),            # 12 chars
    ("hello world", None),
    ("", None),
    (None, None),
    ("https://www.youtube.com/watch", None),
    ("https://www.youtube.com/watch?v=short", None),
    ("https://youtu.be/", None),
]
bad_cases = [c for c, want in cases if here.youtube_id_from(c) != want]
check("youtube_id_from: all URL shapes + junk rejected", not bad_cases,
      str(bad_cases))

# ---------------- 2. /music/preview ----------------
r = anon.post("/music/preview", json={"url": f"https://youtu.be/{GOOD}"})
check("preview requires login (401)", r.status_code == 401)
r = A.post("/music/preview", json={"url": f"https://youtu.be/{GOOD}"})
d = r.json()
check("preview ok for embeddable video",
      d.get("ok") and d.get("video_id") == GOOD and d.get("title") == "Test Song"
      and d.get("artist") == "Test Artist" and d.get("thumb") == GOOD_THUMB,
      str(d))
r = A.post("/music/preview", json={"url": f"https://www.youtube.com/watch?v={BAD}"})
d = r.json()
check("preview rejects non-embeddable video",
      not d.get("ok") and "can't be played inside HERE" in (d.get("error") or ""),
      str(d))
r = A.post("/music/preview", json={"url": "https://example.com/x"})
d = r.json()
check("preview rejects non-YouTube link",
      not d.get("ok") and "YouTube link" in (d.get("error") or ""), str(d))

# ---------------- 3. story attach (YouTube) ----------------
before = dbq("SELECT COUNT(*) c FROM stories")[0]["c"]
r = post_story(A, caption="with music",
               music_url=f"https://youtu.be/{GOOD}",
               music_start="12", music_len="15", music_volume="70")
check("story with YouTube music posts (303 home)",
      r.status_code == 303 and "/home" in r.headers.get("location", ""),
      f"{r.status_code} {r.headers.get('location')}")
rows = dbq("SELECT * FROM stories WHERE user_id=? ORDER BY id DESC LIMIT 1",
           (AID,))
srow = rows[0]
check("story row snapshots youtube music",
      srow["music_kind"] == "youtube" and srow["music_ref"] == GOOD
      and srow["music_title"] == "Test Song"
      and srow["music_artist"] == "Test Artist"
      and srow["music_thumb"] == GOOD_THUMB
      and srow["music_start"] == 12 and srow["music_len"] == 15
      and srow["music_volume"] == 70)
q = B.get("/stories/queue").json()
gA = [g for g in q["groups"] if g["user_id"] == AID][0]
sm = [s for s in gA["stories"] if s["id"] == srow["id"]][0]
m = sm.get("music") or {}
check("queue payload carries youtube music + trim",
      m.get("kind") == "youtube" and m.get("video_id") == GOOD
      and m.get("title") == "Test Song" and m.get("artist") == "Test Artist"
      and m.get("thumb") == GOOD_THUMB and m.get("start") == 12
      and m.get("len") == 15 and m.get("volume") == 70, str(m))

# ---------------- 4. unembeddable story music fails the save ------------
r = post_story(A, caption="bad music",
               music_url=f"https://www.youtube.com/watch?v={BAD}")
loc = r.headers.get("location", "")
check("unembeddable song -> back to composer with the error",
      r.status_code == 303 and loc.startswith("/stories/new")
      and "played inside HERE" in urllib.parse.unquote(loc), loc)
after = dbq("SELECT COUNT(*) c FROM stories")[0]["c"]
check("unembeddable song creates NO story", after == before + 1,
      f"{before} -> {after}")

# ---------------- 5. forged catalog/upload fields are ignored ----------
r = post_story(A, caption="forged", music_kind="catalog", music_ref="1",
               music_url="")
q = B.get("/stories/queue").json()
gA = [g for g in q["groups"] if g["user_id"] == AID][0]
forged = [s for s in gA["stories"] if s["caption"] == "forged"][0]
check("forged music_kind=catalog attaches nothing",
      forged.get("music") is None, str(forged.get("music")))
# an actual music_file upload must also be ignored now
files = {"media": ("x.png", PNG, "image/png"),
         "music_file": ("song.mp3", b"ID3-fake", "audio/mpeg")}
r = A.post("/stories/new", data={"caption": "mp3 try"}, files=files)
q = B.get("/stories/queue").json()
gA = [g for g in q["groups"] if g["user_id"] == AID][0]
mp3try = [s for s in gA["stories"] if s["caption"] == "mp3 try"][0]
check("music_file upload attaches nothing",
      mp3try.get("music") is None, str(mp3try.get("music")))
check("no story_music_uploads rows are written anymore",
      dbq("SELECT COUNT(*) c FROM story_music_uploads")[0]["c"] == 0)

# ---------------- 6. post attach + music bar ----------------------------
r = A.post("/posts", data={"kind": "text", "body": "my jam",
                           "music_url": f"https://www.youtube.com/watch?v={GOOD}",
                           "music_start": "42"})
check("post with music saves", r.status_code == 303, str(r.status_code))
prow = dbq("SELECT * FROM posts WHERE user_id=? ORDER BY id DESC LIMIT 1",
           (AID,))[0]
check("post row snapshots youtube music",
      prow["music_video_id"] == GOOD and prow["music_title"] == "Test Song"
      and prow["music_artist"] == "Test Artist"
      and prow["music_thumb"] == GOOD_THUMB and prow["music_start"] == 42)
home_html = A.get("/home").text
check("feed renders the music bar + player hook",
      'class="post-music"' in home_html and "Test Song" in home_html
      and f'data-vid="{GOOD}"' in home_html and 'data-start="42"' in home_html
      and "post-music-play" in home_html)
post_html = A.get(f"/post/{prow['id']}").text
check("post page renders the music bar", 'class="post-music"' in post_html)
prof_html = A.get(f"/user/{AID}").text
check("profile renders the music bar", 'class="post-music"' in prof_html)

# ---------------- 7. post with unembeddable song fails ------------------
pcount = dbq("SELECT COUNT(*) c FROM posts")[0]["c"]
r = A.post("/posts", data={"kind": "text", "body": "bad song",
                           "music_url": f"https://youtu.be/{BAD}"})
check("post with unembeddable song is refused",
      r.status_code == 303
      and "played inside HERE" in urllib.parse.unquote(
          r.headers.get("location", "")))
check("refused post was not created",
      dbq("SELECT COUNT(*) c FROM posts")[0]["c"] == pcount)

# popular-pick save path: music_video_id only, snapshot reused (no stub
# call needed — even if oEmbed now failed, the snapshot carries it)
r = A.post("/posts", data={"kind": "text", "body": "popular pick",
                           "music_video_id": GOOD})
prow2 = dbq("SELECT * FROM posts WHERE body='popular pick'")[0]
check("popular pick (id only) snapshots from existing attachment",
      prow2["music_video_id"] == GOOD and prow2["music_title"] == "Test Song")

# ---------------- 8. /music/popular aggregates -------------------------
r = A.get("/music/popular")
d = r.json()
top = (d.get("songs") or [{}])[0]
check("popular aggregates story + posts (uses=3)",
      d.get("ok") and top.get("video_id") == GOOD and top.get("uses") == 3
      and top.get("title") == "Test Song", str(d))
r = anon.get("/music/popular")
check("popular requires login", r.status_code == 401)

# ---------------- 9. legacy catalog story renders silent ----------------
conn = sqlite3.connect(os.environ["HERE_DB"])
conn.execute(
    "INSERT INTO stories(user_id, media, caption, created_at, expires_at,"
    " music_kind, music_ref, music_title, music_artist)"
    " VALUES(?,?,?,?,?,?,?,?,?)",
    (AID, "stories/legacy.png", "old one", here.now_iso(),
     here.story_expires_at(60), "catalog", "1", "Carefree", "Kevin MacLeod"))
legacy_id = conn.execute("SELECT last_insert_rowid() i").fetchone()[0]
conn.commit()
conn.close()
r = B.get(f"/story/{legacy_id}")
check("legacy story viewer page loads", r.status_code == 200)
check("legacy story has NO audio element / preset file",
      "<audio" not in r.text and "sv-audio" not in r.text
      and "/static/music/" not in r.text)
q = B.get("/stories/queue").json()
gA = [g for g in q["groups"] if g["user_id"] == AID][0]
leg = [s for s in gA["stories"] if s["id"] == legacy_id][0]
check("legacy story payload music is null", leg.get("music") is None)

# ---------------- 10. composer: presets gone ---------------------------
html_new = A.get("/stories/new").text
check("story composer has no catalog tab / My Music / upload",
      "HERE Music" not in html_new and "My Music" not in html_new
      and 'name="music_file"' not in html_new
      and "/static/music/" not in html_new
      and 'id="catalog-list"' not in html_new
      and 'name="music_kind"' not in html_new)
check("story composer has the YouTube picker",
      'name="music_url"' in html_new and "Paste a YouTube link" in html_new
      and "Find a song on YouTube" in html_new
      and 'id="story-music-picker"' in html_new)
home_html = A.get("/home").text
check("wall composer has the YouTube picker",
      'id="wall-music-picker"' in home_html and "Add music" in home_html
      and 'name="music_start"' in home_html)

# ---------------- 11. preset files deleted ------------------------------
gone = [f for f in ("carefree", "investigations", "lobby-time",
                    "local-forecast-elevator", "merry-go",
                    "monkeys-spinning-monkeys", "quirky-dog", "sneaky-snitch")
        if os.path.exists(os.path.join(BASE, "static", "music", f + ".mp3"))]
check("all 8 preset MP3s deleted from the tree", not gone, str(gone))

# ---------------- 12. rewatch: unlimited --------------------------------
r1 = post_story(A, caption="rw1")
r2 = post_story(A, caption="rw2")
q1 = B.get("/stories/queue").json()
gA = [g for g in q1["groups"] if g["user_id"] == AID][0]
all_ids = [s["id"] for s in gA["stories"]]
rw_ids = [s["id"] for s in gA["stories"] if s["caption"] in ("rw1", "rw2")]
check("queue lists both rewatch stories", len(rw_ids) == 2, str(rw_ids))
# B watches EVERY story A has, all the way through
for sid in all_ids:
    B.post(f"/story/{sid}/view")
q2 = B.get("/stories/queue").json()
gA2 = [g for g in q2["groups"] if g["user_id"] == AID][0]
check("after viewing ALL, queue returns them all again (all seen=true)",
      len(gA2["stories"]) == len(all_ids)
      and all(s["seen"] for s in gA2["stories"]),
      str([(s["id"], s["seen"]) for s in gA2["stories"]]))
home_b = B.get("/home").text
oldest_id = gA2["stories"][0]["id"]
check("tray still lists the all-viewed user, tile at oldest story",
      f'href="/story/{oldest_id}"' in home_b, "")
check("tray ring shows the viewed (gray) state",
      "story-ring viewed" in home_b)
r = B.get(f"/story/{oldest_id}")
check("viewer reopens an all-viewed story (200)",
      r.status_code == 200 and 'id="story-viewer"' in r.text)
check("viewer offers Watch again + clickable segments",
      "Watch again" in r.text and 'id="sv-again"' in r.text
      and "Jump to this story" in r.text)
check("viewer start target is the group's FIRST story even though seen",
      oldest_id == min(all_ids) and gA2["stories"][0]["seen"] is True)

# ---------------- 13. regression smokes ---------------------------------
# privacy suite: C goes private; stranger D gets the locked face + no posts
C.post("/posts", data={"kind": "text", "body": "carol secret post"})
C.post("/settings/privacy", data={"private_profile": "1"})
r = D.get(f"/user/{CID}")
check("private profile: stranger sees locked face",
      r.status_code == 200 and "carol secret post" not in r.text)
r = D.get("/home")
check("private profile: stranger feed excludes the post",
      "carol secret post" not in r.text)
# delete-for-me: A messages B; B deletes the thread for B only
A.post(f"/messages/{BID}", data={"body": "hello bob dm"})
binbox = B.get("/messages").text
check("message arrives in B's inbox", "hello bob dm" in binbox
      or "Alice A" in binbox)
B.post(f"/messages/{AID}/delete-thread")
binbox2 = B.get("/messages").text
check("delete-for-me clears B's view of the thread",
      "Alice A" not in binbox2)
ainbox = A.get("/messages").text
check("delete-for-me leaves A's copy untouched", "Bob B" in ainbox)
# calls: friends can start a call
r = A.post("/calls/start", json={"user_id": BID, "kind": "voice"})
check("calls: friend call starts", r.json().get("ok") is True, r.text[:120])
# spot: A sets a spot name; profile shows it
A.post("/settings/spot", data={"spot_name": "Alice's Corner"})
r = B.get(f"/user/{AID}")
check("spot: name renders on profile", "Alice&#39;s Corner" in r.text
      or "Alice's Corner" in r.text)
# live: start + end a room
r = A.post("/live/start", json={"title": "Alice live"})
lj = r.json()
check("live: room starts", lj.get("ok") is True, r.text[:120])
lid = lj.get("id")
r = B.get(f"/live/{lid}")
check("live: watch page loads for a friend", r.status_code == 200)
r = A.post(f"/live/{lid}/end")
check("live: host can end the room", r.json().get("ok") is True
      if r.headers.get("content-type", "").startswith("application/json")
      else r.status_code in (200, 303), r.text[:120])

# ---------------- 14. node --check on rendered JS -----------------------
def js_blocks(html_text):
    return re.findall(r"<script>(.*?)</script>", html_text, re.S)


pages = {
    "home": A.get("/home").text,
    "story_new": A.get("/stories/new").text,
    "story_viewer": B.get(f"/story/{gA2['stories'][0]['id']}").text,
}
all_ok, bad = True, []
for label, html_text in pages.items():
    for i, block in enumerate(js_blocks(html_text)):
        path = os.path.join(_tmp, f"chk_{label}_{i}.js")
        with open(path, "w") as f:
            f.write(block)
        p = subprocess.run(["node", "--check", path],
                           capture_output=True, text=True)
        if p.returncode != 0:
            all_ok = False
            bad.append(f"{label}#{i}: {p.stderr.strip()[:160]}")
check("node --check: all inline JS on changed pages parses", all_ok,
      "; ".join(bad))

# ---------------- summary ----------------
fails = [n for n, ok, _ in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL GREEN")
