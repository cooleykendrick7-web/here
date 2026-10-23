"""Lives discovery page (/lives + /lives.json) and photo albums
(/user/{id}/photos + profile Photos strip) tests for HERE.

Run:  ./venv/bin/python tests/test_lives_albums.py
Uses a throwaway DB/uploads dir; no network.
"""
import os
import re
import sqlite3
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-la-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

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
    assert r.status_code == 303, (name, r.status_code)
    uid = dbq("SELECT id FROM users WHERE email=?", (email,))[0]["id"]
    return c, uid


def befriend(c1, u1, c2, u2):
    r = c2.post(f"/friends/request/{u1}")
    assert r.status_code in (200, 303), r.status_code
    r = c1.post(f"/friends/accept/{u2}")
    assert r.status_code in (200, 303), r.status_code


def start_live(client, title):
    r = client.post("/live/start", json={"title": title})
    assert r.status_code == 200 and r.json().get("ok"), r.text[:200]
    return r.json()["id"]


JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x00" * 64 + b"\xff\xd9"
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 64

# ---------------- users ----------------
HF, HFID = signup("Host Friend", "hf@la.io")
HS, HSID = signup("Host Stranger", "hs@la.io")
HP, HPID = signup("Host Private", "hp@la.io")
V, VID = signup("Viewer Vee", "v@la.io")
FP, FPID = signup("Friend Private", "fp@la.io")
W1, W1ID = signup("Watcher One", "w1@la.io")
W2, W2ID = signup("Watcher Two", "w2@la.io")
G, GID = signup("Guest Gwen", "g@la.io")
HS2, HS2ID = signup("Host Second", "hs2@la.io")
AO, AOID = signup("Album Owner", "ao@la.io")
AF, AFID = signup("Album Friend", "af@la.io")
AS, ASID = signup("Album Stranger", "as@la.io")
AS2, AS2ID = signup("Album Blocked", "as2@la.io")
PO, POID = signup("Private Owner", "po@la.io")
POF, POFID = signup("Private Friend", "pof@la.io")

# ---------------- lives setup ----------------
befriend(V, VID, HF, HFID)          # V friends with HF
V.post(f"/follow/{HSID}")           # V follows HS (not friends)
HP.post("/settings/privacy", data={"private_profile": "1"})
befriend(HP, HPID, FP, FPID)        # FP is HP's friend; V is not

L_HF = start_live(HF, "HF live title")
L_HS = start_live(HS, "HS live title")
L_HP = start_live(HP, "HP live title")
# HS gets MORE watchers than HF, so viewers-desc alone would put HS
# first — the friends-first rule is what must put HF first.
W1.post(f"/live/{L_HS}/heartbeat")
W2.post(f"/live/{L_HS}/heartbeat")

html = V.get("/lives").text
check("V /lives contains friend host HF", "HF live title" in html)
check("V /lives contains followed host HS", "HS live title" in html)
check("HF appears BEFORE HS (friends-first beats viewers)",
      -1 < html.find("HF live title") < html.find("HS live title"))
check("private host HP absent from stranger-ish V's /lives",
      "HP live title" not in html)
html_fp = FP.get("/lives").text
check("HP's friend FP sees HP's live", "HP live title" in html_fp)

data = V.get("/lives.json").json()
check("/lives.json ok", data.get("ok") is True, str(data)[:200])
lives = data.get("lives") or []
ids = [x["host_id"] for x in lives]
check("/lives.json order matches HTML (HF before HS)",
      HFID in ids and HSID in ids and ids.index(HFID) < ids.index(HSID),
      str(ids))
by_host = {x["host_id"]: x for x in lives}
check("/lives.json keys present",
      all(k in by_host.get(HFID, {}) for k in
          ("host_name", "host_avatar", "viewers", "panel_count",
           "following", "is_self")))
check("/lives.json following True for followed HS",
      by_host.get(HSID, {}).get("following") is True)
check("/lives.json following False for HF (not followed)",
      by_host.get(HFID, {}).get("following") is False)

anon = TestClient(here.app, follow_redirects=False)
r = anon.get("/lives.json")
check("anon /lives.json -> 401 login JSON",
      r.status_code == 401 and r.json().get("error") == "login",
      str(r.status_code))

# panel/viewer counts on HF's live: a watcher + an approved guest
W1.post(f"/live/{L_HF}/heartbeat")
G.post(f"/live/{L_HF}/heartbeat")
r = G.post(f"/live/{L_HF}/join-request")
req_id = r.json()["request"]["id"]
HF.post(f"/live/{L_HF}/join-decide",
        json={"request_id": req_id, "approve": True})
data = V.get("/lives.json").json()
hf_entry = {x["host_id"]: x for x in data["lives"]}.get(HFID, {})
check("HF entry viewers>=1 after watcher heartbeat",
      (hf_entry.get("viewers") or 0) >= 1, str(hf_entry))
check("HF entry panel_count==2 after approved guest",
      hf_entry.get("panel_count") == 2, str(hf_entry))

# nav + profile Lives link
home = V.get("/home").text
check("nav contains href=/lives", 'href="/lives"' in home)
prof_hf = V.get(f"/user/{HFID}").text
check("profile page contains the 🔴 Lives action link",
      'href="/lives"' in prof_hf and "🔴 Lives" in prof_hf)

# follow from a lives card (fresh host HS2)
L_HS2 = start_live(HS2, "HS2 live title")
html = V.get("/lives").text
check("HS2 card shows + Follow form",
      f'action="/follow/{HS2ID}"' in html and "+ Follow" in html)
r = V.post(f"/follow/{HS2ID}", headers={"referer": "/lives"})
check("follow from /lives redirects back to /lives (303)",
      r.status_code == 303 and r.headers.get("location") == "/lives",
      str(r.status_code) + " " + r.headers.get("location", ""))
html = V.get("/lives").text
check("HS2 card now shows Following ✓ and no follow form",
      "Following ✓" in html and f'action="/follow/{HS2ID}"' not in html)

LIVES_TO_END = [(HF, L_HF), (HS, L_HS), (HP, L_HP), (HS2, L_HS2)]

# ---------------- albums ----------------
r = AO.post("/posts", data={"kind": "photo", "body": "ao public pic",
                            "privacy": "public"},
            files={"media": ("pic1.jpg", JPEG, "image/jpeg")})
check("AO public photo post created", r.status_code == 303,
      str(r.status_code))
r = AO.post("/posts", data={"kind": "photo", "body": "ao friends pic",
                            "privacy": "friends"},
            files={"media": ("pic2.jpg", JPEG, "image/jpeg")})
check("AO friends photo post created", r.status_code == 303)
r = AO.post("/posts", data={"kind": "video", "body": "ao clip",
                            "privacy": "public"},
            files={"media": ("clip.mp4", MP4, "video/mp4")})
check("AO video post created", r.status_code == 303)
AO.post("/posts", data={"kind": "text", "body": "ao plain text"})

def media_of(body):
    rows = dbq("SELECT media FROM posts WHERE user_id=? AND body=?",
               (AOID, body))
    return rows[0]["media"] if rows else ""

pub_url = "/uploads/" + media_of("ao public pic")
fri_url = "/uploads/" + media_of("ao friends pic")
vid_url = "/uploads/" + media_of("ao clip")
check("album test media captured from DB",
      media_of("ao public pic").endswith(".jpg")
      and media_of("ao friends pic").endswith(".jpg")
      and media_of("ao clip").endswith(".mp4"))

befriend(AO, AOID, AF, AFID)

own = AO.get(f"/user/{AOID}/photos").text
check("AO own album has both photos",
      pub_url in own and fri_url in own)
check("AO own album excludes the video's media URL", vid_url not in own)
af_view = AF.get(f"/user/{AOID}/photos").text
check("friend's album view has both photos",
      pub_url in af_view and fri_url in af_view)
as_view = AS.get(f"/user/{AOID}/photos").text
check("stranger's album view has the public photo", pub_url in as_view)
check("stranger's album view: friends-only URL absent entirely",
      fri_url not in as_view)

prof_as = AS.get(f"/user/{AOID}").text
check("profile strip (stranger): public photo + See all photos link",
      pub_url in prof_as and "See all photos" in prof_as
      and f'href="/user/{AOID}/photos"' in prof_as)
check("profile strip (stranger): friends-only URL absent",
      fri_url not in prof_as)
prof_ao = AO.get(f"/user/{AOID}").text
check("profile strip (owner): both photos present",
      pub_url in prof_ao and fri_url in prof_ao)

# private album owner
PO.post("/settings/privacy", data={"private_profile": "1"})
befriend(PO, POID, POF, POFID)
PO.post("/posts", data={"kind": "photo", "body": "po public pic",
                        "privacy": "public"},
        files={"media": ("po1.jpg", JPEG, "image/jpeg")})
po_media = dbq("SELECT media FROM posts WHERE user_id=? AND body=?",
               (POID, "po public pic"))[0]["media"]
po_url = "/uploads/" + po_media
stranger_view = AS.get(f"/user/{POID}/photos").text
check("private album: stranger gets the locked face",
      "This profile is private" in stranger_view)
check("private album: stranger view has no photo URL",
      po_url not in stranger_view)
friend_view = POF.get(f"/user/{POID}/photos").text
check("private album: friend sees the photo", po_url in friend_view)

# blocked album face
AO.post(f"/block/{AS2ID}")
blocked_view = AS2.get(f"/user/{AOID}/photos")
check("blocked album view shows the unavailable face",
      "isn't available" in blocked_view.text)

# ---------------- block / room-ban lives (LAST, own hosts) ----------------
BLK, BLKID = signup("Block Host", "blk@la.io")
L_BLK = start_live(BLK, "BLK live title")
LIVES_TO_END.append((BLK, L_BLK))
check("BLK live visible to V before the block",
      "BLK live title" in V.get("/lives").text)
BLK.post(f"/block/{VID}")
check("blocked host's live absent from V's /lives",
      "BLK live title" not in V.get("/lives").text)

RB, RBID = signup("Room Ban Host", "rb@la.io")
L_RB = start_live(RB, "RB live title")
LIVES_TO_END.append((RB, L_RB))
check("RB live visible to V before the room ban",
      "RB live title" in V.get("/lives").text)
dbx("INSERT OR IGNORE INTO live_bans(live_id, user_id, by_id, "
    "created_at) VALUES(?,?,?,?)",
    (L_RB, VID, RBID, "2026-10-22T00:00:00+00:00"))
check("room-banned host's live absent from V's /lives",
      "RB live title" not in V.get("/lives").text)

# ---------------- empty state ----------------
for client, lid in LIVES_TO_END:
    client.post(f"/live/{lid}/end")
html = V.get("/lives").text
check("empty state after all lives end",
      "Nobody's live right now" in html)

# node --check on lives.html inline JS
src = open(os.path.join(BASE, "templates", "lives.html")).read()
scripts = re.findall(r"<script>(.*?)</script>", src, re.S)
ok = bool(scripts)
for i, s in enumerate(scripts):
    with open(os.path.join(_tmp, f"lives{i}.js"), "w") as fh:
        fh.write(s)
    p = subprocess.run(["node", "--check",
                        os.path.join(_tmp, f"lives{i}.js")],
                       capture_output=True, text=True)
    if p.returncode != 0:
        ok = False
        print("node --check failed on lives script", i, p.stderr[:300])
check("node --check: templates/lives.html inline JS parses", ok)

# ---------------- summary ----------------
fails = [n for n, okk in RESULTS if not okk]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
