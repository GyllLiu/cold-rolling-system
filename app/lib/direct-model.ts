import artifactJson from "../../backend/app/model_artifacts/recommendation_direct_v041.json";

type CoverageBounds = {
  min: number;
  p01: number;
  p99: number;
  max: number;
};

type DirectMetrics = {
  mae: number;
  p95_absolute_error: number;
  r2: number | null;
};

type ArtifactModel = {
  label: string;
  unit: string;
  role:
    | "thickness_control"
    | "machine_setup"
    | "secondary_reference"
    | "monitor_only"
    | "flatness_monitor";
  priority: "primary" | "secondary" | "monitor";
  reliability: "high" | "medium" | "low" | "monitor";
  feature_names: string[];
  target_transform: "raw" | "reduction_share";
  feature_mean: number[];
  feature_scale: number[];
  basis_terms: number[][];
  coefficients: number[];
  hard_min: number | null;
  hard_max: number | null;
  prediction_interval: {
    half_width: number;
    p95_half_width: number;
  };
  final_test: {
    direct: DirectMetrics;
    historical_5nn_median_baseline: {
      mae: number;
    };
    mae_improvement_percent: number;
  };
};

type DirectArtifact = {
  model_version: string;
  model_family: string;
  inference_method: string;
  training_datasets: string[];
  validation_dataset: string;
  training_case_count: number;
  validation_case_count: number;
  training_policy: {
    high_speed_plateau_only: boolean;
    historical_neighbour_method_used_at_inference: boolean;
  };
  input_coverage: Record<string, CoverageBounds>;
  width_ablation: Record<
    string,
    {
      selected_uses_width: boolean;
      hybrid_validation_improvement_percent: number;
      final_test_mae_improvement_percent: number;
    }
  >;
  models: Record<string, ArtifactModel>;
  flatness_monitor: {
    role: string;
    native_unit: string;
    unit_status: string;
    zone_count: number;
    zone_orientation: string;
    lag_alignment: string;
    training_case_count: number;
    validation_case_count: number;
    models: Record<string, ArtifactModel>;
    runtime_associations: Record<
      string,
      Array<{
        control: string;
        label: string;
        spearman_rho: number;
        absolute_rho: number;
        sample_count: number;
        spec_group_count: number;
      }>
    >;
    data_quality: {
      all_row_count: number;
      valid_flatness_row_count: number;
      valid_percent: number;
      width_to_active_zone_spearman: number;
    };
  };
  summary: {
    target_count: number;
    direct_win_count: number;
    mean_mae_improvement_percent: number;
  };
};

const artifact = artifactJson as unknown as DirectArtifact;

const parameterOrder = [
  "intermediate_thickness_set_mode",
  "uncoiler_tension_set_median",
  "interstand_tension_set_median",
  "coiler_tension_set_median",
  "tm1_closed_gap_set_median",
  "tm2_closed_gap_set_median",
] as const;

const featureLabels: Record<string, string> = {
  entry_thickness_set_mode: "入口厚度",
  exit_thickness_set_mode: "目标出口厚度",
  width_set_mode: "来料宽度",
  total_reduction_ratio: "总压下率",
  entry_thickness_actual_median: "入口实测厚度",
  log_thickness_ratio: "厚度比对数",
  entry_cross_section_proxy: "入口截面代理",
  exit_cross_section_proxy: "出口截面代理",
};

const hmiPresentation: Record<string, { unit: string; factor: number }> = {};

function toHmiValue(target: string, value: number) {
  return value * (hmiPresentation[target]?.factor ?? 1);
}

function hmiUnit(target: string, nativeUnit: string) {
  return hmiPresentation[target]?.unit ?? nativeUnit;
}

export type DirectModelInput = {
  entry_thickness: number;
  entry_actual_thickness?: number | null;
  target_exit_thickness: number;
  incoming_width: number;
};

export function getDirectModelMetadata() {
  const validation_metrics = Object.fromEntries(
    Object.entries(artifact.models).map(([target, model]) => [
      target,
      {
        direct_mae: round(toHmiValue(target, model.final_test.direct.mae)),
        historical_baseline_mae: round(
          toHmiValue(target, model.final_test.historical_5nn_median_baseline.mae),
        ),
        mae_improvement_percent: model.final_test.mae_improvement_percent,
      },
    ]),
  );
  return {
    model_version: artifact.model_version,
    model_family: artifact.model_family,
    inference_method: artifact.inference_method,
    training_datasets: artifact.training_datasets,
    validation_dataset: artifact.validation_dataset,
    training_case_count: artifact.training_case_count,
    validation_case_count: artifact.validation_case_count,
    validation_composite_improvement_percent: artifact.summary.mean_mae_improvement_percent,
    mean_mae_improvement_percent: artifact.summary.mean_mae_improvement_percent,
    direct_win_count: artifact.summary.direct_win_count,
    target_count: artifact.summary.target_count,
    high_speed_only: artifact.training_policy.high_speed_plateau_only,
    historical_neighbour_method_used_at_inference:
      artifact.training_policy.historical_neighbour_method_used_at_inference,
    width_validation_improvement_percent: Object.fromEntries(
      Object.entries(artifact.width_ablation).map(([target, value]) => [
        target,
        value.hybrid_validation_improvement_percent,
      ]),
    ),
    flatness_training_case_count: artifact.flatness_monitor.training_case_count,
    flatness_validation_case_count: artifact.flatness_monitor.validation_case_count,
    flatness_valid_percent: artifact.flatness_monitor.data_quality.valid_percent,
    validation_metrics,
  };
}

function requestFeatures(input: DirectModelInput) {
  const actualDefaulted = input.entry_actual_thickness == null;
  return {
    values: {
      entry_thickness_set_mode: input.entry_thickness,
      exit_thickness_set_mode: input.target_exit_thickness,
      width_set_mode: input.incoming_width,
      total_reduction_ratio:
        (input.entry_thickness - input.target_exit_thickness) / input.entry_thickness,
      entry_thickness_actual_median:
        input.entry_actual_thickness ?? input.entry_thickness,
      log_thickness_ratio: Math.log(
        input.entry_thickness / input.target_exit_thickness,
      ),
      entry_cross_section_proxy: input.entry_thickness * input.incoming_width,
      exit_cross_section_proxy:
        input.target_exit_thickness * input.incoming_width,
    },
    actualDefaulted,
  };
}

function basisRow(
  values: Record<string, number>,
  model: ArtifactModel,
) {
  const standardized = model.feature_names.map(
    (name, index) =>
      (values[name] - model.feature_mean[index]) / model.feature_scale[index],
  );
  return [
    1,
    ...model.basis_terms.map((term) =>
      term.reduce((product, index) => product * standardized[index], 1),
    ),
  ];
}

function applyBounds(
  value: number,
  target: string,
  model: ArtifactModel,
  features: Record<string, number>,
) {
  let bounded = value;
  if (target === "intermediate_thickness_set_mode") {
    bounded = Math.min(
      features.entry_thickness_set_mode - 0.001,
      Math.max(features.exit_thickness_set_mode + 0.001, bounded),
    );
  }
  if (model.hard_min != null) bounded = Math.max(model.hard_min, bounded);
  if (model.hard_max != null) bounded = Math.min(model.hard_max, bounded);
  return bounded;
}

function predict(
  target: string,
  model: ArtifactModel,
  features: Record<string, number>,
) {
  const row = basisRow(features, model);
  const encoded = row.reduce(
    (sum, value, index) => sum + value * model.coefficients[index],
    0,
  );
  const value =
    model.target_transform === "reduction_share"
      ? features.entry_thickness_set_mode -
        encoded *
          (features.entry_thickness_set_mode -
            features.exit_thickness_set_mode)
      : encoded;
  if (!Number.isFinite(value)) throw new Error(`模型输出异常：${target}`);
  return applyBounds(value, target, model, features);
}

function coverageState(features: Record<string, number>) {
  const outside: string[] = [];
  const edge: string[] = [];
  for (const [feature, value] of Object.entries(features)) {
    const bounds = artifact.input_coverage[feature];
    if (!bounds) continue;
    const label = featureLabels[feature] ?? feature;
    if (value < bounds.min || value > bounds.max) outside.push(label);
    else if (value < bounds.p01 || value > bounds.p99) edge.push(label);
  }
  return { outside, edge };
}

function round(value: number) {
  return Number(value.toFixed(4));
}

function parameterRow(
  target: string,
  model: ArtifactModel,
  features: Record<string, number>,
) {
  const nativeReference = predict(target, model, features);
  const halfWidth = model.prediction_interval.half_width;
  const nativeLow = applyBounds(nativeReference - halfWidth, target, model, features);
  const nativeHigh = applyBounds(nativeReference + halfWidth, target, model, features);
  return {
    code: target,
    label: model.label,
    unit: hmiUnit(target, model.unit),
    reference: round(toHmiValue(target, nativeReference)),
    low: round(toHmiValue(target, nativeLow)),
    high: round(toHmiValue(target, nativeHigh)),
    role: model.role,
    priority: model.priority,
    reliability: model.reliability,
    interval_source: "日期滚动与整规格留出残差的保守80%区间",
    final_test_mae: round(toHmiValue(target, model.final_test.direct.mae)),
    final_test_p95_error: round(
      toHmiValue(target, model.final_test.direct.p95_absolute_error),
    ),
    historical_baseline_mae: round(
      toHmiValue(target, model.final_test.historical_5nn_median_baseline.mae),
    ),
    mae_improvement_percent: model.final_test.mae_improvement_percent,
  };
}

function buildFlatnessMonitor(features: Record<string, number>) {
  const monitor = artifact.flatness_monitor;
  const metrics = Object.entries(monitor.models).map(([target, model]) => {
    const r2 = model.final_test.direct.r2;
    const specificationPredictable = r2 != null && r2 >= 0.35;
    const reference = specificationPredictable
      ? predict(target, model, features)
      : null;
    return {
      code: target,
      label: model.label,
      native_unit: monitor.native_unit,
      specification_predictable: specificationPredictable,
      reference: reference == null ? null : round(reference),
      low:
        reference == null
          ? null
          : round(reference - model.prediction_interval.half_width),
      high:
        reference == null
          ? null
          : round(reference + model.prediction_interval.half_width),
      final_test_mae: round(model.final_test.direct.mae),
      final_test_r2: r2 == null ? null : round(r2),
      top_runtime_associations: (monitor.runtime_associations[target] ?? [])
        .slice(0, 3)
        .map((item) => ({
          label: item.label,
          spearman_rho: item.spearman_rho,
          absolute_rho: item.absolute_rho,
          sample_count: item.sample_count,
        })),
    };
  });
  return {
    status: "runtime_dat_required" as const,
    actionable: false,
    zone_count: monitor.zone_count,
    native_unit: monitor.native_unit,
    unit_status: monitor.unit_status,
    orientation_status: monitor.zone_orientation,
    lag_alignment_status: monitor.lag_alignment,
    training_case_count: monitor.training_case_count,
    validation_case_count: monitor.validation_case_count,
    valid_percent: monitor.data_quality.valid_percent,
    width_to_active_zone_spearman:
      monitor.data_quality.width_to_active_zone_spearman,
    metrics,
    conclusion:
      "四项规格前预测R²均低于0.10，不能仅凭入口/出口/宽度预判版型；必须在运行后读取49路DAT再判断。",
  };
}

export function buildDirectRecommendation(input: DirectModelInput) {
  const metadata = getDirectModelMetadata();
  const { values: features, actualDefaulted } = requestFeatures(input);
  const { outside, edge } = coverageState(features);

  if (outside.length) {
    return {
      status: "insufficient_data",
      confidence_score: 0,
      model_version: artifact.model_version,
      request_payload: input,
      result_payload: {
        headline: "规格超出训练覆盖，拒绝强行预测",
        can_apply: false,
        parameters: [],
        similar_cases: [],
        risks: [
          `超出训练范围的输入：${outside.join("、")}。`,
          "补充该规格的高速稳定数据并重新训练后再给推荐值。",
        ],
        model: metadata,
        coverage: {
          in_domain: false,
          edge_features: edge,
          out_of_range_features: outside,
          training_case_count: metadata.training_case_count,
          validation_case_count: metadata.validation_case_count,
        },
      },
    };
  }

  const parameters = parameterOrder.map((target) =>
    parameterRow(target, artifact.models[target], features),
  );
  const lowReliability = parameters
    .filter((row) => row.reliability === "low")
    .map((row) => row.label);
  const worseThanHistory = parameters
    .filter((row) => row.mae_improvement_percent < 0)
    .map((row) => row.label);
  const risks = [
    "结果由高速稳定生产段训练的固定回归方程直接计算，不检索或平均历史卷；系统不会写入PLC，必须由主操确认。",
    "0719是日期隔离复用对照，衡量高速稳定设定的复现误差；尚无“模型实际下发后”的成品厚度闭环准确率。",
    "F1/F2输出为高速稳定生产段学习得到的推荐辊缝，供主操上卷时手动参考输入，以减少调试次数；当前无PLC，不自动写入操作台。",
    "版型无法由产品规格可靠预测；弯辊、倾斜、左右轧制力和左右实际辊缝只在运行后结合49路DAT监控。",
  ];
  if (actualDefaulted) {
    risks.push("当前无PLC/实时DAT接入，入口厚度由主操手工填写，不会自动传入。");
  }
  if (edge.length) {
    risks.push(`靠近训练边界：${edge.join("、")}；已降为低置信参考。`);
  }
  if (lowReliability.length) {
    risks.push(
      `跨日解释能力较弱、只可人工参考：${lowReliability.join("、")}。`,
    );
  }
  if (worseThanHistory.length) {
    risks.push(
      `0719上直接模型未优于历史基线：${worseThanHistory.join("、")}；仍未回退到历史平均。`,
    );
  }

  return {
    status: edge.length ? "reference_only" : "recommendable",
    confidence_score: edge.length ? 52 : 76,
    model_version: artifact.model_version,
    request_payload: input,
    result_payload: {
      headline: edge.length
        ? "已生成直接预测，边界规格需谨慎确认"
        : "已生成高速数据推荐值，需主操确认",
      can_apply: false,
      parameters,
      similar_cases: [],
      risks,
      model: metadata,
      flatness_monitor: buildFlatnessMonitor(features),
      coverage: {
        in_domain: true,
        edge_features: edge,
        out_of_range_features: [],
        training_case_count: metadata.training_case_count,
        validation_case_count: metadata.validation_case_count,
      },
      input_snapshot: Object.fromEntries(
        Object.entries(features).map(([key, value]) => [key, round(value)]),
      ),
    },
  };
}
