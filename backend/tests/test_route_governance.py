import copy

from app.process_framework import load_material_library, load_rulebook
from app.route_governance import validate_route_records


def _route_rows():
    base = {
        "route_id": "R-TEST-001",
        "coil_id": "COIL-001",
        "dataset": "260999",
        "dat_source_files": "pda001.dat",
        "alloy": "1060",
        "anneal_before": "NONE",
        "anneal_recipe_id": "",
        "anneal_temperature_C": "",
        "anneal_hold_minutes": "",
        "confirmed_cumulative_true_strain_before": "",
        "width_mm": "1213",
        "product_tolerance_code": "CUSTOMER-A-001",
        "dat_audit_grade": "A",
        "abnormal_operation": "FALSE",
        "process_owner_decision": "APPROVED",
        "process_owner": "主任A",
        "decision_date": "2026-08-19",
        "interpass_thickness_change_reason": "",
        "interpass_width_change_reason": "",
        "notes": "test fixture",
    }
    first = {
        **base,
        "pass_number": "1",
        "candidate_id": "candidate_001",
        "entry_temper": "O",
        "entry_anneal_state": "FULLY_ANNEALED",
        "entry_thickness_mm": "6.0",
        "intermediate_thickness_mm": "3.4",
        "exit_thickness_mm": "2.0",
        "output_temper": "H14",
        "quality_decision": "NOT_INSPECTED",
    }
    second = {
        **base,
        "pass_number": "2",
        "candidate_id": "candidate_002",
        "entry_temper": "H14",
        "entry_anneal_state": "WORK_HARDENED",
        "entry_thickness_mm": "2.0",
        "intermediate_thickness_mm": "1.2",
        "exit_thickness_mm": "0.7",
        "output_temper": "H18",
        "quality_decision": "PASS",
    }
    return [first, second]


def _approved_configuration():
    rules = copy.deepcopy(load_rulebook())
    rules["status"] = "approved_for_model_training"
    materials = copy.deepcopy(load_material_library())
    materials["status"] = "approved_for_production"
    limits = materials["plant_equipment_limits"]
    limits["approved_for_production"] = True
    for key, value in limits.items():
        if key != "approved_for_production" and value is None:
            limits[key] = 1.0
    materials["materials"]["1060"]["status"] = "approved_for_production"
    return rules, materials


def test_complete_route_is_candidate_but_draft_governance_blocks_training():
    result = validate_route_records(_route_rows())[0]
    assert result.structurally_valid is True
    assert result.label_complete is True
    assert result.parameter_training_candidate is True
    assert result.route_training_eligible is False
    assert {issue.code for issue in result.issues} >= {
        "FRAMEWORK_NOT_APPROVED_FOR_TRAINING",
        "EQUIPMENT_LIMITS_NOT_APPROVED",
        "MATERIAL_ROUTE_NOT_APPROVED",
    }
    assert result.computed_passes[0]["cumulative_true_strain_before"] == 0.0
    assert result.computed_passes[1]["cumulative_true_strain_before"] > 1.0


def test_approved_configuration_releases_only_the_verified_route_label():
    rules, materials = _approved_configuration()
    result = validate_route_records(
        _route_rows(), rulebook=rules, material_library=materials
    )[0]
    assert result.parameter_training_candidate is True
    assert result.route_training_eligible is True
    assert result.issues == []


def test_unexplained_interpass_discontinuity_is_rejected():
    rows = _route_rows()
    rows[1]["entry_thickness_mm"] = "1.8"
    result = validate_route_records(rows)[0]
    assert result.structurally_valid is False
    assert result.parameter_training_candidate is False
    assert "INTERPASS_THICKNESS_DISCONTINUITY" in {
        issue.code for issue in result.issues
    }


def test_partial_anneal_cannot_implicitly_reset_cold_work():
    rows = _route_rows()
    rows[1].update(
        {
            "anneal_before": "PARTIAL",
            "anneal_recipe_id": "PA-001",
            "entry_anneal_state": "PARTIALLY_ANNEALED",
            "entry_temper": "H14",
            "confirmed_cumulative_true_strain_before": "",
        }
    )
    result = validate_route_records(rows)[0]
    codes = {issue.code for issue in result.issues}
    assert "ANNEAL_RECIPE_UNVERIFIED" in codes
    assert "PARTIAL_ANNEAL_COLD_WORK_UNKNOWN" in codes
    assert result.parameter_training_candidate is False
    assert result.route_training_eligible is False


def test_declared_interpass_cut_is_retained_as_warning_not_silently_dropped():
    rows = _route_rows()
    rows[1]["entry_thickness_mm"] = "1.8"
    rows[1]["interpass_thickness_change_reason"] = "现场确认中间切除异常头段"
    result = validate_route_records(rows)[0]
    assert result.structurally_valid is True
    assert "INTERPASS_THICKNESS_CHANGE_DECLARED" in {
        issue.code for issue in result.issues
    }
