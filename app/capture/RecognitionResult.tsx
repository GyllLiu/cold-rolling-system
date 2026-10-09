"use client";

import { useEffect, useState } from "react";

export type PhotoDetails = {
  display_rotation: number; replaces_photo_id: string | null; replaced_by_photo_id: string | null;
  photo_id: string; photo_url: string; thumbnail_url: string; captured_at: string; record_id: string | null;
  started_at: string | null; ended_at: string | null;
  selected_pass_no: number | null;
  ocr_status: string; ocr_error: string | null;
  fields: Record<string, string | boolean | null>; warnings: string[];
  reviewed_at: string | null; reviewed_model_alloy: string | null; labels_ready: boolean;
};
export const fieldLabels = { batch_no: "批号", alloy: "牌号", card_state: "状态" };
export const recognitionStatuses: Record<string, string> = {
  not_configured: "暂未启用识别", queued: "等待识别", processing: "正在识别",
  succeeded: "已识别 · 未核对", needs_review: "识别结果需核对", failed: "识别失败 · 照片已保存",
};

export function RecognitionFields({ fields }: { fields: PhotoDetails["fields"] }) {
  return <dl className="capture-core-fields">{Object.entries(fieldLabels).map(([key, label]) =>
    <div key={key}><dt>{label}</dt><dd>{typeof fields[key] === "string" && fields[key] ? fields[key] : "—"}</dd></div>,
  )}</dl>;
}

export default function RecognitionResult({ photoId, onLocked }: { photoId: string; onLocked: () => void }) {
  const [result, setResult] = useState<PhotoDetails | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    async function load() {
      try {
        const response = await fetch(`/api/capture/photos/${photoId}/details`, { signal: controller.signal, cache: "no-store" });
        if (response.status === 401) { onLocked(); return; }
        if (!response.ok) throw new Error("识别结果暂时无法加载，正在重试。照片保存不受影响。");
        const value: PhotoDetails = await response.json();
        if (controller.signal.aborted) return;
        setResult(value); setError("");
        timer = setTimeout(load, ["queued", "processing"].includes(value.ocr_status) ? 3000 : 30000);
      } catch (cause) {
        if (!controller.signal.aborted) {
          setError(cause instanceof Error ? cause.message : "识别结果加载失败，正在重试。");
          timer = setTimeout(load, 10000);
        }
      }
    }
    void load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [photoId, onLocked]);
  return <section className="capture-current-result" aria-label="当前照片识别结果" aria-live="polite">
    <div className="capture-result-heading"><strong>照片信息</strong><span className="capture-status-badge" data-status={result?.reviewed_at ? "reviewed" : result?.ocr_status}>{result ? result.reviewed_at ? "已人工核对" : recognitionStatuses[result.ocr_status] || "等待识别" : "正在读取"}</span></div>
    <RecognitionFields fields={result?.fields ?? {}} />
    <p className="capture-result-hint">{error || (result?.reviewed_at ? "三项信息已对照原图核对。" : result?.ocr_status === "needs_review" ? "请对照照片核对三项信息，可在历史中查看详情。" : result?.ocr_status === "failed" ? "可在历史中重新识别，不影响开始和结束计时。" : "识别在后台进行，不影响开始和结束计时。")}</p>
  </section>;
}
