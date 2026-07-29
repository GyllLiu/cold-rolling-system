from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .database import Base, SessionLocal, engine, ensure_schema_compatibility, get_db
from .dat_import import dataset_foundation_status, scan_dat_directory
from .models import (
    AuditLog,
    DatFile,
    HistoricalCandidate,
    ParameterAdjustment,
    PassEvent,
    Product,
    ProductionCard,
    ProductionBatch,
    Recommendation,
    RollingPass,
    ThicknessSample,
)
from .recommendation import build_recommendation, get_model_metadata
from .schemas import (
    AdjustmentCreate,
    AdjustmentOut,
    BatchOut,
    EventCreate,
    EventOut,
    PassComplete,
    PassCreate,
    PassDetailOut,
    PassOut,
    ProductOut,
    ProductionCardCreate,
    ProductionCardOut,
    ProductionCardUpdate,
    RecommendationOut,
    RecommendationRequest,
    SampleCreate,
    SampleOut,
)
from .seed import seed_all


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    ensure_schema_compatibility(engine)
    with SessionLocal() as db:
        seed_all(db)
    yield


app = FastAPI(title="冷二联轧参数建议与记录系统", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[0-1])\.\d+\.\d+)(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def audit(db: Session, action: str, entity_type: str, entity_id: str, detail: dict, actor: str = "system") -> None:
    db.add(AuditLog(actor=actor or "system", action=action, entity_type=entity_type, entity_id=entity_id, detail=detail))


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok", "service": "cold-rolling-local", "version": "0.1.0"}


@app.get("/api/dashboard")
def dashboard(db: Session = Depends(get_db)) -> dict:
    products = db.scalar(select(func.count()).select_from(Product)) or 0
    batches = db.scalar(select(func.count()).select_from(ProductionBatch)) or 0
    cards = db.scalar(select(func.count()).select_from(ProductionCard)) or 0
    passes = db.scalar(select(func.count()).select_from(RollingPass)) or 0
    samples = db.scalar(select(func.count()).select_from(ThicknessSample)) or 0
    dat_files = db.scalar(select(func.count()).select_from(DatFile)) or 0
    candidates = db.scalar(select(func.count()).select_from(HistoricalCandidate)) or 0
    model = get_model_metadata()
    pending_products = db.scalar(select(func.count()).select_from(Product).where(Product.review_status != "approved")) or 0
    pending_cards = db.scalar(select(func.count()).select_from(ProductionCard).where(ProductionCard.review_status != "approved")) or 0
    pending_reviews = pending_products + pending_cards
    active_passes = db.scalar(select(func.count()).select_from(RollingPass).where(RollingPass.status.in_(["started", "adjusting", "high_speed"]))) or 0
    recent_recommendations = list(db.scalars(select(Recommendation).order_by(Recommendation.created_at.desc()).limit(5)).all())
    return {
        "counts": {
            "products": products,
            "batches": batches,
            "cards": cards,
            "passes": passes,
            "samples": samples,
            "dat_files": dat_files,
            "historical_candidates": max(candidates, model.get("training_case_count", 0)),
            "pending_reviews": pending_reviews,
            "active_passes": active_passes,
        },
        "recent_recommendations": [
            {
                "id": item.id,
                "status": item.status,
                "confidence_score": item.confidence_score,
                "created_at": item.created_at.isoformat(),
                "request": item.request_payload,
            }
            for item in recent_recommendations
        ],
        "model": model,
    }


@app.get("/api/model/status")
def model_status() -> dict:
    return get_model_metadata()


@app.get("/api/products", response_model=list[ProductOut])
def products(db: Session = Depends(get_db)):
    return list(db.scalars(select(Product).order_by(Product.target_thickness.desc())).all())


@app.get("/api/batches", response_model=list[BatchOut])
def batches(db: Session = Depends(get_db)):
    return list(db.scalars(select(ProductionBatch).order_by(ProductionBatch.dat_anchor_time)).all())


@app.get("/api/cards", response_model=list[ProductionCardOut])
def cards(db: Session = Depends(get_db)):
    return list(db.scalars(select(ProductionCard).order_by(ProductionCard.created_at.desc())).all())


@app.post("/api/cards", response_model=ProductionCardOut)
def create_card(payload: ProductionCardCreate, db: Session = Depends(get_db)):
    item = ProductionCard(**payload.model_dump())
    db.add(item)
    db.flush()
    audit(db, "create_card", "production_card", item.id, {"batch_no": item.batch_no}, payload.created_by)
    db.commit()
    db.refresh(item)
    return item


@app.patch("/api/cards/{card_id}", response_model=ProductionCardOut)
def update_card(card_id: str, payload: ProductionCardUpdate, db: Session = Depends(get_db)):
    item = db.get(ProductionCard, card_id)
    if not item:
        raise HTTPException(404, "生产卡片不存在")
    changes = payload.model_dump(exclude_unset=True)
    if changes.get("batch_no") == "":
        raise HTTPException(400, "批号不能为空")
    for key, value in changes.items():
        setattr(item, key, value)
    audit(db, "update_card", "production_card", item.id, changes, payload.created_by or item.created_by)
    db.commit()
    db.refresh(item)
    return item


@app.get("/api/passes", response_model=list[PassOut])
def passes(db: Session = Depends(get_db)):
    return list(db.scalars(select(RollingPass).order_by(RollingPass.created_at.desc())).all())


@app.get("/api/passes/{pass_id}", response_model=PassDetailOut, response_model_by_alias=True)
def pass_detail(pass_id: str, db: Session = Depends(get_db)) -> dict:
    item = db.get(RollingPass, pass_id)
    if not item:
        raise HTTPException(404, "道次不存在")
    return {
        "pass": PassOut.model_validate(item),
        "samples": [SampleOut.model_validate(row) for row in sorted(item.samples, key=lambda row: row.sequence_no)],
        "adjustments": [AdjustmentOut.model_validate(row) for row in sorted(item.adjustments, key=lambda row: row.adjusted_at)],
        "events": [EventOut.model_validate(row) for row in sorted(item.events, key=lambda row: row.occurred_at)],
    }


@app.post("/api/passes", response_model=PassOut)
def create_pass(payload: PassCreate, db: Session = Depends(get_db)):
    if payload.target_exit_thickness >= payload.entry_thickness:
        raise HTTPException(400, "目标出口厚度必须小于入口厚度")
    if payload.batch_id and not db.get(ProductionBatch, payload.batch_id):
        raise HTTPException(400, "批次不存在")
    item = RollingPass(**payload.model_dump(), status="started")
    db.add(item)
    db.flush()
    audit(db, "create_pass", "rolling_pass", item.id, payload.model_dump(), payload.operator_name)
    db.commit()
    db.refresh(item)
    return item


@app.post("/api/passes/{pass_id}/samples", response_model=SampleOut)
def add_sample(pass_id: str, payload: SampleCreate, db: Session = Depends(get_db)):
    item = db.get(RollingPass, pass_id)
    if not item:
        raise HTTPException(404, "道次不存在")
    sequence = (db.scalar(select(func.max(ThicknessSample.sequence_no)).where(ThicknessSample.pass_id == pass_id)) or 0) + 1
    sample = ThicknessSample(pass_id=pass_id, sequence_no=sequence, **payload.model_dump())
    item.status = "adjusting"
    db.add(sample)
    db.flush()
    audit(db, "add_sample", "thickness_sample", sample.id, payload.model_dump(), payload.measurer)
    db.commit()
    db.refresh(sample)
    return sample


@app.post("/api/passes/{pass_id}/adjustments")
def add_adjustment(pass_id: str, payload: AdjustmentCreate, db: Session = Depends(get_db)):
    item = db.get(RollingPass, pass_id)
    if not item:
        raise HTTPException(404, "道次不存在")
    adjustment = ParameterAdjustment(pass_id=pass_id, **payload.model_dump())
    item.status = "adjusting"
    db.add(adjustment)
    db.flush()
    audit(db, "add_adjustment", "parameter_adjustment", adjustment.id, payload.model_dump(), payload.operator_name)
    db.commit()
    return {"id": adjustment.id, "status": "saved"}


@app.post("/api/passes/{pass_id}/events")
def add_event(pass_id: str, payload: EventCreate, db: Session = Depends(get_db)):
    item = db.get(RollingPass, pass_id)
    if not item:
        raise HTTPException(404, "道次不存在")
    event = PassEvent(pass_id=pass_id, **payload.model_dump())
    if payload.event_type == "high_speed_started":
        item.high_speed_at = payload.occurred_at
        item.status = "high_speed"
    db.add(event)
    db.flush()
    audit(db, "add_event", "pass_event", event.id, payload.model_dump())
    db.commit()
    return {"id": event.id, "status": item.status}


@app.post("/api/passes/{pass_id}/complete", response_model=PassOut)
def complete_pass(pass_id: str, payload: PassComplete, db: Session = Depends(get_db)):
    item = db.get(RollingPass, pass_id)
    if not item:
        raise HTTPException(404, "道次不存在")
    item.ended_at = payload.ended_at
    item.final_thickness = payload.final_thickness
    item.quality_result = payload.quality_result
    item.status = "completed"
    if payload.notes:
        item.notes = f"{item.notes}\n{payload.notes}".strip()
    audit(db, "complete_pass", "rolling_pass", item.id, payload.model_dump(), item.operator_name)
    db.commit()
    db.refresh(item)
    return item


@app.post("/api/recommendations", response_model=RecommendationOut)
def recommend(payload: RecommendationRequest, db: Session = Depends(get_db)):
    if payload.target_exit_thickness >= payload.entry_thickness:
        raise HTTPException(400, "目标出口厚度必须小于入口厚度")
    status, confidence, model_version, result = build_recommendation(db, payload)
    item = Recommendation(
        requested_by=payload.requested_by,
        status=status,
        confidence_score=confidence,
        model_version=model_version,
        request_payload=payload.model_dump(),
        result_payload=result,
    )
    db.add(item)
    db.flush()
    audit(db, "create_recommendation", "recommendation", item.id, {"status": status, "confidence": confidence}, payload.requested_by)
    db.commit()
    db.refresh(item)
    return item


@app.get("/api/recommendations", response_model=list[RecommendationOut])
def recommendations(db: Session = Depends(get_db)):
    return list(db.scalars(select(Recommendation).order_by(Recommendation.created_at.desc()).limit(50)).all())


@app.post("/api/dat/scan")
def scan_dat(db: Session = Depends(get_db)):
    result = scan_dat_directory(db)
    audit(db, "scan_dat", "dat_directory", result.get("root", ""), result)
    db.commit()
    return result


@app.get("/api/dat/files")
def dat_files(db: Session = Depends(get_db)):
    items = list(db.scalars(select(DatFile).order_by(DatFile.start_time.desc()).limit(500)).all())
    return [
        {
            "id": item.id,
            "filename": item.filename,
            "start_time": item.start_time,
            "end_time": item.end_time,
            "frames": item.frames,
            "channel_count": item.channel_count,
            "quality_status": item.quality_status,
            "schema_hash": item.schema_hash,
        }
        for item in items
    ]


@app.get("/api/dat/datasets")
def dat_datasets() -> list[dict]:
    return dataset_foundation_status()


@app.get("/api/audit")
def audit_logs(db: Session = Depends(get_db)):
    items = list(db.scalars(select(AuditLog).order_by(AuditLog.created_at.desc()).limit(200)).all())
    return [
        {
            "id": item.id,
            "actor": item.actor,
            "action": item.action,
            "entity_type": item.entity_type,
            "entity_id": item.entity_id,
            "detail": item.detail,
            "created_at": item.created_at.isoformat(),
        }
        for item in items
    ]
