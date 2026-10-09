#!/usr/bin/env node

import { writeFile } from "node:fs/promises";

const cases = [
  [7, 0.7], [7, 0.4], [7, 0.3], [6, 0.5], [6, 1.0], [6, 0.8],
  [6.4, 4.2], [6.4, 3.7], [6.4, 1.15], [6.4, 0.77],
  [6.4, 0.43], [6.4, 0.33], [6.4, 0.22], [6.4, 0.17],
];

async function fetchPlan(entryThickness, targetThickness) {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("replay", `${process.pid}-${entryThickness}-${targetThickness}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  const response = await worker.fetch(
    new Request("http://localhost/api/process-plan", {
      method: "POST",
      headers: { accept: "application/json", "content-type": "application/json" },
      body: JSON.stringify({
        alloy: "1060",
        incoming_state: "annealed",
        entry_thickness: entryThickness,
        target_thickness: targetThickness,
        width: entryThickness === 6.4 ? 1320 : 1280,
        target_state: "h18",
      }),
    }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
  const payload = await response.json();
  if (!response.ok) return { entryThickness, targetThickness, error: payload.detail ?? payload };
  return {
    entryThickness,
    targetThickness,
    plannerVersion: payload.plannerVersion,
    passCount: payload.passCount,
    routeMethod: payload.evidence.routeMethod,
    risk: payload.risk,
    passes: payload.passes.map((pass) => ({
      pass: pass.index,
      entry: pass.entryThickness,
      middle: pass.intermediateThickness,
      exit: pass.exitThickness,
      f1ReductionPercent: pass.f1ReductionPercent,
      f2ReductionPercent: pass.f2ReductionPercent,
      f1ForceT: pass.f1Force?.total ?? null,
      f2ForceT: pass.f2Force?.total ?? null,
      f1SafetyT: pass.f1Force?.safetyTotal ?? null,
      f2SafetyT: pass.f2Force?.safetyTotal ?? null,
      f1EquipmentUtilizationPercent: pass.f1Force?.equipmentUtilizationPercent ?? null,
      f2EquipmentUtilizationPercent: pass.f2Force?.equipmentUtilizationPercent ?? null,
      f1PhysicsBaselineT: pass.f1Force?.physicsBaseline ?? null,
      f2PhysicsBaselineT: pass.f2Force?.physicsBaseline ?? null,
    })),
  };
}

const results = [];
for (const [entry, target] of cases) results.push(await fetchPlan(entry, target));
const output = process.argv[2];
if (output) await writeFile(output, `${JSON.stringify(results, null, 2)}\n`, "utf8");
console.log(JSON.stringify(results, null, 2));
