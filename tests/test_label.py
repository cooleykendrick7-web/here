"""THE LABEL tests (Oct 29): Verified Artist applications +
owner grant, the golden badge on profiles, track likes,
track gifts (verified + own-rights only), the digital
contract (offer -> typed sign -> PDF -> terminate either
side), and the 3% label fee at cash-out feeding the Artist
Fund — with the decline path returning the fee in full.

Run:  ./venv/bin/python tests/test_label.py
"""
import io
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-label-")
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


MP3 = b"ID3" + b"\x00" * 2048


def make_artist(c, uid, mode="artist"):
    c.post("/settings/media-mode", data={"media_mode": mode},
           follow_redirects=False)
    c.post("/settings/creator", data={"creator_mode": "1"},
           follow_redirects=False)


def upload(c, title, rights):
    return c.post("/media/upload",
                  data={"title": title, "rights": rights},
                  files={"file": ("s.mp3", io.BytesIO(MP3),
                                  "audio/mpeg")},
                  follow_redirects=False)


# ---------- people ----------
Ac, ART = signup("Ari Artist", "ari@label.test")
Fc, FAN = signup("Fred Fan", "fred@label.test")
Uc, UNV = signup("Uma Unverified", "uma@label.test")

OWN = TestClient(here.app, follow_redirects=False)
dbx("INSERT INTO owner_sessions(token, created_at) VALUES(?,?)",
    ("label-owner-token", here.now_iso()))
OWN.cookies.set("here_owner", "label-owner-token")

# ---------- application door ----------
r = Ac.post("/settings/artist-apply", data={"note": "too soon"},
            follow_redirects=False)
check("apply refused before Artist mode",
      "err-mode" in r.headers.get("location", ""))
make_artist(Ac, ART)
r = upload(Ac, "First Song", "own")
OWN_TRACK = dbq("SELECT id FROM media_tracks WHERE user_id=? "
                "AND title='First Song'", (ART,))[0]["id"]
r = upload(Ac, "Cover Tune", "notown")
COVER_TRACK = dbq("SELECT id FROM media_tracks WHERE user_id=? "
                  "AND title='Cover Tune'", (ART,))[0]["id"]
r = Ac.post("/settings/artist-apply",
            data={"note": "I make southern soul."},
            follow_redirects=False)
check("application lands",
      "applied" in r.headers.get("location", "")
      and dbq("SELECT status FROM artist_applications WHERE "
              "user_id=?", (ART,))[0]["status"] == "new")
r = Ac.post("/settings/artist-apply", data={"note": "again"},
            follow_redirects=False)
check("double application refused",
      "err-pending" in r.headers.get("location", ""))
r = OWN.get("/owner/artists")
check("owner artists page lists the application",
      r.status_code == 200 and "Ari Artist" in r.text
      and "southern soul" in r.text)
check("owner can LISTEN to the applicant's tracks in-panel",
      "<audio" in r.text and "/uploads/" in r.text
      and "First Song" in r.text)
APP_ID = dbq("SELECT id FROM artist_applications WHERE user_id=?",
             (ART,))[0]["id"]
r = OWN.post(f"/owner/artists/app/{APP_ID}/approve",
             follow_redirects=False)
check("owner approval verifies the artist",
      dbq("SELECT artist_verified FROM users WHERE id=?",
          (ART,))[0]["artist_verified"] == 1
      and dbq("SELECT status FROM artist_applications WHERE "
              "id=?", (APP_ID,))[0]["status"] == "approved")
r = Fc.get(f"/user/{ART}")
check("golden badge renders on the profile",
      "abadge" in r.text and "HERE Verified Artist" in r.text)

# ---------- owner direct grant ----------
make_artist(Uc, UNV)
r = upload(Uc, "Uma Song", "own")
UMA_TRACK = dbq("SELECT id FROM media_tracks WHERE user_id=?",
                (UNV,))[0]["id"]

# ---------- track gifts: the gates ----------
dbx("UPDATE users SET tickets_balance=500 WHERE id=?", (FAN,))
r = Fc.post(f"/media/{UMA_TRACK}/gift",
            json={"gift_key": "money_bag"})
check("gift to UNVERIFIED artist refused",
      r.status_code == 403)
r = Fc.post(f"/media/{COVER_TRACK}/gift",
            json={"gift_key": "money_bag"})
check("gift to not-own-rights track refused",
      r.status_code == 403)
r = Fc.post(f"/media/{OWN_TRACK}/gift",
            json={"gift_key": "money_bear"})
rose_cost = here.GIFT_CATALOG["money_bear"]["tickets"]
check("gift to verified own-rights track lands",
      r.status_code == 200 and r.json().get("ok"))
check("artist earned the tickets",
      dbq("SELECT gift_earned FROM users WHERE id=?",
          (ART,))[0]["gift_earned"] == rose_cost)
check("fan was charged",
      dbq("SELECT tickets_balance FROM users WHERE id=?",
          (FAN,))[0]["tickets_balance"] == 500 - rose_cost)
check("track gift ledger lines written",
      dbq("SELECT COUNT(*) c FROM ticket_txns WHERE note='track' "
          "AND kind='gift_receive' AND user_id=?",
          (ART,))[0]["c"] == 1)
r = Ac.post(f"/media/{OWN_TRACK}/gift",
            json={"gift_key": "money_bear"})
check("self-gift refused", r.status_code == 400)

# ---------- track likes ----------
r = Fc.post(f"/media/{OWN_TRACK}/like")
check("fan like lands", r.status_code == 200
      and r.json().get("liked") is True
      and r.json().get("likes") == 1)
r = Fc.post(f"/media/{OWN_TRACK}/like")
check("second tap unlikes", r.json().get("liked") is False
      and r.json().get("likes") == 0)
r = Ac.post(f"/media/{OWN_TRACK}/like")
check("self-like lands (Ken's order: the heart works on your "
      "own uploads)", r.status_code == 200
      and r.json().get("liked") is True
      and r.json().get("likes") == 1)
r = Ac.post(f"/media/{OWN_TRACK}/like")
check("self un-like lands too", r.json().get("liked") is False
      and r.json().get("likes") == 0)
Fc.post(f"/media/{OWN_TRACK}/like")

# ---------- the contract ----------
r = Ac.get("/artist/contract")
check("contract page shows the road before any offer",
      r.status_code == 200 and "No offer yet" in r.text)
r = OWN.post(f"/owner/artists/{ART}/offer",
             follow_redirects=False)
check("owner offer opens the contract",
      dbq("SELECT artist_contract FROM users WHERE id=?",
          (ART,))[0]["artist_contract"] == "offered"
      and dbq("SELECT status FROM artist_contracts WHERE "
              "user_id=?", (ART,))[0]["status"] == "offered")
r = Ac.get("/artist/contract")
check("offered artist sees the sign form",
      "Sign it" in r.text and "signed_name" in r.text)
r = Ac.post("/artist/contract/sign",
            data={"signed_name": "Ari Artist"},
            follow_redirects=False)
check("signing without both boxes refused",
      "err=form" in r.headers.get("location", ""))
r = Ac.post("/artist/contract/sign",
            data={"signed_name": "Ari Artist",
                  "agree_read": "1", "agree_sign": "1"},
            follow_redirects=False)
crow = dbq("SELECT * FROM artist_contracts WHERE user_id=?",
           (ART,))[0]
check("typed signature signs the deal",
      crow["status"] == "signed" and crow["signed_at"]
      and crow["signed_name"] == "Ari Artist"
      and dbq("SELECT artist_contract FROM users WHERE id=?",
              (ART,))[0]["artist_contract"] == "signed")
r = Ac.get("/artist/contract.pdf")
check("signed contract downloads as a real PDF",
      r.status_code == 200 and r.content[:5] == b"%PDF-"
      and b"Ari Artist" in r.content)
r = Fc.get("/artists")
check("program page lists the verified artist",
      r.status_code == 200 and "Ari Artist" in r.text
      and "abadge" in r.text)

# ---------- the 3% fee at cash-out ----------
dbx("UPDATE users SET gift_earned=10000 WHERE id=?", (ART,))
r = Ac.post("/tickets/cashout",
            data={"payout_note": "Cash App $Ari"},
            follow_redirects=False)
pr = dbq("SELECT * FROM payout_requests WHERE user_id=?",
         (ART,))[0]
check("signed artist cash-out takes the 3% fee",
      pr["fee_tickets"] == 300 and pr["tickets"] == 9700,
      f"fee={pr['fee_tickets']} tickets={pr['tickets']}")
check("fee dollars are exact",
      pr["dollars_cents"] == 4850)
fee = dbq("SELECT * FROM label_fees WHERE user_id=?", (ART,))
check("label fee ledger row written",
      len(fee) == 1 and fee[0]["tickets"] == 300
      and fee[0]["cents"] == 150)
_conn = here.db()
fund = here._fund_cents(_conn)
_conn.close()
check("Artist Fund received the fee", fund == 150,
      f"fund={fund}")
r = OWN.post(f"/owner/payouts/{pr['id']}/declined",
             follow_redirects=False)
check("declined cash-out returns EVERYTHING incl. the fee",
      dbq("SELECT gift_earned FROM users WHERE id=?",
          (ART,))[0]["gift_earned"] == 10000)
_conn = here.db()
fund = here._fund_cents(_conn)
_conn.close()
check("decline pulls the fee back out of the fund", fund == 0)
check("declined fee ledger row removed",
      dbq("SELECT COUNT(*) c FROM label_fees WHERE user_id=?",
          (ART,))[0]["c"] == 0)

# unsigned artist: no fee, ever
dbx("UPDATE users SET gift_earned=5000 WHERE id=?", (UNV,))
r = Uc.post("/tickets/cashout",
            data={"payout_note": "bank"}, follow_redirects=False)
pr2 = dbq("SELECT * FROM payout_requests WHERE user_id=?",
          (UNV,))[0]
check("unsigned artist cash-out is untouched",
      pr2["fee_tickets"] == 0 and pr2["tickets"] == 5000
      and pr2["dollars_cents"] == 2500)

# ---------- termination: artist side ----------
r = Ac.post("/artist/contract/terminate",
            follow_redirects=False)
check("artist can end their own contract",
      dbq("SELECT artist_contract FROM users WHERE id=?",
          (ART,))[0]["artist_contract"] == "ended"
      and dbq("SELECT ended_by FROM artist_contracts WHERE "
              "user_id=?", (ART,))[0]["ended_by"] == "artist")
check("termination never touches the wallet",
      dbq("SELECT gift_earned FROM users WHERE id=?",
          (ART,))[0]["gift_earned"] == 10000)
r = Ac.post("/tickets/cashout",
            data={"payout_note": "Cash App $Ari"},
            follow_redirects=False)
pr3 = dbq("SELECT * FROM payout_requests WHERE user_id=? AND "
          "status='pending'", (ART,))[0]
check("no fee after the contract ends",
      pr3["fee_tickets"] == 0 and pr3["tickets"] == 10000)

# ---------- termination: owner side + revoke ----------
OWN.post(f"/owner/artists/{UNV}/verify", follow_redirects=False)
OWN.post(f"/owner/artists/{UNV}/offer", follow_redirects=False)
Uc.post("/artist/contract/sign",
        data={"signed_name": "Uma Unverified",
              "agree_read": "1", "agree_sign": "1"},
        follow_redirects=False)
check("second artist signed",
      dbq("SELECT artist_contract FROM users WHERE id=?",
          (UNV,))[0]["artist_contract"] == "signed")
OWN.post(f"/owner/artists/{UNV}/contract-end",
         follow_redirects=False)
check("owner can terminate the contract",
      dbq("SELECT artist_contract FROM users WHERE id=?",
          (UNV,))[0]["artist_contract"] == "ended"
      and dbq("SELECT ended_by FROM artist_contracts WHERE "
              "user_id=?", (UNV,))[0]["ended_by"] == "owner")
OWN.post(f"/owner/artists/{UNV}/revoke", follow_redirects=False)
check("owner can revoke the badge",
      dbq("SELECT artist_verified FROM users WHERE id=?",
          (UNV,))[0]["artist_verified"] == 0)

# ---------- the official door: video -> playbook -> sign ----------
Dc, DEV = signup("Dev Door", "dev@label.test")
make_artist(Dc, DEV)
r = Dc.get("/artist/start")
check("start page shows CEO video one",
      r.status_code == 200 and "ceo-welcome" in r.text
      and "here-playbook.pdf" in r.text)
r = Dc.post("/artist/start",
            data={"signed_name": "Dev Door"},
            follow_redirects=False)
check("sign-off without both boxes refused",
      "err=form" in r.headers.get("location", ""))
r = Dc.post("/artist/start",
            data={"signed_name": "Dev Door",
                  "note": "door pitch",
                  "agree_read": "1", "agree_money": "1"},
            follow_redirects=False)
check("sign-off lands the artist on the closer",
      "done=1" in r.headers.get("location", ""))
app_row = dbq("SELECT * FROM artist_applications WHERE "
              "user_id=?", (DEV,))[0]
check("application carries the Playbook sign-off",
      app_row["signed_name"] == "Dev Door"
      and app_row["read_playbook"] == 1
      and app_row["note"] == "door pitch")
r = Dc.get("/artist/start?done=1")
check("done page plays the sign-yourself closer",
      "ceo-sign" in r.text)
Ec, EARLY = signup("Eve Early", "eve@label.test")
make_artist(Ec, EARLY)
r = Ec.get("/settings/profile")
check("settings card routes through the official door",
      "/artist/start" in r.text)
r = OWN.get("/owner/artists")
check("owner desk shows the Playbook sign-off",
      "Playbook read" in r.text and "Dev Door" in r.text)

failed = [n for n, ok in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
