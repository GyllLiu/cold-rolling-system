#!/usr/bin/env python3
"""Grade DAT high-speed segments under the versioned rule-first framework.

The command never changes the frozen recommendation artifact.  It produces a
reviewable A/B/C/D ledger, an unapproved gold-candidate pool and a risk-candidate
pool.  A rows remain blocked from model training until the process owner approves
both the draft rulebook and the individual rows.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.process_framework import (  # noqa: E402
    DEFAULT_RULEBOOK_PATH,
    SegmentAuditResult,
    audit_rows,
    load_rulebook,
)


DEFAULT_OUTPUT_DIR = WORKSPACE_ROOT / "outputs" / "process_framework_v1"
ENRICHED_FRAMEWORK_INPUT = (
    DEFAULT_OUTPUT_DIR / "historical_segment_features_enriched.csv"
)
FROZEN_V4_FEATURE_INPUT = WORKSPACE_ROOT / "outputs" / "model_v4" / "segment_features.csv"
DEFAULT_INPUT = (
    ENRICHED_FRAMEWORK_INPUT
    if ENRICHED_FRAMEWORK_INPUT.exists()
    else FROZEN_V4_FEATURE_INPUT
)
FROZEN_V4_ARTIFACT = (
    PROJECT_ROOT
    / "backend"
    / "app"
    / "model_artifacts"
    / "recommendation_direct_v4.json"
)
DISPLAY_V041_ARTIFACT = (
    PROJECT_ROOT
    / "backend"
    / "app"
    / "model_artifacts"
    / "recommendation_direct_v041.json"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def _source_context(
    source_rows: Iterable[Mapping[str, str]],
) -> dict[tuple[str, str], dict[str, str]]:
    return {
        (str(row.get("dataset", "")), str(row.get("candidate_id", ""))): {
            "start_time": str(row.get("start_time", "")),
            "end_time": str(row.get("end_time", "")),
            "source_files": str(row.get("source_files", "")),
            "entry_thickness_set_mode": str(
                row.get("entry_thickness_set_mode", "")
            ),
            "intermediate_thickness_set_mode": str(
                row.get("intermediate_thickness_set_mode", "")
            ),
            "exit_thickness_set_mode": str(row.get("exit_thickness_set_mode", "")),
            "width_set_mode": str(row.get("width_set_mode", "")),
        }
        for row in source_rows
    }


def _result_records(
    results: list[SegmentAuditResult],
    context: Mapping[tuple[str, str], Mapping[str, str]],
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for result in results:
        record = {
            **context.get((result.dataset, result.candidate_id), {}),
            **result.to_record(),
        }
        records.append(record)
    return records


def _summary(
    results: list[SegmentAuditResult],
    *,
    rulebook: Mapping[str, Any],
) -> dict[str, Any]:
    grade_counts = Counter(result.grade for result in results)
    per_day: dict[str, Counter[str]] = defaultdict(Counter)
    reason_counts: Counter[str] = Counter()
    unit_hierarchy_values: list[bool] = []
    f1_shares: list[float] = []
    for result in results:
        per_day[result.dataset][result.grade] += 1
        reason_counts.update(reason.code for reason in result.reasons)
        hierarchy = result.metrics.get("unit_tension_hierarchy_expected")
        if isinstance(hierarchy, bool):
            unit_hierarchy_values.append(hierarchy)
        share = result.metrics.get("f1_true_strain_share")
        if isinstance(share, (int, float)) and math.isfinite(float(share)):
            f1_shares.append(float(share))

    return {
        "framework_version": rulebook["framework_version"],
        "framework_status": rulebook["status"],
        "row_count": len(results),
        "grade_counts": {
            grade: grade_counts.get(grade, 0) for grade in ("A", "B", "C", "D")
        },
        "training_pool_candidate_count": sum(
            result.training_pool_candidate for result in results
        ),
        "approved_for_parameter_training_count": sum(
            result.approved_for_parameter_training for result in results
        ),
        "route_metadata_complete_count": sum(
            result.route_metadata_complete for result in results
        ),
        "route_training_candidate_count": sum(
            result.route_training_candidate for result in results
        ),
        "risk_training_candidate_count": sum(
            result.risk_training_candidate for result in results
        ),
        "per_day_grade_counts": {
            dataset: {
                grade: counts.get(grade, 0) for grade in ("A", "B", "C", "D")
            }
            for dataset, counts in sorted(per_day.items())
        },
        "reason_counts": dict(reason_counts.most_common()),
        "observed_checks": {
            "unit_tension_hierarchy_support_fraction": (
                sum(unit_hierarchy_values) / len(unit_hierarchy_values)
                if unit_hierarchy_values
                else None
            ),
            "unit_tension_hierarchy_sample_count": len(unit_hierarchy_values),
            "f1_true_strain_share_mean": (
                sum(f1_shares) / len(f1_shares) if f1_shares else None
            ),
            "f1_true_strain_share_sample_count": len(f1_shares),
        },
        "pending_engineering_inputs": rulebook["pending_engineering_inputs"],
    }


def _report_markdown(
    summary: Mapping[str, Any],
    *,
    input_path: Path,
    rulebook_path: Path,
) -> str:
    counts = summary["grade_counts"]
    lines = [
        "# DAT工艺框架审查报告 V1",
        "",
        f"- 框架版本：`{summary['framework_version']}`",
        f"- 框架状态：`{summary['framework_status']}`",
        f"- 输入：`{input_path}`",
        f"- 规则库：`{rulebook_path}`",
        f"- 审查高速稳定段：{summary['row_count']}",
        "- 当前所有A类仅为黄金数据候选；主任未确认前，允许进入正式训练的数据仍为0。",
        "",
        "## 分级结果",
        "",
        "| 等级 | 数量 | 当前用途 |",
        "| --- | ---: | --- |",
        f"| A | {counts['A']} | 黄金候选，待主任逐条/按规格组确认 |",
        f"| B | {counts['B']} | 人工复核和压力测试，不进入第一版核心训练 |",
        f"| C | {counts['C']} | 异常/风险样本，不作为参数标签 |",
        f"| D | {counts['D']} | 损坏或核心字段缺失，完全排除 |",
        "",
        "## 按日期分级",
        "",
        "| 日期 | A | B | C | D |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for dataset, day_counts in summary["per_day_grade_counts"].items():
        lines.append(
            f"| {dataset} | {day_counts['A']} | {day_counts['B']} | "
            f"{day_counts['C']} | {day_counts['D']} |"
        )

    lines.extend(
        [
            "",
            "## 主要审查原因",
            "",
            "| 原因代码 | 数量 |",
            "| --- | ---: |",
        ]
    )
    for code, count in summary["reason_counts"].items():
        lines.append(f"| `{code}` | {count} |")

    observed = summary["observed_checks"]
    hierarchy = observed["unit_tension_hierarchy_support_fraction"]
    lines.extend(
        [
            "",
            "## 数据核验",
            "",
            (
                "- 换算为单位张力后，卷取＞机架间＞开卷的支持率为"
                f" {hierarchy:.2%}（n={observed['unit_tension_hierarchy_sample_count']}）。"
                if hierarchy is not None
                else "- 单位张力层级没有足够数据。"
            ),
            f"- F1真应变占比均值：{observed['f1_true_strain_share_mean']:.4f}。",
            "- 厚度质量门槛目前是厂内待确认的临时筛选线，不宣称来自GB/T 3880.3。后续必须按产品、厚度和宽度接入正式公差表。",
            "- 版型工程单位、操作/传动侧方向和时间延迟未确认，因此只检查可用性，不用版型数值判定动作是否正确。",
            "",
            "## 尚待主任/设备方确认",
            "",
        ]
    )
    lines.extend(
        f"- {item}" for item in summary["pending_engineering_inputs"]
    )
    lines.extend(
        [
            "",
            "## 训练闸门",
            "",
            f"- 黄金候选：{summary['training_pool_candidate_count']}",
            f"- 已批准进入参数训练：{summary['approved_for_parameter_training_count']}",
            f"- 路线元数据完整：{summary['route_metadata_complete_count']}",
            f"- 可用于轧制次数/退火路线训练：{summary['route_training_candidate_count']}",
            f"- 可用于风险建模的C类候选：{summary['risk_training_candidate_count']}",
            "- 在规则库状态改为 `approved_for_model_training` 且主任确认黄金样本前，不重训、不替换 v0.4.1。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULEBOOK_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    with args.input.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rulebook = load_rulebook(args.rules)
    results = audit_rows(rows, rulebook=rulebook)
    context = _source_context(rows)
    records = _result_records(results, context)
    summary = _summary(results, rulebook=rulebook)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(args.output_dir / "segment_audit.csv", records)
    _write_csv(
        args.output_dir / "gold_candidates_unapproved.csv",
        [record for record in records if record["grade"] == "A"],
    )
    _write_csv(
        args.output_dir / "risk_candidates.csv",
        [record for record in records if record["grade"] == "C"],
    )
    review_records = []
    for record in records:
        if record["grade"] not in {"A", "B"}:
            continue
        review_records.append(
            {
                "dataset": record["dataset"],
                "candidate_id": record["candidate_id"],
                "start_time": record.get("start_time", ""),
                "end_time": record.get("end_time", ""),
                "source_files": record.get("source_files", ""),
                "entry_thickness_mm": record.get("entry_thickness_set_mode", ""),
                "intermediate_thickness_mm": record.get(
                    "intermediate_thickness_set_mode", ""
                ),
                "exit_thickness_mm": record.get("exit_thickness_set_mode", ""),
                "width_mm": record.get("width_set_mode", ""),
                "audit_grade": record["grade"],
                "audit_reason_codes": record["reason_codes"],
                "f1_true_strain_share": record.get("f1_true_strain_share", ""),
                "exit_error_abs_p95_ratio": record.get(
                    "exit_error_abs_p95_ratio", ""
                ),
                "unit_tension_hierarchy_expected": record.get(
                    "unit_tension_hierarchy_expected", ""
                ),
                "process_owner_decision": "",
                "process_owner_comment": "",
                "confirmed_alloy": "",
                "confirmed_initial_temper": "",
                "confirmed_anneal_state": "",
                "confirmed_pass_number": "",
                "confirmed_full_anneal_before": "",
                "confirmed_product_tolerance_code": "",
                "confirmed_abnormal_operation": "",
            }
        )
    _write_csv(
        args.output_dir / "process_owner_review_template.csv", review_records
    )
    (args.output_dir / "audit_summary.json").write_text(
        json.dumps(_json_safe(summary), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "audit_report.md").write_text(
        _report_markdown(summary, input_path=args.input, rulebook_path=args.rules),
        encoding="utf-8",
    )
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "framework_version": rulebook["framework_version"],
        "framework_status": rulebook["status"],
        "input_path": str(args.input.resolve()),
        "input_sha256": _sha256(args.input),
        "rulebook_path": str(args.rules.resolve()),
        "rulebook_sha256": _sha256(args.rules),
        "row_count": len(rows),
        "outputs": [
            "segment_audit.csv",
            "gold_candidates_unapproved.csv",
            "risk_candidates.csv",
            "process_owner_review_template.csv",
            "audit_summary.json",
            "audit_report.md",
        ],
        "frozen_v041_changed": False,
        "frozen_v4_artifact": str(FROZEN_V4_ARTIFACT.resolve()),
        "frozen_v4_sha256": _sha256(FROZEN_V4_ARTIFACT),
        "display_v041_artifact": str(DISPLAY_V041_ARTIFACT.resolve()),
        "display_v041_sha256": _sha256(DISPLAY_V041_ARTIFACT),
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary["grade_counts"], ensure_ascii=False))
    print(args.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
