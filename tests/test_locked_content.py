"""Build LOCKED CONTENT: creators (Creator Mode ON) sell locked
posts + locked DM attachments for tickets. Covers:
- schema: posts.lock_price/lock_adult, messages.lock_media/
  lock_price/lock_adult, content_unlocks (UNIQUE per buyer)
- seller gate: non-creator lock refused (post + DM, nothing
  created); price must be an integer 5..5000 (no silent clamp);
  page posts + polls can't be locked; a browser-direct media_key
  can never carry a lock
- locked post rendering: non-unlockers get the LOCK CARD — no body
  text, no media URL, no locked/ key, no gate URL anywhere in
  feed / profile / post page HTML; the author sees it open with
  the /locked/post/{id} gate URL; media lives in LOCKED_DIR,
  never UPLOAD_DIR
- the unlock transaction moves money EXACTLY (buyer -N spendable,
  seller gift_earned +N AND gift_lifetime +N), writes the
  content_unlocks row + both ticket_txns rows (note='unlock',
  kinds unlock_spend/unlock_receive, live_id NULL), and the
  two-ledger invariant holds on both sides
- re-unlock: "already unlocked", NO second charge; insufficient
  balance: plain refusal, no writes; seller self-unlock refused
- adult locks: unlock without confirm_adult refused (no charge),
  with it works (JSON and classic form shapes)
- post privacy runs FIRST: a friends-only locked post is
  unreachable by a non-friend — even to unlock — while a friend
  can unlock and open it
- /p/{id}: open for a logged-in buyer, teaser for everyone else,
  og never references locked media; /u/{id} grid skips locked
- DM locked: recipient sees the lock card (no caption, no media
  URL); gate 403 pre-unlock → unlock pays the seller → gate 200
  for the recipient; a third user gets 404 always and cannot
  unlock; the sender's own unlock attempt is refused; inbox
  preview reads "🔒 Locked content"
- seller notification kind='content_unlock' both surfaces;
  /tickets lines name the other party both ways; /earnings gains
  the "🔒 Locked content" line with the exact total while the
  🎁 Page gifts and Earlier-streams buckets stay untouched
- turning Creator Mode OFF leaves existing locks purchasable
  and buyers' access intact; only NEW locks are refused
"""
import os, sys, re, sqlite3, tempfile, subprocess
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
TMP = tempfile.mkdtemp(prefix="here_locked_")
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
    uid = dbq("SELECT MAX(id) m FROM users")[0]["m"]
    return cl, uid

def bal(uid):
    r = dbq("SELECT tickets_balance b, gift_earned e, "
            "gift_lifetime l FROM users WHERE id=?", (uid,))[0]
    return r["b"] or 0, r["e"] or 0, r["l"] or 0

def grant_spendable(uid, n):
    dbx("UPDATE users SET tickets_balance="
        "COALESCE(tickets_balance,0)+? WHERE id=?", (n, uid))
    dbx("INSERT INTO ticket_txns (user_id, kind, tickets, created_at) "
        "VALUES (?, 'buy', ?, ?)", (uid, n, here.now_iso()))

def invariant(uid):
    b, e, _l = bal(uid)
    spend = dbq("SELECT COALESCE(SUM(tickets),0) s FROM ticket_txns "
                "WHERE user_id=? AND kind IN ('buy','gift_send',"
                "'shout','unlock_spend')", (uid,))[0]["s"]
    earn = dbq("SELECT COALESCE(SUM(tickets),0) s FROM ticket_txns "
               "WHERE user_id=? AND kind IN ('gift_receive',"
               "'cashout_hold','cashout_return','unlock_receive')",
               (uid,))[0]["s"]
    return b == spend and e == earn

def befriend(a_client, a_id, b_client, b_id):
    r = a_client.post(f"/friends/request/{b_id}")
    assert r.status_code in (303, 302), r.status_code
    r = b_client.post(f"/friends/accept/{a_id}")
    assert r.status_code in (303, 302), r.status_code

def creator_on(uid):
    dbx("UPDATE users SET creator_mode=1 WHERE id=?", (uid,))

def creator_off(uid):
    dbx("UPDATE users SET creator_mode=0 WHERE id=?", (uid,))

def post_by_body(body):
    rows = dbq("SELECT * FROM posts WHERE body=?", (body,))
    return rows[0] if rows else None

def unlocks(ctype, cid):
    return dbq("SELECT * FROM content_unlocks WHERE content_type=? "
               "AND content_id=? ORDER BY id", (ctype, cid))

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000000020001e221bc330000"
    "000049454e44ae426082")
LOCKED_DIR = here.LOCKED_DIR

C, CID = signup("Lock Creator", "lockc@x.io")     # the seller
B, BID = signup("Lock Buyer", "lockb@x.io")       # the fan
V, VID = signup("Lock Viewer", "lockv@x.io")      # a third user
K, KID = signup("Lock Broke", "lockk@x.io")       # nearly broke
N, NID = signup("Lock Plain", "lockn@x.io")       # not a creator
ANON = TestClient(here.app, follow_redirects=False)
creator_on(CID)
befriend(C, CID, B, BID)   # B is C's fan-friend from the start;
                            # V stays a stranger throughout
grant_spendable(BID, 500)
grant_spendable(VID, 500)
grant_spendable(KID, 10)

print("== 1. Schema ==")
pcols = {r["name"] for r in dbq("PRAGMA table_info(posts)")}
mcols = {r["name"] for r in dbq("PRAGMA table_info(messages)")}
check("posts carry lock_price + lock_adult",
      {"lock_price", "lock_adult"} <= pcols)
check("messages carry lock_media + lock_price + lock_adult",
      {"lock_media", "lock_price", "lock_adult"} <= mcols)
check("content_unlocks table exists",
      bool(dbq("SELECT name FROM sqlite_master WHERE type='table' "
               "AND name='content_unlocks'")))

print("== 2. Seller gate + price discipline (posts) ==")
r = N.post("/posts", data={"kind": "text", "body": "PLAIN LOCK TRY",
                           "privacy": "public", "lock_price": "50"})
check("non-creator lock refused in plain words",
      "Creator%20Mode" in (r.headers.get("location") or ""),
      r.headers.get("location"))
check("non-creator lock created nothing",
      post_by_body("PLAIN LOCK TRY") is None)
r = C.post("/posts", data={"kind": "text", "body": "TOO CHEAP",
                           "privacy": "public", "lock_price": "4"})
check("price below 5 refused", post_by_body("TOO CHEAP") is None
      and "between" in (r.headers.get("location") or "").replace(
          "%20", " "))
r = C.post("/posts", data={"kind": "text", "body": "TOO RICH",
                           "privacy": "public", "lock_price": "5001"})
check("price above 5000 refused", post_by_body("TOO RICH") is None)
r = C.post("/posts", data={"kind": "text", "body": "NOT A NUMBER",
                           "privacy": "public", "lock_price": "abc"})
check("non-integer price refused", post_by_body("NOT A NUMBER") is None)
r = C.post("/posts", data={"kind": "poll", "body": "POLL LOCK TRY",
                           "option1": "a", "option2": "b",
                           "privacy": "public", "lock_price": "50"})
check("polls can't be locked", post_by_body("POLL LOCK TRY") is None)
r = C.post("/pages", data={"name": "Lock Shop", "ptype": "seller",
                           "description": "shop"})
PGID = dbq("SELECT id FROM pages WHERE owner_id=?", (CID,))[0]["id"]
r = C.post(f"/page/{PGID}/post", data={
    "kind": "text", "body": "PAGE LOCK TRY", "lock_price": "50"})
check("page posts can't be locked",
      post_by_body("PAGE LOCK TRY") is None
      and "Pages" in (r.headers.get("location") or "").replace(
          "%20", " "), r.headers.get("location"))
# A browser-direct media_key can never carry a lock (R2 mode only —
# the refusal must fire before the key is even looked at).
_orig_r2 = here.storage.r2_enabled
here.storage.r2_enabled = lambda: True
try:
    r = C.post("/posts", data={
        "kind": "photo", "body": "KEY LOCK TRY", "privacy": "public",
        "lock_price": "50",
        "media_key": f"posts/uu{CID}-" + "a" * 32 + ".png"})
finally:
    here.storage.r2_enabled = _orig_r2
check("locked + media_key refused plainly, nothing created",
      post_by_body("KEY LOCK TRY") is None
      and "quick" in (r.headers.get("location") or "").replace(
          "%20", " "), r.headers.get("location"))

print("== 3. Locked text post: the lock card leaks nothing ==")
C.post("/posts", data={"kind": "text", "body": "SECRET TEXT BODY",
                       "privacy": "public", "lock_price": "100"})
TP = post_by_body("SECRET TEXT BODY")
check("locked text post stored with its price",
      TP and TP["lock_price"] == 100 and not TP["lock_adult"])
TPID = TP["id"]
html = B.get("/home").text
check("buyer's feed shows the lock card",
      "Locked content" in html and "100 🎟️" in html)
check("buyer's feed hides the body", "SECRET TEXT BODY" not in html)
check("buyer's feed emits no gate URL for it",
      f"/locked/post/{TPID}" not in html)
html = B.get(f"/user/{CID}").text
check("profile shows the lock card, hides the body",
      "Locked content" in html and "SECRET TEXT BODY" not in html)
html = C.get("/home").text
check("author sees their own locked post open",
      "SECRET TEXT BODY" in html)
html = B.get("/home").text
check("composer lock fields hidden from a non-creator",
      'name="lock_price"' not in html)
creator_on(BID)
html = B.get("/home").text
check("composer lock fields shown to a creator",
      'name="lock_price"' in html and 'name="lock_adult"' in html)
creator_off(BID)

print("== 4. Locked photo post: media discipline ==")
C.post("/posts", data={"kind": "photo", "body": "PIC SECRET",
                       "privacy": "public", "lock_price": "150"},
       files={"media": ("lockedpic.png", PNG, "image/png")})
PP = post_by_body("PIC SECRET")
PPID = PP["id"]
check("locked media stored under a locked/ key",
      (PP["media"] or "").startswith("locked/posts/"), PP["media"])
KEY = PP["media"]
check("locked file lives in LOCKED_DIR",
      os.path.isfile(os.path.join(LOCKED_DIR, KEY[len("locked/"):])))
check("locked file is NOT under UPLOAD_DIR",
      not os.path.exists(os.path.join(here.UPLOAD_DIR, KEY)))
html = B.get("/home").text
check("feed hides locked photo body + key + gate URL",
      "PIC SECRET" not in html and KEY not in html
      and f"/locked/post/{PPID}" not in html)
check("feed still shows its lock card with the price",
      "Locked content" in html and "150 🎟️" in html)
html = C.get("/home").text
check("author's card points at the gate, never /uploads",
      f"/locked/post/{PPID}" in html and KEY not in html)
r = B.get(f"/locked/post/{PPID}")
check("gate refuses a non-unlocker (403)", r.status_code == 403,
      str(r.status_code))
r = ANON.get(f"/locked/post/{PPID}")
check("gate refuses anonymous (404)", r.status_code == 404,
      str(r.status_code))
r = C.get(f"/locked/post/{PPID}")
check("gate serves the author the exact bytes",
      r.status_code == 200 and r.content == PNG, str(r.status_code))

print("== 5. Unlock: exact money, once ==")
cb0, ce0, cl0 = bal(CID)
r = B.post(f"/post/{PPID}/unlock", json={})
check("unlock succeeds", r.status_code == 200 and r.json()["ok"],
      r.text[:120])
bb, be, bl = bal(BID)
check("buyer debited exactly the price",
      bb == 350 and be == 0 and bl == 0, str((bb, be, bl)))
cb, ce, cl = bal(CID)
check("seller earned + lifetime grew by exactly the price",
      ce == ce0 + 150 and cl == cl0 + 150, str((cb, ce, cl)))
u = unlocks("post", PPID)
check("one unlock record with the sale snapshot",
      len(u) == 1 and u[0]["buyer_id"] == BID
      and u[0]["seller_id"] == CID and u[0]["price"] == 150)
tx = dbq("SELECT * FROM ticket_txns WHERE note='unlock' "
         "AND gift_key=? ORDER BY id", (f"post:{PPID}",))
check("both ledger rows written (marker, kinds, live_id NULL)",
      len(tx) == 2
      and tx[0]["user_id"] == BID and tx[0]["kind"] == "unlock_spend"
      and tx[0]["tickets"] == -150 and tx[0]["live_id"] is None
      and tx[1]["user_id"] == CID
      and tx[1]["kind"] == "unlock_receive"
      and tx[1]["tickets"] == 150 and tx[1]["live_id"] is None)
check("two-ledger invariant holds for buyer + seller",
      invariant(BID) and invariant(CID))
r = B.get(f"/locked/post/{PPID}")
check("gate serves the buyer after unlock",
      r.status_code == 200 and r.content == PNG)
html = B.get("/home").text
check("buyer's feed now shows the post open via the gate",
      "PIC SECRET" in html and f"/locked/post/{PPID}" in html)
html = V.get(f"/user/{CID}").text
check("a third user still sees only the lock card",
      "PIC SECRET" not in html and "Locked content" in html)
r = B.post(f"/post/{PPID}/unlock", json={})
check("re-unlock answers 'already unlocked'",
      r.status_code == 200 and "already unlocked" in r.json()["message"])
check("re-unlock charged nothing, recorded nothing",
      bal(BID)[0] == 350 and len(unlocks("post", PPID)) == 1
      and len(dbq("SELECT * FROM ticket_txns WHERE note='unlock' "
                  "AND gift_key=?", (f"post:{PPID}",))) == 2)
r = C.post(f"/post/{PPID}/unlock", json={})
check("seller can never unlock their own post",
      r.status_code == 400 and "own content" in r.json()["error"])
nt = dbq("SELECT * FROM notifications WHERE user_id=? "
         "AND kind='content_unlock'", (CID,))
check("seller notified (kind, words, link)",
      len(nt) == 1 and "unlocked your post (+150 🎟️)" in nt[0]["text"]
      and nt[0]["link"] == f"/post/{PPID}", str(nt))
html = B.get("/tickets").text
check("buyer's /tickets line names the seller + price",
      "Unlocked Lock Creator&#39;s post (−150 🎟️)" in html)
html = C.get("/tickets").text
check("seller's /tickets line names the buyer + price",
      "Lock Buyer unlocked your post (+150 🎟️)" in html)

print("== 6. Insufficient balance: clean refusal ==")
r = K.post(f"/post/{TPID}/unlock", json={})
check("broke buyer refused plainly",
      r.status_code == 400 and "Not enough tickets" in r.json()["error"])
check("refusal wrote nothing",
      bal(KID)[0] == 10 and unlocks("post", TPID) == []
      and invariant(KID))

print("== 7. /p/{id} public page + /u/{id} grid ==")
html = B.get(f"/p/{PPID}").text
check("/p renders open for a logged-in buyer",
      "PIC SECRET" in html and f"/locked/post/{PPID}" in html)
html = V.get(f"/p/{PPID}").text
check("/p is the teaser for a non-unlocker",
      "PIC SECRET" not in html and KEY not in html
      and f"/locked/post/{PPID}" not in html)
html = ANON.get(f"/p/{PPID}").text
check("/p is the teaser anonymously (og never leaks)",
      "PIC SECRET" not in html and KEY not in html)
html = ANON.get(f"/u/{CID}").text
check("/u photo grid skips the locked post entirely",
      KEY not in html and f"/locked/post/{PPID}" not in html)

print("== 8. Adult lock: the 18+ confirm gates the sale ==")
C.post("/posts", data={"kind": "text", "body": "ADULT SECRET",
                       "privacy": "public", "lock_price": "50",
                       "lock_adult": "1"})
AP = post_by_body("ADULT SECRET")
APID = AP["id"]
check("adult flag stored", AP["lock_adult"] == 1)
bb0 = bal(BID)[0]
r = B.post(f"/post/{APID}/unlock", json={})
check("adult unlock without confirm refused",
      r.status_code == 400 and "18+" in r.json()["error"])
check("refusal charged nothing",
      bal(BID)[0] == bb0 and unlocks("post", APID) == [])
r = B.post(f"/post/{APID}/unlock", data={"confirm_adult": "1"})
check("adult unlock with the form confirm works (303)",
      r.status_code == 303, str(r.status_code))
check("confirm sale moved exactly the price",
      bal(BID)[0] == bb0 - 50 and len(unlocks("post", APID)) == 1)
html = B.get("/home").text
check("adult post opens for its buyer", "ADULT SECRET" in html)

print("== 9. Privacy first: friends-only locked post ==")
C.post("/posts", data={"kind": "text", "body": "FRIEND SECRET",
                       "privacy": "friends", "lock_price": "80"})
FP = post_by_body("FRIEND SECRET")
FPID = FP["id"]
html = V.get(f"/post/{FPID}").text
check("non-friend can't even see the locked post",
      "FRIEND SECRET" not in html and "Locked content" not in html)
r = V.post(f"/post/{FPID}/unlock", json={})
check("non-friend can't unlock what they can't see",
      r.status_code == 400 and bal(VID)[0] == 500
      and unlocks("post", FPID) == [])
html = B.get(f"/post/{FPID}").text
check("friend sees the lock card on the post page",
      "Locked content" in html and "FRIEND SECRET" not in html)
r = B.post(f"/post/{FPID}/unlock", json={})
check("friend unlocks fine", r.status_code == 200 and r.json()["ok"])
html = B.get(f"/post/{FPID}").text
check("friend reads it after unlocking", "FRIEND SECRET" in html)

print("== 10. Locked DM content ==")
r = N.post(f"/messages/{BID}", files={
    "lock_media": ("nope.png", PNG, "image/png")},
    data={"lock_price": "75", "body": "x"})
check("non-creator DM lock refused, nothing sent",
      dbq("SELECT * FROM messages WHERE sender_id=? AND lock_price>0",
          (NID,)) == [])
r = C.post(f"/messages/{BID}", files={
    "lock_media": ("dmsecret.png", PNG, "image/png")},
    data={"lock_price": "200", "body": "DM SECRET CAPTION"})
check("locked DM send redirects to the thread",
      r.status_code == 303, str(r.status_code))
DM = dbq("SELECT * FROM messages WHERE sender_id=? AND lock_price>0",
         (CID,))[0]
MID = DM["id"]
check("DM row carries key + price + stored caption",
      DM["lock_media"].startswith("locked/dm/")
      and DM["lock_price"] == 200
      and DM["body"] == "DM SECRET CAPTION", str(DM))
check("DM file lives in LOCKED_DIR",
      os.path.isfile(os.path.join(
          LOCKED_DIR, DM["lock_media"][len("locked/"):])))
html = B.get(f"/messages/{CID}").text
check("recipient sees the lock card with the price",
      "Locked content" in html and "200 🎟️" in html)
check("recipient gets NO caption, NO key, NO gate URL",
      "DM SECRET CAPTION" not in html
      and DM["lock_media"] not in html
      and f"/locked/dm/{MID}" not in html)
html = C.get(f"/messages/{BID}").text
check("sender sees their own DM open (caption + gate URL)",
      "DM SECRET CAPTION" in html and f"/locked/dm/{MID}" in html)
r = B.get(f"/locked/dm/{MID}")
check("DM gate 403 for the recipient pre-unlock",
      r.status_code == 403)
r = V.get(f"/locked/dm/{MID}")
check("DM gate 404 for a third user, always",
      r.status_code == 404)
r = ANON.get(f"/locked/dm/{MID}")
check("DM gate 404 anonymously", r.status_code == 404)
r = V.post(f"/messages/{MID}/unlock", json={})
check("a third user cannot unlock someone else's DM",
      r.status_code == 400 and bal(VID)[0] == 500
      and unlocks("dm", MID) == [])
r = C.post(f"/messages/{MID}/unlock", json={})
check("the sender never unlocks their own DM",
      r.status_code == 400 and "own content" in r.json()["error"])
bb0, cb0 = bal(BID)[0], bal(CID)[1]
r = B.post(f"/messages/{MID}/unlock", json={})
check("recipient unlock succeeds",
      r.status_code == 200 and r.json()["ok"], r.text[:120])
check("DM unlock moved the money exactly",
      bal(BID)[0] == bb0 - 200 and bal(CID)[1] == cb0 + 200)
check("DM unlock recorded", len(unlocks("dm", MID)) == 1)
r = B.get(f"/locked/dm/{MID}")
check("DM gate serves the recipient after unlock",
      r.status_code == 200 and r.content == PNG)
r = V.get(f"/locked/dm/{MID}")
check("DM gate still 404 for the third user", r.status_code == 404)
html = B.get(f"/messages/{CID}").text
check("thread shows the caption + media after unlock",
      "DM SECRET CAPTION" in html and f"/locked/dm/{MID}" in html)
nt = dbq("SELECT * FROM notifications WHERE user_id=? "
         "AND kind='content_unlock' AND text LIKE '%message%'",
         (CID,))
check("seller notified about the DM sale",
      len(nt) == 1 and "unlocked your message (+200 🎟️)"
      in nt[0]["text"], str(nt))
html = C.get("/messages?box=sent").text
check("sent-box preview reads as the lock, never the caption",
      "🔒 Locked content" in html
      and "DM SECRET CAPTION" not in html)
html = C.get("/tickets").text
check("seller's /tickets line for the DM sale",
      "Lock Buyer unlocked your message (+200 🎟️)" in html)

print("== 11. Earnings: the 🔒 line, buckets untouched ==")
# Neighbouring buckets with known contents: one page gift (10) and
# one legacy NULL-live_id gift_receive (30, applied straight to the
# ledgers the way an old live gift would have landed).
r = B.post(f"/user/{CID}/gift", json={"gift_key": "gift_stack"})
check("setup: page gift still works beside locks",
      r.status_code == 200, r.text[:100])
dbx("UPDATE users SET gift_earned=COALESCE(gift_earned,0)+30, "
    "gift_lifetime=COALESCE(gift_lifetime,0)+30 WHERE id=?", (CID,))
dbx("INSERT INTO ticket_txns (user_id, kind, tickets, live_id, "
    "note, created_at) VALUES (?, 'gift_receive', 30, NULL, '', ?)",
    (CID, here.now_iso()))
html = C.get("/earnings").text
m = re.search(r"🔒 Locked content.{0,400}?(\d[\d,]*) 🎟️", html, re.S)
check("earnings shows the 🔒 Locked content line", bool(m))
# Sales so far: 150 (photo) + 50 (adult) + 80 (friend post)
#             + 200 (DM) = 480.
check("🔒 line total is exactly the unlock sales",
      m and int(m.group(1).replace(",", "")) == 480,
      m.group(1) if m else "no line")
m2 = re.search(r"🎁 Page gifts.{0,400}?(\d[\d,]*) 🎟️", html, re.S)
check("page-gifts bucket unaffected (still its own 10)",
      m2 and int(m2.group(1).replace(",", "")) == 10)
m3 = re.search(r"Earlier streams.{0,400}?(\d[\d,]*) 🎟️", html, re.S)
check("Earlier-streams bucket unaffected (still its own 30)",
      m3 and int(m3.group(1).replace(",", "")) == 30)
check("invariant still holds for the seller", invariant(CID))

print("== 12. Creator Mode OFF: old locks live, new locks die ==")
creator_off(CID)
r = V.post(f"/post/{TPID}/unlock", json={})
check("existing lock stays purchasable after seller goes dark",
      r.status_code == 200 and r.json()["ok"], r.text[:120])
check("the late sale still pays the (now non-creator) seller",
      len(unlocks("post", TPID)) == 1)
html = B.get("/home").text
check("earlier buyers keep their access",
      "PIC SECRET" in html)
r = C.post("/posts", data={"kind": "text", "body": "DARK LOCK TRY",
                           "privacy": "public", "lock_price": "25"})
check("new locks refused once Creator Mode is off",
      post_by_body("DARK LOCK TRY") is None)

print("== 13. Shipped JS still parses ==")
rc = subprocess.run(
    ["node", "--check",
     os.path.join(os.path.dirname(__file__), "..", "static",
                  "r2upload.js")]).returncode
check("r2upload.js parses (node --check)", rc == 0)

print(f"\n{ PASS[0] } passed, { FAIL[0] } failed")
sys.exit(1 if FAIL[0] else 0)
