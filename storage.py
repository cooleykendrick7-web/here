"""HERE media storage backends (Build U — Cloudflare R2).

Two backends, chosen from the environment at CALL time (so tests and
ops can flip modes without code changes):

- **R2 mode** — Cloudflare R2 (S3-compatible) when ALL of these are set:
  ``R2_ACCOUNT_ID``, ``R2_ACCESS_KEY_ID``, ``R2_SECRET_ACCESS_KEY``,
  ``R2_BUCKET``. Optional ``R2_PUBLIC_BASE`` is the public read base
  (an r2.dev URL or a custom domain bound to the bucket); when unset,
  reads resolve through short-lived presigned GET URLs.
- **Disk mode** — anything unset: files live under ``HERE_UPLOADS``
  exactly as they always have. This module then does nothing and the
  app uses its historical disk code paths untouched.

Stdlib only — SigV4 signing with hmac/hashlib, HTTP with urllib — the
same no-new-deps pattern as the Stripe integration. ALL network I/O
goes through :func:`_http_request` so tests can fake the wire.

Object keys are the same relative paths the DB has always stored
(``posts/<uuid>.mp4``, ``avatars/<uuid>.png``, ...); the DB value never
changes shape between backends.
"""
import hashlib
import hmac
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

_REGION = "auto"          # R2's SigV4 region is literally "auto".
_SERVICE = "s3"
_ALGORITHM = "AWS4-HMAC-SHA256"


class StorageError(Exception):
    """Any R2 transport/API failure. Callers turn this into the app's
    generic friendly upload error — never a 500, never a stack trace."""


# ---------------- config ----------------

def config():
    """The R2 config dict, or None when R2 mode is off."""
    acct = (os.environ.get("R2_ACCOUNT_ID") or "").strip()
    akid = (os.environ.get("R2_ACCESS_KEY_ID") or "").strip()
    secret = (os.environ.get("R2_SECRET_ACCESS_KEY") or "").strip()
    bucket = (os.environ.get("R2_BUCKET") or "").strip()
    if not (acct and akid and secret and bucket):
        return None
    return {
        "account": acct,
        "access_key": akid,
        "secret": secret,
        "bucket": bucket,
        "public_base": (os.environ.get("R2_PUBLIC_BASE") or "").strip(),
    }


def r2_enabled():
    return config() is not None


def _endpoint(cfg):
    return f"https://{cfg['account']}.r2.cloudflarestorage.com"


def _object_url(cfg, key):
    return f"{_endpoint(cfg)}/{cfg['bucket']}/{quote(key, safe='/')}"


def public_url(cfg, key):
    """The stable public read URL for key, or None when the bucket has
    no public base configured (reads then use presigned GETs)."""
    if cfg and cfg["public_base"]:
        return cfg["public_base"].rstrip("/") + "/" + quote(key, safe="/")
    return None


def public_ref(key):
    """The value stored in DB rows for an object: the key itself, in
    both backends — identical to the historical uploads-relative path."""
    return key


# ---------------- SigV4 ----------------

def _hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(cfg, date_stamp):
    k = _hmac(("AWS4" + cfg["secret"]).encode("utf-8"), date_stamp)
    k = _hmac(k, _REGION)
    k = _hmac(k, _SERVICE)
    return _hmac(k, "aws4_request")


def _scope(date_stamp):
    return f"{date_stamp}/{_REGION}/{_SERVICE}/aws4_request"


def _now():
    return datetime.now(timezone.utc)


def _signed_headers(cfg, method, key, extra=None, payload_hash="UNSIGNED-PAYLOAD",
                    content_type=None):
    """Header-signed (Authorization header) request parts for a live
    server-side S3 call. Returns (url, headers dict)."""
    url = _object_url(cfg, key)
    host = urlparse(url).netloc
    now = _now()
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    headers = {
        "Host": host,
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_hash,
    }
    if content_type:
        headers["Content-Type"] = content_type
    if extra:
        headers.update(extra)
    signed_names = sorted(h.lower() for h in headers)
    canonical = "".join(
        f"{name}:{_header_value(headers, name)}\n" for name in signed_names)
    canonical_req = "\n".join([
        method, urlparse(url).path, "", canonical,
        ";".join(signed_names), payload_hash])
    sts = "\n".join([_ALGORITHM, amz_date, _scope(date_stamp),
                     hashlib.sha256(canonical_req.encode()).hexdigest()])
    sig = hmac.new(_signing_key(cfg, date_stamp), sts.encode(),
                   hashlib.sha256).hexdigest()
    headers["Authorization"] = (
        f"{_ALGORITHM} Credential={cfg['access_key']}/{_scope(date_stamp)}, "
        f"SignedHeaders={';'.join(signed_names)}, Signature={sig}")
    return url, headers


def _header_value(headers, lower_name):
    for k, v in headers.items():
        if k.lower() == lower_name:
            return str(v).strip()
    return ""


def presign_url(cfg, method, key, expires=1800, content_type=None):
    """A query-string-signed URL (browser-direct uploads / private
    reads). Signs only the host header — the browser sets Content-Type
    from the File itself, so it is deliberately NOT signed."""
    url = _object_url(cfg, key)
    parsed = urlparse(url)
    now = _now()
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    date_stamp = now.strftime("%Y%m%d")
    params = {
        "X-Amz-Algorithm": _ALGORITHM,
        "X-Amz-Credential": f"{cfg['access_key']}/{_scope(date_stamp)}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(int(expires)),
        "X-Amz-SignedHeaders": "host",
    }
    canonical_qs = "&".join(
        f"{quote(k, safe='')}={quote(v, safe='')}"
        for k, v in sorted(params.items()))
    canonical_req = "\n".join([
        method, parsed.path, canonical_qs,
        f"host:{parsed.netloc}\n", "host", "UNSIGNED-PAYLOAD"])
    sts = "\n".join([_ALGORITHM, amz_date, _scope(date_stamp),
                     hashlib.sha256(canonical_req.encode()).hexdigest()])
    sig = hmac.new(_signing_key(cfg, date_stamp), sts.encode(),
                   hashlib.sha256).hexdigest()
    return f"{url}?{canonical_qs}&X-Amz-Signature={sig}"


def presign_put(key, content_type=None, expires=1800):
    cfg = config()
    if not cfg:
        raise StorageError("R2 is not configured.")
    return presign_url(cfg, "PUT", key, expires=expires)


def presign_get(key, expires=3600):
    cfg = config()
    if not cfg:
        raise StorageError("R2 is not configured.")
    return presign_url(cfg, "GET", key, expires=expires)


def read_url(key, expires=3600):
    """Where a browser should GET this object: the public base when
    configured, else a presigned GET."""
    cfg = config()
    if not cfg:
        raise StorageError("R2 is not configured.")
    return public_url(cfg, key) or presign_url(cfg, "GET", key, expires=expires)


# ---------------- transport ----------------

def _http_request(method, url, headers=None, body=None, timeout=120):
    """The single wire. Returns (status, headers dict, body bytes).
    HTTP error statuses are RETURNED (callers decide); transport
    failures raise StorageError. ``body`` may be bytes or a file-like
    object (urllib streams it when Content-Length is set)."""
    req = urllib.request.Request(url, data=body, method=method,
                                 headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers.items()), resp.read()
    except urllib.error.HTTPError as e:
        try:
            payload = e.read()
        except Exception:
            payload = b""
        return e.code, dict(e.headers.items() if e.headers else []), payload
    except Exception as e:
        raise StorageError(f"R2 request failed: {e.__class__.__name__}") from e


# ---------------- object ops ----------------

def put_object(key, data, content_type=None, size=None):
    """Store an object. ``data`` is bytes or a file-like object; a
    file-like REQUIRES ``size`` (sent as Content-Length so urllib
    streams instead of buffering). Returns the stored key."""
    cfg = config()
    if not cfg:
        raise StorageError("R2 is not configured.")
    if isinstance(data, (bytes, bytearray)):
        body = bytes(data)
        payload_hash = hashlib.sha256(body).hexdigest()
        length = len(body)
    else:
        body = data
        payload_hash = "UNSIGNED-PAYLOAD"
        if size is None:
            raise StorageError("file-object PUT requires a size.")
        length = int(size)
    url, headers = _signed_headers(cfg, "PUT", key,
                                   payload_hash=payload_hash,
                                   content_type=content_type)
    headers["Content-Length"] = str(length)
    status, _h, payload = _http_request("PUT", url, headers, body,
                                        timeout=900)
    if status not in (200, 201, 204):
        raise StorageError(f"R2 PUT {key} -> HTTP {status}: "
                           f"{payload[:200]!r}")
    return public_ref(key)


def head_object(key):
    """{'size': int, 'content_type': str} or None when absent.
    Transport failures raise StorageError (absence is a 404, not an
    error)."""
    cfg = config()
    if not cfg:
        raise StorageError("R2 is not configured.")
    url, headers = _signed_headers(
        cfg, "HEAD", key,
        payload_hash=hashlib.sha256(b"").hexdigest())
    status, resp_headers, _p = _http_request("HEAD", url, headers,
                                             timeout=30)
    if status == 404:
        return None
    if status != 200:
        raise StorageError(f"R2 HEAD {key} -> HTTP {status}")
    lowered = {k.lower(): v for k, v in resp_headers.items()}
    try:
        size = int(lowered.get("content-length") or 0)
    except (TypeError, ValueError):
        size = 0
    return {"size": size,
            "content_type": lowered.get("content-type", "")}


def exists(key):
    return head_object(key) is not None


def delete_object(key):
    """Best-effort delete; True when R2 accepted (204/200/404 all mean
    'gone'). Raises StorageError on transport failure so callers can
    log — deletes in the app are fire-and-forget by design."""
    cfg = config()
    if not cfg:
        raise StorageError("R2 is not configured.")
    url, headers = _signed_headers(
        cfg, "DELETE", key,
        payload_hash=hashlib.sha256(b"").hexdigest())
    status, _h, payload = _http_request("DELETE", url, headers,
                                         timeout=30)
    if status not in (200, 204, 404):
        raise StorageError(f"R2 DELETE {key} -> HTTP {status}: "
                           f"{payload[:200]!r}")
    return True


def copy_object(src_key, dst_key):
    """Server-side copy within the bucket (story-clip snapshots)."""
    cfg = config()
    if not cfg:
        raise StorageError("R2 is not configured.")
    url, headers = _signed_headers(
        cfg, "PUT", dst_key,
        extra={"x-amz-copy-source":
               f"/{cfg['bucket']}/{quote(src_key, safe='/')}"},
        payload_hash=hashlib.sha256(b"").hexdigest())
    status, _h, payload = _http_request("PUT", url, headers, None,
                                        timeout=120)
    if status not in (200, 201):
        raise StorageError(f"R2 COPY {src_key} -> HTTP {status}: "
                           f"{payload[:200]!r}")
    return public_ref(dst_key)
