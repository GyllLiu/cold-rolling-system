#!/usr/bin/env node

import {
  createRoutePlan,
  evaluateFixedThicknessRoute,
  type PassPlan,
  type RouteInput,
} from "../app/lib/route-planner";

type Scenario = {
  label: string;
  input: RouteInput;
  factoryReductions: Array<{ f1: number; f2: number }>;
};

const scenarios: Scenario[] = [
  {
    label: "6.5×1560→2.35×1500",
    input: {
      alloy: "1060",
      incomingState: "annealed",
      entryThickness: 6.5,
      targetThickness: 2.35,
      width: 1560,
      targetState: "h18",
    },
    factoryReductions: [{ f1: 0.247, f2: 0.215 }, { f1: 0.237, f2: 0.199 }],
  },
  {
    label: "5.8×1350→0.77×1210",
    input: {
      alloy: "1060",
      incomingState: "annealed",
      entryThickness: 5.8,
      targetThickness: 0.77,
      width: 1350,
      targetState: "h18",
    },
    factoryReductions: [{ f1: 0.524, f2: 0.456 }, { f1: 0.298, f2: 0.269 }],
  },
];

function gaugesFromReductions(entryThickness: number, reductions: Scenario["factoryReductions"]) {
  let entry = entryThickness;
  return reductions.map(({ f1, f2 }) => {
    const intermediateThickness = entry * (1 - f1);
    const exitThickness = intermediateThickness * (1 - f2);
    entry = exitThickness;
    return { intermediateThickness, exitThickness };
  });
}

function summarize(passes: PassPlan[]) {
  const forceValues = passes.flatMap((pass) => [
    pass.f1Force?.safetyTotal,
    pass.f2Force?.safetyTotal,
  ]).filter((value): value is number => value != null);
  return {
    route: passes.map((pass) => ({
      pass: pass.index,
      entry: pass.entryThickness,
      middle: pass.intermediateThickness,
      exit: pass.exitThickness,
      f1ReductionPercent: pass.f1ReductionPercent,
      f2ReductionPercent: pass.f2ReductionPercent,
      f1DisplayedForceT: pass.f1Force?.total,
      f2DisplayedForceT: pass.f2Force?.total,
      f1SafetyForceT: pass.f1Force?.safetyTotal,
      f2SafetyForceT: pass.f2Force?.safetyTotal,
    })),
    maximumSafetyForceT: Math.max(...forceValues),
    maximumEquipmentUtilizationPercent: Math.max(...forceValues) / 10,
  };
}

const results = scenarios.map((scenario) => {
  const current = createRoutePlan(scenario.input);
  const factoryGauges = gaugesFromReductions(scenario.input.entryThickness, scenario.factoryReductions);
  // Round only the final gauge to the stated product target; the supplied
  // reduction percentages are rounded and otherwise close to the requested gauge.
  factoryGauges.at(-1)!.exitThickness = scenario.input.targetThickness;
  const factory = evaluateFixedThicknessRoute(scenario.input, factoryGauges);
  return {
    scenario: scenario.label,
    current: summarize(current.passes),
    factory: summarize(factory),
  };
});

console.log(JSON.stringify(results, null, 2));
