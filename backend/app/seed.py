from __future__ import annotations

import csv
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import HistoricalCandidate, Product, ProductionBatch


WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
CANDIDATE_CSV = WORKSPACE_ROOT / "outputs" / "data_foundation" / "production_segments.csv"


def _float(row: dict[str, str], key: str) -> float | None:
    value = row.get(key, "").strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def seed_products(db: Session) -> None:
    if db.scalar(select(Product.id).limit(1)):
        return

    product_077 = Product(
        code="P-1060-H18-077-1210-A",
        name="阳极氧化料 0.77×1210",
        input_alloy="1050（待确认）",
        nominal_alloy="1060（待确认）",
        temper="H18",
        target_thickness=0.77,
        target_width=1210,
        lower_tolerance=0.02,
        upper_tolerance=0.0,
        process_target_tolerance=0.005,
        purpose="保单面阳极氧化",
        review_status="pending_review",
        source_notes="由07312、07360生产随行卡合并形成的产品草稿，需工艺/质量审核。",
    )
    product_057 = Product(
        code="P-1060-H18-057-1190-A",
        name="阳极氧化料 0.57×1190",
        input_alloy="1050（待确认）",
        nominal_alloy="1060（待确认）",
        temper="H18",
        target_thickness=0.57,
        target_width=1190,
        lower_tolerance=0.01,
        upper_tolerance=0.01,
        process_target_tolerance=0.005,
        purpose="保单面阳极氧化",
        review_status="pending_review",
        source_notes="由061071生产随行卡形成的产品草稿，需工艺/质量审核。",
    )
    db.add_all([product_077, product_057])
    db.flush()

    db.add_all(
        [
            ProductionBatch(
                batch_no="07312",
                product_id=product_077.id,
                cast_coil_no="YH-2-164-1（待复核）",
                dat_anchor_time="2026-07-14 14:22:00+08:00",
                cast_spec="约7.0×1260",
                intermediate_spec="待确认",
                finished_spec="0.77×1210×C",
                status="waiting_dat_link",
                source_notes="三张纸卡OCR初稿；批号不是已确认的长期产品编码。",
            ),
            ProductionBatch(
                batch_no="07360",
                product_id=product_077.id,
                cast_coil_no="YH-2-168-2",
                dat_anchor_time="2026-07-14 14:47:00+08:00",
                cast_spec="7.0×1260",
                intermediate_spec="1.5×1280；切边1.5×1213",
                finished_spec="0.77×1210×C",
                status="waiting_dat_link",
                source_notes="纸卡识别初稿，化学成分待人工复核。",
            ),
            ProductionBatch(
                batch_no="061071",
                product_id=product_057.id,
                cast_coil_no="YH-2-138-2",
                dat_anchor_time="2026-07-14 15:12:00+08:00",
                cast_spec="约6.7×1280",
                intermediate_spec="1.6×1280；切边1.6×1193",
                finished_spec="0.57×1190×C",
                status="waiting_dat_link",
                source_notes="纸卡识别初稿，化学成分待人工复核。",
            ),
        ]
    )


def seed_historical_candidates(db: Session) -> None:
    if db.scalar(select(HistoricalCandidate.id).limit(1)) or not CANDIDATE_CSV.exists():
        return

    setting_fields = [
        "main_speed_set_median",
        "line_speed_actual_median",
        "f1_gap_average_median",
        "f2_gap_average_median",
        "tm1_gap_command_median",
        "tm2_gap_command_median",
        "f1_bending_set_median",
        "f2_bending_set_median",
        "uncoiler_tension_set_median",
        "interstand_tension_set_median",
        "coiler_tension_set_median",
        "f1_roll_force_sum_median",
        "f2_roll_force_sum_median",
    ]
    with CANDIDATE_CSV.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            db.add(
                HistoricalCandidate(
                    candidate_id=row["candidate_id"],
                    start_time=row["start_time"],
                    end_time=row["end_time"],
                    confirmed=False,
                    confidence_label=row.get("confidence", "low"),
                    entry_thickness=_float(row, "entry_thickness_set_mode"),
                    intermediate_thickness=_float(row, "intermediate_thickness_set_mode"),
                    exit_thickness=_float(row, "exit_thickness_set_mode"),
                    width=_float(row, "width_set_mode"),
                    total_reduction_ratio=_float(row, "total_reduction_ratio"),
                    active_seconds=int(float(row.get("active_seconds_1hz", "0") or 0)),
                    stable_seconds=int(float(row.get("stable_high_speed_seconds_1hz", "0") or 0)),
                    settings={key: _float(row, key) for key in setting_fields},
                    source_files=row.get("source_files", ""),
                )
            )


def seed_all(db: Session) -> None:
    seed_products(db)
    seed_historical_candidates(db)
    db.commit()
