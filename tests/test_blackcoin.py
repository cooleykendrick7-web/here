"""Black Coin dashboard tests (Nov 5, 2026 — Ken's order):
 * GET /blackcoin is public (NO login, NO owner session) and renders the
   live-telemetry dashboard shell with its truth markers — the server
   itself serves no price; the page fetches chain data client-side.
 * The owner dashboard carries the small bottom-left coin door to
   /blackcoin, and /owner still refuses non-owners.

Run:  ./venv/bin/python tests/test_blackcoin.py
"""
import os
import sqlite3
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-blackcoin-")
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


def dbx(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"])
    conn.execute(sql, args)
    conn.commit()
    conn.close()


# ---- /blackcoin: public, no login of any kind ----
anon = TestClient(here.app, follow_redirects=False)
r = anon.get("/blackcoin")
check("GET /blackcoin is 200 for a total stranger", r.status_code == 200,
      f"got {r.status_code}")
body = r.text
check("dashboard welcomes Kendrick Cooley by name",
      "Welcome" in body and "Kendrick Cooley" in body
      and "your Black Coin dashboard" in body)
check("dashboard shows the coin logo", "/static/blackcoin.png" in body)
check("dashboard names the BC contract",
      "0x0Ea794511a1864dFD4CFA85982a4C7fbAA6f1e88" in body)
check("dashboard names Ken's wallet",
      "0xef8fb653a0ab5b7f01d0cd0dca07b2c1ee3196e3" in body)
check("dashboard names the Aerodrome pool",
      "0x7e953C84f1Dc95d7C94e777CA533c3570eD8590A" in body)
check("dashboard reads the Base chain client-side",
      "mainnet.base.org" in body and "eth_call" in body)
check("dashboard polls DexScreener + GeckoTerminal",
      "api.dexscreener.com" in body and "api.geckoterminal.com" in body)
check("dashboard carries the honesty note (paper value, tiny pool)",
      "Paper value at the pool" in body and "could not actually be sold" in body)
check("dashboard carries the no-promise line",
      "nothing here is a promise" in body)
check("dashboard has gauge cluster markers",
      'id="gPrice"' in body and 'id="gMcap"' in body
      and 'id="gLiq"' in body and 'id="gShare"' in body)
check("dashboard auto-refreshes", "30000" in body)

# The static logo itself is served.
r = anon.get("/static/blackcoin.png")
check("GET /static/blackcoin.png is 200", r.status_code == 200,
      f"got {r.status_code}")

# ---- /owner still gated, and carries the coin door for the owner ----
r = anon.get("/owner")
check("GET /owner still refuses strangers",
      r.status_code in (303, 307)
      and "/owner/login" in r.headers.get("location", ""),
      f"got {r.status_code}")

dbx("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
    ("blackcoin-owner-token", here.now_iso()))
own = TestClient(here.app, follow_redirects=False)
own.cookies.set("here_owner", "blackcoin-owner-token")
r = own.get("/owner")
check("GET /owner renders for the owner", r.status_code == 200,
      f"got {r.status_code}")
check("owner panel has the Black Coin door (bottom-left coin)",
      'href="/blackcoin"' in r.text and "/static/blackcoin.png" in r.text
      and "bottom:14px" in r.text and "left:14px" in r.text)

# ---- summary ----
fails = [n for n, okk in RESULTS if not okk]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
