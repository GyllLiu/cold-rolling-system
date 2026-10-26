#!/usr/bin/env python3
"""Build a compact, auditable data foundation from ibaPDA ``.dat`` files.

The script deliberately keeps the original ``parse_ibapda.py`` untouched.  It
uses ``parse_dat`` to decode only a small set of core signals, samples those
signals at 1 Hz, and writes metadata/quality/candidate-segment artifacts.

Production segments emitted here are heuristic *candidates*.  They are not
coil IDs and must not be treated as ground-truth rolls or passes.

Example
-------
python3 build_data_foundation.py 260703/260703 \
    --output-dir outputs/data_foundation
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import math
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Optional

from parse_ibapda import parse_dat


CORE_CHANNELS: dict[str, str] = {
    # Product/specification signals.
    "entry_thickness_set": "YH.PLC.YH.入口厚度设定",
    "entry_thickness_actual": "YH.PLC.YH.入口实际厚度",
    "intermediate_thickness_set": "YH.PLC.YH.中间厚度设定",
    "exit_thickness_set": "YH.PLC.YH.出口厚度设定",
    "exit_thickness_actual": "YH.PLC.YH.出口实际厚度",
    "width_set": "YH.PLC.YH.宽度设定",
    # Speed and load signals used for activity detection.
    "main_speed_set": "YH.PLC.YH.主速度设定",
    "line_speed_actual": "YH.PLC.YH.机架间测速m/min",
    "f1_speed_set": "YH.PLC.YH.F1机架给定速度",
    "f2_speed_set": "YH.PLC.YH.F2机架给定速度",
    "f1_main_current": "YH.PLC.YH.1-1主机电流",
    "f2_main_current": "YH.PLC.YH.2-1主机电流",
    "f1_roll_force_sum": "YH.PLC.YH.F1#轧制力和T",
    "f2_roll_force_sum": "YH.PLC.YH.F2#轧制力和T",
    # Gap/bending controls and feedback.
    "f1_gap_average": "YH.PLC.YH.F1#平均辊缝mm",
    "f2_gap_average": "YH.PLC.YH.F2#平均辊缝mm",
    "tm1_gap_command": "YH.PLC.YH.TM1综合设定辊缝um",
    "tm2_gap_command": "YH.PLC.YH.TM2综合设定辊缝um",
    "tm1_tilt_set": "YH.PLC.YH.TM1倾斜量设定um",
    "tm2_tilt_set": "YH.PLC.YH.TM2倾斜量设定um",
    "f1_bending_set": "YH.PLC.YH.F1弯辊力设定",
    "f2_bending_set": "YH.PLC.YH.F2弯辊力设定",
    # Three tension zones.
    "uncoiler_tension_set": "YH.PLC.YH.开卷张力设定",
    "uncoiler_tension_actual": "YH.PLC.YH.开卷实际张力",
    "interstand_tension_set": "YH.PLC.YH.机架张力设定",
    "interstand_tension_actual": "YH.PLC.YH.机架实际张力",
    "coiler_tension_set": "YH.PLC.YH.卷取张力设定",
    "coiler_tension_actual": "YH.PLC.YH.卷取实际张力",
    # Diameter helps identify possible material transitions.
    "uncoiler_diameter_actual": "YH.PLC.YH.开卷机实际卷径",
    "coiler_diameter_actual": "YH.PLC.YH.卷取实际卷径",
    # Digital states are sampled only when parse_dat returns frame-aligned data.
    "drive_enabled": "YH.PLC.YH.传动使能",
    "machine_started": "YH.PLC.YH.机组已启动",
    "machine_stopped": "YH.PLC.YH.机组已停止",
    "uncoiler_tension_built": "YH.PLC.YH.开卷已建张",
    "interstand_tension_built": "YH.PLC.YH.机架已建张",
    "coiler_tension_built": "YH.PLC.YH.卷取已建张",
}

DIGITAL_ALIASES = {
    "drive_enabled",
    "machine_started",
    "machine_stopped",
    "uncoiler_tension_built",
    "interstand_tension_built",
    "coiler_tension_built",
}

FREEZE_SIGNATURE_ALIASES = (
    "line_speed_actual",
    "entry_thickness_actual",
    "exit_thickness_actual",
    "f1_roll_force_sum",
    "f2_roll_force_sum",
    "f1_gap_average",
    "f2_gap_average",
    "interstand_tension_actual",
    "uncoiler_diameter_actual",
    "coiler_diameter_actual",
)

RUN_SPEED_THRESHOLD = 10.0
HIGH_SPEED_MIN_M_PER_MIN = 50.0
HIGH_SPEED_RATIO_OF_SEGMENT_MAX = 0.70
MIN_LEARNING_STABLE_SECONDS = 120
FREEZE_MIN_SECONDS = 300
ACTIVE_GAP_SPLIT_SECONDS = 120
SPEC_DEBOUNCE_ACTIVE_SECONDS = 30
MIN_CANDIDATE_ACTIVE_SECONDS = 10
CONTINUITY_TOLERANCE_SECONDS = 0.05


@dataclass
class ChannelMeta:
    index: int
    name: str
    digital: bool


@dataclass
class FileMeta:
    path: Path
    filename: str
    file_number: Optional[int]
    size_bytes: int
    start_dt: Optional[datetime]
    clk: Optional[float]
    frames: Optional[int]
    channels: list[ChannelMeta] = field(default_factory=list)
    metadata_status: str = "ok"
    metadata_error: str = ""

    @property
    def end_dt(self) -> Optional[datetime]:
        if self.start_dt is None or self.clk is None or self.frames is None:
            return None
        return self.start_dt + timedelta(seconds=self.clk * self.frames)

    @property
    def schema_hash(self) -> str:
        payload = "\n".join(
            f"{channel.index}|{channel.name}|{int(channel.digital)}"
            for channel in self.channels
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


@dataclass
class SamplePoint:
    timestamp: datetime
    file_index: int
    filename: str
    values: dict[str, Any]
    suspected_frozen: bool = False


def _decode_text(raw: bytes) -> str:
    try:
        return raw.decode("gbk")
    except (UnicodeDecodeError, LookupError):
        return raw.decode("latin1", errors="replace")


def _extract_scalar(blob: bytes, key: bytes) -> Optional[str]:
    match = re.search(rb"(?:^|\r\n)" + re.escape(key) + rb":([^\r\n]*)", blob)
    if not match:
        return None
    return match.group(1).decode("ascii", errors="replace").strip()


def read_file_metadata(path: Path) -> FileMeta:
    """Read header/channel metadata without decoding time-series payloads."""
    match = re.search(r"(\d+)", path.stem)
    file_number = int(match.group(1)) if match else None
    result = FileMeta(
        path=path.resolve(),
        filename=path.name,
        file_number=file_number,
        size_bytes=path.stat().st_size,
        start_dt=None,
        clk=None,
        frames=None,
    )
    try:
        with path.open("rb") as handle:
            magic = handle.read(4)
            if magic != b"PDA2":
                raise ValueError(f"invalid magic {magic!r}")
            handle.read(4)
            ascii_offset = int.from_bytes(handle.read(4), "little")
            handle.seek(ascii_offset)
            # Channel metadata ends near the beginning of these files.  Two MiB
            # is deliberately bounded so indexing never reads a full payload.
            blob = handle.read(2 * 1024 * 1024)

        starttime = _extract_scalar(blob, b"starttime")
        clk = _extract_scalar(blob, b"clk")
        frames = _extract_scalar(blob, b"frames")
        if not starttime or clk is None or frames is None:
            raise ValueError("required header field missing")
        result.start_dt = datetime.strptime(starttime, "%d.%m.%Y %H:%M:%S.%f")
        result.clk = float(clk)
        result.frames = int(frames)

        channel_pattern = re.compile(
            rb"(?:^|\r\n)beginchannel:(\d+)\r\n(.*?)\r\nendchannel:",
            re.DOTALL,
        )
        for channel_match in channel_pattern.finditer(blob):
            channel_index = int(channel_match.group(1))
            block = channel_match.group(2)
            name_match = re.search(rb"(?:^|\r\n)name:([^\r\n]*)", block)
            if not name_match:
                continue
            result.channels.append(
                ChannelMeta(
                    index=channel_index,
                    name=_decode_text(name_match.group(1)),
                    digital=b"\r\ndigchannel:" in b"\r\n" + block,
                )
            )
        if not result.channels:
            raise ValueError("no channel metadata found")
    except Exception as exc:  # Per-file indexing must be fault tolerant.
        result.metadata_status = "error"
        result.metadata_error = f"{type(exc).__name__}: {exc}"
    return result


def _format_dt(value: Optional[datetime]) -> str:
    return value.isoformat(timespec="milliseconds") if value else ""


def _safe_float(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _sample_value(values: list[Any], index: int) -> Any:
    if index >= len(values):
        return None
    value = values[index]
    if isinstance(value, bool):
        return value
    return _safe_float(value)


def _mode_numeric(points: Iterable[SamplePoint], alias: str, digits: int) -> Optional[float]:
    values = []
    for point in points:
        value = _safe_float(point.values.get(alias))
        if value is not None:
            values.append(round(value, digits))
    if not values:
        return None
    return Counter(values).most_common(1)[0][0]


def _numeric_values(points: Iterable[SamplePoint], alias: str) -> list[float]:
    output = []
    for point in points:
        value = _safe_float(point.values.get(alias))
        if value is not None:
            output.append(value)
    return output


def _median(points: Iterable[SamplePoint], alias: str) -> Optional[float]:
    values = _numeric_values(points, alias)
    return statistics.median(values) if values else None


def _round_or_blank(value: Optional[float], digits: int = 6) -> Any:
    return "" if value is None else round(value, digits)


def _channel_category(name: str) -> str:
    if "AFC版型偏差" in name:
        return "flatness"
    if re.search(r"YH\.PLC\.YH\.(?:R|B)", name):
        return "raw_register"
    categories = (
        ("厚", "thickness"),
        ("宽", "width"),
        ("速度", "speed"),
        ("转速", "speed"),
        ("张力", "tension"),
        ("辊缝", "gap"),
        ("弯辊", "bending"),
        ("轧制力", "rolling_force"),
        ("卷径", "diameter"),
        ("电流", "drive"),
        ("转矩", "drive"),
        ("故障", "fault"),
        ("油", "lubrication_status"),
    )
    for token, category in categories:
        if token in name:
            return category
    return "other"


def _channel_role(name: str, digital: bool) -> str:
    if re.search(r"YH\.PLC\.YH\.(?:R|B)", name):
        return "unmapped_raw_register"
    if digital:
        return "status_or_command"
    if any(token in name for token in ("计算", "调节量", "压下率", "单位张力")):
        return "plc_calculation"
    if "综合设定辊缝" in name:
        return "plc_computed_setpoint"
    if "设定" in name:
        if "张力" in name:
            return "plc_or_recipe_setpoint"
        return "user_or_recipe_setpoint"
    if any(
        token in name
        for token in (
            "实际",
            "厚差",
            "轧制力",
            "磁尺",
            "电流",
            "转矩",
            "转速",
            "版型偏差",
        )
    ):
        return "process_feedback"
    return "process_signal"


def _unit_hint(name: str, digital: bool) -> str:
    if digital:
        return "boolean"
    if "m/min" in name:
        return "m/min"
    if "m/s" in name:
        return "m/s"
    if "μm" in name or "um" in name:
        return "um"
    if "mm" in name:
        return "mm"
    if "轧制力T" in name or "弯辊力" in name:
        return "T"
    if "开口度" in name:
        return "% (inferred)"
    if any(token in name for token in ("厚度", "宽度", "卷径")):
        return "mm (inferred from HMI)"
    return ""


def _source_module(name: str) -> str:
    if name.startswith("YH.PLC.AFC"):
        return "AFC"
    if name.startswith("YH.PLC.YH"):
        return "YH PLC"
    return "unknown"


def _freeze_signature(point: SamplePoint) -> Optional[tuple[Any, ...]]:
    signature: list[Any] = []
    valid = 0
    for alias in FREEZE_SIGNATURE_ALIASES:
        value = _safe_float(point.values.get(alias))
        if value is None:
            signature.append(None)
        else:
            signature.append(round(value, 6))
            valid += 1
    return tuple(signature) if valid >= 6 else None


def mark_suspected_freezes(
    points: list[SamplePoint], min_seconds: int
) -> list[dict[str, Any]]:
    """Mark exact, multi-signal flatlines; these remain suspicion, not proof."""
    intervals: list[dict[str, Any]] = []
    if not points:
        return intervals
    run_start = 0
    previous_signature = _freeze_signature(points[0])
    for index in range(1, len(points) + 1):
        boundary = index == len(points)
        current_signature = None if boundary else _freeze_signature(points[index])
        if not boundary:
            gap = (points[index].timestamp - points[index - 1].timestamp).total_seconds()
            boundary = gap > 1.1 or current_signature != previous_signature
        if boundary:
            start_point = points[run_start]
            end_point = points[index - 1]
            duration = int(round((end_point.timestamp - start_point.timestamp).total_seconds())) + 1
            if previous_signature is not None and duration >= min_seconds:
                for frozen_index in range(run_start, index):
                    points[frozen_index].suspected_frozen = True
                intervals.append(
                    {
                        "start_time": start_point.timestamp,
                        "end_time": end_point.timestamp + timedelta(seconds=1),
                        "duration_seconds": duration,
                        "files": sorted(
                            {point.filename for point in points[run_start:index]}
                        ),
                    }
                )
            run_start = index
            previous_signature = current_signature
    return intervals


def _is_active(point: SamplePoint) -> bool:
    speed = _safe_float(point.values.get("line_speed_actual"))
    return bool(
        speed is not None
        and abs(speed) > RUN_SPEED_THRESHOLD
        and not point.suspected_frozen
    )


def _primary_spec(point: SamplePoint) -> Optional[tuple[float, float, float]]:
    entry = _safe_float(point.values.get("entry_thickness_set"))
    exit_value = _safe_float(point.values.get("exit_thickness_set"))
    width = _safe_float(point.values.get("width_set"))
    if not entry or not exit_value or not width:
        return None
    return round(entry, 3), round(exit_value, 3), round(width, 1)


def _split_active_cluster_on_stable_spec(
    cluster: list[SamplePoint], debounce_seconds: int
) -> list[list[SamplePoint]]:
    if not cluster:
        return []
    segments: list[list[SamplePoint]] = []
    segment_start = 0
    current_spec = _primary_spec(cluster[0])
    pending_spec: Optional[tuple[float, float, float]] = None
    pending_start = 0
    pending_count = 0
    for index, point in enumerate(cluster[1:], start=1):
        spec = _primary_spec(point)
        if spec is None or spec == current_spec:
            pending_spec = None
            pending_count = 0
            continue
        if spec == pending_spec:
            pending_count += 1
        else:
            pending_spec = spec
            pending_start = index
            pending_count = 1
        if pending_count >= debounce_seconds:
            if pending_start > segment_start:
                segments.append(cluster[segment_start:pending_start])
            segment_start = pending_start
            current_spec = pending_spec
            pending_spec = None
            pending_count = 0
    segments.append(cluster[segment_start:])
    return segments


def build_candidate_segments(points: list[SamplePoint]) -> list[dict[str, Any]]:
    active_points = [point for point in points if _is_active(point)]
    if not active_points:
        return []

    clusters: list[list[SamplePoint]] = []
    cluster = [active_points[0]]
    for point in active_points[1:]:
        gap = (point.timestamp - cluster[-1].timestamp).total_seconds()
        if gap > ACTIVE_GAP_SPLIT_SECONDS:
            clusters.append(cluster)
            cluster = [point]
        else:
            cluster.append(point)
    clusters.append(cluster)

    candidate_point_groups: list[list[SamplePoint]] = []
    for active_cluster in clusters:
        candidate_point_groups.extend(
            _split_active_cluster_on_stable_spec(
                active_cluster, SPEC_DEBOUNCE_ACTIVE_SECONDS
            )
        )

    rows: list[dict[str, Any]] = []
    for group in candidate_point_groups:
        if len(group) < MIN_CANDIDATE_ACTIVE_SECONDS:
            continue
        speeds = _numeric_values(group, "line_speed_actual")
        max_speed = max(abs(value) for value in speeds) if speeds else 0.0
        stable_threshold = max(
            HIGH_SPEED_MIN_M_PER_MIN,
            HIGH_SPEED_RATIO_OF_SEGMENT_MAX * max_speed,
        )
        stable_points = [
            point
            for point in group
            if (_safe_float(point.values.get("line_speed_actual")) is not None)
            and abs(float(point.values["line_speed_actual"])) >= stable_threshold
        ]
        # Low-speed operation is retained as an auditable tuning candidate, but
        # it must not contribute targets to the recommendation model.  Only the
        # high-speed plateau represents settings the operator has already tuned.
        summary_points = stable_points or group

        entry_set = _mode_numeric(summary_points, "entry_thickness_set", 3)
        intermediate_set = _mode_numeric(summary_points, "intermediate_thickness_set", 3)
        exit_set = _mode_numeric(summary_points, "exit_thickness_set", 3)
        width_set = _mode_numeric(summary_points, "width_set", 1)
        exit_actuals = [
            value
            for value in _numeric_values(stable_points, "exit_thickness_actual")
            if 0 < value < 20
        ]
        exit_errors = (
            [value - exit_set for value in exit_actuals]
            if exit_set is not None
            else []
        )
        files = list(dict.fromkeys(point.filename for point in group))
        digital_values_available = any(
            isinstance(point.values.get(alias), bool)
            for point in group
            for alias in DIGITAL_ALIASES
        )
        learning_eligible = bool(
            len(stable_points) >= MIN_LEARNING_STABLE_SECONDS
            and entry_set is not None
            and exit_set is not None
            and width_set is not None
            and len(exit_actuals) >= 30
        )
        confidence = "medium" if learning_eligible else "low"
        if learning_eligible and digital_values_available:
            confidence = "medium_plus"

        total_reduction = None
        f1_reduction = None
        f2_reduction = None
        if entry_set and exit_set:
            total_reduction = (entry_set - exit_set) / entry_set
        if entry_set and intermediate_set:
            f1_reduction = (entry_set - intermediate_set) / entry_set
        if intermediate_set and exit_set:
            f2_reduction = (intermediate_set - exit_set) / intermediate_set


        rows.append(
            {
                "candidate_id": f"candidate_{len(rows) + 1:03d}",
                "record_type": "heuristic_production_segment_candidate",
                "is_true_coil_or_pass": "false",
                "start_time": _format_dt(group[0].timestamp),
                "end_time": _format_dt(group[-1].timestamp + timedelta(seconds=1)),
                "span_seconds": int(
                    round((group[-1].timestamp - group[0].timestamp).total_seconds())
                )
                + 1,
                "active_seconds_1hz": len(group),
                "stable_high_speed_seconds_1hz": len(stable_points),
                "low_speed_tuning_seconds_1hz": len(group) - len(stable_points),
                "high_speed_threshold_m_per_min": _round_or_blank(stable_threshold),
                "learning_eligible": str(learning_eligible).lower(),
                "source_files": ";".join(files),
                "confidence": confidence,
                "entry_thickness_set_mode": _round_or_blank(entry_set),
                "intermediate_thickness_set_mode": _round_or_blank(intermediate_set),
                "exit_thickness_set_mode": _round_or_blank(exit_set),
                "width_set_mode": _round_or_blank(width_set, 1),
                "total_reduction_ratio": _round_or_blank(total_reduction),
                "f1_reduction_ratio": _round_or_blank(f1_reduction),
                "f2_reduction_ratio": _round_or_blank(f2_reduction),
                "main_speed_set_median": _round_or_blank(
                    _median(stable_points, "main_speed_set")
                ),
                "line_speed_actual_median": _round_or_blank(
                    _median(stable_points, "line_speed_actual")
                ),
                "line_speed_actual_max_abs": _round_or_blank(max_speed),
                "entry_thickness_actual_median": _round_or_blank(
                    _median(stable_points, "entry_thickness_actual")
                ),
                "exit_thickness_actual_median": _round_or_blank(
                    statistics.median(exit_actuals) if exit_actuals else None
                ),
                "exit_thickness_error_median": _round_or_blank(
                    statistics.median(exit_errors) if exit_errors else None
                ),
                "exit_thickness_error_abs_p95": _round_or_blank(
                    _percentile([abs(value) for value in exit_errors], 0.95)
                    if exit_errors
                    else None
                ),
                "f1_gap_average_median": _round_or_blank(_median(stable_points, "f1_gap_average")),
                "f2_gap_average_median": _round_or_blank(_median(stable_points, "f2_gap_average")),
                "tm1_gap_command_median": _round_or_blank(_median(stable_points, "tm1_gap_command")),
                "tm2_gap_command_median": _round_or_blank(_median(stable_points, "tm2_gap_command")),
                "tm1_tilt_set_median": _round_or_blank(_median(stable_points, "tm1_tilt_set")),
                "tm2_tilt_set_median": _round_or_blank(_median(stable_points, "tm2_tilt_set")),
                "f1_bending_set_median": _round_or_blank(_median(stable_points, "f1_bending_set")),
                "f2_bending_set_median": _round_or_blank(_median(stable_points, "f2_bending_set")),
                "uncoiler_tension_set_median": _round_or_blank(_median(stable_points, "uncoiler_tension_set")),
                "interstand_tension_set_median": _round_or_blank(_median(stable_points, "interstand_tension_set")),
                "coiler_tension_set_median": _round_or_blank(_median(stable_points, "coiler_tension_set")),
                "f1_roll_force_sum_median": _round_or_blank(_median(stable_points, "f1_roll_force_sum")),
                "f2_roll_force_sum_median": _round_or_blank(_median(stable_points, "f2_roll_force_sum")),
                "uncoiler_diameter_start": _round_or_blank(
                    _safe_float(group[0].values.get("uncoiler_diameter_actual"))
                ),
                "uncoiler_diameter_end": _round_or_blank(
                    _safe_float(group[-1].values.get("uncoiler_diameter_actual"))
                ),
                "coiler_diameter_start": _round_or_blank(
                    _safe_float(group[0].values.get("coiler_diameter_actual"))
                ),
                "coiler_diameter_end": _round_or_blank(
                    _safe_float(group[-1].values.get("coiler_diameter_actual"))
                ),
                "digital_state_usable": str(digital_values_available).lower(),
                "notes": (
                    "Candidate only; recommendation targets use the high-speed plateau "
                    "and exclude low-speed operator tuning. Requires coil/pass ID and "
                    "operator validation."
                ),
            }
        )
    return rows


def _percentile(values: list[float], fraction: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _markdown_table(headers: list[str], rows: list[list[Any]]) -> str:
    output = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    for row in rows:
        output.append("| " + " | ".join(str(value) for value in row) + " |")
    return "\n".join(output)


def build_report(
    input_dir: Path,
    file_rows: list[dict[str, Any]],
    quality_rows: list[dict[str, Any]],
    channel_rows: list[dict[str, Any]],
    points: list[SamplePoint],
    freeze_intervals: list[dict[str, Any]],
    segment_rows: list[dict[str, Any]],
) -> str:
    successful = sum(row["parse_status"] == "ok" for row in file_rows)
    failed = len(file_rows) - successful
    gaps = sum(row["time_continuity"] == "gap" for row in file_rows)
    overlaps = sum(row["time_continuity"] == "overlap" for row in file_rows)
    frozen_seconds = sum(point.suspected_frozen for point in points)
    raw_active = sum(
        abs(_safe_float(point.values.get("line_speed_actual")) or 0)
        > RUN_SPEED_THRESHOLD
        for point in points
    )
    usable_active = sum(_is_active(point) for point in points)
    digital_mismatch_files = sum(
        bool(row.get("digital_length_mismatch_channels")) for row in quality_rows
    )
    schema_hashes = {row["schema_hash"] for row in file_rows if row["schema_hash"]}
    start_times = [row["start_time"] for row in file_rows if row["start_time"]]
    end_times = [row["end_time"] for row in file_rows if row["end_time"]]
    total_bytes = sum(int(row["size_bytes"]) for row in file_rows)
    raw_register_count = sum(
        row.get("category") == "raw_register" for row in channel_rows
    )
    if digital_mismatch_files:
        digital_quality_text = (
            f"- 有数字量长度不等于文件帧数的文件：{digital_mismatch_files}/"
            f"{len(quality_rows)}。这些文件的数字状态不能用于硬判定生产边界。"
        )
    else:
        digital_quality_text = (
            f"- 本次 {len(quality_rows)} 个文件均通过数字量长度等于帧数的校验。"
            "解析器历史上存在数字量解码问题，因此现场投用前仍应抽取停机、切换、"
            "稳定生产文件，与 iba 官方导出逐点核对。"
        )

    lines = [
        "# 冷二联数据基础质量报告",
        "",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 输入目录：`{input_dir.resolve()}`",
        "- 抽样策略：核心信号 1 Hz；原始 100 Hz 数据不展开为全量 CSV。",
        "- 重要声明：`production_segments.csv` 中所有记录均为算法候选段，**不是真实卷号或道次标签**。",
        "",
        "## 数据总览",
        "",
        _markdown_table(
            ["项目", "结果"],
            [
                ["DAT 文件", len(file_rows)],
                ["解析成功 / 失败", f"{successful} / {failed}"],
                ["原始总大小", f"{total_bytes:,} bytes ({total_bytes / 1024 / 1024:.1f} MiB)"],
                ["时间范围", f"{min(start_times) if start_times else '-'} 至 {max(end_times) if end_times else '-'}"],
                ["1 Hz 样本点", f"{len(points):,}"],
                ["通道字典条目", len(channel_rows)],
                ["Schema 数量", len(schema_hashes)],
                ["文件时间缺口 / 重叠", f"{gaps} / {overlaps}"],
            ],
        ),
        "",
        "## 主要数据质量结论",
        "",
        f"- 多信号精确平线达到 {FREEZE_MIN_SECONDS} 秒即标记为“疑似冻结/保持值”；本次共 {len(freeze_intervals)} 段、{frozen_seconds:,} 秒。该标记是风险提示，不等同于已经证明采集故障。",
        f"- 机架间测速绝对值大于 {RUN_SPEED_THRESHOLD:g} m/min 的原始抽样点为 {raw_active:,} 秒；排除疑似冻结后为 {usable_active:,} 秒。",
        digital_quality_text,
        f"- 共生成 {len(segment_rows)} 个生产候选段；需要卷号、道次号和主操复核后才能形成真实训练样本。",
        "",
        "### 疑似冻结/保持值区间",
        "",
    ]
    if freeze_intervals:
        lines.append(
            _markdown_table(
                ["开始", "结束", "秒数", "涉及文件数"],
                [
                    [
                        _format_dt(interval["start_time"]),
                        _format_dt(interval["end_time"]),
                        interval["duration_seconds"],
                        len(interval["files"]),
                    ]
                    for interval in freeze_intervals
                ],
            )
        )
    else:
        lines.append("未检测到达到阈值的多信号精确平线。")

    lines.extend(
        [
            "",
        "## 生产候选段规则",
            "",
            f"1. 使用 `机架间测速m/min`，绝对值大于 {RUN_SPEED_THRESHOLD:g} m/min 视为活动点。",
            "2. 先排除疑似冻结点。",
            f"3. 两活动点相隔超过 {ACTIVE_GAP_SPLIT_SECONDS} 秒即分段。",
            f"4. 入口厚度、出口厚度、来料宽度的新组合连续出现至少 {SPEC_DEBOUNCE_ACTIVE_SECONDS} 个活动秒，才确认规格边界，避免设定斜坡产生大量碎段。",
            f"5. 少于 {MIN_CANDIDATE_ACTIVE_SECONDS} 个活动秒的短动作不输出为生产候选。",
        "6. 中间厚度可在同一主规格内调整，因此用于摘要，但不单独触发主规格边界。",
        f"7. 高速稳定区定义为机架间实际速度不低于 {HIGH_SPEED_MIN_M_PER_MIN:g} m/min，且不低于该候选段最高速度的 {HIGH_SPEED_RATIO_OF_SEGMENT_MAX:.0%}。",
        f"8. 推荐参数只取高速稳定区；低速点记为主操调试过程。高速稳定区不足 {MIN_LEARNING_STABLE_SECONDS} 秒的候选段不得进入学习集。",
            "",
            "## 已知字段缺口",
            "",
            "- 无命名卷号、母卷号、道次号、材质/合金状态、人工测厚和最终质检标签。",
            "- `宽度设定` 对应来料宽度；没有成品实际宽度或切边结果。",
            "- 无命名 F1/F2 冷却油/水开口度、油温、油压或实际流量。",
            "- 无命名出口厚度 AI 补偿百分比及 AGC 投入状态。",
            "- 无命名卷重、套筒直径和卷层字段。",
            f"- {raw_register_count} 个 `R/B` 原始寄存器需要 PLC 点表才能解释，缺失字段可能藏在其中。",
            "",
            "## 输出文件",
            "",
            "- `file_index.csv`：文件级元数据、时间连续性、schema 和解析状态。",
            "- `channel_dictionary.csv`：通道类型、类别、角色、单位提示和文件覆盖率。",
            "- `data_quality.csv`：每文件缺失、长度异常、冻结比例和活动秒数。",
            "- `production_segments.csv`：启发式生产候选段及 1 Hz 稳态摘要。",
            "- `data_quality_report.md`：本报告。",
            "",
            "## 使用限制",
            "",
            "当前产物适合数据审计、现场点表核对和相似配方原型。没有真实卷/道次、材质、人工测量和合格标签前，不应用于宣称“最佳参数”，也不得直接写入 PLC。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "input_dir",
        nargs="?",
        default="260703/260703",
        help="Directory containing ibaPDA .dat files (searched recursively)",
    )
    parser.add_argument(
        "--output-dir",
        default="outputs/data_foundation",
        help="Directory for generated foundation artifacts",
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir)
    output_dir = Path(args.output_dir)
    if not input_dir.exists():
        parser.error(f"input directory does not exist: {input_dir}")
    dat_files = sorted(
        input_dir.rglob("*.dat"),
        key=lambda path: (
            int(re.search(r"(\d+)", path.stem).group(1))
            if re.search(r"(\d+)", path.stem)
            else sys.maxsize,
            path.name,
        ),
    )
    if not dat_files:
        parser.error(f"no .dat files found under {input_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[1/5] Indexing {len(dat_files)} files from {input_dir}")
    metadata = [read_file_metadata(path) for path in dat_files]
    metadata.sort(
        key=lambda item: (
            item.start_dt or datetime.max,
            item.file_number if item.file_number is not None else sys.maxsize,
        )
    )

    schema_counts = Counter(item.schema_hash for item in metadata if item.channels)
    canonical_schema = schema_counts.most_common(1)[0][0] if schema_counts else ""
    file_rows: list[dict[str, Any]] = []
    previous_end: Optional[datetime] = None
    for meta in metadata:
        gap_seconds: Optional[float] = None
        continuity = "unknown"
        if meta.start_dt is not None:
            if previous_end is None:
                continuity = "first"
            else:
                gap_seconds = (meta.start_dt - previous_end).total_seconds()
                if abs(gap_seconds) <= CONTINUITY_TOLERANCE_SECONDS:
                    continuity = "contiguous"
                elif gap_seconds > 0:
                    continuity = "gap"
                else:
                    continuity = "overlap"
        if meta.end_dt is not None:
            previous_end = meta.end_dt
        analog_count = sum(not channel.digital for channel in meta.channels)
        digital_count = sum(channel.digital for channel in meta.channels)
        file_rows.append(
            {
                "filename": meta.filename,
                "file_number": meta.file_number if meta.file_number is not None else "",
                "path": str(meta.path),
                "size_bytes": meta.size_bytes,
                "start_time": _format_dt(meta.start_dt),
                "end_time": _format_dt(meta.end_dt),
                "clk_seconds": meta.clk if meta.clk is not None else "",
                "sample_rate_hz": round(1 / meta.clk, 6) if meta.clk else "",
                "frames": meta.frames if meta.frames is not None else "",
                "duration_seconds": round(meta.frames * meta.clk, 6)
                if meta.frames is not None and meta.clk is not None
                else "",
                "channel_count": len(meta.channels),
                "analog_channel_count": analog_count,
                "digital_channel_count": digital_count,
                "schema_hash": meta.schema_hash if meta.channels else "",
                "schema_consistent": str(meta.schema_hash == canonical_schema).lower()
                if meta.channels
                else "false",
                "metadata_status": meta.metadata_status,
                "metadata_error": meta.metadata_error,
                "time_gap_from_previous_seconds": ""
                if gap_seconds is None
                else round(gap_seconds, 6),
                "time_continuity": continuity,
                "parse_status": "pending",
                "parse_error": "",
            }
        )

    print("[2/5] Building channel dictionary")
    channel_occurrences: Counter[tuple[str, bool]] = Counter()
    channel_index_examples: dict[tuple[str, bool], int] = {}
    for meta in metadata:
        for channel in meta.channels:
            key = (channel.name, channel.digital)
            channel_occurrences[key] += 1
            channel_index_examples.setdefault(key, channel.index)
    core_names = set(CORE_CHANNELS.values())
    channel_rows: list[dict[str, Any]] = []
    for (name, digital), occurrence_count in sorted(
        channel_occurrences.items(), key=lambda item: channel_index_examples[item[0]]
    ):
        channel_rows.append(
            {
                "channel_index_example": channel_index_examples[(name, digital)],
                "channel_name": name,
                "channel_type": "digital" if digital else "analog",
                "source_module": _source_module(name),
                "category": _channel_category(name),
                "role": _channel_role(name, digital),
                "unit_hint": _unit_hint(name, digital),
                "sampled_as_core_1hz": str(name in core_names).lower(),
                "file_occurrence_count": occurrence_count,
                "file_coverage_fraction": round(occurrence_count / len(metadata), 6),
                "notes": "PLC point table required" if "raw_register" in _channel_role(name, digital) else "",
            }
        )

    print("[3/5] Streaming core-channel decode and 1 Hz sampling")
    points: list[SamplePoint] = []
    quality_rows: list[dict[str, Any]] = []
    for file_index, (meta, file_row) in enumerate(zip(metadata, file_rows)):
        quality = {
            "filename": meta.filename,
            "start_time": _format_dt(meta.start_dt),
            "end_time": _format_dt(meta.end_dt),
            "metadata_status": meta.metadata_status,
            "parse_status": "error",
            "core_channel_count_decoded": 0,
            "missing_core_channels": "",
            "length_mismatch_channels": "",
            "digital_length_mismatch_channels": "",
            "nonfinite_core_channels": "",
            "sample_points_1hz": 0,
            "raw_active_seconds_1hz": 0,
            "usable_active_seconds_1hz": 0,
            "suspected_frozen_seconds_1hz": 0,
            "suspected_frozen_fraction": 0.0,
            "suspected_frozen": "false",
            "time_continuity": file_row["time_continuity"],
            "time_gap_from_previous_seconds": file_row[
                "time_gap_from_previous_seconds"
            ],
            "schema_consistent": file_row["schema_consistent"],
            "size_bytes": meta.size_bytes,
            "quality_flags": "",
            "parse_error": "",
        }
        try:
            result = parse_dat(str(meta.path), list(CORE_CHANNELS.values()))
            frames = int(result["frames"])
            clk = float(result["clk"])
            start_dt = result["start_dt"] or meta.start_dt
            if start_dt is None:
                raise ValueError("start datetime unavailable")
            decoded = result["data"]
            decoded_by_alias = {
                alias: decoded.get(channel_name, [])
                for alias, channel_name in CORE_CHANNELS.items()
            }
            missing_aliases = [
                alias for alias, values in decoded_by_alias.items() if not values
            ]
            mismatch_aliases = [
                alias
                for alias, values in decoded_by_alias.items()
                if values and len(values) != frames
            ]
            digital_mismatch_aliases = [
                alias for alias in mismatch_aliases if alias in DIGITAL_ALIASES
            ]
            nonfinite_aliases = []
            for alias, values in decoded_by_alias.items():
                if alias in DIGITAL_ALIASES or not values:
                    continue
                if any(_safe_float(value) is None for value in values):
                    nonfinite_aliases.append(alias)

            step = max(1, int(round(1.0 / clk)))
            file_points: list[SamplePoint] = []
            for frame_index in range(0, frames, step):
                values = {
                    alias: _sample_value(channel_values, frame_index)
                    for alias, channel_values in decoded_by_alias.items()
                }
                file_points.append(
                    SamplePoint(
                        timestamp=start_dt + timedelta(seconds=frame_index * clk),
                        file_index=file_index,
                        filename=meta.filename,
                        values=values,
                    )
                )
            points.extend(file_points)
            raw_active_seconds = sum(
                abs(_safe_float(point.values.get("line_speed_actual")) or 0)
                > RUN_SPEED_THRESHOLD
                for point in file_points
            )
            quality.update(
                {
                    "parse_status": "ok",
                    "core_channel_count_decoded": sum(
                        bool(values) for values in decoded_by_alias.values()
                    ),
                    "missing_core_channels": ";".join(missing_aliases),
                    "length_mismatch_channels": ";".join(mismatch_aliases),
                    "digital_length_mismatch_channels": ";".join(
                        digital_mismatch_aliases
                    ),
                    "nonfinite_core_channels": ";".join(nonfinite_aliases),
                    "sample_points_1hz": len(file_points),
                    "raw_active_seconds_1hz": raw_active_seconds,
                }
            )
            file_row["parse_status"] = "ok"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            quality["parse_error"] = error
            file_row["parse_status"] = "error"
            file_row["parse_error"] = error
        quality_rows.append(quality)
        if (file_index + 1) % 24 == 0 or file_index + 1 == len(metadata):
            print(f"  decoded {file_index + 1}/{len(metadata)} files")

    points.sort(key=lambda point: point.timestamp)
    print("[4/5] Detecting suspected freezes and production candidates")
    freeze_intervals = mark_suspected_freezes(points, FREEZE_MIN_SECONDS)
    frozen_by_file: Counter[int] = Counter()
    usable_active_by_file: Counter[int] = Counter()
    for point in points:
        if point.suspected_frozen:
            frozen_by_file[point.file_index] += 1
        if _is_active(point):
            usable_active_by_file[point.file_index] += 1
    for file_index, quality in enumerate(quality_rows):
        sample_count = int(quality["sample_points_1hz"] or 0)
        frozen = frozen_by_file[file_index]
        quality["suspected_frozen_seconds_1hz"] = frozen
        quality["suspected_frozen_fraction"] = round(
            frozen / sample_count, 6
        ) if sample_count else 0.0
        quality["suspected_frozen"] = str(frozen > 0).lower()
        quality["usable_active_seconds_1hz"] = usable_active_by_file[file_index]
        flags = []
        if quality["metadata_status"] != "ok":
            flags.append("metadata_error")
        if quality["parse_status"] != "ok":
            flags.append("parse_error")
        if quality["time_continuity"] in {"gap", "overlap"}:
            flags.append(f"time_{quality['time_continuity']}")
        if quality["schema_consistent"] != "true":
            flags.append("schema_change")
        if quality["missing_core_channels"]:
            flags.append("missing_core_channel")
        if quality["length_mismatch_channels"]:
            flags.append("core_length_mismatch")
        if quality["nonfinite_core_channels"]:
            flags.append("nonfinite_core_value")
        if frozen:
            flags.append("suspected_frozen_or_held")
        quality["quality_flags"] = ";".join(flags)

    segment_rows = build_candidate_segments(points)

    print("[5/5] Writing artifacts")
    file_fields = [
        "filename",
        "file_number",
        "path",
        "size_bytes",
        "start_time",
        "end_time",
        "clk_seconds",
        "sample_rate_hz",
        "frames",
        "duration_seconds",
        "channel_count",
        "analog_channel_count",
        "digital_channel_count",
        "schema_hash",
        "schema_consistent",
        "metadata_status",
        "metadata_error",
        "time_gap_from_previous_seconds",
        "time_continuity",
        "parse_status",
        "parse_error",
    ]
    channel_fields = [
        "channel_index_example",
        "channel_name",
        "channel_type",
        "source_module",
        "category",
        "role",
        "unit_hint",
        "sampled_as_core_1hz",
        "file_occurrence_count",
        "file_coverage_fraction",
        "notes",
    ]
    quality_fields = [
        "filename",
        "start_time",
        "end_time",
        "metadata_status",
        "parse_status",
        "core_channel_count_decoded",
        "missing_core_channels",
        "length_mismatch_channels",
        "digital_length_mismatch_channels",
        "nonfinite_core_channels",
        "sample_points_1hz",
        "raw_active_seconds_1hz",
        "usable_active_seconds_1hz",
        "suspected_frozen_seconds_1hz",
        "suspected_frozen_fraction",
        "suspected_frozen",
        "time_continuity",
        "time_gap_from_previous_seconds",
        "schema_consistent",
        "size_bytes",
        "quality_flags",
        "parse_error",
    ]
    segment_fields = list(segment_rows[0].keys()) if segment_rows else [
        "candidate_id",
        "record_type",
        "is_true_coil_or_pass",
        "start_time",
        "end_time",
        "notes",
    ]
    _write_csv(output_dir / "file_index.csv", file_rows, file_fields)
    _write_csv(output_dir / "channel_dictionary.csv", channel_rows, channel_fields)
    _write_csv(output_dir / "data_quality.csv", quality_rows, quality_fields)
    _write_csv(output_dir / "production_segments.csv", segment_rows, segment_fields)
    report = build_report(
        input_dir,
        file_rows,
        quality_rows,
        channel_rows,
        points,
        freeze_intervals,
        segment_rows,
    )
    (output_dir / "data_quality_report.md").write_text(report, encoding="utf-8")

    print(f"Done: {output_dir.resolve()}")
    print(
        "Summary: "
        f"files={len(file_rows)}, channels={len(channel_rows)}, "
        f"points_1hz={len(points)}, freezes={len(freeze_intervals)}, "
        f"candidates={len(segment_rows)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
