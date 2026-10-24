"""Fullscreen live + minimize-to-tile + browse mode suite, plus the
panel cap change to host + 8 guests (Part 2+3 of the live rework).

Server-side: cap behavior on BOTH admission paths (join-decide
approve + invite-decide accept) with the exact new error copy, the
8th guest still admitted, and the server-rendered fullscreen /
browse structure of GET /live/{id}.

DOM: real headless Chromium (CDP Fetch-relay pattern from
tests/test_story_viewer_dom.py / test_live_media_e2e.py — this
Chromium build refuses direct loopback navigations, so HTTP is
proxied through Python urllib; the page, CSS and JS run for real).
Proves minimize MOVES the very same <video> element into the mini
tile (identity marker), browse mode appears, no leave/end beacon
fires on minimize, expanding moves the same element back, and the
host's ✕ opens a sheet offering Shrink + End live.

Run:  ./venv/bin/python tests/test_live_fullscreen.py
"""
import base64
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-livefs-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{extra}]" if extra and not cond else ""), flush=True)


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
    assert r.status_code == 303, (name, r.status_code, r.text[:200])
    uid = dbq("SELECT id FROM users WHERE email=?", (email,))[0]["id"]
    return c, uid


# ================= cap: host + 8 guests =================
check("LIVE_PANEL_MAX is 9 (host + 8 guests)", here.LIVE_PANEL_MAX == 9,
      str(here.LIVE_PANEL_MAX))

CH, CHID = signup("Cap Host", "chost@fs.io")
guests = []
for i in range(9):
    gc, gid = signup(f"Cap Guest {i}", f"cg{i}@fs.io")
    guests.append((gc, gid))
IVC, IVID = signup("Cap Invitee", "civ@fs.io")

r = CH.post("/live/start", json={"title": "Cap room"})
CLID = r.json()["id"]
for gc, gid in guests:
    assert gc.post(f"/live/{CLID}/heartbeat").status_code == 200
assert IVC.post(f"/live/{CLID}/heartbeat").status_code == 200
# The invitee is invited BEFORE the panel fills; they accept after.
r = CH.post(f"/live/{CLID}/invite", json={"user_id": IVID})
check("cap: invitee invited while panel open", r.json().get("ok"),
      r.text[:200])
inv_cap = r.json()["invite"]["id"]

for i, (gc, gid) in enumerate(guests[:8]):
    rq = gc.post(f"/live/{CLID}/join-request").json()["request"]["id"]
    r = CH.post(f"/live/{CLID}/join-decide",
                json={"request_id": rq, "approve": True})
    check(f"cap: guest {i + 1} admitted via approve",
          r.status_code == 200 and r.json().get("ok"), r.text[:200])
check("cap: panel size is 9 after 8 guests",
      here._live_panel_size(here.db(), CLID) == 9)
gc9, gid9 = guests[8]
rq9 = gc9.post(f"/live/{CLID}/join-request").json()["request"]["id"]
r = CH.post(f"/live/{CLID}/join-decide",
            json={"request_id": rq9, "approve": True})
check("cap: 9th guest approve -> 409 exact copy",
      r.status_code == 409
      and r.json().get("error") == "Panel is full (8 guests max).",
      r.text[:200])
r = IVC.post(f"/live/{CLID}/invite-decide",
             json={"request_id": inv_cap, "accept": True})
check("cap: invite-accept at cap -> 409 exact copy",
      r.status_code == 409
      and r.json().get("error") == "Panel is full (8 guests max).",
      r.text[:200])
check("cap: refused paths took no seat",
      here._live_panel_size(here.db(), CLID) == 9)
CH.post(f"/live/{CLID}/end")

# ================= server-rendered fullscreen structure =================
HA, HAID = signup("Host Alpha", "halpha@fs.io")
HB, HBID = signup("Host Beta", "hbeta@fs.io")
W, WID = signup("Watcher Fs", "wfs@fs.io")

r = W.post("/posts", data={"kind": "text", "privacy": "public",
                           "body": "browse mode post body hello"})
assert r.status_code == 303, r.status_code
PID = dbq("SELECT id FROM posts WHERE user_id=?", (WID,))[0]["id"]

LA = HA.post("/live/start", json={"title": "Alpha live"}).json()["id"]
LB = HB.post("/live/start", json={"title": "Beta live"}).json()["id"]

page = W.get(f"/live/{LA}").text
check("fs: fullscreen layer present", 'id="live-fs"' in page)
check("fs: stage fills 100dvh", "height:100dvh" in page)
check("fs: stage-bg (host background tile) CSS exists",
      ".live-tile.stage-bg" in page and "object-fit:cover" in page)
check("fs: panel overlay stack CSS exists",
      ".live-grid.has-panel" in page)
check("fs: exit button present", 'id="live-exit"' in page and "✕" in page)
check("fs: chrome wrapper + rail present",
      'id="live-chrome"' in page and "live-rail" in page)
check("fs: chrome-dim CSS fades chrome to <=0.08",
      ".chrome-dim" in page and "opacity:.08" in page)
check("fs: exit exempt from full dim (>=0.35 rule)",
      "#live-fs.chrome-dim #live-exit" in page and "opacity:.35" in page)
check("fs: chrome-dim JS timer (~3.5s) present",
      "chrome-dim" in page and "3500" in page)
check("fs: reduced-motion kills the fade transition",
      "prefers-reduced-motion" in page)
check("fs: floating comments still at bottom:25%",
      "bottom:25%" in page)
check("fs: mini tile present with close + host end",
      'id="live-mini"' in page and 'id="live-mini-close"' in page
      and 'id="live-mini-end"' in page
      and "🔴 You're live" in page)
check("fs: minimized stage hides the comments float",
      ".live-stage.in-mini .live-float" in page)
check("fs: browse section present, hidden at load",
      'id="live-browse" hidden' in page)
browse = page[page.index('id="live-browse"'):]
check("browse: 'More lives' heading", "🔴 More lives" in browse)
check("browse: 'Latest posts' heading", "Latest posts" in browse)
check("browse: more-lives card links to the other live",
      f'href="/live/{LB}"' in browse and "Host Beta" in browse
      and "Beta live" in browse)
check("browse: does NOT list the current live as a card",
      f'href="/live/{LA}"' not in browse)
check("browse: latest post row rendered (author, body, post link)",
      "browse mode post body hello" in browse
      and f'href="/post/{PID}"' in browse and "Watcher Fs" in browse)
check("cap: join button uses the 9 cap in page JS",
      "panel.length >= 9" in page and "🎥 Panel is full" in page)
check("cap: old 16 cap gone from page JS",
      "panel.length >= 16" not in page)

blocks = re.findall(r"<script>(.*?)</script>", page, re.S)
all_ok, bad = True, []
for i, block in enumerate(blocks):
    path = os.path.join(_tmp, f"chk_fs_{i}.js")
    with open(path, "w") as f:
        f.write(block)
    p = subprocess.run(["node", "--check", path],
                       capture_output=True, text=True)
    if p.returncode != 0:
        all_ok = False
        bad.append(f"#{i}: {p.stderr.strip()[:160]}")
check("node --check: live page inline JS parses", all_ok, "; ".join(bad))

# ================= DOM: minimize moves the same <video> =================
PORT = 8981
ORIGIN = f"http://127.0.0.1:{PORT}"
HA_TOKEN = HA.cookies.get("here_session")
W_TOKEN = W.cookies.get("here_session")
assert HA_TOKEN and W_TOKEN

# A fresh live for the DOM run (LA is still up; HA can only host one,
# so starting LC ends LA — fine, the render checks are done).
LC = HA.post("/live/start", json={"title": "DOM live"}).json()["id"]


class Browser:
    def __init__(self, label, cdp_port, token):
        self.label = label
        self.token = token
        self.proc = subprocess.Popen(
            ["/opt/meta-chromium/chrome", "--headless=new", "--no-sandbox",
             "--disable-dev-shm-usage", "--disable-gpu",
             "--use-fake-device-for-media-stream",
             "--use-fake-ui-for-media-stream",
             "--disable-features=WebRtcHideLocalIpsWithMdns",
             "--autoplay-policy=no-user-gesture-required",
             f"--remote-debugging-port={cdp_port}",
             "--remote-allow-origins=*",
             f"--user-data-dir={os.path.join(_tmp, 'profile-' + label)}",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        from websockets.sync.client import connect as ws_connect
        version = None
        deadline = time.time() + 30
        while version is None:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{cdp_port}/json/version",
                        timeout=2) as r:
                    version = json.loads(r.read())
            except Exception:
                if time.time() > deadline:
                    raise RuntimeError(f"{label}: CDP did not come up")
                time.sleep(0.25)
        self.ws = ws_connect(version["webSocketDebuggerUrl"])
        self._seq = 0
        self._pending = {}
        self.sess = None
        target = self.cdp("Target.createTarget", {"url": "about:blank"})
        att = self.cdp("Target.attachToTarget",
                       {"targetId": target["targetId"], "flatten": True})
        self.sess = att["sessionId"]
        self.cdp("Page.enable")
        self.cdp("Runtime.enable")
        self.cdp("Fetch.enable", {"patterns": [{"urlPattern": "*"}]})

    def _send_raw(self, method, params, session):
        self._seq += 1
        msg = {"id": self._seq, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send(json.dumps(msg))
        return self._seq

    def _handle_paused(self, m):
        prm = m["params"]
        req = prm["request"]
        sess_id = m.get("sessionId")
        url = req["url"]
        try:
            headers = dict(req.get("headers") or {})
            if url.startswith(ORIGIN):
                headers["Cookie"] = f"here_session={self.token}"
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
            self._send_raw("Fetch.fulfillRequest",
                           {"requestId": prm["requestId"],
                            "responseCode": code,
                            "responseHeaders": out_headers,
                            "body": base64.b64encode(rbody).decode()},
                           sess_id)
        except Exception:
            try:
                self._send_raw("Fetch.failRequest",
                               {"requestId": prm["requestId"],
                                "errorReason": "Failed"}, sess_id)
            except Exception:
                pass

    def _pump_until(self, mid, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            if mid in self._pending:
                return self._pending.pop(mid)
            try:
                raw = self.ws.recv(timeout=max(0.1, end - time.time()))
            except TimeoutError:
                continue
            m = json.loads(raw)
            if "id" in m:
                self._pending[m["id"]] = m
            elif m.get("method") == "Fetch.requestPaused":
                self._handle_paused(m)
        raise TimeoutError(f"{self.label}: CDP response {mid}")

    def cdp(self, method, params=None, timeout=30):
        self._seq += 1
        msg = {"id": self._seq, "method": method, "params": params or {}}
        if self.sess:
            msg["sessionId"] = self.sess
        self.ws.send(json.dumps(msg))
        m = self._pump_until(self._seq, timeout)
        if "error" in m:
            raise RuntimeError(f"CDP {method}: {m['error']}")
        return m.get("result", {})

    def ev(self, expr, await_promise=False):
        r = self.cdp("Runtime.evaluate",
                     {"expression": expr, "returnByValue": True,
                      "awaitPromise": await_promise})
        if r.get("exceptionDetails"):
            raise RuntimeError("JS error: "
                               + json.dumps(r["exceptionDetails"])[:300])
        return r.get("result", {}).get("value")

    def wait_for(self, expr, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.ev(expr):
                    return True
            except Exception:
                pass
            time.sleep(0.1)
        return False

    def navigate(self, path):
        self.cdp("Page.navigate", {"url": ORIGIN + path})

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


env = dict(os.environ)
server = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app:app",
     "--host", "127.0.0.1", "--port", str(PORT)],
    cwd=BASE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True)
host_br = watch_br = None
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

    INSTRUMENT = r"""
window.__beacons = [];
navigator.sendBeacon = function (url) {
  window.__beacons.push(String(url)); return true;
};
true;
"""
    host_br = Browser("host", 8982, HA_TOKEN)
    host_br.navigate(f"/live/{LC}")
    # Wait for the LOCAL tile specifically, WITH its stream bound:
    # marking before getUserMedia lands would capture a null
    # srcObject that bindLocalTile then legitimately replaces.
    ok = host_br.wait_for(
        "!!document.querySelector('.live-tile video.localvid') "
        "&& !!document.querySelector('.live-tile video.localvid')"
        ".srcObject && !!window.__liveFs")
    check("dom host: live page booted with a tile video", ok)
    if ok:
        host_br.ev(INSTRUMENT)
        host_br.ev(r"""
var v = document.querySelector('.live-tile video.localvid');
v.__mark = 1; window.__marked = v;
window.__markedSrc = v.srcObject;
true;
""")
        # Host taps ✕ -> the exit sheet offers Shrink + End live.
        host_br.ev("document.getElementById('live-exit').click(); true;")
        ok_sheet = host_br.wait_for(
            "!document.getElementById('live-sheet').hidden "
            "&& document.getElementById('live-sheet-actions')"
            ".textContent.indexOf('Shrink') !== -1")
        sheet_txt = host_br.ev(
            "document.getElementById('live-sheet-actions').textContent")
        check("dom host: ✕ opens sheet with Shrink + End live",
              ok_sheet and "End live" in (sheet_txt or ""),
              str(sheet_txt))
        host_br.ev(r"""
var btns = document.querySelectorAll('#live-sheet-actions button');
for (var i = 0; i < btns.length; i++) {
  if (btns[i].textContent.indexOf('Shrink') !== -1) btns[i].click();
}
true;
""")
        ok_min = host_br.wait_for("window.__liveFs.isMinimized()")
        in_tile = host_br.ev(
            "document.getElementById('live-mini-slot')"
            ".contains(window.__marked) && window.__marked.__mark === 1 "
            "&& window.__marked.srcObject === window.__markedSrc")
        browse_vis = host_br.ev(
            "!document.getElementById('live-browse').hidden "
            "&& !document.getElementById('live-mini').hidden "
            "&& document.getElementById('live-fs').hidden")
        beacons = host_br.ev("window.__beacons.length")
        host_label = host_br.ev(
            "!document.getElementById('live-mini-host').hidden")
        check("dom host: Shrink minimizes", ok_min)
        check("dom host: SAME video element (identity + srcObject) "
              "now lives in the mini tile", bool(in_tile))
        check("dom host: browse mode visible, fullscreen hidden",
              bool(browse_vis))
        check("dom host: NO leave/end beacon fired on minimize",
              beacons == 0, str(beacons))
        check("dom host: mini tile shows 'You're live' + End",
              bool(host_label))
        # Tap the tile (pointerdown+up, no drag) -> back to fullscreen.
        host_br.ev(r"""
var m = document.getElementById('live-mini');
m.dispatchEvent(new PointerEvent('pointerdown',
  {bubbles: true, clientX: 20, clientY: 20}));
m.dispatchEvent(new PointerEvent('pointerup',
  {bubbles: true, clientX: 20, clientY: 20}));
true;
""")
        ok_back = host_br.wait_for("!window.__liveFs.isMinimized()")
        back = host_br.ev(
            "document.getElementById('live-stage-slot')"
            ".contains(window.__marked) "
            "&& document.getElementById('live-browse').hidden "
            "&& !document.getElementById('live-fs').hidden")
        check("dom host: tap tile restores fullscreen, same element "
              "back in the stage slot", bool(ok_back and back))

    watch_br = Browser("watcher", 8983, W_TOKEN)
    watch_br.navigate(f"/live/{LC}")
    ok = watch_br.wait_for(
        "!!document.querySelector('.live-tile video') "
        "&& !!window.__liveFs")
    check("dom watcher: live page booted with the host tile", ok)
    if ok:
        watch_br.ev(INSTRUMENT)
        watch_br.ev(r"""
var v = document.querySelector('.live-tile video');
v.__mark = 1; window.__marked = v; true;
""")
        watch_br.ev("document.getElementById('live-exit').click(); true;")
        ok_min = watch_br.wait_for("window.__liveFs.isMinimized()")
        in_tile = watch_br.ev(
            "document.getElementById('live-mini-slot')"
            ".contains(window.__marked) && window.__marked.__mark === 1")
        browse_vis = watch_br.ev(
            "!document.getElementById('live-browse').hidden")
        beacons = watch_br.ev("window.__beacons.length")
        check("dom watcher: ✕ minimizes directly (no sheet)",
              bool(ok_min))
        check("dom watcher: SAME host video element in the mini tile",
              bool(in_tile))
        check("dom watcher: browse mode visible", bool(browse_vis))
        check("dom watcher: NO leave beacon fired on minimize",
              beacons == 0, str(beacons))
        watch_br.ev("window.__liveFs.restore(); true;")
        back = watch_br.ev(
            "document.getElementById('live-stage-slot')"
            ".contains(window.__marked) "
            "&& !window.__liveFs.isMinimized()")
        check("dom watcher: restore returns the same element",
              bool(back))
finally:
    for br in (host_br, watch_br):
        if br is not None:
            br.close()
    try:
        server.terminate()
        server.wait(timeout=10)
    except Exception:
        try:
            server.kill()
        except Exception:
            pass

fails = [n for n, ok_ in RESULTS if not ok_]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL GREEN")
