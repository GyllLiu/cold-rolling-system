"""Single-mill photo records; SQLite and photos live outside the deployment directory."""
from __future__ import annotations

import csv
from contextlib import contextmanager, asynccontextmanager
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import threading
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from uuid import UUID

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.background import BackgroundTask
from PIL import Image, ImageOps
from pydantic import BaseModel, Field, field_validator

from . import capture_ocr, capture_storage
from .capture_corrections import ReviewConflict, review_photo

ROOT = Path(os.environ.get("CAPTURE_DATA_DIR", Path(__file__).resolve().parents[2] / "data" / "capture"))
ACCESS_CODE = os.environ.get("CAPTURE_ACCESS_CODE", "")
MAX_PHOTO_BYTES = 20 * 1024 * 1024
ALLOYS = {"1060", "1100", "3003"}
STATES = {"O", "H14", "H16", "H18", "H22", "H24", "H26", "硬态", "未知"}
IMAGE_LOCK = threading.Lock()
@asynccontextmanager
async def lifespan(_app):
    from .capture_tasks import start_worker
    with connection() as db:
        # Also recover photos created during a rollback to the pre-OCR application.
        db.execute("INSERT OR IGNORE INTO photo_ocr(photo_id,updated_at) SELECT id,created_at FROM photos")
    stop, worker = start_worker()
    yield
    stop.set()
    worker.join(timeout=2)


app = FastAPI(title="冷二联拍照记录", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@contextmanager
def connection():
    ROOT.mkdir(parents=True, exist_ok=True)
    (ROOT / "photos").mkdir(exist_ok=True)
    db = sqlite3.connect(ROOT / "records.sqlite3", timeout=15)
    try:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS photos (
              id TEXT PRIMARY KEY, filename TEXT NOT NULL, media_type TEXT NOT NULL,
              sha256 TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS capture_records (
              id TEXT PRIMARY KEY, photo_id TEXT NOT NULL REFERENCES photos(id),
              coil_no TEXT NOT NULL, alloy TEXT NOT NULL, incoming_state TEXT NOT NULL,
              started_at TEXT NOT NULL, start_received_at TEXT NOT NULL,
              ended_at TEXT, end_received_at TEXT, machine_id TEXT NOT NULL DEFAULT 'cold-tandem-1'
            );
            CREATE UNIQUE INDEX IF NOT EXISTS capture_one_active
              ON capture_records(machine_id) WHERE ended_at IS NULL;
            CREATE INDEX IF NOT EXISTS capture_coil_time ON capture_records(coil_no, started_at);
            CREATE TABLE IF NOT EXISTS capture_field_corrections (
              id INTEGER PRIMARY KEY, photo_id TEXT NOT NULL REFERENCES photos(id),
              field TEXT NOT NULL CHECK(field IN ('batch_no','alloy','card_state')),
              previous_value TEXT, corrected_value TEXT NOT NULL, reason TEXT NOT NULL,
              created_at TEXT NOT NULL
            );
        """)
        if db.execute("PRAGMA user_version").fetchone()[0] < 1:
            # Append-only migration: preserve deployed photos, records and access cookies.
            with db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("PRAGMA user_version").fetchone()[0] < 1:
                    columns = {row[1] for row in db.execute("PRAGMA table_info(photos)")}
                    for name, declaration in {"captured_at": "TEXT", "byte_size": "INTEGER",
                                              "width": "INTEGER", "height": "INTEGER", "oss_key": "TEXT"}.items():
                        if name not in columns:
                            db.execute(f"ALTER TABLE photos ADD COLUMN {name} {declaration}")
                    db.execute("""CREATE TABLE IF NOT EXISTS photo_ocr (
                        photo_id TEXT PRIMARY KEY REFERENCES photos(id),
                        status TEXT NOT NULL DEFAULT 'not_configured'
                          CHECK(status IN ('not_configured','queued','processing','succeeded','needs_review','failed')),
                        fields_json TEXT, warnings_json TEXT, model TEXT, prompt_version TEXT,
                        request_id TEXT, error TEXT, attempts INTEGER NOT NULL DEFAULT 0,
                        next_attempt REAL NOT NULL DEFAULT 0, lease_until REAL,
                        updated_at TEXT NOT NULL
                    )""")
                    db.execute("""CREATE TABLE IF NOT EXISTS photo_ocr_attempts (
                        id INTEGER PRIMARY KEY, photo_id TEXT NOT NULL REFERENCES photos(id),
                        status TEXT NOT NULL, raw_json TEXT, error TEXT, model TEXT,
                        prompt_version TEXT, request_id TEXT, created_at TEXT NOT NULL
                    )""")
                    db.execute("CREATE INDEX IF NOT EXISTS photos_capture_time ON photos(COALESCE(captured_at,created_at) DESC,id DESC)")
                    db.execute("CREATE INDEX IF NOT EXISTS photo_ocr_queue ON photo_ocr(status,next_attempt)")
                    db.execute("CREATE INDEX IF NOT EXISTS photo_ocr_attempt_photo ON photo_ocr_attempts(photo_id,id DESC)")
                    db.execute("CREATE INDEX IF NOT EXISTS capture_record_photo ON capture_records(photo_id)")
                    db.execute("INSERT OR IGNORE INTO photo_ocr(photo_id,updated_at) SELECT id,created_at FROM photos")
                    db.execute("PRAGMA user_version=1")
        if db.execute("PRAGMA user_version").fetchone()[0] < 2:
            with db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("PRAGMA user_version").fetchone()[0] < 2:
                    columns = {row[1] for row in db.execute("PRAGMA table_info(photos)")}
                    for name, declaration in {
                        "replaces_photo_id": "TEXT REFERENCES photos(id)",
                        "replaced_by_photo_id": "TEXT REFERENCES photos(id)",
                        "display_rotation": "INTEGER",
                    }.items():
                        if name not in columns:
                            db.execute(f"ALTER TABLE photos ADD COLUMN {name} {declaration}")
                    db.execute("PRAGMA user_version=2")
        if db.execute("PRAGMA user_version").fetchone()[0] < 3:
            with db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("PRAGMA user_version").fetchone()[0] < 3:
                    columns = {row[1] for row in db.execute("PRAGMA table_info(capture_records)")}
                    if "selected_pass_no" not in columns:
                        db.execute("ALTER TABLE capture_records ADD COLUMN selected_pass_no INTEGER CHECK(selected_pass_no BETWEEN 1 AND 5)")
                    db.execute("PRAGMA user_version=3")
        if db.execute("PRAGMA user_version").fetchone()[0] < 4:
            with db:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("PRAGMA user_version").fetchone()[0] < 4:
                    db.execute("""CREATE TABLE IF NOT EXISTS capture_photo_reviews (
                        id INTEGER PRIMARY KEY, photo_id TEXT NOT NULL REFERENCES photos(id),
                        fields_json TEXT NOT NULL, model_alloy TEXT, created_at TEXT NOT NULL
                    )""")
                    db.execute("CREATE INDEX IF NOT EXISTS capture_photo_reviews_latest ON capture_photo_reviews(photo_id,id DESC)")
                    db.execute("PRAGMA user_version=4")
        with db:
            yield db
    finally:
        db.close()


def valid_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        raise HTTPException(422, "记录编号无效")


def timestamp(value: str) -> str:
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
        return stamp.astimezone(timezone.utc).isoformat(timespec="milliseconds")
    except ValueError:
        raise HTTPException(422, "时间必须包含时区")


def signature(expiry: str) -> str:
    return hmac.new(ACCESS_CODE.encode(), expiry.encode(), hashlib.sha256).hexdigest()


def authorized(request: Request) -> bool:
    if not ACCESS_CODE:
        return True
    cookie = request.cookies.get("mill_capture", "")
    expiry, _, signed = cookie.partition(".")
    return expiry.isdigit() and int(expiry) > time.time() and hmac.compare_digest(signed, signature(expiry))


@app.middleware("http")
async def guard(request: Request, call_next):
    if request.method not in {"GET", "HEAD"}:
        from urllib.parse import urlsplit
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
            return JSONResponse({"detail": "请求来源不匹配"}, status_code=403)
    public = {"/api/capture/health", "/api/capture/auth"}
    if request.url.path not in public and not authorized(request):
        return JSONResponse({"detail": "请输入工位访问码"}, status_code=401)
    response = await call_next(request)
    response.headers["Cache-Control"] = "private, no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


class Login(BaseModel):
    code: str = Field(max_length=128)


@app.post("/api/capture/auth")
def login(body: Login, request: Request):
    if not hmac.compare_digest(body.code.encode(), ACCESS_CODE.encode()):
        raise HTTPException(401, "访问码不正确")
    expiry = str(int(time.time()) + 7 * 86400)
    response = JSONResponse({"ok": True})
    response.set_cookie("mill_capture", f"{expiry}.{signature(expiry)}", max_age=7 * 86400,
                        httponly=True, samesite="strict", path="/api/capture",
                        secure=request.headers.get("x-forwarded-proto", request.url.scheme) == "https")
    return response


@app.get("/api/capture/health")
def health():
    with connection() as db:
        db.execute("SELECT 1")
    return {"status": "ok", "storage": "oss" if capture_storage.configured() else "ecs-local", "ocr": capture_ocr.configured()}


QUERY = """WITH linked AS (
           SELECT r.*, json_extract(o.fields_json,'$.batch_no') AS batch_no,
             json_extract(o.fields_json,'$.alloy') AS ocr_alloy,
             json_extract(o.fields_json,'$.card_state') AS card_state,
             o.status AS ocr_status, o.prompt_version AS ocr_prompt_version,
             CASE WHEN v.id IS NOT NULL
               AND json_extract(o.fields_json,'$.batch_no') IS json_extract(v.fields_json,'$.batch_no')
               AND json_extract(o.fields_json,'$.alloy') IS json_extract(v.fields_json,'$.alloy')
               AND json_extract(o.fields_json,'$.card_state') IS json_extract(v.fields_json,'$.card_state')
               THEN v.created_at ELSE NULL END AS reviewed_at,
             CASE WHEN v.id IS NOT NULL
               AND json_extract(o.fields_json,'$.batch_no') IS json_extract(v.fields_json,'$.batch_no')
               AND json_extract(o.fields_json,'$.alloy') IS json_extract(v.fields_json,'$.alloy')
               AND json_extract(o.fields_json,'$.card_state') IS json_extract(v.fields_json,'$.card_state')
               THEN v.model_alloy ELSE NULL END AS model_alloy
           FROM capture_records r LEFT JOIN photo_ocr o ON o.photo_id=r.photo_id
           LEFT JOIN capture_photo_reviews v ON v.id=(SELECT id FROM capture_photo_reviews
             WHERE photo_id=r.photo_id ORDER BY id DESC LIMIT 1)
           )
           SELECT linked.*, CASE WHEN NULLIF(TRIM(coil_no), '') IS NOT NULL
             THEN ROW_NUMBER() OVER (PARTITION BY coil_no ORDER BY started_at, id)
             ELSE NULL END AS pass_no,
             CASE WHEN NULLIF(TRIM(batch_no), '') IS NOT NULL
             THEN ROW_NUMBER() OVER (PARTITION BY machine_id,batch_no ORDER BY started_at,id)
             ELSE NULL END AS batch_sequence
           FROM linked"""


def record_dict(row):
    value = dict(row)
    for field in ("coil_no", "alloy", "incoming_state"):
        value[field] = value[field] or None
    value["photo_url"] = f"/api/capture/photos/{value['photo_id']}"
    return value


@app.get("/api/capture/state")
def state():
    with connection() as db:
        active = db.execute(f"SELECT * FROM ({QUERY}) WHERE ended_at IS NULL").fetchone()
        recent = db.execute(f"SELECT * FROM ({QUERY}) ORDER BY started_at DESC LIMIT 20").fetchall()
    return {"active": record_dict(active) if active else None, "recent": [record_dict(r) for r in recent],
            "server_time": utcnow()}


@app.put("/api/capture/photos/{photo_id}")
async def upload_photo(photo_id: str, request: Request):
    photo_id = valid_id(photo_id)
    captured_at = timestamp(request.headers.get("x-capture-time", utcnow()))
    replaces = request.headers.get("x-replaces-photo-id")
    replaces = valid_id(replaces) if replaces else None
    if replaces == photo_id:
        raise HTTPException(422, "重拍照片必须使用新编号")
    rotation_header = request.headers.get("x-display-rotation")
    try:
        display_rotation = int(rotation_header) if rotation_header is not None else None
        if display_rotation not in {None, 0, 90, 180, 270}:
            raise ValueError()
    except ValueError:
        raise HTTPException(422, "照片方向无效")
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_PHOTO_BYTES:
            raise HTTPException(413, "照片不能超过20MB")
    try:
        with Image.open(io.BytesIO(raw)) as picture:
            if picture.format not in {"JPEG", "PNG", "WEBP"} or picture.width * picture.height > 25_000_000:
                raise ValueError()
            extension, media = {"JPEG": ("jpg", "image/jpeg"), "PNG": ("png", "image/png"),
                                "WEBP": ("webp", "image/webp")}[picture.format]
            width, height = picture.size
            # Browsers and thumbnails honor EXIF; keep the same effective dimensions.
            if picture.getexif().get(274) in (5, 6, 7, 8):
                width, height = height, width
            picture.verify()
    except Exception:
        raise HTTPException(422, "照片无法读取，请重新拍照")
    digest = hashlib.sha256(raw).hexdigest()
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
        if existing:
            if existing["sha256"] != digest or existing["replaces_photo_id"] != replaces:
                raise HTTPException(409, "照片编号已使用")
        else:
            if replaces:
                previous = db.execute("SELECT * FROM photos WHERE id=?", (replaces,)).fetchone()
                if not previous:
                    raise HTTPException(404, "被替换的照片不存在")
                if previous["replaced_by_photo_id"]:
                    raise HTTPException(409, "旧照片已被替换，请刷新后使用最新照片")
                if db.execute("SELECT id FROM capture_records WHERE photo_id=?", (replaces,)).fetchone():
                    raise HTTPException(409, "照片已关联生产记录，不能重拍替换")
            filename = f"{photo_id}.{extension}"
            target = ROOT / "photos" / filename
            temporary = target.with_suffix(".upload")
            temporary.write_bytes(raw)
            temporary.replace(target)
            db.execute("""INSERT INTO photos(id,filename,media_type,sha256,created_at,captured_at,byte_size,width,height,replaces_photo_id,display_rotation)
                          VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                       (photo_id, filename, media, digest, utcnow(), captured_at, len(raw), width, height, replaces, display_rotation))
            if replaces:
                db.execute("UPDATE photos SET replaced_by_photo_id=? WHERE id=?", (photo_id, replaces))
        db.execute("INSERT OR IGNORE INTO photo_ocr(photo_id,status,updated_at) VALUES(?,?,?)",
                   (photo_id, "queued" if capture_ocr.configured() else "not_configured", utcnow()))
    ensure_thumbnail(photo_id, ROOT / "photos" / f"{photo_id}.{extension}")
    return {"photo_id": photo_id, "photo_url": f"/api/capture/photos/{photo_id}", "replaces_photo_id": replaces}


def ensure_thumbnail(photo_id, original):
    target = ROOT / "photos" / f"{photo_id}.thumb.jpg"
    with IMAGE_LOCK:
        if not target.exists() and original.exists():
            with Image.open(original) as source:
                thumbnail = ImageOps.exif_transpose(source).convert("RGB")
                thumbnail.thumbnail((400, 400))
                temporary = target.with_suffix(".upload")
                thumbnail.save(temporary, format="JPEG", quality=80)
                temporary.replace(target)
    return target


def serve_local(path, media_type):
    # Opening the descriptor before responding remains safe if archival unlinks the path.
    handle = path.open("rb")
    size = os.fstat(handle.fileno()).st_size

    def chunks():
        try:
            while block := handle.read(65536):
                yield block
        finally:
            handle.close()
    return StreamingResponse(chunks(), media_type=media_type, headers={"Content-Length": str(size)},
                             background=BackgroundTask(handle.close))


@app.get("/api/capture/photos/{photo_id}/thumbnail")
def photo_thumbnail(photo_id: str):
    photo_id = valid_id(photo_id)
    with connection() as db:
        row = db.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
    if not row:
        raise HTTPException(404, "照片不存在")
    target = ensure_thumbnail(photo_id, ROOT / "photos" / row["filename"])
    try:
        return serve_local(target, "image/jpeg")
    except FileNotFoundError:
        with connection() as db:
            row = db.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
    if row["oss_key"] and capture_storage.configured():
        return RedirectResponse(capture_storage.signed_photo_url(capture_storage.thumbnail_key(row["oss_key"])), status_code=302)
    raise HTTPException(503, "缩略图暂不可用，请稍后重试")


@app.get("/api/capture/photos/{photo_id}")
def photo(photo_id: str):
    with connection() as db:
        row = db.execute("SELECT * FROM photos WHERE id=?", (valid_id(photo_id),)).fetchone()
    if not row:
        raise HTTPException(404, "照片不存在")
    local = ROOT / "photos" / row["filename"]
    try:
        return serve_local(local, row["media_type"])
    except FileNotFoundError:
        with connection() as db:
            row = db.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
    if row["oss_key"] and capture_storage.configured():
        return RedirectResponse(capture_storage.signed_photo_url(row["oss_key"]), status_code=302)
    raise HTTPException(503, "照片存储暂不可用，请稍后重试")


class StartRecord(BaseModel):
    id: str
    photo_id: str
    # Keep empty strings compatible with the existing NOT NULL storage schema.
    # Metadata remains unresolved until OCR; older clients can still supply it.
    coil_no: str = Field(default="", max_length=100)
    alloy: str = ""
    incoming_state: str = ""
    started_at: str
    selected_pass_no: int | None = Field(default=None, ge=1, le=5)

    @field_validator("coil_no")
    @classmethod
    def normalize_coil(cls, value):
        value = value.strip().upper()
        if any(ord(c) < 32 for c in value):
            raise ValueError("请填写有效卷号")
        return value


@app.post("/api/capture/records")
def start_record(body: StartRecord):
    record_id, photo_id = valid_id(body.id), valid_id(body.photo_id)
    start = timestamp(body.started_at)
    if (body.alloy and body.alloy not in ALLOYS) or (body.incoming_state and body.incoming_state not in STATES):
        raise HTTPException(422, "牌号或来料状态无效")
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        existing = db.execute("SELECT * FROM capture_records WHERE id=?", (record_id,)).fetchone()
        if existing:
            expected = {"photo_id": photo_id, "started_at": start}
            # A retry without metadata must also work after a later OCR backfill.
            for field in ("coil_no", "alloy", "incoming_state", "selected_pass_no"):
                if field in body.model_fields_set:
                    expected[field] = getattr(body, field)
            if any(existing[k] != v for k, v in expected.items()):
                raise HTTPException(409, "该开始记录已保存，请刷新查看")
        else:
            if not db.execute("SELECT id FROM photos WHERE id=?", (photo_id,)).fetchone():
                raise HTTPException(422, "请先拍照并保存成功")
            if db.execute("SELECT id FROM photos WHERE id=? AND replaced_by_photo_id IS NOT NULL", (photo_id,)).fetchone():
                raise HTTPException(409, "这张照片已被重拍替换，请使用最新照片")
            if db.execute("SELECT id FROM capture_records WHERE ended_at IS NULL").fetchone():
                raise HTTPException(409, "当前道次尚未结束，请先完成当前道次")
            if db.execute("SELECT id FROM capture_records WHERE photo_id=?", (photo_id,)).fetchone():
                raise HTTPException(409, "这张照片已关联道次，请为当前道次重新拍照")
            if body.selected_pass_no is None:
                raise HTTPException(422, "请先选择第 1–5 道")
            db.execute("""INSERT INTO capture_records
                (id,photo_id,coil_no,alloy,incoming_state,started_at,start_received_at,selected_pass_no)
                VALUES(?,?,?,?,?,?,?,?)""", (record_id, photo_id, body.coil_no, body.alloy,
                                           body.incoming_state, start, utcnow(), body.selected_pass_no))
        result = db.execute(f"SELECT * FROM ({QUERY}) WHERE id=?", (record_id,)).fetchone()
    return record_dict(result)


HISTORY_QUERY = """SELECT p.id AS photo_id,p.created_at,COALESCE(p.captured_at,p.created_at) AS captured_at,
    p.width,p.height,p.byte_size,p.oss_key,p.display_rotation,p.replaces_photo_id,p.replaced_by_photo_id,
    o.status AS ocr_status,o.fields_json,o.warnings_json,o.error AS ocr_error,o.updated_at AS ocr_updated_at,
    v.fields_json AS reviewed_fields_json,v.model_alloy AS reviewed_model_alloy,v.created_at AS reviewed_at,
    r.id AS record_id,r.started_at,r.ended_at,r.selected_pass_no
    FROM photos p LEFT JOIN photo_ocr o ON o.photo_id=p.id
    LEFT JOIN capture_photo_reviews v ON v.id=(SELECT id FROM capture_photo_reviews
      WHERE photo_id=p.id ORDER BY id DESC LIMIT 1)
    LEFT JOIN capture_records r ON r.id=(SELECT id FROM capture_records
      WHERE photo_id=p.id ORDER BY started_at DESC,id DESC LIMIT 1)"""


def history_dict(row):
    item = dict(row)
    item["photo_url"] = f"/api/capture/photos/{item['photo_id']}"
    if item["display_rotation"] is None:
        item["display_rotation"] = 90 if (item["width"] or 0) > (item["height"] or 0) else 0
    item["thumbnail_url"] = item["photo_url"] + "/thumbnail"
    item["fields"] = json.loads(item.pop("fields_json") or "{}")
    item["warnings"] = json.loads(item.pop("warnings_json") or "[]")
    reviewed_fields = json.loads(item.pop("reviewed_fields_json") or "{}")
    if not all(item["fields"].get(key) == reviewed_fields.get(key) for key in capture_ocr.FIELDS) or not reviewed_fields:
        item["reviewed_at"] = None
        item["reviewed_model_alloy"] = None
    item["labels_ready"] = bool(item["reviewed_at"] and item["reviewed_model_alloy"]
                                and re.fullmatch(r"[0-9]{5,6}", item["fields"].get("batch_no") or "")
                                and capture_ocr.STATE_PATTERN.fullmatch(item["fields"].get("card_state") or ""))
    item.pop("oss_key", None)
    return item


@app.get("/api/capture/history")
def history(day: date | None = None, offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100), include_replaced: bool = False):
    zone = ZoneInfo("Asia/Shanghai")
    day = day or datetime.now(zone).date()
    start = datetime.combine(day, datetime.min.time(), tzinfo=zone)
    bounds = tuple(t.astimezone(timezone.utc).isoformat(timespec="milliseconds") for t in (start, start + timedelta(days=1)))
    predicate = "COALESCE(p.captured_at,p.created_at)>=? AND COALESCE(p.captured_at,p.created_at)<?"
    if not include_replaced:
        predicate += " AND p.replaced_by_photo_id IS NULL"
    with connection() as db:
        rows = db.execute(HISTORY_QUERY + f" WHERE {predicate} ORDER BY captured_at DESC,p.id DESC LIMIT ? OFFSET ?",
                          (*bounds, limit, offset)).fetchall()
        total = db.execute(f"SELECT COUNT(*) FROM photos p WHERE {predicate}", bounds).fetchone()[0]
    return {"date": day.isoformat(), "timezone": "Asia/Shanghai", "total": total,
            "items": [history_dict(row) for row in rows], "has_more": offset + len(rows) < total}


@app.get("/api/capture/photos/{photo_id}/details")
def photo_details(photo_id: str):
    with connection() as db:
        row = db.execute(HISTORY_QUERY + " WHERE p.id=?", (valid_id(photo_id),)).fetchone()
    if not row:
        raise HTTPException(404, "照片不存在")
    return history_dict(row)


class PhotoReview(BaseModel):
    expected_fields: dict[str, str | None]
    batch_no: str = Field(max_length=100)
    alloy: str = Field(max_length=100)
    card_state: str = Field(max_length=100)
    model_alloy: str | None = Field(default=None, max_length=20)


@app.post("/api/capture/photos/{photo_id}/review")
def confirm_photo_fields(photo_id: str, body: PhotoReview):
    try:
        review_photo(valid_id(photo_id), body.expected_fields,
                     {"batch_no": body.batch_no, "alloy": body.alloy, "card_state": body.card_state},
                     body.model_alloy)
    except ReviewConflict as error:
        raise HTTPException(409, str(error)) from error
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    return photo_details(photo_id)


class PhotoRotation(BaseModel):
    rotation: int
    expected_rotation: int

    @field_validator("rotation", "expected_rotation")
    @classmethod
    def quarter_turn(cls, value):
        if value not in (0, 90, 180, 270):
            raise ValueError("只支持 0、90、180、270 度")
        return value


@app.put("/api/capture/photos/{photo_id}/rotation")
def rotate_photo(photo_id: str, body: PhotoRotation):
    photo_id = valid_id(photo_id)
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
        if not row:
            raise HTTPException(404, "照片不存在")
        current = row["display_rotation"]
        if current is None:
            current = 90 if (row["width"] or 0) > (row["height"] or 0) else 0
        if current not in (body.expected_rotation, body.rotation):
            raise HTTPException(409, "照片方向已更新，请关闭预览后重新打开")
        db.execute("UPDATE photos SET display_rotation=? WHERE id=?", (body.rotation, photo_id))
    return {"photo_id": photo_id, "display_rotation": body.rotation}


@app.post("/api/capture/photos/{photo_id}/recognize")
def retry_recognition(photo_id: str):
    photo_id = valid_id(photo_id)
    if not capture_ocr.configured():
        raise HTTPException(503, "OCR 尚未开通，照片已保存")
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        if db.execute("SELECT id FROM photos WHERE id=? AND replaced_by_photo_id IS NOT NULL", (photo_id,)).fetchone():
            raise HTTPException(409, "这张照片已被替换，请识别最新照片")
        row = db.execute("SELECT * FROM photo_ocr WHERE photo_id=?", (photo_id,)).fetchone()
        if not row:
            raise HTTPException(404, "照片不存在")
        if row["status"] not in ("processing", "queued"):
            db.execute("""UPDATE photo_ocr SET status='queued',attempts=0,next_attempt=0,
                          error=NULL,updated_at=? WHERE photo_id=?""", (utcnow(), photo_id))
    return {"status": "queued", "photo_id": photo_id}


class EndRecord(BaseModel):
    ended_at: str


@app.post("/api/capture/records/{record_id}/end")
def end_record(record_id: str, body: EndRecord):
    record_id, end = valid_id(record_id), timestamp(body.ended_at)
    with connection() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT * FROM capture_records WHERE id=?", (record_id,)).fetchone()
        if not row:
            raise HTTPException(404, "道次记录不存在")
        if not row["ended_at"]:
            if end < row["started_at"]:
                raise HTTPException(422, "结束时间不能早于开始时间")
            db.execute("UPDATE capture_records SET ended_at=?,end_received_at=? WHERE id=?",
                       (end, utcnow(), record_id))
        result = db.execute(f"SELECT * FROM ({QUERY}) WHERE id=?", (record_id,)).fetchone()
    return record_dict(result)


@app.get("/api/capture/export.csv")
def export_csv():
    output = io.StringIO()
    writer = csv.writer(output)
    fields = ["id", "coil_no", "alloy", "incoming_state", "pass_no", "selected_pass_no", "started_at", "ended_at",
              "start_received_at", "end_received_at", "photo_id", "machine_id", "batch_no",
              "batch_sequence", "ocr_alloy", "card_state", "ocr_status", "ocr_prompt_version",
              "reviewed_at", "model_alloy"]
    writer.writerow(fields)
    with connection() as db:
        for row in db.execute(f"SELECT * FROM ({QUERY}) ORDER BY started_at"):
            # Protect spreadsheets from interpreting a photographed/typed identifier as a formula.
            writer.writerow([("'" + str(row[k])) if str(row[k] or "").startswith(("=", "+", "-", "@"))
                             else row[k] for k in fields])
    return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="rolling-pass-records.csv"'})
