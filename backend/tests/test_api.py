import json
import os
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect, text


test_database = Path(tempfile.gettempdir()) / f"cold_rolling_api_test_{os.getpid()}.db"
test_database.unlink(missing_ok=True)
os.environ["DATABASE_URL"] = f"sqlite:///{test_database.as_posix()}"
from app.main import app
from app.database import ensure_schema_compatibility
from app.recommendation import ROLLBACK_ARTIFACT_PATH


client = TestClient(app)


def test_health():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_seeded_products_and_batches():
    with client:
        products = client.get("/api/products")
        batches = client.get("/api/batches")
    assert products.status_code == 200
    assert len(products.json()) == 2
    assert batches.status_code == 200
    assert {item["batch_no"] for item in batches.json()} == {"07312", "07360", "061071"}


def test_recommendation_uses_direct_model_without_historical_lookup():
    request = {
        "entry_thickness": 1.5,
        "target_exit_thickness": 0.77,
        "incoming_width": 1213,
    }
    with client:
        response = client.post(
            "/api/recommendations",
            json=request,
        )
        repeated = client.post("/api/recommendations", json=request)
    assert response.status_code == 200
    payload = response.json()
    assert repeated.status_code == 200
    assert payload["status"] == "recommendable"
    assert payload["result_payload"]["can_apply"] is False
    assert payload["model_version"] == "direct-ridge-startup-flatness-v0.4.1"
    assert payload["result_payload"]["model"]["training_datasets"] == [
        "260712",
        "260713",
        "260714",
        "260716",
        "260717",
        "260718",
    ]
    assert payload["result_payload"]["model"]["validation_dataset"] == "260719"
    assert payload["result_payload"]["model"]["historical_neighbour_method_used_at_inference"] is False
    assert payload["result_payload"]["similar_cases"] == []
    parameters = payload["result_payload"]["parameters"]
    assert len(parameters) == 6
    assert {
        "intermediate_thickness_set_mode",
        "tm1_closed_gap_set_median",
        "tm2_closed_gap_set_median",
        "uncoiler_tension_set_median",
        "interstand_tension_set_median",
        "coiler_tension_set_median",
    } <= {row["code"] for row in parameters}
    assert [row["code"] for row in parameters] == [
        "intermediate_thickness_set_mode",
        "uncoiler_tension_set_median",
        "interstand_tension_set_median",
        "coiler_tension_set_median",
        "tm1_closed_gap_set_median",
        "tm2_closed_gap_set_median",
    ]
    labels = {row["code"]: row["label"] for row in parameters}
    assert labels["intermediate_thickness_set_mode"] == "中间厚度推荐"
    assert labels["uncoiler_tension_set_median"] == "开卷张力推荐"
    assert labels["interstand_tension_set_median"] == "机架张力推荐"
    assert labels["coiler_tension_set_median"] == "卷取张力推荐"
    assert labels["tm1_closed_gap_set_median"] == "F1推荐辊缝"
    assert labels["tm2_closed_gap_set_median"] == "F2推荐辊缝"
    assert all("推荐辊缝" in labels[code] for code in (
        "tm1_closed_gap_set_median",
        "tm2_closed_gap_set_median",
    ))
    assert all("安全首设辊缝" not in row["label"] for row in parameters)
    assert all("闭合辊缝首设" not in row["label"] for row in parameters)
    assert all("保守首设" not in row["label"] for row in parameters)
    risks = " ".join(payload["result_payload"]["risks"])
    assert "高速稳定生产段训练得到" in risks
    assert "入口在线实测厚度可由DAT自动传入" not in risks
    assert "未提供入口在线实测厚度" not in risks
    assert "保守首设" not in risks
    assert payload["result_payload"]["flatness_monitor"]["zone_count"] == 49
    assert payload["result_payload"]["flatness_monitor"]["actionable"] is False
    assert [row["reference"] for row in parameters] == [
        row["reference"] for row in repeated.json()["result_payload"]["parameters"]
    ]


def test_v04_artifact_is_preserved_for_rollback():
    v4_path = ROLLBACK_ARTIFACT_PATH
    payload = json.loads(v4_path.read_text(encoding="utf-8"))
    assert payload["model_version"] == "direct-ridge-startup-flatness-v0.4"
    assert payload["models"]["tm1_closed_gap_set_median"]["label"] == "F1闭合辊缝首设"
    assert payload["models"]["tm2_closed_gap_set_median"]["label"] == "F2闭合辊缝首设"
    assert payload["training_case_count"] == 281
    assert payload["validation_case_count"] == 64


def test_direct_model_rejects_out_of_coverage_specification():
    with client:
        response = client.post(
            "/api/recommendations",
            json={
                "entry_thickness": 10,
                "target_exit_thickness": 1,
                "incoming_width": 1213,
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "insufficient_data"
    assert payload["result_payload"]["parameters"] == []
    assert "入口厚度" in payload["result_payload"]["coverage"]["out_of_range_features"]


def test_model_status_reports_frozen_independent_validation():
    with client:
        response = client.get("/api/model/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["training_case_count"] == 281
    assert payload["validation_case_count"] == 64
    assert payload["high_speed_only"] is True
    assert payload["direct_win_count"] == 5
    assert payload["target_count"] == 6
    assert payload["mean_mae_improvement_percent"] > 0
    assert payload["historical_neighbour_method_used_at_inference"] is False


def test_dat_dataset_status_separates_training_and_validation():
    with client:
        response = client.get("/api/dat/datasets")
    assert response.status_code == 200
    payload = {row["dataset"]: row for row in response.json()}
    assert payload["260712"]["role"] == "training"
    assert payload["260713"]["role"] == "training"
    assert payload["260714"]["role"] == "training"
    assert payload["260714"]["learning_eligible_count"] == 32
    assert payload["260719"]["role"] == "validation"
    assert payload["260719"]["learning_eligible_count"] == 64


def test_pass_workflow_keeps_sample_and_entry_times_separate():
    with client:
        created = client.post(
            "/api/passes",
            json={
                "coil_no": "TEST-COIL-001",
                "pass_no": 1,
                "entry_thickness": 1.5,
                "target_exit_thickness": 0.77,
                "incoming_width": 1213,
                "operator_name": "测试主操",
                "started_at": "2026-07-15T08:00",
            },
        )
        assert created.status_code == 200
        pass_id = created.json()["id"]

        sample = client.post(
            f"/api/passes/{pass_id}/samples",
            json={
                "sampled_at": "2026-07-15T08:04",
                "entered_at": "2026-07-15T08:06",
                "manual_thickness": 0.775,
                "laser_thickness": 0.771,
                "supplement_percent": 0.3,
                "measurer": "测试副手",
            },
        )
        assert sample.status_code == 200

        adjustment = client.post(
            f"/api/passes/{pass_id}/adjustments",
            json={
                "adjusted_at": "2026-07-15T08:07",
                "parameter_code": "f1_gap",
                "parameter_label": "F1辊缝",
                "before_value": 0.42,
                "after_value": 0.41,
                "unit": "mm",
                "operator_name": "测试主操",
            },
        )
        assert adjustment.status_code == 200

        event = client.post(
            f"/api/passes/{pass_id}/events",
            json={
                "event_type": "high_speed_started",
                "occurred_at": "2026-07-15T08:10",
                "notes": "人工厚度合格",
            },
        )
        assert event.status_code == 200
        assert event.json()["status"] == "high_speed"

        detail = client.get(f"/api/passes/{pass_id}")
        assert detail.status_code == 200
        payload = detail.json()
        assert payload["pass"]["high_speed_at"] == "2026-07-15T08:10"
        assert payload["samples"][0]["sampled_at"] == "2026-07-15T08:04"
        assert payload["samples"][0]["entered_at"] == "2026-07-15T08:06"
        assert payload["adjustments"][0]["before_value"] == 0.42
        assert payload["events"][0]["event_type"] == "high_speed_started"


def test_pass_rejects_non_reducing_target():
    with client:
        response = client.post(
            "/api/passes",
            json={
                "coil_no": "INVALID-COIL",
                "entry_thickness": 0.77,
                "target_exit_thickness": 0.77,
                "incoming_width": 1210,
            },
        )
    assert response.status_code == 400


def test_create_card_and_approve_without_image_upload():
    with client:
        created = client.post(
            "/api/cards",
            json={
                "batch_no": "NEW-BATCH-2026-001",
                "card_date": "2026-07-15",
                "cast_coil_no": "YH-TEST-001",
                "input_alloy": "1050",
                "nominal_alloy": "1060",
                "temper": "H18",
                "intermediate_thickness": 1.6,
                "intermediate_width": 1280,
                "trimmed_width": 1193,
                "target_thickness": 0.57,
                "target_width": 1190,
                "thickness_lower_tolerance": 0.01,
                "thickness_upper_tolerance": 0.01,
                "process_requirements": "冷轧\n重卷切边\n拉矫清洗",
                "transverse_plate_diff": [
                    {"point": 1, "value": "6.66"},
                    {"point": 2, "value": "66"},
                ],
                "longitudinal_plate_diff": [
                    {"position_mm": 0, "drive_side": "6.72", "operator_side": "6.71"},
                    {"position_mm": 400, "drive_side": "74", "operator_side": "75"},
                ],
                "surface_quality": "原卡手写内容，待人工复核",
                "process_stage_records": {
                    "cold_rolling": {
                        "after_coil_weight": "13880",
                        "after_thickness": "0.57",
                        "after_width": "1190",
                        "quality_status": "合格",
                        "self_check": "A",
                        "special_check": "A",
                        "operator_name": "测试主操",
                        "inspector_name": "测试质检",
                        "record_date": "2026-07-15",
                    },
                    "rewinding": {
                        "cut_width": "1213",
                        "split_status": "",
                        "weight": "13910",
                        "quality_status": "切边",
                        "notes": "",
                        "operator_name": "王玉良",
                        "inspector_name": "",
                        "record_date": "2026-07-12",
                    },
                },
                "review_status": "draft",
            },
        )
        assert created.status_code == 200
        card_id = created.json()["id"]
        assert created.json()["transverse_plate_diff"][1]["value"] == "66"
        assert created.json()["longitudinal_plate_diff"][0]["drive_side"] == "6.72"
        assert created.json()["surface_quality"] == "原卡手写内容，待人工复核"
        assert created.json()["process_stage_records"]["rewinding"]["cut_width"] == "1213"
        assert created.json()["process_stage_records"]["rewinding"]["quality_status"] == "切边"
        assert created.json()["process_stage_records"]["packaging"]["finished_weight"] == ""

        upload_endpoint = client.post(
            f"/api/cards/{card_id}/attachments",
            params={"filename": "production-card.jpg", "card_type": "production_follow"},
            content=b"fake-jpeg-card-content",
            headers={"content-type": "image/jpeg"},
        )
        assert upload_endpoint.status_code == 404

        approved = client.patch(f"/api/cards/{card_id}", json={"review_status": "approved"})
        assert approved.status_code == 200
        assert approved.json()["review_status"] == "approved"
        cards = client.get("/api/cards")
        assert cards.status_code == 200
        assert any(item["batch_no"] == "NEW-BATCH-2026-001" for item in cards.json())


def test_old_sqlite_card_table_is_additively_migrated():
    path = Path(tempfile.gettempdir()) / f"cold_rolling_old_schema_{os.getpid()}.db"
    path.unlink(missing_ok=True)
    old_engine = create_engine(f"sqlite:///{path.as_posix()}")
    with old_engine.begin() as connection:
        connection.execute(text("CREATE TABLE production_cards (id VARCHAR(36) PRIMARY KEY, batch_no VARCHAR(80) NOT NULL)"))
        connection.execute(text("INSERT INTO production_cards (id, batch_no) VALUES ('old-card', 'OLD-001')"))

    ensure_schema_compatibility(old_engine)

    columns = {column["name"] for column in inspect(old_engine).get_columns("production_cards")}
    assert {"transverse_plate_diff", "longitudinal_plate_diff", "surface_quality", "process_stage_records"} <= columns
    with old_engine.connect() as connection:
        row = connection.execute(
            text("SELECT transverse_plate_diff, longitudinal_plate_diff, surface_quality, process_stage_records FROM production_cards WHERE id='old-card'")
        ).one()
    assert tuple(row) == ("[]", "[]", "", "{}")
