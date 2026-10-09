import assert from 'node:assert/strict';
import { test } from 'node:test';
import { readFileSync } from 'node:fs';
import ts from 'typescript';

const source = readFileSync(new URL('../app/capture/photo-upload.ts', import.meta.url), 'utf8');
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText;
const { uploadPhoto } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`);

class RequestStub {
  static requests = [];
  upload = {};
  headers = {};
  status = 200;
  responseText = '';
  open(method, url) { this.method = method; this.url = url; }
  setRequestHeader(key, value) { this.headers[key] = value; }
  send(body) { this.body = body; RequestStub.requests.push(this); }
  abort() { this.onabort?.(); }
  respond(body, status = 200) { this.status = status; this.responseText = JSON.stringify(body); this.onload(); }
}

test('upload waits for persistence acknowledgement, retries the same bytes and ID after timeout', async () => {
  const previous = globalThis.XMLHttpRequest;
  globalThis.XMLHttpRequest = RequestStub;
  try {
    const photo = { id: 'photo-one', capturedAt: '2026-09-16T09:00:00Z', blob: new Blob(['image'], { type: 'image/jpeg' }), url: 'blob:local' };
    const progress = [];
    let complete = false;
    const first = uploadPhoto(photo, value => progress.push(value), new AbortController().signal);
    first.then(() => { complete = true; }, () => {});
    const request = RequestStub.requests.at(-1);
    request.upload.onprogress({ lengthComputable: true, loaded: 5, total: 5 });
    await Promise.resolve();
    assert.deepEqual(progress, [100]);
    assert.equal(complete, false, '100% transferred is not a confirmed save');
    request.ontimeout();
    await assert.rejects(first, /尚未确认保存成功/);
    const retry = uploadPhoto(photo, () => {}, new AbortController().signal);
    const retried = RequestStub.requests.at(-1);
    assert.equal(retried.url, request.url);
    assert.equal(retried.body, request.body);
    assert.equal(retried.headers['X-Capture-Time'], photo.capturedAt);
    retried.respond({ photo_id: photo.id, photo_url: '/api/capture/photos/photo-one' });
    assert.equal((await retry).photo_id, photo.id);
  } finally { globalThis.XMLHttpRequest = previous; }
});

test('authorization failure, invalid response, and abort never report a saved photo', async () => {
  const previous = globalThis.XMLHttpRequest;
  globalThis.XMLHttpRequest = RequestStub;
  try {
    const photo = { id: 'photo-two', capturedAt: '2026-09-16T09:00:00Z', blob: new Blob(['image']), url: 'blob:local' };
    const denied = uploadPhoto(photo, () => {}, new AbortController().signal);
    RequestStub.requests.at(-1).respond({ detail: '请输入工位访问码' }, 401);
    await assert.rejects(denied, error => error.status === 401);
    const invalid = uploadPhoto(photo, () => {}, new AbortController().signal);
    RequestStub.requests.at(-1).respond({ photo_id: 'another-photo', photo_url: '/wrong' });
    await assert.rejects(invalid, /尚未确认保存成功/);
    const controller = new AbortController();
    const aborted = uploadPhoto(photo, () => {}, controller.signal);
    controller.abort();
    await assert.rejects(aborted, /尚未确认保存成功/);
  } finally { globalThis.XMLHttpRequest = previous; }
});

test('retakes retry with the same replacement relationship and display angle', async () => {
  const previous = globalThis.XMLHttpRequest;
  globalThis.XMLHttpRequest = RequestStub;
  try {
    const photo = { id: 'retake', replacesId: 'old-photo', displayRotation: 270,
      capturedAt: '2026-09-23T09:00:00Z', blob: new Blob(['new image']), url: 'blob:retake' };
    const first = uploadPhoto(photo, () => {}, new AbortController().signal);
    const request = RequestStub.requests.at(-1);
    assert.equal(request.headers['X-Replaces-Photo-Id'], 'old-photo');
    assert.equal(request.headers['X-Display-Rotation'], '270');
    request.ontimeout();
    await assert.rejects(first, /尚未确认保存成功/);
    const retry = uploadPhoto(photo, () => {}, new AbortController().signal);
    const retried = RequestStub.requests.at(-1);
    assert.deepEqual(retried.headers, request.headers);
    assert.equal(retried.body, request.body);
    retried.respond({ photo_id: 'retake', photo_url: '/api/capture/photos/retake' });
    assert.equal((await retry).photo_id, 'retake');
  } finally { globalThis.XMLHttpRequest = previous; }
});
