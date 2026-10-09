import { readFile, writeFile } from 'node:fs/promises';
import { loadTypescript } from '../tests/helpers/load-typescript.mjs';
const root = new URL('../',import.meta.url);
const out = new URL('../../outputs/astra_review_v071/',import.meta.url);
const source = new URL('app/lib/route-planner.ts',root);
const beforeSource = await readFile(new URL('before/route-planner.ts',out),'utf8');
const before = (await loadTypescript(source,beforeSource)).module;
const after = (await loadTypescript(source)).module;
const cases=[[7,.7],[7,.4],[7,.3],[6,.5],[6,1],[6,.8],[6.4,4.2],[6.4,3.7],[6.4,1.15],[6.4,.77],[6.4,.43],[6.4,.33],[6.4,.22],[6.4,.17]];
const input=(entryThickness,targetThickness,width=entryThickness===6.4?1320:1280)=>({alloy:'1060',incomingState:'annealed',targetState:'h18',entryThickness,targetThickness,width});
const route=(plan)=>plan.passes.map(p=>[p.entryThickness,p.intermediateThickness,p.exitThickness]);
const replay=[];
for(const [entry,target] of cases) {
  const raw=input(entry,target), old=before.createRoutePlan(raw), current=after.createRoutePlan(raw);
  replay.push({input:raw,before:{passCount:old.passCount,route:route(old),maxCheck:Math.max(...old.passes.flatMap(p=>[p.f1Force?.safetyTotal??0,p.f2Force?.safetyTotal??0]))},after:current});
}
const probes=[];
for(const [entry,target] of cases) for(const delta of [-1,-.1,0,.1,1]) probes.push(input(entry,target,(entry===6.4?1320:1280)+delta));
for(const edge of [1150,1220,1280,1350]) for(const delta of [-.01,0,.01]) probes.push(input(6.4,.77,edge+delta));
for(const target of [.2499,.25,.2501,.2849,.285,.2851,.4999,.5,.5001]) probes.push(input(6.4,target));
for(const entry of [.7499,.75,.7501,1.2499,1.25,1.2501,1.9999,2,2.0001,2.9999,3,3.0001,4.4999,4.5,4.5001]) probes.push(input(entry,.3));
const evaluate=(planner,raw)=>{
  try {
    const plan=planner.createRoutePlan(raw);
    const failures=[];
    for(const [i,p] of plan.passes.entries()) {
      if(!(p.entryThickness>p.intermediateThickness&&p.intermediateThickness>p.exitThickness)) failures.push('gauge_order');
      if(i&&p.entryThickness!==plan.passes[i-1].exitThickness) failures.push('route_closure');
      if(p.f1Force && (p.f1Force.safetyTotal>1000||p.f2Force.safetyTotal>1000)) failures.push('force_limit');
    }
    return {passCount:plan.passCount,route:route(plan),maximumCheck:Math.max(...plan.passes.flatMap(p=>[p.f1Force?.safetyTotal??0,p.f2Force?.safetyTotal??0])),failures,method:plan.evidence.routeMethod};
  } catch(error) {return {error:error.message};}
};
const perturbations=probes.map(raw=>({input:raw,before:evaluate(before,raw),after:evaluate(after,raw)}));
const summary={replayCases:replay.length,unchangedPassCount:replay.filter(r=>r.before.passCount===r.after.passCount).length,unchangedGaugeRoutes:replay.filter(r=>JSON.stringify(r.before.route)===JSON.stringify(route(r.after))).length,probeCount:perturbations.length,beforeRejected:perturbations.filter(r=>r.before.error).length,afterRejected:perturbations.filter(r=>r.after.error).length,newlyRejected:perturbations.filter(r=>!r.before.error&&r.after.error).map(r=>r.input),afterInvariantFailures:perturbations.filter(r=>r.after.failures?.length).length};
await writeFile(new URL('route_replay_comparison.json',out),JSON.stringify(replay,null,2)+'\n');
await writeFile(new URL('perturbation_audit.json',out),JSON.stringify({summary,probes:perturbations},null,2)+'\n');
console.log(JSON.stringify(summary,null,2));
