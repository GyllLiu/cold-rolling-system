#!/usr/bin/env python3
"""Train a direct cold-rolling setup predictor and seal 260719 as final test.

The deployed predictor is a regularized polynomial regression.  At inference it
evaluates a fixed coefficient equation; it does not search historical cases,
average neighbours, or copy a historical recipe.

Model choices are made with rolling-origin folds ending at 260718.  The 260719
dataset is loaded only after every target configuration has been frozen.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import statistics
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[1]

SPEC_FEATURES = (
    "entry_thickness_set_mode",
    "exit_thickness_set_mode",
    "width_set_mode",
    "total_reduction_ratio",
)
ACTUAL_FEATURES = SPEC_FEATURES + ("entry_thickness_actual_median",)
FEATURE_SETS = {
    "spec4": SPEC_FEATURES,
    "spec5": ACTUAL_FEATURES,
}


@dataclass(frozen=True)
class TargetSpec:
    code: str
    label: str
    unit: str
    role: str
    priority: str
    hard_min: float | None = None
    hard_max: float | None = None


TARGET_SPECS = (
    TargetSpec(
        "intermediate_thickness_set_mode",
        "中间厚度直接预测",
        "mm",
        "thickness_control",
        "primary",
        0.01,
        20.0,
    ),
    TargetSpec(
        "tm1_gap_command_median",
        "F1综合辊缝直接预测",
        "μm",
        "machine_setup",
        "primary",
        0.0,
        20000.0,
    ),
    TargetSpec(
        "tm2_gap_command_median",
        "F2综合辊缝直接预测",
        "μm",
        "machine_setup",
        "primary",
        0.0,
        20000.0,
    ),
    TargetSpec(
        "uncoiler_tension_set_median",
        "开卷张力直接预测",
        "",
        "machine_setup",
        "secondary",
        0.0,
        None,
    ),
    TargetSpec(
        "interstand_tension_set_median",
        "机架张力直接预测",
        "",
        "machine_setup",
        "secondary",
        0.0,
        None,
    ),
    TargetSpec(
        "coiler_tension_set_median",
        "卷取张力直接预测",
        "",
        "machine_setup",
        "secondary",
        0.0,
        None,
    ),
    TargetSpec(
        "f1_bending_set_median",
        "F1弯辊力直接预测",
        "T",
        "machine_setup",
        "secondary",
    ),
    TargetSpec(
        "f2_bending_set_median",
        "F2弯辊力直接预测",
        "T",
        "machine_setup",
        "secondary",
    ),
    TargetSpec(
        "tm1_tilt_set_median",
        "F1倾斜量直接预测",
        "μm",
        "machine_setup",
        "secondary",
    ),
    TargetSpec(
        "tm2_tilt_set_median",
        "F2倾斜量直接预测",
        "μm",
        "machine_setup",
        "secondary",
    ),
    TargetSpec(
        "main_speed_set_median",
        "高速主速度直接预测",
        "m/min",
        "secondary_reference",
        "secondary",
        0.0,
        None,
    ),
    TargetSpec(
        "f1_roll_force_sum_median",
        "F1轧制力预计",
        "T",
        "monitor_only",
        "monitor",
        0.0,
        None,
    ),
    TargetSpec(
        "f2_roll_force_sum_median",
        "F2轧制力预计",
        "T",
        "monitor_only",
        "monitor",
        0.0,
        None,
    ),
)
TARGET_BY_CODE = {target.code: target for target in TARGET_SPECS}

DAY_ORDER = ("260712", "260713", "260714", "260716", "260717", "260718")
ROLLING_FOLDS = (
    (("260712", "260713"), "260714"),
    (("260712", "260713", "260714"), "260716"),
    (("260712", "260713", "260714", "260716"), "260717"),
    (("260712", "260713", "260714", "260716", "260717"), "260718"),
)


@dataclass(frozen=True)
class Case:
    dataset: str
    candidate_id: str
    start_time: str
    source_files: str
    values: dict[str, float]


@dataclass(frozen=True)
class ModelConfig:
    feature_set: str
    degree: int
    ridge_lambda: float
    target_transform: str


@dataclass
class FittedModel:
    config: ModelConfig
    feature_names: tuple[str, ...]
    feature_mean: list[float]
    feature_scale: list[float]
    basis_terms: list[list[int]]
    coefficients: list[float]


def _finite_float(row: dict[str, str], key: str) -> float:
    value = float(row[key])
    if not math.isfinite(value):
        raise ValueError(f"non-finite {key}")
    return value


def load_cases(paths: Iterable[Path]) -> list[Case]:
    required = set(ACTUAL_FEATURES) | set(TARGET_BY_CODE)
    cases: list[Case] = []
    for path in paths:
        dataset = path.parent.name.removeprefix("data_foundation_")
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            missing_columns = required - set(reader.fieldnames or ())
            if missing_columns:
                raise ValueError(
                    f"{path} is missing columns: {', '.join(sorted(missing_columns))}"
                )
            for row in reader:
                eligible = row.get("learning_eligible", "").lower() == "true"
                if not row.get("learning_eligible"):
                    eligible = row.get("confidence") == "medium_plus"
                if not eligible:
                    continue
                try:
                    values = {key: _finite_float(row, key) for key in required}
                except (KeyError, TypeError, ValueError):
                    continue
                cases.append(
                    Case(
                        dataset=dataset,
                        candidate_id=f"{dataset}_{row['candidate_id']}",
                        start_time=row.get("start_time", ""),
                        source_files=row.get("source_files", ""),
                        values=values,
                    )
                )
    return cases


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _std(values: Sequence[float], mean: float) -> float:
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return max(math.sqrt(variance), 1e-9)


def _basis_terms(feature_count: int, degree: int) -> list[list[int]]:
    terms = [[index] for index in range(feature_count)]
    if degree == 2:
        terms.extend(
            [left, right]
            for left in range(feature_count)
            for right in range(left, feature_count)
        )
    return terms


def _basis_row(
    values: dict[str, float],
    feature_names: Sequence[str],
    means: Sequence[float],
    scales: Sequence[float],
    terms: Sequence[Sequence[int]],
) -> list[float]:
    standardized = [
        (values[name] - mean) / scale
        for name, mean, scale in zip(feature_names, means, scales)
    ]
    row = [1.0]
    for term in terms:
        product = 1.0
        for index in term:
            product *= standardized[index]
        row.append(product)
    return row


def _solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [matrix[row][:] + [vector[row]] for row in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("singular ridge system")
        if pivot != column:
            augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor == 0:
                continue
            augmented[row] = [
                current - factor * pivot_value
                for current, pivot_value in zip(augmented[row], augmented[column])
            ]
    return [augmented[row][-1] for row in range(size)]


def _encode_target(case: Case, target: str, transform: str) -> float:
    if transform == "raw":
        return case.values[target]
    if transform == "reduction_share":
        entry = case.values["entry_thickness_set_mode"]
        exit_value = case.values["exit_thickness_set_mode"]
        return (entry - case.values[target]) / (entry - exit_value)
    raise ValueError(f"unknown target transform {transform}")


def _decode_target(
    encoded: float, values: dict[str, float], target: str, transform: str
) -> float:
    if transform == "raw":
        result = encoded
    elif transform == "reduction_share":
        entry = values["entry_thickness_set_mode"]
        exit_value = values["exit_thickness_set_mode"]
        result = entry - encoded * (entry - exit_value)
    else:
        raise ValueError(f"unknown target transform {transform}")

    spec = TARGET_BY_CODE[target]
    if target == "intermediate_thickness_set_mode":
        result = min(
            values["entry_thickness_set_mode"] - 0.001,
            max(values["exit_thickness_set_mode"] + 0.001, result),
        )
    if spec.hard_min is not None:
        result = max(spec.hard_min, result)
    if spec.hard_max is not None:
        result = min(spec.hard_max, result)
    return result


def fit_model(cases: Sequence[Case], target: str, config: ModelConfig) -> FittedModel:
    feature_names = FEATURE_SETS[config.feature_set]
    columns = [[case.values[name] for case in cases] for name in feature_names]
    means = [_mean(column) for column in columns]
    scales = [_std(column, mean) for column, mean in zip(columns, means)]
    terms = _basis_terms(len(feature_names), config.degree)
    design = [
        _basis_row(case.values, feature_names, means, scales, terms)
        for case in cases
    ]
    response = [_encode_target(case, target, config.target_transform) for case in cases]
    dimension = len(design[0])
    gram = [[0.0] * dimension for _ in range(dimension)]
    rhs = [0.0] * dimension
    for row, output in zip(design, response):
        for left in range(dimension):
            rhs[left] += row[left] * output
            for right in range(left, dimension):
                gram[left][right] += row[left] * row[right]
    for left in range(dimension):
        for right in range(left):
            gram[left][right] = gram[right][left]
        if left:
            gram[left][left] += config.ridge_lambda
    coefficients = _solve_linear_system(gram, rhs)
    return FittedModel(
        config=config,
        feature_names=feature_names,
        feature_mean=means,
        feature_scale=scales,
        basis_terms=terms,
        coefficients=coefficients,
    )


def predict_model(model: FittedModel, case: Case, target: str) -> float:
    row = _basis_row(
        case.values,
        model.feature_names,
        model.feature_mean,
        model.feature_scale,
        model.basis_terms,
    )
    encoded = sum(value * coefficient for value, coefficient in zip(row, model.coefficients))
    return _decode_target(
        encoded, case.values, target, model.config.target_transform
    )


def _percentile(values: Sequence[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return math.nan
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    lower = int(position)
    upper = min(len(ordered) - 1, lower + 1)
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _metric_summary(actual: Sequence[float], predicted: Sequence[float]) -> dict[str, float]:
    errors = [prediction - value for value, prediction in zip(actual, predicted)]
    absolute = [abs(error) for error in errors]
    mean_actual = _mean(actual)
    denominator = sum((value - mean_actual) ** 2 for value in actual)
    r2 = (
        1 - sum(error * error for error in errors) / denominator
        if denominator > 1e-12
        else math.nan
    )
    return {
        "mae": _mean(absolute),
        "rmse": math.sqrt(_mean([error * error for error in errors])),
        "p80_absolute_error": _percentile(absolute, 0.80),
        "p95_absolute_error": _percentile(absolute, 0.95),
        "bias": _mean(errors),
        "r2": r2,
    }


def _candidate_configs(target: str) -> Iterable[ModelConfig]:
    transforms = ("raw", "reduction_share") if target == "intermediate_thickness_set_mode" else ("raw",)
    for feature_set, degree, ridge_lambda, transform in itertools.product(
        FEATURE_SETS,
        (1, 2),
        (0.01, 0.1, 1.0, 10.0, 100.0),
        transforms,
    ):
        yield ModelConfig(feature_set, degree, ridge_lambda, transform)


def _rolling_errors(
    cases: Sequence[Case], target: str, config: ModelConfig
) -> tuple[list[float], list[dict[str, float | str | int]]]:
    all_errors: list[float] = []
    fold_rows: list[dict[str, float | str | int]] = []
    for train_days, validation_day in ROLLING_FOLDS:
        train = [case for case in cases if case.dataset in train_days]
        validation = [case for case in cases if case.dataset == validation_day]
        model = fit_model(train, target, config)
        actual = [case.values[target] for case in validation]
        predicted = [predict_model(model, case, target) for case in validation]
        errors = [abs(prediction - value) for value, prediction in zip(actual, predicted)]
        all_errors.extend(errors)
        fold_rows.append(
            {
                "validation_dataset": validation_day,
                "training_case_count": len(train),
                "validation_case_count": len(validation),
                "mae": _mean(errors),
            }
        )
    return all_errors, fold_rows


def select_config(
    development_cases: Sequence[Case], target: str
) -> tuple[ModelConfig, list[float], list[dict[str, float | str | int]]]:
    best: tuple[
        float,
        float,
        int,
        float,
        ModelConfig,
        list[float],
        list[dict[str, float | str | int]],
    ] | None = None
    for config in _candidate_configs(target):
        errors, folds = _rolling_errors(development_cases, target, config)
        equal_day_mae = _mean([float(row["mae"]) for row in folds])
        pooled_mae = _mean(errors)
        complexity = len(_basis_terms(len(FEATURE_SETS[config.feature_set]), config.degree))
        score = (
            equal_day_mae,
            pooled_mae,
            complexity,
            -config.ridge_lambda,
            config,
            errors,
            folds,
        )
        if best is None or score[:4] < best[:4]:
            best = score
    if best is None:
        raise ValueError(f"no configuration for {target}")
    return best[4], best[5], best[6]


def _nearest_median_baseline(
    train: Sequence[Case], queries: Sequence[Case], target: str, k: int = 5
) -> list[float]:
    means = {
        name: _mean([case.values[name] for case in train])
        for name in SPEC_FEATURES
    }
    scales = {
        name: _std([case.values[name] for case in train], means[name])
        for name in SPEC_FEATURES
    }

    def distance(left: Case, right: Case) -> float:
        return math.sqrt(
            _mean(
                [
                    ((left.values[name] - right.values[name]) / scales[name]) ** 2
                    for name in SPEC_FEATURES
                ]
            )
        )

    predictions = []
    for query in queries:
        nearest = sorted(train, key=lambda item: distance(query, item))[:k]
        predictions.append(statistics.median(case.values[target] for case in nearest))
    return predictions


def _round_metrics(metrics: dict[str, float]) -> dict[str, float | None]:
    return {
        key: None if not math.isfinite(value) else round(value, 6)
        for key, value in metrics.items()
    }


def _model_to_dict(
    model: FittedModel,
    target: TargetSpec,
    rolling_errors: Sequence[float],
    rolling_folds: Sequence[dict[str, float | str | int]],
    final_metrics: dict[str, float],
    baseline_metrics: dict[str, float],
) -> dict:
    improvement = (
        (baseline_metrics["mae"] - final_metrics["mae"])
        / baseline_metrics["mae"]
        * 100
        if baseline_metrics["mae"]
        else 0.0
    )
    if target.role == "monitor_only":
        reliability = "monitor"
    elif target.code == "intermediate_thickness_set_mode":
        reliability = "high"
    elif (
        improvement < 0
        or final_metrics["r2"] < 0.35
        or target.code in {"tm1_tilt_set_median", "tm2_tilt_set_median"}
    ):
        reliability = "low"
    else:
        reliability = "medium"
    return {
        "label": target.label,
        "unit": target.unit,
        "role": target.role,
        "priority": target.priority,
        "reliability": reliability,
        "feature_set": model.config.feature_set,
        "feature_names": list(model.feature_names),
        "degree": model.config.degree,
        "ridge_lambda": model.config.ridge_lambda,
        "target_transform": model.config.target_transform,
        "feature_mean": [round(value, 12) for value in model.feature_mean],
        "feature_scale": [round(value, 12) for value in model.feature_scale],
        "basis_terms": model.basis_terms,
        "coefficients": [round(value, 12) for value in model.coefficients],
        "hard_min": target.hard_min,
        "hard_max": target.hard_max,
        "rolling_origin": {
            "evaluated_case_count": len(rolling_errors),
            "mae": round(_mean(rolling_errors), 6),
            "p80_absolute_error": round(_percentile(rolling_errors, 0.80), 6),
            "p95_absolute_error": round(_percentile(rolling_errors, 0.95), 6),
            "folds": [
                {
                    **row,
                    "mae": round(float(row["mae"]), 6),
                }
                for row in rolling_folds
            ],
        },
        "prediction_interval": {
            "method": "absolute rolling-origin residual",
            "coverage": 0.80,
            "half_width": round(_percentile(rolling_errors, 0.80), 6),
            "p95_half_width": round(_percentile(rolling_errors, 0.95), 6),
        },
        "final_test": {
            "direct": _round_metrics(final_metrics),
            "historical_5nn_median_baseline": _round_metrics(baseline_metrics),
            "mae_improvement_percent": round(improvement, 3),
        },
    }


def _input_coverage(cases: Sequence[Case]) -> dict[str, dict[str, float]]:
    coverage = {}
    for feature in ACTUAL_FEATURES:
        values = [case.values[feature] for case in cases]
        coverage[feature] = {
            "min": round(min(values), 6),
            "p01": round(_percentile(values, 0.01), 6),
            "p99": round(_percentile(values, 0.99), 6),
            "max": round(max(values), 6),
        }
    return coverage


def _exit_quality_summary(test_csv: Path) -> dict[str, float | int]:
    errors = []
    relative = []
    with test_csv.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("learning_eligible", "").lower() != "true":
                continue
            try:
                absolute = abs(float(row["exit_thickness_error_abs_p95"]))
                target = float(row["exit_thickness_set_mode"])
            except (KeyError, TypeError, ValueError):
                continue
            errors.append(absolute)
            relative.append(absolute / target * 100)
    return {
        "case_count": len(errors),
        "median_segment_p95_absolute_error_mm": round(statistics.median(errors), 6),
        "median_segment_p95_relative_error_percent": round(
            statistics.median(relative), 6
        ),
        "worst_segment_p95_relative_error_percent": round(max(relative), 6),
    }


def _exact_spec_overlap(
    training: Sequence[Case], test: Sequence[Case]
) -> dict[str, float | int]:
    keys = {
        tuple(case.values[name] for name in SPEC_FEATURES[:3])
        for case in training
    }
    overlap = sum(
        tuple(case.values[name] for name in SPEC_FEATURES[:3]) in keys
        for case in test
    )
    return {
        "test_case_count": len(test),
        "exact_spec_overlap_count": overlap,
        "exact_spec_overlap_percent": round(overlap / len(test) * 100, 3),
    }


def build_report(artifact: dict) -> str:
    lines = [
        "# 冷二联直接预测模型 v0.3 训练与封存验证报告",
        "",
        "## 结论",
        "",
        "- 线上结果已改为固定系数的正则化多项式回归；推理时不检索历史卷、不做近邻加权或历史均值。",
        f"- 训练数据：`{'、'.join(artifact['training_datasets'])}`，高速稳定段 `{artifact['training_case_count']}` 条。",
        f"- 最终封存测试：`{artifact['validation_dataset']}`，高速稳定段 `{artifact['validation_case_count']}` 条；该日未参与特征、阶数、正则强度或目标变换选择。",
        f"- 0719 与训练集完全相同规格仅 `{artifact['spec_overlap']['exact_spec_overlap_count']}/{artifact['spec_overlap']['test_case_count']}` 条（`{artifact['spec_overlap']['exact_spec_overlap_percent']:.1f}%`），因此主要检验的是跨规格预测。",
        f"- 直接模型在 `{artifact['summary']['direct_win_count']}/{artifact['summary']['target_count']}` 个目标上优于历史 5 近邻中位数基线。",
        "",
        "模型形式：先标准化入口设定厚度、出口目标厚度、宽度、总压下率（部分目标再加入入口实测厚度），再计算一次/二次项并与冻结系数点乘。预测结果由方程直接产生。",
        "",
        "## 日期隔离方案",
        "",
        "| 轮次 | 训练日期 | 只用于验证的下一日期 |",
        "| --- | --- | --- |",
    ]
    for fold in artifact["development_folds"]:
        lines.append(
            f"| {fold['index']} | {'、'.join(fold['training_datasets'])} | {fold['validation_dataset']} |"
        )
    lines.extend(
        [
            "",
            "以上四个滚动时间折只用于选择每个输出的特征集、一次/二次模型和正则强度。配置冻结后，才读取 0719 做一次最终测试。",
            "",
            "## 0719 最终测试",
            "",
            "| 参数 | 角色 | 直接预测 MAE | 历史5NN MAE | MAE变化 | 直接预测 P95误差 | 模型 |",
            "| --- | --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    role_labels = {
        "thickness_control": "厚度核心",
        "machine_setup": "机器设定",
        "secondary_reference": "次要参考",
        "monitor_only": "负荷校核",
    }
    for spec in TARGET_SPECS:
        model = artifact["models"][spec.code]
        direct = model["final_test"]["direct"]
        baseline = model["final_test"]["historical_5nn_median_baseline"]
        change = model["final_test"]["mae_improvement_percent"]
        suffix = f" {spec.unit}" if spec.unit else ""
        lines.append(
            f"| {spec.label} | {role_labels[spec.role]} | "
            f"{direct['mae']:.4f}{suffix} | {baseline['mae']:.4f}{suffix} | "
            f"{change:+.1f}% | {direct['p95_absolute_error']:.4f}{suffix} | "
            f"{model['feature_set']}/d{model['degree']}/λ={model['ridge_lambda']:g}"
            f"{'/压下分配率' if model['target_transform'] == 'reduction_share' else ''} |"
        )
    quality = artifact["exit_quality_observation"]
    lines.extend(
        [
            "",
            "表中“MAE变化”为相对历史近邻基线的改善，正数表示直接预测误差更小。P95 是 0719 绝对误差的第 95 百分位，不是产品公差。",
            "",
            "## 与出口厚度达标的关系",
            "",
            f"- 0719 高速稳定段中，单段出口厚差 P95 的中位数为 `{quality['median_segment_p95_absolute_error_mm']:.4f} mm`，相对目标厚度中位数为 `{quality['median_segment_p95_relative_error_percent']:.3f}%`，最差单段为 `{quality['worst_segment_p95_relative_error_percent']:.3f}%`。",
            "- 这些数值证明训练目标来自已经稳定生产的区间，但不能证明“模型下发后”的成品厚度准确率；当前没有模型建议实际下发、再由人工/在线测厚回传的闭环标签。",
            "- 第一优先级因此是中间厚度与两机架综合辊缝；速度只是稳定生产参考，不参与厚度目标加权。",
            "",
            "## 使用边界",
            "",
            "- 输出区间来自四个滚动时间验证折的残差分位数，不是历史邻居范围。",
            "- 输入超出训练数据最小/最大范围时，接口拒绝给出可执行建议；靠近 1%/99% 边界时降为低置信参考。",
            "- 张力、弯辊和倾斜量跨日漂移较大，必须看各自的 0719 MAE 与 P95；误差较大的项只允许主操人工确认。",
            "- F1/F2轧制力是结果校核量，不是建议写入量。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--artifact",
        type=Path,
        default=PROJECT_ROOT
        / "backend/app/model_artifacts/recommendation_direct_v3.json",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=WORKSPACE_ROOT / "outputs/recommendation_model_v3_report.md",
    )
    parser.add_argument(
        "--test-csv",
        type=Path,
        default=WORKSPACE_ROOT
        / "outputs/data_foundation_260719/production_segments.csv",
    )
    args = parser.parse_args()
    development_paths = [
        WORKSPACE_ROOT
        / f"outputs/data_foundation_{day}/production_segments.csv"
        for day in DAY_ORDER
    ]
    for path in [*development_paths, args.test_csv]:
        if not path.exists():
            parser.error(f"missing input: {path}")

    development_cases = load_cases(development_paths)
    if len(development_cases) < 100:
        parser.error("fewer than 100 eligible development cases")

    selected: dict[
        str,
        tuple[ModelConfig, list[float], list[dict[str, float | str | int]]],
    ] = {}
    for index, target in enumerate(TARGET_SPECS, start=1):
        print(f"[{index}/{len(TARGET_SPECS)}] selecting {target.code}", flush=True)
        selected[target.code] = select_config(development_cases, target.code)

    # 260719 is deliberately not opened before every configuration is frozen.
    test_cases = load_cases([args.test_csv])
    if not test_cases:
        parser.error("no eligible final-test cases")

    models: dict[str, dict] = {}
    direct_wins = 0
    improvements = []
    for target in TARGET_SPECS:
        config, rolling_errors, rolling_folds = selected[target.code]
        model = fit_model(development_cases, target.code, config)
        actual = [case.values[target.code] for case in test_cases]
        direct = [predict_model(model, case, target.code) for case in test_cases]
        baseline = _nearest_median_baseline(
            development_cases, test_cases, target.code
        )
        direct_metrics = _metric_summary(actual, direct)
        baseline_metrics = _metric_summary(actual, baseline)
        if direct_metrics["mae"] < baseline_metrics["mae"]:
            direct_wins += 1
        if baseline_metrics["mae"]:
            improvements.append(
                (baseline_metrics["mae"] - direct_metrics["mae"])
                / baseline_metrics["mae"]
                * 100
            )
        models[target.code] = _model_to_dict(
            model,
            target,
            rolling_errors,
            rolling_folds,
            direct_metrics,
            baseline_metrics,
        )

    artifact = {
        "model_version": "direct-ridge-high-speed-v0.3",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_family": "standardized polynomial ridge regression",
        "inference_method": "fixed coefficient equation; no neighbour lookup or averaging",
        "training_datasets": list(DAY_ORDER),
        "validation_dataset": args.test_csv.parent.name.removeprefix(
            "data_foundation_"
        ),
        "training_case_count": len(development_cases),
        "validation_case_count": len(test_cases),
        "development_folds": [
            {
                "index": index,
                "training_datasets": list(train_days),
                "validation_dataset": validation_day,
            }
            for index, (train_days, validation_day) in enumerate(
                ROLLING_FOLDS, start=1
            )
        ],
        "training_policy": {
            "learning_eligible_only": True,
            "high_speed_plateau_only": True,
            "low_speed_role": "operator_tuning_excluded_from_targets",
            "model_selection": "four rolling-origin date folds",
            "validation_used_for_tuning": False,
            "historical_neighbour_method_used_at_inference": False,
        },
        "input_features": list(ACTUAL_FEATURES),
        "input_coverage": _input_coverage(development_cases),
        "models": models,
        "spec_overlap": _exact_spec_overlap(development_cases, test_cases),
        "exit_quality_observation": _exit_quality_summary(args.test_csv),
        "summary": {
            "target_count": len(TARGET_SPECS),
            "direct_win_count": direct_wins,
            "mean_mae_improvement_percent": round(_mean(improvements), 3),
            "historical_baseline": "standardized spec 5-nearest median; evaluation only",
        },
    }
    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    args.artifact.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(build_report(artifact), encoding="utf-8")
    print(
        f"model={artifact['model_version']} train={len(development_cases)} "
        f"test={len(test_cases)} direct_wins={direct_wins}/{len(TARGET_SPECS)}"
    )
    print(f"artifact={args.artifact.resolve()}")
    print(f"report={args.report.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
