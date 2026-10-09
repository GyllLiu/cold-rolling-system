#!/usr/bin/env node
// Console-only experiment: never writes a report, changes the live model, or tunes on 260805.
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { loadTypescript } from '../tests/helpers/load-typescript.mjs';
const root = new URL('../',import.meta.url);
const { module: planner } = await loadTypescript(new URL('app/lib/route-planner.ts',root));
const input = {alloy:'1060',incomingState:'annealed',targetState:'h18',entryThickness:6,targetThickness:2.8,width:1280};
const example = planner.createAllocationFusionEvaluator(input);
const exampleResults = [3.9,4].map(middle=>example.evaluate(middle));
const python = spawnSync('python3',[fileURLToPath(new URL('scripts/export_allocation_validation.py',root))],{encoding:'utf8',maxBuffer:20*1024*1024});
if(python.status!==0) throw new Error(python.stderr || 'validation input export failed');
const {calibration,rows,referenceAlloy} = JSON.parse(python.stdout);
const quantile = (values,p) => {
  const sorted=[...values].sort((a,b)=>a-b), pos=(sorted.length-1)*p;
  const low=Math.floor(pos), high=Math.ceil(pos);
  return sorted[low]+(sorted[high]-sorted[low])*(pos-low);
};
const metrics = values => {
  if(!values.length) return null;
  const errors=values.map(v=>v.predicted-v.actual);
  return {n:errors.length,mae:errors.reduce((s,e)=>s+Math.abs(e),0)/errors.length,
    p95:quantile(errors.map(Math.abs),.95),bias:errors.reduce((s,e)=>s+e,0)/errors.length};
};
const results=[];
for(const row of rows) {
  // Unlabelled samples must use the model's unscaled reference, not an assumed
  // 1060 material correction on one side of a supposedly paired comparison.
  const raw={...input,alloy:referenceAlloy,entryThickness:row.entry,targetThickness:row.exit,width:row.width};
  try {
    const evaluator=planner.createAllocationFusionEvaluator(raw,{calibration});
    if (evaluator.referenceData.some((force,i)=>Math.abs(force-row.datForces[i])>1e-6)) throw new Error('unpaired_DAT_reference');
    const predicted=evaluator.evaluate(row.middle);
    const observed=evaluator.evaluate(row.middle,row.actualTensions);
    results.push({...row,predicted,observed});
  } catch(error) {
    if(error.message==='unpaired_DAT_reference') throw error;
    results.push({...row,referenceFailure:error.message});
  }
}
const summarize=(group)=>{
  const output={plateaus:group.length,referenceUnavailable:group.filter(r=>r.referenceFailure).length};
  for(const variant of ['predicted','observed']) {
    const supported=group.filter(r=>r[variant]?.status==='evaluated');
    const subsetBaseline=supported.flatMap(r=>r.actualForces.map((actual,i)=>({actual,predicted:r.datForces[i]})));
    const subsetFusion=supported.flatMap(r=>r.actualForces.map((actual,i)=>({actual,predicted:r[variant].fusedForces[i]})));
    output[variant]={supportedPlateaus:supported.length,baselineOnSameSubset:metrics(subsetBaseline),fusionOnSameSubset:metrics(subsetFusion),
      allRowsBaseline:metrics(group.flatMap(r=>r.actualForces.map((actual,i)=>({actual,predicted:r.datForces[i]})))),
      allRowsFusionWithExplicitDatFallback:metrics(group.flatMap(r=>r.actualForces.map((actual,i)=>({actual,predicted:r[variant]?.status==='evaluated'?r[variant].fusedForces[i]:r.datForces[i]})))),
      byStand:['f1','f2'].map((stand,i)=>({stand,baseline:metrics(supported.map(r=>({actual:r.actualForces[i],predicted:r.datForces[i]}))),fusion:metrics(supported.map(r=>({actual:r.actualForces[i],predicted:r[variant].fusedForces[i]})))})),
      unsupportedReasons:group.filter(r=>r[variant]?.status==='unsupported').reduce((counts,r)=>{for(const reason of r[variant].reasons)counts[reason]=(counts[reason]??0)+1;return counts;},{})};
  }
  return output;
};
const selectedRouteCoverage=[];
for(const [entry,target] of [[7,.7],[7,.4],[7,.3],[6,.5],[6,1],[6,.8],[6.4,4.2],[6.4,3.7],[6.4,1.15],[6.4,.77],[6.4,.43],[6.4,.33]]) {
 const plan=planner.createRoutePlan({...input,entryThickness:entry,targetThickness:target,width:entry===6.4?1320:1280});
 let strain=0;
 selectedRouteCoverage.push({entry,target,passes:plan.passes.map(pass=>{
   const evaluator=planner.createAllocationFusionEvaluator({...plan.input,entryThickness:pass.entryThickness,targetThickness:pass.exitThickness},{routeStrainBefore:strain});
   const result=evaluator.evaluate(pass.intermediateThickness);
   strain+=Math.log(pass.entryThickness/pass.exitThickness);
   return {pass:pass.index,status:result.status,...(result.status==='unsupported'?{reasons:result.reasons}:{fused:result.fusedForces})};
 })});
}
console.log(JSON.stringify({scope:'conditional measured-midpoint replay; no alloy/temper labels; 260805 reused, not blind; fixed local policy without audit-day tuning',
 example:{referenceMiddle:example.referenceMiddle,referenceData:example.referenceData,results:exampleResults},
 earlierDatesRetrospective:summarize(results.filter(r=>r.date<'260805')),
 reused260805:summarize(results.filter(r=>r.date==='260805')),selectedRouteCoverage},null,2));
