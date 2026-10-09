#!/usr/bin/env python3
"""Train and validate the v0.4 startup predictor and flatness monitor.

v0.4 deliberately separates two causal moments:

1. Cold start: predict only the six values the chief operator enters before a run.
2. Runtime: compare the 49-zone AFC profile with a specification-conditioned
   observational baseline and show descriptive control associations.

All online predictions are fixed ridge equations.  Historical neighbours are used
only as a benchmark in this training report, never at inference.
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

import train_validate_direct_prediction as core


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEVELOPMENT_DAYS = ("260712", "260713", "260714", "260716", "260717", "260718")
FINAL_TEST_DAY = "260719"
ROLLING_FOLDS = (
    (("260712", "260713"), "260714"),
    (("260712", "260713", "260714"), "260716"),
    (("260712", "260713", "260714", "260716"), "260717"),
    (("260712", "260713", "260714", "260716", "260717"), "260718"),
)

RAW_SPEC_FEATURES = (
    "entry_thickness_set_mode",
    "exit_thickness_set_mode",
    "width_set_mode",
    "total_reduction_ratio",
)
RAW_INPUT_FEATURES = RAW_SPEC_FEATURES + ("entry_thickness_actual_median",)
PHYSICS_FEATURES = RAW_SPEC_FEATURES + (
    "log_thickness_ratio",
    "entry_cross_section_proxy",
    "exit_cross_section_proxy",
)
STARTUP_FEATURE_SETS = {
    # The selector is allowed to reject width entirely when it does not improve
    # date- and specification-held-out error.
    "thickness2": RAW_SPEC_FEATURES[:2],
    "spec4": RAW_SPEC_FEATURES,
    "physics7": PHYSICS_FEATURES,
    "physics8_actual": PHYSICS_FEATURES + ("entry_thickness_actual_median",),
}

STARTUP_TARGETS = (
    core.TargetSpec(
        "intermediate_thickness_set_mode",
        "中间厚度首设",
        "mm",
        "thickness_control",
        "primary",
        0.01,
        20.0,
    ),
    core.TargetSpec(
        "tm1_closed_gap_set_median",
        "F1高速稳定运行辊缝参考",
        "mm",
        "machine_setup",
        "primary",
        0.0,
        20.0,
    ),
    core.TargetSpec(
        "tm2_closed_gap_set_median",
        "F2高速稳定运行辊缝参考",
        "mm",
        "machine_setup",
        "primary",
        0.0,
        20.0,
    ),
    core.TargetSpec(
        "uncoiler_tension_set_median",
        "开卷张力首设",
        "T",
        "machine_setup",
        "secondary",
        0.0,
        None,
    ),
    core.TargetSpec(
        "interstand_tension_set_median",
        "机架张力首设",
        "T",
        "machine_setup",
        "secondary",
        0.0,
        None,
    ),
    core.TargetSpec(
        "coiler_tension_set_median",
        "卷取张力首设",
        "T",
        "machine_setup",
        "secondary",
        0.0,
        None,
    ),
)

FLATNESS_TARGETS = (
    core.TargetSpec(
        "flatness_std_median",
        "横向离散基线",
        "DAT原始值",
        "flatness_monitor",
        "monitor",
        0.0,
        None,
    ),
    core.TargetSpec(
        "flatness_lr_asymmetry_median",
        "左右不对称基线",
        "DAT原始值",
        "flatness_monitor",
        "monitor",
        None,
        None,
    ),
    core.TargetSpec(
        "flatness_edge_minus_center_median",
        "边部－中部基线",
        "DAT原始值",
        "flatness_monitor",
        "monitor",
        None,
        None,
    ),
    core.TargetSpec(
        "flatness_temporal_std_median",
        "版型时序波动基线",
        "DAT原始值",
        "flatness_monitor",
        "monitor",
        0.0,
        None,
    ),
)

CONTROL_LABELS = {
    "uncoiler_tension_set_median": "开卷张力设定",
    "interstand_tension_set_median": "机架张力设定",
    "coiler_tension_set_median": "卷取张力设定",
    "f1_force_drive_minus_operation_median": "F1传动侧－操作侧轧制力",
    "f2_force_drive_minus_operation_median": "F2传动侧－操作侧轧制力",
    "f1_gap_drive_minus_operation_median": "F1传动侧－操作侧辊缝",
    "f2_gap_drive_minus_operation_median": "F2传动侧－操作侧辊缝",
    "f1_positive_bending_actual_median": "F1实际正弯辊",
    "f1_negative_bending_actual_median": "F1实际负弯辊",
    "f2_positive_bending_actual_median": "F2实际正弯辊",
    "f2_negative_bending_actual_median": "F2实际负弯辊",
    "tm1_tilt_adjustment_median": "F1倾斜调节量",
    "tm2_tilt_adjustment_median": "F2倾斜调节量",
}


@dataclass(frozen=True)
class Selection:
    config: core.ModelConfig
    rolling_errors: list[float]
    rolling_folds: list[dict[str, float | str | int]]
    grouped_errors: list[float]
    grouped_folds: list[dict[str, float | str | int]]
    score: float


def _finite(row: dict[str, str], name: str) -> float:
    value = float(row[name])
    if not math.isfinite(value):
        raise ValueError(name)
    return value


def _derived_values(row: dict[str, str]) -> dict[str, float]:
    entry = _finite(row, "entry_thickness_set_mode")
    exit_value = _finite(row, "exit_thickness_set_mode")
    width = _finite(row, "width_set_mode")
    if entry <= 0 or exit_value <= 0:
        raise ValueError("thickness must be positive")
    return {
        "entry_thickness_set_mode": entry,
        "exit_thickness_set_mode": exit_value,
        "width_set_mode": width,
        "total_reduction_ratio": _finite(row, "total_reduction_ratio"),
        "entry_thickness_actual_median": _finite(
            row, "entry_thickness_actual_median"
        ),
        "log_thickness_ratio": math.log(entry / exit_value),
        "entry_cross_section_proxy": entry * width,
        "exit_cross_section_proxy": exit_value * width,
    }


def load_feature_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"empty feature table: {path}")
    return rows


def make_cases(
    rows: Sequence[dict[str, str]], targets: Sequence[core.TargetSpec]
) -> list[core.Case]:
    cases = []
    target_names = [target.code for target in targets]
    for row in rows:
        try:
            values = _derived_values(row)
            values.update({name: _finite(row, name) for name in target_names})
        except (KeyError, TypeError, ValueError):
            continue
        dataset = row["dataset"]
        cases.append(
            core.Case(
                dataset=dataset,
                candidate_id=f"{dataset}_{row.get('candidate_id', '')}",
                start_time=row.get("start_time", ""),
                source_files=row.get("source_files", ""),
                values=values,
            )
        )
    return cases


def configure_core(
    targets: Sequence[core.TargetSpec], feature_sets: dict[str, tuple[str, ...]]
) -> None:
    core.SPEC_FEATURES = RAW_SPEC_FEATURES
    core.ACTUAL_FEATURES = tuple(dict.fromkeys(itertools.chain.from_iterable(feature_sets.values())))
    core.FEATURE_SETS = feature_sets
    core.TARGET_SPECS = tuple(targets)
    core.TARGET_BY_CODE = {target.code: target for target in targets}
    core.DAY_ORDER = DEVELOPMENT_DAYS
    core.ROLLING_FOLDS = ROLLING_FOLDS


def _spec_key(case: core.Case) -> tuple[float, float, float]:
    return tuple(case.values[name] for name in RAW_SPEC_FEATURES[:3])  # type: ignore[return-value]


def _grouped_spec_errors(
    cases: Sequence[core.Case], target: str, config: core.ModelConfig, fold_count: int = 5
) -> tuple[list[float], list[dict[str, float | int]]]:
    groups: dict[tuple[float, float, float], list[core.Case]] = defaultdict(list)
    for case in cases:
        groups[_spec_key(case)].append(case)
    folds: list[list[core.Case]] = [[] for _ in range(fold_count)]
    fold_group_counts = [0] * fold_count
    for _, group_cases in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
        index = min(
            range(fold_count),
            key=lambda value: (len(folds[value]), fold_group_counts[value], value),
        )
        folds[index].extend(group_cases)
        fold_group_counts[index] += 1

    errors: list[float] = []
    rows = []
    for index, validation in enumerate(folds, start=1):
        validation_ids = {id(case) for case in validation}
        train = [case for case in cases if id(case) not in validation_ids]
        model = core.fit_model(train, target, config)
        fold_errors = [
            abs(core.predict_model(model, case, target) - case.values[target])
            for case in validation
        ]
        errors.extend(fold_errors)
        rows.append(
            {
                "fold": index,
                "training_case_count": len(train),
                "validation_case_count": len(validation),
                "validation_spec_count": fold_group_counts[index - 1],
                "mae": statistics.fmean(fold_errors),
            }
        )
    return errors, rows


def _startup_configs(target: str) -> Iterable[core.ModelConfig]:
    transforms = (
        ("raw", "reduction_share")
        if target == "intermediate_thickness_set_mode"
        else ("raw",)
    )
    for feature_set, degree, ridge_lambda, transform in itertools.product(
        STARTUP_FEATURE_SETS,
        (1, 2),
        (0.01, 0.1, 1.0, 10.0, 100.0),
        transforms,
    ):
        yield core.ModelConfig(feature_set, degree, ridge_lambda, transform)


def _flatness_configs(target: str) -> Iterable[core.ModelConfig]:
    del target
    for feature_set, degree, ridge_lambda in itertools.product(
        STARTUP_FEATURE_SETS,
        (1, 2),
        (0.1, 1.0, 10.0, 100.0),
    ):
        yield core.ModelConfig(feature_set, degree, ridge_lambda, "raw")


def select_hybrid(
    cases: Sequence[core.Case],
    target: str,
    configs: Iterable[core.ModelConfig],
) -> Selection:
    best: tuple[tuple[float, float, float, int, float], Selection] | None = None
    for config in configs:
        try:
            rolling_errors, rolling_folds = core._rolling_errors(cases, target, config)
            grouped_errors, grouped_folds = _grouped_spec_errors(cases, target, config)
        except ValueError:
            continue
        equal_day_mae = statistics.fmean(float(row["mae"]) for row in rolling_folds)
        grouped_mae = statistics.fmean(grouped_errors)
        combined = 0.60 * equal_day_mae + 0.40 * grouped_mae
        complexity = len(
            core._basis_terms(len(core.FEATURE_SETS[config.feature_set]), config.degree)
        )
        key = (combined, equal_day_mae, grouped_mae, complexity, -config.ridge_lambda)
        selection = Selection(
            config,
            rolling_errors,
            rolling_folds,
            grouped_errors,
            grouped_folds,
            combined,
        )
        if best is None or key < best[0]:
            best = (key, selection)
    if best is None:
        raise ValueError(f"no valid configuration for {target}")
    return best[1]


def _error_summary(errors: Sequence[float]) -> dict[str, float | int]:
    return {
        "evaluated_case_count": len(errors),
        "mae": round(statistics.fmean(errors), 6),
        "p80_absolute_error": round(core._percentile(errors, 0.80), 6),
        "p95_absolute_error": round(core._percentile(errors, 0.95), 6),
    }


def _serialize_model(
    model: core.FittedModel,
    target: core.TargetSpec,
    selection: Selection,
    direct_metrics: dict[str, float],
    baseline_metrics: dict[str, float],
    *,
    monitor: bool = False,
) -> dict:
    result = core._model_to_dict(
        model,
        target,
        selection.rolling_errors,
        selection.rolling_folds,
        direct_metrics,
        baseline_metrics,
    )
    result["selection"] = {
        "method": "60% rolling-origin date MAE + 40% held-out-spec MAE",
        "combined_score": round(selection.score, 6),
    }
    result["grouped_spec_validation"] = {
        **_error_summary(selection.grouped_errors),
        "folds": [
            {**row, "mae": round(float(row["mae"]), 6)}
            for row in selection.grouped_folds
        ],
    }
    conservative_p80 = max(
        core._percentile(selection.rolling_errors, 0.80),
        core._percentile(selection.grouped_errors, 0.80),
    )
    conservative_p95 = max(
        core._percentile(selection.rolling_errors, 0.95),
        core._percentile(selection.grouped_errors, 0.95),
    )
    result["prediction_interval"] = {
        "method": "max of rolling-date and held-out-spec residual quantiles",
        "coverage": 0.80,
        "half_width": round(conservative_p80, 6),
        "p95_half_width": round(conservative_p95, 6),
    }
    if monitor:
        result["reliability"] = "monitor"
    elif target.code == "intermediate_thickness_set_mode" and direct_metrics["r2"] >= 0.8:
        result["reliability"] = "high"
    elif (
        math.isfinite(direct_metrics["r2"])
        and direct_metrics["r2"] >= 0.35
        and direct_metrics["mae"] <= baseline_metrics["mae"]
    ):
        result["reliability"] = "medium"
    else:
        result["reliability"] = "low"
    return result


def _coverage(cases: Sequence[core.Case]) -> dict[str, dict[str, float]]:
    coverage = {}
    for name in RAW_INPUT_FEATURES:
        values = [case.values[name] for case in cases]
        coverage[name] = {
            "min": round(min(values), 6),
            "p01": round(core._percentile(values, 0.01), 6),
            "p99": round(core._percentile(values, 0.99), 6),
            "max": round(max(values), 6),
        }
    return coverage


def _rank(values: Sequence[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(order):
        end = cursor + 1
        while end < len(order) and values[order[end]] == values[order[cursor]]:
            end += 1
        rank = (cursor + end - 1) / 2.0
        for index in order[cursor:end]:
            ranks[index] = rank
        cursor = end
    return ranks


def _pearson(left: Sequence[float], right: Sequence[float]) -> float:
    left_mean = statistics.fmean(left)
    right_mean = statistics.fmean(right)
    numerator = sum(
        (x - left_mean) * (y - right_mean) for x, y in zip(left, right)
    )
    left_sum = sum((x - left_mean) ** 2 for x in left)
    right_sum = sum((y - right_mean) ** 2 for y in right)
    denominator = math.sqrt(left_sum * right_sum)
    return numerator / denominator if denominator else math.nan


def _spearman(left: Sequence[float], right: Sequence[float]) -> float:
    return _pearson(_rank(left), _rank(right))


def _within_spec_association(
    rows: Sequence[dict[str, str]], output_name: str, control_name: str
) -> tuple[float, int, int]:
    groups: dict[tuple[float, float, float], list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        try:
            key = (
                _finite(row, "entry_thickness_set_mode"),
                _finite(row, "exit_thickness_set_mode"),
                _finite(row, "width_set_mode"),
            )
            groups[key].append((_finite(row, output_name), _finite(row, control_name)))
        except (KeyError, TypeError, ValueError):
            continue
    left: list[float] = []
    right: list[float] = []
    used_groups = 0
    for pairs in groups.values():
        if len(pairs) < 2:
            continue
        used_groups += 1
        output_median = statistics.median(value for value, _ in pairs)
        control_median = statistics.median(value for _, value in pairs)
        left.extend(value - output_median for value, _ in pairs)
        right.extend(value - control_median for _, value in pairs)
    return _spearman(left, right), len(left), used_groups


def build_associations(rows: Sequence[dict[str, str]]) -> dict[str, list[dict]]:
    result = {}
    for target in FLATNESS_TARGETS:
        associations = []
        for control, label in CONTROL_LABELS.items():
            rho, sample_count, group_count = _within_spec_association(
                rows, target.code, control
            )
            if not math.isfinite(rho):
                continue
            associations.append(
                {
                    "control": control,
                    "label": label,
                    "spearman_rho": round(rho, 4),
                    "absolute_rho": round(abs(rho), 4),
                    "sample_count": sample_count,
                    "spec_group_count": group_count,
                }
            )
        result[target.code] = sorted(
            associations, key=lambda item: (-item["absolute_rho"], item["control"])
        )
    return result


def _simple_spearman(rows: Sequence[dict[str, str]], left_name: str, right_name: str) -> float:
    pairs = []
    for row in rows:
        try:
            pairs.append((_finite(row, left_name), _finite(row, right_name)))
        except (KeyError, TypeError, ValueError):
            continue
    return _spearman([left for left, _ in pairs], [right for _, right in pairs])


def _observed_range(rows: Sequence[dict[str, str]], name: str) -> dict[str, float | int]:
    values = []
    for row in rows:
        try:
            values.append(_finite(row, name))
        except (KeyError, TypeError, ValueError):
            continue
    return {
        "count": len(values),
        "min": round(min(values), 6),
        "median": round(statistics.median(values), 6),
        "max": round(max(values), 6),
    }


def _observed_distribution(
    rows: Sequence[dict[str, str]], name: str
) -> dict[str, float | int]:
    values = []
    for row in rows:
        try:
            values.append(_finite(row, name))
        except (KeyError, TypeError, ValueError):
            continue
    return {
        "count": len(values),
        "min": round(min(values), 6),
        "p05": round(core._percentile(values, 0.05), 6),
        "median": round(statistics.median(values), 6),
        "p95": round(core._percentile(values, 0.95), 6),
        "max": round(max(values), 6),
    }


def _exact_overlap(training: Sequence[core.Case], test: Sequence[core.Case]) -> dict:
    keys = {_spec_key(case) for case in training}
    overlap = sum(_spec_key(case) in keys for case in test)
    return {
        "test_case_count": len(test),
        "exact_spec_overlap_count": overlap,
        "exact_spec_overlap_percent": round(overlap / len(test) * 100, 3),
    }


def _exit_quality(rows: Sequence[dict[str, str]]) -> dict:
    errors = []
    relative = []
    for row in rows:
        if row.get("dataset") != FINAL_TEST_DAY:
            continue
        try:
            value = abs(_finite(row, "exit_thickness_error_abs_p95"))
            target = _finite(row, "exit_thickness_set_mode")
        except (KeyError, TypeError, ValueError):
            continue
        errors.append(value)
        relative.append(value / target * 100)
    return {
        "case_count": len(errors),
        "median_segment_p95_absolute_error_mm": round(statistics.median(errors), 6),
        "median_segment_p95_relative_error_percent": round(
            statistics.median(relative), 6
        ),
        "worst_segment_p95_relative_error_percent": round(max(relative), 6),
    }


def _fit_target_models(
    development: Sequence[core.Case],
    test: Sequence[core.Case],
    targets: Sequence[core.TargetSpec],
    config_factory,
    *,
    monitor: bool = False,
) -> tuple[dict[str, dict], int]:
    selected: dict[str, Selection] = {}
    for index, target in enumerate(targets, start=1):
        print(f"[{index}/{len(targets)}] selecting {target.code}", flush=True)
        selected[target.code] = select_hybrid(
            development, target.code, config_factory(target.code)
        )

    models = {}
    direct_wins = 0
    for target in targets:
        selection = selected[target.code]
        fitted = core.fit_model(development, target.code, selection.config)
        actual = [case.values[target.code] for case in test]
        direct = [core.predict_model(fitted, case, target.code) for case in test]
        baseline = core._nearest_median_baseline(development, test, target.code)
        direct_metrics = core._metric_summary(actual, direct)
        baseline_metrics = core._metric_summary(actual, baseline)
        direct_wins += direct_metrics["mae"] < baseline_metrics["mae"]
        models[target.code] = _serialize_model(
            fitted,
            target,
            selection,
            direct_metrics,
            baseline_metrics,
            monitor=monitor,
        )
    return models, direct_wins


def build_width_ablation(
    development: Sequence[core.Case],
    test: Sequence[core.Case],
    selected_models: dict[str, dict],
) -> dict[str, dict]:
    """Quantify whether width earns its place instead of assuming it does."""
    result = {}
    for target in STARTUP_TARGETS:
        no_width = select_hybrid(
            development,
            target.code,
            (
                config
                for config in _startup_configs(target.code)
                if config.feature_set == "thickness2"
            ),
        )
        fitted = core.fit_model(development, target.code, no_width.config)
        actual = [case.values[target.code] for case in test]
        predicted = [core.predict_model(fitted, case, target.code) for case in test]
        no_width_metrics = core._metric_summary(actual, predicted)
        selected = selected_models[target.code]
        selected_score = float(selected["selection"]["combined_score"])
        selected_mae = float(selected["final_test"]["direct"]["mae"])
        result[target.code] = {
            "selected_uses_width": "width_set_mode" in selected["feature_names"],
            "selected_hybrid_validation_score": round(selected_score, 6),
            "best_no_width_hybrid_validation_score": round(no_width.score, 6),
            "hybrid_validation_improvement_percent": round(
                (no_width.score - selected_score) / no_width.score * 100
                if no_width.score
                else 0.0,
                3,
            ),
            "selected_final_test_mae": selected_mae,
            "best_no_width_final_test_mae": round(no_width_metrics["mae"], 6),
            "final_test_mae_improvement_percent": round(
                (no_width_metrics["mae"] - selected_mae)
                / no_width_metrics["mae"]
                * 100
                if no_width_metrics["mae"]
                else 0.0,
                3,
            ),
            "interpretation": (
                "width retained because it improved the pre-test hybrid validation score; "
                "it is a load/tension correction feature, not the target thickness definition"
            ),
        }
    return result


def build_report(artifact: dict) -> str:
    lines = [
        "# 冷二联直接预测 v0.4 训练与验证报告",
        "",
        "## 本版变化",
        "",
        "- 输出从 13 项收敛为主操实际手输的 6 项：中间厚度、三段张力、F1/F2高速稳定运行辊缝参考。",
        "- 原 v0.3 的“综合设定辊缝um”是PLC高速运行计算量；本版F1/F2学到的是高速稳定运行辊缝，不能当作上卷安全首设。",
        "- 弯辊、倾斜、左右轧制力和左右实际辊缝移到版型运行反馈层；轧制力不再伪装为人工首设。",
        "- 49路AFC版型已进入独立监控模型；由于DAT未写工程单位、传感器序号方向未确认，本版只给观测基线和风险关联，不自动给调节方向。",
        "- 推理仍是冻结系数方程，不检索历史卷、不做Top3/Top5加权平均。",
        "",
        f"开发集：0712—0718共 `{artifact['training_case_count']}` 个高速稳定段；日期隔离测试：0719共 `{artifact['validation_case_count']}` 段。",
        "",
        "## 0719首设复现误差",
        "",
        "| 首设参数 | 0719 MAE | P95绝对误差 | R² | 历史5NN MAE（仅对照） | 可靠性 | 模型 |",
        "| --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for target in STARTUP_TARGETS:
        model = artifact["models"][target.code]
        direct = model["final_test"]["direct"]
        baseline = model["final_test"]["historical_5nn_median_baseline"]
        r2 = "—" if direct["r2"] is None else f"{direct['r2']:.3f}"
        lines.append(
            f"| {target.label} | {direct['mae']:.4f} {target.unit} | "
            f"{direct['p95_absolute_error']:.4f} {target.unit} | {r2} | "
            f"{baseline['mae']:.4f} {target.unit} | {model['reliability']} | "
            f"{model['feature_set']}/d{model['degree']}/λ={model['ridge_lambda']:g} |"
        )
    lines.extend(
        [
            "",
            "这里衡量的是对高速稳定后人工设定的复现误差，不是模型真实下发后的成品厚度误差。"
            "F1/F2是高速稳定运行辊缝参考，不能直接作为上卷安全首设；主操必须留安全余量，跑带测厚后逐步调整，严禁自动写PLC。",
            "",
            "## 版型运行基线",
            "",
            "| 指标 | 可用开发段 | 0719 MAE | R² | 用途 |",
            "| --- | ---: | ---: | ---: | --- |",
        ]
    )
    flatness = artifact["flatness_monitor"]
    for target in FLATNESS_TARGETS:
        model = flatness["models"][target.code]
        metrics = model["final_test"]["direct"]
        r2 = "—" if metrics["r2"] is None else f"{metrics['r2']:.3f}"
        lines.append(
            f"| {target.label} | {flatness['training_case_count']} | "
            f"{metrics['mae']:.4f} DAT原始值 | {r2} | 运行后偏离检查，不用于自动调节 |"
        )
    lines.extend(
        [
            "",
            "相关性为同规格内、同一高速段的描述性Spearman相关；闭环控制会造成反向因果。必须完成测厚仪/板形仪到机架的时间延迟对齐、左右方向和工程单位确认，才能研究调节方向。",
            "",
            "## 验收边界",
            "",
            "- 0719已被旧版本用于过对照，因此属于日期隔离复用基准，不再声称是从未查看的盲测；下一批现场DAT应作为真正影子测试。",
            "- 参数模型验收必须同时记录主操采用值、首轮微调次数、达到厚度稳定所需时间、断带/跑偏/过载以及49点版型。",
            "- 版型DAT单位目前为空，本报告保持“DAT原始值”，未采纳外部报告中的μm假定。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--features",
        type=Path,
        default=WORKSPACE_ROOT / "outputs/model_v4/segment_features.csv",
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        default=WORKSPACE_ROOT
        / "outputs/v5_existing_data/recommendation_direct_from_v4_trainer.json",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=WORKSPACE_ROOT / "outputs/v5_existing_data/recommendation_model_from_v4_trainer.md",
    )
    args = parser.parse_args()
    frozen_v4 = (
        PROJECT_ROOT / "backend/app/model_artifacts/recommendation_direct_v4.json"
    ).resolve()
    if args.artifact.resolve() == frozen_v4:
        parser.error("refusing to overwrite frozen recommendation_direct_v4.json")
    if not args.features.exists():
        parser.error(f"missing feature table: {args.features}")
    rows = load_feature_rows(args.features)
    development_rows = [row for row in rows if row.get("dataset") in DEVELOPMENT_DAYS]
    test_rows = [row for row in rows if row.get("dataset") == FINAL_TEST_DAY]

    configure_core(STARTUP_TARGETS, STARTUP_FEATURE_SETS)
    development = make_cases(development_rows, STARTUP_TARGETS)
    test = make_cases(test_rows, STARTUP_TARGETS)
    if len(development) < 100 or not test:
        parser.error("insufficient startup cases")
    startup_models, startup_wins = _fit_target_models(
        development, test, STARTUP_TARGETS, _startup_configs
    )
    width_ablation = build_width_ablation(development, test, startup_models)

    configure_core(FLATNESS_TARGETS, STARTUP_FEATURE_SETS)
    flat_development = make_cases(development_rows, FLATNESS_TARGETS)
    flat_test = make_cases(test_rows, FLATNESS_TARGETS)
    flat_models, flat_wins = _fit_target_models(
        flat_development,
        flat_test,
        FLATNESS_TARGETS,
        _flatness_configs,
        monitor=True,
    )

    configure_core(STARTUP_TARGETS, STARTUP_FEATURE_SETS)
    startup_improvements = [
        model["final_test"]["mae_improvement_percent"]
        for model in startup_models.values()
    ]
    artifact = {
        "model_version": "direct-ridge-startup-flatness-v0.4",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model_family": "standardized physics-feature polynomial ridge regression",
        "inference_method": "fixed coefficient equation; no neighbour lookup or averaging",
        "training_datasets": list(DEVELOPMENT_DAYS),
        "validation_dataset": FINAL_TEST_DAY,
        "training_case_count": len(development),
        "validation_case_count": len(test),
        "training_policy": {
            "learning_eligible_only": True,
            "high_speed_plateau_only": True,
            "low_speed_role": "operator_tuning_excluded_from_targets",
            "model_selection": "60% rolling-origin date validation + 40% whole-spec held-out validation",
            "validation_used_for_tuning": False,
            "validation_status": "date-isolated reused benchmark; not a never-seen blind test",
            "historical_neighbour_method_used_at_inference": False,
        },
        "parameter_policy": {
            "startup_outputs": [target.code for target in STARTUP_TARGETS],
            "removed_from_startup": [
                "main_speed_set_median",
                "f1_bending_set_median",
                "f2_bending_set_median",
                "tm1_tilt_set_median",
                "tm2_tilt_set_median",
                "f1_roll_force_sum_median",
                "f2_roll_force_sum_median",
                "tm1_gap_command_median",
                "tm2_gap_command_median",
            ],
            "closed_gap_safety": (
                "high-speed stable running gap reference only; "
                "not a safe coil-on first set; operator must leave a safety margin; "
                "never auto-write PLC"
            ),
        },
        "input_features": list(RAW_INPUT_FEATURES),
        "feature_engineering": {
            "log_thickness_ratio": "ln(entry_thickness / target_exit_thickness)",
            "entry_cross_section_proxy": "entry_thickness * width",
            "exit_cross_section_proxy": "target_exit_thickness * width",
        },
        "input_coverage": _coverage(development),
        "width_ablation": width_ablation,
        "models": startup_models,
        "spec_overlap": _exact_overlap(development, test),
        "exit_quality_observation": _exit_quality(rows),
        "flatness_monitor": {
            "role": "runtime observational baseline; not a startup input and not automatic correction",
            "native_unit": "DAT原始值",
            "unit_status": "DAT metadata blank; engineering unit unconfirmed",
            "zone_count": 49,
            "zone_orientation": "unconfirmed; low/high sensor numbers not mapped to operation/drive side",
            "lag_alignment": "not yet applied; required before causal adjustment modelling",
            "training_case_count": len(flat_development),
            "validation_case_count": len(flat_test),
            "models": flat_models,
            "observed_distributions": {
                target.code: _observed_distribution(development_rows, target.code)
                for target in FLATNESS_TARGETS
            },
            "runtime_associations": build_associations(development_rows),
            "data_quality": {
                "all_row_count": len(rows),
                "valid_flatness_row_count": sum(
                    bool(row.get("flatness_std_median")) for row in rows
                ),
                "valid_percent": round(
                    sum(bool(row.get("flatness_std_median")) for row in rows)
                    / len(rows)
                    * 100,
                    3,
                ),
                "width_to_active_zone_spearman": round(
                    _simple_spearman(rows, "width_set_mode", "flatness_active_zone_count"),
                    4,
                ),
            },
        },
        "observed_units_and_ranges": {
            "tm1_closed_gap_set_median": {
                **_observed_range(rows, "tm1_closed_gap_set_median"),
                "unit": "mm (HMI/range confirmed; DAT metadata blank)",
            },
            "tm2_closed_gap_set_median": {
                **_observed_range(rows, "tm2_closed_gap_set_median"),
                "unit": "mm (HMI/range confirmed; DAT metadata blank)",
            },
        },
        "summary": {
            "target_count": len(STARTUP_TARGETS),
            "direct_win_count": startup_wins,
            "mean_mae_improvement_percent": round(
                statistics.fmean(startup_improvements), 3
            ),
            "flatness_target_count": len(FLATNESS_TARGETS),
            "flatness_direct_win_count": flat_wins,
            "historical_baseline": "standardized specification 5-nearest median; evaluation only",
        },
    }
    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    args.artifact.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(build_report(artifact), encoding="utf-8")
    print(
        f"model={artifact['model_version']} train={len(development)} test={len(test)} "
        f"startup_wins={startup_wins}/{len(STARTUP_TARGETS)} "
        f"flatness={len(flat_development)}/{len(flat_test)}",
        flush=True,
    )
    print(f"artifact={args.artifact.resolve()}", flush=True)
    print(f"report={args.report.resolve()}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
