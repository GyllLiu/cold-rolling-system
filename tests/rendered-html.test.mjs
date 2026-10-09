import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const root = new URL("../", import.meta.url);

async function fetchApp(pathname = "/", init = {}) {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}-${Math.random()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(
    new Request(`http://localhost${pathname}`, {
      headers: { accept: "text/html", ...(init.headers ?? {}) },
      ...init,
    }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
}

test("server-renders the customer-facing process planning page", async () => {
  const response = await fetchApp();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);
  const html = await response.text();
  assert.match(html, /<title>冷二联工艺规划<\/title>/i);
  assert.match(html, /工艺推演台/);
  assert.match(html, /计算整道次方案/);
  assert.match(html, /一个道次/);
  assert.match(html, /F1 \+ F2 完整通过/);
  assert.match(html, /按 kN\/mm² 逐段校核/);
  assert.match(html, /来料厚度/);
  assert.match(html, /目标厚度/);
  assert.match(html, /产品宽度/);
  assert.match(html, /H14 \/ 半硬/);
  assert.match(html, /H16 \/ 3\/4硬/);
  assert.match(html, /H18 \/ 全硬/);
  assert.match(html, /H22 \/ 1\/4硬（部分退火）/);
  assert.match(html, /H24 \/ 半硬（部分退火）/);
  assert.match(html, /H26 \/ 3\/4硬（部分退火）/);
  assert.doesNotMatch(html, /使用方式|主操确认|参数不自动下发|不自动写入设备|系统正常|系统检查中|确认后使用|填写6项条件|高速数据推荐|待计算/);
  assert.doesNotMatch(html, /正常范围最少/);
  assert.doesNotMatch(html, /流程框架|算法待接入|算法接口|接口预留|开发与验收|PREVIEW|RESERVED/);
  assert.doesNotMatch(html, /高速稳定运行辊缝参考/);
  assert.doesNotMatch(html, /直接计算首设参数/);
  assert.doesNotMatch(html, /一键下发|下发到PLC|自动写PLC/);
  assert.doesNotMatch(html, /生产首页|卷道次记录|生产卡片录入|审计记录/);
});

test("capture page keeps three factory actions and the 1–5 pass selector without manual identifiers", async () => {
  const response = await fetchApp("/capture");
  assert.equal(response.status, 200);
  const html = await response.text();
  for (const text of ["拍照记录", "拍照", "开始", "结束", "本道全部完成时按下"]) {
    assert.ok(html.includes(text), text);
  }
  assert.equal((html.match(/<button\b/g) ?? []).length, 8);
  assert.ok(html.includes("选择当前道次"));
  assert.doesNotMatch(html, /从相册选择|选择相册|卷号|来料状态|最近记录|导出记录|<select\b/);
});

test("removed application pages return not found", async () => {
  for (const pathname of ["/recommendations", "/records", "/products", "/dat", "/audit"]) {
    const response = await fetchApp(pathname);
    assert.equal(response.status, 404, pathname);
  }
});

test("deployed worker computes the frozen direct model", async () => {
  const health = await fetchApp("/api/health", { headers: { accept: "application/json" } });
  assert.equal(health.status, 200);
  assert.equal((await health.json()).status, "ok");

  const response = await fetchApp("/api/recommendations", {
    method: "POST",
    headers: { accept: "application/json", "content-type": "application/json" },
    body: JSON.stringify({
      entry_thickness: 1.5,
      target_exit_thickness: 0.77,
      incoming_width: 1213,
    }),
  });
  assert.equal(response.status, 200);
  const result = await response.json();
  assert.equal(result.model_version, "direct-ridge-startup-flatness-v0.4.1");
  assert.equal(result.status, "recommendable");
  assert.equal(result.result_payload.can_apply, false);
  assert.equal(result.result_payload.similar_cases.length, 0);
  assert.equal(result.result_payload.parameters.length, 6);
  assert.deepEqual(
    result.result_payload.parameters.map((row) => row.code),
    [
      "intermediate_thickness_set_mode",
      "uncoiler_tension_set_median",
      "interstand_tension_set_median",
      "coiler_tension_set_median",
      "tm1_closed_gap_set_median",
      "tm2_closed_gap_set_median",
    ],
  );
  const intermediate = result.result_payload.parameters.find(
    (row) => row.code === "intermediate_thickness_set_mode",
  );
  assert.equal(intermediate.reference, 1.0614);
  const f1Gap = result.result_payload.parameters.find(
    (row) => row.code === "tm1_closed_gap_set_median",
  );
  assert.equal(f1Gap.unit, "mm");
  assert.equal(f1Gap.label, "F1推荐辊缝");
  assert.equal(f1Gap.reference, 1.3369);
  assert.equal(f1Gap.final_test_mae, 0.1499);
  const f2Gap = result.result_payload.parameters.find(
    (row) => row.code === "tm2_closed_gap_set_median",
  );
  assert.equal(f2Gap.label, "F2推荐辊缝");
  const joinedRisks = result.result_payload.risks.join(" ");
  assert.match(joinedRisks, /高速稳定生产段.*(训练|学习)得到/);
  assert.doesNotMatch(joinedRisks, /保守首设|入口在线实测厚度可由DAT自动传入/);
  const uncoilerTension = result.result_payload.parameters.find(
    (row) => row.code === "uncoiler_tension_set_median",
  );
  assert.equal(uncoilerTension.unit, "T");
  assert.equal(result.result_payload.flatness_monitor.zone_count, 49);
  assert.equal(result.result_payload.flatness_monitor.actionable, false);
  assert.ok(
    result.result_payload.flatness_monitor.metrics.every(
      (metric) => metric.specification_predictable === false,
    ),
  );
});

test("out-of-range specification is rejected without neighbour lookup", async () => {
  const response = await fetchApp("/api/recommendations", {
    method: "POST",
    headers: { accept: "application/json", "content-type": "application/json" },
    body: JSON.stringify({
      entry_thickness: 10,
      target_exit_thickness: 1,
      incoming_width: 1213,
    }),
  });
  assert.equal(response.status, 200);
  const result = await response.json();
  assert.equal(result.status, "insufficient_data");
  assert.equal(result.result_payload.can_apply, false);
  assert.equal(result.result_payload.parameters.length, 0);
  assert.equal(result.result_payload.similar_cases.length, 0);
});

async function requestFactoryPlan(overrides = {}) {
  const response = await fetchApp("/api/process-plan", {
    method: "POST",
    headers: { accept: "application/json", "content-type": "application/json" },
    body: JSON.stringify({ alloy: "1060", incoming_state: "annealed", entry_thickness: 6,
      target_thickness: .7, width: 1280, target_state: "h18", ...overrides }),
  });
  const result = await response.json();
  assert.equal(response.status, 200, JSON.stringify(result));
  return result;
}

test("worker returns one complete pass without imposing a finishing cap", async () => {
  const result = await requestFactoryPlan({entry_thickness:6.4,target_thickness:2.2});
  assert.equal(result.plannerVersion,"factory-route-v0.7.2");
  assert.equal(result.passCount,1);
  assert.equal(result.finishingBand,"single_pass");
  assert.ok(Math.abs(result.passes[0].f1MinusF2PercentPoints-5)<1e-7);
  assert.ok(result.passes[0].f1ReductionPercent>38);
  assert.ok(result.passes[0].f1Force.safetyTotal<=1000);
});

test("worker delivers both the feasible two-pass and finishing-preferred three-pass route", async () => {
  const result=await requestFactoryPlan();
  assert.equal(result.passCount,3);
  assert.equal(result.minimumFeasiblePassCount,2);
  assert.equal(result.alternatives[0].passCount,2);
  assert.equal(result.alternatives[0].passes.at(-1).exitThickness,.7);
  assert.ok(result.selection.rankingOrder.every(key=>!/load|force/.test(key)));
  assert.equal(result.anneal.intermediate,"excluded");
  assert.equal(result.anneal.reviewAfterPass,null);
});

test("worker applies 3003 opening cap and independent five-point hardened taper", async () => {
  for(const incoming_state of ["annealed","h18"]) {
    const result=await requestFactoryPlan({alloy:"3003",incoming_state});
    for(const option of [result,...result.alternatives]) {
      assert.ok(option.passes[0].f1ReductionPercent<=45+1e-7);
      assert.equal(option.passes.at(-1).exitThickness,.7);
      for(const [i,pass] of option.passes.entries()) {
        assert.ok(pass.f1Force.safetyTotal<=1000&&pass.f2Force.safetyTotal<=1000);
        const tensions=pass.parameters.filter(p=>p.unitTension!=null);
        assert.ok(tensions.every(p=>p.value>=0&&p.value<=14));
        assert.ok(tensions[0].unitTension<=tensions[1].unitTension&&tensions[1].unitTension<=tensions[2].unitTension);
        if(i) for(const key of ["f1ReductionPercent","f2ReductionPercent"]) {
          if(incoming_state==="annealed"&&i===1&&key==="f1ReductionPercent") continue;
          const drop=option.passes[i-1][key]-pass[key];
          assert.ok(drop>=3-1e-7&&drop<=7+1e-7);
        }
      }
    }
  }
});

test("0.1 to below 0.285 mm routes never fabricate unsupported parameters", async () => {
  for(const target_thickness of [.1,.17,.22]) {
    const result=await requestFactoryPlan({entry_thickness:7,target_thickness});
    assert.equal(result.parameterRecommendationAvailable,false);
    assert.match(result.parameterUnavailableReason,/仅提供厚度路线和压下率/);
    for(const option of [result,...result.alternatives]) {
      assert.equal(option.assessment.equipment,"not_evaluated");
      assert.equal(option.passes.at(-1).exitThickness,target_thickness);
      assert.ok(option.passes.every(p=>p.parameters.length===0&&p.f1Force===null&&p.f2Force===null));
    }
  }
});

test("incoming material state changes force and may select a safer supported gauge chain", async () => {
  const requestPlan = async (incomingState) => {
    const response = await fetchApp("/api/process-plan", {
      method: "POST",
      headers: { accept: "application/json", "content-type": "application/json" },
      body: JSON.stringify({
        alloy: "1060",
        incoming_state: incomingState,
        entry_thickness: 6,
        target_thickness: 0.7,
        width: 1200,
        target_state: "hard",
      }),
    });
    assert.equal(response.status, 200);
    return response.json();
  };
  const annealed = await requestPlan("annealed");
  const unannealed = await requestPlan("h18");
  assert.equal(annealed.modelVersion, "high-speed-material-state-v0.6.1");
  assert.equal(annealed.passes.at(-1).exitThickness, 0.7);
  assert.equal(unannealed.passes.at(-1).exitThickness, 0.7);
  assert.notEqual(annealed.passes[0].f1Force.total, unannealed.passes[0].f1Force.total);
  assert.notEqual(annealed.passes[0].f1Force.materialFactor, unannealed.passes[0].f1Force.materialFactor);
  assert.ok(unannealed.passes.every((pass) => pass.f2ReductionPercent <= 50));
});

test("H22 H24 H26 apply bounded force proxies and may select an equipment-safer split", async () => {
  const results = [];
  for (const incomingState of ["h22", "h24", "h26"]) {
    const response = await fetchApp("/api/process-plan", {
      method: "POST",
      headers: { accept: "application/json", "content-type": "application/json" },
      body: JSON.stringify({
        alloy: "1060",
        incoming_state: incomingState,
        entry_thickness: 1.5,
        target_thickness: 0.77,
        width: 1213,
        target_state: incomingState,
      }),
    });
    assert.equal(response.status, 200, incomingState);
    results.push(await response.json());
  }
  assert.ok(results.every((plan) => plan.passCount === 1));
  assert.ok(results.every((plan) => plan.passes[0].entryThickness === 1.5));
  assert.ok(results.every((plan) => plan.passes[0].exitThickness === 0.77));
  assert.ok(results.every((plan) => plan.passes[0].f1ReductionPercent > plan.passes[0].f2ReductionPercent));
  assert.ok(results.every((plan) => plan.passes[0].f1Force.safetyTotal <= 1000));
  assert.ok(results.every((plan) => plan.passes[0].f2Force.safetyTotal <= 1000));
  const factors = results.map((plan) => plan.passes[0].f1Force.materialFactor);
  assert.ok(factors[0] <= factors[1] && factors[1] <= factors[2]);
  assert.ok(factors.every((value) => value >= 0.85 && value <= 1.15));
  assert.ok(results.every((plan) => plan.anneal.final === "partial_anneal_recipe_required"));
});

test("runtime flatness endpoint evaluates 49-zone data without issuing controls", async () => {
  const profile = Array.from({ length: 49 }, (_, index) =>
    index >= 8 && index <= 40 ? Math.sin((index - 8) / 5) * 3 + 0.2 : 0,
  );
  const response = await fetchApp("/api/flatness/evaluate", {
    method: "POST",
    headers: { accept: "application/json", "content-type": "application/json" },
    body: JSON.stringify({ profile }),
  });
  assert.equal(response.status, 200);
  const result = await response.json();
  assert.equal(result.status, "observed_not_actionable");
  assert.equal(result.actionable, false);
  assert.equal(result.profile_count, 1);
  assert.ok(result.active_zone_count >= 30);
  assert.equal(result.metrics.length, 4);
  assert.match(result.warnings.join(" "), /不生成自动/);
});

test("finished source has no starter marker", async () => {
  const packageJson = await readFile(new URL("package.json", root), "utf8");
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
});

test("local trial export keeps the fields needed to align operator input with DAT time", async () => {
  const source = await readFile(new URL("../app/lib/trial-log.ts", import.meta.url), "utf8");
  for (const field of [
    "generated_at_local", "generated_at_iso", "generated_at_epoch_ms", "time_zone",
    "model_version", "alloy", "incoming_state", "target_state", "entry_thickness_mm",
    "target_thickness_mm", "width_mm", "pass_index", "f1_gap_position_mm",
    "f2_gap_position_mm", "f1_recommended_force_total_t", "f2_recommended_force_total_t", "f1_material_factor",
    "f2_material_factor", "f1_physical_check_force_t", "f2_physical_check_force_t",
    "f1_constitutive_mean_flow_stress_mpa", "f2_constitutive_mean_flow_stress_mpa",
    "uncoiler_unit_tension_kn_mm2", "interstand_unit_tension_kn_mm2",
    "coiler_unit_tension_kn_mm2",
  ]) {
    assert.match(source, new RegExp(field));
  }
});
