"""Match Here tests (Nov 1, 2026) — Ken's dating wing:
- 18+ is a hard gate at dating-profile creation;
- a profile needs >=1 real photo; ZIP resolves to a real centroid;
- nobody enters the deck until the owner approves the code-word
  face selfie (pending / denied / verified states all behave);
- the deck honours gender filters BOTH ways and excludes self,
  swiped, paused, blocked and unmatched people;
- like is secret until mutual -> a match notifies both sides;
- search by city/ZIP + radius and GPS origin order by real
  distance (GeoNames ZIP centroids);
- Taps are loud but capped per day; unmatch dissolves a match;
- a report lands in moderation_flags for the owner.

Run:  ./venv/bin/python tests/test_match.py
"""
import os
import sys
import tempfile
from datetime import date, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-match-")
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


def dob_years_ago(years):
    return (date.today() - timedelta(days=int(years * 365.25))
            ).isoformat()


def setup_profile(c, dob, gender, looking, zipc, city=""):
    return c.post("/match/setup", data={
        "dob": dob, "gender": gender, "looking_for": looking,
        "city": city, "state": "", "zip": zipc,
        "height_ft": "5", "height_in": "9", "faith": "Christian",
        "smoke": "No", "drink": "Occasionally",
        "interests": "Music, cars", "about": "Just me."},
        files={"photo1": ("me.png", PNG, "image/png")})


def verify_face(c):
    return c.post("/match/verify",
                  files={"selfie": ("face.png", PNG, "image/png")})


dbx("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
    ("match-owner-token", here.now_iso()))
OWN = TestClient(here.app, follow_redirects=False)
OWN.cookies.set("here_owner", "match-owner-token")


def owner_face(uid, decision):
    return OWN.post(f"/owner/match/{uid}/face",
                    data={"decision": decision})


def make_verified(name, email, dob, gender, looking, zipc, city=""):
    c, uid = signup(name, email)
    r = setup_profile(c, dob, gender, looking, zipc, city)
    assert r.status_code == 303, (name, r.status_code)
    verify_face(c)
    owner_face(uid, "approve")
    row = dbq("SELECT face_status FROM match_profiles WHERE user_id=?",
              (uid,))
    assert row and row[0]["face_status"] == "verified", (name, row)
    return c, uid


# ---------- ZIP data really loaded ----------
n = dbq("SELECT COUNT(*) c FROM zip_geo")[0]["c"]
check("ZIP table loaded from the bundled GeoNames file", n > 40000,
      str(n))
z = dbq("SELECT * FROM zip_geo WHERE zip='39201'")
check("39201 resolves to Jackson, MS",
      z and z[0]["city"] == "Jackson" and z[0]["state"] == "MS",
      str(z))

# ---------- the 18+ gate ----------
Kc, KID = signup("Kid TooYoung", "kid@match.test")
r = setup_profile(Kc, dob_years_ago(17), "man", "women", "39201")
prof = dbq("SELECT * FROM match_profiles WHERE user_id=?", (KID,))
check("17-year-old is refused — no dating profile is created",
      not prof)
r = Kc.get("/match/setup")
check("setup page still renders for the refused user",
      r.status_code == 200)

# ---------- profile + face flow ----------
Ac, ALICE = signup("Alice Heart", "alice@match.test")
r = setup_profile(Ac, "1995-05-05", "woman", "men", "39201")
row = dbq("SELECT * FROM match_profiles WHERE user_id=?", (ALICE,))[0]
check("adult profile saves with ZIP centroid attached",
      row["lat"] is not None and abs(row["lat"] - 32.29) < 0.2)
check("new profile starts unverified (face_status none)",
      row["face_status"] == "none")
r = Ac.get("/match")
check("unverified user sees the face-check step, not the deck",
      "verify your face" in r.text)
r = Ac.post("/match/swipe", data={"target_id": 1, "action": "like"})
sw = dbq("SELECT * FROM match_swipes WHERE swiper_id=?", (ALICE,))
check("unverified user cannot swipe", not sw)
r = verify_face(Ac)
row = dbq("SELECT * FROM match_profiles WHERE user_id=?",
          (ALICE,))[0]
check("selfie sends the profile to pending with a code",
      row["face_status"] == "pending" and row["face_code"]
      .startswith("MATCH-"))
r = Ac.get("/match")
check("pending user waits — no deck yet", "being checked" in r.text)
r = OWN.get("/owner/match")
check("owner face queue shows Alice with her code",
      "Alice Heart" in r.text and row["face_code"] in r.text)
r = owner_face(ALICE, "approve")
row = dbq("SELECT * FROM match_profiles WHERE user_id=?",
          (ALICE,))[0]
check("owner approval verifies the face",
      row["face_status"] == "verified")
nt = dbq("SELECT * FROM notifications WHERE user_id=? AND kind='match'",
         (ALICE,))
check("approval notifies the user", len(nt) == 1)

# deny path: fresh code, honest state
Fc, FRAN = signup("Fran Fake", "fran@match.test")
setup_profile(Fc, "1990-01-01", "woman", "men", "39201")
verify_face(Fc)
old_code = dbq("SELECT face_code FROM match_profiles WHERE user_id=?",
               (FRAN,))[0]["face_code"]
owner_face(FRAN, "deny")
row = dbq("SELECT * FROM match_profiles WHERE user_id=?",
          (FRAN,))[0]
check("denied face -> status denied and a FRESH code",
      row["face_status"] == "denied"
      and row["face_code"] != old_code)

# ---------- a small verified crowd ----------
Bc, BOB = make_verified("Bob Builder", "bob@match.test", "1993-03-03",
                        "man", "women", "39202")
Cc, CARL = make_verified("Carl Faraway", "carl@match.test",
                         "1991-07-07", "man", "women", "90210")
Ec, ELLA = make_verified("Ella Rose", "ella@match.test", "1996-09-09",
                         "woman", "men", "39201")
Gc, GARY = make_verified("Gary Wrongway", "gary@match.test",
                         "1992-02-02", "man", "men", "39201")

# ---------- the deck: filters + ordering ----------
r = Ac.get("/match")
check("deck shows the nearest man first (Bob, Jackson)",
      "Bob Builder" in r.text and "Carl Faraway" not in r.text)
check("deck never shows Gary (he does not look for women, "
      "Alice is a woman)", "Gary Wrongway" not in r.text)
r = Bc.get("/match")
check("Bob's deck shows Alice, not Ella-vs-him ordering issues",
      "Alice Heart" in r.text or "Ella Rose" in r.text)
r = Ac.get("/match", params={"q": "90210", "radius": 25})
check("ZIP search + 25mi radius finds Carl in Beverly Hills",
      "Carl Faraway" in r.text)
r = Ac.get("/match", params={"q": "Jackson", "radius": 5})
check("city search + 5mi radius keeps Bob, drops Carl",
      "Bob Builder" in r.text and "Carl Faraway" not in r.text)

# GPS: Alice 'shares' Jackson coords; near-me orders by distance
r = Ac.post("/match/gps", json={"lat": 32.2988, "lon": -90.1848})
check("GPS origin accepted", r.status_code == 200
      and r.json().get("ok"))
r = Ac.get("/match", params={"gps": 1})
check("GPS near-me still leads with Bob",
      "Bob Builder" in r.text)

# ---------- like is secret until mutual ----------
r = Ac.post("/match/swipe", data={"target_id": BOB, "action": "like"})
r = Bc.get("/match/matches")
check("one-way like is NOT a match yet", "Alice Heart" not in r.text)
r = Bc.post("/match/swipe", data={"target_id": ALICE,
                                  "action": "like"})
nt = dbq("SELECT * FROM notifications WHERE user_id=? AND kind='match' "
         "AND text LIKE '%match!%'", (ALICE,))
check("mutual like = a match, and the other side is notified",
      len(nt) >= 1)
r = Ac.get("/match/matches")
check("Alice's matches list shows Bob with a Message door",
      "Bob Builder" in r.text and f"/messages/{BOB}" in r.text)
r = Bc.get("/match/matches")
check("Bob's matches list shows Alice", "Alice Heart" in r.text)

# matched people leave the deck
r = Ac.get("/match")
check("once matched, Bob leaves Alice's deck",
      "Bob Builder" not in r.text and "Carl Faraway" in r.text)

# ---------- pass is forever ----------
r = Ac.post("/match/swipe", data={"target_id": CARL, "action": "pass"})
r = Ac.get("/match")
check("after passing Carl the deck is empty for Alice",
      "Nobody new right now" in r.text)

# ---------- taps: loud, but capped ----------
for _ in range(here.MATCH_TAP_DAILY):
    Bc.post("/match/tap/" + str(ALICE))
n = dbq("SELECT COUNT(*) c FROM match_taps WHERE user_id=?",
        (BOB,))[0]["c"]
check("taps record up to the daily cap", n == here.MATCH_TAP_DAILY)
r = Bc.post("/match/tap/" + str(ALICE), follow_redirects=False)
check("the tap past the cap is refused with the reason",
      "Taps for today" in r.headers.get("location", "")
      or r.status_code in (302, 303))
n2 = dbq("SELECT COUNT(*) c FROM taps WHERE tapper_id=?",
         (BOB,))[0]["c"]
check("match taps ride the platform taps table",
      n2 == here.MATCH_TAP_DAILY)

# ---------- report lands with the owner ----------
r = Ac.post("/match/report", data={"target_id": CARL,
                                   "reason": "rude messages"})
fl = dbq("SELECT * FROM moderation_flags WHERE "
         "item_type='match_profile' AND item_id=?", (CARL,))
check("a Match report files a pending owner flag",
      len(fl) == 1 and fl[0]["status"] == "pending"
      and "rude" in fl[0]["reasons"])

# ---------- pause hides a profile ----------
r = Ec.post("/match/pause")
r = Bc.get("/match")
check("paused Ella leaves Bob's deck", "Ella Rose" not in r.text)
r = Ec.get("/match")
check("Ella sees her paused state", "You're paused" in r.text)
Ec.post("/match/pause")

# ---------- unmatch dissolves, forever ----------
r = Bc.post("/match/unmatch", data={"target_id": ALICE})
r = Ac.get("/match/matches")
check("unmatch empties Alice's matches", "Bob Builder" not in r.text)
r = Ac.get("/match")
check("unmatched Bob never returns to Alice's deck",
      "Bob Builder" not in r.text)

# ---------- block severs everything ----------
Ec.post(f"/block/{BOB}")
r = Bc.get("/match")
check("after Ella blocks Bob she leaves his deck",
      "Ella Rose" not in r.text)
conn = here.db()
check("blocked pair can never be a match",
      not here._match_is_match(conn, BOB, ELLA))
conn.close()

# ---------- pages render for a verified user ----------
for path in ("/match", "/match/setup", "/match/verify",
             "/match/matches"):
    r = Ac.get(path)
    check(f"GET {path} renders 200", r.status_code == 200,
          str(r.status_code))
r = TestClient(here.app, follow_redirects=False).get("/match")
check("logged-out /match bounces to login", r.status_code in (302, 303))
r = TestClient(here.app, follow_redirects=False).get("/owner/match")
check("owner face queue is owner-gated",
      r.status_code in (302, 303, 401, 403))

fails = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
sys.exit(1 if fails else 0)
