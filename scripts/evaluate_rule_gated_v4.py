#!/usr/bin/env python3
"""Evaluate frozen v0.4 on unseen dates before any rule or model refit.

Two inference views are reported:

* ``deployment_3_input`` reproduces the actual UI contract by substituting the
  operator's entry set thickness when online actual entry thickness is absent;
* ``diagnostic_actual_entry`` uses the DAT actual-entry median only to quantify
  how much accuracy is lost because there is no PLC connection.

Metrics are split by framework grade.  The script never retrains or modifies an
artifact and therefore preserves the new dates as time-out-of-sample evidence.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
SCRIPT_ROOT = PROJECT_ROOT / "scripts"
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(SCRIPT_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPT_ROOT))
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from evaluate_frozen_v4 import (  # noqa: E402
    EXPECTED_BASELINE,
    STARTUP_TARGETS,
    metric_summary,
    predict_from_artifact,
)
from app.process_framework import assess_spec_support  # noqa: E402


DEFAULT_FEATURES = (
    WORKSPACE_ROOT / "outputs" / "process_framework_v1" / "new_segment_features.csv"
)
DEFAULT_AUDIT = (
    WORKSPACE_ROOT
    / "outputs"
    / "process_framework_v1"
    / "new_dates_audit"
    / "segment_audit.csv"
)
DEFAULT_ARTIFACT = (
    PROJECT_ROOT
    / "backend"
    / "app"
    / "model_artifacts"
    / "recommendation_direct_v4.json"
)
DEFAULT_SPEC_ENVELOPE = (
    PROJECT_ROOT / "process_framework" / "spec_support_envelope_v1.json"
)
DEFAULT_OUTPUT_DIR = (
    WORKSPACE_ROOT / "outputs" / "process_framework_v1" / "new_dates_v4_evaluation"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite(row: Mapping[str, str], key: str) -> float:
    value = float(row[key])
    if not math.isfinite(value):
        raise ValueError(key)
    return value


def _features(row: Mapping[str, str], *, use_actual_entry: bool) -> dict[str, float]:
    entry = _finite(row, "entry_thickness_set_mode")
    exit_value = _finite(row, "exit_thickness_set_mode")
    width = _finite(row, "width_set_mode")
    actual_entry = (
        _finite(row, "entry_thickness_actual_median")
        if use_actual_entry
        else entry
    )
    if not entry > exit_value > 0 or width <= 0:
        raise ValueError("invalid specification")
    return {
        "entry_thickness_set_mode": entry,
        "exit_thickness_set_mode": exit_value,
        "width_set_mode": width,
        "total_reduction_ratio": (entry - exit_value) / entry,
        "entry_thickness_actual_median": actual_entry,
        "log_thickness_ratio": math.log(entry / exit_value),
        "entry_cross_section_proxy": entry * width,
        "exit_cross_section_proxy": exit_value * width,
    }


def _coverage(
    artifact: Mapping[str, Any], features: Mapping[str, float]
) -> tuple[bool, list[str]]:
    outside: list[str] = []
    for name, bounds in artifact.get("input_coverage", {}).items():
        if name not in features:
            continue
        value = features[name]
        if value < float(bounds["min"]) or value > float(bounds["max"]):
            outside.append(name)
    return not outside, outside


def _load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    if not records:
        path.write_text("", encoding="utf-8")
        return
    fields: list[str] = []
    seen: set[str] = set()
    for record in records:
        for key in record:
            if key not in seen:
                seen.add(key)
                fields.append(key)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(records)


def _evaluate(
    feature_rows: list[dict[str, str]],
    audit_by_key: Mapping[tuple[str, str], Mapping[str, str]],
    artifact: Mapping[str, Any],
    spec_envelope: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    predictions: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str, str], tuple[list[float], list[float]]] = {}
    modes = {
        "deployment_3_input": False,
        "diagnostic_actual_entry": True,
    }
    for row in feature_rows:
        key = (row.get("dataset", ""), row.get("candidate_id", ""))
        audit = audit_by_key.get(key)
        if audit is None:
            raise ValueError(f"Missing audit result for {key}")
        grade = audit["grade"]
        support = assess_spec_support(
            row.get("entry_thickness_set_mode"),
            row.get("exit_thickness_set_mode"),
            row.get("width_set_mode"),
            spec_envelope,
        )
        for mode, use_actual_entry in modes.items():
            features = _features(row, use_actual_entry=use_actual_entry)
            in_coverage, outside = _coverage(artifact, features)
            framework_gate_passed = in_coverage and support.gate_passed
            for target, label, unit in STARTUP_TARGETS:
                actual = _finite(row, target)
                predicted = predict_from_artifact(
                    target, artifact["models"][target], features
                )
                error = predicted - actual
                predictions.append(
                    {
                        "dataset": key[0],
                        "candidate_id": key[1],
                        "framework_grade": grade,
                        "mode": mode,
                        "in_frozen_model_coverage": in_coverage,
                        "outside_coverage_features": ";".join(outside),
                        "spec_support_status": support.status,
                        "spec_support_gate_passed": support.gate_passed,
                        "framework_coverage_gate_passed": framework_gate_passed,
                        "coverage_confidence_ceiling": (
                            support.coverage_confidence_ceiling
                        ),
                        "nearest_spec_distance": support.nearest_distance,
                        "nearest_entry_thickness_mm": (
                            support.nearest_specification["entry_thickness_mm"]
                        ),
                        "nearest_exit_thickness_mm": (
                            support.nearest_specification["exit_thickness_mm"]
                        ),
                        "nearest_width_mm": support.nearest_specification["width_mm"],
                        "outside_spec_axis_fields": ";".join(
                            support.outside_axis_fields
                        ),
                        "target": target,
                        "label": label,
                        "unit": unit,
                        "actual": actual,
                        "predicted": predicted,
                        "error": error,
                        "absolute_error": abs(error),
                    }
                )
                if in_coverage:
                    subsets = ["all"]
                    subsets.append(f"grade_{grade}")
                    if grade in {"A", "B"}:
                        subsets.append("grade_A_or_B")
                    subsets.append(f"support_{support.status}")
                    if framework_gate_passed:
                        subsets.extend(
                            [
                                "framework_supported",
                                f"grade_{grade}_framework_supported",
                            ]
                        )
                    for subset in subsets:
                        group_key = (mode, subset, target)
                        if group_key not in grouped:
                            grouped[group_key] = ([], [])
                        grouped[group_key][0].append(actual)
                        grouped[group_key][1].append(predicted)

    metrics: list[dict[str, Any]] = []
    target_meta = {code: (label, unit) for code, label, unit in STARTUP_TARGETS}
    for (mode, subset, target), (actual, predicted) in sorted(grouped.items()):
        if not actual:
            continue
        summary = metric_summary(actual, predicted)
        label, unit = target_meta[target]
        metrics.append(
            {
                "mode": mode,
                "subset": subset,
                "target": target,
                "label": label,
                "unit": unit,
                "case_count": int(summary["evaluated_case_count"]),
                "mae": summary["mae"],
                "p95_absolute_error": summary["p95_absolute_error"],
                "bias": summary["bias"],
                "rmse": summary["rmse"],
                "r2": summary["r2"],
                "mape_percent": summary["mape"],
            }
        )
    return predictions, metrics


def _report(
    metrics: list[dict[str, Any]],
    predictions: list[dict[str, Any]],
    *,
    artifact: Mapping[str, Any],
) -> str:
    unique_rows = {
        (row["dataset"], row["candidate_id"]) for row in predictions
    }
    coverage_rows = {
        (row["dataset"], row["candidate_id"])
        for row in predictions
        if row["mode"] == "deployment_3_input" and row["in_frozen_model_coverage"]
    }
    support_by_row = {
        (row["dataset"], row["candidate_id"]): row["spec_support_status"]
        for row in predictions
        if row["mode"] == "deployment_3_input"
    }
    support_counts: dict[str, int] = defaultdict(int)
    for status in support_by_row.values():
        support_counts[str(status)] += 1
    framework_supported_rows = {
        (row["dataset"], row["candidate_id"])
        for row in predictions
        if row["mode"] == "deployment_3_input"
        and row["framework_coverage_gate_passed"]
    }
    metric_by_key = {
        (row["mode"], row["subset"], row["target"]): row for row in metrics
    }
    lines = [
        "# 新日期规则分级下的冻结 v0.4 评估",
        "",
        f"- 模型：`{artifact['model_version']}`，未重训。",
        f"- 新日期高速稳定段：{len(unique_rows)}。",
        f"- 旧版逐字段最小/最大覆盖内：{len(coverage_rows)}。",
        f"- 新增规格组合支持度闸门通过：{len(framework_supported_rows)}。",
        (
            "- 组合支持分布："
            + "、".join(
                f"`{status}` {count}"
                for status, count in sorted(support_counts.items())
            )
            + "。"
        ),
        "- 组合距离仅用于拒绝/降置信，不读取历史输出，也不做近邻加权。",
        "- `deployment_3_input` 才是现场网页真实口径；`diagnostic_actual_entry` 仅量化未来接入入口实测厚度可能带来的差异。",
        "- A 类仍是待主任确认的黄金候选，不代表已经获准训练。",
        "",
        "## 三输入部署口径",
        "",
        "| 子集 | 输出 | n | MAE | P95 | Bias | MAPE |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for row in metrics:
        if row["mode"] != "deployment_3_input" or row["subset"] not in {
            "all",
            "framework_supported",
            "grade_A",
            "grade_B",
            "grade_C",
        }:
            continue
        lines.append(
            f"| {row['subset']} | {row['label']} | {row['case_count']} | "
            f"{row['mae']:.6f} {row['unit']} | {row['p95_absolute_error']:.6f} | "
            f"{row['bias']:.6f} | {row['mape_percent']:.3f}% |"
        )

    lines.extend(
        [
            "",
            "## 与260719复用基准及规则筛选效果比较",
            "",
            "| 输出 | 260719 MAE | 新日期全部MAE | 新日期A级MAE | A级相对全部 |",
            "| --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for target, label, unit in STARTUP_TARGETS:
        all_row = metric_by_key.get(("deployment_3_input", "all", target))
        a_row = metric_by_key.get(("deployment_3_input", "grade_A", target))
        if not all_row or not a_row:
            continue
        all_mae = float(all_row["mae"])
        a_mae = float(a_row["mae"])
        improvement = (all_mae - a_mae) / all_mae * 100 if all_mae else math.nan
        lines.append(
            f"| {label} | {EXPECTED_BASELINE[target]['mae']:.6f} {unit} | "
            f"{all_mae:.6f} | {a_mae:.6f} | {improvement:+.1f}% |"
        )

    all_modes_identical = True
    for target, _label, _unit in STARTUP_TARGETS:
        deployment = metric_by_key.get(("deployment_3_input", "all", target))
        diagnostic = metric_by_key.get(("diagnostic_actual_entry", "all", target))
        if not deployment or not diagnostic:
            continue
        if abs(float(deployment["mae"]) - float(diagnostic["mae"])) > 1e-12:
            all_modes_identical = False
            break
    lines.extend(
        [
            "",
            (
                "- 本次两种入口厚度口径结果完全一致，因为当前六个冻结子模型均未实际使用入口实测厚度特征。"
                if all_modes_identical
                else "- 入口实测厚度口径与三输入部署口径存在差异，详见metrics.csv。"
            ),
            "- A级筛选明显改善中间厚度，但并未统一改善两机架稳定辊缝和全部张力，说明仅靠通用数据清洗不够；下一版必须按牌号、状态、道次和退火阶段分层。",
            "- 本批新日期已被用于检查组合闸门效果，不能再宣称是该闸门的盲测集；下一批未查看日期才是冻结验收集。",
        ]
    )

    lines.extend(
        [
            "",
            "## 解释限制",
            "",
            "- 这些日期在 v0.4 训练和 260719 复用基准之后，可作为时间外推证据；但样本仍是启发式段，不是真实卷/道次标签。",
            "- 规则库在解析这些新日期前已经建立；本报告不得反向修改门槛后再宣称同一批数据是盲测。",
            "- 只比较冻结模型误差，不据此自动批准 A 类样本，也不生成 PLC 动作。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--features", type=Path, default=DEFAULT_FEATURES)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument(
        "--spec-envelope", type=Path, default=DEFAULT_SPEC_ENVELOPE
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    features = _load_csv(args.features)
    audits = _load_csv(args.audit)
    audit_by_key = {
        (row["dataset"], row["candidate_id"]): row for row in audits
    }
    artifact = json.loads(args.artifact.read_text(encoding="utf-8"))
    spec_envelope = json.loads(args.spec_envelope.read_text(encoding="utf-8"))
    predictions, metrics = _evaluate(
        features, audit_by_key, artifact, spec_envelope
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(args.output_dir / "predictions.csv", predictions)
    _write_csv(args.output_dir / "metrics.csv", metrics)
    (args.output_dir / "report.md").write_text(
        _report(metrics, predictions, artifact=artifact), encoding="utf-8"
    )
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "time_out_of_sample_rule_gated_evaluation_before_refit",
        "features": str(args.features.resolve()),
        "features_sha256": _sha256(args.features),
        "audit": str(args.audit.resolve()),
        "audit_sha256": _sha256(args.audit),
        "artifact": str(args.artifact.resolve()),
        "artifact_sha256": _sha256(args.artifact),
        "spec_support_envelope": str(args.spec_envelope.resolve()),
        "spec_support_envelope_sha256": _sha256(args.spec_envelope),
        "spec_support_envelope_status": spec_envelope.get("status"),
        "model_version": artifact.get("model_version"),
        "retrained": False,
        "row_count": len(features),
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"rows={len(features)} metrics={len(metrics)}")
    print(args.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
