"""Build PANEL GIFTS — gifts aimed at any panelist.

Run:  ./venv/bin/python tests/test_panel_gifts.py

Viewers tap a panel member's tile (host, admin, on-stage guest)
and their gifts pay THAT person. Covers: the default target is
the host (exact legacy behavior), a panel guest is credited
(sender debited, recipient named in live_gifts + ticket_txns,
guest's own /earnings + /tickets show it), refusals with NO
debit (target a plain watcher, a stranger, the sender themself,
a panelist who left, a garbage/unknown id, the host gifting
their own live), the free gift to a guest with the 2s guard
intact, Boost-style repeat sends carrying the target, the
state payload's recipient fields (+ the legacy NULL fallback),
Showdown recipient scoring (battle host scores for their side
from either room; a non-host panelist is paid but scores 0),
and the live page's targeting client wiring.
"""
import os, re, shutil, sqlite3, subprocess, sys, tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-panelgifts-")
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


def bal(uid):
    r = dbq("SELECT tickets_balance, gift_earned, gift_lifetime "
            "FROM users WHERE id=?", (uid,))[0]
    return (r["tickets_balance"] or 0), (r["gift_earned"] or 0), \
        (r["gift_lifetime"] or 0)


def grant_spendable(uid, n):
    dbx("UPDATE users SET tickets_balance = "
        "COALESCE(tickets_balance,0) + ? WHERE id=?", (n, uid))
    dbx("INSERT INTO ticket_txns(user_id, kind, tickets, created_at) "
        "VALUES(?,?,?,?)", (uid, "buy", n, here.now_iso()))


def join_panel(member_client, host_client, lid):
    r = member_client.post(f"/live/{lid}/join-request")
    req_id = r.json()["request"]["id"]
    r = host_client.post(f"/live/{lid}/join-decide",
                         json={"request_id": req_id, "approve": True})
    assert r.json().get("ok"), r.text[:200]


def gift_count(lid):
    return dbq("SELECT COUNT(*) c FROM live_gifts WHERE live_id=?",
               (lid,))[0]["c"]


def battle_row(bid):
    rows = dbq("SELECT * FROM live_battles WHERE id=?", (bid,))
    return dict(rows[0]) if rows else None


H, HID = signup("Hana Host", "hana@pg.io")
G, GID = signup("Gwen Guest", "gwen@pg.io")
G2, G2ID = signup("Gabe Guest", "gabe@pg.io")
W, WID = signup("Walt Watcher", "walt@pg.io")
O, OID = signup("Ollie Outsider", "ollie@pg.io")
A, AID = signup("Abby Absent", "abby@pg.io")

r = H.post("/live/start", json={"title": "Panel Gifts Night"})
LID = r.json()["id"]
for c in (W, O, G, G2):
    assert c.post(f"/live/{LID}/heartbeat").status_code == 200
join_panel(G, H, LID)          # Gwen is ON the panel
grant_spendable(WID, 1000)

# ---------------- default target = the host (regression) ----------
r = W.post(f"/live/{LID}/gift", json={"gift_key": "here_rocket"})
j = r.json()
check("no target -> host paid (response names the host)",
      r.status_code == 200 and j.get("ok")
      and j.get("recipient_id") == HID
      and j.get("recipient_name") == "Hana Host", r.text[:160])
check("no target -> balances: sender -25, host earned +25, guest 0",
      bal(WID)[0] == 975 and bal(HID)[1] == 25 and bal(GID)[1] == 0,
      str((bal(WID), bal(HID), bal(GID))))
row = dbq("SELECT * FROM live_gifts WHERE live_id=?", (LID,))[0]
check("live_gifts row: recipient_id = host on a default gift",
      row["recipient_id"] == HID and row["host_id"] == HID)

r = W.post(f"/live/{LID}/gift",
           json={"gift_key": "money_bag", "target_user_id": HID})
check("explicit host target pays the host",
      r.status_code == 200 and bal(HID)[1] == 26
      and bal(WID)[0] == 974)

# ---------------- a panel guest is credited -----------------------
r = W.post(f"/live/{LID}/gift",
           json={"gift_key": "flying_cash", "target_user_id": GID})
j = r.json()
check("guest target accepted (response names the guest)",
      r.status_code == 200 and j.get("ok")
      and j.get("recipient_id") == GID
      and j.get("recipient_name") == "Gwen Guest", r.text[:160])
check("guest gift moves exactly 5: sender -5, guest earned +5, "
      "host untouched",
      bal(WID)[0] == 969 and bal(GID)[1] == 5 and bal(HID)[1] == 26,
      str((bal(WID), bal(GID), bal(HID))))
check("guest lifetime-earned (streamer level) += 5 too",
      bal(GID)[2] == 5)
row = dbq("SELECT * FROM live_gifts WHERE live_id=? AND "
          "gift_key='flying_cash'", (LID,))[0]
check("live_gifts row names recipient AND room host apart",
      row["recipient_id"] == GID and row["host_id"] == HID
      and row["sender_id"] == WID)
txn = dbq("SELECT * FROM ticket_txns WHERE kind='gift_receive' "
          "AND live_id=? AND tickets=5", (LID,))
check("gift_receive ledger row belongs to the GUEST (live_id set)",
      len(txn) == 1 and txn[0]["user_id"] == GID, str(len(txn)))

st = W.get(f"/live/{LID}/state").json()
rg = st["recent_gifts"]
rocket = [g for g in rg if g["gift_key"] == "here_rocket"]
check("state recent_gifts: default gift recipient = host",
      len(rocket) == 1 and rocket[0]["recipient_id"] == HID
      and rocket[0]["recipient_name"] == "Hana Host", str(rocket))
fly = [g for g in rg if g["gift_key"] == "flying_cash"]
check("state recent_gifts: guest gift carries recipient id + name",
      len(fly) == 1 and fly[0]["recipient_id"] == GID
      and fly[0]["recipient_name"] == "Gwen Guest", str(fly))

r = G.get("/earnings")
check("guest /earnings shows the host's stream in HER per-stream "
      "table", r.status_code == 200 and "Panel Gifts Night" in r.text)
r = G.get("/tickets")
check("guest wallet history: 'Received Flying Cash (+5 🎟️)'",
      "Received Flying Cash (+5 🎟️)" in r.text)

# ---------------- refusals: no debit, no credit, no row -----------
w_before, g_before, n_before = bal(WID)[0], bal(GID)[1], gift_count(LID)
r = W.post(f"/live/{LID}/gift",
           json={"gift_key": "flying_cash", "target_user_id": OID})
check("target = present-but-NOT-panel watcher refused",
      r.status_code == 400 and "panel" in r.json().get("error", ""),
      r.text[:160])
r = W.post(f"/live/{LID}/gift",
           json={"gift_key": "flying_cash", "target_user_id": AID})
check("target = user not in the live at all refused",
      r.status_code == 400, str(r.status_code))
r = W.post(f"/live/{LID}/gift",
           json={"gift_key": "flying_cash", "target_user_id": WID})
check("target = the sender themself refused",
      r.status_code == 400
      and "yourself" in r.json().get("error", ""), r.text[:160])
r = W.post(f"/live/{LID}/gift",
           json={"gift_key": "flying_cash", "target_user_id": 999999})
check("target = nonexistent user refused", r.status_code == 400)
r = W.post(f"/live/{LID}/gift",
           json={"gift_key": "flying_cash", "target_user_id": "abc"})
check("target = garbage id refused (400, no crash)",
      r.status_code == 400, str(r.status_code))
r = H.post(f"/live/{LID}/gift",
           json={"gift_key": "flying_cash", "target_user_id": GID})
check("host still can't gift on their own live, even at a guest",
      r.status_code == 400, str(r.status_code))
check("every refusal moved nothing (balance / earned / rows)",
      bal(WID)[0] == w_before and bal(GID)[1] == g_before
      and gift_count(LID) == n_before,
      str((bal(WID)[0], bal(GID)[1], gift_count(LID))))

r = W.post(f"/live/{LID}/gift",
           json={"gift_key": "money_bag", "target_user_id": None})
check("explicit null target = the host (legacy shape)",
      r.status_code == 200 and bal(HID)[1] == 27
      and bal(WID)[0] == 968)

# ---------------- a panelist who LEFT can't be gifted ------------
join_panel(G2, H, LID)
r = G2.post(f"/live/{LID}/leave")
check("second guest joins then leaves the panel",
      r.status_code == 200
      and dbq("SELECT left_at FROM live_members WHERE live_id=? "
              "AND user_id=?", (LID, G2ID))[0]["left_at"] is not None,
      str(r.status_code))
w_before, n_before = bal(WID)[0], gift_count(LID)
r = W.post(f"/live/{LID}/gift",
           json={"gift_key": "flying_cash", "target_user_id": G2ID})
check("target who LEFT the panel refused, no debit",
      r.status_code == 400 and "panel" in r.json().get("error", "")
      and bal(WID)[0] == w_before and gift_count(LID) == n_before
      and bal(G2ID)[1] == 0, r.text[:160])

# ---------------- free gift to a guest; 2s guard intact -----------
r = W.post(f"/live/{LID}/gift",
           json={"gift_key": "neon_heart", "target_user_id": GID})
check("free gift to a panel guest works",
      r.status_code == 200 and r.json().get("ok"), r.text[:160])
check("free gift moved no money (sender + guest earned unchanged)",
      bal(WID)[0] == 968 and bal(GID)[1] == 5)
frow = dbq("SELECT * FROM live_gifts WHERE live_id=? AND "
           "gift_key='neon_heart'", (LID,))
check("free gift row: 0 tickets, recipient = the guest",
      len(frow) == 1 and frow[0]["tickets"] == 0
      and frow[0]["recipient_id"] == GID)
r = W.post(f"/live/{LID}/gift", json={"gift_key": "neon_heart"})
check("free-gift 2s guard still fires (even aimed at the host)",
      r.status_code == 429, str(r.status_code))

# ---------------- panelist as sender; panelist -> panelist --------
grant_spendable(GID, 10)
r = G.post(f"/live/{LID}/gift", json={"gift_key": "money_bag"})
check("panelist gifts the host (default target)",
      r.status_code == 200 and bal(HID)[1] == 28)
join_panel(G2, H, LID)         # Gabe rejoins the panel
r = G.post(f"/live/{LID}/gift",
           json={"gift_key": "flying_cash", "target_user_id": G2ID})
check("panelist -> panelist gift pays the second guest",
      r.status_code == 200 and bal(G2ID)[1] == 5
      and bal(GID)[0] == 4, r.text[:160])

# ---------------- Boost-style repeats carry the target ------------
for _ in range(2):
    r = W.post(f"/live/{LID}/gift",
               json={"gift_key": "flying_cash",
                     "target_user_id": GID})
    assert r.status_code == 200, r.text[:160]
check("repeat sends (the Boost path) credit the guest every tap",
      bal(GID)[1] == 15 and bal(WID)[0] == 958,
      str((bal(GID), bal(WID))))

# ---------------- legacy rows: NULL recipient reads as host -------
dbx("INSERT INTO live_gifts(live_id, sender_id, host_id, "
    "recipient_id, gift_key, tickets, created_at) "
    "VALUES(?,?,?,?,?,?,?)",
    (LID, WID, HID, None, "money_bag", 1, here.now_iso()))
st = W.get(f"/live/{LID}/state").json()
legacy = [g for g in st["recent_gifts"]
          if g["gift_key"] == "money_bag"
          and g["sender_id"] == WID and g["tickets"] == 1
          and g["recipient_id"] == HID]
check("legacy NULL-recipient gift renders as a gift to the host",
      len(legacy) >= 1
      and all(g["recipient_name"] == "Hana Host" for g in legacy),
      str(legacy))
cols = [r[1] for r in dbq("PRAGMA table_info(live_gifts)")]
check("migration: live_gifts has recipient_id", "recipient_id" in cols)

# ---------------- ledger invariants --------------------------------
for uid, nm in ((WID, "Walt"), (HID, "Hana"), (GID, "Gwen"),
                (G2ID, "Gabe")):
    spend, earned, _life = bal(uid)
    s = dbq("SELECT COALESCE(SUM(tickets),0) s FROM ticket_txns "
            "WHERE user_id=? AND kind IN ('buy','gift_send','shout')",
            (uid,))[0]["s"]
    e = dbq("SELECT COALESCE(SUM(tickets),0) s FROM ticket_txns "
            "WHERE user_id=? AND kind IN ('gift_receive',"
            "'cashout_hold','cashout_return')", (uid,))[0]["s"]
    check(f"ledger invariant holds for {nm}", spend == s and earned == e,
          str((spend, s, earned, e)))

# ---------------- live page: targeting client wiring --------------
page = W.get(f"/live/{LID}").text
for needle, label in [
        (".live-tile.panel-selected", "selected-tile CSS class"),
        ("@keyframes panelrainbow", "rainbow glow keyframes"),
        (".live-tile.panel-selected{animation:none;",
         "reduced-motion steady glow"),
        (".live-tile.gift-flash", "recipient tile flash class"),
        ('id="gift-target"', "Gifting label element"),
        ("🎁 Gifting: ", "Gifting label text"),
        ("var giftTargetId = (B.host && B.host.id)",
         "default target = the host"),
        ("selectGiftTarget(uid)", "tile tap selects the panelist"),
        ("syncGiftTarget", "poll resets a departed target"),
        ("giftTargetRefused", "refused send resets to host"),
        ("flashGiftTile", "gift event flashes the recipient tile"),
        ("g.recipient_name", "client consumes recipient_name"),
        ('id="gift-sheet-target"', "gift sheet names the target"),
        ("score points for their side",
         "sheet note states the recipient scoring rule")]:
    check(f"live page: {label} present", needle in page)
check("live page: BOTH send paths (sheet + Boost) carry the target",
      page.count("target_user_id: giftTargetId") == 2,
      str(page.count("target_user_id: giftTargetId")))
check("live page: my own tile is never selectable (guard in code)",
      "if (uid === B.me) return;" in page)

blocks = re.findall(r"<script>(.*?)</script>", page, re.S)
node = shutil.which("node")
if node:
    all_ok, bad = True, []
    for i, block in enumerate(blocks):
        if "{{" in block:
            continue  # the LIVE_BOOT jinja block, not raw JS
        path = os.path.join(_tmp, f"chk_pg_{i}.js")
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

# ---------------- Showdown: points follow the recipient -----------
HA, HAID = signup("Alice Army", "alice@pg.io")
HB, HBID = signup("Bob Battle", "bob@pg.io")
FA, FAID = signup("Fan Amy", "amy@pg.io")
FB, FBID = signup("Fan Ben", "ben@pg.io")
GA, GAID = signup("Gia Guest", "gia@pg.io")
LA = HA.post("/live/start", json={"title": "Alice live"}).json()["id"]
LB = HB.post("/live/start", json={"title": "Bob live"}).json()["id"]
FA.post(f"/live/{LA}/heartbeat")
FB.post(f"/live/{LB}/heartbeat")
GA.post(f"/live/{LA}/heartbeat")
join_panel(GA, HA, LA)         # Gia guests on Alice's panel
grant_spendable(FAID, 500)
grant_spendable(FBID, 500)

r = HA.post(f"/live/{LA}/battle/challenge",
            json={"opponent_live_id": LB, "duration_sec": 300})
BID_B = r.json()["battle_id"]
r = HB.post(f"/live/{LB}/battle/{BID_B}/accept")
check("battle is active", r.status_code == 200 and r.json().get("ok"),
      r.text[:160])

r = FA.post(f"/live/{LA}/gift",
            json={"gift_key": "here_rocket", "target_user_id": GAID})
row = battle_row(BID_B)
check("gift to a NON-HOST panelist pays her but scores ZERO",
      r.status_code == 200 and bal(GAID)[1] == 25
      and row["challenger_score"] == 0 and row["opponent_score"] == 0,
      str((bal(GAID), row["challenger_score"], row["opponent_score"])))
r = FA.post(f"/live/{LA}/gift", json={"gift_key": "here_rocket"})
row = battle_row(BID_B)
check("gift to MY room's battle host scores for my side (25)",
      row["challenger_score"] == 25 and row["opponent_score"] == 0
      and bal(HAID)[1] == 25, str(row["challenger_score"]))

# Bob (a battle host) guests on ALICE's panel: a gift to him in
# Alice's room must score for BOB's side — points follow the
# recipient, whichever room the sender is standing in.
HB.post(f"/live/{LA}/heartbeat")
join_panel(HB, HA, LA)
r = FA.post(f"/live/{LA}/gift",
            json={"gift_key": "flying_cash", "target_user_id": HBID})
row = battle_row(BID_B)
check("gift to the OTHER battle host (guesting here) scores for "
      "HIS side", r.status_code == 200 and bal(HBID)[1] == 5
      and row["opponent_score"] == 5
      and row["challenger_score"] == 25,
      str((row["challenger_score"], row["opponent_score"])))
r = FB.post(f"/live/{LB}/gift", json={"gift_key": "flying_cash"})
row = battle_row(BID_B)
check("default gift in Bob's room still scores for Bob (10)",
      row["opponent_score"] == 10 and row["challenger_score"] == 25)
r = FA.post(f"/live/{LA}/gift",
            json={"gift_key": "neon_heart", "target_user_id": HAID})
row = battle_row(BID_B)
check("free gift to a battle host still scores 0",
      row["challenger_score"] == 25 and row["opponent_score"] == 10)

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
