#!/usr/bin/env python3
"""Build an auditable Golden Recipe Dataset from all existing DAT foundations."""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

PACKAGE_DIR = Path(__file__).resolve().parent
WORKSPACE_ROOT = PACKAGE_DIR.parents[1]
sys.path.insert(0, str(PACKAGE_DIR))

from src.column_mapper import CHANNELS  # noqa: E402
from src.data_profile import DataProfiler  # noqa: E402
from src.dat_sampler import sample_dat  # noqa: E402
from src.golden_scorer import calibrate_thresholds, merge_adjacent_gold, score_window  # noqa: E402
from src.outlier_detector import finite, percentile  # noqa: E402
from src.report_generator import (  # noqa: E402
    build_all_channel_inventory,
    write_column_mapping,
    write_report,
    write_trace_svg,
)
from src.stable_segment import detect_windows  # noqa: E402


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], preferred: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys: set[str] = set()
    for row in rows:
        keys.update(key for key in row if not key.startswith("__"))
    preferred = preferred or []
    fieldnames = [name for name in preferred if name in keys]
    fieldnames.extend(sorted(keys - set(fieldnames)))
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        if not fieldnames:
            handle.write("")
            return
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def dataset_name(foundation_dir: Path, file_rows: list[dict[str, str]]) -> str:
    suffix = foundation_dir.name.removeprefix("data_foundation_")
    if suffix != foundation_dir.name:
        return suffix
    starts = [row.get("start_time", "") for row in file_rows if row.get("start_time")]
    if starts:
        return datetime.fromisoformat(min(starts)).strftime("%y%m%d")
    return "unknown"


def _converted(alias: str, value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if alias in {"f1_gap_set", "f2_gap_set"}:
        return number / 1000.0
    return number


def sampled_file_points(path: Path, config: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    result = sample_dat(
        path,
        CHANNELS.values(),
        sample_interval_seconds=config["sample_interval_seconds"],
    )
    reverse = {channel: alias for alias, channel in CHANNELS.items()}
    points: list[dict[str, Any]] = []
    for index, timestamp in enumerate(result["timestamps"]):
        point: dict[str, Any] = {"timestamp": timestamp, "source_file": path.name}
        for channel, alias in reverse.items():
            values = result["data"].get(channel)
            point[alias] = _converted(alias, values[index]) if values and index < len(values) else None
        points.append(point)
    return points, result["errors"]


def sample_worker(task: tuple[str, dict[str, Any]]) -> tuple[str, list[dict[str, Any]] | None, dict[str, str], str]:
    """Process-pool entry point; returns errors instead of crashing the run."""
    path_text, config = task
    path = Path(path_text)
    try:
        points, errors = sampled_file_points(path, config)
        return path.name, points, errors, ""
    except Exception as exc:
        return path.name, None, {}, f"{type(exc).__name__}: {exc}"


def candidate_points(
    candidate: dict[str, str], file_cache: dict[str, list[dict[str, Any]]]
) -> tuple[list[dict[str, Any]], int, int]:
    start = datetime.fromisoformat(candidate["start_time"])
    end = datetime.fromisoformat(candidate["end_time"])
    selected: list[dict[str, Any]] = []
    for name in candidate.get("source_files", "").split(";"):
        selected.extend(
            point
            for point in file_cache.get(name, [])
            if start <= point["timestamp"] < end
        )
    selected.sort(key=lambda point: point["timestamp"])
    deduplicated: list[dict[str, Any]] = []
    duplicates = 0
    conflicts = 0
    by_time: dict[datetime, dict[str, Any]] = {}
    for point in selected:
        timestamp = point["timestamp"]
        if timestamp not in by_time:
            by_time[timestamp] = point
            deduplicated.append(point)
            continue
        duplicates += 1
        previous = by_time[timestamp]
        if any(previous.get(alias) != point.get(alias) for alias in CHANNELS):
            conflicts += 1
    return deduplicated, duplicates, conflicts


def enrich_window(
    row: dict[str, Any],
    *,
    dataset: str,
    candidate: dict[str, str],
    detection: dict[str, Any],
    duplicates: int,
    conflicts: int,
    foundation_dir: Path,
) -> dict[str, Any]:
    row.update(
        {
            "dataset": dataset,
            "candidate_id": candidate["candidate_id"],
            "coil_id": f"INFERRED-{dataset}-{candidate['candidate_id']}",
            "coil_boundary_confidence": candidate.get("confidence", "low"),
            "is_true_coil_id": False,
            "pass_no": "",
            "pass_confidence": "unavailable",
            "original_entry_thickness": "",
            "cumulative_true_strain": "",
            "f1_force_set": "",
            "f2_force_set": "",
            "source_files": candidate.get("source_files", ""),
            "candidate_start": candidate.get("start_time", ""),
            "candidate_end": candidate.get("end_time", ""),
            "high_speed_ratio": detection.get("high_speed_ratio"),
            "candidate_active_seconds": detection.get("active_seconds"),
            "duplicate_timestamp_count": duplicates,
            "conflicting_timestamp_count": conflicts,
            "__foundation_dir": str(foundation_dir),
        }
    )
    row["entry_tension_set"] = row.get("entry_tension_set_median")
    row["entry_tension_actual_median"] = row.get("entry_tension_actual_median")
    row["interstand_tension_set"] = row.get("interstand_tension_set_median")
    row["interstand_tension_actual_median"] = row.get("interstand_tension_actual_median")
    row["exit_tension_set"] = row.get("exit_tension_set_median")
    row["exit_tension_actual_median"] = row.get("exit_tension_actual_median")
    return row


def load_all_channel_count(foundation_dirs: list[Path]) -> int:
    names: set[str] = set()
    for directory in foundation_dirs:
        path = directory / "channel_dictionary.csv"
        if path.exists():
            names.update(row.get("channel_name", "") for row in read_csv(path))
    return len(names)


def load_trace(row: dict[str, Any], config: dict[str, Any]) -> list[dict[str, Any]]:
    foundation = Path(row["__foundation_dir"])
    index_rows = read_csv(foundation / "file_index.csv")
    paths = {item["filename"]: Path(item["path"]) for item in index_rows}
    cache: dict[str, list[dict[str, Any]]] = {}
    for filename in str(row.get("source_files", "")).split(";"):
        path = paths.get(filename)
        if path and path.exists():
            cache[filename], _ = sampled_file_points(path, config)
    candidate = {
        "start_time": row["candidate_start"],
        "end_time": row["candidate_end"],
        "source_files": row.get("source_files", ""),
    }
    return candidate_points(candidate, cache)[0]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=str(PACKAGE_DIR / "config" / "config.yaml"),
        help="JSON-compatible YAML configuration file",
    )
    parser.add_argument("--limit-datasets", type=int, default=0, help="test only: process first N days")
    args = parser.parse_args()
    with Path(args.config).open(encoding="utf-8") as handle:
        config = json.load(handle)
    output_dir = (PACKAGE_DIR / config["output_dir"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    foundation_dirs = sorted(
        path
        for path in (WORKSPACE_ROOT / "outputs").glob("data_foundation*")
        if path.is_dir() and (path / "production_segments.csv").exists()
    )
    if args.limit_datasets:
        foundation_dirs = foundation_dirs[: args.limit_datasets]
    if not foundation_dirs:
        raise SystemExit("no data foundation directories found")

    profiler = DataProfiler(list(CHANNELS), config["random_seed"])
    high_windows: list[dict[str, Any]] = []
    low_windows: list[dict[str, Any]] = []
    candidate_lookup: dict[tuple[str, str], dict[str, Any]] = {}
    parse_errors: list[dict[str, Any]] = []
    raw_file_count = 0
    valid_raw_file_count = 0
    invalid_raw_file_count = 0
    raw_frame_count = 0
    sampled_point_count = 0
    candidate_count = 0
    all_starts: list[str] = []
    all_ends: list[str] = []
    high_speed_ratios: list[float] = []
    adaptive_high_speed_seconds = 0
    fixed_high_speed_seconds = 0

    with ProcessPoolExecutor(max_workers=max(1, int(config.get("worker_processes", 1)))) as executor:
      for day_index, foundation in enumerate(foundation_dirs, 1):
        file_rows = read_csv(foundation / "file_index.csv")
        candidates = read_csv(foundation / "production_segments.csv")
        dataset = dataset_name(foundation, file_rows)
        print(f"[{day_index}/{len(foundation_dirs)}] {dataset}: {len(candidates)} candidate segments", flush=True)
        raw_file_count += len(file_rows)
        valid_file_rows = [
            row for row in file_rows
            if row.get("metadata_status") == "ok" and row.get("parse_status") == "ok"
        ]
        valid_raw_file_count += len(valid_file_rows)
        invalid_raw_file_count += len(file_rows) - len(valid_file_rows)
        raw_frame_count += sum(int(float(row.get("frames") or 0)) for row in valid_file_rows)
        all_starts.extend(row["start_time"] for row in valid_file_rows if row.get("start_time"))
        all_ends.extend(row["end_time"] for row in valid_file_rows if row.get("end_time"))
        path_map = {row["filename"]: Path(row["path"]) for row in file_rows}
        needed_files = {
            name
            for candidate in candidates
            for name in candidate.get("source_files", "").split(";")
            if name
        }
        cache: dict[str, list[dict[str, Any]]] = {}
        tasks = []
        for filename in sorted(needed_files):
            path = path_map.get(filename)
            if path is None or not path.exists():
                parse_errors.append({"dataset": dataset, "filename": filename, "error": "file_missing"})
            else:
                tasks.append((str(path), config))
        for filename, points, errors, fatal_error in executor.map(sample_worker, tasks, chunksize=1):
            if fatal_error or points is None:
                parse_errors.append({"dataset": dataset, "filename": filename, "error": fatal_error})
                continue
            cache[filename] = points
            sampled_point_count += len(points)
            for point in points:
                profiler.add_point(point)
            for channel, error in errors.items():
                parse_errors.append({"dataset": dataset, "filename": filename, "channel": channel, "error": error})

        for candidate in candidates:
            candidate_count += 1
            points, duplicates, conflicts = candidate_points(candidate, cache)
            high, low, detection = detect_windows(points, config)
            if isinstance(detection.get("high_speed_ratio"), (int, float)):
                high_speed_ratios.append(float(detection["high_speed_ratio"]))
            adaptive_high_speed_seconds += int(detection.get("adaptive_high_speed_seconds") or 0)
            fixed_high_speed_seconds += int(detection.get("fixed_high_speed_seconds") or 0)
            key = (dataset, candidate["candidate_id"])
            candidate_lookup[key] = {
                "dataset": dataset,
                "candidate_id": candidate["candidate_id"],
                "candidate_start": candidate.get("start_time", ""),
                "candidate_end": candidate.get("end_time", ""),
                "source_files": candidate.get("source_files", ""),
                "__foundation_dir": str(foundation),
            }
            high_windows.extend(
                enrich_window(
                    row,
                    dataset=dataset,
                    candidate=candidate,
                    detection=detection,
                    duplicates=duplicates,
                    conflicts=conflicts,
                    foundation_dir=foundation,
                )
                for row in high
            )
            low_windows.extend(
                enrich_window(
                    row,
                    dataset=dataset,
                    candidate=candidate,
                    detection=detection,
                    duplicates=duplicates,
                    conflicts=conflicts,
                    foundation_dir=foundation,
                )
                for row in low
            )
        del cache

    print(f"Calibrating score with {len(high_windows)} high-speed windows", flush=True)
    thresholds = calibrate_thresholds(high_windows, config)
    scored = [score_window(row, thresholds, config) for row in high_windows]
    gold_rows = merge_adjacent_gold(scored)
    abnormal_rows = [row for row in scored if row.get("golden_level") == "ABNORMAL"]
    profile_rows = profiler.rows()

    preferred = [
        "golden_segment_id", "coil_id", "is_true_coil_id", "coil_boundary_confidence",
        "pass_no", "pass_confidence", "dataset", "candidate_id", "stable_segment_start",
        "stable_segment_end", "stable_duration", "merged_window_count", "current_entry_thickness",
        "f1_exit_thickness", "f2_exit_thickness", "target_exit_thickness", "width",
        "reduction_f1", "reduction_f2", "f1_force_actual_median", "f2_force_actual_median",
        "entry_tension_set", "entry_tension_actual_median", "interstand_tension_set",
        "interstand_tension_actual_median", "exit_tension_set", "exit_tension_actual_median",
        "entry_tension_stress_kn_per_mm2", "interstand_tension_stress_kn_per_mm2",
        "exit_tension_stress_kn_per_mm2", "speed_median", "speed_p95", "f1_force_cv",
        "f2_force_cv", "entry_tension_cv", "interstand_tension_cv", "exit_tension_cv",
        "golden_score", "golden_level", "abnormal_reason", "source_files",
    ]
    write_csv(output_dir / "golden_recipe_dataset.csv", gold_rows, preferred)
    write_csv(output_dir / "abnormal_segments.csv", abnormal_rows, preferred)
    write_csv(output_dir / "all_high_speed_window_audit.csv", scored, preferred)
    write_csv(output_dir / "low_speed_window_audit.csv", low_windows, preferred)
    write_csv(output_dir / "parse_errors.csv", parse_errors)
    profiler.write_csv(output_dir / "column_profile.csv")
    channel_dictionaries = [
        (directory / "channel_dictionary.csv", read_csv(directory / "channel_dictionary.csv"))
        for directory in foundation_dirs
        if (directory / "channel_dictionary.csv").exists()
    ]
    write_csv(
        output_dir / "all_channel_inventory.csv",
        build_all_channel_inventory(channel_dictionaries, raw_file_count),
    )
    (output_dir / "calibrated_thresholds.json").write_text(
        json.dumps(thresholds, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    write_column_mapping(output_dir / "column_mapping.md", load_all_channel_count(foundation_dirs))

    figures_dir = output_dir / "figures"
    figures_dir.mkdir(parents=True, exist_ok=True)
    for stale_figure in figures_dir.glob("*.svg"):
        stale_figure.unlink()
    selections: list[dict[str, Any]] = []
    for level, count in (("GOLD_A", 5), ("GOLD_B", 3), ("ABNORMAL", 3)):
        candidates = [row for row in scored if row.get("golden_level") == level]
        candidates.sort(key=lambda row: row.get("golden_score", 0), reverse=level != "ABNORMAL")
        seen_candidates: set[tuple[str, str]] = set()
        for row in candidates:
            key = (str(row.get("dataset")), str(row.get("candidate_id")))
            if key in seen_candidates:
                continue
            seen_candidates.add(key)
            selections.append(row)
            if len(seen_candidates) >= count:
                break
    figure_names: list[str] = []
    for index, row in enumerate(selections, 1):
        try:
            trace = load_trace(row, config)
        except Exception as exc:
            parse_errors.append({"dataset": row.get("dataset"), "filename": "figure_trace", "error": str(exc)})
            continue
        name = f"{index:02d}_{row['golden_level']}_{row['dataset']}_{row['candidate_id']}.svg"
        write_trace_svg(
            output_dir / "figures" / name,
            trace,
            row["stable_segment_start"],
            row["stable_segment_end"],
            f"{row['golden_level']} · {row['dataset']} · {row['candidate_id']} · score {row['golden_score']}",
        )
        figure_names.append(name)

    raw_summary = {
        "version": config["version"],
        "file_count": raw_file_count,
        "valid_file_count": valid_raw_file_count,
        "invalid_file_count": invalid_raw_file_count,
        "raw_frame_count": raw_frame_count,
        "time_start": min(all_starts) if all_starts else "—",
        "time_end": max(all_ends) if all_ends else "—",
        "candidate_count": candidate_count,
        "sampled_point_count": sampled_point_count,
        "median_high_speed_ratio": percentile(high_speed_ratios, 0.50),
        "adaptive_high_speed_seconds": adaptive_high_speed_seconds,
        "fixed_high_speed_seconds": fixed_high_speed_seconds,
    }
    write_report(
        output_dir / "cleaning_report.md",
        raw_summary=raw_summary,
        gold_rows=gold_rows,
        all_high_rows=scored,
        low_rows=low_windows,
        thresholds=thresholds,
        profile_rows=profile_rows,
        figure_names=figure_names,
    )
    (output_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                **raw_summary,
                "golden_segment_count": len(gold_rows),
                "window_level_counts": Counter(row["golden_level"] for row in scored),
                "output_files": sorted(str(path.relative_to(output_dir)) for path in output_dir.rglob("*") if path.is_file()),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Done: {output_dir}")
    print(f"Golden segments: {len(gold_rows)}; high-speed windows: {len(scored)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
