import csv
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from tandem_data_filter import tandem_exclusion_reason, is_tandem_record
import train_compare_high_speed_v6 as v6
import benchmark_physics_v070 as physics


class TandemFilterTests(unittest.TestCase):
    def test_zero_missing_negative_and_positive_are_distinct(self):
        self.assertEqual(tandem_exclusion_reason({'f1_force_actual_median':0,'f2_force_actual_median':200}), 'f2_only_single_stand')
        self.assertEqual(tandem_exclusion_reason({'f2_force_actual_median':200}), 'stand_activity_unknown')
        self.assertEqual(tandem_exclusion_reason({'f1_force_actual_median':'nan','f2_force_actual_median':200}), 'stand_activity_unknown')
        self.assertFalse(is_tandem_record({'f1_force_actual_median':-1,'f2_force_actual_median':200}))
        self.assertTrue(is_tandem_record({'f1_force_actual_median':100,'f2_force_actual_median':200}))

    def test_single_stand_never_contributes_f2_physics_sample(self):
        row=dict(width=1200,current_entry_thickness=2,f1_exit_thickness=1.5,f2_exit_thickness=1,
            f1_force_actual_median=0,f2_force_actual_median=200,interstand_tension_actual_median=2,exit_tension_actual_median=3)
        self.assertIsNone(physics.sample_from_row(row,'f2'))

    def test_zero_force_is_rejected_even_when_plc_middle_gauge_looks_like_tandem(self):
        row=dict(stable_high_speed_seconds_1hz=180,entry_thickness_set_mode=2,
            intermediate_thickness_set_mode=1.5,exit_thickness_set_mode=1,width_set_mode=1200,
            uncoiler_tension_set_median=2,interstand_tension_set_median=3,coiler_tension_set_median=4,
            tm1_gap_command_median=1200,tm2_gap_command_median=700,
            f1_roll_force_sum_median=0,f2_roll_force_sum_median=200,learning_eligible='true',digital_state_usable='true')
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'production_segments.csv'
            with path.open('w') as f:
                writer=csv.DictWriter(f,fieldnames=list(row));writer.writeheader();writer.writerow(row)
                writer.writerow({**row,'f1_roll_force_sum_median':150})
            self.assertEqual(len(v6.load_cases([path])),1)


if __name__ == '__main__':
    unittest.main()
