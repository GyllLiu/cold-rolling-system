#!/usr/bin/env python3
"""Step 4: per-target model competition on existing data.

Selection uses only 260712-260718.  260719 is a date-isolated reused
benchmark scored once after the development choice.  5NN is offline only.
Tree families are recorded if local packages exist; this script will not
install packages or change the system environment.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import evaluate_existing_models_v5 as ev
import feature_ablation_v5 as ab
import train_validate_direct_prediction as core
import train_validate_v4 as v4


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEVELOPMENT_DAYS = ev.DEVELOPMENT_DAYS
BENCHMARK_DAY = ev.BENCHMARK_DAY
STARTUP_TARGETS = ev.STARTUP_TARGETS
TARGET_LABELS = ev.TARGET_LABELS
TARGET_UNITS = ev.TARGET_UNITS

OFFICIAL_FEATURE_SET = {
    "intermediate_thickness_set_mode": "physics7",
    "tm1_closed_gap_set_median": "physics7",
    "tm2_closed_gap_set_median": "physics7",
    "uncoiler_tension_set_median": "spec3",
    "interstand_tension_set_median": "spec3",
    "coiler_tension_set_median": "spec3",
}

GAP_TARGETS = {"tm1_closed_gap_set_median", "tm2_closed_gap_set_median"}
TENSION_TARGETS = {
    "uncoiler_tension_set_median",
    "interstand_tension_set_median",
    "coiler_tension_set_median",
}


def try_import(name: str) -> Any | None:
    try:
        return __import__(name)
    except ImportError:
        return None


def constrain(target: str, value: float, case: core.Case) -> float:
    entry = case.values["entry_thickness_set_mode"]
    exit_value = case.values["exit_thickness_set_mode"]
    if target == "intermediate_thickness_set_mode":
        value = min(entry - 0.001, max(exit_value + 0.001, value))
    if target in GAP_TARGETS or target in TENSION_TARGETS:
        value = max(0.0, value)
    return value


def ols(design: Sequence[Sequence[float]], response: Sequence[float], ridge: float = 1e-8) -> list[float]:
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
            gram[left][left] += ridge
    return core._solve_linear_system(gram, rhs)


def share_row(case: core.Case) -> list[float]:
    return [
        1.0,
        case.values["total_reduction_ratio"],
        case.values["log_thickness_ratio"],
    ]


def observed_share(case: core.Case) -> float:
    entry = case.values["entry_thickness_set_mode"]
    exit_value = case.values["exit_thickness_set_mode"]
    mid = case.values["intermediate_thickness_set_mode"]
    return (entry - mid) / (entry - exit_value)


def predict_share(coefficients: Sequence[float], case: core.Case) -> float:
    row = share_row(case)
    raw = sum(value * coefficient for value, coefficient in zip(row, coefficients))
    return min(0.95, max(0.05, raw))


def predict_intermediate(coefficients: Sequence[float], case: core.Case) -> float:
    entry = case.values["entry_thickness_set_mode"]
    exit_value = case.values["exit_thickness_set_mode"]
    return entry - predict_share(coefficients, case) * (entry - exit_value)


@dataclass
class PhysicalModel:
    target: str
    share_coefficients: list[float]
    output_coefficients: list[float]

    def predict(self, case: core.Case) -> float:
        if self.target == "intermediate_thickness_set_mode":
            value = predict_intermediate(self.share_coefficients, case)
        elif self.target == "tm1_closed_gap_set_median":
            mid = predict_intermediate(self.share_coefficients, case)
            value = self.output_coefficients[0] + self.output_coefficients[1] * mid
        elif self.target == "tm2_closed_gap_set_median":
            exit_value = case.values["exit_thickness_set_mode"]
            value = self.output_coefficients[0] + self.output_coefficients[1] * exit_value
        else:
            entry_section = case.values["entry_cross_section_proxy"]
            exit_section = case.values["exit_cross_section_proxy"]
            value = (
                self.output_coefficients[0]
                + self.output_coefficients[1] * entry_section
                + self.output_coefficients[2] * exit_section
            )
        return constrain(self.target, value, case)


def fit_physical(train: Sequence[core.Case], target: str) -> PhysicalModel:
    share_y = [observed_share(case) for case in train]
    share_x = [share_row(case) for case in train]
    share_coefficients = ols(share_x, share_y)
    if target == "intermediate_thickness_set_mode":
        return PhysicalModel(target, share_coefficients, [])
    if target == "tm1_closed_gap_set_median":
        design = [
            [1.0, predict_intermediate(share_coefficients, case)]
            for case in train
        ]
        response = [case.values[target] for case in train]
        return PhysicalModel(target, share_coefficients, ols(design, response))
    if target == "tm2_closed_gap_set_median":
        design = [[1.0, case.values["exit_thickness_set_mode"]] for case in train]
        response = [case.values[target] for case in train]
        return PhysicalModel(target, share_coefficients, ols(design, response))
    design = [
        [1.0, case.values["entry_cross_section_proxy"], case.values["exit_cross_section_proxy"]]
        for case in train
    ]
    response = [case.values[target] for case in train]
    return PhysicalModel(target, share_coefficients, ols(design, response))


def knn_predict(
    train: Sequence[core.Case],
    queries: Sequence[core.Case],
    target: str,
    k: int = 5,
) -> list[float]:
    names = ("entry_thickness_set_mode", "exit_thickness_set_mode", "width_set_mode")
    means = {name: core._mean([case.values[name] for case in train]) for name in names}
    scales = {
        name: core._std([case.values[name] for case in train], means[name])
        for name in names
    }

    def distance(left: core.Case, right: core.Case) -> float:
        return math.sqrt(
            statistics.fmean(
                [
                    ((left.values[name] - right.values[name]) / scales[name]) ** 2
                    for name in names
                ]
            )
        )

    predictions = []
    for query in queries:
        nearest = sorted(train, key=lambda item: distance(query, item))[:k]
        value = statistics.median(case.values[target] for case in nearest)
        predictions.append(constrain(target, value, query))
    return predictions


Predictor = Callable[[Sequence[core.Case], Sequence[core.Case], str], list[float]]


def ridge_predictor(config: core.ModelConfig) -> Predictor:
    def predict(train: Sequence[core.Case], queries: Sequence[core.Case], target: str) -> list[float]:
        model = core.fit_model(train, target, config)
        return [constrain(target, core.predict_model(model, case, target), case) for case in queries]

    return predict


def physical_predictor() -> Predictor:
    def predict(train: Sequence[core.Case], queries: Sequence[core.Case], target: str) -> list[float]:
        model = fit_physical(train, target)
        return [model.predict(case) for case in queries]

    return predict


def knn_predictor() -> Predictor:
    def predict(train: Sequence[core.Case], queries: Sequence[core.Case], target: str) -> list[float]:
        return knn_predict(train, queries, target)

    return predict


def frozen_ridge_predictor(artifact: dict[str, Any]) -> Predictor:
    def predict(_train: Sequence[core.Case], queries: Sequence[core.Case], target: str) -> list[float]:
        return [
            constrain(target, ev.predict_frozen_artifact(artifact, case, target), case)
            for case in queries
        ]

    return predict


def evaluate_predictor(
    development: Sequence[core.Case],
    grouped_folds: Sequence[Sequence[core.Case]],
    target: str,
    predictor: Predictor,
) -> dict[str, Any]:
    rolling_maes = []
    rolling_p95s = []
    rolling_bias = []
    rolling_rows = []
    for index, (train_days, val_day) in enumerate(v4.ROLLING_FOLDS, start=1):
        train = [case for case in development if case.dataset in train_days]
        validation = [case for case in development if case.dataset == val_day]
        predicted = predictor(train, validation, target)
        pairs = [
            (case.dataset, case.values[target], value)
            for case, value in zip(validation, predicted)
        ]
        scored = ev.evaluate_predictions(pairs)
        rolling_maes.append(scored["mae"])
        rolling_p95s.append(scored["p95"])
        rolling_bias.append(scored["bias"])
        rolling_rows.append({"fold_id": f"rolling_{index}", "day": val_day, **scored})

    grouped_maes = []
    grouped_p95s = []
    grouped_bias = []
    grouped_rows = []
    for index, validation in enumerate(grouped_folds, start=1):
        validation_ids = {id(case) for case in validation}
        train = [case for case in development if id(case) not in validation_ids]
        predicted = predictor(train, validation, target)
        pairs = [
            (case.dataset, case.values[target], value)
            for case, value in zip(validation, predicted)
        ]
        scored = ev.evaluate_predictions(pairs)
        grouped_maes.append(scored["mae"])
        grouped_p95s.append(scored["p95"])
        grouped_bias.append(scored["bias"])
        grouped_rows.append({"fold_id": f"spec_{index}", **scored})

    lodo_maes = []
    lodo_daily = {}
    for day in DEVELOPMENT_DAYS:
        train = [case for case in development if case.dataset != day]
        validation = [case for case in development if case.dataset == day]
        predicted = predictor(train, validation, target)
        pairs = [
            (case.dataset, case.values[target], value)
            for case, value in zip(validation, predicted)
        ]
        scored = ev.evaluate_predictions(pairs)
        lodo_maes.append(scored["mae"])
        lodo_daily[day] = scored["mae"]

    equal_day = statistics.fmean(rolling_maes)
    grouped_mae = statistics.fmean(grouped_maes)
    return {
        "rolling_equal_day_mae": equal_day,
        "rolling_worst_day_mae": max(rolling_maes),
        "rolling_p95": statistics.fmean(rolling_p95s),
        "rolling_bias": statistics.fmean(rolling_bias),
        "rolling_maes": rolling_maes,
        "grouped_mae": grouped_mae,
        "grouped_p95": statistics.fmean(grouped_p95s),
        "grouped_bias": statistics.fmean(grouped_bias),
        "hybrid_score": 0.60 * equal_day + 0.40 * grouped_mae,
        "lodo_equal_day_mae": statistics.fmean(lodo_maes),
        "lodo_worst_day_mae": max(lodo_maes),
        "lodo_daily_mae": {day: round(value, 6) for day, value in lodo_daily.items()},
        "rolling_rows": rolling_rows,
        "grouped_rows": grouped_rows,
    }


def reserved_predictor_score(
    development: Sequence[core.Case],
    benchmark: Sequence[core.Case],
    target: str,
    predictor: Predictor,
) -> dict[str, Any]:
    predicted = predictor(development, benchmark, target)
    pairs = [
        (case.dataset, case.values[target], value)
        for case, value in zip(benchmark, predicted)
    ]
    return ev.evaluate_predictions(pairs)


def dangerous_bias(target: str, baseline_bias: float, new_bias: float, baseline_mae: float) -> bool:
    margin = 0.10 * baseline_mae
    if target in GAP_TARGETS or target == "intermediate_thickness_set_mode":
        # More negative bias means systematically tighter / thinner than history.
        return new_bias < baseline_bias - margin and new_bias < 0
    return abs(new_bias) > abs(baseline_bias) + margin


def replacement_checks(
    target: str,
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    reserved_baseline: dict[str, Any],
    reserved_candidate: dict[str, Any],
) -> tuple[bool, list[str]]:
    reasons = []
    improve = (baseline["hybrid_score"] - candidate["hybrid_score"]) / baseline["hybrid_score"] * 100
    if improve < 5:
        reasons.append(f"开发综合MAE改善{improve:.2f}% < 5%")
    better_days = sum(
        new < old for old, new in zip(baseline["rolling_maes"], candidate["rolling_maes"])
    )
    if better_days < 3:
        reasons.append(f"滚动日期折仅{better_days}/4改善")
    if candidate["rolling_worst_day_mae"] > baseline["rolling_worst_day_mae"] * 1.10:
        reasons.append("最差日期MAE恶化超过10%")
    if candidate["grouped_p95"] > baseline["grouped_p95"] * 1.05:
        reasons.append("整规格P95恶化超过5%")
    if candidate["rolling_p95"] > baseline["rolling_p95"] * 1.05:
        reasons.append("滚动P95恶化超过5%")
    if dangerous_bias(target, baseline["rolling_bias"], candidate["rolling_bias"], baseline["rolling_equal_day_mae"]):
        reasons.append("Bias出现危险方向偏移")
    if reserved_candidate["mae"] > reserved_baseline["mae"] * 1.10:
        reasons.append("260719复用基准MAE恶化超过10%，视为崩溃")
    if reserved_candidate["p95"] > reserved_baseline["p95"] * 1.15:
        reasons.append("260719复用基准P95明显变差")
    return not reasons, reasons


def write_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
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
    comparison_rows: Sequence[dict[str, Any]],
    selection: dict[str, Any],
    unavailable: Sequence[str],
) -> None:
    lines = [
        "# 现有数据按输出模型竞争",
        "",
        f"生成时间：{datetime.now(timezone.utc).isoformat()}",
        "选择只用260712–260718；260719是日期隔离复用基准，只在开发期选定挑战者之后评估一次。",
        "5NN只作离线基线，不用于线上推理。未使用随机8/2切分。",
        "",
    ]
    if unavailable:
        lines.append("## 本地不可用的模型族")
        lines.append("")
        for name in unavailable:
            lines.append(f"- {name}：本地未安装依赖，未安装、未改系统环境，因此未参赛。")
        lines.append("")
    lines.extend(
        [
            "## 替换门槛",
            "",
            "- 开发综合MAE至少改善5%",
            "- 4个滚动日期折中多数改善",
            "- 最差日期MAE不恶化超过10%",
            "- P95不恶化超过5%",
            "- Bias不出现危险方向偏移（辊缝/中间厚度不得系统性偏薄/偏小）",
            "- 260719复用基准不明显崩溃",
            "- 只用正式可得输入；树模型若胜出但无审计过的TS推理，只标影子候选",
            "",
            "## 开发期综合分（越小越好）",
            "",
            "| 输出 | v0.4岭回归 | 正式集岭回归 | 物理压下分配 | 5NN离线 | 是否替换 |",
            "| --- | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    by_target: dict[str, dict[str, dict[str, Any]]] = {}
    for row in comparison_rows:
        by_target.setdefault(row["target"], {})[row["model_family"]] = row
    for target in STARTUP_TARGETS:
        item = by_target[target]
        chosen = selection[target]["online_choice"]
        lines.append(
            f"| {TARGET_LABELS[target]} | {item['ridge_v04']['hybrid_score']:.6f} | "
            f"{item['ridge_official']['hybrid_score']:.6f} | "
            f"{item['physical_allocation']['hybrid_score']:.6f} | "
            f"{item['knn5_offline']['hybrid_score']:.6f} | "
            f"{'是 → ' + chosen if selection[target]['replace'] else '否，保持v0.4'} |"
        )
    lines.extend(["", "## 逐输出结论", ""])
    for target in STARTUP_TARGETS:
        row = selection[target]
        lines.append(f"### {TARGET_LABELS[target]}")
        lines.append("")
        lines.append(f"- 正式输入：`{row['official_feature_set']}` = {', '.join(row['official_features'])}")
        lines.append(f"- 开发期挑战者：{row['challenger']}")
        lines.append(f"- 线上选择：{row['online_choice']}")
        lines.append(f"- 替换v0.4：{'是' if row['replace'] else '否'}")
        if row["fail_reasons"]:
            lines.append("- 未通过门槛：" + "；".join(row["fail_reasons"]))
        else:
            lines.append("- 已满足全部替换门槛。" if row["replace"] else "- 无更优可上线挑战者。")
        if row.get("shadow_candidates"):
            lines.append("- 影子候选：" + ", ".join(row["shadow_candidates"]))
        reserved = by_target[target]
        lines.append(
            f"- 260719复用：v0.4 MAE {reserved['ridge_v04']['reserved_mae']:.6f}；"
            f"挑战者 {row['challenger']} MAE {reserved[row['challenger']]['reserved_mae']:.6f}"
        )
        lines.append("")
    lines.extend(
        [
            "禁止因为某一个输出变好而整体切换模型族。F1/F2输出仍是高速稳定运行辊缝参考，",
            "不是上卷安全首设。5NN禁止进入线上推理。",
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
        "--artifact",
        type=Path,
        default=PROJECT_ROOT / "backend/app/model_artifacts/recommendation_direct_v4.json",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=WORKSPACE_ROOT / "outputs/v5_existing_data",
    )
    args = parser.parse_args()

    artifact = json.loads(args.artifact.read_text(encoding="utf-8"))
    if artifact.get("model_version") != "direct-ridge-startup-flatness-v0.4":
        parser.error("baseline must remain frozen v0.4")

    sklearn = try_import("sklearn")
    xgboost = try_import("xgboost")
    unavailable = []
    if sklearn is None:
        unavailable.extend(["ExtraTrees", "HistGradientBoosting"])
    if xgboost is None:
        unavailable.append("XGBoost")

    cases = ab.load_cases(args.features)
    development = [case for case in cases if case.dataset in DEVELOPMENT_DAYS]
    benchmark = [case for case in cases if case.dataset == BENCHMARK_DAY]
    feature_sets = dict(v4.STARTUP_FEATURE_SETS)
    feature_sets["spec3"] = ab.FEATURE_SETS["spec3"]
    feature_sets["physics7"] = ab.FEATURE_SETS["physics7"]
    v4.configure_core(v4.STARTUP_TARGETS, feature_sets)
    grouped_folds = ev.grouped_spec_folds(development)
    v04_configs = ev.frozen_configs(artifact)

    comparison_rows: list[dict[str, Any]] = []
    fold_rows: list[dict[str, Any]] = []
    selection: dict[str, Any] = {}

    for target in STARTUP_TARGETS:
        print(f"[compare] {target}", flush=True)
        official_set = OFFICIAL_FEATURE_SET[target]
        ridge_official_config, _ = ab.select_config(development, target, official_set, grouped_folds)
        families: list[tuple[str, Predictor, str, bool, str]] = [
            (
                "ridge_v04",
                ridge_predictor(v04_configs[target]),
                v04_configs[target].feature_set,
                True,
                f"{v04_configs[target].feature_set}/d{v04_configs[target].degree}/λ={v04_configs[target].ridge_lambda:g}/{v04_configs[target].target_transform}",
            ),
            (
                "ridge_official",
                ridge_predictor(ridge_official_config),
                official_set,
                True,
                f"{official_set}/d{ridge_official_config.degree}/λ={ridge_official_config.ridge_lambda:g}/{ridge_official_config.target_transform}",
            ),
            (
                "physical_allocation",
                physical_predictor(),
                "physics_reduction_allocation",
                True,
                "share~reduction+log; gap~outgoing_thickness; tension~section",
            ),
            (
                "knn5_offline",
                knn_predictor(),
                "spec3",
                False,
                "standardized 5NN median on entry/exit/width; offline only",
            ),
        ]

        scored_families: dict[str, dict[str, Any]] = {}
        reserved_families: dict[str, dict[str, Any]] = {}
        for name, predictor, feature_set, online_ok, detail in families:
            scored = evaluate_predictor(development, grouped_folds, target, predictor)
            if name == "ridge_v04":
                reserved = reserved_predictor_score(
                    development, benchmark, target, frozen_ridge_predictor(artifact)
                )
            else:
                reserved = reserved_predictor_score(development, benchmark, target, predictor)
            scored_families[name] = scored
            reserved_families[name] = reserved
            comparison_rows.append(
                {
                    "target": target,
                    "target_label": TARGET_LABELS[target],
                    "unit": TARGET_UNITS[target],
                    "model_family": name,
                    "online_allowed": online_ok,
                    "feature_set": feature_set,
                    "detail": detail,
                    "hybrid_score": round(scored["hybrid_score"], 6),
                    "rolling_equal_day_mae": round(scored["rolling_equal_day_mae"], 6),
                    "rolling_worst_day_mae": round(scored["rolling_worst_day_mae"], 6),
                    "rolling_p95": round(scored["rolling_p95"], 6),
                    "rolling_bias": round(scored["rolling_bias"], 6),
                    "grouped_mae": round(scored["grouped_mae"], 6),
                    "grouped_p95": round(scored["grouped_p95"], 6),
                    "lodo_equal_day_mae": round(scored["lodo_equal_day_mae"], 6),
                    "lodo_worst_day_mae": round(scored["lodo_worst_day_mae"], 6),
                    "reserved_mae": round(reserved["mae"], 6),
                    "reserved_p95": round(reserved["p95"], 6),
                    "reserved_bias": round(reserved["bias"], 6),
                    "reserved_r2": round(reserved["r2"], 6),
                    "used_260719_for_selection": False,
                }
            )
            for fold in scored["rolling_rows"]:
                fold_rows.append(
                    {
                        "target": target,
                        "model_family": name,
                        "protocol": "rolling_origin",
                        "fold_id": fold["fold_id"],
                        "mae": round(fold["mae"], 6),
                        "p95": round(fold["p95"], 6),
                        "bias": round(fold["bias"], 6),
                    }
                )

        baseline = scored_families["ridge_v04"]
        reserved_baseline = reserved_families["ridge_v04"]
        online_names = [name for name, _pred, _fs, online_ok, _d in families if online_ok]
        challenger = min(online_names, key=lambda name: scored_families[name]["hybrid_score"])
        if challenger == "ridge_v04":
            replace = False
            reasons = ["无可上线挑战者优于v0.4综合分"]
        else:
            replace, reasons = replacement_checks(
                target,
                baseline,
                scored_families[challenger],
                reserved_baseline,
                reserved_families[challenger],
            )
        selection[target] = {
            "label": TARGET_LABELS[target],
            "official_feature_set": official_set,
            "official_features": list(feature_sets[official_set]),
            "challenger": challenger,
            "online_choice": challenger if replace else "ridge_v04",
            "replace": replace,
            "fail_reasons": reasons,
            "shadow_candidates": [],
            "offline_baseline": "knn5_offline",
            "unavailable_families": list(unavailable),
            "tree_models_online": False,
            "historical_neighbour_inference": False,
        }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    comparison_path = args.output_dir / "model_comparison.csv"
    selection_path = args.output_dir / "per_target_selection.json"
    report_path = args.output_dir / "model_comparison_report.md"
    fold_path = args.output_dir / "model_comparison_folds.csv"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "baseline_artifact": "recommendation_direct_v4.json",
        "baseline_model_version": "direct-ridge-startup-flatness-v0.4",
        "reserved_benchmark": BENCHMARK_DAY,
        "reserved_benchmark_is_not_blind_test": True,
        "unavailable_families": unavailable,
        "replace_any": any(row["replace"] for row in selection.values()),
        "targets": selection,
    }
    write_csv(comparison_path, comparison_rows)
    write_csv(fold_path, fold_rows)
    selection_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_report(
        report_path,
        comparison_rows=comparison_rows,
        selection=selection,
        unavailable=unavailable,
    )
    print(f"unavailable={unavailable}")
    for target, row in selection.items():
        print(
            f"{target} challenger={row['challenger']} replace={row['replace']} "
            f"reasons={row['fail_reasons']}"
        )
    print(f"wrote {comparison_path}")
    print(f"wrote {selection_path}")
    print(f"wrote {report_path}")
    print("MODEL_COMPARISON_READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
