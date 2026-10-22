"""Story social tests for HERE: no end card, own-story "N views",
story replies/reactions landing in the inbox with a surviving clip,
and tap numbers that clear on acknowledgment.

Run:  ./venv/bin/python tests/test_story_social.py
Uses a throwaway DB/uploads dir; no network access happens here.
"""
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-social-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond), extra))
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


def befriend(c1, u1, c2, u2):
    r = c2.post(f"/friends/request/{u1}")
    assert r.status_code in (200, 303), r.status_code
    r = c1.post(f"/friends/accept/{u2}")
    assert r.status_code in (200, 303), r.status_code


PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
       b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64


def post_story(client, fname="x.png", blob=PNG, ctype="image/png", **fields):
    files = {"media": (fname, blob, ctype)}
    return client.post("/stories/new", data=fields, files=files)


# ---------------- users ----------------
anon = TestClient(here.app, follow_redirects=False)
A, AID = signup("Alice A", "alice@social.io")   # story owner
B, BID = signup("Bob B", "bob@social.io")       # friend: replies/reacts
C, CID = signup("Carol C", "carol@social.io")   # stranger
D, DID = signup("Dave D", "dave@social.io")     # friend, later blocked
E, EID = signup("Erin E", "erin@social.io")     # second viewer
befriend(A, AID, B, BID)
befriend(A, AID, E, EID)
befriend(A, AID, D, DID)

# Alice posts an image story
r = post_story(A, caption="alice story")
assert r.status_code == 303, r.status_code
SID = dbq("SELECT id FROM stories WHERE user_id=? ORDER BY id DESC",
          (AID,))[0]["id"]
story_media = dbq("SELECT media FROM stories WHERE id=?", (SID,))[0]["media"]

# ---------------- 1. viewer shell: no end card, reply bar ----------------
r = B.get(f"/story/{SID}")
viewer_html = r.text
check("viewer page loads for a friend", r.status_code == 200)
check("viewer has NO 'caught up' end card text",
      "caught up" not in viewer_html.lower())
check("viewer has NO Watch again button",
      "Watch again" not in viewer_html and 'id="sv-again"' not in viewer_html
      and 'id="sv-end"' not in viewer_html)
check("running off the last story closes the viewer (JS contract)",
      "flat.length) { closeViewer(); return; }" in viewer_html)
check("someone else's story gets the reply bar",
      'id="sv-replybar"' in viewer_html
      and 'id="sv-reply-input"' in viewer_html
      and 'Reply to ' in viewer_html)
check("reaction row carries all 6 kinds",
      all(f'data-kind="{k}"' in viewer_html
          for k in ("like", "love", "laugh", "wow", "sad", "angry")))
check("tappable progress segments kept", "Jump to this story" in viewer_html)

# ---------------- 2. own-story bar: "N views" ----------------
r = E.get(f"/story/{SID}")          # Erin views too (Bob viewed above)
assert r.status_code == 200
r = A.get(f"/story/{SID}")          # owner opens own story (own view row
owner_html = r.text                  # is excluded from the count)
check("own bar renders the views counter",
      'id="sv-viewers-count"' in owner_html and "</span> views" in owner_html)
d = A.get(f"/story/{SID}/viewers").json()
check("viewers JSON counts 2 distinct viewers (owner excluded)",
      d.get("ok") and len(d.get("viewers") or []) == 2, str(d))
d = B.get(f"/story/{SID}/viewers")
check("viewers list stays owner-only (404 for a viewer)",
      d.status_code == 404)

# ---------------- 3. story reply -> inbox -------------------------------
r = B.post(f"/story/{SID}/reply", json={"body": "Nice one!"})
j = r.json()
check("friend reply accepted", r.status_code == 200 and j.get("ok"), r.text[:160])
mrow = dbq("SELECT * FROM messages WHERE id=?", (j.get("id"),))[0]
check("reply is a messages row sender->owner with story_id",
      mrow["sender_id"] == BID and mrow["receiver_id"] == AID
      and mrow["story_id"] == SID and mrow["body"] == "Nice one!")
clip_rel = mrow["story_media"] or ""
check("reply carries a shared clip copy under story_replies/",
      clip_rel.startswith("story_replies/")
      and clip_rel.endswith(os.path.basename(story_media))
      and os.path.isfile(os.path.join(os.environ["HERE_UPLOADS"], clip_rel)),
      clip_rel)
check("reply starts unread for the owner (nav badge counts it)",
      mrow["is_read"] == 0)
thr_a = A.get(f"/messages/{BID}").text
check("owner thread shows the clip card + 'replied to your story' + text",
      "Bob B replied to your story" in thr_a
      and f"/uploads/{clip_rel}" in thr_a
      and '<img class="story-clip"' in thr_a and "Nice one!" in thr_a)
thr_b = B.get(f"/messages/{AID}").text
check("sender thread shows the 'You replied to …'s story' card",
      "You replied to Alice A&#39;s story" in thr_b
      and f"/uploads/{clip_rel}" in thr_b)
inbox_a = A.get("/messages").text
check("owner inbox preview is prefixed as a story reply",
      "📖 Story reply: Nice one!" in inbox_a)
sent_b = B.get("/messages?box=sent").text
check("sender Sent preview is prefixed as a story reply",
      "📖 Story reply: Nice one!" in sent_b)

# ---------------- 4. story reaction -> inbox -----------------------------
r = B.post(f"/story/{SID}/react", json={"kind": "love"})
j2 = r.json()
check("reaction accepted", r.status_code == 200 and j2.get("ok"), r.text[:160])
rrow = dbq("SELECT * FROM messages WHERE id=?", (j2.get("id"),))[0]
check("reaction message body is the emoji, story-linked",
      rrow["body"] == "❤️" and rrow["story_id"] == SID)
check("reaction reuses the SAME shared clip copy",
      (rrow["story_media"] or "") == clip_rel, rrow["story_media"])
thr_a2 = A.get(f"/messages/{BID}").text
check("owner thread renders the 'reacted to your story' label",
      "Bob B reacted to your story" in thr_a2)
inbox_a2 = A.get("/messages").text
check("owner inbox preview shows the reaction wording",
      "📖 ❤️ reacted to a story" in inbox_a2)

# a video story's clip renders as a <video> card
r = post_story(A, fname="v.mp4", blob=MP4, ctype="video/mp4",
               caption="alice video story")
assert r.status_code == 303, r.status_code
SID2 = dbq("SELECT id FROM stories WHERE user_id=? ORDER BY id DESC",
           (AID,))[0]["id"]
r = B.post(f"/story/{SID2}/reply", json={"body": "vid reply"})
check("reply to a video story accepted", r.json().get("ok") is True)
thr_a3 = A.get(f"/messages/{BID}").text
check("video clip renders as a <video> story card",
      '<video class="story-clip"' in thr_a3 and "vid reply" in thr_a3)

# missing-clip placeholder (media gone at first reply -> story_media='')
conn = sqlite3.connect(os.environ["HERE_DB"])
conn.execute(
    "INSERT INTO messages(sender_id, receiver_id, body, created_at,"
    " story_id, story_media) VALUES(?,?,?,datetime('now'),?,?)",
    (BID, AID, "ghost note", 999999, ""))
conn.commit()
conn.close()
thr_a4 = A.get(f"/messages/{BID}").text
check("story-linked message with no clip renders the placeholder tile",
      "📖 Story no longer available" in thr_a4)

# ---------------- 5. gates -------------------------------------------------
r = anon.post(f"/story/{SID}/reply", json={"body": "x"})
check("reply requires login (401)", r.status_code == 401)
r = C.post(f"/story/{SID}/reply", json={"body": "hi"})
check("stranger reply refused (404)", r.status_code == 404
      and not r.json().get("ok"))
r = C.post(f"/story/{SID}/react", json={"kind": "like"})
check("stranger react refused (404)", r.status_code == 404)
r = A.post(f"/story/{SID}/reply", json={"body": "me"})
check("reply to own story refused (400)", r.status_code == 400
      and not r.json().get("ok"))
r = A.post(f"/story/{SID}/react", json={"kind": "like"})
check("react to own story refused (400)", r.status_code == 400)
r = B.post(f"/story/{SID}/reply", json={"body": "   "})
check("empty reply body refused (400)", r.status_code == 400)
r = B.post(f"/story/{SID}/react", json={"kind": "shrug"})
check("unknown reaction kind refused (400)", r.status_code == 400)
n_before = dbq("SELECT COUNT(*) c FROM messages")[0]["c"]
r = B.post(f"/messages/{AID}", data={"body": "plain dm still works"})
n_after = dbq("SELECT COUNT(*) c FROM messages")[0]["c"]
check("normal DM path unchanged (no story link)",
      r.status_code == 303 and n_after == n_before + 1
      and dbq("SELECT story_id FROM messages ORDER BY id DESC LIMIT 1"
              )[0]["story_id"] is None)

# blocked pair: Dave is Alice's friend; Alice blocks him
r = A.post(f"/block/{DID}")
assert r.status_code in (200, 303), r.status_code
r = D.post(f"/story/{SID}/reply", json={"body": "let me in"})
j = r.json()
check("blocked sender reply refused with the message route's wording",
      r.status_code == 403
      and j.get("error") == "You can't message this person.", r.text[:160])
r = D.post(f"/story/{SID}/react", json={"kind": "wow"})
check("blocked sender react refused (403, same wording)",
      r.status_code == 403
      and r.json().get("error") == "You can't message this person.")

# ---------------- 6. delete-for-me interplay ------------------------------
reply_mid = dbq("SELECT id FROM messages WHERE body='Nice one!'")[0]["id"]
r = A.post(f"/messages/{reply_mid}/delete")
assert r.status_code in (200, 303), r.status_code
thr_a5 = A.get(f"/messages/{BID}").text
check("recipient delete-for-me hides the story reply from THEIR thread",
      "Nice one!" not in thr_a5)
check("... while the reaction card still shows for them",
      "Bob B reacted to your story" in thr_a5)
thr_b2 = B.get(f"/messages/{AID}").text
check("sender's copy of the story reply is untouched",
      "Nice one!" in thr_b2)

# ---------------- 7. clip survives the story's expiry ---------------------
conn = sqlite3.connect(os.environ["HERE_DB"])
conn.execute("UPDATE stories SET expires_at='2000-01-01T00:00:00+00:00' "
             "WHERE id=?", (SID,))
conn.commit()
conn.close()
here.prune_stories()
check("story row is pruned after expiry",
      dbq("SELECT COUNT(*) c FROM stories WHERE id=?", (SID,))[0]["c"] == 0)
check("the shared clip copy still exists on disk",
      os.path.isfile(os.path.join(os.environ["HERE_UPLOADS"], clip_rel)))
thr_a6 = A.get(f"/messages/{BID}").text
check("thread STILL renders the clip after the story is gone",
      f"/uploads/{clip_rel}" in thr_a6
      and "Bob B reacted to your story" in thr_a6)

# ---------------- 8. taps: numbers clear on acknowledgment -----------------
def unacked(uid):
    conn = here.db()
    n = here._unacked_tap_count(conn, uid)
    conn.close()
    return n

A.post(f"/tap/{BID}")
A.post(f"/tap/{BID}")
check("2 fresh taps count as 2 unacknowledged", unacked(BID) == 2,
      str(unacked(BID)))
prof_b = B.get(f"/user/{BID}").text
check("recipient's profile shows '🖐️ 2 taps'",
      "🖐️ 2 taps" in prof_b)
B.get(f"/user/{AID}")   # Bob visits the tapper's profile -> acknowledged
check("visiting the tapper acknowledges their taps", unacked(BID) == 0)
prof_b2 = B.get(f"/user/{BID}").text
check("... and the number is GONE from the profile (no 🖐️ at all)",
      "🖐️" not in prof_b2)
A.post(f"/tap/{BID}")
check("a fresh tap after the ack counts again (1)", unacked(BID) == 1)
prof_b3 = B.get(f"/user/{BID}").text
check("profile shows the fresh '🖐️ 1 taps'", "🖐️ 1 taps" in prof_b3)
B.post(f"/tap/{AID}")   # Bob taps Alice back -> acknowledges hers
check("tapping back acknowledges too", unacked(BID) == 0)
prof_b4 = B.get(f"/user/{BID}").text
check("number gone again after the tap-back", "🖐️" not in prof_b4)

# ---------------- 9. migration safety ---------------------------------------
# (a) a pre-change-shaped DB: messages without the story columns and
# no tap_acks table must boot, gain both, and keep its rows.
old_dir = tempfile.mkdtemp(prefix="here-test-old-")
old_db = os.path.join(old_dir, "old.db")
conn = sqlite3.connect(old_db)
conn.execute("CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT,"
             " sender_id INTEGER NOT NULL, receiver_id INTEGER NOT NULL,"
             " body TEXT NOT NULL, is_read INTEGER DEFAULT 0,"
             " created_at TEXT NOT NULL)")
conn.execute("INSERT INTO messages(sender_id, receiver_id, body,"
             " created_at) VALUES(1, 2, 'old row',"
             " '2026-01-01T00:00:00+00:00')")
conn.execute("CREATE TABLE taps (id INTEGER PRIMARY KEY AUTOINCREMENT,"
             " tapper_id INTEGER NOT NULL, tapped_id INTEGER NOT NULL,"
             " created_at TEXT NOT NULL)")
conn.commit()
conn.close()
env = dict(os.environ, HERE_DB=old_db,
           HERE_UPLOADS=os.path.join(old_dir, "uploads"))
boot = subprocess.run(
    [sys.executable, "-c",
     "import app; from fastapi.testclient import TestClient;"
     "print(TestClient(app.app).get('/login').status_code)"],
    cwd=BASE, env=env, capture_output=True, text=True)
check("old-shaped DB boots (login 200)", boot.stdout.strip() == "200",
      boot.stderr[-200:] if boot.returncode else boot.stdout)
cols = [r[1] for r in sqlite3.connect(old_db).execute(
    "PRAGMA table_info(messages)")]
check("old DB gains story_id + story_media, keeps its rows",
      "story_id" in cols and "story_media" in cols
      and sqlite3.connect(old_db).execute(
          "SELECT body FROM messages WHERE id=1").fetchone()[0] == "old row")
tabs = [r[0] for r in sqlite3.connect(old_db).execute(
    "SELECT name FROM sqlite_master WHERE type='table'")]
check("old DB gains tap_acks", "tap_acks" in tabs)

# (b) the real dev DB (a copy — the original is never touched)
dev_db = os.path.join(BASE, "here.db")
if os.path.exists(dev_db):
    copy_db = os.path.join(old_dir, "devcopy.db")
    shutil.copyfile(dev_db, copy_db)
    env2 = dict(os.environ, HERE_DB=copy_db,
                HERE_UPLOADS=os.path.join(old_dir, "uploads2"))
    boot2 = subprocess.run(
        [sys.executable, "-c",
         "import app; from fastapi.testclient import TestClient;"
         "print(TestClient(app.app).get('/login').status_code)"],
        cwd=BASE, env=env2, capture_output=True, text=True)
    cols2 = [r[1] for r in sqlite3.connect(copy_db).execute(
        "PRAGMA table_info(messages)")]
    check("dev-DB copy boots with the new columns",
          boot2.stdout.strip() == "200" and "story_id" in cols2
          and "story_media" in cols2,
          boot2.stderr[-200:] if boot2.returncode else "")

# ---------------- 10. node --check on rendered JS ---------------------------
def js_blocks(html_text):
    return re.findall(r"<script>(.*?)</script>", html_text, re.S)


pages = {"story_viewer": viewer_html,
         "thread": B.get(f"/messages/{AID}").text,
         "home": B.get("/home").text}
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
check("node --check: inline JS on changed pages parses", all_ok,
      "; ".join(bad))

# ---------------- summary ----------------
fails = [n for n, ok, _ in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL GREEN")
