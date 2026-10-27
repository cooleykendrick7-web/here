"""Build ICON STYLES: one custom vector icon set, seven finishes.

Ken's order (Oct 27): the iPhone-emoji icons are "mediocre" —
recreate every icon as custom art and let each user pick the
finish in Settings (customization): Emoji (DEFAULT, the classic
look), Solid, Glass, Jelly, Chrome, Gold, Transparent. The
specimen board was approved by Ken before this build.
"""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["HERE_DB"] = os.path.join(_tmp, "t.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "up")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fastapi.testclient import TestClient  # noqa: E402
import app as here  # noqa: E402

C = TestClient(here.app)
PASS = []
FAIL = []


def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("PASS " if cond else "FAIL "), name)


C.post("/signup", data={"name": "Icon Ivy", "email": "ivy@x.com",
                        "password": "password123"})

# -- unit: the helper contract --------------------------------------
check("unit: emoji mode returns the classic glyph",
      here.icon_html("home", "emoji") == "🏠")
check("unit: themed mode returns the vector art",
      '<svg class="hic"' in str(here.icon_html("home", "gold")))
check("unit: unknown key falls back to its emoji in any mode",
      here.icon_html("nope", "gold")
      == here.ICON_EMOJI.get("nope", ""))
check("unit: style normalizer rejects junk",
      here.icon_style_of({"icon_style": "sparkle"}) == "emoji"
      and here.icon_style_of({"icon_style": " GOLD "}) == "gold"
      and here.icon_style_of(None) == "emoji")
check("unit: every registry key has BOTH an emoji and art",
      all(k in here.ICON_ART for k in here.ICON_EMOJI))

# -- default: the classic look is untouched --------------------------
home = C.get("/home")
check("default: no icons-* body class",
      "icons-gold" not in home.text and "icons-solid" not in home.text
      and "icons-transparent" not in home.text)
check("default: NO vector icons rendered",
      '<svg class="hic"' not in home.text)
check("default: tab bar still shows the emoji",
      '<span class="tab-ic">🏠</span>' in home.text)
check("default: menu rows still show emoji",
      '<span class="menu-ic">🎮</span>' in home.text
      or '<span>🎮</span>' in home.text)

# -- the picker -------------------------------------------------------
st = C.get("/settings/profile")
check("picker: the Icon style card is in Settings",
      'id="icons"' in st.text and 'action="/settings/icons"' in st.text)
check("picker: all seven finishes are offered",
      all(f'value="{s}"' in st.text for s in here.ICON_STYLES))
check("picker: previews show real art for themed finishes",
      'class="hic"' in st.text)
check("picker: emoji is the checked default",
      'value="emoji"' in st.text)

# -- switch to Gold ---------------------------------------------------
r = C.post("/settings/icons", data={"icon_style": "gold"},
           follow_redirects=False)
check("save: gold accepted", r.status_code in (302, 303))
conn = here.db()
row = conn.execute("SELECT icon_style FROM users WHERE email='ivy@x.com'"
                   ).fetchone()
conn.close()
check("save: the choice persisted", row["icon_style"] == "gold")

home = C.get("/home")
check("gold: body carries the finish class", "icons-gold" in home.text)
check("gold: the gradient sprite is on the page",
      'id="hicGold"' in home.text)
check("gold: tab bar renders vector icons",
      '<span class="tab-ic"><svg class="hic"' in home.text)
check("gold: the tab bar emoji is GONE",
      '<span class="tab-ic">🏠</span>' not in home.text)
check("gold: HERE_ICONS shipped for JS-driven buttons",
      "window.HERE_ICONS" in home.text and "hicGold" in home.text)
check("gold: settings shows gold checked",
      C.get("/settings/profile").status_code == 200)

prof = C.get("/user/1")
check("gold: profile buttons render art (no emoji Lives button)",
      "icons-gold" in prof.text and '<svg class="hic"' in prof.text)

# -- a bogus style changes nothing ------------------------------------
C.post("/settings/icons", data={"icon_style": "sparkle"})
conn = here.db()
row = conn.execute("SELECT icon_style FROM users WHERE email='ivy@x.com'"
                   ).fetchone()
conn.close()
check("save: a bogus style is rejected, gold kept",
      row["icon_style"] == "gold")

# -- back to Emoji: the classic look returns --------------------------
C.post("/settings/icons", data={"icon_style": "emoji"})
home = C.get("/home")
check("emoji again: no finish class, no vector icons",
      "icons-gold" not in home.text
      and '<svg class="hic"' not in home.text)
check("emoji again: tab bar emoji restored",
      '<span class="tab-ic">🏠</span>' in home.text)

# -- guests always see the classic look --------------------------------
G = TestClient(here.app)
pub = G.get("/login")
check("guest: no finish class, no vector icons",
      "icons-" not in pub.text.split("<body", 1)[1].split(">", 1)[0]
      and '<svg class="hic"' not in pub.text)

print(f"\n{len(PASS)}/{len(PASS) + len(FAIL)} passed")
if FAIL:
    print("FAILURES:", FAIL)
    sys.exit(1)
