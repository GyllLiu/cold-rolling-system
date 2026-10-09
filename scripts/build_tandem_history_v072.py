"""Audit immutable source inputs and write a tandem-only route history view."""
import csv
import json
from collections import Counter
from pathlib import Path
from tandem_data_filter import tandem_exclusion_reason
import train_compare_high_speed_v6 as v6

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT.parent / 'outputs/golden_recipe_v1/golden_recipe_dataset.csv'
ARTIFACTS = ROOT / 'backend/app/model_artifacts'


def read(path):
    with path.open(encoding='utf-8-sig', newline='') as handle:
        return list(csv.DictReader(handle))


def main():
    rows = read(SOURCE)
    reasons = Counter(tandem_exclusion_reason(row) for row in rows)
    retained = [row for row in rows if tandem_exclusion_reason(row) is None
                and row.get('golden_level') in {'GOLD_A', 'GOLD_B'}
                and float(row.get('lubrication_status_ok_ratio') or 0) >= .99]
    records = []
    for row in retained:
        entry, middle, exit_, width = [float(row[key]) for key in
            ['current_entry_thickness', 'f1_exit_thickness', 'f2_exit_thickness', 'width']]
        if not entry > middle > exit_ > 0:
            continue
        records.append(dict(entry=entry, middle=middle, exit=exit_, width=width,
            stable_seconds=float(row['stable_duration']), date=row['dataset'], dataset=row['dataset'],
            f1_force_t=float(row['f1_force_actual_median']), f2_force_t=float(row['f2_force_actual_median']),
            golden_level=row['golden_level'], source_segment_id=row['golden_segment_id']))
    paths = sorted((ROOT.parent/'outputs').glob('data_foundation*/production_segments.csv'))
    source_counts = Counter(tandem_exclusion_reason(row) for path in paths for row in read(path))
    after = v6.load_cases(paths)
    original = v6.is_tandem_record
    try:
        v6.is_tandem_record = lambda row: True
        before = v6.load_cases(paths)
    finally:
        v6.is_tandem_record = original
    removed_training_ids = sorted({row.candidate_id for row in before} - {row.candidate_id for row in after})
    audit = {'version': 'tandem-data-scope-v0.7.2',
        'rule': 'exclude measured F1=0/F2>0, inactive or unknown stands, and explicit intermediate annealing',
        'raw_data_modified': False, 'golden_source_rows': len(rows), 'tandem_history_records': len(records),
        'golden_exclusions': {k:v for k,v in reasons.items() if k is not None},
        'production_segment_exclusions': {k:v for k,v in source_counts.items() if k is not None},
        'eligible_dat_cases_before': len(before), 'retained_dat_training_cases': len(after),
        'balanced_training_cases': len(v6.balance_cases(after)),
        'removed_training_ids': removed_training_ids,
        'model_refit_required': any(k is not None for k in reasons) or bool(removed_training_ids),
        'limitation': 'audit covers existing derived windows/segments; no claim that raw DAT contains no single-stand intervals'}
    (ARTIFACTS/'route_history_tandem_v072.json').write_text(json.dumps({
        'artifact_version': 'tandem-route-history-v0.7.2', 'case_count': len(records),
        'source_dates': sorted({row['dataset'] for row in records}), 'selection': audit,
        'records': records}, ensure_ascii=False, separators=(',', ':'))+'\n')
    (ARTIFACTS/'tandem_data_audit_v072.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(audit,ensure_ascii=False))


if __name__ == '__main__':
    main()
