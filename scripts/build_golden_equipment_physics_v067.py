#!/usr/bin/env python3
"""Build a μ=0.08 equipment/physics gate and Golden route-edge library."""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from tandem_data_filter import is_tandem_record

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
GOLD_PATH = WORKSPACE / "outputs" / "golden_recipe_v1" / "golden_recipe_dataset.csv"
ARTIFACT_DIR = ROOT / "backend" / "app" / "model_artifacts"

WORK_ROLL_DIAMETER_MM = 380.0
WORK_ROLL_RADIUS_MM = WORK_ROLL_DIAMETER_MM / 2
SUPPORT_ROLL_DIAMETER_MM = 1000.0
ROLL_BODY_LENGTH_MM = 1750.0
MAX_ROLL_FORCE_T = 1000.0
FRICTION_COEFFICIENT = 0.08
SPEED_RANGE_M_PER_MIN = [400.0, 1000.0]
ROLL_YOUNG_MODULUS_MPA = 210_000.0
ROLL_POISSON_RATIO = 0.30
PLANE_STRAIN_FACTOR = 1.15
THICKNESS_BANDS = [0.0, 0.75, 1.25, 2.0, 3.0, 4.5, 8.0]


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def number(row: dict[str, str], key: str) -> float | None:
    try:
        value = float(row.get(key, ""))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def percentile(values: list[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    left = int(position)
    right = min(left + 1, len(ordered) - 1)
    return ordered[left] + (ordered[right] - ordered[left]) * (position - left)


def band_index(thickness: float) -> int:
    for index, (low, high) in enumerate(zip(THICKNESS_BANDS, THICKNESS_BANDS[1:])):
        if low < thickness <= high:
            return index
    return len(THICKNESS_BANDS) - 2


def tension_stress_mpa(tonnes: float, width_mm: float, thickness_mm: float) -> float:
    return 9.80665 * tonnes / (width_mm * thickness_mm) * 1000.0


def inverse_effective_flow_stress(
    force_t: float,
    width_mm: float,
    entry_mm: float,
    exit_mm: float,
    back_tension_t: float,
    front_tension_t: float,
) -> tuple[float, float, float, float] | None:
    delta_h = entry_mm - exit_mm
    if delta_h <= 0 or width_mm <= 0:
        return None
    force_n = force_t * 9806.65
    flattened_radius = WORK_ROLL_RADIUS_MM
    for _ in range(30):
        contact_length = math.sqrt(flattened_radius * delta_h)
        next_radius = WORK_ROLL_RADIUS_MM * (
            1.0
            + 16.0 * (1.0 - ROLL_POISSON_RATIO**2)
            / (math.pi * ROLL_YOUNG_MODULUS_MPA)
            * force_n / (width_mm * delta_h)
        )
        if abs(next_radius - flattened_radius) < 1e-8:
            break
        flattened_radius = next_radius
    contact_length = math.sqrt(flattened_radius * delta_h)
    mean_thickness = (entry_mm + exit_mm) / 2.0
    friction_argument = FRICTION_COEFFICIENT * contact_length / mean_thickness
    friction_factor = math.expm1(friction_argument) / friction_argument if friction_argument else 1.0
    mean_tension = (
        tension_stress_mpa(back_tension_t, width_mm, entry_mm)
        + tension_stress_mpa(front_tension_t, width_mm, exit_mm)
    ) / 2.0
    flow_stress = ((force_n / (width_mm * contact_length)) / friction_factor + mean_tension) / PLANE_STRAIN_FACTOR
    if not 5.0 <= flow_stress <= 1000.0:
        return None
    return flow_stress, contact_length, friction_factor, flattened_radius


def force_from_flow_stress(
    flow_stress_mpa: float,
    width_mm: float,
    entry_mm: float,
    exit_mm: float,
    back_tension_t: float,
    front_tension_t: float,
) -> float:
    delta_h = entry_mm - exit_mm
    flattened_radius = WORK_ROLL_RADIUS_MM
    force_n = 0.0
    for _ in range(30):
        contact_length = math.sqrt(flattened_radius * delta_h)
        mean_thickness = (entry_mm + exit_mm) / 2.0
        friction_argument = FRICTION_COEFFICIENT * contact_length / mean_thickness
        friction_factor = math.expm1(friction_argument) / friction_argument if friction_argument else 1.0
        mean_tension = (
            tension_stress_mpa(back_tension_t, width_mm, entry_mm)
            + tension_stress_mpa(front_tension_t, width_mm, exit_mm)
        ) / 2.0
        pressure = max(1.0, PLANE_STRAIN_FACTOR * flow_stress_mpa - mean_tension) * friction_factor
        force_n = pressure * width_mm * contact_length
        next_radius = WORK_ROLL_RADIUS_MM * (
            1.0
            + 16.0 * (1.0 - ROLL_POISSON_RATIO**2)
            / (math.pi * ROLL_YOUNG_MODULUS_MPA)
            * force_n / (width_mm * delta_h)
        )
        if abs(next_radius - flattened_radius) < 1e-8:
            break
        flattened_radius = next_radius
    return force_n / 9806.65


def stand_sample(row: dict[str, str], stand: str) -> dict[str, Any] | None:
    if not is_tandem_record(row):
        return None
    width = number(row, "width")
    if stand == "f1":
        keys = (
            "current_entry_thickness", "f1_exit_thickness", "f1_force_actual_median",
            "entry_tension_actual_median", "interstand_tension_actual_median",
        )
    else:
        keys = (
            "f1_exit_thickness", "f2_exit_thickness", "f2_force_actual_median",
            "interstand_tension_actual_median", "exit_tension_actual_median",
        )
    values = [number(row, key) for key in keys]
    if width is None or any(value is None for value in values):
        return None
    entry, exit_, force, back, front = (float(value) for value in values)
    result = inverse_effective_flow_stress(force, width, entry, exit_, back, front)
    if result is None:
        return None
    flow, contact, friction_factor, flattened_radius = result
    return {
        "dataset": row["dataset"],
        "stand": stand,
        "entry": entry,
        "exit": exit_,
        "width": width,
        "force": force,
        "back_tension": back,
        "front_tension": front,
        "flow": flow,
        "contact_length": contact,
        "friction_factor": friction_factor,
        "flattened_radius": flattened_radius,
        "golden_level": row["golden_level"],
    }


def fit_bands(samples: list[dict[str, Any]], stand: str) -> list[dict[str, Any]]:
    stand_rows = [row for row in samples if row["stand"] == stand]
    fallback = [row["flow"] for row in stand_rows]
    output: list[dict[str, Any]] = []
    for index, (low, high) in enumerate(zip(THICKNESS_BANDS, THICKNESS_BANDS[1:])):
        values = [row["flow"] for row in stand_rows if band_index(row["entry"]) == index]
        if len(values) < 10:
            values = fallback
        output.append({
            "entry_thickness_low_mm": low,
            "entry_thickness_high_mm": high,
            "count": len(values),
            "effective_flow_stress_p05_mpa": round(percentile(values, 0.05) or 0.0, 6),
            "effective_flow_stress_median_mpa": round(percentile(values, 0.50) or 0.0, 6),
            "effective_flow_stress_p95_mpa": round(percentile(values, 0.95) or 0.0, 6),
        })
    return output


def selected_band(bands: list[dict[str, Any]], thickness: float) -> dict[str, Any]:
    return bands[band_index(thickness)]


def leave_date_out_validation(samples: list[dict[str, Any]]) -> dict[str, Any]:
    errors: list[float] = []
    signed: list[float] = []
    day_rows: list[dict[str, Any]] = []
    for dataset in sorted({row["dataset"] for row in samples}):
        train = [row for row in samples if row["dataset"] != dataset]
        test = [row for row in samples if row["dataset"] == dataset]
        bands = {stand: fit_bands(train, stand) for stand in ("f1", "f2")}
        local_errors: list[float] = []
        for row in test:
            flow = selected_band(bands[row["stand"]], row["entry"])["effective_flow_stress_median_mpa"]
            predicted = force_from_flow_stress(
                flow, row["width"], row["entry"], row["exit"],
                row["back_tension"], row["front_tension"],
            )
            difference = predicted - row["force"]
            errors.append(abs(difference))
            signed.append(difference)
            local_errors.append(abs(difference))
        day_rows.append({
            "dataset": dataset,
            "n": len(test),
            "mae_t": round(statistics.fmean(local_errors), 6),
            "p95_absolute_error_t": round(percentile(local_errors, 0.95) or 0.0, 6),
        })
    return {
        "method": "leave-one-production-date-out",
        "n": len(errors),
        "mae_t": round(statistics.fmean(errors), 6),
        "p95_absolute_error_t": round(percentile(errors, 0.95) or 0.0, 6),
        "bias_t": round(statistics.fmean(signed), 6),
        "by_date": day_rows,
        "use": "equipment feasibility and route ranking only; does not replace the displayed DAT force model",
    }


def main() -> int:
    rows = read_rows(GOLD_PATH)
    eligible = [
        row for row in rows
        if row.get("golden_level") in {"GOLD_A", "GOLD_B"}
        and is_tandem_record(row)
        and (number(row, "lubrication_status_ok_ratio") or 0.0) >= 0.99
    ]
    samples = [
        sample
        for row in eligible
        for stand in ("f1", "f2")
        if (sample := stand_sample(row, stand)) is not None
    ]
    bands = {stand: fit_bands(samples, stand) for stand in ("f1", "f2")}
    now = datetime.now(timezone.utc).isoformat()
    physics = {
        "schema_version": "cold-tandem-equipment-physics-v0.6.7",
        "generated_at": now,
        "equipment": {
            "work_roll_diameter_mm": WORK_ROLL_DIAMETER_MM,
            "work_roll_radius_mm": WORK_ROLL_RADIUS_MM,
            "support_roll_diameter_mm": SUPPORT_ROLL_DIAMETER_MM,
            "roll_body_length_mm": ROLL_BODY_LENGTH_MM,
            "maximum_total_rolling_force_t_per_stand": MAX_ROLL_FORCE_T,
            "normal_speed_range_m_per_min": SPEED_RANGE_M_PER_MIN,
            "roll_young_modulus_mpa": ROLL_YOUNG_MODULUS_MPA,
            "roll_poisson_ratio": ROLL_POISSON_RATIO,
        },
        "normal_lubrication": {
            "friction_coefficient": FRICTION_COEFFICIENT,
            "source": "workshop director supplied nominal coefficient; held fixed while Golden data invert effective flow stress",
            "identifiability_warning": "unlabelled DAT cannot separately identify friction and material flow stress; μ=0.08 is an engineering assumption, not a fitted truth",
            "eligible_golden_plateaus": len(eligible),
            "stand_samples": len(samples),
        },
        "model": {
            "family": "Bland-Ford-Hill-inspired mean-pressure model with iterative Hitchcock roll flattening",
            "contact_length": "sqrt(R_flattened * delta_h)",
            "friction_factor": "(exp(mu*contact_length/mean_thickness)-1)/(mu*contact_length/mean_thickness)",
            "force": "width * contact_length * max(1, 1.15*effective_flow_stress-mean_tension_stress) * friction_factor",
            "bands": bands,
        },
        "observed": {
            stand: {
                "count": len([row for row in samples if row["stand"] == stand]),
                "contact_length_median_mm": round(statistics.median(row["contact_length"] for row in samples if row["stand"] == stand), 6),
                "flattened_radius_median_mm": round(statistics.median(row["flattened_radius"] for row in samples if row["stand"] == stand), 6),
                "friction_factor_median": round(statistics.median(row["friction_factor"] for row in samples if row["stand"] == stand), 6),
            }
            for stand in ("f1", "f2")
        },
        "validation": leave_date_out_validation(samples),
        "planning_policy": {
            "hard_force_gate": "max(DAT prediction + DAT P95 error, physics P95 estimate) <= 1000 T",
            "ranking": "within the same quality band and pass count, prefer lower maximum force utilisation and smoother force/reduction curve",
            "bite_condition": "reported for threading review only; not used as a hard high-speed reduction limit because the tandem strip is already threaded and tensioned",
            "support_roll_use": "equipment traceability and future roll-deflection model; not inserted into the plane-strain bite-force equation",
            "speed_use": "defines the nominal lubrication regime; motor power/torque cannot be constrained until their limits are supplied",
        },
        "sources": [
            "https://doi.org/10.1155/2022/9167017",
            "https://doi.org/10.1002/srin.200005714",
            "https://doi.org/10.1016/0924-0136(92)90346-T",
        ],
    }
    history = {
        "artifact_version": "golden-reverse-route-v0.6.7",
        "generated_at": now,
        "selection": {
            "source": "golden_recipe_v1/golden_recipe_dataset.csv",
            "levels": ["GOLD_A", "GOLD_B"],
            "normal_lubrication_required": True,
            "record_meaning": "one stable complete tandem edge: entry -> F1 middle -> F2 exit",
            "limitation": "edges remain unlinked across passes because DAT has no true coil/pass id",
        },
        "case_count": len(eligible),
        "source_dates": sorted({row["dataset"] for row in eligible}),
        "records": [
            {
                "entry": round(number(row, "current_entry_thickness") or 0.0, 6),
                "middle": round(number(row, "f1_exit_thickness") or 0.0, 6),
                "exit": round(number(row, "f2_exit_thickness") or 0.0, 6),
                "width": round(number(row, "width") or 0.0, 3),
                "stable_seconds": round(number(row, "stable_duration") or 0.0, 3),
                "date": row["dataset"],
                "dataset": row["dataset"],
                "golden_level": row["golden_level"],
                "golden_score": round(number(row, "golden_score") or 0.0, 3),
            }
            for row in eligible
        ],
    }
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    (ARTIFACT_DIR / "equipment_physics_v067.json").write_text(
        json.dumps(physics, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (ARTIFACT_DIR / "route_history_golden_v067.json").write_text(
        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "eligible_golden_plateaus": len(eligible),
        "stand_samples": len(samples),
        "validation": physics["validation"],
        "physics_artifact": str(ARTIFACT_DIR / "equipment_physics_v067.json"),
        "history_artifact": str(ARTIFACT_DIR / "route_history_golden_v067.json"),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
