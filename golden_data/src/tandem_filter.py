"""Factory V072 scope: train only on complete, active F1+F2 passes.

Zero F1 / positive F2 identifies single-stand rolling in this plant. Keep raw
records, but exclude them from tandem learning; do not fabricate an anneal time.
Missing or nonfinite signals are not equivalent to a measured zero.
"""
import math


def value(row, *keys):
    for key in keys:
        try:
            number = float(row[key])
            if math.isfinite(number):
                return number
        except (KeyError, TypeError, ValueError):
            pass
    return None


def tandem_exclusion_reason(row):
    mode = str(row.get('rolling_mode', '')).strip().lower()
    if mode in {'single', 'single_stand', 'f2_only'}:
        return 'single_stand_out_of_scope'
    if str(row.get('intermediate_annealing', '')).strip().lower() in {'true', '1', 'yes'}:
        return 'intermediate_annealing_out_of_scope'
    f1 = value(row, 'f1_force_actual_median', 'f1_roll_force_sum_median', 'f1_force_t')
    f2 = value(row, 'f2_force_actual_median', 'f2_roll_force_sum_median', 'f2_force_t')
    if f1 == 0 and f2 is not None and f2 > 0:
        return 'f2_only_single_stand'
    if f1 is None or f2 is None:
        return 'stand_activity_unknown'
    if f1 <= 0 or f2 <= 0:
        return 'stand_not_active'
    middle = value(row, 'f1_exit_thickness', 'intermediate_thickness_set_mode', 'middle')
    if middle == 0:
        return 'f1_geometry_zero'
    return None


def is_tandem_record(row):
    return tandem_exclusion_reason(row) is None
