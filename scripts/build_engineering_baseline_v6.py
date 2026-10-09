#!/usr/bin/env python3
"""Build provisional engineering envelopes from high-speed stable DAT segments.

This artifact is deliberately an observed operating envelope, not a nameplate or
legal safety limit.  It combines the governed 555-case setup table with the
enriched side-force tables and reserves 260805 as a temporal audit day.
"""

from __future__ import annotations

import csv
import json
import math
import random
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
OUTPUT = WORKSPACE_ROOT / "outputs" / "v6_high_speed" / "engineering_baseline_v6.json"
APP_OUTPUT = PROJECT_ROOT / "backend" / "app" / "model_artifacts" / "engineering_baseline_v6.json"

PRODUCTION_FILES = sorted(WORKSPACE_ROOT.glob("outputs/data_foundation*/production_segments.csv"))
ENRICHED_FILES = (
    WORKSPACE_ROOT / "outputs/model_v4/segment_features.csv",
    WORKSPACE_ROOT / "outputs/process_framework_v1/new_segment_features.csv",
    WORKSPACE_ROOT / "outputs/process_framework_v1/new_segment_features_late.csv",
)


def number(row: dict[str, str], key: str) -> float:
    value = float(row[key])
    if not math.isfinite(value):
        raise ValueError(key)
    return value


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


def percentile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("empty values")
    position = (len(ordered) - 1) * probability
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def bootstrap_ci(values: Sequence[float], probability: float, seed: int = 20260821) -> list[float]:
    rng = random.Random(seed)
    estimates = []
    for _ in range(1000):
        sample = [values[rng.randrange(len(values))] for _ in values]
        estimates.append(percentile(sample, probability))
    return [round(percentile(estimates, 0.025), 6), round(percentile(estimates, 0.975), 6)]


def envelope(values: Sequence[float], *, include_zero: bool = False) -> dict[str, object]:
    clean = [value for value in values if math.isfinite(value) and (include_zero or value > 0)]
    return {
        "count": len(clean),
        "p01": round(percentile(clean, 0.01), 6),
        "p05": round(percentile(clean, 0.05), 6),
        "median": round(statistics.median(clean), 6),
        "p95": round(percentile(clean, 0.95), 6),
        "p95_bootstrap_95ci": bootstrap_ci(clean, 0.95),
        "p99": round(percentile(clean, 0.99), 6),
        "max_observed": round(max(clean), 6),
        "classification": {
            "normal": "P05—P95 historical high-speed operating band",
            "warning": "P01—P05 or P95—P99 historical edge band",
            "outside_history": "below P01 or above P99",
        },
    }


def production_day(path: Path) -> str:
    suffix = path.parent.name.removeprefix("data_foundation_")
    return "260703" if suffix == "data_foundation" else suffix


def eligible(row: dict[str, str]) -> bool:
    try:
        entry = number(row, "entry_thickness_set_mode")
        middle = number(row, "intermediate_thickness_set_mode")
        exit_value = number(row, "exit_thickness_set_mode")
        width = number(row, "width_set_mode")
        stable = number(row, "stable_high_speed_seconds_1hz")
    except (KeyError, TypeError, ValueError):
        return False
    return (
        truthy(row.get("learning_eligible"))
        and truthy(row.get("digital_state_usable"))
        and stable >= 120
        and 0 < exit_value < middle < entry <= 30
        and 500 <= width <= 2000
    )


def load_production() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for path in PRODUCTION_FILES:
        day = production_day(path)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                if not eligible(raw):
                    continue
                entry = number(raw, "entry_thickness_set_mode")
                middle = number(raw, "intermediate_thickness_set_mode")
                exit_value = number(raw, "exit_thickness_set_mode")
                rows.append({
                    "day": day,
                    "entry": entry,
                    "middle": middle,
                    "exit": exit_value,
                    "width": number(raw, "width_set_mode"),
                    "f1_reduction": 1 - middle / entry,
                    "f2_reduction": 1 - exit_value / middle,
                    "total_reduction": 1 - exit_value / entry,
                    "true_strain": math.log(entry / exit_value),
                    "f1_true_share": math.log(entry / middle) / math.log(entry / exit_value),
                    "f1_thickness_share": (entry - middle) / (entry - exit_value),
                    "uncoiler": number(raw, "uncoiler_tension_set_median"),
                    "interstand": number(raw, "interstand_tension_set_median"),
                    "coiler": number(raw, "coiler_tension_set_median"),
                    "uncoiler_unit": 9.80665 * number(raw, "uncoiler_tension_set_median") / (
                        number(raw, "width_set_mode") * entry
                    ),
                    "interstand_unit": 9.80665 * number(raw, "interstand_tension_set_median") / (
                        number(raw, "width_set_mode") * middle
                    ),
                    "coiler_unit": 9.80665 * number(raw, "coiler_tension_set_median") / (
                        number(raw, "width_set_mode") * exit_value
                    ),
                    "f1_total_force": number(raw, "f1_roll_force_sum_median"),
                    "f2_total_force": number(raw, "f2_roll_force_sum_median"),
                    "f1_gap": number(raw, "tm1_gap_command_median") * 0.001,
                    "f2_gap": number(raw, "tm2_gap_command_median") * 0.001,
                })
    return rows


def load_enriched() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    seen: set[tuple[str, str, str]] = set()
    for path in ENRICHED_FILES:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                if not eligible(raw):
                    continue
                key = (raw.get("dataset", ""), raw.get("candidate_id", ""), raw.get("start_time", ""))
                if key in seen:
                    continue
                seen.add(key)
                try:
                    values = {
                        "day": raw["dataset"],
                        "f1_operation": number(raw, "f1_force_operation_median"),
                        "f1_drive": number(raw, "f1_force_drive_median"),
                        "f2_operation": number(raw, "f2_force_operation_median"),
                        "f2_drive": number(raw, "f2_force_drive_median"),
                    }
                except (KeyError, TypeError, ValueError):
                    continue
                f1_sum = values["f1_operation"] + values["f1_drive"]
                f2_sum = values["f2_operation"] + values["f2_drive"]
                if f1_sum <= 0 or f2_sum <= 0:
                    continue
                values["f1_operation_share"] = values["f1_operation"] / f1_sum
                values["f2_operation_share"] = values["f2_operation"] / f2_sum
                values["f1_imbalance_ratio"] = abs(values["f1_drive"] - values["f1_operation"]) / f1_sum
                values["f2_imbalance_ratio"] = abs(values["f2_drive"] - values["f2_operation"]) / f2_sum
                rows.append(values)
    return rows


def values(rows: Iterable[dict[str, object]], key: str) -> list[float]:
    return [float(row[key]) for row in rows]


def pass_bands(rows: Sequence[dict[str, object]]) -> list[dict[str, object]]:
    bands = ((0.0, 0.4), (0.4, 0.55), (0.55, 0.7), (0.7, 1.0))
    output = []
    for low, high in bands:
        group = [row for row in rows if low <= float(row["total_reduction"]) < high]
        if len(group) < 10:
            continue
        output.append({
            "total_reduction_band": [low, high],
            "count": len(group),
            "f1_true_strain_share_p10": round(percentile(values(group, "f1_true_share"), 0.1), 6),
            "f1_true_strain_share_median": round(statistics.median(values(group, "f1_true_share")), 6),
            "f1_true_strain_share_p90": round(percentile(values(group, "f1_true_share"), 0.9), 6),
        })
    return output


def main() -> None:
    production = load_production()
    enriched = load_enriched()
    if len(production) < 500 or len(enriched) < 400:
        raise RuntimeError(f"insufficient governed data: production={len(production)}, enriched={len(enriched)}")

    metric_map = {
        "f1_reduction_ratio": "f1_reduction",
        "f2_reduction_ratio": "f2_reduction",
        "total_reduction_ratio": "total_reduction",
        "true_strain_per_tandem_pass": "true_strain",
        "f1_true_strain_share": "f1_true_share",
        "f1_thickness_reduction_share": "f1_thickness_share",
        "uncoiler_tension_t": "uncoiler",
        "interstand_tension_t": "interstand",
        "coiler_tension_t": "coiler",
        "uncoiler_unit_tension_kn_mm2": "uncoiler_unit",
        "interstand_unit_tension_kn_mm2": "interstand_unit",
        "coiler_unit_tension_kn_mm2": "coiler_unit",
        "f1_total_force_t": "f1_total_force",
        "f2_total_force_t": "f2_total_force",
        "f1_high_speed_gap_mm": "f1_gap",
        "f2_high_speed_gap_mm": "f2_gap",
    }
    envelopes = {name: envelope(values(production, key)) for name, key in metric_map.items()}
    for name, key in {
        "f1_operation_force_t": "f1_operation",
        "f1_drive_force_t": "f1_drive",
        "f2_operation_force_t": "f2_operation",
        "f2_drive_force_t": "f2_drive",
        "f1_operation_share": "f1_operation_share",
        "f2_operation_share": "f2_operation_share",
        "f1_side_imbalance_ratio": "f1_imbalance_ratio",
        "f2_side_imbalance_ratio": "f2_imbalance_ratio",
    }.items():
        envelopes[name] = envelope(values(enriched, key))

    target_strain = float(envelopes["true_strain_per_tandem_pass"]["median"])
    normal_max_strain = float(envelopes["true_strain_per_tandem_pass"]["p95"])
    strict_total_tension_order = [
        row
        for row in production
        if row["uncoiler"] < row["interstand"] < row["coiler"]
    ]
    strict_unit_tension_order = [
        row
        for row in production
        if row["uncoiler_unit"] < row["interstand_unit"] < row["coiler_unit"]
    ]
    artifact = {
        "schema_version": "cold-rolling-engineering-baseline-v0.6",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "provisional_high_speed_observed_envelope",
        "units": {
            "thickness": "mm",
            "width": "mm",
            "force": "T",
            "total_tension": "T",
            "unit_tension": "kN/mm²",
        },
        "data": {
            "production_case_count": len(production),
            "side_force_case_count": len(enriched),
            "days": sorted({str(row["day"]) for row in production}),
            "temporal_audit_day": "260805",
            "filter": "learning_eligible and digital_state_usable and stable_high_speed_seconds>=120",
        },
        "envelopes": envelopes,
        "route_policy": {
            "target_true_strain_per_tandem_pass": round(target_strain, 6),
            "normal_max_true_strain_per_tandem_pass": round(normal_max_strain, 6),
            "planning_formula": "choose the fewest complete F1+F2 passes satisfying manufacturer stand-reduction and force limits; historical envelopes are warnings",
            "f1_f2_distribution": "front-loaded route strain; F1 true-strain share rises from opening to finishing pass",
            "manufacturer_stand_limits": {
                "1xxx": {
                    "f1_max_reduction": 0.60,
                    "f2_max_reduction": 0.46,
                    "thin_f1_max_reduction": 0.55,
                    "thin_f2_max_reduction": 0.46,
                },
                "3xxx": {
                    "f1_max_reduction": 0.55,
                    "f2_max_reduction": 0.45,
                    "thin_f1_max_reduction": 0.50,
                    "thin_f2_max_reduction": 0.40,
                },
            },
            "hard_incoming_reduction_margin": 0.05,
            "thin_exit_threshold_mm": 1.0,
            "f1_true_strain_share_opening": 0.52,
            "f1_true_strain_share_finishing": 0.58,
            "distribution_bands": pass_bands(production),
            "pass_strain_taper": 0.07,
            "pass_strain_taper_method": "linear front-loaded true-strain weights, normalized to preserve total true strain",
            "refeed": "coil then reload; strip direction unchanged",
        },
        "tension_order_policy": {
            "status": "vendor_rule_confirmed_by_high_speed_dat",
            "comparison_basis": "unit_tension_kN_per_mm2",
            "relation": "uncoiler_unit <= interstand_unit <= coiler_unit",
            "formula": "q = 9.80665*T/(width*local_thickness)",
            "local_sections": {
                "uncoiler": "width*entry_thickness",
                "interstand": "width*intermediate_thickness",
                "coiler": "width*exit_thickness",
            },
            "strict_compliant_count": len(strict_unit_tension_order),
            "total_case_count": len(production),
            "strict_compliant_percent": round(
                100 * len(strict_unit_tension_order) / len(production), 4
            ),
            "total_tension_strict_compliant_count": len(strict_total_tension_order),
            "total_tension_strict_compliant_percent": round(
                100 * len(strict_total_tension_order) / len(production), 4
            ),
            "application": "minimum-change isotonic projection in unit-tension space, then convert back to HMI tonnes",
        },
        "confirmed_plant_limits": {
            "uncoiler_tension_t_max": 14.0,
            "interstand_tension_t_max": 14.0,
            "coiler_tension_t_max": 14.0,
            "f1_total_force_t_max": 1000.0,
            "f2_total_force_t_max": 1000.0,
            "status": "manufacturer-confirmed hard upper limits",
        },
        "annealing_policy": {
            "intermediate_review_cumulative_reduction": {
                "1060": 0.90,
                "1100": 0.90,
                "3003": 0.70,
            },
            "threshold_status": {
                "1060": "vendor experience review threshold; not an automatic anneal command",
                "1100": "vendor experience review threshold; not an automatic anneal command",
                "3003": "vendor reply recommends review above approximately 70% cumulative reduction",
            },
            "final_o_temper_reference": {
                "1100": {"temperature_c": 350, "hold_hours": [0.5, 2.0], "source": "Australian Aluminium Council Rolled Products Manual 2024 Table 21"},
                "3003": {"temperature_c": 410, "hold_hours": [0.5, 2.0], "source": "Australian Aluminium Council Rolled Products Manual 2024 Table 21"},
                "1060": {"temperature_c": None, "hold_hours": None, "source": "plant recipe required; 1060 is not explicit in cited table"},
            },
            "intermediate_rule": "flag process review when the alloy-specific cumulative reduction threshold is reached; do not auto-prescribe furnace parameters",
        },
        "side_force_policy": {
            "method": "predicted stand total multiplied by the historical median operation-side share; drive side is the remainder",
            "f1_operation_share": envelopes["f1_operation_share"]["median"],
            "f2_operation_share": envelopes["f2_operation_share"]["median"],
            "balance_monitor": "compare absolute drive-operation difference / stand total against P95/P99",
        },
        "classification": {
            "normal": "inside P05—P95 of accepted high-speed history",
            "warning": "P01—P05 or P95—P99; route planner should add a pass or require review",
            "outside_history": "outside P01—P99; no numeric recommendation",
        },
        "limitations": [
            "These are historical operating envelopes, not equipment nameplate, alarm or statutory safety limits.",
            "DAT has no validated alloy/pass linkage, so 1060/1100/3003 currently share the same statistical envelope.",
            "Intermediate annealing events are absent from DAT and cannot be learned reliably.",
            "Final authority remains the equipment manual, alarm settings and approved plant process card.",
        ],
    }
    encoded = json.dumps(artifact, ensure_ascii=False, indent=2)
    for path in (OUTPUT, APP_OUTPUT):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(encoded, encoding="utf-8")
        print(f"wrote {path}")
    print(json.dumps({
        "production": len(production),
        "side_force": len(enriched),
        "days": Counter(str(row["day"]) for row in enriched),
    }, ensure_ascii=False, default=dict))


if __name__ == "__main__":
    main()
