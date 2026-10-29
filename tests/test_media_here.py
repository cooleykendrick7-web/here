"""Media HERE tests (Oct 29): settings switch (off / music /
artist), track uploads with a mandatory rights pick, caps,
the profile box, play counts, delete rules, skin saving, and
the floating player riding base.html.

Run:  ./venv/bin/python tests/test_media_here.py
"""
import io
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-media-")
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


def signup(name, email):
    c = TestClient(here.app, follow_redirects=False)
    r = c.post("/signup", data={"name": name, "email": email,
                                "password": "pw123456"})
    assert r.status_code == 303, (name, r.status_code)
    uid = dbq("SELECT id FROM users WHERE email=?",
              (email,))[0]["id"]
    return c, uid


MP3 = b"ID3" + b"\x00" * 2048
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 256


def upload(c, title="My Song", rights="own", fname="song.mp3"):
    return c.post("/media/upload",
                  data={"title": title, "rights": rights},
                  files={"file": (fname, io.BytesIO(MP3),
                                  "audio/mpeg")},
                  follow_redirects=False)


Ac, ARTIST = signup("Ari Artist", "ari@media.test")
Fc, FAN = signup("Fred Fan", "fred@media.test")
Oc, OFF = signup("Ollie Off", "ollie@media.test")

r = upload(Ac)
check("upload refused while Media HERE is off",
      "err-mode" in r.headers.get("location", ""))
r = Ac.post("/settings/media-mode", data={"media_mode": "artist"},
            follow_redirects=False)
check("settings switch turns artist mode on",
      dbq("SELECT media_mode FROM users WHERE id=?",
          (ARTIST,))[0]["media_mode"] == "artist")
r = Ac.post("/media/upload", data={"title": "No Rights"},
            files={"file": ("x.mp3", io.BytesIO(MP3),
                            "audio/mpeg")},
            follow_redirects=False)
check("upload without the rights pick is refused",
      "err-rights" in r.headers.get("location", ""))
r = upload(Ac, title="First Song", rights="own")
rows = dbq("SELECT * FROM media_tracks WHERE user_id=?",
           (ARTIST,))
check("upload lands with the rights answer on record",
      "media=ok" in r.headers.get("location", "")
      and len(rows) == 1 and rows[0]["rights"] == "own")
tid = rows[0]["id"]
upload(Ac, title="Borrowed Song", rights="notown",
       fname="borrowed.m4a")
check("a second track (not-owned) lands too",
      len(dbq("SELECT id FROM media_tracks WHERE user_id=?",
              (ARTIST,))) == 2)

r = Fc.get(f"/user/{ARTIST}")
check("artist profile shows the Media box up top with ARTIST",
      "Media HERE" in r.text and "ARTIST" in r.text
      and "First Song" in r.text and "mh-play-btn" in r.text)
r = Fc.get("/home")
check("the floating player rides every page (base chrome)",
      'id="mh-player"' in r.text and 'id="mh-pill"' in r.text
      and 'id="mh-audio"' in r.text)
r = Fc.post(f"/media/{tid}/play")
plays = dbq("SELECT plays FROM media_tracks WHERE id=?",
            (tid,))[0]["plays"]
check("play pings count", r.status_code == 200 and plays == 1)

r = Fc.post(f"/media/{tid}/delete", follow_redirects=False)
check("a stranger cannot delete someone else's track",
      len(dbq("SELECT id FROM media_tracks WHERE id=?",
              (tid,))) == 1)
r = Ac.post(f"/media/{tid}/delete", follow_redirects=False)
check("the owner can delete their track",
      len(dbq("SELECT id FROM media_tracks WHERE id=?",
              (tid,))) == 0)

Fc.post("/settings/media-mode", data={"media_mode": "music"})
for i in range(here.MEDIA_FREE_TRACKS):
    upload(Fc, title=f"Filler {i}")
r = upload(Fc, title="One Too Many")
n = dbq("SELECT COUNT(*) c FROM media_tracks WHERE user_id=?",
        (FAN,))[0]["c"]
check("the free track cap holds",
      n == here.MEDIA_FREE_TRACKS
      and "err-cap" in r.headers.get("location", ""))

r = Fc.get(f"/user/{FAN}")
check("music-mode profiles show the box without ARTIST",
      "Media HERE" in r.text and "Filler 0" in r.text)
r = Oc.post("/settings/media-skin",
            files={"skin": ("skin.png", io.BytesIO(PNG),
                            "image/png")},
            follow_redirects=False)
skin = dbq("SELECT media_skin FROM users WHERE id=?",
           (OFF,))[0]["media_skin"]
check("skin saves even with Media HERE off (it is YOUR player)",
      bool(skin))
r = Oc.get("/home")
check("the saved skin paints the player's face",
      "/uploads/" + skin in r.text)

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILS:", fails)
    sys.exit(1)
