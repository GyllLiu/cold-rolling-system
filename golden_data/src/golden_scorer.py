"""Distribution-calibrated, configurable Golden Score implementation."""

from __future__ import annotations

import statistics
from typing import Any

from .outlier_detector import finite, percentile
from .tandem_filter import is_tandem_record, tandem_exclusion_reason


CV_FIELDS = {
    "speed": "speed_cv",
    "f1_force": "f1_force_cv",
    "f2_force": "f2_force_cv",
    "entry_tension": "entry_tension_cv",
    "interstand_tension": "interstand_tension_cv",
    "exit_tension": "exit_tension_cv",
}


def _calibrated_pair(
    values: list[float], *, floor: float, cap_a: float, cap_b: float
) -> tuple[float, float]:
    if not values:
        return cap_a, cap_b
    p75 = percentile(values, 0.75) or cap_a
    p90 = percentile(values, 0.90) or cap_b
    threshold_a = min(cap_a, max(floor, p75))
    threshold_b = min(cap_b, max(threshold_a * 1.35, p90))
    return threshold_a, threshold_b


def calibrate_thresholds(
    windows: list[dict[str, Any]], config: dict[str, Any]
) -> dict[str, Any]:
    caps = config["stability_caps"]
    valid = [row for row in windows if not row.get("hard_abnormal_reasons") and is_tandem_record(row)]
    thresholds: dict[str, Any] = {}
    for family, field in CV_FIELDS.items():
        values = finite(row.get(field) for row in valid)
        if family == "speed":
            cap_a, cap_b = caps["speed_cv_a"], caps["speed_cv_b"]
            floor = 0.0005
        elif "force" in family:
            cap_a, cap_b = caps["force_cv_a"], caps["force_cv_b"]
            floor = 0.0005
        else:
            cap_a, cap_b = caps["tension_cv_a"], caps["tension_cv_b"]
            floor = 0.0001
        threshold_a, threshold_b = _calibrated_pair(
            values, floor=floor, cap_a=cap_a, cap_b=cap_b
        )
        thresholds[family] = {
            "field": field,
            "a": threshold_a,
            "b": threshold_b,
            "observed_median": percentile(values, 0.50),
            "observed_p75": percentile(values, 0.75),
            "observed_p90": percentile(values, 0.90),
            "observed_p95": percentile(values, 0.95),
        }
    p95_speeds = finite(row.get("candidate_speed_p95") for row in valid)
    thresholds["population_p10_candidate_p95_speed"] = percentile(p95_speeds, 0.10) or 50.0
    thresholds["calibration_window_count"] = len(valid)
    return thresholds


def _stability_fraction(value: Any, threshold_a: float, threshold_b: float) -> float:
    values = finite([value])
    if not values:
        return 0.0
    number = values[0]
    if number <= threshold_a:
        return 1.0
    if number <= threshold_b:
        position = (number - threshold_a) / max(threshold_b - threshold_a, 1e-12)
        return 1.0 - 0.30 * position
    if number >= 2 * threshold_b:
        return 0.0
    return 0.70 * (2 * threshold_b - number) / threshold_b


def score_window(
    row: dict[str, Any], thresholds: dict[str, Any], config: dict[str, Any]
) -> dict[str, Any]:
    row = dict(row)
    weights = config["score_weights"]
    parts: dict[str, float] = {}
    population_floor = thresholds["population_p10_candidate_p95_speed"]
    candidate_speed = row.get("candidate_speed_p95") or 0.0
    if candidate_speed >= population_floor:
        speed_level_fraction = 1.0
    elif candidate_speed >= 0.8 * population_floor:
        speed_level_fraction = 0.7
    else:
        speed_level_fraction = 0.3
    parts["speed_level"] = weights["speed_level"] * speed_level_fraction
    mapping = {
        "speed_stability": "speed",
        "f1_force_stability": "f1_force",
        "f2_force_stability": "f2_force",
        "entry_tension_stability": "entry_tension",
        "interstand_tension_stability": "interstand_tension",
        "exit_tension_stability": "exit_tension",
    }
    for score_name, family in mapping.items():
        definition = thresholds[family]
        parts[score_name] = weights[score_name] * _stability_fraction(
            row.get(definition["field"]), definition["a"], definition["b"]
        )
    duration_fraction = min(1.0, (row.get("stable_duration") or 0) / config["minimum_stable_duration_seconds"])
    parts["duration"] = weights["duration"] * duration_fraction
    parts["data_completeness"] = weights["data_completeness"] * (row.get("data_completeness") or 0)
    changes = int(row.get("setpoint_change_count") or 0)
    quietness = 1.0 if changes == 0 else 0.7 if changes == 1 else 0.35 if changes <= 3 else 0.0
    parts["setpoint_quietness"] = weights["setpoint_quietness"] * quietness
    alarm_free = not row.get("hard_alarm_count") and not row.get("slowdown_command_count")
    if row.get("lubrication_status_available") and (row.get("lubrication_status_ok_ratio") or 0) < 0.99:
        alarm_free = False
    parts["alarm_free"] = weights["alarm_free"] * float(alarm_free)
    target = row.get("target_exit_thickness")
    error = row.get("exit_thickness_error")
    if target and error is not None:
        relative = abs(error) / target
        thickness_fraction = 1.0 if relative <= 0.03 else 0.7 if relative <= 0.05 else 0.2 if relative <= 0.10 else 0.0
    else:
        thickness_fraction = 0.4
    parts["thickness_quality"] = weights["thickness_quality"] * thickness_fraction
    score = sum(parts.values())
    row["golden_score"] = round(score, 3)
    row["score_components"] = ";".join(f"{name}={value:.2f}" for name, value in parts.items())
    stability_values = []
    for definition in (
        thresholds["speed"], thresholds["f1_force"], thresholds["f2_force"],
        thresholds["entry_tension"], thresholds["interstand_tension"], thresholds["exit_tension"],
    ):
        values = finite([row.get(definition["field"])])
        stability_values.append((values[0] if values else None, definition))
    a_hits = sum(value is not None and value <= definition["a"] for value, definition in stability_values)
    b_hits = sum(value is not None and value <= definition["b"] for value, definition in stability_values)
    row["stability_a_hit_count"] = a_hits
    row["stability_b_hit_count"] = b_hits
    if row.get("hard_abnormal_reasons"):
        level = "ABNORMAL"
        abnormal_reason = row["hard_abnormal_reasons"]
    elif score >= config["score_thresholds"]["gold_a"] and a_hits >= 5 and b_hits == 6:
        level = "GOLD_A"
        abnormal_reason = ""
    elif score >= config["score_thresholds"]["gold_b"] and b_hits >= 5:
        level = "GOLD_B"
        abnormal_reason = ""
    else:
        level = "NORMAL"
        weak = []
        for family, definition in thresholds.items():
            if not isinstance(definition, dict) or "field" not in definition:
                continue
            value = row.get(definition["field"])
            if value is None or value > definition["b"]:
                weak.append(f"unstable_{family}")
        abnormal_reason = ";".join(weak) or "golden_score_below_threshold"
    limits = config["physical_limits"]
    if level in {"GOLD_A", "GOLD_B"} and (
        (row.get("f1_force_actual_median") or 0) > limits["rolling_force_reference_max_t"]
        or (row.get("f2_force_actual_median") or 0) > limits["rolling_force_reference_max_t"]
    ):
        level = "NORMAL"
        abnormal_reason = "rolling_force_above_reference_limit"
    row["golden_level"] = level
    row["abnormal_reason"] = abnormal_reason
    exclusion = tandem_exclusion_reason(row)
    if exclusion:
        row["golden_level"] = "EXCLUDED"
        row["abnormal_reason"] = exclusion
        row["tandem_exclusion_reason"] = exclusion
    return row


def merge_adjacent_gold(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge neighboring 30 s windows so one stable plateau becomes one sample."""
    gold = sorted(
        (row for row in rows if row.get("golden_level") in {"GOLD_A", "GOLD_B"} and is_tandem_record(row)),
        key=lambda row: (row.get("dataset", ""), row.get("candidate_id", ""), row["stable_segment_start"]),
    )
    groups: list[list[dict[str, Any]]] = []
    for row in gold:
        if not groups:
            groups.append([row])
            continue
        previous = groups[-1][-1]
        same = (
            previous.get("dataset") == row.get("dataset")
            and previous.get("candidate_id") == row.get("candidate_id")
        )
        previous_end = previous["stable_segment_end"]
        gap = (
            __import__("datetime").datetime.fromisoformat(row["stable_segment_start"])
            - __import__("datetime").datetime.fromisoformat(previous_end)
        ).total_seconds()
        if same and gap <= 2:
            groups[-1].append(row)
        else:
            groups.append([row])

    output: list[dict[str, Any]] = []
    for group in groups:
        base = dict(group[0])
        base["stable_segment_start"] = group[0]["stable_segment_start"]
        base["stable_segment_end"] = group[-1]["stable_segment_end"]
        a_count = sum(row.get("golden_level") == "GOLD_A" for row in group)
        # A short B fluctuation should not fragment a long plateau.  The merged
        # stage remains A only when at least 80% of its windows were A.
        base["golden_level"] = "GOLD_A" if a_count / len(group) >= 0.80 else "GOLD_B"
        base["gold_a_window_ratio"] = a_count / len(group)
        numeric_fields = set.intersection(
            *(set(key for key, value in row.items() if isinstance(value, (int, float)) and not isinstance(value, bool)) for row in group)
        )
        for field in numeric_fields:
            values = finite(row.get(field) for row in group)
            if values:
                base[field] = statistics.median(values)
        base["stable_duration"] = (
            __import__("datetime").datetime.fromisoformat(base["stable_segment_end"])
            - __import__("datetime").datetime.fromisoformat(base["stable_segment_start"])
        ).total_seconds()
        base["merged_window_count"] = len(group)
        base["golden_score"] = round(statistics.fmean(float(row["golden_score"]) for row in group), 3)
        output.append(base)
    for index, row in enumerate(output, 1):
        row["golden_segment_id"] = f"GOLD-{index:05d}"
    return output
