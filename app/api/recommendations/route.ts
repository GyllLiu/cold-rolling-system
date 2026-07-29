import {
  buildDirectRecommendation,
  type DirectModelInput,
} from "../../lib/direct-model";

function finiteNumber(value: unknown) {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export async function POST(request: Request) {
  let body: Record<string, unknown>;
  try {
    body = (await request.json()) as Record<string, unknown>;
  } catch {
    return Response.json({ detail: "请求内容不是有效JSON" }, { status: 400 });
  }

  const entry = finiteNumber(body.entry_thickness);
  const exit = finiteNumber(body.target_exit_thickness);
  const width = finiteNumber(body.incoming_width);
  const entryActual =
    body.entry_actual_thickness == null
      ? null
      : finiteNumber(body.entry_actual_thickness);

  if (entry == null || entry <= 0 || entry > 20) {
    return Response.json({ detail: "入口厚度必须在0—20mm之间" }, { status: 400 });
  }
  if (exit == null || exit <= 0 || exit > 20) {
    return Response.json({ detail: "目标出口厚度必须在0—20mm之间" }, { status: 400 });
  }
  if (exit >= entry) {
    return Response.json({ detail: "目标出口厚度必须小于入口厚度" }, { status: 400 });
  }
  if (width == null || width <= 0 || width > 5000) {
    return Response.json({ detail: "来料宽度必须在0—5000mm之间" }, { status: 400 });
  }
  if (body.entry_actual_thickness != null && (entryActual == null || entryActual <= 0 || entryActual > 20)) {
    return Response.json({ detail: "入口实测厚度必须在0—20mm之间" }, { status: 400 });
  }

  const input: DirectModelInput = {
    entry_thickness: entry,
    entry_actual_thickness: entryActual,
    target_exit_thickness: exit,
    incoming_width: width,
  };
  try {
    const result = buildDirectRecommendation(input);
    return Response.json({
      id: crypto.randomUUID(),
      ...result,
    });
  } catch {
    return Response.json({ detail: "模型计算失败，请检查输入后重试" }, { status: 500 });
  }
}
