"""Window-level robust summaries and engineering features."""

from __future__ import annotations

import math
import statistics
from datetime import datetime
from typing import Any

from .column_mapper import HARD_ALARM_ALIASES, LUBRICATION_STATUS_ALIASES
from .outlier_detector import cv, finite, hampel_spike_count, mad, percentile


NUMERIC_SIGNALS = (
    "line_speed_actual",
    "f1_force_actual",
    "f2_force_actual",
    "entry_tension_actual",
    "interstand_tension_actual",
    "exit_tension_actual",
    "entry_tension_set",
    "interstand_tension_set",
    "exit_tension_set",
    "entry_thickness_set",
    "entry_thickness_actual",
    "intermediate_thickness_set",
    "intermediate_thickness_actual",
    "exit_thickness_set",
    "exit_thickness_actual",
    "width_set",
    "f1_gap_set",
    "f2_gap_set",
)


def _values(points: list[dict[str, Any]], alias: str) -> list[Any]:
    return [point.get(alias) for point in points]


def _median(points: list[dict[str, Any]], alias: str) -> float | None:
    values = finite(_values(points, alias))
    return statistics.median(values) if values else None


def _changes(values: list[Any], tolerance: float) -> int:
    numbers = finite(values)
    if len(numbers) < 2:
        return 0
    return sum(abs(right - left) > tolerance for left, right in zip(numbers, numbers[1:]))


def _safe_ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or abs(denominator) < 1e-12:
        return None
    return numerator / denominator


def _tension_stress(tension_t: float | None, width: float | None, thickness: float | None) -> float | None:
    if tension_t is None or width is None or thickness is None or width <= 0 or thickness <= 0:
        return None
    return 9.80665 * tension_t / (width * thickness)


def summarize_window(
    points: list[dict[str, Any]],
    config: dict[str, Any],
    *,
    candidate_p95_speed: float,
    adaptive_threshold: float,
    phase: str,
) -> dict[str, Any]:
    if not points:
        raise ValueError("empty window")
    row: dict[str, Any] = {
        "stable_segment_start": points[0]["timestamp"].isoformat(timespec="seconds"),
        "stable_segment_end": points[-1]["timestamp"].isoformat(timespec="seconds"),
        "stable_duration": (points[-1]["timestamp"] - points[0]["timestamp"]).total_seconds() + 1,
        "phase": phase,
        "candidate_speed_p95": candidate_p95_speed,
        "adaptive_speed_threshold": adaptive_threshold,
        "sample_count": len(points),
    }
    for alias in NUMERIC_SIGNALS:
        values = _values(points, alias)
        row[f"{alias}_median"] = _median(points, alias)
        if alias in {
            "line_speed_actual",
            "f1_force_actual",
            "f2_force_actual",
            "entry_tension_actual",
            "interstand_tension_actual",
            "exit_tension_actual",
        }:
            row[f"{alias}_cv"] = cv(values)
            row[f"{alias}_mad"] = mad(values)
            row[f"{alias}_p10"] = percentile(values, 0.10)
            row[f"{alias}_p90"] = percentile(values, 0.90)
            row[f"{alias}_spike_count"] = hampel_spike_count(values)

    row["speed_median"] = row["line_speed_actual_median"]
    row["speed_p95"] = percentile(_values(points, "line_speed_actual"), 0.95)
    row["speed_cv"] = row["line_speed_actual_cv"]
    row["f1_force_actual_median"] = row["f1_force_actual_median"]
    row["f2_force_actual_median"] = row["f2_force_actual_median"]
    row["f1_force_cv"] = row["f1_force_actual_cv"]
    row["f2_force_cv"] = row["f2_force_actual_cv"]
    row["entry_tension_cv"] = row["entry_tension_actual_cv"]
    row["interstand_tension_cv"] = row["interstand_tension_actual_cv"]
    row["exit_tension_cv"] = row["exit_tension_actual_cv"]

    tolerances = config["setpoint_change_tolerance"]
    row["setpoint_change_count"] = sum(
        (
            _changes(_values(points, "entry_thickness_set"), tolerances["thickness_mm"]),
            _changes(_values(points, "intermediate_thickness_set"), tolerances["thickness_mm"]),
            _changes(_values(points, "exit_thickness_set"), tolerances["thickness_mm"]),
            _changes(_values(points, "entry_tension_set"), tolerances["tension_t"]),
            _changes(_values(points, "interstand_tension_set"), tolerances["tension_t"]),
            _changes(_values(points, "exit_tension_set"), tolerances["tension_t"]),
            _changes(_values(points, "f1_gap_set"), tolerances["gap_mm"]),
            _changes(_values(points, "f2_gap_set"), tolerances["gap_mm"]),
        )
    )
    row["hard_alarm_count"] = sum(
        bool(point.get(alias)) for point in points for alias in HARD_ALARM_ALIASES
    )
    row["slowdown_command_count"] = sum(
        bool(point.get(alias))
        for point in points
        for alias in ("current_slowdown", "diameter_slowdown")
    )
    lubricant_values = [
        point.get(alias)
        for point in points
        for alias in LUBRICATION_STATUS_ALIASES
        if isinstance(point.get(alias), bool)
    ]
    row["lubrication_status_available"] = bool(lubricant_values)
    row["lubrication_status_ok_ratio"] = (
        sum(lubricant_values) / len(lubricant_values) if lubricant_values else None
    )

    limits = config["physical_limits"]
    reasons: list[str] = []
    entry = row["entry_thickness_set_median"]
    intermediate = row["intermediate_thickness_set_median"]
    exit_value = row["exit_thickness_set_median"]
    width = row["width_set_median"]
    for label, value in (("entry", entry), ("intermediate", intermediate), ("exit", exit_value)):
        if value is None or value <= 0:
            reasons.append(f"missing_or_nonpositive_{label}_thickness")
        elif not limits["minimum_thickness_mm"] <= value <= limits["maximum_thickness_mm"]:
            reasons.append(f"{label}_thickness_outside_physical_range")
    if width is None or not limits["minimum_width_mm"] <= width <= limits["maximum_width_mm"]:
        reasons.append("width_outside_physical_range")
    for alias in ("f1_force_actual_median", "f2_force_actual_median"):
        value = row.get(alias)
        if value is None or value < 0:
            reasons.append(f"invalid_{alias}")
        elif value > limits["rolling_force_hard_max_t"]:
            reasons.append(f"{alias}_above_hard_limit")
    for alias in (
        "entry_tension_actual_median",
        "interstand_tension_actual_median",
        "exit_tension_actual_median",
    ):
        value = row.get(alias)
        if value is None or value < 0:
            reasons.append(f"invalid_{alias}")
        elif value > limits["tension_hard_max_t"]:
            reasons.append(f"{alias}_above_hard_limit")
    if row["hard_alarm_count"]:
        reasons.append("hard_alarm_present")
    if row["slowdown_command_count"]:
        reasons.append("slowdown_command_present")
    if row["lubrication_status_available"] and (row["lubrication_status_ok_ratio"] or 0) < 0.99:
        reasons.append("lubrication_status_not_ok")
    row["hard_abnormal_reasons"] = ";".join(sorted(set(reasons)))

    row["current_entry_thickness"] = entry
    row["f1_exit_thickness"] = (
        row["intermediate_thickness_actual_median"]
        if row["intermediate_thickness_actual_median"] and row["intermediate_thickness_actual_median"] > 0
        else intermediate
    )
    row["f2_exit_thickness"] = (
        row["exit_thickness_actual_median"]
        if row["exit_thickness_actual_median"] and row["exit_thickness_actual_median"] > 0
        else exit_value
    )
    row["target_exit_thickness"] = exit_value
    row["width"] = width
    row["reduction_f1"] = (
        (entry - intermediate) / entry if entry and intermediate and entry > 0 else None
    )
    row["reduction_f2"] = (
        (intermediate - exit_value) / intermediate
        if intermediate and exit_value and intermediate > 0
        else None
    )
    row["delta_h_f1"] = entry - intermediate if entry and intermediate else None
    row["delta_h_f2"] = intermediate - exit_value if intermediate and exit_value else None
    row["contact_length_f1_mm"] = (
        math.sqrt(190.0 * row["delta_h_f1"]) if row["delta_h_f1"] and row["delta_h_f1"] > 0 else None
    )
    row["contact_length_f2_mm"] = (
        math.sqrt(190.0 * row["delta_h_f2"]) if row["delta_h_f2"] and row["delta_h_f2"] > 0 else None
    )
    row["f1_force_per_width_t_per_mm"] = _safe_ratio(row["f1_force_actual_median"], width)
    row["f2_force_per_width_t_per_mm"] = _safe_ratio(row["f2_force_actual_median"], width)
    row["entry_tension_stress_kn_per_mm2"] = _tension_stress(
        row["entry_tension_actual_median"], width, entry
    )
    row["interstand_tension_stress_kn_per_mm2"] = _tension_stress(
        row["interstand_tension_actual_median"], width, intermediate
    )
    row["exit_tension_stress_kn_per_mm2"] = _tension_stress(
        row["exit_tension_actual_median"], width, exit_value
    )
    actual_exit = row["exit_thickness_actual_median"]
    row["exit_thickness_error"] = actual_exit - exit_value if actual_exit is not None and exit_value else None
    expected = (
        "line_speed_actual_median",
        "f1_force_actual_median",
        "f2_force_actual_median",
        "entry_tension_actual_median",
        "interstand_tension_actual_median",
        "exit_tension_actual_median",
        "entry_thickness_set_median",
        "intermediate_thickness_set_median",
        "exit_thickness_set_median",
        "width_set_median",
    )
    row["data_completeness"] = sum(row.get(name) is not None for name in expected) / len(expected)
    return row
