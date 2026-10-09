#!/usr/bin/env python3
"""Build the draft three-input specification-combination support gate.

Only A-class gold *candidates* from the governed DAT audit are used.  The
resulting artifact stores specification coordinates, never output targets, and
therefore cannot become a hidden nearest-neighbour recommendation model.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.process_framework import build_spec_support_envelope  # noqa: E402


DEFAULT_AUDIT = (
    WORKSPACE_ROOT / "outputs" / "process_framework_v1" / "segment_audit.csv"
)
DEFAULT_ARTIFACT = (
    PROJECT_ROOT / "process_framework" / "spec_support_envelope_v1.json"
)
DEFAULT_REPORT = (
    WORKSPACE_ROOT / "outputs" / "process_framework_v1" / "spec_support_report.md"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _report(envelope: dict[str, Any], *, audit_path: Path) -> str:
    thresholds = envelope["leave_one_unique_spec_out_nearest_distance"]
    scales = envelope["robust_scales"]
    return "\n".join(
        [
            "# 三输入规格组合支持度闸门 V1",
            "",
            f"- 状态：`{envelope['status']}`，尚未获主任批准用于正式拒绝。",
            f"- 来源：`{audit_path}` 中的 A 类黄金候选。",
            f"- 来源行数：{envelope['source_row_count']}。",
            f"- 去重规格组合数：{envelope['unique_specification_count']}。",
            "- 本闸门只衡量入口厚度、出口厚度、宽度组合的历史支持度，不读取历史输出、不做近邻平均。",
            "- 距离采用 log(入口厚度)、log(出口厚度)、宽度、log(入口/出口) 的稳健标准化欧氏距离。",
            "",
            "## 留一规格校准",
            "",
            "| 指标 | 距离 |",
            "| --- | ---: |",
            f"| P90 | {thresholds['p90']:.6f} |",
            f"| P95 | {thresholds['p95']:.6f} |",
            f"| P99 | {thresholds['p99']:.6f} |",
            f"| 最大值 | {thresholds['maximum']:.6f} |",
            "",
            "## 稳健尺度（P90-P10）",
            "",
            "| 特征 | 尺度 |",
            "| --- | ---: |",
            *[f"| `{name}` | {value:.9f} |" for name, value in scales.items()],
            "",
            "## 影子决策",
            "",
            "- 完全重合或距离不超过 P90：通过组合支持度闸门；这不会提高模型本身的置信度。",
            "- P90～P95：仅低置信人工试用。",
            "- 超过 P95 或任一原始输入越过 A 类边界：拒绝预测，先收集/确认对应工艺路线数据。",
            "- 在下一批真正未查看的新日期上冻结验证后，才可申请将本影子闸门接入正式页面。",
            "",
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    args = parser.parse_args()

    rows = _load_csv(args.audit)
    gold_candidates = [
        row
        for row in rows
        if row.get("grade") == "A"
        and row.get("training_pool_candidate", "").strip().lower() == "true"
    ]
    envelope = build_spec_support_envelope(gold_candidates)
    envelope["generated_at"] = datetime.now(timezone.utc).isoformat()
    envelope["source_audit"] = str(args.audit.resolve())
    envelope["source_audit_sha256"] = _sha256(args.audit)
    envelope["source_selection"] = (
        "grade == A and training_pool_candidate == true; individual rows and "
        "framework remain unapproved"
    )
    envelope["approved_for_production"] = False

    args.artifact.parent.mkdir(parents=True, exist_ok=True)
    args.artifact.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(_report(envelope, audit_path=args.audit), encoding="utf-8")
    print(
        f"gold_rows={len(gold_candidates)} "
        f"unique_specs={envelope['unique_specification_count']}"
    )
    print(args.artifact.resolve())
    print(args.report.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
