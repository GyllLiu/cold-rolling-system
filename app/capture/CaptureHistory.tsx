"use client";

import PhotoFrame from "./PhotoFrame";
import { memo, useEffect, useState } from "react";
import { RecognitionFields, recognitionStatuses as statuses, type PhotoDetails } from "./RecognitionResult";
type History = { date: string; total: number; items: PhotoDetails[]; has_more: boolean };
type ReviewDraft = { batch_no: string; alloy: string; card_state: string; model_alloy: string };
const reviewDraft = (item: PhotoDetails): ReviewDraft => ({
  batch_no: String(item.fields.batch_no || ""), alloy: String(item.fields.alloy || ""),
  card_state: String(item.fields.card_state || ""),
  model_alloy: item.reviewed_model_alloy || (/^\d{4}$/.test(String(item.fields.alloy || "")) ? String(item.fields.alloy) : ""),
});
const dateFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});
const time = (value: string | null) => value ? dateFormatter.format(new Date(value)) : "—";

export default memo(function CaptureHistory({ revision, onLocked, onResume, canResume, onChanged }: {
  revision: number; onLocked: () => void; onResume: (photo: PhotoDetails) => void; canResume: boolean; onChanged: () => void;
}) {
  const [includeReplaced, setIncludeReplaced] = useState(false);
  const [day, setDay] = useState("");
  const [page, setPage] = useState(0);
  const [data, setData] = useState<History | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<PhotoDetails | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [retryRevision, setRetryRevision] = useState(0);
  const [review, setReview] = useState<ReviewDraft | null>(null);
  const [reviewNotice, setReviewNotice] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        const params = new URLSearchParams({ offset: String(page * 30), limit: "30" });
        if (includeReplaced) params.set("include_replaced", "true");
        if (day) params.set("day", day);
        const response = await fetch(`/api/capture/history?${params}`, { signal: controller.signal, cache: "no-store" });
        if (response.status === 401) { onLocked(); return; }
        const value = await response.json();
        if (!response.ok) throw new Error(typeof value.detail === "string" ? value.detail : "历史记录加载失败");
        setData(value); setError("");
        const waiting = value.items.some((item: PhotoDetails) => !item.replaced_by_photo_id && ["queued", "processing"].includes(item.ocr_status));
        timer = setTimeout(load, waiting ? 3000 : 30000);
      } catch (cause) {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "历史记录加载失败");
      }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [day, page, revision, retryRevision, onLocked, includeReplaced]);

  useEffect(() => {
    if (!selected) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        const response = await fetch(`/api/capture/photos/${selected}/details`, { signal: controller.signal, cache: "no-store" });
        if (response.status === 401) { onLocked(); return; }
        if (!response.ok) throw new Error("照片详情加载失败");
        const value = await response.json();
        setDetail(value);
        if (!value.replaced_by_photo_id && ["queued", "processing"].includes(value.ocr_status)) timer = setTimeout(load, 3000);
      } catch (cause) {
        if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : "加载失败");
      }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [selected, retryRevision, onLocked]);

  async function retry() {
    if (!selected) return;
    setBusy(true); setError("");
    try {
      const response = await fetch(`/api/capture/photos/${selected}/recognize`, { method: "POST" });
      if (response.status === 401) { onLocked(); return; }
      const value = await response.json();
      if (!response.ok) throw new Error(value.detail || "提交识别失败");
      setRetryRevision(value => value + 1);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "提交失败"); }
    finally { setBusy(false); }
  }

  async function confirmReview(event: React.FormEvent) {
    event.preventDefault();
    if (!detail || !review || busy) return;
    setBusy(true); setError(""); setReviewNotice("");
    try {
      const response = await fetch(`/api/capture/photos/${detail.photo_id}/review`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ expected_fields: {
          batch_no: detail.fields.batch_no || null, alloy: detail.fields.alloy || null,
          card_state: detail.fields.card_state || null,
        }, ...review, model_alloy: review.model_alloy || null }),
      });
      if (response.status === 401) { onLocked(); return; }
      const value = await response.json();
      if (!response.ok) throw new Error(value.detail || "核对结果保存失败");
      setDetail(value); setReview(null); setReviewNotice("已保存人工核对结果");
      onChanged();
    } catch (cause) { setError(cause instanceof Error ? cause.message : "核对失败"); }
    finally { setBusy(false); }
  }

  async function rotate() {
    if (!detail || busy) return;
    setBusy(true); setError("");
    try {
      const rotation = (detail.display_rotation + 90) % 360;
      const response = await fetch(`/api/capture/photos/${detail.photo_id}/rotation`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rotation, expected_rotation: detail.display_rotation }),
      });
      if (response.status === 401) { onLocked(); return; }
      const value = await response.json();
      if (!response.ok) throw new Error(value.detail || "转正失败");
      setDetail({ ...detail, display_rotation: value.display_rotation });
      onChanged();
      setRetryRevision(value => value + 1);
    } catch (cause) { setError(cause instanceof Error ? cause.message : "转正失败"); }
    finally { setBusy(false); }
  }

  return <section className="capture-history" aria-label="拍照历史">
    <div className="capture-history-heading"><h2>拍照历史</h2>
      <div className="capture-date-filter"><label>拍照日期<input type="date" value={day || data?.date || ""} onChange={event => {
        setDay(event.target.value); setPage(0); setData(null);
      }} /></label><button onClick={() => { setDay(""); setPage(0); setData(null); setRetryRevision(value => value + 1); }}>今天</button></div>
    </div>
    <p className="capture-history-meta">北京时间 · 从新到旧{data ? ` · ${data.total} 张` : ""}</p>
    <label className="capture-history-toggle"><input type="checkbox" checked={includeReplaced} onChange={event => {
      setIncludeReplaced(event.target.checked); setPage(0); setData(null);
    }} />显示已被重拍替换的旧照片</label>
    {error && <p className="capture-error" role="alert">{error}</p>}
    {!data ? <p>正在加载…</p> : !data.items.length ? <p className="capture-history-empty">当天还没有拍照记录</p> :
      <div className="capture-history-grid">{data.items.map(item => <button className="capture-history-card" key={item.photo_id} onClick={() => { setSelected(item.photo_id); setDetail(item); setReview(null); setReviewNotice(""); setError(""); }}>
        <span className="capture-history-thumb"><PhotoFrame src={item.thumbnail_url} alt={`生产随行卡 ${time(item.captured_at)}`} loading="lazy" rotation={item.display_rotation} /></span>
        <div><strong>{time(item.captured_at)}</strong><span className="capture-status-badge" data-status={item.reviewed_at ? "reviewed" : item.ocr_status}>{item.reviewed_at ? "已人工核对" : statuses[item.ocr_status] || "等待识别"}</span>
          <RecognitionFields fields={item.fields} />
          <small>{item.selected_pass_no ? `第 ${item.selected_pass_no} 道 · ` : ""}{item.replaced_by_photo_id ? "已被重拍替换" : item.ended_at ? "本道已完成" : item.started_at ? "本道进行中" : "尚未开始"}</small></div>
      </button>)}</div>}
    {data && (page > 0 || data.has_more) && <div className="capture-pagination">
      <button disabled={page === 0} onClick={() => { setPage(value => value - 1); setData(null); }}>上一页</button>
      <span>第 {page + 1} 页</span><button disabled={!data.has_more} onClick={() => { setPage(value => value + 1); setData(null); }}>下一页</button>
    </div>}
    {selected && detail && <div className="capture-detail-overlay" role="dialog" aria-modal="true" aria-label="照片与识别结果" onKeyDown={event => { if (event.key === "Escape" && !busy) setSelected(null); }}>
      <div className="capture-detail"><div className="capture-detail-heading"><h2>照片与识别结果</h2><button autoFocus disabled={busy} onClick={() => setSelected(null)}>关闭</button></div>
        <div className="capture-detail-grid"><div className="capture-detail-photo"><PhotoFrame src={detail.photo_url} rotation={detail.display_rotation} alt="生产随行卡" />
          <div className="capture-photo-tools"><button disabled={busy} onClick={rotate}>旋转 90°</button><a href={detail.photo_url} target="_blank" rel="noreferrer">查看原文件</a></div></div>
          <div><p className="capture-status-badge" data-status={detail.reviewed_at ? "reviewed" : detail.ocr_status}>{detail.reviewed_at ? "已人工核对" : statuses[detail.ocr_status]}</p>
            <RecognitionFields fields={detail.fields} />
            {detail.reviewed_at && <p className="capture-result-hint">已对照原图核对三项信息。{detail.labels_ready ? `实际牌号：${detail.reviewed_model_alloy}；与 DAT 匹配仍需校验时间和规格。` : "实际牌号或状态尚待确认，不影响拍照记录。"}</p>}
            {!detail.replaced_by_photo_id && !review && <button className="capture-retry" disabled={busy || ["queued", "processing"].includes(detail.ocr_status)} onClick={() => { setReview(reviewDraft(detail)); setReviewNotice(""); }}>核对 / 更正三项信息</button>}
            {review && <form className="capture-review-form" onSubmit={confirmReview}>
              <strong>对照左侧原图逐项确认</strong>
              <label>批号<input required inputMode="numeric" maxLength={100} value={review.batch_no} onChange={event => setReview({ ...review, batch_no: event.target.value })} /></label>
              <label>牌号原文<input required maxLength={100} value={review.alloy} onChange={event => setReview({ ...review, alloy: event.target.value })} /></label>
              <label>状态<input required maxLength={100} value={review.card_state} onChange={event => setReview({ ...review, card_state: event.target.value })} /></label>
              <label>实际牌号 · 用于模型<input inputMode="numeric" maxLength={4} placeholder="复合牌号不确定时可留空" value={review.model_alloy} onChange={event => setReview({ ...review, model_alloy: event.target.value })} /></label>
              <small>“1050标1060”等复合写法请保留在牌号原文；实际牌号由现场核对后填写，不确定可留空。状态 O 请勿写成数字 0。</small>
              <div><button type="button" disabled={busy} onClick={() => setReview(null)}>取消</button><button type="submit" disabled={busy}>{busy ? "保存中…" : "确认并保存"}</button></div>
            </form>}
            {reviewNotice && <p className="capture-review-saved" role="status">{reviewNotice}</p>}
            <dl className="capture-result"><div><dt>道次</dt><dd>{detail.selected_pass_no ? `第 ${detail.selected_pass_no} 道` : "未标注"}</dd></div><div><dt>拍照时间</dt><dd>{time(detail.captured_at)}</dd></div>
              <div><dt>开始时间</dt><dd>{time(detail.started_at)}</dd></div><div><dt>结束时间</dt><dd>{time(detail.ended_at)}</dd></div>
            </dl>
            {detail.replaced_by_photo_id && <p className="capture-review-note">此照片已被重拍替换，仅保留用于追溯。</p>}
            {!detail.started_at && !detail.replaced_by_photo_id && <button className="capture-retry" disabled={busy || !canResume} onClick={() => { onResume(detail); setSelected(null); }}>使用此照片 / 重拍</button>}
            {detail.reviewed_at && detail.warnings.length > 0 && <p className="capture-result-hint">下方是自动识别时的提醒，三项信息已人工核对。</p>}
            {detail.warnings.map(warning => <p className="capture-review-note" key={warning}>{warning}</p>)}
            {detail.ocr_error && <p className="capture-error">{detail.ocr_error}</p>}
            {detail.fields.raw_text && <details><summary>查看识别原文</summary><pre className="capture-raw-text">{String(detail.fields.raw_text)}</pre></details>}
            {!detail.replaced_by_photo_id && ["succeeded", "needs_review", "failed"].includes(detail.ocr_status) && <button className="capture-retry" disabled={busy} onClick={retry}>{busy ? "正在提交…" : "重新识别"}</button>}
            {error && <p className="capture-error" role="alert">{error}</p>}
          </div></div>
      </div>
    </div>}
  </section>;
});
