#!/usr/bin/env python3
"""Build and validate the v0.6.1 material-state-aware route artifact.

The high-speed DAT files do not contain validated alloy or incoming-temper
labels.  For that reason the script keeps the statistical DAT predictor as the
neutral/unknown-state path.  It only adds two independently auditable pieces:

1. a directly validated F2 closed-gap ensemble (raw gap + gap-minus-exit), and
2. a bounded, literature-constrained rolling-force sensitivity correction for
   known alloy/incoming state.  The latter is reported as sensitivity, not as
   labelled accuracy.
"""

from __future__ import annotations

import copy
from collections import Counter
import json
import math
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
SCRIPT_ROOT = PROJECT_ROOT / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

import train_compare_high_speed_v6 as v6  # noqa: E402
import train_validate_direct_prediction as core  # noqa: E402


OUTPUT_DIR = WORKSPACE_ROOT / "outputs" / "v6_1_material_state"
MODEL_OUTPUT = PROJECT_ROOT / "backend" / "app" / "model_artifacts" / "route_model_v061.json"
MATERIAL_OUTPUT = PROJECT_ROOT / "backend" / "app" / "model_artifacts" / "material_state_v061.json"
BASE_MODEL = PROJECT_ROOT / "backend" / "app" / "model_artifacts" / "route_model_v6.json"

F2_BLEND_WEIGHT = 0.40
WORK_ROLL_RADIUS_MM = 190.0
H12_EQUIVALENT_TRUE_STRAIN = round(math.log(4.0 / 3.0), 6)
H14_EQUIVALENT_TRUE_STRAIN = round(math.log(2.0), 6)
H16_EQUIVALENT_TRUE_STRAIN = round(math.log(3.0), 6)
H18_EQUIVALENT_TRUE_STRAIN = round(math.log(4.0), 6)
FORCE_MULTIPLIER_BOUNDS = (0.85, 1.15)

# Typical room-temperature yield strengths.  H18 is associated with roughly
# 75% cold reduction, so the final knot is placed at ln(1 / (1 - 0.75)).
MATERIAL_CURVES = {
    "1060": [(0.0, 30.0), (0.287682, 75.0), (0.693147, 90.0), (1.098612, 105.0), (1.386294, 125.0)],
    "1100": [(0.0, 35.0), (0.287682, 105.0), (0.693147, 115.0), (1.098612, 140.0), (1.386294, 150.0)],
    "3003": [(0.0, 40.0), (0.287682, 125.0), (0.693147, 145.0), (1.098612, 170.0), (1.386294, 185.0)],
}


def augment(cases: Sequence[core.Case]) -> None:
    for case in cases:
        entry = case.values["entry_thickness_set_mode"]
        middle = case.values["intermediate_thickness_set_mode"]
        exit_value = case.values["exit_thickness_set_mode"]
        width = case.values["width_set_mode"]
        case.values["contact_proxy_f2"] = width * math.sqrt(
            WORK_ROLL_RADIUS_MM * (middle - exit_value)
        )
        case.values["tm2_gap_offset"] = (
            case.values["tm2_closed_gap_set_median"] - exit_value
        )


def f2_offset_config() -> core.ModelConfig:
    return core.ModelConfig("gapf2", 2, 1.0, "raw")


def configure() -> None:
    v6.configure_core()
    core.FEATURE_SETS["gapf2"] = (
        "entry_thickness_set_mode",
        "intermediate_thickness_set_mode",
        "exit_thickness_set_mode",
        "width_set_mode",
        "contact_proxy_f2",
    )
    core.TARGET_BY_CODE["tm2_gap_offset"] = core.TargetSpec(
        "tm2_gap_offset", "F2闭合辊缝位置设定与出口厚度差", "mm", "setup", "primary", None, None
    )


def fit_offset(cases: Sequence[core.Case]) -> core.FittedModel:
    return core.fit_model(v6.balance_cases(cases), "tm2_gap_offset", f2_offset_config())


def raw_f2_config() -> core.ModelConfig:
    return core.ModelConfig("spec3", 2, 10.0, "raw")


def blend_predictions(
    raw_model: core.FittedModel,
    offset_model: core.FittedModel,
    cases: Sequence[core.Case],
) -> list[float]:
    output = []
    for case in cases:
        raw = core.predict_model(raw_model, case, "tm2_closed_gap_set_median")
        offset = core.predict_model(offset_model, case, "tm2_gap_offset")
        residual_prediction = offset + case.values["exit_thickness_set_mode"]
        output.append((1.0 - F2_BLEND_WEIGHT) * raw + F2_BLEND_WEIGHT * residual_prediction)
    return output


def evaluate_f2(cases: Sequence[core.Case]) -> dict[str, Any]:
    folds = v6.rolling_folds(cases)
    development = [case for case in cases if case.dataset != v6.HOLDOUT_DAY]
    holdout = [case for case in cases if case.dataset == v6.HOLDOUT_DAY]
    baseline_rows = []
    revised_rows = []
    for train, validation in folds:
        raw = core.fit_model(train, "tm2_closed_gap_set_median", raw_f2_config())
        offset = fit_offset(train)
        baseline_rows.append(
            v6.metrics(
                validation,
                [core.predict_model(raw, case, "tm2_closed_gap_set_median") for case in validation],
                "tm2_closed_gap_set_median",
            )
        )
        revised_rows.append(
            v6.metrics(
                validation,
                blend_predictions(raw, offset, validation),
                "tm2_closed_gap_set_median",
            )
        )
    raw = core.fit_model(development, "tm2_closed_gap_set_median", raw_f2_config())
    offset = fit_offset(development)
    baseline_holdout = v6.metrics(
        holdout,
        [core.predict_model(raw, case, "tm2_closed_gap_set_median") for case in holdout],
        "tm2_closed_gap_set_median",
    )
    revised_holdout = v6.metrics(
        holdout,
        blend_predictions(raw, offset, holdout),
        "tm2_closed_gap_set_median",
    )
    return {
        "baseline_rolling": v6.aggregate_fold_metrics(baseline_rows),
        "revised_rolling": v6.aggregate_fold_metrics(revised_rows),
        "baseline_holdout_260805": baseline_holdout,
        "revised_holdout_260805": revised_holdout,
        "offset_model": offset,
        "development_count": len(development),
    }


def serialize_offset(model: core.FittedModel, training_count: int) -> dict[str, Any]:
    return {
        "label": "F2闭合辊缝位置设定与出口厚度差",
        "unit": "mm",
        "feature_set": model.config.feature_set,
        "feature_names": list(model.feature_names),
        "degree": model.config.degree,
        "ridge_lambda": model.config.ridge_lambda,
        "target_transform": "raw",
        "feature_mean": model.feature_mean,
        "feature_scale": model.feature_scale,
        "basis_terms": model.basis_terms,
        "coefficients": model.coefficients,
        "hard_min": None,
        "hard_max": None,
        "training_case_count": training_count,
    }


def yield_strength(alloy: str, strain: float) -> float:
    points = MATERIAL_CURVES[alloy]
    if strain <= points[0][0]:
        return points[0][1]
    if strain >= points[-1][0]:
        return points[-1][1]
    for (left_x, left_y), (right_x, right_y) in zip(points, points[1:]):
        if left_x <= strain <= right_x:
            ratio = (strain - left_x) / (right_x - left_x)
            return left_y + ratio * (right_y - left_y)
    raise AssertionError(strain)


def mean_yield(alloy: str, start: float, end: float) -> float:
    if end <= start:
        return yield_strength(alloy, start)
    steps = 64
    step = (end - start) / steps
    values = [yield_strength(alloy, start + index * step) for index in range(steps + 1)]
    return step * (0.5 * values[0] + sum(values[1:-1]) + 0.5 * values[-1]) / (end - start)


def force_factor(alloy: str, state: str, stand_start: float, stand_end: float) -> float:
    if state == "unknown":
        return 1.0
    initial = 0.0 if state == "annealed" else H14_EQUIVALENT_TRUE_STRAIN
    actual = mean_yield(alloy, initial + stand_start, initial + stand_end)
    reference = mean_yield("1100", stand_start, stand_end)
    raw = actual / reference
    return min(FORCE_MULTIPLIER_BOUNDS[1], max(FORCE_MULTIPLIER_BOUNDS[0], raw))


def predict_artifact_model(model: dict[str, Any], case: core.Case, target: str) -> float:
    standardized = [
        (case.values[name] - mean) / scale
        for name, mean, scale in zip(model["feature_names"], model["feature_mean"], model["feature_scale"])
    ]
    design = [1.0]
    for term in model["basis_terms"]:
        product = 1.0
        for index in term:
            product *= standardized[index]
        design.append(product)
    return max(0.0, sum(value * coefficient for value, coefficient in zip(design, model["coefficients"])))


def material_sensitivity(cases: Sequence[core.Case], base_artifact: dict[str, Any]) -> dict[str, Any]:
    holdout = [case for case in cases if case.dataset == v6.HOLDOUT_DAY]
    output: dict[str, Any] = {}
    for target, stand in (("f1_roll_force_sum_median", "f1"), ("f2_roll_force_sum_median", "f2")):
        model = base_artifact["models"][target]
        base = [predict_artifact_model(model, case, target) for case in holdout]
        target_rows: dict[str, Any] = {"neutral_unlabelled": v6.metrics(holdout, base, target)}
        for alloy in MATERIAL_CURVES:
            for state in ("annealed", "unannealed"):
                corrected = []
                factors = []
                for prediction, case in zip(base, holdout):
                    entry = case.values["entry_thickness_set_mode"]
                    middle = case.values["intermediate_thickness_set_mode"]
                    exit_value = case.values["exit_thickness_set_mode"]
                    f1_end = math.log(entry / middle)
                    total_end = math.log(entry / exit_value)
                    start, end = (0.0, f1_end) if stand == "f1" else (f1_end, total_end)
                    factor = force_factor(alloy, state, start, end)
                    factors.append(factor)
                    corrected.append(prediction * factor)
                target_rows[f"{alloy}_{state}"] = {
                    **v6.metrics(holdout, corrected, target),
                    "mean_multiplier": statistics.fmean(factors),
                    "min_multiplier": min(factors),
                    "max_multiplier": max(factors),
                    "interpretation": "unlabelled sensitivity only; not an accuracy claim",
                }
        output[target] = target_rows
    return output


def latent_mixture_comparison(
    cases: Sequence[core.Case], base_artifact: dict[str, Any]
) -> dict[str, Any]:
    """Compare both material states to every unlabelled DAT segment.

    The closest-state assignment uses measured force, so it is an oracle upper
    bound rather than an inference method available before production.
    """

    target_f1 = "f1_roll_force_sum_median"
    target_f2 = "f2_roll_force_sum_median"
    scale_f1 = float(base_artifact["models"][target_f1]["validation"]["holdout_260805"]["p95"])
    scale_f2 = float(base_artifact["models"][target_f2]["validation"]["holdout_260805"]["p95"])

    def evaluate(rows: Sequence[core.Case]) -> dict[str, Any]:
        base_f1 = [predict_artifact_model(base_artifact["models"][target_f1], case, target_f1) for case in rows]
        base_f2 = [predict_artifact_model(base_artifact["models"][target_f2], case, target_f2) for case in rows]
        actual_f1 = [case.values[target_f1] for case in rows]
        actual_f2 = [case.values[target_f2] for case in rows]
        output: dict[str, Any] = {
            "n": len(rows),
            "baseline": {
                "f1": v6.metrics(rows, base_f1, target_f1),
                "f2": v6.metrics(rows, base_f2, target_f2),
            },
            "fixed_alloy_closest_state": {},
        }

        best_alloy_state_f1: list[float] = []
        best_alloy_state_f2: list[float] = []
        best_alloy_state_labels: list[tuple[str, str]] = []
        candidate_cache: list[list[tuple[float, str, str, float, float]]] = []
        for index, case in enumerate(rows):
            entry = case.values["entry_thickness_set_mode"]
            middle = case.values["intermediate_thickness_set_mode"]
            exit_value = case.values["exit_thickness_set_mode"]
            f1_end = math.log(entry / middle)
            total_end = math.log(entry / exit_value)
            candidates = []
            for alloy in MATERIAL_CURVES:
                for state in ("annealed", "unannealed"):
                    predicted_f1 = base_f1[index] * force_factor(alloy, state, 0.0, f1_end)
                    predicted_f2 = base_f2[index] * force_factor(alloy, state, f1_end, total_end)
                    score = (
                        abs(predicted_f1 - actual_f1[index]) / scale_f1
                        + abs(predicted_f2 - actual_f2[index]) / scale_f2
                    )
                    candidates.append((score, alloy, state, predicted_f1, predicted_f2))
            candidate_cache.append(candidates)
            _, alloy, state, predicted_f1, predicted_f2 = min(candidates)
            best_alloy_state_labels.append((alloy, state))
            best_alloy_state_f1.append(predicted_f1)
            best_alloy_state_f2.append(predicted_f2)

        for alloy in MATERIAL_CURVES:
            selected_f1 = []
            selected_f2 = []
            selected_states = []
            for candidates in candidate_cache:
                _, _, state, predicted_f1, predicted_f2 = min(
                    row for row in candidates if row[1] == alloy
                )
                selected_states.append(state)
                selected_f1.append(predicted_f1)
                selected_f2.append(predicted_f2)
            output["fixed_alloy_closest_state"][alloy] = {
                "f1": v6.metrics(rows, selected_f1, target_f1),
                "f2": v6.metrics(rows, selected_f2, target_f2),
                "state_counts": dict(Counter(selected_states)),
                "interpretation": "oracle closest-state upper bound; measured force was used to choose state",
            }

        output["closest_alloy_and_state"] = {
            "f1": v6.metrics(rows, best_alloy_state_f1, target_f1),
            "f2": v6.metrics(rows, best_alloy_state_f2, target_f2),
            "combination_counts": {
                f"{alloy}_{state}": count
                for (alloy, state), count in Counter(best_alloy_state_labels).items()
            },
            "interpretation": "oracle upper bound only; neither alloy nor state was recorded in DAT",
        }
        return output

    return {
        "all_555_descriptive": evaluate(cases),
        "holdout_260805": evaluate([case for case in cases if case.dataset == v6.HOLDOUT_DAY]),
    }


def build_material_artifact() -> dict[str, Any]:
    return {
        "schema_version": "cold-rolling-material-state-v0.6.1",
        "model_version": "literature-bounded-material-state-v0.6.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "provisional_physics_correction_requires_labelled_plant_validation",
        "reference": {
            "alloy": "1100",
            "incoming_state": "annealed",
            "reason": "central commercially-pure grade used only to anchor relative force sensitivity",
        },
        "incoming_state": {
            "annealed_initial_true_strain": 0.0,
            "unannealed_initial_true_strain": H14_EQUIVALENT_TRUE_STRAIN,
            "unannealed_assumption": "H14-equivalent fallback; binary hard state cannot distinguish the specific H temper",
            "initial_true_strain_by_temper": {
                "annealed": 0.0,
                "h14": H14_EQUIVALENT_TRUE_STRAIN,
                "h16": H16_EQUIVALENT_TRUE_STRAIN,
                "h18": H18_EQUIVALENT_TRUE_STRAIN,
                "h22": H12_EQUIVALENT_TRUE_STRAIN,
                "h24": H14_EQUIVALENT_TRUE_STRAIN,
                "h26": H16_EQUIVALENT_TRUE_STRAIN,
            },
            "temper_mapping_status": "H22/H24/H26 use H12/H14/H16 strength-level proxy only; engineering approximation pending alloy-temper-product data",
            "unknown_policy": "no material correction",
        },
        "force_correction": {
            "formula": "clip(mean_yield(alloy,state,strain_interval) / mean_yield(1100-O,route_strain_interval), 0.85, 1.15)",
            "minimum_multiplier": FORCE_MULTIPLIER_BOUNDS[0],
            "maximum_multiplier": FORCE_MULTIPLIER_BOUNDS[1],
            "scope": "F1/F2 predicted total force and derived side forces; no direct gap or tension scaling",
            "reason_for_bound": "DAT has no temper labels; keep the literature sensitivity inside the current force model's observed error scale",
        },
        "alloys": {
            alloy: {
                "yield_strength_curve_mpa": [[strain, strength] for strain, strength in curve],
                "temper_knots": ["O", "H12", "H14", "H16", "H18"],
            }
            for alloy, curve in MATERIAL_CURVES.items()
        },
        "sources": [
            {
                "title": "Properties of Wrought Aluminum and Aluminum Alloys",
                "url": "https://materialsdata.nist.gov/bitstream/handle/11115/179/Properties%20of%20Wrought%20Aluminum.pdf",
                "use": "typical O/H12/H14/H16/H18 yield strengths for 1060, 1100 and 3003",
            },
            {
                "title": "Introduction to Aluminum Alloys and Tempers",
                "url": "https://materialsdata.nist.gov/bitstream/handle/11115/186/Understanding%20Temper%20Designation.pdf",
                "use": "temper meaning and typical mechanical-property cross-check",
            },
            {
                "title": "GB/T 16475-2023",
                "url": "https://openstd.samr.gov.cn/bzgk/std/newGbInfo?hcno=F2A7A140EEAE49982CD4CD6F71CDC91C",
                "use": "Chinese wrought-aluminium temper designation system",
            },
            {
                "title": "ANSI H35.1/H35.1M 2024",
                "url": "https://www.aluminum.org/ansi-h351h351m",
                "use": "alloy and temper designation system; state code is not a universal rolling-force equation",
            },
            {
                "title": "Hydro aluminium temper designation chart",
                "url": "https://www.hydro.com/contentassets/14e9b5069c2e471186197a3d5de01033/langenuen-suspension-bridge-aluminium-girder-alternative.pdf",
                "use": "H2 means strain hardened and partially annealed; final digit 2/4/6 denotes quarter/half/three-quarter hard level",
            },
            {
                "title": "Physics-constrained cold-rolling force model",
                "url": "https://link.springer.com/article/10.1007/s10845-025-02729-8",
                "use": "rolling force depends on mean material flow stress across the pass",
            },
        ],
        "limitations": [
            "No validated alloy or incoming-temper labels exist in the current DAT foundation.",
            "The correction is a bounded engineering prior, not a learned causal effect.",
            "H22/H24/H26 share nominal strength levels with H12/H14/H16 but have different process histories; the proxy must not be treated as equivalence of process route or guaranteed properties.",
            "Gap correction requires mill modulus/zero convention; tension correction requires an approved stress policy.",
        ],
    }


def markdown_report(result: dict[str, Any]) -> str:
    f2 = result["f2_gap_validation"]
    mixture = result["latent_mixture_comparison"]
    all_rows = mixture["all_555_descriptive"]
    holdout_rows = mixture["holdout_260805"]
    lines = [
        "# v0.6.1 材料状态修正版验证报告",
        "",
        "## 发布结论",
        "",
        "- F2闭合辊缝采用‘原始辊缝模型60% + 辊缝厚度差模型40%’；该部分有高速DAT时间外推证据。",
        "- 中间厚度、三段张力、F1闭合辊缝继续使用v0.6模型，避免无证据替换。",
        "- 牌号/退火状态只修正预计轧制力和由此产生的道次风险；不直接缩放张力或辊缝。",
        "- DAT没有牌号/退火标签，因此材料修正结果只能做敏感性验证，不能宣称监督学习准确率。",
        "",
        "## F2闭合辊缝：可直接验证结果",
        "",
        "| 指标 | v0.6 | v0.6.1 | 变化 |",
        "|---|---:|---:|---:|",
    ]
    for label, section, key in (
        ("滚动验证平均MAE (mm)", "rolling", "equal_day_mae"),
        ("滚动验证最差日MAE (mm)", "rolling", "worst_day_mae"),
        ("滚动验证平均P95 (mm)", "rolling", "equal_day_p95"),
        ("260805 MAE (mm)", "holdout_260805", "mae"),
        ("260805 P95 (mm)", "holdout_260805", "p95"),
    ):
        before = f2[f"baseline_{section}"][key]
        after = f2[f"revised_{section}"][key]
        change = (after / before - 1.0) * 100
        lines.append(f"| {label} | {before:.6f} | {after:.6f} | {change:+.2f}% |")
    lines.extend([
        "",
        "## 退火/牌号修正：555段混合数据直接对比",
        "",
        "每一段同时计算退火态和未退火态，再用F1、F2实测轧制力选择更接近的一支。该结果能衡量两状态模型最多可以解释多少历史差异，但因为选择时看了实测轧制力，属于上限而不是上线准确率。",
        "",
        "| 数据 | 模型 | F1 MAE (T) | F2 MAE (T) |",
        "|---|---|---:|---:|",
        f"| 全部555段 | v0.6基线 | {all_rows['baseline']['f1']['mae']:.3f} | {all_rows['baseline']['f2']['mae']:.3f} |",
        f"| 全部555段 | 最接近牌号+状态（上限） | {all_rows['closest_alloy_and_state']['f1']['mae']:.3f} | {all_rows['closest_alloy_and_state']['f2']['mae']:.3f} |",
        f"| 260805留出日 | v0.6基线 | {holdout_rows['baseline']['f1']['mae']:.3f} | {holdout_rows['baseline']['f2']['mae']:.3f} |",
        f"| 260805留出日 | 最接近牌号+状态（上限） | {holdout_rows['closest_alloy_and_state']['f1']['mae']:.3f} | {holdout_rows['closest_alloy_and_state']['f2']['mae']:.3f} |",
        "",
        f"全部555段最佳匹配组合：{json.dumps(all_rows['closest_alloy_and_state']['combination_counts'], ensure_ascii=False)}。",
        "",
        "### 固定牌号、仅反推退火状态",
        "",
        "| 数据 | 固定牌号 | F1 MAE (T) | F2 MAE (T) | 反推退火/未退火段数 |",
        "|---|---|---:|---:|---:|",
    ])
    for dataset_label, dataset in (("全部555段", all_rows), ("260805留出日", holdout_rows)):
        for alloy in ("1060", "1100", "3003"):
            row = dataset["fixed_alloy_closest_state"][alloy]
            annealed = row["state_counts"].get("annealed", 0)
            unannealed = row["state_counts"].get("unannealed", 0)
            lines.append(
                f"| {dataset_label} | {alloy} | {row['f1']['mae']:.3f} | {row['f2']['mae']:.3f} | {annealed}/{unannealed} |"
            )
    lines.extend([
        "",
        "3003分支在无牌号数据上整体高估轧制力，不能由这批混合数据确认其准确性；这不等于3003文献强度关系错误，只说明缺少‘哪一段是3003’这一监督标签。1060/1100分支可明显缩小残差，说明牌号与来料状态值得作为正式输入。",
        "",
        "## 退火/牌号修正：验证边界",
        "",
        "强度曲线来自文献，轧制力修正限制在0.85—1.15倍。对260805留出日分别假设全部数据属于某一牌号/状态所得MAE，仅用于显示敏感度；由于真实标签缺失，不用于挑选哪一种状态。",
        "",
        "完整数值见 `validation.json`。正式确认需要新增卷的牌号与来料状态标签。",
        "",
    ])
    return "\n".join(lines)


def main() -> int:
    configure()
    paths = sorted((WORKSPACE_ROOT / "outputs").glob("data_foundation*/production_segments.csv"))
    cases = v6.load_cases(paths)
    augment(cases)
    base_artifact = json.loads(BASE_MODEL.read_text(encoding="utf-8"))
    f2_validation = evaluate_f2(cases)

    revised = copy.deepcopy(base_artifact)
    revised["model_version"] = "high-speed-material-state-v0.6.1"
    revised["generated_at"] = datetime.now(timezone.utc).isoformat()
    revised["material_state_artifact"] = "material_state_v061.json"
    raw_f2 = copy.deepcopy(base_artifact["models"]["tm2_closed_gap_set_median"])
    raw_f2["label"] = "F2闭合辊缝位置设定"
    offset = serialize_offset(f2_validation["offset_model"], f2_validation["development_count"])
    revised_f2 = copy.deepcopy(raw_f2)
    revised_f2.update({
        "model_type": "weighted_ensemble",
        "label": "F2闭合辊缝位置设定",
        "components": [
            {"weight": 1.0 - F2_BLEND_WEIGHT, "postprocess": "raw", "model": raw_f2},
            {"weight": F2_BLEND_WEIGHT, "postprocess": "add_exit_thickness", "model": offset},
        ],
        "validation": {
            "rolling": f2_validation["revised_rolling"],
            "holdout_260805": f2_validation["revised_holdout_260805"],
        },
        "selection": {
            "source": "v0.6 raw-gap plus residual-gap ensemble",
            "weight_raw": 1.0 - F2_BLEND_WEIGHT,
            "weight_gap_minus_exit": F2_BLEND_WEIGHT,
        },
    })
    revised["models"]["tm1_closed_gap_set_median"]["label"] = "F1闭合辊缝位置设定"
    revised["models"]["tm2_closed_gap_set_median"] = revised_f2

    result = {
        "generated_at": revised["generated_at"],
        "case_count": len(cases),
        "label_availability": {"alloy": False, "incoming_anneal_state": False},
        "f2_gap_validation": {
            key: value for key, value in f2_validation.items() if key != "offset_model"
        },
        "material_force_sensitivity_holdout_260805": material_sensitivity(cases, base_artifact),
        "latent_mixture_comparison": latent_mixture_comparison(cases, base_artifact),
        "release_gate": {
            "directly_validated_change": "F2 closed-gap ensemble",
            "neutral_dat_path_non_degradation": True,
            "material_correction_status": "provisional sensitivity pending labelled coils",
        },
    }
    material = build_material_artifact()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for path, payload in (
        (OUTPUT_DIR / "route_model_v061.json", revised),
        (MODEL_OUTPUT, revised),
        (OUTPUT_DIR / "material_state_v061.json", material),
        (MATERIAL_OUTPUT, material),
        (OUTPUT_DIR / "validation.json", result),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (OUTPUT_DIR / "validation_report.md").write_text(markdown_report(result), encoding="utf-8")
    print(json.dumps({
        "cases": len(cases),
        "f2_baseline_holdout": f2_validation["baseline_holdout_260805"],
        "f2_revised_holdout": f2_validation["revised_holdout_260805"],
        "outputs": [str(MODEL_OUTPUT), str(MATERIAL_OUTPUT), str(OUTPUT_DIR / "validation_report.md")],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
