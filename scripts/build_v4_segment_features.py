#!/usr/bin/env python3
"""Build the auditable v0.4 segment feature table from ibaPDA DAT files.

The existing production segment table remains the source of product specifications,
high-speed thresholds and eligibility.  This script enriches only those eligible
high-speed plateaus with:

* the two operator-entered closed-gap presets;
* 49-zone AFC flatness deviation summaries in the native (undocumented) DAT value;
* drive/operation-side force and gap feedback plus bending/tilt feedback.

Low-speed tuning samples never enter the recommendation targets.  Flatness data is
runtime feedback, not a cold-start input.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from array import array
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from parse_ibapda import parse_dat  # noqa: E402


DEFAULT_DAYS = (
    "260712",
    "260713",
    "260714",
    "260716",
    "260717",
    "260718",
    "260719",
)

SPEED_CHANNEL = "YH.PLC.YH.机架间测速m/min"
FLATNESS_CHANNELS = tuple(
    f"YH.PLC.AFC版型偏差.版型偏差#{index}" for index in range(1, 50)
)

SCALAR_CHANNELS = {
    "tm1_closed_gap_set_median": "YH.PLC.YH.TM1闭辊缝位置设定",
    "tm2_closed_gap_set_median": "YH.PLC.YH.TM2闭辊缝位置设定",
    "f1_force_operation_median": "YH.PLC.YH.F1#操作侧轧制力T",
    "f1_force_drive_median": "YH.PLC.YH.F1#传动侧轧制力T",
    "f1_force_difference_plc_median": "YH.PLC.YH.F1#轧制力差T",
    "f2_force_operation_median": "YH.PLC.YH.F2#操作侧轧制力T",
    "f2_force_drive_median": "YH.PLC.YH.F2#传动侧轧制力T",
    "f2_force_difference_plc_median": "YH.PLC.YH.F2#轧制力差T",
    "f1_gap_operation_median": "YH.PLC.YH.F1#操作侧辊缝mm",
    "f1_gap_drive_median": "YH.PLC.YH.F1#传动侧辊缝mm",
    "f1_gap_difference_plc_median": "YH.PLC.YH.F1#辊缝差mm",
    "f2_gap_operation_median": "YH.PLC.YH.F2#操作侧辊缝mm",
    "f2_gap_drive_median": "YH.PLC.YH.F2#传动侧辊缝mm",
    "f2_gap_difference_plc_median": "YH.PLC.YH.F2#辊缝差mm",
    "f1_positive_bending_actual_median": "YH.PLC.YH.F1#正弯辊力T",
    "f1_negative_bending_actual_median": "YH.PLC.YH.F1#负弯辊力T",
    "f2_positive_bending_actual_median": "YH.PLC.YH.F2#正弯辊力T",
    "f2_negative_bending_actual_median": "YH.PLC.YH.F2#负弯辊力T",
    "tm1_tilt_adjustment_median": "YH.PLC.YH.1#机架倾斜调节量",
    "tm2_tilt_adjustment_median": "YH.PLC.YH.2#机架倾斜调节量",
    "uncoiler_tension_actual_median": "YH.PLC.YH.开卷实际张力",
    "interstand_tension_actual_median": "YH.PLC.YH.机架实际张力",
    "coiler_tension_actual_median": "YH.PLC.YH.卷取实际张力",
    "uncoiler_unit_tension_dat_median": "YH.PLC.YH.开卷单位张力",
    "interstand_unit_tension_dat_median": "YH.PLC.YH.机架单位张力",
    "coiler_unit_tension_dat_median": "YH.PLC.YH.卷取单位张力",
    "tm1_tension_adjustment_median": "YH.PLC.YH.TM1张力调节量um",
    "tm2_tension_adjustment_median": "YH.PLC.YH.TM2张力调节量um",
}

STATE_CHANNELS = {
    "strip_break_flag_max": ("YH.PLC.YH.机架断带", "max"),
    "drive_no_fault_state_min": ("YH.PLC.YH.传动无故障", "min"),
    # The two emergency-stop channels appear as 1 during normal high-speed
    # operation.  Their polarity must be confirmed in the PLC variable table,
    # so they are extracted but not interpreted as an alarm by this script.
    "total_estop_state_min": ("YH.PLC.YH.总急停", "min"),
    "fire_estop_state_min": ("YH.PLC.YH.灭火急停", "min"),
}

DERIVED_SIDE_COLUMNS = (
    "f1_force_drive_minus_operation_median",
    "f2_force_drive_minus_operation_median",
    "f1_gap_drive_minus_operation_median",
    "f2_gap_drive_minus_operation_median",
)

FLATNESS_COLUMNS = (
    "flatness_decoded_zone_count",
    "flatness_active_zone_start",
    "flatness_active_zone_end",
    "flatness_active_zone_count",
    "flatness_profile_valid_samples",
    "flatness_mean_median",
    "flatness_std_median",
    "flatness_rms_median",
    "flatness_max_abs_median",
    "flatness_peak_to_peak_median",
    "flatness_lr_asymmetry_median",
    "flatness_edge_minus_center_median",
    "flatness_slope_coefficient_median",
    "flatness_curvature_coefficient_median",
    "flatness_temporal_std_median",
)


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _median(values: Iterable[float]) -> float | None:
    clean = [value for value in values if math.isfinite(value)]
    return statistics.median(clean) if clean else None


def _format(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        if not math.isfinite(value):
            return ""
        return f"{value:.9g}"
    return str(value)


@dataclass
class SegmentAccumulator:
    row: dict[str, str]
    start: datetime
    end: datetime
    threshold: float
    scalar_values: dict[str, array] = field(
        default_factory=lambda: defaultdict(lambda: array("f"))
    )
    zone_values: dict[int, array] = field(
        default_factory=lambda: defaultdict(lambda: array("f"))
    )
    selected_samples: int = 0
    decoded_zones: set[int] = field(default_factory=set)
    channel_errors: set[str] = field(default_factory=set)
    parsed_files: set[str] = field(default_factory=set)


def _profile_metrics(zones: list[int], values: list[float]) -> dict[str, float] | None:
    points = [
        (zone, value)
        for zone, value in zip(zones, values)
        if math.isfinite(value)
    ]
    if len(points) < 5:
        return None

    first = min(zone for zone, _ in points)
    last = max(zone for zone, _ in points)
    span = max(last - first, 1)
    xs = [2.0 * (zone - first) / span - 1.0 for zone, _ in points]
    ys = [value for _, value in points]
    mean_y = statistics.fmean(ys)
    std_y = statistics.pstdev(ys)
    mean_x2 = statistics.fmean(x * x for x in xs)
    slope_denominator = sum(x * x for x in xs)
    slope = (
        sum(x * (y - mean_y) for x, y in zip(xs, ys)) / slope_denominator
        if slope_denominator
        else 0.0
    )
    qs = [x * x - mean_x2 for x in xs]
    curvature_denominator = sum(q * q for q in qs)
    curvature = (
        sum(q * (y - mean_y) for q, y in zip(qs, ys)) / curvature_denominator
        if curvature_denominator
        else 0.0
    )
    left = [y for x, y in zip(xs, ys) if x <= -0.25]
    right = [y for x, y in zip(xs, ys) if x >= 0.25]
    edge = [y for x, y in zip(xs, ys) if abs(x) >= 0.70]
    center = [y for x, y in zip(xs, ys) if abs(x) <= 0.20]
    return {
        "flatness_mean_median": mean_y,
        "flatness_std_median": std_y,
        "flatness_rms_median": math.sqrt(statistics.fmean(y * y for y in ys)),
        "flatness_max_abs_median": max(abs(y) for y in ys),
        "flatness_peak_to_peak_median": max(ys) - min(ys),
        # Positive means the high-numbered sensor side is higher.  Which physical
        # side that represents must be confirmed against the HMI wiring map.
        "flatness_lr_asymmetry_median": (
            statistics.fmean(right) - statistics.fmean(left)
            if left and right
            else math.nan
        ),
        "flatness_edge_minus_center_median": (
            statistics.fmean(edge) - statistics.fmean(center)
            if edge and center
            else math.nan
        ),
        "flatness_slope_coefficient_median": slope,
        "flatness_curvature_coefficient_median": curvature,
    }


def _summarize_segment(segment: SegmentAccumulator) -> dict[str, object]:
    enriched: dict[str, object] = dict(segment.row)
    enriched["extracted_high_speed_samples_1hz"] = segment.selected_samples
    for alias in SCALAR_CHANNELS:
        enriched[alias] = _median(segment.scalar_values.get(alias, ()))
    for alias, (_channel, reducer) in STATE_CHANNELS.items():
        values = [
            value
            for value in segment.scalar_values.get(alias, ())
            if math.isfinite(value)
        ]
        if not values:
            enriched[alias] = None
        elif reducer == "max":
            enriched[alias] = max(values)
        elif reducer == "min":
            enriched[alias] = min(values)
        else:
            raise ValueError(f"Unknown state reducer {reducer!r}")

    for stand in ("f1", "f2"):
        operation_force = enriched.get(f"{stand}_force_operation_median")
        drive_force = enriched.get(f"{stand}_force_drive_median")
        operation_gap = enriched.get(f"{stand}_gap_operation_median")
        drive_gap = enriched.get(f"{stand}_gap_drive_median")
        enriched[f"{stand}_force_drive_minus_operation_median"] = (
            float(drive_force) - float(operation_force)
            if drive_force is not None and operation_force is not None
            else None
        )
        enriched[f"{stand}_gap_drive_minus_operation_median"] = (
            float(drive_gap) - float(operation_gap)
            if drive_gap is not None and operation_gap is not None
            else None
        )

    enriched.update({column: None for column in FLATNESS_COLUMNS})
    enriched["flatness_decoded_zone_count"] = len(segment.decoded_zones)
    if segment.selected_samples:
        active_zones = []
        for zone in range(1, 50):
            values = segment.zone_values.get(zone, ())
            finite_values = [value for value in values if math.isfinite(value)]
            if not finite_values:
                continue
            nonzero_fraction = sum(abs(value) > 1e-8 for value in finite_values) / len(
                finite_values
            )
            if nonzero_fraction >= 0.50:
                active_zones.append(zone)

        if active_zones:
            enriched["flatness_active_zone_start"] = min(active_zones)
            enriched["flatness_active_zone_end"] = max(active_zones)
            enriched["flatness_active_zone_count"] = len(active_zones)
            metric_values: dict[str, list[float]] = defaultdict(list)
            for sample_index in range(segment.selected_samples):
                profile = [
                    segment.zone_values[zone][sample_index]
                    if sample_index < len(segment.zone_values[zone])
                    else math.nan
                    for zone in active_zones
                ]
                metrics = _profile_metrics(active_zones, profile)
                if metrics is None:
                    continue
                for key, value in metrics.items():
                    if math.isfinite(value):
                        metric_values[key].append(value)
            enriched["flatness_profile_valid_samples"] = len(
                metric_values.get("flatness_std_median", ())
            )
            for key, values in metric_values.items():
                enriched[key] = _median(values)

            zone_temporal_std = []
            for zone in active_zones:
                values = [
                    value
                    for value in segment.zone_values[zone]
                    if math.isfinite(value)
                ]
                if len(values) >= 2:
                    zone_temporal_std.append(statistics.pstdev(values))
            enriched["flatness_temporal_std_median"] = _median(zone_temporal_std)

    enriched["extraction_parsed_files"] = ";".join(sorted(segment.parsed_files))
    enriched["extraction_channel_error_count"] = len(segment.channel_errors)
    enriched["extraction_channel_errors"] = ";".join(sorted(segment.channel_errors))
    return enriched


def _load_day(day: str) -> tuple[list[SegmentAccumulator], dict[str, Path]]:
    foundation = WORKSPACE_ROOT / "outputs" / f"data_foundation_{day}"
    segment_path = foundation / "production_segments.csv"
    file_index_path = foundation / "file_index.csv"
    if not segment_path.exists() or not file_index_path.exists():
        raise FileNotFoundError(f"Missing data foundation for {day}: {foundation}")

    with segment_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = [
            row
            for row in csv.DictReader(handle)
            if row.get("learning_eligible", "").lower() == "true"
        ]
    segments = []
    for row in rows:
        copied = dict(row)
        copied["dataset"] = day
        segments.append(
            SegmentAccumulator(
                row=copied,
                start=_parse_datetime(row["start_time"]),
                end=_parse_datetime(row["end_time"]),
                threshold=float(row["high_speed_threshold_m_per_min"]),
            )
        )

    with file_index_path.open(encoding="utf-8-sig", newline="") as handle:
        file_paths = {
            row["filename"]: Path(row["path"])
            for row in csv.DictReader(handle)
            if row.get("parse_status") == "ok"
        }
    return segments, file_paths


def _extract_day(day: str) -> tuple[list[dict[str, object]], Counter[str]]:
    segments, file_paths = _load_day(day)
    by_filename: dict[str, list[SegmentAccumulator]] = defaultdict(list)
    for segment in segments:
        for filename in segment.row["source_files"].split(";"):
            if filename:
                by_filename[filename].append(segment)

    requested_channels = [
        SPEED_CHANNEL,
        *SCALAR_CHANNELS.values(),
        *(channel for channel, _reducer in STATE_CHANNELS.values()),
        *FLATNESS_CHANNELS,
    ]
    counters: Counter[str] = Counter()
    total_files = len(by_filename)
    for file_index, filename in enumerate(sorted(by_filename), start=1):
        path = file_paths.get(filename)
        if path is None or not path.exists():
            counters["missing_file"] += 1
            for segment in by_filename[filename]:
                segment.channel_errors.add(f"{filename}:missing_file")
            continue
        try:
            parsed = parse_dat(
                str(path),
                exact_channel_filter=requested_channels,
                skip_channel_errors=True,
            )
        except (OSError, ValueError) as exc:
            counters["parse_failure"] += 1
            for segment in by_filename[filename]:
                segment.channel_errors.add(f"{filename}:file:{type(exc).__name__}")
            continue

        data = parsed["data"]
        speed = data.get(SPEED_CHANNEL)
        if speed is None:
            counters["missing_speed"] += 1
            continue
        step = max(1, round(1.0 / float(parsed["clk"])))
        sample_indices = range(0, int(parsed["frames"]), step)
        sample_times = [
            parsed["start_dt"] + timedelta(seconds=index * float(parsed["clk"]))
            for index in sample_indices
        ]
        sample_indices = list(sample_indices)

        reverse_scalars = {channel: alias for alias, channel in SCALAR_CHANNELS.items()}
        reverse_states = {
            channel: alias for alias, (channel, _reducer) in STATE_CHANNELS.items()
        }
        zone_channels = {channel: index for index, channel in enumerate(FLATNESS_CHANNELS, 1)}
        decoded_zones = {
            zone for channel, zone in zone_channels.items() if channel in data
        }
        error_channels = set(parsed.get("channel_errors", {}))
        counters["channel_errors"] += len(error_channels)

        for segment in by_filename[filename]:
            segment.parsed_files.add(filename)
            segment.decoded_zones.update(decoded_zones)
            segment.channel_errors.update(f"{filename}:{name}" for name in error_channels)
            for raw_index, timestamp in zip(sample_indices, sample_times):
                if timestamp < segment.start or timestamp > segment.end:
                    continue
                speed_value = _finite(speed[raw_index])
                if speed_value is None or abs(speed_value) < segment.threshold:
                    continue
                for channel, alias in reverse_scalars.items():
                    values = data.get(channel)
                    value = _finite(values[raw_index]) if values is not None else None
                    if value is not None:
                        segment.scalar_values[alias].append(value)
                for channel, alias in reverse_states.items():
                    values = data.get(channel)
                    value = _finite(values[raw_index]) if values is not None else None
                    if value is not None:
                        segment.scalar_values[alias].append(value)
                for channel, zone in zone_channels.items():
                    values = data.get(channel)
                    value = _finite(values[raw_index]) if values is not None else None
                    segment.zone_values[zone].append(
                        value if value is not None else math.nan
                    )
                segment.selected_samples += 1
        counters["parsed_file"] += 1
        if file_index % 25 == 0 or file_index == total_files:
            print(
                f"[{day}] files {file_index}/{total_files}, "
                f"channel_errors={counters['channel_errors']}",
                flush=True,
            )

    rows = [_summarize_segment(segment) for segment in segments]
    counters["segments"] = len(rows)
    counters["segments_with_closed_gaps"] = sum(
        bool(row.get("tm1_closed_gap_set_median") is not None)
        and bool(row.get("tm2_closed_gap_set_median") is not None)
        for row in rows
    )
    counters["segments_with_flatness"] = sum(
        int(row.get("flatness_profile_valid_samples") or 0) > 0 for row in rows
    )
    return rows, counters


def build(days: tuple[str, ...], output: Path) -> None:
    all_rows: list[dict[str, object]] = []
    day_summaries: dict[str, dict[str, int]] = {}
    for day in days:
        rows, counters = _extract_day(day)
        all_rows.extend(rows)
        day_summaries[day] = dict(counters)
        print(
            f"[{day}] segments={counters['segments']} "
            f"closed_gaps={counters['segments_with_closed_gaps']} "
            f"flatness={counters['segments_with_flatness']}",
            flush=True,
        )

    if not all_rows:
        raise RuntimeError("No eligible production segments were extracted")
    output.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(all_rows[0])
    additional = [
        "extracted_high_speed_samples_1hz",
        *SCALAR_CHANNELS,
        *STATE_CHANNELS,
        *DERIVED_SIDE_COLUMNS,
        *FLATNESS_COLUMNS,
        "extraction_parsed_files",
        "extraction_channel_error_count",
        "extraction_channel_errors",
    ]
    for fieldname in additional:
        if fieldname not in fieldnames:
            fieldnames.append(fieldname)
    with output.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in all_rows:
            writer.writerow({key: _format(row.get(key)) for key in fieldnames})

    summary = {
        "schema_version": "v0.4-segment-features-1",
        "generated_at": datetime.now().astimezone().isoformat(),
        "days": list(days),
        "row_count": len(all_rows),
        "day_summaries": day_summaries,
        "sampling": {
            "rate_hz": 1,
            "window": "eligible segment samples at or above its stored high-speed threshold",
            "low_speed_excluded": True,
        },
        "units": {
            "closed_gap": "mm (provisional: HMI label and observed magnitude; DAT metadata is blank)",
            "flatness": "native DAT value (metadata is blank; not asserted as um/I-unit)",
            "force": "T where encoded in the channel name",
            "side_gap": "mm",
        },
        "flatness_orientation": (
            "low/high zone numbers are not yet mapped to operation/drive side; "
            "signed adjustment direction is intentionally not inferred"
        ),
        "output": str(output),
    }
    summary_path = output.with_suffix(".summary.json")
    summary_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"wrote {output} ({len(all_rows)} rows)", flush=True)
    print(f"wrote {summary_path}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--days",
        default=",".join(DEFAULT_DAYS),
        help="comma-separated dataset directory names",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=WORKSPACE_ROOT / "outputs" / "model_v4" / "segment_features.csv",
    )
    args = parser.parse_args()
    days = tuple(day.strip() for day in args.days.split(",") if day.strip())
    build(days, args.output.resolve())


if __name__ == "__main__":
    main()
