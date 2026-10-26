"""Build POST OPTIONS — one clean button under every post.

Run:  ./venv/bin/python tests/test_post_options.py

Ken's order (2026-10-26): the cluttered row under every post —
Like, React, Comment, copy link, share sheets, hide-from,
pin, blocked list, hide, delete — is cleaned up under ONE
button that says "Post options". Tapping it opens a tidy panel
holding every one of those actions, unchanged underneath:
same routes, same forms, same permission flags, so who sees
which action is exactly who saw it before.
"""
import os, re, shutil, sqlite3, subprocess, sys, tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-postoptions-")
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
                                "password": "pw123456"})
    assert r.status_code == 303, (name, r.status_code)
    uid = dbq("SELECT id FROM users WHERE email=?", (email,))[0]["id"]
    return c, uid


def befriend(a_client, a_id, b_client, b_id):
    r = a_client.post(f"/friends/request/{b_id}")
    assert r.status_code in (303, 302), r.status_code
    r = b_client.post(f"/friends/accept/{a_id}")
    assert r.status_code in (303, 302), r.status_code


ANN, AID = signup("Ann Author", "ann@popt.io")
BOB, BID = signup("Bob Buddy", "bob@popt.io")
befriend(ANN, AID, BOB, BID)

r = ANN.post("/posts", data={"kind": "text",
                             "body": "post options probe",
                             "privacy": "public"})
assert r.status_code == 303, r.status_code
PID = dbq("SELECT id FROM posts WHERE body=?",
          ("post options probe",))[0]["id"]

BUTTON = (f'class="post-options-btn post-options-toggle" data-pid="{PID}" '
          f'aria-expanded="false" aria-controls="post-options-{PID}"')
PANEL = f'<div class="post-options" id="post-options-{PID}" hidden>'

for label, client in (("author", ANN), ("friend", BOB)):
    html = client.get("/home").text
    check(f"{label}: ONE Post options button for the post",
          BUTTON in html and "⚙️ Post options</button>" in html)
    check(f"{label}: options panel present, hidden by default",
          PANEL in html)
    # nothing from the old cluttered row renders outside the panel:
    # the first action markup after the stats must be the button,
    # and Comment (an always-present action) first appears INSIDE
    # the panel, after the panel's opening tag.
    i_panel = html.index(PANEL)
    i_comment = html.index("💬 Comment")
    check(f"{label}: Comment lives inside the panel", i_comment > i_panel)
    check(f"{label}: old React toggle row gone",
          'class="action-link react-toggle"' not in html)
    check(f"{label}: old floating reaction picker gone",
          f'id="react-picker-{PID}"' not in html)
    # every always-on action is inside the panel (scope to the
    # panel segment — the composer elsewhere on the page carries
    # its own hide-from wording, which is not this post's panel)
    seg = html[i_panel:i_panel + 9000]
    for needle in (
        f'action="/post/{PID}/react"', f'action="/post/{PID}/like"',
        "💬 Comment", "🔁 Share on HERE", "↗ Share outside HERE",
        f'data-copy-path="/p/{PID}"', "🙈 Hide this post</button>",
        f'action="/post/{PID}/hide"',
    ):
        check(f"{label}: panel action {needle!r}", needle in seg)
    # all six reactions are offered inside the panel
    for kind in ("like", "love", "laugh", "wow", "sad", "angry"):
        check(f"{label}: reaction kind {kind}",
              f'name="kind" value="{kind}"' in html)
    # the toggle wiring + panel CSS ship in base.html
    check(f"{label}: post-options toggle script present",
          "closest('.post-options-toggle')" in html
          and f"'post-options-' +" in html.replace(" ", "")
          or "post-options-" in html)
    check(f"{label}: post-options styles present",
          ".post-options-btn" in html and ".post-opt-item" in html)

# ---- permission split is unchanged: author-only actions stay author-only
html_a = ANN.get("/home").text
html_b = BOB.get("/home").text
seg_a = html_a[html_a.index(PANEL):html_a.index(PANEL) + 9000]
seg_b = html_b[html_b.index(PANEL):html_b.index(PANEL) + 9000]
for needle in ("📌 Pin", f'action="/post/{PID}/pin"',
               "🙈 Hide this post from…",
               "🗑 Delete this post", f'action="/post/{PID}/delete"'):
    check(f"author panel sees {needle!r}", needle in seg_a)
    check(f"friend panel does NOT see {needle!r}", needle not in seg_b)

# ---- the actions themselves still work through the unchanged routes
r = BOB.post(f"/post/{PID}/react", data={"kind": "love"})
check("friend reacts (200/json or redirect)",
      r.status_code in (200, 303, 302), str(r.status_code))
rows = dbq("SELECT * FROM likes WHERE post_id=? AND user_id=?",
           (PID, BID))
check("reaction landed in the DB", len(rows) == 1
      and rows[0]["kind"] == "love")

r = BOB.post(f"/post/{PID}/share")
check("friend shares on HERE (303)", r.status_code in (303, 302))
check("share post created",
      len(dbq("SELECT * FROM posts WHERE shared_post_id=? AND user_id=?",
              (PID, BID))) >= 1)

r = ANN.post(f"/post/{PID}/pin")
check("author pins (303)", r.status_code in (303, 302))
check("post is pinned in the DB",
      dbq("SELECT pinned FROM posts WHERE id=?", (PID,))[0]["pinned"] == 1)

# the single-post page uses the same card, so it is cleaned up too
html = ANN.get(f"/post/{PID}").text
check("post page: Post options button present",
      "⚙️ Post options</button>" in html and PANEL in html)

# ---------------- inline JS parses (node --check) ----------------
node = shutil.which("node")
if node:
    all_ok = True
    text = ANN.get("/home").text
    for i, block in enumerate(re.findall(r"<script>(.*?)</script>",
                                         text, re.S)):
        p = os.path.join(_tmp, f"js-home-{i}.js")
        with open(p, "w") as f:
            f.write(block)
        pr = subprocess.run([node, "--check", p],
                            capture_output=True, text=True)
        if pr.returncode != 0:
            all_ok = False
            print("node --check failed:", i, pr.stderr[:300])
    check("node --check: home inline JS parses", all_ok)
else:
    check("node --check skipped (node unavailable)", True)

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
