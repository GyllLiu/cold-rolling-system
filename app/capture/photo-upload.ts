export type StagedPhoto = { url: string; blob: Blob; id: string; capturedAt: string; replacesId?: string; displayRotation?: number };
export type SavedPhoto = { photo_id: string; photo_url: string };

export class PhotoUploadError extends Error {
  constructor(message: string, readonly status: number) { super(message); }
}

/** Keep the same ID and bytes on retry: a lost response may still mean the server saved it. */
export function uploadPhoto(photo: StagedPhoto, onProgress: (percent: number) => void, signal: AbortSignal): Promise<SavedPhoto> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const abort = () => xhr.abort();
    const finish = () => signal.removeEventListener("abort", abort);
    const unconfirmed = "尚未确认保存成功，照片仍保留在本页，请重试。";
    xhr.open("PUT", `/api/capture/photos/${photo.id}`);
    xhr.timeout = 45000;
    xhr.setRequestHeader("Content-Type", photo.blob.type || "image/jpeg");
    xhr.setRequestHeader("X-Capture-Time", photo.capturedAt);
    if (photo.displayRotation !== undefined) xhr.setRequestHeader("X-Display-Rotation", String(photo.displayRotation));
    if (photo.replacesId) xhr.setRequestHeader("X-Replaces-Photo-Id", photo.replacesId);
    xhr.upload.onprogress = event => {
      if (event.lengthComputable) onProgress(Math.min(100, Math.round(event.loaded / event.total * 100)));
    };
    xhr.onload = () => {
      finish();
      let body;
      try { body = JSON.parse(xhr.responseText); }
      catch { reject(new PhotoUploadError(unconfirmed, xhr.status)); return; }
      if (xhr.status < 200 || xhr.status >= 300) {
        reject(new PhotoUploadError(typeof body?.detail === "string" ? body.detail : unconfirmed, xhr.status)); return;
      }
      if (body?.photo_id !== photo.id || typeof body?.photo_url !== "string") {
        reject(new PhotoUploadError(unconfirmed, xhr.status)); return;
      }
      resolve(body);
    };
    xhr.onerror = () => { finish(); reject(new PhotoUploadError(`网络连接中断。${unconfirmed}`, 0)); };
    xhr.ontimeout = () => { finish(); reject(new PhotoUploadError(`上传等待超时。${unconfirmed}`, 0)); };
    xhr.onabort = () => { finish(); reject(new PhotoUploadError(unconfirmed, 0)); };
    if (signal.aborted) { reject(new PhotoUploadError(unconfirmed, 0)); return; }
    signal.addEventListener("abort", abort, { once: true });
    xhr.send(photo.blob);
  });
}
