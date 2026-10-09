"""Markdown and dependency-free SVG reporting."""

from __future__ import annotations

import html
import json
import statistics
from collections import Counter
from pathlib import Path
from typing import Any

from .column_mapper import CHANNELS, ROLE_BY_ALIAS
from .outlier_detector import finite, percentile


def build_all_channel_inventory(
    channel_dictionaries: list[tuple[Path, list[dict[str, str]]]],
    total_file_count: int,
) -> list[dict[str, Any]]:
    """Union every foundation dictionary without pretending all channels were decoded.

    The per-day dictionaries already contain the channel type and file occurrence
    count.  Aggregating those counters gives an auditable all-DAT inventory while
    the more expensive value distribution profile remains limited to the core
    business channels used by the cleaner.
    """
    inventory: dict[str, dict[str, Any]] = {}
    for dictionary_path, rows in channel_dictionaries:
        dataset = dictionary_path.parent.name.removeprefix("data_foundation_")
        for row in rows:
            name = row.get("channel_name", "").strip()
            if not name:
                continue
            item = inventory.setdefault(
                name,
                {
                    "channel_name": name,
                    "channel_types": set(),
                    "source_modules": set(),
                    "categories": set(),
                    "roles": set(),
                    "unit_hints": set(),
                    "dataset_count": 0,
                    "datasets": [],
                    "file_occurrence_count": 0,
                    "notes": set(),
                },
            )
            for source_key, target_key in (
                ("channel_type", "channel_types"),
                ("source_module", "source_modules"),
                ("category", "categories"),
                ("role", "roles"),
                ("unit_hint", "unit_hints"),
                ("notes", "notes"),
            ):
                value = row.get(source_key, "").strip()
                if value:
                    item[target_key].add(value)
            item["dataset_count"] += 1
            item["datasets"].append(dataset)
            try:
                item["file_occurrence_count"] += int(float(row.get("file_occurrence_count") or 0))
            except ValueError:
                pass
    output: list[dict[str, Any]] = []
    core_names = set(CHANNELS.values())
    for item in inventory.values():
        output.append(
            {
                "channel_name": item["channel_name"],
                "channel_type": ";".join(sorted(item["channel_types"])) or "UNKNOWN",
                "source_module": ";".join(sorted(item["source_modules"])) or "UNKNOWN",
                "category": ";".join(sorted(item["categories"])) or "UNKNOWN",
                "role": ";".join(sorted(item["roles"])) or "UNKNOWN",
                "unit_hint": ";".join(sorted(item["unit_hints"])) or "UNKNOWN",
                "is_core_cleaning_channel": item["channel_name"] in core_names,
                "dataset_count": item["dataset_count"],
                "datasets": ";".join(item["datasets"]),
                "file_occurrence_count": item["file_occurrence_count"],
                "file_coverage_fraction": (
                    item["file_occurrence_count"] / total_file_count if total_file_count else 0
                ),
                "value_profile_status": (
                    "see column_profile.csv" if item["channel_name"] in core_names
                    else "inventory_only_not_value_decoded"
                ),
                "notes": ";".join(sorted(item["notes"])),
            }
        )
    return sorted(output, key=lambda row: row["channel_name"])


def write_column_mapping(path: Path, all_channel_count: int) -> None:
    lines = [
        "# 冷二联 DAT 字段映射",
        "",
        f"当前通道字典共 {all_channel_count} 个不同原始通道。本清洗层只解码黄金段识别需要的通道，其他通道仍保留在原始 DAT。",
        "",
        "## 已确认业务映射",
        "",
        "| 业务字段 | 原始 DAT 通道 | 角色 | 结论 |",
        "| --- | --- | --- | --- |",
    ]
    for alias, channel in CHANNELS.items():
        conclusion = "已按通道名映射"
        if alias == "intermediate_thickness_actual":
            conclusion = "PLC计算厚度，不等同于独立厚度仪实测"
        if alias in {"filter_oil_ok", "thin_oil_ok", "oil_air_ok"}:
            conclusion = "仅正常/异常状态，不是油温、流量或压力"
        if alias in {"fire_emergency_stop", "total_emergency_stop", "total_quick_stop"}:
            conclusion = "正常高速时常为1，PLC极性未确认；仅保留审计，不作硬报警"
        lines.append(f"| `{alias}` | `{channel}` | {ROLE_BY_ALIAS.get(alias, 'process signal')} | {conclusion} |")
    lines.extend(
        [
            "",
            "## 当前无法确定或不存在",
            "",
            "- `coil_id`、真实道次号：DAT 中没有明确字段；输出使用候选段代理ID，并明确标注推断置信度。",
            "- 牌号、来料状态、是否退火、质量合格结果：DAT 中没有明确字段，禁止猜测。",
            "- F1/F2轧制力设定：没有发现明确设定通道；只使用总轧制力实际反馈。",
            "- 润滑油温度、流量、压力、黏度和喷淋开度：没有命名通道。`R_102~R_107` 保持 `UNKNOWN`，未取得PLC点表前不作为润滑量使用。",
            "- 原始母卷厚度链和累计真实应变：没有跨道次卷ID，暂不强行计算。",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def _median(rows: list[dict[str, Any]], field: str) -> float | None:
    values = finite(row.get(field) for row in rows)
    return statistics.median(values) if values else None


def write_report(
    path: Path,
    *,
    raw_summary: dict[str, Any],
    gold_rows: list[dict[str, Any]],
    all_high_rows: list[dict[str, Any]],
    low_rows: list[dict[str, Any]],
    thresholds: dict[str, Any],
    profile_rows: list[dict[str, Any]],
    figure_names: list[str],
) -> None:
    levels = Counter(row.get("golden_level", "UNKNOWN") for row in all_high_rows)
    abnormalities = Counter(
        reason
        for row in all_high_rows
        for reason in str(row.get("abnormal_reason", "")).split(";")
        if reason and reason != "None"
    )
    high_force_cv = [_median(all_high_rows, "f1_force_cv"), _median(all_high_rows, "f2_force_cv")]
    low_force_cv = [_median(low_rows, "f1_force_cv"), _median(low_rows, "f2_force_cv")]
    high_tension_cv = [
        _median(all_high_rows, "entry_tension_cv"),
        _median(all_high_rows, "interstand_tension_cv"),
        _median(all_high_rows, "exit_tension_cv"),
    ]
    low_tension_cv = [
        _median(low_rows, "entry_tension_cv"),
        _median(low_rows, "interstand_tension_cv"),
        _median(low_rows, "exit_tension_cv"),
    ]
    def fmt(value: Any, digits: int = 4) -> str:
        return "—" if value is None else f"{float(value):.{digits}f}"
    total_windows = max(1, len(all_high_rows))
    lines = [
        "# 冷二联黄金工艺数据清洗报告",
        "",
        f"- 清洗版本：`{raw_summary['version']}`",
        f"- 原始 DAT 文件：{raw_summary['file_count']:,}（有效 {raw_summary.get('valid_file_count', raw_summary['file_count']):,}，异常 {raw_summary.get('invalid_file_count', 0):,}）",
        f"- 有效文件原始100Hz帧数：{raw_summary['raw_frame_count']:,}",
        f"- 时间跨度：{raw_summary['time_start']} 至 {raw_summary['time_end']}",
        f"- 明确卷号数量：0（DAT缺失）；推断候选卷段：{raw_summary['candidate_count']:,}",
        f"- 实际解析的1Hz候选数据点：{raw_summary['sampled_point_count']:,}",
        "",
        "## 清洗结果",
        "",
        "| 类别 | 30秒窗口数 | 占高速窗口比例 |",
        "| --- | ---: | ---: |",
        f"| GOLD_A | {levels['GOLD_A']} | {levels['GOLD_A']/total_windows:.1%} |",
        f"| GOLD_B | {levels['GOLD_B']} | {levels['GOLD_B']/total_windows:.1%} |",
        f"| NORMAL | {levels['NORMAL']} | {levels['NORMAL']/total_windows:.1%} |",
        f"| ABNORMAL | {levels['ABNORMAL']} | {levels['ABNORMAL']/total_windows:.1%} |",
        f"| 合并后的黄金稳定段 | {len(gold_rows)} | — |",
        "",
        "黄金数据是一段稳定平台一条记录，不是每秒一条训练样本。`coil_id` 为推断代理ID，不能冒充真实卷号。",
        "",
        "## 自适应阈值",
        "",
        "阈值由所有高速候选30秒窗口的CV分布生成，同时受工程上限约束；不是只凭经验写死。",
        "",
        "| 指标 | A阈值 | B阈值 | 实测中位数 | P90 |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for family in ("speed", "f1_force", "f2_force", "entry_tension", "interstand_tension", "exit_tension"):
        item = thresholds[family]
        lines.append(
            f"| {family} CV | {fmt(item['a'])} | {fmt(item['b'])} | {fmt(item['observed_median'])} | {fmt(item['observed_p90'])} |"
        )
    lines.extend(
        [
            "",
            "## 重点验证问题",
            "",
            f"1. 高速段F1/F2轧制力CV中位数为 {fmt(high_force_cv[0])}/{fmt(high_force_cv[1])}；低速调试窗为 {fmt(low_force_cv[0])}/{fmt(low_force_cv[1])}。",
            f"2. 高速段三段张力CV中位数为 {'/'.join(fmt(value) for value in high_tension_cv)}；低速窗为 {'/'.join(fmt(value) for value in low_tension_cv)}。",
            f"3. 自适应高速时间占活动时间中位数为 {fmt(raw_summary.get('median_high_speed_ratio'), 3)}。",
            f"4. 长期高速但参数波动窗口数量：{sum(row.get('golden_level') == 'NORMAL' and (row.get('speed_cv') or 0) < thresholds['speed']['b'] for row in all_high_rows)}。",
            f"5. 低速但稳定窗口数量：{sum(all((row.get(field) or 999) <= thresholds[family]['a'] for family, field in [('f1_force','f1_force_cv'),('f2_force','f2_force_cv')]) for row in low_rows)}。这些窗口仅用于对比，不进入黄金训练集。",
            f"6. 固定400m/min识别 {raw_summary.get('fixed_high_speed_seconds',0):,} 秒；自适应阈值识别 {raw_summary.get('adaptive_high_speed_seconds',0):,} 秒。自适应方法能适配不同卷的生产平台，但仍用群体P10速度防止把异常低速卷误判为A类。",
            "7. 最有价值的识别字段：机架间实际速度、F1/F2总轧制力实际值、三段实际张力、三段设定值变化、出口实际厚度、断带/急停/降速状态。",
            "8. 最大缺口：真实卷号/道次、牌号与状态、质量判定、润滑连续量、换辊记录。它们比继续增加无标签DAT数量更影响后续推荐可靠性。",
            "",
            "## 最常见排除原因 TOP 10",
            "",
            "| 原因 | 窗口数 |",
            "| --- | ---: |",
        ]
    )
    for reason, count in abnormalities.most_common(10):
        lines.append(f"| `{reason}` | {count} |")
    lines.extend(
        [
            "",
        "## 当前使用限制",
            "",
            "- 没有质量合格标签，因此 GOLD_A 代表过程稳定，不等于已经证明成品合格。",
            "- 没有真实卷号和道次，当前只能形成卷段代理样本，不能计算可靠的跨道次累计应变。",
            "- F1中间实际厚度使用PLC计算厚度，不能当作独立厚度仪的绝对真值。",
            "- 润滑只有三个正常/异常状态，没有油温、油量、油压等连续量。",
            "- 本次产物应先用于重新训练和时间外推验证，验证不下降后再替换旧训练集。",
            "- `all_channel_inventory.csv` 覆盖全部原始通道的名称、类型和文件覆盖率；`column_profile.csv` 仅对清洗实际使用的核心通道统计数值分布。",
            "",
            "## 单卷段时序图",
            "",
        ]
    )
    lines.extend(f"- [{name}](figures/{name})" for name in figure_names)
    path.write_text("\n".join(lines), encoding="utf-8")


def write_trace_svg(
    path: Path,
    points: list[dict[str, Any]],
    stable_start: str,
    stable_end: str,
    title: str,
) -> None:
    signals = [
        ("line_speed_actual", "速度 m/min", "#00a6a6"),
        ("f1_force_actual", "F1总轧制力 T", "#d85c41"),
        ("f2_force_actual", "F2总轧制力 T", "#5475c6"),
        ("entry_tension_actual", "开卷张力 T", "#c58a1b"),
        ("interstand_tension_actual", "机架张力 T", "#7b61a8"),
        ("exit_tension_actual", "卷取张力 T", "#27805b"),
    ]
    width, panel_h, left, right, top = 1100, 118, 100, 30, 72
    height = top + panel_h * len(signals) + 45
    if not points:
        return
    times = [point["timestamp"].timestamp() for point in points]
    t0, t1 = min(times), max(times)
    stable0 = __import__("datetime").datetime.fromisoformat(stable_start).timestamp()
    stable1 = __import__("datetime").datetime.fromisoformat(stable_end).timestamp()
    def x(value: float) -> float:
        return left + (value - t0) / max(t1 - t0, 1) * (width - left - right)
    chunks = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#f4f3ef"/>',
        f'<text x="{left}" y="36" font-family="Arial, sans-serif" font-size="22" font-weight="700" fill="#172329">{html.escape(title)}</text>',
        f'<text x="{left}" y="58" font-family="Arial, sans-serif" font-size="13" fill="#5d6a70">灰色：非黄金阶段　绿色：本次黄金稳定区</text>',
    ]
    for index, (alias, label, color) in enumerate(signals):
        y0 = top + index * panel_h
        values = finite(point.get(alias) for point in points)
        if not values:
            continue
        vmin, vmax = min(values), max(values)
        pad = max((vmax - vmin) * 0.08, 0.01)
        vmin, vmax = vmin - pad, vmax + pad
        def y(value: float) -> float:
            return y0 + panel_h - 22 - (value - vmin) / max(vmax - vmin, 1e-12) * (panel_h - 40)
        chunks.append(f'<rect x="{left}" y="{y0}" width="{width-left-right}" height="{panel_h-10}" fill="#ffffff" stroke="#d6d8d5"/>')
        chunks.append(f'<rect x="{x(stable0):.1f}" y="{y0}" width="{max(1,x(stable1)-x(stable0)):.1f}" height="{panel_h-10}" fill="#dff0e7"/>')
        coords = []
        for point in points:
            raw = point.get(alias)
            if isinstance(raw, (int, float)):
                coords.append(f"{x(point['timestamp'].timestamp()):.1f},{y(float(raw)):.1f}")
        chunks.append(f'<polyline points="{" ".join(coords)}" fill="none" stroke="{color}" stroke-width="1.7"/>')
        chunks.append(f'<text x="12" y="{y0+24}" font-family="Arial, sans-serif" font-size="14" font-weight="700" fill="#26343a">{html.escape(label)}</text>')
        chunks.append(f'<text x="12" y="{y0+46}" font-family="Arial, sans-serif" font-size="11" fill="#6b777c">{vmin:.3g}—{vmax:.3g}</text>')
    chunks.append('</svg>')
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(chunks), encoding="utf-8")
