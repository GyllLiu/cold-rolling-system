from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Sequence

from sqlalchemy.orm import Session

from .schemas import RecommendationRequest


ARTIFACT_PATH = (
    Path(__file__).resolve().parent
    / "model_artifacts"
    / "recommendation_direct_v3.json"
)

PARAMETER_ORDER = (
    "intermediate_thickness_set_mode",
    "tm1_gap_command_median",
    "tm2_gap_command_median",
    "uncoiler_tension_set_median",
    "interstand_tension_set_median",
    "coiler_tension_set_median",
    "f1_bending_set_median",
    "f2_bending_set_median",
    "tm1_tilt_set_median",
    "tm2_tilt_set_median",
    "main_speed_set_median",
    "f1_roll_force_sum_median",
    "f2_roll_force_sum_median",
)

FEATURE_LABELS = {
    "entry_thickness_set_mode": "入口厚度",
    "exit_thickness_set_mode": "目标出口厚度",
    "width_set_mode": "来料宽度",
    "total_reduction_ratio": "总压下率",
    "entry_thickness_actual_median": "入口实测厚度",
}


def _load_artifact() -> dict[str, Any] | None:
    if not ARTIFACT_PATH.exists():
        return None
    try:
        artifact = json.loads(ARTIFACT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(artifact.get("models"), dict):
        return None
    return artifact


def get_model_metadata() -> dict[str, Any]:
    artifact = _load_artifact()
    if not artifact:
        return {
            "model_version": "direct-model-missing",
            "model_family": "",
            "inference_method": "",
            "training_datasets": [],
            "validation_dataset": "",
            "training_case_count": 0,
            "validation_case_count": 0,
            "validation_composite_improvement_percent": None,
            "mean_mae_improvement_percent": None,
            "direct_win_count": 0,
            "target_count": 0,
            "high_speed_only": False,
            "validation_metrics": {},
        }
    summary = artifact.get("summary", {})
    validation_metrics = {
        target: {
            "direct_mae": model.get("final_test", {})
            .get("direct", {})
            .get("mae"),
            "historical_baseline_mae": model.get("final_test", {})
            .get("historical_5nn_median_baseline", {})
            .get("mae"),
            "mae_improvement_percent": model.get("final_test", {}).get(
                "mae_improvement_percent"
            ),
        }
        for target, model in artifact.get("models", {}).items()
    }
    mean_improvement = summary.get("mean_mae_improvement_percent")
    return {
        "model_version": artifact["model_version"],
        "model_family": artifact.get("model_family", ""),
        "inference_method": artifact.get("inference_method", ""),
        "training_datasets": artifact.get("training_datasets", []),
        "validation_dataset": artifact.get("validation_dataset", ""),
        "training_case_count": artifact.get("training_case_count", 0),
        "validation_case_count": artifact.get("validation_case_count", 0),
        # Retained for older dashboard clients; this is now mean target MAE
        # improvement, not the old composite KNN score.
        "validation_composite_improvement_percent": mean_improvement,
        "mean_mae_improvement_percent": mean_improvement,
        "direct_win_count": summary.get("direct_win_count", 0),
        "target_count": summary.get("target_count", 0),
        "high_speed_only": bool(
            artifact.get("training_policy", {}).get("high_speed_plateau_only")
        ),
        "historical_neighbour_method_used_at_inference": bool(
            artifact.get("training_policy", {}).get(
                "historical_neighbour_method_used_at_inference"
            )
        ),
        "validation_metrics": validation_metrics,
    }


def _request_features(request: RecommendationRequest) -> tuple[dict[str, float], bool]:
    actual = request.entry_actual_thickness
    actual_defaulted = actual is None
    if actual is None:
        actual = request.entry_thickness
    return (
        {
            "entry_thickness_set_mode": request.entry_thickness,
            "exit_thickness_set_mode": request.target_exit_thickness,
            "width_set_mode": request.incoming_width,
            "total_reduction_ratio": (
                request.entry_thickness - request.target_exit_thickness
            )
            / request.entry_thickness,
            "entry_thickness_actual_median": actual,
        },
        actual_defaulted,
    )


def _basis_row(
    values: dict[str, float],
    feature_names: Sequence[str],
    means: Sequence[float],
    scales: Sequence[float],
    terms: Sequence[Sequence[int]],
) -> list[float]:
    standardized = [
        (values[name] - mean) / scale
        for name, mean, scale in zip(feature_names, means, scales)
    ]
    row = [1.0]
    for term in terms:
        product = 1.0
        for index in term:
            product *= standardized[index]
        row.append(product)
    return row


def _apply_bounds(
    value: float,
    target: str,
    model: dict[str, Any],
    features: dict[str, float],
) -> float:
    if target == "intermediate_thickness_set_mode":
        value = min(
            features["entry_thickness_set_mode"] - 0.001,
            max(features["exit_thickness_set_mode"] + 0.001, value),
        )
    if model.get("hard_min") is not None:
        value = max(float(model["hard_min"]), value)
    if model.get("hard_max") is not None:
        value = min(float(model["hard_max"]), value)
    return value


def _predict(
    target: str, model: dict[str, Any], features: dict[str, float]
) -> float:
    row = _basis_row(
        features,
        model["feature_names"],
        model["feature_mean"],
        model["feature_scale"],
        model["basis_terms"],
    )
    encoded = sum(
        value * coefficient
        for value, coefficient in zip(row, model["coefficients"])
    )
    if model.get("target_transform") == "reduction_share":
        entry = features["entry_thickness_set_mode"]
        exit_value = features["exit_thickness_set_mode"]
        value = entry - encoded * (entry - exit_value)
    else:
        value = encoded
    if not math.isfinite(value):
        raise ValueError(f"non-finite model output for {target}")
    return _apply_bounds(value, target, model, features)


def _coverage_state(
    artifact: dict[str, Any], features: dict[str, float]
) -> tuple[list[str], list[str]]:
    outside = []
    edge = []
    for feature, value in features.items():
        bounds = artifact.get("input_coverage", {}).get(feature)
        if not bounds:
            continue
        label = FEATURE_LABELS.get(feature, feature)
        if value < float(bounds["min"]) or value > float(bounds["max"]):
            outside.append(label)
        elif value < float(bounds["p01"]) or value > float(bounds["p99"]):
            edge.append(label)
    return outside, edge


def _parameter_row(
    target: str, model: dict[str, Any], features: dict[str, float]
) -> dict[str, Any]:
    reference = _predict(target, model, features)
    half_width = float(model["prediction_interval"]["half_width"])
    low = _apply_bounds(reference - half_width, target, model, features)
    high = _apply_bounds(reference + half_width, target, model, features)
    test = model.get("final_test", {})
    direct = test.get("direct", {})
    baseline = test.get("historical_5nn_median_baseline", {})
    return {
        "code": target,
        "label": model["label"],
        "unit": model.get("unit", ""),
        "reference": round(reference, 4),
        "low": round(low, 4),
        "high": round(high, 4),
        "role": model.get("role", "machine_setup"),
        "priority": model.get("priority", "secondary"),
        "reliability": model.get("reliability", "low"),
        "interval_source": "滚动时间验证残差80%区间",
        "final_test_mae": direct.get("mae"),
        "final_test_p95_error": direct.get("p95_absolute_error"),
        "historical_baseline_mae": baseline.get("mae"),
        "mae_improvement_percent": test.get("mae_improvement_percent"),
    }


def build_recommendation(
    _db: Session, request: RecommendationRequest
) -> tuple[str, float, str, dict[str, Any]]:
    artifact = _load_artifact()
    metadata = get_model_metadata()
    if not artifact:
        return "insufficient_data", 0.0, metadata["model_version"], {
            "headline": "直接预测模型尚未生成",
            "can_apply": False,
            "parameters": [],
            "similar_cases": [],
            "risks": ["需要先运行直接预测模型训练脚本。"],
            "model": metadata,
        }

    features, actual_defaulted = _request_features(request)
    outside, edge = _coverage_state(artifact, features)
    if outside:
        return "insufficient_data", 0.0, artifact["model_version"], {
            "headline": "规格超出训练覆盖，拒绝强行预测",
            "can_apply": False,
            "parameters": [],
            "similar_cases": [],
            "risks": [
                f"超出训练范围的输入：{'、'.join(outside)}。",
                "补充该规格的高速稳定数据并重新训练后再给首设。",
            ],
            "model": metadata,
            "coverage": {
                "in_domain": False,
                "edge_features": edge,
                "out_of_range_features": outside,
                "training_case_count": metadata["training_case_count"],
                "validation_case_count": metadata["validation_case_count"],
            },
        }

    parameters = [
        _parameter_row(target, artifact["models"][target], features)
        for target in PARAMETER_ORDER
        if target in artifact["models"]
    ]
    low_reliability = [
        row["label"] for row in parameters if row["reliability"] == "low"
    ]
    worse_than_history = [
        row["label"]
        for row in parameters
        if (row.get("mae_improvement_percent") or 0) < 0
    ]
    risks = [
        "结果由固定回归方程直接计算，不检索或平均历史卷；系统不会写入PLC，首卷必须由主操确认。",
        "0719验证衡量的是对高速稳定设定的预测误差；尚无“模型实际下发后”的成品厚度闭环准确率。",
    ]
    if actual_defaulted:
        risks.append("未提供入口在线实测厚度，本次按入口设定厚度代入。")
    if edge:
        risks.append(f"靠近训练边界：{'、'.join(edge)}；已降为低置信参考。")
    if low_reliability:
        risks.append(f"跨日解释能力较弱、只可人工参考：{'、'.join(low_reliability)}。")
    if worse_than_history:
        risks.append(
            f"0719上直接模型未优于历史基线：{'、'.join(worse_than_history)}；"
            "按你的要求仍未回退到历史平均。"
        )

    status = "reference_only" if edge else "recommendable"
    confidence = 52.0 if edge else 76.0
    headline = (
        "已生成直接预测，边界规格需谨慎确认"
        if edge
        else "已生成直接预测首设，需主操确认"
    )
    return status, confidence, artifact["model_version"], {
        "headline": headline,
        # Read-only by design: this flag means no automatic PLC application.
        "can_apply": False,
        "parameters": parameters,
        "similar_cases": [],
        "risks": risks,
        "model": metadata,
        "coverage": {
            "in_domain": True,
            "edge_features": edge,
            "out_of_range_features": [],
            "training_case_count": metadata["training_case_count"],
            "validation_case_count": metadata["validation_case_count"],
        },
        "input_snapshot": {
            key: round(value, 6) for key, value in features.items()
        },
    }
