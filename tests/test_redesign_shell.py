"""Redesign Phase 1 (Ken's order 2026-10-26, from his Facebook
walkthrough video): the FAMILIAR shell in HERE's skin — top bar
with a create button, a create sheet (Post/Story/Reel/Live), a
floating glass bottom tab bar with badges, a drawer profile
card, TALL story cards on home, a composer strip (avatar +
pill + photo), feed modules (People you may know, Reels rail),
and a /reels page. Glass + Spot vars must survive untouched.

Run:  ./venv/bin/python tests/test_redesign_shell.py
"""
import os
import sqlite3
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-shell-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{extra}]" if extra and not cond else ""), flush=True)


def dbq(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"], timeout=10)
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


PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
       b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
       b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n"
       b"-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64

A, AID = signup("Shell Alice", "alice@shell.io")
B, BID = signup("Shell Bob", "bob@shell.io")
C, CID = signup("Shell Cleo", "cleo@shell.io")

# A fills the feed (own posts show in A's home feed)
for i in range(4):
    r = A.post("/posts", data={"kind": "text",
                               "body": f"shell post {i}",
                               "privacy": "public"})
    assert r.status_code in (200, 303), r.status_code
# two video posts -> the Reels rail + /reels page have content
for i in range(2):
    r = A.post("/posts", data={"kind": "video",
                               "body": f"shell reel {i}",
                               "privacy": "public"},
               files={"media": (f"reel{i}.mp4", MP4, "video/mp4")})
    assert r.status_code in (200, 303), (i, r.status_code, r.text[:200])
# A posts an image story (rail thumb)
r = A.post("/stories/new", data={},
           files={"media": ("story.png", PNG, "image/png")})
assert r.status_code in (200, 303), r.status_code
story_media = dbq("SELECT media FROM stories WHERE user_id=?",
                  (AID,))[0]["media"]

home = A.get("/home")
check("home renders 200", home.status_code == 200)
page = home.text

# ---- the shell (base.html) ----
for needle, label in [
    ('id="menu-btn"', "menu button in the top bar"),
    ('id="create-btn"', "create (+) button in the top bar"),
    ('id="create-sheet"', "create sheet"),
    ('id="create-golive"', "create sheet Live row wired to go-live"),
    ('href="/stories/new"', "create sheet Story row"),
    ('compose=video', "create sheet Reel row deep-links the composer"),
    ('id="site-tabbar"', "bottom tab bar"),
    ('data-tab="/views"', "tab bar Peeps tab (points at the feed)"),
    ('>Peeps</a>', "tab bar says Peeps, not Reels"),
    ('data-tab="/friends"', "tab bar Friends tab"),
    ('data-tab="/market"', "tab bar Market tab"),
    ('data-tab="/notifications"', "tab bar Alerts tab"),
    ('class="tab-it tab-av"', "tab bar avatar (You) tab"),
    ('has-tabbar', "body carries has-tabbar"),
    ('drawer-profile', "drawer profile card"),
    ('--glass-alpha', "glass vars still emitted (glass look kept)"),
    ("phase1-style", "phase-1 style block"),
]:
    check(f"shell: {label}", needle in page)

# the tab bar must NOT ride the live page chrome
live_r = A.post("/live/start", json={"title": "Shell Live"})
lid = live_r.json()["id"]
live_page = A.get(f"/live/{lid}").text
check("shell: live page body is marked live-page (tab bar hides)",
      "live-page" in live_page)
A.post(f"/live/{lid}/end", json={})

# ---- home: stories rail ----
check("home: tall stories rail present", 'class="stories-rail"' in page)
check("home: Create story card first", 'story-card create' in page
      and "Create story" in page)
check("home: own story card present", "Your story" in page)
check("home: story card uses the story image as its cover",
      story_media and story_media in page)

# ---- home: composer strip ----
check("home: composer strip with avatar + pill",
      'class="composer-strip"' in page and "What's on your mind?" in page)
check("home: composer strip photo button", 'id="slim-photo"' in page)
check("home: deep-link JS opens the composer",
      "compose=video" in page and "openComposer" in page)

# ---- home: feed modules ----
check("home: People-you-may-know module renders",
      "People you may know" in page)
check("home: PYMK suggests the stranger (Bob)",
      "Shell Bob" in page)
check("home: PYMK hides existing friends/self only — Cleo also there",
      "Shell Cleo" in page)
check("home: PYMK carries a working Add friend form",
      f'action="/friends/request/{BID}"' in page)
check("home: Peeps rail module renders", "reels-rail" in page
      and "▶ Peep" in page and "🎬 Peeps" in page)

# add-friend from the module actually files the request
r = A.post(f"/friends/request/{BID}")
check("pymk: Add friend files a pending request",
      r.status_code in (200, 303)
      and dbq("SELECT status FROM friendships WHERE user_id=? "
              "AND friend_id=?", (AID, BID))[0]["status"] == "pending",
      str(r.status_code))
# after the request, Bob leaves the suggestions
page2 = A.get("/home").text
check("pymk: requested person leaves the module",
      f'action="/friends/request/{BID}"' not in page2)

# ---- /reels + /peeps (the Peeps grid) ----
rp = A.get("/reels")
check("reels: page renders 200", rp.status_code == 200)
check("reels: grid carries the video posts",
      "reels-grid" in rp.text and ".mp4" in rp.text)
check("peeps: grid is branded Peeps",
      "🎬 Peeps" in rp.text and "Post a peep" in rp.text)
pp = A.get("/peeps")
check("peeps: /peeps alias serves the same grid",
      pp.status_code == 200 and "reels-grid" in pp.text)
vp = A.get("/views")
check("peeps: the tab's feed page is branded Peeps",
      vp.status_code == 200 and "🎬 Peeps" in vp.text)
check("reels: guests are bounced to login",
      TestClient(here.app, follow_redirects=False)
      .get("/reels").status_code in (302, 303, 401))

# tray data: thumb is the image story's media
tray = here.stories_tray(AID)
mine = [x for x in tray if x["is_self"]]
check("server: stories_tray carries the image thumb",
      bool(mine and mine[0]["thumb"] == story_media),
      str(mine))

# ---- transparency: the owner's choice rules their profile ----
import re as _re


def body_alpha(html):
    m = _re.search(r"<body[^>]*style=\"([^\"]*)\"", html)
    if not m:
        return None
    a = _re.search(r"--glass-alpha:([0-9.]+)", m.group(1))
    return a.group(1) if a else None


def scope_soft(html):
    m = _re.search(r'class="spot-scope[^"]*" style="([^"]*)"', html)
    if not m:
        return None
    hits = _re.findall(r"--glass-alpha-soft:([0-9.]+)",
                       m.group(1))
    return hits[-1] if hits else None


def scope_alpha(html):
    m = _re.search(r'class="spot-scope[^"]*" style="([^"]*)"', html)
    if not m:
        return None
    hits = _re.findall(r"--glass-alpha:([0-9.]+)", m.group(1))
    return hits[-1] if hits else None


os.makedirs(os.path.join(os.environ["HERE_UPLOADS"],
                         "backgrounds"), exist_ok=True)
with open(os.path.join(os.environ["HERE_UPLOADS"],
                       "backgrounds", "cleo.jpg"), "wb") as fh:
    fh.write(PNG)
conn = sqlite3.connect(os.environ["HERE_DB"])
conn.execute("UPDATE users SET background=? WHERE id=?",
             ("backgrounds/cleo.jpg", CID))
conn.commit()
conn.close()
prof = B.get(f"/user/{CID}")
check("transparency: profile SCOPE defaults to CLEAR GLASS "
      "when the owner has a background (0.35)",
      scope_alpha(prof.text) == "0.35",
      str(scope_alpha(prof.text)))
check("transparency: visitor chrome keeps the visitor's own "
      "alpha on a profile (0.82)", body_alpha(prof.text) == "0.82",
      str(body_alpha(prof.text)))
check("transparency: the owner's background is on the page",
      "/uploads/backgrounds/cleo.jpg" in prof.text)
check("transparency: the page_bg rule sits INSIDE a <style> "
      "element (a missing opener once dumped the rule as text "
      "and no background ever painted)",
      "<style>\nbody.has-profile-bg{background-image:" in prof.text
      or "<style>body.has-profile-bg{background-image:" in prof.text)
check("transparency: profile cards PAINT from the glass vars "
      "(no stale hardcode shadows the slider)",
      "has-profile-bg .card{background:rgba(var(--glass-rgb"
      in A.get("/home").text)
check("transparency: comment bubbles join clear glass (soft "
      "var is 0.35 too, not the 0.82 default)",
      scope_soft(prof.text) == "0.35",
      str(scope_soft(prof.text)))
check("transparency: profile page emits --profile-glass for "
      "the chrome floating over the background",
      "--profile-glass:0.35" in prof.text)
check("transparency: chrome override ships in the CSS",
      "has-profile-bg .topbar{background:rgba(17,17,17,"
      "var(--profile-glass" in A.get("/home").text
      and "has-profile-bg #site-tabbar" in A.get("/home").text)
b_home = B.get("/home")
check("transparency: visitor's own pages keep THEIR alpha (0.82)",
      body_alpha(b_home.text) == "0.82", str(body_alpha(b_home.text)))
r = C.post("/settings/boxes", data={"box_opacity": "30"})
check("transparency: slider save accepted", r.status_code in (200, 303))
prof2 = B.get(f"/user/{CID}")
check("transparency: owner's saved 30 rules their profile SCOPE "
      "for visitors (0.3)", scope_alpha(prof2.text) == "0.3",
      str(scope_alpha(prof2.text)))
own = C.get(f"/user/{CID}")
check("transparency: NO Transparency button on the profile "
      "(Ken's order — it lives in Settings only)",
      "🪟 Transparency</a>" not in own.text)
check("transparency: the slider card still lives in Settings",
      "Glass transparency" in C.get("/settings/profile").text)

# ---- the post page is the author's stage too ----
# (Cleo has the background; Alice's post is on Alice — use
# Cleo's own post for the permalink check)
r = C.post("/posts", data={"kind": "text",
                           "body": "cleo stage post",
                           "privacy": "public"})
assert r.status_code in (200, 303), r.status_code
_cpid = dbq("SELECT id FROM posts WHERE user_id=? "
            "ORDER BY id DESC", (CID,))[0]["id"]
perm = B.get(f"/post/{_cpid}")
check("permalink: wears the author's background",
      "/uploads/backgrounds/cleo.jpg" in perm.text)
check("permalink: emits the author's showcase vars",
      "--profile-glass:0.3" in perm.text
      and "--profile-glass-soft:" in perm.text)
check("permalink: bubbles prefer the showcase soft in CSS",
      "has-profile-bg .comment .cbub{background:rgba(var"
      "(--glass-rgb,255,255,255),var(--profile-glass-soft"
      in perm.text)

# ---- Build PROFILE TOOLS (Ken Oct 27): the own-profile button
# stack collapses into ONE pill + hidden panel of full-width rows;
# visitors keep Lives + Share out in the open.
ownp = C.get(f"/user/{CID}")
check("profile tools: ONE Profile tools pill on own profile",
      ownp.text.count("profile-tools-toggle") == 1
      and "Profile tools" in ownp.text)
check("profile tools: panel starts hidden",
      'id="profile-tools" style="flex:1 1 100%" hidden'
      in ownp.text)
for rowtxt in ("Lives", "Share profile", "Invite friends",
               "Edit Profile", "My earnings", "Customize my spot",
               "Creator Mode:"):
    check(f"profile tools: row '{rowtxt}'", rowtxt in ownp.text)
check("profile tools: no loose owner buttons left",
      'class="btn-ghost" href="/earnings"' not in ownp.text
      and 'class="btn-ghost" href="/invite"' not in ownp.text)
visp = B.get(f"/user/{CID}")
check("profile tools: visitors get NO tools panel",
      "profile-tools-toggle" not in visp.text)
check("profile tools: visitors keep Lives + Share visible",
      'class="btn-ghost" href="/lives"' in visp.text
      and "Share profile" in visp.text)

failed = [n for n, ok in RESULTS if not ok]
print()
print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
if failed:
    print("FAILURES:", failed)
    sys.exit(1)
