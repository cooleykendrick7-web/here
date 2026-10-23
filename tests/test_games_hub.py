"""Games Pack 2 (Build H) tests for HERE: live room games (guess
the number + dice duel) and the Games Hub with friend challenges
(Tic-Tac-Toe + Rock-Paper-Scissors matches).

Run:  ./venv/bin/python tests/test_games_hub.py
Uses a throwaway DB/uploads dir; no network.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import date, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-games2-")
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


def befriend(c1, u1, c2, u2):
    r = c2.post(f"/friends/request/{u1}")
    assert r.status_code in (200, 303), r.status_code
    r = c1.post(f"/friends/accept/{u2}")
    assert r.status_code in (200, 303), r.status_code


def has_key(obj, key):
    """True when `key` appears as a dict key anywhere in obj."""
    if isinstance(obj, dict):
        return key in obj or any(has_key(v, key) for v in obj.values())
    if isinstance(obj, list):
        return any(has_key(v, key) for v in obj)
    return False


# ---------------- 0. migrations ----------------
_tabs = {r[0] for r in dbq(
    "SELECT name FROM sqlite_master WHERE type='table'")}
check("migration: games-2 tables exist",
      {"live_games", "live_game_plays", "game_matches"} <= _tabs,
      str(_tabs))

# ---------------- 1. live room games ----------------
LH, LHID = signup("Game Host", "gh@games2.io")
LW, LWID = signup("Game Watcher", "gw@games2.io")
LA, LAID = signup("Game Admin", "ga@games2.io")
r = LH.post("/live/start", json={"title": "Game night"})
assert r.status_code == 200 and r.json().get("ok"), r.text[:200]
LID = r.json()["id"]
for _c in (LW, LA):
    rr = _c.post(f"/live/{LID}/heartbeat")
    assert rr.status_code == 200, rr.text[:200]
r = LH.post(f"/live/{LID}/appoint-admin", json={"user_id": LAID})
assert r.json().get("ok") is True, r.text[:200]

r = LW.post(f"/live/{LID}/game/start", json={"kind": "guess"})
check("live game: a watcher CANNOT start (403)", r.status_code == 403)
r = LH.post(f"/live/{LID}/game/start", json={"kind": "poker"})
check("live game: an unknown kind is refused (400)",
      r.status_code == 400)
r = LH.post(f"/live/{LID}/game/start", json={"kind": "guess"})
_j = r.json()
check("live game: the host starts Guess the Number",
      r.status_code == 200 and _j.get("ok")
      and _j["game"]["kind"] == "guess", r.text[:200])
G1 = _j["game"]["id"]
_secret = dbq("SELECT secret FROM live_games WHERE id=?",
              (G1,))[0]["secret"]
check("live game: a secret 1-100 was dealt server-side",
      isinstance(_secret, int) and 1 <= _secret <= 100, str(_secret))
dbx("UPDATE live_games SET secret=50 WHERE id=?", (G1,))

# THE LEAK CHECK: while the game is active, the whole state payload
# (parsed, walked recursively, AND the raw JSON text) must contain
# no 'secret' key anywhere; nobody has played yet, so the secret
# value itself must not appear as the game's data either.
st_raw = LW.get(f"/live/{LID}/state")
st = st_raw.json()
check("live game: state active_game has NO 'secret' key (recursive)",
      st.get("active_game") is not None
      and not has_key(st, "secret"), str(st.get("active_game")))
check("live game: raw state JSON contains no \"secret\" field",
      '"secret"' not in st_raw.text)
check("live game: last_game is null while a game is active",
      st.get("last_game") is None)

r = LW.post(f"/live/{LID}/game/play", json={"guess": 30})
_j = r.json()
check("live game: watcher makes a guess",
      r.status_code == 200 and _j["game"]["my_play"] == 30,
      r.text[:200])
r = LW.post(f"/live/{LID}/game/play", json={"guess": 40})
_j = r.json()
check("live game: re-guessing REPLACES the guess (one row)",
      _j["game"]["my_play"] == 40
      and len(dbq("SELECT * FROM live_game_plays WHERE game_id=? "
                  "AND user_id=?", (G1, LWID))) == 1)
for _bad in (0, 101, "abc", None):
    r = LW.post(f"/live/{LID}/game/play", json={"guess": _bad})
    check(f"live game: invalid guess {_bad!r} -> 400",
          r.status_code == 400)
LA.post(f"/live/{LID}/game/play", json={"guess": 60})
LH.post(f"/live/{LID}/game/play", json={"guess": 90})
st = LW.get(f"/live/{LID}/state").json()
_players = {p["name"]: p["display"] for p in st["active_game"]["players"]}
check("live game: guesses are public in the player list once made",
      _players.get("Game Watcher") == 40
      and _players.get("Game Admin") == 60, str(_players))
check("live game: STILL no secret anywhere after plays",
      not has_key(st, "secret"))

r = LW.post(f"/live/{LID}/game/end")
check("live game: a watcher cannot end the game (403)",
      r.status_code == 403)
r = LH.post(f"/live/{LID}/game/end")
_j = r.json()
check("live game: ending reveals the secret (50)",
      r.status_code == 200 and _j.get("secret") == 50, r.text[:300])
check("live game: closest guesses win — a 40/60 tie around 50",
      sorted(_j.get("winners") or []) == ["Game Admin", "Game Watcher"],
      str(_j.get("winners")))
st = LH.get(f"/live/{LID}/state").json()
_lg = st.get("last_game")
check("live game: state last_game reveals secret + winners after end",
      st.get("active_game") is None and _lg is not None
      and _lg["secret"] == 50
      and sorted(_lg["winners"]) == ["Game Admin", "Game Watcher"]
      and _lg["summary"] == "The number was 50.", str(_lg))
r = LH.post(f"/live/{LID}/game/play", json={"guess": 50})
check("live game: playing after the game ended -> 409",
      r.status_code == 409)

# starting game B ends game A
r = LH.post(f"/live/{LID}/game/start", json={"kind": "guess"})
G2 = r.json()["game"]["id"]
r = LA.post(f"/live/{LID}/game/start", json={"kind": "dice"})
_j = r.json()
check("live game: an ADMIN can start a game",
      r.status_code == 200 and _j["game"]["kind"] == "dice",
      r.text[:200])
G3 = _j["game"]["id"]
check("live game: starting game B ended game A",
      dbq("SELECT status FROM live_games WHERE id=?",
          (G2,))[0]["status"] == "ended"
      and dbq("SELECT status FROM live_games WHERE id=?",
              (G3,))[0]["status"] == "active")
st = LW.get(f"/live/{LID}/state").json()
check("live game: last_game hides again while a new game runs",
      st.get("active_game") is not None
      and st["active_game"]["kind"] == "dice"
      and st.get("last_game") is None, str(st.get("active_game")))

# dice: server rolls 1-6, best = max, plays increments
_rolls = []
_best = 0
for _i in range(5):
    r = LW.post(f"/live/{LID}/game/play", json={})
    _j = r.json()
    assert r.status_code == 200 and _j.get("ok"), r.text[:200]
    _rolls.append(_j["roll"])
    _best = max(_best, _j["roll"])
    assert _j["game"]["my_play"] == _best, (_j, _best)
check("live game: dice rolls are all 1-6",
      all(1 <= x <= 6 for x in _rolls), str(_rolls))
_row = dbq("SELECT * FROM live_game_plays WHERE game_id=? AND "
           "user_id=?", (G3, LWID))[0]
check("live game: dice row tracks value=last, best=max, plays=count",
      _row["plays"] == 5 and _row["best"] == _best
      and _row["value"] == _rolls[-1], str(dict(_row)))
LA.post(f"/live/{LID}/game/play", json={})
LA.post(f"/live/{LID}/game/play", json={})
st = LW.get(f"/live/{LID}/state").json()
_disp = {p["name"]: p["display"] for p in st["active_game"]["players"]}
check("live game: dice display reads '🎲 best {best} ({plays} rolls)'",
      _disp.get("Game Watcher") == f"🎲 best {_best} (5 rolls)"
      and str(_disp.get("Game Admin", "")).endswith("(2 rolls)"),
      str(_disp))
r = LH.post(f"/live/{LID}/game/end")
_j = r.json()
check("live game: the dice end names a highest-best winner",
      r.status_code == 200 and len(_j.get("winners") or []) >= 1
      and "secret" not in _j, r.text[:300])

# a non-room (blocked) user and an anonymous user can't play
LH2, LH2ID = signup("Game Host Two", "gh2@games2.io")
LB, LBID = signup("Game Blocked", "gb@games2.io")
r = LH2.post("/live/start", json={"title": "Closed room"})
LID2 = r.json()["id"]
LH2.post(f"/live/{LID2}/game/start", json={"kind": "dice"})
LH2.post(f"/block/{LBID}")
r = LB.post(f"/live/{LID2}/game/play", json={})
check("live game: a non-room (blocked) user's play is refused (403)",
      r.status_code == 403, str(r.status_code))
_anon = TestClient(here.app, follow_redirects=False)
r = _anon.post(f"/live/{LID2}/game/play", json={})
check("live game: anonymous play refused (401)", r.status_code == 401)
r = _anon.post(f"/live/{LID2}/game/start", json={"kind": "dice"})
check("live game: anonymous start refused (401)", r.status_code == 401)

# ending the live ends its active game
r = LH.post(f"/live/{LID}/game/start", json={"kind": "guess"})
G4 = r.json()["game"]["id"]
r = LH.post(f"/live/{LID}/end")
assert r.json().get("ok") is True, r.text[:200]
check("live game: ending the live ends the active game",
      dbq("SELECT status FROM live_games WHERE id=?",
          (G4,))[0]["status"] == "ended")

_lv = LH2.get(f"/live/{LID2}").text
check("live page: game card + result card + Games control render",
      'id="live-game-card"' in _lv and 'id="live-game-closed"' in _lv
      and 'id="ctl-games"' in _lv and "🎮 Games" in _lv)

# ---------------- 2. featured rotation ----------------
_mon, _sun = here.week_bounds(date(2026, 3, 4))
check("rotation: deterministic within a week (Mon == Sun)",
      here.featured_game(_mon) == here.featured_game(_sun)
      and here.featured_game(_mon) is here.featured_game(
          _mon + timedelta(days=3)))
check("rotation: next fabricated week features a different game",
      here.featured_game(_mon + timedelta(days=7))["key"]
      != here.featured_game(_mon)["key"])
check("rotation: the catalog covers all 8 games",
      [g["key"] for g in here.GAMES_CATALOG] == [
          "ttt", "rps", "guess", "dice", "trivia", "contest",
          "daily", "myviews"]
      and all(g["emoji"] and g["title"] and g["blurb"] and g["link"]
              for g in here.GAMES_CATALOG))

# ---------------- 3. friend challenges: tic-tac-toe ----------------
HA, HAID = signup("Hub Ann", "ha@games2.io")
HB, HBID = signup("Hub Ben", "hb@games2.io")
HC, HCID = signup("Hub Cam", "hc@games2.io")
HS, HSID = signup("Hub Stranger", "hs@games2.io")
befriend(HA, HAID, HB, HBID)
befriend(HA, HAID, HC, HCID)

r = HA.post("/games/challenge", json={"kind": "ttt", "user_id": HSID})
check("hub: challenging a non-friend -> 403", r.status_code == 403)
r = HA.post("/games/challenge", json={"kind": "ttt", "user_id": HAID})
check("hub: challenging yourself -> 400", r.status_code == 400)
r = HA.post("/games/challenge", json={"kind": "poker", "user_id": HBID})
check("hub: an unknown game -> 400", r.status_code == 400)
r = HA.post("/games/challenge", json={"kind": "ttt", "user_id": HBID})
_j = r.json()
check("hub: a friend challenge creates the match",
      r.status_code == 200 and _j.get("ok") and _j.get("match_id"),
      r.text[:200])
M1 = _j["match_id"]
_m = dbq("SELECT * FROM game_matches WHERE id=?", (M1,))[0]
_st = json.loads(_m["state"])
check("hub: ttt starts empty, challenger is X and moves first",
      _st["board"] == [None] * 9 and _m["a_id"] == HAID
      and _m["turn_id"] == HAID and _m["status"] == "active")
_notif = dbq("SELECT * FROM notifications WHERE user_id=? AND "
             "link=?", (HBID, f"/games/match/{M1}"))
check("hub: the challenged friend was notified with the match link",
      len(_notif) == 1
      and "challenged you to Tic-Tac-Toe!" in _notif[0]["text"],
      str([dict(n) for n in _notif]))

r = HB.post(f"/games/match/{M1}/move", json={"cell": 4})
check("hub ttt: moving out of turn -> 4xx", r.status_code == 403)
dbx("DELETE FROM user_activity_days WHERE user_id=?", (HAID,))
r = HA.post(f"/games/match/{M1}/move", json={"cell": 0})
check("hub ttt: X opens at cell 0",
      r.status_code == 200 and r.json()["match"]["board"][0] == "X",
      r.text[:200])
check("hub ttt: a move records streak activity",
      dbq("SELECT day FROM user_activity_days WHERE user_id=?",
          (HAID,)) != [])
r = HB.post(f"/games/match/{M1}/move", json={"cell": 0})
check("hub ttt: an occupied cell -> 4xx", r.status_code == 400)
r = HB.post(f"/games/match/{M1}/move", json={"cell": 9})
check("hub ttt: an off-board cell -> 4xx", r.status_code == 400)
HB.post(f"/games/match/{M1}/move", json={"cell": 3})
HA.post(f"/games/match/{M1}/move", json={"cell": 1})
HB.post(f"/games/match/{M1}/move", json={"cell": 4})
r = HA.post(f"/games/match/{M1}/move", json={"cell": 2})
_j = r.json()
_m = dbq("SELECT * FROM game_matches WHERE id=?", (M1,))[0]
check("hub ttt: completing the top row wins the match",
      _j["match"]["status"] == "done" and _m["winner_id"] == HAID
      and _m["is_draw"] == 0, str(dict(_m)))
r = HB.post(f"/games/match/{M1}/move", json={"cell": 5})
check("hub ttt: moving in a finished match -> 409",
      r.status_code == 409)
_page = HB.get(f"/games/match/{M1}").text
check("hub ttt: the match page shows the result banner",
      "🏆 Hub Ann won" in _page and 'data-cell="0"' in _page)
_notif = dbq("SELECT * FROM notifications WHERE user_id=? AND "
             "link=? AND text LIKE '🏆%'", (HBID, f"/games/match/{M1}"))
check("hub ttt: the loser was notified of the result",
      len(_notif) >= 1, str(len(_notif)))

# a scripted draw: X O X / X O O / O X X
r = HA.post("/games/challenge", json={"kind": "ttt", "user_id": HBID})
M2 = r.json()["match_id"]
for _c, _cell in ((HA, 0), (HB, 1), (HA, 2), (HB, 4), (HA, 3),
                  (HB, 5), (HA, 7), (HB, 6), (HA, 8)):
    rr = _c.post(f"/games/match/{M2}/move", json={"cell": _cell})
    assert rr.status_code == 200, (rr.status_code, rr.text[:200])
_m = dbq("SELECT * FROM game_matches WHERE id=?", (M2,))[0]
check("hub ttt: a full board with no line is a draw",
      _m["status"] == "done" and _m["is_draw"] == 1
      and _m["winner_id"] is None, str(dict(_m)))

# ---------------- 4. rock-paper-scissors ----------------
r = HA.post("/games/challenge", json={"kind": "rps", "user_id": HBID})
M3 = r.json()["match_id"]
r = HA.post(f"/games/match/{M3}/move", json={"choice": "rock"})
_j = r.json()
check("hub rps: my first pick lands, hidden from the opponent",
      r.status_code == 200 and _j["match"]["my_pick"] == "rock"
      and _j["match"]["opp_picked"] is False
      and "scissors" not in json.dumps(_j["match"]),
      r.text[:300])
r = HA.post(f"/games/match/{M3}/move", json={"choice": "paper"})
check("hub rps: picking twice in one round -> 400",
      r.status_code == 400)
_bpage = HB.get(f"/games/match/{M3}").text
check("hub rps: the opponent's page shows I picked — never WHAT",
      "has picked" in _bpage and "a_pick" not in _bpage
      and "You picked 🪨" not in _bpage, _bpage[:0])
_apage = HA.get(f"/games/match/{M3}").text
check("hub rps: my page confirms my own pick while I wait",
      "You picked 🪨" in _apage and "waiting on Hub Ben" in _apage)
r = HB.post(f"/games/match/{M3}/move", json={"choice": "scissors"})
_j = r.json()
check("hub rps: rock beats scissors — round 1 to A, picks now public",
      _j["match"]["round"] == 2 and _j["match"]["opp_wins"] == 1
      and _j["match"]["last"]["a_pick"] == "rock"
      and _j["match"]["last"]["b_pick"] == "scissors",
      r.text[:300])
_bpage = HB.get(f"/games/match/{M3}").text
check("hub rps: the resolved round shows both throws + the score",
      "you threw ✂️" in _bpage and "Hub Ann threw 🪨" in _bpage
      and "You 0 — 1 Hub Ann" in _bpage, _bpage[:0])
HB.post(f"/games/match/{M3}/move", json={"choice": "paper"})
r = HA.post(f"/games/match/{M3}/move", json={"choice": "rock"})
_j = r.json()
check("hub rps: paper beats rock — round 2 to B, tied 1-1",
      _j["match"]["my_wins"] == 1 and _j["match"]["opp_wins"] == 1
      and _j["match"]["status"] == "active", r.text[:300])
HA.post(f"/games/match/{M3}/move", json={"choice": "scissors"})
r = HB.post(f"/games/match/{M3}/move", json={"choice": "paper"})
_j = r.json()
_m = dbq("SELECT * FROM game_matches WHERE id=?", (M3,))[0]
check("hub rps: first to 2 round-wins finishes the match",
      _j["match"]["status"] == "done" and _m["winner_id"] == HAID,
      str(dict(_m)))

# a tie round scores nobody
r = HA.post("/games/challenge", json={"kind": "rps", "user_id": HCID})
M4 = r.json()["match_id"]
HA.post(f"/games/match/{M4}/move", json={"choice": "rock"})
r = HC.post(f"/games/match/{M4}/move", json={"choice": "rock"})
_j = r.json()
check("hub rps: a tied round scores nobody and rolls on",
      _j["match"]["round"] == 2 and _j["match"]["my_wins"] == 0
      and _j["match"]["opp_wins"] == 0
      and _j["match"]["last"]["winner_id"] is None
      and _j["match"]["status"] == "active", r.text[:300])
r = HA.post(f"/games/match/{M4}/move", json={"choice": "spock"})
check("hub rps: a bad choice -> 400", r.status_code == 400)

# ---------------- 5. forfeit, rematch, strangers ----------------
r = HA.post("/games/challenge", json={"kind": "ttt", "user_id": HCID})
M5 = r.json()["match_id"]
r = HS.get(f"/games/match/{M5}")
check("hub: a non-participant gets 404 on the match page",
      r.status_code == 404)
r = HS.post(f"/games/match/{M5}/move", json={"cell": 0})
check("hub: a non-participant cannot move (404)",
      r.status_code == 404)
r = HS.post(f"/games/match/{M5}/forfeit")
check("hub: a non-participant cannot forfeit (404)",
      r.status_code == 404)
r = HC.post(f"/games/match/{M5}/forfeit")
_m = dbq("SELECT * FROM game_matches WHERE id=?", (M5,))[0]
check("hub: forfeiting ends the match, the other player wins",
      r.status_code == 200 and _m["status"] == "done"
      and _m["winner_id"] == HAID, str(dict(_m)))
r = HC.post(f"/games/match/{M5}/forfeit")
check("hub: forfeiting a finished match -> 409",
      r.status_code == 409)

r = HB.post(f"/games/match/{M1}/rematch")
check("hub: rematch redirects to the new match",
      r.status_code == 303 and "/games/match/" in
      r.headers.get("location", ""), r.headers.get("location", ""))
M6 = int(r.headers["location"].rstrip("/").split("/")[-1])
_m = dbq("SELECT * FROM game_matches WHERE id=?", (M6,))[0]
_st = json.loads(_m["state"])
check("hub: the rematch is a fresh match with the starter swapped",
      M6 != M1 and _m["a_id"] == HBID and _m["b_id"] == HAID
      and _m["turn_id"] == HBID and _st["board"] == [None] * 9
      and _m["status"] == "active", str(dict(_m)))
r = HB.post(f"/games/match/{M6}/rematch")
check("hub: rematching an ACTIVE match -> 409",
      r.status_code == 409)

# ---------------- 6. the hub page ----------------
_gpage = HA.get("/games").text
_i1 = _gpage.index("⭐ Game of the Week")
_i2 = _gpage.index("🎮 Play with a friend")
_i3 = _gpage.index("🔴 Games inside a live")
_i4 = _gpage.index("More ways to play")
check("hub page: all 4 sections render in order",
      _i1 < _i2 < _i3 < _i4)
check("hub page: featured card + Monday note + challenge pickers",
      "New featured game every Monday 🔄" in _gpage
      and 'id="friend-ttt"' in _gpage and 'id="friend-rps"' in _gpage
      and "Hub Ben" in _gpage and 'href="/lives"' in _gpage
      and 'href="/contest"' in _gpage and 'href="/daily"' in _gpage
      and 'href="/views"' in _gpage)
check("hub page: My matches lists matches with turn labels",
      "My matches" in _gpage
      and f"Waiting on Hub Ben" in _gpage, _gpage[:0])
_gpage_b = HB.get("/games").text
check("hub page: the player to move sees 'Your move 🔴'",
      "Your move 🔴" in _gpage_b)
_home = HA.get("/home").text
check("hub page: nav links 🎮 Games in the chrome",
      'href="/games"' in _home)
_rpage = HB.get(f"/games/match/{M3}").text
check("hub rps page: the finished match shows the result banner",
      "🏆 Hub Ann won" in _rpage and "🔁 Rematch" in _rpage)

# ---------------- 7. inline JS syntax (node --check) ----------------
def js_ok(html_text, tag):
    blocks = re.findall(r"<script>(.*?)</script>", html_text, re.S)
    ok, bad = True, []
    for i, b in enumerate(blocks):
        if not b.strip():
            continue
        path = os.path.join(_tmp, f"{tag}_{i}.js")
        with open(path, "w") as fh:
            fh.write(b)
        p = subprocess.run(["node", "--check", path],
                           capture_output=True, text=True)
        if p.returncode != 0:
            ok = False
            bad.append(p.stderr.strip()[:160])
    return ok, "; ".join(bad)


for _label, _html in [
        ("games", HA.get("/games").text),
        ("game_match_ttt", HA.get(f"/games/match/{M6}").text),
        ("game_match_rps", HA.get(f"/games/match/{M4}").text),
        ("live_games", LH2.get(f"/live/{LID2}").text)]:
    _ok, _bad = js_ok(_html, _label)
    check(f"node --check: {_label} inline JS parses", _ok, _bad)

# ---------------- 8. dev-DB boot on a copy ----------------
dev_db = os.path.join(BASE, "here.db")
if os.path.exists(dev_db):
    before = os.path.getmtime(dev_db)
    copy_db = os.path.join(_tmp, "devcopy.db")
    shutil.copyfile(dev_db, copy_db)
    env2 = dict(os.environ, HERE_DB=copy_db,
                HERE_UPLOADS=os.path.join(_tmp, "devup"))
    boot = subprocess.run(
        [sys.executable, "-c",
         "import app; from fastapi.testclient import TestClient;"
         "print(TestClient(app.app).get('/login').status_code)"],
        cwd=BASE, env=env2, capture_output=True, text=True)
    check("dev DB copy: boots without exception (login 200)",
          boot.returncode == 0 and boot.stdout.strip() == "200",
          (boot.stdout + boot.stderr)[-300:])
    _cc = sqlite3.connect(copy_db)
    _tabs2 = {r[0] for r in _cc.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    _cc.close()
    check("dev DB copy: games-2 tables migrated in",
          {"live_games", "live_game_plays", "game_matches"} <= _tabs2)
    check("dev DB copy: the original dev DB file was not modified",
          os.path.getmtime(dev_db) == before)
else:
    check("dev DB copy: skipped (no here.db in repo)", True)

# ---------------- summary ----------------
fails = [n for n, okk in RESULTS if not okk]
print(f"\n{len(RESULTS) - len(fails)}/{len(RESULTS)} passed")
if fails:
    print("FAILURES:", fails)
    sys.exit(1)
