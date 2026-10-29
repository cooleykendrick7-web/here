"""Live Connect suite (Build L): capture-card / screen source
picker on the live page (templates/live.html, client-only).

Static: the 🎮 Live Connect button + every load-bearing string of
the feature on a TestClient-rendered live page, and node --check
on all inline script blocks.

DOM: a real headless Chromium host whose mediaDevices are mocked
via Page.addScriptToEvaluateOnNewDocument BEFORE navigation
(pattern from tests/test_live_media_e2e.py — Fetch-relayed HTTP,
fake-device donor stream). The mock enumerateDevices reports a
FaceTime camera, a "USB Video Capture" capture card, a built-in
mic and a headset mic; the mock getUserMedia records its
(JSON-cloned) constraints, hands out clones of a real donor
stream (patched getSettings so an exact-constrained request
reports the requested deviceId, like a real browser), and can
reject once on demand. Proves: join, sheet rows, video/audio
switching via replaceTrack bookkeeping, mute-state carry, mirror
rules, screen share + 'ended' restore, picker-cancel silence,
busy-device toast, devicechange fallback, localStorage
persistence across lives, and the stale-saved-id join fallback.

Two-page: a plain fake-device guest proves the host's peer
senders actually get the NEW track after a switch (sender ids
from window.__liveConnect.senders()).

Run:  ./venv/bin/python tests/test_live_connect.py
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
_tmp = tempfile.mkdtemp(prefix="here-test-liveconn-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond), extra))
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{extra}]" if extra else ""), flush=True)


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
    assert r.status_code == 303, (name, r.status_code, r.text[:200])
    uid = dbq("SELECT id FROM users WHERE email=?", (email,))[0]["id"]
    return c, uid


H, HID = signup("Connect Host", "host@lc.io")
G, GID = signup("Connect Guest", "guest@lc.io")
H_TOKEN = H.cookies.get("here_session")
G_TOKEN = G.cookies.get("here_session")
assert H_TOKEN and G_TOKEN, "missing session cookies"

# ================= static: rendered page =================
SLID = H.post("/live/start", json={"title": "Static LC"}).json()["id"]
page = H.get(f"/live/{SLID}").text

m = re.search(r'<button[^>]*id="ctl-connect"[^>]*>(.*?)</button>',
              page, re.S)
check("static: ctl-connect button present with 🎮 Live Connect text",
      bool(m) and "🎮 Live Connect" in m.group(1), (m.group(0)[:120] if m else "missing"))
check("static: ctl-connect uses the live-ctl glass class (38px bar)",
      bool(m) and 'class="live-ctl"' in m.group(0))
check("static: ctl-connect sits right after ctl-flip",
      page.index('id="ctl-flip"') < page.index('id="ctl-connect"')
      < page.index('id="ctl-panel-leave"'))
for needle, label in [
        ("here_lc_video", "localStorage video key"),
        ("here_lc_audio", "localStorage audio key"),
        ("getDisplayMedia", "getDisplayMedia used"),
        ("co-streaming? Plug in your capture card", "capture-card helper line"),
        ("Sound: screen", "screen-audio sheet state"),
        ("Screen share ended — you're back on your camera.",
         "screen-ended restore toast"),
        ("That device is busy", "busy-device toast"),
        ("switched you back", "devicechange fallback toast"),
        ("replaceTrack", "replaceTrack switching"),
        ("A saved camera/mic wasn't found", "stale saved-id toast"),
        ("__liveConnect", "diagnostics surface")]:
    check(f"static: {label} present", needle in page)
check("static: localvid-nomirror CSS present",
      ".localvid-nomirror" in page and "transform:none" in page)
check("static: lc-src-tag CSS present", ".lc-src-tag" in page)

blocks = re.findall(r"<script>(.*?)</script>", page, re.S)
all_ok, bad = True, []
for i, block in enumerate(blocks):
    path = os.path.join(_tmp, f"chk_lc_{i}.js")
    with open(path, "w") as f:
        f.write(block)
    p = subprocess.run(["node", "--check", path],
                       capture_output=True, text=True)
    if p.returncode != 0:
        all_ok = False
        bad.append(f"#{i}: {p.stderr.strip()[:160]}")
check("node --check: live page inline JS parses", all_ok, "; ".join(bad))

PORT = 8991
ORIGIN = f"http://127.0.0.1:{PORT}"


# ---------------- one Chromium per user, Fetch-relayed ----------------
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


# ---------------- the mediaDevices mock (host only) ----------------
MOCK_JS = r"""
(function () {
  try {
    var md = navigator.mediaDevices;
    if (!md || !md.getUserMedia) return;
    var realGUM = md.getUserMedia.bind(md);
    var donorP = null;
    function donor() {
      if (!donorP) donorP = realGUM({audio: true, video: true});
      return donorP;
    }
    window.__lcMock = {
      devices: [
        {deviceId: 'cam-front', kind: 'videoinput', label: 'FaceTime HD Camera'},
        {deviceId: 'usb-cap', kind: 'videoinput', label: 'USB Video Capture'},
        {deviceId: 'mic-builtin', kind: 'audioinput', label: 'Built-in Microphone'},
        {deviceId: 'mic-headset', kind: 'audioinput', label: 'Headset Mic'}
      ],
      gumCalls: [],
      gumError: null,
      displayError: null,
      displayAudio: false,
      displayCalls: 0,
      lastDisplay: null
    };
    function findDev(kind, id) {
      var ds = window.__lcMock.devices;
      for (var i = 0; i < ds.length; i++)
        if (ds[i].kind === kind && ds[i].deviceId === id) return ds[i];
      return null;
    }
    function firstDev(kind) {
      var ds = window.__lcMock.devices;
      for (var i = 0; i < ds.length; i++)
        if (ds[i].kind === kind) return ds[i];
      return null;
    }
    function requestedId(c, key) {
      var cst = c ? c[key] : null;
      if (!cst || cst === true) return null;
      if (cst.deviceId) {
        if (typeof cst.deviceId === 'string') return cst.deviceId;
        if (cst.deviceId.exact) return cst.deviceId.exact;
      }
      return null;
    }
    function patch(tr, dev) {
      if (!tr || !dev) return tr;
      try {
        tr.getSettings = function () { return {deviceId: dev.deviceId}; };
      } catch (e) {}
      try {
        Object.defineProperty(tr, 'label',
          {value: dev.label, configurable: true});
      } catch (e) {}
      return tr;
    }
    md.getUserMedia = function (c) {
      window.__lcMock.gumCalls.push(
        JSON.parse(JSON.stringify(c || {})));
      try {
        var ff = localStorage.getItem('__lcMockFailFirst');
        if (ff) {
          localStorage.removeItem('__lcMockFailFirst');
          return Promise.reject({name: ff});
        }
      } catch (e) {}
      if (window.__lcMock.gumError) {
        var ge = window.__lcMock.gumError;
        window.__lcMock.gumError = null;
        return Promise.reject(ge);
      }
      return donor().then(function (ds) {
        var tracks = [];
        if (c && c.audio) {
          var aId = requestedId(c, 'audio');
          var aDev = (aId && findDev('audioinput', aId))
            || (aId ? {deviceId: aId, label: ''} : firstDev('audioinput'));
          var aD = ds.getAudioTracks()[0];
          if (aD) tracks.push(patch(aD.clone(), aDev));
        }
        if (c && c.video) {
          var vId = requestedId(c, 'video');
          var vDev = (vId && findDev('videoinput', vId))
            || (vId ? {deviceId: vId, label: ''} : firstDev('videoinput'));
          var vD = ds.getVideoTracks()[0];
          if (vD) tracks.push(patch(vD.clone(), vDev));
        }
        return new MediaStream(tracks);
      });
    };
    md.enumerateDevices = function () {
      return Promise.resolve(window.__lcMock.devices.map(function (d) {
        return {deviceId: d.deviceId, kind: d.kind, label: d.label,
                groupId: 'g1'};
      }));
    };
    md.getDisplayMedia = function (c) {
      window.__lcMock.displayCalls++;
      if (window.__lcMock.displayError) {
        var de = window.__lcMock.displayError;
        window.__lcMock.displayError = null;
        return Promise.reject(de);
      }
      return donor().then(function (ds) {
        var tracks = [];
        var vD = ds.getVideoTracks()[0];
        if (vD) tracks.push(vD.clone());
        if (window.__lcMock.displayAudio) {
          var aD = ds.getAudioTracks()[0];
          if (aD) tracks.push(aD.clone());
        }
        var s = new MediaStream(tracks);
        window.__lcMock.lastDisplay = s;
        return s;
      });
    };
  } catch (e) {}
})();
"""


ALL_BROWSERS = []


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


def state(b):
    try:
        return b.ev("window.__liveConnect ? __liveConnect.state() : null") or {}
    except Exception:
        return {}


def local_tracks(b):
    try:
        return b.ev("window.__liveConnect ? __liveConnect.localTracks() : []") or []
    except Exception:
        return []


def track_of(tracks, kind):
    for t in tracks or []:
        if t.get("kind") == kind:
            return t
    return None


def click_row(b, needle):
    expr = ("(function(){var bs=document.querySelectorAll("
            "'#live-sheet-actions button');"
            "for(var i=0;i<bs.length;i++){"
            "if(bs[i].textContent.indexOf(" + json.dumps(needle)
            + ")>=0){bs[i].click();return true;}}return false;})()")
    return b.ev(expr)


def sheet_text(b):
    return b.ev("document.getElementById('live-sheet-actions').textContent") or ""


def toast_text(b):
    return b.ev("document.getElementById('live-toast').textContent") or ""


def guest_join_panel(lid):
    rr = G.post(f"/live/{lid}/heartbeat")
    assert rr.status_code == 200, rr.text[:200]
    rr = G.post(f"/live/{lid}/join-request")
    assert rr.json().get("ok"), rr.text[:200]
    rid = rr.json()["request"]["id"]
    rr = H.post(f"/live/{lid}/join-decide",
                json={"request_id": rid, "approve": True})
    assert rr.json().get("ok"), rr.text[:200]


def start_live():
    r = H.post("/live/start", json={"title": "LC night"})
    assert r.status_code == 200 and r.json().get("ok"), r.text[:200]
    return r.json()["id"]


env = dict(os.environ)
server = subprocess.Popen(
    [sys.executable, "-m", "uvicorn", "app:app",
     "--host", "127.0.0.1", "--port", str(PORT)],
    cwd=BASE, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    text=True)
host_b = guest_b = None
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

    host_b = Browser("host", 8992, H_TOKEN)
    guest_b = Browser("guest", 8993, G_TOKEN)
    ALL_BROWSERS.extend([host_b, guest_b])
    host_b.cdp("Page.addScriptToEvaluateOnNewDocument",
               {"source": MOCK_JS})

    # ================= host flow on live #1 =================
    L1 = start_live()
    host_b.navigate(f"/live/{L1}")
    ok = wait_js(host_b,
                 "window.__liveConnect && __liveConnect.localTracks()"
                 ".some(function(t){return t.kind==='video'})",
                 25, others=[guest_b])
    check("boot: host local video track live under the mock", ok,
          str(local_tracks(host_b)))
    check("boot: local tile video has srcObject",
          bool(host_b.ev("!!(document.querySelector('.live-tile video')"
                         "||{}).srcObject")))
    check("boot: ctl-connect NOT hidden for a panel member",
          host_b.ev("document.getElementById('ctl-connect').hidden") is False)
    check("boot: controls live in the slim left rail (Ken's "
          "redesign): ctl-connect is a stacked rail button and "
          "the bar itself is ~60px wide",
          host_b.ev("parseFloat(getComputedStyle(document."
                    "getElementById('ctl-connect')).height)") >= 40
          and host_b.ev("parseFloat(getComputedStyle(document."
                        "getElementById('live-controls')).width)")
          <= 64,
          str(host_b.ev("getComputedStyle(document.getElementById"
                        "('ctl-connect')).height")))

    # ---- open the Live Connect sheet ----
    host_b.ev("document.getElementById('ctl-connect').click(); true")
    ok = wait_js(host_b,
                 "!document.getElementById('live-sheet').hidden"
                 " && document.getElementById('live-sheet-actions')"
                 ".textContent.indexOf('USB Video Capture')>=0",
                 15, others=[guest_b])
    check("sheet: opens and lists the capture card by name", ok,
          sheet_text(host_b)[:200])
    txt = sheet_text(host_b)
    for needle in ("FaceTime HD Camera", "Built-in Microphone",
                   "Headset Mic", "Share my screen", "co-streaming? Plug in your capture card"):
        check(f"sheet: row/content '{needle}' present", needle in txt)
    rows = host_b.ev("Array.from(document.querySelectorAll("
                     "'#live-sheet-actions button'))"
                     ".map(function(b){return b.textContent})") or []
    check("sheet: the default camera row is the active one (●)",
          any("FaceTime HD Camera" in t and "●" in t for t in rows),
          str(rows))

    # ---- video switch to the capture card ----
    host_b.ev("window.__oldVidTrack = document.querySelector("
              "'.live-tile video').srcObject.getVideoTracks()[0]; true")
    old_vid = track_of(local_tracks(host_b), "video") or {}
    check("video switch: USB row clickable",
          bool(click_row(host_b, "USB Video Capture")))
    ok = wait_js(host_b,
                 "window.__liveConnect && __liveConnect.state()"
                 ".videoId==='usb-cap'", 20, others=[guest_b])
    check("video switch: state().videoId is usb-cap", ok, str(state(host_b)))
    new_vid = track_of(local_tracks(host_b), "video") or {}
    check("video switch: local video track id changed",
          bool(new_vid.get("id")) and new_vid.get("id") != old_vid.get("id"),
          f"{old_vid} -> {new_vid}")
    check("video switch: old video track ended",
          host_b.ev("window.__oldVidTrack.readyState") == "ended")
    st = state(host_b)
    check("mirror: capture card is NOT mirrored (state)",
          st.get("mirror") is False, str(st))
    check("mirror: own video lost .localvid, gained .localvid-nomirror",
          host_b.ev("document.querySelector('.live-tile video')"
                    ".classList.contains('localvid')") is False
          and host_b.ev("document.querySelector('.live-tile video')"
                        ".classList.contains('localvid-nomirror')") is True)
    label_txt = host_b.ev("document.querySelector('.live-tile .tile-label')"
                          ".textContent") or ""
    check("tag: own tile shows '🎮 Live Connect'",
          "🎮 Live Connect" in label_txt, label_txt)
    check("video switch: localStorage here_lc_video saved",
          host_b.ev("localStorage.getItem('here_lc_video')") == "usb-cap")

    # ---- mirror toggle ----
    host_b.ev("document.querySelector('#live-sheet-actions "
              "input[type=checkbox]').click(); true")
    pump_all(0.6)
    check("mirror toggle: state().mirror back on",
          state(host_b).get("mirror") is True, str(state(host_b)))
    check("mirror toggle: own video regains .localvid",
          host_b.ev("document.querySelector('.live-tile video')"
                    ".classList.contains('localvid')") is True)

    # ---- audio switch ----
    host_b.ev("window.__oldAudTrack = document.querySelector("
              "'.live-tile video').srcObject.getAudioTracks()[0]; true")
    check("audio switch: Headset Mic row clickable",
          bool(click_row(host_b, "Headset Mic")))
    ok = wait_js(host_b,
                 "window.__liveConnect && __liveConnect.state()"
                 ".audioId==='mic-headset'", 20, others=[guest_b])
    check("audio switch: state().audioId is mic-headset", ok,
          str(state(host_b)))
    check("audio switch: localStorage here_lc_audio saved",
          host_b.ev("localStorage.getItem('here_lc_audio')") == "mic-headset")
    check("audio switch: old audio track ended",
          host_b.ev("window.__oldAudTrack.readyState") == "ended")

    # ---- mute/camera-off state carries onto a switched track ----
    host_b.ev("document.getElementById('ctl-cam').click(); true")
    pump_all(0.4)
    check("mute carry: camera off before switch (track disabled)",
          (track_of(local_tracks(host_b), "video") or {}).get("enabled") is False)
    check("mute carry: FaceTime row clickable",
          bool(click_row(host_b, "FaceTime HD Camera")))
    ok = wait_js(host_b,
                 "window.__liveConnect && __liveConnect.state()"
                 ".videoId==='cam-front'", 20, others=[guest_b])
    check("mute carry: switched back to cam-front", ok, str(state(host_b)))
    check("mute carry: NEW video track is disabled (camera-off carried)",
          (track_of(local_tracks(host_b), "video") or {}).get("enabled") is False,
          str(local_tracks(host_b)))
    host_b.ev("document.getElementById('ctl-cam').click(); true")
    pump_all(0.4)
    check("mute carry: camera back on re-enables the track",
          (track_of(local_tracks(host_b), "video") or {}).get("enabled") is True)

    # ---- screen share (with screen audio) + ended restore ----
    host_b.ev("window.__lcMock.displayAudio = true; true")
    check("screen: Share-my-screen row clickable",
          bool(click_row(host_b, "Share my screen")))
    ok = wait_js(host_b,
                 "window.__liveConnect && __liveConnect.state()"
                 ".videoKind==='screen'", 20, others=[guest_b])
    check("screen: state().videoKind is screen", ok, str(state(host_b)))
    check("screen: local video track IS the display track",
          host_b.ev("window.__liveConnect.localTracks()"
                    ".filter(function(t){return t.kind==='video'})[0].id"
                    " === window.__lcMock.lastDisplay"
                    ".getVideoTracks()[0].id") is True)
    check("screen: state().screenAudio is true",
          state(host_b).get("screenAudio") is True, str(state(host_b)))
    label_txt = host_b.ev("document.querySelector('.live-tile .tile-label')"
                          ".textContent") or ""
    check("screen: own tile tag shows '🖥 Screen'",
          "🖥 Screen" in label_txt, label_txt)
    host_b.ev("window.__lcMock.lastDisplay.getVideoTracks()[0]"
              ".dispatchEvent(new Event('ended')); true")
    ok = wait_js(host_b,
                 "window.__liveConnect && __liveConnect.state()"
                 ".videoKind==='camera' && __liveConnect.state()"
                 ".videoId==='cam-front' && __liveConnect.state()"
                 ".audioId==='mic-headset'", 25, others=[guest_b])
    check("screen ended: camera + mic restored (cam-front / mic-headset)",
          ok, str(state(host_b)))
    check("screen ended: toast says you're back on your camera",
          "Screen share ended" in toast_text(host_b), toast_text(host_b))

    # ---- picker cancel is a silent no-op ----
    before_vid = (track_of(local_tracks(host_b), "video") or {}).get("id")
    host_b.ev("window.__lcMock.displayError = {name: 'NotAllowedError'}; true")
    click_row(host_b, "Share my screen")
    pump_all(1.5)
    st = state(host_b)
    after_vid = (track_of(local_tracks(host_b), "video") or {}).get("id")
    check("picker cancel: still on camera, video track unchanged",
          st.get("videoKind") == "camera" and after_vid == before_vid,
          f"{st} {before_vid} -> {after_vid}")

    # ---- busy device error ----
    host_b.ev("window.__lcMock.gumError = {name: 'NotReadableError'}; true")
    click_row(host_b, "USB Video Capture")
    pump_all(1.5)
    check("busy device: toast says the device is busy",
          "busy" in toast_text(host_b), toast_text(host_b))
    check("busy device: state unchanged (still cam-front)",
          state(host_b).get("videoId") == "cam-front", str(state(host_b)))

    # ---- devicechange: active camera unplugged ----
    check("devicechange: switch to usb-cap first",
          bool(click_row(host_b, "USB Video Capture")))
    ok = wait_js(host_b,
                 "window.__liveConnect && __liveConnect.state()"
                 ".videoId==='usb-cap'", 20, others=[guest_b])
    check("devicechange: on usb-cap before unplug", ok, str(state(host_b)))
    host_b.ev("window.__lcMock.devices = window.__lcMock.devices"
              ".filter(function(d){return d.deviceId!=='usb-cap'}); true")
    host_b.ev("navigator.mediaDevices.dispatchEvent("
              "new Event('devicechange')); true")
    ok = wait_js(host_b,
                 "window.__liveConnect && __liveConnect.state()"
                 ".videoId && __liveConnect.state().videoId!=='usb-cap'",
                 20, others=[guest_b])
    check("devicechange: fell back off the unplugged camera", ok,
          str(state(host_b)))
    check("devicechange: toast says the camera went away",
          "went away" in toast_text(host_b), toast_text(host_b))

    # ================= two-page sender check =================
    guest_b.navigate(f"/live/{L1}")
    pump_all(2.5)
    check("guest: ctl-connect hidden for a watcher (not a member)",
          guest_b.ev("document.getElementById('ctl-connect').hidden") is True)
    guest_join_panel(L1)

    def host_senders():
        try:
            return host_b.ev("window.__liveConnect"
                             " ? __liveConnect.senders() : {}") or {}
        except Exception:
            return {}

    ok = False
    end = time.time() + 40
    while time.time() < end and not ok:
        s = host_senders()
        if (s.get(str(GID)) or {}).get("video"):
            ok = True
        try:
            guest_b.ev("1")
        except Exception:
            pass
        time.sleep(0.15)
    check("senders: host has a video sender for the guest", ok,
          str(host_senders()))
    old_sender_vid = (host_senders().get(str(GID)) or {}).get("video")
    old_local_vid = (track_of(local_tracks(host_b), "video") or {}).get("id")
    host_b.ev("window.__liveConnect.switchVideo("
              "'usb-cap', 'USB Video Capture'); true")
    ok = wait_js(host_b,
                 "window.__liveConnect && __liveConnect.state()"
                 ".videoId==='usb-cap'", 20, others=[guest_b])
    check("senders: host switched to usb-cap for the sender check", ok,
          str(state(host_b)))
    new_local_vid = (track_of(local_tracks(host_b), "video") or {}).get("id")
    ok = False
    end = time.time() + 30
    while time.time() < end and not ok:
        s = host_senders()
        if (s.get(str(GID)) or {}).get("video") == new_local_vid \
                and new_local_vid != old_local_vid:
            ok = True
        try:
            guest_b.ev("1")
        except Exception:
            pass
        time.sleep(0.15)
    check("senders: guest's sender now carries the NEW local track",
          ok, f"old sender={old_sender_vid} old local={old_local_vid} "
              f"new local={new_local_vid} senders={host_senders()}")
    check("senders: sender track id actually changed",
          (host_senders().get(str(GID)) or {}).get("video") != old_sender_vid,
          str(host_senders()))

    # ================= persistence across lives =================
    check("persist: localStorage holds the chosen devices",
          host_b.ev("localStorage.getItem('here_lc_video')") == "usb-cap"
          and host_b.ev("localStorage.getItem('here_lc_audio')") == "mic-headset",
          str(host_b.ev("[localStorage.getItem('here_lc_video'),"
                        " localStorage.getItem('here_lc_audio')]")))
    L2 = start_live()
    host_b.navigate(f"/live/{L2}")
    ok = wait_js(host_b,
                 "window.__lcMock && __lcMock.gumCalls.length>=1"
                 " && window.__liveConnect && __liveConnect.localTracks()"
                 ".some(function(t){return t.kind==='video'})",
                 25, others=[guest_b])
    check("persist: live #2 boots with a local video", ok,
          str(local_tracks(host_b)))
    calls = host_b.ev("window.__lcMock.gumCalls") or []
    first = calls[0] if calls else {}
    check("persist: live #2 FIRST gUM used the saved video device",
          ((first.get("video") or {}).get("deviceId") or {}).get("exact")
          == "usb-cap", json.dumps(first))
    check("persist: live #2 FIRST gUM used the saved audio device",
          ((first.get("audio") or {}).get("deviceId") or {}).get("exact")
          == "mic-headset", json.dumps(first))

    # ---- stale saved id: join must fall back, never block ----
    host_b.ev("localStorage.setItem('here_lc_video', 'gone-cam');"
              " localStorage.setItem('__lcMockFailFirst', 'NotFoundError');"
              " true")
    L3 = start_live()
    host_b.navigate(f"/live/{L3}")
    ok = wait_js(host_b,
                 "window.__lcMock && __lcMock.gumCalls.length>=2"
                 " && window.__liveConnect && __liveConnect.localTracks()"
                 ".some(function(t){return t.kind==='video'})",
                 25, others=[guest_b])
    check("stale id: live #3 still boots with a local video", ok,
          str(local_tracks(host_b)))
    calls = host_b.ev("window.__lcMock.gumCalls") or []
    check("stale id: a fallback retry happened (2+ gUM calls)",
          len(calls) >= 2, json.dumps(calls)[:300])
    check("stale id: the retry does NOT mention the gone camera",
          len(calls) >= 2 and "gone-cam" not in json.dumps(calls[1]),
          json.dumps(calls)[:300])
    check("stale id: state().videoKind is camera after fallback",
          state(host_b).get("videoKind") == "camera", str(state(host_b)))
finally:
    for b in (guest_b, host_b):
        if b is not None:
            b.close()
    try:
        server.terminate()
        server.wait(timeout=10)
    except Exception:
        try:
            server.kill()
        except Exception:
            pass

fails = [n for n, ok_, _ in RESULTS if not ok_]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL GREEN")
