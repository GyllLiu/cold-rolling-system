#!/usr/bin/env python3
"""Conditional 260805 replay: measured geometry, predicted/projected tensions.
No true online/quality claim; this reused day cannot be a new blind test.
"""
import json
import math
import statistics
import benchmark_physics_v070 as b
from audit_physics_v071 import smooth_width, OUT


def ordered_tensions(values, width, thicknesses):
    blocks = [[i, i, 1, b.tension_stress_mpa(t, width, h)]
              for i, (t, h) in enumerate(zip(values, thicknesses))]
    i = 0
    while i < len(blocks)-1:
        left, right = blocks[i:i+2]
        if left[3] <= right[3]:
            i += 1
            continue
        count = left[2]+right[2]
        blocks[i:i+2] = [[left[0], right[1], count, (left[2]*left[3]+right[2]*right[3])/count]]
        i = max(0, i-1)
    units = [0]*3
    for start, end, _, value in blocks:
        for i in range(start, end+1):
            units[i] = value
    for i in range(2, -1, -1):
        units[i] = max(0, min(units[i], b.tension_stress_mpa(14,width,thicknesses[i]), units[i+1] if i<2 else math.inf))
    return [unit*width*h/9806.65 for unit,h in zip(units,thicknesses)]


def error_margin(model):
    return model.get('validation',{}).get('holdout_260805',{}).get('p95',
        model.get('final_test',{}).get('direct',{}).get('p95_absolute_error',
            model.get('prediction_interval',{}).get('p95_half_width',0)))


def main():
    rows = [r for r in b.read_rows(b.GOLD_PATH) if r.get('golden_level') in {'GOLD_A','GOLD_B'}
            and (b.number(r,'lubrication_status_ok_ratio') or 0)>=.99]
    samples = [s for r in rows for stand in ('f1','f2') if (s:=b.sample_from_row(r,stand)) is not None]
    train = [s for s in samples if s['dataset'] < '260805']
    flows = b.fit_flow_bands(train,b.q_hill,b.tension_hill)
    stats = b.fit_flow_band_statistics(train,b.q_hill,b.tension_hill)
    factors = b.fit_width_factors(train,flows,b.q_hill,b.tension_hill)
    route = json.loads(b.ROUTE_MODEL_PATH.read_text())['models']
    tensions = ['uncoiler_tension_set_median','interstand_tension_set_median','coiler_tension_set_median']
    results = []
    for row in rows:
        if row['dataset'] != '260805': continue
        entry, middle, exit_, width = [b.number(row,k) for k in ['current_entry_thickness','f1_exit_thickness','f2_exit_thickness','width']]
        features={'entry_thickness_set_mode':entry,'exit_thickness_set_mode':exit_,'width_set_mode':width,
                  'total_reduction_ratio':1-exit_/entry,'log_thickness_ratio':math.log(entry/exit_),
                  'entry_cross_section_proxy':entry*width,'exit_cross_section_proxy':exit_*width}
        predicted = ordered_tensions([b.predict_route_model(route[key],features) for key in tensions],width,[entry,middle,exit_])
        lower = [max(0,value-error_margin(route[key])) for value,key in zip(predicted,tensions)]
        for index, stand in enumerate(('f1','f2')):
            measured=b.sample_from_row(row,stand)
            sample={**measured,'back_tension':predicted[index],'front_tension':predicted[index+1]}
            flow=flows[stand][b.thickness_band_index(sample['entry'])]
            upper=stats[stand][b.thickness_band_index(sample['entry'])]['effective_flow_stress_p95_mpa']
            factor=smooth_width(sample,factors)
            nominal=b.force_from_flow(sample,flow,b.q_hill,b.tension_hill,factor)
            high=b.force_from_flow(sample,upper,b.q_hill,b.tension_hill,factor)
            low=b.force_from_flow({**sample,'back_tension':lower[index],'front_tension':lower[index+1]},upper,b.q_hill,b.tension_hill,factor)
            dat=b.predict_route_model(route[f'{stand}_roll_force_sum_median'],features)
            old_screen=max(nominal+115.378653, high, dat+error_margin(route[f'{stand}_roll_force_sum_median']))
            results.append({'stand':stand,'actual':sample['force'],'predicted_tension_force':nominal,
                'old_screen':old_screen,'lower_tension_screen':max(old_screen,low)})
    errors=[r['predicted_tension_force']-r['actual'] for r in results]
    result={'scope':'reused 260805, physics fitted only on earlier days; measured gauge geometry and unlabelled reference material; projected DAT tension forecasts; conditional replay, not full online validation',
        'n':len(results),'mae_t':statistics.fmean(map(abs,errors)),
        'p95_absolute_error_t':b.percentile(list(map(abs,errors)),.95),'bias_t':statistics.fmean(errors),
        'old_screen_exceedances':sum(r['actual']>r['old_screen'] for r in results),
        'lower_tension_screen_exceedances':sum(r['actual']>r['lower_tension_screen'] for r in results),
        'rows':results}
    (OUT/'tension_scenario_audit.json').write_text(json.dumps(result,indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k!='rows'},indent=2))


if __name__=='__main__':main()
