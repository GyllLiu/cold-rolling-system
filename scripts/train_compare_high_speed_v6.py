#!/usr/bin/env python3
"""Compare new high-speed-only predictors against frozen v0.4.

The script uses only governed high-speed stable segments.  Model selection uses
rolling future-day validation beginning at 260724; 260805 is held out until the
challenger for each target is frozen.  A target is replaced only when the
challenger is non-inferior on every release metric and improves mean MAE.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
SCRIPT_ROOT = PROJECT_ROOT / "scripts"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))

import train_validate_direct_prediction as core  # noqa: E402
from tandem_data_filter import is_tandem_record


HOLDOUT_DAY = "260805"
FIRST_SELECTION_DAY = "260724"
FEATURE_SETS = {
    "thickness2": ("entry_thickness_set_mode", "exit_thickness_set_mode"),
    "spec3": (
        "entry_thickness_set_mode",
        "exit_thickness_set_mode",
        "width_set_mode",
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
}

TARGETS = (
    core.TargetSpec("intermediate_thickness_set_mode", "中间厚度推荐", "mm", "setup", "primary", 0.001, 30.0),
    core.TargetSpec("uncoiler_tension_set_median", "开卷张力推荐", "T", "setup", "secondary", 0.0, None),
    core.TargetSpec("interstand_tension_set_median", "机架张力推荐", "T", "setup", "secondary", 0.0, None),
    core.TargetSpec("coiler_tension_set_median", "卷取张力推荐", "T", "setup", "secondary", 0.0, None),
    core.TargetSpec("tm1_closed_gap_set_median", "F1推荐辊缝", "mm", "setup", "primary", 0.0, None),
    core.TargetSpec("tm2_closed_gap_set_median", "F2推荐辊缝", "mm", "setup", "primary", 0.0, None),
    core.TargetSpec("f1_roll_force_sum_median", "F1预计总轧制力", "T", "load", "monitor", 0.0, None),
    core.TargetSpec("f2_roll_force_sum_median", "F2预计总轧制力", "T", "load", "monitor", 0.0, None),
)
TARGET_BY_CODE = {item.code: item for item in TARGETS}
BASELINE_TARGETS = tuple(item.code for item in TARGETS[:6])
FORCE_TARGETS = tuple(item.code for item in TARGETS[6:])


@dataclass(frozen=True)
class Candidate:
    config: core.ModelConfig
    balance_spec_day: bool

    @property
    def name(self) -> str:
        suffix = "balanced" if self.balance_spec_day else "raw"
        return (
            f"ridge_{self.config.feature_set}_d{self.config.degree}_"
            f"l{self.config.ridge_lambda:g}_{self.config.target_transform}_{suffix}"
        )


def finite(row: dict[str, str], key: str) -> float:
    value = float(row[key])
    if not math.isfinite(value):
        raise ValueError(key)
    return value


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


def dataset_from_path(path: Path) -> str:
    suffix = path.parent.name.removeprefix("data_foundation_")
    return "260703" if suffix == "data_foundation" else suffix


def load_cases(paths: Iterable[Path]) -> list[core.Case]:
    cases: list[core.Case] = []
    for path in paths:
        dataset = dataset_from_path(path)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if not is_tandem_record(row):
                    continue
                try:
                    stable = finite(row, "stable_high_speed_seconds_1hz")
                    entry = finite(row, "entry_thickness_set_mode")
                    middle = finite(row, "intermediate_thickness_set_mode")
                    exit_value = finite(row, "exit_thickness_set_mode")
                    width = finite(row, "width_set_mode")
                    values = {
                        "entry_thickness_set_mode": entry,
                        "intermediate_thickness_set_mode": middle,
                        "exit_thickness_set_mode": exit_value,
                        "width_set_mode": width,
                        "total_reduction_ratio": 1.0 - exit_value / entry,
                        "log_thickness_ratio": math.log(entry / exit_value),
                        "entry_cross_section_proxy": entry * width,
                        "exit_cross_section_proxy": exit_value * width,
                        "uncoiler_tension_set_median": finite(row, "uncoiler_tension_set_median"),
                        "interstand_tension_set_median": finite(row, "interstand_tension_set_median"),
                        "coiler_tension_set_median": finite(row, "coiler_tension_set_median"),
                        "tm1_closed_gap_set_median": finite(row, "tm1_gap_command_median") * 0.001,
                        "tm2_closed_gap_set_median": finite(row, "tm2_gap_command_median") * 0.001,
                        "f1_roll_force_sum_median": finite(row, "f1_roll_force_sum_median"),
                        "f2_roll_force_sum_median": finite(row, "f2_roll_force_sum_median"),
                    }
                except (KeyError, TypeError, ValueError, ZeroDivisionError):
                    continue
                if not (
                    truthy(row.get("learning_eligible"))
                    and truthy(row.get("digital_state_usable"))
                    and stable >= 120
                    and 0 < exit_value < middle < entry <= 30
                    and 500 <= width <= 2000
                ):
                    continue
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


def median_case(group: Sequence[core.Case]) -> core.Case:
    names = group[0].values.keys()
    values = {name: statistics.median(item.values[name] for item in group) for name in names}
    first = group[0]
    return core.Case(first.dataset, first.candidate_id + "_median", first.start_time, first.source_files, values)


def balance_cases(cases: Sequence[core.Case]) -> list[core.Case]:
    groups: dict[tuple[Any, ...], list[core.Case]] = defaultdict(list)
    for case in cases:
        key = (
            case.dataset,
            round(case.values["entry_thickness_set_mode"], 4),
            round(case.values["exit_thickness_set_mode"], 4),
            round(case.values["width_set_mode"], 1),
        )
        groups[key].append(case)
    return [median_case(group) for _, group in sorted(groups.items())]


def configure_core() -> None:
    core.FEATURE_SETS = FEATURE_SETS
    core.TARGET_SPECS = TARGETS
    core.TARGET_BY_CODE = TARGET_BY_CODE


def constrain(target: str, value: float, case: core.Case) -> float:
    if target == "intermediate_thickness_set_mode":
        return min(
            case.values["entry_thickness_set_mode"] - 0.001,
            max(case.values["exit_thickness_set_mode"] + 0.001, value),
        )
    return max(0.0, value)


def predict_fitted(model: core.FittedModel, cases: Sequence[core.Case], target: str) -> list[float]:
    return [constrain(target, core.predict_model(model, case, target), case) for case in cases]


def predict_artifact(artifact: dict[str, Any], cases: Sequence[core.Case], target: str) -> list[float]:
    model = artifact["models"][target]
    predictions = []
    for case in cases:
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
        encoded = sum(value * coefficient for value, coefficient in zip(design, model["coefficients"]))
        value = core._decode_target(encoded, case.values, target, model.get("target_transform", "raw"))
        predictions.append(constrain(target, value, case))
    return predictions


def percentile(values: Sequence[float], probability: float) -> float:
    return core._percentile(values, probability)


def metrics(cases: Sequence[core.Case], predicted: Sequence[float], target: str) -> dict[str, float]:
    actual = [case.values[target] for case in cases]
    errors = [p - a for p, a in zip(predicted, actual)]
    absolute = [abs(value) for value in errors]
    mean_actual = statistics.fmean(actual)
    denominator = sum((value - mean_actual) ** 2 for value in actual)
    return {
        "n": len(actual),
        "mae": statistics.fmean(absolute),
        "p95": percentile(absolute, 0.95),
        "bias": statistics.fmean(errors),
        "r2": 1.0 - sum(value * value for value in errors) / denominator if denominator > 1e-12 else math.nan,
    }


def configs_for(target: str) -> list[Candidate]:
    transforms = ("raw", "reduction_share") if target == "intermediate_thickness_set_mode" else ("raw",)
    return [
        Candidate(core.ModelConfig(feature_set, degree, ridge, transform), balanced)
        for feature_set in FEATURE_SETS
        for degree in (1, 2)
        for ridge in (0.1, 1.0, 10.0)
        for transform in transforms
        for balanced in (False, True)
    ]


def rolling_folds(cases: Sequence[core.Case]) -> list[tuple[list[core.Case], list[core.Case]]]:
    dates = sorted({case.dataset for case in cases if case.dataset != HOLDOUT_DAY})
    folds = []
    for day in dates:
        if day < FIRST_SELECTION_DAY:
            continue
        train = [case for case in cases if case.dataset < day]
        validation = [case for case in cases if case.dataset == day]
        if train and validation:
            folds.append((train, validation))
    return folds


def aggregate_fold_metrics(rows: Sequence[dict[str, float]]) -> dict[str, float]:
    return {
        "fold_count": len(rows),
        "equal_day_mae": statistics.fmean(row["mae"] for row in rows),
        "worst_day_mae": max(row["mae"] for row in rows),
        "equal_day_p95": statistics.fmean(row["p95"] for row in rows),
        "absolute_bias": statistics.fmean(abs(row["bias"]) for row in rows),
    }


def evaluate_candidate(candidate: Candidate, folds: Sequence[tuple[list[core.Case], list[core.Case]]], target: str) -> tuple[dict[str, float], list[dict[str, float]]]:
    rows = []
    for train, validation in folds:
        fit_rows = balance_cases(train) if candidate.balance_spec_day else list(train)
        model = core.fit_model(fit_rows, target, candidate.config)
        rows.append(metrics(validation, predict_fitted(model, validation, target), target))
    return aggregate_fold_metrics(rows), rows


def evaluate_baseline(artifact: dict[str, Any], folds: Sequence[tuple[list[core.Case], list[core.Case]]], target: str) -> tuple[dict[str, float], list[dict[str, float]]]:
    rows = [metrics(validation, predict_artifact(artifact, validation, target), target) for _train, validation in folds]
    return aggregate_fold_metrics(rows), rows


def serialize_model(model: core.FittedModel, target: str, training_count: int, validation: dict[str, Any]) -> dict[str, Any]:
    spec = TARGET_BY_CODE[target]
    return {
        "label": spec.label,
        "unit": spec.unit,
        "feature_set": model.config.feature_set,
        "feature_names": list(model.feature_names),
        "degree": model.config.degree,
        "ridge_lambda": model.config.ridge_lambda,
        "target_transform": model.config.target_transform,
        "feature_mean": model.feature_mean,
        "feature_scale": model.feature_scale,
        "basis_terms": model.basis_terms,
        "coefficients": model.coefficients,
        "hard_min": spec.hard_min,
        "hard_max": spec.hard_max,
        "training_case_count": training_count,
        "validation": validation,
    }


def copy_baseline_model(artifact: dict[str, Any], target: str, comparison: dict[str, Any]) -> dict[str, Any]:
    model = dict(artifact["models"][target])
    model["label"] = TARGET_BY_CODE[target].label
    model["selection"] = {"source": "frozen_v0.4", **comparison}
    return model


def non_inferior(
    baseline_dev: dict[str, float],
    challenger_dev: dict[str, float],
    baseline_holdout: dict[str, float],
    challenger_holdout: dict[str, float],
    baseline_folds: Sequence[dict[str, float]],
    challenger_folds: Sequence[dict[str, float]],
) -> tuple[bool, list[str]]:
    reasons = []
    if challenger_dev["equal_day_mae"] >= baseline_dev["equal_day_mae"]:
        reasons.append("开发期平均MAE未改善")
    for key, label in (
        ("worst_day_mae", "开发期最差日MAE"),
        ("equal_day_p95", "开发期P95"),
        ("absolute_bias", "开发期绝对Bias"),
    ):
        if challenger_dev[key] > baseline_dev[key] + 1e-12:
            reasons.append(f"{label}下降")
    improved_folds = sum(c["mae"] < b["mae"] for b, c in zip(baseline_folds, challenger_folds))
    if improved_folds <= len(baseline_folds) // 2:
        reasons.append("多数未来日期折未改善")
    for key, label in (("mae", "0805 MAE"), ("p95", "0805 P95"), ("bias", "0805 Bias")):
        left = abs(challenger_holdout[key]) if key == "bias" else challenger_holdout[key]
        right = abs(baseline_holdout[key]) if key == "bias" else baseline_holdout[key]
        if left > right + 1e-12:
            reasons.append(f"{label}下降")
    return not reasons, reasons


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=WORKSPACE_ROOT / "outputs/v6_high_speed")
    parser.add_argument(
        "--baseline",
        type=Path,
        default=PROJECT_ROOT / "backend/app/model_artifacts/recommendation_direct_v041.json",
    )
    args = parser.parse_args()
    configure_core()
    paths = sorted((WORKSPACE_ROOT / "outputs").glob("data_foundation*/production_segments.csv"))
    cases = load_cases(paths)
    folds = rolling_folds(cases)
    holdout = [case for case in cases if case.dataset == HOLDOUT_DAY]
    development = [case for case in cases if case.dataset != HOLDOUT_DAY]
    if not folds or not holdout:
        raise RuntimeError("insufficient rolling folds or missing 260805 holdout")
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))

    selection: dict[str, Any] = {}
    artifact_models: dict[str, Any] = {}
    comparison_rows: list[dict[str, Any]] = []
    for target in tuple(item.code for item in TARGETS):
        candidates = configs_for(target)
        scored = []
        for candidate in candidates:
            summary, rows = evaluate_candidate(candidate, folds, target)
            scored.append((summary["equal_day_mae"], summary["equal_day_p95"], candidate.name, candidate, summary, rows))
        _, _, _, winner, winner_dev, winner_rows = min(scored, key=lambda item: item[:3])
        fit_rows = balance_cases(development) if winner.balance_spec_day else development
        winner_model = core.fit_model(fit_rows, target, winner.config)
        winner_holdout = metrics(holdout, predict_fitted(winner_model, holdout, target), target)

        if target in BASELINE_TARGETS:
            baseline_dev, baseline_rows = evaluate_baseline(baseline, folds, target)
            baseline_holdout = metrics(holdout, predict_artifact(baseline, holdout, target), target)
            replace, reasons = non_inferior(
                baseline_dev,
                winner_dev,
                baseline_holdout,
                winner_holdout,
                baseline_rows,
                winner_rows,
            )
            if replace:
                artifact_models[target] = serialize_model(
                    winner_model,
                    target,
                    len(fit_rows),
                    {"rolling": winner_dev, "holdout_260805": winner_holdout},
                )
            else:
                artifact_models[target] = copy_baseline_model(
                    baseline,
                    target,
                    {"challenger": winner.name, "reasons": reasons},
                )
        else:
            baseline_dev = None
            baseline_holdout = None
            replace = True
            reasons = ["新增轧制力输出，无v0.4基线"]
            artifact_models[target] = serialize_model(
                winner_model,
                target,
                len(fit_rows),
                {"rolling": winner_dev, "holdout_260805": winner_holdout},
            )

        selection[target] = {
            "label": TARGET_BY_CODE[target].label,
            "winner": winner.name,
            "replace_v04": replace if target in BASELINE_TARGETS else None,
            "decision": "use_challenger" if replace else "keep_v0.4",
            "reasons": reasons,
            "challenger_development": winner_dev,
            "challenger_holdout_260805": winner_holdout,
            "baseline_development": baseline_dev,
            "baseline_holdout_260805": baseline_holdout,
        }
        comparison_rows.append({"target": target, **selection[target]})

    dates = sorted({case.dataset for case in cases})
    artifact = {
        "schema_version": "cold-rolling-route-model-v1",
        "model_version": "high-speed-champion-challenger-v0.6",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "training_policy": "high_speed_stable_only",
        "selection_days": [day for day in dates if FIRST_SELECTION_DAY <= day < HOLDOUT_DAY],
        "holdout_day": HOLDOUT_DAY,
        "case_count": len(cases),
        "development_case_count": len(development),
        "holdout_case_count": len(holdout),
        "supported_alloys": ["1060", "1100", "3003"],
        "alloy_specific_models": False,
        "feature_bounds": {
            name: [min(case.values[name] for case in development), max(case.values[name] for case in development)]
            for name in FEATURE_SETS["spec3"]
        },
        "models": artifact_models,
        "selection": selection,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "route_model_v6.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "model_comparison.json").write_text(
        json.dumps(
            {
                "generated_at": artifact["generated_at"],
                "protocol": {
                    "training": "all dates before each validation day",
                    "selection_validation_days": artifact["selection_days"],
                    "holdout": HOLDOUT_DAY,
                    "replacement_gate": "strict per-target non-inferiority",
                },
                "case_count": len(cases),
                "selection": selection,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    with (args.output_dir / "model_comparison.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("target", "label", "winner", "decision", "replace_v04", "reasons"))
        writer.writeheader()
        for row in comparison_rows:
            writer.writerow({
                "target": row["target"],
                "label": row["label"],
                "winner": row["winner"],
                "decision": row["decision"],
                "replace_v04": row["replace_v04"],
                "reasons": "；".join(row["reasons"]),
            })
    print(f"cases={len(cases)} development={len(development)} holdout={len(holdout)} folds={len(folds)}")
    for target, row in selection.items():
        print(f"{target}: {row['decision']} winner={row['winner']} reasons={'；'.join(row['reasons'])}")
    print(args.output_dir / "route_model_v6.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
