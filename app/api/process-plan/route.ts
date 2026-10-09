import { createRoutePlan, RoutePlanningError, type IncomingState, type TargetState } from "../../lib/route-planner";

export async function POST(request: Request) {
  let body: Record<string, unknown>;
  try {
    body = await request.json() as Record<string, unknown>;
  } catch {
    return Response.json({ detail: "请求内容不是有效JSON" }, { status: 400 });
  }
  try {
    return Response.json(createRoutePlan({
      alloy: String(body.alloy ?? ""),
      incomingState: String(body.incoming_state ?? "unknown") as IncomingState,
      entryThickness: Number(body.entry_thickness),
      targetThickness: Number(body.target_thickness),
      width: Number(body.width),
      targetState: String(body.target_state ?? "unspecified") as TargetState,
    }));
  } catch (error) {
    return Response.json(
      { detail: error instanceof Error ? error.message : "工艺方案计算失败",
        ...(error instanceof RoutePlanningError ? { candidates: error.candidates } : {}) },
      { status: 400 },
    );
  }
}
