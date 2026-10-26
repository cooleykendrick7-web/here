"""Build SHINE suite: the lit-glass finish, gated by scheme.

- render() stamps the viewer's scheme class (scheme-glass /
  scheme-mix / scheme-solid) on <body> and emits --look-accent-rgb
  (the accent as an rgba() triplet) beside --look-accent; a guest
  defaults to scheme-glass.
- The profile .spot-scope carries the OWNER's scheme class and the
  owner's accent triplet, so a profile shines by its owner.
- The shine CSS lives in ONE delimited block in base.html: deeper
  frost blur(20px) saturate(1.35) on the card family, the lit edge
  (top sheen + accent rim + halo), the floating header strip, orb
  buttons with a pulsing live orb, glass-pill buttons, red-glass
  Go Live, sheen-only chips, the profile avatar glow ring, plain
  RESTORE rules for non-glass scopes, and a
  prefers-reduced-transparency kill switch. Every shine selector
  is scheme-gated, and no small surface ever gains backdrop-filter
  (the kill switch's backdrop-filter:none excepted).
- Alpha semantics are untouched: at --glass-alpha:0 the shine
  rules still apply (frosted outlines that still shine).

Run:  ./venv/bin/python tests/test_shine.py
"""
import os, re, sqlite3, sys, tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-shine-")
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


def shine_block(html):
    """The delimited Build SHINE CSS block out of a rendered page."""
    a = html.find("Build SHINE: the lit-glass finish")
    b = html.find("/Build SHINE", a)
    return html[a:b] if a >= 0 and b > a else ""


def block_rules(block):
    """(selector, declarations) pairs of a CSS block, with the
    @keyframes body removed (its percentage selectors aren't rules)
    and at-rule headers skipped (their inner rules appear as their
    own pairs)."""
    block = re.sub(r"@keyframes[^{]*\{(?:[^{}]|\{[^{}]*\})*\}", "",
                   block)
    out = []
    for m in re.finditer(r"([^{}]+)\{([^{}]*)\}", block):
        sel = " ".join(m.group(1).split())
        if sel.startswith("@"):
            continue
        out.append((sel, m.group(2)))
    return out


A, AID = signup("Shine A", "a@shine.io")
B, BID = signup("Shine B", "b@shine.io")

# ---------------- gate: scheme class + accent triplet ------------
row = user_row(AID)
check("G: helpers — fresh user is scheme-glass, red triplet",
      here.look_scheme_class_for(row) == "scheme-glass"
      and here.look_accent_rgb_str_for(row) == "212,31,46"
      and here.look_scheme_class_for(None) == "scheme-glass"
      and here.look_accent_rgb_str_for(None) == "212,31,46")
check("G: scope style carries the accent triplet",
      "--look-accent-rgb:212,31,46" in here.look_scope_style_for(row))
home = A.get("/home").text
bt = body_tag(home)
check("G: fresh body — scheme-glass + default accent triplet",
      "scheme-glass" in bt and "--look-accent-rgb:212,31,46" in bt, bt)
A.post("/settings/look", data={"scheme": "mix"})
bt = body_tag(A.get("/home").text)
check("G: mix body carries scheme-mix (and not scheme-glass)",
      "scheme-mix" in bt and "scheme-glass" not in bt, bt)
A.post("/settings/look", data={"scheme": "solid"})
bt = body_tag(A.get("/home").text)
check("G: solid body carries scheme-solid (and not scheme-glass)",
      "scheme-solid" in bt and "scheme-glass" not in bt, bt)
A.post("/settings/look", data={"scheme": "glass",
                               "accent": "#d4af37"})
row = user_row(AID)
bt = body_tag(A.get("/home").text)
check("G: custom accent #d4af37 emits triplet 212,175,55",
      here.look_accent_rgb_str_for(row) == "212,175,55"
      and "--look-accent:#d4af37" in bt
      and "--look-accent-rgb:212,175,55" in bt, bt)

# ---------------- profile scope shines by the OWNER --------------
# A is glass + gold accent; B (visitor) goes solid + rose accent.
B.post("/settings/look", data={"scheme": "solid",
                               "accent": "#fb7185"})
prof = B.get(f"/user/{AID}").text
sc, bt = scope_tag(prof), body_tag(prof)
check("P: scope carries the OWNER's scheme-glass + gold triplet",
      "scheme-glass" in sc and "--look-accent-rgb:212,175,55" in sc, sc)
check("P: visitor chrome keeps the visitor's scheme-solid + rose",
      "scheme-solid" in bt and "--look-accent-rgb:251,113,133" in bt
      and "scheme-glass" not in bt, bt)
# Flip the owner to solid: the scope follows the owner, not B.
A.post("/settings/look", data={"scheme": "solid"})
prof = B.get(f"/user/{AID}").text
sc = scope_tag(prof)
check("P: owner turning solid un-shines the scope",
      "scheme-solid" in sc and "scheme-glass" not in sc, sc)
A.post("/settings/look", data={"scheme": "glass",
                               "accent": "#d4af37"})
# Logged-out public card: owner's scheme class + triplet ride along.
pub = TestClient(here.app, follow_redirects=False).get(
    f"/u/{AID}").text
check("P: public /u card carries the owner's scheme-glass + triplet",
      "scheme-glass" in pub
      and "--look-accent-rgb:212,175,55" in pub
      and ".card.scheme-glass" in pub)

# ---------------- the shine block itself --------------------------
home = A.get("/home").text
blk = shine_block(home)
check("B: the delimited SHINE block is present", bool(blk))
check("B: deeper frost — blur(20px) saturate(1.35)",
      "blur(20px) saturate(1.35)" in blk
      and "body.scheme-glass .card" in blk)
check("B: lit edge — top sheen, accent rim, outer halo",
      "inset 0 1px 0 rgba(255,255,255,.38)" in blk
      and "inset 0 -1px 0 rgba(var(--look-accent-rgb,212,31,46),.35)"
      in blk
      and "0 0 26px rgba(var(--look-accent-rgb,212,31,46),.10)" in blk
      and "border:1px solid rgba(255,255,255,.26)" in blk)
check("B: header is a floating glass strip",
      "body.scheme-glass .topbar{top:10px;left:10px;right:10px;"
      "width:auto;border-radius:20px" in blk)
check("B: orbs — lit rims, and the live orb pulses (class in markup)",
      "body.scheme-glass .nav-ic" in blk
      and "@keyframes shinepulse" in blk
      and 'class="nav-ic nav-live"' in home)
check("B: buttons — accent glass pill + stronger red-glass Go Live",
      "background:rgba(var(--look-accent-rgb,212,31,46),.20)" in blk
      and "body.scheme-glass #golive-btn" in blk
      and "background:rgba(212,31,46,.34)" in blk)
check("B: profile avatar glow ring in glass scopes",
      ".spot-scope.scheme-glass .avatar.av-100" in blk
      and "0 0 20px rgba(var(--look-accent-rgb,212,31,46),.45)" in blk)
check("B: RESTORE rules hand non-glass scopes their plain surfaces",
      ".spot-scope.scheme-solid .card" in blk
      and ".spot-scope.scheme-mix .card" in blk
      and ".spot-scope.scheme-solid .btn" in blk)
check("B: kill switch — reduced transparency turns shine off",
      "@media (prefers-reduced-transparency: reduce)" in blk
      and "backdrop-filter:none" in blk)
check("B: pulse respects reduced motion",
      "@media (prefers-reduced-motion: reduce)" in blk
      and "animation:none" in blk)

rules = block_rules(blk)
check("B: rules were parsed out of the block", len(rules) >= 20,
      str(len(rules)))
ungated = [s for s, _ in rules
           if "scheme-glass" not in s and "scheme-mix" not in s
           and "scheme-solid" not in s]
check("B: EVERY shine rule is scheme-gated", not ungated,
      "; ".join(ungated[:3]))
SMALL = (".cbub", ".msg-theirs", ".kind-tab", ".kind-tag",
         ".picker-chip", ".poll-opt-btn", ".nav-ic", ".chip")
blursed = [s for s, d in rules
           if "backdrop-filter" in d
           and "backdrop-filter:none" not in d
           and any(k in s for k in SMALL)]
check("B: no small surface gains backdrop-filter", not blursed,
      "; ".join(blursed[:3]))
cardblur = [s for s, d in rules
            if "blur(20px)" in d and ".card" in s]
check("B: the deep frost lands on the card family", bool(cardblur))
mixcard = [s for s, d in rules
           if "scheme-mix" in s and ".card" in s and "blur(20px)" in d]
check("B: mix never gets the deep frost on the card family",
      not mixcard, "; ".join(mixcard[:3]))

# ---------------- alpha still rules (CLEAR untouched) -------------
A.post("/settings/boxes", data={"box_opacity": "0"})
home = A.get("/home").text
bt = body_tag(home)
check("A: at completely-clear the shine still applies by class",
      "scheme-glass" in bt and "--glass-alpha:0" in bt
      and "blur(20px) saturate(1.35)" in shine_block(home), bt)

print()
failed = [n for n, ok in RESULTS if not ok]
print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
if failed:
    print("FAILED:", *failed, sep="\n  ")
    sys.exit(1)
