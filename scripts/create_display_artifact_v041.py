#!/usr/bin/env python3
"""Create the v0.4.1 display-metadata artifact from frozen v0.4.

Coefficients, features and metrics are copied unchanged. Only labels,
warnings and version metadata are updated. The original v0.4 file is never
overwritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
V4_PATH = PROJECT_ROOT / "backend/app/model_artifacts/recommendation_direct_v4.json"
V041_PATH = PROJECT_ROOT / "backend/app/model_artifacts/recommendation_direct_v041.json"

LABEL_UPDATES = {
    "intermediate_thickness_set_mode": "中间厚度推荐",
    "uncoiler_tension_set_median": "开卷张力推荐",
    "interstand_tension_set_median": "机架张力推荐",
    "coiler_tension_set_median": "卷取张力推荐",
    "tm1_closed_gap_set_median": "F1推荐辊缝",
    "tm2_closed_gap_set_median": "F2推荐辊缝",
}

OPERATOR_WARNING = (
    "F1/F2输出由高速稳定生产段训练得到，作为上卷时的人工推荐值；"
    "主操确认后输入。当前无PLC，系统不会自动下发。"
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=V4_PATH)
    parser.add_argument("--output", type=Path, default=V041_PATH)
    args = parser.parse_args()
    if not args.source.exists():
        parser.error(f"missing frozen v0.4 artifact: {args.source}")
    if args.output.resolve() == args.source.resolve():
        parser.error("refusing to overwrite the frozen v0.4 artifact")

    source_sha = sha256_file(args.source)
    artifact = json.loads(args.source.read_text(encoding="utf-8"))
    if artifact.get("model_version") != "direct-ridge-startup-flatness-v0.4":
        parser.error(f"unexpected source version: {artifact.get('model_version')}")

    for code, label in LABEL_UPDATES.items():
        artifact["models"][code]["label"] = label

    artifact["model_version"] = "direct-ridge-startup-flatness-v0.4.1"
    artifact["generated_at"] = datetime.now(timezone.utc).isoformat()
    artifact["parent_model_version"] = "direct-ridge-startup-flatness-v0.4"
    artifact["parent_artifact"] = args.source.name
    artifact["parent_artifact_sha256"] = source_sha
    artifact["rollback_artifact"] = args.source.name
    artifact["change_scope"] = "display_semantics_only"
    artifact["coefficients_unchanged_from_parent"] = True
    parameter_policy = dict(artifact.get("parameter_policy") or {})
    parameter_policy["closed_gap_safety"] = (
        "high-speed stable running gap reference only; "
        "not a safe coil-on first set; operator must leave a safety margin; "
        "never auto-write PLC"
    )
    parameter_policy["operator_warning"] = OPERATOR_WARNING
    parameter_policy["plc_available"] = False
    parameter_policy["can_apply"] = False
    artifact["parameter_policy"] = parameter_policy

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if sha256_file(args.source) != source_sha:
        raise RuntimeError("frozen v0.4 artifact changed while writing v0.4.1")
    print(f"source={args.source}")
    print(f"source_sha256={source_sha}")
    print(f"wrote={args.output}")
    print(f"output_sha256={sha256_file(args.output)}")
    print("v0.4 unchanged")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
