"""Optional private OSS archive. Existing local photos remain readable during setup."""
import os
import hashlib
import time
from pathlib import Path

_retry_after = 0.0


def configured():
    return all(os.environ.get(key) for key in ("CAPTURE_OSS_BUCKET", "OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET"))


def bucket(public=False):
    import oss2
    from oss2.credentials import EnvironmentVariableCredentialsProvider
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    region = os.environ.get("CAPTURE_OSS_REGION", "us-east-1")
    endpoint = f"https://oss-{region}{'' if public else '-internal'}.aliyuncs.com"
    if not public:
        endpoint = os.environ.get("CAPTURE_OSS_ENDPOINT", endpoint)
    # Cross-region transfers can hit transient TLS/read failures. Retry only idempotent operations.
    retry = Retry(total=1, connect=1, read=1, status=1, backoff_factor=1,
                  allowed_methods=frozenset({"GET", "HEAD", "PUT"}),
                  status_forcelist=(429, 500, 502, 503, 504))
    session = oss2.Session(adapter=HTTPAdapter(max_retries=retry))
    return oss2.Bucket(oss2.ProviderAuthV4(EnvironmentVariableCredentialsProvider()), endpoint,
                       os.environ["CAPTURE_OSS_BUCKET"], region=region, connect_timeout=(5, 30), session=session)


def signed_photo_url(key):
    return bucket(public=True).sign_url("GET", key, 300)


def thumbnail_key(key):
    return key.rsplit(".", 1)[0] + ".thumb.jpg"


def photo_bytes(root: Path, row):
    local = root / "photos" / row["filename"]
    if local.exists():
        return local.read_bytes()
    if row["oss_key"] and configured():
        with bucket().get_object(row["oss_key"]) as response:
            return response.read()
    raise FileNotFoundError("Photo storage unavailable")


def archive_one():
    global _retry_after
    if not configured() or time.time() < _retry_after:
        return False
    from .capture import ROOT, connection, ensure_thumbnail
    with connection() as db:
        row = db.execute("SELECT * FROM photos WHERE oss_key IS NULL ORDER BY created_at,id LIMIT 1").fetchone()
    if not row:
        return False
    path = ROOT / "photos" / row["filename"]
    key = "cold-rolling/photos/" + row["filename"]
    try:
        client = bucket()
        client.put_object_from_file(key, str(path), headers={"Content-Type": row["media_type"],
            "x-oss-object-acl": "private", "x-oss-meta-sha256": row["sha256"]})
        head = client.head_object(key)
        if head.content_length != path.stat().st_size or head.headers.get("x-oss-meta-sha256") != row["sha256"]:
            raise ValueError("Archive verification failed")
        thumbnail = ensure_thumbnail(row["id"], path)
        thumb_sha = hashlib.sha256(thumbnail.read_bytes()).hexdigest()
        client.put_object_from_file(thumbnail_key(key), str(thumbnail), headers={"Content-Type": "image/jpeg",
            "x-oss-object-acl": "private", "x-oss-meta-sha256": thumb_sha})
        thumb_head = client.head_object(thumbnail_key(key))
        if thumb_head.content_length != thumbnail.stat().st_size or thumb_head.headers.get("x-oss-meta-sha256") != thumb_sha:
            raise ValueError("Thumbnail archive verification failed")
        with connection() as db:
            db.execute("UPDATE photos SET oss_key=? WHERE id=?", (key, row["id"]))
        # Keep originals until the verified archive is both present and recorded in SQLite.
        path.unlink()
        thumbnail.unlink(missing_ok=True)
    except Exception:
        _retry_after = time.time() + 60
        return False
    return True
