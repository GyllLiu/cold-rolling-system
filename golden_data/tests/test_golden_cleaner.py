from datetime import datetime, timedelta
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.golden_scorer import calibrate_thresholds, merge_adjacent_gold, score_window
from src.stable_segment import detect_windows


class GoldenCleanerTest(unittest.TestCase):
    def setUp(self):
        self.config = json.loads((ROOT / "config" / "config.yaml").read_text())

    def point(self, second, speed=500.0, force=300.0, alarm=False):
        point = {
            "timestamp": datetime(2026, 7, 1) + timedelta(seconds=second),
            "line_speed_actual": speed,
            "f1_force_actual": force,
            "f2_force_actual": force * 0.8,
            "entry_tension_actual": 5.0,
            "interstand_tension_actual": 7.0,
            "exit_tension_actual": 9.0,
            "entry_tension_set": 5.0,
            "interstand_tension_set": 7.0,
            "exit_tension_set": 9.0,
            "entry_thickness_set": 2.0,
            "entry_thickness_actual": 2.0,
            "intermediate_thickness_set": 1.2,
            "intermediate_thickness_actual": 1.2,
            "exit_thickness_set": 0.7,
            "exit_thickness_actual": 0.7,
            "width_set": 1200.0,
            "f1_gap_set": 1.2,
            "f2_gap_set": 0.7,
            "stand_break": alarm,
            "fire_emergency_stop": False,
            "total_emergency_stop": False,
            "total_quick_stop": False,
            "current_slowdown": False,
            "diameter_slowdown": False,
            "filter_oil_ok": True,
            "thin_oil_ok": True,
            "oil_air_ok": True,
        }
        return point

    def test_adaptive_high_speed_window(self):
        points = [self.point(i, speed=100 + i * 8 if i < 50 else 500) for i in range(140)]
        high, low, meta = detect_windows(points, self.config)
        self.assertGreaterEqual(len(high), 2)
        self.assertGreater(meta["adaptive_threshold"], 300)
        self.assertGreater(len(low), 0)

    def test_alarm_is_abnormal(self):
        points = [self.point(i, alarm=True) for i in range(80)]
        high, _, _ = detect_windows(points, self.config)
        thresholds = calibrate_thresholds(high, self.config)
        scored = score_window(high[0], thresholds, self.config)
        self.assertEqual(scored["golden_level"], "ABNORMAL")
        self.assertIn("hard_alarm_present", scored["abnormal_reason"])

    def test_adjacent_gold_windows_merge_without_losing_duration(self):
        rows = []
        for index, level in enumerate(("GOLD_A", "GOLD_A", "GOLD_B")):
            start = datetime(2026, 7, 1) + timedelta(seconds=index * 30)
            rows.append(
                {
                    "dataset": "260701",
                    "candidate_id": "candidate_001",
                    "stable_segment_start": start.isoformat(),
                    "stable_segment_end": (start + timedelta(seconds=30)).isoformat(),
                    "stable_duration": 30.0,
                    "golden_level": level,
                    "golden_score": 95.0 - index,
                    "speed_median": 500.0 + index,
                    "f1_force_actual_median": 300.0,
                    "f2_force_actual_median": 240.0,
                }
            )
        merged = merge_adjacent_gold(rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["stable_duration"], 90.0)
        self.assertEqual(merged[0]["merged_window_count"], 3)
        self.assertEqual(merged[0]["dataset"], "260701")

    def test_f2_only_window_is_excluded_from_scoring_and_merge(self):
        points = [self.point(second) for second in range(180)]
        high, _, _ = detect_windows(points, self.config)
        thresholds = calibrate_thresholds(high, self.config)
        single = {**high[0], "f1_force_actual_median": 0, "f2_force_actual_median": 240}
        scored = score_window(single, thresholds, self.config)
        self.assertEqual(scored["golden_level"], "EXCLUDED")
        self.assertEqual(scored["tandem_exclusion_reason"], "f2_only_single_stand")
        self.assertEqual(merge_adjacent_gold([{**scored, "golden_level": "GOLD_A"}]), [])


if __name__ == "__main__":
    unittest.main()
