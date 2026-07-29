from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def new_id() -> str:
    return str(uuid4())


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Product(Base):
    __tablename__ = "products"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    input_alloy: Mapped[str] = mapped_column(String(40), default="")
    nominal_alloy: Mapped[str] = mapped_column(String(40), default="")
    temper: Mapped[str] = mapped_column(String(40), default="")
    target_thickness: Mapped[float] = mapped_column(Float)
    target_width: Mapped[float] = mapped_column(Float)
    lower_tolerance: Mapped[float] = mapped_column(Float, default=0.005)
    upper_tolerance: Mapped[float] = mapped_column(Float, default=0.005)
    process_target_tolerance: Mapped[float] = mapped_column(Float, default=0.005)
    purpose: Mapped[str] = mapped_column(String(160), default="")
    review_status: Mapped[str] = mapped_column(String(40), default="pending_review")
    source_notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    batches: Mapped[list["ProductionBatch"]] = relationship(back_populates="product")


class ProductionBatch(Base):
    __tablename__ = "production_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    batch_no: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    product_id: Mapped[str | None] = mapped_column(ForeignKey("products.id"), nullable=True)
    cast_coil_no: Mapped[str] = mapped_column(String(100), default="")
    dat_anchor_time: Mapped[str] = mapped_column(String(80), default="")
    cast_spec: Mapped[str] = mapped_column(String(120), default="")
    intermediate_spec: Mapped[str] = mapped_column(String(120), default="")
    finished_spec: Mapped[str] = mapped_column(String(120), default="")
    chemistry: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(40), default="waiting_dat_link")
    ocr_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    source_notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    product: Mapped[Product | None] = relationship(back_populates="batches")
    passes: Mapped[list["RollingPass"]] = relationship(back_populates="batch")


class ProductionCard(Base):
    __tablename__ = "production_cards"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    batch_no: Mapped[str] = mapped_column(String(80), index=True)
    card_date: Mapped[str] = mapped_column(String(40), default="")
    cast_coil_no: Mapped[str] = mapped_column(String(100), default="")
    input_alloy: Mapped[str] = mapped_column(String(40), default="")
    nominal_alloy: Mapped[str] = mapped_column(String(40), default="")
    temper: Mapped[str] = mapped_column(String(40), default="")
    input_quantity: Mapped[int] = mapped_column(Integer, default=1)
    cast_thickness: Mapped[float | None] = mapped_column(Float, nullable=True)
    cast_width: Mapped[float | None] = mapped_column(Float, nullable=True)
    intermediate_thickness: Mapped[float | None] = mapped_column(Float, nullable=True)
    intermediate_width: Mapped[float | None] = mapped_column(Float, nullable=True)
    trimmed_width: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_thickness: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_width: Mapped[float | None] = mapped_column(Float, nullable=True)
    thickness_lower_tolerance: Mapped[float | None] = mapped_column(Float, nullable=True)
    thickness_upper_tolerance: Mapped[float | None] = mapped_column(Float, nullable=True)
    width_lower_tolerance: Mapped[float | None] = mapped_column(Float, nullable=True)
    width_upper_tolerance: Mapped[float | None] = mapped_column(Float, nullable=True)
    purpose: Mapped[str] = mapped_column(String(200), default="")
    process_requirements: Mapped[str] = mapped_column(Text, default="")
    chemistry: Mapped[dict] = mapped_column(JSON, default=dict)
    transverse_plate_diff: Mapped[list] = mapped_column(JSON, default=list)
    longitudinal_plate_diff: Mapped[list] = mapped_column(JSON, default=list)
    surface_quality: Mapped[str] = mapped_column(Text, default="")
    process_stage_records: Mapped[dict] = mapped_column(JSON, default=dict)
    review_status: Mapped[str] = mapped_column(String(40), default="draft")
    created_by: Mapped[str] = mapped_column(String(80), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

class RollingPass(Base):
    __tablename__ = "rolling_passes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    batch_id: Mapped[str | None] = mapped_column(ForeignKey("production_batches.id"), nullable=True)
    coil_no: Mapped[str] = mapped_column(String(100), index=True)
    pass_no: Mapped[int] = mapped_column(Integer, default=1)
    entry_thickness: Mapped[float] = mapped_column(Float)
    target_exit_thickness: Mapped[float] = mapped_column(Float)
    incoming_width: Mapped[float] = mapped_column(Float)
    operator_name: Mapped[str] = mapped_column(String(80), default="")
    assistant_name: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(40), default="draft")
    started_at: Mapped[str] = mapped_column(String(80), default="")
    high_speed_at: Mapped[str] = mapped_column(String(80), default="")
    ended_at: Mapped[str] = mapped_column(String(80), default="")
    final_thickness: Mapped[float | None] = mapped_column(Float, nullable=True)
    quality_result: Mapped[str] = mapped_column(String(40), default="pending")
    dat_start_time: Mapped[str] = mapped_column(String(80), default="")
    dat_end_time: Mapped[str] = mapped_column(String(80), default="")
    initial_settings: Mapped[dict] = mapped_column(JSON, default=dict)
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    batch: Mapped[ProductionBatch | None] = relationship(back_populates="passes")
    samples: Mapped[list["ThicknessSample"]] = relationship(back_populates="rolling_pass", cascade="all, delete-orphan")
    adjustments: Mapped[list["ParameterAdjustment"]] = relationship(back_populates="rolling_pass", cascade="all, delete-orphan")
    events: Mapped[list["PassEvent"]] = relationship(back_populates="rolling_pass", cascade="all, delete-orphan")


class ThicknessSample(Base):
    __tablename__ = "thickness_samples"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pass_id: Mapped[str] = mapped_column(ForeignKey("rolling_passes.id"), index=True)
    sequence_no: Mapped[int] = mapped_column(Integer)
    sampled_at: Mapped[str] = mapped_column(String(80))
    entered_at: Mapped[str] = mapped_column(String(80), default="")
    manual_thickness: Mapped[float] = mapped_column(Float)
    laser_thickness: Mapped[float | None] = mapped_column(Float, nullable=True)
    supplement_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    manual_width: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_position: Mapped[str] = mapped_column(String(60), default="single_point")
    measurer: Mapped[str] = mapped_column(String(80), default="")
    instrument: Mapped[str] = mapped_column(String(100), default="")
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    is_retest: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    rolling_pass: Mapped[RollingPass] = relationship(back_populates="samples")


class ParameterAdjustment(Base):
    __tablename__ = "parameter_adjustments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pass_id: Mapped[str] = mapped_column(ForeignKey("rolling_passes.id"), index=True)
    adjusted_at: Mapped[str] = mapped_column(String(80))
    parameter_code: Mapped[str] = mapped_column(String(100))
    parameter_label: Mapped[str] = mapped_column(String(120))
    before_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    after_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str] = mapped_column(String(40), default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    operator_name: Mapped[str] = mapped_column(String(80), default="")
    source: Mapped[str] = mapped_column(String(40), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    rolling_pass: Mapped[RollingPass] = relationship(back_populates="adjustments")


class PassEvent(Base):
    __tablename__ = "pass_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pass_id: Mapped[str] = mapped_column(ForeignKey("rolling_passes.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(80))
    occurred_at: Mapped[str] = mapped_column(String(80))
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    rolling_pass: Mapped[RollingPass] = relationship(back_populates="events")


class DatFile(Base):
    __tablename__ = "dat_files"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    filename: Mapped[str] = mapped_column(String(255), index=True)
    file_path: Mapped[str] = mapped_column(Text)
    file_sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    start_time: Mapped[str] = mapped_column(String(80), default="")
    end_time: Mapped[str] = mapped_column(String(80), default="")
    sample_interval: Mapped[float | None] = mapped_column(Float, nullable=True)
    frames: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channel_count: Mapped[int] = mapped_column(Integer, default=0)
    schema_hash: Mapped[str] = mapped_column(String(40), default="")
    quality_status: Mapped[str] = mapped_column(String(40), default="ok")
    error_message: Mapped[str] = mapped_column(Text, default="")
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class HistoricalCandidate(Base):
    __tablename__ = "historical_candidates"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    candidate_id: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    start_time: Mapped[str] = mapped_column(String(80))
    end_time: Mapped[str] = mapped_column(String(80))
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence_label: Mapped[str] = mapped_column(String(40), default="low")
    entry_thickness: Mapped[float | None] = mapped_column(Float, nullable=True)
    intermediate_thickness: Mapped[float | None] = mapped_column(Float, nullable=True)
    exit_thickness: Mapped[float | None] = mapped_column(Float, nullable=True)
    width: Mapped[float | None] = mapped_column(Float, nullable=True)
    total_reduction_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    active_seconds: Mapped[int] = mapped_column(Integer, default=0)
    stable_seconds: Mapped[int] = mapped_column(Integer, default=0)
    settings: Mapped[dict] = mapped_column(JSON, default=dict)
    source_files: Mapped[str] = mapped_column(Text, default="")


class Recommendation(Base):
    __tablename__ = "recommendations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    requested_by: Mapped[str] = mapped_column(String(80), default="")
    status: Mapped[str] = mapped_column(String(40))
    confidence_score: Mapped[float] = mapped_column(Float, default=0)
    model_version: Mapped[str] = mapped_column(String(80), default="rules-similar-v0.1")
    request_payload: Mapped[dict] = mapped_column(JSON)
    result_payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor: Mapped[str] = mapped_column(String(80), default="system")
    action: Mapped[str] = mapped_column(String(120))
    entity_type: Mapped[str] = mapped_column(String(80))
    entity_id: Mapped[str] = mapped_column(String(80), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
