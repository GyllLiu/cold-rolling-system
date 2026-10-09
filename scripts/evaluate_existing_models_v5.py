#!/usr/bin/env python3
"""Existing-data validation protocol for later model selection.

Selection may use only 260712-260718.  260719 is a date-isolated reused
benchmark and is scored once after selection.  No random 8/2 split.
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
from typing import Any, Iterable, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import train_validate_direct_prediction as core
import train_validate_v4 as v4


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEVELOPMENT_DAYS = v4.DEVELOPMENT_DAYS
BENCHMARK_DAY = v4.FINAL_TEST_DAY
SPEC_FEATURES = ("entry_thickness_set_mode", "exit_thickness_set_mode", "width_set_mode")
STARTUP_TARGETS = tuple(target.code for target in v4.STARTUP_TARGETS)
TARGET_LABELS = {target.code: target.label for target in v4.STARTUP_TARGETS}
TARGET_UNITS = {target.code: target.unit for target in v4.STARTUP_TARGETS}


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
        "n": float(len(actual)),
        "mae": statistics.fmean(absolute),
        "mape": statistics.fmean(relative) * 100 if relative else math.nan,
        "p80": core._percentile(absolute, 0.80),
        "p95": core._percentile(absolute, 0.95),
        "bias": statistics.fmean(errors),
        "rmse": math.sqrt(statistics.fmean([error * error for error in errors])),
        "r2": r2,
    }


def daily_mae(pairs: Sequence[tuple[str, float, float]]) -> dict[str, float]:
    grouped: dict[str, list[float]] = defaultdict(list)
    for day, actual, predicted in pairs:
        grouped[day].append(abs(predicted - actual))
    return {day: statistics.fmean(values) for day, values in grouped.items() if values}


def coverage_bounds(train: Sequence[core.Case]) -> dict[str, tuple[float, float]]:
    bounds = {}
    for name in SPEC_FEATURES:
        values = [case.values[name] for case in train]
        bounds[name] = (min(values), max(values))
    return bounds


def out_of_range_ratio(validation: Sequence[core.Case], bounds: dict[str, tuple[float, float]]) -> float:
    if not validation:
        return math.nan
    outside = 0
    for case in validation:
        if any(
            case.values[name] < low or case.values[name] > high
            for name, (low, high) in bounds.items()
        ):
            outside += 1
    return outside / len(validation)


def spec_key(case: core.Case) -> tuple[float, float, float]:
    return tuple(case.values[name] for name in SPEC_FEATURES)  # type: ignore[return-value]


def grouped_spec_folds(cases: Sequence[core.Case], fold_count: int = 5) -> list[list[core.Case]]:
    groups: dict[tuple[float, float, float], list[core.Case]] = defaultdict(list)
    for case in cases:
        groups[spec_key(case)].append(case)
    folds: list[list[core.Case]] = [[] for _ in range(fold_count)]
    fold_group_counts = [0] * fold_count
    for _, group_cases in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])):
        index = min(
            range(fold_count),
            key=lambda value: (len(folds[value]), fold_group_counts[value], value),
        )
        folds[index].extend(group_cases)
        fold_group_counts[index] += 1
    return folds


def load_startup_cases(features_path: Path) -> list[core.Case]:
    rows = v4.load_feature_rows(features_path)
    v4.configure_core(v4.STARTUP_TARGETS, v4.STARTUP_FEATURE_SETS)
    return v4.make_cases(rows, v4.STARTUP_TARGETS)


def frozen_configs(artifact: dict[str, Any]) -> dict[str, core.ModelConfig]:
    configs = {}
    for target in STARTUP_TARGETS:
        model = artifact["models"][target]
        configs[target] = core.ModelConfig(
            model["feature_set"],
            int(model["degree"]),
            float(model["ridge_lambda"]),
            model["target_transform"],
        )
    return configs


def predict_frozen_artifact(
    artifact: dict[str, Any], case: core.Case, target: str
) -> float:
    model = artifact["models"][target]
    standardized = [
        (case.values[name] - mean) / scale
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
    return core._decode_target(
        encoded,
        case.values,
        target,
        model.get("target_transform", "raw"),
    )


def evaluate_predictions(
    pairs: Sequence[tuple[str, float, float]],
) -> dict[str, Any]:
    actual = [item[1] for item in pairs]
    predicted = [item[2] for item in pairs]
    summary = metric_summary(actual, predicted)
    by_day = daily_mae(pairs)
    equal_day = statistics.fmean(by_day.values()) if by_day else math.nan
    worst_day = max(by_day, key=by_day.get) if by_day else ""
    return {
        **summary,
        "equal_day_mae": equal_day,
        "worst_day": worst_day,
        "worst_day_mae": by_day.get(worst_day, math.nan),
        "daily_mae": {day: round(value, 6) for day, value in sorted(by_day.items())},
    }


def score_refit(
    train: Sequence[core.Case],
    validation: Sequence[core.Case],
    target: str,
    config: core.ModelConfig,
) -> dict[str, Any]:
    model = core.fit_model(train, target, config)
    pairs = [
        (case.dataset, case.values[target], core.predict_model(model, case, target))
        for case in validation
    ]
    result = evaluate_predictions(pairs)
    result["out_of_range_ratio"] = out_of_range_ratio(validation, coverage_bounds(train))
    result["n_train"] = len(train)
    result["n_val"] = len(validation)
    return result


def build_protocol(development: Sequence[core.Case]) -> dict[str, Any]:
    spec_groups = defaultdict(int)
    day_counts = defaultdict(int)
    for case in development:
        spec_groups[spec_key(case)] += 1
        day_counts[case.dataset] += 1
    grouped = grouped_spec_folds(development)
    rolling = []
    for index, (train_days, val_day) in enumerate(v4.ROLLING_FOLDS, start=1):
        rolling.append(
            {
                "fold_id": f"rolling_{index}",
                "protocol": "rolling_origin",
                "used_for_selection": True,
                "train_days": list(train_days),
                "validation_days": [val_day],
                "n_train": sum(1 for case in development if case.dataset in train_days),
                "n_val": sum(1 for case in development if case.dataset == val_day),
            }
        )
    lodo = []
    for day in DEVELOPMENT_DAYS:
        lodo.append(
            {
                "fold_id": f"lodo_{day}",
                "protocol": "leave_one_day_out",
                "used_for_selection": False,
                "role": "stability_diagnostic",
                "train_days": [item for item in DEVELOPMENT_DAYS if item != day],
                "validation_days": [day],
                "n_train": sum(1 for case in development if case.dataset != day),
                "n_val": sum(1 for case in development if case.dataset == day),
            }
        )
    grouped_rows = []
    for index, validation in enumerate(grouped, start=1):
        validation_ids = {id(case) for case in validation}
        train = [case for case in development if id(case) not in validation_ids]
        grouped_rows.append(
            {
                "fold_id": f"spec_{index}",
                "protocol": "grouped_spec_5fold",
                "used_for_selection": True,
                "train_days": sorted({case.dataset for case in train}),
                "validation_days": sorted({case.dataset for case in validation}),
                "n_train": len(train),
                "n_val": len(validation),
                "validation_spec_count": len({spec_key(case) for case in validation}),
            }
        )
    return {
        "schema_version": "v5-existing-data-validation-1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "development_days": list(DEVELOPMENT_DAYS),
        "reserved_benchmark_day": BENCHMARK_DAY,
        "reserved_benchmark_status": "date-isolated reused benchmark; not a never-seen blind test; not used for selection",
        "random_split_forbidden": True,
        "selection_score": "0.60 * equal-day rolling MAE + 0.40 * grouped-spec MAE",
        "reporting": {
            "metrics": ["mae", "mape", "p80", "p95", "bias", "rmse", "r2", "daily_mae", "worst_day_mae", "n", "out_of_range_ratio"],
            "date_equal_weight": True,
            "pooled_sample_metrics_also_reported": True,
        },
        "development_case_count": len(development),
        "development_spec_count": len(spec_groups),
        "development_day_counts": dict(day_counts),
        "folds": {
            "rolling_origin": rolling,
            "grouped_spec_5fold": grouped_rows,
            "leave_one_day_out": lodo,
            "reserved_benchmark": [
                {
                    "fold_id": "reserved_260719",
                    "protocol": "reserved_date_isolated_reused_benchmark",
                    "used_for_selection": False,
                    "train_days": list(DEVELOPMENT_DAYS),
                    "validation_days": [BENCHMARK_DAY],
                }
            ],
        },
    }


def append_fold_row(
    rows: list[dict[str, Any]],
    *,
    fold_id: str,
    protocol: str,
    used_for_selection: bool,
    train_days: Sequence[str],
    validation_days: Sequence[str],
    target: str,
    config: core.ModelConfig,
    scored: dict[str, Any],
    notes: str,
) -> None:
    rows.append(
        {
            "fold_id": fold_id,
            "protocol": protocol,
            "used_for_selection": used_for_selection,
            "train_days": "|".join(train_days),
            "validation_days": "|".join(validation_days),
            "target": target,
            "target_label": TARGET_LABELS[target],
            "unit": TARGET_UNITS[target],
            "feature_set": config.feature_set,
            "degree": config.degree,
            "ridge_lambda": config.ridge_lambda,
            "target_transform": config.target_transform,
            "n_train": scored.get("n_train", ""),
            "n_val": int(scored["n"]),
            "mae": round(scored["mae"], 6),
            "mape": round(scored["mape"], 3),
            "p80": round(scored["p80"], 6),
            "p95": round(scored["p95"], 6),
            "bias": round(scored["bias"], 6),
            "rmse": round(scored["rmse"], 6),
            "r2": round(scored["r2"], 6),
            "equal_day_mae": round(scored["equal_day_mae"], 6),
            "worst_day": scored["worst_day"],
            "worst_day_mae": round(scored["worst_day_mae"], 6),
            "daily_mae": json.dumps(scored["daily_mae"], ensure_ascii=False),
            "out_of_range_ratio": round(float(scored["out_of_range_ratio"]), 6),
            "notes": notes,
        }
    )


def evaluate_protocol(
    development: Sequence[core.Case],
    benchmark: Sequence[core.Case],
    artifact: dict[str, Any],
) -> list[dict[str, Any]]:
    configs = frozen_configs(artifact)
    rows: list[dict[str, Any]] = []

    for index, (train_days, val_day) in enumerate(v4.ROLLING_FOLDS, start=1):
        train = [case for case in development if case.dataset in train_days]
        validation = [case for case in development if case.dataset == val_day]
        for target, config in configs.items():
            scored = score_refit(train, validation, target, config)
            append_fold_row(
                rows,
                fold_id=f"rolling_{index}",
                protocol="rolling_origin",
                used_for_selection=True,
                train_days=train_days,
                validation_days=[val_day],
                target=target,
                config=config,
                scored=scored,
                notes="refit frozen v0.4 config on earlier dates only",
            )

    grouped = grouped_spec_folds(development)
    for index, validation in enumerate(grouped, start=1):
        validation_ids = {id(case) for case in validation}
        train = [case for case in development if id(case) not in validation_ids]
        for target, config in configs.items():
            scored = score_refit(train, validation, target, config)
            append_fold_row(
                rows,
                fold_id=f"spec_{index}",
                protocol="grouped_spec_5fold",
                used_for_selection=True,
                train_days=sorted({case.dataset for case in train}),
                validation_days=sorted({case.dataset for case in validation}),
                target=target,
                config=config,
                scored=scored,
                notes="whole specification held out; refit frozen v0.4 config",
            )

    for day in DEVELOPMENT_DAYS:
        train = [case for case in development if case.dataset != day]
        validation = [case for case in development if case.dataset == day]
        for target, config in configs.items():
            scored = score_refit(train, validation, target, config)
            append_fold_row(
                rows,
                fold_id=f"lodo_{day}",
                protocol="leave_one_day_out",
                used_for_selection=False,
                train_days=[item for item in DEVELOPMENT_DAYS if item != day],
                validation_days=[day],
                target=target,
                config=config,
                scored=scored,
                notes="stability diagnostic only; not used for model selection",
            )

    train = list(development)
    for target, config in configs.items():
        pairs = [
            (case.dataset, case.values[target], predict_frozen_artifact(artifact, case, target))
            for case in benchmark
        ]
        scored = evaluate_predictions(pairs)
        scored["out_of_range_ratio"] = out_of_range_ratio(benchmark, coverage_bounds(train))
        scored["n_train"] = len(train)
        append_fold_row(
            rows,
            fold_id="reserved_260719",
            protocol="reserved_date_isolated_reused_benchmark",
            used_for_selection=False,
            train_days=DEVELOPMENT_DAYS,
            validation_days=[BENCHMARK_DAY],
            target=target,
            config=config,
            scored=scored,
            notes="frozen coefficients; evaluated once; not used for selection",
        )
    return rows


def write_folds_csv(path: Path, rows: Sequence[dict[str, Any]]) -> None:
    fieldnames = [
        "fold_id",
        "protocol",
        "used_for_selection",
        "train_days",
        "validation_days",
        "target",
        "target_label",
        "unit",
        "feature_set",
        "degree",
        "ridge_lambda",
        "target_transform",
        "n_train",
        "n_val",
        "mae",
        "mape",
        "p80",
        "p95",
        "bias",
        "rmse",
        "r2",
        "equal_day_mae",
        "worst_day",
        "worst_day_mae",
        "daily_mae",
        "out_of_range_ratio",
        "notes",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def summarize_selection_metrics(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    summary = {}
    for target in STARTUP_TARGETS:
        rolling = [row for row in rows if row["target"] == target and row["protocol"] == "rolling_origin"]
        grouped = [row for row in rows if row["target"] == target and row["protocol"] == "grouped_spec_5fold"]
        lodo = [row for row in rows if row["target"] == target and row["protocol"] == "leave_one_day_out"]
        reserved = [row for row in rows if row["target"] == target and row["protocol"] == "reserved_date_isolated_reused_benchmark"]
        equal_day_mae = statistics.fmean(row["mae"] for row in rolling)
        grouped_mae = statistics.fmean(row["mae"] for row in grouped)
        summary[target] = {
            "label": TARGET_LABELS[target],
            "rolling_equal_day_mae": round(equal_day_mae, 6),
            "rolling_worst_day_mae": round(max(row["mae"] for row in rolling), 6),
            "grouped_mae": round(grouped_mae, 6),
            "selection_score": round(0.60 * equal_day_mae + 0.40 * grouped_mae, 6),
            "lodo_equal_day_mae": round(statistics.fmean(row["mae"] for row in lodo), 6),
            "lodo_worst_day_mae": round(max(row["mae"] for row in lodo), 6),
            "reserved_260719_mae": reserved[0]["mae"] if reserved else None,
            "reserved_260719_p95": reserved[0]["p95"] if reserved else None,
            "reserved_260719_r2": reserved[0]["r2"] if reserved else None,
            "reserved_260719_mape": reserved[0]["mape"] if reserved else None,
        }
    return summary


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
    if not args.features.exists():
        parser.error(f"missing feature table: {args.features}")
    if not args.artifact.exists():
        parser.error(f"missing frozen artifact: {args.artifact}")

    artifact = json.loads(args.artifact.read_text(encoding="utf-8"))
    if artifact.get("model_version") != "direct-ridge-startup-flatness-v0.4":
        parser.error("protocol baseline must evaluate frozen v0.4, not a later display artifact")

    cases = load_startup_cases(args.features)
    development = [case for case in cases if case.dataset in DEVELOPMENT_DAYS]
    benchmark = [case for case in cases if case.dataset == BENCHMARK_DAY]
    if len(development) != 281 or len(benchmark) != 64:
        parser.error(f"unexpected case counts: development={len(development)} benchmark={len(benchmark)}")

    protocol = build_protocol(development)
    fold_rows = evaluate_protocol(development, benchmark, artifact)
    protocol["frozen_v04_selection_metrics"] = summarize_selection_metrics(fold_rows)
    protocol["fold_row_count"] = len(fold_rows)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    protocol_path = args.output_dir / "validation_protocol.json"
    folds_path = args.output_dir / "validation_folds.csv"
    protocol_path.write_text(json.dumps(protocol, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_folds_csv(folds_path, fold_rows)

    print(f"development={len(development)} benchmark={len(benchmark)} fold_rows={len(fold_rows)}")
    for target, row in protocol["frozen_v04_selection_metrics"].items():
        print(
            f"{target} score={row['selection_score']:.6f} "
            f"roll={row['rolling_equal_day_mae']:.6f} spec={row['grouped_mae']:.6f} "
            f"lodo={row['lodo_equal_day_mae']:.6f} "
            f"260719={row['reserved_260719_mae']:.6f}"
        )
    print(f"wrote {protocol_path}")
    print(f"wrote {folds_path}")
    print("VALIDATION_PROTOCOL_READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
