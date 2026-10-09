#!/usr/bin/env python3
"""Reproduce frozen v0.4 metrics on the 260719 date-isolated reused benchmark.

This script does not retrain. It loads the frozen coefficient artifact and the
existing v0.4 feature table, then recomputes MAE / P95 / R2 / MAPE. 260719 is
a reused date-isolated benchmark, not a never-seen blind test.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import platform
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEVELOPMENT_DAYS = ("260712", "260713", "260714", "260716", "260717", "260718")
FINAL_TEST_DAY = "260719"

STARTUP_TARGETS = (
    ("intermediate_thickness_set_mode", "中间厚度", "mm"),
    ("tm1_closed_gap_set_median", "F1高速稳定运行辊缝", "mm"),
    ("tm2_closed_gap_set_median", "F2高速稳定运行辊缝", "mm"),
    ("uncoiler_tension_set_median", "开卷张力", "T"),
    ("interstand_tension_set_median", "机架张力", "T"),
    ("coiler_tension_set_median", "卷取张力", "T"),
)

# Published v0.4 260719 baseline from the execution brief.
EXPECTED_BASELINE = {
    "intermediate_thickness_set_mode": {
        "mae": 0.035860,
        "p95_absolute_error": 0.107288,
        "r2": 0.998731,
        "mape": 1.322,
    },
    "tm1_closed_gap_set_median": {
        "mae": 0.149884,
        "p95_absolute_error": 0.340863,
        "r2": 0.977920,
        "mape": 6.548,
    },
    "tm2_closed_gap_set_median": {
        "mae": 0.109086,
        "p95_absolute_error": 0.313802,
        "r2": 0.967434,
        "mape": 4.841,
    },
    "uncoiler_tension_set_median": {
        "mae": 0.416618,
        "p95_absolute_error": 0.814588,
        "r2": 0.715519,
        "mape": 6.029,
    },
    "interstand_tension_set_median": {
        "mae": 0.601281,
        "p95_absolute_error": 1.378964,
        "r2": 0.924434,
        "mape": 8.217,
    },
    "coiler_tension_set_median": {
        "mae": 0.648807,
        "p95_absolute_error": 2.062649,
        "r2": 0.873373,
        "mape": 9.297,
    },
}

MAE_P95_R2_TOLERANCE = 5e-7
MAPE_TOLERANCE = 5e-4


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite(row: dict[str, str], name: str) -> float:
    value = float(row[name])
    if not math.isfinite(value):
        raise ValueError(name)
    return value


def derived_features(row: dict[str, str]) -> dict[str, float]:
    entry = _finite(row, "entry_thickness_set_mode")
    exit_value = _finite(row, "exit_thickness_set_mode")
    width = _finite(row, "width_set_mode")
    if entry <= 0 or exit_value <= 0:
        raise ValueError("thickness must be positive")
    return {
        "entry_thickness_set_mode": entry,
        "exit_thickness_set_mode": exit_value,
        "width_set_mode": width,
        # Use the stored training feature so frozen-coefficient evaluation
        # matches the original v0.4 260719 numbers. Production inference still
        # recomputes this from the three operator inputs.
        "total_reduction_ratio": _finite(row, "total_reduction_ratio"),
        "entry_thickness_actual_median": _finite(row, "entry_thickness_actual_median"),
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


def percentile(values: Sequence[float], fraction: float) -> float:
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


def metric_summary(actual: Sequence[float], predicted: Sequence[float]) -> dict[str, float]:
    errors = [prediction - value for value, prediction in zip(actual, predicted)]
    absolute = [abs(error) for error in errors]
    mean_actual = statistics.fmean(actual)
    denominator = sum((value - mean_actual) ** 2 for value in actual)
    r2 = (
        1 - sum(error * error for error in errors) / denominator
        if denominator > 1e-12
        else math.nan
    )
    relative = [
        abs(error) / abs(value)
        for value, error in zip(actual, errors)
        if abs(value) > 1e-12
    ]
    return {
        "mae": statistics.fmean(absolute),
        "rmse": math.sqrt(statistics.fmean([error * error for error in errors])),
        "p80_absolute_error": percentile(absolute, 0.80),
        "p95_absolute_error": percentile(absolute, 0.95),
        "bias": statistics.fmean(errors),
        "r2": r2,
        "mape": statistics.fmean(relative) * 100 if relative else math.nan,
        "evaluated_case_count": float(len(actual)),
    }


def predict_from_artifact(
    target: str, model: dict[str, Any], features: dict[str, float]
) -> float:
    standardized = [
        (features[name] - mean) / scale
        for name, mean, scale in zip(
            model["feature_names"], model["feature_mean"], model["feature_scale"]
        )
    ]
    row = [1.0]
    for term in model["basis_terms"]:
        product = 1.0
        for index in term:
            product *= standardized[index]
        row.append(product)
    encoded = sum(value * coefficient for value, coefficient in zip(row, model["coefficients"]))
    if model.get("target_transform") == "reduction_share":
        entry = features["entry_thickness_set_mode"]
        exit_value = features["exit_thickness_set_mode"]
        value = entry - encoded * (entry - exit_value)
    else:
        value = encoded
    if target == "intermediate_thickness_set_mode":
        value = min(
            features["entry_thickness_set_mode"] - 0.001,
            max(features["exit_thickness_set_mode"] + 0.001, value),
        )
    if model.get("hard_min") is not None:
        value = max(float(model["hard_min"]), value)
    if model.get("hard_max") is not None:
        value = min(float(model["hard_max"]), value)
    if not math.isfinite(value):
        raise ValueError(f"non-finite prediction for {target}")
    return value


def evaluate_day(
    rows: Sequence[dict[str, str]],
    artifact: dict[str, Any],
    day: str,
) -> dict[str, dict[str, float]]:
    metrics = {}
    for code, _label, _unit in STARTUP_TARGETS:
        model = artifact["models"][code]
        actual: list[float] = []
        predicted: list[float] = []
        for row in rows:
            if row.get("dataset") != day:
                continue
            try:
                features = derived_features(row)
                actual.append(_finite(row, code))
                predicted.append(predict_from_artifact(code, model, features))
            except (KeyError, TypeError, ValueError):
                continue
        if not actual:
            raise ValueError(f"no evaluable rows for {code} on {day}")
        metrics[code] = metric_summary(actual, predicted)
    return metrics


def compare_to_expected(metrics: dict[str, dict[str, float]]) -> tuple[bool, list[dict[str, Any]]]:
    rows = []
    passed = True
    for code, _label, _unit in STARTUP_TARGETS:
        expected = EXPECTED_BASELINE[code]
        observed = metrics[code]
        row = {
            "target": code,
            "mae_expected": expected["mae"],
            "mae_observed": round(observed["mae"], 6),
            "p95_expected": expected["p95_absolute_error"],
            "p95_observed": round(observed["p95_absolute_error"], 6),
            "r2_expected": expected["r2"],
            "r2_observed": round(observed["r2"], 6),
            "mape_expected": expected["mape"],
            "mape_observed": round(observed["mape"], 3),
            "mae_delta": abs(observed["mae"] - expected["mae"]),
            "p95_delta": abs(observed["p95_absolute_error"] - expected["p95_absolute_error"]),
            "r2_delta": abs(observed["r2"] - expected["r2"]),
            "mape_delta": abs(observed["mape"] - expected["mape"]),
        }
        row["pass"] = (
            row["mae_delta"] <= MAE_P95_R2_TOLERANCE
            and row["p95_delta"] <= MAE_P95_R2_TOLERANCE
            and row["r2_delta"] <= MAE_P95_R2_TOLERANCE
            and row["mape_delta"] <= MAPE_TOLERANCE
        )
        passed = passed and bool(row["pass"])
        rows.append(row)
    return passed, rows


def day_counts(rows: Sequence[dict[str, str]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        day = row.get("dataset", "")
        counts[day] = counts.get(day, 0) + 1
    return counts


def write_metrics_csv(path: Path, metrics: dict[str, dict[str, float]], comparisons: list[dict[str, Any]]) -> None:
    comparison_by_target = {row["target"]: row for row in comparisons}
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "target",
                "label",
                "unit",
                "evaluated_case_count",
                "mae",
                "p95_absolute_error",
                "r2",
                "mape_percent",
                "bias",
                "rmse",
                "expected_mae",
                "expected_p95",
                "expected_r2",
                "expected_mape",
                "pass",
            ],
        )
        writer.writeheader()
        for code, label, unit in STARTUP_TARGETS:
            observed = metrics[code]
            comparison = comparison_by_target[code]
            writer.writerow(
                {
                    "target": code,
                    "label": label,
                    "unit": unit,
                    "evaluated_case_count": int(observed["evaluated_case_count"]),
                    "mae": round(observed["mae"], 6),
                    "p95_absolute_error": round(observed["p95_absolute_error"], 6),
                    "r2": round(observed["r2"], 6),
                    "mape_percent": round(observed["mape"], 3),
                    "bias": round(observed["bias"], 6),
                    "rmse": round(observed["rmse"], 6),
                    "expected_mae": comparison["mae_expected"],
                    "expected_p95": comparison["p95_expected"],
                    "expected_r2": comparison["r2_expected"],
                    "expected_mape": comparison["mape_expected"],
                    "pass": comparison["pass"],
                }
            )


def write_report(
    path: Path,
    *,
    passed: bool,
    metrics: dict[str, dict[str, float]],
    comparisons: list[dict[str, Any]],
    counts: dict[str, int],
) -> None:
    lines = [
        "# 冻结 v0.4 基线复现",
        "",
        "评估对象：冻结 `recommendation_direct_v4.json` + `outputs/model_v4/segment_features.csv`。",
        "260719 是日期隔离复用基准，不是全新盲测。本脚本不重新训练。",
        "",
        f"总样本 `{sum(counts.values())}`；开发期 `{sum(counts.get(day, 0) for day in DEVELOPMENT_DAYS)}`；260719 `{counts.get(FINAL_TEST_DAY, 0)}`。",
        "",
        f"复现结果：{'通过' if passed else '未通过'}",
        "",
        "| 输出 | MAE | P95 | R2 | MAPE | 是否匹配基线 |",
        "| --- | ---: | ---: | ---: | ---: | --- |",
    ]
    label_by_code = {code: label for code, label, _unit in STARTUP_TARGETS}
    unit_by_code = {code: unit for code, _label, unit in STARTUP_TARGETS}
    for row in comparisons:
        code = row["target"]
        lines.append(
            f"| {label_by_code[code]} | {row['mae_observed']:.6f} {unit_by_code[code]} | "
            f"{row['p95_observed']:.6f} | {row['r2_observed']:.6f} | "
            f"{row['mape_observed']:.3f}% | {'是' if row['pass'] else '否'} |"
        )
    lines.extend(["", "与执行文档期望值的绝对偏差：", ""])
    for row in comparisons:
        lines.append(
            f"- {label_by_code[row['target']]}: "
            f"MAE Δ={row['mae_delta']:.8f}, P95 Δ={row['p95_delta']:.8f}, "
            f"R2 Δ={row['r2_delta']:.8f}, MAPE Δ={row['mape_delta']:.6f}"
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def build_manifest(
    *,
    artifact_path: Path,
    features_path: Path,
    passed: bool,
    metrics: dict[str, dict[str, float]],
    comparisons: list[dict[str, Any]],
    counts: dict[str, int],
    artifact: dict[str, Any],
    display_artifact_path: Path | None = None,
) -> dict[str, Any]:
    return {
        "step": 0,
        "purpose": "freeze_v04_baseline_before_any_retraining",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "workspace": str(WORKSPACE_ROOT),
        "runtime": {
            "python": sys.version,
            "platform": platform.platform(),
            "implementation": platform.python_implementation(),
        },
        "sources": {
            "artifact": {
                "path": str(artifact_path),
                "sha256": sha256_file(artifact_path),
                "model_version": artifact.get("model_version"),
                "model_family": artifact.get("model_family"),
                "inference_method": artifact.get("inference_method"),
                "must_not_overwrite": True,
            },
            "feature_table": {
                "path": str(features_path),
                "sha256": sha256_file(features_path),
                "row_count": sum(counts.values()),
            },
            "display_artifact_v041": {
                "path": str(display_artifact_path) if display_artifact_path else None,
                "sha256": sha256_file(display_artifact_path) if display_artifact_path and display_artifact_path.exists() else None,
                "model_version": "direct-ridge-startup-flatness-v0.4.1",
                "change_scope": "display_semantics_only",
                "coefficients_unchanged_from_v04": True,
                "does_not_replace_frozen_v04": True,
            },
        },
        "datasets": {
            "all_days": sorted(counts),
            "development_days": list(DEVELOPMENT_DAYS),
            "date_isolated_reused_benchmark": FINAL_TEST_DAY,
            "date_isolated_reused_benchmark_is_not_blind_test": True,
            "day_counts": counts,
            "development_case_count": sum(counts.get(day, 0) for day in DEVELOPMENT_DAYS),
            "benchmark_case_count": counts.get(FINAL_TEST_DAY, 0),
        },
        "training_policy_snapshot": artifact.get("training_policy", {}),
        "baseline_260719": {
            code: {
                "label": label,
                "unit": unit,
                "mae": round(metrics[code]["mae"], 6),
                "p95_absolute_error": round(metrics[code]["p95_absolute_error"], 6),
                "r2": round(metrics[code]["r2"], 6),
                "mape_percent": round(metrics[code]["mape"], 3),
                "bias": round(metrics[code]["bias"], 6),
                "rmse": round(metrics[code]["rmse"], 6),
                "evaluated_case_count": int(metrics[code]["evaluated_case_count"]),
            }
            for code, label, unit in STARTUP_TARGETS
        },
        "expected_baseline": EXPECTED_BASELINE,
        "comparison": comparisons,
        "reproduced": passed,
        "notes": [
            "Metrics come from frozen coefficients, not a refit.",
            "260719 is a date-isolated reused benchmark, not a never-seen blind test.",
            "No random 8/2 split is used.",
            "Historical neighbours are not used for inference.",
        ],
    }


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
    parser.add_argument(
        "--display-artifact",
        type=Path,
        default=PROJECT_ROOT / "backend/app/model_artifacts/recommendation_direct_v041.json",
    )
    args = parser.parse_args()
    if not args.features.exists():
        parser.error(f"missing feature table: {args.features}")
    if not args.artifact.exists():
        parser.error(f"missing frozen artifact: {args.artifact}")

    artifact = json.loads(args.artifact.read_text(encoding="utf-8"))
    if artifact.get("model_version") != "direct-ridge-startup-flatness-v0.4":
        parser.error(f"unexpected artifact version: {artifact.get('model_version')}")

    rows = load_feature_rows(args.features)
    counts = day_counts(rows)
    metrics = evaluate_day(rows, artifact, FINAL_TEST_DAY)
    passed, comparisons = compare_to_expected(metrics)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest(
        artifact_path=args.artifact,
        features_path=args.features,
        passed=passed,
        metrics=metrics,
        comparisons=comparisons,
        counts=counts,
        artifact=artifact,
        display_artifact_path=args.display_artifact if args.display_artifact.exists() else None,
    )
    manifest_path = args.output_dir / "baseline_manifest.json"
    metrics_path = args.output_dir / "baseline_260719_metrics.csv"
    report_path = args.output_dir / "baseline_reproduction.md"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_metrics_csv(metrics_path, metrics, comparisons)
    write_report(
        report_path,
        passed=passed,
        metrics=metrics,
        comparisons=comparisons,
        counts=counts,
    )

    print(f"artifact_sha256={manifest['sources']['artifact']['sha256']}")
    print(f"features_sha256={manifest['sources']['feature_table']['sha256']}")
    print(f"rows={sum(counts.values())} development={manifest['datasets']['development_case_count']} benchmark={manifest['datasets']['benchmark_case_count']}")
    for row in comparisons:
        status = "PASS" if row["pass"] else "FAIL"
        print(
            f"{status} {row['target']} "
            f"MAE={row['mae_observed']:.6f} P95={row['p95_observed']:.6f} "
            f"R2={row['r2_observed']:.6f} MAPE={row['mape_observed']:.3f}%"
        )
    print(f"wrote {manifest_path}")
    print(f"wrote {metrics_path}")
    print(f"wrote {report_path}")
    print("BASELINE_REPRODUCED" if passed else "BASELINE_MISMATCH")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
