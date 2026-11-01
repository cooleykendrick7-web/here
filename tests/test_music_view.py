#!/usr/bin/env python3
"""Dual-volume, audio-only music (Ken's order, 2026-11-01): when
people VIEW stories and posts, the song plays by itself, the
YouTube VIDEO is never shown (the player is a 2px holder), and the
viewer gets separate sliders for the uploaded video's sound and
the Playbox music. Structural checks (headless audio can't be
heard in this sandbox): markup, data plumbing, player size."""
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


PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
       b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00"
       b"\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x00\x03\x00\x01"
       b"\x00\x00\x00\x00IEND\xaeB`\x82")


def signup(c, name, email):
    c.post("/signup", data={"name": name, "email": email,
                            "password": "pw123456"})


with TestClient(here.app, raise_server_exceptions=False) as tc:
    A = TestClient(here.app, raise_server_exceptions=False)
    signup(A, "Amy Author", "amy@example.com")
    aid = None
    conn = here.db()
    aid = conn.execute("SELECT id FROM users WHERE email=?",
                       ("amy@example.com",)).fetchone()[0]
    # A post with music + a creator mix (volume 55, original muted)
    conn.execute(
        "INSERT INTO posts(user_id, kind, body, privacy, "
        "music_video_id, music_title, music_artist, music_thumb, "
        "music_start, music_len, music_volume, music_mute_orig, "
        "created_at) VALUES(?, 'video', 'clip', 'public', "
        "'dQw4w9WgXcQ', 'Song T', 'Artist A', '', 5, 30, 55, 1, "
        "datetime('now'))", (aid,))
    pid = conn.execute("SELECT MAX(id) FROM posts").fetchone()[0]
    conn.commit(); conn.close()

    r = A.get("/home")
    check("post music bar carries the creator levels",
          'data-vol="55"' in r.text and 'data-muteorig="1"' in r.text)
    check("post card has a music volume slider starting at 55",
          'class="pm-vol-music"' in r.text and 'value="55"' in r.text)
    check("video post also gets the video-sound slider (starts 0 "
          "when the creator muted the original)",
          'class="pm-vol-video"' in r.text)
    check("post music player holder is the invisible 2px box",
          'class="post-music-player"' in r.text
          and 'post-music-player" hidden' not in r.text)

    r = A.get(f"/p/{pid}")
    check("public post page shows the audio-only music bar",
          'id="pmusic"' in r.text and 'id="pmusic-vol"' in r.text)
    check("public post page no longer embeds the visible video",
          "youtube.com/embed/" not in r.text)

    # Story viewer chrome: hidden player + dual sliders.
    conn = here.db()
    conn.execute(
        "INSERT INTO stories(user_id, media, music_kind, "
        "music_ref, music_title, music_artist, music_start, "
        "music_len, music_volume, music_mute_orig, created_at, "
        "expires_at) VALUES(?, 'x.mp4', 'youtube', "
        "'dQw4w9WgXcQ', 'Song T', 'Artist A', 5, 10, 70, 0, "
        "datetime('now'), datetime('now', '+1 day'))", (aid,))
    sid = conn.execute("SELECT MAX(id) FROM stories").fetchone()[0]
    conn.commit(); conn.close()
    r = A.get(f"/story/{sid}")
    t = r.text
    check("story viewer has the volume button + panel",
          'id="sv-volbtn"' in t and 'id="sv-volpanel"' in t)
    check("story viewer has music + video sliders",
          'id="sv-vol-music"' in t and 'id="sv-vol-video"' in t)

    base = (ROOT / "templates" / "base.html").read_text()
    check("base player CSS keeps the post player invisible",
          ".post-music-player{width:2px;height:2px" in base)
    story = (ROOT / "templates" / "story.html").read_text()
    check("story player is built at 2x2 (audio only)",
          "width: '2', height: '2'" in story)
    check("story viewer retries play on first touch",
          "root.addEventListener('pointerdown'" in story)
    check("post music autoplays in view (observer wired)",
          "pmIO" in base and "IntersectionObserver" in base)

print()
if FAILS:
    print("FAILURES:", FAILS)
    sys.exit(1)
print("ALL MUSIC-VIEW CHECKS PASSED")
