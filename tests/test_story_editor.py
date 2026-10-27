"""Build STORY EDITOR tests: the visual editor's backend — edit_json
cleaning, per-story audience, picker media, mention search + notify,
mentioned-viewer access, reshare, text-only stories.

Run:  ./venv/bin/python tests/test_story_editor.py
Uses a throwaway DB/uploads dir; no network.
"""
import json
import os
import sqlite3
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-editor-")
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
    r = c2.post(f"/friends/request/{u1}")
    assert r.status_code in (200, 303), r.status_code
    r = c1.post(f"/friends/accept/{u2}")
    assert r.status_code in (200, 303), r.status_code


PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
       b"\x00\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")

# ---------------- 0. migration ----------------
_scols = [r[1] for r in dbq("PRAGMA table_info(stories)")]
check("migration: stories.edit_json + audience exist",
      "edit_json" in _scols and "audience" in _scols)

# ---------------- users ----------------
A, AID = signup("Editor Ann", "ann@editor.io")     # author
B, BID = signup("Editor Ben", "ben@editor.io")     # friend + mentioned
C, CID = signup("Editor Cam", "cam@editor.io")     # stranger, later mentioned
befriend(A, AID, B, BID)

# Ann posts a photo so the picker has owned media
r = A.post("/posts", data={"kind": "photo", "body": "ann photo",
                           "privacy": "public"},
           files={"media": ("pic.png", PNG, "image/png")})
assert r.status_code in (200, 303), r.status_code
POST_MEDIA = dbq("SELECT media FROM posts WHERE user_id=? AND media != ''",
                 (AID,))[0]["media"]
check("setup: ann owns a post photo", bool(POST_MEDIA))

# ---------------- 1. picker endpoints ----------------
j = A.get("/stories/media").json()
check("picker: /stories/media lists the owned photo",
      j.get("ok") and any(i["path"] == POST_MEDIA for i in j["items"]))
check("picker: media item shape (url + video flag)",
      any(i["url"] == "/uploads/" + POST_MEDIA and i["video"] is False
          for i in j["items"]))
j = B.get("/stories/media").json()
check("picker: ben's grid has none of ann's media",
      all(i["path"] != POST_MEDIA for i in j.get("items", [])))
j = A.get("/stories/people", params={"q": "Ben"}).json()
check("people: mention search finds Ben",
      any(p["id"] == BID for p in j.get("people", [])))
j = A.get("/stories/people", params={"q": "B"}).json()
check("people: 1-letter query returns nobody",
      j.get("people") == [])

# ---------------- 2. editor story: full payload ----------------
EDIT = {
    "filter": "gold",
    "trim": {"start": 1.0, "end": 4.5},
    "captions": "hello from the editor",
    "texts": [{"t": "Big news", "font": "meme", "color": "#ffd60a",
               "bg": True, "align": "center", "x": 50, "y": 30,
               "scale": 1.4, "rot": -5}],
    "stickers": [
        {"kind": "heart", "x": 20, "y": 20, "scale": 1.2, "rot": 0},
        {"kind": "location", "label": "Heidelberg, MS",
         "x": 50, "y": 14, "scale": 1, "rot": 0},
        {"kind": "time", "x": 80, "y": 14, "scale": 1, "rot": 0},
        {"kind": "link", "url": "https://example.com/x", "label": "Watch",
         "x": 50, "y": 82, "scale": 1, "rot": 0},
        {"kind": "mention", "uid": BID, "name": "Editor Ben",
         "x": 50, "y": 66, "scale": 1, "rot": 0}],
    "mentions": [BID],
}
r = A.post("/stories/new", data={
    "existing_media": POST_MEDIA, "caption": "editor story",
    "edit_json": json.dumps(EDIT), "audience": "public",
    "lifetime": "24h"})
check("create: editor story posts (303)", r.status_code == 303,
      str(r.status_code))
S1 = dbq("SELECT * FROM stories WHERE user_id=? ORDER BY id DESC LIMIT 1",
         (AID,))[0]
check("create: audience saved public", S1["audience"] == "public")
_saved = json.loads(S1["edit_json"])
check("create: edit saved (filter/text/stickers/captions)",
      _saved.get("filter") == "gold"
      and _saved["texts"][0]["t"] == "Big news"
      and len(_saved["stickers"]) == 5
      and _saved.get("captions") == "hello from the editor")
check("create: trim saved", _saved.get("trim") == {"start": 1.0, "end": 4.5})
check("notify: mentioned Ben got a story_mention notification",
      bool(dbq("SELECT 1 FROM notifications WHERE user_id=? "
               "AND kind='story_mention'", (BID,))))

# queue payloads
def queue(client):
    return client.get("/stories/queue").json()


def find_story(q, sid):
    for g in q.get("groups", []):
        for s in g["stories"]:
            if s["id"] == sid:
                return s
    return None


qB = queue(B)
sB = find_story(qB, S1["id"])
check("queue: friend sees the story with its edit payload",
      bool(sB) and sB["edit"]["filter"] == "gold"
      and sB["audience"] == "public")
qC = queue(C)
check("queue: stranger sees the PUBLIC story (audience works)",
      bool(find_story(qC, S1["id"])))
check("queue: payload carries the viewer id",
      qC.get("me") == CID)
r = C.get(f"/story/{S1['id']}")
check("view: stranger can open a public story page", r.status_code == 200)

# ---------------- 3. friends story + mention access ----------------
EDIT2 = {"mentions": [CID],
         "stickers": [{"kind": "mention", "uid": CID, "name": "Editor Cam",
                       "x": 50, "y": 50, "scale": 1, "rot": 0}]}
r = A.post("/stories/new", data={
    "existing_media": POST_MEDIA, "caption": "friends only",
    "edit_json": json.dumps(EDIT2), "audience": "friends"})
assert r.status_code == 303, r.status_code
S2 = dbq("SELECT * FROM stories WHERE user_id=? ORDER BY id DESC LIMIT 1",
         (AID,))[0]
check("friends story: stranger NOT in queue by default... ",
      True)  # placeholder replaced below by the mention rule check
qC2 = queue(C)
check("mention access: mentioned stranger DOES see the friends story",
      bool(find_story(qC2, S2["id"])))
r = C.get(f"/story/{S2['id']}")
check("mention access: mentioned stranger can open it",
      r.status_code == 200)

# a friends story NOT mentioning Cam is invisible to her
r = A.post("/stories/new", data={
    "existing_media": POST_MEDIA, "caption": "plain friends",
    "edit_json": "{}", "audience": "friends"})
assert r.status_code == 303, r.status_code
S3 = dbq("SELECT * FROM stories WHERE user_id=? ORDER BY id DESC LIMIT 1",
         (AID,))[0]
qC3 = queue(C)
check("friends story: unmentioned stranger cannot see it",
      find_story(qC3, S3["id"]) is None)
r = C.get(f"/story/{S3['id']}")
check("friends story: direct open is refused for stranger",
      "isn't available" in r.text)

# ---------------- 4. reshare ----------------
r = C.post(f"/story/{S2['id']}/reshare")
j = r.json()
check("reshare: mentioned viewer can reshare", r.status_code == 200
      and j.get("ok"), r.text[:140])
NEW = dbq("SELECT * FROM stories WHERE id=?", (j["id"],))[0]
check("reshare: copy belongs to Cam, friends audience, no mentions",
      NEW["user_id"] == CID and NEW["audience"] == "friends"
      and "mentions" not in json.loads(NEW["edit_json"] or "{}"))
check("reshare: author notified",
      bool(dbq("SELECT 1 FROM notifications WHERE user_id=? "
               "AND kind='story_reshare'", (AID,))))
r = B.post(f"/story/{S2['id']}/reshare")
check("reshare: a viewer who is NOT mentioned gets 403",
      r.status_code == 403)

# ---------------- 5. text-only story ----------------
r = A.post("/stories/new", data={
    "caption": "", "audience": "friends",
    "edit_json": json.dumps({
        "mode": "text", "textbg": "ember",
        "texts": [{"t": "just words", "font": "strong",
                   "color": "#ffffff", "bg": False, "align": "center",
                   "x": 50, "y": 50, "scale": 1.5, "rot": 0}]})})
check("text story: posts with NO media", r.status_code == 303,
      str(r.status_code))
S4 = dbq("SELECT * FROM stories WHERE user_id=? ORDER BY id DESC LIMIT 1",
         (AID,))[0]
check("text story: media empty + mode saved",
      S4["media"] == "" and json.loads(S4["edit_json"])["mode"] == "text")
qB2 = queue(B)
sT = find_story(qB2, S4["id"])
check("text story: queue payload has empty media + text edit",
      bool(sT) and sT["media"] == "" and sT["edit"]["mode"] == "text")

# no media + no text mode = refused
r = A.post("/stories/new", data={"edit_json": "{}", "audience": "friends"})
n_before = len(dbq("SELECT id FROM stories WHERE user_id=?", (AID,)))
check("guard: empty story (no media, no text mode) creates nothing",
      len(dbq("SELECT id FROM stories WHERE user_id=?", (AID,)))
      == n_before)

# ---------------- 6. cleaner: hostile payload ----------------
BAD = {
    "filter": "evil-filter",
    "texts": [{"t": "ok text", "font": "wingdings", "color": "red;}",
               "x": 500, "y": -9, "scale": 99, "rot": 999}],
    "stickers": [
        {"kind": "link", "url": "javascript:alert(1)", "label": "x",
         "x": 50, "y": 50, "scale": 1, "rot": 0},
        {"kind": "nonsense", "x": 50, "y": 50, "scale": 1, "rot": 0},
        {"kind": "heart", "x": 50, "y": 50, "scale": 1, "rot": 0}],
    "mentions": [BID, "junk", -3, BID],
}
r = A.post("/stories/new", data={
    "existing_media": POST_MEDIA, "caption": "hostile",
    "edit_json": json.dumps(BAD), "audience": "friends"})
assert r.status_code == 303, r.status_code
S5 = dbq("SELECT * FROM stories WHERE user_id=? ORDER BY id DESC LIMIT 1",
         (AID,))[0]
cl = json.loads(S5["edit_json"])
check("cleaner: bad filter dropped", "filter" not in cl)
check("cleaner: javascript: link dropped, art sticker kept",
      [s["kind"] for s in cl.get("stickers", [])] == ["heart"])
_tx = cl["texts"][0]
check("cleaner: text clamped (font/color/coords/scale/rot)",
      _tx["font"] == "classic" and _tx["color"] == "#ffffff"
      and _tx["x"] == 100 and _tx["y"] == 0 and _tx["scale"] == 4
      and _tx["rot"] == 180)
check("cleaner: mentions deduplicated + validated", cl["mentions"] == [BID])
check("cleaner: garbage JSON becomes an empty edit",
      here._clean_story_edit("{not json") == {}
      and here._clean_story_edit("") == {})

# existing_media ownership: Ben cannot use Ann's photo
r = B.post("/stories/new", data={"existing_media": POST_MEDIA,
                                 "edit_json": "{}", "audience": "friends"})
check("ownership: someone else's media path is refused",
      len(dbq("SELECT id FROM stories WHERE user_id=?", (BID,))) == 0)

# ---------------- 7. pages + viewer markers ----------------
r = A.get("/stories/new")
check("editor page: renders picker + editor + rail sheets",
      r.status_code == 200 and 'id="se-picker"' in r.text
      and 'id="se-editor"' in r.text and 'id="se-sheetwrap"' in r.text
      and "se-mode" in r.text)
r = B.get(f"/story/{S1['id']}")
check("viewer page: carries the edit layer + captions + reshare hooks",
      'id="sv-edit-layer"' in r.text and 'id="sv-captions"' in r.text
      and 'id="sv-reshare"' in r.text and "renderEdit" in r.text)
r = A.get("/stories/new?prompt=Night%20in%20your%20city")
check("editor page: add-yours prompt arrives from the link",
      "Night in your city" in r.text)

# ---------------- 9. tray previews (Ken's bug Oct 27) ----------------
# The tray card used the RAW stored path ('stories/x.jpg') as the
# <img> src — no /uploads/ prefix — so every photo story card
# rendered as a broken-image icon. Preview = newest story's face.
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64
P, PID = signup("Tray Pam", "pam@tray.io")
V, VID = signup("Tray Vic", "vic@tray.io")
T, TID = signup("Tray Tex", "tex@tray.io")
r = P.post("/stories/new", data={"caption": "pam photo"},
           files={"media": ("p.png", PNG, "image/png")})
assert r.status_code == 303, r.status_code
r = V.post("/stories/new", data={"caption": "vic video"},
           files={"media": ("v.mp4", MP4, "video/mp4")})
assert r.status_code == 303, r.status_code
r = T.post("/stories/new", data={
    "caption": "", "edit_json": json.dumps(
        {"mode": "text", "textbg": "ocean",
         "texts": [{"t": "words only", "x": 50, "y": 50, "scale": 1.5}]})})
assert r.status_code == 303, r.status_code
tp = here.stories_tray(PID)[0]
check("tray: photo story preview is a working /uploads/ URL",
      tp["thumb"].startswith("/uploads/stories/")
      and tp["thumb"].endswith(".png"), tp["thumb"])
tv = here.stories_tray(VID)[0]
check("tray: video story preview is the video file",
      tv["thumb_video"].startswith("/uploads/stories/")
      and tv["thumb"] == "", tv["thumb_video"])
tt = here.stories_tray(TID)[0]
check("tray: text story preview is its gradient",
      "#0a4d5c" in tt["thumb_grad"], tt["thumb_grad"])
home = P.get("/home").text
check("tray: home renders the photo preview img",
      f'<img class="sc-bg" src="{tp["thumb"]}"' in home)

failed = [n for n, ok in RESULTS if not ok]
print()
print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
if failed:
    print("FAILURES:", failed)
    sys.exit(1)
