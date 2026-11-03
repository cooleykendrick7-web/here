"""Growth trio suite (Ken's 'make HERE better' build, Nov 3, 2026):
(1) guest live preview, (2) Invite Race home share strip,
(3) image compression on upload.

Run:  ./venv/bin/python tests/test_growth_trio.py
"""
import io
import os
import sqlite3
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-trio-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image  # noqa: E402

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


def dbx(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"], timeout=10)
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def signup(name, email):
    c = TestClient(here.app, follow_redirects=False)
    r = c.post("/signup", data={"name": name, "email": email,
                                "password": "pw123456"})
    assert r.status_code == 303, (name, r.status_code)
    uid = dbq("SELECT id FROM users WHERE email=?", (email,))[0]["id"]
    return c, uid


# ================= (3) image compression =================
noise = Image.effect_noise((2600, 1700), 100).convert("RGB")
buf = io.BytesIO()
noise.save(buf, "PNG")
big_png = buf.getvalue()

packed = here._compress_image(big_png, "posts")
check("compress: noisy PNG becomes JPEG",
      packed is not None and packed[1] == "jpg")
check("compress: output much smaller",
      packed and len(packed[0]) < len(big_png) // 3,
      f"{len(big_png)} -> {len(packed[0]) if packed else 0}")
im = Image.open(io.BytesIO(packed[0]))
check("compress: downscaled to 1920 cap",
      max(im.size) == 1920, str(im.size))
av = here._compress_image(big_png, "avatars")
check("compress: avatars capped at 512",
      av and max(Image.open(io.BytesIO(av[0])).size) == 512)
alpha = Image.new("RGBA", (700, 700), (200, 30, 30, 120))
b2 = io.BytesIO()
alpha.save(b2, "PNG")
check("compress: transparency keeps PNG",
      here._compress_image(b2.getvalue(), "posts")[1] == "png")
check("compress: garbage returns None (original kept)",
      here._compress_image(b"definitely not an image", "posts") is None)


class Up:
    def __init__(self, data, name):
        self.file = io.BytesIO(data)
        self.filename = name
        self.content_type = "image/png"


rel = here.save_upload(Up(big_png, "shot.png"), "avatars",
                       {"png", "jpg", "jpeg", "webp"})
full = os.path.join(os.environ["HERE_UPLOADS"], rel)
check("save_upload: avatar lands as .jpg, web-weight",
      rel.endswith(".jpg") and os.path.getsize(full) < len(big_png) // 10,
      f"{rel} {os.path.getsize(full)} vs {len(big_png)}")

# ================= accounts =================
H, HID = signup("Trio Host", "host@trio.io")
W, WID = signup("Trio Watcher", "watcher@trio.io")
ANON = TestClient(here.app, follow_redirects=False)

# ================= (2) race share strip =================
home = H.get("/home").text
code = dbq("SELECT invite_code FROM users WHERE id=?",
           (HID,))[0]["invite_code"]
check("race strip: home carries the share card while race open",
      'id="race-share"' in home)
check("race strip: carries MY invite link",
      ("/join/" + code) in home)
check("race strip: ready-made share text wired",
      "help me win the $100 Invite Race" in home)
check("race strip: shows days left + my score",
      "real joins" in home and "left" in home)

# ================= (1) guest live preview =================
lid = H.post("/live/start", json={"title": "Trio public show"}).json()["id"]

page = ANON.get(f"/live/{lid}").text
check("guest page: public live opens the WATCH page, not the gate",
      'id="gw-stage"' in page and "Create your free profile to watch"
      not in page)
check("guest page: inline join form posts to /signup with next back",
      'action="/signup"' in page
      and f'name="next" value="/live/{lid}"' in page)
check("guest page: read-only chat shell present",
      'id="gw-chat"' in page and "read-only for guests" in page)

r = ANON.post(f"/live/{lid}/guest/join", json={})
j = r.json()
check("guest join: ok with token + NEGATIVE signaling id",
      r.status_code == 200 and j.get("ok") and j.get("token")
      and j.get("guest_uid", 0) < 0, str(j)[:120])
token, guid = j["token"], j["guest_uid"]
check("guest join: panel carries the host",
      any(p["id"] == HID for p in j.get("panel", [])))
check("guest join: guest NOT in live_viewers (no count inflation)",
      dbq("SELECT COUNT(*) c FROM live_viewers WHERE live_id=?",
          (lid,))[0]["c"] == 0)

r = ANON.post(f"/live/{lid}/guest/heartbeat", json={"token": token})
check("guest heartbeat: ok, viewers still 0 real members",
      r.status_code == 200 and r.json().get("viewers") == 0)

r = ANON.post(f"/live/{lid}/comment", json={"body": "hello room"})
# (members comment via form or json elsewhere; guest must NOT be
# able to use the member comment route at all — it needs login)
check("guest cannot comment through the member route",
      r.status_code in (401, 403, 404, 405), str(r.status_code))

# a member comment appears in the guest's read-only chat
W.post(f"/live/{lid}/heartbeat", json={})
dbx("INSERT INTO live_comments(live_id, user_id, body, created_at) "
    "VALUES(?,?,?,?)", (lid, WID, "great show!", here.now_iso()))
r = ANON.get(f"/live/{lid}/guest/state",
             params={"token": token, "since_comment": 0})
s = r.json()
check("guest state: sees member comments read-only",
      any(c["body"] == "great show!" for c in s.get("comments", [])))
check("guest state: viewer count counts members only (1 watcher)",
      s.get("viewers") == 1, str(s.get("viewers")))

r = ANON.post(f"/live/{lid}/guest/signal",
              json={"token": token, "to_id": HID, "kind": "hello",
                    "payload": {"watch": 1}})
check("guest signal: hello to host accepted", r.status_code == 200)
sigs = H.get(f"/live/{lid}/signals", params={"since": 0}).json()["signals"]
check("host receives the guest hello under the negative id",
      any(sg["from_id"] == guid and sg["kind"] == "hello"
          for sg in sigs))
r = H.post(f"/live/{lid}/signal",
           json={"to_id": guid, "kind": "offer",
                 "payload": {"type": "offer", "sdp": "x"}})
check("host can signal the guest back (offer reaches allowed set)",
      r.status_code == 200, str(r.status_code))
gs = ANON.get(f"/live/{lid}/guest/signals",
              params={"token": token, "since": 0}).json()["signals"]
check("guest poll receives the host's offer",
      any(sg["kind"] == "offer" and sg["from_id"] == HID for sg in gs))

r = ANON.post(f"/live/{lid}/guest/signal",
              json={"token": token, "to_id": HID, "kind": "offer",
                    "payload": {"type": "offer", "sdp": "x"}})
check("guest may NEVER offer (400)", r.status_code == 400)
r = ANON.post(f"/live/{lid}/guest/signal",
              json={"token": token, "to_id": WID, "kind": "hello",
                    "payload": {"watch": 1}})
check("guest may only signal the panel, not watchers (403)",
      r.status_code == 403)
r = ANON.get(f"/live/{lid}/guest/state", params={"token": "bogus"})
check("bad guest token refused (403)", r.status_code == 403)

# ticket-door live stays locked for guests
H.post(f"/live/{lid}/end", json={})
H.post("/settings/creator", data={"creator_mode": "1"})
lid2 = H.post("/live/start", json={"title": "Trio ticket door",
                                   "entry_tickets": 200}).json()["id"]
r = ANON.post(f"/live/{lid2}/guest/join", json={})
check("ticket-door live: guest join refused (403)",
      r.status_code == 403)
page2 = ANON.get(f"/live/{lid2}").text
check("ticket-door live: anonymous page is the gate, not the watch",
      'id="gw-stage"' not in page2)

# private host stays locked for guests
H.post(f"/live/{lid2}/end", json={})
dbx("UPDATE users SET private_profile=1 WHERE id=?", (HID,))
lid3 = H.post("/live/start", json={"title": "Trio private host"}).json()["id"]
r = ANON.post(f"/live/{lid3}/guest/join", json={})
check("private host: guest join refused (403)", r.status_code == 403)
dbx("UPDATE users SET private_profile=0 WHERE id=?", (HID,))

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} trio checks passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
print("ALL TRIO CHECKS PASSED")
