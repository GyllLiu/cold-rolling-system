#!/usr/bin/env python3
"""Audit vendor tandem-pass guidance against governed high-speed DAT segments."""

from __future__ import annotations

import csv
import json
import math
import statistics
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
OUTPUT_DIR = WORKSPACE_ROOT / "outputs" / "v062_tandem_rule_audit"
FILES = sorted(WORKSPACE_ROOT.glob("outputs/data_foundation*/production_segments.csv"))


def number(row: dict[str, str], key: str) -> float:
    value = float(row[key])
    if not math.isfinite(value):
        raise ValueError(key)
    return value


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


def percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    low = math.floor(position)
    high = math.ceil(position)
    if low == high:
        return ordered[low]
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summary(values: list[float]) -> dict[str, float]:
    return {
        "p05": round(percentile(values, 0.05), 6),
        "median": round(statistics.median(values), 6),
        "p95": round(percentile(values, 0.95), 6),
    }


def load() -> list[dict[str, float]]:
    output: list[dict[str, float]] = []
    for path in FILES:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                try:
                    entry = number(row, "entry_thickness_set_mode")
                    middle = number(row, "intermediate_thickness_set_mode")
                    exit_value = number(row, "exit_thickness_set_mode")
                    width = number(row, "width_set_mode")
                    stable = number(row, "stable_high_speed_seconds_1hz")
                    uncoiler = number(row, "uncoiler_tension_set_median")
                    interstand = number(row, "interstand_tension_set_median")
                    coiler = number(row, "coiler_tension_set_median")
                except (KeyError, TypeError, ValueError):
                    continue
                if not (
                    truthy(row.get("learning_eligible"))
                    and truthy(row.get("digital_state_usable"))
                    and stable >= 120
                    and 0 < exit_value < middle < entry <= 30
                    and 500 <= width <= 2000
                ):
                    continue
                f1 = 1 - middle / entry
                f2 = 1 - exit_value / middle
                output.append({
                    "entry": entry,
                    "exit": exit_value,
                    "tandem_reduction": 1 - exit_value / entry,
                    "f1_reduction": f1,
                    "f2_reduction": f2,
                    "f1_minus_f2_pp": 100 * (f1 - f2),
                    "uncoiler": uncoiler,
                    "interstand": interstand,
                    "coiler": coiler,
                    "uncoiler_unit": 9.80665 * uncoiler / (width * entry),
                    "interstand_unit": 9.80665 * interstand / (width * middle),
                    "coiler_unit": 9.80665 * coiler / (width * exit_value),
                })
    return output


def percent(rows: list[dict[str, float]], predicate) -> float:
    return round(100 * sum(1 for row in rows if predicate(row)) / len(rows), 4)


def analyse(rows: list[dict[str, float]]) -> dict[str, object]:
    return {
        "case_count": len(rows),
        "tandem_pass_reduction": summary([row["tandem_reduction"] for row in rows]),
        "f1_reduction": summary([row["f1_reduction"] for row in rows]),
        "f2_reduction": summary([row["f2_reduction"] for row in rows]),
        "vendor_band_support_percent": {
            "whole_tandem_20_to_35": percent(rows, lambda row: 0.20 <= row["tandem_reduction"] <= 0.35),
            "whole_tandem_40_to_55": percent(rows, lambda row: 0.40 <= row["tandem_reduction"] <= 0.55),
            "f1_20_to_35": percent(rows, lambda row: 0.20 <= row["f1_reduction"] <= 0.35),
            "f2_20_to_35": percent(rows, lambda row: 0.20 <= row["f2_reduction"] <= 0.35),
            "f1_greater_than_f2": percent(rows, lambda row: row["f1_reduction"] > row["f2_reduction"]),
            "f1_minus_f2_5_to_15pp": percent(rows, lambda row: 5 <= row["f1_minus_f2_pp"] <= 15),
        },
        "tension_hierarchy_support_percent": {
            "raw_total_tonnes_uncoiler_lt_interstand_lt_coiler": percent(
                rows, lambda row: row["uncoiler"] < row["interstand"] < row["coiler"]
            ),
            "section_normalized_unit_tension_uncoiler_lt_interstand_lt_coiler": percent(
                rows,
                lambda row: row["uncoiler_unit"] < row["interstand_unit"] < row["coiler_unit"],
            ),
        },
        "unit_tension_kN_per_mm2": {
            name: summary([row[name] for row in rows])
            for name in ("uncoiler_unit", "interstand_unit", "coiler_unit")
        },
    }


def main() -> None:
    rows = load()
    if len(rows) < 500:
        raise RuntimeError(f"governed high-speed sample is incomplete: {len(rows)}")
    all_result = analyse(rows)
    thin_result = analyse([row for row in rows if row["exit"] <= 1.0])
    result = {
        "scope": "governed high-speed stable tandem-pass segments without reliable route-pass ordinal",
        "all": all_result,
        "thin_exit_le_1mm": thin_result,
        "decision": {
            "pass_ordinal_limitation": "Current DAT cannot reliably identify opening, middle, or finishing pass.",
            "vendor_route_rule": "Accept front-heavy/back-light as route guidance; exact opening and finishing bands require linked multi-pass coil records.",
            "band_validation": "The mixed high-speed segments neither validate nor refute the vendor's 40%-55% opening and 20%-35% finishing bands.",
            "f1_f2_rule": "F1>F2 is well supported; a 5-15 percentage-point difference is not universal.",
            "tension_rule": "Enforce the hierarchy in unit-tension space and convert back to HMI tonnes.",
        },
    }
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "audit.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    a = all_result
    t = thin_result
    markdown = f"""# 二机架整道次规则与高速 DAT 对照（v0.6.2）

## 数据口径

- 样本：{a['case_count']} 段可学习、数字状态可用且高速稳定不少于 120 秒的数据。
- 一个“道次”是钢卷依次经过 F1、F2；再次轧制需成卷后重新上卷。
- 当前数据没有可靠的卷号—道次序号串联，不能判断某段属于首道、中间道还是末道。

## 核验结果

| 统计项 | 全量高速数据 | 出口≤1 mm | 可得结论 |
|---|---:|---:|---|
| 混合段整道压下率在 20%—35% | {a['vendor_band_support_percent']['whole_tandem_20_to_35']:.1f}% | {t['vendor_band_support_percent']['whole_tandem_20_to_35']:.1f}% | 仅是混合段分布，不能当作末道核验 |
| 混合段整道压下率在 40%—55% | {a['vendor_band_support_percent']['whole_tandem_40_to_55']:.1f}% | {t['vendor_band_support_percent']['whole_tandem_40_to_55']:.1f}% | 仅是混合段分布，不能当作首道核验 |
| F1 单机架压下率在 20%—35% | {a['vendor_band_support_percent']['f1_20_to_35']:.1f}% | {t['vendor_band_support_percent']['f1_20_to_35']:.1f}% | 描述机架分布，不代表末道规则 |
| F2 单机架压下率在 20%—35% | {a['vendor_band_support_percent']['f2_20_to_35']:.1f}% | {t['vendor_band_support_percent']['f2_20_to_35']:.1f}% | 描述机架分布，不代表末道规则 |
| F1 压下率大于 F2 | {a['vendor_band_support_percent']['f1_greater_than_f2']:.1f}% | {t['vendor_band_support_percent']['f1_greater_than_f2']:.1f}% | 作为推荐方向 |
| F1 比 F2 高 5—15 个百分点 | {a['vendor_band_support_percent']['f1_minus_f2_5_to_15pp']:.1f}% | {t['vendor_band_support_percent']['f1_minus_f2_5_to_15pp']:.1f}% | 不能硬编码 |
| 总张力 T 满足开卷<机架<卷取 | {a['tension_hierarchy_support_percent']['raw_total_tonnes_uncoiler_lt_interstand_lt_coiler']:.1f}% | {t['tension_hierarchy_support_percent']['raw_total_tonnes_uncoiler_lt_interstand_lt_coiler']:.1f}% | 口径错误 |
| 单位张力满足开卷<机架<卷取 | {a['tension_hierarchy_support_percent']['section_normalized_unit_tension_uncoiler_lt_interstand_lt_coiler']:.1f}% | {t['tension_hierarchy_support_percent']['section_normalized_unit_tension_uncoiler_lt_interstand_lt_coiler']:.1f}% | 与 DAT 高度一致 |

## 算法处理

1. 厂商“首道较重、末道 20%—35%”按整套路线理解；当前先执行前重后轻方向，不声称 DAT 已核验具体首末道区间。
2. 每个生成道次仍按高速历史覆盖检查，并显示“F1 主变形、F2 调整”的关系。
3. 三段张力先按 `q = 9.80665 × T ÷ (宽度 × 当地厚度)` 换算为 kN/mm²，在单位张力空间校正顺序，再换回操作画面的 T。
4. 历史最大值只表示当前 DAT 覆盖，不冒充设备铭牌安全上限。
5. 等获得同一卷可串联的多道次记录后，再单独核验首道、各中间道和末道区间。
"""
    (OUTPUT_DIR / "二机架整道次_DAT核验报告.md").write_text(markdown, encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
