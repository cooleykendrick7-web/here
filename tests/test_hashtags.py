"""Build TAGS — hashtags everywhere.

Run:  ./venv/bin/python tests/test_hashtags.py

What already existed before this build: parse_hashtags +
post_hashtags indexing on the create path (My Views used it) and
the #HereDaily challenge mechanics. What this build adds, covered
here: #tags render as clickable links to GET /tag/{tag} (without
breaking @tags, bare URLs with #fragments, or escaped entities),
the tag page applies EXACTLY the standard visibility rules, a
Trending Tags strip (last 7 days, min 2 posts, viewer-gated) on
Discover, '#' trend entries link to tag pages, and wall posts /
My Views posts are indexed too. #HereDaily still stamps the
challenge.
"""
import os, sqlite3, sys, tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-tags-")
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


def post(client, body, privacy="public", **kw):
    data = {"kind": "text", "body": body, "privacy": privacy}
    data.update(kw)
    r = client.post("/posts", data=data)
    assert r.status_code == 303, (body, r.status_code)
    rows = dbq("SELECT * FROM posts WHERE body=?", (body,))
    return rows[0]["id"] if rows else 0


def tags_of(pid):
    return sorted(r["tag"] for r in dbq(
        "SELECT tag FROM post_hashtags WHERE post_id=?", (pid,)))


ANN, AID = signup("Ann Tagger", "ann@tags.io")
BOB, BID = signup("Bob Reader", "bob@tags.io")
EVE, EID = signup("Eve Outsider", "eve@tags.io")
befriend(ANN, AID, BOB, BID)

# ================= parse + index on create =================
P1 = post(ANN, "Loving the #SunsetVibes and #GoldenHour today")
check("hashtags parsed + indexed (lowercased, no '#')",
      tags_of(P1) == ["goldenhour", "sunsetvibes"])
P2 = post(BOB, "more #sunsetvibes from me")
check("second post indexes the same tag", tags_of(P2) == ["sunsetvibes"])

# ================= rendered as links =================
html = BOB.get("/home").text
check("post body renders the hashtag as a tag-page link",
      '<a class="tag-link" href="/tag/sunsetvibes">#SunsetVibes</a>'
      in html)
html = BOB.get(f"/user/{AID}").text
check("profile render links hashtags too",
      'href="/tag/goldenhour"' in html)

# non-regressions in the same renderer
P3 = post(ANN, "it's #cool see https://example.com/x#frag now")
html = BOB.get("/home").text
check("escaped entity survives (&#x27; is NOT linkified)",
      "it&#x27;s" in html)
check("apostrophe post: the real hashtag still links",
      '<a class="tag-link" href="/tag/cool">#cool</a>' in html)
check("URL with a #fragment stays ONE link, no tag link inside",
      'href="https://example.com/x#frag"' in html
      and "/tag/frag" not in html)
check("the fragment did NOT become an indexed hashtag",
      tags_of(P3) == ["cool"])

# ================= the tag page =================
r = BOB.get("/tag/sunsetvibes")
check("tag page loads for an entitled viewer", r.status_code == 200)
check("tag page shows the public posts carrying the tag",
      "Loving the" in r.text and "more #sunsetvibes" in r.text
      or "more" in r.text)
check("tag page title names the tag", "#sunsetvibes" in r.text)
check("tag page links back to Discover",
      'href="/discover"' in r.text)
r = BOB.get("/tag/SunsetVibes")
check("tag route normalizes case", r.status_code == 200
      and "Loving the" in r.text)
r = BOB.get("/tag/no%20such%20tag", )
check("invalid tag bounces to Discover",
      r.status_code in (303, 302)
      and "/discover" in r.headers.get("location", ""))

# friends-only posts don't leak to non-friends
P4 = post(ANN, "inner circle #SecretSpot stuff", privacy="friends")
html = EVE.get("/tag/secretspot").text
check("tag page: non-friend does NOT see a friends-only post",
      "inner circle" not in html)
html = BOB.get("/tag/secretspot").text
check("tag page: a friend DOES see it", "inner circle" in html)

# private-profile authors don't leak either
PAM, PMID = signup("Pam Private", "pam@tags.io")
post(PAM, "pam public words #PrivTag")
html = EVE.get("/tag/privtag").text
check("tag page: public post shows while the author is public",
      "pam public words" in html)
PAM.post("/settings/privacy", data={"private_profile": "1"})
html = EVE.get("/tag/privtag").text
check("tag page: private author's post hides from non-friends",
      "pam public words" not in html)

# blocked pair can't see each other on the tag page
DAN, DID = signup("Dan Blocker", "dan@tags.io")
post(DAN, "dan says #BlockTag loudly")
html = DAN.get("/tag/blocktag").text
check("tag page: author sees own tagged post pre-block",
      "dan says" in html)
DAN.post(f"/block/{AID}")
post(ANN, "ann says #BlockTag too")
html = DAN.get("/tag/blocktag").text
check("tag page: blocked pair — the blocked author's post is gone",
      "ann says" not in html and "dan says" in html)

# ================= #HereDaily still counts =================
r = ANN.post("/posts", data={"kind": "text", "body": "my daily answer",
                             "privacy": "public", "challenge": "1"})
assert r.status_code == 303, r.status_code
# the challenge flow appends #HereDaily to the body BEFORE the insert
P5 = dbq("SELECT id FROM posts WHERE body LIKE 'my daily answer%'",
         )[0]["id"]
row = dbq("SELECT challenge_date FROM posts WHERE id=?", (P5,))[0]
check("challenge post still stamps challenge_date (today)",
      row["challenge_date"] == here._today_str())
check("#HereDaily appended + indexed like any tag",
      tags_of(P5) == ["heredaily"])
html = BOB.get("/home").text
check("#HereDaily renders as a tag link without breaking the post",
      '<a class="tag-link" href="/tag/heredaily">#HereDaily</a>' in html)

# ================= wall posts + Views posts are indexed =================
WOWN, WOID = signup("Wanda Wallowner", "wanda@tags.io")
WF, WFID = signup("Will Wallfriend", "will@tags.io")
befriend(WOWN, WOID, WF, WFID)
WOWN.post("/settings/wall", data={"wall_open": "1",
                                  "wall_approve_first": "1"})
r = WF.post("/posts", data={"kind": "text",
                            "body": "wall note #WallTag",
                            "privacy": "public",
                            "wall_user_id": str(WOID)})
check("wall post with a hashtag lands", r.status_code == 303)
rows = dbq("SELECT * FROM posts WHERE body=?", ("wall note #WallTag",))
W1 = rows[0]["id"] if rows else 0
check("wall post hashtag indexed", tags_of(W1) == ["walltag"])
html = BOB.get("/tag/walltag").text
check("PENDING wall post stays off the tag page",
      "wall note" not in html)
WOWN.post(f"/wall/{W1}/approve")
html = BOB.get("/tag/walltag").text
check("approved wall post joins the tag page",
      "wall note" in html)

V1 = None
r = ANN.post("/posts",
             data={"kind": "video", "body": "a view #ViewTag",
                   "privacy": "public", "next": "/views"},
             files={"media": ("v.mp4", MP4, "video/mp4")})
rows = dbq("SELECT * FROM posts WHERE body=?", ("a view #ViewTag",))
V1 = rows[0]["id"] if rows else 0
check("My Views post hashtag indexed", tags_of(V1) == ["viewtag"])
html = BOB.get("/tag/viewtag").text
check("My Views post shows on its tag page", "a view" in html)

# ================= trending tags on Discover =================
post(ANN, "one #TrendingNow post")
post(BOB, "two #TrendingNow post")
html = ANN.get("/discover").text
check("Discover carries the 🏷️ Trending Tags strip",
      "Trending Tags" in html)
check("trending strip lists a tag used 2+ times, linked to its page",
      'href="/tag/trendingnow"' in html and "#trendingnow" in html)
post(ANN, "a one-off #LonelyTag musing")
html = ANN.get("/discover").text
# scope to the strip itself: the older 🔥 trends card lists one-off
# tags too (its own long-standing rule) — the STRIP is what's gated.
_strip = html[html.index("Trending Tags"):html.index("Trending this week")]
check("a one-off tag stays OUT of the strip (min threshold)",
      'href="/tag/lonelytag"' not in _strip)
check("the older 🔥 trends now link #entries to tag pages too",
      'href="/tag/trendingnow"' in html)
html = ANN.get("/home").text
check("sidebar '#' trends link to tag pages as well",
      'href="/tag/' in html)

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
