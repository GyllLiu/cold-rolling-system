"""Read-only DAT preparation for the offline v073 parameter experiment.

Reuses the existing 1 Hz decoder and governed-segment builder. Outputs live
outside the application; neither source DAT nor the production DB is modified.
These are heuristic segments, not certified individual coils or quality labels.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO), str(REPO.parent)]
from build_data_foundation import (  # noqa: E402
    CORE_CHANNELS, SamplePoint, build_candidate_segments, mark_suspected_freezes,
    read_file_metadata,
)
from golden_data.src.dat_sampler import sample_dat  # noqa: E402


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")


def write_csv(path, rows):
    if not rows:
        path.write_text("")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(dict.fromkeys(k for row in rows for k in row)))
        writer.writeheader()
        writer.writerows(rows)


def prepare(dat_dir: Path, output: Path):
    output.mkdir(parents=True, exist_ok=True)
    cache = output / "samples_1hz.jsonl.gz"
    files = sorted(dat_dir.glob("*.dat"))
    if not files:
        raise ValueError(f"No DAT files in {dat_dir}")
    sources = [{"path": str(p.resolve()), "bytes": p.stat().st_size,
                "mtime_ns": p.stat().st_mtime_ns} for p in files]
    signature = hashlib.sha256(json.dumps(sources, sort_keys=True).encode()).hexdigest()
    manifest_path = output / "source_manifest.json"
    if cache.exists():
        if not manifest_path.exists() or json.loads(manifest_path.read_text())["signature"] != signature:
            raise ValueError("Cache provenance changed; choose a new output directory")
    else:
        metadata, errors = [], []
        partial = cache.with_suffix(".partial")
        with gzip.open(partial, "wt") as handle:
            for index, path in enumerate(files):
                meta = read_file_metadata(path)
                if meta.metadata_status != "ok":
                    raise ValueError(f"{path.name}: {meta.metadata_error}")
                sample = sample_dat(path, CORE_CHANNELS.values())
                if sample["errors"]:
                    errors.append({"file": path.name, "errors": sample["errors"]})
                with path.open("rb") as source:
                    digest = hashlib.file_digest(source, "sha256").hexdigest()
                metadata.append({"file": path.name, "start": meta.start_dt, "end": meta.end_dt,
                                 "sha256": digest})
                for j, stamp in enumerate(sample["timestamps"]):
                    values = {alias: sample["data"][name][j] for alias, name in CORE_CHANNELS.items()
                              if name in sample["data"]}
                    handle.write(json.dumps({"timestamp": stamp.isoformat(), "file_index": index,
                                             "filename": path.name, "values": values}) + "\n")
                if (index + 1) % 12 == 0 or index + 1 == len(files):
                    print(f"{dat_dir.name}: decoded {index + 1}/{len(files)}", flush=True)
        dump(output / "decode_errors.json", errors)
        if errors:
            raise ValueError("DAT decoding errors; inspect decode_errors.json before proceeding")
        partial.rename(cache)
        dump(manifest_path, {"signature": signature, "sources": sources, "metadata": metadata,
                             "sampling_seconds": 1, "method": "existing direct RLE sampler"})
    points = []
    with gzip.open(cache, "rt") as handle:
        for line in handle:
            row = json.loads(line)
            row["timestamp"] = datetime.fromisoformat(row["timestamp"])
            points.append(SamplePoint(**row))
    points.sort(key=lambda point: point.timestamp)
    if len({point.timestamp for point in points}) != len(points):
        raise ValueError("Duplicate DAT timestamps; refusing to silently double count")
    freezes = mark_suspected_freezes(points, 300)
    rows = build_candidate_segments(points)
    write_csv(output / "production_segments.csv", rows)
    dump(output / "summary.json", {"source_day": dat_dir.name, "sample_count_1hz": len(points),
                                   "start": points[0].timestamp, "end": points[-1].timestamp + timedelta(seconds=1),
                                   "candidate_segments": len(rows), "suspected_freezes": freezes,
                                   "raw_data_changed": False, "certified_coil_ids": False})
    print(f"{dat_dir.name}: {len(rows)} candidate segments", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dat-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.dat_dir, args.output)


if __name__ == "__main__":
    main()
