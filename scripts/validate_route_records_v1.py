#!/usr/bin/env python3
"""Validate director-confirmed coil/pass labels before any route-model training."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.process_framework import (  # noqa: E402
    DEFAULT_MATERIAL_LIBRARY_PATH,
    DEFAULT_RULEBOOK_PATH,
)
from app.route_governance import validate_route_records  # noqa: E402


DEFAULT_OUTPUT_DIR = (
    WORKSPACE_ROOT / "outputs" / "process_framework_v1" / "route_label_validation"
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


def _recipe_catalogue(rows: list[Mapping[str, str]]) -> dict[str, dict[str, str]]:
    catalogue: dict[str, dict[str, str]] = {}
    for row in rows:
        recipe_id = str(row.get("anneal_recipe_id", "")).strip().upper()
        if recipe_id:
            catalogue[recipe_id] = dict(row)
    return catalogue


def _report(records: list[dict[str, Any]], issue_counts: Counter[str]) -> str:
    candidate_count = sum(bool(row["parameter_training_candidate"]) for row in records)
    eligible_count = sum(bool(row["route_training_eligible"]) for row in records)
    lines = [
        "# 真实卷/道次标签校验报告 V1",
        "",
        f"- 路线数：{len(records)}。",
        f"- 结构及人工标签达到候选条件：{candidate_count}。",
        f"- 通过全部正式训练闸门：{eligible_count}。",
        "- 候选不等于可训练；规则库、设备限制、材料路线和退火制度任一未批准都会阻断。",
        "",
        "## 路线结果",
        "",
        "| 路线 | 道次 | 结构有效 | 标签完整 | A类DAT | 无异常操作 | 成品合格 | 主任批准 | 候选 | 可训练 |",
        "| --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in records:
        lines.append(
            f"| {row['route_id']} | {row['row_count']} | "
            f"{row['structurally_valid']} | {row['label_complete']} | "
            f"{row['dat_gold_only']} | {row['no_abnormal_operation']} | "
            f"{row['final_quality_passed']} | {row['process_owner_approved']} | "
            f"{row['parameter_training_candidate']} | {row['route_training_eligible']} |"
        )
    lines.extend(
        [
            "",
            "## 问题代码",
            "",
            "| 代码 | 数量 |",
            "| --- | ---: |",
        ]
    )
    for code, count in issue_counts.most_common():
        lines.append(f"| `{code}` | {count} |")
    if not issue_counts:
        lines.append("| 无 | 0 |")
    lines.extend(
        [
            "",
            "本程序不修改模型、不自动批准样本、不写 PLC。",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="真实卷道次CSV")
    parser.add_argument("--anneal-recipes", type=Path, help="已批准退火制度CSV")
    parser.add_argument("--rules", type=Path, default=DEFAULT_RULEBOOK_PATH)
    parser.add_argument("--materials", type=Path, default=DEFAULT_MATERIAL_LIBRARY_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    rows = _load_csv(args.input)
    rulebook = json.loads(args.rules.read_text(encoding="utf-8"))
    material_library = json.loads(args.materials.read_text(encoding="utf-8"))
    recipes = (
        _recipe_catalogue(_load_csv(args.anneal_recipes))
        if args.anneal_recipes
        else {}
    )
    results = validate_route_records(
        rows,
        rulebook=rulebook,
        material_library=material_library,
        anneal_recipes=recipes,
    )
    route_records = [result.to_record() for result in results]
    issues = [
        issue.to_dict()
        for result in results
        for issue in result.issues
    ]
    issue_counts = Counter(issue["code"] for issue in issues)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(args.output_dir / "route_validation.csv", route_records)
    _write_csv(args.output_dir / "route_validation_issues.csv", issues)
    (args.output_dir / "route_validation.json").write_text(
        json.dumps([result.to_dict() for result in results], ensure_ascii=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    (args.output_dir / "report.md").write_text(
        _report(route_records, issue_counts), encoding="utf-8"
    )
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "validate_human_confirmed_route_labels_before_training",
        "input": str(args.input.resolve()),
        "input_sha256": _sha256(args.input),
        "anneal_recipes": (
            str(args.anneal_recipes.resolve()) if args.anneal_recipes else None
        ),
        "anneal_recipes_sha256": (
            _sha256(args.anneal_recipes) if args.anneal_recipes else None
        ),
        "rules": str(args.rules.resolve()),
        "rules_sha256": _sha256(args.rules),
        "materials": str(args.materials.resolve()),
        "materials_sha256": _sha256(args.materials),
        "route_count": len(results),
        "route_training_eligible_count": sum(
            result.route_training_eligible for result in results
        ),
        "automatic_model_training": False,
        "automatic_plc_write": False,
    }
    (args.output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"routes={len(results)} eligible={manifest['route_training_eligible_count']}")
    print(args.output_dir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
