"""Glass + live overlay suite (Build G): Box Transparency setting
(app-wide, per-viewer) and the live screen rework (floating comments
over the stage + client-only join/panel notices). Throwaway DB.

Run:  ./venv/bin/python tests/test_glass_live.py
"""
import os, re, shutil, sqlite3, subprocess, sys, tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-glass-")
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
    assert r.status_code == 303, (name, r.status_code, r.text[:200])
    uid = dbq("SELECT id FROM users WHERE email=?", (email,))[0]["id"]
    return c, uid


def box_opacity_of(uid):
    return dbq("SELECT box_opacity FROM users WHERE id=?",
               (uid,))[0]["box_opacity"]


def body_tag(html_text):
    """The rendered <body ...> tag only. NOTE: base.html's CSS block
    itself contains the literal strings 'boxes-glass' and
    '--box-alpha' (selectors + var(--box-alpha,1)), so whole-page
    containment can never distinguish the on/off states — the body
    tag's class/style attributes are what render() actually emits,
    and they are what these checks scope to. (The match is anchored
    on '<body class=' because CSS/HTML comments mention <body> too.)"""
    m = re.search(r"<body class=[^>]*>", html_text, re.S)
    return m.group(0) if m else ""


# ---------------- 1. Box Transparency setting ----------------
A, AID = signup("Glass A", "a@x.io")
B, BID = signup("Glass B", "b@x.io")

r = A.post("/settings/boxes", data={"box_opacity": "55"})
check("POST /settings/boxes 55 -> 303", r.status_code == 303,
      str(r.status_code))
check("DB box_opacity == 55 after save", box_opacity_of(AID) == 55,
      str(box_opacity_of(AID)))
home = A.get("/home").text
check("/home <body> contains boxes-glass at 55",
      "boxes-glass" in body_tag(home))
check("/home <body> contains --box-alpha:0.55",
      "--box-alpha:0.55" in body_tag(home))
prof = A.get("/settings/profile").text
check("/settings/profile contains Box Transparency card",
      "Box Transparency" in prof)
check("/settings/profile contains the box-opacity slider",
      'id="box-opacity"' in prof and 'name="box_opacity"' in prof)

r = A.post("/settings/boxes", data={"box_opacity": "5"})
check("POST /settings/boxes 5 -> 303", r.status_code == 303,
      str(r.status_code))
check("POST 5 clamps DB to 30", box_opacity_of(AID) == 30,
      str(box_opacity_of(AID)))
check("/home <body> contains --box-alpha:0.3 at 30",
      "--box-alpha:0.3" in body_tag(A.get("/home").text))

r = A.post("/settings/boxes", data={"box_opacity": "250"})
check("POST /settings/boxes 250 -> 303", r.status_code == 303,
      str(r.status_code))
check("POST 250 clamps DB to 100", box_opacity_of(AID) == 100,
      str(box_opacity_of(AID)))
home100 = A.get("/home").text
check("/home <body> at 100 has NO boxes-glass",
      "boxes-glass" not in body_tag(home100))
check("/home <body> at 100 has NO --box-alpha",
      "--box-alpha" not in body_tag(home100))

# Per-viewer: A back at 55, fresh user B (never saved) stays solid.
A.post("/settings/boxes", data={"box_opacity": "55"})
check("A reset to 55 for the per-viewer check",
      box_opacity_of(AID) == 55, str(box_opacity_of(AID)))
check("A /home <body> has boxes-glass again at 55",
      "boxes-glass" in body_tag(A.get("/home").text))
b_home = B.get("/home").text
check("fresh user B /home <body> has NO boxes-glass",
      "boxes-glass" not in body_tag(b_home))
check("fresh user B /home <body> has NO --box-alpha",
      "--box-alpha" not in body_tag(b_home))

r = A.post("/settings/boxes", data={"box_opacity": "abc"})
check("POST /settings/boxes 'abc' -> 303", r.status_code == 303,
      str(r.status_code))
check("POST 'abc' leaves DB value unchanged (55)",
      box_opacity_of(AID) == 55, str(box_opacity_of(AID)))

# ---------------- 2. Live page rework ----------------
H, HID = signup("Host H", "host@x.io")
r = H.post("/live/start", json={"title": "Glass night"})
check("live start ok", r.status_code == 200 and r.json().get("ok"),
      r.text[:200])
LID = r.json()["id"]
live_html = H.get(f"/live/{LID}").text
for needle in ("live-float", "bottom:25%", "live-notice",
               "function liveRoomDiff", "joined the live",
               "is on the panel", "live-comment-input"):
    check(f"live page contains {needle!r}", needle in live_html)
check("live page has exactly ONE id=\"live-comments\"",
      live_html.count('id="live-comments"') == 1,
      str(live_html.count('id="live-comments"')))
check("'live-float' appears BEFORE 'live-comment-form'",
      -1 < live_html.find("live-float") < live_html.find("live-comment-form"))
check("live page does NOT keep .live-panel{background:var(--card)",
      ".live-panel{background:var(--card)" not in live_html)

# ---------------- 3. liveRoomDiff harness + node --check ----------------
_node = shutil.which("node")
if not _node:
    print("SKIP node unavailable — diff harness + node --check skipped")
    check("node checks skipped (node unavailable)", True)
else:
    m = re.search(r"function liveRoomDiff\(prev, next\)\{[\s\S]*?\n\}",
                  live_html)
    check("liveRoomDiff extracted from rendered live page", bool(m))
    if m:
        driver = m.group(0) + "\n" + "\n".join([
            "var seed = liveRoomDiff(null, "
            "{viewers:{1:'A'},panel:{1:'A'}});",
            "console.log(JSON.stringify(seed));",
            "var prev = {viewers:{1:'A'},panel:{1:'A'}};",
            "var next = {viewers:{1:'A',2:'B',3:'C'},panel:{1:'A',3:'C'}};",
            "console.log(JSON.stringify(liveRoomDiff(prev, next)));",
            "console.log(JSON.stringify(liveRoomDiff(next, prev)));",
            ""])
        dpath = os.path.join(_tmp, "diff.js")
        with open(dpath, "w") as fh:
            fh.write(driver)
        p = subprocess.run([_node, dpath], capture_output=True, text=True)
        lines = p.stdout.strip().splitlines() if p.stdout.strip() else []
        check("diff harness runs clean under node", p.returncode == 0,
              p.stderr[:300])
        check("diff: seed(null) -> []",
              len(lines) > 0 and lines[0] == "[]",
              lines[0] if lines else p.stdout[:200])
        check("diff: arrivals -> [panel 3 C, join 2 B] exactly",
              len(lines) > 1 and lines[1] == (
                  '[{"kind":"panel","id":3,"name":"C"},'
                  '{"kind":"join","id":2,"name":"B"}]'),
              lines[1] if len(lines) > 1 else p.stdout[:200])
        check("diff: leaving (next subset of prev) -> []",
              len(lines) > 2 and lines[2] == "[]",
              lines[2] if len(lines) > 2 else p.stdout[:200])
    # node --check every inline <script> body on the rendered live page
    blocks = re.findall(r"<script>(.*?)</script>", live_html, re.S)
    check("live page has inline scripts to check", len(blocks) >= 3,
          str(len(blocks)))
    ok, bad = True, ""
    for i, b in enumerate(blocks):
        if not b.strip():
            continue
        path = os.path.join(_tmp, f"live_{i}.js")
        with open(path, "w") as fh:
            fh.write(b)
        p = subprocess.run([_node, "--check", path],
                           capture_output=True, text=True)
        if p.returncode != 0:
            ok = False
            bad = f"script {i}: " + p.stderr.strip()[:200]
    check("node --check: rendered live page inline JS parses", ok, bad)

# ---------------- 4. boot-on-copy (dev DB migration) ----------------
dev_db = os.path.join(BASE, "here.db")
if os.path.exists(dev_db):
    copy_db = os.path.join(_tmp, "devcopy.db")
    shutil.copyfile(dev_db, copy_db)
    env2 = dict(os.environ, HERE_DB=copy_db,
                HERE_UPLOADS=os.path.join(_tmp, "uploads2"))
    boot = subprocess.run(
        [sys.executable, "-c",
         "import app; from fastapi.testclient import TestClient;"
         "print(TestClient(app.app).get('/login').status_code)"],
        cwd=BASE, env=env2, capture_output=True, text=True)
    check("dev DB copy: boots without exception (login 200)",
          boot.returncode == 0 and boot.stdout.strip() == "200",
          (boot.stdout + boot.stderr)[-300:])
    cols = [r[1] for r in sqlite3.connect(copy_db).execute(
        "PRAGMA table_info(users)")]
    check("dev DB copy: box_opacity column exists after boot",
          "box_opacity" in cols, str(cols))
else:
    print("SKIP no dev DB present — boot-on-copy check skipped")
    check("dev DB copy: skipped (no here.db in repo)", True)

# ---------------- summary ----------------
fails = [n for n, okk in RESULTS if not okk]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
