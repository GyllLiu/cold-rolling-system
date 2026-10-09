"""Offline photo-window / DAT association. Does not train or alter production data.

DAT wall times are interpreted as Asia/Shanghai; capture UTC timestamps are
converted explicitly. Candidate intervals are not asserted to be true passes.
"""
from __future__ import annotations
import argparse,bisect,csv,gzip,json,sys,statistics
from collections import Counter
from datetime import datetime,timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
REPO=Path(__file__).resolve().parents[1];sys.path[:0]=[str(REPO),str(REPO.parent)]
from build_data_foundation import CORE_CHANNELS, SamplePoint,read_file_metadata,mark_suspected_freezes,build_candidate_segments
from golden_data.src.dat_sampler import sample_dat

def dump(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2,default=str))
def csvwrite(p,rows):
 if rows:
  with p.open('w',encoding='utf-8-sig',newline='') as f:
   w=csv.DictWriter(f,fieldnames=list(dict.fromkeys(k for r in rows for k in r)));w.writeheader();w.writerows(rows)
def local(s):return datetime.fromisoformat(s).astimezone(ZoneInfo('Asia/Shanghai')).replace(tzinfo=None)
def main():
 a=argparse.ArgumentParser();a.add_argument('--dat-dir',type=Path,required=True);a.add_argument('--history',type=Path,required=True);a.add_argument('--out',type=Path,required=True);a.add_argument('--corrections',type=Path);a.add_argument('--context-history',type=Path,action='append',default=[]);args=a.parse_args();args.out.mkdir(parents=True,exist_ok=True)
 corrections=json.loads(args.corrections.read_text()) if args.corrections else {}
 cache=args.out/'samples_1hz.jsonl.gz';points=[];metadata=[];errors=[]
 if not cache.exists():
  metas=sorted((read_file_metadata(p) for p in args.dat_dir.glob('*.dat')),key=lambda m:m.start_dt or datetime.min)
  with gzip.open(cache.with_suffix('.partial'),'wt') as f:
   for i,m in enumerate(metas):
    if m.metadata_status!='ok':raise ValueError(f'{m.filename}: {m.metadata_error}')
    s=sample_dat(m.path,CORE_CHANNELS.values());metadata.append({'filename':m.filename,'size':m.size_bytes,'start':m.start_dt,'end':m.end_dt,'clk':m.clk,'frames':m.frames,'schema':m.schema_hash})
    if s['errors']:errors.append({'filename':m.filename,'errors':s['errors']})
    for j,t in enumerate(s['timestamps']):
     values={k:s['data'][v][j] for k,v in CORE_CHANNELS.items() if v in s['data']}
     f.write(json.dumps({'timestamp':t.isoformat(),'file_index':i,'filename':m.filename,'values':values},ensure_ascii=False)+'\n')
    print(f'{i+1}/{len(metas)} {m.filename}',flush=True)
  cache.with_suffix('.partial').rename(cache);dump(args.out/'dat_metadata.json',metadata);dump(args.out/'decode_errors.json',errors)
 with gzip.open(cache,'rt') as f:
  for line in f:
   x=json.loads(line);x['timestamp']=datetime.fromisoformat(x['timestamp']);points.append(SamplePoint(**x))
 points.sort(key=lambda p:p.timestamp);ts=[p.timestamp for p in points]
 assert len(set(ts))==len(ts),'Overlapping DAT timestamps require manual resolution'
 frozen=mark_suspected_freezes(points,300);dump(args.out/'suspected_freezes.json',frozen)
 segments=build_candidate_segments(points);csvwrite(args.out/'dat_segments.csv',segments)
 photos=sorted(json.loads(args.history.read_text())['items'],key=lambda x:x['started_at'] or x['captured_at']);matches=[]
 for photo in photos:
  if not photo['started_at'] or not photo['ended_at']:continue
  start,end=local(photo['started_at']),local(photo['ended_at']);window=points[bisect.bisect_left(ts,start):bisect.bisect_left(ts,end)]
  active=[p for p in window if not p.suspected_frozen and abs(p.values.get('line_speed_actual') or 0)>10]
  threshold=max(50,0.7*max((abs(p.values.get('line_speed_actual') or 0) for p in active),default=0))
  plateau=[p for p in active if abs(p.values.get('line_speed_actual') or 0)>=threshold]
  tandem=[p for p in plateau if (p.values.get('f1_roll_force_sum') or 0)>1 and (p.values.get('f2_roll_force_sum') or 0)>1 and 0<(p.values.get('exit_thickness_set') or 0)<(p.values.get('intermediate_thickness_set') or 0)<(p.values.get('entry_thickness_set') or 0)-0.005]
  # 1 T and 0.005 mm only exclude numerical zero / non-reduction for this audit.
  summary_points=plateau
  med=lambda key:round(statistics.median([p.values[key] for p in summary_points if p.values.get(key) is not None]),5) if any(p.values.get(key) is not None for p in summary_points) else None
  specs=Counter(tuple(round(p.values.get(k) or 0,3) for k in ['entry_thickness_set','intermediate_thickness_set','exit_thickness_set','width_set']) for p in plateau)
  overlaps=[]
  for s in segments:
   st,et=datetime.fromisoformat(s['start_time']),datetime.fromisoformat(s['end_time']);duration=max(0,(min(end,et)-max(start,st)).total_seconds())
   if duration>0:overlaps.append((s,duration))
  fields=photo['fields'];batch=fields.get('batch_no');correction=''
  reviewed=corrections.get(photo['photo_id'])
  if reviewed:
   assert batch==reviewed['original_batch_no'],'OCR changed since correction review'
   batch=reviewed['batch_no'];correction=reviewed['reason']
  r={'photo_id':photo['photo_id'],'record_id':photo['record_id'],'batch_no':batch,'original_batch_no':fields.get('batch_no'),'batch_correction':correction,'alloy_ocr':fields.get('alloy'),'card_state_ocr':fields.get('card_state'),'ocr_status':photo['ocr_status'],'start_beijing':start.isoformat(),'end_beijing':end.isoformat(),'window_seconds':round((end-start).total_seconds(),1),'dat_samples_1hz':len(window),'active_seconds_1hz':len(active),'high_speed_seconds_1hz':len(plateau),'tandem_high_speed_seconds_1hz':len(tandem),'high_speed_threshold':round(threshold,2),'dat_active_start':active[0].timestamp.isoformat() if active else None,'dat_active_end':active[-1].timestamp.isoformat() if active else None,'candidate_ids':';'.join(s['candidate_id'] for s,_ in overlaps),'significant_candidate_count':sum(d>=30 for _,d in overlaps),'specs_high_speed':json.dumps([{'entry':sp[0],'middle':sp[1],'exit':sp[2],'width':sp[3],'seconds_1hz':n} for sp,n in specs.most_common()],ensure_ascii=False),'dat_source_files':';'.join(dict.fromkeys(p.filename for p in window)),'screening_mode':'tandem_candidate' if len(tandem)>=30 else 'single_stand_or_insufficient_tandem','training_approved':False,'training_blockers':'labels_require_review;card_state_is_not_verified_incoming_state;route_not_verified'}
  for k in ['entry_thickness_set','intermediate_thickness_set','exit_thickness_set','entry_thickness_actual','exit_thickness_actual','width_set','line_speed_actual','f1_roll_force_sum','f2_roll_force_sum','uncoiler_tension_actual','interstand_tension_actual','coiler_tension_actual','uncoiler_tension_set','interstand_tension_set','coiler_tension_set']:r[k+'_median']=med(k)
  matches.append(r)
 context_photos=[p for path in args.context_history for p in json.loads(path.read_text())['items'] if p['started_at'] and p['ended_at']]
 for s in segments:
  st,et=datetime.fromisoformat(s['start_time']),datetime.fromisoformat(s['end_time'])
  covering=[r['batch_no'] for r in matches if max(st,datetime.fromisoformat(r['start_beijing']))<min(et,datetime.fromisoformat(r['end_beijing']))]
  covering+=['context:'+p['fields'].get('batch_no','') for p in context_photos if max(st,local(p['started_at']))<min(et,local(p['ended_at']))]
  s['overlapping_photo_batches']=';'.join(covering);s['association']='no_photo_window' if not covering else 'candidate_overlap_only'
 csvwrite(args.out/'dat_segments.csv',segments);csvwrite(args.out/'photo_dat_matches.csv',matches);dump(args.out/'photo_dat_matches.json',matches)
 summary={'dat_start_beijing':ts[0],'dat_end_beijing':ts[-1]+timedelta(seconds=1),'sample_interval_seconds':1,'raw_interval_seconds':.01,'photos':len(photos),'matched_with_activity':sum(r['active_seconds_1hz']>0 for r in matches),'matched_with_120s_tandem_high_speed':sum(r['tandem_high_speed_seconds_1hz']>=120 for r in matches),'unique_batches':len(set(r['batch_no'] for r in matches)),'segments':len(segments),'segments_without_photo_overlap':sum(not s['overlapping_photo_batches'] for s in segments),'training_approved':0,'timezone_assumption':'DAT wall clock Asia/Shanghai, UTC capture converted; no arbitrary shift applied','caveat':'1Hz exploratory summaries; not yet 100Hz training labels or verified complete routes'}
 dump(args.out/'summary.json',summary);print(json.dumps(summary,ensure_ascii=False,default=str))
if __name__=='__main__':main()
