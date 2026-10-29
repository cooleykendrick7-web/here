"""Unit checks for the unified WebRTC ICE config (GET /ice.json).

Run:  ./venv/bin/python tests/test_live_ice_config.py

Covers: /ice.json returns STUN + turn: URLs by default; the
HERE_ICE_SERVERS env override is honored when valid JSON and
ignored (defaults served) when invalid; both WebRTC client
templates (live mesh + 1:1 calls) fetch /ice.json and keep the
hardcoded fallback literal.
"""
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-ice-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
os.environ.pop("HERE_ICE_SERVERS", None)
sys.path.insert(0, BASE)

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{extra}]" if extra and not cond else ""))


C = TestClient(here.app)

r = C.get("/ice.json")
check("/ice.json returns 200", r.status_code == 200, str(r.status_code))
servers = (r.json() or {}).get("iceServers") or []
urls = []
for s in servers:
    u = s.get("urls")
    urls.extend(u if isinstance(u, list) else [u])
check("/ice.json default includes stun: URLs",
      any(str(u).startswith("stun:") for u in urls), str(urls))
check("/ice.json default includes turn: URLs (Open Relay)",
      any(str(u).startswith("turn:") for u in urls)
      and any("openrelay" in str(u) for u in urls), str(urls))
# Build W1: the full documented Open Relay set — the relay paths
# that survive strict phone / captive networks.
check("W1: default includes stun:openrelay.metered.ca:80",
      "stun:openrelay.metered.ca:80" in urls, str(urls))
check("W1: default includes turn:openrelay.metered.ca:3478 (UDP)",
      "turn:openrelay.metered.ca:3478" in urls, str(urls))
check("W1: default includes turns: TLS on 443/tcp",
      "turns:openrelay.metered.ca:443?transport=tcp" in urls,
      str(urls))
check("W1: pre-W1 entries kept (turn :80, :443, :443/tcp)",
      all(u in urls for u in (
          "turn:openrelay.metered.ca:80",
          "turn:openrelay.metered.ca:443",
          "turn:openrelay.metered.ca:443?transport=tcp")), str(urls))
check("get_ice_servers() matches the endpoint payload",
      here.get_ice_servers() == servers)

custom = [{"urls": "stun:stun.example.org:3478"},
          {"urls": ["turn:turn.example.org:3478"],
           "username": "u1", "credential": "p1"}]
os.environ["HERE_ICE_SERVERS"] = json.dumps(custom)
try:
    r2 = C.get("/ice.json")
    check("env override: /ice.json serves HERE_ICE_SERVERS verbatim",
          (r2.json() or {}).get("iceServers") == custom,
          r2.text[:200])
finally:
    os.environ.pop("HERE_ICE_SERVERS", None)

os.environ["HERE_ICE_SERVERS"] = "{not valid json"
try:
    r3 = C.get("/ice.json")
    s3 = (r3.json() or {}).get("iceServers") or []
    check("env override: invalid JSON falls back to defaults",
          any("openrelay" in json.dumps(s) for s in s3), r3.text[:200])
finally:
    os.environ.pop("HERE_ICE_SERVERS", None)

os.environ["HERE_ICE_SERVERS"] = json.dumps([{"no_urls": 1}, "junk"])
try:
    r4 = C.get("/ice.json")
    s4 = (r4.json() or {}).get("iceServers") or []
    check("env override: entries without urls are dropped -> defaults",
          any("openrelay" in json.dumps(s) for s in s4), r4.text[:200])
finally:
    os.environ.pop("HERE_ICE_SERVERS", None)

for tpl in ("live.html", "base.html"):
    with open(os.path.join(BASE, "templates", tpl)) as f:
        text = f.read()
    check(f"{tpl} fetches /ice.json for its ICE config",
          "/ice.json" in text and "getIceConfig" in text)
    check(f"{tpl} keeps the hardcoded ICE fallback literal",
          "turn:openrelay.metered.ca:443" in text
          and "ICE_CONFIG" in text)
    check(f"W1: {tpl} fallback carries the turns: TLS entry",
          "turns:openrelay.metered.ca:443?transport=tcp" in text)
    check(f"W1: {tpl} fallback carries turn :3478 + openrelay stun",
          "turn:openrelay.metered.ca:3478" in text
          and "stun:openrelay.metered.ca:80" in text)

# ---- Cloudflare TURN layer (tournament fix, Oct 29) ----
os.environ["HERE_CF_TURN_KEY_ID"] = "key123"
os.environ["HERE_CF_TURN_TOKEN"] = "tok456"
_cf_calls = {"n": 0}


def _fake_fetch(key_id, token):
    _cf_calls["n"] += 1
    assert key_id == "key123" and token == "tok456"
    # The real generate-ice-servers shape: a LIST — CF STUN
    # plus the TURN entry carrying the ephemeral credential.
    return [{"urls": ["stun:stun.cloudflare.com:3478"]},
            {"urls": ["turn:turn.cloudflare.com:3478?transport=udp",
                      "turns:turn.cloudflare.com:443?transport=tcp"],
             "username": "cf-user", "credential": "cf-cred"}]


_orig_fetch = here._cf_turn_fetch
here._cf_turn_fetch = _fake_fetch
here._CF_ICE_CACHE["servers"] = None
here._CF_ICE_CACHE["exp"] = 0.0
r = C.get("/ice.json")
servers = (r.json() or {}).get("iceServers") or []
urls = []
for s in servers:
    u = s.get("urls")
    urls.extend(u if isinstance(u, list) else [u])
check("CF: /ice.json serves Cloudflare TURN when keys are set",
      any("turn.cloudflare.com" in str(u) for u in urls),
      str(urls))
check("CF: Open Relay gone while Cloudflare answers",
      not any("openrelay" in str(u) for u in urls), str(urls))
r = C.get("/ice.json")
check("CF: credentials cached, not re-minted per request",
      _cf_calls["n"] == 1, str(_cf_calls))


def _boom(key_id, token):
    raise RuntimeError("network down")


here._cf_turn_fetch = _boom
here._CF_ICE_CACHE["servers"] = None
here._CF_ICE_CACHE["exp"] = 0.0
r = C.get("/ice.json")
servers = (r.json() or {}).get("iceServers") or []
urls = []
for s in servers:
    u = s.get("urls")
    urls.extend(u if isinstance(u, list) else [u])
check("CF: mint failure falls back to Open Relay defaults",
      any("openrelay" in str(u) for u in urls), str(urls))
here._cf_turn_fetch = _orig_fetch
os.environ.pop("HERE_CF_TURN_KEY_ID", None)
os.environ.pop("HERE_CF_TURN_TOKEN", None)
here._CF_ICE_CACHE["servers"] = None
here._CF_ICE_CACHE["exp"] = 0.0

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL GREEN")
