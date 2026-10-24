"""Real two-browser WebRTC media test for the HERE live panel mesh.

Ken's report (2026-10-23): host went live on his phone, added people
to the panel — "I can't see them or hear them and they can't see or
hear me" (both directions, real phones).

Until now the live mesh client (templates/live.html) had only ever
passed scripted-HTTP signaling sims (tests/test_live_mesh_sim.py) —
no real browser ever negotiated media through it. This suite fixes
that: TWO headless Chromium processes (one per user, separate CDP
ports + separate Fetch relays/cookies, reusing the relay pattern
from tests/test_story_viewer_dom.py — this Chromium build refuses
direct loopback navigations, so all HTTP transport is proxied
through Python urllib with the page owner's session cookie), a real
uvicorn server, fake camera/mic devices, and the REAL live page on
both sides:

  phase 1a  host starts a live and opens it; guest opens it as a
            watcher -> guest must RECEIVE the host's video
            (panelist -> watcher offer path).
  phase 1b  guest asks to join the panel (real join-request +
            host approve over HTTP) -> both become panelists ->
            BOTH remote videos must play (panelist <-> panelist
            path, earlier-panelist-offers rule).
  phase 2   same flow on a fresh live, but the GUEST's
            getUserMedia is delayed 4s via a script injected before
            page load. The guest therefore hellos + answers the
            first offer with ZERO local tracks; when its media
            finally lands it must re-hello and the host must
            re-offer (with the guest's tracks riding the new
            answer), or the guest stays invisible forever — the
            S1 "hello lost while busy" race from the diagnosis.

Assertions use the page's own surface: tile <video> elements
(videoWidth / currentTime advance / srcObject tracks), the
window.__liveDiag diagnostics object, and the live_signals rows in
the DB (offer/answer/ICE actually exchanged for the pair).

SANDBOX NOTE (verified 2026-10-23 in the Muse VM): this sandbox
forbids UDP socket creation outright (Python socket() -> EPERM),
so Chromium's ICE connectivity checks can never complete HERE —
both pcs reach signaling 'stable' with offers/answers/ICE fully
exchanged and remote tracks attached, then ICE goes
checking -> disconnected -> failed (with exactly one bounded
restart). The media assertions below are kept STRICT on purpose:
on any host where UDP is permitted they must all pass; in this
sandbox the run demonstrates everything up to the ICE line and
fails only the pixel/connected checks. Do not soften them.

Run:  ./venv/bin/python tests/test_live_media_e2e.py
"""
import base64
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-livee2e-")
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


H, HID = signup("Host E2E", "host@e2e.io")
G, GID = signup("Guest E2E", "guest@e2e.io")
H_TOKEN = H.cookies.get("here_session")
G_TOKEN = G.cookies.get("here_session")
assert H_TOKEN and G_TOKEN, "missing session cookies"

PORT = 8951
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
             # Real IP host candidates (no mDNS .local names): this
             # VM has no mDNS responder, so hidden-IP candidates can
             # never resolve between the two test browsers. Phones on
             # a real LAN handle mDNS fine; the rig must not.
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

    # -- CDP plumbing (story-viewer pattern, per-browser instance) --
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
        # _send uses self.sess; for pre-attach calls sess is unset.
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


PROBE_JS = r"""
window.__probeLive = function () {
  var out = {diag: null, tiles: [], grid: !!document.getElementById('live-grid'),
             status: (document.getElementById('live-status') || {}).textContent || ''};
  try { out.diag = window.__liveDiag ? JSON.parse(JSON.stringify(window.__liveDiag)) : null; } catch (e) {}
  document.querySelectorAll('.live-tile').forEach(function (el) {
    var v = el.querySelector('video');
    var t = {hasVideo: el.classList.contains('has-video'), local: !!(v && v.classList.contains('localvid')),
             vw: v ? v.videoWidth : 0, ct: v ? v.currentTime : 0,
             ready: v ? v.readyState : -1, muted: v ? v.muted : null,
             paused: v ? v.paused : null, tracks: []};
    if (v && v.srcObject) {
      try { t.tracks = v.srcObject.getTracks().map(function (tr) { return tr.kind + ':' + tr.readyState; }); } catch (e) {}
    }
    out.tiles.push(t);
  });
  return out;
};
true;
"""


def remote_tile(probe):
    """The (single) remote tile's video stats, or None."""
    if not probe:
        return None
    for t in probe.get("tiles") or []:
        if not t["local"]:
            return t
    return None


def pump_probe(browser):
    # (Re)define the probe on every call: navigation wipes window.*
    # helpers, so a one-time injection before navigate() is useless.
    try:
        return browser.ev(PROBE_JS + "\n__probeLive()")
    except Exception:
        return None


ALL_BROWSERS = []


def wait_media(judged, seconds, label):
    """Pump EVERY browser (a starved browser's Fetch relay would
    freeze its page's signaling — the browsers not being judged
    still need their requests relayed), but only judge the REMOTE
    videos of the browsers in `judged`. Success = pixels on the
    remote tile AND its clock advancing. Returns (ok, probes)."""
    deadline = time.time() + seconds
    first_ct, last = {}, {}
    ok = {b.label: False for b in judged}
    while time.time() < deadline:
        for b in ALL_BROWSERS:
            p = pump_probe(b)
            if p:
                last[b.label] = p
            if b not in judged:
                continue
            rt = remote_tile(p)
            if rt and rt["vw"] > 0:
                if b.label not in first_ct:
                    first_ct[b.label] = rt["ct"]
                elif rt["ct"] >= first_ct[b.label] + 0.3:
                    ok[b.label] = True
        if all(ok.values()):
            return True, last
        time.sleep(0.15)
    return False, last


def sig_rows(lid):
    rows = dbq("SELECT id, from_id, to_id, kind, consumed FROM live_signals "
               "WHERE live_id=? ORDER BY id", (lid,))
    return [(r["id"], r["from_id"], r["to_id"], r["kind"], r["consumed"])
            for r in rows]


def sig_counts(lid):
    rows = dbq("SELECT from_id, to_id, kind, COUNT(*) c FROM live_signals "
               "WHERE live_id=? GROUP BY from_id, to_id, kind", (lid,))
    return {(r["from_id"], r["to_id"], r["kind"]): r["c"] for r in rows}


def start_live():
    r = H.post("/live/start", json={"title": "E2E media night"})
    assert r.status_code == 200 and r.json().get("ok"), r.text[:200]
    return r.json()["id"]


def guest_join_panel(lid):
    rr = G.post(f"/live/{lid}/heartbeat")
    assert rr.status_code == 200, rr.text[:200]
    rr = G.post(f"/live/{lid}/join-request")
    assert rr.json().get("ok"), rr.text[:200]
    rid = rr.json()["request"]["id"]
    rr = H.post(f"/live/{lid}/join-decide",
                json={"request_id": rid, "approve": True})
    assert rr.json().get("ok"), rr.text[:200]


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

    host_b = Browser("host", 8952, H_TOKEN)
    guest_b = Browser("guest", 8953, G_TOKEN)
    ALL_BROWSERS.extend([host_b, guest_b])

    # ---------------- phase 1a: watcher receives the host ------------
    LID = start_live()
    # Regression pin for the diagnosed root cause: the host is
    # panelist #1 by role but never a live_members row, and the
    # state payload once reported my_panel.am_member=False for the
    # host — the host's own client demoted itself to watcher, tore
    # down every peer and ignored all hellos (total blackout).
    st_h = H.get(f"/live/{LID}/state").json()
    check("server: host my_panel.am_member is True (host is always on the panel)",
          st_h.get("my_panel", {}).get("am_member") is True,
          str(st_h.get("my_panel")))
    host_b.navigate(f"/live/{LID}")
    guest_b.navigate(f"/live/{LID}")
    # Host local media up?
    ok_local = False
    end = time.time() + 20
    while time.time() < end and not ok_local:
        p = pump_probe(host_b)
        pump_probe(guest_b)
        if p:
            for t in p.get("tiles") or []:
                if t["local"] and t["vw"] > 0:
                    ok_local = True
        time.sleep(0.15)
    check("phase1: host local camera renders on its own tile", ok_local,
          str(pump_probe(host_b)))

    ok_w, probes = wait_media([guest_b], 25, "watcher")
    # host_b has no remote yet (guest is a watcher) — only guest judged.
    print("INFO phase1 watcher sig rows:", sig_rows(LID), flush=True)
    rt = remote_tile(probes.get("guest"))
    check("phase1 watcher: guest RECEIVES host video (pixels + clock advancing)",
          ok_w, f"guest remote tile: {rt} diag: {(probes.get('guest') or {}).get('diag')}")

    # ---------------- phase 1b: guest joins the panel ----------------
    guest_join_panel(LID)
    ok_p, probes = wait_media([host_b, guest_b], 45, "panel")
    print("INFO phase1 panel sig rows:", sig_rows(LID), flush=True)
    for lbl, b in (("host", host_b), ("guest", guest_b)):
        rt = remote_tile(probes.get(lbl))
        check(f"phase1 panel: {lbl} RECEIVES the other's video",
              ok_p and bool(rt) and rt["vw"] > 0, f"{lbl} remote tile: {rt}")
    sc = sig_counts(LID)
    check("phase1: offer host->guest exchanged",
          sc.get((HID, GID, "offer"), 0) >= 1, str(sc))
    check("phase1: answer guest->host exchanged",
          sc.get((GID, HID, "answer"), 0) >= 1, str(sc))
    check("phase1: ICE exchanged both ways",
          sc.get((HID, GID, "ice"), 0) >= 1
          and sc.get((GID, HID, "ice"), 0) >= 1, str(sc))
    for lbl, b in (("host", host_b), ("guest", guest_b)):
        p = probes.get(lbl) or pump_probe(b) or {}
        diag = p.get("diag")
        peer = None
        if diag and diag.get("peers"):
            vals = list(diag["peers"].values())
            peer = vals[0] if vals else None
        check(f"phase1: {lbl} __liveDiag reports connected peer with tracks",
              bool(peer) and peer.get("state") == "connected"
              and peer.get("hasRemoteTrack") and peer.get("hasLocalSenders"),
              f"diag={diag}")

    # ---------------- phase 2: slow guest getUserMedia (S1) ----------
    # Inject the delay BEFORE any page script of the next live runs.
    guest_b.cdp("Page.addScriptToEvaluateOnNewDocument", {"source": r"""
(function () {
  try {
    var md = navigator.mediaDevices;
    var orig = md.getUserMedia.bind(md);
    md.getUserMedia = function (c) {
      return new Promise(function (res, rej) {
        setTimeout(function () { orig(c).then(res, rej); }, 4000);
      });
    };
  } catch (e) {}
})();
"""})
    LID2 = start_live()  # starting a new live ends the first server-side
    host_b.navigate(f"/live/{LID2}")
    guest_b.navigate(f"/live/{LID2}")
    guest_join_panel(LID2)
    ok_s, probes2 = wait_media([host_b, guest_b], 50, "slow-media")
    print("INFO phase2 sig rows:", sig_rows(LID2), flush=True)
    for lbl in ("host", "guest"):
        rt = remote_tile(probes2.get(lbl))
        check(f"phase2 slow-gUM: {lbl} RECEIVES the other's video",
              ok_s and bool(rt) and rt["vw"] > 0,
              f"{lbl} remote tile: {rt} diag: {(probes2.get(lbl) or {}).get('diag')}")
    sc2 = sig_counts(LID2)
    check("phase2: at least TWO offers host->guest (initial + re-offer after media)",
          sc2.get((HID, GID, "offer"), 0) >= 2, str(sc2))
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
