"""Build W3 — video-path hardening for the audio-yes/video-no
failure.

Run:  ./venv/bin/python tests/test_live_w3.py

Ken's field report (2026-10-24): on a live with another person
he got their AUDIO but NO VIDEO. W3 hardens every code path
that can produce that symptom:

TIER 1 (TestClient) — the server-side video_ok plumbing:
  * panel rows carry video_ok, defaulting True for members and
    hosts who never reported (legacy clients);
  * the heartbeat — the member-state POST the client already
    makes — carries {video_ok} for the host (live_sessions.
    host_video_ok) and for panel members (live_members.video_ok),
    round-tripping through the state poll both ways;
  * a heartbeat with NO video_ok key (legacy) never clobbers a
    stored flag; a watcher's report is ignored (they publish
    nothing); the columns exist via the init_db migration.
  * the live page carries the W3 client machinery (markers) and
    its inline JS parses (node --check).

TIER 2 (real Chromium, the media-fix rig: fake media devices,
Fetch-relayed HTTP against one real server):
  * SENDER: the host's client verifies its fake camera
    (videoOk() true). Killing the track (stop() + the OS-level
    'ended' event a dead camera produces) flips videoOk() false,
    shows the persistent "📷 Your camera isn't sending" prompt,
    and the room's state poll reports video_ok=false. Tapping
    "Tap to retry camera" re-acquires and everything flips back.
    Only the 'ended' EVENT is simulated — the same category of
    input-faking as the media-fix suite's stubbed getStats; the
    verification, publishing, prompt and retry logic under test
    is the page's real code.
  * RECEIVER (flag): with the host's video_ok=false on the
    server, a watcher's page renders the host's tile as the
    avatar face with "📷 Camera off" (never a black box) and
    __liveDiag reports videoState 'camera-off'. Restoring the
    flag is proven at the payload level the renderer consumes.
  * RECEIVER (zero-frame rule): the host peer on the watcher's
    page is stubbed the way the media-fix stall phase stubs —
    scripted getStats (audio frames advancing, video frames
    pinned at 0), unmuted live receiver tracks, connectionState
    'connected', born past the grace window. The rule fires ONE
    fresh-peer rebuild charged to the shared W1 budget, the tile
    reads "📶 Reconnecting video…"; with the budget spent it
    refuses to rebuild and the tile/diag settle on the honest
    'zero-frames' / "📶 No video — tap 🔄 to retry"; advancing
    video frames then read 'flowing' with the real count.

Real frame flow across two phones stays the owner's two-phone
test — the sandbox cannot decode real remote frames, and this
suite does not pretend otherwise.
"""
import http.client  # noqa: F401  (rig parity with media_fix)
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{extra}]" if extra and not cond else ""), flush=True)


# ================================================================
# TIER 1 — server plumbing (TestClient)
# ================================================================
def tier1():
    import sqlite3
    tmp = tempfile.mkdtemp(prefix="here-test-w3-")
    os.environ["HERE_DB"] = os.path.join(tmp, "test.db")
    os.environ["HERE_UPLOADS"] = os.path.join(tmp, "uploads")
    import app as here
    from fastapi.testclient import TestClient

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
        uid = dbq("SELECT id FROM users WHERE email=?",
                  (email,))[0]["id"]
        return c, uid

    # Migration: both columns exist on a fresh DB (the CREATE
    # TABLEs predate them — init_db's ALTER path must add them).
    lm_cols = [r["name"] for r in dbq("PRAGMA table_info(live_members)")]
    ls_cols = [r["name"] for r in dbq("PRAGMA table_info(live_sessions)")]
    check("migration: live_members.video_ok column exists",
          "video_ok" in lm_cols)
    check("migration: live_sessions.host_video_ok column exists",
          "host_video_ok" in ls_cols)

    H, HID = signup("W3 Host", "w3h@x.io")
    G, GID = signup("W3 Guest", "w3g@x.io")
    W, WID = signup("W3 Watcher", "w3w@x.io")

    r = H.post("/live/start", json={"title": "W3 plumbing"})
    assert r.status_code == 200 and r.json().get("ok"), r.text[:200]
    LID = r.json()["id"]

    def panel(c):
        st = c.get(f"/live/{LID}/state").json()
        return {p["user_id"]: p for p in st["panel"]}, st

    p, _ = panel(G)
    check("default: host video_ok True (never reported)",
          p[HID]["video_ok"] is True)
    check("db: host_video_ok starts NULL (legacy = true)",
          dbq("SELECT host_video_ok v FROM live_sessions WHERE id=?",
              (LID,))[0]["v"] is None)

    r = H.post(f"/live/{LID}/heartbeat", json={"video_ok": False})
    check("host heartbeat {video_ok:false} accepted",
          r.status_code == 200 and r.json().get("ok"))
    p, _ = panel(G)
    check("host video_ok False round-trips through state",
          p[HID]["video_ok"] is False)
    check("db: host_video_ok persisted as 0",
          dbq("SELECT host_video_ok v FROM live_sessions WHERE id=?",
              (LID,))[0]["v"] == 0)

    H.post(f"/live/{LID}/heartbeat")          # no body at all
    p, _ = panel(G)
    check("legacy heartbeat (no body) does NOT clobber the flag",
          p[HID]["video_ok"] is False)
    H.post(f"/live/{LID}/heartbeat", json={})  # empty JSON object
    p, _ = panel(G)
    check("legacy heartbeat ({}) does NOT clobber the flag",
          p[HID]["video_ok"] is False)
    H.post(f"/live/{LID}/heartbeat", json={"video_ok": True})
    p, _ = panel(G)
    check("host video_ok True round-trips back", p[HID]["video_ok"] is True)

    # A guest joins the panel.
    rr = G.post(f"/live/{LID}/join-request")
    rid = rr.json()["request"]["id"]
    H.post(f"/live/{LID}/join-decide",
           json={"request_id": rid, "approve": True})
    p, _ = panel(H)
    check("default: member video_ok True (never reported)",
          p[GID]["video_ok"] is True)
    check("db: member video_ok starts NULL (legacy = true)",
          dbq("SELECT video_ok v FROM live_members WHERE live_id=?"
              " AND user_id=?", (LID, GID))[0]["v"] is None)
    G.post(f"/live/{LID}/heartbeat", json={"video_ok": False})
    p, _ = panel(H)
    check("member video_ok False round-trips through state",
          p[GID]["video_ok"] is False)
    p, _ = panel(G)
    check("member sees their own video_ok False too",
          p[GID]["video_ok"] is False)
    G.post(f"/live/{LID}/heartbeat")
    p, _ = panel(H)
    check("member legacy heartbeat keeps the stored False",
          p[GID]["video_ok"] is False)
    G.post(f"/live/{LID}/heartbeat", json={"video_ok": True})
    p, _ = panel(H)
    check("member video_ok True round-trips back",
          p[GID]["video_ok"] is True)

    # A watcher reporting video_ok changes nothing (they publish
    # nothing and have no member row).
    r = W.post(f"/live/{LID}/heartbeat", json={"video_ok": False})
    check("watcher heartbeat with video_ok is accepted, ignored",
          r.status_code == 200 and r.json().get("ok"))
    p, _ = panel(H)
    check("watcher report leaves the panel untouched",
          p[HID]["video_ok"] is True and p[GID]["video_ok"] is True
          and WID not in p)

    H.post(f"/live/{LID}/end")

    # ---------------- client machinery markers -----------------
    EVE, _EID = signup("W3 Eve", "w3e@x.io")
    r = EVE.post("/live/start", json={"title": "W3 markers"})
    lid2 = r.json()["id"]
    page = EVE.get(f"/live/{lid2}").text
    for marker in (
            'id="live-cam-note"', "Your camera isn",
            'id="live-cam-retry"', "Tap to retry camera",
            "checkMyVideo", "retryCamera", "publishVideoOk",
            "video_ok: myVideoOk", "scheduleVideoRecheck",
            "ZERO_FRAME_GRACE_MS", "evaluateZeroFrame",
            "fireVideoRebuild", "peerVideoState",
            "updateTileVideoState", "videoReconnectUntil",
            "videoFrames", "videoState",
            "📷 Camera off", "📶 Connecting video",
            "📶 Reconnecting video", "📶 No video",
            "ph-status"):
        check(f"W3 client marker present: {marker}", marker in page)
    check("zero-frame grace is ~8s (ZERO_FRAME_GRACE_MS = 8000)",
          "ZERO_FRAME_GRACE_MS = 8000" in page)
    check("visibility return re-plays remote tiles (W3)",
          "eachRemoteVideo(function(v){ guardPlayVideo(v); });"
          in page)
    EVE.post(f"/live/{lid2}/end")

    src = open(os.path.join(BASE, "templates", "live.html"),
               encoding="utf-8").read()
    scripts = re.findall(r"<script>(.*?)</script>", src, re.S)
    ok = True
    for i, s in enumerate(scripts):
        if not s.strip():
            continue
        pth = os.path.join(tmp, f"w3_{i}.js")
        open(pth, "w").write(
            re.sub(r"\{\{.*?\}\}", "0", s, flags=re.S))
        res = subprocess.run(["node", "--check", pth],
                             capture_output=True, text=True)
        if res.returncode != 0:
            ok = False
            print(res.stderr[:300])
    check("node --check: live.html inline JS parses", ok)


# ================================================================
# TIER 2 — browser rig (adapted from test_live_media_fix.py)
# ================================================================
PORT = 8973
ORIGIN = f"http://127.0.0.1:{PORT}"
CHROME = ("/opt/meta-chromium/chrome"
          if os.path.exists("/opt/meta-chromium/chrome")
          else shutil.which("chromium")
          or shutil.which("chromium-browser"))


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Browser:
    def __init__(self, label, origin, server_port, cookies):
        self.label = label
        self.origin = origin
        self.server_port = server_port
        self.token = (cookies or {}).get("here_session")
        self.port = free_port()
        self.proc = subprocess.Popen(
            [CHROME, "--headless=new", "--no-sandbox",
             "--disable-dev-shm-usage", "--disable-gpu",
             "--use-fake-device-for-media-stream",
             "--use-fake-ui-for-media-stream",
             "--disable-features=WebRtcHideLocalIpsWithMdns",
             "--autoplay-policy=no-user-gesture-required",
             f"--remote-debugging-port={self.port}",
             "--remote-debugging-address=127.0.0.1",
             "--remote-allow-origins=*",
             f"--user-data-dir={tempfile.mkdtemp(prefix='w3prof_')}",
             "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        from websockets.sync.client import connect as ws_connect
        version = None
        deadline = time.time() + 30
        while version is None:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{self.port}/json/version",
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
        target = self.cdp("Target.createTarget",
                          {"url": "about:blank"})
        att = self.cdp("Target.attachToTarget",
                       {"targetId": target["targetId"],
                        "flatten": True})
        self.sess = att["sessionId"]
        self.cdp("Page.enable")
        self.cdp("Runtime.enable")
        self.cdp("Fetch.enable",
                 {"patterns": [{"urlPattern": "*",
                                "requestStage": "Request"}]})

    def _send(self, method, params=None, session="__self__"):
        self._seq += 1
        msg = {"id": self._seq, "method": method,
               "params": params or {}}
        sess = self.sess if session == "__self__" else session
        if sess:
            msg["sessionId"] = sess
        self.ws.send(json.dumps(msg))
        return self._seq

    def _handle_paused(self, m):
        prm = m["params"]
        req = prm["request"]
        sess_id = m.get("sessionId")
        url = req["url"]
        try:
            headers = dict(req.get("headers") or {})
            if url.startswith(self.origin) and self.token:
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
                code, rheaders, rbody = (resp.status, resp.headers,
                                         resp.read())
            except urllib.error.HTTPError as e:
                code, rheaders, rbody = e.code, e.headers, e.read()
            out_headers = [{"name": k, "value": v}
                           for k, v in rheaders.items()
                           if k.lower() not in ("transfer-encoding",
                                                "connection",
                                                "content-length")]
            import base64
            self._send("Fetch.fulfillRequest",
                       {"requestId": prm["requestId"],
                        "responseCode": code,
                        "responseHeaders": out_headers,
                        "body": base64.b64encode(rbody).decode()},
                       session=sess_id)
        except Exception:
            try:
                self._send("Fetch.failRequest",
                           {"requestId": prm["requestId"],
                            "errorReason": "Failed"},
                           session=sess_id)
            except Exception:
                pass

    def _pump_until(self, mid, timeout=30):
        end = time.time() + timeout
        while time.time() < end:
            if mid in self._pending:
                return self._pending.pop(mid)
            try:
                raw = self.ws.recv(
                    timeout=max(0.1, end - time.time()))
            except TimeoutError:
                continue
            m = json.loads(raw)
            if "id" in m:
                self._pending[m["id"]] = m
            elif m.get("method") == "Fetch.requestPaused":
                self._handle_paused(m)
        raise TimeoutError(f"{self.label}: CDP response {mid}")

    def cdp(self, method, params=None, timeout=30):
        mid = self._send(method, params)
        m = self._pump_until(mid, timeout)
        if "error" in m:
            raise RuntimeError(f"CDP {method}: {m['error']}")
        return m.get("result", {})

    def pump(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            try:
                raw = self.ws.recv(
                    timeout=max(0.05, end - time.time()))
            except TimeoutError:
                break
            except Exception:
                break
            m = json.loads(raw)
            if "id" in m:
                self._pending[m["id"]] = m
            elif m.get("method") == "Fetch.requestPaused":
                self._handle_paused(m)

    def js(self, expr, await_promise=False):
        r = self.cdp("Runtime.evaluate",
                     {"expression": expr, "returnByValue": True,
                      "awaitPromise": await_promise})
        if r.get("exceptionDetails"):
            raise RuntimeError(
                f"JS error in {self.label}: "
                + json.dumps(r["exceptionDetails"])[:400])
        return r.get("result", {}).get("value")

    def navigate(self, path, settle=1.2):
        self.cdp("Page.navigate", {"url": self.origin + path})
        self.pump(settle)

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


def start_server(db, port):
    env = dict(os.environ)
    env["HERE_DB"] = db
    env["HERE_UPLOADS"] = tempfile.mkdtemp(prefix="here_w3_up_")
    env.pop("HERE_ICE_SERVERS", None)
    proc = subprocess.Popen(
        [os.path.join(BASE, "venv", "bin", "python"), "-m", "uvicorn",
         "app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=BASE, env=env, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 40
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/login",
                                        timeout=3) as r:
                if r.status == 200:
                    return proc
        except Exception:
            pass
        time.sleep(0.4)
    proc.terminate()
    raise RuntimeError("server did not come up")


def http_client(port):
    import http.cookiejar
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.port = port
    return opener


def req(opener, method, path, data=None, expect=None):
    url = f"http://127.0.0.1:{opener.port}" + path
    body = None
    headers = {}
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    r = urllib.request.Request(url, data=body, headers=headers,
                               method=method)
    try:
        resp = opener.open(r, timeout=20)
        code, payload = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        code, payload = e.code, e.read()
    if expect is not None and code != expect:
        raise AssertionError(f"{method} {path} -> {code}, want {expect}")
    return code, payload


def jreq(opener, path, obj, expect=None):
    url = f"http://127.0.0.1:{opener.port}" + path
    body = json.dumps(obj).encode()
    r = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    try:
        resp = opener.open(r, timeout=20)
        code, payload = resp.status, resp.read()
    except urllib.error.HTTPError as e:
        code, payload = e.code, e.read()
    if expect is not None and code != expect:
        raise AssertionError(f"POST {path} -> {code}, want {expect}")
    return code, payload


def json_of(payload):
    return json.loads(payload.decode())


def session_cookie(opener):
    import urllib.request as u
    for handler in opener.handlers:
        if isinstance(handler, u.HTTPCookieProcessor):
            for c in handler.cookiejar:
                if c.name == "here_session":
                    return {"here_session": c.value}
    raise RuntimeError("no session cookie")


def signup2(port, tag, name):
    op = http_client(port)
    email = f"w3{tag}@example.com"
    req(op, "POST", "/signup",
        {"name": name, "email": email,
         "password": "password123", "password2": "password123"},
        expect=200)
    code, payload = req(op, "GET", "/home", expect=200)
    m = re.search(rb'href="/user/(\d+)"', payload)
    return op, int(m.group(1)) if m else None


def wait_for(browsers, fn, timeout=20, label=""):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for b in browsers:
            b.pump(0.25)
        try:
            if fn():
                return True
        except Exception:
            pass
    print("  (wait_for timed out:", label, ")", flush=True)
    return False


def tier2():
    tmp = tempfile.mkdtemp(prefix="here_w3_")
    db = os.path.join(tmp, "test.db")
    shutil.copy(os.path.join(BASE, "here.db"), db)
    server = start_server(db, PORT)
    browsers = []
    try:
        host, host_id = signup2(PORT, "host", "W3 Host")
        guest, guest_id = signup2(PORT, "guest", "W3 Guest")
        code, payload = jreq(host, "/live/start",
                             {"title": "W3 Rig Live"}, expect=200)
        lid = json_of(payload).get("id")
        check("rig: host live started", bool(lid), f"id={lid}")

        def state_panel(op):
            _c, pl = req(op, "GET", f"/live/{lid}/state", expect=200)
            st = json_of(pl)
            return {p["user_id"]: p for p in st.get("panel", [])}

        # ---------- Phase S: sender camera truth ----------------
        hb = Browser("host", ORIGIN, PORT, session_cookie(host))
        browsers.append(hb)
        hb.navigate(f"/live/{lid}", settle=2.0)
        ok = wait_for([hb], lambda: hb.js(
            "window.__liveMedia && "
            "window.__liveMedia.videoOk() === true"), 25,
            "host videoOk true")
        check("sender: fake camera verifies live (videoOk true)", ok)
        tr = hb.js("var t = window.__liveMedia.localVideoTrack();"
                   "t ? {rs: t.readyState, kind: t.kind} : null")
        check("sender: a live local video track exists",
              bool(tr) and tr["rs"] == "live", json.dumps(tr))

        # The camera dies the way a real one does: the track ends
        # and the OS-level 'ended' event fires. (stop() alone does
        # not dispatch 'ended' in the spec — a yanked device does —
        # so the event is dispatched to the page's own listener;
        # everything downstream is the page's real logic.)
        hb.js("var t = window.__liveMedia.localVideoTrack();"
              "t.stop(); t.dispatchEvent(new Event('ended')); 'ok'")
        ok = wait_for([hb], lambda: hb.js(
            "window.__liveMedia.videoOk() === false"), 12,
            "host videoOk false after camera death")
        check("sender: dead camera flips videoOk false", ok)
        note = hb.js("var n = document.getElementById('live-cam-note');"
                     "({hidden: n.hidden, text: n.textContent"
                     ".replace(/\\s+/g, ' ').trim()})")
        check("sender: persistent cam-note prompt is showing",
              note and note["hidden"] is False
              and "camera isn" in note["text"]
              and "Tap to retry camera" in note["text"],
              json.dumps(note))
        p = state_panel(guest)
        check("sender: room state now reports host video_ok False",
              p[host_id]["video_ok"] is False, json.dumps(p))

        hb.js("document.getElementById('live-cam-retry').click(); 'ok'")
        ok = wait_for([hb], lambda: hb.js(
            "window.__liveMedia.videoOk() === true"), 15,
            "host videoOk true after retry")
        check("sender: retry camera re-acquires + verifies", ok)
        ok = wait_for([hb], lambda: hb.js(
            "document.getElementById('live-cam-note').hidden === true"),
            8, "cam-note hides after retry")
        check("sender: cam-note hides once the camera sends", ok)
        p = state_panel(guest)
        check("sender: room state reports host video_ok True again",
              p[host_id]["video_ok"] is True)

        # ---------- Phase R: receiver renders the flag ----------
        gb = Browser("guest", ORIGIN, PORT, session_cookie(guest))
        browsers.append(gb)
        gb.navigate(f"/live/{lid}", settle=1.5)
        ok = wait_for([hb, gb], lambda: gb.js(
            f"!!(window.__liveMedia.peers[{host_id}])"), 25,
            "guest holds a peer to the host")
        check("receiver: watcher page holds a peer to the host", ok)

        jreq(host, f"/live/{lid}/heartbeat", {"video_ok": False},
             expect=200)
        ok = wait_for([hb, gb], lambda: gb.js(
            f"var d = window.__liveDiag.peers[{host_id}];"
            "d && d.videoState === 'camera-off'"), 20,
            "guest diag camera-off")
        check("receiver: diag videoState 'camera-off' from the flag",
              ok)
        tile = gb.js(
            "var out = null;"
            "document.querySelectorAll('.live-tile')"
            ".forEach(function(t){"
            "  var lb = t.querySelector('.tile-label');"
            "  if (lb && lb.textContent.indexOf('W3 Host') !== -1){"
            "    var ps = t.querySelector('.ph-status');"
            "    out = {status: ps ? ps.textContent : null,"
            "           hasVideo: t.classList.contains('has-video')};"
            "  }}); out")
        check("receiver: host tile is the avatar face (no has-video)",
              tile and tile["hasVideo"] is False, json.dumps(tile))
        check("receiver: host tile reads '📷 Camera off'",
              tile and tile["status"] == "📷 Camera off",
              json.dumps(tile))
        jreq(host, f"/live/{lid}/heartbeat", {"video_ok": True},
             expect=200)
        fetch_js = (
            "fetch('/live/" + str(lid) + "/state', "
            "{credentials: 'same-origin'})"
            ".then(r => r.json())"
            ".then(d => (d.panel.find("
            "p => p.user_id === " + str(host_id) + ") || {})"
            ".video_ok === true)")
        ok = wait_for([gb], lambda: gb.js(
            fetch_js, await_promise=True), 15,
            "guest page sees restored flag")
        check("receiver: restored flag reaches the watcher's client",
              ok)

        # ---------- Phase Z: the zero-frame rule ----------------
        stub = f"""
          var P = window.__liveMedia.peers[{host_id}];
          window.__zf = {{v: 0, a: 100}};
          function stubPeer(P){{
            P.pc.getStats = function(){{
              return Promise.resolve(new Map([
                ['v', {{type: 'inbound-rtp', kind: 'video',
                        framesDecoded: window.__zf.v}}],
                ['a', {{type: 'inbound-rtp', kind: 'audio',
                        framesDecoded: window.__zf.a}}]]));
            }};
            P.pc.getReceivers = function(){{ return [
              {{track: {{kind: 'video', readyState: 'live',
                         muted: false}}}},
              {{track: {{kind: 'audio', readyState: 'live',
                         muted: false}}}}]; }};
            Object.defineProperty(P.pc, 'connectionState',
              {{configurable: true,
                get: function(){{ return 'connected'; }}}});
            P.bornAt = Date.now() - 20000;
            P.lastRestartAt = 0;
          }}
          stubPeer(P);
          window.__zfP1 = P;
          window.__liveMedia.restartCounts[{host_id}] = 0;
          'stubbed'"""
        r = gb.js(stub)
        check("zero-frame setup: peer stubbed (audio flows, "
              "video at 0, past grace)", r == "stubbed", str(r))
        # The stubbed audio counter must ADVANCE from wherever
        # the real one left off (real audio framesDecoded may
        # already be in the thousands — a fixed small number
        # would read as "not advancing").
        gb.js(f"var P = window.__liveMedia.peers[{host_id}];"
              "window.__zf.a = (P.audioLastFrames || 0) + 160;"
              "window.__liveMedia.sampleStats(); 'ok'")
        ok = wait_for([hb, gb], lambda: gb.js(
            f"window.__liveMedia.restartCounts[{host_id}] === 1"
            f" && window.__liveMedia.peers[{host_id}]"
            " !== window.__zfP1"), 15, "zero-frame rebuild fired")
        check("zero-frame: ONE fresh-peer rebuild fired, charged "
              "to the shared budget", ok)
        diag = gb.js(f"window.__liveDiag.peers[{host_id}]")
        check("zero-frame: diag reads 'reconnecting' after the "
              "rebuild", diag and diag["videoState"] == "reconnecting",
              json.dumps(diag))
        tile = gb.js(
            "var out = null;"
            "document.querySelectorAll('.live-tile')"
            ".forEach(function(t){"
            "  var lb = t.querySelector('.tile-label');"
            "  if (lb && lb.textContent.indexOf('W3 Host') !== -1){"
            "    var ps = t.querySelector('.ph-status');"
            "    out = ps ? ps.textContent : null; }}); out")
        check("zero-frame: tile reads '📶 Reconnecting video…'",
              tile == "📶 Reconnecting video…", str(tile))

        # Budget spent: same signature, no second rebuild, and
        # the tile/diag settle on the honest end state.
        gb.js(f"""
          var P2 = window.__liveMedia.peers[{host_id}];
          window.__zfP2 = P2;
          P2.pc.getStats = function(){{
            return Promise.resolve(new Map([
              ['v', {{type: 'inbound-rtp', kind: 'video',
                      framesDecoded: window.__zf.v}}],
              ['a', {{type: 'inbound-rtp', kind: 'audio',
                      framesDecoded: window.__zf.a}}]]));
          }};
          P2.pc.getReceivers = function(){{ return [
            {{track: {{kind: 'video', readyState: 'live',
                       muted: false}}}},
            {{track: {{kind: 'audio', readyState: 'live',
                       muted: false}}}}]; }};
          Object.defineProperty(P2.pc, 'connectionState',
            {{configurable: true,
              get: function(){{ return 'connected'; }}}});
          P2.bornAt = Date.now() - 20000;
          P2.lastRestartAt = 0;
          P2.flowSince = 0;
          window.__liveMedia.restartCounts[{host_id}] = 5;
          window.__zf.a = (P2.audioLastFrames || 0) + 260;
          window.__liveMedia.sampleStats(); 'ok'""")
        gb.pump(1.2)
        hb.pump(0.4)
        same = gb.js(
            f"window.__liveMedia.peers[{host_id}] === window.__zfP2")
        cnt = gb.js(f"window.__liveMedia.restartCounts[{host_id}]")
        check("zero-frame: budget spent -> NO further rebuild",
              same is True and cnt == 5,
              f"same={same} count={cnt}")
        # Keep the stubbed audio ADVANCING while waiting (in
        # production the working audio track is what keeps this
        # state alive — a frozen stub counter would decay the
        # "audio flowing" premise the state is computed from).
        settled = False
        deadline = time.time() + 25
        while time.time() < deadline:
            gb.js(f"var P4 = window.__liveMedia.peers[{host_id}];"
                  "window.__zf.a = (P4.audioLastFrames || 0) + 300;"
                  "window.__liveMedia.sampleStats(); 'ok'")
            gb.pump(0.6)
            hb.pump(0.2)
            d = gb.js(f"window.__liveDiag.peers[{host_id}]")
            if d and d["videoState"] == "zero-frames":
                settled = True
                break
        check("zero-frame: diag settles on 'zero-frames'", settled)
        tile = gb.js(
            "var out = null;"
            "document.querySelectorAll('.live-tile')"
            ".forEach(function(t){"
            "  var lb = t.querySelector('.tile-label');"
            "  if (lb && lb.textContent.indexOf('W3 Host') !== -1){"
            "    var ps = t.querySelector('.ph-status');"
            "    out = ps ? ps.textContent : null; }}); out")
        check("zero-frame: tile settles on the honest retry state",
              tile == "📶 No video — tap 🔄 to retry", str(tile))

        # Frames finally flow: state + counter tell the truth.
        gb.js("window.__zf.v = 500;"
              "window.__liveMedia.sampleStats(); 'ok'")
        gb.pump(0.5)
        gb.js(f"var P3 = window.__liveMedia.peers[{host_id}];"
              "window.__zf.v = 560;"
              "window.__zf.a = (P3.audioLastFrames || 0) + 60;"
              "window.__liveMedia.sampleStats(); 'ok'")
        ok = wait_for([hb, gb], lambda: gb.js(
            f"var d = window.__liveDiag.peers[{host_id}];"
            "d && d.videoState === 'flowing'"
            " && d.videoFrames === 560"), 12, "diag flowing")
        check("zero-frame: advancing frames read 'flowing' with "
              "the decoded count", ok)
        # ---------- Phase S: zero-frame SLOW path ---------------
        # Ken's PC guest (Oct 27): mic silent, so the audio-proof
        # fast path never armed and the black tile sat forever.
        # Same never-flowed signature with NO audio flow must
        # still earn ONE budget rebuild once the peer is 30s old.
        gb.js(f"""
          var PS = window.__liveMedia.peers[{host_id}];
          window.__zfS = PS;
          PS.pc.getStats = function(){{
            return Promise.resolve(new Map([
              ['v', {{type: 'inbound-rtp', kind: 'video',
                      framesDecoded: 0}}],
              ['a', {{type: 'inbound-rtp', kind: 'audio',
                      framesDecoded: (PS.audioLastFrames || 0)}}]]));
          }};
          PS.pc.getReceivers = function(){{ return [
            {{track: {{kind: 'video', readyState: 'live',
                       muted: false}}}},
            {{track: {{kind: 'audio', readyState: 'live',
                       muted: false}}}}]; }};
          Object.defineProperty(PS.pc, 'connectionState',
            {{configurable: true,
              get: function(){{ return 'connected'; }}}});
          PS.bornAt = Date.now() - 31000;
          PS.lastRestartAt = 0;
          PS.flowSince = 0;
          PS.audioStatsSeen = true;
          PS.audioAdvanceAt = Date.now() - 20000;
          window.__liveMedia.restartCounts[{host_id}] = 0;
          window.__liveMedia.sampleStats(); 'ok'""")
        ok = wait_for([hb, gb], lambda: gb.js(
            f"window.__liveMedia.restartCounts[{host_id}] === 1"
            f" && window.__liveMedia.peers[{host_id}]"
            " !== window.__zfS"), 15, "slow-path rebuild fired")
        check("slow path: mic-silent zero-frame peer earns ONE "
              "rebuild at 30s", ok)

        # ---------- Phase V: the self-view watchdog -------------
        # Ken (Oct 27): "I can't see myself" — the self tile sat
        # black with the avatar hidden. Sabotage the host's tile
        # with an empty stream: the watchdog must call it dark,
        # show the honest status, and rebind the outgoing stream.
        tile = hb.js(
            "var out = null;"
            "document.querySelectorAll('.live-tile')"
            ".forEach(function(t){"
            "  var vid = t.querySelector("
            "'video.localvid, video.localvid-nomirror');"
            "  if (vid){"
            "    vid.srcObject = new MediaStream();"
            "    window.__liveSelfView.sample();"
            "    window.__liveSelfView.sample();"
            "    window.__liveSelfView.sample();"
            "    var ps = t.querySelector('.ph-status');"
            "    out = {dark: window.__liveSelfView.view.dark,"
            "      hasVideo: t.classList.contains('has-video'),"
            "      status: ps ? ps.textContent : null}; }}); out")
        check("self-view: a dead tile reads dark, drops the "
              "has-video face, and speaks up",
              tile and tile["dark"] >= 2
              and tile["hasVideo"] is False
              and tile["status"]
              and "not showing" in tile["status"],
              json.dumps(tile))
        recovered = False
        deadline = time.time() + 20
        while time.time() < deadline:
            hb.js("window.__liveSelfView.sample(); 'ok'")
            hb.pump(0.5)
            st = hb.js(
                "var out = null;"
                "document.querySelectorAll('.live-tile')"
                ".forEach(function(t){"
                "  if (t.querySelector("
                "'video.localvid, video.localvid-nomirror')){"
                "    out = {dark: window.__liveSelfView.view.dark,"
                "      hasVideo: t.classList"
                ".contains('has-video')}; }}); out")
            if st and st["dark"] == 0 and st["hasVideo"]:
                recovered = True
                break
        check("self-view: the watchdog rebinds the outgoing "
              "picture and the tile recovers", recovered)
        # ---------- Phase T: tile truth (the audience fix) ------
        # Ken's phone-as-viewer (Oct 28): the picture is judged by
        # the TILE ITSELF now. Sabotage the host tile on the guest
        # with a live track that never paints a frame: the sampler
        # must call it dark, fire ONE budgeted rebuild, and put
        # the honest face up.
        c0 = gb.js(f"window.__liveMedia.restartCounts[{host_id}] || 0")
        gb.js(f"var P = window.__liveMedia.peers[{host_id}];"
              "if (P) P.lastRestartAt = 0; 'ok'")
        tile = gb.js(
            "var out = null;"
            "document.querySelectorAll('.live-tile')"
            ".forEach(function(t){"
            "  var lb = t.querySelector('.tile-label');"
            "  if (lb && lb.textContent.indexOf('W3 Host') !== -1){"
            "    var vid = t.querySelector('video');"
            "    var cv = document.createElement('canvas');"
            "    cv.width = 64; cv.height = 64;"
            "    vid.srcObject = cv.captureStream(0);"
            "    window.__liveTiles.sample();"
            "    window.__liveTiles.sample();"
            "    window.__liveTiles.sample();"
            "    var ps = t.querySelector('.ph-status');"
            "    out = {dark: (window.__liveTiles.truth["
            + str(host_id) + "] || {}).dark || 0,"
            "      hasVideo: t.classList.contains('has-video'),"
            "      status: ps ? ps.textContent : null}; }}); out")
        check("tile truth: a bound-but-dead tile is called dark "
              "by its own pixels", tile and tile["dark"] >= 3
              and tile["hasVideo"] is False
              and tile["status"] and "No video" in tile["status"],
              json.dumps(tile))
        ok = wait_for([hb, gb], lambda: gb.js(
            f"(window.__liveMedia.restartCounts[{host_id}] || 0)"
            f" === {c0} + 1"), 15, "tile-truth rebuild fired")
        check("tile truth: exactly one budgeted rebuild fired", ok)

        # ---------- Phase U: ranks + spotlight on real tiles ----
        # The rig's guest has been a watcher; seat them first.
        _c, pl = jreq(guest, f"/live/{lid}/join-request", {},
                      expect=200)
        rid = json_of(pl)["request"]["id"]
        jreq(host, f"/live/{lid}/join-decide",
             {"request_id": rid, "approve": True}, expect=200)
        jreq(host, f"/live/{lid}/rank",
             {"user_id": guest_id, "rank": "regular"}, expect=200)
        ok = wait_for([hb], lambda: hb.js(
            "var out = false;"
            "document.querySelectorAll('.live-tile')"
            ".forEach(function(t){"
            "  var lb = t.querySelector('.tile-label');"
            "  if (lb && lb.textContent.indexOf('W3 Guest') !== -1"
            "      && lb.textContent.indexOf('⭐') !== -1)"
            "    out = true; }); out"), 15, "rank star on tile")
        check("talk-show: a Regular wears the star on their tile",
              ok)
        grip = hb.js(
            "var g = document.querySelector('.tile-grip');"
            "g ? !g.hidden : null")
        check("talk-show: the host sees drag grips on the blocks",
              grip is True)
        jreq(host, f"/live/{lid}/spotlight",
             {"user_id": guest_id}, expect=200)
        ok = wait_for([hb, gb], lambda: gb.js(
            "var g = document.querySelector('.live-grid');"
            "var s = document.querySelector('.live-tile.spot-tile');"
            "!!(g && g.className.indexOf('has-spotlight') !== -1"
            "  && s)"), 15, "spotlight painted")
        check("talk-show: spotlight takes the big stage spot on "
              "a viewer's screen too", ok)
        jreq(host, f"/live/{lid}/spotlight",
             {"user_id": None}, expect=200)
    finally:
        for b in browsers:
            b.close()
        server.terminate()


def main():
    tier1()
    tier2()
    passed = sum(1 for _, ok in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} passed", flush=True)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
