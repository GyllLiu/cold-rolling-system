"""One durable SQLite queue and one I/O worker; start/end never wait for OCR."""
import json
import logging
import threading
import time

from . import capture_ocr, capture_storage


def process_one() -> bool:
    from .capture import connection, ROOT, utcnow
    if not capture_ocr.configured():
        return False
    now = time.time()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("""SELECT o.*,p.filename,p.oss_key FROM photo_ocr o JOIN photos p ON p.id=o.photo_id
            WHERE p.replaced_by_photo_id IS NULL AND ((o.status IN ('queued','not_configured') AND o.next_attempt<=?)
               OR (o.status='processing' AND o.lease_until<?))
            ORDER BY p.created_at,o.photo_id LIMIT 1""", (now, now)).fetchone()
        if not row:
            return False
        attempt = row["attempts"] + 1
        lease = now + 120
        db.execute("""UPDATE photo_ocr SET status='processing',attempts=?,lease_until=?,updated_at=?
                      WHERE photo_id=?""", (attempt, lease, utcnow(), row["photo_id"]))
    result, error, retryable = None, None, False
    try:
        result = capture_ocr.recognize(capture_storage.photo_bytes(ROOT, row))
        status = "needs_review" if result["warnings"] else "succeeded"
    except capture_ocr.OcrError as cause:
        error, retryable = str(cause), cause.retryable
        status = "failed"
    except Exception:
        # Never persist credential-bearing exceptions or HTTP bodies in public errors.
        error, status = "识别未完成，照片已保存，请重试", "failed"
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        current = db.execute("SELECT lease_until,status FROM photo_ocr WHERE photo_id=?", (row["photo_id"],)).fetchone()
        if current["status"] != "processing" or current["lease_until"] != lease:
            return True
        db.execute("""INSERT INTO photo_ocr_attempts(photo_id,status,raw_json,error,model,prompt_version,request_id,created_at)
                      VALUES(?,?,?,?,?,?,?,?)""", (row["photo_id"], status,
                      json.dumps(result["raw"], ensure_ascii=False) if result else None, error,
                      result["model"] if result else None, capture_ocr.PROMPT_VERSION,
                      result["request_id"] if result else None, utcnow()))
        if result:
            from .capture_corrections import apply_corrections
            result["fields"], result["warnings"] = apply_corrections(
                db, row["photo_id"], result["fields"], result["warnings"])
            status = "needs_review" if result["warnings"] else "succeeded"
            db.execute("""UPDATE photo_ocr SET status=?,fields_json=?,warnings_json=?,model=?,prompt_version=?,
                          request_id=?,error=NULL,lease_until=NULL,updated_at=? WHERE photo_id=?""",
                       (status, json.dumps(result["fields"], ensure_ascii=False), json.dumps(result["warnings"], ensure_ascii=False),
                        result["model"], result["prompt_version"], result["request_id"], utcnow(), row["photo_id"]))
        else:
            queued = retryable and attempt < 3
            db.execute("""UPDATE photo_ocr SET status=?,error=?,lease_until=NULL,next_attempt=?,updated_at=?
                          WHERE photo_id=?""", ("queued" if queued else "failed", error,
                          time.time() + 30 * attempt, utcnow(), row["photo_id"]))
    return True


def start_worker():
    stop = threading.Event()

    def run():
        while not stop.is_set():
            try:
                did_work = process_one()
                did_work = capture_storage.archive_one() or did_work
            except Exception:
                logging.getLogger(__name__).error("Capture worker cycle failed; retrying later")
                did_work = False
            stop.wait(0.2 if did_work else 3)

    thread = threading.Thread(target=run, name="capture-ocr", daemon=True)
    thread.start()
    return stop, thread
