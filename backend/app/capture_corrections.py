"""Operator-confirmed label corrections; no anonymous public mutation endpoint.

Keep provider responses in photo_ocr_attempts and an append-only correction trail.
Materialize effective fields so existing history/export consumers see corrections.
"""
import json
import re

from .capture_ocr import FIELDS


def apply_corrections(db, photo_id, fields, warnings):
    fields, warnings = dict(fields), list(warnings)
    rows = db.execute("SELECT field,corrected_value FROM capture_field_corrections WHERE photo_id=? ORDER BY id",
                      (photo_id,)).fetchall()
    for row in rows:
        fields[row["field"]] = row["corrected_value"]
        label = FIELDS[row["field"]]
        warnings = [w for w in warnings if not (w.startswith(label) or w == f"未识别到{label}")]
    return fields, warnings


def correct_field(photo_id, field, expected_value, corrected_value, reason):
    from .capture import connection, utcnow
    if field not in FIELDS or not isinstance(corrected_value, str) or not corrected_value.strip() or not reason:
        raise ValueError("A confirmed field value and reason are required")
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM photo_ocr WHERE photo_id=?", (photo_id,)).fetchone()
        if row is None:
            raise ValueError("Photo OCR record does not exist")
        fields = json.loads(row["fields_json"] or "{}")
        if fields.get(field) != expected_value:
            raise ValueError("Field changed since review; refusing to overwrite")
        db.execute("""INSERT INTO capture_field_corrections
            (photo_id,field,previous_value,corrected_value,reason,created_at) VALUES(?,?,?,?,?,?)""",
            (photo_id, field, expected_value, corrected_value.strip(), reason, utcnow()))
        fields, warnings = apply_corrections(db, photo_id, fields, json.loads(row["warnings_json"] or "[]"))
        # Leave in-flight jobs/retry state alone; the worker reapplies corrections.
        status = row["status"]
        if status in {"succeeded", "needs_review"}:
            status = "needs_review" if warnings else "succeeded"
        db.execute("UPDATE photo_ocr SET fields_json=?,warnings_json=?,status=?,updated_at=? WHERE photo_id=?",
            (json.dumps(fields, ensure_ascii=False), json.dumps(warnings, ensure_ascii=False), status, utcnow(), photo_id))


class ReviewConflict(ValueError):
    pass


def review_photo(photo_id, expected_fields, confirmed_fields, model_alloy):
    """Confirm what the card says; keep OCR raw output and every change auditable."""
    from .capture import connection, utcnow

    values = {}
    for field in FIELDS:
        value = confirmed_fields.get(field)
        if not isinstance(value, str) or not value.strip() or len(value.strip()) > 100:
            raise ValueError(f"请核对并填写{FIELDS[field]}")
        values[field] = value.strip().upper() if field == "card_state" else value.strip()
    if not re.fullmatch(r"[0-9]{5,6}", values["batch_no"]):
        raise ValueError("批号须为照片上实际写的5或6位数字，不自动补齐")
    model_alloy = model_alloy.strip() if isinstance(model_alloy, str) else None
    model_alloy = model_alloy or None
    if model_alloy and not re.fullmatch(r"[0-9]{4}", model_alloy):
        raise ValueError("用于模型的实际牌号须为4位数字；不确定时留空")
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        photo = db.execute("SELECT replaced_by_photo_id FROM photos WHERE id=?", (photo_id,)).fetchone()
        if photo is None:
            raise ValueError("照片不存在")
        if photo["replaced_by_photo_id"]:
            raise ReviewConflict("照片已被重拍替换，请核对新照片")
        row = db.execute("SELECT * FROM photo_ocr WHERE photo_id=?", (photo_id,)).fetchone()
        if row is None:
            raise ValueError("识别记录不存在")
        if row["status"] in {"queued", "processing"}:
            raise ReviewConflict("识别尚未结束，请稍后再核对")
        old = json.loads(row["fields_json"] or "{}")
        if any(old.get(field) != expected_fields.get(field) for field in FIELDS):
            raise ReviewConflict("识别结果已更新，请重新打开照片后核对")
        latest = db.execute("SELECT * FROM capture_photo_reviews WHERE photo_id=? ORDER BY id DESC LIMIT 1", (photo_id,)).fetchone()
        if (latest and json.loads(latest["fields_json"]) == values and latest["model_alloy"] == model_alloy
                and all(old.get(field) == values[field] for field in FIELDS)):
            return
        for field in FIELDS:
            if old.get(field) != values[field]:
                db.execute("""INSERT INTO capture_field_corrections
                    (photo_id,field,previous_value,corrected_value,reason,created_at) VALUES(?,?,?,?,?,?)""",
                    (photo_id, field, old.get(field), values[field], "现场对照原图确认", utcnow()))
        fields, warnings = apply_corrections(db, photo_id, old, json.loads(row["warnings_json"] or "[]"))
        status = row["status"]
        if status in {"succeeded", "needs_review"}:
            status = "needs_review" if warnings else "succeeded"
        db.execute("UPDATE photo_ocr SET fields_json=?,warnings_json=?,status=?,updated_at=? WHERE photo_id=?",
                   (json.dumps(fields, ensure_ascii=False), json.dumps(warnings, ensure_ascii=False), status, utcnow(), photo_id))
        db.execute("INSERT INTO capture_photo_reviews(photo_id,fields_json,model_alloy,created_at) VALUES(?,?,?,?)",
                   (photo_id, json.dumps(values, ensure_ascii=False), model_alloy, utcnow()))
