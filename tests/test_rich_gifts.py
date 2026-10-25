"""Build RICH tests: earnings worth + cash-out readiness on the
private account surfaces (/earnings + /tickets), per-gift send
animations (catalog anim names, the live page's animation engine,
sender parity with per-send dedupe), and the 🔥 Boost chip
(repeat sends through the normal gift path).

Worth math: dollars are ALWAYS here._earned_dollars(tickets)
(the cash-out's own function, floor(tickets/2) cents) formatted
with _money2 — the tests assert the rendered figures equal that
math at 155 / 1999 / 2000 / 2600, including the threshold edge:
1999 earned -> worth $9.99, 1 more ticket, $0.01 more to ready.

Boost sends ride POST /live/{id}/gift unchanged, so repeat sends
move both ledgers per tap, write a live_gifts row per tap, score
Showdown points per tap (test_showdown.py proves the scoring
hook), and the FREE gift's 2s guard still refuses a too-fast
repeat — all asserted at the API level here; the chip itself is
client-side and asserted as markup + JS wiring on the live page.

Run:  ./venv/bin/python tests/test_rich_gifts.py
Uses a throwaway DB/uploads dir; no network.
"""
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-rich-")
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


def seed_earned(uid, earned, lifetime=None):
    dbx("UPDATE users SET gift_earned=?, gift_lifetime=? WHERE id=?",
        (earned, earned if lifetime is None else lifetime, uid))


def bal(uid):
    r = dbq("SELECT tickets_balance, gift_earned FROM users WHERE id=?",
            (uid,))[0]
    return (r["tickets_balance"] or 0), (r["gift_earned"] or 0)


def grant_spendable(uid, n):
    dbx("UPDATE users SET tickets_balance = "
        "COALESCE(tickets_balance,0) + ? WHERE id=?", (n, uid))
    dbx("INSERT INTO ticket_txns(user_id, kind, tickets, created_at) "
        "VALUES(?,?,?,?)", (uid, "buy", n, here.now_iso()))


def worth(tickets):
    return here._money2(here._earned_dollars(tickets))


# ---------------- worth math unit: the cash-out's own function ------
for n, expect in ((0, "$0.00"), (1, "$0.00"), (2, "$0.01"),
                  (155, "$0.77"), (1999, "$9.99"), (2000, "$10.00"),
                  (2001, "$10.00"), (2600, "$13.00"),
                  (2695, "$13.47")):
    check(f"worth math: {n} tickets renders {expect}",
          worth(n) == expect, worth(n))
check("shortfall dollars at 1999 = min worth - worth (=$0.01)",
      here._money2(here._earned_dollars(2000)
                   - here._earned_dollars(1999)) == "$0.01")
check("shortfall dollars at 155 = $9.23 (1000c - 77c)",
      here._money2(here._earned_dollars(2000)
                   - here._earned_dollars(155)) == "$9.23")

# ---------------- /earnings + /tickets: below the minimum -----------
B, BID = signup("Below Bea", "bea@x.io")
seed_earned(BID, 155)
html = B.get("/earnings").text
check("earnings: lifetime + available both carry worth $0.77",
      html.count("worth $0.77") == 2, str(html.count("worth $0.77")))
check("earnings below-min status: exact shortfall line",
      "1845 more earned tickets" in html
      and "($9.23 more)" in html
      and "minimum 2,000 tickets = $10.00" in html)
check("earnings below-min: no ready line, no cash-out form",
      "You're ready" not in html
      and 'action="/tickets/cashout"' not in html)
tk = B.get("/tickets").text
check("tickets line (below): earned + worth + cash out at 2,000",
      "Earned tickets: 155 — worth $0.77" in tk
      and "cash out at 2,000" in tk and 'href="/earnings"' in tk)
check("tickets line (below): no ready wording",
      "ready to cash out" not in tk)

# ---------------- edge: 1999 -----------------------------------------
E, EID = signup("Edge Ed", "ed@x.io")
seed_earned(EID, 1999)
html = E.get("/earnings").text
check("earnings edge 1999: worth $9.99, 1 more ticket, $0.01 more",
      "worth $9.99" in html and "1 more earned tickets" in html
      and "($0.01 more)" in html)
check("earnings edge 1999: still no form",
      'action="/tickets/cashout"' not in html)

# ---------------- ready: 2000 earned / 2600 lifetime -----------------
R, RID = signup("Ready Rita", "rita@x.io")
seed_earned(RID, 2000, lifetime=2600)
html = R.get("/earnings").text
check("earnings ready: lifetime worth $13.00, available worth $10.00",
      "worth $13.00" in html and "worth $10.00" in html)
check("earnings ready status line: can cash out $10.00 now",
      "✅ You're ready — you can cash out $10.00 now" in html)
check("earnings ready: the cash-out form is offered",
      'action="/tickets/cashout"' in html)
tk = R.get("/tickets").text
check("tickets line (ready): worth $10.00 — ready to cash out",
      "Earned tickets: 2000 — worth $10.00 — ready to cash out →"
      in tk)

# ---------------- pending: what happens next --------------------------
P, PID = signup("Pending Pete", "pete@x.io")
seed_earned(PID, 2500)
r = P.post("/tickets/cashout", data={"payout_note": "Cash App $pete"})
check("pending setup: cash-out POST -> 303", r.status_code == 303)
prs = dbq("SELECT * FROM payout_requests WHERE user_id=?", (PID,))
check("pending request dollars = _earned_dollars(2500) = 1250c",
      len(prs) == 1 and prs[0]["dollars_cents"] == 1250
      and prs[0]["dollars_cents"] == here._earned_dollars(2500))
html = P.get("/earnings").text
check("earnings pending status: approval + what happens next",
      "Cash-out status: waiting for the owner's approval" in html
      and "once it's approved, $12.50 is on its way to you." in html)
check("earnings pending: no second form, no ready line",
      'action="/tickets/cashout"' not in html
      and "You're ready" not in html)
check("earnings pending: available worth drops to $0.00 (held)",
      "worth $0.00" in html)

# ---------------- catalog: every gift has an animation ----------------
check("catalog still 67 gifts", len(here.GIFT_CATALOG) == 67)
noanim = [k for k, g in here.GIFT_CATALOG.items()
          if g.get("anim") not in here.GIFT_ANIMS]
check("every gift's anim is in the implemented set",
      not noanim, str(noanim))
check("all 8 animation names are used across the catalog",
      {g["anim"] for g in here.GIFT_CATALOG.values()}
      == set(here.GIFT_ANIMS))
check("the FREE gift stays light: neon_heart -> rise",
      here.GIFT_CATALOG["neon_heart"]["anim"] == "rise")
big = [k for k, g in here.GIFT_CATALOG.items() if g["tickets"] >= 5000]
check("grand gifts (>=5000) get showpiece anims",
      all(here.GIFT_CATALOG[k]["anim"]
          in ("shower", "burst", "zoomin", "flyby") for k in big),
      str([(k, here.GIFT_CATALOG[k]["anim"]) for k in big]))
mid = [k for k, g in here.GIFT_CATALOG.items() if g["tickets"] >= 100]
check("no >=100 gift uses a light anim (rise/bounce)",
      all(here.GIFT_CATALOG[k]["anim"] not in ("rise", "bounce")
          for k in mid),
      str([(k, here.GIFT_CATALOG[k]["anim"]) for k in mid
           if here.GIFT_CATALOG[k]["anim"] in ("rise", "bounce")]))
check("themed flybys: rocket / jet / cutlass fly across",
      here.GIFT_CATALOG["here_rocket"]["anim"] == "flyby"
      and here.GIFT_CATALOG["private_jet"]["anim"] == "flyby"
      and here.GIFT_CATALOG["the_cutlass"]["anim"] == "flyby")
pub = here._gift_public("here_rocket")
check("_gift_public keeps the tier int AND adds the anim name",
      pub["anim"] == 1 and pub["anim_name"] == "flyby", str(pub))
pub = here._gift_public("here_crown")
check("_gift_public: crown tier 3 + zoomin",
      pub["anim"] == 3 and pub["anim_name"] == "zoomin", str(pub))
pub = here._gift_public("ant")
check("_gift_public legacy key: fallback anim name, no crash",
      pub["anim"] == 1 and pub["anim_name"] == "rise", str(pub))

# ---------------- live flows: state carries anim_name -----------------
H, HID = signup("Host Hana", "host@x.io")
V, VID = signup("Viewer Vic", "vic@x.io")
r = H.post("/live/start", json={"title": "Rich night"})
LID = r.json()["id"]
assert V.post(f"/live/{LID}/heartbeat").status_code == 200
grant_spendable(VID, 4000)
r = V.post(f"/live/{LID}/gift", json={"gift_key": "here_crown"})
check("crown sends (1500)", r.status_code == 200 and r.json().get("ok"),
      r.text[:160])
st = V.get(f"/live/{LID}/state").json()
crown = [g for g in st["recent_gifts"] if g["gift_key"] == "here_crown"]
check("state recent_gifts carry anim (tier) + anim_name (zoomin)",
      len(crown) == 1 and crown[0]["anim"] == 3
      and crown[0]["anim_name"] == "zoomin", str(crown))

# repeat sends (what every Boost tap does): each is a real send
vb0, _ = bal(VID)
_, he0 = bal(HID)
for i in range(3):
    r = V.post(f"/live/{LID}/gift", json={"gift_key": "money_bag"})
    assert r.json().get("ok"), r.text[:120]
vb1, _ = bal(VID)
_, he1 = bal(HID)
check("3 repeat sends move exactly 3 tickets each side",
      vb1 == vb0 - 3 and he1 == he0 + 3, f"V {vb0}->{vb1} H {he0}->{he1}")
rows = dbq("SELECT id FROM live_gifts WHERE live_id=? AND "
           "gift_key='money_bag'", (LID,))
check("3 repeat sends wrote 3 live_gifts rows (one per tap)",
      len(rows) == 3, str(len(rows)))
ids = [g["id"] for g in
       V.get(f"/live/{LID}/state").json()["recent_gifts"]]
check("every send has its own gift id (the dedupe key)",
      len(ids) == len(set(ids)) and len(ids) >= 4, str(ids))

# the FREE gift's 2s guard still refuses a too-fast Boost repeat
r = V.post(f"/live/{LID}/gift", json={"gift_key": "neon_heart"})
check("free gift sends", r.status_code == 200 and r.json().get("ok"))
r = V.post(f"/live/{LID}/gift", json={"gift_key": "neon_heart"})
check("free gift instant repeat -> 429 guard (no Boost bypass)",
      r.status_code == 429, str(r.status_code))
dbx("UPDATE live_gifts SET created_at='2000-01-01T00:00:00+00:00' "
    "WHERE live_id=? AND tickets=0", (LID,))
r = V.post(f"/live/{LID}/gift", json={"gift_key": "neon_heart"})
check("free gift allowed again after the guard window",
      r.status_code == 200 and r.json().get("ok"))

# boost stops on insufficient balance: the send itself refuses
r = V.post(f"/live/{LID}/gift", json={"gift_key": "mansion_sunset"})
check("insufficient-balance repeat refused (chip's stop condition)",
      r.status_code == 400
      and "Not enough tickets" in r.json().get("error", ""))

# ---------------- live page: engine + parity + Boost wiring ----------
page = V.get(f"/live/{LID}").text
for name in here.GIFT_ANIMS:
    check(f"live page implements animation '{name}'",
          f"@keyframes giftfx-{name}" in page
          and f"'{name}'" in page)
for needle, label in [
        ("GIFT_ANIMS", "animation name set in the engine"),
        ("GIFT_ANIM_MS", "per-anim duration map"),
        ("giftAnimName", "per-gift animation resolver"),
        ("anim_name", "client consumes the poll's anim_name"),
        ("GIFT_BY_KEY", "catalog lookup by gift key"),
        ("spawnGiftFx", "float-layer fx spawner"),
        ("GIFT_FX_CAP", "concurrent fx cap constant"),
        (">= GIFT_FX_CAP", "cap enforced before spawning"),
        ("giftfx-particle", "burst particle animation"),
        ("gift-rain-piece", "shower rain pieces"),
        ("REDUCED_MOTION", "reduced-motion gate in JS"),
        ("prefers-reduced-motion: reduce", "reduced-motion CSS"),
        ("giftfx-fade", "reduced-motion simple fade"),
        ("renderLocalGift", "sender-parity local renderer"),
        ("seenGiftIds", "per-send dedupe set"),
        ("seenGiftIds[g.id]", "poll echo suppressed by gift id"),
        ('id="gift-boost"', "Boost chip markup"),
        ("🔥 Boost", "Boost chip label"),
        ('id="gift-boost-count"', "Boost combo counter element"),
        ("showBoostChip(g, 1)", "chip appears at x1 after a send"),
        ("showBoostChip(g, boostCount + 1)", "chip counter increments"),
        ("setTimeout(hideBoostChip, 6000)", "chip ~6s expiry"),
        ("boostBusy", "boost in-flight lockout flag"),
        ("boostLastAt < 150", "boost ~150ms tap lockout"),
        ("hideBoostChip();", "chip cleared (sheet close / expiry)")]:
    check(f"live page: {label} present", needle in page)
check("live page: sender renders locally on BOTH send paths",
      page.count("renderLocalGift(g, d.gift_id)") == 2,
      str(page.count("renderLocalGift(g, d.gift_id)")))
check("live page: boost reuses the normal /gift send path",
      page.count("post('/gift', {gift_key: g.key})") == 2,
      str(page.count("post('/gift', {gift_key: g.key})")))
check("live page: the old sender-suppression line is gone",
      "lastGiftId = Math.max(lastGiftId, d.gift_id)" not in page)
check("live page: boost chip follows the last sent gift (state var)",
      "boostGift = g;" in page and "var boostGift = null" in page)

blocks = re.findall(r"<script>(.*?)</script>", page, re.S)
node = shutil.which("node")
if node:
    all_ok, bad = True, []
    for i, block in enumerate(blocks):
        if "{{" in block:
            continue  # the LIVE_BOOT jinja block, not raw JS
        path = os.path.join(_tmp, f"chk_rich_{i}.js")
        with open(path, "w") as f:
            f.write(block)
        p = subprocess.run([node, "--check", path],
                           capture_output=True, text=True)
        if p.returncode != 0:
            all_ok = False
            bad.append(f"#{i}: {p.stderr.strip()[:160]}")
    check("node --check: live page inline JS parses", all_ok,
          "; ".join(bad))
else:
    check("node --check skipped (node unavailable)", True)

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} checks passed")
if fails:
    print("FAILED:", *fails, sep="\n  ")
    sys.exit(1)
