import {
  createRoutePlan,
  type IncomingState,
  type RouteInput,
  type RoutePlan,
  type TargetState,
} from "./route-planner";

export type { IncomingState, TargetState } from "./route-planner";
export type ProcessFrameworkInput = RouteInput;
export type ProcessFrameworkPlan = RoutePlan;

export function createFrameworkPlan(input: ProcessFrameworkInput): ProcessFrameworkPlan {
  return createRoutePlan(input);
}

export function incomingStateLabel(value: IncomingState) {
  if (value === "annealed") return "经过退火 / O态软料";
  if (value === "h14") return "H14 / 半硬";
  if (value === "h16") return "H16 / 3/4硬";
  if (value === "h18") return "H18 / 全硬";
  if (value === "h22") return "H22 / 1/4硬（部分退火）";
  if (value === "h24") return "H24 / 半硬（部分退火）";
  if (value === "h26") return "H26 / 3/4硬（部分退火）";
  if (value === "unannealed") return "铸轧直入 / 未退火";
  return "状态未知";
}

export function targetStateLabel(value: TargetState) {
  if (value === "annealed") return "O态成品";
  if (value === "h14") return "H14成品";
  if (value === "h16") return "H16成品";
  if (value === "h18") return "H18成品";
  if (value === "h22") return "H22成品（部分退火）";
  if (value === "h24") return "H24成品（部分退火）";
  if (value === "h26") return "H26成品（部分退火）";
  if (value === "hard") return "硬态成品 / 未细分";
  return "暂不指定";
}
