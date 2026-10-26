"""Checks protecting offline validation from target leakage and unit mistakes."""
import copy
import sys
import unittest
from pathlib import Path

try:
    import numpy as np
    import sklearn  # noqa: F401
except ImportError:
    raise unittest.SkipTest("Install scripts/requirements-training.txt in the offline training environment")

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import train_parameter_experiment_v073 as e
import train_capture_pilot_v073 as pilot


def case(index=0, day="260703", time="2026-07-03 12:00:00"):
    h0, h2, width = 3 + index * .03, 1 + index * .01, 1200 + index
    values = {e.ENTRY: h0, e.EXIT: h2, e.WIDTH: width, e.MIDDLE: (h0 + h2) / 2}
    for j, target in enumerate(e.TARGETS[1:]):
        values[target] = 2 + j + index * .02
    return e.v6.core.Case(day, f"{day}_{index}", time, "source.dat", values)


class ExperimentTests(unittest.TestCase):
    def test_middle_prediction_never_reads_middle_truth(self):
        rows = [case(i) for i in range(25)]
        cfg = dict(kind="ridge", features="geometry", degree=1, alpha=10., transform="share")
        model = e.fit(rows, e.MIDDLE, cfg)
        changed = copy.deepcopy(rows)
        for row in changed:
            row.values[e.MIDDLE] = 900
        np.testing.assert_allclose(e.predict(model, rows), e.predict(model, changed))

    def test_route_inputs_ignore_measurements_and_ocr(self):
        a = case()
        b = copy.deepcopy(a)
        for key in e.TARGETS[1:]:
            b.values[key] = 999999
        b.values["alloy"] = "unverified OCR"
        self.assertEqual(e.feature_values(a.values, 2), e.feature_values(b.values, 2))

    def test_weighted_baseline_ensemble_is_not_evaluated_as_one_head(self):
        def constant(value):
            return dict(feature_names=[], feature_mean=[], feature_scale=[], basis_terms=[], coefficients=[value])
        model = {"model_type": "weighted_ensemble", "components": [
            {"weight": .6, "postprocess": "raw", "model": constant(4)},
            {"weight": .4, "postprocess": "add_exit_thickness", "model": constant(1)}]}
        self.assertAlmostEqual(e.predict_artifact_head(model, {e.EXIT: 2}), 3.6)

    def test_unit_tension_projection_does_not_sort_total_tonnes(self):
        # Decreasing tonnes can represent increasing stress as thickness drops.
        row = case()
        tonnes = np.array([[6., 5., 4.]])
        projected = e.project_tensions(tonnes, [row], [2.], [100, 100, 100])
        np.testing.assert_allclose(projected, tonnes)
        projected = e.project_tensions(np.array([[20., 5., 1.]]), [row], [2.], [100, 100, 100])
        self.assertTrue(np.all(np.diff(projected[0] / [3., 2., 1.]) >= -1e-12))

    def test_geometry_target_normalization_is_reversible(self):
        row = case()
        for target in e.TARGETS[1:]:
            scale, offset = e.target_affine([row], target, "geometry_normalized", [2.])
            encoded = (row.values[target] - offset) / scale
            self.assertAlmostEqual(float((encoded * scale + offset)[0]), row.values[target])

    def test_no_future_rows_or_close_boundary_in_training_fold(self):
        rows = [case(i) for i in range(31)]
        rows += [case(90, "260723", "2026-07-23 23:30:00"),
                 case(91, "260724", "2026-07-24 00:30:00"),
                 case(92, "260725", "2026-07-25 12:00:00")]
        day, train, validation = next(e.rolling_folds(rows))
        self.assertEqual(day, "260724")
        self.assertEqual(len(train), 31)
        self.assertTrue(all(row.dataset < day for row in train))
        self.assertTrue(all(row.dataset == day for row in validation))

    def test_confirmed_composite_alloy_uses_actual_1050(self):
        value = pilot.weak_metadata({"ocr_alloy_raw": "1050 标1060", "card_state_raw": "H18"})
        self.assertEqual(value["alloy"], "1050")
        self.assertEqual(value["card_state"], "H18")

    def test_confirmed_mapping_does_not_generalize_to_other_labels(self):
        for alloy in ("1100F标1060", "1060标1050", "1050材1060"):
            self.assertIsNone(pilot.weak_metadata({"ocr_alloy_raw": alloy, "card_state_raw": "H18"})["alloy"])

    def test_original_pilot_policy_remains_reproducible(self):
        value = pilot.weak_metadata({"ocr_alloy_raw": "1050标1060", "card_state_raw": "H18"}, "unresolved-v1")
        self.assertIsNone(value["alloy"])

    def test_uncertain_state_zero_is_not_inferred_as_annealed(self):
        value = pilot.weak_metadata({"ocr_alloy_raw": "1100F", "card_state_raw": "0"})
        self.assertEqual(value["alloy"], "1100")
        self.assertIsNone(value["card_state"])


if __name__ == "__main__":
    unittest.main()
