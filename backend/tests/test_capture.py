import csv
from contextlib import closing
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image

from backend.app import capture, capture_ocr, capture_storage, capture_tasks


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = patch.object(capture, "ROOT", Path(self.directory.name))
        self.code = patch.object(capture, "ACCESS_CODE", "test-station-code")
        self.root.start()
        self.code.start()
        self.ocr_config = patch.object(capture_ocr, "configured", return_value=False)
        self.ocr_config.start()
        self.storage_config = patch.object(capture_storage, "configured", return_value=False)
        self.storage_config.start()
        self.client = TestClient(capture.app)
        self.assertEqual(self.client.post("/api/capture/auth", json={"code": "test-station-code"}).status_code, 200)
        image = io.BytesIO()
        Image.new("RGB", (32, 32), "blue").save(image, format="JPEG")
        self.photo = image.getvalue()

    def tearDown(self):
        self.client.close()
        self.code.stop()
        self.ocr_config.stop()
        self.storage_config.stop()
        self.root.stop()
        self.directory.cleanup()

    def test_retake_replaces_only_after_success_and_retry_is_idempotent(self):
        previous = self.upload()
        replacement = str(uuid4())
        url = f"/api/capture/photos/{replacement}"
        headers = {"X-Replaces-Photo-Id": previous, "X-Display-Rotation": "270"}
        self.assertEqual(self.client.put(url, content=b"invalid", headers=headers).status_code, 422)
        old = self.client.get(f"/api/capture/photos/{previous}/details").json()
        self.assertIsNone(old["replaced_by_photo_id"])
        for _ in range(2):
            self.assertEqual(self.client.put(url, content=self.photo, headers=headers).status_code, 200)
        history = self.client.get("/api/capture/history").json()
        self.assertEqual(history["total"], 1)
        self.assertEqual(history["items"][0]["photo_id"], replacement)
        self.assertEqual(history["items"][0]["display_rotation"], 270)
        self.assertEqual(history["items"][0]["replaces_photo_id"], previous)
        self.assertEqual(self.client.get("/api/capture/history?include_replaced=true").json()["total"], 2)
        self.assertEqual(self.client.get(f"/api/capture/photos/{previous}").content, self.photo)
        self.assertEqual(self.client.get(f"/api/capture/photos/{previous}/details").json()["replaced_by_photo_id"], replacement)
        with capture.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM photos").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM photo_ocr").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM capture_records").fetchone()[0], 0)
        body = {"id": str(uuid4()), "photo_id": previous, "started_at": capture.utcnow(), "selected_pass_no": 1}
        self.assertEqual(self.client.post("/api/capture/records", json=body).status_code, 409)
        body["photo_id"] = replacement
        self.assertEqual(self.client.post("/api/capture/records", json=body).status_code, 200)
        self.assertEqual(self.client.post("/api/capture/records", json=body).status_code, 200)
        self.assertEqual(len(self.client.get("/api/capture/state").json()["recent"]), 1)

    def test_retake_rejects_linked_missing_and_already_replaced_photos(self):
        body = self.payload()
        self.client.post("/api/capture/records", json=body)
        for ended in (False, True):
            if ended:
                self.client.post(f"/api/capture/records/{body['id']}/end", json={"ended_at": "2026-09-05T01:00:00Z"})
            response = self.client.put(f"/api/capture/photos/{uuid4()}", content=self.photo,
                                       headers={"X-Replaces-Photo-Id": body["photo_id"]})
            self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.put(f"/api/capture/photos/{uuid4()}", content=self.photo,
                         headers={"X-Replaces-Photo-Id": str(uuid4())}).status_code, 404)
        old = self.upload()
        for expected in (200, 409):
            self.assertEqual(self.client.put(f"/api/capture/photos/{uuid4()}", content=self.photo,
                             headers={"X-Replaces-Photo-Id": old}).status_code, expected)
        with capture.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM photos").fetchone()[0], 3)

    def test_concurrent_retakes_have_one_effective_winner(self):
        from concurrent.futures import ThreadPoolExecutor
        previous = self.upload()
        def replace(_):
            with TestClient(capture.app) as client:
                client.post("/api/capture/auth", json={"code": "test-station-code"})
                return client.put(f"/api/capture/photos/{uuid4()}", content=self.photo,
                                  headers={"X-Replaces-Photo-Id": previous}).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(replace, range(2))), [200, 409])
        self.assertEqual(self.client.get("/api/capture/history").json()["total"], 1)

    def test_rotation_persists_without_changing_original_or_record(self):
        photo_id = str(uuid4())
        landscape = io.BytesIO()
        Image.new("RGB", (80, 40), "red").save(landscape, format="JPEG")
        self.client.put(f"/api/capture/photos/{photo_id}", content=landscape.getvalue())
        path = f"/api/capture/photos/{photo_id}"
        self.assertEqual(self.client.get(path + "/details").json()["display_rotation"], 90)
        for _ in range(2):
            self.assertEqual(self.client.put(path + "/rotation", json={"rotation": 180, "expected_rotation": 90}).status_code, 200)
        self.assertEqual(self.client.get(path + "/details").json()["display_rotation"], 180)
        self.assertEqual(self.client.get(path).content, landscape.getvalue())
        self.assertEqual(self.client.put(path + "/rotation", json={"rotation": 270, "expected_rotation": 0}).status_code, 409)
        self.assertEqual(self.client.put(path + "/rotation", json={"rotation": 45, "expected_rotation": 180}).status_code, 422)
        self.assertEqual(self.client.put(f"/api/capture/photos/{uuid4()}", content=self.photo,
                         headers={"X-Display-Rotation": "bad"}).status_code, 422)
        self.assertEqual(self.client.get("/api/capture/state").json()["recent"], [])

    def test_exif_orientation_matches_browser_and_thumbnail(self):
        photo_id = str(uuid4())
        encoded = io.BytesIO()
        original = Image.new("RGB", (80, 40), "red")
        exif = Image.Exif()
        exif[274] = 6
        original.save(encoded, format="JPEG", exif=exif)
        self.assertEqual(self.client.put(f"/api/capture/photos/{photo_id}", content=encoded.getvalue()).status_code, 200)
        details = self.client.get(f"/api/capture/photos/{photo_id}/details").json()
        self.assertEqual((details["width"], details["height"], details["display_rotation"]), (40, 80, 0))

    def test_worker_skips_superseded_queue_entries(self):
        previous = self.upload()
        replacement = str(uuid4())
        self.client.put(f"/api/capture/photos/{replacement}", content=self.photo,
                        headers={"X-Replaces-Photo-Id": previous})
        with patch.object(capture_ocr, "configured", return_value=True), patch.object(
                capture_ocr, "recognize", side_effect=capture_ocr.OcrError("test failure")) as recognize:
            self.assertTrue(capture_tasks.process_one())
            self.assertFalse(capture_tasks.process_one())
            self.assertEqual(recognize.call_count, 1)
            self.assertEqual(self.client.post(f"/api/capture/photos/{previous}/recognize").status_code, 409)
        with capture.connection() as db:
            self.assertEqual(db.execute("SELECT photo_id FROM photo_ocr_attempts").fetchone()[0], replacement)

    def upload(self):
        photo_id = str(uuid4())
        response = self.client.put(f"/api/capture/photos/{photo_id}", content=self.photo)
        self.assertEqual(response.status_code, 200, response.text)
        return photo_id

    def payload(self, **overrides):
        return {"id": str(uuid4()), "photo_id": self.upload(), "coil_no": " coil-01 ",
                "alloy": "1060", "incoming_state": "O", "started_at": "2026-09-05T08:00:00+08:00",
                "selected_pass_no": 1, **overrides}

    def test_complete_pass_persists_across_connections_and_retries(self):
        body = self.payload()
        response = self.client.post("/api/capture/records", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        row = response.json()
        self.assertEqual(row["started_at"], "2026-09-05T00:00:00.000+00:00")
        self.assertEqual(row["coil_no"], "COIL-01")
        self.assertEqual(self.client.post("/api/capture/records", json=body).json()["id"], row["id"])
        with TestClient(capture.app, cookies=self.client.cookies) as refreshed:
            self.assertEqual(refreshed.get("/api/capture/state").json()["active"]["id"], row["id"])
            self.assertEqual(refreshed.get(row["photo_url"]).content, self.photo)
        end = f"/api/capture/records/{row['id']}/end"
        self.assertEqual(self.client.post(end, json={"ended_at": "2026-09-04T23:00:00Z"}).status_code, 422)
        first_end = self.client.post(end, json={"ended_at": "2026-09-05T00:30:00Z"}).json()
        retry = self.client.post(end, json={"ended_at": "2026-09-05T00:31:00Z"}).json()
        self.assertEqual(retry["ended_at"], first_end["ended_at"])
        self.assertIsNone(self.client.get("/api/capture/state").json()["active"])

    def test_only_one_active_pass_and_mismatched_retry_rejected(self):
        body = self.payload()
        self.assertEqual(self.client.post("/api/capture/records", json=body).status_code, 200)
        self.assertEqual(self.client.post("/api/capture/records", json=self.payload()).status_code, 409)
        self.assertEqual(self.client.post("/api/capture/records", json={**body, "alloy": "3003"}).status_code, 409)

    def test_pass_numbers_follow_coil_and_time(self):
        for coil, hour in [("A", 2), ("B", 3), ("A", 1)]:
            body = self.payload(coil_no=coil, started_at=f"2026-09-05T0{hour}:00:00Z")
            self.assertEqual(self.client.post("/api/capture/records", json=body).status_code, 200)
            self.client.post(f"/api/capture/records/{body['id']}/end", json={"ended_at": f"2026-09-05T0{hour}:10:00Z"})
        rows = self.client.get("/api/capture/state").json()["recent"]
        self.assertEqual([(r["coil_no"], r["pass_no"]) for r in rows], [("B", 1), ("A", 2), ("A", 1)])

    def test_selected_pass_is_required_and_preserved_across_views(self):
        invalid = self.payload(selected_pass_no=0)
        for value in (None, 0, 6):
            body = {**invalid, "selected_pass_no": value}
            self.assertEqual(self.client.post("/api/capture/records", json=body).status_code, 422)
        with capture.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM capture_records").fetchone()[0], 0)
        for n in (1, 5):
            body = self.payload(selected_pass_no=n, coil_no="", alloy="", incoming_state="",
                                started_at=f"2026-09-05T0{n}:00:00Z")
            response = self.client.post("/api/capture/records", json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["selected_pass_no"], n)
            self.assertEqual(self.client.post("/api/capture/records", json=body).json()["selected_pass_no"], n)
            self.assertEqual(self.client.post("/api/capture/records", json={**body, "selected_pass_no": 3}).status_code, 409)
            self.assertEqual(self.client.get("/api/capture/state").json()["active"]["selected_pass_no"], n)
            self.assertEqual(self.client.get(f"/api/capture/photos/{body['photo_id']}/details").json()["selected_pass_no"], n)
            self.client.post(f"/api/capture/records/{body['id']}/end", json={"ended_at": f"2026-09-05T0{n}:30:00Z"})
        rows = list(csv.DictReader(io.StringIO(self.client.get("/api/capture/export.csv").text.lstrip("\ufeff"))))
        self.assertEqual([r["selected_pass_no"] for r in rows], ["1", "5"])
        self.assertEqual([r["pass_no"] for r in rows], ["", ""])

    def test_photo_only_records_persist_without_guessing_metadata_or_pass_number(self):
        bodies = []
        for hour in (1, 2):
            body = {"id": str(uuid4()), "photo_id": self.upload(),
                    "started_at": f"2026-09-05T0{hour}:00:00Z", "selected_pass_no": hour}
            bodies.append(body)
            response = self.client.post("/api/capture/records", json=body)
            self.assertEqual(response.status_code, 200, response.text)
            row = response.json()
            for field in ("coil_no", "alloy", "incoming_state", "pass_no"):
                self.assertIsNone(row[field], field)
            self.assertEqual(self.client.post("/api/capture/records", json=body).json()["id"], body["id"])
            with TestClient(capture.app, cookies=self.client.cookies) as refreshed:
                active = refreshed.get("/api/capture/state").json()["active"]
                self.assertEqual(active["photo_id"], body["photo_id"])
                self.assertEqual(refreshed.get(active["photo_url"]).content, self.photo)
            end = f"/api/capture/records/{body['id']}/end"
            ended_at = f"2026-09-05T0{hour}:30:00Z"
            self.assertEqual(self.client.post(end, json={"ended_at": ended_at}).status_code, 200)
        state = self.client.get("/api/capture/state").json()
        self.assertIsNone(state["active"])
        self.assertEqual(len(state["recent"]), 2)
        for row in state["recent"]:
            self.assertIsNone(row["pass_no"])
            self.assertIsNotNone(row["ended_at"])
        rows = list(csv.DictReader(io.StringIO(self.client.get("/api/capture/export.csv").text.lstrip("\ufeff"))))
        self.assertEqual(len(rows), 2)
        for row in rows:
            for field in ("coil_no", "alloy", "incoming_state", "pass_no"):
                self.assertEqual(row[field], "")

        # Simulate future OCR backfill: numbering is derived from the identified coil.
        with capture.connection() as db:
            db.execute("UPDATE capture_records SET coil_no='COIL-A',alloy='1060',incoming_state='O'")
        rows = self.client.get("/api/capture/state").json()["recent"]
        self.assertEqual([(r["id"], r["pass_no"]) for r in rows],
                         [(bodies[1]["id"], 2), (bodies[0]["id"], 1)])
        retry = self.client.post("/api/capture/records", json=bodies[0])
        self.assertEqual(retry.status_code, 200, retry.text)
        self.assertEqual(retry.json()["coil_no"], "COIL-A")
        self.assertIsNone(self.client.get("/api/capture/state").json()["active"])

    def test_auth_origin_and_private_export(self):
        with TestClient(capture.app) as anonymous:
            self.assertEqual(anonymous.get("/api/capture/health").status_code, 200)
            for path in ["/state", "/export.csv", f"/photos/{uuid4()}"]:
                self.assertEqual(anonymous.get("/api/capture" + path).status_code, 401)
            self.assertEqual(anonymous.post("/api/capture/auth", json={"code": "错误"}).status_code, 401)
            self.assertEqual(anonymous.post("/api/capture/auth", json={"code": "test-station-code"},
                                           headers={"Origin": "https://evil.example"}).status_code, 403)
        body = self.payload(coil_no="=FORMULA")
        self.client.post("/api/capture/records", json=body)
        exported = self.client.get("/api/capture/export.csv")
        self.assertIn("'=FORMULA", exported.text)
        self.assertIn("no-store", exported.headers["cache-control"])

    def test_invalid_images_and_missing_photo_rejected(self):
        self.assertEqual(self.client.put(f"/api/capture/photos/{uuid4()}", content=b"not an image").status_code, 422)
        body = self.payload(photo_id=str(uuid4()))
        self.assertEqual(self.client.post("/api/capture/records", json=body).status_code, 422)
        photo_id = self.upload()
        self.assertEqual(self.client.put(f"/api/capture/photos/{photo_id}", content=self.photo).status_code, 200)
        with patch.object(capture, "MAX_PHOTO_BYTES", 10):
            self.assertEqual(self.client.put(f"/api/capture/photos/{uuid4()}", content=self.photo).status_code, 413)

    def test_history_uses_beijing_photo_date_descending_and_includes_unstarted_photos(self):
        ids = []
        for stamp in ("2026-09-07T15:59:59Z", "2026-09-07T16:00:00Z", "2026-09-08T15:59:59Z", "2026-09-08T16:00:00Z"):
            photo_id = str(uuid4())
            ids.append(photo_id)
            response = self.client.put(f"/api/capture/photos/{photo_id}", content=self.photo, headers={"X-Capture-Time": stamp})
            self.assertEqual(response.status_code, 200)
        response = self.client.get("/api/capture/history?day=2026-09-08&limit=1")
        self.assertEqual(response.status_code, 200, response.text)
        result = response.json()
        self.assertEqual(result["total"], 2)
        self.assertTrue(result["has_more"])
        self.assertEqual(result["items"][0]["photo_id"], ids[2])
        self.assertIsNone(result["items"][0]["record_id"])
        next_page = self.client.get("/api/capture/history?day=2026-09-08&limit=1&offset=1").json()
        self.assertEqual(next_page["items"][0]["photo_id"], ids[1])
        self.assertFalse(next_page["has_more"])
        self.assertEqual(self.client.get("/api/capture/history?day=invalid").status_code, 422)
        self.assertEqual(self.client.get("/api/capture/history?offset=-1").status_code, 422)
        self.assertEqual(self.client.get("/api/capture/history").json()["date"],
                         capture.datetime.now(capture.ZoneInfo("Asia/Shanghai")).date().isoformat())
        with TestClient(capture.app) as anonymous:
            self.assertEqual(anonymous.get("/api/capture/history").status_code, 401)
            self.assertEqual(anonymous.get(f"/api/capture/photos/{ids[0]}/details").status_code, 401)

    def test_ocr_returns_structured_fields_but_does_not_guess_pass_identity_or_state(self):
        photo_id = self.upload()
        result = {"fields": {"batch_no": "09101", "alloy": "3003", "card_state": "H14",
                             "coil_no": None, "incoming_state": None, "anneal_mentioned": True},
                  "warnings": [], "raw": {"unit_test": True}, "model": "test-model", "request_id": "test-request",
                  "prompt_version": "test-prompt"}
        body = {"id": str(uuid4()), "photo_id": photo_id, "started_at": "2026-09-08T02:00:00Z", "selected_pass_no": 1}
        self.assertEqual(self.client.post("/api/capture/records", json=body).status_code, 200)
        with patch.object(capture_ocr, "configured", return_value=True), patch.object(capture_ocr, "recognize", return_value=result) as recognize:
            self.assertTrue(capture_tasks.process_one())
            self.assertFalse(capture_tasks.process_one())
            self.assertEqual(recognize.call_count, 1)
        detail = self.client.get(f"/api/capture/photos/{photo_id}/details").json()
        self.assertEqual(detail["ocr_status"], "succeeded")
        self.assertEqual(detail["fields"]["batch_no"], "09101")
        active = self.client.get("/api/capture/state").json()["active"]
        for field in ("coil_no", "incoming_state", "pass_no"):
            self.assertIsNone(active[field])
        with capture.connection() as db:
            audit = db.execute("SELECT * FROM photo_ocr_attempts WHERE photo_id=?", (photo_id,)).fetchone()
            self.assertEqual(json.loads(audit["raw_json"]), {"unit_test": True})
        self.assertEqual(self.client.post(f"/api/capture/records/{body['id']}/end", json={"ended_at": "2026-09-08T02:30:00Z"}).status_code, 200)
        reuse = {**body, "id": str(uuid4())}
        self.assertEqual(self.client.post("/api/capture/records", json=reuse).status_code, 409)

    def test_ocr_retry_is_durable_and_bounded_and_does_not_block_capture(self):
        photo_id = self.upload()
        self.assertEqual(self.client.post(f"/api/capture/photos/{photo_id}/recognize").status_code, 503)
        with patch.object(capture_ocr, "configured", return_value=True), patch.object(capture_ocr, "recognize", side_effect=capture_ocr.OcrError("OCR 连接超时", True)):
            for attempt in range(3):
                with capture.connection() as db:
                    db.execute("UPDATE photo_ocr SET next_attempt=0 WHERE photo_id=?", (photo_id,))
                self.assertTrue(capture_tasks.process_one())
            self.assertFalse(capture_tasks.process_one())
        detail = self.client.get(f"/api/capture/photos/{photo_id}/details").json()
        self.assertEqual(detail["ocr_status"], "failed")
        self.assertEqual(self.client.get(detail["photo_url"]).content, self.photo)
        body = {"id": str(uuid4()), "photo_id": photo_id, "started_at": "2026-09-08T02:00:00Z", "selected_pass_no": 1}
        self.assertEqual(self.client.post("/api/capture/records", json=body).status_code, 200)
        with patch.object(capture_ocr, "configured", return_value=True):
            self.assertEqual(self.client.post(f"/api/capture/photos/{photo_id}/recognize").status_code, 200)
            self.assertEqual(self.client.post(f"/api/capture/photos/{photo_id}/recognize").status_code, 200)
        with capture.connection() as db:
            row = db.execute("SELECT * FROM photo_ocr WHERE photo_id=?", (photo_id,)).fetchone()
            self.assertEqual((row["status"], row["attempts"]), ("queued", 0))
            self.assertEqual(db.execute("SELECT COUNT(*) FROM photo_ocr_attempts").fetchone()[0], 3)

    def test_expired_worker_lease_is_recovered(self):
        photo_id = self.upload()
        with capture.connection() as db:
            db.execute("UPDATE photo_ocr SET status='processing',lease_until=0 WHERE photo_id=?", (photo_id,))
        with patch.object(capture_ocr, "configured", return_value=True), patch.object(capture_ocr, "recognize", side_effect=capture_ocr.OcrError("需要检查凭证")):
            self.assertTrue(capture_tasks.process_one())
        self.assertEqual(self.client.get(f"/api/capture/photos/{photo_id}/details").json()["ocr_status"], "failed")

    def test_legacy_database_upgrade_preserves_data_and_adds_pending_ocr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with closing(sqlite3.connect(root / "records.sqlite3")) as db:
                db.execute("CREATE TABLE photos(id TEXT PRIMARY KEY,filename TEXT NOT NULL,media_type TEXT NOT NULL,sha256 TEXT NOT NULL,created_at TEXT NOT NULL)")
                photo_id = str(uuid4())
                original = (photo_id, "legacy.jpg", "image/jpeg", "original-hash", "2026-09-08T01:00:00.000+00:00")
                db.execute("INSERT INTO photos VALUES(?,?,?,?,?)", original)
                db.commit()
            with patch.object(capture, "ROOT", root):
                for _ in range(2):
                    with capture.connection() as db:
                        self.assertEqual(tuple(db.execute("SELECT id,filename,media_type,sha256,created_at FROM photos").fetchone()), original)
                        self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
                        self.assertEqual(db.execute("SELECT status FROM photo_ocr").fetchone()[0], "not_configured")
                self.assertEqual(self.client.get("/api/capture/history?day=2026-09-08").json()["total"], 1)

    def test_version_two_upgrade_keeps_existing_record_without_guessing_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            photo_id, record_id = str(uuid4()), str(uuid4())
            with closing(sqlite3.connect(root / "records.sqlite3")) as db:
                db.executescript("""CREATE TABLE photos (
                    id TEXT PRIMARY KEY, filename TEXT NOT NULL, media_type TEXT NOT NULL,
                    sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
                    CREATE TABLE capture_records (
                    id TEXT PRIMARY KEY, photo_id TEXT NOT NULL REFERENCES photos(id),
                    coil_no TEXT NOT NULL, alloy TEXT NOT NULL, incoming_state TEXT NOT NULL,
                    started_at TEXT NOT NULL, start_received_at TEXT NOT NULL,
                    ended_at TEXT, end_received_at TEXT, machine_id TEXT NOT NULL DEFAULT 'cold-tandem-1');
                    PRAGMA user_version=2;""")
                db.execute("INSERT INTO photos VALUES(?,?,?,?,?)",
                           (photo_id, "old.jpg", "image/jpeg", "original", "2026-09-08T01:00:00Z"))
                db.execute("INSERT INTO capture_records(id,photo_id,coil_no,alloy,incoming_state,started_at,start_received_at) VALUES(?,?,?,?,?,?,?)",
                           (record_id, photo_id, "", "", "", "2026-09-08T02:00:00Z", "2026-09-08T02:00:01Z"))
                db.commit()
            with patch.object(capture, "ROOT", root):
                with capture.connection() as db:
                    self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)
                    row = db.execute("SELECT * FROM capture_records WHERE id=?", (record_id,)).fetchone()
                    self.assertEqual(row["photo_id"], photo_id)
                    self.assertIsNone(row["selected_pass_no"])

    def test_core_ocr_preserves_zero_and_ignores_unneeded_fields(self):
        fields, warnings = capture_ocr.normalize_result(json.dumps({
            "batch_no": "09101", "alloy": "3003", "card_state": "h14",
            "finished_spec": "2.82±0.04×1400±3×C", "process_route": "中退",
            "coil_no": "1卷", "incoming_state": "O", "raw_text": "批号:09101 牌号:3003 状态:H14"}))
        self.assertEqual(fields["batch_no"], "09101")
        self.assertEqual(fields["alloy"], "3003")
        self.assertEqual(fields["card_state"], "H14")
        self.assertEqual(set(fields), {"batch_no", "alloy", "card_state", "raw_text"})
        self.assertEqual(warnings, [])
        fields, warnings = capture_ocr.normalize_result('{"batch_no":9101,"alloy":"30?3","card_state":"0"}')
        self.assertIsNone(fields["batch_no"])
        self.assertEqual(fields["card_state"], "0")
        self.assertTrue(any("状态" in w for w in warnings))
        with self.assertRaises(capture_ocr.OcrError):
            capture_ocr.normalize_result("not json")

    def test_batch_length_is_review_signal_not_truncation(self):
        for batch in ("095180", "091580"):
            fields, warnings = capture_ocr.normalize_result(json.dumps({"batch_no": batch, "alloy": "1060", "card_state": "H18"}))
            self.assertEqual(fields["batch_no"], batch)
            self.assertTrue(any("批号为六位" in w for w in warnings))
        for batch in ("9716", "０９５８０"):
            fields, warnings = capture_ocr.normalize_result(json.dumps({"batch_no": batch, "alloy": "1060", "card_state": "H18"}))
            self.assertEqual(fields["batch_no"], batch)
            self.assertTrue(any("批号" in w for w in warnings))
        fields, warnings = capture_ocr.normalize_result('{"batch_no":"09580","alloy":"null","card_state":"H18"}')
        self.assertEqual(fields["batch_no"], "09580")
        self.assertIsNone(fields["alloy"])
        self.assertIn("未识别到牌号", warnings)

    def test_confirmed_batch_survives_ocr_retry_and_keeps_raw_audit(self):
        from backend.app.capture_corrections import correct_field
        photo_id = self.upload()
        raw = {"batch_no": "091580", "alloy": "1050标1060", "card_state": "H18"}
        fields, warnings = capture_ocr.normalize_result(json.dumps(raw))
        result = {"fields": fields, "warnings": warnings, "raw": raw,
                  "model": "test", "prompt_version": capture_ocr.PROMPT_VERSION, "request_id": "test"}
        with patch.object(capture_ocr, "configured", return_value=True), patch.object(capture_ocr, "recognize", return_value=result):
            self.assertTrue(capture_tasks.process_one())
            correct_field(photo_id, "batch_no", "091580", "09580", "user confirmed original card")
            with self.assertRaises(ValueError):
                correct_field(photo_id, "batch_no", "091580", "09581", "stale review")
            self.client.post(f"/api/capture/photos/{photo_id}/recognize")
            self.assertTrue(capture_tasks.process_one())
        detail = self.client.get(f"/api/capture/photos/{photo_id}/details").json()
        self.assertEqual(detail["fields"]["batch_no"], "09580")
        self.assertEqual(detail["ocr_status"], "succeeded")
        self.assertFalse(any("批号" in w for w in detail["warnings"]))
        self.assertFalse(any("牌号" in w for w in detail["warnings"]))
        with capture.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM capture_field_corrections").fetchone()[0], 1)
            attempts = db.execute("SELECT raw_json FROM photo_ocr_attempts").fetchall()
            self.assertEqual(len(attempts), 2)
            self.assertTrue(all(json.loads(r[0])["batch_no"] == "091580" for r in attempts))

    def test_batch_export_joins_ocr_and_preserves_pass_identity(self):
        import csv
        for hour in (2, 1):
            body = self.payload(coil_no="", alloy="", incoming_state="", started_at=f"2026-09-05T0{hour}:00:00Z")
            self.assertEqual(self.client.post("/api/capture/records", json=body).status_code, 200)
            self.client.post(f"/api/capture/records/{body['id']}/end", json={"ended_at": f"2026-09-05T0{hour}:30:00Z"})
            with capture.connection() as db:
                db.execute("UPDATE photo_ocr SET fields_json=?,status='succeeded',prompt_version=? WHERE photo_id=?",
                           (json.dumps({"batch_no":"09101","alloy":"3003","card_state":"H14"}),
                            capture_ocr.PROMPT_VERSION, body["photo_id"]))
        rows = self.client.get("/api/capture/state").json()["recent"]
        self.assertEqual([r["batch_sequence"] for r in rows], [2, 1])
        self.assertEqual([r["batch_no"] for r in rows], ["09101", "09101"])
        self.assertTrue(all(r["pass_no"] is None and r["incoming_state"] is None for r in rows))
        exported = list(csv.DictReader(io.StringIO(self.client.get("/api/capture/export.csv").text.lstrip("\ufeff"))))
        self.assertEqual(exported[0]["batch_no"], "09101")
        self.assertEqual(exported[0]["ocr_alloy"], "3003")
        self.assertEqual(exported[0]["card_state"], "H14")

    def test_thumbnails_and_verified_oss_archive_preserve_original_and_remain_private(self):
        photo_id = self.upload()
        thumbnail = self.client.get(f"/api/capture/photos/{photo_id}/thumbnail")
        self.assertEqual(thumbnail.status_code, 200)
        with Image.open(io.BytesIO(thumbnail.content)) as image:
            self.assertLessEqual(max(image.size), 400)

        class FakeBucket:
            def __init__(self):
                self.objects = {}

            def put_object_from_file(self, key, filename, headers):
                self.objects[key] = (Path(filename).read_bytes(), headers)

            def head_object(self, key):
                raw, headers = self.objects[key]
                return SimpleNamespace(content_length=len(raw), headers=headers)

            def get_object(self, key):
                return io.BytesIO(self.objects[key][0])

            def sign_url(self, method, key, expires):
                return "https://private-photos.example.test/" + key + "?signed=example"

        bucket = FakeBucket()
        with patch.object(capture_storage, "configured", return_value=True), patch.object(capture_storage, "bucket", return_value=bucket), patch.object(capture_storage, "_retry_after", 0):
            self.assertTrue(capture_storage.archive_one())
            with capture.connection() as db:
                row = db.execute("SELECT * FROM photos WHERE id=?", (photo_id,)).fetchone()
            self.assertFalse((capture.ROOT / "photos" / row["filename"]).exists())
            self.assertEqual(capture_storage.photo_bytes(capture.ROOT, row), self.photo)
            self.assertEqual(len(bucket.objects), 2)
            self.assertEqual(self.client.get(f"/api/capture/photos/{photo_id}", follow_redirects=False).status_code, 302)
            self.assertEqual(self.client.get(f"/api/capture/photos/{photo_id}/thumbnail", follow_redirects=False).status_code, 302)
            with TestClient(capture.app) as anonymous:
                self.assertEqual(anonymous.get(f"/api/capture/photos/{photo_id}", follow_redirects=False).status_code, 401)

    def test_failed_oss_verification_never_removes_local_photo(self):
        photo_id = self.upload()
        with patch.object(capture_storage, "configured", return_value=True), patch.object(capture_storage, "bucket") as bucket, patch.object(capture_storage, "_retry_after", 0):
            bucket.return_value.head_object.return_value = SimpleNamespace(content_length=0, headers={})
            self.assertFalse(capture_storage.archive_one())
        self.assertEqual(self.client.get(f"/api/capture/photos/{photo_id}").content, self.photo)
        with capture.connection() as db:
            self.assertIsNone(db.execute("SELECT oss_key FROM photos WHERE id=?", (photo_id,)).fetchone()[0])

    def test_cloud_ocr_request_and_response_contract(self):
        reply = {"id": "test-request", "model": "test-model", "choices": [{"finish_reason": "stop", "message": {
            "content": json.dumps({"batch_no": "09101", "alloy": "3003", "card_state": "H14", "raw_text": "批号：09101 牌号：3003 状态：H14"}, ensure_ascii=False)}}]}
        with patch.dict(capture_ocr.os.environ, {"CAPTURE_OCR_API_KEY": "unit-test-only"}), patch.object(capture_ocr, "urlopen", return_value=io.BytesIO(json.dumps(reply).encode())) as http:
            result = capture_ocr.recognize(self.photo)
            request = http.call_args.args[0]
            payload = json.loads(request.data)
            self.assertTrue(request.full_url.endswith("/chat/completions"))
            self.assertTrue(payload["messages"][0]["content"][0]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
            self.assertEqual(result["fields"]["batch_no"], "09101")
            self.assertEqual(result["request_id"], "test-request")

    def test_focused_retry_recovers_misaligned_fields_without_guessing_batch(self):
        def answer(fields):
            return io.BytesIO(json.dumps({"id": "test", "choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(fields, ensure_ascii=False)}}]}).encode())
        primary = {"batch_no": "10156", "alloy": "H18", "card_state": None}
        focused = {"batch_no": "10156", "alloy": "1060", "card_state": "H18"}
        with patch.dict(capture_ocr.os.environ, {"CAPTURE_OCR_API_KEY": "unit-test-only"}), patch.object(
                capture_ocr, "urlopen", side_effect=[answer(primary), answer(focused)]) as http:
            result = capture_ocr.recognize(self.photo)
        self.assertEqual(http.call_count, 2)
        self.assertEqual({k: result["fields"][k] for k in capture_ocr.FIELDS}, focused)
        self.assertEqual(result["warnings"], [])
        self.assertIn("focused", result["raw"])

    def test_review_records_ground_truth_and_survives_retry(self):
        photo_id = self.upload()
        with capture.connection() as db:
            db.execute("UPDATE photo_ocr SET status='needs_review',fields_json=?,warnings_json=? WHERE photo_id=?",
                       (json.dumps({"batch_no": "10156", "alloy": "H18", "card_state": None}),
                        json.dumps(["牌号与常见书写格式不符，请核对原图", "未识别到状态"]), photo_id))
        endpoint = f"/api/capture/photos/{photo_id}/review"
        payload = {"expected_fields": {"batch_no": "10156", "alloy": "H18", "card_state": None},
                   "batch_no": "10156", "alloy": "1060", "card_state": "H18", "model_alloy": "1060"}
        self.assertEqual(self.client.post(endpoint, json=payload).status_code, 200)
        detail = self.client.get(f"/api/capture/photos/{photo_id}/details").json()
        self.assertEqual(detail["fields"]["alloy"], "1060")
        self.assertTrue(detail["reviewed_at"] and detail["labels_ready"])
        self.assertEqual(detail["reviewed_model_alloy"], "1060")
        self.assertEqual(self.client.post(endpoint, json=payload).status_code, 409)
        with capture.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM capture_field_corrections").fetchone()[0], 2)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM capture_photo_reviews").fetchone()[0], 1)
            db.execute("UPDATE photo_ocr SET fields_json=? WHERE photo_id=?",
                       (json.dumps({"batch_no": "10156", "alloy": "3003", "card_state": "H18"}), photo_id))
        self.assertFalse(self.client.get(f"/api/capture/photos/{photo_id}/details").json()["labels_ready"])


if __name__ == "__main__":
    unittest.main()
