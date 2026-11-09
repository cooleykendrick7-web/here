"""Match Here v3 + Profile Basics tests (Nov 8, 2026):
- main profiles carry city/state/hometown/birthdate; only the
  AGE is ever displayed, never the birthday;
- people search matches city/state/hometown tokens and the
  explicit ?city= / ?state= filters, filters-only included;
- dating profiles gain question prompts (one shows on the
  card), a voice intro (upload + delete), and Two Truths & A
  Lie — the lie stays hidden until the one allowed guess;
- comment-on-a-profile openers land as context messages and
  respect the opener limit;
- first-chat guardrails block phone numbers / links / other
  apps between unmatched daters only;
- the Daily Pick is stable per day and prefers rich profiles;
- Share My Date only fires match -> accepted friend;
- the post-match check-in appears at 24h+ for talking matches
  and dismisses for good.

Run:  ./venv/bin/python tests/test_match_v3.py
"""
import os
import sys
import tempfile
from datetime import date, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-v3-")
os.environ["HERE_DB"] = os.path.join(_tmp, "test.db")
os.environ["HERE_UPLOADS"] = os.path.join(_tmp, "uploads")
sys.path.insert(0, BASE)

import app as here  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

RESULTS = []
PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
       b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00"
       b"\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x00\x03\x00\x01"
       b"\x00\x00\x00\x00IEND\xaeB`\x82")
VOICE = b"\x1aE\xdf\xa3 fake-webm-voice-bytes"


def check(name, cond, extra=""):
    RESULTS.append((name, bool(cond)))
    print(("PASS " if cond else "FAIL ") + name
          + (f"  [{extra}]" if extra and not cond else ""))


def dbq(sql, args=()):
    conn = here.db()
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return rows


def dbx(sql, args=()):
    conn = here.db()
    conn.execute(sql, args)
    conn.commit()
    conn.close()


def signup(name, email):
    c = TestClient(here.app, follow_redirects=False)
    r = c.post("/signup", data={"name": name, "email": email,
                                "password": "pw123456"})
    assert r.status_code == 303, (name, r.status_code)
    uid = dbq("SELECT id FROM users WHERE email=?",
              (email,))[0]["id"]
    return c, uid


def setup_profile(c, dob, gender, looking, zipc, **extra):
    data = {
        "dob": dob, "gender": gender, "looking_for": looking,
        "city": "", "state": "", "zip": zipc,
        "height_ft": "5", "height_in": "9", "faith": "Christian",
        "smoke": "No", "drink": "Occasionally",
        "interests": "Music, cars", "about": "Just me."}
    data.update(extra)
    return c.post("/match/setup", data=data,
                  files={"photo1": ("me.png", PNG, "image/png")})


dbx("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
    ("v3-owner-token", here.now_iso()))
OWN = TestClient(here.app, follow_redirects=False)
OWN.cookies.set("here_owner", "v3-owner-token")


def make_verified(name, email, dob, gender, looking, zipc):
    c, uid = signup(name, email)
    r = setup_profile(c, dob, gender, looking, zipc)
    assert r.status_code == 303, (name, r.status_code)
    c.post("/match/verify",
           files={"selfie": ("face.png", PNG, "image/png")})
    OWN.post(f"/owner/match/{uid}/face", data={"decision": "approve"})
    row = dbq("SELECT face_status FROM match_profiles WHERE user_id=?",
              (uid,))
    assert row and row[0]["face_status"] == "verified", (name, row)
    return c, uid


def msgs(a, b):
    return dbq("SELECT * FROM messages WHERE (sender_id=? AND "
               "receiver_id=?) OR (sender_id=? AND receiver_id=?) "
               "ORDER BY id", (a, b, b, a))


# ================= people =================
Ac, ALICE = make_verified("Alice Heart", "alice@v3.test",
                          "1995-05-05", "woman", "men", "39201")
Bc, BOB = make_verified("Bob Stone", "bob@v3.test",
                        "1993-03-03", "man", "women", "39201")
Cc, CAROL = make_verified("Carol May", "carol@v3.test",
                          "1996-06-06", "woman", "men", "39201")
Dc, DAVE = make_verified("Dave Ray", "dave@v3.test",
                         "1994-04-04", "man", "women", "39201")
Ec, EVE = signup("Eve Plain", "eve@v3.test")

# ================= profile basics =================
r = Ac.post("/settings/profile", data={
    "name": "Alice Heart", "bio": "Just me.",
    "city": "Bismarck", "state": "ND", "hometown": "Jackson",
    "birthdate": "1995-05-05"})
check("profile POST accepts the basics fields", r.status_code == 303,
      str(r.status_code))
row = dbq("SELECT city, state, hometown, birthdate FROM users "
          "WHERE id=?", (ALICE,))[0]
check("basics saved on the user row",
      row["city"] == "Bismarck" and row["state"] == "ND"
      and row["hometown"] == "Jackson"
      and row["birthdate"] == "1995-05-05", str(dict(row)))
r = Ec.get(f"/user/{ALICE}")
age = here._match_age("1995-05-05")
check("profile page shows city/state, age and hometown",
      "Bismarck" in r.text and "Age " + str(age) in r.text
      and "From Jackson" in r.text)
check("profile page NEVER shows the full birthday",
      "1995-05-05" not in r.text)
r = Ac.get("/settings/profile")
check("edit form carries city/state/hometown/birthdate inputs",
      'name="city"' in r.text and 'name="state"' in r.text
      and 'name="hometown"' in r.text and 'name="birthdate"' in r.text
      and "Bismarck" in r.text)

# ================= people search =================
r = Ec.get("/search", params={"q": "Bismarck"})
check("search box token matches a member's city",
      "Alice Heart" in r.text)
r = Ec.get("/search", params={"q": "Jackson"})
check("search box token matches a member's hometown",
      "Alice Heart" in r.text)
r = Ec.get("/search", params={"city": "Bismarck", "state": "ND"})
check("filters-only search (no q) finds by city + state",
      "Alice Heart" in r.text)
r = Ec.get("/search", params={"city": "Nowhereville"})
check("filters-only search with no match finds nobody",
      "Alice Heart" not in r.text)
r = Ec.get("/search")
check("search page offers city + state filter inputs",
      'name="city"' in r.text and 'name="state"' in r.text)
r = Ec.get("/search", params={"q": "Alice"})
check("search results show the basics line (city + age)",
      "Bismarck" in r.text and ("Age " + str(age)) in r.text)

# ================= prompts + TTL on setup =================
Q1 = here.MATCH_PROMPTS[1]
r = Bc.post("/match/setup", data={
    "dob": "1993-03-03", "gender": "man", "looking_for": "women",
    "city": "", "state": "", "zip": "39201", "height_ft": "6",
    "height_in": "0", "faith": "Christian", "smoke": "No",
    "drink": "Socially", "interests": "Trucks", "about": "Bob here.",
    "prompt_q1": Q1, "prompt_a1": "Slow Sunday mornings.",
    "prompt_q2": "Not a real prompt", "prompt_a2": "should drop",
    "ttl_s1": "I have been to 40 states",
    "ttl_s2": "I once won a pie contest",
    "ttl_s3": "I can juggle", "ttl_lie": "2"},
    files={"photo1": ("me.png", PNG, "image/png")})
check("setup re-post with prompts + TTL saves", r.status_code == 303)
row = dbq("SELECT * FROM match_profiles WHERE user_id=?",
          (BOB,))[0]
check("prompt 1 saved; a made-up prompt question is refused",
      row["prompt_q1"] == Q1
      and row["prompt_a1"] == "Slow Sunday mornings."
      and row["prompt_q2"] == "" and row["prompt_a2"] == "",
      str(dict(row)))
check("TTL statements + lie saved",
      row["ttl_s2"] == "I once won a pie contest"
      and row["ttl_lie"] == 2)

conn = here.db()
bprof = conn.execute("SELECT * FROM match_profiles WHERE user_id=?",
                     (BOB,)).fetchone()
aprof = conn.execute("SELECT * FROM match_profiles WHERE user_id=?",
                     (ALICE,)).fetchone()
card = here._match_card(conn, bprof, aprof,
                        here._match_my_origin(aprof))
conn.close()
check("card carries the first prompt",
      card["prompt_q"] == Q1
      and card["prompt_a"] == "Slow Sunday mornings.")
check("card knows the TTL game is playable", card["ttl"] is True)
check("card is fresh when the member was just seen",
      card["fresh"] is True)
r = Ac.get(f"/match/view/{BOB}")
check("view page shows prompt answer + TTL statements",
      "Slow Sunday mornings." in r.text
      and "I once won a pie contest" in r.text)
check("view page does NOT reveal which statement is the lie",
      "🤥" not in r.text and "This is the lie" in r.text)
check("view page offers the specific-comment box",
      f"/match/view/{BOB}/comment" in r.text)

# ================= voice intro =================
r = Bc.post("/match/setup", data={
    "dob": "1993-03-03", "gender": "man", "looking_for": "women",
    "city": "", "state": "", "zip": "39201", "height_ft": "6",
    "height_in": "0", "faith": "Christian", "smoke": "No",
    "drink": "Socially", "interests": "Trucks", "about": "Bob here."},
    files={"photo1": ("me.png", PNG, "image/png"),
           "voice": ("intro.webm", VOICE, "audio/webm")})
row = dbq("SELECT voice_note FROM match_profiles WHERE user_id=?",
          (BOB,))[0]
check("voice intro upload saves on the profile",
      bool(row["voice_note"]), str(dict(row)))
r = Ac.get(f"/match/view/{BOB}")
check("view page plays the voice intro",
      "<audio" in r.text and "/uploads/" + row["voice_note"] in r.text)
conn = here.db()
bprof = conn.execute("SELECT * FROM match_profiles WHERE user_id=?",
                     (BOB,)).fetchone()
aprof = conn.execute("SELECT * FROM match_profiles WHERE user_id=?",
                     (ALICE,)).fetchone()
card = here._match_card(conn, bprof, aprof,
                        here._match_my_origin(aprof))
conn.close()
check("card flags the voice intro", card["has_voice"] is True)
r = Bc.post("/match/setup", data={
    "dob": "1993-03-03", "gender": "man", "looking_for": "women",
    "city": "", "state": "", "zip": "39201", "height_ft": "6",
    "height_in": "0", "faith": "Christian", "smoke": "No",
    "drink": "Socially", "interests": "Trucks", "about": "Bob here.",
    "voice_del": "1"},
    files={"photo1": ("me.png", PNG, "image/png")})
row = dbq("SELECT voice_note FROM match_profiles WHERE user_id=?",
          (BOB,))[0]
check("voice intro delete clears it", row["voice_note"] == "")

# stale member is not "fresh"
dbx("UPDATE users SET last_seen_at=? WHERE id=?",
    ("2020-01-01T00:00:00+00:00", BOB))
conn = here.db()
bprof = conn.execute("SELECT * FROM match_profiles WHERE user_id=?",
                     (BOB,)).fetchone()
aprof = conn.execute("SELECT * FROM match_profiles WHERE user_id=?",
                     (ALICE,)).fetchone()
card = here._match_card(conn, bprof, aprof,
                        here._match_my_origin(aprof))
conn.close()
check("a member unseen for a week+ is NOT fresh",
      card["fresh"] is False)

# ================= Two Truths & A Lie game =================
r = Ac.post(f"/match/guess/{BOB}", data={"choice": "1"})
check("a wrong guess redirects with the verdict",
      r.status_code == 303 and "guess=wrong" in r.headers["location"],
      r.headers.get("location", ""))
g = dbq("SELECT * FROM match_guesses WHERE viewer_id=? AND "
        "target_id=?", (ALICE, BOB))
check("the guess is recorded once, marked wrong",
      len(g) == 1 and g[0]["correct"] == 0)
r = Ac.get(f"/match/view/{BOB}")
check("after guessing, the lie is revealed and buttons are gone",
      "🤥" in r.text and "This is the lie" not in r.text)
n = dbq("SELECT COUNT(*) c FROM notifications WHERE user_id=? "
        "AND kind='match_ttl'", (BOB,))[0]["c"]
check("a wrong guess does not ping the profile owner", n == 0)
r = Cc.post(f"/match/guess/{BOB}", data={"choice": "2"})
g = dbq("SELECT * FROM match_guesses WHERE viewer_id=? AND "
        "target_id=?", (CAROL, BOB))
check("a right guess is recorded as correct",
      len(g) == 1 and g[0]["correct"] == 1)
n = dbq("SELECT COUNT(*) c FROM notifications WHERE user_id=? "
        "AND kind='match_ttl'", (BOB,))[0]["c"]
check("a right guess pings the profile owner", n == 1)
Ac.post(f"/match/guess/{BOB}", data={"choice": "2"})
g = dbq("SELECT * FROM match_guesses WHERE viewer_id=? AND "
        "target_id=?", (ALICE, BOB))
check("nobody gets a second guess", len(g) == 1
      and g[0]["correct"] == 0)

# ================= comment opener =================
before = len(msgs(DAVE, ALICE))
r = Dc.post(f"/match/view/{ALICE}/comment",
            data={"about": "your photo",
                  "body": "That river shot is great."})
check("comment opener redirects into the thread",
      r.status_code == 303
      and f"/messages/{ALICE}" in r.headers["location"],
      r.headers.get("location", ""))
m = msgs(DAVE, ALICE)
check("comment lands as a message with its context line",
      len(m) == before + 1
      and m[-1]["body"].startswith("💬 About your photo:"),
      m[-1]["body"] if m else "")
for i in range(2):
    Dc.post(f"/match/view/{ALICE}/comment",
            data={"about": "your profile", "body": f"more {i}"})
n3 = len(msgs(DAVE, ALICE))
Dc.post(f"/match/view/{ALICE}/comment",
        data={"about": "your profile", "body": "one too many"})
check("the opener limit still caps comment openers",
      len(msgs(DAVE, ALICE)) == n3)

# ================= first-chat guardrails =================
r = Cc.post(f"/messages/{BOB}", data={"body": "call me 601 555 1234"})
check("a phone number in a first dater message is blocked",
      len(msgs(CAROL, BOB)) == 0)
r = Cc.post(f"/messages/{BOB}",
            data={"body": "find me at example.com"})
check("a link in a first dater message is blocked",
      len(msgs(CAROL, BOB)) == 0)
r = Cc.post(f"/messages/{BOB}",
            data={"body": "add me on snapchat sometime"})
check("an off-app handle in a first dater message is blocked",
      len(msgs(CAROL, BOB)) == 0)
r = Cc.post(f"/messages/{BOB}", data={"body": "Hey! Nice truck."})
check("a clean first message goes through",
      len(msgs(CAROL, BOB)) == 1)
r = Ec.post(f"/messages/{BOB}", data={"body": "my number is 6015551234"})
check("guardrails do NOT apply outside Match pairs",
      len(msgs(EVE, BOB)) == 1)
Cc.post("/match/swipe", data={"target_id": BOB, "action": "like"})
Bc.post("/match/swipe", data={"target_id": CAROL, "action": "like"})
n = len(msgs(CAROL, BOB))
r = Cc.post(f"/messages/{BOB}", data={"body": "ok call me 601 555 1234"})
check("once matched, the guardrail stands down",
      len(msgs(CAROL, BOB)) == n + 1)

# ================= Daily Pick =================
r = Ac.get("/match/pick")
check("Daily Pick page renders", r.status_code == 200
      and "Daily Pick" in r.text)
row = dbq("SELECT * FROM match_daily_picks WHERE viewer_id=?",
          (ALICE,))
check("the pick is recorded for today",
      len(row) == 1
      and row[0]["day"] == here._today_chicago().isoformat(),
      str([dict(x) for x in row]))
check("the pick prefers the profile with a prompt (Bob)",
      row and row[0]["target_id"] == BOB,
      str(row[0]["target_id"] if row else None))
Ac.get("/match/pick")
row2 = dbq("SELECT * FROM match_daily_picks WHERE viewer_id=?",
           (ALICE,))
check("the pick is stable across the day", len(row2) == 1
      and row2[0]["target_id"] == row[0]["target_id"])

# ================= Share My Date =================
Ac.post("/match/swipe", data={"target_id": BOB, "action": "like"})
Bc.post("/match/swipe", data={"target_id": ALICE, "action": "like"})
Ac.post("/friends/request/" + str(CAROL))
Cc.post("/friends/accept/" + str(ALICE))
r = Ac.post("/match/share-date", data={
    "target_id": BOB, "friend_id": DAVE,
    "when_text": "Friday 7pm", "where_text": "Applebee's"})
check("sharing with a non-friend is refused",
      not dbq("SELECT * FROM match_date_shares WHERE user_id=?",
              (ALICE,)))
r = Ac.post("/match/share-date", data={
    "target_id": BOB, "friend_id": CAROL,
    "when_text": "Friday 7pm", "where_text": "Applebee's on State St"})
sh = dbq("SELECT * FROM match_date_shares WHERE user_id=?",
         (ALICE,))
check("Share My Date records the plan",
      len(sh) == 1 and sh[0]["friend_id"] == CAROL
      and sh[0]["when_text"] == "Friday 7pm", str([dict(x) for x in sh]))
m = [x for x in msgs(ALICE, CAROL) if "Share My Date" in x["body"]]
check("the friend receives the plan as a message",
      len(m) == 1 and "Applebee's on State St" in m[0]["body"])
r = Ac.get("/match/matches")
check("matches page shows Share My Date + who it went to",
      "Share My Date" in r.text and "Carol May" in r.text)

# ================= post-match check-in =================
old = (date.today() - timedelta(days=3)).isoformat() \
    + "T12:00:00+00:00"
dbx("UPDATE match_swipes SET created_at=? WHERE "
    "((swiper_id=? AND target_id=?) OR (swiper_id=? AND "
    "target_id=?)) AND action='like'", (old, ALICE, BOB, BOB, ALICE))
Ac.post(f"/messages/{BOB}", data={"body": "Friday works for me!"})
Bc.post(f"/messages/{ALICE}", data={"body": "See you there."})
r = Ac.get("/match/matches")
check("a 24h+ talking match earns a check-in",
      "Checking in" in r.text)
n = dbq("SELECT COUNT(*) c FROM notifications WHERE user_id=? "
        "AND kind='match_checkin'", (ALICE,))[0]["c"]
check("the check-in pings once", n == 1)
Ac.get("/match/matches")
n = dbq("SELECT COUNT(*) c FROM notifications WHERE user_id=? "
        "AND kind='match_checkin'", (ALICE,))[0]["c"]
check("reopening Matches does not re-ping", n == 1)
r = Ac.post(f"/match/checkin/{BOB}/ok")
row = dbq("SELECT dismissed FROM match_checkins WHERE user_id=? "
          "AND other_id=?", (ALICE, BOB))
check("All good dismisses the check-in",
      row and row[0]["dismissed"] == 1)
r = Ac.get("/match/matches")
check("a dismissed check-in stays gone", "Checking in" not in r.text)
r = Cc.get("/match/matches")
check("a brand-new match gets no check-in yet",
      "Checking in" not in r.text)

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
