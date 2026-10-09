#!/usr/bin/env python3
"""Audit specification coverage after importing additional DAT days.

This script does not train or modify a model. It reports the distinction
between every detected candidate and the governed high-speed stable subset.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections import Counter
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
OUTPUT_DIR = WORKSPACE_ROOT / "outputs" / "v063_spec_coverage_audit"
FILES = sorted((WORKSPACE_ROOT / "outputs").glob("data_foundation*/production_segments.csv"))


def day_for(path: Path) -> str:
    suffix = path.parent.name.removeprefix("data_foundation_")
    return "260703" if suffix == "data_foundation" else suffix


def number(row: dict[str, str], key: str) -> float:
    value = float(row[key])
    if not math.isfinite(value):
        raise ValueError(key)
    return value


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


def governed(row: dict[str, str]) -> bool:
    try:
        entry = number(row, "entry_thickness_set_mode")
        middle = number(row, "intermediate_thickness_set_mode")
        exit_value = number(row, "exit_thickness_set_mode")
        width = number(row, "width_set_mode")
        stable = number(row, "stable_high_speed_seconds_1hz")
    except (KeyError, TypeError, ValueError):
        return False
    return (
        truthy(row.get("learning_eligible"))
        and truthy(row.get("digital_state_usable"))
        and stable >= 120
        and 0 < exit_value < middle < entry <= 30
        and 500 <= width <= 2000
    )


def percentile(values: list[float], probability: float) -> float:
    values = sorted(values)
    position = (len(values) - 1) * probability
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return values[low]
    return values[low] + (values[high] - values[low]) * (position - low)


def distribution(values: list[float]) -> dict[str, float]:
    return {
        "min": round(min(values), 6),
        "p01": round(percentile(values, 0.01), 6),
        "median": round(statistics.median(values), 6),
        "p99": round(percentile(values, 0.99), 6),
        "max": round(max(values), 6),
    }


def main() -> None:
    all_rows: list[dict[str, object]] = []
    governed_rows: list[dict[str, object]] = []
    day_summary: dict[str, dict[str, int]] = {}
    for path in FILES:
        day = day_for(path)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for raw in csv.DictReader(handle):
                try:
                    row = {
                        "day": day,
                        "candidate_id": raw.get("candidate_id", ""),
                        "entry": number(raw, "entry_thickness_set_mode"),
                        "middle": number(raw, "intermediate_thickness_set_mode"),
                        "exit": number(raw, "exit_thickness_set_mode"),
                        "width": number(raw, "width_set_mode"),
                        "stable_seconds": number(raw, "stable_high_speed_seconds_1hz"),
                    }
                except (KeyError, TypeError, ValueError):
                    continue
                all_rows.append(row)
                day_summary.setdefault(day, {"candidate": 0, "governed": 0})["candidate"] += 1
                if governed(raw):
                    governed_rows.append(row)
                    day_summary[day]["governed"] += 1

    if not governed_rows:
        raise RuntimeError("no governed high-speed rows")

    entry_counts = Counter(row["entry"] for row in governed_rows)
    rows_above_7 = [row for row in governed_rows if float(row["entry"]) > 7]
    incomplete_days = []
    for day in sorted(day_summary):
        raw_dir = WORKSPACE_ROOT / "数据" / day
        index_path = WORKSPACE_ROOT / "outputs" / f"data_foundation_{day}" / "file_index.csv"
        if not raw_dir.exists() or not index_path.exists():
            continue
        raw_count = sum(1 for _ in raw_dir.rglob("*.dat"))
        with index_path.open(encoding="utf-8-sig", newline="") as handle:
            indexed_count = sum(1 for _ in csv.DictReader(handle))
        if raw_count != indexed_count:
            incomplete_days.append({
                "day": day,
                "raw_file_count_at_audit": raw_count,
                "indexed_file_count": indexed_count,
            })
    result = {
        "source_file_count": len(FILES),
        "candidate_count": len(all_rows),
        "governed_high_speed_count": len(governed_rows),
        "day_summary": day_summary,
        "incomplete_or_syncing_days": incomplete_days,
        "governed_distribution": {
            "entry_thickness_mm": distribution([float(row["entry"]) for row in governed_rows]),
            "exit_thickness_mm": distribution([float(row["exit"]) for row in governed_rows]),
            "width_mm": distribution([float(row["width"]) for row in governed_rows]),
        },
        "all_candidate_distribution": {
            "entry_thickness_mm": distribution([float(row["entry"]) for row in all_rows]),
            "exit_thickness_mm": distribution([float(row["exit"]) for row in all_rows]),
        },
        "governed_entry_mode_counts": {
            str(value): count for value, count in sorted(entry_counts.items())
        },
        "governed_entry_above_7mm": rows_above_7,
        "decision": {
            "expand_to_8mm": len([row for row in rows_above_7 if float(row["entry"]) >= 8]) >= 20,
            "expand_to_10mm": len([row for row in rows_above_7 if float(row["entry"]) >= 10]) >= 20,
            "gate": "No model-bound expansion without enough governed high-speed rows and a held-out date at the proposed thickness.",
        },
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "coverage.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    entry = result["governed_distribution"]["entry_thickness_mm"]
    exit_dist = result["governed_distribution"]["exit_thickness_mm"]
    all_entry = result["all_candidate_distribution"]["entry_thickness_mm"]
    rows = "\n".join(
        f"| {day} | {counts['candidate']} | {counts['governed']} |"
        for day, counts in sorted(day_summary.items())
    )
    high_rows = "\n".join(
        f"| {row['day']} | {row['entry']:.3f} | {row['middle']:.3f} | {row['exit']:.3f} | {row['width']:.0f} | {row['stable_seconds']:.0f} |"
        for row in rows_above_7
    ) or "| — | — | — | — | — | — |"
    markdown = f"""# 新增 DAT 规格覆盖审计 v0.6.3

## 结论

- 共读取 {len(FILES)} 个日期输出、{len(all_rows)} 个候选段，其中 {len(governed_rows)} 个满足当前高速稳定学习条件。
- 合格入口厚度范围：{entry['min']}—{entry['max']} mm；P01—P99 为 {entry['p01']}—{entry['p99']} mm。
- 包含低速调试在内的全部候选段，入口最大值也是 {all_entry['max']} mm。
- 合格出口厚度范围：{exit_dist['min']}—{exit_dist['max']} mm；P01—P99 为 {exit_dist['p01']}—{exit_dist['p99']} mm。
- 入口大于 7 mm 的合格高速段：{len(rows_above_7)} 个。
- 是否可扩到 8 mm：{result['decision']['expand_to_8mm']}；是否可扩到 10 mm：{result['decision']['expand_to_10mm']}。
- 同步/解析尚未一致的日期：{', '.join(row['day'] for row in incomplete_days) or '无'}。这些日期的结果只作临时统计，不作为放宽边界依据。

判断原则：必须有足够的高速稳定样本，并至少留出一个新日期在该厚度区间做时间外推；不能因为低速调试段或设备个例出现过该厚度就放开模型。

## 各日期

| 日期 | 候选段 | 合格高速段 |
|---|---:|---:|
{rows}

## 入口厚度大于 7 mm 的合格段

| 日期 | 入口 mm | 中间 mm | 出口 mm | 宽度 mm | 高速稳定秒数 |
|---|---:|---:|---:|---:|---:|
{high_rows}
"""
    (OUTPUT_DIR / "新增DAT规格覆盖报告.md").write_text(markdown, encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
