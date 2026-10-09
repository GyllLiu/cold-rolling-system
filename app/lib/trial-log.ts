import type { RoutePlan } from "./route-planner";

const STORAGE_KEY = "yh.cold-rolling.route-trials.v061";
const MAX_RECORDS = 5000;

export type RouteTrialRecord = {
  recordId: string;
  generatedAtIso: string;
  generatedAtLocal: string;
  generatedAtEpochMs: number;
  timeZone: string;
  plan: RoutePlan;
};

function available() {
  return typeof window !== "undefined" && window.localStorage != null;
}

export function readRouteTrialRecords(): RouteTrialRecord[] {
  if (!available()) return [];
  try {
    const parsed = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed as RouteTrialRecord[] : [];
  } catch {
    return [];
  }
}

export function saveRouteTrial(plan: RoutePlan) {
  if (!available()) return 0;
  try {
    const now = new Date(plan.createdAt);
    const record: RouteTrialRecord = {
      recordId: typeof crypto !== "undefined" && "randomUUID" in crypto
        ? crypto.randomUUID()
        : `${now.getTime()}-${Math.random().toString(16).slice(2)}`,
      generatedAtIso: now.toISOString(),
      generatedAtLocal: now.toLocaleString("sv-SE", { hour12: false }),
      generatedAtEpochMs: now.getTime(),
      timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      plan,
    };
    const records = [record, ...readRouteTrialRecords()].slice(0, MAX_RECORDS);
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(records));
    return records.length;
  } catch {
    return readRouteTrialRecords().length;
  }
}

function csvCell(value: unknown) {
  const text = value == null ? "" : String(value);
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function parameterValue(plan: RoutePlan, passIndex: number, code: string) {
  return plan.passes[passIndex]?.parameters.find((item) => item.code === code)?.value ?? "";
}

export function routeTrialCsv(records: RouteTrialRecord[]) {
  const headers = [
    "record_id", "plan_id", "generated_at_local", "generated_at_iso", "generated_at_epoch_ms", "time_zone",
    "model_version", "planner_version", "alloy", "incoming_state", "target_state", "entry_thickness_mm",
    "target_thickness_mm", "width_mm", "route_pass_count", "route_risk", "parameter_recommendation_available", "pass_index",
    "pass_entry_mm", "intermediate_mm", "pass_exit_mm", "uncoiler_tension_t",
    "interstand_tension_t", "coiler_tension_t", "uncoiler_unit_tension_kn_mm2",
    "interstand_unit_tension_kn_mm2", "coiler_unit_tension_kn_mm2", "f1_gap_position_mm", "f2_gap_position_mm",
    "f1_recommended_force_total_t", "f1_physical_check_force_t", "f1_force_drive_t", "f1_force_operation_t", "f1_material_factor",
    "f1_constitutive_mean_flow_stress_mpa", "f1_calibrated_mean_flow_stress_mpa", "f1_q_factor", "f1_flattened_radius_mm", "f1_width_correction_factor",
    "f2_recommended_force_total_t", "f2_physical_check_force_t", "f2_force_drive_t", "f2_force_operation_t", "f2_material_factor",
    "f2_constitutive_mean_flow_stress_mpa", "f2_calibrated_mean_flow_stress_mpa", "f2_q_factor", "f2_flattened_radius_mm", "f2_width_correction_factor",
    "data_coverage", "equipment_status", "quality_status", "quality_validated",
    "f1_conservative_check_total_t", "f2_conservative_check_total_t",
    "f1_low_tension_check_t", "f2_low_tension_check_t", "selection_explanation",
    "route_option", "candidate_id", "factory_rule_version",
  ];
  const rows: unknown[][] = [];
  for (const record of records) {
    const original = record.plan;
    const options = [{ plan: original, kind: "recommended", candidateId: original.selection?.selectedCandidateId },
      ...(original.alternatives ?? []).map(option => ({ kind: option.kind, candidateId: option.candidateId,
        plan: { ...original, passes: option.passes, passCount: option.passCount, risk: option.risk,
          finishingBand: option.finishingBand, assessment: option.assessment } }))];
    for (const option of options) {
    const { plan } = option;
    plan.passes.forEach((pass, index) => rows.push([
      record.recordId, plan.id, record.generatedAtLocal, record.generatedAtIso, record.generatedAtEpochMs,
      record.timeZone, plan.modelVersion, plan.plannerVersion, plan.input.alloy, plan.input.incomingState,
      plan.input.targetState, plan.input.entryThickness, plan.input.targetThickness, plan.input.width,
      plan.passCount, plan.risk, plan.parameterRecommendationAvailable, pass.index, pass.entryThickness, pass.intermediateThickness,
      pass.exitThickness, parameterValue(plan, index, "uncoiler_tension_set_median"),
      parameterValue(plan, index, "interstand_tension_set_median"),
      parameterValue(plan, index, "coiler_tension_set_median"),
      pass.parameters.find((item) => item.code === "uncoiler_tension_set_median")?.unitTension ?? "",
      pass.parameters.find((item) => item.code === "interstand_tension_set_median")?.unitTension ?? "",
      pass.parameters.find((item) => item.code === "coiler_tension_set_median")?.unitTension ?? "",
      parameterValue(plan, index, "tm1_closed_gap_set_median"),
      parameterValue(plan, index, "tm2_closed_gap_set_median"),
      pass.f1Force?.total ?? "", pass.f1Force?.physicsBaseline ?? "", pass.f1Force?.drive ?? "", pass.f1Force?.operation ?? "", pass.f1Force?.materialFactor ?? "",
      pass.f1Force?.constitutiveMeanFlowStressMpa ?? "", pass.f1Force?.calibratedMeanFlowStressMpa ?? "", pass.f1Force?.qFactor ?? "", pass.f1Force?.flattenedRadiusMm ?? "", pass.f1Force?.widthCorrectionFactor ?? "",
      pass.f2Force?.total ?? "", pass.f2Force?.physicsBaseline ?? "", pass.f2Force?.drive ?? "", pass.f2Force?.operation ?? "", pass.f2Force?.materialFactor ?? "",
      pass.f2Force?.constitutiveMeanFlowStressMpa ?? "", pass.f2Force?.calibratedMeanFlowStressMpa ?? "", pass.f2Force?.qFactor ?? "", pass.f2Force?.flattenedRadiusMm ?? "", pass.f2Force?.widthCorrectionFactor ?? "",
      plan.assessment?.dataCoverage ?? "", plan.assessment?.equipment ?? "",
      plan.assessment?.quality ?? "", plan.assessment?.qualityValidated ?? "",
      pass.f1Force?.safetyTotal ?? "", pass.f2Force?.safetyTotal ?? "",
      pass.f1Force?.lowTensionCheckTotal ?? "", pass.f2Force?.lowTensionCheckTotal ?? "",
      plan.selection?.explanation ?? "",
      option.kind, option.candidateId, plan.ruleSummary?.version ?? "",
    ]));
    }
  }
  return "\uFEFF" + [headers, ...rows].map((row) => row.map(csvCell).join(",")).join("\r\n");
}

export function exportRouteTrialCsv() {
  const records = readRouteTrialRecords();
  if (!records.length) return false;
  const csv = routeTrialCsv(records);
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = `冷轧工艺试用记录_${new Date().toISOString().slice(0, 10)}.csv`;
  anchor.click();
  URL.revokeObjectURL(url);
  return true;
}
