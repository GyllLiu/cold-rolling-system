"""Business aliases and honest DAT field mapping."""

from __future__ import annotations


CHANNELS: dict[str, str] = {
    "entry_thickness_set": "YH.PLC.YH.入口厚度设定",
    "entry_thickness_actual": "YH.PLC.YH.入口实际厚度",
    "intermediate_thickness_set": "YH.PLC.YH.中间厚度设定",
    "intermediate_thickness_actual": "YH.PLC.YH.机架计算厚度",
    "exit_thickness_set": "YH.PLC.YH.出口厚度设定",
    "exit_thickness_actual": "YH.PLC.YH.出口实际厚度",
    "width_set": "YH.PLC.YH.宽度设定",
    "line_speed_actual": "YH.PLC.YH.机架间测速m/min",
    "main_speed_set": "YH.PLC.YH.主速度设定",
    "f1_force_actual": "YH.PLC.YH.F1#轧制力和T",
    "f2_force_actual": "YH.PLC.YH.F2#轧制力和T",
    "entry_tension_set": "YH.PLC.YH.开卷张力设定",
    "entry_tension_actual": "YH.PLC.YH.开卷实际张力",
    "interstand_tension_set": "YH.PLC.YH.机架张力设定",
    "interstand_tension_actual": "YH.PLC.YH.机架实际张力",
    "exit_tension_set": "YH.PLC.YH.卷取张力设定",
    "exit_tension_actual": "YH.PLC.YH.卷取实际张力",
    "f1_gap_set": "YH.PLC.YH.TM1综合设定辊缝um",
    "f2_gap_set": "YH.PLC.YH.TM2综合设定辊缝um",
    "stand_break": "YH.PLC.YH.机架断带",
    "fire_emergency_stop": "YH.PLC.YH.灭火急停",
    "total_emergency_stop": "YH.PLC.YH.总急停",
    "total_quick_stop": "YH.PLC.YH.总快停",
    "current_slowdown": "YH.PLC.YH.电流降速命令",
    "diameter_slowdown": "YH.PLC.YH.卷径降速命令",
    "filter_oil_ok": "YH.PLC.YH.滤油机正常",
    "thin_oil_ok": "YH.PLC.YH.稀油正常",
    "oil_air_ok": "YH.PLC.YH.油气正常",
}

ACTUAL_ALIASES = (
    "line_speed_actual",
    "f1_force_actual",
    "f2_force_actual",
    "entry_tension_actual",
    "interstand_tension_actual",
    "exit_tension_actual",
)

SETPOINT_ALIASES = (
    "entry_thickness_set",
    "intermediate_thickness_set",
    "exit_thickness_set",
    "entry_tension_set",
    "interstand_tension_set",
    "exit_tension_set",
    "f1_gap_set",
    "f2_gap_set",
)

# Only strip break has confirmed active-high semantics.  The three emergency/
# quick-stop points are 1 during known normal high-speed runs, so their PLC
# polarity must be confirmed before they can be used as hard alarms.
HARD_ALARM_ALIASES = ("stand_break",)

UNKNOWN_POLARITY_STATUS_ALIASES = (
    "fire_emergency_stop",
    "total_emergency_stop",
    "total_quick_stop",
)

LUBRICATION_STATUS_ALIASES = ("filter_oil_ok", "thin_oil_ok", "oil_air_ok")


ROLE_BY_ALIAS = {
    **{name: "actual/process feedback" for name in ACTUAL_ALIASES},
    **{name: "setpoint" for name in SETPOINT_ALIASES},
    "entry_thickness_actual": "measurement/process feedback",
    "intermediate_thickness_actual": "PLC calculated feedback",
    "exit_thickness_actual": "measurement/process feedback",
    "width_set": "setpoint/specification",
    "main_speed_set": "setpoint",
    **{name: "alarm/status" for name in HARD_ALARM_ALIASES},
    "current_slowdown": "status/command",
    "diameter_slowdown": "status/command",
    **{name: "lubrication status only" for name in LUBRICATION_STATUS_ALIASES},
}
