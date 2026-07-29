from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class OrmModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ProductOut(OrmModel):
    id: str
    code: str
    name: str
    input_alloy: str
    nominal_alloy: str
    temper: str
    target_thickness: float
    target_width: float
    lower_tolerance: float
    upper_tolerance: float
    process_target_tolerance: float
    purpose: str
    review_status: str
    source_notes: str


class BatchOut(OrmModel):
    id: str
    batch_no: str
    product_id: str | None
    cast_coil_no: str
    dat_anchor_time: str
    cast_spec: str
    intermediate_spec: str
    finished_spec: str
    chemistry: dict[str, Any]
    status: str
    ocr_confirmed: bool
    source_notes: str


class TransversePlatePoint(BaseModel):
    point: int = Field(ge=1, le=99)
    value: str = Field(default="", max_length=40)


class LongitudinalPlatePoint(BaseModel):
    position_mm: int = Field(ge=0, le=100000)
    drive_side: str = Field(default="", max_length=40)
    operator_side: str = Field(default="", max_length=40)


class ProcessStageBase(BaseModel):
    operator_name: str = Field(default="", max_length=80)
    inspector_name: str = Field(default="", max_length=80)
    record_date: str = Field(default="", max_length=40)


class QualityStageBase(ProcessStageBase):
    quality_status: str = Field(default="", max_length=1000)
    self_check: str = Field(default="", max_length=40)
    special_check: str = Field(default="", max_length=40)


class ColdRollingStage(QualityStageBase):
    after_coil_weight: str = Field(default="", max_length=40)
    after_thickness: str = Field(default="", max_length=40)
    after_width: str = Field(default="", max_length=40)


class RewindingStage(ProcessStageBase):
    cut_width: str = Field(default="", max_length=40)
    split_status: str = Field(default="", max_length=500)
    weight: str = Field(default="", max_length=40)
    quality_status: str = Field(default="", max_length=1000)
    notes: str = Field(default="", max_length=1000)


class CrossCuttingStage(QualityStageBase):
    cut_total_weight: str = Field(default="", max_length=40)
    sheet_specification: str = Field(default="", max_length=160)


class StraighteningStage(QualityStageBase):
    straightened_total_weight: str = Field(default="", max_length=40)
    straightened_thickness: str = Field(default="", max_length=40)
    straightened_width: str = Field(default="", max_length=40)


class AnnealingStage(ProcessStageBase):
    charging_time: str = Field(default="", max_length=80)
    schedule_code: str = Field(default="", max_length=100)
    furnace_load: str = Field(default="", max_length=40)
    discharging_time: str = Field(default="", max_length=80)
    final_metal_temperature: str = Field(default="", max_length=40)
    notes: str = Field(default="", max_length=1000)


class PackagingStage(QualityStageBase):
    finished_weight: str = Field(default="", max_length=40)
    specification: str = Field(default="", max_length=160)


class ProcessStageRecords(BaseModel):
    cold_rolling: ColdRollingStage = Field(default_factory=ColdRollingStage)
    rewinding: RewindingStage = Field(default_factory=RewindingStage)
    cross_cutting: CrossCuttingStage = Field(default_factory=CrossCuttingStage)
    straightening: StraighteningStage = Field(default_factory=StraighteningStage)
    annealing: AnnealingStage = Field(default_factory=AnnealingStage)
    packaging: PackagingStage = Field(default_factory=PackagingStage)


class ProductionCardCreate(BaseModel):
    batch_no: str = Field(min_length=1, max_length=80)
    card_date: str = ""
    cast_coil_no: str = ""
    input_alloy: str = ""
    nominal_alloy: str = ""
    temper: str = ""
    input_quantity: int = Field(default=1, ge=1, le=999)
    cast_thickness: float | None = Field(default=None, gt=0, le=20)
    cast_width: float | None = Field(default=None, gt=0, le=5000)
    intermediate_thickness: float | None = Field(default=None, gt=0, le=20)
    intermediate_width: float | None = Field(default=None, gt=0, le=5000)
    trimmed_width: float | None = Field(default=None, gt=0, le=5000)
    target_thickness: float | None = Field(default=None, gt=0, le=20)
    target_width: float | None = Field(default=None, gt=0, le=5000)
    thickness_lower_tolerance: float | None = Field(default=None, ge=0, le=1)
    thickness_upper_tolerance: float | None = Field(default=None, ge=0, le=1)
    width_lower_tolerance: float | None = Field(default=None, ge=0, le=100)
    width_upper_tolerance: float | None = Field(default=None, ge=0, le=100)
    purpose: str = ""
    process_requirements: str = ""
    chemistry: dict[str, Any] = Field(default_factory=dict)
    transverse_plate_diff: list[TransversePlatePoint] = Field(default_factory=list, max_length=99)
    longitudinal_plate_diff: list[LongitudinalPlatePoint] = Field(default_factory=list, max_length=100)
    surface_quality: str = Field(default="", max_length=2000)
    process_stage_records: ProcessStageRecords = Field(default_factory=ProcessStageRecords)
    review_status: str = "draft"
    created_by: str = ""
    notes: str = ""


class ProductionCardUpdate(BaseModel):
    batch_no: str | None = None
    card_date: str | None = None
    cast_coil_no: str | None = None
    input_alloy: str | None = None
    nominal_alloy: str | None = None
    temper: str | None = None
    input_quantity: int | None = Field(default=None, ge=1, le=999)
    cast_thickness: float | None = Field(default=None, gt=0, le=20)
    cast_width: float | None = Field(default=None, gt=0, le=5000)
    intermediate_thickness: float | None = Field(default=None, gt=0, le=20)
    intermediate_width: float | None = Field(default=None, gt=0, le=5000)
    trimmed_width: float | None = Field(default=None, gt=0, le=5000)
    target_thickness: float | None = Field(default=None, gt=0, le=20)
    target_width: float | None = Field(default=None, gt=0, le=5000)
    thickness_lower_tolerance: float | None = Field(default=None, ge=0, le=1)
    thickness_upper_tolerance: float | None = Field(default=None, ge=0, le=1)
    width_lower_tolerance: float | None = Field(default=None, ge=0, le=100)
    width_upper_tolerance: float | None = Field(default=None, ge=0, le=100)
    purpose: str | None = None
    process_requirements: str | None = None
    chemistry: dict[str, Any] | None = None
    transverse_plate_diff: list[TransversePlatePoint] | None = Field(default=None, max_length=99)
    longitudinal_plate_diff: list[LongitudinalPlatePoint] | None = Field(default=None, max_length=100)
    surface_quality: str | None = Field(default=None, max_length=2000)
    process_stage_records: ProcessStageRecords | None = None
    review_status: str | None = None
    created_by: str | None = None
    notes: str | None = None


class ProductionCardOut(OrmModel):
    id: str
    batch_no: str
    card_date: str
    cast_coil_no: str
    input_alloy: str
    nominal_alloy: str
    temper: str
    input_quantity: int
    cast_thickness: float | None
    cast_width: float | None
    intermediate_thickness: float | None
    intermediate_width: float | None
    trimmed_width: float | None
    target_thickness: float | None
    target_width: float | None
    thickness_lower_tolerance: float | None
    thickness_upper_tolerance: float | None
    width_lower_tolerance: float | None
    width_upper_tolerance: float | None
    purpose: str
    process_requirements: str
    chemistry: dict[str, Any]
    transverse_plate_diff: list[TransversePlatePoint]
    longitudinal_plate_diff: list[LongitudinalPlatePoint]
    surface_quality: str
    process_stage_records: ProcessStageRecords
    review_status: str
    created_by: str
    notes: str
    created_at: Any
    updated_at: Any


class PassCreate(BaseModel):
    batch_id: str | None = None
    coil_no: str = Field(min_length=1, max_length=100)
    pass_no: int = Field(default=1, ge=1, le=99)
    entry_thickness: float = Field(gt=0, le=20)
    target_exit_thickness: float = Field(gt=0, le=20)
    incoming_width: float = Field(gt=0, le=5000)
    operator_name: str = ""
    assistant_name: str = ""
    started_at: str = ""
    initial_settings: dict[str, Any] = Field(default_factory=dict)
    notes: str = ""


class PassOut(OrmModel):
    id: str
    batch_id: str | None
    coil_no: str
    pass_no: int
    entry_thickness: float
    target_exit_thickness: float
    incoming_width: float
    operator_name: str
    assistant_name: str
    status: str
    started_at: str
    high_speed_at: str
    ended_at: str
    final_thickness: float | None
    quality_result: str
    dat_start_time: str
    dat_end_time: str
    initial_settings: dict[str, Any]
    notes: str


class SampleCreate(BaseModel):
    sampled_at: str
    entered_at: str = ""
    manual_thickness: float = Field(gt=0, le=20)
    laser_thickness: float | None = Field(default=None, gt=0, le=20)
    supplement_percent: float | None = Field(default=None, ge=-50, le=50)
    manual_width: float | None = Field(default=None, gt=0, le=5000)
    sample_position: str = "single_point"
    measurer: str = ""
    instrument: str = ""
    is_valid: bool = True
    is_retest: bool = False


class SampleOut(OrmModel):
    id: str
    pass_id: str
    sequence_no: int
    sampled_at: str
    entered_at: str
    manual_thickness: float
    laser_thickness: float | None
    supplement_percent: float | None
    manual_width: float | None
    sample_position: str
    measurer: str
    instrument: str
    is_valid: bool
    is_retest: bool


class AdjustmentOut(OrmModel):
    id: str
    pass_id: str
    adjusted_at: str
    parameter_code: str
    parameter_label: str
    before_value: float | None
    after_value: float | None
    unit: str
    reason: str
    operator_name: str
    source: str


class EventOut(OrmModel):
    id: str
    pass_id: str
    event_type: str
    occurred_at: str
    notes: str


class PassDetailOut(BaseModel):
    rolling_pass: PassOut = Field(alias="pass")
    samples: list[SampleOut]
    adjustments: list[AdjustmentOut]
    events: list[EventOut]

    model_config = ConfigDict(populate_by_name=True)


class AdjustmentCreate(BaseModel):
    adjusted_at: str
    parameter_code: str
    parameter_label: str
    before_value: float | None = None
    after_value: float | None = None
    unit: str = ""
    reason: str = ""
    operator_name: str = ""
    source: str = "manual"


class EventCreate(BaseModel):
    event_type: str
    occurred_at: str
    notes: str = ""


class PassComplete(BaseModel):
    ended_at: str
    final_thickness: float | None = None
    quality_result: str
    notes: str = ""


class RecommendationRequest(BaseModel):
    product_id: str | None = None
    card_id: str | None = None
    batch_no: str = ""
    coil_no: str = ""
    pass_no: int = Field(default=1, ge=1, le=99)
    input_alloy: str = ""
    nominal_alloy: str = ""
    temper: str = ""
    entry_thickness: float = Field(gt=0, le=20)
    entry_actual_thickness: float | None = Field(default=None, gt=0, le=20)
    target_exit_thickness: float = Field(gt=0, le=20)
    incoming_width: float = Field(gt=0, le=5000)
    requested_by: str = ""


class RecommendationOut(OrmModel):
    id: str
    status: str
    confidence_score: float
    model_version: str
    request_payload: dict[str, Any]
    result_payload: dict[str, Any]
