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

test("server-renders only the direct parameter page", async () => {
  const response = await fetchApp();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);
  const html = await response.text();
  assert.match(html, /<title>冷二联首设参数 \| 直接预测<\/title>/i);
  assert.match(html, /三个必要输入/);
  assert.match(html, /直接计算首设参数/);
  assert.match(html, /不写入PLC/);
  assert.doesNotMatch(html, /生产首页|卷道次记录|生产卡片录入|审计记录/);
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
  assert.equal(result.model_version, "direct-ridge-high-speed-v0.3");
  assert.equal(result.status, "recommendable");
  assert.equal(result.result_payload.similar_cases.length, 0);
  assert.equal(result.result_payload.parameters.length, 13);
  const intermediate = result.result_payload.parameters.find(
    (row) => row.code === "intermediate_thickness_set_mode",
  );
  assert.equal(intermediate.reference, 1.0614);
});

test("finished source has no starter marker", async () => {
  const packageJson = await readFile(new URL("package.json", root), "utf8");
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
});
