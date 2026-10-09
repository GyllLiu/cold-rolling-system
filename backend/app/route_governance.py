"""Validation gates for human-confirmed multi-pass and annealing route labels.

This module deliberately separates a structurally valid route label from a route
that is permitted to train or influence production.  A complete row can still be
blocked by an unapproved rulebook, material route, equipment envelope or process-
owner decision.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Sequence

from .process_framework import load_material_library, load_rulebook


ROUTE_RECORD_COLUMNS: tuple[str, ...] = (
    "route_id",
    "coil_id",
    "pass_number",
    "dataset",
    "candidate_id",
    "dat_source_files",
    "alloy",
    "entry_temper",
    "entry_anneal_state",
    "anneal_before",
    "anneal_recipe_id",
    "anneal_temperature_C",
    "anneal_hold_minutes",
    "confirmed_cumulative_true_strain_before",
    "entry_thickness_mm",
    "intermediate_thickness_mm",
    "exit_thickness_mm",
    "width_mm",
    "output_temper",
    "product_tolerance_code",
    "dat_audit_grade",
    "abnormal_operation",
    "quality_decision",
    "process_owner_decision",
    "process_owner",
    "decision_date",
    "interpass_thickness_change_reason",
    "interpass_width_change_reason",
    "notes",
)

ANNEAL_EVENTS = {"NONE", "FULL", "PARTIAL"}
ANNEAL_STATES = {"FULLY_ANNEALED", "WORK_HARDENED", "PARTIALLY_ANNEALED"}
QUALITY_DECISIONS = {"PASS", "FAIL", "CONDITIONAL", "NOT_INSPECTED", "PENDING"}
OWNER_DECISIONS = {"APPROVED", "REJECTED", "PENDING"}


def _text(value: object) -> str:
    return str(value or "").strip()


def _upper(value: object) -> str:
    return _text(value).upper()


def _number(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _integer(value: object) -> int | None:
    number = _number(value)
    if number is None or not number.is_integer():
        return None
    return int(number)


def _boolean(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    normalized = _upper(value)
    if normalized in {"TRUE", "YES", "Y", "1", "是", "有"}:
        return True
    if normalized in {"FALSE", "NO", "N", "0", "否", "无"}:
        return False
    return None


@dataclass(frozen=True)
class RouteValidationIssue:
    route_id: str
    pass_number: int | None
    code: str
    severity: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RouteValidationResult:
    route_id: str
    row_count: int
    structurally_valid: bool
    label_complete: bool
    dat_gold_only: bool
    no_abnormal_operation: bool
    final_quality_passed: bool
    process_owner_approved: bool
    framework_approved: bool
    equipment_limits_approved: bool
    material_route_approved: bool
    parameter_training_candidate: bool
    route_training_eligible: bool
    computed_passes: list[dict[str, Any]]
    issues: list[RouteValidationIssue]

    def to_record(self) -> dict[str, Any]:
        counts = Counter(issue.severity for issue in self.issues)
        return {
            "route_id": self.route_id,
            "row_count": self.row_count,
            "structurally_valid": self.structurally_valid,
            "label_complete": self.label_complete,
            "dat_gold_only": self.dat_gold_only,
            "no_abnormal_operation": self.no_abnormal_operation,
            "final_quality_passed": self.final_quality_passed,
            "process_owner_approved": self.process_owner_approved,
            "framework_approved": self.framework_approved,
            "equipment_limits_approved": self.equipment_limits_approved,
            "material_route_approved": self.material_route_approved,
            "parameter_training_candidate": self.parameter_training_candidate,
            "route_training_eligible": self.route_training_eligible,
            "error_count": counts.get("error", 0),
            "blocker_count": counts.get("blocker", 0),
            "warning_count": counts.get("warning", 0),
            "issue_codes": ";".join(issue.code for issue in self.issues),
            "issue_messages": " | ".join(issue.message for issue in self.issues),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.to_record(),
            "computed_passes": self.computed_passes,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _issue(
    issues: list[RouteValidationIssue],
    *,
    route_id: str,
    pass_number: int | None,
    code: str,
    severity: str,
    message: str,
) -> None:
    issues.append(
        RouteValidationIssue(
            route_id=route_id,
            pass_number=pass_number,
            code=code,
            severity=severity,
            message=message,
        )
    )


def validate_route_records(
    rows: Sequence[Mapping[str, object]],
    *,
    rulebook: Mapping[str, Any] | None = None,
    material_library: Mapping[str, Any] | None = None,
    anneal_recipes: Mapping[str, Mapping[str, object]] | None = None,
    thickness_continuity_abs_tolerance_mm: float = 0.01,
    thickness_continuity_rel_tolerance: float = 0.005,
    width_continuity_tolerance_mm: float = 1.0,
) -> list[RouteValidationResult]:
    """Validate verified coil/pass labels and decide their training eligibility.

    Continuity tolerances are record-linkage tolerances, not rolling limits.  A
    larger intentional inter-pass change may be retained only with an explicit
    reason, and still remains visible as a warning.
    """

    if not rows:
        return []
    if thickness_continuity_abs_tolerance_mm < 0:
        raise ValueError("thickness continuity tolerance cannot be negative")
    if thickness_continuity_rel_tolerance < 0:
        raise ValueError("relative continuity tolerance cannot be negative")
    if width_continuity_tolerance_mm < 0:
        raise ValueError("width continuity tolerance cannot be negative")

    rulebook = rulebook or load_rulebook()
    material_library = material_library or load_material_library()
    recipe_catalogue = {key.upper(): value for key, value in (anneal_recipes or {}).items()}

    grouped: dict[str, list[Mapping[str, object]]] = defaultdict(list)
    anonymous_index = 0
    for row in rows:
        route_id = _text(row.get("route_id"))
        if not route_id:
            anonymous_index += 1
            route_id = f"__MISSING_ROUTE_ID_{anonymous_index}"
        grouped[route_id].append(row)

    results: list[RouteValidationResult] = []
    for route_id, route_rows in sorted(grouped.items()):
        issues: list[RouteValidationIssue] = []
        parsed: list[dict[str, Any]] = []
        for row in route_rows:
            pass_number = _integer(row.get("pass_number"))
            if route_id.startswith("__MISSING_ROUTE_ID_"):
                _issue(
                    issues,
                    route_id=route_id,
                    pass_number=pass_number,
                    code="MISSING_ROUTE_ID",
                    severity="error",
                    message="route_id is required to link passes into one route.",
                )
            if pass_number is None or pass_number < 1:
                _issue(
                    issues,
                    route_id=route_id,
                    pass_number=pass_number,
                    code="INVALID_PASS_NUMBER",
                    severity="error",
                    message="pass_number must be a positive integer.",
                )

            required_text = (
                "coil_id",
                "dataset",
                "candidate_id",
                "dat_source_files",
                "alloy",
                "entry_temper",
                "entry_anneal_state",
                "product_tolerance_code",
                "dat_audit_grade",
                "quality_decision",
                "process_owner_decision",
            )
            missing = [field for field in required_text if not _text(row.get(field))]
            if missing:
                _issue(
                    issues,
                    route_id=route_id,
                    pass_number=pass_number,
                    code="MISSING_ROUTE_LABEL_FIELDS",
                    severity="error",
                    message="Missing fields: " + ", ".join(missing),
                )

            dimensions = {
                field: _number(row.get(field))
                for field in (
                    "entry_thickness_mm",
                    "intermediate_thickness_mm",
                    "exit_thickness_mm",
                    "width_mm",
                )
            }
            if any(value is None for value in dimensions.values()):
                _issue(
                    issues,
                    route_id=route_id,
                    pass_number=pass_number,
                    code="INVALID_ROUTE_DIMENSIONS",
                    severity="error",
                    message="All three thicknesses and width must be finite numbers.",
                )
                entry = intermediate = exit_value = width = None
            else:
                entry = float(dimensions["entry_thickness_mm"])
                intermediate = float(dimensions["intermediate_thickness_mm"])
                exit_value = float(dimensions["exit_thickness_mm"])
                width = float(dimensions["width_mm"])
                if not entry > intermediate > exit_value > 0 or width <= 0:
                    _issue(
                        issues,
                        route_id=route_id,
                        pass_number=pass_number,
                        code="INVALID_THICKNESS_ROUTE",
                        severity="error",
                        message="Expected entry > intermediate > exit > 0 and width > 0.",
                    )

            anneal_event = _upper(row.get("anneal_before")) or "NONE"
            if anneal_event not in ANNEAL_EVENTS:
                _issue(
                    issues,
                    route_id=route_id,
                    pass_number=pass_number,
                    code="INVALID_ANNEAL_EVENT",
                    severity="error",
                    message=f"anneal_before must be one of {sorted(ANNEAL_EVENTS)}.",
                )
            entry_state = _upper(row.get("entry_anneal_state"))
            if entry_state not in ANNEAL_STATES:
                _issue(
                    issues,
                    route_id=route_id,
                    pass_number=pass_number,
                    code="INVALID_ENTRY_ANNEAL_STATE",
                    severity="error",
                    message=f"entry_anneal_state must be one of {sorted(ANNEAL_STATES)}.",
                )

            recipe_id = _upper(row.get("anneal_recipe_id"))
            if anneal_event in {"FULL", "PARTIAL"}:
                if not recipe_id:
                    _issue(
                        issues,
                        route_id=route_id,
                        pass_number=pass_number,
                        code="MISSING_ANNEAL_RECIPE",
                        severity="error",
                        message="An anneal event requires anneal_recipe_id.",
                    )
                elif recipe_id not in recipe_catalogue:
                    _issue(
                        issues,
                        route_id=route_id,
                        pass_number=pass_number,
                        code="ANNEAL_RECIPE_UNVERIFIED",
                        severity="blocker",
                        message=f"Anneal recipe {recipe_id} is not in the approved catalogue.",
                    )
                if anneal_event == "FULL" and entry_state != "FULLY_ANNEALED":
                    _issue(
                        issues,
                        route_id=route_id,
                        pass_number=pass_number,
                        code="FULL_ANNEAL_STATE_MISMATCH",
                        severity="error",
                        message="FULL anneal requires entry_anneal_state FULLY_ANNEALED.",
                    )
                if anneal_event == "PARTIAL" and entry_state != "PARTIALLY_ANNEALED":
                    _issue(
                        issues,
                        route_id=route_id,
                        pass_number=pass_number,
                        code="PARTIAL_ANNEAL_STATE_MISMATCH",
                        severity="error",
                        message="PARTIAL anneal requires PARTIALLY_ANNEALED entry state.",
                    )

            abnormal = _boolean(row.get("abnormal_operation"))
            if abnormal is None:
                _issue(
                    issues,
                    route_id=route_id,
                    pass_number=pass_number,
                    code="UNKNOWN_ABNORMAL_OPERATION",
                    severity="error",
                    message="abnormal_operation must explicitly be TRUE or FALSE.",
                )

            audit_grade = _upper(row.get("dat_audit_grade"))
            if audit_grade not in {"A", "B", "C", "D"}:
                _issue(
                    issues,
                    route_id=route_id,
                    pass_number=pass_number,
                    code="INVALID_DAT_AUDIT_GRADE",
                    severity="error",
                    message="dat_audit_grade must be A, B, C or D.",
                )
            quality = _upper(row.get("quality_decision"))
            if quality not in QUALITY_DECISIONS:
                _issue(
                    issues,
                    route_id=route_id,
                    pass_number=pass_number,
                    code="INVALID_QUALITY_DECISION",
                    severity="error",
                    message=f"quality_decision must be one of {sorted(QUALITY_DECISIONS)}.",
                )
            owner_decision = _upper(row.get("process_owner_decision"))
            if owner_decision not in OWNER_DECISIONS:
                _issue(
                    issues,
                    route_id=route_id,
                    pass_number=pass_number,
                    code="INVALID_OWNER_DECISION",
                    severity="error",
                    message=f"process_owner_decision must be one of {sorted(OWNER_DECISIONS)}.",
                )
            if owner_decision == "APPROVED" and not _text(row.get("process_owner")):
                _issue(
                    issues,
                    route_id=route_id,
                    pass_number=pass_number,
                    code="APPROVAL_WITHOUT_OWNER",
                    severity="error",
                    message="An APPROVED row requires process_owner.",
                )
            if owner_decision == "APPROVED" and not _text(row.get("decision_date")):
                _issue(
                    issues,
                    route_id=route_id,
                    pass_number=pass_number,
                    code="APPROVAL_WITHOUT_DATE",
                    severity="error",
                    message="An APPROVED row requires decision_date for traceability.",
                )

            pass_true_strain = None
            f1_share = None
            total_reduction = None
            if (
                entry is not None
                and intermediate is not None
                and exit_value is not None
                and entry > intermediate > exit_value > 0
            ):
                pass_true_strain = math.log(entry / exit_value)
                f1_share = math.log(entry / intermediate) / pass_true_strain
                total_reduction = 1.0 - exit_value / entry

            parsed.append(
                {
                    "source": row,
                    "pass_number": pass_number,
                    "coil_id": _text(row.get("coil_id")),
                    "alloy": _upper(row.get("alloy")),
                    "entry_temper": _upper(row.get("entry_temper")),
                    "output_temper": _upper(row.get("output_temper")),
                    "entry_anneal_state": entry_state,
                    "anneal_before": anneal_event,
                    "entry_thickness_mm": entry,
                    "intermediate_thickness_mm": intermediate,
                    "exit_thickness_mm": exit_value,
                    "width_mm": width,
                    "confirmed_cumulative_true_strain_before": _number(
                        row.get("confirmed_cumulative_true_strain_before")
                    ),
                    "pass_true_strain": pass_true_strain,
                    "f1_true_strain_share": f1_share,
                    "total_reduction_ratio": total_reduction,
                    "dat_audit_grade": audit_grade,
                    "abnormal_operation": abnormal,
                    "quality_decision": quality,
                    "process_owner_decision": owner_decision,
                }
            )

        parsed.sort(
            key=lambda item: (
                item["pass_number"] is None,
                item["pass_number"] if item["pass_number"] is not None else math.inf,
            )
        )
        pass_numbers = [item["pass_number"] for item in parsed if item["pass_number"] is not None]
        if len(pass_numbers) != len(set(pass_numbers)):
            _issue(
                issues,
                route_id=route_id,
                pass_number=None,
                code="DUPLICATE_PASS_NUMBER",
                severity="error",
                message="Pass numbers must be unique within a route.",
            )
        if pass_numbers and pass_numbers != list(range(1, len(pass_numbers) + 1)):
            _issue(
                issues,
                route_id=route_id,
                pass_number=None,
                code="NONCONTIGUOUS_PASS_SEQUENCE",
                severity="error",
                message="Pass numbers must start at 1 and remain contiguous.",
            )

        for identity_field in ("coil_id", "alloy"):
            values = {item[identity_field] for item in parsed if item[identity_field]}
            if len(values) > 1:
                _issue(
                    issues,
                    route_id=route_id,
                    pass_number=None,
                    code=f"INCONSISTENT_{identity_field.upper()}",
                    severity="error",
                    message=f"All passes in one route must share {identity_field}.",
                )

        cumulative: float | None = None
        computed_passes: list[dict[str, Any]] = []
        for index, item in enumerate(parsed):
            pass_number = item["pass_number"]
            provided_cumulative = item["confirmed_cumulative_true_strain_before"]
            if item["anneal_before"] == "FULL":
                cumulative = 0.0
            elif item["anneal_before"] == "PARTIAL":
                if provided_cumulative is None:
                    cumulative = None
                    _issue(
                        issues,
                        route_id=route_id,
                        pass_number=pass_number,
                        code="PARTIAL_ANNEAL_COLD_WORK_UNKNOWN",
                        severity="blocker",
                        message=(
                            "Partial anneal must not reset cold work implicitly; provide "
                            "confirmed_cumulative_true_strain_before or an approved state rule."
                        ),
                    )
                else:
                    cumulative = provided_cumulative
            elif index == 0:
                if item["entry_anneal_state"] == "FULLY_ANNEALED":
                    cumulative = 0.0
                else:
                    cumulative = provided_cumulative
                    if cumulative is None:
                        _issue(
                            issues,
                            route_id=route_id,
                            pass_number=pass_number,
                            code="INITIAL_COLD_WORK_UNKNOWN",
                            severity="blocker",
                            message=(
                                "The first pass is not fully annealed and has no confirmed "
                                "cumulative true strain."
                            ),
                        )
            elif (
                item["anneal_before"] == "NONE"
                and item["entry_anneal_state"] == "FULLY_ANNEALED"
            ):
                _issue(
                    issues,
                    route_id=route_id,
                    pass_number=pass_number,
                    code="UNDECLARED_FULL_ANNEAL_TRANSITION",
                    severity="error",
                    message=(
                        "A later pass is marked FULLY_ANNEALED but anneal_before is NONE."
                    ),
                )
            elif provided_cumulative is not None and cumulative is not None:
                if not math.isclose(provided_cumulative, cumulative, abs_tol=1e-4):
                    _issue(
                        issues,
                        route_id=route_id,
                        pass_number=pass_number,
                        code="CUMULATIVE_STRAIN_MISMATCH",
                        severity="error",
                        message=(
                            f"Provided cumulative strain {provided_cumulative:.6f} does "
                            f"not match linked-pass value {cumulative:.6f}."
                        ),
                    )

            cumulative_before = cumulative
            if cumulative is not None and item["pass_true_strain"] is not None:
                cumulative = cumulative + item["pass_true_strain"]

            if index > 0:
                previous = parsed[index - 1]
                if (
                    item["anneal_before"] == "NONE"
                    and previous["output_temper"]
                    and item["entry_temper"]
                    and previous["output_temper"] != item["entry_temper"]
                ):
                    _issue(
                        issues,
                        route_id=route_id,
                        pass_number=pass_number,
                        code="UNEXPLAINED_TEMPER_TRANSITION",
                        severity="error",
                        message=(
                            f"Previous output temper {previous['output_temper']} and "
                            f"current entry temper {item['entry_temper']} differ without "
                            "an anneal event."
                        ),
                    )
                previous_exit = previous["exit_thickness_mm"]
                entry = item["entry_thickness_mm"]
                if previous_exit is not None and entry is not None:
                    continuous = math.isclose(
                        previous_exit,
                        entry,
                        rel_tol=thickness_continuity_rel_tolerance,
                        abs_tol=thickness_continuity_abs_tolerance_mm,
                    )
                    if not continuous:
                        reason = _text(
                            item["source"].get("interpass_thickness_change_reason")
                        )
                        _issue(
                            issues,
                            route_id=route_id,
                            pass_number=pass_number,
                            code=(
                                "INTERPASS_THICKNESS_CHANGE_DECLARED"
                                if reason
                                else "INTERPASS_THICKNESS_DISCONTINUITY"
                            ),
                            severity="warning" if reason else "error",
                            message=(
                                f"Previous exit {previous_exit:g} mm and current entry "
                                f"{entry:g} mm differ; reason={reason or 'missing'}."
                            ),
                        )
                previous_width = previous["width_mm"]
                width = item["width_mm"]
                if previous_width is not None and width is not None:
                    width_change = width - previous_width
                    if abs(width_change) > width_continuity_tolerance_mm:
                        reason = _text(
                            item["source"].get("interpass_width_change_reason")
                        )
                        _issue(
                            issues,
                            route_id=route_id,
                            pass_number=pass_number,
                            code=(
                                "INTERPASS_WIDTH_CHANGE_DECLARED"
                                if reason
                                else "INTERPASS_WIDTH_DISCONTINUITY"
                            ),
                            severity="warning" if reason else "error",
                            message=(
                                f"Width changed from {previous_width:g} to {width:g} mm; "
                                f"reason={reason or 'missing'}."
                            ),
                        )

            computed_passes.append(
                {
                    "pass_number": pass_number,
                    "pass_true_strain": item["pass_true_strain"],
                    "f1_true_strain_share": item["f1_true_strain_share"],
                    "total_reduction_ratio": item["total_reduction_ratio"],
                    "cumulative_true_strain_before": cumulative_before,
                    "cumulative_true_strain_after": cumulative,
                }
            )

        alloys = {item["alloy"] for item in parsed if item["alloy"]}
        alloy = next(iter(alloys)) if len(alloys) == 1 else ""
        material = material_library.get("materials", {}).get(alloy)
        material_route_approved = bool(
            material_library.get("status") == "approved_for_production"
            and material
            and material.get("status") == "approved_for_production"
            and all(
                item["entry_temper"] in set(material.get("allowed_input_tempers", ()))
                for item in parsed
                if item["entry_temper"]
            )
        )
        if not material_route_approved:
            _issue(
                issues,
                route_id=route_id,
                pass_number=None,
                code="MATERIAL_ROUTE_NOT_APPROVED",
                severity="blocker",
                message=f"Alloy/state route {alloy or 'UNKNOWN'} is not approved.",
            )

        framework_approved = rulebook.get("status") == "approved_for_model_training"
        if not framework_approved:
            _issue(
                issues,
                route_id=route_id,
                pass_number=None,
                code="FRAMEWORK_NOT_APPROVED_FOR_TRAINING",
                severity="blocker",
                message="The process rulebook is still draft.",
            )
        equipment_limits = material_library.get("plant_equipment_limits", {})
        equipment_approved = bool(
            equipment_limits.get("approved_for_production")
            and all(
                value is not None
                for key, value in equipment_limits.items()
                if key != "approved_for_production"
            )
        )
        if not equipment_approved:
            _issue(
                issues,
                route_id=route_id,
                pass_number=None,
                code="EQUIPMENT_LIMITS_NOT_APPROVED",
                severity="blocker",
                message="Plant equipment limits are incomplete or unapproved.",
            )

        structurally_valid = not any(issue.severity == "error" for issue in issues)
        label_complete = structurally_valid and all(
            item["pass_number"] is not None
            and item["entry_thickness_mm"] is not None
            and item["intermediate_thickness_mm"] is not None
            and item["exit_thickness_mm"] is not None
            and item["width_mm"] is not None
            for item in parsed
        )
        dat_gold_only = bool(parsed) and all(
            item["dat_audit_grade"] == "A" for item in parsed
        )
        no_abnormal = bool(parsed) and all(
            item["abnormal_operation"] is False for item in parsed
        )
        final_quality_passed = bool(parsed) and parsed[-1]["quality_decision"] == "PASS"
        owner_approved = bool(parsed) and all(
            item["process_owner_decision"] == "APPROVED" for item in parsed
        )
        label_blocking_codes = {
            "ANNEAL_RECIPE_UNVERIFIED",
            "PARTIAL_ANNEAL_COLD_WORK_UNKNOWN",
            "INITIAL_COLD_WORK_UNKNOWN",
        }
        parameter_training_candidate = bool(
            label_complete
            and dat_gold_only
            and no_abnormal
            and final_quality_passed
            and owner_approved
            and not any(issue.code in label_blocking_codes for issue in issues)
        )
        route_training_eligible = bool(
            parameter_training_candidate
            and framework_approved
            and equipment_approved
            and material_route_approved
            and not any(issue.severity == "blocker" for issue in issues)
        )
        results.append(
            RouteValidationResult(
                route_id=route_id,
                row_count=len(route_rows),
                structurally_valid=structurally_valid,
                label_complete=label_complete,
                dat_gold_only=dat_gold_only,
                no_abnormal_operation=no_abnormal,
                final_quality_passed=final_quality_passed,
                process_owner_approved=owner_approved,
                framework_approved=framework_approved,
                equipment_limits_approved=equipment_approved,
                material_route_approved=material_route_approved,
                parameter_training_candidate=parameter_training_candidate,
                route_training_eligible=route_training_eligible,
                computed_passes=computed_passes,
                issues=issues,
            )
        )
    return results
