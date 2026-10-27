"""Studio suite (Build STUDIO): 🎬 Studio on the live page —
game + facecam multitask compositing, face effects, green
screen, picture-on-screen, and the PC bottom-bar fix.

Static: the 🎬 Studio button, the floating PC bar rules, the
mouse-wake hook, and every load-bearing Studio string on a
TestClient-rendered live page + node --check.

DOM: one headless Chromium host on a real uvicorn (harness
pattern from tests/test_live_connect.py — Fetch-relayed HTTP,
fake media devices). Proves Studio starts (canvas track live,
own tile shows the 1280x720 Studio picture), facecam turns on,
the sheet's effect/green-screen controls land in state, a
picture file lands as an overlay, and Stop restores the plain
camera picture. Sender-side proof with a real watcher peer is
the same replaceTrack funnel Live Connect's suite proves; here
the peer-less publish path + tile binding are what's new.

Run:  ./venv/bin/python tests/test_live_studio.py
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
_tmp = tempfile.mkdtemp(prefix="here-test-studio-")
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
    conn = sqlite3.connect(os.environ["HERE_DB"], timeout=10)
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


H, HID = signup("Studio Host", "host@studio.io")
H_TOKEN = H.cookies.get("here_session")
assert H_TOKEN, "missing session cookie"

# ================= static: rendered page =================
SLID = H.post("/live/start", json={"title": "Static Studio"}).json()["id"]
page = H.get(f"/live/{SLID}").text

m = re.search(r'<button[^>]*id="ctl-studio"[^>]*>(.*?)</button>', page, re.S)
check("static: ctl-studio button present with 🎬 Studio text",
      bool(m) and "🎬 Studio" in m.group(1))
check("static: ctl-studio sits right after ctl-connect",
      page.index('id="ctl-connect"') < page.index('id="ctl-studio"')
      < page.index('id="ctl-panel-leave"'))
for needle, label in [
        ("captureStream(30)", "canvas captureStream publish"),
        ("studioKeyed", "chroma-key pass"),
        ("__liveStudio", "diagnostics surface"),
        ("Facecam (you, over the game)", "facecam section"),
        ("Green screen", "green screen section"),
        ("Picture on screen", "picture section"),
        ("here_studio", "settings localStorage key"),
        ("studioAfterSourceChange", "source-change funnel hook"),
        ("Put behind me", "backdrop picker"),
        ("Key strength", "key sensitivity slider"),
        ("viewers see the finished picture", "sender-composite note"),
]:
    check(f"static: {label} present", needle in page)
check("static: PC bar floats as a centered pill on wide screens",
      "@media (min-width:900px)" in page
      and "transform:translateX(-50%)" in page
      and "bottom:14px" in page)
check("static: bar keeps safe-area padding on small screens",
      "env(safe-area-inset-bottom" in page)
check("static: dimmed chrome recovers on hover (PC)",
      ".chrome-dim .live-chrome:hover" in page)
check("static: mouse move wakes the chrome (PC)",
      "pointermove" in page and "chrome-dim" in page)

blocks = re.findall(r"<script>(.*?)</script>", page, re.S)
all_ok, bad = True, []
for i, block in enumerate(blocks):
    path = os.path.join(_tmp, f"chk_st_{i}.js")
    with open(path, "w") as f:
        f.write(block)
    p = subprocess.run(["node", "--check", path],
                       capture_output=True, text=True)
    if p.returncode != 0:
        all_ok = False
        bad.append(f"#{i}: {p.stderr.strip()[:160]}")
check("node --check: live page inline JS parses", all_ok, "; ".join(bad))

PORT = 8995
ORIGIN = f"http://127.0.0.1:{PORT}"

class Browser:
    def __init__(self, label, cdp_port, token):
        self.label = label
        self.token = token
        self.cdp_port = cdp_port
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
        target = self.cdp("Target.createTarget", {"url": "about:blank"})
        att = self.cdp("Target.attachToTarget",
                       {"targetId": target["targetId"], "flatten": True})
        self.sess = att["sessionId"]
        self.cdp("Page.enable", session=self.sess)
        self.cdp("Runtime.enable", session=self.sess)
        self.cdp("Fetch.enable", {"patterns": [{"urlPattern": "*"}]},
                 session=self.sess)

    def _send(self, method, params=None):
        self._seq += 1
        msg = {"id": self._seq, "method": method, "params": params or {}}
        if getattr(self, "sess", None):
            msg["sessionId"] = self.sess
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

    def _send_raw(self, method, params, session):
        self._seq += 1
        msg = {"id": self._seq, "method": method, "params": params or {}}
        if session:
            msg["sessionId"] = session
        self.ws.send(json.dumps(msg))
        return self._seq

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

    def cdp(self, method, params=None, session=None, timeout=30):
        self._seq += 1
        msg = {"id": self._seq, "method": method, "params": params or {}}
        sess = session if session is not None else getattr(self, "sess", None)
        if sess:
            msg["sessionId"] = sess
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



def wait_js(b, expr, seconds=25, others=()):
    """Pump `b` until expr is truthy; keep the other browsers'
    Fetch relays fed meanwhile (a starved relay freezes a page)."""
    end = time.time() + seconds
    while time.time() < end:
        try:
            if b.ev(expr):
                return True
        except Exception:
            pass
        for o in others:
            try:
                o.ev("1")
            except Exception:
                pass
        time.sleep(0.15)
    return False


def pump_all(seconds):
    end = time.time() + seconds
    while time.time() < end:
        for b in ALL_BROWSERS:
            try:
                b.ev("1")
            except Exception:
                pass
        time.sleep(0.1)

# ================= DOM: real Chromium host =================
env = dict(os.environ)
server = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app:app",
     "--host", "127.0.0.1", "--port", str(PORT)],
    cwd=BASE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True)
host_b = None
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

    host_b = Browser("studiohost", 8996, H_TOKEN)
    LID = H.post("/live/start", json={"title": "Studio DOM"}).json()["id"]
    host_b.navigate(f"/live/{LID}")
    ok = wait_js(host_b,
                 "window.__liveConnect && __liveConnect.localTracks()"
                 ".some(function(t){return t.kind==='video'})", 25)
    check("boot: host local video live (fake camera)", ok)
    ok = wait_js(host_b,
                 "var b=document.getElementById('ctl-studio');"
                 "!!b && !b.hidden", 15)
    check("boot: 🎬 Studio button visible for the host", ok)

    # ---- PC bar: wide viewport -> floating pill geometry ----
    host_b.cdp("Emulation.setDeviceMetricsOverride",
               {"width": 1400, "height": 900, "deviceScaleFactor": 1,
                "mobile": False})
    time.sleep(0.4)
    geo = host_b.ev(
        "(function(){var b=document.getElementById('live-controls');"
        "var r=b.getBoundingClientRect();"
        "return {top:r.top,bottom:r.bottom,height:r.height,"
        "vh:window.innerHeight,"
        "fully: r.bottom<=window.innerHeight-10 && r.top>=0,"
        "radius:getComputedStyle(b).borderRadius};})()")
    check("PC bar: whole floating bar inside the viewport",
          bool(geo and geo["fully"]), str(geo))
    check("PC bar: pill shape on wide screens (rounded)",
          bool(geo and geo["radius"] not in ("0px", "")), str(geo))

    # ---- open Studio and start it ----
    host_b.ev("window.__liveStudio.openPanel()")
    time.sleep(0.4)
    txt = host_b.ev("document.getElementById('live-sheet-actions')"
                    ".innerText")
    for needle in ("Start Studio", "Facecam", "Face effects",
                   "green screen", "Picture on screen"):
        check(f"sheet: shows {needle!r}", needle in (txt or ""),
              (txt or "")[:200])
    host_b.ev("(function(){var bs=document.querySelectorAll("
              "'#live-sheet-actions button');"
              "for(var i=0;i<bs.length;i++){"
              " if(bs[i].textContent.indexOf('Start Studio')>=0){"
              "  bs[i].click(); return true;}} return false;})()")
    ok = wait_js(host_b,
                 "window.__liveStudio && __liveStudio.state().on"
                 " && __liveStudio.state().trackLive", 20)
    check("studio: starts — canvas track live", ok)
    st = host_b.ev("window.__liveStudio.state()")
    tile = host_b.ev(
        "(function(){var vs=document.querySelectorAll('video');"
        "for(var i=0;i<vs.length;i++){"
        " if(vs[i].srcObject && vs[i].videoWidth===1280)"
        "  return {w:vs[i].videoWidth,h:vs[i].videoHeight};}"
        "return null;})()")
    check("studio: own tile shows the 1280x720 Studio picture",
          bool(tile and tile["w"] == 1280), str(tile))

    # ---- facecam on (fake device doubles as the second camera) ----
    host_b.ev("window.__liveStudio.openPanel()")
    time.sleep(0.3)
    host_b.ev("(function(){var bs=document.querySelectorAll("
              "'#live-sheet-actions .studio-seg button');"
              "for(var i=0;i<bs.length;i++){"
              " if(bs[i].textContent.indexOf('Facecam on')>=0){"
              "  bs[i].click(); return true;}} return false;})()")
    ok = wait_js(host_b,
                 "__liveStudio.state().faceOn"
                 " && __liveStudio.state().faceLive", 20)
    check("studio: facecam turns on (second camera live)", ok)

    # ---- effect + green screen state via the sheet ----
    host_b.ev("(function(){var bs=document.querySelectorAll("
              "'#live-sheet-actions .studio-seg button');"
              "for(var i=0;i<bs.length;i++){"
              " var t=bs[i].textContent;"
              " if(t.indexOf('Vivid')>=0) bs[i].click();}"
              "return true;})()")
    time.sleep(0.3)
    st = host_b.ev("window.__liveStudio.state()")
    check("studio: effect picker lands (vivid)",
          bool(st and st["effect"] == "vivid"), str(st))
    host_b.ev("(function(){var bs=document.querySelectorAll("
              "'#live-sheet-actions .studio-seg button');"
              "for(var i=0;i<bs.length;i++){"
              " if(bs[i].textContent.indexOf('Green screen on')>=0){"
              "  bs[i].click(); return true;}} return false;})()")
    time.sleep(0.3)
    st = host_b.ev("window.__liveStudio.state()")
    check("studio: green screen turns on in state",
          bool(st and st["keyOn"]), str(st))
    check("studio: still on + track live after all the mixing",
          bool(st and st["on"] and st["trackLive"]), str(st))

    # ---- picture overlay via a real File on the sheet input ----
    host_b.ev(
        "(function(){var inp=document.querySelector("
        "'#live-sheet-actions input[type=file]');"
        "var c=document.createElement('canvas');"
        "c.width=80; c.height=50;"
        "var x=c.getContext('2d'); x.fillStyle='#ff0000';"
        "x.fillRect(0,0,80,50);"
        "return new Promise(function(res){ c.toBlob(function(b){"
        " var f=new File([b],'probe.png',{type:'image/png'});"
        " var dt=new DataTransfer(); dt.items.add(f);"
        " inp.files=dt.files;"
        " inp.dispatchEvent(new Event('change',{bubbles:true}));"
        " setTimeout(function(){"
        "  res(__liveStudio.state().hasPic); },600); }); });})()",
        await_promise=True)
    st = host_b.ev("window.__liveStudio.state()")
    check("studio: picture lands as an on-screen overlay",
          bool(st and st["hasPic"]), str(st))

    # ---- stop restores the plain camera ----
    host_b.ev("window.__liveStudio.stop()", await_promise=True)
    time.sleep(0.5)
    st = host_b.ev("window.__liveStudio.state()")
    check("studio: stop turns it off", bool(st and not st["on"]), str(st))
    plain = host_b.ev(
        "(function(){var me=document.querySelector('.live-tile')"
        "||null; var vs=document.querySelectorAll('video');"
        "for(var i=0;i<vs.length;i++){"
        " if(vs[i].srcObject) return vs[i].videoWidth;}"
        "return 0;})()")
    check("studio: own picture back to the raw camera (not 1280)",
          plain not in (1280, None), str(plain))
finally:
    if host_b:
        host_b.close()
    server.terminate()
    try:
        server.wait(timeout=10)
    except Exception:
        server.kill()

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
