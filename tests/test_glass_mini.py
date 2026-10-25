"""Build GLASS suite: site-wide glass look + the unified Glass
transparency setting, the floating mini live player (embed mode,
tile markup, minimize control), and live share links.

- Part A: ONE per-user preference (box_opacity + glass_set) drives
  --glass-alpha on the main surfaces site-wide; the default for a
  user who never saved is the clean glass 0.82; Your Spot's old
  bubble-transparency slider is replaced by the unified one (its
  stored tint strength is preserved on Spot saves) and the Spot
  tint now multiplies by --glass-alpha.
- Part B: watchers (not the host) get a Minimize control that stores
  here_mini_live; base.html carries the hidden mini tile + script
  (storage keys, ⤢ expand / ✕ stop wiring, drag position key);
  /live/{id}?embed=1 renders chrome-less for viewers and a static
  no-script card for the host; an ended live's embed posts the
  self-clear message.
- Part C: the live page's ↗ share sheet carries the canonical
  https://joincomehere.com/l/{id} link + FB/X/WhatsApp intents for
  a public live, and is absent for a private-profile host (the /l
  gate).

Run:  ./venv/bin/python tests/test_glass_mini.py
"""
import os, re, shutil, sqlite3, subprocess, sys, tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-glassmini-")
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


def body_tag(html):
    # '<body class=' on purpose: base.html's CSS comments mention a
    # bare <body> too (same helper shape as test_glass_live).
    m = re.search(r"<body class=[^>]*>", html)
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


FRESH, FRESH_ID = signup("Glass Fresh", "fresh@glass.io")
HOST, HOST_ID = signup("Glass Host", "host@glass.io")
VIEW, VIEW_ID = signup("Glass Viewer", "view@glass.io")

# ---------------- Part A: default glass for a fresh user ---------
home = FRESH.get("/home").text
bt = body_tag(home)
check("A: fresh user body carries the default --glass-alpha:0.82",
      "--glass-alpha:0.82" in bt)
check("A: fresh user has NO boxes-glass class / --box-alpha "
      "(legacy saved-pref layer untouched)", "boxes-glass" not in bt
      and "--box-alpha" not in bt)
check("A: :root declares the glass default",
      "--glass-alpha:0.82;" in home)
check("A: .card fills with the glass var",
      ".card{background:rgba(255,255,255,var(--glass-alpha))" in home)
check("A: .topbar fills with the glass var",
      "background:rgba(17,17,17,var(--glass-alpha))" in home)
check("A: comment bubbles fill with the glass var (no blur)",
      ".comment .cbub{background:rgba(240,242,245,var(--glass-alpha))"
      in home)
check("A: sheets fill with the glass var",
      ".aud-popup{position:fixed" in home
      and "background:rgba(255,255,255,var(--glass-alpha))" in home)
check("A: large surfaces get the unconditional glass blur",
      "){ .card, .page-card, .stat-card, .topbar" in home)
check("A: Your Spot tint multiplies by the unified glass alpha",
      "calc(var(--spot-card-alpha,.55) * var(--glass-alpha,1))" in home)
check("A: glass_alpha_for(None) is the default (guests)",
      here.glass_alpha_for(None) == "0.82")
_u = dbq("SELECT * FROM users WHERE id=?", (FRESH_ID,))[0]
check("A: glass_alpha_for(fresh user) is the default",
      here.glass_alpha_for(dict(_u)) == "0.82")

# ---------------- Part A: the Settings slider ---------------------
st = FRESH.get("/settings/profile").text
check("A: settings shows the Glass transparency card",
      "🪟 Glass transparency" in st)
check("A: settings keeps the Box Transparency words (legacy name)",
      "Box Transparency" in st)
check("A: the one slider keeps id/name box-opacity + box_opacity",
      'id="box-opacity"' in st and 'name="box_opacity"' in st)
check("A: slider ends are labeled More clear / More solid",
      "More clear" in st and "More solid" in st)
_m = re.search(r'id="box-opacity"[^>]*value="(\d+)"', st)
check("A: slider thumb sits at the effective default 82",
      _m and _m.group(1) == "82", st[st.find('id="box-opacity"'):
                                     st.find('id="box-opacity"') + 160]
      if 'id="box-opacity"' in st else "slider missing")
check("A: Your Spot bubble-transparency input is GONE (replaced)",
      'id="spot-opacity"' not in st
      and 'name="spot_card_opacity"' not in st)

r = FRESH.post("/settings/boxes", data={"box_opacity": "55"})
check("A: saving 55 redirects to settings", r.status_code == 303)
row = dbq("SELECT box_opacity, glass_set FROM users WHERE id=?",
          (FRESH_ID,))[0]
check("A: save persists box_opacity=55 AND glass_set=1",
      row["box_opacity"] == 55 and row["glass_set"] == 1)
home = FRESH.get("/home").text
bt = body_tag(home)
check("A: saved 55 -> body --glass-alpha:0.55 (and legacy "
      "--box-alpha:0.55 layer intact)",
      "--glass-alpha:0.55" in bt and "--box-alpha:0.55" in bt
      and "boxes-glass" in bt)
check("A: glass_alpha_for(saved 55) == '0.55'",
      here.glass_alpha_for(dict(dbq(
          "SELECT * FROM users WHERE id=?", (FRESH_ID,))[0])) == "0.55")

FRESH.post("/settings/boxes", data={"box_opacity": "100"})
bt = body_tag(FRESH.get("/home").text)
check("A: saved 100 -> --glass-alpha:1, solid BY CHOICE (no "
      "--box-alpha, no boxes-glass)", "--glass-alpha:1" in bt
      and "--box-alpha" not in bt.replace("--glass-alpha:1", "")
      and "boxes-glass" not in bt)
check("A: glass_alpha_for(saved 100) == '1'",
      here.glass_alpha_for(dict(dbq(
          "SELECT * FROM users WHERE id=?", (FRESH_ID,))[0])) == "1")

FRESH.post("/settings/boxes", data={"box_opacity": "5"})
row = dbq("SELECT box_opacity FROM users WHERE id=?", (FRESH_ID,))[0]
bt = body_tag(FRESH.get("/home").text)
check("A: 5 clamps to the 30 readability floor everywhere",
      row["box_opacity"] == 30 and "--glass-alpha:0.3" in bt)

# Spot saves preserve the stored tint strength (field absent now)
FRESH.post("/settings/spot", data={
    "spot_name": "Glass Pad", "spot_text_color": "#111111",
    "spot_card_color": "#ffd700", "spot_card_opacity": "44"})
check("A: legacy spot save with the field still stores 44",
      dbq("SELECT spot_card_opacity FROM users WHERE id=?",
          (FRESH_ID,))[0]["spot_card_opacity"] == 44)
FRESH.post("/settings/spot", data={
    "spot_name": "Glass Pad", "spot_text_color": "#222222",
    "spot_card_color": "#22c55e"})
check("A: spot save WITHOUT the field preserves spot_card_opacity",
      dbq("SELECT spot_card_opacity FROM users WHERE id=?",
          (FRESH_ID,))[0]["spot_card_opacity"] == 44)

# ---------------- Part B + C: a live to work with -----------------
r = HOST.post("/live/start", json={"title": "Glass Party"})
LID = r.json()["id"]
lurl = f"https://joincomehere.com/l/{LID}"

vpage = VIEW.get(f"/live/{LID}").text
check("B: viewer live page has the Minimize control",
      'id="ctl-minimize"' in vpage)
check("B: minimize stores here_mini_live with id/title/host",
      "localStorage.setItem('here_mini_live'" in vpage
      and "id: B.id" in vpage and "host: (B.host" in vpage)
check("B: minimize navigates back with a /home fallback",
      "window.history.length > 1" in vpage
      and "window.location.href = '/home'" in vpage)
hpage = HOST.get(f"/live/{LID}").text
check("B: host live page has NO Minimize control",
      'id="ctl-minimize"' not in hpage)

emb = VIEW.get(f"/live/{LID}?embed=1").text
check("B: embed body carries the live-embed class",
      "live-embed" in body_tag(emb))
check("B: embed boot joins as a normal watcher with embed on",
      '"role": "watcher"' in emb and '"embed": true' in emb)
check("B: embed hides the chrome (topbar/rail/overlay stack)",
      "body.live-embed .live-topbar" in emb
      and "body.live-embed .live-chrome" in emb
      and "body.live-embed .live-overlay-stack" in emb)
check("B: embed has the glass speaker chip",
      'id="embed-sound"' in emb)
check("B: embed chip/token discipline (lg tokens, no banned fills)",
      "background:var(--lg-bg-strong)" in emb
      and "rgba(0,0,0,.45)" not in emb
      and "background:rgba(255,255,255,.78)" not in emb)
hemb = HOST.get(f"/live/{LID}?embed=1").text
check("B: host embed is the static card — no live script at all",
      "You're hosting this live" in hemb and "LIVE_BOOT" not in hemb)
check("B: embed page inline JS parses", node_check(emb, "emb"))
check("B: viewer live page inline JS parses", node_check(vpage, "live"))

# mini tile in base (rendered on a normal app page)
home = VIEW.get("/home").text
check("B: mini tile markup is in base, hidden by default",
      '<div id="mini-live" hidden>' in home)
check("B: tile carries frame + expand + stop controls",
      'id="mini-live-frame"' in home and 'id="mini-live-expand"' in home
      and 'id="mini-live-stop"' in home)
check("B: tile script keys on here_mini_live / here_mini_pos",
      "here_mini_live" in home and "here_mini_pos" in home)
check("B: tile embeds /live/{id}?embed=1",
      "'/live/' + data.id + '?embed=1'" in home)
check("B: tile stop clears the storage and unloads the iframe",
      "localStorage.removeItem(KEY)" in home
      and "frame.src = 'about:blank'" in home)
check("B: tile expand navigates top-level to the full live",
      "location.href = '/live/' + data.id" in home)
check("B: tile never renders on live/owner/auth paths (JS gate)",
      "path.indexOf('/live/') === 0" in home
      and "path.indexOf('/owner') === 0" in home
      and "path === '/login'" in home)
check("B: tile self-clears on the embed's ended message",
      "d.hereMini === 'ended'" in home)
check("B: tile script parses", node_check(home, "home"))

# ---------------- Part C: live share sheet ------------------------
check("C: public live page has the share toggle",
      'id="ctl-live-share"' in vpage
      and f'data-sheet="share-sheet-live-{LID}"' in vpage)
check("C: share sheet carries the canonical /l link",
      f'id="share-sheet-live-{LID}"' in vpage and lurl in vpage)
check("C: sheet has Copy link + native share",
      'class="btn btn-sm share-copy"' in vpage
      and "share-native" in vpage)
check("C: sheet has FB / X / WhatsApp intents with the /l link",
      "facebook.com/sharer/sharer.php?u=" in vpage
      and "twitter.com/intent/tweet?url=" in vpage
      and "wa.me/?text=" in vpage
      # Jinja urlencode (the same filter Build SHARE's post sheets
      # use) encodes ':' but leaves '/' — the intents carry the
      # canonical link in that form.
      and f"https%3A//joincomehere.com/l/{LID}" in vpage)

# private-profile host: no external sheet (mirrors the /l gate)
HOST2, HOST2_ID = signup("Glass Private", "priv@glass.io")
dbx("UPDATE users SET private_profile=1 WHERE id=?", (HOST2_ID,))
r = HOST2.post("/live/start", json={"title": "Quiet Room"})
LID2 = r.json()["id"]
p2 = HOST2.get(f"/live/{LID2}").text
check("C: private host's live page exposes NO share sheet/toggle",
      "share-sheet-live-" not in p2 and 'id="ctl-live-share"' not in p2)
HOST2.post(f"/live/{LID2}/end")

# ---------------- ended embed self-clears -------------------------
HOST.post(f"/live/{LID}/end")
eemb = VIEW.get(f"/live/{LID}?embed=1").text
check("B: ended live's embed posts the self-clear message",
      "hereMini:'ended'" in eemb and "This live has ended" in eemb)
check("B: ended embed JS parses", node_check(eemb, "eemb"))

print()
fails = [n for n, ok in RESULTS if not ok]
print(f"{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", *fails, sep="\n  ")
    sys.exit(1)
shutil.rmtree(_tmp, ignore_errors=True)
