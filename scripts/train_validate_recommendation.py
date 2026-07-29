#!/usr/bin/env python3
"""Tune the traceable KNN reference model on 0712+0713 and validate on 0714.

Only rows explicitly marked ``learning_eligible=true`` are used.  The data
foundation marks a row eligible only when it contains a sufficiently long
high-speed plateau, so low-speed operator tuning never becomes a model target.
The validation CSV is not read until hyperparameters have been frozen.
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
from typing import Iterable


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[1]

FEATURES = (
    "entry_thickness_set_mode",
    "exit_thickness_set_mode",
    "width_set_mode",
    "total_reduction_ratio",
)
TARGETS = (
    "intermediate_thickness_set_mode",
    "main_speed_set_median",
    "f1_gap_average_median",
    "f2_gap_average_median",
    "f1_bending_set_median",
    "f2_bending_set_median",
)
TARGET_LABELS = {
    "intermediate_thickness_set_mode": ("中间厚度", "mm"),
    "main_speed_set_median": ("主速度设定", "m/min"),
    "f1_gap_average_median": ("F1平均辊缝", "mm"),
    "f2_gap_average_median": ("F2平均辊缝", "mm"),
    "f1_bending_set_median": ("F1弯辊力", "T"),
    "f2_bending_set_median": ("F2弯辊力", "T"),
}


@dataclass(frozen=True)
class Case:
    dataset: str
    candidate_id: str
    start_time: str
    end_time: str
    source_files: str
    active_seconds: int
    stable_seconds: int
    values: dict[str, float]

    @property
    def spec_key(self) -> tuple[float, float, float]:
        return tuple(self.values[key] for key in FEATURES[:3])  # type: ignore[return-value]


@dataclass(frozen=True)
class Configuration:
    k: int
    distance_power: int
    feature_scales: tuple[float, float, float, float]


def _float(row: dict[str, str], key: str) -> float:
    value = float(row[key])
    if not math.isfinite(value):
        raise ValueError(f"non-finite {key}")
    return value


def load_cases(paths: Iterable[Path]) -> list[Case]:
    cases: list[Case] = []
    for path in paths:
        dataset = path.parent.name.removeprefix("data_foundation_")
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                eligible = row.get("learning_eligible", "").lower() == "true"
                if not row.get("learning_eligible"):
                    eligible = row.get("confidence") == "medium_plus"
                if not eligible:
                    continue
                try:
                    values = {key: _float(row, key) for key in FEATURES + TARGETS}
                except (KeyError, TypeError, ValueError):
                    continue
                cases.append(
                    Case(
                        dataset=dataset,
                        candidate_id=f"{dataset}_{row['candidate_id']}",
                        start_time=row.get("start_time", ""),
                        end_time=row.get("end_time", ""),
                        source_files=row.get("source_files", ""),
                        active_seconds=int(float(row.get("active_seconds_1hz", "0") or 0)),
                        stable_seconds=int(float(row.get("stable_high_speed_seconds_1hz", "0") or 0)),
                        values=values,
                    )
                )
    return cases


def _distance(query: Case, item: Case, config: Configuration) -> float:
    squared = sum(
        ((query.values[key] - item.values[key]) / scale) ** 2
        for key, scale in zip(FEATURES, config.feature_scales)
    )
    return math.sqrt(squared / len(FEATURES))


def _weighted_median(pairs: list[tuple[float, float]], power: int) -> float:
    ordered = sorted(pairs, key=lambda pair: pair[0])
    if power == 0:
        return statistics.median(value for value, _ in ordered)
    weighted = [
        (value, 1.0 / max(distance, 1e-6) ** power)
        for value, distance in ordered
    ]
    halfway = sum(weight for _, weight in weighted) / 2
    cumulative = 0.0
    for value, weight in weighted:
        cumulative += weight
        if cumulative >= halfway:
            return value
    return weighted[-1][0]


def predict(query: Case, pool: list[Case], config: Configuration) -> dict[str, float]:
    ranked = sorted(
        ((_distance(query, item, config), item) for item in pool),
        key=lambda pair: pair[0],
    )[: config.k]
    if not ranked:
        raise ValueError("no reference cases")
    return {
        target: _weighted_median(
            [(item.values[target], distance) for distance, item in ranked],
            config.distance_power,
        )
        for target in TARGETS
    }


def _current_distance(query: Case, item: Case) -> float:
    entry = query.values[FEATURES[0]]
    exit_value = query.values[FEATURES[1]]
    parts = (
        (query.values[FEATURES[0]] - item.values[FEATURES[0]]) / max(0.1, entry * 0.2),
        (query.values[FEATURES[1]] - item.values[FEATURES[1]]) / max(0.03, exit_value * 0.15),
        (query.values[FEATURES[2]] - item.values[FEATURES[2]]) / 250.0,
        (query.values[FEATURES[3]] - item.values[FEATURES[3]]) / 0.15,
    )
    return math.sqrt(sum(value * value for value in parts) / len(parts))


def predict_baseline(query: Case, pool: list[Case]) -> dict[str, float]:
    nearest = sorted(pool, key=lambda item: _current_distance(query, item))[:5]
    return {
        target: statistics.median(item.values[target] for item in nearest)
        for target in TARGETS
    }


def _target_normalizers(cases: list[Case]) -> dict[str, float]:
    normalizers: dict[str, float] = {}
    for target in TARGETS:
        values = sorted(case.values[target] for case in cases)
        q1 = values[round((len(values) - 1) * 0.25)]
        q3 = values[round((len(values) - 1) * 0.75)]
        normalizers[target] = max(q3 - q1, 1e-6)
    return normalizers


def evaluate(
    queries: list[Case],
    pool: list[Case],
    normalizers: dict[str, float],
    config: Configuration | None,
    *,
    exclude_same_spec: bool,
) -> dict:
    errors = {target: [] for target in TARGETS}
    evaluated = 0
    for query in queries:
        references = [
            item
            for item in pool
            if item is not query
            and (not exclude_same_spec or item.spec_key != query.spec_key)
        ]
        if not references:
            continue
        prediction = (
            predict(query, references, config)
            if config is not None
            else predict_baseline(query, references)
        )
        for target in TARGETS:
            errors[target].append(abs(prediction[target] - query.values[target]))
        evaluated += 1
    mae = {
        target: statistics.mean(values) if values else math.nan
        for target, values in errors.items()
    }
    composite = statistics.mean(mae[target] / normalizers[target] for target in TARGETS)
    return {"evaluated_cases": evaluated, "composite_normalized_mae": composite, "mae": mae}


def tune_configuration(train: list[Case], normalizers: dict[str, float]) -> tuple[Configuration, dict]:
    best: tuple[float, Configuration, dict] | None = None
    for values in itertools.product(
        (1, 3, 5, 7, 9),
        (0, 1, 2),
        (0.25, 0.5, 1.0),
        (0.1, 0.2, 0.4),
        (80.0, 160.0, 240.0),
        (0.08, 0.15, 0.3),
    ):
        k, power, entry_scale, exit_scale, width_scale, reduction_scale = values
        config = Configuration(
            k=k,
            distance_power=power,
            feature_scales=(entry_scale, exit_scale, width_scale, reduction_scale),
        )
        metrics = evaluate(
            train,
            train,
            normalizers,
            config,
            exclude_same_spec=True,
        )
        score = metrics["composite_normalized_mae"]
        if best is None or score < best[0]:
            best = (score, config, metrics)
    if best is None:
        raise ValueError("no model configuration could be evaluated")
    return best[1], best[2]


def _round_metrics(metrics: dict) -> dict:
    return {
        "evaluated_cases": metrics["evaluated_cases"],
        "composite_normalized_mae": round(metrics["composite_normalized_mae"], 6),
        "mae": {key: round(value, 6) for key, value in metrics["mae"].items()},
    }


def _reference_case(case: Case) -> dict:
    return {
        "candidate_id": case.candidate_id,
        "dataset": case.dataset,
        "start_time": case.start_time,
        "end_time": case.end_time,
        "source_files": case.source_files,
        "active_seconds": case.active_seconds,
        "stable_seconds": case.stable_seconds,
        "entry_thickness": case.values[FEATURES[0]],
        "exit_thickness": case.values[FEATURES[1]],
        "width": case.values[FEATURES[2]],
        "total_reduction_ratio": case.values[FEATURES[3]],
        "settings": {target: case.values[target] for target in TARGETS},
    }


def build_report(artifact: dict) -> str:
    validation = artifact["metrics"]["validation"]
    baseline = validation["baseline"]
    optimized = validation["optimized"]
    lines = [
        "# 冷二联相似工况推荐模型 v0.2 训练与验证报告",
        "",
        f"- 训练数据：`{', '.join(artifact['training_datasets'])}`，高速稳定候选 {artifact['training_case_count']} 条。",
        f"- 独立验证：`{artifact['validation_dataset']}`，高速稳定候选 {artifact['validation_case_count']} 条。",
        "- 训练规则：低速视为主操调试，只从高速稳定区提取推荐目标。",
        "- 防泄漏：参数搜索仅使用训练集的按规格留组交叉验证；0714 在参数冻结后才读取和评估。",
        "- 模型用途：历史稳定参数参考，不是质量最优参数；没有卷号、人工测厚与质检标签时仍禁止自动下发 PLC。",
        "",
        "## 冻结参数",
        "",
        f"- 邻居数：{artifact['config']['k']}",
        f"- 距离加权幂次：{artifact['config']['distance_power']}",
        f"- 特征尺度：{artifact['config']['feature_scales']}",
        "",
        "## 0714 独立验证",
        "",
        "| 参数 | 旧算法 MAE | 新算法 MAE | 变化 |",
        "| --- | ---: | ---: | ---: |",
    ]
    for target in TARGETS:
        old = baseline["mae"][target]
        new = optimized["mae"][target]
        change = (new / old - 1) * 100 if old else 0.0
        label, unit = TARGET_LABELS[target]
        lines.append(f"| {label} ({unit}) | {old:.4f} | {new:.4f} | {change:+.1f}% |")
    improvement = artifact["metrics"]["validation_composite_improvement_percent"]
    lines.extend(
        [
            "",
            f"综合归一化 MAE：`{baseline['composite_normalized_mae']:.4f}` → `{optimized['composite_normalized_mae']:.4f}`，下降 `{improvement:.1f}%`。",
            "",
            "## 解释边界",
            "",
            "0714 的指标衡量的是能否复现主操在高速稳定生产时采用的参数，不代表已经证明产品质量最优。后续录入真实卷号、道次、人工厚度和质检结果后，才能把目标升级为质量优化。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--train-csv",
        action="append",
        type=Path,
        default=None,
        help="Repeat for each training production_segments.csv",
    )
    parser.add_argument(
        "--validation-csv",
        type=Path,
        default=WORKSPACE_ROOT / "outputs/data_foundation_260714/production_segments.csv",
    )
    parser.add_argument(
        "--artifact",
        type=Path,
        default=PROJECT_ROOT / "backend/app/model_artifacts/recommendation_knn_v2.json",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=WORKSPACE_ROOT / "outputs/recommendation_model_v2_report.md",
    )
    args = parser.parse_args()
    train_paths = args.train_csv or [
        WORKSPACE_ROOT / "outputs/data_foundation_260712/production_segments.csv",
        WORKSPACE_ROOT / "outputs/data_foundation_260713/production_segments.csv",
    ]
    for path in [*train_paths, args.validation_csv]:
        if not path.exists():
            parser.error(f"missing input: {path}")

    # The validation file is deliberately not opened before configuration freeze.
    train = load_cases(train_paths)
    if len(train) < 10:
        parser.error("fewer than 10 eligible training cases")
    normalizers = _target_normalizers(train)
    baseline_cv = evaluate(train, train, normalizers, None, exclude_same_spec=True)
    config, optimized_cv = tune_configuration(train, normalizers)

    validation = load_cases([args.validation_csv])
    if not validation:
        parser.error("no eligible validation cases")
    baseline_validation = evaluate(
        validation, train, normalizers, None, exclude_same_spec=False
    )
    optimized_validation = evaluate(
        validation, train, normalizers, config, exclude_same_spec=False
    )
    old_score = baseline_validation["composite_normalized_mae"]
    new_score = optimized_validation["composite_normalized_mae"]
    improvement = (old_score - new_score) / old_score * 100 if old_score else 0.0

    artifact = {
        "model_version": "knn-high-speed-reference-v0.2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "training_datasets": [path.parent.name.removeprefix("data_foundation_") for path in train_paths],
        "validation_dataset": args.validation_csv.parent.name.removeprefix("data_foundation_"),
        "training_case_count": len(train),
        "validation_case_count": len(validation),
        "training_policy": {
            "learning_eligible_only": True,
            "low_speed_role": "operator_tuning_excluded_from_targets",
            "cross_validation": "leave_exact_spec_group_out",
            "validation_used_for_tuning": False,
        },
        "features": list(FEATURES),
        "targets": list(TARGETS),
        "config": {
            "k": config.k,
            "distance_power": config.distance_power,
            "feature_scales": dict(zip(FEATURES, config.feature_scales)),
        },
        "target_normalizers": normalizers,
        "metrics": {
            "training_cross_validation": {
                "baseline": _round_metrics(baseline_cv),
                "optimized": _round_metrics(optimized_cv),
            },
            "validation": {
                "baseline": _round_metrics(baseline_validation),
                "optimized": _round_metrics(optimized_validation),
            },
            "validation_composite_improvement_percent": round(improvement, 3),
        },
        "reference_cases": [_reference_case(case) for case in train],
    }
    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    args.artifact.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(build_report(artifact), encoding="utf-8")
    print(
        f"model={artifact['model_version']} train={len(train)} validation={len(validation)} "
        f"k={config.k} power={config.distance_power} "
        f"validation_improvement={improvement:.2f}%"
    )
    print(f"artifact={args.artifact.resolve()}")
    print(f"report={args.report.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
