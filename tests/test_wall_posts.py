"""Build WALL — wall posts + tagging everywhere.

Run:  ./venv/bin/python tests/test_wall_posts.py

Friends can write on your profile wall when it's open (the default);
the post is a normal post stamped with posts.wall_user_id, badged
"<author> wrote on <owner>'s wall" on the owner's profile. Covers:
the write gate (closed wall / non-friend / blocked pair all refuse
and create nothing), approve-first pending flow (invisible to
everyone but the owner → approve → visible; delete-pending),
wall-owner + author delete rights, private-profile walls following
profile privacy, the Settings + quick-toggle persistence, @tags in
wall posts notifying the tagged user, moderation flagging, and the
My Views composer carrying the @tag picker (same /posts pipeline).
"""
import json, os, re, shutil, sqlite3, subprocess, sys, tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-wall-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64


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


def befriend(a_client, a_id, b_client, b_id):
    r = a_client.post(f"/friends/request/{b_id}")
    assert r.status_code in (303, 302), r.status_code
    r = b_client.post(f"/friends/accept/{a_id}")
    assert r.status_code in (303, 302), r.status_code


def wall_post(client, owner_id, body, **kw):
    data = {"kind": "text", "body": body, "privacy": "public",
            "wall_user_id": str(owner_id), "next": f"/user/{owner_id}"}
    data.update(kw)
    return client.post("/posts", data=data)


def posts_by_body(body):
    return dbq("SELECT * FROM posts WHERE body=?", (body,))


def notes(uid, kind=None):
    if kind:
        return dbq("SELECT * FROM notifications WHERE user_id=? "
                   "AND kind=?", (uid, kind))
    return dbq("SELECT * FROM notifications WHERE user_id=?", (uid,))


# ================= cast =================
OWN, OID = signup("Olive Owner", "olive@wall.io")
FRD, FID = signup("Fred Friend", "fred@wall.io")
THD, TID = signup("Thea Third", "thea@wall.io")
STR, SID = signup("Stan Stranger", "stan@wall.io")
TAG, TGID = signup("Tessa Taggee", "tessa@wall.io")
befriend(OWN, OID, FRD, FID)
befriend(OWN, OID, THD, TID)
befriend(FRD, FID, TAG, TGID)   # taggee is the AUTHOR's friend

# ================= defaults =================
row = dbq("SELECT wall_open, wall_approve_first FROM users WHERE id=?",
          (OID,))[0]
check("new accounts: wall_open defaults OPEN (1)",
      row["wall_open"] == 1)
check("new accounts: wall_approve_first defaults OFF (0)",
      row["wall_approve_first"] == 0)

# ================= open wall: friend writes =================
r = wall_post(FRD, OID, "fred was here on the wall")
check("open wall: friend's wall post lands (303)", r.status_code == 303)
rows = posts_by_body("fred was here on the wall")
check("open wall: post row created", len(rows) == 1)
W1 = rows[0]["id"] if rows else 0
check("open wall: wall_user_id stamped with the owner",
      bool(rows) and rows[0]["wall_user_id"] == OID)
check("open wall: not pending", bool(rows) and not rows[0]["wall_pending"])
check("open wall: author is the writer",
      bool(rows) and rows[0]["user_id"] == FID)

html = OWN.get(f"/user/{OID}").text
check("owner profile shows the wall post body",
      "fred was here on the wall" in html)
check("owner profile badges it as a wall post",
      "wrote on" in html and "Olive Owner" in html)
check("wall post card keeps the author's name on it",
      "Fred Friend" in html)
html = FRD.get(f"/user/{OID}").text
check("author sees own wall post on the owner's profile",
      "fred was here on the wall" in html)
check("owner notified: kind 'wall'",
      len(notes(OID, "wall")) == 1
      and "Fred Friend wrote on your wall."
      in (notes(OID, "wall")[0]["text"] if notes(OID, "wall") else ""))
html = OWN.get("/home").text
check("wall post also flows through the author's home feed normally",
      "fred was here on the wall" in html)

# profile composer surfaces (Build SLIM bar, wall label)
html = FRD.get(f"/user/{OID}").text
check("friend sees the slim bar on the owner's profile",
      'id="slim-bar"' in html
      and "Say something on Olive Owner's wall…" in html)
check("friend's profile composer carries the wall target",
      f'name="wall_user_id" value="{OID}"' in html)
check("friend's profile composer is collapsed by default (SLIM)",
      '<div class="slim-body" id="slim-body" hidden>' in html)
html = OWN.get(f"/user/{OID}").text
check("owner's own profile bar keeps the plain label",
      "Say something…" in html and 'name="wall_user_id"' not in html)
check("owner gets the wall quick-toggle strip on their profile",
      'id="wall-settings"' in html and 'name="wall_open"' in html)
html = STR.get(f"/user/{OID}").text
check("non-friend sees NO composer on the profile",
      'id="slim-composer"' not in html)

# own wall_user_id = self is a normal post, no stamp
r = OWN.post("/posts", data={"kind": "text", "body": "my own note",
                             "privacy": "public",
                             "wall_user_id": str(OID)})
check("writing with wall_user_id=self still lands", r.status_code == 303)
rows = posts_by_body("my own note")
check("self-target post carries NO wall stamp",
      len(rows) == 1 and rows[0]["wall_user_id"] is None)

# ================= closed wall refuses =================
r = OWN.post("/settings/wall", data={"wall_open": "0",
                                     "next": "/settings/profile"})
check("settings wall save redirects", r.status_code == 303)
check("wall_open=0 persisted",
      dbq("SELECT wall_open FROM users WHERE id=?",
          (OID,))[0]["wall_open"] == 0)
html = FRD.get(f"/user/{OID}").text
check("friend sees the closed-wall note, no composer",
      "wall is closed" in html and 'id="slim-composer"' not in html)
c2 = TestClient(here.app, follow_redirects=True)
c2.cookies.update(FRD.cookies)
r = c2.post("/posts", data={"kind": "text", "body": "sneak closed",
                            "privacy": "public",
                            "wall_user_id": str(OID),
                            "next": f"/user/{OID}"})
check("closed wall: refusal explains itself",
      "wall is closed" in r.text)
check("closed wall: NO post created",
      len(posts_by_body("sneak closed")) == 0)
OWN.post("/settings/wall", data={"wall_open": "1",
                                 "next": "/settings/profile"})
check("wall_open=1 persisted again",
      dbq("SELECT wall_open FROM users WHERE id=?",
          (OID,))[0]["wall_open"] == 1)

# ================= non-friend refuses =================
c3 = TestClient(here.app, follow_redirects=True)
c3.cookies.update(STR.cookies)
r = c3.post("/posts", data={"kind": "text", "body": "stranger scribble",
                            "privacy": "public",
                            "wall_user_id": str(OID),
                            "next": f"/user/{OID}"})
check("non-friend: refusal says friends only",
      "friends can write on their wall" in r.text)
check("non-friend: NO post created",
      len(posts_by_body("stranger scribble")) == 0)

# ================= blocked pair refuses =================
BO, BOID = signup("Bea Blockowner", "bea@wall.io")
BF, BFID = signup("Ben Blockfriend", "ben@wall.io")
befriend(BO, BOID, BF, BFID)
r = BO.post(f"/block/{BFID}")
check("block lands", r.status_code in (303, 302))
r = wall_post(BF, BOID, "blocked scribble")
check("blocked pair: NO post created",
      len(posts_by_body("blocked scribble")) == 0)

# ================= approve-first =================
AO, AOID = signup("Amy Approve", "amy@wall.io")
AF, AFID = signup("Al Approver", "al@wall.io")
AT, ATID = signup("Ari Athird", "ari@wall.io")
befriend(AO, AOID, AF, AFID)
befriend(AO, AOID, AT, ATID)
AO.post("/settings/wall", data={"wall_open": "1",
                                "wall_approve_first": "1"})
check("approve-first persisted",
      dbq("SELECT wall_approve_first FROM users WHERE id=?",
          (AOID,))[0]["wall_approve_first"] == 1)
r = wall_post(AF, AOID, "pending hello wall")
check("approve-first: post lands (303)", r.status_code == 303)
rows = posts_by_body("pending hello wall")
check("approve-first: row is PENDING",
      len(rows) == 1 and rows[0]["wall_pending"] == 1)
P1 = rows[0]["id"] if rows else 0
check("approve-first: owner got the pending notification",
      len(notes(AOID, "wall_pending")) == 1
      and "wants to write on your wall"
      in (notes(AOID, "wall_pending")[0]["text"]
          if notes(AOID, "wall_pending") else ""))
html = AT.get(f"/user/{AOID}").text
check("pending: invisible to a third friend",
      "pending hello wall" not in html)
html = AF.get(f"/user/{AOID}").text
check("pending: invisible to its own author on the wall",
      "pending hello wall" not in html)
html = AF.get(f"/post/{P1}").text
check("pending: author's own post page doesn't show it either",
      "pending hello wall" not in html)
html = AO.get(f"/user/{AOID}").text
check("pending: owner sees it in the approval section",
      "pending hello wall" in html
      and 'id="wall-pending"' in html
      and f'action="/wall/{P1}/approve"' in html
      and f'action="/wall/{P1}/delete"' in html)
r = AT.post(f"/wall/{P1}/approve")
check("pending: a non-owner CANNOT approve it",
      dbq("SELECT wall_pending FROM posts WHERE id=?",
          (P1,))[0]["wall_pending"] == 1)
_af_notes_before = len(notes(AFID))
r = AO.post(f"/wall/{P1}/approve")
check("approve lands (303)", r.status_code == 303)
check("approve: pending flag cleared",
      dbq("SELECT wall_pending FROM posts WHERE id=?",
          (P1,))[0]["wall_pending"] == 0)
html = AT.get(f"/user/{AOID}").text
check("approved: now visible to the third friend",
      "pending hello wall" in html)
check("approve sends the author NO notification",
      len(notes(AFID)) == _af_notes_before)

# delete-pending path
wall_post(AF, AOID, "pending begone")
rows = posts_by_body("pending begone")
P2 = rows[0]["id"] if rows else 0
r = AT.post(f"/wall/{P2}/delete")
check("pending: a non-owner CANNOT delete it",
      len(dbq("SELECT id FROM posts WHERE id=?", (P2,))) == 1)
r = AO.post(f"/wall/{P2}/delete")
check("owner deletes the pending post",
      len(dbq("SELECT id FROM posts WHERE id=?", (P2,))) == 0)

# ================= delete rights on approved wall posts =================
wall_post(FRD, OID, "fred delete bait")
rows = posts_by_body("fred delete bait")
D1 = rows[0]["id"] if rows else 0
r = THD.post(f"/post/{D1}/delete")
check("third party CANNOT delete someone else's wall post",
      len(dbq("SELECT id FROM posts WHERE id=?", (D1,))) == 1)
r = OWN.post(f"/post/{D1}/delete")
check("wall OWNER can delete a post from their wall",
      len(dbq("SELECT id FROM posts WHERE id=?", (D1,))) == 0)
wall_post(FRD, OID, "fred self delete")
rows = posts_by_body("fred self delete")
D2 = rows[0]["id"] if rows else 0
r = FRD.post(f"/post/{D2}/delete")
check("author can still delete their own wall post",
      len(dbq("SELECT id FROM posts WHERE id=?", (D2,))) == 0)

# ================= private profile wall =================
PO, POID = signup("Pam Private", "pam@wall.io")
PF, PFID = signup("Pete Pal", "pete@wall.io")
befriend(PO, POID, PF, PFID)
PO.post("/settings/privacy", data={"private_profile": "1"})
wall_post(PF, POID, "pam wall secret", privacy="public")
rows = posts_by_body("pam wall secret")
PV = rows[0]["id"] if rows else 0
check("private wall: friend's wall post created", len(rows) == 1)
html = STR.get(f"/user/{POID}").text
check("private wall: non-friend sees the locked profile",
      "This profile is private" in html)
check("private wall: non-friend does NOT see the wall post",
      "pam wall secret" not in html)
html = PF.get(f"/user/{POID}").text
check("private wall: the owner's friend DOES see it",
      "pam wall secret" in html)
html = STR.get("/home").text
check("private wall: stranger's feed stays clean",
      "pam wall secret" not in html)
anon = TestClient(here.app, follow_redirects=False)
html = anon.get(f"/p/{PV}").text
check("private wall: public post page teasers (no body leak)",
      "pam wall secret" not in html)

# ================= settings surface =================
html = OWN.get("/settings/profile").text
check("settings: 🧱 Wall card present with both switches",
      'id="wall"' in html and 'action="/settings/wall"' in html
      and 'name="wall_open"' in html
      and 'name="wall_approve_first"' in html)
r = OWN.post("/settings/wall",
             data={"wall_open": "1", "wall_approve_first": "1",
                   "next": f"/user/{OID}"})
check("quick toggle (profile) saves + lands back on the profile",
      r.status_code == 303
      and r.headers.get("location", "").startswith(f"/user/{OID}"))
check("quick toggle persisted both switches",
      dbq("SELECT wall_open, wall_approve_first FROM users "
          "WHERE id=?", (OID,))[0]["wall_approve_first"] == 1)
OWN.post("/settings/wall", data={"wall_open": "1",
                                 "next": "/settings/profile"})
check("approve-first switched back off",
      dbq("SELECT wall_approve_first FROM users WHERE id=?",
          (OID,))[0]["wall_approve_first"] == 0)

# ================= @tag in a wall post =================
tags = json.dumps([{"kind": "user", "id": TGID, "name": "Tessa Taggee"}])
r = wall_post(FRD, OID, "hi @Tessa Taggee look at this wall",
              tags_json=tags)
check("wall post with a tag lands", r.status_code == 303)
rows = posts_by_body("hi @Tessa Taggee look at this wall")
T1 = rows[0]["id"] if rows else 0
check("wall tag: post_tags row written",
      len(dbq("SELECT * FROM post_tags WHERE post_id=? AND user_id=?",
              (T1, TGID))) == 1)
check("wall tag: tagged user notified (kind 'tag')",
      len(notes(TGID, "tag")) >= 1)
html = OWN.get(f"/user/{OID}").text
check("wall tag: card shows the 'with' line",
      "Tessa Taggee" in html and "with" in html)

# ================= moderation sees wall posts =================
wall_post(FRD, OID, "this wall post is fucked up")
rows = posts_by_body("this wall post is fucked up")
M1 = rows[0]["id"] if rows else 0
check("wall posts are moderation-flagged like normal posts",
      len(dbq("SELECT * FROM moderation_flags WHERE item_type='post' "
              "AND item_id=? AND status='pending'", (M1,))) == 1)

# ================= My Views tagging =================
html = FRD.get("/views").text
check("views composer carries the @tag picker hooks",
      'id="composer-body"' in html and 'id="tags-json"' in html
      and 'id="tag-suggest"' in html)
check("views composer: old unhooked textarea id is gone",
      'id="mv-comp-body"' not in html)
tags = json.dumps([{"kind": "user", "id": TGID, "name": "Tessa Taggee"}])
before = len(notes(TGID, "tag"))
r = FRD.post("/posts",
             data={"kind": "video", "body": "my view @Tessa Taggee",
                   "privacy": "public", "next": "/views",
                   "tags_json": tags},
             files={"media": ("v.mp4", MP4, "video/mp4")})
check("views post lands back on /views",
      r.status_code == 303
      and r.headers.get("location", "").endswith("/views"))
rows = posts_by_body("my view @Tessa Taggee")
V1 = rows[0]["id"] if rows else 0
check("views tag: post_tags row written through the same pipeline",
      len(dbq("SELECT * FROM post_tags WHERE post_id=? AND user_id=?",
              (V1, TGID))) == 1)
check("views tag: tagged user notified",
      len(notes(TGID, "tag")) == before + 1)

# ================= inline JS parses (node --check) =================
node = shutil.which("node")
if node:
    all_ok = True
    for label, client, path in (("profile-owner", OWN, f"/user/{OID}"),
                                ("profile-friend", FRD, f"/user/{OID}"),
                                ("views", FRD, "/views")):
        text = client.get(path).text
        for i, block in enumerate(
                re.findall(r"<script>(.*?)</script>", text, re.S)):
            p = os.path.join(_tmp, f"js-{label}-{i}.js")
            with open(p, "w") as f:
                f.write(block)
            pr = subprocess.run([node, "--check", p],
                                capture_output=True, text=True)
            if pr.returncode != 0:
                all_ok = False
                print("node --check failed:", label, i, pr.stderr[:300])
    check("node --check: profile + views inline JS parses", all_ok)
else:
    check("node --check skipped (node unavailable)", True)

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
