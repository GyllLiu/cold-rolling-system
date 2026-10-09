import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { loadTypescript } from './helpers/load-typescript.mjs';
const { module: planner } = await loadTypescript(new URL('../app/lib/route-planner.ts', import.meta.url));
const { module: policy } = await loadTypescript(new URL('../app/lib/factory-route-policy.ts', import.meta.url));
const { module: log } = await loadTypescript(new URL('../app/lib/trial-log.ts', import.meta.url));
const input = { alloy:'1060', incomingState:'annealed', entryThickness:6, targetThickness:.7, width:1280, targetState:'h18' };

test('factory thickness thirds are continuous and thicker finishes permit larger reductions', () => {
  assert.deepEqual(policy.thicknessRegion(.1).boundariesMm.map(x=>Number(x.toFixed(4))), [.1,2.4,4.7,7]);
  assert.equal(policy.thicknessRegion(2.4).name,'薄段');
  assert.equal(policy.thicknessRegion(2.4001).name,'中段');
  assert.equal(policy.thicknessRegion(4.7001).name,'厚段');
  let previous = 0;
  for (let h=.1; h<=7; h+=.01) {
    const r=policy.finishingPreference(h); assert.ok(r.targetF1>=previous); previous=r.targetF1;
  }
  for(const edge of [2.4,4.7]) assert.ok(Math.abs(policy.finishingPreference(edge-1e-6).targetF1-policy.finishingPreference(edge+1e-6).targetF1)<1e-6);
});

test('single complete pass has exactly five points between stands and no finishing cap', () => {
  const plan=planner.createRoutePlan({...input,entryThickness:6.4,targetThickness:2.2});
  assert.equal(plan.passCount,1); assert.equal(plan.finishingBand,'single_pass');
  assert.ok(plan.passes[0].f1ReductionPercent>38);
  assert.ok(Math.abs(plan.passes[0].f1MinusF2PercentPoints-5)<1e-8);
  assert.throws(()=>planner.createRoutePlan({...input,entryThickness:1,targetThickness:.98}),/5个百分点/);
});

test('F1 and F2 taper independently near five points for hardened incoming material', () => {
  for(const alloy of ['1060','1100','3003']) {
    const plan=planner.createRoutePlan({...input,alloy,incomingState:'h18'});
    for(const option of [plan,...plan.alternatives]) for(let i=1;i<option.passes.length;i++) {
      for(const key of ['f1ReductionPercent','f2ReductionPercent']) {
        const drop=option.passes[i-1][key]-option.passes[i][key];
        assert.ok(drop>=3-1e-7&&drop<=7+1e-7,`${alloy} ${key} ${drop}`);
      }
    }
  }
});

test('hard-state and 1100 capacities follow relative ranges, not percentage-point subtraction', () => {
  const soft=policy.materialReductionLimit(input,6,true);
  const hard=policy.materialReductionLimit({...input,incomingState:'h18'},6,false);
  assert.ok(hard.maximum/soft.maximum>=.8-1e-8&&hard.maximum/soft.maximum<=.9+1e-8);
  for(const state of ['annealed','h14','h18']) for(const entry of [6,3,1]) {
    const a=policy.materialReductionLimit({...input,incomingState:state},entry,true);
    const b=policy.materialReductionLimit({...input,alloy:'1100',incomingState:state},entry,true);
    assert.ok(b.maximum/a.maximum>=.9-1e-8&&b.maximum/a.maximum<=.95+1e-8);
  }
  const f1=policy.passReductionLimits(input,{entry:6,middle:3,exit:1.8},0);
  assert.equal(f1.f1.stateFactor,1); assert.ok(f1.f2.stateFactor<1);
  assert.equal(f1.f2.stageCondition,'加工硬化料');
});

test('opening can exceed fifty while 3003 never exceeds forty-five', () => {
  const plan=planner.createRoutePlan({...input,entryThickness:7,targetThickness:.1});
  assert.ok([plan,...plan.alternatives].some(option=>option.passes[0].f1ReductionPercent>50));
  for(const incomingState of ['annealed','h14','h18']) {
    const r=planner.createRoutePlan({...input,alloy:'3003',incomingState});
    assert.ok([r,...r.alternatives].every(option=>option.passes[0].f1ReductionPercent<=45+1e-8));
  }
});

test('two and three pass options both reach the exact gauge and retain separate audit identities', () => {
  const plan=planner.createRoutePlan(input);
  assert.equal(plan.passCount,3); assert.equal(plan.alternatives[0].passCount,2);
  assert.equal(plan.alternatives[0].kind,'fewest_passes');
  for(const option of [plan,...plan.alternatives]) {
    assert.equal(option.passes.at(-1).exitThickness,input.targetThickness);
    assert.equal(option.assessment.qualityValidated,false);
    assert.ok(option.passes.every(p=>p.f1Force.safetyTotal<=1000&&p.f2Force.safetyTotal<=1000));
  }
  assert.ok(plan.selection.rankingOrder.every(key=>!/load|force/.test(key)));
  assert.equal(plan.anneal.intermediate,'excluded'); assert.equal(plan.anneal.reviewAfterPass,null);
  const csv=log.routeTrialCsv([{recordId:'example',plan}]);
  assert.equal(csv.split('\r\n').length,1+3+2);
  assert.match(csv,/recommended/); assert.match(csv,/fewest_passes/);
});

test('width affects physical load even though lower load has no sorting preference', () => {
  const request={...input,entryThickness:6.4,targetThickness:2.8};
  const narrow=planner.createRoutePlan({...request,width:1000});
  const wide=planner.createRoutePlan({...request,width:1575});
  assert.equal(narrow.passCount,1); assert.equal(wide.passCount,1);
  assert.equal(narrow.passes[0].intermediateThickness,wide.passes[0].intermediateThickness);
  assert.ok(wide.passes[0].f1Force.physicsBaseline>narrow.passes[0].f1Force.physicsBaseline);
});

test('0.1 equipment endpoint is route only and does not extrapolate DAT or certify load', () => {
  const plan=planner.createRoutePlan({...input,entryThickness:7,targetThickness:.1});
  assert.equal(plan.passes.at(-1).exitThickness,.1);
  for(const option of [plan,...plan.alternatives]) {
    assert.equal(option.assessment.equipment,'not_evaluated');
    assert.ok(option.passes.every(pass=>pass.f1Force===null&&pass.f2Force===null&&pass.parameters.length===0));
  }
  assert.throws(()=>planner.createRoutePlan({...input,targetThickness:.099}),/0.1/);
  assert.throws(()=>planner.createRoutePlan({...input,entryThickness:7.1}),/7/);
});

test('runtime historical source contains only active tandem stands with provenance', async () => {
  const artifact=JSON.parse(await readFile(new URL('../backend/app/model_artifacts/route_history_tandem_v072.json',import.meta.url),'utf8'));
  assert.ok(artifact.records.length>0);
  assert.ok(artifact.records.every(r=>r.f1_force_t>0&&r.f2_force_t>0&&r.entry>r.middle&&r.middle>r.exit));
});
