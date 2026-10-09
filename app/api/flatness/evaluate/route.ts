import {
  evaluateFlatnessProfiles,
  FlatnessProfileError,
} from "../../../lib/flatness-profile";

export async function POST(request: Request) {
  let body: Record<string, unknown>;
  try {
    body = (await request.json()) as Record<string, unknown>;
  } catch {
    return Response.json({ detail: "请求内容不是有效JSON" }, { status: 400 });
  }

  const rawProfiles = Array.isArray(body.profiles)
    ? body.profiles
    : Array.isArray(body.profile)
      ? [body.profile]
      : null;
  if (!rawProfiles || !rawProfiles.every(Array.isArray)) {
    return Response.json(
      { detail: "请提供profile（49路）或profiles（多帧49路）" },
      { status: 400 },
    );
  }
  const profiles = rawProfiles.map((profile) =>
    profile.map((value) => {
      if (value == null) return null;
      const number = typeof value === "number" ? value : Number(value);
      return number;
    }),
  );
  try {
    return Response.json(evaluateFlatnessProfiles(profiles));
  } catch (error) {
    if (error instanceof FlatnessProfileError) {
      return Response.json({ detail: error.message }, { status: 400 });
    }
    return Response.json({ detail: "版型评估失败" }, { status: 500 });
  }
}
