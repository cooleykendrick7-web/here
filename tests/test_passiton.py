"""Owner Pack P5 — Pass It On: gift from EARNED tickets.

Covers (via the profile/page gift route, which shares _gift_move
with post/track/live gifts):
- echo gift: sender gift_earned -N, recipient gift_earned +N,
  1:1; tickets_balance untouched on both sides
- conservation: total gift_earned across users unchanged by echo
- ranks/levels: sender's gift_send total (gifter rank input) and
  recipient's gift_lifetime (streamer level input) do NOT move
- ledger: twin 'gift_echo' rows (sender -, recipient +), note kept
- normal gift unchanged: balance debit, lifetime grows, gift_send /
  gift_receive rows, response source='balance'
- insufficient earned refused cleanly (400, no writes, balances
  intact); free gifts never echo (spend the balance path)
"""
import os, sys, sqlite3, tempfile
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
TMP = tempfile.mkdtemp(prefix="here_passiton_")
os.environ["HERE_DB"] = os.path.join(TMP, "t.db")
os.environ["HERE_UPLOADS"] = os.path.join(TMP, "up")
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

def bal(uid):
    r = dbq("SELECT tickets_balance b, gift_earned e, "
            "gift_lifetime l FROM users WHERE id=?", (uid,))[0]
    return r["b"] or 0, r["e"] or 0, r["l"] or 0

def seed_earned(uid, n):
    """Give a user n earned tickets the honest way (a real
    gift_receive ledger row), so lifetime and invariants agree."""
    dbx("UPDATE users SET gift_earned=COALESCE(gift_earned,0)+?, "
        "gift_lifetime=COALESCE(gift_lifetime,0)+? WHERE id=?",
        (n, n, uid))
    dbx("INSERT INTO ticket_txns (user_id, kind, tickets, note, "
        "created_at) VALUES (?, 'gift_receive', ?, 'seed', ?)",
        (uid, n, here.now_iso()))

def seed_spendable(uid, n):
    dbx("UPDATE users SET tickets_balance="
        "COALESCE(tickets_balance,0)+? WHERE id=?", (n, uid))
    dbx("INSERT INTO ticket_txns (user_id, kind, tickets, created_at) "
        "VALUES (?, 'buy', ?, ?)", (uid, n, here.now_iso()))

def sent_total(uid):
    return dbq("SELECT COALESCE(SUM(-tickets),0) s FROM ticket_txns "
               "WHERE user_id=? AND kind='gift_send'",
               (uid,))[0]["s"]

def echo_rows():
    return dbq("SELECT * FROM ticket_txns WHERE kind='gift_echo' "
               "ORDER BY id")

def total_earned():
    return dbq("SELECT COALESCE(SUM(gift_earned),0) s FROM users"
               )[0]["s"]

def invariant(uid):
    b, e, _l = bal(uid)
    spend = dbq("SELECT COALESCE(SUM(tickets),0) s FROM ticket_txns "
                "WHERE user_id=? AND kind IN ('buy','gift_send',"
                "'shout')", (uid,))[0]["s"]
    earn = dbq("SELECT COALESCE(SUM(tickets),0) s FROM ticket_txns "
               "WHERE user_id=? AND kind IN ('gift_receive',"
               "'cashout_hold','cashout_return','gift_echo')",
               (uid,))[0]["s"]
    return b == spend and e == earn

STACK = {"gift_key": "gift_stack"}          # Gift Stack, 10 tickets
S, SID = signup("Echo Sender", "echo-s@x.io")
R, RID = signup("Echo Recip", "echo-r@x.io")
dbx("UPDATE users SET creator_mode=1 WHERE id=?", (RID,))
seed_spendable(SID, 500)
seed_earned(SID, 300)

print("== 1. Echo gift moves earned 1:1, balance untouched ==")
before_total = total_earned()
r = S.post(f"/user/{RID}/gift",
           json={**STACK, "source": "earned"})
check("echo gift ok", r.status_code == 200 and r.json()["ok"],
      str(r.status_code) + " " + r.text[:120])
check("response says source=earned",
      r.json().get("source") == "earned")
sb, se, sl = bal(SID)
rb, re_, rl = bal(RID)
check("sender earned 300 -> 290", se == 290, str(se))
check("sender spendable untouched (500)", sb == 500, str(sb))
check("recipient earned 0 -> 10", re_ == 10, str(re_))
check("recipient spendable untouched (0)", rb == 0, str(rb))
check("total gift_earned conserved",
      total_earned() == before_total,
      f"{before_total} -> {total_earned()}")
rows = echo_rows()
check("two gift_echo ledger rows", len(rows) == 2, str(len(rows)))
check("echo rows are -10 sender / +10 recipient",
      rows and rows[0]["user_id"] == SID and rows[0]["tickets"] == -10
      and rows[1]["user_id"] == RID and rows[1]["tickets"] == 10)
check("page_gifts row still written (the gift itself is real)",
      len(dbq("SELECT * FROM page_gifts WHERE sender_id=?",
              (SID,))) == 1)

print("== 2. Echo does not feed ranks or levels ==")
check("sender gift_send total still 0 (no rank credit)",
      sent_total(SID) == 0, str(sent_total(SID)))
check("recipient gift_lifetime still 0 (no level credit)",
      rl == 0, str(rl))
check("sender gift_lifetime unchanged (300 seed)",
      sl == 300, str(sl))
check("two-ledger invariants hold after echo",
      invariant(SID) and invariant(RID))

print("== 3. Normal gift unchanged ==")
r = S.post(f"/user/{RID}/gift", json=STACK)
check("normal gift ok, source=balance",
      r.status_code == 200
      and r.json().get("source") == "balance")
sb, se, _ = bal(SID)
rb, re_, rl = bal(RID)
check("sender spendable 500 -> 490", sb == 490, str(sb))
check("sender earned stays 290", se == 290, str(se))
check("recipient earned 10 -> 20", re_ == 20, str(re_))
check("recipient lifetime grows to 10", rl == 10, str(rl))
check("sender gift_send total now 10", sent_total(SID) == 10)
check("invariants hold after normal gift",
      invariant(SID) and invariant(RID))

print("== 4. Insufficient earned refused cleanly ==")
P, PID = signup("Echo Poor", "echo-p@x.io")
seed_earned(PID, 5)
n_echo = len(echo_rows())
r = P.post(f"/user/{RID}/gift",
           json={**STACK, "source": "earned"})
check("echo with 5 earned < 10 refused 400",
      r.status_code == 400, str(r.status_code))
check("error names the earned shortfall",
      "earned" in (r.json().get("error") or "").lower(),
      r.text[:120])
check("no echo rows written by the refusal",
      len(echo_rows()) == n_echo)
pb, pe, _pl = bal(PID)
check("poor sender untouched (earned 5, balance 0)",
      pe == 5 and pb == 0, f"{pb}/{pe}")
check("recipient earned unchanged by refusal",
      bal(RID)[1] == 20, str(bal(RID)[1]))

print("== 5. Unknown source value falls back to balance ==")
r = S.post(f"/user/{RID}/gift",
           json={**STACK, "source": "mystery"})
check("bogus source treated as balance gift",
      r.status_code == 200
      and r.json().get("source") == "balance")
check("spendable debited again (490 -> 480)",
      bal(SID)[0] == 480, str(bal(SID)[0]))

print(f"\n{PASS[0]} passed, {FAIL[0]} failed")
sys.exit(1 if FAIL[0] else 0)
