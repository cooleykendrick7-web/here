"""Black Coin deep-Settings opt-in toggle (Ken's order 2026-11-06).

Participation is a switch buried at the very bottom of Settings
that a member turns on themselves — finding it is the signal
they studied HERE. Default OFF for everyone.

Covers:
- default off: no BC balance stat and no BC card on the wallet
  page; the withdraw route refuses server-side and moves nothing
- pack bonus badges (+ N BC FREE) stay visible while off — the
  advertisement is not gated, participation is
- bonuses still ACCRUE on pack buys while off (credit_purchase
  never reads the flag): turn it on later, the BC is waiting
- the settings flow flips it: POST /settings/blackcoin on ->
  card + balance + working withdraw; off again -> hidden +
  refused
- the toggle lives deep: its Settings card renders after the
  IDM card and after the badge-door card
- owner signal: a 🪙 marker in the Users table for opted-in
  members only, and the opted-in count on /owner/bc
- gates: strangers refused on the settings + withdraw routes
"""
import os, sys, sqlite3, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
TMP = tempfile.mkdtemp(prefix="here_bctoggle_")
os.environ["HERE_DB"] = os.path.join(TMP, "t.db")
os.environ["HERE_UPLOADS"] = os.path.join(TMP, "up")
os.environ["STRIPE_SECRET_KEY"] = "sk_test_fake"
import app as here
from fastapi.testclient import TestClient

PASS = [0]; FAIL = [0]

def check(name, cond, extra=""):
    if cond:
        PASS[0] += 1; print("  ok -", name)
    else:
        FAIL[0] += 1; print("  FAIL -", name, extra)

def dbq(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"])
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    conn.close(); return rows

def dbx(sql, args=()):
    conn = sqlite3.connect(os.environ["HERE_DB"])
    conn.execute(sql, args); conn.commit(); conn.close()

def signup(name, email):
    cl = TestClient(here.app, follow_redirects=False)
    r = cl.post("/signup", data={"name": name, "email": email,
                                 "password": "testpass123"})
    assert r.status_code == 303, (name, r.status_code)
    return cl, dbq("SELECT MAX(id) m FROM users")[0]["m"]

ADDR = "0x" + "11" * 20          # a valid-shaped Base address

def flag(uid):
    return dbq("SELECT COALESCE(bc_enabled,0) f FROM users "
               "WHERE id=?", (uid,))[0]["f"]

def bal(uid):
    return here._bc_balance(uid)

def wds(uid):
    return dbq("SELECT * FROM bc_withdrawals WHERE user_id=?",
               (uid,))

BC_CARD = "Black Coin Bonus — withdraw to your wallet"
BC_STAT = "Black Coin Bonus — free BC added with ticket packs"

print("== 0. stranger gates ==")
anon = TestClient(here.app, follow_redirects=False)
r = anon.post("/settings/blackcoin", data={"bc_enabled": "1"})
check("stranger refused /settings/blackcoin",
      r.status_code == 303 and "/login" in
      r.headers.get("location", ""), str(r.status_code))
r = anon.post("/tickets/bc/withdraw",
              data={"address": ADDR, "amount": "25000"})
check("stranger refused /tickets/bc/withdraw",
      r.status_code == 303 and "/login" in
      r.headers.get("location", ""), str(r.status_code))

print("== 1. default OFF: hidden card, refused withdraw ==")
A, AID = signup("Toggle Amy", "amy@x.io")
check("bc_enabled defaults off", flag(AID) == 0)
html = A.get("/tickets").text
check("no BC card while off", BC_CARD not in html)
check("no BC balance stat while off", BC_STAT not in html)
check("pack badges still advertise while off",
      "+ 1,000 BC FREE" in html and "+ 25,000 BC FREE" in html)

print("== 2. bonuses accrue while OFF ==")
check("pack buy credits while off",
      here.credit_purchase(AID, 1500, "cs_t_1500") is True)
check("balance waits in the DB", bal(AID) == 25000)
html = A.get("/tickets").text
check("card still hidden with a balance", BC_CARD not in html)
r = A.post("/tickets/bc/withdraw",
           data={"address": ADDR, "amount": "25000"})
check("withdraw refused while off (303 back to wallet)",
      r.status_code == 303, str(r.status_code))
check("refusal moved nothing",
      bal(AID) == 25000 and wds(AID) == [])

print("== 3. the deep Settings door ==")
html = A.get("/settings/profile").text
check("settings page carries the Black Coin card",
      'id="blackcoin"' in html and "🪙 Black Coin" in html
      and "More doors" in html)
check("toggle sits below IDM and the badge door",
      html.index('id="blackcoin"') > html.index('id="idm"')
      and html.index('id="blackcoin"')
      > html.index("the badge door"))
check("mandated toggle copy is on the door",
      "Turn this on to see your Black Coin Bonus balance and "
      "withdraw it to your own wallet." in html)
check("checkbox starts unchecked",
      'name="bc_enabled" value="1" checked' not in html)

print("== 4. flip it ON through the settings flow ==")
r = A.post("/settings/blackcoin", data={"bc_enabled": "1"})
check("settings save redirects with bc=on",
      r.status_code == 303
      and "bc=on" in r.headers.get("location", ""),
      r.headers.get("location", ""))
check("flag persisted", flag(AID) == 1)
html = A.get("/settings/profile").text
check("checkbox now checked",
      'name="bc_enabled" value="1" checked' in html)
html = A.get("/tickets").text
check("BC card appears once on", BC_CARD in html
      and BC_STAT in html)
check("accrued balance is waiting, shown in full",
      "25,000 BC" in html)
r = A.post("/tickets/bc/withdraw",
           data={"address": ADDR, "amount": "25000"})
check("withdraw works once on", r.status_code == 303
      and len(wds(AID)) == 1
      and wds(AID)[0]["status"] == "pending"
      and bal(AID) == 0)

print("== 5. flip it back OFF: hidden + refused again ==")
B, BID = signup("Toggle Bob", "bob@x.io")
here.credit_purchase(BID, 500, "cs_t_500")
B.post("/settings/blackcoin", data={"bc_enabled": "1"})
check("bob on", flag(BID) == 1)
r = B.post("/settings/blackcoin", data={})
check("empty save (checkbox unticked) turns it off",
      r.status_code == 303
      and "bc=off" in r.headers.get("location", ""))
check("bob off again", flag(BID) == 0)
html = B.get("/tickets").text
check("bob's card hidden again", BC_CARD not in html)
B.post("/tickets/bc/withdraw",
       data={"address": ADDR, "amount": "7500"})
check("bob's withdraw refused while off again",
      wds(BID) == [] and bal(BID) == 7500)

print("== 6. owner signal ==")
dbx("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
    ("bct-owner-token", here.now_iso()))
own = TestClient(here.app, follow_redirects=False)
own.cookies.set("here_owner", "bct-owner-token")
html = own.get("/owner").text
check("exactly one 🪙 member marker (Amy on, Bob off)",
      html.count("Black Coin member — opted in") == 1,
      str(html.count("Black Coin member")))
html = own.get("/owner/bc").text
import re
m = re.search(r'<div class="num">(\d+)</div><div class="lbl">'
              r'Black Coin members', html)
check("/owner/bc member count is 1", bool(m) and m.group(1) == "1",
      m.group(1) if m else "card missing")
B.post("/settings/blackcoin", data={"bc_enabled": "1"})
html = own.get("/owner").text
check("marker count follows the toggle (now 2)",
      html.count("Black Coin member — opted in") == 2)
html = own.get("/owner/bc").text
m = re.search(r'<div class="num">(\d+)</div><div class="lbl">'
              r'Black Coin members', html)
check("/owner/bc member count is now 2",
      bool(m) and m.group(1) == "2",
      m.group(1) if m else "card missing")
r = anon.get("/owner/bc")
check("stranger still refused /owner/bc",
      r.status_code == 303 and "/owner/login" in
      r.headers.get("location", ""))

print(f"\n{PASS[0]} passed, {FAIL[0]} failed")
sys.exit(1 if FAIL[0] else 0)
