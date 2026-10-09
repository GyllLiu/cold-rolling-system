import policy from "../../backend/app/model_artifacts/factory_route_policy_v072.json";
import material from "../../backend/app/model_artifacts/material_state_v061.json";
import physics from "../../backend/app/model_artifacts/equipment_physics_v071.json";
import { swiftMeanStress } from "./rolling-physics";
import type { RouteInput } from "./route-planner";

export const FACTORY_ROUTE_POLICY = policy;
export type GaugePass = { entry: number; middle: number; exit: number };
export type ReductionPass = {
  f1ReductionPercent: number; f2ReductionPercent: number; totalReductionPercent: number;
};
const clamp = (value: number, low: number, high: number) => Math.max(low, Math.min(high, value));
const pointTarget = policy.confirmed.hard_pass_drop_points;
const tolerance = policy.engineering.hard_pass_drop_tolerance_points;

export function thicknessRegion(thickness: number) {
  const [low, high] = policy.confirmed.equipment_thickness_mm;
  const step = (high - low) / 3;
  return { name: thickness <= low + step ? "薄段" : thickness <= low + 2 * step ? "中段" : "厚段",
    boundariesMm: [low, low + step, low + 2 * step, high] };
}

export function finishingPreference(thickness: number) {
  const [low, high] = policy.confirmed.equipment_thickness_mm;
  const progress = clamp((thickness - low) / (high - low) * 3, 0, 3);
  const left = Math.min(2, Math.floor(progress));
  const interpolate = (points: number[]) => points[left] + (points[left + 1] - points[left]) * (progress - left);
  const f1 = interpolate(policy.engineering.finishing_f1_targets_at_thirds);
  const f2 = interpolate(policy.engineering.finishing_f2_targets_at_thirds);
  const half = policy.engineering.finishing_preference_half_width;
  return { targetF1: f1, targetF2: f2,
    preferredF1: [f1 - half, f1 + half], preferredF2: [f2 - half, f2 + half] };
}

function prestrain(state: RouteInput["incomingState"]) {
  const strains = physics.material_hardening.temper_equivalent_prestrain as Record<string, number>;
  return strains[state] ?? strains.unannealed;
}

function strength(alloy: string, strain: number) {
  const parameters = (physics.material_hardening.parameters as Record<string,
    { K_mpa: number; epsilon_0: number; n: number }>)[alloy];
  return swiftMeanStress(parameters, strain, strain);
}

/** This adjusts a search ceiling, not a measured ductility/physical failure law.
 * O is soft only at the first F1; subsequent stages use the work-hardened
 * process rule. Actual constitutive stress still accumulates strain, without
 * relabelling every F1 output as certified H18.
 */
export function materialReductionLimit(input: RouteInput, stageEntry: number, soft: boolean) {
  const strain = prestrain(input.incomingState) + Math.max(0, Math.log(input.entryThickness / stageEntry));
  const actual = strength(input.alloy, strain);
  const reference = strength("1060", strain);
  let alloyFactor = Math.min(1, Math.sqrt(reference / actual));
  if (input.alloy === "1100") {
    const [low, high] = policy.confirmed["1100_vs_1060_relative_reduction"];
    alloyFactor = clamp(alloyFactor, 1 - high, 1 - low);
  }
  const curves = material.alloys as Record<string, { yield_strength_curve_mpa: number[][] }>;
  const knots = curves[input.alloy].yield_strength_curve_mpa;
  const progress = clamp((actual - knots[0][1]) / (knots.at(-1)![1] - knots[0][1]), 0, 1);
  const [low, high] = policy.confirmed.hard_vs_o_relative_reduction;
  const stateFactor = soft ? 1 : 1 - low - (high - low) * progress;
  return { maximum: policy.engineering.reference_1060_o_reduction_ceiling * alloyFactor * stateFactor,
    alloyFactor, stateFactor, stageCondition: soft ? "O态软料" : "加工硬化料" };
}

export function passReductionLimits(input: RouteInput, gauge: GaugePass, index: number) {
  const f1 = materialReductionLimit(input, gauge.entry, input.incomingState === "annealed" && index === 0);
  const f2 = materialReductionLimit(input, gauge.middle, false);
  if (input.alloy === "3003" && index === 0) {
    // Keep the factory absolute cap, and the 10–20% hard-vs-O reduction.
    f1.maximum = Math.min(f1.maximum, policy.confirmed["3003_opening_f1_max"] * f1.stateFactor);
  }
  return { f1, f2 };
}

export function reductions(gauge: GaugePass): ReductionPass {
  return { f1ReductionPercent: 100 * (1 - gauge.middle / gauge.entry),
    f2ReductionPercent: 100 * (1 - gauge.exit / gauge.middle),
    totalReductionPercent: 100 * (1 - gauge.exit / gauge.entry) };
}

export function factoryGeometryReasons(input: RouteInput, gauges: GaugePass[], exactReductions?: ReductionPass[]) {
  const reasons = new Set<string>();
  const passes = exactReductions ?? gauges.map(reductions);
  const epsilon = 1e-7;
  for (const [index, gauge] of gauges.entries()) {
    if (!(gauge.entry > gauge.middle && gauge.middle > gauge.exit && gauge.exit > 0)) {
      reasons.add("non_decreasing_gauge"); continue;
    }
    const pass = passes[index];
    const limits = passReductionLimits(input, gauge, index);
    if (pass.f1ReductionPercent > limits.f1.maximum * 100 + epsilon) reasons.add("material_f1_reduction_limit");
    if (pass.f2ReductionPercent > limits.f2.maximum * 100 + epsilon) reasons.add("material_f2_reduction_limit");
    if (input.alloy === "3003" && index === 0 && pass.f1ReductionPercent > 45 + epsilon) reasons.add("3003_opening_f1_limit");
    if (pass.f1ReductionPercent <= pass.f2ReductionPercent) reasons.add("f1_f2_share");
    if (gauges.length === 1 && Math.abs(pass.f1ReductionPercent - pass.f2ReductionPercent -
      policy.confirmed.single_pass_f1_minus_f2_points) > epsilon) reasons.add("single_pass_f1_f2_difference");
    if (index > 0) {
      const previous = passes[index - 1];
      for (const key of ["f1ReductionPercent", "f2ReductionPercent"] as const) {
        const drop = previous[key] - pass[key];
        // First F1 is the only soft-to-hard transition; all F2 stages are hard.
        const softTransition = key === "f1ReductionPercent" && index === 1 && input.incomingState === "annealed";
        if (drop <= 0 || (!softTransition && Math.abs(drop - pointTarget) > tolerance + epsilon)) {
          reasons.add("hard_pass_drop_outside_trial_band");
        }
      }
    }
  }
  return [...reasons];
}

export function routeShapeScore(input: RouteInput, passes: ReductionPass[]) {
  if (passes.length === 1) return 0;
  let score = 0;
  for (let index = 1; index < passes.length; index++) {
    for (const key of ["f1ReductionPercent", "f2ReductionPercent"] as const) {
      if (key === "f1ReductionPercent" && index === 1 && input.incomingState === "annealed") continue;
      score += Math.abs(passes[index - 1][key] - passes[index][key] - pointTarget);
    }
  }
  return score / (2 * (passes.length - 1));
}

/** Solve exact final gauge for each taper/stand split. No fixed finishing
 * percentage consumes an entire pass; the finish is a ranked preference.
 */
export function generateFactoryGauges(input: RouteInput, count: number): GaugePass[][] {
  const desiredStrain = Math.log(input.entryThickness / input.targetThickness);
  if (count === 1) {
    const gap = policy.confirmed.single_pass_f1_minus_f2_points / 100;
    const ratio = input.targetThickness / input.entryThickness;
    const remaining = (Math.sqrt(gap * gap + 4 * ratio) - gap) / 2;
    const gauges = [{ entry: input.entryThickness, middle: input.entryThickness * remaining, exit: input.targetThickness }];
    return factoryGeometryReasons(input, gauges).length ? [] : [gauges];
  }
  const candidates: GaugePass[][] = [];
  const steps = [3, 4, 5, 6, 7].map(points => points / 100);
  const boosts = input.incomingState === "annealed" ? [0, .03, .06, .09] : [0];
  for (const d1 of steps) for (const d2 of steps) for (const gap of [.03, .05, .07, .09]) for (const boost of boosts) {
    const rs = (base: number, index: number) => [base + gap - index * d1 + (index === 0 ? boost : 0), base - index * d2];
    let low = Math.max((count - 1) * d2, (count - 1) * d1 - gap) + 1e-8;
    let high = Math.min(.75 - gap - boost, .75);
    const strain = (base: number) => Array.from({ length: count }, (_, i) => rs(base, i))
      .flat().reduce((sum, r) => sum - Math.log1p(-r), 0);
    if (low >= high || strain(low) > desiredStrain || strain(high) < desiredStrain) continue;
    for (let iteration = 0; iteration < 45; iteration++) {
      const mid = (low + high) / 2;
      if (strain(mid) < desiredStrain) low = mid; else high = mid;
    }
    const gauges: GaugePass[] = [];
    let entry = input.entryThickness;
    for (let index = 0; index < count; index++) {
      const [r1, r2] = rs((low + high) / 2, index);
      const middle = entry * (1 - r1);
      const exit = index === count - 1 ? input.targetThickness : middle * (1 - r2);
      gauges.push({ entry, middle, exit }); entry = exit;
    }
    if (!factoryGeometryReasons(input, gauges).length) candidates.push(gauges);
  }
  const preference = finishingPreference(input.targetThickness);
  const score = (gauges: GaugePass[]) => {
    const passes = gauges.map(reductions);
    const end = passes.at(-1)!;
    return Math.abs(end.f1ReductionPercent - preference.targetF1 * 100) +
      Math.abs(end.f2ReductionPercent - preference.targetF2 * 100) + routeShapeScore(input, passes) * 2;
  };
  return candidates.sort((a, b) => score(a) - score(b)).slice(0, policy.engineering.geometry_candidates_per_count);
}
