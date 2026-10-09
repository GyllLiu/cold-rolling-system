"""Run daily: make a consistent SQLite snapshot, then store a verified OSS copy."""
from datetime import datetime, timezone
from contextlib import closing
import hashlib
import os
from pathlib import Path
import sqlite3

from . import capture_storage


def main():
    root = Path(os.environ.get("CAPTURE_DATA_DIR", "data/capture"))
    destination = root / "backups"
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = datetime.now(timezone.utc).strftime("records-%Y%m%d-%H%M%S.sqlite3")
    path = destination / name
    with closing(sqlite3.connect(f"file:{root / 'records.sqlite3'}?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(path)) as target:
            source.backup(target)
            assert target.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    path.chmod(0o600)
    if capture_storage.configured():
        client = capture_storage.bucket()
        key = "cold-rolling/backups/" + name
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        client.put_object_from_file(key, str(path), headers={"x-oss-object-acl": "private", "x-oss-meta-sha256": digest})
        result = client.head_object(key)
        assert result.content_length == path.stat().st_size and result.headers.get("x-oss-meta-sha256") == digest
        print("SQLITE_BACKUP_OSS_OK")
    else:
        print("SQLITE_BACKUP_LOCAL_OK; OSS not configured")
    # Retain the latest 14 successful local snapshots; OSS retention is controlled separately.
    for old in sorted(destination.glob("records-*.sqlite3"), reverse=True)[14:]:
        old.unlink()


if __name__ == "__main__":
    main()
