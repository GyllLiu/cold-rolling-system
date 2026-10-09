"""Adaptive high-speed candidate and contiguous stable-window detection."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .feature_engineering import summarize_window
from .outlier_detector import finite, percentile


def _speed(point: dict[str, Any]) -> float | None:
    values = finite([point.get("line_speed_actual")])
    return abs(values[0]) if values else None


def _contiguous_runs(
    points: list[dict[str, Any]], predicate, maximum_gap: float
) -> list[list[dict[str, Any]]]:
    runs: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for point in points:
        accepted = predicate(point)
        gap = (
            (point["timestamp"] - previous["timestamp"]).total_seconds()
            if previous is not None
            else 0
        )
        if accepted and (not current or gap <= maximum_gap):
            current.append(point)
        elif accepted:
            if current:
                runs.append(current)
            current = [point]
        else:
            if current:
                runs.append(current)
                current = []
        previous = point
    if current:
        runs.append(current)
    return runs


def _windows(
    run: list[dict[str, Any]], duration: int, step: int
) -> list[list[dict[str, Any]]]:
    output: list[list[dict[str, Any]]] = []
    if not run:
        return output
    start_index = 0
    while start_index < len(run):
        start_time = run[start_index]["timestamp"]
        end_time = start_time.timestamp() + duration
        window = [
            point
            for point in run[start_index:]
            if point["timestamp"].timestamp() < end_time
        ]
        if window and (window[-1]["timestamp"] - window[0]["timestamp"]).total_seconds() + 1 >= duration:
            output.append(window)
        target = start_time.timestamp() + step
        next_indices = [
            index
            for index in range(start_index + 1, len(run))
            if run[index]["timestamp"].timestamp() >= target
        ]
        if not next_indices:
            break
        start_index = next_indices[0]
    return output


def detect_windows(
    candidate_points: list[dict[str, Any]], config: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    points = sorted(candidate_points, key=lambda point: point["timestamp"])
    active = [
        point
        for point in points
        if (_speed(point) or 0) > config["run_speed_min_m_per_min"]
    ]
    speeds = [_speed(point) for point in active]
    speeds = [value for value in speeds if value is not None]
    if not speeds:
        return [], [], {"reason": "no_active_speed"}
    p95 = percentile(speeds, 0.95) or 0.0
    threshold = max(
        config["adaptive_speed_absolute_floor_m_per_min"],
        config["adaptive_speed_ratio_of_p95"] * p95,
    )
    max_slope = config["maximum_abs_speed_slope_m_per_min_per_second"]
    previous_speed: dict[datetime, float] = {}
    last_speed: float | None = None
    last_time: datetime | None = None
    for point in points:
        speed = _speed(point)
        slope = 0.0
        if speed is not None and last_speed is not None and last_time is not None:
            seconds = (point["timestamp"] - last_time).total_seconds()
            if seconds > 0:
                slope = abs(speed - last_speed) / seconds
        point["_speed_slope"] = slope
        if speed is not None:
            last_speed = speed
            last_time = point["timestamp"]

    high_predicate = lambda point: (
        (_speed(point) or 0) >= threshold
        and point.get("_speed_slope", 0) <= max_slope
    )
    low_predicate = lambda point: (
        config["run_speed_min_m_per_min"] < (_speed(point) or 0) < threshold
    )
    high_runs = _contiguous_runs(points, high_predicate, config["maximum_gap_seconds"])
    low_runs = _contiguous_runs(points, low_predicate, config["maximum_gap_seconds"])
    trimmed_high: list[list[dict[str, Any]]] = []
    for run in high_runs:
        if not run:
            continue
        first = run[0]["timestamp"].timestamp() + config["head_trim_seconds"]
        last = run[-1]["timestamp"].timestamp() - config["tail_trim_seconds"]
        trimmed = [point for point in run if first <= point["timestamp"].timestamp() <= last]
        if trimmed:
            trimmed_high.append(trimmed)
    high_summaries = [
        summarize_window(window, config, candidate_p95_speed=p95, adaptive_threshold=threshold, phase="adaptive_high_speed")
        for run in trimmed_high
        for window in _windows(run, config["window_seconds"], config["window_step_seconds"])
    ]
    low_summaries = [
        summarize_window(window, config, candidate_p95_speed=p95, adaptive_threshold=threshold, phase="low_speed_tuning")
        for run in low_runs
        for window in _windows(run, config["window_seconds"], config["window_step_seconds"])
    ]
    fixed_seconds = sum((_speed(point) or 0) >= config["fixed_speed_comparison_m_per_min"] for point in active)
    metadata = {
        "candidate_p95_speed": p95,
        "adaptive_threshold": threshold,
        "active_seconds": len(active),
        "adaptive_high_speed_seconds": sum(high_predicate(point) for point in points),
        "fixed_high_speed_seconds": fixed_seconds,
        "high_speed_ratio": sum(high_predicate(point) for point in points) / len(active),
    }
    return high_summaries, low_summaries, metadata
