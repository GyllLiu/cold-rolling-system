// The ECS reverse proxy serves this prefix directly. This bridge also supports local preview.
async function proxy(request: Request) {
  const url = new URL(request.url);
  const origin = request.headers.get("origin");
  if (!["GET", "HEAD"].includes(request.method) && origin) {
    try {
      if (new URL(origin).host !== (request.headers.get("host") ?? url.host)) {
        return Response.json({ detail: "请求来源不匹配" }, { status: 403 });
      }
    } catch { return Response.json({ detail: "请求来源无效" }, { status: 403 }); }
  }
  const upstream = `http://127.0.0.1:8001${url.pathname}${url.search}`;
  const headers = new Headers(request.headers);
  headers.delete("host");
  headers.delete("origin");
  // Only the loopback backend receives the already-validated browser request.
  try {
    const response = await fetch(upstream, {
      method: request.method, headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : await request.arrayBuffer(),
      redirect: "manual",
    });
    return new Response(response.body, { status: response.status, headers: response.headers });
  } catch {
    return Response.json({ detail: "拍照记录服务暂不可用，请稍后重试" }, { status: 503 });
  }
}
export const GET = proxy;
export const PUT = proxy;
export const POST = proxy;
