#!/usr/bin/env python3
"""Build the governed high-speed tandem-pass edge library for reverse routing."""

from __future__ import annotations

import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from tandem_data_filter import is_tandem_record


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
FILES = sorted(WORKSPACE_ROOT.glob("outputs/data_foundation*/production_segments.csv"))
OUTPUT = PROJECT_ROOT / "backend/app/model_artifacts/route_history_v064.json"


def number(row: dict[str, str], key: str) -> float:
    value = float(row[key])
    if not math.isfinite(value):
        raise ValueError(key)
    return value


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


def main() -> None:
    records: list[dict[str, object]] = []
    seen: set[tuple[object, ...]] = set()
    for path in FILES:
        dataset = path.parent.name.replace("data_foundation_", "")
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if not is_tandem_record(row):
                    continue
                try:
                    entry = number(row, "entry_thickness_set_mode")
                    middle = number(row, "intermediate_thickness_set_mode")
                    exit_value = number(row, "exit_thickness_set_mode")
                    width = number(row, "width_set_mode")
                    stable_seconds = number(row, "stable_high_speed_seconds_1hz")
                except (KeyError, TypeError, ValueError):
                    continue
                if not (
                    truthy(row.get("learning_eligible"))
                    and truthy(row.get("digital_state_usable"))
                    and stable_seconds >= 120
                    and 0 < exit_value < middle < entry <= 30
                    and 500 <= width <= 2000
                ):
                    continue
                start_time = (row.get("start_time") or "").strip()
                key = (
                    dataset,
                    start_time,
                    round(entry, 5),
                    round(middle, 5),
                    round(exit_value, 5),
                    round(width, 2),
                )
                if key in seen:
                    continue
                seen.add(key)
                records.append(
                    {
                        "entry": round(entry, 5),
                        "middle": round(middle, 5),
                        "exit": round(exit_value, 5),
                        "width": round(width, 2),
                        "stable_seconds": round(stable_seconds),
                        "date": start_time[:10] if start_time else dataset,
                        "dataset": dataset,
                    }
                )
    records.sort(key=lambda item: (item["exit"], item["entry"], item["date"]))
    artifact = {
        "artifact_version": "high-speed-reverse-route-v0.6.4",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "selection": {
            "learning_eligible": True,
            "digital_state_usable": True,
            "minimum_stable_high_speed_seconds": 120,
            "record_meaning": "one complete tandem pass: entry -> F1 middle -> F2 exit",
            "limitation": "records are local pass edges and are not linked by coil id",
        },
        "case_count": len(records),
        "source_dates": sorted({str(item["date"]) for item in records}),
        "records": records,
    }
    if len(records) < 500:
        raise RuntimeError(f"governed route history is incomplete: {len(records)}")
    OUTPUT.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote {len(records)} high-speed route edges to {OUTPUT}")


if __name__ == "__main__":
    main()
