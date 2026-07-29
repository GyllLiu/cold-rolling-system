from __future__ import annotations

import hashlib
import csv
import os
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import DatFile


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DAT_ROOTS = [
    WORKSPACE_ROOT / name
    for name in (
        "260712",
        "260713",
        "260714",
        "260716",
        "260717",
        "260718",
        "260719",
    )
]


def _configured_roots() -> list[Path]:
    if configured := os.getenv("DAT_ROOTS"):
        return [Path(value).expanduser().resolve() for value in configured.split(os.pathsep) if value]
    if legacy := os.getenv("DAT_ROOT"):
        return [Path(legacy).expanduser().resolve()]
    return [path.resolve() for path in DEFAULT_DAT_ROOTS]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def scan_dat_directory(db: Session) -> dict:
    roots = _configured_roots()
    existing_roots = [root for root in roots if root.exists()]
    missing = [str(root) for root in roots if not root.exists()]
    if not existing_roots:
        return {"roots": [], "found": 0, "added": 0, "skipped": 0, "errors": ["DAT目录不存在"]}

    if str(WORKSPACE_ROOT) not in sys.path:
        sys.path.insert(0, str(WORKSPACE_ROOT))
    from build_data_foundation import read_file_metadata

    found = added = skipped = 0
    errors: list[str] = []
    paths = sorted(path for root in existing_roots for path in root.rglob("*.dat"))
    for path in paths:
        found += 1
        try:
            checksum = _sha256(path)
            if db.scalar(select(DatFile.id).where(DatFile.file_sha256 == checksum)):
                skipped += 1
                continue
            meta = read_file_metadata(path)
            db.add(
                DatFile(
                    filename=path.name,
                    file_path=str(path),
                    file_sha256=checksum,
                    start_time=meta.start_dt.isoformat(timespec="milliseconds") if meta.start_dt else "",
                    end_time=meta.end_dt.isoformat(timespec="milliseconds") if meta.end_dt else "",
                    sample_interval=meta.clk,
                    frames=meta.frames,
                    channel_count=len(meta.channels),
                    schema_hash=meta.schema_hash,
                    quality_status=meta.metadata_status,
                    error_message=meta.metadata_error,
                )
            )
            added += 1
        except Exception as exc:
            errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
    db.commit()
    errors.extend(f"目录不存在: {path}" for path in missing)
    return {"roots": [str(root) for root in existing_roots], "found": found, "added": added, "skipped": skipped, "errors": errors}


def dataset_foundation_status() -> list[dict]:
    output = []
    datasets = [
        *((dataset, "training") for dataset in ("260712", "260713", "260714", "260716", "260717", "260718")),
        ("260719", "validation"),
    ]
    for dataset, role in datasets:
        folder = WORKSPACE_ROOT / "outputs" / f"data_foundation_{dataset}"
        file_index = folder / "file_index.csv"
        segments = folder / "production_segments.csv"
        file_rows: list[dict[str, str]] = []
        segment_rows: list[dict[str, str]] = []
        if file_index.exists():
            with file_index.open(encoding="utf-8-sig", newline="") as handle:
                file_rows = list(csv.DictReader(handle))
        if segments.exists():
            with segments.open(encoding="utf-8-sig", newline="") as handle:
                segment_rows = list(csv.DictReader(handle))
        eligible = [row for row in segment_rows if row.get("learning_eligible") == "true"]
        output.append(
            {
                "dataset": dataset,
                "role": role,
                "status": "ready" if file_rows and segment_rows else "missing",
                "file_count": len(file_rows),
                "parse_success_count": sum(row.get("parse_status") == "ok" for row in file_rows),
                "segment_count": len(segment_rows),
                "learning_eligible_count": len(eligible),
                "stable_seconds": sum(int(float(row.get("stable_high_speed_seconds_1hz", "0") or 0)) for row in eligible),
                "tuning_seconds": sum(int(float(row.get("low_speed_tuning_seconds_1hz", "0") or 0)) for row in segment_rows),
            }
        )
    return output
