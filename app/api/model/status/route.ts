import { getDirectModelMetadata } from "../../../lib/direct-model";

export async function GET() {
  return Response.json(getDirectModelMetadata());
}
