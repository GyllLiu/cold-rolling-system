import artifactJson from "../../backend/app/model_artifacts/recommendation_direct_v3.json";

type CoverageBounds = {
  min: number;
  p01: number;
  p99: number;
  max: number;
};

type DirectMetrics = {
  mae: number;
  p95_absolute_error: number;
};

type ArtifactModel = {
  label: string;
  unit: string;
  role: "thickness_control" | "machine_setup" | "secondary_reference" | "monitor_only";
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
  models: Record<string, ArtifactModel>;
  summary: {
    target_count: number;
    direct_win_count: number;
    mean_mae_improvement_percent: number;
  };
};

const artifact = artifactJson as unknown as DirectArtifact;

const parameterOrder = [
  "intermediate_thickness_set_mode",
  "tm1_gap_command_median",
  "tm2_gap_command_median",
  "uncoiler_tension_set_median",
  "interstand_tension_set_median",
  "coiler_tension_set_median",
  "f1_bending_set_median",
  "f2_bending_set_median",
  "tm1_tilt_set_median",
  "tm2_tilt_set_median",
  "main_speed_set_median",
  "f1_roll_force_sum_median",
  "f2_roll_force_sum_median",
] as const;

const featureLabels: Record<string, string> = {
  entry_thickness_set_mode: "入口厚度",
  exit_thickness_set_mode: "目标出口厚度",
  width_set_mode: "来料宽度",
  total_reduction_ratio: "总压下率",
  entry_thickness_actual_median: "入口实测厚度",
};

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
        direct_mae: model.final_test.direct.mae,
        historical_baseline_mae: model.final_test.historical_5nn_median_baseline.mae,
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
  const reference = predict(target, model, features);
  const halfWidth = model.prediction_interval.half_width;
  return {
    code: target,
    label: model.label,
    unit: model.unit,
    reference: round(reference),
    low: round(applyBounds(reference - halfWidth, target, model, features)),
    high: round(applyBounds(reference + halfWidth, target, model, features)),
    role: model.role,
    priority: model.priority,
    reliability: model.reliability,
    interval_source: "滚动时间验证残差80%区间",
    final_test_mae: model.final_test.direct.mae,
    final_test_p95_error: model.final_test.direct.p95_absolute_error,
    historical_baseline_mae:
      model.final_test.historical_5nn_median_baseline.mae,
    mae_improvement_percent: model.final_test.mae_improvement_percent,
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
          "补充该规格的高速稳定数据并重新训练后再给首设。",
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
    "结果由固定回归方程直接计算，不检索或平均历史卷；系统不会写入PLC，首卷必须由主操确认。",
    "0719验证衡量的是对高速稳定设定的预测误差；尚无“模型实际下发后”的成品厚度闭环准确率。",
  ];
  if (actualDefaulted) {
    risks.push("未提供入口在线实测厚度，本次按入口设定厚度代入。");
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
        : "已生成直接预测首设，需主操确认",
      can_apply: false,
      parameters,
      similar_cases: [],
      risks,
      model: metadata,
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
