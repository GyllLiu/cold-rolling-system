"""Tiny, chronology-separated calibration experiment using date-matched OCR.

Uses raw card-state categories, explicitly NOT verified incoming temper. This
pilot cannot be deployed via the production alloy/incoming-state interface.
The user-confirmed '1050标1060' means actual alloy 1050. Other ambiguous
composite alloys remain unknown; batch IDs only govern the split.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path

import joblib
import numpy as np
from sklearn.linear_model import Ridge

import train_parameter_experiment_v073 as e


LABEL_POLICY = "confirmed-1050-v2"
CONFIRMED_ALLOY_ALIASES = {"1050标1060": "1050"}


def weak_metadata(row, policy=LABEL_POLICY):
    alloy = re.sub(r"\s+", "", row["ocr_alloy_raw"]).upper()
    # Interpretation confirmed by the user on 2026-10-26. This does not certify
    # each OCR transcription, the incoming temper, or any other composite label.
    confirmed = CONFIRMED_ALLOY_ALIASES.get(alloy) if policy == LABEL_POLICY else None
    match = re.fullmatch(r"(1050|1060|1100|3003)(?:F)?", alloy)
    state = row["card_state_raw"].strip().upper()
    return {"alloy": confirmed or (match.group(1) if match else None),
            "card_state": state if re.fullmatch(r"O|H(?:14|16|18|22|24|26)", state) else None}


def tokens(metadata):
    return [f"{key}={value}" for key, value in metadata.items() if value is not None]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--label-policy", choices=["unresolved-v1", LABEL_POLICY], default=LABEL_POLICY,
                        help="Legacy policy is retained only to reproduce the pre-confirmation pilot")
    args = parser.parse_args()
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    if (out / "pilot_summary.json").exists():
        raise ValueError("Pilot already evaluated; do not overwrite its fixed result")
    matches = list(csv.DictReader((args.model_run / "capture_associations.csv").open(encoding="utf-8-sig")))
    matches = {r["candidate_id"]: r for r in matches if r["unique_time_candidate"] == "True"}
    predictions = list(csv.DictReader((args.model_run / "external_predictions.csv").open(encoding="utf-8-sig")))
    base_rows = {r["candidate_id"]: r for r in predictions if r["target"] == e.MIDDLE and r["candidate_id"] in matches}
    ordered = sorted(base_rows, key=lambda key: base_rows[key]["start_time"])
    if len(ordered) < 6:
        raise ValueError("Fewer than 6 unique temporal candidates; insufficient even for this tiny split")
    split = len(ordered) * 2 // 3
    train_ids, test_ids = ordered[:split], ordered[split:]
    train_batches = {matches[key]["batch_no"] for key in train_ids}
    test_ids = [key for key in test_ids if matches[key]["batch_no"] not in train_batches]
    if not test_ids:
        raise ValueError("No batch-disjoint chronological pilot test")
    metadata = {key: weak_metadata(matches[key], args.label_policy) for key in ordered}
    vocabulary = sorted({token for key in train_ids for token in tokens(metadata[key])})
    def design(ids, with_metadata):
        return np.array([[1.] + ([float(token in tokens(metadata[key])) for token in vocabulary]
                                 if with_metadata else []) for key in ids])
    protocol = {"training_ids": train_ids, "test_ids": test_ids, "ridge_alpha": 6.,
                "fit_intercept": False, "metadata_vocabulary_from_training_only": vocabulary,
                "fixed_before_fitting": True, "date": "2026-09-16", "release_approved": False,
                "batch_ids_are_features": False, "card_state_is_verified_incoming_state": False,
                "label_policy": args.label_policy,
                "confirmed_alloy_aliases": CONFIRMED_ALLOY_ALIASES if args.label_policy == LABEL_POLICY else {},
                "ambiguous_alloy_policy": "unknown (all alloy indicators zero)",
                "scope": "exploratory same-day residual calibration; not independent future-day validation"}
    e.dump(out / "pilot_protocol.json", protocol)
    comparisons, models = [], {}
    for target in e.TARGETS:
        by_id = {r["candidate_id"]: r for r in predictions if r["target"] == target}
        for anchor in ["frozen_v061", "challenger"]:
            residual = np.array([float(by_id[key]["actual"]) - float(by_id[key][anchor]) for key in train_ids])
            truth = np.array([float(by_id[key]["actual"]) for key in test_ids])
            base = np.array([float(by_id[key][anchor]) for key in test_ids])
            variants = {"uncalibrated": base}
            for variant, use_labels in [("dat_bias_only", False), ("dat_plus_ocr_weak", True)]:
                model = Ridge(alpha=6., fit_intercept=False)
                model.fit(design(train_ids, use_labels), residual)
                variants[variant] = base + model.predict(design(test_ids, use_labels))
                models[(target, anchor, variant)] = model
            for variant, pred in variants.items():
                errors = pred - truth
                comparisons.append({"target": target, "label": e.v6.TARGET_BY_CODE[target].label,
                    "unit": e.v6.TARGET_BY_CODE[target].unit, "anchor": anchor, "variant": variant,
                    "train_n": len(train_ids), "test_n": len(test_ids),
                    "mae": float(np.mean(abs(errors))), "p95": float(np.quantile(abs(errors), .95)),
                    "bias": float(np.mean(errors))})
    e.write_csv(out / "pilot_comparison.csv", comparisons)
    e.write_csv(out / "paired_samples.csv", [{"candidate_id": key, "subset": "train" if key in train_ids else "test",
        "start_time": base_rows[key]["start_time"], "batch_no": matches[key]["batch_no"],
        "ocr_alloy_raw": matches[key]["ocr_alloy_raw"], "card_state_raw": matches[key]["card_state_raw"],
        "weak_alloy": metadata[key]["alloy"], "card_state_category": metadata[key]["card_state"],
        "incoming_state_verified": False, "photo_id": matches[key]["photo_id"]} for key in train_ids + test_ids])
    joblib.dump({"models": models, "vocabulary": vocabulary}, out / "pilot_models.joblib")
    summary = {**protocol, "train_n": len(train_ids), "test_n": len(test_ids),
        "train_weak_alloys": dict(Counter(metadata[key]["alloy"] or "unknown" for key in train_ids)),
        "test_weak_alloys": dict(Counter(metadata[key]["alloy"] or "unknown" for key in test_ids)),
        "test_tokens_unseen_in_training": sorted({t for key in test_ids for t in tokens(metadata[key]) if t not in vocabulary}),
        "conclusion": "Too small, single date, and unverified incoming states: no deployable material-effect claim."}
    e.dump(out / "pilot_summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
