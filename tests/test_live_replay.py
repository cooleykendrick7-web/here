"""Live Replay tests (Nov 13, 2026) — Ken's order: when a host
ends a live, the live has been recorded; the host can download
the replay or post it to their timeline.

The recording itself is client-side (the lives are WebRTC mesh),
so these tests cover the server half end to end:
- chunks are host-only (another member, and a logged-out caller,
  are refused) and the live_replays row is born on chunk 1;
- chunk -> finish joins the parts byte-exactly, in seq order;
- download (host-only) returns the exact joined bytes as an
  attachment; another member cannot download it;
- post creates a public video post whose media is the replay
  file, and marks the row posted with the post id;
- discard deletes the stored bytes and marks the row discarded;
  posting a discarded replay is refused;
- finish is idempotent, and finishing with no chunks is a 400.

Run:  ./venv/bin/python tests/test_live_replay.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-replay-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []
CH1 = b"\x1a\x45\xdf\xa3" + b"A" * 500      # fake webm head chunk
CH2 = b"B" * 700
CH3 = b"C" * 300


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{extra}]" if extra and not cond else ""))


def dbq(sql, args=()):
    conn = here.db()
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


Hc, HOST = signup("Holly Host", "holly@replay.test")
Oc, OTHER = signup("Ollie Other", "ollie@replay.test")
ANON = TestClient(here.app, follow_redirects=False)

r = Hc.post("/live/start", json={"title": "Replay night"})
LID = r.json()["id"]
check("host started a live", bool(LID))

CHUNK_URL = f"/live/{LID}/replay/chunk"
FIN_URL = f"/live/{LID}/replay/finish"


def chunk(client, seq, data, lid_url=CHUNK_URL):
    return client.post(
        lid_url, data={"seq": str(seq), "ext": "webm"},
        files={"chunk": ("chunk.webm", data, "video/webm")})


# ---------- chunk gatekeeping ----------
r = chunk(Oc, 1, CH1)
check("another member cannot upload chunks", r.status_code == 403,
      str(r.status_code))
r = chunk(ANON, 1, CH1)
check("logged-out caller cannot upload chunks",
      r.status_code == 401, str(r.status_code))
r = chunk(Hc, 1, CH1, lid_url="/live/999999/replay/chunk")
check("chunk for a missing live is 404", r.status_code == 404,
      str(r.status_code))
check("no replay row exists yet",
      dbq("SELECT * FROM live_replays WHERE live_id=?",
          (LID,)) == [])

# ---------- chunk round trip ----------
r = chunk(Hc, 1, CH1)
j = r.json()
check("host chunk 1 accepted", r.status_code == 200 and j.get("ok"))
RID = j.get("replay_id")
check("replay id handed back", bool(RID))
rows = dbq("SELECT * FROM live_replays WHERE id=?", (RID,))
check("row born on first chunk, status recording",
      len(rows) == 1 and rows[0]["status"] == "recording"
      and rows[0]["host_id"] == HOST and rows[0]["live_id"] == LID)
r = chunk(Hc, 3, CH3)
check("chunk 3 accepted (out of order is fine)",
      r.status_code == 200 and r.json().get("ok"))
r = chunk(Hc, 2, CH2)
check("chunk 2 accepted", r.status_code == 200 and r.json().get("ok"))
check("parts counter tracks the highest seq",
      dbq("SELECT parts FROM live_replays WHERE id=?",
          (RID,))[0]["parts"] == 3)

# ---------- finish ----------
r = Oc.post(FIN_URL, data={"duration_s": "95", "ext": "webm",
                           "parts": "3"})
check("another member cannot finish", r.status_code == 403)
r = Hc.post("/live/999999/replay/finish", data={"duration_s": "1"})
check("finish for a missing live is 404", r.status_code == 404)
r = Hc.post(FIN_URL, data={"duration_s": "95", "ext": "webm",
                           "parts": "3"})
j = r.json()
check("finish ok", r.status_code == 200 and j.get("ok"), str(r.text))
check("finish hands back the download url",
      j.get("download_url") == f"/live/replay/{RID}/download")
row = dbq("SELECT * FROM live_replays WHERE id=?", (RID,))[0]
JOINED = CH1 + CH2 + CH3
check("row ready with joined byte count + duration",
      row["status"] == "ready" and row["bytes"] == len(JOINED)
      and row["duration_s"] == 95)
final_path = os.path.join(os.environ["HERE_UPLOADS"],
                          "replays", f"r{RID}.webm")
check("joined file on disk is byte-exact, in seq order",
      os.path.isfile(final_path)
      and open(final_path, "rb").read() == JOINED)
parts_left = []
pdir = os.path.join(os.environ["HERE_UPLOADS"], "replays",
                    f"r{RID}")
if os.path.isdir(pdir):
    parts_left = os.listdir(pdir)
check("parts were cleaned up after the join", parts_left == [],
      str(parts_left))
r = Hc.post(FIN_URL, data={"duration_s": "95", "ext": "webm",
                           "parts": "3"})
check("finish is idempotent on a ready replay",
      r.status_code == 200 and r.json().get("ok"))

# ---------- download ----------
DL = f"/live/replay/{RID}/download"
r = Oc.get(DL)
check("another member cannot download the replay",
      r.status_code == 403, str(r.status_code))
r = ANON.get(DL)
check("logged-out caller cannot download", r.status_code == 401,
      str(r.status_code))
r = Hc.get(DL)
check("host download returns the exact joined bytes",
      r.status_code == 200 and r.content == JOINED,
      str(r.status_code))
check("download is an attachment named for the live",
      f"here-live-{LID}.webm"
      in r.headers.get("content-disposition", ""),
      r.headers.get("content-disposition", ""))

# ---------- post to timeline ----------
r = Oc.post(f"/live/replay/{RID}/post", data={"caption": "mine now"})
check("another member cannot post the replay",
      r.status_code == 403)
r = Hc.post(f"/live/replay/{RID}/post",
            data={"caption": "🔴 My live replay — come join HERE! "
                             "joincomehere.com #here"})
j = r.json()
check("host posts the replay", r.status_code == 200 and j.get("ok"),
      str(r.text))
PID = j.get("post_id")
posts = dbq("SELECT * FROM posts WHERE id=?", (PID,))
check("a public video post exists with the replay media",
      len(posts) == 1 and posts[0]["kind"] == "video"
      and posts[0]["user_id"] == HOST
      and posts[0]["media"] == f"replays/r{RID}.webm"
      and posts[0]["privacy"] == "public")
check("caption hashtags were parsed",
      dbq("SELECT * FROM post_hashtags WHERE post_id=? AND tag='here'",
          (PID,)) != [])
row = dbq("SELECT * FROM live_replays WHERE id=?", (RID,))[0]
check("row marked posted with the post id",
      row["status"] == "posted" and row["post_id"] == PID)
r = Hc.get("/home")
check("the replay post shows on the host's home feed",
      f"replays/r{RID}.webm" in r.text)
r = Hc.post(f"/live/replay/{RID}/post", data={"caption": "again"})
check("re-posting hands back the same post (no duplicate)",
      r.status_code == 200 and r.json().get("post_id") == PID)

# ---------- discard (a second live's replay) ----------
r = Hc.post("/live/start", json={"title": "Throwaway live"})
LID2 = r.json()["id"]
r = chunk(Hc, 1, CH1, lid_url=f"/live/{LID2}/replay/chunk")
RID2 = r.json()["replay_id"]
r = Hc.post(f"/live/{LID2}/replay/finish",
            data={"duration_s": "5", "ext": "webm", "parts": "1"})
check("second replay finishes", r.status_code == 200
      and r.json().get("ok"))
path2 = os.path.join(os.environ["HERE_UPLOADS"],
                     "replays", f"r{RID2}.webm")
check("second replay file exists before discard",
      os.path.isfile(path2))
r = Oc.post(f"/live/replay/{RID2}/discard")
check("another member cannot discard", r.status_code == 403)
r = Hc.post(f"/live/replay/{RID2}/discard")
check("host discards", r.status_code == 200 and r.json().get("ok"))
check("discard deleted the bytes", not os.path.exists(path2))
row2 = dbq("SELECT * FROM live_replays WHERE id=?", (RID2,))[0]
check("row kept as discarded for the record",
      row2["status"] == "discarded")
r = Hc.get(f"/live/replay/{RID2}/download")
check("a discarded replay cannot be downloaded",
      r.status_code == 409, str(r.status_code))
r = Hc.post(f"/live/replay/{RID2}/post", data={"caption": "zombie"})
check("a discarded replay cannot be posted",
      r.status_code == 409, str(r.status_code))

# ---------- finish with nothing uploaded ----------
r = Hc.post("/live/start", json={"title": "Silent live"})
LID3 = r.json()["id"]
r = Hc.post(f"/live/{LID3}/replay/finish", data={"duration_s": "3"})
check("finish with no chunks at all is a clean 404",
      r.status_code == 404, str(r.status_code))

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
