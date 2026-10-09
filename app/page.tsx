"use client";

import { FormEvent, useEffect, useState } from "react";
import {
  createFrameworkPlan,
  incomingStateLabel,
  targetStateLabel,
  type IncomingState,
  type ProcessFrameworkPlan,
  type TargetState,
} from "./lib/process-framework";
import {
  getOperatingEnvelopeSummary,
  riskLabel,
  type ForcePrediction,
  type PassPlan,
} from "./lib/route-planner";
import { exportRouteTrialCsv, readRouteTrialRecords, saveRouteTrial } from "./lib/trial-log";

const alloys = ["1060", "1100", "3003"] as const;

export default function ProcessPlanningPage() {
  const [form, setForm] = useState({
    alloy: "1060",
    incomingState: "annealed" as IncomingState,
    entryThickness: "",
    targetThickness: "",
    width: "",
    targetState: "h18" as TargetState,
  });
  const [plan, setPlan] = useState<ProcessFrameworkPlan | null>(null);
  const [error, setError] = useState("");
  const [recordCount, setRecordCount] = useState(0);

  useEffect(() => {
    const timer = window.setTimeout(() => setRecordCount(readRouteTrialRecords().length), 0);
    return () => window.clearTimeout(timer);
  }, []);

  function submit(event: FormEvent) {
    event.preventDefault();
    generatePlan();
  }

  function generatePlan() {
    setError("");
    try {
      const nextPlan = createFrameworkPlan({
        alloy: form.alloy,
        incomingState: form.incomingState,
        entryThickness: Number(form.entryThickness),
        targetThickness: Number(form.targetThickness),
        width: Number(form.width),
        targetState: form.targetState,
      });
      setPlan(nextPlan);
      setRecordCount(saveRouteTrial(nextPlan));
    } catch (reason) {
      setPlan(null);
      setError(reason instanceof Error ? reason.message : "产品条件有误，请检查后重试");
    }
  }

  function reset() {
    setForm({ ...form, entryThickness: "", targetThickness: "", width: "" });
    setPlan(null);
    setError("");
  }

  return (
    <div className="mill-page">
      <header className="mill-hero">
        <div className="mill-hero-copy">
          <span className="mill-kicker">永恒科技 · 冷二联轧</span>
          <h1>工艺推演台</h1>
          <p>按整套二机架道次规划厚度、张力、辊缝与机架负荷。</p>
        </div>
        <div className="mill-hero-track" aria-hidden="true">
          <span>来料</span><i /><b><small>主变形</small>F1</b><i /><b><small>精调</small>F2</b><i /><span>成品</span>
        </div>
      </header>

      <div className="rule-strip" aria-label="本道次计算口径">
        <span><b>一个道次</b>F1 + F2 完整通过</span>
        <span><b>压下顺序</b>前重后轻</span>
        <span><b>张力口径</b>按 kN/mm² 逐段校核</span>
      </div>

      <div className="mill-workbench">
        <form className="spec-console" onSubmit={submit}>
          <header className="console-heading">
            <span>输入</span>
            <h2>本卷条件</h2>
          </header>

          <fieldset className="alloy-switch">
            <legend>牌号</legend>
            <div>
              {alloys.map((alloy) => (
                <button
                  type="button"
                  key={alloy}
                  className={form.alloy === alloy ? "is-active" : ""}
                  onClick={() => setForm({ ...form, alloy })}
                >{alloy}</button>
              ))}
            </div>
          </fieldset>

          <div className="spec-grid">
            <NumberField label="来料厚度" unit="mm" value={form.entryThickness} placeholder="6.000" onChange={(value) => setForm({ ...form, entryThickness: value })} />
            <NumberField label="目标厚度" unit="mm" value={form.targetThickness} placeholder="0.700" onChange={(value) => setForm({ ...form, targetThickness: value })} />
            <NumberField label="产品宽度" unit="mm" value={form.width} placeholder="1213" step="1" onChange={(value) => setForm({ ...form, width: value })} />
            <label className="spec-field">
              <span>来料工艺状态</span>
              <select value={form.incomingState} onChange={(event) => setForm({ ...form, incomingState: event.target.value as IncomingState })}>
                <option value="annealed">经过退火 / O态软料</option>
                <option value="unannealed">铸轧直入 / 未退火</option>
                <option value="h14">H14 / 半硬</option>
                <option value="h16">H16 / 3/4硬</option>
                <option value="h18">H18 / 全硬</option>
                <option value="h22">H22 / 1/4硬（部分退火）</option>
                <option value="h24">H24 / 半硬（部分退火）</option>
                <option value="h26">H26 / 3/4硬（部分退火）</option>
                <option value="unknown">状态未知</option>
              </select>
            </label>
            <label className="spec-field spec-field-wide">
              <span>目标状态</span>
              <select value={form.targetState} onChange={(event) => setForm({ ...form, targetState: event.target.value as TargetState })}>
                <option value="annealed">O态 / 已退火</option>
                <option value="h14">H14 / 半硬</option>
                <option value="h16">H16 / 3/4硬</option>
                <option value="h18">H18 / 全硬</option>
                <option value="h22">H22 / 1/4硬（部分退火）</option>
                <option value="h24">H24 / 半硬（部分退火）</option>
                <option value="h26">H26 / 3/4硬（部分退火）</option>
                <option value="unspecified">暂不指定</option>
              </select>
            </label>
          </div>

          {error ? <div className="console-error" role="alert">{error}</div> : null}
          <div className="console-actions">
            <button className="action-primary" type="button" onClick={generatePlan}><span>计算整道次方案</span><b>→</b></button>
            <button className="action-quiet" type="button" onClick={reset}>清空</button>
          </div>
          <div className="trial-record-bar">
            <span>本机记录 <b>{recordCount}</b></span>
            <button type="button" onClick={() => exportRouteTrialCsv()} disabled={!recordCount}>导出CSV</button>
          </div>
        </form>

        <main className="recipe-stage">
          {!plan ? <EmptyOutput /> : <PlanOutput key={plan.id} plan={plan} />}
        </main>
      </div>
    </div>
  );
}

function NumberField({ label, unit, value, placeholder, step = "0.001", onChange }: {
  label: string; unit: string; value: string; placeholder: string; step?: string; onChange: (value: string) => void;
}) {
  return <label className="spec-field">
    <span>{label}</span>
    <div className="unit-input">
      <input type="number" min="0.001" max="5000" step={step} inputMode="decimal" value={value} onChange={(event) => onChange(event.target.value)} placeholder={placeholder} required />
      <b>{unit}</b>
    </div>
  </label>;
}

function EmptyOutput() {
  return <section className="recipe-empty">
    <div className="empty-rail" aria-hidden="true"><i /><b>F1</b><i /><b>F2</b><i /></div>
    <h2>等待本卷条件</h2>
    <p>计算后按道次展开 F1 主变形、F2 精调及三段张力。</p>
  </section>;
}

function PlanOutput({ plan: original }: { plan: ProcessFrameworkPlan }) {
  const [alternativeId, setAlternativeId] = useState<number | null>(null);
  const alternative = original.alternatives?.find(item => item.candidateId === alternativeId);
  const plan = alternative ? { ...original, passes: alternative.passes, passCount: alternative.passCount,
    finishingBand: alternative.finishingBand, risk: alternative.risk, assessment: alternative.assessment } : original;
  return <div className="recipe-stack">
    <section className="recipe-banner">
      <div>
        <span className="mill-kicker">整道次工艺方案</span>
        <h2><b>{plan.input.alloy}</b> {format(plan.input.entryThickness)} <i>→</i> {format(plan.input.targetThickness)} <small>mm</small></h2>
        <p>{format(plan.input.width)} mm · {incomingStateLabel(plan.input.incomingState)} · {targetStateLabel(plan.input.targetState)}</p>
      </div>
      <div className="recipe-verdict">
        <span>{alternative ? "当前方案" : "推荐道次"}<strong>{plan.passCount}</strong></span>
        <span>总压下率<strong>{format(plan.totalReductionPercent)}<small>%</small></strong></span>
        <em className={`risk-${plan.risk}`}>数据：{plan.assessment.dataCoverage === "unavailable" ? "参数未覆盖" : riskLabel(plan.assessment.dataCoverage)}</em>
      </div>
    </section>

    {original.alternatives?.length ? <section className="route-rail-card">
      <header className="section-label"><span>同时比较</span><h2>两套道次方案</h2></header>
      <div className="route-options">
        {[{ candidateId: original.selection.selectedCandidateId, label: "末道调整优选", passCount: original.passCount,
          passes: original.passes, explanation: "末道更接近板形调整目标，质量效果仍需现场验证。" }, ...original.alternatives].map(option => {
          const isPrimary = option.candidateId === original.selection.selectedCandidateId;
          const active = isPrimary ? !alternative : alternativeId === option.candidateId;
          return <article key={option.candidateId} className={active ? "is-active" : ""}>
            <h3>{option.label} · {option.passCount}道</h3>
            <p>{format(original.input.entryThickness)} → {option.passes.map(pass => format(pass.exitThickness)).join(" → ")} mm</p>
            <p>{option.explanation}</p>
            <button type="button" aria-pressed={active} onClick={() => setAlternativeId(isPrimary ? null : option.candidateId)}>
              {active ? "正在查看" : "查看逐道参数"}
            </button>
          </article>;
        })}
      </div>
    </section> : null}
    <RouteRail plan={plan} />
    <section className="route-rail-card">
      <p>负荷校核：{plan.assessment.equipment === "not_evaluated" ? "未评估" : plan.assessment.equipment === "over_limit" ? "超出设备上限" : plan.assessment.equipment === "near_limit" ? "接近设备上限" : "在已确认力限值内"} · 工艺质量：{plan.assessment.quality === "review_required" ? "需要复核" : "满足暂定规则"}</p>
      <p>板形、轮胎印和目标状态尚无质量结果验证。负荷校核仅覆盖轧制力与张力，不代表已核准设备全部能力。</p>
      <details><summary>查看路线选择依据</summary><p>{plan.selection.explanation}</p>
        <p>负荷仅用于超限筛选，不参与候选排序。{plan.ruleSummary?.thicknessRegion.name}按0.1—7.0 mm线性三等分判断。</p>
        {plan.ruleSummary?.parameterAssumptions.map(text => <p key={text}>{text}</p>)}
        <p>检查了 {plan.selection.candidates.length} 个候选，其中 {plan.selection.candidates.filter((item) => item.accepted).length} 个通过约束。逐项淘汰原因保存在方案结果中。</p>
      </details>
    </section>
    <section className="pass-deck">
      <header className="section-label"><span>逐道展开</span><h2>{plan.parameterRecommendationAvailable ? "参数建议" : "厚度与压下率"}</h2></header>
      {plan.passes.map((pass) => <PassCard
        key={pass.index}
        pass={pass}
        isFinal={pass.index === plan.passCount}
        finishingBand={plan.finishingBand}
      />)}
    </section>
    <AnnealCard plan={plan} />
    <EnvelopeCard />
  </div>;
}

function RouteRail({ plan }: { plan: ProcessFrameworkPlan }) {
  const points = [
    { label: "来料", value: plan.input.entryThickness },
    ...plan.passes.map((pass) => ({ label: pass.index === plan.passCount ? "成品" : `第${pass.index}道`, value: pass.exitThickness })),
  ];
  return <section className="route-rail-card">
    <header className="section-label">
      <div><span>厚度路径</span><h2>工艺路线</h2></div>
    </header>
    <div className="route-rail">
      {points.map((point, index) => <div className="gauge-point" key={`${point.label}-${index}`}>
        <span>{String(index).padStart(2, "0")}</span>
        <i />
        <b>{point.value < 1 ? point.value.toFixed(3) : format(point.value)}</b>
        <small>{point.label} · mm</small>
      </div>)}
    </div>
  </section>;
}

function PassCard({ pass, isFinal, finishingBand }: {
  pass: PassPlan;
  isFinal: boolean;
  finishingBand: ProcessFrameworkPlan["finishingBand"];
}) {
  const middle = pass.parameters.find((parameter) => parameter.code === "intermediate_thickness_set_mode");
  const settings = pass.parameters.filter((parameter) => parameter.code !== "intermediate_thickness_set_mode");
  const constraintText = isFinal
    ? finishingBand === "single_pass"
      ? "单道完成 · 两架相差5个百分点"
      : finishingBand === "preferred"
      ? "末道优选区"
      : finishingBand === "review"
        ? "末道复核区"
        : "末道轻压"
    : pass.index === 1
      ? "首道优先承担压下"
      : "同机架逐道约降5个百分点";
  return <article className={`pass-module risk-frame-${pass.risk}`}>
    <header className="pass-heading">
      <div className="pass-number"><small>PASS</small><b>{String(pass.index).padStart(2, "0")}</b></div>
      <div className="pass-gauge">
        <span>{format(pass.entryThickness)}</span><i>F1</i><strong>{format(pass.intermediateThickness)}</strong><i>F2</i><span>{format(pass.exitThickness)}</span><small>mm</small>
      </div>
      <em className={`risk-badge risk-${pass.risk}`}>{pass.assessment.dataCoverage === "unavailable" ? "负荷未评估" : `数据：${riskLabel(pass.assessment.dataCoverage)}`}</em>
    </header>

    <div className="reduction-band">
      <div><span>本道压下</span><b>{format(pass.totalReductionPercent)}%</b></div>
      <div><span>F1 主变形</span><b>{format(pass.f1ReductionPercent)}%</b></div>
      <div><span>F2 精调</span><b>{format(pass.f2ReductionPercent)}%</b></div>
      <div><span>中间厚度</span><b>{format(middle?.value ?? pass.intermediateThickness)} mm</b></div>
    </div>

    <div className={`stand-relation ${pass.f1MinusF2PercentPoints > 0 ? "is-aligned" : "is-review"}`}>
      <span>机架分配</span>
      <b>F1 比 F2 {pass.f1MinusF2PercentPoints > 0 ? `多压 ${format(pass.f1MinusF2PercentPoints)} 个百分点` : "未形成主次关系"}</b>
      <em>{constraintText}</em>
    </div>
    {pass.materialPolicy ? <p className="pass-material-note">F1来料：{pass.materialPolicy.f1.stageCondition} · F2来料：{pass.materialPolicy.f2.stageCondition}</p> : null}

    {pass.parameterStatus === "route_only" ? <div className="route-only-note">
      <span>张力、辊缝、轧制力</span><b>暂不推荐</b>
    </div> : <>
      <div className="setting-strip">
        {settings.map((parameter) => <article key={parameter.code} className={`setting-tile risk-line-${parameter.risk}`}>
          <span>{parameter.label}</span>
          <strong>{format(parameter.value)}<small>{parameter.unit}</small></strong>
          {parameter.unitTension != null ? <small className="unit-tension-value">{format(parameter.unitTension)} kN/mm²</small> : null}
          <em>历史绝对误差P95 {format(parameter.p95Error)}</em>
        </article>)}
      </div>

      {pass.f1Force && pass.f2Force ? <div className="force-bay">
        <ForceStation label="F1" force={pass.f1Force} />
        <ForceStation label="F2" force={pass.f2Force} />
      </div> : null}
    </>}
  </article>;
}

function ForceStation({ label, force }: { label: string; force: ForcePrediction }) {
  const max = Math.max(force.total, 1);
  return <section className="force-station">
    <header>
      <span>{label} 预计轧制力{Math.abs(force.materialFactor - 1) >= 0.01 ? <em>材料×{format(force.materialFactor)}</em> : null}</span>
      <strong>{format(force.total)} <small>T</small></strong>
    </header>
    <div className="force-split" aria-label={`${label}两侧轧制力`}>
      <div style={{ flexBasis: `${(force.drive / max) * 100}%` }} className={`risk-fill-${force.driveRisk}`}><span>传动侧</span><b>{format(force.drive)}</b></div>
      <div style={{ flexBasis: `${(force.operation / max) * 100}%` }} className={`risk-fill-${force.operationRisk}`}><span>操作侧</span><b>{format(force.operation)}</b></div>
    </div>
    <small>
      DAT参考 {format(force.total)} T · 物理基准 {format(force.physicsBaseline)} T · 保守校核 {format(force.safetyTotal)} / 1000 T
      （{format(force.equipmentUtilizationPercent)}%）
    </small>
    <small>含张力偏低场景 {format(force.lowTensionCheckTotal)} T；校核余量是经验筛选值。两侧为固定比例参考，不能预测偏载。</small>
  </section>;
}

function AnnealCard({ plan }: { plan: ProcessFrameworkPlan }) {
  const finalText = plan.anneal.final === "not_required" ? "不安排" :
    plan.anneal.final === "partial_anneal_recipe_required" ? `${targetStateLabel(plan.input.targetState)} · 工艺卡待补充` :
    plan.anneal.final === "plant_recipe_required" ? "厂内工艺卡待补充" :
    `${plan.anneal.temperatureC}℃ · ${plan.anneal.holdHours?.[0]}—${plan.anneal.holdHours?.[1]} h`;
  return <section className="thermal-card">
    <div className="thermal-symbol" aria-hidden="true"><i /><i /><i /></div>
    <div className="thermal-copy">
      <span>材料状态</span>
      <h2>连续轧制范围</h2>
      <p>{plan.anneal.reason}</p>
    </div>
    <div className="thermal-values">
      <span>中间退火<b>现阶段不纳入</b></span>
      <span>成品退火<b>{finalText}</b></span>
    </div>
  </section>;
}

function EnvelopeCard() {
  return <details className="envelope-drawer">
    <summary><span>高速数据范围 <em>不是设备上限</em></span><b>展开</b></summary>
    <div className="envelope-cards">
      {getOperatingEnvelopeSummary().map((row) => <article key={row.code}>
        <span>{row.label}</span>
        <b>{format(row.p05)}—{format(row.p95)} <small>{row.unit}</small></b>
        <em>警戒 {format(row.p99)} {row.unit}</em>
      </article>)}
    </div>
  </details>;
}

function format(value: number) {
  if (!Number.isFinite(value)) return "—";
  return Number.isInteger(value) ? String(value) : String(Number(value.toFixed(3)));
}
