import unittest
from datetime import datetime, timedelta

from build_data_foundation import SamplePoint, build_candidate_segments


START = datetime(2026, 7, 12, 8, 0, 0)


def point(second: int, speed: float, main_speed: float, intermediate: float) -> SamplePoint:
    return SamplePoint(
        timestamp=START + timedelta(seconds=second),
        file_index=0,
        filename="synthetic.dat",
        values={
            "line_speed_actual": speed,
            "entry_thickness_set": 1.5,
            "entry_thickness_actual": 1.5,
            "intermediate_thickness_set": intermediate,
            "exit_thickness_set": 0.75,
            "exit_thickness_actual": 0.75,
            "width_set": 1210.0,
            "main_speed_set": main_speed,
            "f1_gap_average": 1.1,
            "f2_gap_average": 0.72,
            "f1_bending_set": 42.0,
            "f2_bending_set": 33.0,
            "interstand_tension_actual": 6.0,
            "uncoiler_diameter_actual": 1800.0,
            "coiler_diameter_actual": 900.0,
            "machine_started": True,
        },
    )


class HighSpeedLearningTests(unittest.TestCase):
    def test_low_speed_tuning_is_excluded_from_recommendation_targets(self):
        points = [point(i, 20.0, 80.0, 1.8) for i in range(200)]
        points.extend(point(i, 100.0, 320.0, 1.1) for i in range(200, 380))

        rows = build_candidate_segments(points)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["stable_high_speed_seconds_1hz"], 180)
        self.assertEqual(row["low_speed_tuning_seconds_1hz"], 200)
        self.assertEqual(row["learning_eligible"], "true")
        self.assertEqual(row["main_speed_set_median"], 320.0)
        self.assertEqual(row["intermediate_thickness_set_mode"], 1.1)
        self.assertEqual(row["line_speed_actual_median"], 100.0)

    def test_low_speed_only_candidate_cannot_enter_learning_set(self):
        rows = build_candidate_segments([point(i, 30.0, 90.0, 1.7) for i in range(240)])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["stable_high_speed_seconds_1hz"], 0)
        self.assertEqual(rows[0]["learning_eligible"], "false")
        self.assertEqual(rows[0]["main_speed_set_median"], "")


if __name__ == "__main__":
    unittest.main()
