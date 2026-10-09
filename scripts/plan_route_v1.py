#!/usr/bin/env python3
"""Generate a governed multi-pass shadow route without changing the live model."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = PROJECT_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.process_framework import plan_route  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--entry", type=float, required=True, help="入口厚度 mm")
    parser.add_argument("--exit", type=float, required=True, help="目标出口厚度 mm")
    parser.add_argument("--width", type=float, required=True, help="宽度 mm")
    parser.add_argument("--alloy", default="UNKNOWN", help="合金牌号")
    parser.add_argument("--temper", default="UNKNOWN", help="入口状态")
    parser.add_argument(
        "--anneal-state",
        default="unknown",
        choices=("fully_annealed", "work_hardened", "partially_annealed", "unknown"),
    )
    parser.add_argument("--reference-annealed-thickness", type=float)
    parser.add_argument("--initial-cumulative-true-strain", type=float)
    parser.add_argument("--maximum-pass-true-strain", type=float)
    parser.add_argument("--maximum-cumulative-true-strain", type=float)
    parser.add_argument("--max-passes", type=int, default=8)
    parser.add_argument("--mode", choices=("shadow", "production"), default="shadow")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    request = {
        "entry_thickness_mm": args.entry,
        "target_exit_thickness_mm": args.exit,
        "width_mm": args.width,
        "alloy": args.alloy,
        "initial_temper": args.temper,
        "anneal_state": args.anneal_state,
        "reference_annealed_thickness_mm": args.reference_annealed_thickness,
        "initial_cumulative_true_strain": args.initial_cumulative_true_strain,
        "maximum_true_strain_per_tandem_pass": args.maximum_pass_true_strain,
        "maximum_cumulative_true_strain_before_full_anneal": (
            args.maximum_cumulative_true_strain
        ),
        "max_passes": args.max_passes,
    }
    request = {key: value for key, value in request.items() if value is not None}
    result = plan_route(request, mode=args.mode)
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
