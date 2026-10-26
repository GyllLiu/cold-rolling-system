"""Offline parameter challengers; never writes production model artifacts.

Selection uses expanding past-day folds up to 260805. Later DAT days are
evaluated only after models/configurations are saved. Reported primary metrics
condition on a supplied pass route (including its planned F1 exit thickness),
NOT an end-to-end route optimization or a measured quality improvement.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import math
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import ExtraTreesRegressor, HistGradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.pipeline import make_pipeline
from sklearn.preprocessing import PolynomialFeatures, StandardScaler
from threadpoolctl import threadpool_limits

import train_compare_high_speed_v6 as v6

REPO = Path(__file__).resolve().parents[1]
WORKSPACE = REPO.parent
TARGETS = tuple(t.code for t in v6.TARGETS)
MIDDLE = TARGETS[0]
TENSIONS = TARGETS[1:4]
FORCES = TARGETS[6:]
ENTRY = "entry_thickness_set_mode"
EXIT = "exit_thickness_set_mode"
WIDTH = "width_set_mode"
SPEC3 = (ENTRY, EXIT, WIDTH)
GEOMETRY = (*SPEC3, "total_reduction_ratio", "log_thickness_ratio",
            "entry_cross_section_proxy", "exit_cross_section_proxy")
ROUTE = (*GEOMETRY, MIDDLE, "f1_reduction", "f2_reduction", "f1_strain", "f2_strain",
         "contact_proxy_f1", "contact_proxy_f2")
FEATURES = {"spec3": SPEC3, "geometry": GEOMETRY, "route": ROUTE}


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def write_csv(path, rows):
    if not rows:
        path.write_text("")
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(dict.fromkeys(k for r in rows for k in r)))
        writer.writeheader()
        writer.writerows(rows)


def sha(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def feature_values(values, middle=None):
    """Only available route/spec inputs; never forces, measured tensions or labels."""
    h0, h2, width = (float(values[k]) for k in SPEC3)
    result = {ENTRY: h0, EXIT: h2, WIDTH: width,
              "total_reduction_ratio": 1 - h2 / h0, "log_thickness_ratio": math.log(h0 / h2),
              "entry_cross_section_proxy": h0 * width, "exit_cross_section_proxy": h2 * width}
    if middle is not None:
        if not h2 < middle < h0:
            raise ValueError("Candidate intermediate thickness must lie inside entry/exit")
        result.update({MIDDLE: middle, "f1_reduction": 1 - middle / h0,
                       "f2_reduction": 1 - h2 / middle, "f1_strain": math.log(h0 / middle),
                       "f2_strain": math.log(middle / h2),
                       "contact_proxy_f1": width * math.sqrt(190 * (h0 - middle)),
                       "contact_proxy_f2": width * math.sqrt(190 * (middle - h2))})
    return result


def matrix(cases, name, middles=None):
    columns = FEATURES[name]
    return np.asarray([[feature_values(c.values, None if middles is None else middles[i])[k]
                        for k in columns] for i, c in enumerate(cases)], dtype=float)


def weights(cases):
    keys = [(c.dataset, *(round(c.values[k], 4) for k in SPEC3)) for c in cases]
    counts = Counter(keys)
    w = np.array([1 / counts[k] for k in keys])
    return w / w.mean()


def configs(target):
    names = ["spec3", "geometry"] + ([] if target == MIDDLE else ["route"])
    rows = []
    transform = "share" if target == MIDDLE else "raw"
    for features in names:
        for degree, alpha in ((1, 10.), (2, 10.), (2, 100.)):
            rows.append(dict(kind="ridge", features=features, degree=degree, alpha=alpha, transform=transform))
        if features != "spec3":
            for leaves in (7, 15):
                rows.append(dict(kind="histgb", features=features, leaves=leaves, transform=transform))
            for leaf in (5, 12):
                rows.append(dict(kind="extratrees", features=features, min_leaf=leaf, transform=transform))
    # Geometry-normalized targets are computable for a supplied route. No
    # material identity, measured force, or future response enters an input.
    if target in FORCES or target in TENSIONS or target in TARGETS[4:6]:
        for degree, alpha in ((1, 10.), (2, 100.)):
            rows.append(dict(kind="ridge", features="route", degree=degree, alpha=alpha,
                             transform="geometry_normalized"))
    return rows


def name(config):
    return ":".join(f"{key}={config[key]}" for key in sorted(config))


def target_affine(cases, target, transform, middles=None):
    n = len(cases)
    scale, offset = np.ones(n), np.zeros(n)
    for i, case in enumerate(cases):
        h0, h2, width = (case.values[k] for k in SPEC3)
        middle = None if middles is None else middles[i]
        if transform == "share":
            offset[i], scale[i] = h0, -(h0 - h2)
        elif transform == "geometry_normalized":
            if middle is None:
                raise ValueError("Normalized pass target requires a candidate middle gauge")
            if target in FORCES:
                delta = h0 - middle if target == FORCES[0] else middle - h2
                scale[i] = width * math.sqrt(190 * delta) / 9806.65
            elif target in TENSIONS:
                thickness = [h0, middle, h2][TENSIONS.index(target)]
                scale[i] = width * thickness / 9806.65
            elif target in TARGETS[4:6]:
                offset[i] = middle if target == TARGETS[4] else h2
    return scale, offset


def estimator(config):
    if config["kind"] == "ridge":
        return make_pipeline(StandardScaler(), PolynomialFeatures(config["degree"], include_bias=False),
                             Ridge(alpha=config["alpha"]))
    if config["kind"] == "histgb":
        # No random internal validation split across neighbouring records.
        return HistGradientBoostingRegressor(max_iter=150, learning_rate=.06,
            max_leaf_nodes=config["leaves"], min_samples_leaf=20, l2_regularization=10,
            early_stopping=False, random_state=730)
    return ExtraTreesRegressor(n_estimators=128, min_samples_leaf=config["min_leaf"],
                               max_features=1., n_jobs=2, random_state=730)


def fit(cases, target, config):
    middles = None if target == MIDDLE else [c.values[MIDDLE] for c in cases]
    x = matrix(cases, config["features"], middles)
    y = np.asarray([c.values[target] for c in cases])
    scale, offset = target_affine(cases, target, config["transform"], middles)
    model = estimator(config)
    kw = {"ridge__sample_weight" if config["kind"] == "ridge" else "sample_weight": weights(cases)}
    model.fit(x, (y - offset) / scale, **kw)
    return {"config": config, "model": model, "target": target}


def predict(fitted, cases, middles=None):
    target, config = fitted["target"], fitted["config"]
    if target == MIDDLE:
        middles = None  # Never read held-out middle truth when predicting it.
    elif middles is None:
        middles = [c.values[MIDDLE] for c in cases]
    raw = fitted["model"].predict(matrix(cases, config["features"], middles))
    scale, offset = target_affine(cases, target, config["transform"], middles)
    values = raw * scale + offset
    if target == MIDDLE:
        values = np.clip(values, [c.values[EXIT] + .001 for c in cases],
                         [c.values[ENTRY] - .001 for c in cases])
    # Physical invalid outputs are counted; they are not clipped away to make
    # the error/safety result look better.
    return values


def predict_artifact_head(model, features):
    if model.get("model_type") == "weighted_ensemble":
        return sum(part["weight"] * (predict_artifact_head(part["model"], features) +
                   (features[EXIT] if part["postprocess"] == "add_exit_thickness" else 0))
                   for part in model["components"])
    z = [(features[k] - mean) / sd for k, mean, sd in
         zip(model["feature_names"], model["feature_mean"], model["feature_scale"])]
    terms = [1.] + [math.prod(z[i] for i in indices) for indices in model["basis_terms"]]
    value = sum(a * b for a, b in zip(terms, model["coefficients"]))
    if model.get("target_transform") == "reduction_share":
        value = features[ENTRY] - value * (features[ENTRY] - features[EXIT])
    return value


def predict_baseline(artifact, cases, target, middles=None):
    values = []
    model = artifact["models"][target]
    for i, case in enumerate(cases):
        middle = case.values[MIDDLE] if middles is None else middles[i]
        features = feature_values(case.values, None if target == MIDDLE else middle)
        value = predict_artifact_head(model, features)
        if target == MIDDLE:
            value = max(case.values[EXIT] + .001, min(case.values[ENTRY] - .001, value))
        if model.get("hard_min") is not None:
            value = max(value, model["hard_min"])
        if model.get("hard_max") is not None:
            value = min(value, model["hard_max"])
        values.append(value)
    return np.asarray(values)


def metrics(cases, predicted, target):
    truth = np.asarray([case.values[target] for case in cases])
    errors = np.asarray(predicted) - truth
    denominator = np.sum((truth - truth.mean()) ** 2)
    return {"n": len(cases), "mae": float(np.mean(abs(errors))),
            "p95": float(np.quantile(abs(errors), .95)), "bias": float(errors.mean()),
            "r2": float(1 - np.sum(errors ** 2) / denominator) if denominator > 1e-12 else None}


def rolling_folds(cases):
    for day in sorted({c.dataset for c in cases if c.dataset >= "260724"}):
        validation = [c for c in cases if c.dataset == day]
        boundary = min(datetime.fromisoformat(c.start_time) for c in validation) - timedelta(hours=2)
        train = [c for c in cases if c.dataset < day and datetime.fromisoformat(c.start_time) < boundary]
        if len(train) >= 30:
            yield day, train, validation


def aggregate(rows):
    return {"equal_day_mae": float(np.mean([r["mae"] for r in rows])),
            "equal_day_p95": float(np.mean([r["p95"] for r in rows])),
            "worst_day_mae": max(r["mae"] for r in rows), "fold_count": len(rows)}


def bounds_mask(cases, artifact):
    return np.array([all(lo <= c.values[k] <= hi for k, (lo, hi) in artifact["feature_bounds"].items())
                     for c in cases], dtype=bool)


def project_tensions(tonnes, cases, middles, limits):
    result = np.asarray(tonnes, dtype=float).copy()
    for i, case in enumerate(cases):
        thickness = np.array([case.values[ENTRY], middles[i], case.values[EXIT]])
        factors = 9.80665 / (case.values[WIDTH] * thickness)
        units = result[i] * factors
        blocks = [[j, j, 1, value] for j, value in enumerate(units)]
        j = 0
        while j < len(blocks) - 1:
            a, b = blocks[j], blocks[j + 1]
            if a[3] <= b[3]:
                j += 1
            else:
                count = a[2] + b[2]
                blocks[j:j + 2] = [[a[0], b[1], count, (a[3] * a[2] + b[3] * b[2]) / count]]
                j = max(0, j - 1)
        for start, end, _count, mean in blocks:
            units[start:end + 1] = mean
        caps = np.asarray(limits) * factors
        for j in (2, 1, 0):
            units[j] = max(0, min(units[j], caps[j], units[j + 1] if j < 2 else np.inf))
        result[i] = units / factors
    return result


def capture_associations(segment_path, export_path, output):
    if export_path is None:
        return {"available": False}
    segments = list(csv.DictReader(segment_path.open(encoding="utf-8-sig")))
    records = list(csv.DictReader(export_path.open(encoding="utf-8-sig")))
    from zoneinfo import ZoneInfo
    zone = ZoneInfo("Asia/Shanghai")
    def local(value):
        return datetime.fromisoformat(value).astimezone(zone).replace(tzinfo=None)
    rows = []
    for record in records:
        if not record["started_at"] or not record["ended_at"]:
            continue
        start, end = local(record["started_at"]), local(record["ended_at"])
        if start.date().isoformat() != "2026-09-16":
            continue
        matches = []
        for segment in segments:
            a, b = datetime.fromisoformat(segment["start_time"]), datetime.fromisoformat(segment["end_time"])
            overlap = max(0., (min(end, b) - max(start, a)).total_seconds())
            if overlap >= 30:
                matches.append((segment, overlap, overlap / max(1., (b - a).total_seconds())))
        for segment, seconds, coverage in matches:
            rows.append({"record_id": record["id"], "photo_id": record["photo_id"],
                         "batch_no": record["batch_no"], "ocr_alloy_raw": record["ocr_alloy"],
                         "card_state_raw": record["card_state"], "incoming_state_verified": False,
                         "selected_pass_no": record["selected_pass_no"], "reviewed_at": record["reviewed_at"],
                         "candidate_id": "260916_" + segment["candidate_id"], "overlap_seconds": seconds,
                         "segment_coverage": coverage, "record_candidate_count": len(matches),
                         "unique_time_candidate": len(matches) == 1 and seconds >= 120 and coverage >= .8,
                         "usable_as_verified_material_label": False})
    segment_counts = Counter(row["candidate_id"] for row in rows)
    for row in rows:
        row["segment_record_count"] = segment_counts[row["candidate_id"]]
        row["unique_time_candidate"] = bool(row["unique_time_candidate"] and row["segment_record_count"] == 1)
    write_csv(output / "capture_associations.csv", rows)
    return {"available": True, "photo_records": sum(local(r["started_at"]).date().isoformat() == "2026-09-16"
            for r in records), "unique_time_candidate_ids": [r["candidate_id"] for r in rows if r["unique_time_candidate"]],
            "verified_incoming_material_labels": 0,
            "note": "Temporal association is not certified coil identity; raw OCR is audit-only."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--external", type=Path, nargs="+", required=True)
    parser.add_argument("--capture-export", type=Path)
    args = parser.parse_args()
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    if (out / "frozen_selection.json").exists():
        raise ValueError("Frozen run already exists; use a fresh output directory rather than retune on evaluation")
    artifact_path = REPO / "backend/app/model_artifacts/route_model_v061.json"
    baseline = json.loads(artifact_path.read_text())
    paths = sorted((WORKSPACE / "outputs").glob("data_foundation*/production_segments.csv"))
    paths = [p for p in paths if v6.dataset_from_path(p) <= "260805"]
    train = v6.load_cases(paths)
    assert len({c.candidate_id for c in train}) == len(train)
    folds = list(rolling_folds(train))
    protocol = {"version": "v073-offline-parameter-experiment-1", "created_at": datetime.now(timezone.utc).isoformat(),
        "training_days": sorted({c.dataset for c in train}), "training_candidates": len(train),
        "selection": "expanding-date validation; 2h boundary purge; equal-day MAE then P95",
        "folds": [{"date": day, "training_n": len(a), "validation_n": len(b)} for day, a, b in folds],
        "external_files": [str(p.resolve()) for p in args.external],
        "primary_question": "Predict historical settings/loads for a supplied route, not causal optimum or quality",
        "photo_policy": "raw OCR for association audit only; no invented alloy/incoming-state values",
        "candidate_configs": {t: configs(t) for t in TARGETS},
        "baseline_sha256": sha(artifact_path), "source_hashes": {str(p.resolve()): sha(p) for p in paths},
        "script_sha256": sha(Path(__file__)),
        "dependencies": {p: importlib.metadata.version(p) for p in ["numpy", "scikit-learn", "scipy", "joblib"]},
        "release_approved": False,
        "limitations": ["Old 260805 was used previously; treated as development, never a fresh blind test",
                        "No true-coil IDs on old DAT; date grouping cannot certify coil-independent validation",
                        "1Hz plateau statistics do not validate native-100Hz transient behaviour",
                        "No measured product-quality labels; historical practice is not proven optimal"]}
    dump(out / "protocol.json", protocol)
    all_cv, selection, fitted, spec_fitted = [], {}, {}, {}
    for target in TARGETS:
        scores = []
        for config in configs(target):
            rows = []
            for day, past, future in folds:
                result = metrics(future, predict(fit(past, target, config), future), target)
                rows.append(result)
                all_cv.append({"target": target, "candidate": name(config), "date": day, **result})
            scores.append((aggregate(rows), name(config), config))
        scores.sort(key=lambda item: (item[0]["equal_day_mae"], item[0]["equal_day_p95"], item[1]))
        best = scores[0]
        spec = next(s for s in scores if s[2]["features"] != "route")
        selection[target] = {"label": v6.TARGET_BY_CODE[target].label, "config": best[2],
                             "development": best[0], "spec_only_config": spec[2],
                             "spec_only_development": spec[0]}
        fitted[target], spec_fitted[target] = fit(train, target, best[2]), fit(train, target, spec[2])
        print(f"selected {target}: {name(best[2])}; CV MAE={best[0]['equal_day_mae']:.6g}", flush=True)
    write_csv(out / "development_folds.csv", all_cv)
    dump(out / "frozen_selection.json", selection)
    joblib.dump({"primary": fitted, "spec_only": spec_fitted}, out / "offline_models.joblib")
    # Only now decode external labels into cases. No model re-selection below.
    external = v6.load_cases(args.external)
    if {c.dataset for c in external} & {c.dataset for c in train}:
        raise ValueError("External/train date overlap")
    if not external:
        raise ValueError("No eligible external samples")
    mids = [c.values[MIDDLE] for c in external]
    old_mid = predict_baseline(baseline, external, MIDDLE)
    new_mid = predict(fitted[MIDDLE], external)
    predictions, comparison, details = {}, [], []
    sep_path = next((p for p in args.external if v6.dataset_from_path(p) == "260916"), None)
    capture = capture_associations(sep_path, args.capture_export, out) if sep_path else {"available": False}
    subsets = {"all": np.ones(len(external), dtype=bool), "in_baseline_bounds": bounds_mask(external, baseline)}
    for day in sorted({c.dataset for c in external}):
        subsets[day] = np.array([c.dataset == day for c in external])
    photo_ids = set(capture.get("unique_time_candidate_ids", []))
    subsets["capture_unique_time_candidates"] = np.array([c.candidate_id in photo_ids for c in external])
    for target in TARGETS:
        pred = {"frozen_v061": predict_baseline(baseline, external, target),
                "challenger": predict(fitted[target], external),
                "spec_only_challenger": predict(spec_fitted[target], external),
                "cascade_v061_diagnostic": predict_baseline(baseline, external, target, old_mid),
                "cascade_challenger_diagnostic": predict(fitted[target], external, new_mid)}
        predictions[target] = pred
        for subset, mask in subsets.items():
            rows = [c for c, keep in zip(external, mask) if keep]
            if not rows:
                continue
            for model_name, values in pred.items():
                comparison.append({"target": target, "label": v6.TARGET_BY_CODE[target].label,
                    "unit": v6.TARGET_BY_CODE[target].unit, "subset": subset, "model": model_name,
                    **metrics(rows, values[mask], target)})
        for i, case in enumerate(external):
            details.append({"date": case.dataset, "candidate_id": case.candidate_id,
                            "start_time": case.start_time, "target": target, "actual": case.values[target],
                            "entry": case.values[ENTRY], "middle": case.values[MIDDLE],
                            "exit": case.values[EXIT], "width": case.values[WIDTH],
                            "in_baseline_bounds": bool(subsets["in_baseline_bounds"][i]),
                            **{k: float(v[i]) for k, v in pred.items()}})
    limits_config = json.loads((REPO / "backend/app/model_artifacts/engineering_baseline_v6.json").read_text())
    limits = [limits_config.get("confirmed_plant_limits", {}).get(k) or np.inf for k in
              ["uncoiler_tension_t_max", "interstand_tension_t_max", "coiler_tension_t_max"]]
    for model_name in ("frozen_v061", "challenger", "spec_only_challenger"):
        tonnes = np.column_stack([predictions[t][model_name] for t in TENSIONS])
        projected = project_tensions(tonnes, external, mids, limits)
        for j, target in enumerate(TENSIONS):
            for subset, mask in subsets.items():
                cases = [c for c, keep in zip(external, mask) if keep]
                if cases:
                    comparison.append({"target": target, "label": v6.TARGET_BY_CODE[target].label,
                        "unit": "T", "subset": subset, "model": model_name + "_unit_projected",
                        **metrics(cases, projected[mask, j], target)})
    # Direct geometry sensitivity is a diagnostic, not evidence of causal truth.
    sensitivity = {}
    shifted = [min(c.values[ENTRY] - .001, c.values[MIDDLE] * 1.01) for c in external]
    for target in FORCES:
        before = predictions[target]["challenger"]
        after = predict(fitted[target], external, shifted)
        expected = after <= before + 1e-8 if target == FORCES[0] else after >= before - 1e-8
        sensitivity[target] = {"n": len(external), "expected_direction_count": int(expected.sum()),
                               "unchanged_count": int(np.isclose(after, before, atol=1e-8, rtol=0).sum()),
                               "median_delta_t": float(np.median(after - before))}
    summary = {"training_n": len(train), "external_n": len(external),
               "external_by_day": dict(Counter(c.dataset for c in external)),
               "in_baseline_bounds_n": int(subsets["in_baseline_bounds"].sum()),
               "capture": capture, "capture_numeric_evaluation_n": int(subsets["capture_unique_time_candidates"].sum()),
               "sensitivity": sensitivity, "release_approved": False,
               "evaluation_sha256": {str(p.resolve()): sha(p) for p in args.external},
               "raw_invalid_predictions": {t: int(np.sum(~np.isfinite(predictions[t]["challenger"]) |
                                                (predictions[t]["challenger"] < 0))) for t in TARGETS},
               "force_predictions_over_1000_t": {t: int(np.sum(predictions[t]["challenger"] > 1000)) for t in FORCES}}
    write_csv(out / "comparison.csv", comparison)
    write_csv(out / "external_predictions.csv", details)
    dump(out / "summary.json", summary)
    for target in TARGETS:
        rows = [r for r in comparison if r["target"] == target and r["subset"] == "all"]
        a = next(r for r in rows if r["model"] == "frozen_v061")
        b = next(r for r in rows if r["model"] == "challenger")
        print(f"external {target}: {a['mae']:.6g} -> {b['mae']:.6g}; "
              f"MAE change={(a['mae']-b['mae'])/a['mae']*100:.2f}%", flush=True)
    print(json.dumps(summary, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    with threadpool_limits(limits=2):
        main()
