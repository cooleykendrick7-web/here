#!/usr/bin/env python3
"""Migrate HERE's on-disk uploads into Cloudflare R2 (Build U).

Copies every file under HERE_UPLOADS (default: <repo>/uploads) to the
R2 bucket with the IDENTICAL key (the uploads-relative path), so DB
rows keep resolving: the app's /uploads route redirects to R2 when an
object lives there and keeps serving from disk for keys not yet
migrated — both backends work simultaneously during the move.

DRY-RUN by default: prints what it WOULD copy and changes nothing.
Pass --apply to actually copy. Idempotent: a key already in R2 with a
matching size is skipped. A size mismatch re-copies (and reports).

Requires the R2 env vars (R2_ACCOUNT_ID, R2_ACCESS_KEY_ID,
R2_SECRET_ACCESS_KEY, R2_BUCKET). Run from the repo root:

    R2_ACCOUNT_ID=... R2_ACCESS_KEY_ID=... R2_SECRET_ACCESS_KEY=... \\
    R2_BUCKET=... ./venv/bin/python scripts/migrate_uploads_to_r2.py
    ... same ... --apply
"""
import mimetypes
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import storage  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def upload_dir():
    return os.environ.get("HERE_UPLOADS", os.path.join(BASE, "uploads"))


def collect_files(root):
    """[(key, abspath, size)] for every file under root, key =
    uploads-relative path with forward slashes."""
    out = []
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            ap = os.path.join(dirpath, name)
            key = os.path.relpath(ap, root).replace(os.sep, "/")
            try:
                size = os.path.getsize(ap)
            except OSError:
                continue
            out.append((key, ap, size))
    out.sort()
    return out


def migrate(apply=False, root=None, log=print):
    """Copy disk files to R2. Returns a summary dict. With apply=False
    nothing is written — the plan is only reported."""
    if not storage.r2_enabled():
        raise SystemExit("R2 env vars are not all set — nothing to do.")
    root = root or upload_dir()
    files = collect_files(root)
    summary = {"seen": len(files), "copied": 0, "skipped": 0,
               "bytes": 0, "failed": 0}
    for key, ap, size in files:
        try:
            head = storage.head_object(key)
        except storage.StorageError as e:
            summary["failed"] += 1
            log(f"FAIL  {key}  (head: {e})")
            continue
        if head is not None and head["size"] == size:
            summary["skipped"] += 1
            log(f"skip  {key}  (already in R2, {size} bytes)")
            continue
        if not apply:
            summary["copied"] += 1  # would-copy count in dry-run
            summary["bytes"] += size
            log(f"would copy  {key}  ({size} bytes)")
            continue
        ctype = mimetypes.guess_type(ap)[0] or "application/octet-stream"
        try:
            with open(ap, "rb") as f:
                storage.put_object(key, f, ctype, size=size)
            head = storage.head_object(key)
            if head is None or head["size"] != size:
                summary["failed"] += 1
                log(f"FAIL  {key}  (verify: size mismatch after PUT)")
                continue
        except storage.StorageError as e:
            summary["failed"] += 1
            log(f"FAIL  {key}  ({e})")
            continue
        summary["copied"] += 1
        summary["bytes"] += size
        log(f"copied  {key}  ({size} bytes)")
    return summary


def main(argv):
    apply = "--apply" in argv
    mode = "APPLY" if apply else "DRY-RUN (nothing will be written)"
    print(f"migrate_uploads_to_r2: {mode}")
    print(f"source dir: {upload_dir()}")
    s = migrate(apply=apply)
    verb = "copied" if apply else "would copy"
    print(f"done: {s['seen']} files seen, {s['copied']} {verb} "
          f"({s['bytes']} bytes), {s['skipped']} already in R2, "
          f"{s['failed']} failed")
    return 1 if s["failed"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
