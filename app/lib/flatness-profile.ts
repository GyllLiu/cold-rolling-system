import artifactJson from "../../backend/app/model_artifacts/recommendation_direct_v041.json";

type Distribution = {
  count: number;
  min: number;
  p05: number;
  median: number;
  p95: number;
  max: number;
};

type Association = {
  label: string;
  spearman_rho: number;
  absolute_rho: number;
  sample_count: number;
};

type FlatnessArtifact = {
  flatness_monitor: {
    native_unit: string;
    unit_status: string;
    zone_orientation: string;
    lag_alignment: string;
    observed_distributions: Record<string, Distribution>;
    runtime_associations: Record<string, Association[]>;
  };
};

const artifact = artifactJson as unknown as FlatnessArtifact;
const metricLabels: Record<string, string> = {
  flatness_std_median: "横向离散",
  flatness_lr_asymmetry_median: "左右不对称",
  flatness_edge_minus_center_median: "边部－中部",
  flatness_temporal_std_median: "版型时序波动",
};

export class FlatnessProfileError extends Error {}

function median(values: number[]) {
  const ordered = [...values].sort((left, right) => left - right);
  const middle = Math.floor(ordered.length / 2);
  return ordered.length % 2
    ? ordered[middle]
    : (ordered[middle - 1] + ordered[middle]) / 2;
}

function mean(values: number[]) {
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function standardDeviation(values: number[]) {
  const average = mean(values);
  return Math.sqrt(mean(values.map((value) => (value - average) ** 2)));
}

function round(value: number) {
  return Number(value.toFixed(6));
}

function profileMetrics(activeZones: number[], profile: Array<number | null>) {
  const points = activeZones
    .map((zone) => ({ zone, value: profile[zone - 1] }))
    .filter(
      (point): point is { zone: number; value: number } =>
        typeof point.value === "number" && Number.isFinite(point.value),
    );
  if (points.length < 5) return null;
  const first = Math.min(...points.map((point) => point.zone));
  const last = Math.max(...points.map((point) => point.zone));
  const span = Math.max(last - first, 1);
  const xs = points.map((point) => (2 * (point.zone - first)) / span - 1);
  const ys = points.map((point) => point.value);
  const average = mean(ys);
  const meanX2 = mean(xs.map((value) => value ** 2));
  const slopeDenominator = xs.reduce((sum, value) => sum + value ** 2, 0);
  const slope =
    xs.reduce(
      (sum, value, index) => sum + value * (ys[index] - average),
      0,
    ) / slopeDenominator;
  const qs = xs.map((value) => value ** 2 - meanX2);
  const curvatureDenominator = qs.reduce((sum, value) => sum + value ** 2, 0);
  const curvature =
    qs.reduce(
      (sum, value, index) => sum + value * (ys[index] - average),
      0,
    ) / curvatureDenominator;
  const left = ys.filter((_, index) => xs[index] <= -0.25);
  const right = ys.filter((_, index) => xs[index] >= 0.25);
  const edge = ys.filter((_, index) => Math.abs(xs[index]) >= 0.7);
  const center = ys.filter((_, index) => Math.abs(xs[index]) <= 0.2);
  return {
    flatness_std_median: standardDeviation(ys),
    flatness_lr_asymmetry_median: mean(right) - mean(left),
    flatness_edge_minus_center_median: mean(edge) - mean(center),
    flatness_slope_coefficient_median: slope,
    flatness_curvature_coefficient_median: curvature,
  };
}

export function evaluateFlatnessProfiles(profiles: Array<Array<number | null>>) {
  if (!profiles.length) throw new FlatnessProfileError("至少需要1帧49路版型数据");
  if (profiles.length > 600) {
    throw new FlatnessProfileError("单次最多评估600帧（按1Hz约10分钟）");
  }
  for (const profile of profiles) {
    if (profile.length !== 49) {
      throw new FlatnessProfileError("每帧版型必须恰好包含49路");
    }
    if (
      profile.some(
        (value) =>
          value != null &&
          (typeof value !== "number" || !Number.isFinite(value)),
      )
    ) {
      throw new FlatnessProfileError("版型值必须是有限数字或null");
    }
  }

  const activeZones = Array.from({ length: 49 }, (_, index) => index + 1).filter(
    (zone) => {
      const values = profiles
        .map((profile) => profile[zone - 1])
        .filter((value): value is number => value != null && Number.isFinite(value));
      if (!values.length) return false;
      return values.filter((value) => Math.abs(value) > 1e-8).length / values.length >= 0.5;
    },
  );
  if (activeZones.length < 5) {
    throw new FlatnessProfileError("有效版型区域少于5路，无法计算横向模式");
  }

  const perProfile = profiles
    .map((profile) => profileMetrics(activeZones, profile))
    .filter((value): value is NonNullable<typeof value> => value != null);
  if (!perProfile.length) {
    throw new FlatnessProfileError("没有可计算的有效版型帧");
  }
  const values: Record<string, number | null> = {
    flatness_std_median: median(perProfile.map((row) => row.flatness_std_median)),
    flatness_lr_asymmetry_median: median(
      perProfile.map((row) => row.flatness_lr_asymmetry_median),
    ),
    flatness_edge_minus_center_median: median(
      perProfile.map((row) => row.flatness_edge_minus_center_median),
    ),
    flatness_slope_coefficient_median: median(
      perProfile.map((row) => row.flatness_slope_coefficient_median),
    ),
    flatness_curvature_coefficient_median: median(
      perProfile.map((row) => row.flatness_curvature_coefficient_median),
    ),
    flatness_temporal_std_median: null,
  };

  if (profiles.length >= 2) {
    const temporal = activeZones
      .map((zone) =>
        profiles
          .map((profile) => profile[zone - 1])
          .filter((value): value is number => value != null && Number.isFinite(value)),
      )
      .filter((zoneValues) => zoneValues.length >= 2)
      .map(standardDeviation);
    if (temporal.length) values.flatness_temporal_std_median = median(temporal);
  }

  const monitor = artifact.flatness_monitor;
  const metrics = Object.entries(metricLabels).map(([code, label]) => {
    const value = values[code];
    const distribution = monitor.observed_distributions[code];
    const available = value != null && distribution != null;
    return {
      code,
      label,
      value: value == null ? null : round(value),
      native_unit: monitor.native_unit,
      reference_p05: distribution?.p05 ?? null,
      reference_median: distribution?.median ?? null,
      reference_p95: distribution?.p95 ?? null,
      reference_status: !available
        ? "not_available"
        : value < distribution.p05 || value > distribution.p95
          ? "outside_development_90_percent"
          : "inside_development_90_percent",
      top_runtime_associations: (monitor.runtime_associations[code] ?? []).slice(0, 3),
    };
  });

  return {
    status: "observed_not_actionable" as const,
    actionable: false,
    profile_count: profiles.length,
    active_zone_start: Math.min(...activeZones),
    active_zone_end: Math.max(...activeZones),
    active_zone_count: activeZones.length,
    native_unit: monitor.native_unit,
    metrics,
    shape_modes: {
      slope: round(values.flatness_slope_coefficient_median as number),
      curvature: round(values.flatness_curvature_coefficient_median as number),
    },
    warnings: [
      "开发分位数只用于发现异常偏离，不是产品质量公差。",
      "传感器序号尚未映射操作侧/传动侧，因此左右符号不能直接转换成倾斜调节方向。",
      "尚未按带材速度完成板形仪到机架的时间延迟对齐，不生成自动弯辊、倾斜或张力指令。",
    ],
  };
}
