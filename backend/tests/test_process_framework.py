import copy
import math

import pytest

from app.process_framework import (
    RouteInputError,
    assess_spec_support,
    audit_segment,
    build_spec_support_envelope,
    f1_true_strain_share,
    load_material_library,
    load_rulebook,
    plan_route,
    total_tension_to_unit_tension,
)


def _valid_segment() -> dict[str, object]:
    row: dict[str, object] = {
        "candidate_id": "candidate_test_001",
        "dataset": "260999",
        "learning_eligible": "true",
        "entry_thickness_set_mode": "1.5",
        "intermediate_thickness_set_mode": "1.05",
        "exit_thickness_set_mode": "0.77",
        "width_set_mode": "1213",
        "entry_thickness_actual_median": "1.5",
        "exit_thickness_actual_median": "0.7701",
        "exit_thickness_error_abs_p95": "0.005",
        "uncoiler_tension_set_median": "7.0",
        "interstand_tension_set_median": "5.5",
        "coiler_tension_set_median": "4.5",
        "tm1_closed_gap_set_median": "1.2",
        "tm2_closed_gap_set_median": "0.95",
        "extracted_high_speed_samples_1hz": "300",
        "flatness_profile_valid_samples": "300",
        "flatness_decoded_zone_count": "49",
        "extraction_channel_error_count": "0",
        "uncoiler_tension_actual_median": "7.0",
        "interstand_tension_actual_median": "5.5",
        "coiler_tension_actual_median": "4.5",
        "strip_break_flag_max": "0",
        "drive_no_fault_state_min": "1",
        "total_estop_state_min": "1",
        "fire_estop_state_min": "1",
    }
    row["uncoiler_unit_tension_dat_median"] = str(
        total_tension_to_unit_tension(7.0, 1213.0, 1.5)
    )
    row["interstand_unit_tension_dat_median"] = str(
        total_tension_to_unit_tension(5.5, 1213.0, 1.05)
    )
    row["coiler_unit_tension_dat_median"] = str(
        total_tension_to_unit_tension(4.5, 1213.0, 0.77)
    )
    return row


def test_rulebook_has_traceable_sources_for_every_rule():
    rulebook = load_rulebook()
    source_ids = {source["source_id"] for source in rulebook["sources"]}
    assert rulebook["principles"]["dat_is_not_ground_truth"] is True
    assert rulebook["principles"]["production_auto_apply"] is False
    assert rulebook["status"] == "draft_requires_process_owner_approval"
    assert all(rule["source_ids"] for rule in rulebook["rules"])
    assert all(set(rule["source_ids"]) <= source_ids for rule in rulebook["rules"])


def test_hmi_tonnes_are_normalized_by_local_section():
    # This reproduces the magnitude shown on the plant HMI: 7 T over a
    # 1270 mm x 2 mm section is approximately 0.027 kN/mm².
    unit_tension = total_tension_to_unit_tension(7.0, 1270.0, 2.0)
    assert unit_tension == pytest.approx(0.0270262, rel=1e-4)


def test_true_strain_share_uses_logarithmic_reduction():
    share = f1_true_strain_share(1.5, 1.05, 0.77)
    assert 0.49 < share < 0.56


def _support_rows() -> list[dict[str, object]]:
    return [
        {
            "entry_thickness_set_mode": entry,
            "exit_thickness_set_mode": exit_value,
            "width_set_mode": width,
            "coiler_tension_set_median": forbidden_target,
        }
        for entry, exit_value, width, forbidden_target in (
            (1.5, 0.77, 1210, 4.5),
            (1.6, 0.80, 1210, 4.7),
            (2.0, 1.00, 1250, 5.0),
            (3.0, 1.50, 1300, 6.0),
            (6.0, 2.00, 1070, 7.0),
        )
    ]


def test_spec_support_envelope_contains_coordinates_but_no_output_targets():
    envelope = build_spec_support_envelope(_support_rows())
    serialized = str(envelope)
    assert envelope["unique_specification_count"] == 5
    assert "coiler_tension_set_median" not in serialized
    assert "forbidden_target" not in serialized
    assert envelope["status"] == "shadow_gate_requires_process_owner_approval"


def test_spec_support_exact_match_passes_but_axis_extrapolation_is_rejected():
    envelope = build_spec_support_envelope(_support_rows())
    exact = assess_spec_support(1.5, 0.77, 1210, envelope)
    outside = assess_spec_support(7.0, 0.77, 1210, envelope)
    assert exact.status == "exact_overlap"
    assert exact.gate_passed is True
    assert exact.coverage_confidence_ceiling == "unchanged"
    assert outside.status == "outside_axis_bounds"
    assert outside.gate_passed is False
    assert outside.outside_axis_fields == ("entry_thickness_set_mode",)


def test_valid_segment_is_only_an_unapproved_gold_candidate():
    result = audit_segment(_valid_segment())
    assert result.grade == "A"
    assert result.training_pool_candidate is True
    assert result.approved_for_parameter_training is False
    assert result.human_approval_required is True
    assert result.metrics["unit_tension_hierarchy_expected"] is True
    assert result.metrics["unit_tension_source"] == "DAT_direct"
    assert result.metrics["tension_actual_set_max_relative_error"] == 0.0
    assert result.reasons[0].code == "GOLD_CANDIDATE_PENDING_APPROVAL"


def test_total_tension_order_is_not_used_as_unit_tension_order():
    row = _valid_segment()
    # Total tonnes descend 7 > 5.5 > 4.5, while section-normalized tension
    # rises through the mill because thickness falls.
    result = audit_segment(row)
    assert float(row["uncoiler_tension_set_median"]) > float(
        row["interstand_tension_set_median"]
    )
    assert float(row["interstand_tension_set_median"]) > float(
        row["coiler_tension_set_median"]
    )
    assert result.metrics["coiler_unit_tension_kN_per_mm2"] > result.metrics[
        "interstand_unit_tension_kN_per_mm2"
    ]
    assert result.metrics["interstand_unit_tension_kN_per_mm2"] > result.metrics[
        "uncoiler_unit_tension_kN_per_mm2"
    ]


def test_unstable_exit_thickness_becomes_risk_candidate_not_parameter_target():
    row = _valid_segment()
    row["exit_thickness_error_abs_p95"] = "0.02"
    result = audit_segment(row)
    assert result.grade == "C"
    assert result.training_pool_candidate is False
    assert result.risk_training_candidate is True
    assert "EXIT_THICKNESS_VARIATION_TOO_HIGH" in {
        reason.code for reason in result.reasons
    }


def test_missing_core_signal_is_grade_d_and_not_used_anywhere():
    row = _valid_segment()
    row["tm1_closed_gap_set_median"] = ""
    result = audit_segment(row)
    assert result.grade == "D"
    assert result.training_pool_candidate is False
    assert result.risk_training_candidate is False


def test_shadow_route_for_large_reduction_generates_multiple_candidates():
    result = plan_route(
        {
            "entry_thickness_mm": 6.0,
            "target_exit_thickness_mm": 0.7,
            "width_mm": 1213.0,
            "alloy": "1060",
            "initial_temper": "O",
            "anneal_state": "fully_annealed",
        },
        mode="shadow",
    )
    assert result["status"] == "shadow_candidates_generated"
    assert result["production_releasable"] is False
    assert result["automatic_plc_write"] is False
    assert result["candidates"][0]["pass_count"] == 2
    passes = result["candidates"][0]["passes"]
    assert passes[0]["entry_thickness_mm"] == pytest.approx(6.0)
    assert passes[-1]["exit_thickness_mm"] == pytest.approx(0.7)
    assert passes[0]["exit_thickness_mm"] == pytest.approx(
        passes[1]["entry_thickness_mm"]
    )
    assert all(
        item["entry_thickness_mm"]
        > item["intermediate_thickness_mm"]
        > item["exit_thickness_mm"]
        for item in passes
    )


def test_current_draft_route_library_blocks_production_route():
    result = plan_route(
        {
            "entry_thickness_mm": 6.0,
            "target_exit_thickness_mm": 0.7,
            "width_mm": 1213.0,
            "alloy": "1060",
            "initial_temper": "O",
            "anneal_state": "fully_annealed",
        },
        mode="production",
    )
    assert result["status"] == "blocked_incomplete_engineering_approval"
    assert result["production_releasable"] is False
    assert result["candidates"] == []


def test_approved_test_configuration_can_place_required_full_anneal():
    library = copy.deepcopy(load_material_library())
    library["status"] = "approved_for_production"
    library["plant_equipment_limits"]["approved_for_production"] = True
    for key, value in list(library["plant_equipment_limits"].items()):
        if key != "approved_for_production" and value is None:
            library["plant_equipment_limits"][key] = 1.0
    library["materials"]["1060"]["status"] = "approved_for_production"
    library["materials"]["1060"][
        "maximum_cumulative_true_strain_before_full_anneal"
    ] = 1.5

    result = plan_route(
        {
            "entry_thickness_mm": 6.0,
            "target_exit_thickness_mm": 0.7,
            "width_mm": 1213.0,
            "alloy": "1060",
            "initial_temper": "O",
            "anneal_state": "fully_annealed",
            "maximum_true_strain_per_tandem_pass": 1.1,
        },
        mode="production",
        material_library=library,
    )
    assert result["production_releasable"] is True
    assert result["automatic_plc_write"] is False
    two_pass = next(
        candidate for candidate in result["candidates"] if candidate["pass_count"] == 2
    )
    assert two_pass["anneal_count"] == 1
    assert two_pass["passes"][1]["anneal_before"] == (
        "FULL_ANNEAL_REQUIRED_BY_CONFIGURED_LIMIT"
    )
    assert math.isclose(
        two_pass["passes"][1]["cumulative_true_strain_before"], 0.0
    )


def test_route_rejects_non_reducing_thickness():
    with pytest.raises(RouteInputError):
        plan_route(
            {
                "entry_thickness_mm": 0.7,
                "target_exit_thickness_mm": 0.7,
                "width_mm": 1213,
            }
        )
