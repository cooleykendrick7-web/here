#!/usr/bin/env python3
"""Audio-only music + creation-time mixing (Ken's orders,
2026-11-01, as corrected): when people VIEW stories and posts,
the song plays by itself and the YouTube VIDEO is never shown
(2px holder). Volume lives ONLY at creation — the composer (post
+ story) carries a Song volume slider and a My-video's-sound
slider; the saved mix is applied automatically on view and there
are NO viewer-side volume controls anywhere."""
import os, sys, tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="here-musicview-")
os.environ["HERE_DB"] = os.path.join(TMP, "t.db")
os.environ["HERE_UPLOADS"] = os.path.join(TMP, "up")
os.environ["HERE_SECRET"] = "test-secret"
import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

here._stripe_api = lambda *a, **k: {}
FAILS = []


def check(name, cond):
    print(("PASS " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def signup(c, name, email):
    c.post("/signup", data={"name": name, "email": email,
                            "password": "pw123456"})


with TestClient(here.app, raise_server_exceptions=False) as tc:
    A = TestClient(here.app, raise_server_exceptions=False)
    signup(A, "Amy Author", "amy@example.com")
    conn = here.db()
    aid = conn.execute("SELECT id FROM users WHERE email=?",
                       ("amy@example.com",)).fetchone()[0]
    conn.execute(
        "INSERT INTO posts(user_id, kind, body, privacy, "
        "music_video_id, music_title, music_artist, music_thumb, "
        "music_start, music_len, music_volume, music_mute_orig, "
        "music_orig_volume, created_at) VALUES(?, 'video', 'clip', "
        "'public', 'dQw4w9WgXcQ', 'Song T', 'Artist A', '', 5, 30, "
        "55, 0, 40, datetime('now'))", (aid,))
    pid = conn.execute("SELECT MAX(id) FROM posts").fetchone()[0]
    conn.execute(
        "INSERT INTO stories(user_id, media, music_kind, "
        "music_ref, music_title, music_artist, music_start, "
        "music_len, music_volume, music_mute_orig, "
        "music_orig_volume, created_at, expires_at) VALUES(?, "
        "'x.mp4', 'youtube', 'dQw4w9WgXcQ', 'Song T', 'Artist A', "
        "5, 10, 70, 0, 33, datetime('now'), "
        "datetime('now', '+1 day'))", (aid,))
    sid = conn.execute("SELECT MAX(id) FROM stories").fetchone()[0]
    conn.commit(); conn.close()

    r = A.get("/home")
    check("post card carries the creator mix (song 55, video 40)",
          'data-vol="55"' in r.text and 'data-origvol="40"' in r.text)
    check("post card has NO viewer volume controls",
          "pm-vol-music" not in r.text
          and "post-music-volbtn" not in r.text)
    check("composer offers Song volume + My video's sound sliders",
          'name="music_volume"' in r.text
          and 'name="music_orig_volume"' in r.text)
    check("post music player holder is the invisible 2px box",
          'class="post-music-player"' in r.text)

    r = A.get(f"/p/{pid}")
    check("public post page: audio-only bar, no video embed, "
          "no volume slider",
          'id="pmusic"' in r.text and "youtube.com/embed/" not in r.text
          and 'id="pmusic-vol"' not in r.text)

    q = A.get("/stories/queue").json()
    mus = None
    for g in q.get("groups", []):
        for s in g.get("stories", []):
            if s.get("id") == sid:
                mus = s.get("music")
    check("story payload carries the creator's video-sound level",
          bool(mus) and mus.get("orig_volume") == 33
          and mus.get("volume") == 70)
    r = A.get(f"/story/{sid}")
    t = r.text
    check("story viewer has NO volume panel",
          'id="sv-volpanel"' not in t and 'id="sv-volbtn"' not in t)

    r = A.get("/stories/new")
    check("story editor carries the video-sound slider wiring",
          "se-mus-origvol" in r.text and "se-mus-origF" in r.text)

    base = (ROOT / "templates" / "base.html").read_text()
    check("base CSS keeps the post player invisible",
          ".post-music-player{width:2px;height:2px" in base)
    check("song-pick preview is audio-only too (no video frame)",
          ".mp-pslot{width:2px;height:2px" in base
          and "width: '2', height: '2', videoId: song.video_id" in base)
    check("post playback applies the creator's video level",
          "pmApplyOrigVol" in base)
    story = (ROOT / "templates" / "story.html").read_text()
    check("story player is built at 2x2 (audio only)",
          "width: '2', height: '2'" in story)
    check("story viewer retries play on first touch",
          "root.addEventListener('pointerdown'" in story)
    pub = (ROOT / "templates" / "public_post.html").read_text()
    check("public page starts the song by itself",
          "start();" in pub)

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL MUSIC-VIEW CHECKS PASSED")
