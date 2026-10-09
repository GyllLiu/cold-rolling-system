#!/usr/bin/env python3
"""Export conditional allocation replay inputs to stdout, without test-force leakage.

Physics calibration uses dates strictly before the reused 260805 audit day.
Frozen DAT models are unchanged. Earlier-day results are retrospective only.
"""
import json
import math
import benchmark_physics_v070 as b

rows = [r for r in b.read_rows(b.GOLD_PATH)
        if r.get('golden_level') in {'GOLD_A', 'GOLD_B'}
        and (b.number(r, 'lubrication_status_ok_ratio') or 0) >= .99]
samples = [s for r in rows for stand in ('f1','f2')
           if (s := b.sample_from_row(r,stand)) is not None]
train = [s for s in samples if s['dataset'] < '260805']
calibration = json.loads((b.ROOT/'backend/app/model_artifacts/equipment_physics_v071.json').read_text())
calibration['model']['bands'] = b.fit_flow_band_statistics(train, b.q_hill, b.tension_hill)
calibration['model']['width_correction']['bands'] = b.fit_width_factors(
    train, b.fit_flow_bands(train,b.q_hill,b.tension_hill),b.q_hill,b.tension_hill)
models = json.loads(b.ROUTE_MODEL_PATH.read_text())['models']
output = []
for row in rows:
    entry, middle, exit_, width = [b.number(row,k) for k in
        ['current_entry_thickness','f1_exit_thickness','f2_exit_thickness','width']]
    if None in (entry,middle,exit_,width) or not entry > middle > exit_ > 0: continue
    actual = [b.number(row,f'{stand}_force_actual_median') for stand in ('f1','f2')]
    tensions = [b.number(row,k) for k in ['entry_tension_actual_median','interstand_tension_actual_median','exit_tension_actual_median']]
    if any(v is None or v <= 0 for v in actual) or any(v is None or v < 0 for v in tensions): continue
    features = {'entry_thickness_set_mode':entry,'exit_thickness_set_mode':exit_, 'width_set_mode':width,
        'total_reduction_ratio':1-exit_/entry, 'log_thickness_ratio':math.log(entry/exit_),
        'entry_cross_section_proxy':entry*width,'exit_cross_section_proxy':exit_*width}
    output.append({'date':row['dataset'],'entry':entry,'middle':middle,'exit':exit_,'width':width,
        'actualForces':actual,'actualTensions':tensions,
        'datForces':[b.predict_route_model(models[f'{stand}_roll_force_sum_median'],features) for stand in ('f1','f2')]})
reference_alloy = json.loads((b.ROOT/'backend/app/model_artifacts/material_state_v061.json').read_text())['reference']['alloy']
print(json.dumps({'calibration':calibration,'rows':output,'referenceAlloy':reference_alloy}))
