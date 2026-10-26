"""Build PAGE GIFTS: the 🎁 Gift button on profiles + Pages, gated
by Creator Mode. Covers:
- schema: page_gifts table + users.creator_mode (default off)
- Creator Mode: settings card + POST /settings/creator persists;
  the owner's own profile shows the state line; button hidden +
  routes refuse (no writes) while the recipient's flag is off;
  Pages gate on the PAGE OWNER's flag (a member's flag is moot)
- POST /user/{uid}/gift + POST /page/{pid}/gift move money
  EXACTLY like live gifts (sender -N spendable, recipient
  gift_earned +N and gift_lifetime +N) with note='page' ledger
  rows (live_id NULL) + a page_gifts row, so the two-ledger
  invariant still holds for both sides
- a page gift credits the page OWNER, never a member
- self-gift refused; blocked pair refused both directions;
  private profile: non-friend refused, friend allowed
- the FREE gift works + its 1-per-2s guard (scoped per
  sender+recipient) refuses an instant second; insufficient
  balance refused with no writes
- the page_gift notification (kind, text, link)
- /earnings shows page gifts as their OWN "🎁 Page gifts" line,
  separate from the legacy "Earlier streams" NULL bucket;
  /tickets history reads "Received <Gift> on your page (+N 🎟️)"
- the Recent gifts strip renders for a viewer and is absent
  when empty; the button is hidden on your own profile/page
- the shipped sheet script parses (node --check)
"""
import os, sys, json, sqlite3, tempfile, subprocess, shutil, re
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
TMP = tempfile.mkdtemp(prefix="here_page_gifts_")
os.environ["HERE_DB"] = os.path.join(TMP, "t.db")
os.environ["HERE_UPLOADS"] = os.path.join(TMP, "up")
import app as here
from fastapi.testclient import TestClient

C = TestClient(here.app, follow_redirects=False)
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
    uid = dbq("SELECT MAX(id) m FROM users")[0]["m"]
    return cl, uid

def bal(uid):
    r = dbq("SELECT tickets_balance b, gift_earned e, gift_lifetime l "
            "FROM users WHERE id=?", (uid,))[0]
    return r["b"], r["e"], r["l"]

def grant_spendable(uid, n):
    dbx("UPDATE users SET tickets_balance=tickets_balance+? WHERE id=?",
        (n, uid))
    dbx("INSERT INTO ticket_txns (user_id, kind, tickets, created_at) "
        "VALUES (?, 'buy', ?, ?)", (uid, n, here.now_iso()))

def invariant(uid):
    b, e, _l = bal(uid)
    spend = dbq("SELECT COALESCE(SUM(tickets),0) s FROM ticket_txns "
                "WHERE user_id=? AND kind IN ('buy','gift_send',"
                "'shout')", (uid,))[0]["s"]
    earn = dbq("SELECT COALESCE(SUM(tickets),0) s FROM ticket_txns "
               "WHERE user_id=? AND kind IN ('gift_receive',"
               "'cashout_hold','cashout_return')", (uid,))[0]["s"]
    return b == spend and e == earn

def creator_on(uid):
    dbx("UPDATE users SET creator_mode=1 WHERE id=?", (uid,))

def gift_rows(where="", args=()):
    return dbq("SELECT * FROM page_gifts " + where + " ORDER BY id",
               args)

STACK = {"gift_key": "gift_stack"}   # Gift Stack, 10 tickets
FREE = {"gift_key": "neon_heart"}    # Neon Heart, 0 tickets

S, SID = signup("Pg Sender", "pgs@x.io")
R, RID = signup("Pg Recip", "pgr@x.io")
V, VID = signup("Pg Viewer", "pgv@x.io")
grant_spendable(SID, 500)

print("== 1. Creator Mode: default off gates everything ==")
u = dbq("SELECT creator_mode cm FROM users WHERE id=?", (RID,))[0]
check("creator_mode defaults off", not u["cm"])
html = S.get(f"/user/{RID}").text
check("gift button hidden while recipient creator off",
      'id="pgift-open"' not in html)
r = S.post(f"/user/{RID}/gift", json=STACK)
check("gift route refuses while creator off (403 + plain words)",
      r.status_code == 403 and "Creator Mode" in r.json()["error"],
      str(r.status_code))
check("refusal wrote nothing", gift_rows() == [] and bal(RID)[1] == 0)
st = R.get("/settings/profile").text
check("settings shows the 🎨 Creator Mode card",
      'id="creator-mode"' in st and "Creator Mode" in st)
check("settings checkbox starts unchecked",
      'name="creator_mode" value="1" checked' not in st)
own = R.get(f"/user/{RID}").text
check("own profile shows 'Creator Mode: Off'",
      "Creator Mode: Off" in own)

print("== 2. The switch: POST /settings/creator persists ==")
r = R.post("/settings/creator", data={"creator_mode": "1"})
check("switch on -> 303", r.status_code == 303, str(r.status_code))
check("creator_mode persisted on",
      dbq("SELECT creator_mode cm FROM users WHERE id=?",
          (RID,))[0]["cm"] == 1)
st = R.get("/settings/profile").text
check("settings checkbox now checked",
      'name="creator_mode" value="1" checked' in st)
own = R.get(f"/user/{RID}").text
check("own profile shows 'Creator Mode: On'",
      "Creator Mode: On" in own)
check("own profile still has NO gift button (no self-gifting)",
      'id="pgift-open"' not in own)
html = S.get(f"/user/{RID}").text
check("gift button shows once creator is on",
      'id="pgift-open"' in html and "🎁 Gift" in html)
check("strip absent while no gifts yet", "Recent gifts" not in html)

print("== 3. A profile gift moves money exactly ==")
sb, se, sl = bal(SID); rb, re_, rl = bal(RID)
r = S.post(f"/user/{RID}/gift", json=STACK)
d = r.json()
check("gift ok", r.status_code == 200 and d.get("ok") is True,
      str(r.status_code))
check("response names the recipient",
      d.get("recipient_id") == RID
      and d.get("recipient_name") == "Pg Recip")
check("response balance = sender - 10", d.get("balance") == sb - 10)
check("sender spendable -10, earned/lifetime untouched",
      bal(SID) == (sb - 10, se, sl))
check("recipient earned +10 AND lifetime +10, spendable untouched",
      bal(RID) == (rb, re_ + 10, rl + 10))
rows = gift_rows()
check("one page_gifts row: profile gift (page_id NULL)",
      len(rows) == 1 and rows[0]["sender_id"] == SID
      and rows[0]["recipient_id"] == RID
      and rows[0]["page_id"] is None
      and rows[0]["gift_key"] == "gift_stack"
      and rows[0]["tickets"] == 10)
tx = dbq("SELECT * FROM ticket_txns WHERE user_id IN (?,?) "
         "AND kind LIKE 'gift_%' ORDER BY id", (SID, RID))
check("ledger: gift_send -10 on sender, gift_receive +10 on "
      "recipient, both note='page', live_id NULL",
      len(tx) == 2 and tx[0]["user_id"] == SID
      and tx[0]["tickets"] == -10 and tx[0]["note"] == "page"
      and tx[0]["live_id"] is None
      and tx[1]["user_id"] == RID and tx[1]["tickets"] == 10
      and tx[1]["note"] == "page" and tx[1]["live_id"] is None)
check("two-ledger invariant holds for sender + recipient",
      invariant(SID) and invariant(RID))

print("== 4. Notification + strip + tickets line ==")
n = dbq("SELECT * FROM notifications WHERE user_id=? AND "
        "kind='page_gift'", (RID,))
check("page_gift notification: one row, sender + gift named, "
      "links to the sender's profile",
      len(n) == 1 and "Pg Sender" in n[0]["text"]
      and "Gift Stack" in n[0]["text"]
      and n[0]["link"] == f"/user/{SID}")
html = V.get(f"/user/{RID}").text
check("strip renders for another viewer (sender + gift named)",
      "Recent gifts" in html and "Gift Stack" in html
      and "Pg Sender" in html)
tk = R.get("/tickets").text
check("/tickets history reads 'on your page (+10 🎟️)'",
      "on your page (+10 🎟️)" in tk)

print("== 5. /earnings: page gifts are their OWN line ==")
r = S.post(f"/user/{RID}/gift", json=STACK)   # second gift -> 20
check("second gift ok", r.status_code == 200)
# a legacy pre-marker live gift (live_id NULL, note='') must stay
# in the "Earlier streams" bucket, NOT merge with page gifts
dbx("INSERT INTO ticket_txns (user_id, kind, tickets, live_id, "
    "gift_key, note, created_at) VALUES (?, 'gift_receive', 30, "
    "NULL, 'gift_stack', '', ?)", (RID, here.now_iso()))
dbx("UPDATE users SET gift_earned=gift_earned+30, "
    "gift_lifetime=gift_lifetime+30 WHERE id=?", (RID,))
e = R.get("/earnings").text
check("earnings shows the 🎁 Page gifts line", "🎁 Page gifts" in e)
check("page-gift line totals 20 🎟️ (the two profile gifts)",
      "20 🎟️" in e)
check("legacy bucket still separate at 30 🎟️",
      "Earlier streams" in e and "30 🎟️" in e)
check("invariant still holds after the legacy row",
      invariant(RID))

print("== 6. Free gift + the 2s guard (per sender+recipient) ==")
R2, R2ID = signup("Pg Recip Two", "pgr2@x.io")
creator_on(R2ID)
sb, se, sl = bal(SID); rb, re_, rl = bal(RID)
r = S.post(f"/user/{RID}/gift", json=FREE)
check("free gift ok", r.status_code == 200 and r.json().get("ok"))
check("free gift moves no money on either side",
      bal(SID) == (sb, se, sl) and bal(RID) == (rb, re_, rl))
fr = gift_rows("WHERE tickets=0")
check("free gift still records a page_gifts row",
      len(fr) == 1 and fr[0]["gift_key"] == "neon_heart")
r = S.post(f"/user/{RID}/gift", json=FREE)
check("instant second free gift refused by the guard (429)",
      r.status_code == 429, str(r.status_code))
check("guard refusal wrote nothing",
      len(gift_rows("WHERE tickets=0")) == 1)
r = S.post(f"/user/{R2ID}/gift", json=FREE)
check("guard is per-recipient: another creator is fine",
      r.status_code == 200, str(r.status_code))
r = S.post(f"/user/{RID}/gift", json=STACK)
check("a PAID gift right after a free one is not guarded",
      r.status_code == 200, str(r.status_code))

print("== 7. Insufficient balance: refused, no writes ==")
BROKE, BID = signup("Pg Broke", "pgbroke@x.io")
before = len(gift_rows())
rb2 = bal(RID)
r = BROKE.post(f"/user/{RID}/gift", json=STACK)
check("insufficient refused with the live flow's words",
      r.status_code == 400
      and "Not enough tickets" in r.json()["error"],
      str(r.status_code))
check("no page_gifts row, recipient untouched",
      len(gift_rows()) == before and bal(RID) == rb2)
r = BROKE.post(f"/user/{RID}/gift", json={"gift_key": "nope"})
check("unknown gift refused", r.status_code == 400
      and "Unknown gift" in r.json()["error"])
r = C.post(f"/user/{RID}/gift", json=STACK)
check("logged-out gift refused (401)", r.status_code == 401,
      str(r.status_code))

print("== 8. Self-gift refused ==")
creator_on(SID)
r = S.post(f"/user/{SID}/gift", json=STACK)
check("self-gift refused", r.status_code == 400
      and "gift yourself" in r.json()["error"], str(r.status_code))

print("== 9. Blocked pair refused BOTH directions ==")
B1, B1ID = signup("Pg Block A", "pgba@x.io")
B2, B2ID = signup("Pg Block B", "pgbb@x.io")
creator_on(B1ID); creator_on(B2ID)
grant_spendable(B1ID, 100); grant_spendable(B2ID, 100)
r = B1.post(f"/block/{B2ID}")
check("block placed", r.status_code in (200, 303),
      str(r.status_code))
r = B2.post(f"/user/{B1ID}/gift", json=STACK)
check("blocked sender -> recipient refused",
      r.status_code == 400 and "isn't available" in
      r.json()["error"], str(r.status_code))
r = B1.post(f"/user/{B2ID}/gift", json=STACK)
check("blocker -> blocked refused too",
      r.status_code == 400 and "isn't available" in
      r.json()["error"], str(r.status_code))
check("blocked gifts wrote nothing",
      gift_rows("WHERE sender_id IN (?,?)", (B1ID, B2ID)) == [])

print("== 10. Private profile: friends only ==")
P, PID_ = signup("Pg Priv", "pgpriv@x.io")
F, FID = signup("Pg Friend", "pgfr@x.io")
creator_on(PID_)
grant_spendable(FID, 100)
r = P.post("/settings/privacy", data={"private_profile": "1"})
check("profile made private", r.status_code == 303)
r = F.post(f"/friends/request/{PID_}")
check("friend request sent", r.status_code == 303,
      str(r.status_code))
r = P.post(f"/friends/accept/{FID}")
check("friend request accepted", r.status_code == 303,
      str(r.status_code))
r = S.post(f"/user/{PID_}/gift", json=STACK)
check("non-friend refused on a private profile (403)",
      r.status_code == 403 and "private" in r.json()["error"],
      str(r.status_code))
html = S.get(f"/user/{PID_}").text
check("locked profile shows no gift button",
      'id="pgift-open"' not in html)
pb = bal(PID_)
r = F.post(f"/user/{PID_}/gift", json=STACK)
check("friend CAN gift a private creator",
      r.status_code == 200, str(r.status_code))
check("private recipient earned the 10",
      bal(PID_) == (pb[0], pb[1] + 10, pb[2] + 10))

print("== 11. Pages: the OWNER earns, gated on the OWNER ==")
PO, POID = signup("Pg Page Owner", "pgpo@x.io")
PM, PMID = signup("Pg Page Member", "pgpm@x.io")
PS, PSID = signup("Pg Page Fan", "pgpf@x.io")
grant_spendable(PSID, 100)
r = PO.post("/pages", data={"name": "Gift Shop", "ptype": "seller",
                            "description": "Send the shop a gift"})
check("page created", r.status_code == 303, str(r.status_code))
PGID = dbq("SELECT id FROM pages WHERE owner_id=?", (POID,))[0]["id"]
r = PM.post(f"/page/{PGID}/join")
check("member joined the page", r.status_code in (200, 303),
      str(r.status_code))
html = PS.get(f"/page/{PGID}").text
check("page gift button hidden while OWNER creator off",
      'id="pgift-open"' not in html)
r = PS.post(f"/page/{PGID}/gift", json=STACK)
check("page gift refused while owner creator off",
      r.status_code == 403 and "Creator Mode" in r.json()["error"],
      str(r.status_code))
creator_on(PMID)  # the MEMBER turning creator must NOT open it
r = PS.post(f"/page/{PGID}/gift", json=STACK)
check("member's flag does not open the page's gifts",
      r.status_code == 403, str(r.status_code))
creator_on(POID)
html = PS.get(f"/page/{PGID}").text
check("button shows once the OWNER is a creator",
      'id="pgift-open"' in html)
check("page strip absent before any gift", "Recent gifts" not in html)
po_b, pm_b = bal(POID), bal(PMID)
r = PS.post(f"/page/{PGID}/gift", json=STACK)
d = r.json()
check("page gift ok, recipient named as the page",
      r.status_code == 200 and d.get("recipient_id") == POID
      and d.get("recipient_name") == "Gift Shop",
      str(r.status_code))
check("the OWNER earned +10/+10", bal(POID) == (po_b[0],
      po_b[1] + 10, po_b[2] + 10))
check("the member earned nothing", bal(PMID) == pm_b)
rows = gift_rows("WHERE page_id=?", (PGID,))
check("page_gifts row names the page + the owner as recipient",
      len(rows) == 1 and rows[0]["recipient_id"] == POID
      and rows[0]["sender_id"] == PSID)
tx = dbq("SELECT * FROM ticket_txns WHERE user_id=? AND "
         "kind='gift_receive' AND note='page'", (POID,))
check("owner's ledger has the note='page' receive row",
      len(tx) == 1 and tx[0]["tickets"] == 10
      and tx[0]["live_id"] is None)
html = PS.get(f"/page/{PGID}").text
check("page strip renders the gift for a viewer",
      "Recent gifts" in html and "Gift Stack" in html)
own_page = PO.get(f"/page/{PGID}").text
check("page owner sees NO gift button on their own page",
      'id="pgift-open"' not in own_page)
n = dbq("SELECT * FROM notifications WHERE user_id=? AND "
        "kind='page_gift'", (POID,))
check("page gift notified the OWNER (not the member)",
      len(n) == 1)
check("invariant holds for the page owner", invariant(POID))

print("== 12. Turning Creator Mode back off closes the door ==")
r = R.post("/settings/creator", data={})
check("switch off -> 303", r.status_code == 303)
check("creator_mode persisted off",
      dbq("SELECT creator_mode cm FROM users WHERE id=?",
          (RID,))[0]["cm"] == 0)
html = S.get(f"/user/{RID}").text
check("button gone again", 'id="pgift-open"' not in html)
r = S.post(f"/user/{RID}/gift", json=STACK)
check("route refuses again", r.status_code == 403,
      str(r.status_code))

print("== 13. The sheet script parses (node --check) ==")
creator_on(RID)
prof_html = S.get(f"/user/{RID}").text
page_html = PS.get(f"/page/{PGID}").text
node = shutil.which("node")
if node:
    for label, doc in (("profile", prof_html), ("page", page_html)):
        scripts = [s for s in
                   re.findall(r"<script>(.*?)</script>", doc, re.S)
                   if "var PG" in s]
        check(f"{label}: sheet script present", len(scripts) == 1)
        if scripts:
            p = os.path.join(TMP, f"pgift_{label}.js")
            open(p, "w").write(scripts[0])
            rr = subprocess.run([node, "--check", p],
                                capture_output=True, text=True)
            check(f"{label}: sheet script parses",
                  rr.returncode == 0, rr.stderr[:200])
else:
    check("node present for script check", False, "node missing")

print(f"\nPAGE GIFTS: {PASS[0]} passed, {FAIL[0]} failed")
sys.exit(1 if FAIL[0] else 0)
