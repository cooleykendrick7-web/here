"""HERE Ads (Build ADS) — the platform's smart ad system.

One engine for two kinds of ads:

- **house** ads, created by the platform owner from the owner panel
  ("plug in your words, a picture or a video, and a link — the system
  puts it together and runs it"), and
- **page** ads, created by a Page owner/admin through the page's
  "📣 Run as ad" flow to promote their business for sales and leads.

Phase 1: page ads are FREE to run (no charging yet — see
ROUTE_CONTRACT.md, Build ADS).

"Smart" here is honest platform-side composition + scheduling, no
external APIs (stdlib only):

- **Composition** — :func:`compose_card` turns an ad row into the
  finished Sponsored card: layout picked from the media kind
  (media-top image card / native video card / text-only card),
  headline and body trimmed to feed-friendly lengths, the CTA button,
  and the "Sponsored" label ALWAYS on.
- **Scheduling** — an ad runs always, on picked weekdays (weekly), or
  on picked days of the month (monthly), inside an optional hour
  window and an optional start/end date window.
- **Targeting** — ad interest tags are matched against the viewer's
  own signals (welcome topics, page types/names of pages joined or
  owned, categories of Market listings saved). A match triples the
  ad's selection weight; unmatched ads stay eligible for everyone, so
  there is never dead air.
- **Rotation** — weighted random without replacement, per-user daily
  frequency cap (same ad at most 3 impressions/user/day), ads a user
  hid never come back for that user, and a soft no-repeat rule (an ad
  is not picked again while alternatives exist until it falls out of
  the user's last 5 ad impressions). ``smart_hours`` ads get double
  weight during peak windows (11:00-14:00, 19:00-23:00).

Times: stored timestamps are UTC ISO (the app's ``now_iso``); the
schedule math runs on naive America/Chicago local time, matching the
app's existing Chicago-day conventions. Functions take an injectable
``now`` (naive local datetime) so tests can pin the clock.
"""
import random
import re
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo as _ZoneInfo
    _TZ = _ZoneInfo("America/Chicago")
except Exception:  # pragma: no cover - tzdata present in practice
    _TZ = timezone.utc

PEAK_WINDOWS = ((11, 14), (19, 23))     # local hours [start, end)
DAILY_CAP = 3                           # impressions per ad per user per day
RECENT_WINDOW = 5                       # "last N ad impressions" no-repeat
MAX_ELIGIBLE = 200                      # bounded fetch, small-platform scale

HEADLINE_MAX = 70
BODY_MAX = 160


# ---------------- schema ----------------

def ensure_schema(conn):
    """Create the ads tables if missing. Safe on existing DBs."""
    conn.execute("""CREATE TABLE IF NOT EXISTS ads (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        creator_id INTEGER NOT NULL,
        page_id INTEGER,
        kind TEXT NOT NULL DEFAULT 'house',
        title TEXT NOT NULL DEFAULT '',
        body TEXT NOT NULL DEFAULT '',
        media_kind TEXT NOT NULL DEFAULT 'none',
        media_path TEXT NOT NULL DEFAULT '',
        link_url TEXT NOT NULL DEFAULT '',
        cta_label TEXT NOT NULL DEFAULT 'Learn more',
        tags TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active',
        schedule TEXT NOT NULL DEFAULT 'always',
        weekly_days TEXT NOT NULL DEFAULT '',
        monthly_days TEXT NOT NULL DEFAULT '',
        hour_start INTEGER NOT NULL DEFAULT 0,
        hour_end INTEGER NOT NULL DEFAULT 24,
        smart_hours INTEGER NOT NULL DEFAULT 0,
        weight INTEGER NOT NULL DEFAULT 1,
        starts_on TEXT,
        ends_on TEXT,
        impressions INTEGER NOT NULL DEFAULT 0,
        clicks INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS ad_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ad_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        kind TEXT NOT NULL,
        ts TEXT NOT NULL)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ads_status "
                 "ON ads(status, schedule)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_adev_user "
                 "ON ad_events(user_id, kind, id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_adev_ad "
                 "ON ad_events(ad_id, kind, ts)")


# ---------------- time ----------------

def local_now():
    """Naive America/Chicago local datetime (schedule math runs on it)."""
    return datetime.now(_TZ).replace(tzinfo=None)


def _day_start_utc_iso(local_date):
    """UTC ISO string for Chicago midnight starting ``local_date`` —
    ad_events.ts values are UTC ISO, so day windows compare as strings."""
    start = datetime(local_date.year, local_date.month, local_date.day,
                     tzinfo=_TZ)
    return start.astimezone(timezone.utc).isoformat()


def _in_peak(hour):
    return any(s <= hour < e for s, e in PEAK_WINDOWS)


# ---------------- tags / interests ----------------

def parse_tags(raw):
    """'Cars, live music ,CARS' -> ['cars', 'live music'] (deduped)."""
    out = []
    for t in re.split(r"[,;]", raw or ""):
        t = " ".join(t.strip().lower().split())
        if t and t not in out:
            out.append(t)
    return out


def _signal_tokens(text):
    """A targeting signal contributes itself + its word tokens, so the
    category 'Home & Garden' matches ad tags 'home', 'garden' and
    'home & garden' alike."""
    t = " ".join(str(text or "").strip().lower().split())
    if not t:
        return set()
    out = {t}
    out.update(w for w in re.split(r"[^a-z0-9]+", t) if len(w) > 1)
    return out


def user_interest_tags(conn, user_id):
    """The viewer's interest signals: welcome topics they picked, the
    types + names of pages they joined or own, and the categories of
    Market listings they saved."""
    signals = set()
    for r in conn.execute("SELECT topic FROM user_topics WHERE user_id=?",
                          (user_id,)).fetchall():
        signals |= _signal_tokens(r["topic"])
    for r in conn.execute(
            "SELECT p.ptype, p.name FROM pages p "
            "LEFT JOIN page_members pm ON pm.page_id=p.id AND pm.user_id=? "
            "WHERE p.owner_id=? OR pm.user_id IS NOT NULL",
            (user_id, user_id)).fetchall():
        signals |= _signal_tokens(r["ptype"])
        signals |= _signal_tokens(r["name"])
    for r in conn.execute(
            "SELECT l.category FROM market_saves s "
            "JOIN market_listings l ON l.id=s.listing_id WHERE s.user_id=?",
            (user_id,)).fetchall():
        signals |= _signal_tokens(r["category"])
    return signals


# ---------------- scheduling ----------------

def _csv_ints(raw):
    out = set()
    for part in re.split(r"[,;\s]+", raw or ""):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


def schedule_ok(ad, now):
    """Date window + weekly/monthly day match + hour window for ``now``
    (naive local datetime)."""
    today = now.date().isoformat()
    if ad.get("starts_on") and today < ad["starts_on"]:
        return False
    if ad.get("ends_on") and today > ad["ends_on"]:
        return False
    sched = ad.get("schedule") or "always"
    if sched == "weekly":
        days = _csv_ints(ad.get("weekly_days"))
        if days and now.weekday() not in days:
            return False
    elif sched == "monthly":
        days = _csv_ints(ad.get("monthly_days"))
        if days and now.day not in days:
            return False
    hs = int(ad.get("hour_start") or 0)
    he = int(ad.get("hour_end") if ad.get("hour_end") is not None else 24)
    if hs != he:
        if hs < he:
            if not (hs <= now.hour < he):
                return False
        else:  # window wraps midnight (e.g. 22 -> 2)
            if not (now.hour >= hs or now.hour < he):
                return False
    return True


# ---------------- eligibility + scoring ----------------

def eligible_ads(conn, user_id, now=None):
    """Active, in-schedule ads this user may still be shown, each row
    (dict) annotated with its selection ``score``:

        score = weight
              x 3  when the ad's tags match the user's interests
              x 2  when smart_hours is on and ``now`` is a peak hour

    Hard exclusions: paused/ended, out of schedule, ads the user hid,
    and ads already at the per-user daily impression cap.
    """
    now = now or local_now()
    rows = conn.execute(
        "SELECT * FROM ads WHERE status='active' ORDER BY id DESC "
        "LIMIT ?", (MAX_ELIGIBLE,)).fetchall()
    if not rows:
        return []
    hidden = {r["ad_id"] for r in conn.execute(
        "SELECT DISTINCT ad_id FROM ad_events WHERE user_id=? AND kind='hide'",
        (user_id,)).fetchall()}
    day_start = _day_start_utc_iso(now.date())
    capped = {r["ad_id"] for r in conn.execute(
        "SELECT ad_id FROM ad_events WHERE user_id=? AND kind='imp' "
        "AND ts>=? GROUP BY ad_id HAVING COUNT(*)>=?",
        (user_id, day_start, DAILY_CAP)).fetchall()}
    interests = user_interest_tags(conn, user_id)
    peak = _in_peak(now.hour)
    out = []
    for r in rows:
        ad = dict(r)
        if ad["id"] in hidden or ad["id"] in capped:
            continue
        if not schedule_ok(ad, now):
            continue
        score = max(1, int(ad.get("weight") or 1))
        tags = parse_tags(ad.get("tags"))
        if tags and interests and any(t in interests for t in tags):
            score *= 3
        if ad.get("smart_hours") and peak:
            score *= 2
        ad["score"] = score
        out.append(ad)
    return out


def _weighted_sample(pool, n):
    """Weighted random sample without replacement (Efraimidis-
    Spirakis keys): each ad competes by its score, none repeats."""
    if n <= 0 or not pool:
        return []
    keyed = sorted(
        pool,
        key=lambda a: random.random() ** (1.0 / max(1, a.get("score", 1))),
        reverse=True)
    return keyed[:n]


def pick_feed_ads(conn, user_id, slots, now=None):
    """Choose up to ``slots`` ads for one feed render. The no-repeat
    rule is soft: ads outside the user's last RECENT_WINDOW impressions
    are picked first; if that pool can't fill the slots, the rest of
    the eligible pool tops up — never dead air."""
    if slots <= 0:
        return []
    elig = eligible_ads(conn, user_id, now)
    if not elig:
        return []
    recent = {r["ad_id"] for r in conn.execute(
        "SELECT ad_id FROM ad_events WHERE user_id=? AND kind='imp' "
        "ORDER BY id DESC LIMIT ?", (user_id, RECENT_WINDOW)).fetchall()}
    fresh = [a for a in elig if a["id"] not in recent]
    chosen = _weighted_sample(fresh, min(slots, len(fresh)))
    if len(chosen) < slots:
        picked = {a["id"] for a in chosen}
        rest = [a for a in elig if a["id"] not in picked]
        chosen += _weighted_sample(rest, slots - len(chosen))
    return chosen


# ---------------- events / counters ----------------

def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def record_impression(conn, ad_id, user_id):
    conn.execute(
        "INSERT INTO ad_events(ad_id, user_id, kind, ts) VALUES(?,?,?,?)",
        (ad_id, user_id, "imp", _now_iso()))
    conn.execute("UPDATE ads SET impressions=impressions+1 WHERE id=?",
                 (ad_id,))


def record_click(conn, ad_id, user_id):
    conn.execute(
        "INSERT INTO ad_events(ad_id, user_id, kind, ts) VALUES(?,?,?,?)",
        (ad_id, user_id, "click", _now_iso()))
    conn.execute("UPDATE ads SET clicks=clicks+1 WHERE id=?", (ad_id,))


def record_hide(conn, ad_id, user_id):
    conn.execute(
        "INSERT INTO ad_events(ad_id, user_id, kind, ts) VALUES(?,?,?,?)",
        (ad_id, user_id, "hide", _now_iso()))


# ---------------- link safety + card composition ----------------

def safe_link(url):
    """Only http(s) URLs and internal absolute paths may be click
    targets; anything else (javascript:, data:, protocol-relative,
    bare text) becomes '#'."""
    u = (url or "").strip()
    if u.startswith("http://") or u.startswith("https://"):
        return u
    if u.startswith("/") and not u.startswith("//"):
        return u
    return "#"


def _trim(text, limit):
    t = " ".join(str(text or "").split())
    if len(t) <= limit:
        return t
    return t[:limit - 1].rstrip() + "…"


def compose_card(ad, sponsor_name="", sponsor_avatar=""):
    """The smart builder's output for one ad row: the finished
    Sponsored card's view model. Layout follows the media kind; the
    Sponsored label is structural (the template always renders it)."""
    mk = ad.get("media_kind") or "none"
    layout = {"image": "media", "video": "video"}.get(mk, "text")
    return {
        "id": ad["id"],
        "kind": ad.get("kind") or "house",
        "page_id": ad.get("page_id"),
        "title": _trim(ad.get("title"), HEADLINE_MAX) or "HERE",
        "body": _trim(ad.get("body"), BODY_MAX),
        "layout": layout,
        "media_kind": mk,
        "media_path": ad.get("media_path") or "",
        "cta_label": (ad.get("cta_label") or "Learn more")[:30],
        "click_url": f"/ads/{ad['id']}/click",
        "hide_url": f"/ads/{ad['id']}/hide",
        "sponsor_name": sponsor_name,
        "sponsor_avatar": sponsor_avatar,
    }


def ctr(ad):
    imps = int(ad.get("impressions") or 0)
    if not imps:
        return "—"
    return f"{100.0 * int(ad.get('clicks') or 0) / imps:.1f}%"
