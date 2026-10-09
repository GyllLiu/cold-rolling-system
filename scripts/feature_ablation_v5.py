#!/usr/bin/env python3
"""Step 3: input ablation and importance on existing data only.

Feature-set comparison, drop-column ablation and grouped-fold permutation
importance use 260712-260718.  260719 is scored once after decisions and is
not used to choose inputs.  No random 8/2 split.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import evaluate_existing_models_v5 as ev
import train_validate_direct_prediction as core
import train_validate_v4 as v4


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DEVELOPMENT_DAYS = ev.DEVELOPMENT_DAYS
BENCHMARK_DAY = ev.BENCHMARK_DAY
STARTUP_TARGETS = ev.STARTUP_TARGETS
TARGET_LABELS = ev.TARGET_LABELS
TARGET_UNITS = ev.TARGET_UNITS

FEATURE_LABELS = {
    "entry_thickness_set_mode": "入口厚度设定",
    "exit_thickness_set_mode": "目标出口厚度设定",
    "width_set_mode": "来料宽度设定",
    "total_reduction_ratio": "总压下率",
    "log_thickness_ratio": "厚度比对数",
    "entry_cross_section_proxy": "入口截面积代理",
    "exit_cross_section_proxy": "出口截面积代理",
    "entry_thickness_actual_median": "入口实际厚度（诊断）",
    "uncoiler_diameter_start": "开卷起始卷径（诊断）",
    "coiler_diameter_start": "卷取起始卷径（诊断）",
}

DEPLOY_FEATURES = (
    "entry_thickness_set_mode",
    "exit_thickness_set_mode",
    "width_set_mode",
    "total_reduction_ratio",
    "log_thickness_ratio",
    "entry_cross_section_proxy",
    "exit_cross_section_proxy",
)
DIAGNOSTIC_FEATURES = (
    "entry_thickness_actual_median",
    "uncoiler_diameter_start",
    "coiler_diameter_start",
)

FEATURE_SETS: dict[str, tuple[str, ...]] = {
    "thickness2": (
        "entry_thickness_set_mode",
        "exit_thickness_set_mode",
    ),
    "spec3": (
        "entry_thickness_set_mode",
        "exit_thickness_set_mode",
        "width_set_mode",
    ),
    "physics_no_width": (
        "entry_thickness_set_mode",
        "exit_thickness_set_mode",
        "total_reduction_ratio",
        "log_thickness_ratio",
    ),
    "physics7": (
        "entry_thickness_set_mode",
        "exit_thickness_set_mode",
        "width_set_mode",
        "total_reduction_ratio",
        "log_thickness_ratio",
        "entry_cross_section_proxy",
        "exit_cross_section_proxy",
    ),
    "diagnostic_actual": (
        "entry_thickness_set_mode",
        "exit_thickness_set_mode",
        "width_set_mode",
        "total_reduction_ratio",
        "log_thickness_ratio",
        "entry_cross_section_proxy",
        "exit_cross_section_proxy",
        "entry_thickness_actual_median",
    ),
    "diagnostic_diameter": (
        "entry_thickness_set_mode",
        "exit_thickness_set_mode",
        "width_set_mode",
        "total_reduction_ratio",
        "log_thickness_ratio",
        "entry_cross_section_proxy",
        "exit_cross_section_proxy",
        "uncoiler_diameter_start",
        "coiler_diameter_start",
    ),
}

COMPARE_SETS = (
    "thickness2",
    "spec3",
    "physics_no_width",
    "physics7",
    "diagnostic_actual",
    "diagnostic_diameter",
)
DROP_FROM_SPEC3 = (
    "entry_thickness_set_mode",
    "exit_thickness_set_mode",
    "width_set_mode",
)
DROP_FROM_PHYSICS7 = (
    "total_reduction_ratio",
    "log_thickness_ratio",
    "entry_cross_section_proxy",
    "exit_cross_section_proxy",
    "width_set_mode",
)

F1_SHARE_EXPECTED = {
    "mean": 0.6250,
    "std": 0.0403,
    "p05": 0.573,
    "p95": 0.691,
}


def _finite(row: dict[str, str], name: str) -> float:
    value = float(row[name])
    if not math.isfinite(value):
        raise ValueError(name)
    return value


def load_cases(path: Path) -> list[core.Case]:
    rows = v4.load_feature_rows(path)
    cases = []
    target_names = list(STARTUP_TARGETS)
    for row in rows:
        try:
            values = v4._derived_values(row)
            values.update({name: _finite(row, name) for name in target_names})
            values["uncoiler_diameter_start"] = _finite(row, "uncoiler_diameter_start")
            values["coiler_diameter_start"] = _finite(row, "coiler_diameter_start")
            values["f1_reduction_ratio"] = _finite(row, "f1_reduction_ratio")
            values["f2_reduction_ratio"] = _finite(row, "f2_reduction_ratio")
        except (KeyError, TypeError, ValueError):
            continue
        cases.append(
            core.Case(
                dataset=row["dataset"],
                candidate_id=f"{row['dataset']}_{row.get('candidate_id', '')}",
                start_time=row.get("start_time", ""),
                source_files=row.get("source_files", ""),
                values=values,
            )
        )
    return cases


def register_feature_sets() -> dict[str, tuple[str, ...]]:
    sets = dict(FEATURE_SETS)
    for name in DROP_FROM_SPEC3:
        sets[f"spec3_drop_{name}"] = tuple(item for item in FEATURE_SETS["spec3"] if item != name)
    for name in DROP_FROM_PHYSICS7:
        sets[f"physics7_drop_{name}"] = tuple(
            item for item in FEATURE_SETS["physics7"] if item != name
        )
    return sets


def candidate_configs(target: str) -> list[core.ModelConfig]:
    transforms = (
        ("raw", "reduction_share")
        if target == "intermediate_thickness_set_mode"
        else ("raw",)
    )
    configs = []
    for degree in (1, 2):
        for ridge in (0.1, 1.0, 10.0):
            for transform in transforms:
                configs.append(core.ModelConfig("unused", degree, ridge, transform))
    return configs


def with_feature_set(config: core.ModelConfig, feature_set: str) -> core.ModelConfig:
    return core.ModelConfig(feature_set, config.degree, config.ridge_lambda, config.target_transform)


def score_config(
    development: Sequence[core.Case],
    target: str,
    config: core.ModelConfig,
    grouped_folds: Sequence[Sequence[core.Case]],
) -> dict[str, Any] | None:
    rolling_maes: list[float] = []
    rolling_p95s: list[float] = []
    rolling_errors: list[float] = []
    grouped_maes: list[float] = []
    grouped_p95s: list[float] = []
    grouped_errors: list[float] = []
    fold_rows: list[dict[str, Any]] = []
    try:
        for index, (train_days, val_day) in enumerate(v4.ROLLING_FOLDS, start=1):
            train = [case for case in development if case.dataset in train_days]
            validation = [case for case in development if case.dataset == val_day]
            scored = ev.score_refit(train, validation, target, config)
            rolling_maes.append(scored["mae"])
            rolling_p95s.append(scored["p95"])
            rolling_errors.append(scored["mae"])
            fold_rows.append({"protocol": "rolling_origin", "fold_id": f"rolling_{index}", **scored})
        for index, validation in enumerate(grouped_folds, start=1):
            validation_ids = {id(case) for case in validation}
            train = [case for case in development if id(case) not in validation_ids]
            scored = ev.score_refit(train, validation, target, config)
            grouped_maes.append(scored["mae"])
            grouped_p95s.append(scored["p95"])
            grouped_errors.append(scored["mae"])
            fold_rows.append({"protocol": "grouped_spec_5fold", "fold_id": f"spec_{index}", **scored})
    except ValueError:
        return None
    equal_day_mae = statistics.fmean(rolling_maes)
    grouped_mae = statistics.fmean(grouped_maes)
    return {
        "rolling_equal_day_mae": equal_day_mae,
        "rolling_worst_day_mae": max(rolling_maes),
        "rolling_p95": statistics.fmean(rolling_p95s),
        "grouped_mae": grouped_mae,
        "grouped_p95": statistics.fmean(grouped_p95s),
        "hybrid_score": 0.60 * equal_day_mae + 0.40 * grouped_mae,
        "rolling_folds_worse_than_mean": sum(
            mae > equal_day_mae for mae in rolling_maes
        ),
        "fold_rows": fold_rows,
        "rolling_maes": rolling_maes,
        "grouped_maes": grouped_maes,
    }


def lodo_scores(
    development: Sequence[core.Case], target: str, config: core.ModelConfig
) -> dict[str, Any]:
    daily = {}
    fold_rows = []
    for day in DEVELOPMENT_DAYS:
        train = [case for case in development if case.dataset != day]
        validation = [case for case in development if case.dataset == day]
        scored = ev.score_refit(train, validation, target, config)
        daily[day] = scored
        fold_rows.append({"protocol": "leave_one_day_out", "fold_id": f"lodo_{day}", **scored})
    maes = [row["mae"] for row in daily.values()]
    return {
        "lodo_equal_day_mae": statistics.fmean(maes),
        "lodo_worst_day": max(daily, key=lambda day: daily[day]["mae"]),
        "lodo_worst_day_mae": max(maes),
        "lodo_daily_mae": {day: round(daily[day]["mae"], 6) for day in DEVELOPMENT_DAYS},
        "fold_rows": fold_rows,
    }


def reserved_score(
    development: Sequence[core.Case],
    benchmark: Sequence[core.Case],
    target: str,
    config: core.ModelConfig,
) -> dict[str, Any]:
    return ev.score_refit(development, benchmark, target, config)


def select_config(
    development: Sequence[core.Case],
    target: str,
    feature_set: str,
    grouped_folds: Sequence[Sequence[core.Case]],
) -> tuple[core.ModelConfig, dict[str, Any]]:
    best: tuple[tuple[float, float, int, float], core.ModelConfig, dict[str, Any]] | None = None
    for template in candidate_configs(target):
        config = with_feature_set(template, feature_set)
        scored = score_config(development, target, config, grouped_folds)
        if scored is None:
            continue
        complexity = len(core._basis_terms(len(core.FEATURE_SETS[feature_set]), config.degree))
        key = (scored["hybrid_score"], scored["rolling_equal_day_mae"], complexity, -config.ridge_lambda)
        if best is None or key < best[0]:
            best = (key, config, scored)
    if best is None:
        raise ValueError(f"no valid config for {target} {feature_set}")
    return best[1], best[2]


def permute_cases(cases: Sequence[core.Case], feature: str) -> list[core.Case]:
    values = [case.values[feature] for case in cases]
    shift = max(1, len(values) // 2)
    rotated = values[shift:] + values[:shift]
    if len(set(values)) > 1 and rotated == values:
        rotated = list(reversed(values))
    out = []
    for case, value in zip(cases, rotated):
        updated = dict(case.values)
        updated[feature] = value
        out.append(
            core.Case(
                dataset=case.dataset,
                candidate_id=case.candidate_id,
                start_time=case.start_time,
                source_files=case.source_files,
                values=updated,
            )
        )
    return out


def permutation_importance(
    development: Sequence[core.Case],
    grouped_folds: Sequence[Sequence[core.Case]],
    target: str,
    config: core.ModelConfig,
    features: Sequence[str],
) -> list[dict[str, Any]]:
    rows = []
    for feature in features:
        increases = []
        base_maes = []
        for validation in grouped_folds:
            validation_ids = {id(case) for case in validation}
            train = [case for case in development if id(case) not in validation_ids]
            model = core.fit_model(train, target, config)
            base_pairs = [
                (case.dataset, case.values[target], core.predict_model(model, case, target))
                for case in validation
            ]
            perm_pairs = [
                (case.dataset, case.values[target], core.predict_model(model, case, target))
                for case in permute_cases(validation, feature)
            ]
            base_mae = ev.metric_summary(
                [item[1] for item in base_pairs], [item[2] for item in base_pairs]
            )["mae"]
            perm_mae = ev.metric_summary(
                [item[1] for item in perm_pairs], [item[2] for item in perm_pairs]
            )["mae"]
            base_maes.append(base_mae)
            increases.append(perm_mae - base_mae)
        mean_base = statistics.fmean(base_maes)
        mean_increase = statistics.fmean(increases)
        rows.append(
            {
                "target": target,
                "target_label": TARGET_LABELS[target],
                "feature": feature,
                "feature_label": FEATURE_LABELS.get(feature, feature),
                "method": "grouped_permutation",
                "base_mae": round(mean_base, 6),
                "mae_increase": round(mean_increase, 6),
                "mae_increase_percent": round(mean_increase / mean_base * 100, 3) if mean_base else 0.0,
                "folds_worse": sum(value > 0 for value in increases),
                "fold_count": len(increases),
                "notes": "deterministic half-fold rotation on grouped-spec validation; not a random split",
            }
        )
    return rows


def majority_worse(base_maes: Sequence[float], other_maes: Sequence[float], threshold: float = 0.02) -> bool:
    if len(base_maes) != len(other_maes) or not base_maes:
        return False
    worse = 0
    for base, other in zip(base_maes, other_maes):
        if other >= base * (1 + threshold):
            worse += 1
    return worse > len(base_maes) / 2


def f1_reduction_share(cases: Sequence[core.Case]) -> dict[str, float]:
    shares = []
    for case in cases:
        entry = case.values["entry_thickness_set_mode"]
        mid = case.values["intermediate_thickness_set_mode"]
        exit_value = case.values["exit_thickness_set_mode"]
        total = entry - exit_value
        if total <= 1e-9:
            continue
        shares.append((entry - mid) / total)
    return {
        "n": len(shares),
        "mean": statistics.fmean(shares),
        "std": statistics.pstdev(shares),
        "p05": core._percentile(shares, 0.05),
        "p95": core._percentile(shares, 0.95),
        "min": min(shares),
        "max": max(shares),
    }


def load_availability(path: Path) -> dict[str, dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return {row["feature"]: row for row in csv.DictReader(handle)}


def classify_feature(
    *,
    feature: str,
    target: str,
    availability: dict[str, dict[str, str]],
    set_scores: dict[str, dict[str, Any]],
    drop_scores: dict[str, dict[str, Any]],
    perm_rows: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    info = availability.get(feature, {})
    decision_avail = info.get("decision", "")
    available = info.get("available_before_recommendation", "")
    runtime = info.get("runtime_feedback", "")
    physics = set_scores["physics7"]
    spec3 = set_scores["spec3"]
    thickness2 = set_scores["thickness2"]
    perm = next((row for row in perm_rows if row["feature"] == feature), None)

    role = "拒绝"
    reason = ""
    if feature in DIAGNOSTIC_FEATURES or decision_avail == "仅诊断":
        diagnostic_set = (
            "diagnostic_actual" if feature == "entry_thickness_actual_median" else "diagnostic_diameter"
        )
        scored = set_scores[diagnostic_set]
        improve = (physics["hybrid_score"] - scored["hybrid_score"]) / physics["hybrid_score"] * 100
        p95_worse = scored["grouped_p95"] > physics["grouped_p95"] * 1.05
        lodo_worse = scored["lodo_worst_day_mae"] > physics["lodo_worst_day_mae"] * 1.10
        if available == "否" or runtime == "是" or decision_avail == "仅诊断":
            if improve >= 5 and not p95_worse and not lodo_worse:
                role = "候选"
                reason = (
                    f"开发综合分相对physics7改善{improve:.2f}%，但推荐前不可靠获得或为运行后反馈，"
                    "默认不部署。"
                )
            else:
                role = "拒绝"
                reason = (
                    f"诊断输入相对physics7综合分变化{improve:.2f}%，且当前无PLC/"
                    "页面不能填写，或跨日/P95不稳定。禁止进入启动模型。"
                )
        if feature == "entry_thickness_actual_median":
            reason += "存值是高速段中位数，属于运行后测厚。"
    elif feature in {"entry_thickness_set_mode", "exit_thickness_set_mode"}:
        dropped = drop_scores.get(f"spec3_drop_{feature}")
        worse = False
        if dropped:
            worse = majority_worse(spec3["rolling_maes"], dropped["rolling_maes"]) or majority_worse(
                spec3["grouped_maes"], dropped["grouped_maes"]
            )
        role = "核心"
        reason = "物理必要：入口/出口厚度定义压下任务。当前页面可填，推荐前可得。"
        if worse:
            reason += "从spec3去掉后多数选择折MAE明显变差。"
        elif dropped:
            reason += "去掉后误差上升，仍按物理必要保留为核心输入。"
    elif feature == "width_set_mode":
        improve_spec = (thickness2["hybrid_score"] - spec3["hybrid_score"]) / thickness2["hybrid_score"] * 100
        improve_phys = (
            set_scores["physics_no_width"]["hybrid_score"] - physics["hybrid_score"]
        ) / set_scores["physics_no_width"]["hybrid_score"] * 100
        p95_worse = spec3["grouped_p95"] > thickness2["grouped_p95"] * 1.05
        drop = drop_scores.get("spec3_drop_width_set_mode")
        worse_folds = False
        if drop:
            worse_folds = majority_worse(spec3["rolling_maes"], drop["rolling_maes"]) or majority_worse(
                spec3["grouped_maes"], drop["grouped_maes"]
            )
        if improve_spec >= 2 and not p95_worse:
            role = "修正"
            reason = (
                f"相对thickness2综合分改善{improve_spec:.2f}%（physics相对无宽度{improve_phys:.2f}%），"
                "P95未明显恶化。宽度是负荷修正，不定义目标厚度。"
            )
        elif improve_spec >= 5:
            role = "候选"
            reason = f"改善{improve_spec:.2f}%，但稳定性或P95不足，暂不作为该输出的正式修正。"
        else:
            role = "拒绝"
            reason = f"加入宽度后综合分变化{improve_spec:.2f}%，对该输出帮助不足。"
        if worse_folds:
            reason += "从spec3去掉宽度后多数折变差。"
    else:
        # derived deployable physics features
        improve = (spec3["hybrid_score"] - physics["hybrid_score"]) / spec3["hybrid_score"] * 100
        drop = drop_scores.get(f"physics7_drop_{feature}")
        worse_folds = False
        drop_improve = 0.0
        if drop:
            drop_improve = (drop["hybrid_score"] - physics["hybrid_score"]) / drop["hybrid_score"] * 100
            worse_folds = majority_worse(physics["rolling_maes"], drop["rolling_maes"]) or majority_worse(
                physics["grouped_maes"], drop["grouped_maes"]
            )
        p95_worse = physics["grouped_p95"] > spec3["grouped_p95"] * 1.05
        if worse_folds and improve >= 2 and not p95_worse:
            role = "修正"
            reason = (
                f"physics7相对spec3综合分改善{improve:.2f}%，去掉{FEATURE_LABELS.get(feature, feature)}"
                f"后多数折变差（该列贡献约{drop_improve:.2f}%）。"
            )
        elif improve >= 2 and not p95_worse:
            role = "修正"
            reason = (
                f"physics7相对spec3综合分改善{improve:.2f}%，P95未明显恶化。"
                f"该派生量推荐前可由三项规格计算。"
            )
            if drop and drop_improve < 1:
                reason += "单独去掉该列改善有限，作为physics7组合的一部分保留。"
        elif improve >= 2 and p95_worse:
            role = "拒绝"
            reason = (
                f"physics7相对spec3综合分改善{improve:.2f}%，但grouped P95明显恶化，"
                "该派生组合不作为该输出的正式修正。"
            )
        elif improve >= 5:
            role = "候选"
            reason = f"physics7改善{improve:.2f}%，但单列稳定性或P95不足。"
        else:
            role = "拒绝"
            reason = f"相对spec3综合分变化{improve:.2f}%，该派生量对该输出帮助不足。"

    return {
        "target": target,
        "target_label": TARGET_LABELS[target],
        "feature": feature,
        "feature_label": FEATURE_LABELS.get(feature, feature),
        "role": role,
        "step1_decision": decision_avail,
        "available_before_recommendation": available,
        "manually_filled": info.get("manually_filled", ""),
        "runtime_feedback": runtime,
        "missing_rate_percent": info.get("missing_rate_percent", ""),
        "permutation_increase_percent": perm["mae_increase_percent"] if perm else "",
        "reason": reason,
    }


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    if not rows:
        raise ValueError(f"no rows for {path}")
    fieldnames: list[str] = []
    for row in rows:
        for key in row:
            if key not in fieldnames:
                fieldnames.append(key)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_report(
    path: Path,
    *,
    share: dict[str, float],
    set_table: Sequence[dict[str, Any]],
    decisions: Sequence[dict[str, Any]],
    perm_rows: Sequence[dict[str, Any]],
) -> None:
    share_ok = (
        abs(share["mean"] - F1_SHARE_EXPECTED["mean"]) < 0.005
        and abs(share["std"] - F1_SHARE_EXPECTED["std"]) < 0.005
        and abs(share["p05"] - F1_SHARE_EXPECTED["p05"]) < 0.01
        and abs(share["p95"] - F1_SHARE_EXPECTED["p95"]) < 0.01
    )
    lines = [
        "# 现有数据输入消融与重要性",
        "",
        f"生成时间：{datetime.now(timezone.utc).isoformat()}",
        "选择只使用260712–260718的滚动日期验证和整规格留出；Leave-One-Day-Out只作稳定性诊断。",
        "260719是日期隔离复用基准，只在输入决定之后评估一次，不参与选择。",
        "未使用随机8/2切分，也未用单一Pearson/Spearman决定输入。",
        "",
        "## F1压下占比合理性检查",
        "",
        f"样本 `{int(share['n'])}` 段（含260719）。",
        f"F1压下占比 = (入口厚度 − 中间厚度) / (入口厚度 − 出口厚度)。",
        "",
        f"- 均值 {share['mean']*100:.2f}%（期望约62.50%）",
        f"- 标准差 {share['std']*100:.2f}%（期望约4.03%）",
        f"- P05 {share['p05']*100:.1f}%（期望约57.3%）",
        f"- P95 {share['p95']*100:.1f}%（期望约69.1%）",
        f"- 核对：{'通过' if share_ok else '未通过'}。此量只作合理性检查，不硬编码为固定62%。",
        "",
        "## 特征集综合分（开发期，越小越好）",
        "",
        "| 输出 | thickness2 | spec3 | physics无宽度 | physics7 | 诊断实际厚度 | 诊断卷径 | 选用正式集 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    by_target: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in set_table:
        if row["block"] == "feature_set":
            by_target[row["target"]][row["feature_set"]] = row
    for target in STARTUP_TARGETS:
        item = by_target[target]
        official = "physics7" if item["physics7"]["hybrid_score"] <= item["spec3"]["hybrid_score"] * 0.98 else "spec3"
        if item["spec3"]["hybrid_score"] > item["thickness2"]["hybrid_score"] * 0.98:
            if official == "spec3" and item["thickness2"]["hybrid_score"] < item["spec3"]["hybrid_score"]:
                official = "thickness2"
        lines.append(
            f"| {TARGET_LABELS[target]} | {item['thickness2']['hybrid_score']:.6f} | "
            f"{item['spec3']['hybrid_score']:.6f} | {item['physics_no_width']['hybrid_score']:.6f} | "
            f"{item['physics7']['hybrid_score']:.6f} | {item['diagnostic_actual']['hybrid_score']:.6f} | "
            f"{item['diagnostic_diameter']['hybrid_score']:.6f} | {official} |"
        )

    lines.extend(
        [
            "",
            "正式选用只看允许部署集合。诊断两列不得因数字更好而上线。",
            "",
            "## 有/无宽度",
            "",
            "| 输出 | thickness2→spec3 综合分改善 | physics无宽度→physics7 | spec3 P95变化 |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for target in STARTUP_TARGETS:
        item = by_target[target]
        spec_imp = (item["thickness2"]["hybrid_score"] - item["spec3"]["hybrid_score"]) / item["thickness2"]["hybrid_score"] * 100
        phys_imp = (
            item["physics_no_width"]["hybrid_score"] - item["physics7"]["hybrid_score"]
        ) / item["physics_no_width"]["hybrid_score"] * 100
        p95 = (item["spec3"]["grouped_p95"] - item["thickness2"]["grouped_p95"]) / item["thickness2"]["grouped_p95"] * 100
        lines.append(
            f"| {TARGET_LABELS[target]} | {spec_imp:.2f}% | {phys_imp:.2f}% | {p95:+.2f}% |"
        )

    lines.extend(["", "## 输入决定矩阵（按输出）", ""])
    for target in STARTUP_TARGETS:
        lines.append(f"### {TARGET_LABELS[target]}")
        lines.append("")
        lines.append("| 特征 | 角色 | 推荐前可得 | 置换重要性ΔMAE | 原因 |")
        lines.append("| --- | --- | --- | ---: | --- |")
        for row in decisions:
            if row["target"] != target:
                continue
            perm = row["permutation_increase_percent"]
            perm_text = f"{perm}%" if perm != "" else "—"
            lines.append(
                f"| {row['feature_label']} | {row['role']} | {row['available_before_recommendation']} | "
                f"{perm_text} | {row['reason']} |"
            )
        lines.append("")

    overall: dict[str, list[str]] = defaultdict(list)
    for row in decisions:
        if row["feature"] in DEPLOY_FEATURES:
            overall[row["feature"]].append(row["role"])
    lines.extend(["## 部署输入总评", "", "| 特征 | 跨输出角色 | 部署建议 |", "| --- | --- | --- |"])
    for feature in DEPLOY_FEATURES:
        roles = overall[feature]
        if roles.count("核心") >= 1 and feature in {"entry_thickness_set_mode", "exit_thickness_set_mode"}:
            advice = "正式部署"
        elif roles.count("拒绝") == len(roles):
            advice = "不部署"
        elif roles.count("修正") + roles.count("核心") >= max(1, len(roles) // 2):
            advice = "正式部署（修正项）"
        else:
            advice = "保留在physics7组合中，但不单独强调"
        lines.append(f"| {FEATURE_LABELS[feature]} | {', '.join(roles)} | {advice} |")

    lines.extend(
        [
            "",
            "## 诊断输入",
            "",
            "入口实际厚度、开卷/卷取起始卷径一律默认不部署。即使某输出开发分改善超过5%，",
            "也只标为候选，因为当前无PLC、页面只允许3项输入，且入口实际厚度取自高速段中位数。",
            "",
            "## 260719日期隔离复用基准",
            "",
            "下表是输入决定之后的一次复用评估，不是选择依据。",
            "",
            "| 输出 | physics7 260719 MAE | spec3 260719 MAE | 诊断实际厚度 260719 MAE |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for target in STARTUP_TARGETS:
        item = by_target[target]
        lines.append(
            f"| {TARGET_LABELS[target]} | {item['physics7']['reserved_mae']:.6f} | "
            f"{item['spec3']['reserved_mae']:.6f} | {item['diagnostic_actual']['reserved_mae']:.6f} |"
        )
    lines.extend(
        [
            "",
            "禁止把速度、电流、转矩、实际轧制力、实际辊缝、弯辊、倾斜、出口实际厚度、49区版型",
            "或输出目标当作启动输入。F1/F2是高速稳定运行辊缝参考，不是上卷安全首设。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--features",
        type=Path,
        default=WORKSPACE_ROOT / "outputs/model_v4/segment_features.csv",
    )
    parser.add_argument(
        "--availability",
        type=Path,
        default=WORKSPACE_ROOT / "outputs/v5_existing_data/input_availability.csv",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=WORKSPACE_ROOT / "outputs/v5_existing_data",
    )
    args = parser.parse_args()
    if not args.features.exists():
        parser.error(f"missing feature table: {args.features}")
    if not args.availability.exists():
        parser.error("run Step 1 audit before Step 3")

    cases = load_cases(args.features)
    development = [case for case in cases if case.dataset in DEVELOPMENT_DAYS]
    benchmark = [case for case in cases if case.dataset == BENCHMARK_DAY]
    if len(development) != 281 or len(benchmark) != 64:
        parser.error(f"unexpected counts development={len(development)} benchmark={len(benchmark)}")

    feature_sets = register_feature_sets()
    v4.configure_core(v4.STARTUP_TARGETS, feature_sets)
    grouped_folds = ev.grouped_spec_folds(development)
    availability = load_availability(args.availability)
    share = f1_reduction_share(cases)

    ablation_rows: list[dict[str, Any]] = []
    importance_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []
    set_rows: list[dict[str, Any]] = []

    for target in STARTUP_TARGETS:
        print(f"[ablation] {target}", flush=True)
        selected: dict[str, tuple[core.ModelConfig, dict[str, Any]]] = {}
        set_scores: dict[str, dict[str, Any]] = {}
        for name in COMPARE_SETS:
            config, scored = select_config(development, target, name, grouped_folds)
            lodo = lodo_scores(development, target, config)
            reserved = reserved_score(development, benchmark, target, config)
            scored = {**scored, **lodo, "reserved_mae": reserved["mae"], "reserved_p95": reserved["p95"]}
            selected[name] = (config, scored)
            set_scores[name] = scored
            set_rows.append(
                {
                    "block": "feature_set",
                    "target": target,
                    "target_label": TARGET_LABELS[target],
                    "feature_set": name,
                    "features": "|".join(feature_sets[name]),
                    "degree": config.degree,
                    "ridge_lambda": config.ridge_lambda,
                    "target_transform": config.target_transform,
                    "hybrid_score": round(scored["hybrid_score"], 6),
                    "rolling_equal_day_mae": round(scored["rolling_equal_day_mae"], 6),
                    "rolling_worst_day_mae": round(scored["rolling_worst_day_mae"], 6),
                    "grouped_mae": round(scored["grouped_mae"], 6),
                    "grouped_p95": round(scored["grouped_p95"], 6),
                    "lodo_equal_day_mae": round(scored["lodo_equal_day_mae"], 6),
                    "lodo_worst_day_mae": round(scored["lodo_worst_day_mae"], 6),
                    "lodo_daily_mae": json.dumps(scored["lodo_daily_mae"], ensure_ascii=False),
                    "reserved_mae": round(reserved["mae"], 6),
                    "reserved_p95": round(reserved["p95"], 6),
                    "used_260719_for_selection": False,
                }
            )
            for fold in scored["fold_rows"] + lodo["fold_rows"]:
                ablation_rows.append(
                    {
                        "target": target,
                        "target_label": TARGET_LABELS[target],
                        "block": "feature_set_fold",
                        "feature_set": name,
                        "fold_id": fold["fold_id"],
                        "protocol": fold["protocol"],
                        "mae": round(fold["mae"], 6),
                        "p95": round(fold["p95"], 6),
                        "r2": round(fold["r2"], 6),
                        "used_for_selection": fold["protocol"] != "leave_one_day_out",
                    }
                )

        physics_config = selected["physics7"][0]
        spec_config = selected["spec3"][0]
        drop_scores: dict[str, dict[str, Any]] = {}
        for name in DROP_FROM_SPEC3:
            key = f"spec3_drop_{name}"
            config = with_feature_set(spec_config, key)
            scored = score_config(development, target, config, grouped_folds)
            if scored is None:
                continue
            drop_scores[key] = scored
            base = set_scores["spec3"]["hybrid_score"]
            ablation_rows.append(
                {
                    "target": target,
                    "target_label": TARGET_LABELS[target],
                    "block": "drop_column",
                    "feature_set": key,
                    "dropped_feature": name,
                    "base_set": "spec3",
                    "hybrid_score": round(scored["hybrid_score"], 6),
                    "base_hybrid_score": round(base, 6),
                    "mae_increase_percent": round((scored["hybrid_score"] - base) / base * 100, 3),
                    "rolling_equal_day_mae": round(scored["rolling_equal_day_mae"], 6),
                    "grouped_mae": round(scored["grouped_mae"], 6),
                    "grouped_p95": round(scored["grouped_p95"], 6),
                }
            )
            importance_rows.append(
                {
                    "target": target,
                    "target_label": TARGET_LABELS[target],
                    "feature": name,
                    "feature_label": FEATURE_LABELS[name],
                    "method": "drop_column_spec3",
                    "base_mae": round(base, 6),
                    "mae_increase": round(scored["hybrid_score"] - base, 6),
                    "mae_increase_percent": round((scored["hybrid_score"] - base) / base * 100, 3),
                    "folds_worse": sum(
                        other > base_mae * 1.02
                        for base_mae, other in zip(
                            set_scores["spec3"]["rolling_maes"] + set_scores["spec3"]["grouped_maes"],
                            scored["rolling_maes"] + scored["grouped_maes"],
                        )
                    ),
                    "fold_count": 9,
                    "notes": "drop-column from spec3 using the selected spec3 degree/lambda",
                }
            )
        for name in DROP_FROM_PHYSICS7:
            key = f"physics7_drop_{name}"
            config = with_feature_set(physics_config, key)
            scored = score_config(development, target, config, grouped_folds)
            if scored is None:
                continue
            drop_scores[key] = scored
            base = set_scores["physics7"]["hybrid_score"]
            ablation_rows.append(
                {
                    "target": target,
                    "target_label": TARGET_LABELS[target],
                    "block": "drop_column",
                    "feature_set": key,
                    "dropped_feature": name,
                    "base_set": "physics7",
                    "hybrid_score": round(scored["hybrid_score"], 6),
                    "base_hybrid_score": round(base, 6),
                    "mae_increase_percent": round((scored["hybrid_score"] - base) / base * 100, 3),
                    "rolling_equal_day_mae": round(scored["rolling_equal_day_mae"], 6),
                    "grouped_mae": round(scored["grouped_mae"], 6),
                    "grouped_p95": round(scored["grouped_p95"], 6),
                }
            )
            importance_rows.append(
                {
                    "target": target,
                    "target_label": TARGET_LABELS[target],
                    "feature": name,
                    "feature_label": FEATURE_LABELS[name],
                    "method": "drop_column_physics7",
                    "base_mae": round(base, 6),
                    "mae_increase": round(scored["hybrid_score"] - base, 6),
                    "mae_increase_percent": round((scored["hybrid_score"] - base) / base * 100, 3),
                    "folds_worse": sum(
                        other > base_mae * 1.02
                        for base_mae, other in zip(
                            set_scores["physics7"]["rolling_maes"] + set_scores["physics7"]["grouped_maes"],
                            scored["rolling_maes"] + scored["grouped_maes"],
                        )
                    ),
                    "fold_count": 9,
                    "notes": "drop-column from physics7; derived columns still contain thickness/width",
                }
            )

        perm_rows = permutation_importance(
            development,
            grouped_folds,
            target,
            physics_config,
            FEATURE_SETS["physics7"],
        )
        importance_rows.extend(perm_rows)

        for feature in DEPLOY_FEATURES + DIAGNOSTIC_FEATURES:
            decision_rows.append(
                classify_feature(
                    feature=feature,
                    target=target,
                    availability=availability,
                    set_scores=set_scores,
                    drop_scores=drop_scores,
                    perm_rows=perm_rows,
                )
            )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ablation_path = args.output_dir / "feature_ablation.csv"
    importance_path = args.output_dir / "feature_importance_by_target.csv"
    decision_path = args.output_dir / "input_decision_matrix.csv"
    report_path = args.output_dir / "feature_ablation_report.md"
    write_csv(ablation_path, ablation_rows + set_rows)
    write_csv(importance_path, importance_rows)
    write_csv(decision_path, decision_rows)
    write_report(
        report_path,
        share=share,
        set_table=set_rows,
        decisions=decision_rows,
        perm_rows=importance_rows,
    )

    print(
        f"F1_share mean={share['mean']:.4f} std={share['std']:.4f} "
        f"p05={share['p05']:.4f} p95={share['p95']:.4f} n={share['n']}"
    )
    print(f"wrote {ablation_path}")
    print(f"wrote {importance_path}")
    print(f"wrote {decision_path}")
    print(f"wrote {report_path}")
    print("FEATURE_ABLATION_READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
