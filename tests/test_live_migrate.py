"""Upgrade-path check: an OLD-shape DB (no panel tables/columns, one
approved guest mid-live) must migrate + backfill on import.

Run:  ./venv/bin/python tests/test_live_migrate.py
Also boots a COPY of the dev DB (here.db) when one is present — the
original is never touched.
"""
import os, shutil, sqlite3, subprocess, sys, tempfile, json

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_tmp = tempfile.mkdtemp(prefix="here-test-mig-")
dbp = os.path.join(_tmp, "old.db")
conn = sqlite3.connect(dbp)
conn.executescript("""
CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE, pw_hash TEXT NOT NULL, bio TEXT DEFAULT '',
  avatar TEXT DEFAULT '', last_seen TEXT, last_seen_at TEXT,
  status_mode TEXT DEFAULT 'auto', inbox_status INTEGER DEFAULT 1,
  created_at TEXT NOT NULL);
INSERT INTO users(id,name,email,pw_hash,created_at) VALUES
  (1,'Host','h@x.io','x','2026-01-01T00:00:00+00:00'),
  (2,'Guest','g@x.io','x','2026-01-01T00:00:00+00:00');
CREATE TABLE live_sessions (id INTEGER PRIMARY KEY AUTOINCREMENT,
  host_id INTEGER NOT NULL, title TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'live', created_at TEXT NOT NULL,
  ended_at TEXT, host_seen_at TEXT NOT NULL);
INSERT INTO live_sessions(id,host_id,title,status,created_at,host_seen_at)
  VALUES (1,1,'old room','live','2026-01-01T00:00:00+00:00','2999-01-01T00:00:00+00:00');
CREATE TABLE live_join_requests (id INTEGER PRIMARY KEY AUTOINCREMENT,
  live_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL,
  decided_at TEXT);
INSERT INTO live_join_requests(live_id,user_id,status,created_at)
  VALUES (1,2,'approved','2026-01-01T00:00:00+00:00');
""")
conn.commit(); conn.close()
env = dict(os.environ, HERE_DB=dbp, HERE_UPLOADS=os.path.join(_tmp, "up"))
code = (
    f"import sys; sys.path.insert(0, {BASE!r});"
    "import app as here;"
    "c = here.db();"
    "live = here._live_get(c, 1);"
    "print('panel', here._live_panel_ids(c, live));"
    "print('role2', here._live_role(c, live, 2));"
    "print('cols', [r['name'] for r in c.execute('PRAGMA table_info(live_sessions)')]);"
    "print('jrkind', [dict(r) for r in c.execute('SELECT kind, inviter_id FROM live_join_requests')]);"
)
r = subprocess.run([sys.executable, "-c", code],
                   cwd=BASE, capture_output=True, text=True, env=env)
print(r.stdout); print(r.stderr[-2000:] if r.returncode else "")
assert "panel [1, 2]" in r.stdout, "backfill panel failed"
assert "role2 member" in r.stdout
assert "like_count" in r.stdout and "panel_order" in r.stdout
assert "'kind': 'request'" in r.stdout
print("MIGRATION OK")

# --- the real dev DB (a copy — the original is never touched) ---
dev_db = os.path.join(BASE, "here.db")
if os.path.exists(dev_db):
    copy_db = os.path.join(_tmp, "devcopy.db")
    shutil.copyfile(dev_db, copy_db)
    env2 = dict(os.environ, HERE_DB=copy_db,
                HERE_UPLOADS=os.path.join(_tmp, "uploads2"))
    boot = subprocess.run(
        [sys.executable, "-c",
         "import app; from fastapi.testclient import TestClient;"
         "print(TestClient(app.app).get('/login').status_code)"],
        cwd=BASE, env=env2, capture_output=True, text=True)
    cols = [r[1] for r in sqlite3.connect(copy_db).execute(
        "PRAGMA table_info(live_sessions)")]
    tabs = [r[0] for r in sqlite3.connect(copy_db).execute(
        "SELECT name FROM sqlite_master WHERE type='table'")]
    assert boot.stdout.strip() == "200", boot.stderr[-2000:]
    assert "like_count" in cols and "panel_order" in cols, cols
    assert "live_members" in tabs and "live_admins" in tabs \
        and "live_bans" in tabs and "live_like_events" in tabs, tabs
    print("DEV-DB COPY BOOT + MIGRATION OK")
else:
    print("no dev DB present — dev-copy check skipped")
