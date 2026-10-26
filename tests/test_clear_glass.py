"""Build CLEAR suite: transparency belongs to Glass mode only.

- The 🎨 My Look slider renders under 🪟 Glass alone (Solid/Mix
  hide it); POST /settings/boxes is the Glass door — saving there
  lands the saver in scheme 'glass'.
- The solidity range is the FULL 0..100: 0 = completely clear
  (--glass-alpha:0, borders + large-surface blur remain), and
  out-of-range posts clamp to 0 / 100.
- Glass unsaved still defaults to 0.82 on both alpha vars.
- MIX REDEFINED: cards solid (--glass-alpha:1 in the viewer's
  surface color) while the small surfaces (bubbles, chips, nav
  pills) stay glass at the saved Glass solidity via the new
  --glass-alpha-soft token. The legacy boxes-glass / --box-alpha
  layer is emitted for Glass (<100) and NOT for Mix.
- Solid: both vars "1".

Run:  ./venv/bin/python tests/test_clear_glass.py
"""
import os, re, sqlite3, sys, tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-clear-")
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


A, AID = signup("Clear A", "a@clear.io")
B, BID = signup("Clear B", "b@clear.io")

# ---------------- defaults + the Glass-only slider ----------------
row = user_row(AID)
check("D: fresh user — glass scheme, both alphas at the 0.82 default",
      here.look_scheme_for(row) == "glass"
      and here.glass_alpha_for(row) == "0.82"
      and here.glass_alpha_soft_for(row) == "0.82")
bt = body_tag(A.get("/home").text)
check("D: fresh body emits --glass-alpha AND --glass-alpha-soft",
      "--glass-alpha:0.82" in bt and "--glass-alpha-soft:0.82" in bt)
st = A.get("/settings/profile").text
check("S: slider row is the Glass row, ends labeled for full range",
      'id="look-glass-row"' in st and 'id="look-mix-row"' not in st
      and "Completely clear" in st and "More solid" in st
      and "Box Transparency" in st)
check("S: slider JS gate shows it for Glass only",
      "glassRow.hidden = sc !== 'glass'" in st)
check("S: slider input spans the full 0..100 range",
      'id="box-opacity"' in st and 'min="0"' in st
      and 'max="100"' in st)
check("S: Mix carries its one-line explainer",
      "Cards solid · bubbles glass — bubbles use your Glass "
      "transparency (switch to Glass to change it)." in st)

# ---------------- clamps: the floor is 0 now ----------------------
r = A.post("/settings/boxes", data={"box_opacity": "-5"})
row = user_row(AID)
check("C: posting -5 clamps to 0 (and lands in Glass)",
      r.status_code == 303 and row["box_opacity"] == 0
      and row["look_scheme"] == "glass" and row["glass_set"] == 1)
bt = body_tag(A.get("/home").text)
check("C: 0 = completely clear — both alphas emit 0, legacy on",
      "--glass-alpha:0" in bt and "--glass-alpha-soft:0" in bt
      and "--box-alpha:0" in bt and "boxes-glass" in bt)
check("C: helpers agree at 0",
      here.glass_alpha_for(user_row(AID)) == "0"
      and here.glass_alpha_soft_for(user_row(AID)) == "0")
r = A.post("/settings/boxes", data={"box_opacity": "130"})
row = user_row(AID)
bt = body_tag(A.get("/home").text)
check("C: posting 130 clamps to 100 — solid by choice, no legacy",
      r.status_code == 303 and row["box_opacity"] == 100
      and "--glass-alpha:1" in bt and "--glass-alpha-soft:1" in bt
      and "--box-alpha" not in bt and "boxes-glass" not in bt)
r = A.post("/settings/look", data={"scheme": "glass",
                                   "box_opacity": "0"})
row = user_row(AID)
check("C: /settings/look Glass also saves 0 atomically",
      r.status_code == 303 and row["look_scheme"] == "glass"
      and row["box_opacity"] == 0
      and here.glass_alpha_for(row) == "0")

# ---------------- Mix redefined: solid cards, glass small stuff ---
A.post("/settings/boxes", data={"box_opacity": "40"})
A.post("/settings/look", data={"scheme": "mix",
                               "surface": "#2a1450",
                               "accent": "#a78bfa",
                               "box_opacity": "90"})
row = user_row(AID)
check("M: the Look form does NOT move the solidity outside Glass",
      row["look_scheme"] == "mix" and row["box_opacity"] == 40)
home = A.get("/home").text
bt = body_tag(home)
check("M: mix body — cards alpha 1 in the surface color, soft at 40",
      "--glass-alpha:1" in bt and "--glass-alpha-soft:0.4" in bt
      and "--glass-rgb:42,20,80" in bt, bt)
check("M: mix body has NO legacy boxes-glass / --box-alpha layer",
      "--box-alpha" not in bt and "boxes-glass" not in bt)
check("M: helpers split exactly the same way",
      here.glass_alpha_for(row) == "1"
      and here.glass_alpha_soft_for(row) == "0.4")
check("M: small-surface fills reference the -soft token",
      ".comment .cbub{background:rgba(240,242,245,"
      "var(--glass-alpha-soft))" in home
      and ".msg-theirs{background:rgba(228,230,235,"
      "var(--glass-alpha-soft))" in home
      and "background:rgba(70,70,78,var(--glass-alpha-soft))" in home
      and ".kind-tab{border:1px solid var(--border);background:"
      "rgba(245,246,247,var(--glass-alpha-soft))" in home)
check("M: card-family fills still reference --glass-alpha",
      ".card{background:rgba(var(--glass-rgb,255,255,255),"
      "var(--glass-alpha))" in home
      and ".page-card{background:rgba(var(--glass-rgb,255,255,255),"
      "var(--glass-alpha))" in home)

# a Mix owner's profile scope carries the same split for visitors
prof = B.get(f"/user/{AID}").text
sc = scope_tag(prof)
check("M: profile scope carries the owner's split alphas",
      "--glass-alpha:1" in sc and "--glass-alpha-soft:0.4" in sc, sc)

# ---------------- Solid: the slider touches nothing ---------------
A.post("/settings/look", data={"scheme": "solid"})
row = user_row(AID)
bt = body_tag(A.get("/home").text)
check("S: solid — both vars 1, helpers agree, no legacy layer",
      "--glass-alpha:1" in bt and "--glass-alpha-soft:1" in bt
      and here.glass_alpha_for(row) == "1"
      and here.glass_alpha_soft_for(row) == "1"
      and "--box-alpha" not in bt)

# ---------------- back to Glass: the saved value rules again ------
A.post("/settings/look", data={"scheme": "glass"})
bt = body_tag(A.get("/home").text)
check("G: returning to Glass restores the saved 40 on both vars",
      "--glass-alpha:0.4" in bt and "--glass-alpha-soft:0.4" in bt
      and "--box-alpha:0.4" in bt and "boxes-glass" in bt)

print()
failed = [n for n, ok in RESULTS if not ok]
print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
if failed:
    print("FAILED:", *failed, sep="\n  ")
    sys.exit(1)
