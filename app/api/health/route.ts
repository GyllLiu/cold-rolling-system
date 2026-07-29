export async function GET() {
  return Response.json({
    status: "ok",
    service: "cold-rolling-direct-model",
    version: "0.3.0",
  });
}
