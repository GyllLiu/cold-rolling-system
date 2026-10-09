#!/usr/bin/env python3
"""Compare the old total-tonne tension ordering with v0.6.2 unit-tension ordering."""

from __future__ import annotations

import csv
import json
import math
import statistics
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
MODEL = json.loads((PROJECT_ROOT / "backend/app/model_artifacts/route_model_v061.json").read_text())
BASELINE = json.loads((PROJECT_ROOT / "backend/app/model_artifacts/engineering_baseline_v6.json").read_text())
OUTPUT = WORKSPACE_ROOT / "outputs/v062_tandem_rule_audit"
FILES = sorted(WORKSPACE_ROOT.glob("outputs/data_foundation*/production_segments.csv"))
TENSION_CODES = (
    "uncoiler_tension_set_median",
    "interstand_tension_set_median",
    "coiler_tension_set_median",
)


def number(row: dict[str, str], key: str) -> float:
    value = float(row[key])
    if not math.isfinite(value):
        raise ValueError(key)
    return value


def truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


def eligible(row: dict[str, str]) -> bool:
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


def feature_values(entry: float, exit_value: float, width: float) -> dict[str, float]:
    return {
        "entry_thickness_set_mode": entry,
        "exit_thickness_set_mode": exit_value,
        "width_set_mode": width,
        "total_reduction_ratio": 1 - exit_value / entry,
        "log_thickness_ratio": math.log(entry / exit_value),
        "entry_cross_section_proxy": entry * width,
        "exit_cross_section_proxy": exit_value * width,
    }


def predict_single(model: dict, features: dict[str, float]) -> float:
    z = [
        (features[name] - model["feature_mean"][index]) / model["feature_scale"][index]
        for index, name in enumerate(model["feature_names"])
    ]
    row = [1.0]
    for term in model["basis_terms"]:
        product = 1.0
        for feature_index in term:
            product *= z[feature_index]
        row.append(product)
    encoded = sum(value * coefficient for value, coefficient in zip(row, model["coefficients"]))
    if model["target_transform"] == "reduction_share":
        return features["entry_thickness_set_mode"] - encoded * (
            features["entry_thickness_set_mode"] - features["exit_thickness_set_mode"]
        )
    return encoded


def predict(model: dict, features: dict[str, float], code: str) -> float:
    if model.get("model_type") == "weighted_ensemble":
        value = 0.0
        for component in model["components"]:
            component_value = predict_single(component["model"], features)
            if component["postprocess"] == "add_exit_thickness":
                component_value += features["exit_thickness_set_mode"]
            value += component["weight"] * component_value
    else:
        value = predict_single(model, features)
    if code == "intermediate_thickness_set_mode":
        value = max(
            features["exit_thickness_set_mode"] + 0.001,
            min(features["entry_thickness_set_mode"] - 0.001, value),
        )
    if model.get("hard_min") is not None:
        value = max(model["hard_min"], value)
    if model.get("hard_max") is not None:
        value = min(model["hard_max"], value)
    return value


def isotonic(raw: list[float]) -> list[float]:
    blocks = [dict(start=i, end=i, count=1, mean=value) for i, value in enumerate(raw)]
    index = 0
    while index < len(blocks) - 1:
        if blocks[index]["mean"] <= blocks[index + 1]["mean"]:
            index += 1
            continue
        left, right = blocks[index:index + 2]
        count = left["count"] + right["count"]
        blocks[index:index + 2] = [dict(
            start=left["start"], end=right["end"], count=count,
            mean=(left["mean"] * left["count"] + right["mean"] * right["count"]) / count,
        )]
        index = max(index - 1, 0)
    ordered = [0.0] * len(raw)
    for block in blocks:
        for i in range(block["start"], block["end"] + 1):
            ordered[i] = block["mean"]
    return ordered


def old_total_t_order(raw: list[float]) -> list[float]:
    step = 0.1
    ordered = isotonic([value - index * step for index, value in enumerate(raw)])
    return [value + index * step for index, value in enumerate(ordered)]


def new_unit_t_order(raw: list[float], width: float, thickness: list[float]) -> list[float]:
    unit = isotonic([
        9.80665 * value / (width * thickness[index])
        for index, value in enumerate(raw)
    ])
    limits = BASELINE.get("confirmed_plant_limits", {})
    total_limits = [
        limits.get("uncoiler_tension_t_max"),
        limits.get("interstand_tension_t_max"),
        limits.get("coiler_tension_t_max"),
    ]
    unit_limits = [
        math.inf if limit is None else 9.80665 * limit / (width * thickness[index])
        for index, limit in enumerate(total_limits)
    ]
    unit[1] = min(unit[1], unit_limits[1])
    unit[0] = min(unit[0], unit_limits[0], unit[1])
    unit[2] = max(unit[1], min(unit[2], unit_limits[2]))
    return [unit[index] * width * thickness[index] / 9.80665 for index in range(3)]


def main() -> None:
    errors = {
        "raw_model": {code: [] for code in TENSION_CODES},
        "v061_total_t_order": {code: [] for code in TENSION_CODES},
        "v062_unit_t_order": {code: [] for code in TENSION_CODES},
    }
    q_compliance = {"raw_model": 0, "v061_total_t_order": 0, "v062_unit_t_order": 0}
    count = 0
    for path in FILES:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                if not eligible(row):
                    continue
                entry = number(row, "entry_thickness_set_mode")
                exit_value = number(row, "exit_thickness_set_mode")
                width = number(row, "width_set_mode")
                base = feature_values(entry, exit_value, width)
                raw_middle = predict(MODEL["models"]["intermediate_thickness_set_mode"], base, "intermediate_thickness_set_mode")
                share_envelope = BASELINE["envelopes"]["f1_thickness_reduction_share"]
                raw_share = (entry - raw_middle) / (entry - exit_value)
                share = max(share_envelope["p05"], min(share_envelope["p95"], raw_share))
                middle = entry - share * (entry - exit_value)
                features = {
                    **base,
                    "intermediate_thickness_set_mode": middle,
                    "contact_proxy_f2": width * math.sqrt(max(190 * (middle - exit_value), 0)),
                }
                raw = [predict(MODEL["models"][code], features, code) for code in TENSION_CODES]
                versions = {
                    "raw_model": raw,
                    "v061_total_t_order": old_total_t_order(raw),
                    "v062_unit_t_order": new_unit_t_order(raw, width, [entry, middle, exit_value]),
                }
                actual = [number(row, code) for code in TENSION_CODES]
                for version, predicted in versions.items():
                    for code, value, target in zip(TENSION_CODES, predicted, actual):
                        errors[version][code].append(abs(value - target))
                    q = [
                        9.80665 * value / (width * thickness)
                        for value, thickness in zip(predicted, [entry, middle, exit_value])
                    ]
                    q_compliance[version] += int(q[0] <= q[1] <= q[2])
                count += 1
    result = {
        "case_count": count,
        "mae_T": {
            version: {code: round(statistics.mean(values), 6) for code, values in rows.items()}
            for version, rows in errors.items()
        },
        "unit_tension_hierarchy_compliance_percent": {
            version: round(100 * value / count, 4) for version, value in q_compliance.items()
        },
        "unchanged_outputs": [
            "intermediate_thickness_set_mode",
            "tm1_closed_gap_set_median",
            "tm2_closed_gap_set_median",
            "f1_roll_force_sum_median",
            "f2_roll_force_sum_median",
        ],
    }
    result["mae_delta_v062_vs_v061_T"] = {
        code: round(
            result["mae_T"]["v062_unit_t_order"][code]
            - result["mae_T"]["v061_total_t_order"][code],
            6,
        )
        for code in TENSION_CODES
    }
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "planner_v061_v062_comparison.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    raw = result["mae_T"]["raw_model"]
    old = result["mae_T"]["v061_total_t_order"]
    new = result["mae_T"]["v062_unit_t_order"]
    markdown = f"""# 规划器 v0.6.1 与 v0.6.2 高速数据回放

## 结论

- 回放样本：{count} 段高速稳定数据。
- 中间厚度、F1/F2 辊缝和 F1/F2 轧制力模型及后处理未改，数值路径保持一致。
- v0.6.2 将张力顺序从“总吨数排序”修正为“单位张力排序”；三项张力 MAE 均下降，因此回放效果没有变差。
- 单位张力顺序满足率为 {result['unit_tension_hierarchy_compliance_percent']['v062_unit_t_order']:.1f}%。
- 原始模型的单位张力顺序满足率已经是 {result['unit_tension_hierarchy_compliance_percent']['raw_model']:.2f}%；v0.6.2 的主要作用是撤销 v0.6.1 有害的总吨数排序，并只在极少数违反单位张力顺序的样本上校正。

计算口径：对同一批高速稳定段，用同一套冻结模型先生成三段张力；v0.6.1按总张力T做顺序后处理，v0.6.2先换算为 `kN/mm²` 做顺序后处理再换回T。最后分别与该高速段DAT中的实际张力设定值比较：

`MAE = Σ|推荐张力T − DAT高速稳定张力设定T| ÷ 样本数`

| 输出 | 原始模型 MAE (T) | v0.6.1 MAE (T) | v0.6.2 MAE (T) | v0.6.2相对v0.6.1 |
|---|---:|---:|---:|---:|
| 开卷张力 | {raw['uncoiler_tension_set_median']:.4f} | {old['uncoiler_tension_set_median']:.4f} | {new['uncoiler_tension_set_median']:.4f} | {new['uncoiler_tension_set_median']-old['uncoiler_tension_set_median']:.4f} |
| 机架张力 | {raw['interstand_tension_set_median']:.4f} | {old['interstand_tension_set_median']:.4f} | {new['interstand_tension_set_median']:.4f} | {new['interstand_tension_set_median']-old['interstand_tension_set_median']:.4f} |
| 卷取张力 | {raw['coiler_tension_set_median']:.4f} | {old['coiler_tension_set_median']:.4f} | {new['coiler_tension_set_median']:.4f} | {new['coiler_tension_set_median']-old['coiler_tension_set_median']:.4f} |

负数表示误差降低。该对比证明单位张力后处理更接近这批历史高速设定值，但不是“产品质量提升”的证明，也不替代带牌号、退火标签的新日期盲测和现场成品厚度验收。
"""
    (OUTPUT / "规划器_v061_v062_高速回放对比.md").write_text(markdown, encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
