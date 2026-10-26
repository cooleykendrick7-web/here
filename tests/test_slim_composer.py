"""Build SLIM — the "Say something" composer bar.

Run:  ./venv/bin/python tests/test_slim_composer.py

The always-open composer block now rests as ONE slim glass bar
(✍️ + "Say something…"); tapping it opens the FULL existing
composer right there (same form, same route, same fields), and
posting or the ✕ folds it back. Covers: the bar + collapsed-by-
default body on home and on a page, the open/close + hash
auto-open wiring, every composer control still present inside
the collapsed block, and full posts (text + poll) landing through
the unchanged routes from both surfaces.
"""
import os, re, shutil, sqlite3, subprocess, sys, tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-slim-")
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


ANN, AID = signup("Ann Author", "ann@slim.io")
BOB, BID = signup("Bob Buddy", "bob@slim.io")
befriend(ANN, AID, BOB, BID)   # a friend makes Hide from… render

SLIM_NEEDLES = [
    'id="slim-composer"', 'id="slim-bar"', "✍️", "Say something…",
    '<div class="slim-body" id="slim-body" hidden>', 'id="slim-close"',
    'id="kind-tabs"', 'data-kind="poll"', 'id="composer-body"',
    'id="tags-json"', 'id="tag-suggest"', 'name="privacy"',
]
for label, path in (("home", "/home"),):
    html = ANN.get(path).text
    for n in SLIM_NEEDLES:
        check(f"{label}: slim needle {n!r}", n in html)
    # collapsed by default: the bar comes BEFORE the body, the form
    # lives INSIDE the hidden body, and the bar itself is not hidden.
    i_bar = html.index('id="slim-bar"')
    i_body = html.index('id="slim-body"')
    i_form = html.index('<form action="/posts"')
    check(f"{label}: bar first, form inside the collapsed body",
          i_bar < i_body < i_form)
    check(f"{label}: bar carries aria-expanded=false (resting state)",
          'id="slim-bar" aria-expanded="false"' in html)
    # every composer control survives inside the collapsed block
    for n in ('id="fld-feeling"', 'id="fld-media"', 'id="fld-poll"',
              'id="wall-music-toggle"', 'id="composer-aud-toggle"',
              'name="hide_from"', '>Post</button>'):
        check(f"{label}: composer control {n!r} intact", n in html)
    # the shared open/close wiring ships with the page (base.html)
    check(f"{label}: slim open/close script present",
          "getElementById('slim-bar')" in html
          and "getElementById('slim-body')" in html)
    check(f"{label}: #composer-body deep link auto-opens the composer",
          "location.hash === '#composer-body'" in html)

# ---------------- posting still works from home ----------------
r = ANN.post("/posts", data={"kind": "text",
                             "body": "slim says hello",
                             "privacy": "public"})
check("home text post lands (303)", r.status_code == 303)
rows = dbq("SELECT * FROM posts WHERE user_id=? AND body=?",
           (AID, "slim says hello"))
check("home text post in the DB", len(rows) == 1)
html = ANN.get("/home").text
check("posted text renders on /home", "slim says hello" in html)
check("composer rests collapsed again after posting",
      '<div class="slim-body" id="slim-body" hidden>' in html)

r = ANN.post("/posts", data={"kind": "poll", "body": "Best day?",
                             "option1": "Friday", "option2": "Saturday",
                             "privacy": "public"})
check("home poll post lands (303)", r.status_code == 303)
prow = dbq("SELECT * FROM posts WHERE user_id=? AND body=?",
           (AID, "Best day?"))
check("poll post + poll rows created",
      len(prow) == 1 and len(dbq(
          "SELECT * FROM poll_options WHERE post_id=?",
          (prow[0]["id"],))) == 2)

# ---------------- the page surface ----------------
r = ANN.post("/pages", data={"name": "Slim Page", "ptype": "community",
                             "description": "for slim tests"})
check("page created (303)", r.status_code == 303)
PGID = dbq("SELECT id FROM pages WHERE name=?", ("Slim Page",))[0]["id"]
html = ANN.get(f"/page/{PGID}").text
# the page composer never carried a privacy select (page posts are
# always public) — every other slim needle applies verbatim.
for n in [x for x in SLIM_NEEDLES if x != 'name="privacy"']:
    check(f"page: slim needle {n!r}", n in html)
check("page: no privacy select (page posts public, as always)",
      'name="privacy"' not in html)
check("page: form still posts to the page route",
      f'<form action="/page/{PGID}/post"' in html)
check("page: textarea keeps the page placeholder",
      "Post to Slim Page…" in html)
r = ANN.post(f"/page/{PGID}/post",
             data={"kind": "text", "body": "page post via slim"})
check("page post lands (303)", r.status_code == 303)
rows = dbq("SELECT * FROM posts WHERE page_id=? AND body=?",
           (PGID, "page post via slim"))
check("page post in the DB under the page", len(rows) == 1)
html = ANN.get(f"/page/{PGID}").text
check("page post renders on the page", "page post via slim" in html)

# ---------------- inline JS parses (node --check) ----------------
node = shutil.which("node")
if node:
    all_ok = True
    for label, path in (("home", "/home"), ("page", f"/page/{PGID}")):
        text = ANN.get(path).text
        for i, block in enumerate(
                re.findall(r"<script>(.*?)</script>", text, re.S)):
            p = os.path.join(_tmp, f"js-{label}-{i}.js")
            with open(p, "w") as f:
                f.write(block)
            pr = subprocess.run([node, "--check", p],
                                capture_output=True, text=True)
            if pr.returncode != 0:
                all_ok = False
                print("node --check failed:", label, i,
                      pr.stderr[:300])
    check("node --check: home + page inline JS parses", all_ok)
else:
    check("node --check skipped (node unavailable)", True)

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
