"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import CaptureHistory from "./CaptureHistory";
import PhotoFrame from "./PhotoFrame";
import type { PhotoDetails } from "./RecognitionResult";
import RecognitionResult from "./RecognitionResult";
import { PhotoUploadError, uploadPhoto, type StagedPhoto, type SavedPhoto } from "./photo-upload";
import { frameGeometry, initialRotation, nextRotation, ROTATION_STORAGE_KEY, type QuarterTurn } from "./camera-rotation";
import "./capture.css";

type RecordItem = {
  id: string; photo_id: string; photo_url: string; started_at: string; ended_at: string | null; selected_pass_no: number | null;
};
type CaptureState = { active: RecordItem | null; server_time: string };
type StartPayload = { id: string; photo_id: string; started_at: string; selected_pass_no: number };
class ApiError extends Error { constructor(message: string, readonly status: number) { super(message); } }

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api/capture${path}`, { ...init, cache: "no-store" });
  const body = await response.json();
  if (!response.ok) throw new ApiError(typeof body.detail === "string" ? body.detail : "信息不完整，请检查后重试", response.status);
  return body as T;
}
const post = (value: unknown): RequestInit => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(value) });
const formatTime = (value: string | null) => value ? new Date(value).toLocaleString("zh-CN", {
  timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
}) : "—";

export default function CapturePage() {
  const [active, setActive] = useState<RecordItem | null>(null);
  const [selectedPass, setSelectedPass] = useState<number | null>(null);
  const [photo, setPhoto] = useState<SavedPhoto | null>(null);
  const [staged, setStaged] = useState<StagedPhoto | null>(null);
  const [attemptedUpload, setAttemptedUpload] = useState(false);
  const [savedRotation, setSavedRotation] = useState<number | null>(null);
  const [uploadProgress, setUploadProgress] = useState<number | null>(null);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [loaded, setLoaded] = useState(false);
  const [locked, setLocked] = useState(false);
  const [code, setCode] = useState("");
  const [camera, setCamera] = useState(false);
  const [rotation, setRotation] = useState<QuarterTurn>(90);
  const [videoSize, setVideoSize] = useState({ width: 1920, height: 1080 });
  const [cameraSize, setCameraSize] = useState({ width: 0, height: 0 });
  const [preview, setPreview] = useState<{ url: string; stagedId?: string; photoId?: string; rotation?: number } | null>(null);
  const [historyRevision, setHistoryRevision] = useState(0);
  const lock = useCallback(() => setLocked(true), []);
  const video = useRef<HTMLVideoElement>(null);
  const cameraStage = useRef<HTMLDivElement>(null);
  const takingPhoto = useRef(false);
  const stream = useRef<MediaStream | null>(null);
  const nativeCapture = useRef<HTMLInputElement>(null);
  const pendingStart = useRef<StartPayload | null>(null);
  const pendingEnd = useRef<string | null>(null);
  const clockOffset = useRef(0);
  const uploadController = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    const sent = Date.now();
    const data = await request<CaptureState>("/state");
    clockOffset.current = new Date(data.server_time).getTime() - (sent + Date.now()) / 2;
    setActive(data.active); setLocked(false); setLoaded(true);
  }, []);

  const showError = (cause: unknown) => {
    if ((cause instanceof ApiError || cause instanceof PhotoUploadError) && cause.status === 401) setLocked(true);
    else setError(cause instanceof Error ? cause.message : "保存失败，请重试");
  };
  useEffect(() => {
    // Fetch the current server record on mount, including after a browser refresh.
    // All state updates in refresh happen after its awaited network response.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void refresh().catch(cause => {
      if (cause instanceof ApiError && cause.status === 401) setLocked(true);
      else setError(cause instanceof Error ? cause.message : "连接失败，请刷新重试");
    });
  }, [refresh]);
  useEffect(() => {
    if (camera && video.current && stream.current) video.current.srcObject = stream.current;
    return () => { if (camera) { stream.current?.getTracks().forEach(track => track.stop()); stream.current = null; } };
  }, [camera]);
  useEffect(() => {
    if (!camera || !cameraStage.current) return;
    const observer = new ResizeObserver(([entry]) => setCameraSize({ width: entry.contentRect.width, height: entry.contentRect.height }));
    observer.observe(cameraStage.current);
    return () => observer.disconnect();
  }, [camera]);
  // The local image survives closing its preview and uploading, avoiding a second network download.
  const stagedUrl = staged?.url;
  useEffect(() => () => { if (stagedUrl) URL.revokeObjectURL(stagedUrl); }, [stagedUrl]);
  useEffect(() => () => uploadController.current?.abort(), []);
  useEffect(() => {
    if (uploadProgress === null && (!staged || photo)) return;
    const protect = (event: BeforeUnloadEvent) => { event.preventDefault(); };
    window.addEventListener("beforeunload", protect);
    return () => window.removeEventListener("beforeunload", protect);
  }, [uploadProgress, staged, photo]);

  async function unlock(event: React.FormEvent) {
    event.preventDefault(); setBusy("登录中"); setError("");
    try { await request("/auth", post({ code })); setCode(""); await refresh(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : "连接失败"); }
    finally { setBusy(""); }
  }

  async function openCamera() {
    if (active || busy || (attemptedUpload && !photo)) return;
    setError(""); setNotice("");
    if (!navigator.mediaDevices?.getUserMedia) { nativeCapture.current?.click(); return; }
    try {
      stream.current = await navigator.mediaDevices.getUserMedia({ video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 } }, audio: false });
      const settings = stream.current.getVideoTracks()[0].getSettings();
      const size = { width: settings.width || 1920, height: settings.height || 1080 };
      let saved = null;
      try { saved = localStorage.getItem(ROTATION_STORAGE_KEY); } catch { /* Camera works without browser storage. */ }
      setVideoSize(size); setRotation(initialRotation(saved, size.width, size.height));
      setCamera(true);
    } catch { setError("无法打开相机，请允许此网站使用相机后重试。"); }
  }

  function rotateCamera() {
    const next = nextRotation(rotation);
    setRotation(next);
    try { localStorage.setItem(ROTATION_STORAGE_KEY, String(next)); } catch { /* Optional preference only. */ }
  }

  function stagePhoto(blob: Blob) {
    const next = { url: URL.createObjectURL(blob), blob, id: crypto.randomUUID(),
      capturedAt: new Date(Date.now() + clockOffset.current).toISOString(),
      replacesId: photo?.photo_id ?? staged?.replacesId };
    setAttemptedUpload(false); setSavedRotation(null);
    setStaged(next); setPhoto(null); pendingStart.current = null;
    setPreview({ url: next.url, stagedId: next.id });
  }

  async function savePhoto() {
    if (!staged || uploadController.current) return;
    const controller = new AbortController();
    uploadController.current = controller;
    setAttemptedUpload(true);
    setPreview(null); setUploadProgress(0); setBusy("照片上传中"); setError(""); setNotice("");
    try {
      const result = await uploadPhoto(staged, setUploadProgress, controller.signal);
      setPhoto(result); setSavedRotation(staged.displayRotation ?? null); pendingStart.current = null;
      setNotice(staged.replacesId ? "重拍已保存，旧照片已替换，可点击开始" : "照片已保存，可点击开始");
      setHistoryRevision(value => value + 1);
    } catch (cause) {
      if (!controller.signal.aborted) {
        if (cause instanceof PhotoUploadError && [400, 401, 403, 404, 409, 413, 422].includes(cause.status)) setAttemptedUpload(false);
        showError(cause);
      }
    }
    finally { uploadController.current = null; setUploadProgress(null); setBusy(""); }
  }

  async function cancelRetake() {
    if (!staged?.replacesId || attemptedUpload || busy) return;
    setBusy("恢复原照片"); setError("");
    try {
      const item = await request<PhotoDetails>(`/photos/${staged.replacesId}/details`);
      if (item.replaced_by_photo_id) throw new Error("旧照片已被替换，请刷新并从历史选择最新照片。");
      setPreview(null); setStaged(null);
      setPhoto({ photo_id: item.photo_id, photo_url: item.photo_url });
      setSavedRotation(item.display_rotation); setNotice("已取消重拍，保留原照片");
    } catch (cause) { showError(cause); }
    finally { setBusy(""); }
  }

  async function takePhoto() {
    const source = video.current;
    if (!source?.videoWidth || takingPhoto.current) return;
    takingPhoto.current = true; setBusy("照片生成中");
    try {
      const frame = frameGeometry(source.videoWidth, source.videoHeight, rotation);
      const canvas = document.createElement("canvas");
      canvas.width = frame.width; canvas.height = frame.height;
      const context = canvas.getContext("2d");
      if (!context) throw new Error("无法生成照片，请重试。");
      context.translate(frame.width / 2, frame.height / 2);
      context.rotate(frame.radians);
      context.drawImage(source, -frame.sourceWidth / 2, -frame.sourceHeight / 2, frame.sourceWidth, frame.sourceHeight);
      const blob = await new Promise<Blob | null>(resolve => canvas.toBlob(resolve, "image/jpeg", .9));
      if (!blob) throw new Error("无法生成照片，请重试。");
      setCamera(false); stagePhoto(blob);
    } catch (cause) { showError(cause); }
    finally { takingPhoto.current = false; setBusy(""); }
  }

  async function start() {
    if (!photo) { setError("请先拍照。"); return; }
    if (selectedPass === null) { setError("请先选择第 1–5 道。"); return; }
    setBusy("开始时间保存中"); setError(""); setNotice("");
    pendingStart.current ??= { id: crypto.randomUUID(), photo_id: photo.photo_id,
      started_at: new Date(Date.now() + clockOffset.current).toISOString(), selected_pass_no: selectedPass };
    try {
      const row = await request<RecordItem>("/records", post(pendingStart.current));
      setActive(row); pendingStart.current = null; pendingEnd.current = null;
      setHistoryRevision(value => value + 1);
      setNotice("开始时间已保存"); await refresh();
    } catch (cause) { showError(cause); await refresh().catch(() => {}); }
    finally { setBusy(""); }
  }

  async function end() {
    if (!active) return;
    pendingEnd.current ??= new Date(Date.now() + clockOffset.current).toISOString();
    setBusy("结束时间保存中"); setError("");
    try {
      await request(`/records/${active.id}/end`, post({ ended_at: pendingEnd.current }));
      setActive(null); setPhoto(null); setStaged(null); setSelectedPass(null); setAttemptedUpload(false); setSavedRotation(null); pendingEnd.current = null; pendingStart.current = null;
      setHistoryRevision(value => value + 1);
      setNotice("本道次已完成，记录已保存"); await refresh();
    } catch (cause) { showError(cause); await refresh().catch(() => {}); }
    finally { setBusy(""); }
  }

  const selectedPhotoId = active?.photo_id ?? photo?.photo_id;
  useEffect(() => {
    if (!selectedPhotoId) return;
    let cancelled = false;
    void request<PhotoDetails>(`/photos/${selectedPhotoId}/details`).then(value => {
      if (!cancelled) setSavedRotation(value.display_rotation);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [selectedPhotoId, historyRevision]);

  async function rotatePreview() {
    if (!preview || preview.rotation === undefined || busy) return;
    const next = nextRotation(preview.rotation as QuarterTurn);
    setError("");
    if (preview.stagedId) {
      if (attemptedUpload) return;
      setStaged(value => value ? { ...value, displayRotation: next } : value);
      setPreview({ ...preview, rotation: next });
      return;
    }
    if (!preview.photoId) return;
    setBusy("方向保存中");
    try {
      await request(`/photos/${preview.photoId}/rotation`, {
        ...post({ rotation: next, expected_rotation: preview.rotation }), method: "PUT",
      });
      setSavedRotation(next); setPreview({ ...preview, rotation: next });
      setHistoryRevision(value => value + 1);
    } catch (cause) {
      showError(cause);
      if (cause instanceof ApiError && cause.status === 409) {
        await request<PhotoDetails>(`/photos/${preview.photoId}/details`).then(value => {
          setSavedRotation(value.display_rotation);
          setPreview(current => current?.photoId === value.photo_id ? { ...current, rotation: value.display_rotation } : current);
        }).catch(() => {});
      }
    }
    finally { setBusy(""); }
  }

  function resumePhoto(item: PhotoDetails) {
    if (active || busy || item.started_at || item.replaced_by_photo_id || (attemptedUpload && !photo)) return;
    if (staged && !photo) { setError("请先保存当前待保存照片，再选择历史照片。"); return; }
    setStaged(null); setPhoto({ photo_id: item.photo_id, photo_url: item.photo_url }); setSelectedPass(null);
    setSavedRotation(item.display_rotation); setAttemptedUpload(false);
    pendingStart.current = null; setError(""); setNotice("已选择这张照片，可以重拍替换或开始本道");
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  if (locked) return <div className="capture-page"><form className="capture-access" onSubmit={unlock}>
    <h1>拍照记录</h1><label>工位访问码<input type="password" autoComplete="current-password" value={code} onChange={e => setCode(e.target.value)} required /></label>
    <button disabled={!!busy}>{busy || "进入"}</button><p role="alert">{error}</p>
  </form></div>;

  const visiblePhoto = active ? (staged?.id === active.photo_id ? staged.url : active.photo_url) : staged?.url ?? photo?.photo_url;
  const visiblePhotoId = active?.photo_id ?? photo?.photo_id;
  const unsavedPhoto = !!staged && !photo && !active;
  const sideways = rotation === 90 || rotation === 270;
  const cameraScale = Math.min(cameraSize.width / (sideways ? videoSize.height : videoSize.width),
    cameraSize.height / (sideways ? videoSize.width : videoSize.height));
  return <div className="capture-page">
    <div className="capture-heading"><div><h1>拍照记录</h1><p className="capture-subtitle">拍下随行卡，上机按开始，本道完成按结束。</p></div><span className={active ? "capture-running" : ""}>{!loaded ? "连接中" : active ? "本道进行中" : uploadProgress !== null ? "照片上传中" : photo ? "照片已保存" : unsavedPhoto ? "照片待保存" : "准备上机"}</span></div>
    <div className="capture-workspace">
      <div className="capture-photo">
        {/* Authenticated photos use the same-origin cookie and must not pass through an image CDN. */}
        {visiblePhoto ? <button className="capture-view-photo" aria-label="放大查看已拍照片" onClick={() => setPreview({ url: visiblePhoto, stagedId: unsavedPhoto && uploadProgress === null ? staged?.id : undefined, photoId: visiblePhotoId, rotation: (unsavedPhoto ? staged?.displayRotation : savedRotation) ?? undefined })}><PhotoFrame src={visiblePhoto} rotation={unsavedPhoto ? staged?.displayRotation : savedRotation} alt="本道次生产照片" /><span className="capture-photo-caption">点击照片放大查看</span></button> : <div className="capture-photo-empty"><svg viewBox="0 0 48 48" aria-hidden="true"><path d="M8 14h9l3-5h9l3 5h8v25H8z"/><circle cx="24" cy="26" r="8"/></svg><span>拍下生产随行卡</span><small>让批号、牌号、状态清晰入镜</small></div>}
        <button className="capture-shoot" onClick={openCamera} disabled={!!busy || !!active || !loaded || (attemptedUpload && !photo)}>{photo || staged ? "重拍随行卡" : "拍照"}</button>
        {unsavedPhoto && staged?.replacesId && <><p className="capture-result-hint">新照片保存成功后才替换旧照片，不会新增生产道次。</p><button className="capture-cancel-retake" disabled={!!busy || attemptedUpload} onClick={cancelRetake}>取消重拍，保留原照片</button></>}
        <input hidden ref={nativeCapture} type="file" accept="image/*" capture="environment" onChange={e => {
          const file = e.target.files?.[0]; if (file) stagePhoto(file); e.target.value = "";
        }} />
      </div>
      <div className="capture-controls">
        <div className="capture-pass-picker" role="group" aria-label="选择当前道次">
          <strong>当前道次</strong>
          <div>{[1, 2, 3, 4, 5].map(n => <button type="button" key={n}
            aria-pressed={(active?.selected_pass_no ?? selectedPass) === n}
            disabled={!!active || !!busy}
            onClick={() => {
              if (pendingStart.current) { setError("开始记录正在确认，请先重试或刷新页面。"); return; }
              setSelectedPass(n); setError("");
            }}>第 {n} 道</button>)}</div>
          {active?.selected_pass_no === null && <small>当前进行中的记录没有道次标注</small>}
        </div>
        <div className="capture-action-pair">
          <button className="capture-start" disabled={!!active || !!busy || !loaded || !photo} onClick={start}>开始<span>上机时按下</span></button>
          <button className="capture-end" disabled={!active || !!busy} onClick={end}>结束<span>本道全部完成时按下</span></button>
        </div>
        <div className="capture-time"><span>开始时间</span><b>{formatTime(active?.started_at ?? null)}</b><small>北京时间</small></div>
        {uploadProgress !== null && <div className="capture-upload-status" role="status">
          <strong>{uploadProgress < 100 ? `正在上传照片 · ${uploadProgress}%` : "照片已发送，等待服务器确认…"}</strong>
          <progress max="100" value={uploadProgress} aria-label="照片上传进度" />
          <small>可以查看历史；确认保存后即可开始。请勿关闭或刷新页面。</small>
        </div>}
        {unsavedPhoto && uploadProgress === null && <button className="capture-save-retry" disabled={!!busy} onClick={savePhoto}>{error ? "重试保存照片" : "保存照片"}</button>}
        {visiblePhotoId && <RecognitionResult key={visiblePhotoId} photoId={visiblePhotoId} onLocked={lock} />}
        <div className="capture-feedback" aria-live="polite">{busy || notice || (!loaded ? "正在连接记录服务…" : active ? "中途停机无需按结束。" : photo ? "上机时点击开始。" : unsavedPhoto ? attemptedUpload ? "请先重试确认保存结果，再开始或重拍。" : "照片尚未保存，请保存后再开始。" : "请先拍照，保存后即可开始。")}</div>
        {error && <p className="capture-error" role="alert">{error}</p>}
      </div>
    </div>
    {loaded && <CaptureHistory revision={historyRevision} onLocked={lock} onResume={resumePhoto} canResume={!active && !busy && !(!photo && staged)} onChanged={() => setHistoryRevision(value => value + 1)} />}
    {camera && <div className="capture-camera" role="dialog" aria-modal="true" aria-label="拍摄生产照片" onKeyDown={e => { if (e.key === "Escape") setCamera(false); }}>
      <p className="capture-camera-hint">将随行卡完整放入画面 · 文字不正时点击旋转</p>
      <div className="capture-camera-stage" ref={cameraStage}>
        <video ref={video} autoPlay playsInline muted onLoadedMetadata={event => {
          setVideoSize({ width: event.currentTarget.videoWidth, height: event.currentTarget.videoHeight });
        }} style={{ width: videoSize.width * cameraScale, height: videoSize.height * cameraScale,
          transform: `translate(-50%, -50%) rotate(${rotation}deg)` }} />
      </div>
      {error && <p className="capture-camera-error" role="alert">{error}</p>}
      <div className="capture-camera-actions"><button onClick={() => setCamera(false)}>关闭</button>
        <button disabled={!!busy} onClick={rotateCamera}>旋转 90°</button>
        <button className="capture-shutter" disabled={!!busy} onClick={takePhoto}>{busy || "拍照"}</button></div>
    </div>}
    {preview && <div className="capture-preview" role="dialog" aria-modal="true" aria-label="拍照预览" onKeyDown={event => { if (event.key === "Escape") setPreview(null); }}>
      <h2>{preview.stagedId ? "检查照片是否清晰" : "照片预览"}</h2>
      <PhotoFrame src={preview.url} rotation={preview.rotation} alt="拍摄效果预览" onRotationResolved={angle => setPreview(value => value && value.url === preview.url && value.rotation === undefined ? { ...value, rotation: angle } : value)} />
      {error && <p role="alert">{error}</p>}
      <div><button autoFocus disabled={!!busy} onClick={() => setPreview(null)}>关闭</button>
        {(preview.stagedId || preview.photoId) && <button disabled={!!busy || preview.rotation === undefined || (!!preview.stagedId && attemptedUpload)} onClick={rotatePreview}>旋转 90°</button>}
        {preview.photoId && <small className="capture-rotation-note">方向自动保存，不改变原始照片与生产记录</small>}
        {preview.stagedId && <><button disabled={!!busy || attemptedUpload} onClick={() => { setPreview(null); void openCamera(); }}>重拍</button>
          <button className="capture-shoot" disabled={!!busy} onClick={savePhoto}>{busy || "保存照片"}</button></>}
      </div>
    </div>}
  </div>;
}
