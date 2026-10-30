"""HEARLEY'S FACE tests (Oct 30, 2026): Ken — "I still see the
robot." The 🤖 emoji is gone everywhere Hearley appears: her page
header is an ANIMATED portrait (a looping clip, like Milli's
avatar), her chat bubbles carry her portrait, and the FAQ card +
both menus show her face instead of a robot glyph.

Run:  ./venv/bin/python tests/test_hearley_face.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-hface-")
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
    conn = here.db()
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


c = TestClient(here.app, follow_redirects=False)
r = c.post("/signup", data={"name": "Face Checker",
                            "email": "face@hf.test",
                            "password": "pw123456"})
assert r.status_code == 303

r = c.get("/hearley")
check("Hearley page has the animated portrait",
      r.status_code == 200 and "hearley-live.mp4" in r.text
      and "hearley-face.jpg" in r.text)
check("Hearley page shows no robot", "🤖" not in r.text)
check("chat bubbles use her portrait",
      "av.src = '/static/hearley-face.jpg'" in r.text)

r = c.get("/faq")
check("FAQ card shows her face, no robot",
      "hearley-face.jpg" in r.text and "🤖" not in r.text)

r = c.get("/home")
check("menus show her face, no robot glyph",
      "hearley-face.jpg" in r.text
      and "🤖</span> Ask Hearley" not in r.text
      and '<span class="menu-ic">🤖</span>' not in r.text)

r = c.get("/static/hearley-live.mp4")
check("the animated clip serves as video",
      r.status_code == 200
      and r.headers.get("content-type", "").startswith("video/"),
      r.headers.get("content-type", ""))
r = c.get("/static/hearley-face.jpg")
check("the portrait serves as an image",
      r.status_code == 200
      and r.headers.get("content-type", "").startswith("image/"))

r = c.post("/api/hearley/ask", json={"q": "how do I cash out?"})
check("Hearley still answers after the face-lift",
      r.json().get("ok") is True)

failed = [n for n, ok in RESULTS if not ok]
print()
print(f"{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
if failed:
    print("FAILED:", "; ".join(failed))
    sys.exit(1)
