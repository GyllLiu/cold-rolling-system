#!/usr/bin/env python3
"""Audit existing-data inputs for availability and leakage.

Reads the frozen v0.4 segment feature table and the per-day channel
dictionaries.  Classifies every candidate feature as deployable, diagnostic
only, or forbidden for the startup model.  Does not train or change artifacts.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


WORKSPACE_ROOT = Path(__file__).resolve().parents[2]
DEVELOPMENT_DAYS = ("260712", "260713", "260714", "260716", "260717", "260718")
BENCHMARK_DAY = "260719"
DAYS = DEVELOPMENT_DAYS + (BENCHMARK_DAY,)

REQUIRED_DEPLOY = {
    "entry_thickness_set_mode",
    "exit_thickness_set_mode",
    "width_set_mode",
    "total_reduction_ratio",
    "log_thickness_ratio",
    "entry_cross_section_proxy",
    "exit_cross_section_proxy",
}
REQUIRED_DIAGNOSTIC = {
    "entry_thickness_actual_median",
    "uncoiler_diameter_start",
    "coiler_diameter_start",
}

METADATA_COLUMNS = {
    "candidate_id",
    "record_type",
    "is_true_coil_or_pass",
    "start_time",
    "end_time",
    "span_seconds",
    "active_seconds_1hz",
    "stable_high_speed_seconds_1hz",
    "low_speed_tuning_seconds_1hz",
    "high_speed_threshold_m_per_min",
    "learning_eligible",
    "source_files",
    "confidence",
    "digital_state_usable",
    "notes",
    "dataset",
    "extracted_high_speed_samples_1hz",
    "extraction_parsed_files",
    "extraction_channel_error_count",
    "extraction_channel_errors",
}


@dataclass(frozen=True)
class FeatureSpec:
    feature: str
    label: str
    channel: str
    unit: str
    derived: bool
    derived_from: str
    available_before_recommendation: str
    manually_filled: str
    runtime_feedback: str
    decision: str
    reason: str
    in_feature_table: bool = True


FEATURE_SPECS: tuple[FeatureSpec, ...] = (
    FeatureSpec(
        "entry_thickness_set_mode",
        "入口厚度设定",
        "YH.PLC.YH.入口厚度设定",
        "mm",
        False,
        "",
        "是",
        "是",
        "否",
        "允许部署",
        "产品规格，推荐前主操已知；当前无PLC，页面手工填写入口厚度。",
    ),
    FeatureSpec(
        "exit_thickness_set_mode",
        "目标出口厚度设定",
        "YH.PLC.YH.出口厚度设定",
        "mm",
        False,
        "",
        "是",
        "是",
        "否",
        "允许部署",
        "产品规格，推荐前主操已知；当前无PLC，页面手工填写目标出口厚度。",
    ),
    FeatureSpec(
        "width_set_mode",
        "来料宽度设定",
        "YH.PLC.YH.宽度设定",
        "mm",
        False,
        "",
        "是",
        "是",
        "否",
        "允许部署",
        "产品规格，推荐前主操已知；当前无PLC，页面手工填写来料宽度。只作负荷修正，不定义目标厚度。",
    ),
    FeatureSpec(
        "total_reduction_ratio",
        "总压下率",
        "derived:(entry_thickness_set-exit_thickness_set)/entry_thickness_set",
        "1",
        True,
        "entry_thickness_set_mode,exit_thickness_set_mode",
        "是",
        "否",
        "否",
        "允许部署",
        "由入口/出口设定厚度派生，推荐前可计算，不含运行后反馈。",
    ),
    FeatureSpec(
        "log_thickness_ratio",
        "厚度比对数",
        "derived:ln(entry_thickness_set/exit_thickness_set)",
        "1",
        True,
        "entry_thickness_set_mode,exit_thickness_set_mode",
        "是",
        "否",
        "否",
        "允许部署",
        "由入口/出口设定厚度派生。特征表未单独存列，推理时由三项输入计算。",
        in_feature_table=False,
    ),
    FeatureSpec(
        "entry_cross_section_proxy",
        "入口截面积代理",
        "derived:entry_thickness_set*width_set",
        "mm^2",
        True,
        "entry_thickness_set_mode,width_set_mode",
        "是",
        "否",
        "否",
        "允许部署",
        "由入口厚度与宽度派生的负荷代理，推荐前可计算。",
        in_feature_table=False,
    ),
    FeatureSpec(
        "exit_cross_section_proxy",
        "出口截面积代理",
        "derived:exit_thickness_set*width_set",
        "mm^2",
        True,
        "exit_thickness_set_mode,width_set_mode",
        "是",
        "否",
        "否",
        "允许部署",
        "由出口厚度与宽度派生的负荷代理，推荐前可计算。",
        in_feature_table=False,
    ),
    FeatureSpec(
        "entry_thickness_actual_median",
        "入口实际厚度（高速段中位数）",
        "YH.PLC.YH.入口实际厚度",
        "mm",
        False,
        "",
        "否",
        "否",
        "是",
        "仅诊断",
        "存值是高速稳定段中位数，属于运行后测厚反馈。当前无PLC，不能自动传入；默认不部署。",
    ),
    FeatureSpec(
        "uncoiler_diameter_start",
        "开卷机起始卷径",
        "YH.PLC.YH.开卷机实际卷径",
        "mm",
        False,
        "",
        "部分",
        "否",
        "部分",
        "仅诊断",
        "上卷时HMI可见，但当前页面只允许3项输入且无PLC读取。默认不部署，仅离线诊断。",
    ),
    FeatureSpec(
        "coiler_diameter_start",
        "卷取机起始卷径",
        "YH.PLC.YH.卷取实际卷径",
        "mm",
        False,
        "",
        "部分",
        "否",
        "部分",
        "仅诊断",
        "上卷时空卷径通常接近套筒，但当前页面不允许填写且无PLC读取。默认不部署。",
    ),
    FeatureSpec(
        "intermediate_thickness_set_mode",
        "中间厚度设定（输出目标）",
        "YH.PLC.YH.中间厚度设定",
        "mm",
        False,
        "",
        "否",
        "是",
        "否",
        "禁止",
        "这是启动模型输出，不能再作为输入，否则泄漏。",
    ),
    FeatureSpec(
        "uncoiler_tension_set_median",
        "开卷张力设定（输出目标）",
        "YH.PLC.YH.开卷张力设定",
        "T",
        False,
        "",
        "否",
        "是",
        "否",
        "禁止",
        "这是启动模型输出，不能再作为输入。",
    ),
    FeatureSpec(
        "interstand_tension_set_median",
        "机架张力设定（输出目标）",
        "YH.PLC.YH.机架张力设定",
        "T",
        False,
        "",
        "否",
        "是",
        "否",
        "禁止",
        "这是启动模型输出，不能再作为输入。",
    ),
    FeatureSpec(
        "coiler_tension_set_median",
        "卷取张力设定（输出目标）",
        "YH.PLC.YH.卷取张力设定",
        "T",
        False,
        "",
        "否",
        "是",
        "否",
        "禁止",
        "这是启动模型输出，不能再作为输入。",
    ),
    FeatureSpec(
        "tm1_closed_gap_set_median",
        "F1高速稳定运行辊缝（输出目标）",
        "YH.PLC.YH.TM1闭辊缝位置设定",
        "mm",
        False,
        "",
        "否",
        "是",
        "否",
        "禁止",
        "这是输出目标，学到的是高速稳定运行辊缝，不能当作输入，也不能当作上卷安全首设。",
    ),
    FeatureSpec(
        "tm2_closed_gap_set_median",
        "F2高速稳定运行辊缝（输出目标）",
        "YH.PLC.YH.TM2闭辊缝位置设定",
        "mm",
        False,
        "",
        "否",
        "是",
        "否",
        "禁止",
        "这是输出目标，学到的是高速稳定运行辊缝，不能当作输入，也不能当作上卷安全首设。",
    ),
    FeatureSpec(
        "f1_reduction_ratio",
        "F1压下率",
        "derived:(entry-intermediate)/entry",
        "1",
        True,
        "entry_thickness_set_mode,intermediate_thickness_set_mode",
        "否",
        "否",
        "否",
        "禁止",
        "依赖中间厚度输出，用作输入会泄漏压下分配。只可作合理性检查。",
    ),
    FeatureSpec(
        "f2_reduction_ratio",
        "F2压下率",
        "derived:(intermediate-exit)/intermediate",
        "1",
        True,
        "intermediate_thickness_set_mode,exit_thickness_set_mode",
        "否",
        "否",
        "否",
        "禁止",
        "依赖中间厚度输出，用作输入会泄漏压下分配。只可作合理性检查。",
    ),
    FeatureSpec(
        "main_speed_set_median",
        "主速度设定",
        "YH.PLC.YH.主速度设定",
        "m/min",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "速度类信号。高速稳定段中位数是运行后状态，禁止进入启动模型。",
    ),
    FeatureSpec(
        "line_speed_actual_median",
        "机架间实际测速",
        "YH.PLC.YH.机架间测速m/min",
        "m/min",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "运行后实际速度，禁止进入启动模型。",
    ),
    FeatureSpec(
        "line_speed_actual_max_abs",
        "机架间实际测速最大绝对值",
        "YH.PLC.YH.机架间测速m/min",
        "m/min",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "运行后实际速度极值，禁止进入启动模型。",
    ),
    FeatureSpec(
        "exit_thickness_actual_median",
        "出口实际厚度",
        "YH.PLC.YH.出口实际厚度",
        "mm",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "出口测厚是跑带后反馈，用作输入会直接泄漏成品厚度。",
    ),
    FeatureSpec(
        "exit_thickness_error_median",
        "出口厚差中位数",
        "derived:exit_thickness_actual-exit_thickness_set",
        "mm",
        True,
        "exit_thickness_actual_median,exit_thickness_set_mode",
        "否",
        "否",
        "是",
        "禁止",
        "由出口实际厚度派生的运行后质量反馈。",
    ),
    FeatureSpec(
        "exit_thickness_error_abs_p95",
        "出口厚差绝对P95",
        "derived:abs(exit_thickness_actual-exit_thickness_set)",
        "mm",
        True,
        "exit_thickness_actual_median,exit_thickness_set_mode",
        "否",
        "否",
        "是",
        "禁止",
        "由出口实际厚度派生的运行后质量反馈。",
    ),
    FeatureSpec(
        "f1_roll_force_sum_median",
        "F1轧制力和",
        "YH.PLC.YH.F1#轧制力和T",
        "T",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "实际轧制力，运行后才可得。",
    ),
    FeatureSpec(
        "f2_roll_force_sum_median",
        "F2轧制力和",
        "YH.PLC.YH.F2#轧制力和T",
        "T",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "实际轧制力，运行后才可得。",
    ),
    FeatureSpec(
        "f1_force_operation_median",
        "F1操作侧轧制力",
        "YH.PLC.YH.F1#操作侧轧制力T",
        "T",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "实际轧制力，运行后才可得。",
    ),
    FeatureSpec(
        "f1_force_drive_median",
        "F1传动侧轧制力",
        "YH.PLC.YH.F1#传动侧轧制力T",
        "T",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "实际轧制力，运行后才可得。",
    ),
    FeatureSpec(
        "f1_force_difference_plc_median",
        "F1轧制力差",
        "YH.PLC.YH.F1#轧制力差T",
        "T",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "实际轧制力差，运行后才可得。",
    ),
    FeatureSpec(
        "f2_force_operation_median",
        "F2操作侧轧制力",
        "YH.PLC.YH.F2#操作侧轧制力T",
        "T",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "实际轧制力，运行后才可得。",
    ),
    FeatureSpec(
        "f2_force_drive_median",
        "F2传动侧轧制力",
        "YH.PLC.YH.F2#传动侧轧制力T",
        "T",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "实际轧制力，运行后才可得。",
    ),
    FeatureSpec(
        "f2_force_difference_plc_median",
        "F2轧制力差",
        "YH.PLC.YH.F2#轧制力差T",
        "T",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "实际轧制力差，运行后才可得。",
    ),
    FeatureSpec(
        "f1_force_drive_minus_operation_median",
        "F1传动侧减操作侧轧制力",
        "derived:f1_force_drive-f1_force_operation",
        "T",
        True,
        "f1_force_drive_median,f1_force_operation_median",
        "否",
        "否",
        "是",
        "禁止",
        "由实际轧制力派生。",
    ),
    FeatureSpec(
        "f2_force_drive_minus_operation_median",
        "F2传动侧减操作侧轧制力",
        "derived:f2_force_drive-f2_force_operation",
        "T",
        True,
        "f2_force_drive_median,f2_force_operation_median",
        "否",
        "否",
        "是",
        "禁止",
        "由实际轧制力派生。",
    ),
    FeatureSpec(
        "f1_gap_average_median",
        "F1平均实际辊缝",
        "YH.PLC.YH.F1#平均辊缝mm",
        "mm",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "实际辊缝反馈，运行后才可得。",
    ),
    FeatureSpec(
        "f2_gap_average_median",
        "F2平均实际辊缝",
        "YH.PLC.YH.F2#平均辊缝mm",
        "mm",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "实际辊缝反馈，运行后才可得。",
    ),
    FeatureSpec(
        "f1_gap_operation_median",
        "F1操作侧实际辊缝",
        "YH.PLC.YH.F1#操作侧辊缝mm",
        "mm",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "实际辊缝反馈，运行后才可得。",
    ),
    FeatureSpec(
        "f1_gap_drive_median",
        "F1传动侧实际辊缝",
        "YH.PLC.YH.F1#传动侧辊缝mm",
        "mm",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "实际辊缝反馈，运行后才可得。",
    ),
    FeatureSpec(
        "f1_gap_difference_plc_median",
        "F1辊缝差",
        "YH.PLC.YH.F1#辊缝差mm",
        "mm",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "实际辊缝差，运行后才可得。",
    ),
    FeatureSpec(
        "f2_gap_operation_median",
        "F2操作侧实际辊缝",
        "YH.PLC.YH.F2#操作侧辊缝mm",
        "mm",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "实际辊缝反馈，运行后才可得。",
    ),
    FeatureSpec(
        "f2_gap_drive_median",
        "F2传动侧实际辊缝",
        "YH.PLC.YH.F2#传动侧辊缝mm",
        "mm",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "实际辊缝反馈，运行后才可得。",
    ),
    FeatureSpec(
        "f2_gap_difference_plc_median",
        "F2辊缝差",
        "YH.PLC.YH.F2#辊缝差mm",
        "mm",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "实际辊缝差，运行后才可得。",
    ),
    FeatureSpec(
        "f1_gap_drive_minus_operation_median",
        "F1传动侧减操作侧辊缝",
        "derived:f1_gap_drive-f1_gap_operation",
        "mm",
        True,
        "f1_gap_drive_median,f1_gap_operation_median",
        "否",
        "否",
        "是",
        "禁止",
        "由实际辊缝派生。",
    ),
    FeatureSpec(
        "f2_gap_drive_minus_operation_median",
        "F2传动侧减操作侧辊缝",
        "derived:f2_gap_drive-f2_gap_operation",
        "mm",
        True,
        "f2_gap_drive_median,f2_gap_operation_median",
        "否",
        "否",
        "是",
        "禁止",
        "由实际辊缝派生。",
    ),
    FeatureSpec(
        "tm1_gap_command_median",
        "TM1综合设定辊缝",
        "YH.PLC.YH.TM1综合设定辊缝um",
        "um",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "PLC高速运行计算量，不是主操开机手输值。",
    ),
    FeatureSpec(
        "tm2_gap_command_median",
        "TM2综合设定辊缝",
        "YH.PLC.YH.TM2综合设定辊缝um",
        "um",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "PLC高速运行计算量，不是主操开机手输值。",
    ),
    FeatureSpec(
        "f1_bending_set_median",
        "F1弯辊力设定",
        "YH.PLC.YH.F1弯辊力设定",
        "T",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "弯辊属于运行板形调节，禁止进入启动模型。",
    ),
    FeatureSpec(
        "f2_bending_set_median",
        "F2弯辊力设定",
        "YH.PLC.YH.F2弯辊力设定",
        "T",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "弯辊属于运行板形调节，禁止进入启动模型。",
    ),
    FeatureSpec(
        "f1_positive_bending_actual_median",
        "F1实际正弯辊",
        "YH.PLC.YH.F1#正弯辊力T",
        "T",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "弯辊实际反馈，运行后才可得。",
    ),
    FeatureSpec(
        "f1_negative_bending_actual_median",
        "F1实际负弯辊",
        "YH.PLC.YH.F1#负弯辊力T",
        "T",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "弯辊实际反馈，运行后才可得。",
    ),
    FeatureSpec(
        "f2_positive_bending_actual_median",
        "F2实际正弯辊",
        "YH.PLC.YH.F2#正弯辊力T",
        "T",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "弯辊实际反馈，运行后才可得。",
    ),
    FeatureSpec(
        "f2_negative_bending_actual_median",
        "F2实际负弯辊",
        "YH.PLC.YH.F2#负弯辊力T",
        "T",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "弯辊实际反馈，运行后才可得。",
    ),
    FeatureSpec(
        "tm1_tilt_set_median",
        "F1倾斜量设定",
        "YH.PLC.YH.TM1倾斜量设定um",
        "um",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "倾斜属于运行板形调节，禁止进入启动模型。",
    ),
    FeatureSpec(
        "tm2_tilt_set_median",
        "F2倾斜量设定",
        "YH.PLC.YH.TM2倾斜量设定um",
        "um",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "倾斜属于运行板形调节，禁止进入启动模型。",
    ),
    FeatureSpec(
        "tm1_tilt_adjustment_median",
        "F1倾斜调节量",
        "YH.PLC.YH.1#机架倾斜调节量",
        "",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "倾斜调节反馈，运行后才可得。",
    ),
    FeatureSpec(
        "tm2_tilt_adjustment_median",
        "F2倾斜调节量",
        "YH.PLC.YH.2#机架倾斜调节量",
        "",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "倾斜调节反馈，运行后才可得。",
    ),
    FeatureSpec(
        "uncoiler_diameter_end",
        "开卷机结束卷径",
        "YH.PLC.YH.开卷机实际卷径",
        "mm",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "段末卷径，卷已轧完才知道。",
    ),
    FeatureSpec(
        "coiler_diameter_end",
        "卷取机结束卷径",
        "YH.PLC.YH.卷取实际卷径",
        "mm",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "段末卷径，卷已轧完才知道。",
    ),
    FeatureSpec(
        "flatness_std_median",
        "49区版型横向离散",
        "YH.PLC.AFC版型偏差.版型偏差#1-49",
        "DAT原始值",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "49区版型是运行后反馈，工程单位未确认，禁止进入启动模型，也不给动作。",
    ),
    FeatureSpec(
        "flatness_lr_asymmetry_median",
        "49区版型左右不对称",
        "YH.PLC.AFC版型偏差.版型偏差#1-49",
        "DAT原始值",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "49区版型运行后反馈，禁止进入启动模型。",
    ),
    FeatureSpec(
        "flatness_edge_minus_center_median",
        "49区版型边中差",
        "YH.PLC.AFC版型偏差.版型偏差#1-49",
        "DAT原始值",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "49区版型运行后反馈，禁止进入启动模型。",
    ),
    FeatureSpec(
        "flatness_temporal_std_median",
        "49区版型时序波动",
        "YH.PLC.AFC版型偏差.版型偏差#1-49",
        "DAT原始值",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "49区版型运行后反馈，禁止进入启动模型。",
    ),
    FeatureSpec(
        "current_family",
        "电流族（字典存在，特征表未提取）",
        "YH.PLC.YH.*电流",
        "",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "通道字典中的主机/开卷/卷取/伺服阀电流均为运行后反馈，禁止进入启动模型。",
        in_feature_table=False,
    ),
    FeatureSpec(
        "torque_family",
        "转矩族（字典存在，特征表未提取）",
        "YH.PLC.YH.*转矩",
        "",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "通道字典中的主机/开卷/卷取/偏转辊转矩均为运行后反馈，禁止进入启动模型。",
        in_feature_table=False,
    ),
)


def load_feature_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows:
        raise ValueError(f"empty feature table: {path}")
    return rows


def missing_stats(rows: list[dict[str, str]], column: str) -> tuple[int, int, float]:
    if column not in rows[0]:
        return 0, 0, math.nan
    missing = 0
    for row in rows:
        raw = (row.get(column) or "").strip()
        if raw == "":
            missing += 1
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        if not math.isfinite(value):
            missing += 1
    return missing, len(rows), missing / len(rows)


def derived_missing(rows: list[dict[str, str]], spec: FeatureSpec) -> tuple[int, int, float]:
    if spec.feature == "log_thickness_ratio":
        missing = 0
        for row in rows:
            try:
                entry = float(row["entry_thickness_set_mode"])
                exit_value = float(row["exit_thickness_set_mode"])
                if entry <= 0 or exit_value <= 0 or not math.isfinite(math.log(entry / exit_value)):
                    missing += 1
            except (KeyError, TypeError, ValueError):
                missing += 1
        return missing, len(rows), missing / len(rows)
    if spec.feature in {"entry_cross_section_proxy", "exit_cross_section_proxy"}:
        thickness_col = (
            "entry_thickness_set_mode"
            if spec.feature.startswith("entry_")
            else "exit_thickness_set_mode"
        )
        missing = 0
        for row in rows:
            try:
                thickness = float(row[thickness_col])
                width = float(row["width_set_mode"])
                if thickness <= 0 or width <= 0 or not math.isfinite(thickness * width):
                    missing += 1
            except (KeyError, TypeError, ValueError):
                missing += 1
        return missing, len(rows), missing / len(rows)
    if spec.in_feature_table:
        return missing_stats(rows, spec.feature)
    return 0, len(rows), math.nan


def load_channel_dictionaries(workspace: Path) -> tuple[dict[str, dict[str, Any]], dict[str, int]]:
    by_name: dict[str, dict[str, Any]] = {}
    family_counts = {"current": 0, "torque": 0, "flatness": 0, "all": 0}
    seen_current: set[str] = set()
    seen_torque: set[str] = set()
    seen_flatness: set[str] = set()
    for day in DAYS:
        path = workspace / f"outputs/data_foundation_{day}/channel_dictionary.csv"
        if not path.exists():
            raise FileNotFoundError(path)
        with path.open(encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                name = row["channel_name"]
                info = by_name.setdefault(
                    name,
                    {
                        "channel_name": name,
                        "unit_hint": row.get("unit_hint", ""),
                        "role": row.get("role", ""),
                        "category": row.get("category", ""),
                        "channel_type": row.get("channel_type", ""),
                        "days": [],
                        "file_coverage_by_day": {},
                    },
                )
                info["days"].append(day)
                info["file_coverage_by_day"][day] = row.get("file_coverage_fraction", "")
                if "电流" in name:
                    seen_current.add(name)
                if "转矩" in name:
                    seen_torque.add(name)
                if "版型偏差" in name:
                    seen_flatness.add(name)
    family_counts["current"] = len(seen_current)
    family_counts["torque"] = len(seen_torque)
    family_counts["flatness"] = len(seen_flatness)
    family_counts["all"] = len(by_name)
    return by_name, family_counts


def lookup_unit(spec: FeatureSpec, dictionary: dict[str, dict[str, Any]]) -> tuple[str, str]:
    if spec.channel.startswith("derived:"):
        return spec.unit, "derived"
    if spec.feature in {"current_family", "torque_family"}:
        return spec.unit or "未标注", "channel_family"
    raw_name = spec.channel
    if raw_name.endswith("#1-49"):
        sample = dictionary.get("YH.PLC.AFC版型偏差.版型偏差#1", {})
        hint = sample.get("unit_hint") or "DAT原始值（元数据为空）"
        return hint, "channel_dictionary"
    info = dictionary.get(raw_name, {})
    hint = (info.get("unit_hint") or "").strip()
    if hint:
        return hint, "channel_dictionary"
    if spec.unit:
        return spec.unit, "hmi_or_model_convention"
    return "未标注", "missing"


def classify_unlisted(column: str) -> FeatureSpec | None:
    if column in METADATA_COLUMNS:
        return None
    if column.startswith("flatness_"):
        return FeatureSpec(
            column,
            f"49区版型派生:{column}",
            "YH.PLC.AFC版型偏差.版型偏差#1-49",
            "DAT原始值",
            True,
            "flatness_zones_1_49",
            "否",
            "否",
            "是",
            "禁止",
            "49区版型或其他运行后版型派生量，禁止进入启动模型。",
        )
    return FeatureSpec(
        column,
        column,
        "unmapped",
        "",
        False,
        "",
        "否",
        "否",
        "是",
        "禁止",
        "特征表中未预先分类的过程量，默认禁止进入启动模型，避免泄漏。",
    )


def build_rows(
    features: list[dict[str, str]],
    dictionary: dict[str, dict[str, Any]],
    family_counts: dict[str, int],
) -> list[dict[str, Any]]:
    listed = {spec.feature: spec for spec in FEATURE_SPECS}
    output: list[dict[str, Any]] = []
    for spec in FEATURE_SPECS:
        missing, count, rate = derived_missing(features, spec)
        unit, unit_source = lookup_unit(spec, dictionary)
        raw_channel = spec.channel.split("#1-49")[0] + "#1" if spec.channel.endswith("#1-49") else spec.channel
        info = dictionary.get(raw_channel, {})
        extra = ""
        if spec.feature == "current_family":
            extra = f"字典电流通道{family_counts['current']}个"
        elif spec.feature == "torque_family":
            extra = f"字典转矩通道{family_counts['torque']}个"
        elif spec.channel.endswith("#1-49"):
            extra = f"字典版型通道{family_counts['flatness']}个"
        output.append(
            {
                "feature": spec.feature,
                "label": spec.label,
                "channel": spec.channel,
                "unit": unit,
                "unit_source": unit_source,
                "missing_count": "" if math.isnan(rate) else missing,
                "evaluated_row_count": "" if math.isnan(rate) else count,
                "missing_rate": "" if math.isnan(rate) else f"{rate:.6f}",
                "missing_rate_percent": "" if math.isnan(rate) else f"{rate * 100:.3f}%",
                "available_before_recommendation": spec.available_before_recommendation,
                "manually_filled": spec.manually_filled,
                "runtime_feedback": spec.runtime_feedback,
                "decision": spec.decision,
                "reason": spec.reason if not extra else f"{spec.reason} {extra}",
                "derived": "是" if spec.derived else "否",
                "derived_from": spec.derived_from,
                "in_feature_table": "是" if spec.in_feature_table else "否",
                "dictionary_role": info.get("role", ""),
                "dictionary_category": info.get("category", ""),
                "dictionary_day_coverage": len(info.get("days", [])),
            }
        )

    listed_names = set(listed)
    leftover = []
    for column in features[0]:
        if column in listed_names or column in METADATA_COLUMNS:
            continue
        leftover.append(column)
    for column in leftover:
        spec = classify_unlisted(column)
        if spec is None:
            continue
        missing, count, rate = missing_stats(features, column)
        unit, unit_source = lookup_unit(spec, dictionary)
        output.append(
            {
                "feature": spec.feature,
                "label": spec.label,
                "channel": spec.channel,
                "unit": unit,
                "unit_source": unit_source,
                "missing_count": missing,
                "evaluated_row_count": count,
                "missing_rate": f"{rate:.6f}",
                "missing_rate_percent": f"{rate * 100:.3f}%",
                "available_before_recommendation": spec.available_before_recommendation,
                "manually_filled": spec.manually_filled,
                "runtime_feedback": spec.runtime_feedback,
                "decision": spec.decision,
                "reason": spec.reason,
                "derived": "是" if spec.derived else "否",
                "derived_from": spec.derived_from,
                "in_feature_table": "是",
                "dictionary_role": "",
                "dictionary_category": "",
                "dictionary_day_coverage": 0,
            }
        )
    return output


def decision_sets(rows: list[dict[str, Any]]) -> dict[str, set[str]]:
    grouped: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        grouped[row["decision"]].add(row["feature"])
    return grouped


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "feature",
        "label",
        "channel",
        "unit",
        "unit_source",
        "missing_count",
        "evaluated_row_count",
        "missing_rate",
        "missing_rate_percent",
        "available_before_recommendation",
        "manually_filled",
        "runtime_feedback",
        "decision",
        "reason",
        "derived",
        "derived_from",
        "in_feature_table",
        "dictionary_role",
        "dictionary_category",
        "dictionary_day_coverage",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_report(
    path: Path,
    *,
    rows: list[dict[str, Any]],
    feature_count: int,
    family_counts: dict[str, int],
    grouped: dict[str, set[str]],
    passed: bool,
    issues: list[str],
) -> None:
    def section(decision: str) -> list[dict[str, Any]]:
        return [row for row in rows if row["decision"] == decision]

    lines = [
        "# 现有数据输入可用性与泄漏审计",
        "",
        f"生成时间：{datetime.now(timezone.utc).isoformat()}",
        "数据：`outputs/model_v4/segment_features.csv` + `outputs/data_foundation_260*/channel_dictionary.csv`。",
        "260719 是日期隔离复用基准，不是全新盲测。本脚本不训练模型。",
        "",
        f"高速稳定段 `{feature_count}`；开发期 260712–260718，260719 仅作复用基准。",
        f"通道字典合计 `{family_counts['all']}` 个通道；电流 `{family_counts['current']}`，转矩 `{family_counts['torque']}`，版型 `{family_counts['flatness']}`。",
        "",
        f"审计结论：{'通过' if passed else '未通过'}",
        "",
        "## 分类规则",
        "",
        "- 允许部署：推荐前主操已知，或仅由这三项规格派生；当前页面可填写或可计算。",
        "- 仅诊断：可能有工艺含义，但当前无PLC、页面不能填，或取自运行中位数，默认不部署。",
        "- 禁止：速度、电流、转矩、实际轧制力、实际辊缝、弯辊、倾斜、出口实际厚度、49区版型、输出目标或其他运行后信号。",
        "",
        "## 允许部署",
        "",
        "| 特征 | 通道/公式 | 单位 | 缺失率 | 推荐前可得 | 人工填写 | 运行后反馈 | 原因 |",
        "| --- | --- | --- | ---: | --- | --- | --- | --- |",
    ]
    for row in section("允许部署"):
        lines.append(
            f"| {row['label']} | `{row['channel']}` | {row['unit']} | "
            f"{row['missing_rate_percent']} | {row['available_before_recommendation']} | "
            f"{row['manually_filled']} | {row['runtime_feedback']} | {row['reason']} |"
        )
    lines.extend(
        [
            "",
            "## 仅诊断，默认不部署",
            "",
            "| 特征 | 通道 | 单位 | 缺失率 | 推荐前可得 | 原因 |",
            "| --- | --- | --- | ---: | --- | --- |",
        ]
    )
    for row in section("仅诊断"):
        lines.append(
            f"| {row['label']} | `{row['channel']}` | {row['unit']} | "
            f"{row['missing_rate_percent']} | {row['available_before_recommendation']} | {row['reason']} |"
        )
    forbidden = section("禁止")
    lines.extend(
        [
            "",
            f"## 禁止进入启动模型（{len(forbidden)} 项）",
            "",
            "按泄漏类型汇总：",
            "",
        ]
    )
    buckets = [
        ("输出目标", ("intermediate_thickness_set_mode", "tm1_closed_gap_set_median", "tm2_closed_gap_set_median", "uncoiler_tension_set_median", "interstand_tension_set_median", "coiler_tension_set_median")),
        ("由输出派生", ("f1_reduction_ratio", "f2_reduction_ratio")),
        ("速度", ("main_speed_set_median", "line_speed_actual_median", "line_speed_actual_max_abs")),
        ("出口实际厚度/厚差", ("exit_thickness_actual_median", "exit_thickness_error_median", "exit_thickness_error_abs_p95")),
        ("实际轧制力", tuple(row["feature"] for row in forbidden if "force" in row["feature"] or "轧制力" in row["label"])),
        ("实际辊缝/综合辊缝", tuple(row["feature"] for row in forbidden if "gap" in row["feature"])),
        ("弯辊/倾斜", tuple(row["feature"] for row in forbidden if "bend" in row["feature"] or "tilt" in row["feature"])),
        ("结束卷径", ("uncoiler_diameter_end", "coiler_diameter_end")),
        ("49区版型", tuple(row["feature"] for row in forbidden if row["feature"].startswith("flatness_"))),
        ("电流/转矩", ("current_family", "torque_family")),
    ]
    used: set[str] = set()
    for title, names in buckets:
        present = [name for name in names if name in grouped.get("禁止", set())]
        if not present:
            continue
        used.update(present)
        lines.append(f"- **{title}**：{', '.join(present)}")
    leftover = sorted(grouped.get("禁止", set()) - used)
    if leftover:
        lines.append(f"- **其他运行后信号**：{', '.join(leftover)}")

    missing_nonzero = [
        row
        for row in rows
        if row["missing_rate"] not in {"", "0.000000"} and row["in_feature_table"] == "是"
    ]
    lines.extend(["", "## 缺失率", ""])
    if missing_nonzero:
        for row in missing_nonzero:
            lines.append(
                f"- {row['label']}（`{row['feature']}`）：{row['missing_count']}/{row['evaluated_row_count']} = {row['missing_rate_percent']}"
            )
    else:
        lines.append("特征表中被审计且存在的列，除版型外缺失率为 0。")

    lines.extend(
        [
            "",
            "## 对后续步骤的约束",
            "",
            "1. Step 3 消融只允许在「允许部署」集合内选正式输入；诊断三项只能做对照。",
            "2. 线上推理不得使用速度、电流、转矩、实际轧制力、实际辊缝、弯辊、倾斜、出口实际厚度、版型或历史近邻。",
            "3. 当前无PLC：入口实际厚度不能声明为自动传入。",
            "4. F1/F2闭辊缝是输出，不是输入；语义是高速稳定运行辊缝参考，不是上卷安全首设。",
            "",
        ]
    )
    if issues:
        lines.extend(["## 未通过项", ""])
        for issue in issues:
            lines.append(f"- {issue}")
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def validate(grouped: dict[str, set[str]]) -> list[str]:
    issues = []
    deploy = grouped.get("允许部署", set())
    diagnostic = grouped.get("仅诊断", set())
    if deploy != REQUIRED_DEPLOY:
        issues.append(
            f"允许部署集合不符：expected={sorted(REQUIRED_DEPLOY)} actual={sorted(deploy)}"
        )
    if diagnostic != REQUIRED_DIAGNOSTIC:
        issues.append(
            f"仅诊断集合不符：expected={sorted(REQUIRED_DIAGNOSTIC)} actual={sorted(diagnostic)}"
        )
    forbidden = grouped.get("禁止", set())
    required_forbidden_substrings = {
        "main_speed_set_median",
        "line_speed_actual_median",
        "f1_roll_force_sum_median",
        "f2_roll_force_sum_median",
        "f1_gap_average_median",
        "f2_gap_average_median",
        "f1_bending_set_median",
        "tm1_tilt_set_median",
        "exit_thickness_actual_median",
        "flatness_std_median",
        "current_family",
        "torque_family",
        "intermediate_thickness_set_mode",
        "tm1_closed_gap_set_median",
    }
    missing = required_forbidden_substrings - forbidden
    if missing:
        issues.append(f"禁止集合缺少必要项：{sorted(missing)}")
    overlap = (REQUIRED_DEPLOY & forbidden) | (REQUIRED_DIAGNOSTIC & forbidden) | (REQUIRED_DEPLOY & diagnostic)
    if overlap:
        issues.append(f"分类重叠：{sorted(overlap)}")
    return issues


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--features",
        type=Path,
        default=WORKSPACE_ROOT / "outputs/model_v4/segment_features.csv",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=WORKSPACE_ROOT / "outputs/v5_existing_data",
    )
    args = parser.parse_args()
    if not args.features.exists():
        parser.error(f"missing feature table: {args.features}")

    features = load_feature_rows(args.features)
    dictionary, family_counts = load_channel_dictionaries(WORKSPACE_ROOT)
    rows = build_rows(features, dictionary, family_counts)
    grouped = decision_sets(rows)
    issues = validate(grouped)
    passed = not issues

    args.output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.output_dir / "input_availability.csv"
    report_path = args.output_dir / "input_availability_report.md"
    summary_path = args.output_dir / "input_availability_summary.json"
    write_csv(csv_path, rows)
    write_report(
        report_path,
        rows=rows,
        feature_count=len(features),
        family_counts=family_counts,
        grouped=grouped,
        passed=passed,
        issues=issues,
    )
    summary_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "feature_row_count": len(features),
                "audited_feature_count": len(rows),
                "deploy": sorted(grouped.get("允许部署", set())),
                "diagnostic": sorted(grouped.get("仅诊断", set())),
                "forbidden_count": len(grouped.get("禁止", set())),
                "passed": passed,
                "issues": issues,
                "channel_dictionary_counts": family_counts,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"rows={len(features)} audited={len(rows)}")
    print(f"deploy={sorted(grouped.get('允许部署', set()))}")
    print(f"diagnostic={sorted(grouped.get('仅诊断', set()))}")
    print(f"forbidden={len(grouped.get('禁止', set()))}")
    print(f"wrote {csv_path}")
    print(f"wrote {report_path}")
    print("INPUT_AUDIT_PASSED" if passed else "INPUT_AUDIT_FAILED")
    for issue in issues:
        print(f"ISSUE {issue}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
