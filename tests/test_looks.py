"""Build LOOKS suite: the 🎨 My Look system — scheme picker
(glass / solid / mix), preset looks, surface + accent colors, the
readability guardrail, profile application, Steal This Look, and
the /welcome vibe picker.

- Storage: users.look_scheme (default 'glass'), look_surface,
  look_accent. Build CLEAR semantics: the transparency slider
  belongs to GLASS only (saved box_opacity drives Glass, 0..100,
  0 = completely clear); solid -> alpha "1" everywhere; mix ->
  solid cards (--glass-alpha 1) with the small surfaces glass at
  the saved solidity (--glass-alpha-soft). Saving via the legacy
  /settings/boxes route is the Glass door: it lands in 'glass'.
- Emitted per viewer on <body>: --glass-alpha, --glass-rgb,
  --look-accent (+ look-on-dark / look-on-light from the surface
  luminance). The HERE logo stays on --red, never the accent.
- Profiles: the .spot-scope carries the OWNER's look vars; a
  visitor's chrome keeps the visitor's own.
- Steal copies scheme/colors/spot colors — never the spot name or
  the background photo — and notifies the owner.

Run:  ./venv/bin/python tests/test_looks.py
"""
import os, re, shutil, sqlite3, subprocess, sys, tempfile
from urllib.parse import unquote

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-looks-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)
import app as here
from fastapi.testclient import TestClient

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


def user_row(uid):
    return dict(dbq("SELECT * FROM users WHERE id=?", (uid,))[0])


def signup(name, email):
    c = TestClient(here.app, follow_redirects=False)
    r = c.post("/signup", data={"name": name, "email": email,
                                "password": "pw123456"})
    assert r.status_code == 303, (name, r.status_code)
    uid = dbq("SELECT id FROM users WHERE email=?", (email,))[0]["id"]
    return c, uid


def body_tag(html):
    m = re.search(r"<body class=[^>]*>", html)
    return m.group(0) if m else ""


def scope_tag(html):
    m = re.search(r"<div class=\"spot-scope[^>]*>", html)
    return m.group(0) if m else ""


def node_check(html, label):
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    ok = True
    for i, s in enumerate(scripts):
        p = os.path.join(_tmp, f"{label}{i}.js")
        with open(p, "w") as fh:
            fh.write(s)
        r = subprocess.run(["node", "--check", p],
                           capture_output=True, text=True)
        if r.returncode != 0:
            ok = False
            print("  node error:", r.stderr[:300])
    return ok


FRESH, FRESH_ID = signup("Look Fresh", "fresh@looks.io")
OWNER, OWNER_ID = signup("Look Owner", "owner@looks.io")
VIS, VIS_ID = signup("Look Visitor", "vis@looks.io")

# ---------------- defaults: glass for everybody -------------------
row = user_row(FRESH_ID)
check("D: fresh user stores look_scheme='glass', no surface/accent",
      row["look_scheme"] == "glass" and row["look_surface"] is None
      and row["look_accent"] is None)
check("D: helpers — glass scheme, default rgb + HERE red accent",
      here.look_scheme_for(row) == "glass"
      and here.look_rgb_str_for(row) == "255,255,255"
      and here.look_accent_for(row) == "#d41f2e"
      and here.glass_alpha_for(row) == "0.82"
      and here.glass_alpha_soft_for(row) == "0.82")
check("D: no surface -> no guardrail class",
      here.look_scope_class_for(row) == "")
home = FRESH.get("/home").text
bt = body_tag(home)
check("D: fresh body carries the full default look var set",
      "--glass-alpha:0.82" in bt and "--glass-alpha-soft:0.82" in bt
      and "--glass-rgb:255,255,255" in bt
      and "--look-accent:#d41f2e" in bt)
check("D: fresh body has NO look-on-dark / look-on-light class",
      "look-on-dark" not in bt and "look-on-light" not in bt)

# ---------------- the Settings card -------------------------------
st = FRESH.get("/settings/profile").text
check("S: settings shows the 🎨 My Look card", "🎨 My Look" in st)
check("S: three schemes offered with their labels",
      "🪟 Glass" in st and "🧱 Solid" in st and "🎛 Mix" in st
      and 'name="scheme"' in st)
check("S: surface + accent color inputs present",
      'name="surface"' in st and 'name="accent"' in st
      and 'id="look-surface"' in st and 'id="look-accent-in"' in st)
check("S: live preview card present", 'id="look-preview-card"' in st)
check("S: the slider row belongs to Glass only (CLEAR gate)",
      'id="look-glass-row"' in st and 'id="look-mix-row"' not in st
      and "sc !== 'glass'" in st and 'min="0"' in st
      and "Completely clear" in st)
check("S: Mix explains itself — cards solid, bubbles glass",
      "Cards solid · bubbles glass — bubbles use your Glass "
      "transparency (switch to Glass to change it)." in st)
check("S: all six preset cards render in Settings",
      all(n in st for n in
          ("Classic HERE Glass", "Midnight Gold", "Ice Glass",
           "Royal Purple", "Money Green", "Rose Glow")))
check("S: settings page inline JS parses", node_check(st, "lookset"))

# ---------------- solid round-trip + guardrail --------------------
r = FRESH.post("/settings/look", data={
    "scheme": "solid", "surface": "#101010", "accent": "#d4af37"})
check("S: POST /settings/look -> 303", r.status_code == 303)
row = user_row(FRESH_ID)
check("S: solid + colors persist exactly",
      row["look_scheme"] == "solid" and row["look_surface"] == "#101010"
      and row["look_accent"] == "#d4af37")
bt = body_tag(FRESH.get("/home").text)
check("S: solid body — alpha 1, surface rgb, gold accent, on-dark",
      "--glass-alpha:1" in bt and "--glass-alpha-soft:1" in bt
      and "--glass-rgb:16,16,16" in bt
      and "--look-accent:#d4af37" in bt and "look-on-dark" in bt)
check("S: luminance verdicts — #101010 dark, #cfe8ff light",
      here._hex_luminance((16, 16, 16)) < 0.5
      and here._hex_luminance((207, 232, 255)) >= 0.5)
check("S: logo stays HERE red under a gold accent",
      ".logo{" in home and "color:var(--red)" in home
      and "--red:#d41f2e" in home)

FRESH.post("/settings/look", data={
    "scheme": "solid", "surface": "#cfe8ff", "accent": "#38bdf8"})
bt = body_tag(FRESH.get("/home").text)
check("S: light surface -> look-on-light + its rgb",
      "look-on-light" in bt and "--glass-rgb:207,232,255" in bt
      and "look-on-dark" not in bt)

r = FRESH.post("/settings/look", data={
    "scheme": "solid", "surface": "red", "accent": "#zzzzzz"})
row = user_row(FRESH_ID)
check("S: bad hex is safely stored as unset (no crash, no junk)",
      r.status_code == 303 and row["look_surface"] is None
      and row["look_accent"] is None)
bt = body_tag(FRESH.get("/home").text)
check("S: unset again -> default rgb/accent, no guardrail class",
      "--glass-rgb:255,255,255" in bt and "look-on-dark" not in bt
      and "look-on-light" not in bt)

# ---------------- CLEAR: boxes door -> Glass; Mix split ----------
FRESH.post("/settings/boxes", data={"box_opacity": "55"})
row = user_row(FRESH_ID)
check("M: saving via /settings/boxes lands in GLASS (its door)",
      row["look_scheme"] == "glass" and row["box_opacity"] == 55
      and row["glass_set"] == 1)
bt = body_tag(FRESH.get("/home").text)
check("M: glass body — slider alpha on BOTH vars + legacy layer",
      "--glass-alpha:0.55" in bt and "--glass-alpha-soft:0.55" in bt
      and "--box-alpha:0.55" in bt and "boxes-glass" in bt)
FRESH.post("/settings/look", data={"scheme": "mix",
                                   "box_opacity": "70"})
row = user_row(FRESH_ID)
bt = body_tag(FRESH.get("/home").text)
check("M: Mix — form slider ignored outside Glass; split alphas",
      row["look_scheme"] == "mix" and row["box_opacity"] == 55
      and "--glass-alpha:1" in bt and "--glass-alpha-soft:0.55" in bt
      and "--box-alpha" not in bt and "boxes-glass" not in bt)
FRESH.post("/settings/look", data={"scheme": "glass",
                                   "box_opacity": "70"})
row = user_row(FRESH_ID)
bt = body_tag(FRESH.get("/home").text)
check("M: /settings/look Glass saves the solidity atomically",
      row["look_scheme"] == "glass" and row["box_opacity"] == 70
      and "--glass-alpha:0.7" in bt
      and "--glass-alpha-soft:0.7" in bt
      and "--box-alpha:0.7" in bt and "boxes-glass" in bt)
FRESH.post("/settings/look", data={"scheme": "solid"})
bt = body_tag(FRESH.get("/home").text)
check("M: Solid ignores the stored slider entirely (both vars 1)",
      "--glass-alpha:1" in bt and "--glass-alpha-soft:1" in bt
      and "--box-alpha" not in bt)
r = FRESH.post("/settings/look", data={"reset": "1"})
row = user_row(FRESH_ID)
check("M: Reset-to-classic restores glass + unset colors",
      r.status_code == 303 and row["look_scheme"] == "glass"
      and row["look_surface"] is None and row["look_accent"] is None)

# ---------------- presets: exact table + route --------------------
EXPECTED = {
    "classic": ("glass", None, None, None),
    "midnight_gold": ("solid", "#101010", "#d4af37", None),
    "ice": ("glass", "#cfe8ff", "#38bdf8", None),
    "royal": ("mix", "#2a1450", "#a78bfa", 70),
    "money": ("solid", "#0d2818", "#22c55e", None),
    "rose": ("glass", "#3d1220", "#fb7185", None),
}
for key, (scheme, surface, accent, op) in EXPECTED.items():
    p = here.LOOK_PRESETS[key]
    check(f"P: preset table — {key}",
          p["scheme"] == scheme and p.get("surface") == surface
          and p.get("accent") == accent
          and p.get("box_opacity") == op)
for key, (scheme, surface, accent, op) in EXPECTED.items():
    r = VIS.post("/settings/look", data={"preset": key})
    row = user_row(VIS_ID)
    ok = (r.status_code == 303 and row["look_scheme"] == scheme
          and row["look_surface"] == surface
          and row["look_accent"] == accent
          and (op is None or row["box_opacity"] == op))
    check(f"P: one tap applies {key} exactly", ok,
          f"{row['look_scheme']}/{row['look_surface']}/"
          f"{row['look_accent']}/{row['box_opacity']}")
    check(f"P: flash names the applied look ({key})",
          "Look applied:" in unquote(r.headers.get("location", ""))
          and here.LOOK_PRESETS[key]["name"]
          in unquote(r.headers.get("location", "")))
row = user_row(VIS_ID)  # last applied = rose; midnight spot colors:
VIS.post("/settings/look", data={"preset": "midnight_gold"})
row = user_row(VIS_ID)
check("P: Midnight Gold also brings its Spot colors (Ken's look)",
      row["spot_text_color"] == "#ffd700"
      and row["spot_card_color"] == "#101010")

# ---------------- profile application ----------------------------
OWNER.post("/settings/look", data={
    "scheme": "solid", "surface": "#101010", "accent": "#fb7185"})
OWNER.post("/settings/spot", data={
    "spot_name": "Owner Pad", "spot_text_color": "#ffd700",
    "spot_card_color": "#101010"})
prof = VIS.get(f"/user/{OWNER_ID}").text
sc = scope_tag(prof)
check("V: profile scope carries the OWNER's look vars",
      "--glass-alpha:1" in sc and "--glass-rgb:16,16,16" in sc
      and "--look-accent:#fb7185" in sc and "look-on-dark" in sc, sc)
bt = body_tag(prof)
# VIS currently wears Midnight Gold (solid #101010, accent #d4af37):
# same surface as the owner, DIFFERENT accent — the accent is what
# separates whose look the chrome is wearing.
check("V: the visitor's chrome keeps the VISITOR's own look",
      "--glass-alpha:1" in bt and "--glass-rgb:16,16,16" in bt
      and "--look-accent:#d4af37" in bt
      and "--look-accent:#fb7185" not in bt, bt)
VIS.post("/settings/look", data={"preset": "classic"})
VIS.post("/settings/spot", data={"reset": "1"})
prof = VIS.get(f"/user/{OWNER_ID}").text
sc, bt = scope_tag(prof), body_tag(prof)
check("V: reset visitor — scope still owner's, chrome back to glass",
      # Build CLEAR: Glass honors the visitor's SAVED solidity —
      # VIS chose 70 back in the preset loop, so their chrome is
      # glass at 0.7 (default rgb), not the unsaved 0.82 default.
      "--glass-rgb:16,16,16" in sc and "--glass-alpha:0.7" in bt
      and "--glass-alpha-soft:0.7" in bt
      and "--glass-rgb:255,255,255" in bt)
own = OWNER.get(f"/user/{OWNER_ID}").text
check("V: owner's own profile shows their look in the scope too",
      "--glass-rgb:16,16,16" in scope_tag(own))

ANON = TestClient(here.app, follow_redirects=False)
pub = ANON.get(f"/u/{OWNER_ID}").text
check("V: logged-out /u wears the owner's look on the public card",
      "--glass-rgb:16,16,16" in pub and "look-on-dark" in pub)

# ---------------- Steal This Look ---------------------------------
check("T: another user's profile offers Steal this look",
      "Steal this look" in prof
      and f"/user/{OWNER_ID}/steal-look" in prof)
check("T: own profile never offers it", "Steal this look" not in own)
before = user_row(VIS_ID)
r = VIS.post(f"/user/{OWNER_ID}/steal-look")
after = user_row(VIS_ID)
check("T: steal redirects back to the profile with the exact flash",
      r.status_code == 303
      and f"Look stolen from Look Owner" in unquote(
          r.headers.get("location", ""))
      and "their background photo stays theirs"
      in unquote(r.headers.get("location", "")))
check("T: steal copies scheme + surface + accent + spot colors",
      after["look_scheme"] == "solid"
      and after["look_surface"] == "#101010"
      and after["look_accent"] == "#fb7185"
      and after["spot_text_color"] == "#ffd700"
      and after["spot_card_color"] == "#101010")
check("T: steal does NOT copy the spot name or background photo",
      not after["spot_name"] and not after["background"])
notes = dbq("SELECT * FROM notifications WHERE user_id=? "
            "AND kind='look_steal'", (OWNER_ID,))
check("T: the owner is notified (kind look_steal, link = thief)",
      len(notes) == 1 and "stole your look" in notes[0]["text"]
      and notes[0]["link"] == f"/user/{VIS_ID}")

# private profile: no button, no steal — but a friend may
PRIV, PRIV_ID = signup("Look Private", "priv@looks.io")
PRIV.post("/settings/privacy", data={"private_profile": "1"})
PRIV.post("/settings/look", data={
    "scheme": "solid", "surface": "#0d2818", "accent": "#22c55e"})
STR, STR_ID = signup("Look Stranger", "str@looks.io")
page = STR.get(f"/user/{PRIV_ID}").text
check("T: locked private profile shows no steal button",
      "Steal this look" not in page)
STR.post(f"/user/{PRIV_ID}/steal-look")
check("T: stranger's steal from a private profile is refused",
      user_row(STR_ID)["look_scheme"] == "glass"
      and user_row(STR_ID)["look_surface"] is None)
STR.post(f"/friends/request/{PRIV_ID}")
PRIV.post(f"/friends/accept/{STR_ID}")
STR.post(f"/user/{PRIV_ID}/steal-look")
check("T: a friend CAN steal from a private profile",
      user_row(STR_ID)["look_surface"] == "#0d2818")

# blocked pair: the steal route is as unavailable as the profile
BLK, BLK_ID = signup("Look Blocked", "blk@looks.io")
OWNER.post(f"/block/{BLK_ID}")
BLK.post(f"/user/{OWNER_ID}/steal-look")
check("T: a blocked pair cannot steal (look untouched)",
      user_row(BLK_ID)["look_scheme"] == "glass"
      and user_row(BLK_ID)["look_accent"] is None)

# ---------------- welcome vibe picker ------------------------------
NEWB, NEWB_ID = signup("Look Newbie", "newb@looks.io")
wel = NEWB.get("/welcome").text
check("W: /welcome has the 🎨 Pick your vibe step",
      "Pick your vibe" in wel and "Keep" in wel)
check("W: all six preset cards render on /welcome",
      all(n in wel for n in
          ("Classic HERE Glass", "Midnight Gold", "Ice Glass",
           "Royal Purple", "Money Green", "Rose Glow")))
check("W: welcome inline JS parses", node_check(wel, "lookwel"))
check("W: skipping keeps the glass default",
      user_row(NEWB_ID)["look_scheme"] == "glass")
r = NEWB.post("/welcome/look", data={"preset": "money"})
row = user_row(NEWB_ID)
check("W: tapping a vibe applies it to the new user at once",
      r.status_code == 303 and row["look_scheme"] == "solid"
      and row["look_surface"] == "#0d2818"
      and row["look_accent"] == "#22c55e")
NEWB.post("/welcome/look", data={"preset": "nope"})
check("W: an unknown vibe changes nothing",
      user_row(NEWB_ID)["look_scheme"] == "solid")

print()
failed = [n for n, ok in RESULTS if not ok]
print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
if failed:
    print("FAILED:", *failed, sep="\n  ")
    sys.exit(1)
