"""Studio suite (Build STUDIO + fix round): 🎬 Studio on the
live page — game + facecam multitask compositing, face effects,
green screen, picture-on-screen, and the PC bottom-bar fix.

Round 2 (Ken: "none of the effects actually work"): the first
suite proved plumbing (state, tracks, tiles) but never looked at
a single PIXEL. This one does. The host's mediaDevices are
MOCKED with deterministic donor canvases — main camera = solid
blue; facecam = green field with a red disc — so the outgoing
Studio canvas can be sampled pixel-by-pixel to prove: the main
picture flows, the face effect (Noir) visibly desaturates, the
Safari compatibility filter path (per-pixel, ctx.filter absent)
does the same, the facecam box shows the face donor, the green
screen keys the green away to the main picture, a shared
picture lands on screen, and the Studio sheet does NOT rebuild
itself in a loop (the render->enumerate->render bug that made
every button in the sheet feel dead).

Static: buttons, PC bar rules, no lcEnumerate inside
renderStudioSheet, compat-filter machinery, node --check.

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
        ("studioFilterOK", "ctx.filter capability detection"),
        ("studioPixelFilter", "per-pixel compat filters"),
        ("studioWorked", "compat person-layer pass"),
        ("isn't supported in this browser", "honest start-failure toast"),
        ("Studio could not start", "start exception toast"),
        ("Compatibility mode on this browser", "sheet compat note"),
]:
    check(f"static: {label} present", needle in page)
check("static: faceOn render no longer enumerates unconditionally (loop fix)",
      "if (studio.faceOn){\n      lcEnumerate" not in page
      and "if (studio.faceOn){\n      (lcDevices" in page)
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

MOCK_JS = r"""
(function(){
  try {
    var md = navigator.mediaDevices;
    if (!md) return;
    function donor(w, h, paint){
      var c = document.createElement('canvas');
      c.width = w; c.height = h;
      paint(c.getContext('2d'), w, h);
      return c;
    }
    function paintMain(x, w, h){
      x.fillStyle = 'rgb(10,40,220)'; x.fillRect(0, 0, w, h);
    }
    function paintFace(x, w, h){
      x.fillStyle = 'rgb(0,200,0)'; x.fillRect(0, 0, w, h);
      x.fillStyle = 'rgb(230,30,30)';
      x.beginPath(); x.arc(w / 2, h / 2, 110, 0, 7); x.fill();
    }
    var mainC = donor(640, 480, paintMain);
    var faceC = donor(640, 480, paintFace);
    setInterval(function(){
      paintMain(mainC.getContext('2d'), 640, 480);
      paintFace(faceC.getContext('2d'), 640, 480);
    }, 120);
    var gumCalls = 0, actx = null;
    function vtrack(canvas, id){
      var tr = canvas.captureStream(30).getVideoTracks()[0];
      tr.getSettings = function(){
        return {deviceId: id, width: canvas.width,
                height: canvas.height};
      };
      return tr;
    }
    function atrack(){
      try {
        actx = actx || new (window.AudioContext
                            || window.webkitAudioContext)();
        return actx.createMediaStreamDestination()
          .stream.getAudioTracks()[0];
      } catch (e) { return null; }
    }
    md.getUserMedia = function(cons){
      gumCalls++;
      var tracks = [];
      if (cons && cons.audio){
        var a = atrack();
        if (a) tracks.push(a);
      }
      if (cons && cons.video){
        var first = gumCalls === 1;
        tracks.push(vtrack(first ? mainC : faceC,
                           first ? 'cam-main' : 'cam-face'));
      }
      return Promise.resolve(new MediaStream(tracks));
    };
    md.enumerateDevices = function(){
      return Promise.resolve([
        {deviceId: 'cam-main', kind: 'videoinput',
         label: 'Main Cam', groupId: 'g1'},
        {deviceId: 'cam-face', kind:'videoinput',
         label: 'Face Cam', groupId: 'g1'},
        {deviceId: 'mic-1', kind: 'audioinput',
         label: 'Desk Mic', groupId: 'g1'}
      ]);
    };
  } catch (e) {}
})();
"""

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

# ================= DOM: Chromium host, mocked donors =================
env = dict(os.environ)
server = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app:app",
     "--host", "127.0.0.1", "--port", str(PORT)],
    cwd=BASE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True)
host_b = None


def px(b, x, y):
    return b.ev(
        "(function(){var c=__liveStudio.canvasEl();"
        "var t=document.createElement('canvas');"
        "t.width=1280;t.height=720;"
        "var g=t.getContext('2d'); g.drawImage(c,0,0);"
        "var d=g.getImageData(" + str(x) + "," + str(y) + ",1,1).data;"
        "return [d[0],d[1],d[2]];})()")


def click_text(b, sel, needle):
    return b.ev(
        "(function(){var bs=document.querySelectorAll('" + sel + "');"
        "for(var i=0;i<bs.length;i++){"
        " if(bs[i].textContent.indexOf('" + needle + "')>=0){"
        "  bs[i].click(); return true;}} return false;})()")


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
    host_b.cdp("Page.addScriptToEvaluateOnNewDocument",
               {"source": MOCK_JS})
    LID = H.post("/live/start", json={"title": "Studio DOM"}).json()["id"]
    host_b.navigate(f"/live/{LID}")
    ok = wait_js(host_b,
                 "window.__liveConnect && __liveConnect.localTracks()"
                 ".some(function(t){return t.kind==='video'})", 25)
    check("boot: host local video live (mocked main camera)", ok)
    ok = wait_js(host_b,
                 "var b=document.getElementById('ctl-studio');"
                 "!!b && !b.hidden", 15)
    check("boot: 🎬 Studio button visible for the host", ok)

    # ---- PC bar geometry (wide viewport) ----
    host_b.cdp("Emulation.setDeviceMetricsOverride",
               {"width": 1400, "height": 900, "deviceScaleFactor": 1,
                "mobile": False})
    time.sleep(0.4)
    geo = host_b.ev(
        "(function(){var b=document.getElementById('live-controls');"
        "var r=b.getBoundingClientRect();"
        "return {fully: r.bottom<=window.innerHeight-10 && r.top>=0,"
        "radius:getComputedStyle(b).borderRadius};})()")
    check("PC bar: whole floating bar inside the viewport",
          bool(geo and geo["fully"]), str(geo))
    check("PC bar: pill shape on wide screens (rounded)",
          bool(geo and geo["radius"] not in ("0px", "")), str(geo))

    # ---- start Studio; the main picture must FLOW (pixels) ----
    host_b.ev("window.__liveStudio.openPanel()")
    time.sleep(0.4)
    txt = host_b.ev("document.getElementById('live-sheet-actions')"
                    ".innerText")
    for needle in ("Start Studio", "Facecam", "Face effects",
                   "green screen", "Picture on screen"):
        check(f"sheet: shows {needle!r}", needle in (txt or ""),
              (txt or "")[:200])
    click_text(host_b, "#live-sheet-actions button", "Start Studio")
    ok = wait_js(host_b,
                 "window.__liveStudio && __liveStudio.state().on"
                 " && __liveStudio.state().trackLive", 20)
    check("studio: starts — canvas track live", ok)
    time.sleep(0.8)
    c = px(host_b, 640, 360)
    check("pixels: main camera (blue) flows into the Studio picture",
          bool(c and c[2] > 150 and c[2] - c[0] > 100), str(c))
    tile = host_b.ev(
        "(function(){var vs=document.querySelectorAll('video');"
        "for(var i=0;i<vs.length;i++){"
        " if(vs[i].srcObject && vs[i].videoWidth===1280)"
        "  return vs[i].videoWidth;} return 0;})()")
    check("studio: own tile shows the 1280-wide Studio picture",
          tile == 1280, str(tile))

    # ---- face effect, NATIVE filter path: Noir desaturates ----
    click_text(host_b, "#live-sheet-actions .studio-seg button", "Noir")
    time.sleep(0.6)
    c = px(host_b, 640, 360)
    check("pixels: Noir effect visibly desaturates the picture",
          bool(c and sum(c) > 45 and max(c) - min(c) < 30), str(c))
    click_text(host_b, "#live-sheet-actions .studio-seg button", "None")
    time.sleep(0.6)
    c = px(host_b, 640, 360)
    check("pixels: effect off restores the blue",
          bool(c and c[2] > 150 and c[2] - c[0] > 100), str(c))

    # ---- face effect, SAFARI COMPAT path (per-pixel filters) ----
    host_b.ev("window.__liveStudio.forceCompat()")
    st = host_b.ev("window.__liveStudio.state()")
    check("compat: filterOK reports false once forced",
          bool(st and st["filterOK"] is False), str(st))
    click_text(host_b, "#live-sheet-actions .studio-seg button", "Noir")
    time.sleep(0.8)
    c = px(host_b, 640, 360)
    check("pixels: Noir still desaturates via the compat path",
          bool(c and sum(c) > 45 and max(c) - min(c) < 30), str(c))
    click_text(host_b, "#live-sheet-actions .studio-seg button", "None")
    time.sleep(0.6)

    # ---- picture on screen (real File through the sheet input) ----
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
    time.sleep(0.5)
    c = px(host_b, 186, 131)
    check("pixels: the shared picture is ON the picture (red, top-left)",
          bool(c and c[0] > 150 and c[0] - c[2] > 80), str(c))
    click_text(host_b, "#live-sheet-actions button",
               "Take the picture down")
    time.sleep(0.4)
    st = host_b.ev("window.__liveStudio.state()")
    check("studio: picture removes cleanly",
          bool(st and not st["hasPic"]), str(st))

    # ---- facecam: box shows the face donor (red disc / green field) ----
    click_text(host_b, "#live-sheet-actions .studio-seg button",
               "Facecam on")
    ok = wait_js(host_b,
                 "__liveStudio.state().faceOn"
                 " && __liveStudio.state().faceLive", 20)
    check("studio: facecam turns on (second camera live)", ok)
    time.sleep(0.6)
    click_text(host_b, "#live-sheet-actions .cmenu-item", "Face Cam")
    time.sleep(0.8)
    c = px(host_b, 1086, 568)
    check("pixels: facecam box center shows the face (red disc)",
          bool(c and c[0] > 150 and c[0] - c[1] > 80), str(c))
    c = px(host_b, 945, 470)
    check("pixels: facecam box corner shows the green field (pre-key)",
          bool(c and c[1] > 120 and c[1] - c[0] > 60), str(c))

    # ---- the sheet must NOT rebuild itself in a loop ----
    muts = host_b.ev(
        "new Promise(function(res){"
        " var n=0;"
        " var o=new MutationObserver(function(m){ n+=m.length; });"
        " o.observe(document.getElementById('live-sheet-actions'),"
        "  {childList:true, subtree:true, characterData:true});"
        " setTimeout(function(){ o.disconnect(); res(n); }, 1200);})",
        await_promise=True)
    check("sheet: no rebuild loop while facecam is on (<10 mutations)",
          bool(muts is not None and muts < 10), str(muts))

    # ---- green screen: green keys away to the game, face stays ----
    click_text(host_b, "#live-sheet-actions .studio-seg button",
               "Green screen on")
    time.sleep(0.9)
    st = host_b.ev("window.__liveStudio.state()")
    check("studio: green screen on in state",
          bool(st and st["keyOn"]), str(st))
    c = px(host_b, 945, 470)
    check("pixels: green field keyed away — main (blue) shows through",
          bool(c and c[2] > 120 and c[2] - c[0] > 60), str(c))
    c = px(host_b, 1086, 568)
    check("pixels: the face itself survives the key (still red)",
          bool(c and c[0] > 150 and c[0] - c[1] > 80), str(c))

    # ---- stop restores the plain camera ----
    host_b.ev("window.__liveStudio.stop()", await_promise=True)
    time.sleep(0.5)
    st = host_b.ev("window.__liveStudio.state()")
    check("studio: stop turns it off", bool(st and not st["on"]), str(st))
    ok = wait_js(host_b,
                 "(function(){var vs=document.querySelectorAll('video');"
                 "for(var i=0;i<vs.length;i++){"
                 " if(vs[i].srcObject && vs[i].videoWidth===640)"
                 "  return true;} return false;})()",
                 15)
    check("studio: own picture back to the raw camera (640 donor)",
          ok)
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
