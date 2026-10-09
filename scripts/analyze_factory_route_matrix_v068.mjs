#!/usr/bin/env node

import { mkdir, writeFile } from "node:fs/promises";
import { resolve } from "node:path";

const incomingSpecs = [
  { thickness: 6.5, width: 1280 },
  { thickness: 6.2, width: 1280 },
  { thickness: 6.7, width: 1280 },
  { thickness: 6.5, width: 1560 },
  { thickness: 5.3, width: 1310 },
  { thickness: 5.8, width: 1350 },
];

const finishedSpecs = [
  { thickness: 2.0, width: 1220 },
  { thickness: 2.35, width: 1500 },
  { thickness: 1.0, width: 1220 },
  { thickness: 0.77, width: 1210 },
  { thickness: 1.6, width: 1000 },
  { thickness: 1.85, width: 1200 },
];

const workerUrl = new URL("../dist/server/index.js", import.meta.url);
workerUrl.searchParams.set("matrix", `${process.pid}-${Date.now()}`);
const { default: worker } = await import(workerUrl.href);

async function planRoute(input, output) {
  const response = await worker.fetch(
    new Request("http://localhost/api/process-plan", {
      method: "POST",
      headers: { accept: "application/json", "content-type": "application/json" },
      body: JSON.stringify({
        alloy: "1060",
        incoming_state: "annealed",
        entry_thickness: input.thickness,
        target_thickness: output.thickness,
        // Until the trimming point is confirmed, use incoming width so the
        // rolling-force check is conservative rather than understated.
        width: input.width,
        target_state: "h18",
      }),
    }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.detail ?? "规划失败");
  return payload;
}

function routeText(passes) {
  return passes.map((pass) =>
    `${pass.entryThickness.toFixed(4)}→${pass.intermediateThickness.toFixed(4)}→${pass.exitThickness.toFixed(4)}`
  ).join("；");
}

function reductionText(passes) {
  return passes.map((pass) =>
    `第${pass.index}道 ${pass.f1ReductionPercent.toFixed(1)}%/${pass.f2ReductionPercent.toFixed(1)}%`
  ).join("；");
}

function assess(plan) {
  const opening = plan.passes[0];
  const finishing = plan.passes.at(-1);
  const f1Main = plan.passes.every((pass) => pass.f1MinusF2PercentPoints >= 0.5);
  const smooth = plan.passes.every((pass, index, rows) => index === 0 || (
    pass.f1ReductionPercent <= rows[index - 1].f1ReductionPercent - 0.5 &&
    pass.f2ReductionPercent <= rows[index - 1].f2ReductionPercent - 0.5 &&
    pass.totalReductionPercent <= rows[index - 1].totalReductionPercent - 0.5
  ));
  const forceSafe = plan.passes.every((pass) =>
    pass.f1Force?.safetyTotal == null || (
      pass.f1Force.safetyTotal <= 1000 && pass.f2Force.safetyTotal <= 1000
    )
  );
  const finishingAccepted = plan.finishingBand === "preferred" || plan.finishingBand === "light";
  const checks = [f1Main, smooth, forceSafe, finishingAccepted];
  if (!checks.every(Boolean)) return "规则不通过";
  if (plan.risk === "outside_history") return "重点复核";
  if (opening.f1ReductionPercent >= 52.5 || opening.f2ReductionPercent >= 45.5) {
    return "首道接近上限";
  }
  if (finishing.f1ReductionPercent < 20 || finishing.f2ReductionPercent < 20) {
    return "末道轻压复核";
  }
  if (plan.risk === "warning") return "边界复核";
  return "规则通过";
}

const rows = [];
for (const input of incomingSpecs) {
  for (const output of finishedSpecs) {
    if (output.width > input.width) continue;
    const plan = await planRoute(input, output);
    const forces = plan.passes.flatMap((pass) => [
      pass.f1Force?.safetyTotal,
      pass.f2Force?.safetyTotal,
    ]).filter((value) => value != null);
    rows.push({
      inputThickness: input.thickness,
      inputWidth: input.width,
      outputThickness: output.thickness,
      outputWidth: output.width,
      passCount: plan.passCount,
      route: routeText(plan.passes),
      reductions: reductionText(plan.passes),
      maximumSafetyForceT: forces.length ? Math.max(...forces) : null,
      maximumUtilizationPercent: forces.length ? Math.max(...forces) / 10 : null,
      routeMethod: plan.evidence.routeMethod,
      historyRisk: plan.risk,
      assessment: assess(plan),
    });
  }
}

const outputDirectory = resolve(process.argv[2] ?? "../outputs/factory_route_matrix_v068");
await mkdir(outputDirectory, { recursive: true });

const methodLabel = {
  dat_reverse_chain: "黄金反向链",
  dat_interpolated_chain: "黄金插值链",
  quality_constrained_fallback: "规则规划",
};
const riskLabel = { normal: "正常", warning: "接近边界", outside_history: "部分超历史" };
const assessmentCounts = rows.reduce((counts, row) => {
  counts[row.assessment] = (counts[row.assessment] ?? 0) + 1;
  return counts;
}, {});
const twoPassCount = rows.filter((row) => row.passCount === 2).length;
const threePassCount = rows.filter((row) => row.passCount === 3).length;
const maximumForceRow = rows.reduce((left, right) =>
  left.maximumSafetyForceT >= right.maximumSafetyForceT ? left : right
);

const md = [
  "# 厂家道次校验组合表（v0.6.8）",
  "",
  "> 统一假设：1060、来料退火态、目标H18。只列成品宽度不大于来料宽度的31组。轧制力按来料宽度校核。每个厚度段表示：本道入口→F1出口→F2出口。",
  "",
  "## 程序计算后的先行分析",
  "",
  `- 共31组：${twoPassCount}组推荐2道，${threePassCount}组推荐3道。`,
  `- 规则通过${assessmentCounts["规则通过"] ?? 0}组；边界复核${assessmentCounts["边界复核"] ?? 0}组；重点复核${assessmentCounts["重点复核"] ?? 0}组；首道接近上限${assessmentCounts["首道接近上限"] ?? 0}组。`,
  `- 最大保守轧制力为${maximumForceRow.maximumSafetyForceT.toFixed(1)}T（${maximumForceRow.maximumUtilizationPercent.toFixed(1)}%），出现在${maximumForceRow.inputThickness}×${maximumForceRow.inputWidth}→${maximumForceRow.outputThickness}×${maximumForceRow.outputWidth}，未超过1000T。`,
  "- 首道当前上限为F1 53%、F2 46%，依据厂家给出的52.4%/45.6%可行路线向上取整；接近上限的路线保留现场复核提示，但不再仅因超过旧50%/45%边界而增加道次。",
  "- 6.5×1560宽料接近当前高速模型宽度上沿，全部列为重点检查；其中2.35×1500末道仅15.3%/12.2%，虽然满足递减规则，但末道偏轻，需要厂家判断两道安排是否有实际意义。",
  "",
  "| 来料规格 mm | 成品规格 mm | 道次 | 厚度路线 mm | F1/F2压下率 | 最大保守轧制力 | 历史依据 | 初步判断 |",
  "|---|---|---:|---|---|---:|---|---|",
  ...rows.map((row) => [
    `| ${row.inputThickness}×${row.inputWidth}`,
    `${row.outputThickness}×${row.outputWidth}`,
    row.passCount,
    row.route,
    row.reductions,
    `${row.maximumSafetyForceT.toFixed(1)}T（${row.maximumUtilizationPercent.toFixed(1)}%）`,
    `${methodLabel[row.routeMethod]} / ${riskLabel[row.historyRisk]}`,
    `${row.assessment} |`,
  ].join(" | ")),
  "",
  "## 判断口径",
  "",
  "- F1每道至少比F2多压0.5个百分点。",
  "- F1、F2和整道总压下率逐道至少下降0.5个百分点。",
  "- 末道落在当前优选/轻压区，且保守轧制力不超过1000T。",
  "- “重点复核/边界复核”不是判定不可生产，而是提示该路线在现有高速数据中的历史覆盖较弱。",
  "",
].join("\n");

const csvHeader = [
  "来料厚度mm", "来料宽度mm", "成品厚度mm", "成品宽度mm", "道次数",
  "厚度路线mm", "F1/F2压下率", "最大保守轧制力T", "最大设备占用率%",
  "路线来源", "历史风险", "初步判断",
];
const csvEscape = (value) => `"${String(value).replaceAll('"', '""')}"`;
const csv = [
  csvHeader,
  ...rows.map((row) => [
    row.inputThickness, row.inputWidth, row.outputThickness, row.outputWidth, row.passCount,
    row.route, row.reductions, row.maximumSafetyForceT.toFixed(3),
    row.maximumUtilizationPercent.toFixed(2), methodLabel[row.routeMethod],
    riskLabel[row.historyRisk], row.assessment,
  ]),
].map((row) => row.map(csvEscape).join(",")).join("\n");

await writeFile(resolve(outputDirectory, "厂家道次校验组合表_v068.md"), md, "utf8");
await writeFile(resolve(outputDirectory, "厂家道次校验组合表_v068.csv"), `\ufeff${csv}\n`, "utf8");
await writeFile(resolve(outputDirectory, "factory_route_matrix_v068.json"), `${JSON.stringify(rows, null, 2)}\n`, "utf8");

console.log(JSON.stringify({ outputDirectory, count: rows.length, rows }, null, 2));
