#!/usr/bin/env python3
"""Fixed V071 width-policy audit. Never select/tune a variant using 260805.

260805 has already been inspected in V070, so all new results are retrospective,
not a fresh blind test. The original V070 artifact/report remain immutable.
"""
from __future__ import annotations
import argparse
import copy
import json
from pathlib import Path
import benchmark_physics_v070 as b

OUT = b.WORKSPACE / 'outputs' / 'astra_review_v071'


def smooth_width(sample, factors):
    bands = factors[sample['stand']]
    width = sample['width']
    for left, right in zip(bands, bands[1:]):
        edge = left['width_high_mm']
        span = min(10, (edge - left['width_low_mm']) / 2, (right['width_high_mm'] - edge) / 2)
        if width < edge - span:
            return left['factor']
        if width <= edge + span:
            t = (width - edge + span) / (2 * span)
            return left['factor'] + t * t * (3 - 2 * t) * (right['factor'] - left['factor'])
    return bands[-1]['factor']


def audit():
    rows = [row for row in b.read_rows(b.GOLD_PATH)
            if row.get('golden_level') in {'GOLD_A', 'GOLD_B'}
            and (b.number(row, 'lubrication_status_ok_ratio') or 0) >= .99]
    samples = [s for row in rows for stand in ('f1', 'f2')
               if (s := b.sample_from_row(row, stand)) is not None]
    # Predeclare a single smoothing width; no hyperparameter search.
    legacy = b.width_factor_for_sample
    results = {}
    for name, lookup in [('legacy', legacy), ('smooth_10mm', smooth_width)]:
        b.width_factor_for_sample = lookup
        results[name] = b.evaluate_variant(name, samples, b.q_hill, b.tension_hill, True)
        # Model-selection/development comparison excludes the audit day entirely.
        development = [s for s in samples if s['dataset'] < '260805']
        results[name]['development_before_260805'] = b.evaluate_variant(
            name, development, b.q_hill, b.tension_hill, True)['validation']
    b.width_factor_for_sample = legacy
    result = {
        'policy': 'fixed 10 mm half-width cubic transition; no selection on audit day',
        'limitation': 'retrospective measured-tension force audit; 260805 is reused and is not blind',
        'variants': results,
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / 'width_benchmark.json').write_text(json.dumps(result, indent=2) + '\n')
    return result


def artifact_from_audit(result):
    old = json.loads((b.ROOT / 'backend/app/model_artifacts/equipment_physics_v070.json').read_text())
    artifact = copy.deepcopy(old)
    validation = result['variants']['smooth_10mm']['validation']
    artifact.update(schema_version='cold-tandem-equipment-physics-v0.7.1',
                    status='retrospectively_audited_physical_screen',
                    fallback_artifact='equipment_physics_v070.json')
    artifact['model']['width_correction'].update(
        interpolation='cubic_smoothstep_at_band_edges', transition_half_width_mm=10,
        residual_formula='shrunken V070 band factors; cubic C1 transitions within 10 mm of each edge; constant tails')
    artifact['model']['effective_pressure'] = '1.15*sigma_m-(0.7*sigma_back+0.3*sigma_front); reject <=0'
    artifact['model']['iteration']['failure_policy'] = 'reject candidate on nonfinite or nonconverged solve'
    artifact['validation'] = {**validation,
        'p95_absolute_error_t': max(old['validation']['p95_absolute_error_t'], validation['p95_absolute_error_t']),
        'measured_tension_p95_t': validation['p95_absolute_error_t'],
        'error_margin_policy': 'retain larger V070/V071 empirical residual screen',
        'scope': result['limitation']}
    artifact['comparison_to_v067'] = {'archival_v070_comparison': old['comparison_to_v067']}
    artifact['comparison_to_displayed_v061'] = {
        'archival_v070_comparison': old['comparison_to_displayed_v061'],
        'warning': 'not V071 metrics; 260805 was used in earlier model selection; no fresh blind-test claim'}
    artifact['deployment_policy']['hard_force_gate'] = 'max(nominal_physical+retained_empirical_error, upper_flow_with_lower_tensions, material_scaled_DAT+scaled_error)<=1000T'
    artifact['deployment_policy']['route_ranking'] = 'after conservative feasibility screen, rank by max(DAT nominal, physics nominal) and load roughness; do not optimize marginal uncertainty scenarios'
    artifact['deployment_policy']['uncertainty'] = 'marginal empirical errors and effective-stress percentile are not a calibrated joint 95% bound'
    artifact['limitations'] += ['thickness-flow bands remain piecewise; route selection is discrete',
        'tension forecast error added as lower-tension sensitivity screen, not as certified uncertainty',
        'no motor power, torque, bite/threading or quality capability certification']
    return artifact


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--write-artifact', action='store_true')
    args = parser.parse_args()
    result = audit()
    if args.write_artifact:
        path = b.ROOT / 'backend/app/model_artifacts/equipment_physics_v071.json'
        path.write_text(json.dumps(artifact_from_audit(result), ensure_ascii=False, indent=2) + '\n')
    for name, variant in result['variants'].items():
        print(name, json.dumps(variant['validation'] | {'by_date': 'see JSON'}))
