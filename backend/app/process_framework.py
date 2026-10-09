"""Rule-first process governance for DAT audits and multi-pass shadow planning.

The frozen v0.4.1 recommendation model remains untouched.  This module sits above
future models: standards and approved plant limits define the admissible process
space, DAT segments are graded inside that space, and route candidates are created
only in an explicitly labelled shadow mode until the missing engineering limits are
approved.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RULEBOOK_PATH = PROJECT_ROOT / "process_framework" / "rules_v1.json"
DEFAULT_MATERIAL_LIBRARY_PATH = (
    PROJECT_ROOT / "process_framework" / "material_routes_v1.json"
)

GRADE_RANK = {"A": 0, "B": 1, "C": 2, "D": 3}


class FrameworkConfigurationError(ValueError):
    """Raised when a rule or route library is internally inconsistent."""


class RouteInputError(ValueError):
    """Raised when a route request is geometrically impossible."""


def _read_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise FrameworkConfigurationError(f"Expected a JSON object in {path}")
    return payload


def load_rulebook(path: Path | None = None) -> dict[str, Any]:
    """Load and minimally validate the versioned process-rule source."""

    rulebook = _read_json(path or DEFAULT_RULEBOOK_PATH)
    required = {
        "schema_version",
        "framework_version",
        "status",
        "sources",
        "rules",
        "data_grade_policy",
        "audit_thresholds",
    }
    missing = sorted(required - rulebook.keys())
    if missing:
        raise FrameworkConfigurationError(
            f"Rulebook is missing required keys: {', '.join(missing)}"
        )

    source_ids = {source.get("source_id") for source in rulebook["sources"]}
    rule_ids: set[str] = set()
    for rule in rulebook["rules"]:
        rule_id = str(rule.get("rule_id", "")).strip()
        if not rule_id or rule_id in rule_ids:
            raise FrameworkConfigurationError(
                f"Missing or duplicate rule_id: {rule_id!r}"
            )
        rule_ids.add(rule_id)
        unknown_sources = set(rule.get("source_ids", ())) - source_ids
        if unknown_sources:
            raise FrameworkConfigurationError(
                f"Rule {rule_id} references unknown sources: "
                f"{', '.join(sorted(unknown_sources))}"
            )
    return rulebook


def load_material_library(path: Path | None = None) -> dict[str, Any]:
    """Load the material-state and plant-limit route configuration."""

    library = _read_json(path or DEFAULT_MATERIAL_LIBRARY_PATH)
    required = {
        "schema_version",
        "library_version",
        "status",
        "shadow_defaults_from_current_high_speed_dat",
        "plant_equipment_limits",
        "materials",
        "route_release_policy",
    }
    missing = sorted(required - library.keys())
    if missing:
        raise FrameworkConfigurationError(
            f"Material library is missing required keys: {', '.join(missing)}"
        )
    return library


def _finite(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _threshold(rulebook: Mapping[str, Any], name: str) -> float:
    try:
        value = float(rulebook["audit_thresholds"][name]["value"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FrameworkConfigurationError(
            f"Invalid or missing audit threshold {name!r}"
        ) from exc
    if not math.isfinite(value):
        raise FrameworkConfigurationError(f"Threshold {name!r} is not finite")
    return value


def total_tension_to_unit_tension(
    total_tension_tonnes: float,
    width_mm: float,
    local_thickness_mm: float,
) -> float:
    """Convert HMI total tension T into section-normalized kN/mm².

    The conversion assumes the HMI ``T`` channel is metric tonne-force, which is
    consistent with the plant screen and observed magnitude.  That assumption is
    kept explicit so it can be replaced if the PLC variable table says otherwise.
    """

    values = (total_tension_tonnes, width_mm, local_thickness_mm)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Tension conversion requires finite values")
    if total_tension_tonnes < 0 or width_mm <= 0 or local_thickness_mm <= 0:
        raise ValueError("Tension must be non-negative and section dimensions positive")
    return 9.80665 * total_tension_tonnes / (width_mm * local_thickness_mm)


def f1_true_strain_share(
    entry_thickness_mm: float,
    intermediate_thickness_mm: float,
    exit_thickness_mm: float,
) -> float:
    """Return F1's share of total logarithmic thickness strain."""

    if not entry_thickness_mm > intermediate_thickness_mm > exit_thickness_mm > 0:
        raise ValueError("Expected entry > intermediate > exit > 0")
    return math.log(entry_thickness_mm / intermediate_thickness_mm) / math.log(
        entry_thickness_mm / exit_thickness_mm
    )


SPEC_SUPPORT_RAW_FIELDS: tuple[str, ...] = (
    "entry_thickness_set_mode",
    "exit_thickness_set_mode",
    "width_set_mode",
)
SPEC_SUPPORT_TRANSFORMED_FEATURES: tuple[str, ...] = (
    "log_entry_thickness",
    "log_exit_thickness",
    "width_mm",
    "log_entry_exit_ratio",
)


def _linear_quantile(values: Sequence[float], probability: float) -> float:
    """Return a dependency-free linearly interpolated quantile."""

    if not values:
        raise ValueError("Quantile requires at least one value")
    if not 0 <= probability <= 1:
        raise ValueError("Quantile probability must be between 0 and 1")
    ordered = sorted(float(value) for value in values)
    position = probability * (len(ordered) - 1)
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _spec_values(
    entry_thickness_mm: object,
    exit_thickness_mm: object,
    width_mm: object,
) -> tuple[tuple[float, float, float], tuple[float, float, float, float]]:
    entry = _finite(entry_thickness_mm)
    exit_value = _finite(exit_thickness_mm)
    width = _finite(width_mm)
    if entry is None or exit_value is None or width is None:
        raise ValueError("Specification support requires three finite inputs")
    if not entry > exit_value > 0 or width <= 0:
        raise ValueError("Expected entry thickness > exit thickness > 0 and width > 0")
    raw = (entry, exit_value, width)
    transformed = (
        math.log(entry),
        math.log(exit_value),
        width,
        math.log(entry / exit_value),
    )
    return raw, transformed


def _scaled_spec_distance(
    left: Sequence[float],
    right: Sequence[float],
    scales: Sequence[float],
) -> float:
    if not len(left) == len(right) == len(scales):
        raise FrameworkConfigurationError("Specification distance dimensions differ")
    if any(scale <= 0 or not math.isfinite(scale) for scale in scales):
        raise FrameworkConfigurationError("Specification distance scales must be positive")
    return math.sqrt(
        sum(((a - b) / scale) ** 2 for a, b, scale in zip(left, right, scales))
    )


def build_spec_support_envelope(
    rows: Sequence[Mapping[str, object]],
    *,
    envelope_version: str = "spec-support-envelope-v0.1-draft",
) -> dict[str, Any]:
    """Build a specification-combination support envelope from approved candidates.

    The envelope contains no output targets and performs no neighbour averaging.  It
    only answers whether a three-input request is close to combinations represented
    by the reference rows.  Callers remain responsible for selecting only governed
    A-class rows before invoking this function.
    """

    unique_specs: dict[tuple[float, float, float], tuple[float, float, float]] = {}
    invalid_row_count = 0
    for row in rows:
        try:
            raw, _ = _spec_values(*(row.get(field) for field in SPEC_SUPPORT_RAW_FIELDS))
        except ValueError:
            invalid_row_count += 1
            continue
        # DAT setpoints are much coarser than these digits.  Rounding prevents
        # binary floating-point noise from inventing duplicate specifications.
        key = (round(raw[0], 6), round(raw[1], 6), round(raw[2], 3))
        unique_specs[key] = raw

    points = sorted(unique_specs.values())
    if len(points) < 3:
        raise FrameworkConfigurationError(
            "At least three unique valid specification points are required"
        )
    transformed = [_spec_values(*point)[1] for point in points]
    columns = list(zip(*transformed))
    scales: list[float] = []
    for column in columns:
        robust_range = _linear_quantile(column, 0.90) - _linear_quantile(column, 0.10)
        full_range = max(column) - min(column)
        scales.append(robust_range if robust_range > 1e-12 else full_range or 1.0)

    leave_one_out_nearest: list[float] = []
    for index, vector in enumerate(transformed):
        leave_one_out_nearest.append(
            min(
                _scaled_spec_distance(vector, other, scales)
                for other_index, other in enumerate(transformed)
                if other_index != index
            )
        )

    thresholds = {
        "p90": _linear_quantile(leave_one_out_nearest, 0.90),
        "p95": _linear_quantile(leave_one_out_nearest, 0.95),
        "p99": _linear_quantile(leave_one_out_nearest, 0.99),
    }
    raw_columns = list(zip(*points))
    return {
        "schema_version": "cold-rolling-spec-support-envelope-v1",
        "envelope_version": envelope_version,
        "status": "shadow_gate_requires_process_owner_approval",
        "purpose": (
            "Reject or lower confidence for unsupported three-input specification "
            "combinations; never predict outputs from neighbours."
        ),
        "source_row_count": len(rows),
        "invalid_source_row_count": invalid_row_count,
        "unique_specification_count": len(points),
        "raw_fields": list(SPEC_SUPPORT_RAW_FIELDS),
        "transformed_features": list(SPEC_SUPPORT_TRANSFORMED_FEATURES),
        "distance_formula": (
            "sqrt(sum(((x_i-nearest_i)/robust_scale_i)^2)); robust_scale=p90-p10"
        ),
        "robust_scales": dict(zip(SPEC_SUPPORT_TRANSFORMED_FEATURES, scales)),
        "raw_bounds": {
            field: {"min": min(column), "max": max(column)}
            for field, column in zip(SPEC_SUPPORT_RAW_FIELDS, raw_columns)
        },
        "leave_one_unique_spec_out_nearest_distance": {
            **thresholds,
            "maximum": max(leave_one_out_nearest),
            "sample_count": len(leave_one_out_nearest),
        },
        "exact_match_tolerances": {
            "entry_thickness_mm": 1e-6,
            "exit_thickness_mm": 1e-6,
            "width_mm": 1e-3,
        },
        "decision_policy": {
            "exact_overlap": "gate_pass; coverage does not increase model confidence",
            "well_supported": "gate_pass; distance <= leave-one-out p90",
            "edge_supported": "gate_pass_with_low_confidence; p90 < distance <= p95",
            "sparse_or_outside": "reject; distance > p95",
            "outside_axis_bounds": "reject; any raw input outside reference bounds",
        },
        "support_points": [
            {
                "entry_thickness_mm": raw[0],
                "exit_thickness_mm": raw[1],
                "width_mm": raw[2],
            }
            for raw in points
        ],
    }


@dataclass(frozen=True)
class SpecSupportAssessment:
    status: str
    gate_passed: bool
    coverage_confidence_ceiling: str
    nearest_distance: float
    nearest_specification: dict[str, float]
    outside_axis_fields: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def assess_spec_support(
    entry_thickness_mm: object,
    exit_thickness_mm: object,
    width_mm: object,
    envelope: Mapping[str, Any],
) -> SpecSupportAssessment:
    """Assess one three-input request against a frozen support envelope."""

    raw, vector = _spec_values(entry_thickness_mm, exit_thickness_mm, width_mm)
    if envelope.get("schema_version") != "cold-rolling-spec-support-envelope-v1":
        raise FrameworkConfigurationError("Unsupported specification envelope schema")

    try:
        scales = tuple(
            float(envelope["robust_scales"][name])
            for name in SPEC_SUPPORT_TRANSFORMED_FEATURES
        )
        support_raw = [
            (
                float(point["entry_thickness_mm"]),
                float(point["exit_thickness_mm"]),
                float(point["width_mm"]),
            )
            for point in envelope["support_points"]
        ]
        p90 = float(
            envelope["leave_one_unique_spec_out_nearest_distance"]["p90"]
        )
        p95 = float(
            envelope["leave_one_unique_spec_out_nearest_distance"]["p95"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FrameworkConfigurationError("Malformed specification support envelope") from exc
    if not support_raw:
        raise FrameworkConfigurationError("Specification support envelope is empty")

    support_vectors = [_spec_values(*point)[1] for point in support_raw]
    distances = [
        _scaled_spec_distance(vector, point_vector, scales)
        for point_vector in support_vectors
    ]
    nearest_index = min(range(len(distances)), key=distances.__getitem__)
    nearest_distance = distances[nearest_index]
    nearest_raw = support_raw[nearest_index]

    bounds = envelope.get("raw_bounds", {})
    outside_fields = tuple(
        field
        for field, value in zip(SPEC_SUPPORT_RAW_FIELDS, raw)
        if value < float(bounds[field]["min"]) or value > float(bounds[field]["max"])
    )
    tolerances = envelope.get("exact_match_tolerances", {})
    exact = all(
        math.isclose(
            value,
            reference,
            rel_tol=0.0,
            abs_tol=float(tolerances[tolerance_name]),
        )
        for value, reference, tolerance_name in zip(
            raw,
            nearest_raw,
            ("entry_thickness_mm", "exit_thickness_mm", "width_mm"),
        )
    )

    if outside_fields:
        status, passed, ceiling = "outside_axis_bounds", False, "reject"
    elif exact:
        status, passed, ceiling = "exact_overlap", True, "unchanged"
    elif nearest_distance <= p90:
        status, passed, ceiling = "well_supported", True, "medium"
    elif nearest_distance <= p95:
        status, passed, ceiling = "edge_supported", True, "low"
    else:
        status, passed, ceiling = "sparse_or_outside", False, "reject"

    return SpecSupportAssessment(
        status=status,
        gate_passed=passed,
        coverage_confidence_ceiling=ceiling,
        nearest_distance=nearest_distance,
        nearest_specification={
            "entry_thickness_mm": nearest_raw[0],
            "exit_thickness_mm": nearest_raw[1],
            "width_mm": nearest_raw[2],
        },
        outside_axis_fields=outside_fields,
    )


@dataclass(frozen=True)
class AuditReason:
    code: str
    rule_id: str
    severity: str
    message: str


@dataclass
class SegmentAuditResult:
    candidate_id: str
    dataset: str
    grade: str
    training_pool_candidate: bool
    approved_for_parameter_training: bool
    route_metadata_complete: bool
    route_training_candidate: bool
    risk_training_candidate: bool
    human_approval_required: bool
    metrics: dict[str, Any]
    reasons: list[AuditReason]

    def to_record(self) -> dict[str, Any]:
        """Flatten the result for CSV/JSON output."""

        return {
            "candidate_id": self.candidate_id,
            "dataset": self.dataset,
            "grade": self.grade,
            "training_pool_candidate": self.training_pool_candidate,
            "approved_for_parameter_training": self.approved_for_parameter_training,
            "route_metadata_complete": self.route_metadata_complete,
            "route_training_candidate": self.route_training_candidate,
            "risk_training_candidate": self.risk_training_candidate,
            "human_approval_required": self.human_approval_required,
            "reason_codes": ";".join(reason.code for reason in self.reasons),
            "reason_rule_ids": ";".join(reason.rule_id for reason in self.reasons),
            "reason_messages": " | ".join(reason.message for reason in self.reasons),
            **self.metrics,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return payload


class _AuditBuilder:
    def __init__(self) -> None:
        self.grade = "A"
        self.reasons: list[AuditReason] = []

    def add(
        self,
        *,
        grade: str,
        code: str,
        rule_id: str,
        severity: str,
        message: str,
    ) -> None:
        if grade not in GRADE_RANK:
            raise ValueError(f"Unknown grade {grade}")
        if GRADE_RANK[grade] > GRADE_RANK[self.grade]:
            self.grade = grade
        self.reasons.append(
            AuditReason(
                code=code,
                rule_id=rule_id,
                severity=severity,
                message=message,
            )
        )


CORE_NUMERIC_FIELDS: tuple[str, ...] = (
    "entry_thickness_set_mode",
    "intermediate_thickness_set_mode",
    "exit_thickness_set_mode",
    "width_set_mode",
    "exit_thickness_actual_median",
    "exit_thickness_error_abs_p95",
    "uncoiler_tension_set_median",
    "interstand_tension_set_median",
    "coiler_tension_set_median",
    "tm1_closed_gap_set_median",
    "tm2_closed_gap_set_median",
    "extracted_high_speed_samples_1hz",
)


def audit_segment(
    row: Mapping[str, object],
    *,
    rulebook: Mapping[str, Any] | None = None,
    human_gold_approved: bool = False,
) -> SegmentAuditResult:
    """Grade one stable-segment candidate without treating DAT as truth.

    ``A`` means *gold candidate*, not final approval.  The rulebook deliberately
    requires a process-owner decision before an A row can be used as a parameter
    label.  ``C`` rows remain useful for a future risk/rejection model.
    """

    rulebook = rulebook or load_rulebook()
    builder = _AuditBuilder()
    candidate_id = str(row.get("candidate_id", "")).strip()
    dataset = str(row.get("dataset", "")).strip()
    values = {field: _finite(row.get(field)) for field in CORE_NUMERIC_FIELDS}

    missing = [field for field, value in values.items() if value is None]
    if not candidate_id or not dataset or missing:
        details = []
        if not candidate_id:
            details.append("candidate_id")
        if not dataset:
            details.append("dataset")
        details.extend(missing)
        builder.add(
            grade="D",
            code="MISSING_CORE_DATA",
            rule_id="DATA-STABLE-001",
            severity="error",
            message=f"Missing core fields: {', '.join(details)}",
        )
        return SegmentAuditResult(
            candidate_id=candidate_id,
            dataset=dataset,
            grade="D",
            training_pool_candidate=False,
            approved_for_parameter_training=False,
            route_metadata_complete=False,
            route_training_candidate=False,
            risk_training_candidate=False,
            human_approval_required=False,
            metrics={},
            reasons=builder.reasons,
        )

    h0 = float(values["entry_thickness_set_mode"])
    h1 = float(values["intermediate_thickness_set_mode"])
    h2 = float(values["exit_thickness_set_mode"])
    width = float(values["width_set_mode"])
    samples = float(values["extracted_high_speed_samples_1hz"])
    p95_abs_error = float(values["exit_thickness_error_abs_p95"])
    entry_actual = _finite(row.get("entry_thickness_actual_median"))

    metrics: dict[str, Any] = {
        "f1_true_strain_share": None,
        "exit_error_abs_p95_ratio": None,
        "entry_actual_deviation_ratio": None,
        "uncoiler_unit_tension_kN_per_mm2": None,
        "interstand_unit_tension_kN_per_mm2": None,
        "coiler_unit_tension_kN_per_mm2": None,
        "unit_tension_hierarchy_expected": None,
        "unit_tension_source": None,
        "unit_tension_conversion_max_relative_error": None,
        "tension_actual_set_max_relative_error": None,
        "strip_break_observed": None,
        "drive_fault_observed": None,
        "total_estop_state_min": _finite(row.get("total_estop_state_min")),
        "fire_estop_state_min": _finite(row.get("fire_estop_state_min")),
        "flatness_available": False,
    }

    learning_eligible = str(row.get("learning_eligible", "")).strip().lower()
    if learning_eligible != "true":
        builder.add(
            grade="C",
            code="NOT_STABLE_HIGH_SPEED_TARGET",
            rule_id="DATA-STABLE-001",
            severity="error",
            message="Segment is not marked as a learning-eligible high-speed plateau.",
        )

    minimum_samples = _threshold(rulebook, "minimum_high_speed_samples")
    if samples < minimum_samples:
        builder.add(
            grade="C",
            code="HIGH_SPEED_WINDOW_TOO_SHORT",
            rule_id="DATA-STABLE-001",
            severity="error",
            message=(
                f"Only {samples:.0f} stable samples; provisional minimum is "
                f"{minimum_samples:.0f}."
            ),
        )

    if not h0 > h1 > h2 > 0 or width <= 0:
        builder.add(
            grade="C",
            code="INVALID_THICKNESS_ROUTE",
            rule_id="GEOMETRY-ORDER-001",
            severity="error",
            message=(
                f"Expected entry > intermediate > exit > 0 and positive width; "
                f"received {h0:g}, {h1:g}, {h2:g}, width {width:g}."
            ),
        )
    else:
        share = f1_true_strain_share(h0, h1, h2)
        metrics["f1_true_strain_share"] = share
        share_low = _threshold(rulebook, "f1_true_strain_share_review_low")
        share_high = _threshold(rulebook, "f1_true_strain_share_review_high")
        if share < share_low or share > share_high:
            builder.add(
                grade="B",
                code="F1_STRAIN_SHARE_OUTSIDE_SOFT_PRIOR",
                rule_id="REDUCTION-F1-SHARE-001",
                severity="warning",
                message=(
                    f"F1 true-strain share {share:.3f} is outside the provisional "
                    f"plant soft range {share_low:.2f}-{share_high:.2f}."
                ),
            )

        p95_ratio = p95_abs_error / h2
        metrics["exit_error_abs_p95_ratio"] = p95_ratio
        gold_p95 = _threshold(rulebook, "gold_exit_error_abs_p95_ratio")
        maximum_p95 = _threshold(
            rulebook, "maximum_exit_error_abs_p95_ratio_for_parameter_target"
        )
        if p95_ratio > maximum_p95:
            builder.add(
                grade="C",
                code="EXIT_THICKNESS_VARIATION_TOO_HIGH",
                rule_id="QUALITY-TOLERANCE-001",
                severity="error",
                message=(
                    f"Exit absolute-error P95 is {p95_ratio:.2%} of target, above "
                    f"the provisional parameter-target gate {maximum_p95:.2%}."
                ),
            )
        elif p95_ratio > gold_p95:
            builder.add(
                grade="B",
                code="EXIT_THICKNESS_VARIATION_REVIEW",
                rule_id="QUALITY-TOLERANCE-001",
                severity="warning",
                message=(
                    f"Exit absolute-error P95 is {p95_ratio:.2%} of target, above "
                    f"the provisional gold-candidate gate {gold_p95:.2%}."
                ),
            )

        if entry_actual is not None:
            entry_deviation = abs(entry_actual - h0) / h0
            metrics["entry_actual_deviation_ratio"] = entry_deviation
            review_deviation = _threshold(
                rulebook, "entry_actual_deviation_review_ratio"
            )
            anomaly_deviation = _threshold(
                rulebook, "entry_actual_deviation_anomaly_ratio"
            )
            if entry_deviation > anomaly_deviation:
                builder.add(
                    grade="C",
                    code="ENTRY_SET_ACTUAL_MISMATCH_ANOMALY",
                    rule_id="DATA-STABLE-001",
                    severity="error",
                    message=(
                        f"Entry actual/set mismatch is {entry_deviation:.2%}, above "
                        f"the provisional anomaly gate {anomaly_deviation:.2%}."
                    ),
                )
            elif entry_deviation > review_deviation:
                builder.add(
                    grade="B",
                    code="ENTRY_SET_ACTUAL_MISMATCH_REVIEW",
                    rule_id="DATA-STABLE-001",
                    severity="warning",
                    message=(
                        f"Entry actual/set mismatch is {entry_deviation:.2%}, above "
                        f"the provisional review gate {review_deviation:.2%}."
                    ),
                )

        try:
            uncoiler_q = total_tension_to_unit_tension(
                float(values["uncoiler_tension_set_median"]), width, h0
            )
            interstand_q = total_tension_to_unit_tension(
                float(values["interstand_tension_set_median"]), width, h1
            )
            coiler_q = total_tension_to_unit_tension(
                float(values["coiler_tension_set_median"]), width, h2
            )
            computed_q = (uncoiler_q, interstand_q, coiler_q)
            direct_q = tuple(
                _finite(row.get(field))
                for field in (
                    "uncoiler_unit_tension_dat_median",
                    "interstand_unit_tension_dat_median",
                    "coiler_unit_tension_dat_median",
                )
            )
            if all(value is not None and value >= 0 for value in direct_q):
                used_q = tuple(float(value) for value in direct_q)
                metrics["unit_tension_source"] = "DAT_direct"
                conversion_error = max(
                    abs(direct - computed) / max(abs(direct), 1e-9)
                    for direct, computed in zip(used_q, computed_q)
                )
                metrics[
                    "unit_tension_conversion_max_relative_error"
                ] = conversion_error
                conversion_review = _threshold(
                    rulebook, "unit_tension_conversion_review_ratio"
                )
                conversion_anomaly = _threshold(
                    rulebook, "unit_tension_conversion_anomaly_ratio"
                )
                if conversion_error > conversion_anomaly:
                    builder.add(
                        grade="C",
                        code="UNIT_TENSION_CHANNEL_MISMATCH_ANOMALY",
                        rule_id="TENSION-CHANNEL-CONSISTENCY-001",
                        severity="error",
                        message=(
                            "Direct DAT unit tension differs from the set-value "
                            f"section conversion by as much as {conversion_error:.2%}."
                        ),
                    )
                elif conversion_error > conversion_review:
                    builder.add(
                        grade="B",
                        code="UNIT_TENSION_CHANNEL_MISMATCH_REVIEW",
                        rule_id="TENSION-CHANNEL-CONSISTENCY-001",
                        severity="warning",
                        message=(
                            "Direct DAT unit tension differs from the set-value "
                            f"section conversion by as much as {conversion_error:.2%}."
                        ),
                    )
            else:
                used_q = computed_q
                metrics["unit_tension_source"] = "computed_from_set_values"
                builder.add(
                    grade="B",
                    code="DIRECT_UNIT_TENSION_CHANNELS_MISSING",
                    rule_id="TENSION-CHANNEL-CONSISTENCY-001",
                    severity="warning",
                    message=(
                        "Direct DAT unit-tension channels are incomplete; hierarchy "
                        "was checked only with the explicit conversion formula."
                    ),
                )

            uncoiler_used_q, interstand_used_q, coiler_used_q = used_q
            expected_hierarchy = (
                coiler_used_q > interstand_used_q > uncoiler_used_q
            )
            metrics.update(
                {
                    "uncoiler_unit_tension_kN_per_mm2": uncoiler_used_q,
                    "interstand_unit_tension_kN_per_mm2": interstand_used_q,
                    "coiler_unit_tension_kN_per_mm2": coiler_used_q,
                    "uncoiler_unit_tension_computed_kN_per_mm2": uncoiler_q,
                    "interstand_unit_tension_computed_kN_per_mm2": interstand_q,
                    "coiler_unit_tension_computed_kN_per_mm2": coiler_q,
                    "unit_tension_hierarchy_expected": expected_hierarchy,
                }
            )
            if not expected_hierarchy:
                builder.add(
                    grade="B",
                    code="UNIT_TENSION_HIERARCHY_REVIEW",
                    rule_id="TENSION-UNIT-001",
                    severity="warning",
                    message=(
                        "Section-normalized unit tension does not follow the current "
                        "soft expectation coiler > interstand > uncoiler."
                    ),
                )
        except ValueError as exc:
            builder.add(
                grade="C",
                code="INVALID_TENSION_SECTION",
                rule_id="TENSION-UNIT-001",
                severity="error",
                message=str(exc),
            )

        actual_tensions = tuple(
            _finite(row.get(field))
            for field in (
                "uncoiler_tension_actual_median",
                "interstand_tension_actual_median",
                "coiler_tension_actual_median",
            )
        )
        set_tensions = (
            float(values["uncoiler_tension_set_median"]),
            float(values["interstand_tension_set_median"]),
            float(values["coiler_tension_set_median"]),
        )
        if all(value is not None for value in actual_tensions):
            tracking_error = max(
                abs(float(actual) - set_value) / max(abs(set_value), 1e-9)
                for actual, set_value in zip(actual_tensions, set_tensions)
            )
            metrics["tension_actual_set_max_relative_error"] = tracking_error
            tracking_review = _threshold(
                rulebook, "tension_actual_set_tracking_review_ratio"
            )
            tracking_anomaly = _threshold(
                rulebook, "tension_actual_set_tracking_anomaly_ratio"
            )
            if tracking_error > tracking_anomaly:
                builder.add(
                    grade="C",
                    code="TENSION_TRACKING_ANOMALY",
                    rule_id="TENSION-TRACKING-001",
                    severity="error",
                    message=(
                        "Stable-plateau actual tension differs from its set value by "
                        f"as much as {tracking_error:.2%}."
                    ),
                )
            elif tracking_error > tracking_review:
                builder.add(
                    grade="B",
                    code="TENSION_TRACKING_REVIEW",
                    rule_id="TENSION-TRACKING-001",
                    severity="warning",
                    message=(
                        "Stable-plateau actual tension differs from its set value by "
                        f"as much as {tracking_error:.2%}."
                    ),
                )
        else:
            builder.add(
                grade="B",
                code="ACTUAL_TENSION_CHANNELS_MISSING",
                rule_id="TENSION-TRACKING-001",
                severity="warning",
                message="One or more stable-plateau actual-tension channels are missing.",
            )

    strip_break = _finite(row.get("strip_break_flag_max"))
    drive_no_fault = _finite(row.get("drive_no_fault_state_min"))
    metrics["strip_break_observed"] = (
        strip_break is not None and strip_break > 0.5
    )
    metrics["drive_fault_observed"] = (
        drive_no_fault is not None and drive_no_fault < 0.5
    )
    if strip_break is None or drive_no_fault is None:
        builder.add(
            grade="B",
            code="CONFIRMED_SAFETY_STATE_CHANNELS_MISSING",
            rule_id="SAFETY-STATE-001",
            severity="warning",
            message="Strip-break or drive-no-fault state is missing from the segment.",
        )
    else:
        if strip_break > 0.5:
            builder.add(
                grade="C",
                code="STRIP_BREAK_STATE_OBSERVED",
                rule_id="SAFETY-STATE-001",
                severity="error",
                message="The strip-break flag was active during the selected segment.",
            )
        if drive_no_fault < 0.5:
            builder.add(
                grade="C",
                code="DRIVE_FAULT_STATE_OBSERVED",
                rule_id="SAFETY-STATE-001",
                severity="error",
                message="The drive-no-fault state dropped during the selected segment.",
            )

    flatness_valid_samples = _finite(row.get("flatness_profile_valid_samples"))
    flatness_decoded_zones = _finite(row.get("flatness_decoded_zone_count"))
    flatness_available = bool(
        flatness_valid_samples is not None
        and flatness_valid_samples >= minimum_samples
        and flatness_decoded_zones is not None
        and flatness_decoded_zones >= 5
    )
    metrics["flatness_available"] = flatness_available
    if not flatness_available:
        builder.add(
            grade="B",
            code="FLATNESS_EVIDENCE_INCOMPLETE",
            rule_id="FLATNESS-RUNTIME-001",
            severity="warning",
            message=(
                "Flatness evidence is absent or incomplete. It cannot support a gold "
                "candidate, but does not invalidate the core thickness/tension record."
            ),
        )

    extraction_errors = _finite(row.get("extraction_channel_error_count")) or 0.0
    if extraction_errors > 0:
        builder.add(
            grade="B",
            code="CHANNEL_EXTRACTION_ERRORS_REVIEW",
            rule_id="DATA-STABLE-001",
            severity="warning",
            message=f"The source extraction recorded {extraction_errors:.0f} channel errors.",
        )

    if not builder.reasons:
        builder.add(
            grade="A",
            code="GOLD_CANDIDATE_PENDING_APPROVAL",
            rule_id="DATA-STABLE-001",
            severity="info",
            message="All currently enforceable and provisional audit gates passed.",
        )

    framework_approved = rulebook.get("status") == "approved_for_model_training"
    training_pool_candidate = builder.grade == "A"
    route_metadata_complete = bool(
        str(row.get("is_true_coil_or_pass", "")).strip().lower() == "true"
        and str(row.get("alloy", "")).strip()
        and str(row.get("initial_temper", "")).strip()
        and str(row.get("anneal_state", "")).strip()
        and str(row.get("pass_number", "")).strip()
    )
    approved_for_parameter_training = bool(
        training_pool_candidate and human_gold_approved and framework_approved
    )
    route_training_candidate = bool(
        approved_for_parameter_training and route_metadata_complete
    )
    return SegmentAuditResult(
        candidate_id=candidate_id,
        dataset=dataset,
        grade=builder.grade,
        training_pool_candidate=training_pool_candidate,
        approved_for_parameter_training=approved_for_parameter_training,
        route_metadata_complete=route_metadata_complete,
        route_training_candidate=route_training_candidate,
        risk_training_candidate=builder.grade == "C",
        human_approval_required=builder.grade in {"A", "B"},
        metrics=metrics,
        reasons=builder.reasons,
    )


def _missing_production_limits(library: Mapping[str, Any]) -> list[str]:
    limits = library.get("plant_equipment_limits", {})
    return sorted(
        key
        for key, value in limits.items()
        if key != "approved_for_production" and value is None
    )


def _route_pass(
    *,
    pass_number: int,
    entry_mm: float,
    exit_mm: float,
    f1_share: float,
    cumulative_before: float | None,
    anneal_before: str | None,
) -> dict[str, Any]:
    pass_true_strain = math.log(entry_mm / exit_mm)
    intermediate_mm = entry_mm / math.exp(f1_share * pass_true_strain)
    f1_reduction = 1.0 - intermediate_mm / entry_mm
    f2_reduction = 1.0 - exit_mm / intermediate_mm
    cumulative_after = (
        cumulative_before + pass_true_strain
        if cumulative_before is not None
        else None
    )
    return {
        "pass_number": pass_number,
        "anneal_before": anneal_before,
        "entry_thickness_mm": entry_mm,
        "intermediate_thickness_mm": intermediate_mm,
        "exit_thickness_mm": exit_mm,
        "pass_true_strain": pass_true_strain,
        "f1_true_strain_share": f1_share,
        "f1_reduction_ratio": f1_reduction,
        "f2_reduction_ratio": f2_reduction,
        "cumulative_true_strain_before": cumulative_before,
        "cumulative_true_strain_after": cumulative_after,
        "material_state_after": "WORK_HARDENED_UNSPECIFIED",
    }


def plan_route(
    request: Mapping[str, object],
    *,
    mode: str = "shadow",
    material_library: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create deterministic multi-pass candidates under explicit governance.

    Shadow mode may use the observed 260712-260719 envelope to exercise the route
    state machine.  Production mode refuses to generate a route until every plant
    limit and material/anneal route has been approved.
    """

    if mode not in {"shadow", "production"}:
        raise ValueError("mode must be 'shadow' or 'production'")
    library = material_library or load_material_library()

    entry = _finite(request.get("entry_thickness_mm"))
    exit_target = _finite(request.get("target_exit_thickness_mm"))
    width = _finite(request.get("width_mm"))
    if entry is None or exit_target is None or width is None:
        raise RouteInputError("entry, target exit and width must be finite")
    if not entry > exit_target > 0 or width <= 0:
        raise RouteInputError("Expected entry > target exit > 0 and positive width")

    alloy = str(request.get("alloy", "UNKNOWN")).strip().upper() or "UNKNOWN"
    temper = str(request.get("initial_temper", "UNKNOWN")).strip().upper() or "UNKNOWN"
    anneal_state = (
        str(request.get("anneal_state", "unknown")).strip().lower() or "unknown"
    )
    allowed_anneal_states = {
        "fully_annealed",
        "work_hardened",
        "partially_annealed",
        "unknown",
    }
    if anneal_state not in allowed_anneal_states:
        raise RouteInputError(
            f"anneal_state must be one of {', '.join(sorted(allowed_anneal_states))}"
        )

    missing_limits = _missing_production_limits(library)
    material = library.get("materials", {}).get(alloy)
    blockers: list[str] = []
    if library.get("status") != "approved_for_production":
        blockers.append("material route library is not approved for production")
    if not library.get("plant_equipment_limits", {}).get("approved_for_production"):
        blockers.append("plant equipment limits are not approved")
    if missing_limits:
        blockers.append(
            "missing plant limits: " + ", ".join(missing_limits)
        )
    if material is None:
        blockers.append(f"alloy {alloy} is not in the material route library")
    elif material.get("status") != "approved_for_production":
        blockers.append(f"alloy {alloy} route is not approved for production")
    elif temper not in set(material.get("allowed_input_tempers", ())):
        blockers.append(
            f"temper {temper} is not approved for alloy {alloy} in the route library"
        )
    if temper == "UNKNOWN":
        blockers.append("initial temper is unknown")
    if anneal_state == "unknown":
        blockers.append("anneal state is unknown")

    if mode == "production" and blockers:
        return {
            "framework_version": library.get("library_version"),
            "mode": mode,
            "status": "blocked_incomplete_engineering_approval",
            "production_releasable": False,
            "blockers": blockers,
            "candidates": [],
        }

    defaults = library["shadow_defaults_from_current_high_speed_dat"]
    max_true_strain = _finite(
        request.get(
            "maximum_true_strain_per_tandem_pass",
            defaults.get("maximum_true_strain_per_tandem_pass"),
        )
    )
    target_true_strain = _finite(
        request.get(
            "target_true_strain_per_tandem_pass",
            defaults.get("target_true_strain_per_tandem_pass"),
        )
    )
    f1_share = _finite(
        request.get("f1_true_strain_share", defaults.get("f1_true_strain_share"))
    )
    if (
        max_true_strain is None
        or max_true_strain <= 0
        or target_true_strain is None
        or target_true_strain <= 0
        or f1_share is None
        or not 0 < f1_share < 1
    ):
        raise FrameworkConfigurationError("Invalid shadow route defaults")

    total_true_strain = math.log(entry / exit_target)
    minimum_pass_count = max(1, math.ceil(total_true_strain / max_true_strain))
    target_pass_count = max(1, math.ceil(total_true_strain / target_true_strain))
    max_passes = int(_finite(request.get("max_passes")) or 8)
    if minimum_pass_count > max_passes:
        return {
            "framework_version": library.get("library_version"),
            "mode": mode,
            "status": "no_candidate_within_pass_count_limit",
            "production_releasable": False,
            "blockers": blockers,
            "candidates": [],
        }

    pass_counts = sorted(
        {
            count
            for count in (
                minimum_pass_count,
                target_pass_count,
                target_pass_count + 1,
            )
            if minimum_pass_count <= count <= max_passes
        }
    )

    provided_cumulative = _finite(request.get("initial_cumulative_true_strain"))
    reference_annealed_thickness = _finite(
        request.get("reference_annealed_thickness_mm")
    )
    if provided_cumulative is not None and provided_cumulative < 0:
        raise RouteInputError("initial cumulative true strain cannot be negative")
    if reference_annealed_thickness is not None:
        if reference_annealed_thickness < entry:
            raise RouteInputError(
                "reference annealed thickness cannot be below current entry thickness"
            )
        initial_cumulative: float | None = math.log(
            reference_annealed_thickness / entry
        )
    elif provided_cumulative is not None:
        initial_cumulative = provided_cumulative
    elif anneal_state == "fully_annealed":
        initial_cumulative = 0.0
    else:
        initial_cumulative = None

    material_anneal_limit = (
        material.get("maximum_cumulative_true_strain_before_full_anneal")
        if material
        else None
    )
    anneal_limit = _finite(
        request.get(
            "maximum_cumulative_true_strain_before_full_anneal",
            material_anneal_limit,
        )
    )
    if anneal_limit is not None and anneal_limit <= 0:
        raise RouteInputError("anneal true-strain limit must be positive")

    candidates: list[dict[str, Any]] = []
    production_releasable = mode == "production" and not blockers
    for pass_count in pass_counts:
        per_pass_strain = total_true_strain / pass_count
        if per_pass_strain > max_true_strain + 1e-12:
            continue
        passes: list[dict[str, Any]] = []
        cumulative = initial_cumulative
        anneal_count = 0
        invalid_reason: str | None = None
        for index in range(pass_count):
            pass_entry = entry * math.exp(-per_pass_strain * index)
            pass_exit = (
                exit_target
                if index == pass_count - 1
                else entry * math.exp(-per_pass_strain * (index + 1))
            )
            anneal_before: str | None = None
            if anneal_limit is not None:
                if per_pass_strain > anneal_limit + 1e-12:
                    invalid_reason = (
                        "one tandem pass exceeds the configured cumulative-strain "
                        "limit even immediately after full anneal"
                    )
                    break
                if cumulative is None:
                    invalid_reason = (
                        "initial cumulative strain is unknown, so the configured "
                        "anneal limit cannot be checked"
                    )
                    break
                if cumulative + per_pass_strain > anneal_limit + 1e-12:
                    anneal_before = "FULL_ANNEAL_REQUIRED_BY_CONFIGURED_LIMIT"
                    cumulative = 0.0
                    anneal_count += 1

            route_pass = _route_pass(
                pass_number=index + 1,
                entry_mm=pass_entry,
                exit_mm=pass_exit,
                f1_share=f1_share,
                cumulative_before=cumulative,
                anneal_before=anneal_before,
            )
            cumulative = route_pass["cumulative_true_strain_after"]
            passes.append(route_pass)

        if invalid_reason:
            continue
        review_reasons = list(blockers)
        if anneal_limit is None:
            review_reasons.append(
                "anneal placement is pending an approved alloy/temper cumulative-strain limit"
            )
        if initial_cumulative is None:
            review_reasons.append(
                "initial cumulative cold-work state is unknown"
            )
        score = (
            10.0 * pass_count
            + 5.0 * anneal_count
            + abs(per_pass_strain - target_true_strain)
        )
        candidates.append(
            {
                "candidate_rank": 0,
                "status": (
                    "production_candidate_requires_human_approval"
                    if production_releasable
                    else "shadow_candidate_requires_engineering_review"
                ),
                "production_releasable": production_releasable,
                "automatic_plc_write": False,
                "human_approval_required": True,
                "pass_count": pass_count,
                "anneal_count": anneal_count,
                "total_true_strain": total_true_strain,
                "true_strain_per_pass": per_pass_strain,
                "score_lower_is_preferred": score,
                "passes": passes,
                "review_reasons": sorted(set(review_reasons)),
            }
        )

    candidates.sort(key=lambda item: item["score_lower_is_preferred"])
    for rank, candidate in enumerate(candidates, start=1):
        candidate["candidate_rank"] = rank

    return {
        "framework_version": library.get("library_version"),
        "mode": mode,
        "status": (
            (
                "production_candidates_generated_human_approval_required"
                if production_releasable
                else "shadow_candidates_generated"
            )
            if candidates
            else "no_candidate_after_material_state_checks"
        ),
        "production_releasable": production_releasable,
        "automatic_plc_write": False,
        "human_approval_required": True,
        "input": {
            "entry_thickness_mm": entry,
            "target_exit_thickness_mm": exit_target,
            "width_mm": width,
            "alloy": alloy,
            "initial_temper": temper,
            "anneal_state": anneal_state,
            "initial_cumulative_true_strain": initial_cumulative,
        },
        "blockers": sorted(set(blockers)),
        "candidates": candidates,
    }


def audit_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    rulebook: Mapping[str, Any] | None = None,
) -> list[SegmentAuditResult]:
    """Audit a row sequence with one validated rulebook instance."""

    rulebook = rulebook or load_rulebook()
    return [audit_segment(row, rulebook=rulebook) for row in rows]
