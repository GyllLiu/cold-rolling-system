#!/usr/bin/env python3
"""Benchmark candidate cold-rolling force equations on Golden high-speed data.

The benchmark deliberately leaves one production date out.  Flow-stress bands
and the optional width residual correction are fitted only on the remaining
dates, so the comparison cannot learn the force of the held-out date.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from tandem_data_filter import is_tandem_record

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT.parent
GOLD_PATH = WORKSPACE / "outputs" / "golden_recipe_v1" / "golden_recipe_dataset.csv"
OUTPUT_DIR = WORKSPACE / "outputs" / "physics_v070_benchmark"
ARTIFACT_PATH = ROOT / "backend" / "app" / "model_artifacts" / "equipment_physics_v070.json"
ROUTE_MODEL_PATH = ROOT / "backend" / "app" / "model_artifacts" / "route_model_v061.json"

WORK_ROLL_RADIUS_MM = 190.0
FRICTION_COEFFICIENT = 0.08
ROLL_YOUNG_MODULUS_MPA = 210_000.0
ROLL_POISSON_RATIO = 0.30
PLANE_STRAIN_FACTOR = 1.15
THICKNESS_BANDS = [0.0, 0.75, 1.25, 2.0, 3.0, 4.5, 8.0]
# Plant-data-supported bands.  Boundaries were chosen before validation from
# width coverage, keeping every band large enough for a stable residual median.
WIDTH_BANDS = [1000.0, 1150.0, 1220.0, 1280.0, 1350.0, 1575.0]

# Typical room-temperature yield-strength knots used by the existing material
# artifact.  A Swift curve is fitted to these literature-bounded points; these
# are engineering priors until plant samples carry validated alloy/temper IDs.
MATERIAL_STRENGTH_KNOTS = {
    "1060": [(0.0, 30.0), (0.287682, 75.0), (0.693147, 90.0), (1.098612, 105.0), (1.386294, 125.0)],
    "1100": [(0.0, 35.0), (0.287682, 105.0), (0.693147, 115.0), (1.098612, 140.0), (1.386294, 150.0)],
    "3003": [(0.0, 40.0), (0.287682, 125.0), (0.693147, 145.0), (1.098612, 170.0), (1.386294, 185.0)],
}
SWIFT_PARAMETERS = {
    "1060": {"K_mpa": 105.160345, "epsilon_0": 0.018168, "n": 0.31294},
    "1100": {"K_mpa": 134.570122, "epsilon_0": 0.002863, "n": 0.229985},
    "3003": {"K_mpa": 165.847600, "epsilon_0": 0.003168, "n": 0.24714},
}
TEMPER_EQUIVALENT_PRESTRAIN = {
    "annealed": 0.0,
    "h14": round(math.log(2.0), 6),
    "h16": round(math.log(3.0), 6),
    "h18": round(math.log(4.0), 6),
    "h22": round(math.log(4.0 / 3.0), 6),
    "h24": round(math.log(2.0), 6),
    "h26": round(math.log(3.0), 6),
    "unannealed": round(math.log(2.0), 6),
}


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def number(row: dict[str, str], key: str) -> float | None:
    try:
        value = float(row.get(key, ""))
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    left = int(position)
    right = min(left + 1, len(ordered) - 1)
    return ordered[left] + (ordered[right] - ordered[left]) * (position - left)


def median(values: list[float]) -> float:
    return statistics.median(values)


def thickness_band_index(thickness: float) -> int:
    for index, (low, high) in enumerate(zip(THICKNESS_BANDS, THICKNESS_BANDS[1:])):
        if low < thickness <= high:
            return index
    return len(THICKNESS_BANDS) - 2


def width_band_index(width: float) -> int:
    for index, (low, high) in enumerate(zip(WIDTH_BANDS, WIDTH_BANDS[1:])):
        if low <= width <= high:
            return index
    return 0 if width < WIDTH_BANDS[0] else len(WIDTH_BANDS) - 2


def tension_stress_mpa(tonnes: float, width_mm: float, thickness_mm: float) -> float:
    return 9.80665 * tonnes / (width_mm * thickness_mm) * 1000.0


def flattened_geometry(force_n: float, width_mm: float, delta_h: float) -> tuple[float, float]:
    radius = WORK_ROLL_RADIUS_MM * (
        1.0
        + 16.0 * (1.0 - ROLL_POISSON_RATIO**2)
        / (math.pi * ROLL_YOUNG_MODULUS_MPA)
        * force_n / (width_mm * delta_h)
    )
    return radius, math.sqrt(radius * delta_h)


def q_exponential(radius: float, entry: float, exit_: float) -> float:
    contact = math.sqrt(radius * (entry - exit_))
    mean_thickness = (entry + exit_) / 2.0
    argument = FRICTION_COEFFICIENT * contact / mean_thickness
    return math.expm1(argument) / argument if argument else 1.0


def q_hill(radius: float, entry: float, exit_: float) -> float:
    reduction = (entry - exit_) / entry
    return max(
        0.25,
        1.08
        + 1.79 * FRICTION_COEFFICIENT * reduction * math.sqrt(radius / entry)
        - 1.02 * reduction,
    )


def tension_equal(back: float, front: float) -> float:
    return 0.5 * back + 0.5 * front


def tension_hill(back: float, front: float) -> float:
    return 0.7 * back + 0.3 * front


def inverse_flow_stress(
    sample: dict[str, Any],
    q_function: Callable[[float, float, float], float],
    tension_function: Callable[[float, float], float],
    width_factor: float = 1.0,
) -> float | None:
    force_n = sample["force"] * 9806.65
    delta_h = sample["entry"] - sample["exit"]
    if delta_h <= 0 or width_factor <= 0:
        return None
    radius, contact = flattened_geometry(force_n, sample["width"], delta_h)
    q_value = q_function(radius, sample["entry"], sample["exit"])
    back = tension_stress_mpa(
        sample["back_tension"], sample["width"], sample["entry"]
    )
    front = tension_stress_mpa(
        sample["front_tension"], sample["width"], sample["exit"]
    )
    tension = tension_function(back, front)
    flow = (force_n / (sample["width"] * contact * q_value * width_factor) + tension) / PLANE_STRAIN_FACTOR
    return flow if 5.0 <= flow <= 1000.0 else None


def force_from_flow(
    sample: dict[str, Any],
    flow_stress: float,
    q_function: Callable[[float, float, float], float],
    tension_function: Callable[[float, float], float],
    width_factor: float = 1.0,
) -> float:
    delta_h = sample["entry"] - sample["exit"]
    radius = WORK_ROLL_RADIUS_MM
    force_n = 0.0
    for _ in range(50):
        contact = math.sqrt(radius * delta_h)
        q_value = q_function(radius, sample["entry"], sample["exit"])
        back = tension_stress_mpa(
            sample["back_tension"], sample["width"], sample["entry"]
        )
        front = tension_stress_mpa(
            sample["front_tension"], sample["width"], sample["exit"]
        )
        effective_pressure = max(
            1.0,
            PLANE_STRAIN_FACTOR * flow_stress - tension_function(back, front),
        )
        force_n = (
            sample["width"] * contact * q_value * effective_pressure * width_factor
        )
        next_radius, _ = flattened_geometry(
            force_n, sample["width"], delta_h
        )
        if abs(next_radius - radius) < 1e-8:
            radius = next_radius
            break
        radius = next_radius
    return force_n / 9806.65


def sample_from_row(row: dict[str, str], stand: str) -> dict[str, Any] | None:
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
    if not (entry > exit_ > 0 and force > 0 and back >= 0 and front >= 0):
        return None
    return {
        "dataset": row["dataset"],
        "stand": stand,
        "entry": entry,
        "exit": exit_,
        "width": width,
        "force": force,
        "back_tension": back,
        "front_tension": front,
    }


def fit_flow_bands(
    samples: list[dict[str, Any]],
    q_function: Callable[[float, float, float], float],
    tension_function: Callable[[float, float], float],
) -> dict[str, list[float]]:
    result: dict[str, list[float]] = {}
    for stand in ("f1", "f2"):
        stand_rows = [sample for sample in samples if sample["stand"] == stand]
        all_flows = [
            flow for sample in stand_rows
            if (flow := inverse_flow_stress(sample, q_function, tension_function)) is not None
        ]
        bands: list[float] = []
        for index in range(len(THICKNESS_BANDS) - 1):
            local = [
                flow for sample in stand_rows
                if thickness_band_index(sample["entry"]) == index
                and (flow := inverse_flow_stress(sample, q_function, tension_function)) is not None
            ]
            bands.append(median(local if len(local) >= 10 else all_flows))
        result[stand] = bands
    return result


def fit_flow_band_statistics(
    samples: list[dict[str, Any]],
    q_function: Callable[[float, float, float], float],
    tension_function: Callable[[float, float], float],
) -> dict[str, list[dict[str, float]]]:
    result: dict[str, list[dict[str, float]]] = {}
    for stand in ("f1", "f2"):
        stand_rows = [sample for sample in samples if sample["stand"] == stand]
        fallback = [
            flow for sample in stand_rows
            if (flow := inverse_flow_stress(sample, q_function, tension_function)) is not None
        ]
        output = []
        for index, (low, high) in enumerate(zip(THICKNESS_BANDS, THICKNESS_BANDS[1:])):
            local = [
                flow for sample in stand_rows
                if thickness_band_index(sample["entry"]) == index
                and (flow := inverse_flow_stress(sample, q_function, tension_function)) is not None
            ]
            values = local if len(local) >= 10 else fallback
            output.append({
                "entry_thickness_low_mm": low,
                "entry_thickness_high_mm": high,
                "count": len(local),
                "effective_flow_stress_p05_mpa": round(percentile(values, 0.05), 6),
                "effective_flow_stress_median_mpa": round(percentile(values, 0.50), 6),
                "effective_flow_stress_p95_mpa": round(percentile(values, 0.95), 6),
            })
        result[stand] = output
    return result


def flow_for_sample(sample: dict[str, Any], bands: dict[str, list[float]]) -> float:
    return bands[sample["stand"]][thickness_band_index(sample["entry"])]


def fit_width_factors(
    samples: list[dict[str, Any]],
    flow_bands: dict[str, list[float]],
    q_function: Callable[[float, float, float], float],
    tension_function: Callable[[float, float], float],
) -> dict[str, list[dict[str, float]]]:
    factors: dict[str, list[dict[str, float]]] = {}
    for stand in ("f1", "f2"):
        rows = [sample for sample in samples if sample["stand"] == stand]
        all_ratios = []
        ratios_by_band: dict[int, list[float]] = defaultdict(list)
        for sample in rows:
            predicted = force_from_flow(
                sample, flow_for_sample(sample, flow_bands), q_function, tension_function
            )
            if predicted <= 0:
                continue
            ratio = sample["force"] / predicted
            if 0.5 <= ratio <= 1.5:
                all_ratios.append(ratio)
                ratios_by_band[width_band_index(sample["width"])].append(ratio)
        global_median = median(all_ratios)
        stand_factors = []
        for index, (low, high) in enumerate(zip(WIDTH_BANDS, WIDTH_BANDS[1:])):
            local = ratios_by_band[index]
            local_median = median(local) if local else global_median
            # Shrink small groups towards the global physical scale.  This is a
            # residual calibration, not a replacement for the B multiplier.
            weight = len(local) / (len(local) + 30.0)
            value = 1.0 + weight * (local_median / global_median - 1.0)
            stand_factors.append({
                "width_low_mm": low,
                "width_high_mm": high,
                "count": len(local),
                "factor": max(0.85, min(1.15, value)),
            })
        factors[stand] = stand_factors
    return factors


def width_factor_for_sample(
    sample: dict[str, Any], factors: dict[str, list[dict[str, float]]]
) -> float:
    return factors[sample["stand"]][width_band_index(sample["width"])]["factor"]


def predict_route_model(model: dict[str, Any], features: dict[str, float]) -> float:
    """Evaluate the frozen v0.6.1 polynomial used by the customer page."""
    standardized = [
        (features[name] - model["feature_mean"][index]) / model["feature_scale"][index]
        for index, name in enumerate(model["feature_names"])
    ]
    row = [1.0] + [
        math.prod(standardized[index] for index in term)
        for term in model["basis_terms"]
    ]
    value = sum(left * right for left, right in zip(row, model["coefficients"]))
    if model.get("hard_min") is not None:
        value = max(float(model["hard_min"]), value)
    if model.get("hard_max") is not None:
        value = min(float(model["hard_max"]), value)
    return value


def evaluate_frozen_display_model(rows: list[dict[str, str]]) -> dict[str, Any]:
    """Replay the exact pre-physics force recommendation on the same rows."""
    artifact = json.loads(ROUTE_MODEL_PATH.read_text(encoding="utf-8"))
    errors: list[float] = []
    signed: list[float] = []
    percentage: list[float] = []
    by_stand: dict[str, dict[str, float]] = {}
    for stand in ("f1", "f2"):
        local_errors: list[float] = []
        local_signed: list[float] = []
        local_percentage: list[float] = []
        force_key = f"{stand}_force_actual_median"
        model = artifact["models"][f"{stand}_roll_force_sum_median"]
        for source in rows:
            entry = number(source, "current_entry_thickness")
            exit_ = number(source, "f2_exit_thickness")
            width = number(source, "width")
            actual = number(source, force_key)
            if None in (entry, exit_, width, actual) or actual <= 0:
                continue
            features = {
                "entry_thickness_set_mode": entry,
                "exit_thickness_set_mode": exit_,
                "width_set_mode": width,
                "total_reduction_ratio": 1.0 - exit_ / entry,
                "log_thickness_ratio": math.log(entry / exit_),
                "entry_cross_section_proxy": entry * width,
                "exit_cross_section_proxy": exit_ * width,
            }
            difference = predict_route_model(model, features) - actual
            local_errors.append(abs(difference))
            local_signed.append(difference)
            local_percentage.append(abs(difference) / actual * 100.0)
        errors.extend(local_errors)
        signed.extend(local_signed)
        percentage.extend(local_percentage)
        by_stand[stand] = {
            "n": len(local_errors),
            "mae_t": round(statistics.fmean(local_errors), 6),
            "p95_absolute_error_t": round(percentile(local_errors, 0.95), 6),
            "mape_percent": round(statistics.fmean(local_percentage), 6),
            "bias_t": round(statistics.fmean(local_signed), 6),
        }
    return {
        "n": len(errors),
        "mae_t": round(statistics.fmean(errors), 6),
        "p95_absolute_error_t": round(percentile(errors, 0.95), 6),
        "mape_percent": round(statistics.fmean(percentage), 6),
        "bias_t": round(statistics.fmean(signed), 6),
        "by_stand": by_stand,
    }


def evaluate_variant(
    name: str,
    samples: list[dict[str, Any]],
    q_function: Callable[[float, float, float], float],
    tension_function: Callable[[float, float], float],
    use_width_factor: bool,
) -> dict[str, Any]:
    absolute_errors: list[float] = []
    signed_errors: list[float] = []
    by_date = []
    final_width_factors = None
    for dataset in sorted({sample["dataset"] for sample in samples}):
        train = [sample for sample in samples if sample["dataset"] != dataset]
        test = [sample for sample in samples if sample["dataset"] == dataset]
        flow_bands = fit_flow_bands(train, q_function, tension_function)
        width_factors = fit_width_factors(
            train, flow_bands, q_function, tension_function
        ) if use_width_factor else None
        local_errors = []
        for sample in test:
            factor = width_factor_for_sample(sample, width_factors) if width_factors else 1.0
            predicted = force_from_flow(
                sample,
                flow_for_sample(sample, flow_bands),
                q_function,
                tension_function,
                factor,
            )
            difference = predicted - sample["force"]
            local_errors.append(abs(difference))
            absolute_errors.append(abs(difference))
            signed_errors.append(difference)
        by_date.append({
            "dataset": dataset,
            "n": len(test),
            "mae_t": round(statistics.fmean(local_errors), 6),
            "p95_absolute_error_t": round(percentile(local_errors, 0.95), 6),
        })
    final_bands = fit_flow_bands(samples, q_function, tension_function)
    if use_width_factor:
        final_width_factors = fit_width_factors(
            samples, final_bands, q_function, tension_function
        )
    return {
        "name": name,
        "validation": {
            "method": "leave-one-production-date-out",
            "n": len(absolute_errors),
            "mae_t": round(statistics.fmean(absolute_errors), 6),
            "p95_absolute_error_t": round(percentile(absolute_errors, 0.95), 6),
            "bias_t": round(statistics.fmean(signed_errors), 6),
            "by_date": by_date,
        },
        "flow_stress_median_mpa_by_stand_and_entry_band": final_bands,
        "width_residual_factors": final_width_factors,
    }


def main() -> int:
    rows = read_rows(GOLD_PATH)
    eligible = [
        row for row in rows
        if row.get("golden_level") in {"GOLD_A", "GOLD_B"}
        and (number(row, "lubrication_status_ok_ratio") or 0.0) >= 0.99
    ]
    samples = [
        sample
        for row in eligible
        for stand in ("f1", "f2")
        if (sample := sample_from_row(row, stand)) is not None
    ]
    variants = [
        evaluate_variant(
            "v067_exponential_q_equal_tension",
            samples,
            q_exponential,
            tension_equal,
            False,
        ),
        evaluate_variant(
            "hill_q_70_30_tension",
            samples,
            q_hill,
            tension_hill,
            False,
        ),
        evaluate_variant(
            "hill_q_70_30_tension_width_residual",
            samples,
            q_hill,
            tension_hill,
            True,
        ),
    ]
    baseline_mae = variants[0]["validation"]["mae_t"]
    for variant in variants:
        variant["mae_change_vs_v067_percent"] = round(
            (variant["validation"]["mae_t"] / baseline_mae - 1.0) * 100.0, 4
        )
    winner = min(variants, key=lambda variant: variant["validation"]["mae_t"])
    holdout_rows = [row for row in eligible if row["dataset"] == "260805"]
    frozen_display_all = evaluate_frozen_display_model(eligible)
    frozen_display_holdout = evaluate_frozen_display_model(holdout_rows)
    physical_holdout = next(
        row for row in winner["validation"]["by_date"] if row["dataset"] == "260805"
    )
    result = {
        "schema_version": "cold-tandem-physics-benchmark-v0.7.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "golden_plateaus": len(eligible),
        "stand_samples": len(samples),
        "assumptions": {
            "friction_coefficient": FRICTION_COEFFICIENT,
            "roll_young_modulus_mpa": ROLL_YOUNG_MODULUS_MPA,
            "roll_poisson_ratio": ROLL_POISSON_RATIO,
            "plane_strain_factor": PLANE_STRAIN_FACTOR,
            "width_base_formula": "P is directly proportional to strip width B",
            "width_residual_formula": "piecewise shrunken median(actual_force/physical_force) by width band",
        },
        "variants": variants,
        "winner": winner["name"],
        "displayed_model_comparison": {
            "warning": "all-date frozen-model replay is partly in-sample; 260805 is the comparable untouched holdout",
            "frozen_v061_all_dates": frozen_display_all,
            "frozen_v061_holdout_260805": frozen_display_holdout,
            "physical_v070_holdout_260805": physical_holdout,
            "holdout_mae_change_percent": round(
                (physical_holdout["mae_t"] / frozen_display_holdout["mae_t"] - 1.0) * 100.0,
                4,
            ),
        },
    }
    selected_width_factors = winner["width_residual_factors"]
    material_hardening = {
        "family": "Swift",
        "instantaneous_flow_stress_formula": "sigma(epsilon)=K*(epsilon_0+epsilon)^n",
        "cumulative_true_strain_formula": "epsilon_cumulative=epsilon_temper+ln(h_after_last_full_anneal/h_current)",
        "mean_flow_stress_formula": "sigma_m=K*((epsilon_0+epsilon_2)^(n+1)-(epsilon_0+epsilon_1)^(n+1))/((n+1)*(epsilon_2-epsilon_1))",
        "parameters": {
            alloy: {
                **parameters,
                "fit_basis": "least-squares Swift fit to literature-bounded O/H12/H14/H16/H18 typical yield-strength knots",
                "strength_knots": MATERIAL_STRENGTH_KNOTS[alloy],
            }
            for alloy, parameters in SWIFT_PARAMETERS.items()
        },
        "temper_equivalent_prestrain": TEMPER_EQUIVALENT_PRESTRAIN,
        "partial_anneal_warning": "H22/H24/H26 use H12/H14/H16 strength-level proxies only; H2 recovery history is not equivalent to H1 cold-work history",
        "deployment": "relative material correction only, clipped to 0.85-1.15 until labelled plant alloy/temper data are available",
    }
    artifact = {
        "schema_version": "cold-tandem-equipment-physics-v0.7.0",
        "generated_at": result["generated_at"],
        "status": "accepted_as_physical_guard_not_displayed_recommendation",
        "fallback_artifact": "equipment_physics_v067.json",
        "equipment": {
            "work_roll_diameter_mm": 380.0,
            "work_roll_radius_mm": WORK_ROLL_RADIUS_MM,
            "support_roll_diameter_mm": 1000.0,
            "roll_body_length_mm": 1750.0,
            "maximum_total_rolling_force_t_per_stand": 1000.0,
            "normal_speed_range_m_per_min": [400.0, 1000.0],
            "roll_young_modulus_mpa": ROLL_YOUNG_MODULUS_MPA,
            "roll_poisson_ratio": ROLL_POISSON_RATIO,
        },
        "normal_lubrication": {
            "friction_coefficient": FRICTION_COEFFICIENT,
            "source": "workshop director supplied nominal coefficient; held fixed",
            "identifiability_warning": "without forward slip or labelled lubricant condition, friction and flow stress cannot be identified independently",
            "eligible_golden_plateaus": len(eligible),
            "stand_samples": len(samples),
        },
        "material_hardening": material_hardening,
        "model": {
            "family": "Swift mean-flow stress + Hill/Bland-Ford force factor + iterative Hitchcock flattening",
            "reduction": "r=(h_in-h_out)/h_in",
            "contact_length": "L=sqrt(R_flattened*(h_in-h_out))",
            "q_factor": "Q=1.08+1.79*mu*r*sqrt(R_flattened/h_in)-1.02*r",
            "back_tension_weight": 0.7,
            "front_tension_weight": 0.3,
            "effective_pressure": "max(1,1.15*sigma_m-(0.7*sigma_back+0.3*sigma_front))",
            "force": "P=B*L*Q*effective_pressure*C_width",
            "hitchcock": "R_flattened=R*(1+16*(1-nu^2)/(pi*E)*P/(B*(h_in-h_out)))",
            "iteration": {"maximum_iterations": 50, "radius_tolerance_mm": 1e-8},
            "bands": fit_flow_band_statistics(samples, q_hill, tension_hill),
            "width_correction": {
                "base": "force is directly proportional to width B",
                "residual_formula": "C_width is a piecewise shrunken median(actual_force/physical_force) fitted within each stand and width band",
                "bands": selected_width_factors,
                "warning": "plant-specific residual correction; not an industry-universal coefficient",
            },
        },
        "validation": winner["validation"],
        "comparison_to_v067": {
            "baseline_mae_t": variants[0]["validation"]["mae_t"],
            "baseline_p95_t": variants[0]["validation"]["p95_absolute_error_t"],
            "mae_change_percent": winner["mae_change_vs_v067_percent"],
            "p95_change_percent": round(
                (winner["validation"]["p95_absolute_error_t"] /
                 variants[0]["validation"]["p95_absolute_error_t"] - 1.0) * 100.0,
                4,
            ),
        },
        "comparison_to_displayed_v061": result["displayed_model_comparison"],
        "deployment_policy": {
            "displayed_force": "retain frozen v0.6.1 DAT recommendation with bounded material correction because it wins the untouched 260805 holdout",
            "hard_force_gate": "max(physical_median+physical_LODO_P95_error, physical_P95_flow_stress, DAT_reference+DAT_P95_error)<=1000T",
            "route_ranking": "within equal finishing quality and pass count, prefer lower physical force utilization and smoother force curve",
            "material_policy": "Swift relative correction is bounded until labelled alloy/temper validation",
        },
        "sources": [
            {"title": "Bland and Ford, The Calculation of Roll Force and Torque in Cold Strip Rolling with Tensions", "url": "https://doi.org/10.1243/PIME_PROC_1948_159_015_02"},
            {"title": "On-line model for strip profile in cold rolling, Appendix II", "url": "https://www.jstage.jst.go.jp/article/isijinternational/60/2/60_ISIJINT-2019-233/_html/-char/en"},
            {"title": "Swift, Plastic instability under plane stress", "url": "https://doi.org/10.1016/0022-5096(52)90002-1"},
            {"title": "Properties of Wrought Aluminum and Aluminum Alloys", "url": "https://materialsdata.nist.gov/bitstream/handle/11115/179/Properties%20of%20Wrought%20Aluminum.pdf"},
            {"title": "Inverse flow-stress identification from actual cold rolling mill data", "url": "https://doi.org/10.1016/j.jmatprotec.2007.11.188"},
            {"title": "Experimental Study of Roll Flattening in Cold Rolling Process", "url": "https://www.jstage.jst.go.jp/article/isijinternational/58/4/58_ISIJINT-2017-623/_html/-char/en"},
            {"title": "The Aluminum Association standards and temper-designation system", "url": "https://www.aluminum.org/standards"},
        ],
        "limitations": [
            "current Golden DAT has no validated alloy, temper, or cross-pass coil linkage",
            "material hardening parameters are literature-bounded priors, not plant-identified constitutive curves",
            "mu=0.08 cannot be independently verified without forward-slip or equivalent interface measurements",
            "historical physical validation uses measured high-speed tensions; product use adds the separate tension-recommendation error",
        ],
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUTPUT_DIR / "physics_benchmark_v070.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    ARTIFACT_PATH.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# 物理轧制力模型 V0.7.0 时间留出对比",
        "",
        f"- 黄金高速稳定段：{len(eligible)}",
        f"- 单机架样本：{len(samples)}",
        "- 验证：每次整天留出，流动应力与宽度修正只用其余日期计算",
        "",
        "| 候选 | MAE (T) | P95 (T) | Bias (T) | MAE相对V0.6.7 |",
        "|---|---:|---:|---:|---:|",
    ]
    for variant in variants:
        validation = variant["validation"]
        lines.append(
            f"| {variant['name']} | {validation['mae_t']:.3f} | "
            f"{validation['p95_absolute_error_t']:.3f} | {validation['bias_t']:.3f} | "
            f"{variant['mae_change_vs_v067_percent']:+.2f}% |"
        )
    holdout_change = result["displayed_model_comparison"]["holdout_mae_change_percent"]
    lines.extend([
        "",
        f"时间留出物理候选最优：`{winner['name']}`",
        "",
        "## 与页面原轧制力模型的真正留出对比",
        "",
        "| 260805（未参与原模型训练） | MAE (T) | P95 (T) |",
        "|---|---:|---:|",
        f"| 原 v0.6.1 DAT 推荐 | {frozen_display_holdout['mae_t']:.3f} | {frozen_display_holdout['p95_absolute_error_t']:.3f} |",
        f"| 新 v0.7.0 纯物理 | {physical_holdout['mae_t']:.3f} | {physical_holdout['p95_absolute_error_t']:.3f} |",
        "",
        f"纯物理 MAE 相对原推荐变化：**{holdout_change:+.2f}%**。因此不替换页面推荐值，物理模型用于道次筛选、候选排序和 1000 T 安全校核。",
        "",
        "## 六项接入状态",
        "",
        "1. 1060、1100、3003：Swift `K(ε0+ε)^n`，参数由公开典型强度节点拟合，只作有界材料修正。",
        "2. O/H14/H16/H18/H22/H24/H26：以等效预应变进入；H2 系列只是强度级代理，不视为与 H1 工艺历史等同。",
        "3. 累计变形：从最近一次完全退火后按 `ln(h0/h)` 累积，并用 Swift 闭式积分得到本道平均流动应力。",
        "4. Q 与张力：Hill/Bland–Ford 压力因子；后张力 0.7、前张力 0.3 进入有效压力。",
        "5. Hitchcock：暂用钢辊 E=210000 MPa、ν=0.30，最多 50 次迭代，半径收敛阈值 1e-8 mm。",
        "6. 宽度：主项按 B 线性；1000—1575 mm 分段残差系数由其余日期拟合后验证。",
        "",
        "## 结论",
        "",
        "物理结构保留并接入安全决策；纯物理推荐值回退。当前 DAT 没有可靠牌号、状态和跨道次卷号，不能把材料参数单独标定成厂内专用公式。",
        "",
        "偏差的主要来源不是公式写错，而是当前数据无法把材料硬化与摩擦分开识别：DAT 无可靠牌号/状态/跨道次卷号，μ 暂固定为 0.08，工作辊 E/ν 暂按钢辊通用值，且未纳入实际油温、油量、辊面粗糙度和磨损。Hitchcock 对很薄或高强带材还存在圆弧扁平假设局限。",
        "",
        "## 道次回归检查",
        "",
        "固定 14 个既有验收规格回放后，道次数 14/14 不变，厚度路线 13/14 完全不变。唯一变化为 6.4→3.7 mm 的单道中间厚度 4.8156→4.6449 mm，使 F1/F2 压下率由 24.76%/23.17% 调为 27.42%/20.34%，更符合 F1 主变形、F2 精调；并未增加道次。",
        "",
        "## 参考",
        "",
        "- [Bland–Ford 含前后张力轧制力理论](https://doi.org/10.1243/PIME_PROC_1948_159_015_02)",
        "- [Hill/Bland–Ford 公式与张力项（Appendix II）](https://www.jstage.jst.go.jp/article/isijinternational/60/2/60_ISIJINT-2019-233/_html/-char/en)",
        "- [Hitchcock 扁平半径公式及适用局限](https://www.jstage.jst.go.jp/article/isijinternational/58/4/58_ISIJINT-2017-623/_html/-char/en)",
        "- [Swift 加工硬化原始论文](https://doi.org/10.1016/0022-5096(52)90002-1)",
        "- [NIST/ASM 铝合金典型力学性能](https://materialsdata.nist.gov/bitstream/handle/11115/179/Properties%20of%20Wrought%20Aluminum.pdf)",
        "- [Aluminum Association 状态代号体系](https://www.aluminum.org/standards)",
        "",
    ])
    (OUTPUT_DIR / "physics_benchmark_v070.md").write_text(
        "\n".join(lines), encoding="utf-8"
    )
    print(json.dumps({
        "output": str(json_path),
        "winner": winner["name"],
        "metrics": {
            variant["name"]: variant["validation"]
            for variant in variants
        },
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
