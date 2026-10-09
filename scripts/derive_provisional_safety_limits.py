#!/usr/bin/env python3
"""Derive provisional observed operating envelopes from stable DAT candidates.

These values are historical observed maxima, not equipment design limits.  The
script intentionally refuses to create alloy-specific limits because the DAT
files currently contain no validated alloy/pass labels.
"""

from __future__ import annotations

import csv
import json
import math
from dataclasses import dataclass
from pathlib import Path
from statistics import median


ROOT = Path(__file__).resolve().parents[2]
OUTPUT_ROOT = ROOT / "outputs"
OUT_DIR = OUTPUT_ROOT / "provisional_safety_limits"


@dataclass(frozen=True)
class Sample:
    value: float
    dataset: str
    candidate_id: str
    start_time: str
    source_files: str


METRICS = {
    "f1_total_roll_force_t": ("f1_roll_force_sum_median", 1.0, 0.0, 5000.0),
    "f2_total_roll_force_t": ("f2_roll_force_sum_median", 1.0, 0.0, 5000.0),
    "uncoiler_tension_t": ("uncoiler_tension_set_median", 1.0, 0.0, 50.0),
    "interstand_tension_t": ("interstand_tension_set_median", 1.0, 0.0, 50.0),
    "coiler_tension_t": ("coiler_tension_set_median", 1.0, 0.0, 50.0),
    "f1_observed_closed_gap_mm": ("tm1_gap_command_median", 0.001, 0.0, 30.0),
    "f2_observed_closed_gap_mm": ("tm2_gap_command_median", 0.001, 0.0, 30.0),
    "f1_reduction_ratio": ("f1_reduction_ratio", 1.0, 0.0, 0.8),
    "f2_reduction_ratio": ("f2_reduction_ratio", 1.0, 0.0, 0.8),
}


def as_float(value: str | None) -> float | None:
    try:
        number = float(value or "")
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


def percentile(values: list[float], probability: float) -> float:
    if not values:
        raise ValueError("empty values")
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def valid_candidate(row: dict[str, str]) -> bool:
    stable_seconds = as_float(row.get("stable_high_speed_seconds_1hz"))
    entry = as_float(row.get("entry_thickness_set_mode"))
    middle = as_float(row.get("intermediate_thickness_set_mode"))
    exit_thickness = as_float(row.get("exit_thickness_set_mode"))
    width = as_float(row.get("width_set_mode"))
    return bool(
        truthy(row.get("learning_eligible"))
        and truthy(row.get("digital_state_usable"))
        and stable_seconds is not None
        and stable_seconds >= 120
        and entry is not None
        and middle is not None
        and exit_thickness is not None
        and 0 < exit_thickness < middle < entry <= 30
        and width is not None
        and 500 <= width <= 2000
    )


def main() -> int:
    files = sorted(OUTPUT_ROOT.glob("data_foundation*/production_segments.csv"))
    samples: dict[str, list[Sample]] = {name: [] for name in METRICS}
    tandem_true_strains: list[float] = []
    f1_true_strain_shares: list[float] = []
    accepted = 0
    rows_seen = 0

    for path in files:
        dataset = path.parent.name.removeprefix("data_foundation_")
        if dataset == "data_foundation":
            dataset = "legacy"
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                rows_seen += 1
                if not valid_candidate(row):
                    continue
                accepted += 1
                entry = as_float(row.get("entry_thickness_set_mode"))
                middle = as_float(row.get("intermediate_thickness_set_mode"))
                exit_thickness = as_float(row.get("exit_thickness_set_mode"))
                if entry and middle and exit_thickness:
                    total_strain = math.log(entry / exit_thickness)
                    tandem_true_strains.append(total_strain)
                    f1_true_strain_shares.append(
                        math.log(entry / middle) / total_strain
                    )
                for name, (column, scale, lower, upper) in METRICS.items():
                    raw = as_float(row.get(column))
                    if raw is None:
                        continue
                    value = raw * scale
                    if not lower < value <= upper:
                        continue
                    samples[name].append(
                        Sample(
                            value=value,
                            dataset=dataset,
                            candidate_id=row.get("candidate_id", ""),
                            start_time=row.get("start_time", ""),
                            source_files=row.get("source_files", ""),
                        )
                    )

    metrics: dict[str, object] = {}
    for name, metric_samples in samples.items():
        values = [sample.value for sample in metric_samples]
        if not values:
            metrics[name] = {"count": 0}
            continue
        maximum = max(metric_samples, key=lambda sample: sample.value)
        metrics[name] = {
            "count": len(values),
            "median": round(median(values), 6),
            "p95": round(percentile(values, 0.95), 6),
            "p99": round(percentile(values, 0.99), 6),
            "max_effective_observed": round(maximum.value, 6),
            "max_source": {
                "dataset": maximum.dataset,
                "candidate_id": maximum.candidate_id,
                "start_time": maximum.start_time,
                "source_files": maximum.source_files,
            },
        }

    result = {
        "status": "provisional_observed_envelope_only",
        "source_files": [str(path.relative_to(ROOT)) for path in files],
        "rows_seen": rows_seen,
        "accepted_stable_candidates": accepted,
        "filter": {
            "learning_eligible": True,
            "digital_state_usable": True,
            "stable_high_speed_seconds_min": 120,
            "thickness_relation": "0 < exit < intermediate < entry <= 30 mm",
            "width_range_mm": [500, 2000],
        },
        "metrics": metrics,
        "route_shadow_defaults": {
            "maximum_true_strain_per_tandem_pass": round(
                max(tandem_true_strains), 10
            ),
            "target_true_strain_per_tandem_pass": round(
                median(tandem_true_strains), 10
            ),
            "f1_true_strain_share": round(median(f1_true_strain_shares), 10),
            "source": "accepted high-speed stable candidates; shadow planning only",
        },
        "alloy_specific_reduction_limits": {
            "status": "unavailable_without_alloy_labels",
            "supported_alloys": ["1060", "1100", "3003"],
        },
        "limitations": [
            "Observed maxima are not equipment nameplate or alarm limits.",
            "Gap recommendations use high-speed stable values as manual setup references; no PLC write-back is assumed.",
            "DAT currently has no validated alloy/pass labels, so per-alloy maxima cannot be inferred.",
            "Candidate segments are heuristic and still require operator/coil validation.",
        ],
    }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "provisional_limits.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
