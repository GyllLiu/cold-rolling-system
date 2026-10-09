import assert from 'node:assert/strict';
import test from 'node:test';
import { loadTypescript } from './helpers/load-typescript.mjs';
const { module: physics } = await loadTypescript(new URL('../app/lib/rolling-physics.ts', import.meta.url));
const { module: planner } = await loadTypescript(new URL('../app/lib/route-planner.ts', import.meta.url));
const standard = {
  entry: 2, exit: 1.2, width: 1200, backTension: 5, frontTension: 6,
  flowStressMpa: 180, mu: .08, radiusMm: 190, youngModulusMpa: 210000,
  poissonRatio: .3, backWeight: .7, frontWeight: .3, widthFactor: 1,
  maximumIterations: 50, radiusToleranceMm: 1e-8,
};
const input = { alloy: '1060', incomingState: 'annealed', targetState: 'h18', entryThickness: 6.4, targetThickness: .77, width: 1320 };

test('tonne-force and MPa conversion has no factor-of-1000 error', () => {
  assert.ok(Math.abs(physics.tensionStressMpa(10, 1000, 1) - 98.0665) < 1e-10);
});
test('Hill formula equals the outlet-thickness form, with no extra reduction square root', () => {
  const r = .4, radius = 220, entry = 2, exit = entry * (1 - r), mu = .08;
  const reference = 1.08 + 1.79 * mu * r * Math.sqrt(1-r) * Math.sqrt(radius/exit) - 1.02*r;
  assert.ok(Math.abs(physics.hillQ(mu, radius, entry, exit) - reference) < 1e-12);
});
test('Swift average agrees with numerical quadrature and remains stable for tiny strain increments', () => {
  const p = { K_mpa: 105.160345, epsilon_0: .018168, n: .31294 };
  let integral = 0; const n = 10000, start = .4, end = 1.3;
  for (let i=0;i<n;i++) integral += p.K_mpa * (p.epsilon_0+start+(i+.5)*(end-start)/n)**p.n/n;
  assert.ok(Math.abs(physics.swiftMeanStress(p,start,end)-integral)<1e-6);
  assert.ok(Math.abs(physics.swiftMeanStress(p,1,1+1e-12)-p.K_mpa*(p.epsilon_0+1)**p.n)<1e-8);
});
test('converged force and geometry satisfy the independent Hitchcock equation', () => {
  const result = physics.solveRollingForce(standard);
  const radius = 190*(1+16*(1-.3**2)/(Math.PI*210000)*(result.forceT*9806.65)/(1200*.8));
  assert.ok(Math.abs(radius-result.flattenedRadiusMm)<=1e-8);
  assert.ok(Math.abs(result.contactLengthMm**2-result.flattenedRadiusMm*.8)<1e-9);
});
test('width scaling at equal unit tension is linear and lower tension raises force', () => {
  const force = physics.solveRollingForce(standard).forceT;
  const doubled = physics.solveRollingForce({ ...standard, width: 2400, backTension: 10, frontTension: 12 }).forceT;
  assert.ok(Math.abs(doubled-2*force)<1e-8);
  assert.ok(physics.solveRollingForce({ ...standard, backTension: 0, frontTension: 0 }).forceT > force);
});
test('invalid pressure and nonconvergence cannot masquerade as low safe loads', () => {
  assert.throws(()=>physics.solveRollingForce({ ...standard, maximumIterations: 1 }), /未收敛/);
  assert.throws(()=>physics.solveRollingForce({ ...standard, flowStressMpa: 1 }), /张力超过/);
  assert.throws(()=>physics.solveRollingForce({ ...standard, entry: NaN }), /适用域/);
});
test('width correction is continuous at all original edges and bounded by band factors', () => {
  const bands=[{width_low_mm:1000,width_high_mm:1150,factor:.95},{width_low_mm:1150,width_high_mm:1220,factor:1.05},{width_low_mm:1220,width_high_mm:1575,factor:.98}];
  for(const edge of [1140,1150,1160,1210,1220,1230]) assert.ok(Math.abs(physics.smoothWidthFactor(bands,edge-1e-6)-physics.smoothWidthFactor(bands,edge+1e-6))<1e-7);
  for(let width=900;width<=1800;width++) assert.ok(physics.smoothWidthFactor(bands,width)>=.95 && physics.smoothWidthFactor(bands,width)<=1.05);
});
test('route-only output never claims equipment evaluation and exposes candidate scope', () => {
  const plan=planner.createRoutePlan({...input,targetThickness:.22});
  assert.equal(plan.assessment.equipment,'not_evaluated');
  assert.equal(plan.assessment.dataCoverage,'unavailable');
  assert.equal(plan.minimumPassCountScope,'generated_candidates_only');
  assert.equal(plan.assessment.qualityValidated,false);
});
test('candidate audit agrees with chosen route and lower-tension screen is included', () => {
  const plan=planner.createRoutePlan(input);
  const selected=plan.selection.candidates.find(c=>c.id===plan.selection.selectedCandidateId);
  assert.equal(selected.accepted,true); assert.equal(selected.passCount,plan.passCount);
  assert.deepEqual(selected.reasons,[]);
  assert.ok(plan.selection.candidates.some(c=>!c.accepted&&c.reasons.length));
  for(const pass of plan.passes) for(const force of [pass.f1Force,pass.f2Force]) {
    assert.ok(force.safetyTotal>=force.lowTensionCheckTotal);
    assert.ok(force.safetyTotal>=force.total);
    assert.ok(force.convergence.radiusResidualMm<=1e-8);
  }
});
test('all alloy and incoming-state combinations close the route and respect limits', () => {
  for(const alloy of ['1060','1100','3003']) for(const incomingState of ['annealed','h14','h16','h18','h22','h24','h26','unannealed','unknown']) {
    const plan=planner.createRoutePlan({...input,alloy,incomingState,entryThickness:1.5});
    assert.equal(plan.passes.at(-1).exitThickness,.77);
    for(const pass of plan.passes) {
      assert.ok(pass.f1ReductionPercent-pass.f2ReductionPercent>=.5-1e-9);
      const tensions=pass.parameters.filter(p=>p.unitTension!=null);
      assert.ok(tensions.every(t=>t.value>=0 && t.value<=14));
      assert.ok(tensions[0].unitTension<=tensions[1].unitTension && tensions[1].unitTension<=tensions[2].unitTension);
      assert.ok(pass.f1Force.safetyTotal<=1000 && pass.f2Force.safetyTotal<=1000);
    }
  }
});

test('intermediate annealing is excluded even beyond old cumulative thresholds', () => {
  const plan = planner.createRoutePlan({...input,alloy:'3003',entryThickness:3,targetThickness:.7});
  assert.ok(plan.totalReductionPercent>=70);
  assert.equal(plan.anneal.intermediate,'excluded');
  assert.equal(plan.anneal.reviewAfterPass,null);
});

test('the former 1280 mm width discontinuity does not switch the 6 to 0.5 route', () => {
  const gauges = [1279.9,1280,1280.1].map(width => planner.createRoutePlan({
    ...input,entryThickness:6,targetThickness:.5,width,
  }).passes.map(pass=>[pass.intermediateThickness,pass.exitThickness]));
  assert.deepEqual(gauges[0],gauges[1]);
  assert.deepEqual(gauges[1],gauges[2]);
});

test('all 14 reference specifications and alternatives retain exact thickness closure', () => {
  const cases = [[7,.7,3],[7,.4,3],[7,.3,4],[6,.5,3],[6,1,2],[6,.8,2],[6.4,4.2,1],[6.4,3.7,1],[6.4,1.15,2],[6.4,.77,3],[6.4,.43,3],[6.4,.33,3],[6.4,.22,4],[6.4,.17,4]];
  for(const [entryThickness,targetThickness] of cases) {
    const plan=planner.createRoutePlan({...input,entryThickness,targetThickness,width:entryThickness===6.4?1320:1280});
    assert.ok(plan.passCount>=1&&plan.passCount<=8);
    assert.equal(plan.passes[0].entryThickness,entryThickness);
    assert.equal(plan.passes.at(-1).exitThickness,targetThickness);
    for(const [index,pass] of plan.passes.entries()) {
      if(index) assert.equal(pass.entryThickness,plan.passes[index-1].exitThickness);
      assert.ok(pass.entryThickness>pass.intermediateThickness && pass.intermediateThickness>pass.exitThickness);
      assert.ok(pass.f1Force===null || pass.f1Force.safetyTotal<=1000);
      assert.ok(pass.f2Force===null || pass.f2Force.safetyTotal<=1000);
    }
  }
});

test('allocation fusion exactly recovers the material-adjusted DAT force at its fixed reference', () => {
  for (const alloy of ['1060','1100','3003']) {
    const raw={...input,alloy,entryThickness:6,targetThickness:2.8,width:1280};
    const evaluator=planner.createAllocationFusionEvaluator(raw);
    const result=evaluator.evaluate(evaluator.referenceMiddle);
    assert.equal(evaluator.mode,'shadow_only'); assert.equal(evaluator.canApply,false);
    assert.equal(result.status,'evaluated');
    assert.deepEqual(result.ratios,[1,1]);
    assert.deepEqual(result.fusedForces,evaluator.referenceData);
    const actualPath=planner.evaluateFixedThicknessRoute(raw,[{intermediateThickness:evaluator.referenceMiddle,exitThickness:2.8}])[0];
    assert.ok(Math.abs(result.fusedForces[0]-actualPath.f1Force.total)<.0001);
    assert.ok(Math.abs(result.fusedForces[1]-actualPath.f2Force.total)<.0001);
  }
});

test('allocation fusion transfers load from F1 to F2 when F1 exit thickness increases', () => {
  const evaluator=planner.createAllocationFusionEvaluator({...input,entryThickness:6,targetThickness:2.8,width:1280});
  const heavierF1=evaluator.evaluate(3.9), lighterF1=evaluator.evaluate(4);
  assert.equal(heavierF1.status,'evaluated'); assert.equal(lighterF1.status,'evaluated');
  assert.ok(lighterF1.fusedForces[0]<heavierF1.fusedForces[0]);
  assert.ok(lighterF1.fusedForces[1]>heavierF1.fusedForces[1]);
  assert.deepEqual(heavierF1.referenceData,lighterF1.referenceData);
  assert.deepEqual(heavierF1.referencePhysics,lighterF1.referencePhysics);
  assert.deepEqual(evaluator.evaluate(3.9),heavierF1);
});

test('shared interstand tension changes both stand predictions without recalibrating the reference', () => {
  const evaluator=planner.createAllocationFusionEvaluator({...input,entryThickness:6,targetThickness:2.8,width:1280});
  const base=evaluator.evaluate(evaluator.referenceMiddle);
  const tensions=[...evaluator.referenceTensions]; tensions[1]+=.3;
  const result=evaluator.evaluate(evaluator.referenceMiddle,tensions);
  assert.equal(result.status,'evaluated');
  assert.ok(result.fusedForces[0]<base.fusedForces[0]);
  assert.ok(result.fusedForces[1]<base.fusedForces[1]);
  assert.deepEqual(result.referencePhysics,base.referencePhysics);
});

test('unsupported allocation and excessive tension are explicit failures, not clipped safe-looking results', () => {
  const evaluator=planner.createAllocationFusionEvaluator({...input,entryThickness:6,targetThickness:2.8,width:1280});
  assert.deepEqual(evaluator.evaluate(5),{status:'unsupported',reasons:['outside_local_allocation_range']});
  assert.equal(evaluator.evaluate(6).status,'unsupported');
  assert.equal(evaluator.evaluate(4,[5,14.1,5]).status,'unsupported');
  assert.throws(()=>planner.createAllocationFusionEvaluator({...input,incomingState:'unknown'}),/来料状态/);
  assert.throws(()=>planner.createAllocationFusionEvaluator({...input,targetThickness:.22}),/覆盖/);
});

test('a local response crossing the old 1.25 mm F2 band edge has no artificial jump', async () => {
  const {module:fusion}=await loadTypescript(new URL('../app/lib/allocation-fusion.ts',import.meta.url));
  const fixed={entry:2,exit:.8,width:1200,equipment:standard,widthFactors:[1,1],
    reference:{middle:1.25,tensions:[5,6,5],dataForces:[300,300],flowStresses:[180,195]}};
  const left=fusion.computeAllocationFusion({...fixed,candidate:{middle:1.25-1e-7,tensions:[5,6,5]}});
  const right=fusion.computeAllocationFusion({...fixed,candidate:{middle:1.25+1e-7,tensions:[5,6,5]}});
  assert.equal(left.status,'evaluated'); assert.equal(right.status,'evaluated');
  assert.ok(Math.abs(left.fusedForces[0]-right.fusedForces[0])<.001);
  assert.ok(Math.abs(left.fusedForces[1]-right.fusedForces[1])<.001);
});
