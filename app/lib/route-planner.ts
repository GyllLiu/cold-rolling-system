import routeModelJson from "../../backend/app/model_artifacts/route_model_v061.json";
import baselineJson from "../../backend/app/model_artifacts/engineering_baseline_v6.json";
import materialStateJson from "../../backend/app/model_artifacts/material_state_v061.json";
import routeHistoryJson from "../../backend/app/model_artifacts/route_history_tandem_v072.json";
import equipmentPhysicsJson from "../../backend/app/model_artifacts/equipment_physics_v071.json";
import { PhysicsDomainError, smoothWidthFactor, solveRollingForce, swiftMeanStress } from "./rolling-physics";
import { ALLOCATION_FUSION_POLICY, computeAllocationFusion, type AllocationFusionResult, type StandPair, type TensionTriple } from "./allocation-fusion";
import { FACTORY_ROUTE_POLICY, factoryGeometryReasons, finishingPreference, generateFactoryGauges,
  passReductionLimits, routeShapeScore, thicknessRegion, type GaugePass } from "./factory-route-policy";

export type IncomingState =
  "annealed" | "h14" | "h16" | "h18" | "h22" | "h24" | "h26" | "unannealed" | "unknown";
export type TargetState =
  "h14" | "h16" | "h18" | "h22" | "h24" | "h26" | "hard" | "annealed" | "unspecified";
export type RiskLevel = "normal" | "warning" | "outside_history";
export type EquipmentStatus = "within_limits" | "near_limit" | "over_limit" | "not_evaluated";
export type Assessment = {
  dataCoverage: RiskLevel | "unavailable";
  equipment: EquipmentStatus;
  quality: "provisional_rules_met" | "review_required";
  qualityValidated: false;
};

export type CandidateAudit = {
  id: number;
  method: RoutePlan["evidence"]["routeMethod"];
  passCount: number;
  accepted: boolean;
  reasons: string[];
  finishingBand: FinishingBand | null;
  maximumCheckForceT: number | null;
  ranking: number[] | null;
  notSelectedReason?: string;
};

export class RoutePlanningError extends Error {
  constructor(message: string, public candidates: CandidateAudit[]) { super(message); }
}

export type RouteInput = {
  alloy: string;
  incomingState: IncomingState;
  entryThickness: number;
  targetThickness: number;
  width: number;
  targetState: TargetState;
};

type Model = {
  label: string;
  unit: string;
  feature_names: string[];
  feature_mean: number[];
  feature_scale: number[];
  basis_terms: number[][];
  coefficients: number[];
  target_transform: "raw" | "reduction_share";
  hard_min?: number | null;
  hard_max?: number | null;
  prediction_interval?: { p95_half_width: number };
  validation?: { holdout_260805?: { mae: number; p95: number; r2: number } };
  final_test?: { direct?: { mae: number; p95_absolute_error: number; r2: number } };
  model_type?: "weighted_ensemble";
  components?: Array<{
    weight: number;
    postprocess: "raw" | "add_exit_thickness";
    model: Model;
  }>;
};

type Envelope = {
  count: number;
  p01: number;
  p05: number;
  median: number;
  p95: number;
  p99: number;
  max_observed: number;
};

type RouteArtifact = {
  model_version: string;
  case_count: number;
  supported_alloys: string[];
  feature_bounds: Record<string, [number, number]>;
  models: Record<string, Model>;
};

type BaselineArtifact = {
  data: { production_case_count: number; side_force_case_count: number };
  envelopes: Record<string, Envelope>;
  route_policy: {
    target_true_strain_per_tandem_pass: number;
    normal_max_true_strain_per_tandem_pass: number;
    pass_strain_taper?: number;
    manufacturer_stand_limits: Record<
      "1xxx" | "3xxx",
      {
        f1_max_reduction: number;
        f2_max_reduction: number;
        thin_f1_max_reduction: number;
        thin_f2_max_reduction: number;
      }
    >;
    hard_incoming_reduction_margin: number;
    thin_exit_threshold_mm: number;
    f1_true_strain_share_opening: number;
    f1_true_strain_share_finishing: number;
    distribution_bands: Array<{
      total_reduction_band: [number, number];
      f1_true_strain_share_median: number;
    }>;
  };
  tension_order_policy?: {
    status: string;
    comparison_basis: string;
    relation: string;
    strict_compliant_count: number;
    total_case_count: number;
    formula: string;
  };
  confirmed_plant_limits?: {
    uncoiler_tension_t_max: number | null;
    interstand_tension_t_max: number | null;
    coiler_tension_t_max: number | null;
    f1_total_force_t_max: number | null;
    f2_total_force_t_max: number | null;
  };
  annealing_policy: {
    intermediate_review_cumulative_reduction?: Record<string, number>;
    final_o_temper_reference: Record<
      string,
      { temperature_c: number | null; hold_hours: [number, number] | null; source: string }
    >;
  };
  side_force_policy: { f1_operation_share: number; f2_operation_share: number };
};

type MaterialStateArtifact = {
  model_version: string;
  reference: { alloy: string; incoming_state: "annealed" };
  incoming_state: {
    annealed_initial_true_strain: number;
    unannealed_initial_true_strain: number;
    initial_true_strain_by_temper?: Partial<Record<IncomingState, number>>;
  };
  force_correction: {
    minimum_multiplier: number;
    maximum_multiplier: number;
  };
  alloys: Record<string, { yield_strength_curve_mpa: Array<[number, number]> }>;
};

type HistoricalRouteRecord = {
  entry: number;
  middle: number;
  exit: number;
  width: number;
  stable_seconds: number;
  date: string;
  dataset: string;
  f1_force_t: number;
  f2_force_t: number;
};

type RouteHistoryArtifact = {
  artifact_version: string;
  case_count: number;
  source_dates: string[];
  records: HistoricalRouteRecord[];
};

type FlowStressBand = {
  entry_thickness_low_mm: number;
  entry_thickness_high_mm: number;
  count: number;
  effective_flow_stress_p05_mpa: number;
  effective_flow_stress_median_mpa: number;
  effective_flow_stress_p95_mpa: number;
};

type EquipmentPhysicsArtifact = {
  schema_version: string;
  equipment: {
    work_roll_diameter_mm: number;
    work_roll_radius_mm: number;
    support_roll_diameter_mm: number;
    roll_body_length_mm: number;
    maximum_total_rolling_force_t_per_stand: number;
    normal_speed_range_m_per_min: [number, number];
    roll_young_modulus_mpa: number;
    roll_poisson_ratio: number;
  };
  normal_lubrication: {
    friction_coefficient: number;
    eligible_golden_plateaus: number;
    stand_samples: number;
  };
  material_hardening: {
    family: "Swift";
    parameters: Record<string, { K_mpa: number; epsilon_0: number; n: number }>;
    temper_equivalent_prestrain: Partial<Record<IncomingState, number>>;
  };
  model: {
    bands: Record<"f1" | "f2", FlowStressBand[]>;
    back_tension_weight: number;
    front_tension_weight: number;
    width_correction: {
      bands: Record<"f1" | "f2", Array<{
        width_low_mm: number;
        width_high_mm: number;
        count: number;
        factor: number;
      }>>;
    };
    iteration: { maximum_iterations: number; radius_tolerance_mm: number };
  };
  validation: {
    mae_t: number;
    p95_absolute_error_t: number;
    bias_t: number;
  };
};

const routeArtifact = routeModelJson as unknown as RouteArtifact;
const baseline = baselineJson as unknown as BaselineArtifact;
const materialState = materialStateJson as unknown as MaterialStateArtifact;
const routeHistory = routeHistoryJson as unknown as RouteHistoryArtifact;
const equipmentPhysics = equipmentPhysicsJson as unknown as EquipmentPhysicsArtifact;

const outputCodes = [
  "intermediate_thickness_set_mode",
  "uncoiler_tension_set_median",
  "interstand_tension_set_median",
  "coiler_tension_set_median",
  "tm1_closed_gap_set_median",
  "tm2_closed_gap_set_median",
  "f1_roll_force_sum_median",
  "f2_roll_force_sum_median",
] as const;

export type ParameterPrediction = {
  code: string;
  label: string;
  value: number;
  unit: string;
  p95Error: number;
  risk: RiskLevel;
  unitTension?: number;
};

export type ForcePrediction = {
  source: "dat_recommendation_with_physical_guard";
  operation: number;
  drive: number;
  total: number;
  operationRisk: RiskLevel;
  driveRisk: RiskLevel;
  totalRisk: RiskLevel;
  p95Error: number;
  materialFactor: number;
  constitutiveMeanFlowStressMpa: number | null;
  calibratedMeanFlowStressMpa: number;
  baselineTotal: number;
  dataReferenceTotal: number;
  physicalMinusDataT: number;
  physicsBaseline: number;
  physicsP95: number;
  safetyTotal: number;
  equipmentUtilizationPercent: number;
  contactLengthMm: number;
  flattenedRadiusMm: number;
  qFactor: number;
  widthCorrectionFactor: number;
  backTensionStressMpa: number;
  frontTensionStressMpa: number;
  lowTensionCheckTotal: number;
  uncertaintyBasis: "empirical_screen_not_joint_95_interval";
  convergence: { nominalIterations: number; upperIterations: number; radiusResidualMm: number };
};

export type PassPlan = {
  index: number;
  entryThickness: number;
  intermediateThickness: number;
  exitThickness: number;
  totalReductionPercent: number;
  f1ReductionPercent: number;
  f2ReductionPercent: number;
  f1MinusF2PercentPoints: number;
  f1TrueStrainShare: number;
  parameterStatus: "available" | "route_only";
  parameters: ParameterPrediction[];
  f1Force: ForcePrediction | null;
  f2Force: ForcePrediction | null;
  risk: RiskLevel;
  assessment: Assessment;
  materialPolicy?: ReturnType<typeof passReductionLimits>;
};

export type FinishingBand = "preferred" | "light" | "review" | "single_pass";

export type AnnealDecision = {
  intermediate: "not_triggered" | "review_required" | "excluded";
  final: "not_required" | "industry_reference" | "plant_recipe_required" | "partial_anneal_recipe_required";
  temperatureC: number | null;
  holdHours: [number, number] | null;
  reviewAfterPass: number | null;
  reason: string;
};

export type RoutePlan = {
  id: string;
  createdAt: string;
  modelVersion: string;
  plannerVersion: string;
  input: RouteInput;
  totalReductionPercent: number;
  totalTrueStrain: number;
  passCount: number;
  minimumFeasiblePassCount: number;
  minimumPassCountScope: "generated_candidates_only";
  finishingBand: FinishingBand;
  parameterRecommendationAvailable: boolean;
  parameterUnavailableReason: string | null;
  passes: PassPlan[];
  risk: RiskLevel;
  assessment: Assessment;
  alternatives: RouteAlternative[];
  ruleSummary: {
    version: string;
    thicknessRegion: ReturnType<typeof thicknessRegion>;
    hardPassDropPoints: number;
    trialDropTolerancePoints: number;
    intermediateAnnealing: "excluded";
    parameterAssumptions: string[];
  };
  selection: {
    policy: "finishing_then_count_then_reduction_shape";
    selectedCandidateId: number;
    extraPassesForFinishing: number;
    explanation: string;
    rankingOrder: string[];
    candidates: CandidateAudit[];
  };
  anneal: AnnealDecision;
  evidence: {
    productionCases: number;
    sideForceCases: number;
    envelopeType: "high_speed_history";
    routeMethod: "dat_reverse_chain" | "dat_interpolated_chain" | "quality_constrained_fallback";
    routeHistoryCases: number;
    routeSupportDates: number[];
    routeHistoryReferences: Array<{
      entry: number;
      middle: number;
      exit: number;
      width: number;
      date: string;
      dataset: string;
    }>;
    equipment: {
      workRollDiameterMm: number;
      supportRollDiameterMm: number;
      rollBodyLengthMm: number;
      maximumForceT: number;
      normalSpeedRangeMPerMin: [number, number];
      frictionCoefficient: number;
      goldenPlateaus: number;
      physicsModelVersion: string;
      physicsMethod: "Swift-Hill-Bland-Ford-Hitchcock";
      physicsMaeT: number;
      physicsP95T: number;
    };
  };
};

export type RouteAlternative = {
  candidateId: number;
  kind: "fewest_passes";
  label: string;
  passCount: number;
  passes: PassPlan[];
  finishingBand: FinishingBand;
  risk: RiskLevel;
  assessment: Assessment;
  explanation: string;
};

function round(value: number, digits = 4) {
  return Number(value.toFixed(digits));
}

function featureValues(entry: number, exit: number, width: number): Record<string, number> {
  return {
    entry_thickness_set_mode: entry,
    exit_thickness_set_mode: exit,
    width_set_mode: width,
    total_reduction_ratio: 1 - exit / entry,
    log_thickness_ratio: Math.log(entry / exit),
    entry_cross_section_proxy: entry * width,
    exit_cross_section_proxy: exit * width,
  };
}

function predictSingle(model: Model, features: Record<string, number>) {
  const standardized = model.feature_names.map(
    (name, index) => (features[name] - model.feature_mean[index]) / model.feature_scale[index],
  );
  const row = [
    1,
    ...model.basis_terms.map((term) =>
      term.reduce((product, featureIndex) => product * standardized[featureIndex], 1),
    ),
  ];
  const encoded = row.reduce(
    (sum, value, index) => sum + value * model.coefficients[index],
    0,
  );
  return model.target_transform === "reduction_share"
    ? features.entry_thickness_set_mode - encoded *
      (features.entry_thickness_set_mode - features.exit_thickness_set_mode)
    : encoded;
}

function predict(model: Model, features: Record<string, number>, target: string) {
  let value = model.model_type === "weighted_ensemble" && model.components
    ? model.components.reduce((sum, component) => {
      let componentValue = predictSingle(component.model, features);
      if (component.postprocess === "add_exit_thickness") {
        componentValue += features.exit_thickness_set_mode;
      }
      return sum + component.weight * componentValue;
    }, 0)
    : predictSingle(model, features);
  if (target === "intermediate_thickness_set_mode") {
    value = Math.max(
      features.exit_thickness_set_mode + 0.001,
      Math.min(features.entry_thickness_set_mode - 0.001, value),
    );
  }
  if (model.hard_min != null) value = Math.max(model.hard_min, value);
  if (model.hard_max != null) value = Math.min(model.hard_max, value);
  if (!Number.isFinite(value)) throw new Error("工艺模型计算失败");
  return value;
}

function meanFlowStress(alloy: string, start: number, end: number) {
  const parameters = equipmentPhysics.material_hardening.parameters[alloy];
  return parameters == null ? null : swiftMeanStress(parameters, start, end);
}

function materialForceFactor(
  alloy: string,
  state: IncomingState,
  routeStrainStart: number,
  routeStrainEnd: number,
) {
  if (state === "unknown") return 1;
  const initial = equipmentPhysics.material_hardening.temper_equivalent_prestrain[state] ??
    materialState.incoming_state.initial_true_strain_by_temper?.[state] ??
    materialState.incoming_state.unannealed_initial_true_strain;
  const actual = meanFlowStress(alloy, initial + routeStrainStart, initial + routeStrainEnd);
  const reference = meanFlowStress(
    materialState.reference.alloy,
    routeStrainStart,
    routeStrainEnd,
  );
  if (actual == null || reference == null || reference <= 0) return 1;
  return Math.min(
    materialState.force_correction.maximum_multiplier,
    Math.max(materialState.force_correction.minimum_multiplier, actual / reference),
  );
}

function materialMeanFlowStress(
  alloy: string,
  state: IncomingState,
  routeStrainStart: number,
  routeStrainEnd: number,
) {
  if (state === "unknown") return null;
  const initial = equipmentPhysics.material_hardening.temper_equivalent_prestrain[state] ??
    materialState.incoming_state.initial_true_strain_by_temper?.[state] ??
    materialState.incoming_state.unannealed_initial_true_strain;
  return meanFlowStress(alloy, initial + routeStrainStart, initial + routeStrainEnd);
}

function effectiveFlowStress(
  stand: "f1" | "f2",
  entryThickness: number,
  percentileName: "median" | "p95",
) {
  const bands = equipmentPhysics.model.bands[stand];
  const band = bands.find((candidate) =>
    entryThickness > candidate.entry_thickness_low_mm &&
    entryThickness <= candidate.entry_thickness_high_mm
  ) ?? bands.at(-1)!;
  return percentileName === "median"
    ? band.effective_flow_stress_median_mpa
    : band.effective_flow_stress_p95_mpa;
}

function physicsRollingForce(
  stand: "f1" | "f2", entry: number, exit: number, width: number,
  backTension: number, frontTension: number, materialFactor: number,
  percentileName: "median" | "p95",
) {
  const equipment = equipmentPhysics.equipment;
  return solveRollingForce({
    entry, exit, width, backTension, frontTension,
    flowStressMpa: effectiveFlowStress(stand, entry, percentileName) * materialFactor,
    mu: equipmentPhysics.normal_lubrication.friction_coefficient,
    radiusMm: equipment.work_roll_radius_mm,
    youngModulusMpa: equipment.roll_young_modulus_mpa,
    poissonRatio: equipment.roll_poisson_ratio,
    widthFactor: smoothWidthFactor(equipmentPhysics.model.width_correction.bands[stand], width),
    backWeight: equipmentPhysics.model.back_tension_weight,
    frontWeight: equipmentPhysics.model.front_tension_weight,
    maximumIterations: equipmentPhysics.model.iteration.maximum_iterations,
    radiusToleranceMm: equipmentPhysics.model.iteration.radius_tolerance_mm,
  });
}

function equipmentStatus(forceT: number): EquipmentStatus {
  if (!Number.isFinite(forceT)) return "not_evaluated";
  const maximum = equipmentPhysics.equipment.maximum_total_rolling_force_t_per_stand;
  return forceT > maximum ? "over_limit" : forceT > maximum * 0.8 ? "near_limit" : "within_limits";
}

function equipmentForceRisk(forceT: number): RiskLevel {
  const utilization = forceT / equipmentPhysics.equipment.maximum_total_rolling_force_t_per_stand;
  if (utilization > 1) return "outside_history";
  if (utilization > 0.80) return "warning";
  return "normal";
}

function p95Error(model: Model) {
  return model.validation?.holdout_260805?.p95 ??
    model.final_test?.direct?.p95_absolute_error ??
    model.prediction_interval?.p95_half_width ?? 0;
}

function upperRisk(value: number, envelope: Envelope): RiskLevel {
  if (value > envelope.p99) return "outside_history";
  if (value > envelope.p95) return "warning";
  return "normal";
}

function bandRisk(value: number, envelope: Envelope): RiskLevel {
  if (value < envelope.p01 || value > envelope.p99) return "outside_history";
  if (value < envelope.p05 || value > envelope.p95) return "warning";
  return "normal";
}

function combineRisk(values: RiskLevel[]): RiskLevel {
  if (values.includes("outside_history")) return "outside_history";
  if (values.includes("warning")) return "warning";
  return "normal";
}

function manufacturerF1TrueStrainShare(index: number, count: number) {
  const opening = baseline.route_policy.f1_true_strain_share_opening;
  const finishing = baseline.route_policy.f1_true_strain_share_finishing;
  if (count <= 1) return (opening + finishing) / 2;
  return opening + (finishing - opening) * (index - 1) / (count - 1);
}

type SupportedHistoryRecord = HistoricalRouteRecord & {
  passStrain: number;
  f1StrainShare: number;
  supportCount: number;
  supportDates: number;
};

type ReverseHistoryPath = {
  reversedRecords: SupportedHistoryRecord[];
  predictedEntry: number;
  score: number;
};

function logDistance(left: number, right: number) {
  return Math.abs(Math.log(left / right));
}

const tandemHistoryRecords = routeHistory.records.filter(record =>
  Number.isFinite(record.f1_force_t) && record.f1_force_t > 0 &&
  Number.isFinite(record.f2_force_t) && record.f2_force_t > 0 && record.entry > record.middle && record.middle > record.exit && record.exit > 0);
const supportedHistoryRecords: SupportedHistoryRecord[] = tandemHistoryRecords.map((record) => {
  const passStrain = Math.log(record.entry / record.exit);
  const f1StrainShare = Math.log(record.entry / record.middle) / passStrain;
  const neighbours = tandemHistoryRecords.filter((candidate) =>
    logDistance(candidate.exit, record.exit) <= 0.08 &&
    logDistance(candidate.entry / candidate.exit, record.entry / record.exit) <= 0.12 &&
    Math.abs(candidate.width - record.width) <= 200,
  );
  return {
    ...record,
    passStrain,
    f1StrainShare,
    supportCount: neighbours.length,
    supportDates: new Set(neighbours.map((candidate) => candidate.date)).size,
  };
});

function reverseHistoryPaths(input: RouteInput, maximumLinkDistance = 0.08) {
  type BeamState = {
    currentExit: number;
    reversedRecords: SupportedHistoryRecord[];
    score: number;
  };
  let beam: BeamState[] = [{
    currentExit: input.targetThickness,
    reversedRecords: [],
    score: 0,
  }];
  const completed: ReverseHistoryPath[] = [];
  const widthWindow = Math.max(180, input.width * 0.15);

  for (let depth = 1; depth <= 6; depth += 1) {
    const next: BeamState[] = [];
    for (const state of beam) {
      const local = supportedHistoryRecords
        .filter((record) =>
          Math.abs(record.width - input.width) <= widthWindow &&
          logDistance(record.exit, state.currentExit) <= maximumLinkDistance &&
          record.f1StrainShare >= 0.48,
        )
        .map((record) => {
          const proposedEntry = state.currentExit * record.entry / record.exit;
          const exitCost = logDistance(record.exit, state.currentExit) * 8;
          const widthCost = Math.abs(record.width - input.width) / widthWindow;
          const supportCost = 0.8 / Math.sqrt(Math.max(1, record.supportDates));
          const overshootCost = proposedEntry > input.entryThickness * 1.2
            ? logDistance(proposedEntry, input.entryThickness * 1.2) * 12
            : 0;
          return {
            currentExit: proposedEntry,
            reversedRecords: [...state.reversedRecords, record],
            score: state.score + exitCost + widthCost + supportCost + overshootCost,
          };
        })
        .sort((left, right) => left.score - right.score);
      next.push(...local);
    }

    const deduplicated = new Map<string, BeamState>();
    next.forEach((state) => {
      const last = state.reversedRecords.at(-1)!;
      const key = [
        Math.round(Math.log(state.currentExit) / 0.035),
        Math.round(last.passStrain / 0.04),
        Math.round(last.f1StrainShare / 0.04),
      ].join(":");
      const existing = deduplicated.get(key);
      if (existing == null || state.score < existing.score) deduplicated.set(key, state);
    });
    beam = [...deduplicated.values()]
      .sort((left, right) => left.score - right.score)
      .slice(0, 256);

    beam.forEach((state) => {
      const startDistance = logDistance(state.currentExit, input.entryThickness);
      if (startDistance <= maximumLinkDistance) {
        completed.push({
          reversedRecords: state.reversedRecords,
          predictedEntry: state.currentExit,
          score: state.score + startDistance * 18 + depth * 0.08,
        });
      }
    });
  }

  // Keep strong candidates at every depth. Route count is decided only after
  // opening-mark and finishing-shape constraints have been checked; otherwise
  // a short but over-heavy finishing pass crowds safer routes out of the beam.
  return Array.from({ length: 6 }, (_, index) => index + 1).flatMap((depth) =>
    completed
      .filter((candidate) => candidate.reversedRecords.length === depth)
      .sort((left, right) => left.score - right.score)
      .slice(0, 80),
  );
}

function tonnesToUnitTension(tonnes: number, width: number, thickness: number) {
  return 9.80665 * tonnes / (width * thickness);
}

function unitTensionToTonnes(unitTension: number, width: number, thickness: number) {
  return unitTension * width * thickness / 9.80665;
}

function isotonicNonDecreasing(raw: number[]) {
  const blocks = raw.map((value, index) => ({
    start: index,
    end: index,
    count: 1,
    mean: value,
  }));
  for (let index = 0; index < blocks.length - 1;) {
    if (blocks[index].mean <= blocks[index + 1].mean) {
      index += 1;
      continue;
    }
    const left = blocks[index];
    const right = blocks[index + 1];
    const count = left.count + right.count;
    blocks.splice(index, 2, {
      start: left.start,
      end: right.end,
      count,
      mean: (left.mean * left.count + right.mean * right.count) / count,
    });
    if (index > 0) index -= 1;
  }
  const ordered = new Array<number>(raw.length);
  blocks.forEach((block) => {
    for (let index = block.start; index <= block.end; index += 1) {
      ordered[index] = block.mean;
    }
  });
  return ordered;
}

function orderedTensionValues(
  values: Record<(typeof outputCodes)[number], number>,
  width: number,
  entry: number,
  middle: number,
  exit: number,
) {
  const localThickness = [entry, middle, exit];
  const rawTonnes = [
    values.uncoiler_tension_set_median,
    values.interstand_tension_set_median,
    values.coiler_tension_set_median,
  ];
  const rawUnit = rawTonnes.map((value, index) =>
    tonnesToUnitTension(value, width, localThickness[index]));
  const orderedUnit = baseline.tension_order_policy
    ? isotonicNonDecreasing(rawUnit)
    : rawUnit;
  const limits = baseline.confirmed_plant_limits;
  const totalLimits = [
    limits?.uncoiler_tension_t_max,
    limits?.interstand_tension_t_max,
    limits?.coiler_tension_t_max,
  ];
  const unitLimits = totalLimits.map((limit, index) => limit == null
    ? Number.POSITIVE_INFINITY
    : tonnesToUnitTension(limit, width, localThickness[index]));
  // Propagate upper bounds backwards so a tighter downstream limit cannot
  // be exceeded when maintaining unit-tension order.
  for (let index = orderedUnit.length - 1; index >= 0; index -= 1) {
    orderedUnit[index] = Math.max(0, Math.min(
      orderedUnit[index], unitLimits[index], orderedUnit[index + 1] ?? Infinity,
    ));
  }
  const orderedTonnes = orderedUnit.map((value, index) =>
    unitTensionToTonnes(value, width, localThickness[index]));
  return {
    values: {
      ...values,
      uncoiler_tension_set_median: orderedTonnes[0],
      interstand_tension_set_median: orderedTonnes[1],
      coiler_tension_set_median: orderedTonnes[2],
    },
    unitTension: {
      uncoiler_tension_set_median: orderedUnit[0],
      interstand_tension_set_median: orderedUnit[1],
      coiler_tension_set_median: orderedUnit[2],
    },
  };
}

function modelCoverage(entry: number, exit: number, width: number) {
  const values = featureValues(entry, exit, width);
  return Object.entries(routeArtifact.feature_bounds).every(([name, [low, high]]) => {
    const value = values[name];
    return Number.isFinite(value) && value >= low && value <= high;
  });
}

function buildPass(
  index: number,
  count: number,
  entry: number,
  exit: number,
  width: number,
  input: RouteInput,
  routeStrainBefore: number,
  historicalMiddle?: number,
): PassPlan {
  const baseFeatures = featureValues(entry, exit, width);
  const totalStrain = Math.log(entry / exit);
  const f1TrueStrainShare = historicalMiddle == null
    ? manufacturerF1TrueStrainShare(index, count)
    : Math.log(entry / historicalMiddle) / totalStrain;
  // The plant route rule is expressed in deformation, not fixed gauges: F1
  // carries the main deformation and its share rises towards the finishing
  // pass so F2 can trim the thin strip.  This naturally reproduces routes such
  // as 7 -> about 3.7 -> about 2.05 -> about 1.1 -> 0.7 without hard-coding
  // any of those thicknesses.
  const middle = historicalMiddle ?? entry * Math.exp(-totalStrain * f1TrueStrainShare);
  const features = {
    ...baseFeatures,
    intermediate_thickness_set_mode: middle,
    contact_proxy_f2: width * Math.sqrt(Math.max(190 * (middle - exit), 0)),
  };
  const rawPredicted = Object.fromEntries(outputCodes.map((code) => {
    if (code === "intermediate_thickness_set_mode") return [code, middle];
    const model = routeArtifact.models[code];
    return [code, predict(model, features, code)];
  })) as Record<(typeof outputCodes)[number], number>;
  const tensionProjection = orderedTensionValues(
    rawPredicted,
    width,
    entry,
    middle,
    exit,
  );
  const predicted = tensionProjection.values;

  const f1Reduction = 1 - middle / entry;
  const f2Reduction = 1 - exit / middle;
  const reductions = [
    upperRisk(f1Reduction, baseline.envelopes.f1_reduction_ratio),
    upperRisk(f2Reduction, baseline.envelopes.f2_reduction_ratio),
  ];

  const parameterDefinitions = [
    ["intermediate_thickness_set_mode", "中间厚度", "mm", "none", "none"],
    ["uncoiler_tension_set_median", "开卷张力", "T", "uncoiler_tension_t", "uncoiler_unit_tension_kn_mm2"],
    ["interstand_tension_set_median", "机架张力", "T", "interstand_tension_t", "interstand_unit_tension_kn_mm2"],
    ["coiler_tension_set_median", "卷取张力", "T", "coiler_tension_t", "coiler_unit_tension_kn_mm2"],
    ["tm1_closed_gap_set_median", "F1辊缝", "mm", "f1_high_speed_gap_mm", "none"],
    ["tm2_closed_gap_set_median", "F2辊缝", "mm", "f2_high_speed_gap_mm", "none"],
  ] as const;
  const parameters = parameterDefinitions.map(([code, label, unit, envelopeCode, unitEnvelopeCode]) => {
    const unitTension = code in tensionProjection.unitTension
      ? tensionProjection.unitTension[code as keyof typeof tensionProjection.unitTension]
      : undefined;
    const risks: RiskLevel[] = envelopeCode === "none"
      ? ["normal"]
      : [bandRisk(predicted[code], baseline.envelopes[envelopeCode])];
    if (unitEnvelopeCode !== "none" && unitTension != null) {
      risks.push(bandRisk(unitTension, baseline.envelopes[unitEnvelopeCode]));
    }
    return {
      code,
      label,
      value: round(predicted[code]),
      unit,
      p95Error: round(p95Error(routeArtifact.models[code])),
      risk: combineRisk(risks),
      unitTension: unitTension == null ? undefined : round(unitTension, 5),
    };
  });

  const f1Strain = Math.log(entry / middle);
  const f1MaterialFactor = materialForceFactor(
    input.alloy,
    input.incomingState,
    routeStrainBefore,
    routeStrainBefore + f1Strain,
  );
  const f2MaterialFactor = materialForceFactor(
    input.alloy,
    input.incomingState,
    routeStrainBefore + f1Strain,
    routeStrainBefore + totalStrain,
  );
  const f1DataReferenceTotal = predicted.f1_roll_force_sum_median * f1MaterialFactor;
  const f2DataReferenceTotal = predicted.f2_roll_force_sum_median * f2MaterialFactor;
  const f1Physics = physicsRollingForce(
    "f1", entry, middle, width,
    predicted.uncoiler_tension_set_median,
    predicted.interstand_tension_set_median,
    f1MaterialFactor,
    "median",
  );
  const f2Physics = physicsRollingForce(
    "f2", middle, exit, width,
    predicted.interstand_tension_set_median,
    predicted.coiler_tension_set_median,
    f2MaterialFactor,
    "median",
  );
  // The DAT force remains the displayed recommendation. Lower-than-recommended
  // tensions increase force: screen that scenario with the upper effective
  // stress band. These marginal empirical errors are not a joint 95% bound.
  const lowTension = (code: (typeof outputCodes)[number]) =>
    Math.max(0, predicted[code] - p95Error(routeArtifact.models[code]));
  const f1PhysicsP95 = physicsRollingForce(
    "f1", entry, middle, width,
    predicted.uncoiler_tension_set_median,
    predicted.interstand_tension_set_median,
    Math.max(1, f1MaterialFactor),
    "p95",
  );
  const f2PhysicsP95 = physicsRollingForce(
    "f2", middle, exit, width,
    predicted.interstand_tension_set_median,
    predicted.coiler_tension_set_median,
    Math.max(1, f2MaterialFactor),
    "p95",
  );
  const f1LowTension = physicsRollingForce(
    "f1", entry, middle, width,
    lowTension("uncoiler_tension_set_median"),
    lowTension("interstand_tension_set_median"),
    Math.max(1, f1MaterialFactor),
    "p95",
  );
  const f2LowTension = physicsRollingForce(
    "f2", middle, exit, width,
    lowTension("interstand_tension_set_median"),
    lowTension("coiler_tension_set_median"),
    Math.max(1, f2MaterialFactor),
    "p95",
  );
  const f1ConstitutiveMeanFlowStress = materialMeanFlowStress(
    input.alloy,
    input.incomingState,
    routeStrainBefore,
    routeStrainBefore + f1Strain,
  );
  const f2ConstitutiveMeanFlowStress = materialMeanFlowStress(
    input.alloy,
    input.incomingState,
    routeStrainBefore + f1Strain,
    routeStrainBefore + totalStrain,
  );
  const f1EquipmentSafetyTotal = Math.max(
    f1Physics.forceT + equipmentPhysics.validation.p95_absolute_error_t,
    f1PhysicsP95.forceT,
    f1LowTension.forceT,
    f1DataReferenceTotal + Math.max(1, f1MaterialFactor) * p95Error(routeArtifact.models.f1_roll_force_sum_median),
  );
  const f2EquipmentSafetyTotal = Math.max(
    f2Physics.forceT + equipmentPhysics.validation.p95_absolute_error_t,
    f2PhysicsP95.forceT,
    f2LowTension.forceT,
    f2DataReferenceTotal + Math.max(1, f2MaterialFactor) * p95Error(routeArtifact.models.f2_roll_force_sum_median),
  );
  const maximumForce = equipmentPhysics.equipment.maximum_total_rolling_force_t_per_stand;
  const f1HistoricalRiskLoad = Math.max(f1Physics.forceT, f1DataReferenceTotal);
  const f2HistoricalRiskLoad = Math.max(f2Physics.forceT, f2DataReferenceTotal);
  const f1Operation = f1DataReferenceTotal * baseline.side_force_policy.f1_operation_share;
  const f2Operation = f2DataReferenceTotal * baseline.side_force_policy.f2_operation_share;
  const f1Force: ForcePrediction = {
    source: "dat_recommendation_with_physical_guard",
    operation: round(f1Operation),
    drive: round(round(f1DataReferenceTotal) - round(f1Operation)),
    total: round(f1DataReferenceTotal),
    operationRisk: upperRisk(f1HistoricalRiskLoad * baseline.side_force_policy.f1_operation_share, baseline.envelopes.f1_operation_force_t),
    driveRisk: upperRisk(f1HistoricalRiskLoad * (1 - baseline.side_force_policy.f1_operation_share), baseline.envelopes.f1_drive_force_t),
    totalRisk: combineRisk([
      upperRisk(f1HistoricalRiskLoad, baseline.envelopes.f1_total_force_t),
      equipmentForceRisk(f1EquipmentSafetyTotal),
    ]),
    p95Error: round(p95Error(routeArtifact.models.f1_roll_force_sum_median)),
    materialFactor: round(f1MaterialFactor, 3),
    constitutiveMeanFlowStressMpa: f1ConstitutiveMeanFlowStress == null
      ? null
      : round(f1ConstitutiveMeanFlowStress),
    calibratedMeanFlowStressMpa: round(f1Physics.flowStressMpa),
    baselineTotal: round(predicted.f1_roll_force_sum_median),
    dataReferenceTotal: round(f1DataReferenceTotal),
    physicalMinusDataT: round(f1Physics.forceT - f1DataReferenceTotal),
    physicsBaseline: round(f1Physics.forceT),
    physicsP95: round(f1PhysicsP95.forceT),
    safetyTotal: Math.ceil(f1EquipmentSafetyTotal * 10000) / 10000,
    equipmentUtilizationPercent: round(f1EquipmentSafetyTotal / maximumForce * 100, 2),
    contactLengthMm: round(f1Physics.contactLengthMm),
    flattenedRadiusMm: round(f1Physics.flattenedRadiusMm),
    qFactor: round(f1Physics.qFactor, 5),
    widthCorrectionFactor: round(f1Physics.widthCorrectionFactor, 5),
    backTensionStressMpa: round(f1Physics.backTensionStressMpa),
    frontTensionStressMpa: round(f1Physics.frontTensionStressMpa),
    lowTensionCheckTotal: round(f1LowTension.forceT),
    uncertaintyBasis: "empirical_screen_not_joint_95_interval",
    convergence: {
      nominalIterations: f1Physics.iterations,
      upperIterations: Math.max(f1PhysicsP95.iterations, f1LowTension.iterations),
      radiusResidualMm: Math.max(f1Physics.radiusResidualMm, f1PhysicsP95.radiusResidualMm, f1LowTension.radiusResidualMm),
    },
  };
  const f2Force: ForcePrediction = {
    source: "dat_recommendation_with_physical_guard",
    operation: round(f2Operation),
    drive: round(round(f2DataReferenceTotal) - round(f2Operation)),
    total: round(f2DataReferenceTotal),
    operationRisk: upperRisk(f2HistoricalRiskLoad * baseline.side_force_policy.f2_operation_share, baseline.envelopes.f2_operation_force_t),
    driveRisk: upperRisk(f2HistoricalRiskLoad * (1 - baseline.side_force_policy.f2_operation_share), baseline.envelopes.f2_drive_force_t),
    totalRisk: combineRisk([
      upperRisk(f2HistoricalRiskLoad, baseline.envelopes.f2_total_force_t),
      equipmentForceRisk(f2EquipmentSafetyTotal),
    ]),
    p95Error: round(p95Error(routeArtifact.models.f2_roll_force_sum_median)),
    materialFactor: round(f2MaterialFactor, 3),
    constitutiveMeanFlowStressMpa: f2ConstitutiveMeanFlowStress == null
      ? null
      : round(f2ConstitutiveMeanFlowStress),
    calibratedMeanFlowStressMpa: round(f2Physics.flowStressMpa),
    baselineTotal: round(predicted.f2_roll_force_sum_median),
    dataReferenceTotal: round(f2DataReferenceTotal),
    physicalMinusDataT: round(f2Physics.forceT - f2DataReferenceTotal),
    physicsBaseline: round(f2Physics.forceT),
    physicsP95: round(f2PhysicsP95.forceT),
    safetyTotal: Math.ceil(f2EquipmentSafetyTotal * 10000) / 10000,
    equipmentUtilizationPercent: round(f2EquipmentSafetyTotal / maximumForce * 100, 2),
    contactLengthMm: round(f2Physics.contactLengthMm),
    flattenedRadiusMm: round(f2Physics.flattenedRadiusMm),
    qFactor: round(f2Physics.qFactor, 5),
    widthCorrectionFactor: round(f2Physics.widthCorrectionFactor, 5),
    backTensionStressMpa: round(f2Physics.backTensionStressMpa),
    frontTensionStressMpa: round(f2Physics.frontTensionStressMpa),
    lowTensionCheckTotal: round(f2LowTension.forceT),
    uncertaintyBasis: "empirical_screen_not_joint_95_interval",
    convergence: {
      nominalIterations: f2Physics.iterations,
      upperIterations: Math.max(f2PhysicsP95.iterations, f2LowTension.iterations),
      radiusResidualMm: Math.max(f2Physics.radiusResidualMm, f2PhysicsP95.radiusResidualMm, f2LowTension.radiusResidualMm),
    },
  };
  const risk = combineRisk([
    ...reductions,
    ...parameters.map((parameter) => parameter.risk),
    f1Force.operationRisk, f1Force.driveRisk, f1Force.totalRisk,
    f2Force.operationRisk, f2Force.driveRisk, f2Force.totalRisk,
  ]);
  return {
    index,
    entryThickness: round(entry),
    intermediateThickness: round(middle),
    exitThickness: round(exit),
    totalReductionPercent: (1 - exit / entry) * 100,
    f1ReductionPercent: f1Reduction * 100,
    f2ReductionPercent: f2Reduction * 100,
    f1MinusF2PercentPoints: (f1Reduction - f2Reduction) * 100,
    f1TrueStrainShare: round(f1TrueStrainShare, 4),
    parameterStatus: "available",
    parameters,
    f1Force,
    f2Force,
    risk,
    assessment: {
      dataCoverage: combineRisk([
        ...reductions, ...parameters.map((parameter) => parameter.risk),
        upperRisk(f1HistoricalRiskLoad, baseline.envelopes.f1_total_force_t),
        upperRisk(f2HistoricalRiskLoad, baseline.envelopes.f2_total_force_t),
      ]),
      equipment: equipmentStatus(Math.max(f1EquipmentSafetyTotal, f2EquipmentSafetyTotal)),
      quality: "provisional_rules_met",
      qualityValidated: false,
    },
  };
}

function buildRouteOnlyPass(
  index: number,
  entry: number,
  middle: number,
  exit: number,
): PassPlan {
  const f1Reduction = 1 - middle / entry;
  const f2Reduction = 1 - exit / middle;
  const totalStrain = Math.log(entry / exit);
  const f1TrueStrainShare = Math.log(entry / middle) / totalStrain;
  return {
    index,
    entryThickness: round(entry),
    intermediateThickness: round(middle),
    exitThickness: round(exit),
    totalReductionPercent: (1 - exit / entry) * 100,
    f1ReductionPercent: f1Reduction * 100,
    f2ReductionPercent: f2Reduction * 100,
    f1MinusF2PercentPoints: (f1Reduction - f2Reduction) * 100,
    f1TrueStrainShare: round(f1TrueStrainShare, 4),
    parameterStatus: "route_only",
    parameters: [],
    f1Force: null,
    f2Force: null,
    assessment: { dataCoverage: "unavailable", equipment: "not_evaluated", quality: "provisional_rules_met", qualityValidated: false },
    risk: combineRisk([
      upperRisk(f1Reduction, baseline.envelopes.f1_reduction_ratio),
      upperRisk(f2Reduction, baseline.envelopes.f2_reduction_ratio),
    ]),
  };
}

function finishingPolicy(targetThickness: number) {
  return finishingPreference(targetThickness);
}

function buildFactoryRoute(input: RouteInput, gauges: GaugePass[], parametersAvailable: boolean) {
  if (factoryGeometryReasons(input, gauges).length) return null;
  const passes: PassPlan[] = [];
  for (const [index, gauge] of gauges.entries()) {
    if (parametersAvailable && !modelCoverage(gauge.entry, gauge.exit, input.width)) return null;
    const pass = parametersAvailable
      ? buildPass(index + 1, gauges.length, gauge.entry, gauge.exit, input.width,
        input, Math.log(input.entryThickness / gauge.entry), gauge.middle)
      : buildRouteOnlyPass(index + 1, gauge.entry, gauge.middle, gauge.exit);
    pass.materialPolicy = passReductionLimits(input, gauge, index);
    passes.push(pass);
  }
  return passes;
}

function attemptHistoricalRoute(input: RouteInput, path: ReverseHistoryPath) {
  const records = [...path.reversedRecords].reverse();
  const requiredStrain = Math.log(input.entryThickness / input.targetThickness);
  const historicalStrain = records.reduce((sum, record) => sum + record.passStrain, 0);
  const strainScale = requiredStrain / historicalStrain;
  if (strainScale < 0.86 || strainScale > 1.16) return null;

  const passes: PassPlan[] = [];
  let entry = input.entryThickness;
  let routeStrainBefore = 0;
  for (let index = 0; index < records.length; index += 1) {
    const record = records[index];
    const passStrain = record.passStrain * strainScale;
    const exit = index === records.length - 1
      ? input.targetThickness
      : entry * Math.exp(-passStrain);
    const f1Share = Math.max(0.5, Math.min(0.72, record.f1StrainShare));
    const middle = entry * Math.exp(-passStrain * f1Share);
    if (!modelCoverage(entry, exit, input.width)) return null;
    passes.push(buildPass(
      index + 1,
      records.length,
      entry,
      exit,
      input.width,
      input,
      routeStrainBefore,
      middle,
    ));
    routeStrainBefore += passStrain;
    entry = exit;
  }
  return {
    passes,
    supportDates: records.map((record) => record.supportDates),
    references: records.map((record) => ({
      entry: record.entry,
      middle: record.middle,
      exit: record.exit,
      width: record.width,
      date: record.date,
      dataset: record.dataset,
    })),
  };
}

function routeRejectionReasons(input: RouteInput, passes: PassPlan[]) {
  if (!passes.length) return ["empty_route"];
  const gauges = passes.map(pass => ({ entry: pass.entryThickness, middle: pass.intermediateThickness, exit: pass.exitThickness }));
  const reasons = factoryGeometryReasons(input, gauges, passes);
  if (input.width > equipmentPhysics.equipment.roll_body_length_mm) reasons.push("roll_body_width");
  const limits = baseline.confirmed_plant_limits;
  for (const [index, pass] of passes.entries()) {
    const expectedEntry = index === 0 ? input.entryThickness : passes[index - 1].exitThickness;
    if (Math.abs(pass.entryThickness - expectedEntry) > 0.000051) reasons.push("route_discontinuity");
    for (const [force, limit] of [
      [pass.f1Force, limits?.f1_total_force_t_max], [pass.f2Force, limits?.f2_total_force_t_max],
    ] as const) {
      const maximum = limit ?? equipmentPhysics.equipment.maximum_total_rolling_force_t_per_stand;
      if (force && (!Number.isFinite(force.safetyTotal) || force.safetyTotal > maximum)) reasons.push("equipment_force_limit");
    }
  }
  if (Math.abs(passes.at(-1)!.exitThickness - input.targetThickness) > 0.000051) reasons.push("target_not_closed");
  return [...new Set(reasons)];
}

function finishingBand(input: RouteInput, passes: PassPlan[]): FinishingBand | null {
  const finishing = passes.at(-1);
  if (finishing == null) return null;
  if (passes.length === 1) return "single_pass";
  const policy = finishingPolicy(input.targetThickness);
  const f1 = finishing.f1ReductionPercent / 100;
  const f2 = finishing.f2ReductionPercent / 100;
  const f1Preferred = f1 >= policy.preferredF1[0] - 1e-9 && f1 <= policy.preferredF1[1] + 1e-9;
  const f2Preferred = f2 >= policy.preferredF2[0] - 1e-9 && f2 <= policy.preferredF2[1] + 1e-9;
  if (f1Preferred && f2Preferred) return "preferred";
  if (f1 <= policy.preferredF1[1] && f2 <= policy.preferredF2[1]) return "light";
  return "review";
}


function manufacturerCurvePenalty(input: RouteInput, passes: PassPlan[]) {
  const finishing = passes.at(-1);
  if (finishing == null) return Number.POSITIVE_INFINITY;
  const policy = finishingPolicy(input.targetThickness);
  return Math.abs(finishing.f1ReductionPercent / 100 - policy.targetF1) +
    Math.abs(finishing.f2ReductionPercent / 100 - policy.targetF2);
}

function finishingBandRank(value: FinishingBand, totalTrueStrain: number, targetThickness: number) {
  if (value === "preferred" || value === "single_pass") return 0;
  const policy = finishingPolicy(targetThickness);
  const preferredFinishingStrain = -Math.log(
    (1 - policy.targetF1) * (1 - policy.targetF2),
  );
  if (value === "light" && totalTrueStrain <= preferredFinishingStrain) return 0;
  return value === "light" ? 1 : 2;
}

export function createRoutePlan(rawInput: RouteInput): RoutePlan {
  if (rawInput == null || typeof rawInput.alloy !== "string") throw new Error("牌号必须为字符串");
  const alloy = rawInput.alloy.trim().toUpperCase();
  const input = { ...rawInput, alloy };
  if (!routeArtifact.supported_alloys.includes(alloy)) throw new Error("牌号仅支持1060、1100、3003");
  if (!["annealed", "h14", "h16", "h18", "h22", "h24", "h26", "unannealed", "unknown"].includes(input.incomingState)) throw new Error("来料状态无效");
  if (!["h14", "h16", "h18", "h22", "h24", "h26", "hard", "annealed", "unspecified"].includes(input.targetState)) throw new Error("目标状态无效");
  if (!Number.isFinite(input.entryThickness) || input.entryThickness <= 0) throw new Error("来料厚度必须大于0");
  if (!Number.isFinite(input.targetThickness) || input.targetThickness <= 0) throw new Error("目标厚度必须大于0");
  if (input.targetThickness >= input.entryThickness) throw new Error("目标厚度必须小于来料厚度");
  if (!Number.isFinite(input.width) || input.width <= 0) throw new Error("产品宽度必须大于0");
  const [widthLow, widthHigh] = routeArtifact.feature_bounds.width_set_mode;
  if (input.width < widthLow || input.width > widthHigh) {
    throw new Error(`产品宽度超出高速数据范围 ${widthLow}—${widthHigh} mm`);
  }
  const [entryLow, entryHigh] = routeArtifact.feature_bounds.entry_thickness_set_mode;
  const [exitLow, exitHigh] = routeArtifact.feature_bounds.exit_thickness_set_mode;
  const [equipmentLow, equipmentHigh] = FACTORY_ROUTE_POLICY.confirmed.equipment_thickness_mm;
  if (input.entryThickness > equipmentHigh || input.targetThickness < equipmentLow) {
    throw new Error(`厂家确认厚度总体范围为 ${equipmentLow}—${equipmentHigh} mm`);
  }
  if (1 - input.targetThickness / input.entryThickness <= 0.05) {
    throw new Error("总压下不足以同时满足单道F1比F2高5个百分点且两架均有正压下，请复核该规格的分配规则");
  }
  const totalTrueStrain = Math.log(input.entryThickness / input.targetThickness);
  const parameterRecommendationAvailable = input.entryThickness >= entryLow && input.entryThickness <= entryHigh &&
    input.targetThickness >= exitLow && input.targetThickness <= exitHigh;
  type Candidate = {
    id: number;
    passes: PassPlan[];
    band: FinishingBand;
    method: RoutePlan["evidence"]["routeMethod"];
    methodRank: number;
    historyScore: number;
    supportDates: number[];
    references: RoutePlan["evidence"]["routeHistoryReferences"];
  };
  const candidates: Candidate[] = [];
  const audits: CandidateAudit[] = [];
  const ranking = (candidate: Omit<Candidate, "id">) => [
    finishingBandRank(candidate.band, totalTrueStrain, input.targetThickness),
    candidate.passes.length,
    round(routeShapeScore(input, candidate.passes), 6),
    -round(candidate.passes[0].totalReductionPercent, 6),
    round(manufacturerCurvePenalty(input, candidate.passes), 6),
    candidate.methodRank,
    Number.isFinite(candidate.historyScore) ? candidate.historyScore : Number.MAX_SAFE_INTEGER,
  ];
  const attempt = (method: Candidate["method"], passCount: number, build: () => Omit<Candidate, "id"> | null) => {
    const id = audits.length + 1;
    try {
      const candidate = build();
      const reasons = candidate == null ? ["construction_or_model_coverage"] : routeRejectionReasons(input, candidate.passes);
      const forces = candidate?.passes.flatMap((pass) => [pass.f1Force?.safetyTotal, pass.f2Force?.safetyTotal])
        .filter((value): value is number => value != null) ?? [];
      audits.push({ id, method, passCount, accepted: reasons.length === 0, reasons,
        finishingBand: candidate ? finishingBand(input, candidate.passes) : null,
        maximumCheckForceT: forces.length ? Math.max(...forces) : null,
        ranking: reasons.length === 0 && candidate ? ranking(candidate) : null,
      });
      if (!reasons.length && candidate) candidates.push({ ...candidate, id });
    } catch (error) {
      if (!(error instanceof PhysicsDomainError)) throw error;
      audits.push({ id, method, passCount, accepted: false, reasons: ["physics_invalid", error.message],
        finishingBand: null, maximumCheckForceT: null, ranking: null });
    }
  };

  // Work backwards from the requested finished gauge.  Each selected item is
  // a real high-speed entry -> F1 middle -> F2 exit edge; local edges are
  // composed into a candidate route and then re-scaled slightly so the route
  // closes exactly on the requested entry and target gauges.  Since DAT lacks
  // a reliable coil id this is evidence-backed route composition, not a claim
  // that the selected edges belonged to one historical coil.
  if (parameterRecommendationAvailable) {
    for (const search of [
      { tolerance: 0.08, method: "dat_reverse_chain" as const, methodRank: 0 },
      { tolerance: 0.14, method: "dat_interpolated_chain" as const, methodRank: 1 },
    ]) {
      for (const path of reverseHistoryPaths(input, search.tolerance)) {
        attempt(search.method, path.reversedRecords.length, () => {
          const candidate = attemptHistoricalRoute(input, path);
          if (!candidate) return null;
          return {
            passes: candidate.passes,
            band: finishingBand(input, candidate.passes) ?? "review",
            method: search.method,
            methodRank: search.methodRank,
            historyScore: path.score,
            supportDates: candidate.supportDates,
            references: candidate.references,
          };
        });
      }
    }
  }

  // Generate a range around the factory 5-point taper, closing the final
  // thickness exactly. Load only rejects infeasible candidates; it never ranks.
  for (let count = 1; count <= FACTORY_ROUTE_POLICY.engineering.maximum_pass_count; count += 1) {
    const geometries = generateFactoryGauges(input, count);
    if (!geometries.length) attempt("quality_constrained_fallback", count, () => null);
    for (const gauges of geometries) {
      attempt("quality_constrained_fallback", count, () => {
        const passes = buildFactoryRoute(input, gauges, parameterRecommendationAvailable);
        if (!passes) return null;
        return { passes, band: finishingBand(input, passes) ?? "review",
          method: "quality_constrained_fallback", methodRank: 2,
          historyScore: Number.MAX_SAFE_INTEGER, supportDates: [], references: [] };
      });
    }
  }
  if (!candidates.length) throw new RoutePlanningError("当前候选搜索未找到同时满足工艺约束、参数覆盖及有效负荷校核的路线；不代表设备一定无法生产", audits);
  candidates.sort((left, right) => {
    const leftRank = ranking(left);
    const rightRank = ranking(right);
    for (let index = 0; index < leftRank.length; index += 1) {
      if (leftRank[index] !== rightRank[index]) return leftRank[index] - rightRank[index];
    }
    return left.id - right.id;
  });
  const selected = candidates[0];
  const rankingOrder = ["finishing_policy", "pass_count", "hard_pass_drop", "opening_reduction", "finishing_target", "history_method", "history_score"];
  const selectedRank = ranking(selected);
  audits.filter((audit) => audit.accepted && audit.id !== selected.id).forEach((audit) => {
    const difference = audit.ranking!.findIndex((value, index) => value !== selectedRank[index]);
    audit.notSelectedReason = difference < 0 ? "stable_tie_break" : rankingOrder[difference];
  });
  const passes = selected.passes;
  const minimumFeasiblePassCount = Math.min(...candidates.map((candidate) => candidate.passes.length));
  const routeMethod = selected.method;
  const routeSupportDates = selected.supportDates;
  const routeHistoryReferences = selected.references;
  const risk = combineRisk(passes.map((pass) => pass.risk));
  const recipe = baseline.annealing_policy.final_o_temper_reference[alloy];
  const partialAnnealTarget = ["h22", "h24", "h26"].includes(input.targetState);
  const finalAnneal = partialAnnealTarget ? "partial_anneal_recipe_required" as const :
    input.targetState !== "annealed" ? "not_required" as const :
      recipe?.temperature_c != null ? "industry_reference" as const : "plant_recipe_required" as const;
  const assess = (candidate: Candidate): Assessment => ({
    dataCoverage: parameterRecommendationAvailable ? combineRisk(candidate.passes.map(pass => pass.assessment.dataCoverage as RiskLevel)) : "unavailable",
    equipment: parameterRecommendationAvailable
      ? equipmentStatus(Math.max(...candidate.passes.flatMap(pass => [pass.f1Force!.safetyTotal, pass.f2Force!.safetyTotal])))
      : "not_evaluated",
    quality: ["preferred", "single_pass"].includes(candidate.band) && input.incomingState !== "unknown" && input.targetState === "unspecified"
      ? "provisional_rules_met" : "review_required",
    qualityValidated: false,
  });
  const fewest = candidates.find(candidate => candidate.passes.length === minimumFeasiblePassCount)!;
  const alternatives: RouteAlternative[] = selected.passes.length > fewest.passes.length ? [{
    candidateId: fewest.id, kind: "fewest_passes", label: "少道次方案",
    passCount: fewest.passes.length, passes: fewest.passes, finishingBand: fewest.band,
    risk: combineRisk(fewest.passes.map(pass => pass.risk)), assessment: assess(fewest),
    explanation: `${fewest.passes.length}道满足当前筛选条件；${selected.passes.length}道更接近末道板形调整目标，两套方案同时提供。`,
  }] : [];
  return {
    id: `RP-${new Date().toISOString().replace(/\D/g, "").slice(0, 14)}`,
    createdAt: new Date().toISOString(),
    modelVersion: routeArtifact.model_version,
    plannerVersion: "factory-route-v0.7.2",
    input,
    totalReductionPercent: round((1 - input.targetThickness / input.entryThickness) * 100, 2),
    totalTrueStrain: round(totalTrueStrain),
    passCount: passes.length,
    minimumFeasiblePassCount,
    minimumPassCountScope: "generated_candidates_only",
    selection: {
      policy: "finishing_then_count_then_reduction_shape",
      selectedCandidateId: selected.id,
      rankingOrder,
      extraPassesForFinishing: passes.length - minimumFeasiblePassCount,
      explanation: passes.length > minimumFeasiblePassCount
        ? `已生成候选中最少${minimumFeasiblePassCount}道；按末道优选规则选择${passes.length}道，增加${passes.length - minimumFeasiblePassCount}道，并同时提供少道次方案。末道优选尚无质量标签验证。`
        : `在已生成且通过约束的候选中选择${passes.length}道，按约5个百分点递减、首道多承担压下及末道目标比较；负荷不参与排序，未证明为全局最少道次。`,
      candidates: audits,
    },
    assessment: assess(selected),
    alternatives,
    ruleSummary: {
      version: FACTORY_ROUTE_POLICY.version,
      thicknessRegion: thicknessRegion(input.targetThickness),
      hardPassDropPoints: FACTORY_ROUTE_POLICY.confirmed.hard_pass_drop_points,
      trialDropTolerancePoints: FACTORY_ROUTE_POLICY.engineering.hard_pass_drop_tolerance_points,
      intermediateAnnealing: "excluded",
      parameterAssumptions: [
        "相邻硬态道次按F1对F1、F2对F2约降5个百分点，当前试算带宽为3—7个百分点。",
        "O态经过首架后按加工硬化料分配，不等同于已达到H18；物理模型继续累计实际变形。",
        "薄中厚按0.1—7.0 mm线性三等分；末道目标连续插值，目标点及容差仍为可调整试算值。",
        "材料压下能力采用文献强度差异和厂家相对修正；宽度进入轧制力公式，负荷仅作超限筛选。",
      ],
    },
    finishingBand: selected.band,
    parameterRecommendationAvailable,
    parameterUnavailableReason: parameterRecommendationAvailable
      ? null
      : `该规格超出完整参数模型覆盖范围，仅提供厚度路线和压下率；未评估设备负荷`,
    passes,
    risk,
    anneal: {
      intermediate: "excluded",
      final: finalAnneal,
      temperatureC: finalAnneal === "industry_reference" ? recipe.temperature_c : null,
      holdHours: finalAnneal === "industry_reference" ? recipe.hold_hours : null,
      reviewAfterPass: null,
      reason: "现阶段仅规划连续双机架轧制，不安排或推荐中间退火；F1为0且仅F2工作的单轧记录排除。",
    },
    evidence: {
      productionCases: baseline.data.production_case_count,
      sideForceCases: baseline.data.side_force_case_count,
      envelopeType: "high_speed_history",
      routeMethod,
      routeHistoryCases: routeHistory.case_count,
      routeSupportDates,
      routeHistoryReferences,
      equipment: {
        workRollDiameterMm: equipmentPhysics.equipment.work_roll_diameter_mm,
        supportRollDiameterMm: equipmentPhysics.equipment.support_roll_diameter_mm,
        rollBodyLengthMm: equipmentPhysics.equipment.roll_body_length_mm,
        maximumForceT: equipmentPhysics.equipment.maximum_total_rolling_force_t_per_stand,
        normalSpeedRangeMPerMin: equipmentPhysics.equipment.normal_speed_range_m_per_min,
        frictionCoefficient: equipmentPhysics.normal_lubrication.friction_coefficient,
        goldenPlateaus: equipmentPhysics.normal_lubrication.eligible_golden_plateaus,
        physicsModelVersion: equipmentPhysics.schema_version,
        physicsMethod: "Swift-Hill-Bland-Ford-Hitchcock",
        physicsMaeT: equipmentPhysics.validation.mae_t,
        physicsP95T: equipmentPhysics.validation.p95_absolute_error_t,
      },
    },
  };
}

export function riskLabel(risk: RiskLevel) {
  return risk === "normal" ? "正常" : risk === "warning" ? "接近历史边界" : "超出历史范围";
}

export type AllocationPhysicsCalibration = Pick<EquipmentPhysicsArtifact, "equipment" | "normal_lubrication" | "model">;

/**
 * Experimental fixed-pass evaluator. Preparing the DAT reference once makes
 * it impossible for a candidate split to redefine its own calibration anchor.
 * Separate entry point: allocation fusion remains experimental and is not used for production ranking.
 */
export function createAllocationFusionEvaluator(
  input: RouteInput,
  options: { routeStrainBefore?: number; calibration?: AllocationPhysicsCalibration } = {},
) {
  const alloy = typeof input.alloy === "string" ? input.alloy.trim().toUpperCase() : "";
  if (!routeArtifact.supported_alloys.includes(alloy)) throw new Error("牌号不受支持");
  if (!["annealed", "h14", "h16", "h18", "h22", "h24", "h26", "unannealed"].includes(input.incomingState)) {
    throw new Error("局部分配试算需要已指定的来料状态");
  }
  const { entryThickness: entry, targetThickness: exit, width } = input;
  const strainBefore = options.routeStrainBefore ?? 0;
  if (![entry, exit, width, strainBefore].every(Number.isFinite) || strainBefore < 0 ||
      !(entry > exit && exit > 0 && width > 0) || !modelCoverage(entry, exit, width)) {
    throw new Error("局部分配试算超出参数覆盖或应变无效");
  }
  const features = featureValues(entry, exit, width);
  // Use the actual learned midpoint, never the candidate or a geometry clamp.
  const middle = predictSingle(routeArtifact.models.intermediate_thickness_set_mode, features);
  if (!(entry > middle && middle > exit)) throw new Error("DAT参考中间厚度无效，不能建立融合基准");
  const referenceFeatures = {
    ...features,
    intermediate_thickness_set_mode: middle,
    contact_proxy_f2: width * Math.sqrt(190 * (middle - exit)),
  };
  const raw = Object.fromEntries(outputCodes.map(code => [code, code === "intermediate_thickness_set_mode"
    ? middle : predict(routeArtifact.models[code], referenceFeatures, code)])) as Record<(typeof outputCodes)[number], number>;
  const referenceProjection = orderedTensionValues(raw, width, entry, middle, exit).values;
  const tensionsOf = (values: typeof raw): TensionTriple => [
    values.uncoiler_tension_set_median, values.interstand_tension_set_median, values.coiler_tension_set_median,
  ];
  const referenceTensions = tensionsOf(referenceProjection);
  const strainF1 = Math.log(entry / middle);
  const totalStrain = Math.log(entry / exit);
  const materialFactors: StandPair = [
    materialForceFactor(alloy, input.incomingState, strainBefore, strainBefore + strainF1),
    materialForceFactor(alloy, input.incomingState, strainBefore + strainF1, strainBefore + totalStrain),
  ];
  const referenceData: StandPair = [raw.f1_roll_force_sum_median * materialFactors[0], raw.f2_roll_force_sum_median * materialFactors[1]];
  const calibration = options.calibration ?? equipmentPhysics;
  const equipment = calibration.equipment;
  const referenceFlows = (["f1", "f2"] as const).map((stand, index) => {
    const thickness = index === 0 ? entry : middle;
    const band = calibration.model.bands[stand].find(item =>
      thickness > item.entry_thickness_low_mm && thickness <= item.entry_thickness_high_mm);
    if (!band) throw new Error("参考厚度超出物理标定范围");
    return band.effective_flow_stress_median_mpa * materialFactors[index];
  }) as StandPair;
  const widthFactors = (["f1", "f2"] as const).map(stand =>
    smoothWidthFactor(calibration.model.width_correction.bands[stand], width)) as StandPair;
  const limits = baseline.confirmed_plant_limits;
  const tensionLimits = [limits?.uncoiler_tension_t_max, limits?.interstand_tension_t_max, limits?.coiler_tension_t_max];
  return {
    mode: "shadow_only" as const,
    canApply: false as const,
    responseAssumption: "fixed_reference_effective_stress_for_local_geometry_and_tension_changes" as const,
    policy: ALLOCATION_FUSION_POLICY,
    referenceMiddle: middle,
    referenceTensions: [...referenceTensions] as TensionTriple,
    referenceData: [...referenceData] as StandPair,
    evaluate(candidateMiddle: number, suppliedTensions?: TensionTriple): AllocationFusionResult {
      if (!(entry > candidateMiddle && candidateMiddle > exit)) return { status: "unsupported", reasons: ["invalid_candidate_geometry"] };
      const candidateTensions = suppliedTensions ?? tensionsOf(orderedTensionValues(raw, width, entry, candidateMiddle, exit).values);
      if (candidateTensions.length !== 3 || candidateTensions.some((value, index) => !Number.isFinite(value) || value < 0 ||
        (tensionLimits[index] != null && value > tensionLimits[index]!))) {
        return { status: "unsupported", reasons: ["tension_outside_confirmed_limits"] };
      }
      return computeAllocationFusion({
        entry, exit, width, widthFactors,
        equipment: {
          mu: calibration.normal_lubrication.friction_coefficient,
          radiusMm: equipment.work_roll_radius_mm, youngModulusMpa: equipment.roll_young_modulus_mpa,
          poissonRatio: equipment.roll_poisson_ratio,
          backWeight: calibration.model.back_tension_weight, frontWeight: calibration.model.front_tension_weight,
          maximumIterations: calibration.model.iteration.maximum_iterations,
          radiusToleranceMm: calibration.model.iteration.radius_tolerance_mm,
        },
        reference: { middle, tensions: referenceTensions, dataForces: referenceData, flowStresses: referenceFlows },
        candidate: { middle: candidateMiddle, tensions: candidateTensions },
      });
    },
  };
}

/**
 * Recalculate a plant-supplied gauge route with exactly the same DAT, material
 * correction and equipment-force models used by the planner. This is an audit
 * helper: it evaluates the supplied route but deliberately does not select or
 * silently modify it.
 */
export function evaluateFixedThicknessRoute(
  rawInput: RouteInput,
  gauges: Array<{ intermediateThickness: number; exitThickness: number }>,
) {
  const input = { ...rawInput, alloy: rawInput.alloy.trim().toUpperCase() };
  if (!routeArtifact.supported_alloys.includes(input.alloy)) throw new Error("牌号不受支持");
  if (!gauges.length) throw new Error("固定路线不能为空");
  let entry = input.entryThickness;
  let routeStrainBefore = 0;
  const passes = gauges.map((gauge, index) => {
    const middle = gauge.intermediateThickness;
    const exit = gauge.exitThickness;
    if (!(entry > middle && middle > exit)) throw new Error(`第${index + 1}道厚度必须严格递减`);
    if (!modelCoverage(entry, exit, input.width)) throw new Error(`第${index + 1}道超出当前高速模型覆盖`);
    const pass = buildPass(
      index + 1,
      gauges.length,
      entry,
      exit,
      input.width,
      input,
      routeStrainBefore,
      middle,
    );
    routeStrainBefore += Math.log(entry / exit);
    entry = exit;
    return pass;
  });
  if (Math.abs(entry - input.targetThickness) > 0.005) {
    throw new Error("固定路线末端厚度与目标厚度不一致");
  }
  const reasons = routeRejectionReasons(input, passes);
  if (reasons.length) passes.forEach((pass) => { pass.assessment.quality = "review_required"; });
  return passes;
}

export function getOperatingEnvelopeSummary() {
  const item = (code: string, label: string, unit: string) => {
    const value = baseline.envelopes[code];
    return { code, label, unit, p05: value.p05, p95: value.p95, p99: value.p99, count: value.count };
  };
  return [
    item("f1_reduction_ratio", "F1压下率", "%"),
    item("f2_reduction_ratio", "F2压下率", "%"),
    item("uncoiler_tension_t", "开卷张力", "T"),
    item("interstand_tension_t", "机架张力", "T"),
    item("coiler_tension_t", "卷取张力", "T"),
    item("uncoiler_unit_tension_kn_mm2", "开卷单位张力", "kN/mm²"),
    item("interstand_unit_tension_kn_mm2", "机架单位张力", "kN/mm²"),
    item("coiler_unit_tension_kn_mm2", "卷取单位张力", "kN/mm²"),
    item("f1_operation_force_t", "F1操作侧轧制力", "T"),
    item("f1_drive_force_t", "F1传动侧轧制力", "T"),
    item("f2_operation_force_t", "F2操作侧轧制力", "T"),
    item("f2_drive_force_t", "F2传动侧轧制力", "T"),
  ].map((row) => row.unit === "%" ? {
    ...row,
    p05: round(row.p05 * 100, 1),
    p95: round(row.p95 * 100, 1),
    p99: round(row.p99 * 100, 1),
  } : row);
}
