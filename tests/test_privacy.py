"""Privacy page (2026-11-08): public, honest, linked from auth pages.
Run:  ./venv/bin/python tests/test_privacy.py
"""
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-privacy-")
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


c = TestClient(here.app, follow_redirects=False)
r = c.get("/privacy")
check("privacy page loads logged-out (200)", r.status_code == 200)
check("states the no-sell promise", "never sell your personal" in r.text.lower())
check("covers Google sign-in data honestly", "Continue with Google" in r.text)
check("names Stripe for payments", "Stripe" in r.text)
check("gives the support contact", "wearehereyouarehere@gmail.com" in r.text)
check("states passwords are hashed", "hashed" in r.text)
r = c.get("/login")
check("login page links the policy", 'href="/privacy"' in r.text)
r = c.get("/signup")
check("signup page links the policy", 'href="/privacy"' in r.text)

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    sys.exit(1)
