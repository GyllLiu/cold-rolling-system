"use client";

import { FormEvent, useState } from "react";
import { api } from "./lib/api";
import type { Recommendation, RecommendationParameter } from "./lib/types";
import { ConfidenceGauge, Field, PageHeader, StatusPill } from "./ui/components";

const groups = [
  {
    role: "thickness_control",
    title: "厚度核心",
    note: "中间厚度是两机架压下分配的核心预测。",
  },
  {
    role: "machine_setup",
    title: "机器首设",
    note: "综合辊缝、三段张力、弯辊与倾斜量，均由回归方程直接计算。",
  },
  {
    role: "secondary_reference",
    title: "次要参考",
    note: "速度不参与厚度目标加权；厚度调好后再由主操确认高速值。",
  },
  {
    role: "monitor_only",
    title: "负荷校核",
    note: "轧制力只用于检查预测工况是否合理，不是写入参数。",
  },
] as const;

export default function RecommendationsPage() {
  const [result, setResult] = useState<Recommendation | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [form, setForm] = useState({
    entry_thickness: "",
    target_exit_thickness: "",
    incoming_width: "",
  });

  async function submit(event: FormEvent) {
    event.preventDefault();
    setLoading(true);
    setError("");
    setResult(null);
    try {
      const response = await api<Recommendation>("/api/recommendations", {
        method: "POST",
        body: JSON.stringify({
          entry_thickness: Number(form.entry_thickness),
          target_exit_thickness: Number(form.target_exit_thickness),
          incoming_width: Number(form.incoming_width),
        }),
      });
      setResult(response);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "生成失败");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="page-stack">
      <PageHeader
        eyebrow="直接预测"
        title="冷二联首设参数"
        description="现场只填三个会影响模型计算的规格。批号、卷号、牌号和纸卡不会参与机器参数预测，因此这里不要求工人填写。"
      />
      <div className="content-grid recommendation-layout">
        <form className="panel form-panel" onSubmit={submit}>
          <div className="panel-heading">
            <div>
              <span className="kicker">INPUT / DAT SPEC</span>
              <h2>三个必要输入</h2>
            </div>
            <StatusPill tone="info">固定方程</StatusPill>
          </div>

          <div className="card-picker-note">
            <b>不再匹配历史卷</b>
            <span>输入规格后，模型直接计算 11 项首设和 2 项负荷校核。</span>
          </div>

          <div className="form-grid recommendation-input-grid">
            <Field label="入口厚度" hint="mm">
              <input
                type="number"
                min="0.01"
                max="20"
                step="0.001"
                inputMode="decimal"
                value={form.entry_thickness}
                onChange={(event) => setForm({ ...form, entry_thickness: event.target.value })}
                placeholder="例如 1.500"
                required
              />
            </Field>
            <Field label="目标出口厚度" hint="mm">
              <input
                type="number"
                min="0.01"
                max="20"
                step="0.001"
                inputMode="decimal"
                value={form.target_exit_thickness}
                onChange={(event) => setForm({ ...form, target_exit_thickness: event.target.value })}
                placeholder="例如 0.770"
                required
              />
            </Field>
            <Field label="来料宽度" hint="mm">
              <input
                type="number"
                min="1"
                max="5000"
                step="1"
                inputMode="numeric"
                value={form.incoming_width}
                onChange={(event) => setForm({ ...form, incoming_width: event.target.value })}
                placeholder="例如 1213"
                required
              />
            </Field>
          </div>

          <div className="form-context">
            入口在线实测厚度可由 DAT 自动传入；当前手工页面未接设备时，后台自动使用入口设定厚度，不增加现场填写项。
          </div>
          {error ? <div className="alert alert-danger">{error}</div> : null}
          <button className={`button button-primary button-block ${loading ? "button-loading" : ""}`} disabled={loading}>
            {loading ? "正在执行直接预测…" : "直接计算首设参数"}
          </button>
        </form>

        <section className={`panel result-panel result-cabin ${result || loading ? "has-output" : ""}`}>
          <div className="cabin-scanline" aria-hidden />
          <div className="panel-heading cabin-heading">
            <div>
              <span className="kicker">OUTPUT / DIRECT MODEL</span>
              <h2>预测值与验证误差区间</h2>
            </div>
            {result ? (
              <StatusPill tone={result.status === "recommendable" ? "success" : "warning"}>
                {statusText(result.status)}
              </StatusPill>
            ) : (
              <StatusPill tone="neutral">待命</StatusPill>
            )}
          </div>

          {loading ? (
            <div className="result-stack cabin-loading">
              <div className="confidence-row cabin-confidence">
                <ConfidenceGauge value={null} />
                <div>
                  <h3>正在计算固定回归方程…</h3>
                  <p>不检索历史案例，不做加权平均</p>
                  <div className="cabin-loading-bar" aria-hidden><i /></div>
                </div>
              </div>
              <div className="parameter-chips">
                {Array.from({ length: 4 }, (_, index) => (
                  <div className="parameter-chip is-skeleton" key={index}>
                    <span className="chip-label skeleton skeleton-line short" />
                    <strong className="skeleton skeleton-value" />
                    <small className="skeleton skeleton-line" />
                  </div>
                ))}
              </div>
            </div>
          ) : !result ? (
            <div className="result-placeholder cabin-idle">
              <ConfidenceGauge value={null} />
              <h3>等待产品规格</h3>
              <p>模型只读取入口厚度、出口目标厚度和来料宽度。超出训练范围时会明确拒绝强行给值。</p>
              <div className="cabin-idle-hints">
                <span>DIRECT RIDGE</span>
                <span>0719封存验证</span>
                <span>PLC只读</span>
              </div>
            </div>
          ) : (
            <div className="result-stack">
              <div className="confidence-row cabin-confidence">
                <ConfidenceGauge value={result.confidence_score} />
                <div>
                  <h3>{result.result_payload.headline}</h3>
                  <p>
                    模型 {result.model_version} ·
                    {result.result_payload.coverage?.in_domain ? " 规格在训练覆盖内" : " 规格超出训练覆盖"}
                  </p>
                </div>
              </div>

              {result.result_payload.model ? (
                <div className="model-validation-strip">
                  <div><span>TRAIN</span><b>0712—0718</b></div>
                  <div><span>高速稳定段</span><b>{result.result_payload.model.training_case_count}</b></div>
                  <div><span>SEALED TEST</span><b>{result.result_payload.model.validation_dataset}</b></div>
                  <div>
                    <span>优于历史基线</span>
                    <b className="good-number">
                      {result.result_payload.model.direct_win_count}/{result.result_payload.model.target_count}
                    </b>
                  </div>
                </div>
              ) : null}

              {result.result_payload.parameters.length ? (
                groups.map((group) => {
                  const rows = result.result_payload.parameters.filter((row) => row.role === group.role);
                  if (!rows.length) return null;
                  return (
                    <div key={group.role}>
                      <h3 className="section-title cabin-section-title">{group.title}</h3>
                      <p className="section-note">{group.note}</p>
                      <div className="parameter-chips">
                        {rows.map((row) => <ParameterChip key={row.code} row={row} />)}
                      </div>
                    </div>
                  );
                })
              ) : (
                <div className="cabin-empty-params">当前规格未输出参数：{statusText(result.status)}。</div>
              )}

              {result.result_payload.parameters.length ? (
                <div>
                  <h3 className="section-title cabin-section-title">0719验证误差明细</h3>
                  <div className="parameter-table cabin-table">
                    <div className="parameter-head">
                      <span>参数</span>
                      <span>预测</span>
                      <span>MAE</span>
                      <span>可靠性</span>
                    </div>
                    {result.result_payload.parameters.map((row) => (
                      <div className="parameter-row" key={row.code}>
                        <span><b>{row.label}</b><small>{row.unit || "设备原单位"}</small></span>
                        <strong>{formatParam(row.reference)}</strong>
                        <span>{formatParam(row.final_test_mae)}</span>
                        <span>{reliabilityText(row.reliability)}</span>
                      </div>
                    ))}
                  </div>
                </div>
              ) : null}

              {result.result_payload.risks?.length ? (
                <div className="alert alert-warning cabin-alert">
                  <b>使用边界</b>
                  {result.result_payload.risks.map((risk) => <span key={risk}>{risk}</span>)}
                </div>
              ) : null}
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

function ParameterChip({ row }: { row: RecommendationParameter }) {
  return (
    <article className={`parameter-chip reliability-${row.reliability}`}>
      <span className="chip-label">{row.label}</span>
      <strong className="chip-value">
        {formatParam(row.reference)}
        <small>{row.unit}</small>
      </strong>
      <span className="chip-range">
        80%误差区间 {formatParam(row.low)} — {formatParam(row.high)}
      </span>
      <small>{reliabilityText(row.reliability)} · 0719 MAE {formatParam(row.final_test_mae)}</small>
    </article>
  );
}

function statusText(status: Recommendation["status"]) {
  return status === "recommendable" ? "可供主操确认" : status === "reference_only" ? "边界参考" : "超范围拒绝";
}

function reliabilityText(value: RecommendationParameter["reliability"]) {
  return value === "high" ? "高" : value === "medium" ? "中" : value === "monitor" ? "仅校核" : "低";
}

function formatParam(value: number | null | undefined) {
  if (value == null || !Number.isFinite(value)) return "—";
  if (Number.isInteger(value)) return String(value);
  return String(Number(value.toFixed(4)));
}
