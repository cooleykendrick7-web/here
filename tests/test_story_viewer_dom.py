"""Story-viewer DOM tests for HERE: the split-screen regression.

Ken's report: when stories transitioned — especially to another
person's story — the full-screen viewer showed HALF of one story and
half of another side by side. Root cause: base.html's author rule
`.sv-stage img,.sv-stage video{display:block}` (specificity 0,1,1)
beats the UA stylesheet's `[hidden]{display:none}`, so setting
`el.hidden = true` in story.html's show() did NOT hide the inactive
media element. Once both #sv-img and #sv-vid had srcs (after the first
photo<->video transition) BOTH rendered in the flex row, each ~half
the stage.

Fix under test (two layers):
  1. base.html: `#story-viewer [hidden]{display:none!important}`.
  2. story.html show(): hide AND clear both media elements first
     (img.removeAttribute('src'); vid.pause() + removeAttribute('src')
     + vid.load()), then set up only the active one.

This suite drives the REAL viewer in headless Chromium over CDP
against the app served over real HTTP by uvicorn — visibility is
judged by the hidden attribute AND getComputedStyle().display, i.e.
the real CSS cascade, not string matching.

Run:  ./venv/bin/python tests/test_story_viewer_dom.py
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-svdom-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond), extra))
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


def befriend(c1, u1, c2, u2):
    r = c2.post(f"/friends/request/{u1}")
    assert r.status_code in (200, 303), r.status_code
    r = c1.post(f"/friends/accept/{u2}")
    assert r.status_code in (200, 303), r.status_code


PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDATx\x9cc\x00\x01"
       b"\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82")
MP4_FALLBACK = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64


def make_mp4():
    """A real tiny mp4 when ffmpeg is handy; visibility toggling is
    what's under test, so undecodable bytes are an acceptable fallback."""
    path = os.path.join(_tmp, "clip.mp4")
    try:
        p = subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             "color=c=red:s=64x64:d=0.5", "-pix_fmt", "yuv420p", path],
            capture_output=True, timeout=30)
        if p.returncode == 0 and os.path.getsize(path) > 0:
            with open(path, "rb") as f:
                return f.read()
    except Exception:
        pass
    return MP4_FALLBACK


def post_story(client, fname="x.png", blob=PNG, ctype="image/png", **fields):
    files = {"media": (fname, blob, ctype)}
    r = client.post("/stories/new", data=fields, files=files)
    assert r.status_code == 303, r.status_code
    return dbq("SELECT id FROM stories ORDER BY id DESC LIMIT 1")[0]["id"]


# ---------------- users + stories ----------------
# Queue order: viewer's own group first (Alice has none), then friends
# by latest story, newest first. Posting order C-video, C-photo, then
# B-photo makes the flat queue: B(photo) -> C(video) -> C(photo).
A, AID = signup("Alice A", "alice@svdom.io")
B, BID = signup("Bob B", "bob@svdom.io")
C, CID = signup("Carol C", "carol@svdom.io")
befriend(A, AID, B, BID)
befriend(A, AID, C, CID)

C_VID = post_story(C, fname="c.mp4", blob=make_mp4(), ctype="video/mp4")
C_PHOTO = post_story(C, caption="carol photo caption")
B_PHOTO = post_story(B, caption="bob photo caption",
                     overlay_text="BOB OVERLAY", overlay_color="#ffffff",
                     overlay_pos="middle")

# Pin a deterministic created_at ladder (group "latest" drives queue
# order; within a group stories run oldest -> newest).
conn = sqlite3.connect(os.environ["HERE_DB"])
for sid, ts in ((C_VID, "2026-10-22T10:00:00+00:00"),
                (C_PHOTO, "2026-10-22T10:00:01+00:00"),
                (B_PHOTO, "2026-10-22T10:00:02+00:00")):
    conn.execute("UPDATE stories SET created_at=? WHERE id=?", (ts, sid))
conn.commit()
conn.close()

q = A.get("/stories/queue").json()
flat_ids = [s["id"] for g in q["groups"] for s in g["stories"]]
check("queue order is B-photo -> C-video -> C-photo",
      flat_ids == [B_PHOTO, C_VID, C_PHOTO], str(flat_ids))

# ---------------- static contract checks ----------------
viewer_html = A.get(f"/story/{B_PHOTO}").text
check("base CSS carries the [hidden] guarantee for the viewer",
      "#story-viewer [hidden]" in viewer_html
      and "display:none!important" in viewer_html)
check("show() clears both media elements before setup",
      "img.removeAttribute('src')" in viewer_html
      and "vid.removeAttribute('src')" in viewer_html
      and "vid.load()" in viewer_html)

# ---------------- serve over real HTTP + drive Chromium ----------------
PORT = 8931
CDP_PORT = 8932
ORIGIN = f"http://127.0.0.1:{PORT}"
token = A.cookies.get("here_session")
assert token, "no session cookie from signup"

env = dict(os.environ)
server = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app:app",
     "--host", "127.0.0.1", "--port", str(PORT)],
    cwd=BASE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True)
chrome = None
try:
    deadline = time.time() + 30
    while True:
        try:
            with urllib.request.urlopen(ORIGIN + "/login", timeout=2) as r:
                if r.status == 200:
                    break
        except Exception:
            if time.time() > deadline:
                raise RuntimeError("uvicorn did not come up")
            time.sleep(0.25)

    chrome = subprocess.Popen(
        ["/opt/meta-chromium/chrome", "--headless=new", "--no-sandbox",
         "--disable-dev-shm-usage", "--disable-gpu",
         "--autoplay-policy=no-user-gesture-required",
         f"--remote-debugging-port={CDP_PORT}",
         "--remote-allow-origins=*", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    from websockets.sync.client import connect as ws_connect

    version = None
    deadline = time.time() + 30
    while version is None:
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{CDP_PORT}/json/version",
                    timeout=2) as r:
                version = json.loads(r.read())
        except Exception:
            if time.time() > deadline:
                raise RuntimeError("chromium CDP did not come up")
            time.sleep(0.25)

    ws = ws_connect(version["webSocketDebuggerUrl"])
    _seq = [0]
    _pending = {}

    # This Chromium build refuses ALL direct loopback navigations
    # (ERR_BLOCKED_BY_LOCAL_NETWORK_ACCESS_CHECKS — its Local Network
    # Access checks treat every CDP-driven initiator as a "public
    # page", and no flag/permission/preference we probed lifts it).
    # So the browser never touches the network itself: the CDP Fetch
    # domain pauses every request in the browser process and we
    # fulfill it here, proxied through Python's urllib. The page, its
    # CSS cascade, and its JS all run 100% for real in Chromium; only
    # the transport is relayed. The session cookie rides along as a
    # header on same-origin requests.
    def _send(method, params=None, session=None):
        _seq[0] += 1
        msg = {"id": _seq[0], "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        ws.send(json.dumps(msg))
        return _seq[0]

    def _handle_paused(m):
        import base64
        import urllib.error
        prm = m["params"]
        req = prm["request"]
        sess_id = m.get("sessionId")
        url = req["url"]
        try:
            headers = dict(req.get("headers") or {})
            if url.startswith(ORIGIN):
                headers["Cookie"] = f"here_session={token}"
            body = None
            if req.get("postData"):
                body = req["postData"].encode()
            rq = urllib.request.Request(url, data=body,
                                        method=req["method"])
            for k, v in headers.items():
                if k.lower() not in ("host", "content-length"):
                    rq.add_header(k, v)

            class _NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, *a, **kw):
                    return None

            opener = urllib.request.build_opener(_NoRedirect)
            try:
                resp = opener.open(rq, timeout=10)
                code, rheaders, rbody = (resp.status,
                                         resp.headers, resp.read())
            except urllib.error.HTTPError as e:
                code, rheaders, rbody = e.code, e.headers, e.read()
            out_headers = [{"name": k, "value": v}
                           for k, v in rheaders.items()
                           if k.lower() not in ("transfer-encoding",
                                                "connection",
                                                "content-length")]
            _send("Fetch.fulfillRequest",
                  {"requestId": prm["requestId"], "responseCode": code,
                   "responseHeaders": out_headers,
                   "body": base64.b64encode(rbody).decode()}, sess_id)
        except Exception:
            try:
                _send("Fetch.failRequest",
                      {"requestId": prm["requestId"],
                       "errorReason": "Failed"}, sess_id)
            except Exception:
                pass

    def _pump_until(mid, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            if mid in _pending:
                return _pending.pop(mid)
            raw = ws.recv(timeout=max(0.1, end - time.time()))
            m = json.loads(raw)
            if "id" in m:
                _pending[m["id"]] = m
            elif m.get("method") == "Fetch.requestPaused":
                _handle_paused(m)
        raise TimeoutError(f"CDP response {mid}")

    def cdp(method, params=None, session=None, timeout=30):
        m = _pump_until(_send(method, params, session), timeout)
        if "error" in m:
            raise RuntimeError(f"CDP {method}: {m['error']}")
        return m.get("result", {})

    target = cdp("Target.createTarget", {"url": "about:blank"})
    att = cdp("Target.attachToTarget",
              {"targetId": target["targetId"], "flatten": True})
    sess = att["sessionId"]
    cdp("Page.enable", session=sess)
    cdp("Runtime.enable", session=sess)
    cdp("Fetch.enable", {"patterns": [{"urlPattern": "*"}]}, session=sess)

    def ev(expr, await_promise=False):
        r = cdp("Runtime.evaluate",
                {"expression": expr, "returnByValue": True,
                 "awaitPromise": await_promise}, session=sess)
        res = r.get("result", {})
        if r.get("exceptionDetails"):
            raise RuntimeError("JS error: "
                               + json.dumps(r["exceptionDetails"])[:300])
        return res.get("value")

    def wait_for(expr, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if ev(expr):
                    return True
            except Exception:
                pass
            time.sleep(0.1)
        return False

    cdp("Page.navigate", {"url": f"{ORIGIN}/story/{B_PHOTO}"}, session=sess)
    ev(r"""
window.__vis = function (id) {
  var el = document.getElementById(id);
  if (!el) return null;
  return { hidden: el.hidden,
           display: getComputedStyle(el).display,
           src: el.getAttribute('src'),
           text: el.textContent };
};
window.__visOk = function (id) {
  var v = window.__vis(id);
  return !!v && !v.hidden && v.display !== 'none';
};
window.__visNo = function (id) {
  var v = window.__vis(id);
  return !!v && v.hidden && v.display === 'none';
};
window.__name = function () {
  var el = document.getElementById('sv-name');
  return el ? el.textContent : '';
};
window.__pause = function () {
  document.getElementById('sv-stage').dispatchEvent(
    new PointerEvent('pointerdown', {bubbles: true, clientX: 10}));
};
window.__next = function () {
  document.dispatchEvent(new KeyboardEvent('keydown',
    {key: 'ArrowRight', bubbles: true}));
};
true;
""")

    # ---- step 1: Bob's photo story ----
    ok = wait_for("__name() === 'Bob B' && __visOk('sv-img')")
    dbg = "name=%r img=%s" % (ev("__name()"), ev("__vis('sv-img')"))
    check("viewer opens on Bob's photo story", ok, dbg)
    if ok:
        ev("__pause()")  # hold the 5s photo clock while we assert
        v_img, v_vid = ev("__vis('sv-img')"), ev("__vis('sv-vid')")
        check("step1 photo: #sv-img visible with a src",
              v_img and not v_img["hidden"] and v_img["display"] != "none"
              and bool(v_img["src"]), str(v_img))
        check("step1 photo: #sv-vid hidden AND computed display:none "
              "(the split-screen regression)",
              v_vid and v_vid["hidden"] and v_vid["display"] == "none",
              str(v_vid))
        cap, ovl = ev("__vis('sv-caption')"), ev("__vis('sv-overlay')")
        check("step1: Bob's caption visible",
              cap and not cap["hidden"] and cap["display"] != "none"
              and cap["text"] == "bob photo caption", str(cap))
        check("step1: Bob's overlay visible",
              ovl and not ovl["hidden"] and ovl["display"] != "none"
              and ovl["text"] == "BOB OVERLAY", str(ovl))
        check("step1: YouTube wrap hidden (no music)",
              bool(ev("__visNo('sv-yt-wrap')")),
              str(ev("__vis('sv-yt-wrap')")))

        # ---- step 2: Carol's video story ----
        ev("__next()")
        ok2 = wait_for("__name() === 'Carol C' && __visOk('sv-vid')")
        dbg2 = "name=%r vid=%s" % (ev("__name()"), ev("__vis('sv-vid')"))
        check("advance to Carol's video story", ok2, dbg2)
        if ok2:
            ev("__pause()")
            v_img, v_vid = ev("__vis('sv-img')"), ev("__vis('sv-vid')")
            check("step2 video: #sv-vid visible with a src",
                  v_vid and not v_vid["hidden"]
                  and v_vid["display"] != "none" and bool(v_vid["src"]),
                  str(v_vid))
            check("step2 video: #sv-img hidden AND computed display:none",
                  v_img and v_img["hidden"] and v_img["display"] == "none",
                  str(v_img))
            check("step2 video: #sv-img src cleared",
                  v_img and v_img["src"] is None, str(v_img))
            dbg_cap = "cap=%s ovl=%s" % (ev("__vis('sv-caption')"),
                                         ev("__vis('sv-overlay')"))
            check("step2: Bob's caption + overlay are gone",
                  bool(ev("__visNo('sv-caption')"))
                  and bool(ev("__visNo('sv-overlay')")),
                  dbg_cap)
            check("step2: YouTube wrap still hidden",
                  bool(ev("__visNo('sv-yt-wrap')")),
                  str(ev("__vis('sv-yt-wrap')")))

            # ---- step 3: Carol's photo story ----
            ev("__next()")
            ok3 = wait_for("__visOk('sv-img') && __visNo('sv-vid')")
            dbg3 = "img=%s vid=%s" % (ev("__vis('sv-img')"),
                                      ev("__vis('sv-vid')"))
            check("advance to Carol's photo story", ok3, dbg3)
            if ok3:
                ev("__pause()")
                v_img, v_vid = ev("__vis('sv-img')"), ev("__vis('sv-vid')")
                check("step3 photo: header still Carol C",
                      ev("__name()") == "Carol C", ev("__name()"))
                check("step3 photo: #sv-img visible with a src",
                      v_img and not v_img["hidden"]
                      and v_img["display"] != "none" and bool(v_img["src"]),
                      str(v_img))
                check("step3 photo: #sv-vid hidden AND computed display:none",
                      v_vid and v_vid["hidden"]
                      and v_vid["display"] == "none", str(v_vid))
                check("step3 photo: #sv-vid src cleared (no stale frame)",
                      v_vid and v_vid["src"] is None, str(v_vid))
                cap = ev("__vis('sv-caption')")
                check("step3: Carol's caption visible",
                      cap and not cap["hidden"] and cap["display"] != "none"
                      and cap["text"] == "carol photo caption", str(cap))
                check("step3: YouTube wrap still hidden",
                      bool(ev("__visNo('sv-yt-wrap')")),
                      str(ev("__vis('sv-yt-wrap')")))
    try:
        ws.close()
    except Exception:
        pass
finally:
    for proc in (chrome, server):
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

# ---------------- node --check on the rendered viewer script ----------------
blocks = re.findall(r"<script>(.*?)</script>", viewer_html, re.S)
all_ok, bad = True, []
for i, block in enumerate(blocks):
    path = os.path.join(_tmp, f"chk_story_{i}.js")
    with open(path, "w") as f:
        f.write(block)
    p = subprocess.run(["node", "--check", path],
                       capture_output=True, text=True)
    if p.returncode != 0:
        all_ok = False
        bad.append(f"#{i}: {p.stderr.strip()[:160]}")
check("node --check: story viewer inline JS parses", all_ok, "; ".join(bad))

# ---------------- summary ----------------
fails = [n for n, ok_, _ in RESULTS if not ok_]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL GREEN")
