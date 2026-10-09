#!/usr/bin/env python3
"""Rebuild merged/report artifacts from the immutable window audit CSV."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from main import PACKAGE_DIR, WORKSPACE_ROOT, read_csv, write_csv
from src.golden_scorer import merge_adjacent_gold
from src.outlier_detector import percentile
from src.report_generator import build_all_channel_inventory, write_report


def coerce(row: dict[str, str]) -> dict[str, Any]:
    output: dict[str, Any] = {}
    for key, value in row.items():
        if key in {
            "dataset", "candidate_id", "coil_id", "source_files", "candidate_start",
            "candidate_end", "stable_segment_start", "stable_segment_end", "golden_level",
            "abnormal_reason", "hard_abnormal_reasons", "pass_confidence", "score_components",
        }:
            output[key] = value
            continue
        if value == "":
            output[key] = None
        elif value.lower() == "true":
            output[key] = True
        elif value.lower() == "false":
            output[key] = False
        else:
            try:
                output[key] = float(value)
            except (TypeError, ValueError):
                output[key] = value
    return output


def main() -> int:
    config = json.loads((PACKAGE_DIR / "config" / "config.yaml").read_text())
    output_dir = (PACKAGE_DIR / config["output_dir"]).resolve()
    audit = [coerce(row) for row in read_csv(output_dir / "all_high_speed_window_audit.csv")]
    low = [coerce(row) for row in read_csv(output_dir / "low_speed_window_audit.csv")]
    thresholds = json.loads((output_dir / "calibrated_thresholds.json").read_text())
    profile = read_csv(output_dir / "column_profile.csv")
    gold = merge_adjacent_gold(audit)
    preferred = list(read_csv(output_dir / "golden_recipe_dataset.csv")[0]) if (output_dir / "golden_recipe_dataset.csv").stat().st_size else []
    write_csv(output_dir / "golden_recipe_dataset.csv", gold, preferred)

    foundations = sorted(
        path for path in (WORKSPACE_ROOT / "outputs").glob("data_foundation*")
        if (path / "file_index.csv").exists()
    )
    file_rows = [row for path in foundations for row in read_csv(path / "file_index.csv")]
    valid = [row for row in file_rows if row.get("metadata_status") == "ok" and row.get("parse_status") == "ok"]
    starts = [row["start_time"] for row in valid if row.get("start_time")]
    ends = [row["end_time"] for row in valid if row.get("end_time")]
    candidate_rows = [row for path in foundations for row in read_csv(path / "production_segments.csv")]
    channel_dictionaries = [
        (path / "channel_dictionary.csv", read_csv(path / "channel_dictionary.csv"))
        for path in foundations
        if (path / "channel_dictionary.csv").exists()
    ]
    write_csv(
        output_dir / "all_channel_inventory.csv",
        build_all_channel_inventory(channel_dictionaries, len(file_rows)),
    )
    ratios: dict[tuple[str, str], float] = {}
    for row in audit:
        value = row.get("high_speed_ratio")
        if isinstance(value, (int, float)):
            ratios[(str(row.get("dataset")), str(row.get("candidate_id")))] = float(value)
    old_manifest = json.loads((output_dir / "run_manifest.json").read_text())
    raw_summary = {
        "version": config["version"],
        "file_count": len(file_rows),
        "valid_file_count": len(valid),
        "invalid_file_count": len(file_rows) - len(valid),
        "raw_frame_count": sum(int(float(row.get("frames") or 0)) for row in valid),
        "time_start": min(starts) if starts else "—",
        "time_end": max(ends) if ends else "—",
        "candidate_count": len(candidate_rows),
        "sampled_point_count": old_manifest.get("sampled_point_count", 0),
        "median_high_speed_ratio": percentile(ratios.values(), 0.50),
        "adaptive_high_speed_seconds": old_manifest.get("adaptive_high_speed_seconds", 0),
        "fixed_high_speed_seconds": old_manifest.get("fixed_high_speed_seconds", 0),
    }
    figure_names = sorted(path.name for path in (output_dir / "figures").glob("*.svg"))
    write_report(
        output_dir / "cleaning_report.md",
        raw_summary=raw_summary,
        gold_rows=gold,
        all_high_rows=audit,
        low_rows=low,
        thresholds=thresholds,
        profile_rows=profile,
        figure_names=figure_names,
    )
    manifest = {
        **raw_summary,
        "golden_segment_count": len(gold),
        "window_level_counts": Counter(str(row["golden_level"]) for row in audit),
        "output_files": sorted(str(path.relative_to(output_dir)) for path in output_dir.rglob("*") if path.is_file()),
    }
    (output_dir / "run_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Rebuilt {len(gold)} merged golden plateaus from {len(audit)} audited windows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
