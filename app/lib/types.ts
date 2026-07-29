export type Product = {
  id: string;
  code: string;
  name: string;
  input_alloy: string;
  nominal_alloy: string;
  temper: string;
  target_thickness: number;
  target_width: number;
  lower_tolerance: number;
  upper_tolerance: number;
  process_target_tolerance: number;
  purpose: string;
  review_status: string;
  source_notes: string;
};

export type Batch = {
  id: string;
  batch_no: string;
  product_id: string | null;
  cast_coil_no: string;
  dat_anchor_time: string;
  cast_spec: string;
  intermediate_spec: string;
  finished_spec: string;
  chemistry: Record<string, number | string>;
  status: string;
  ocr_confirmed: boolean;
  source_notes: string;
};

export type ProcessStageBase = {
  operator_name: string;
  inspector_name: string;
  record_date: string;
};

export type QualityStageBase = ProcessStageBase & {
  quality_status: string;
  self_check: string;
  special_check: string;
};

export type ProcessStageRecords = {
  cold_rolling: QualityStageBase & {
    after_coil_weight: string;
    after_thickness: string;
    after_width: string;
  };
  rewinding: ProcessStageBase & {
    cut_width: string;
    split_status: string;
    weight: string;
    quality_status: string;
    notes: string;
  };
  cross_cutting: QualityStageBase & {
    cut_total_weight: string;
    sheet_specification: string;
  };
  straightening: QualityStageBase & {
    straightened_total_weight: string;
    straightened_thickness: string;
    straightened_width: string;
  };
  annealing: ProcessStageBase & {
    charging_time: string;
    schedule_code: string;
    furnace_load: string;
    discharging_time: string;
    final_metal_temperature: string;
    notes: string;
  };
  packaging: QualityStageBase & {
    finished_weight: string;
    specification: string;
  };
};

export type ProductionCard = {
  id: string;
  batch_no: string;
  card_date: string;
  cast_coil_no: string;
  input_alloy: string;
  nominal_alloy: string;
  temper: string;
  input_quantity: number;
  cast_thickness: number | null;
  cast_width: number | null;
  intermediate_thickness: number | null;
  intermediate_width: number | null;
  trimmed_width: number | null;
  target_thickness: number | null;
  target_width: number | null;
  thickness_lower_tolerance: number | null;
  thickness_upper_tolerance: number | null;
  width_lower_tolerance: number | null;
  width_upper_tolerance: number | null;
  purpose: string;
  process_requirements: string;
  chemistry: Record<string, number | string>;
  transverse_plate_diff: Array<{ point: number; value: string }>;
  longitudinal_plate_diff: Array<{ position_mm: number; drive_side: string; operator_side: string }>;
  surface_quality: string;
  process_stage_records: ProcessStageRecords;
  review_status: "draft" | "pending_review" | "approved";
  created_by: string;
  notes: string;
  created_at: string;
  updated_at: string;
};

export type RollingPass = {
  id: string;
  batch_id: string | null;
  coil_no: string;
  pass_no: number;
  entry_thickness: number;
  target_exit_thickness: number;
  incoming_width: number;
  operator_name: string;
  assistant_name: string;
  status: string;
  started_at: string;
  high_speed_at: string;
  ended_at: string;
  final_thickness: number | null;
  quality_result: string;
  dat_start_time: string;
  dat_end_time: string;
  initial_settings: Record<string, number | string>;
  notes: string;
};

export type ThicknessSample = {
  id: string;
  pass_id: string;
  sequence_no: number;
  sampled_at: string;
  entered_at: string;
  manual_thickness: number;
  laser_thickness: number | null;
  supplement_percent: number | null;
  manual_width: number | null;
  measurer: string;
  is_valid: boolean;
  is_retest: boolean;
  sample_position: string;
  instrument: string;
};

export type ParameterAdjustment = {
  id: string;
  pass_id: string;
  adjusted_at: string;
  parameter_code: string;
  parameter_label: string;
  before_value: number | null;
  after_value: number | null;
  unit: string;
  reason: string;
  operator_name: string;
  source: string;
};

export type PassEvent = {
  id: string;
  pass_id: string;
  event_type: string;
  occurred_at: string;
  notes: string;
};

export type PassDetail = {
  pass: RollingPass;
  samples: ThicknessSample[];
  adjustments: ParameterAdjustment[];
  events: PassEvent[];
};

export type Recommendation = {
  id: string;
  status: "recommendable" | "reference_only" | "insufficient_data";
  confidence_score: number;
  model_version: string;
  request_payload: Record<string, number | string | null>;
  result_payload: {
    headline: string;
    can_apply: boolean;
    parameters: RecommendationParameter[];
    similar_cases: Array<Record<string, number | string | boolean | null>>;
    risks: string[];
    coverage?: {
      in_domain: boolean;
      edge_features: string[];
      out_of_range_features: string[];
      training_case_count?: number;
      validation_case_count?: number;
    };
    model?: ModelStatus;
  };
};

export type RecommendationParameter = {
  code: string;
  label: string;
  unit: string;
  reference: number;
  low: number;
  high: number;
  role: "thickness_control" | "machine_setup" | "secondary_reference" | "monitor_only";
  priority: "primary" | "secondary" | "monitor";
  reliability: "high" | "medium" | "low" | "monitor";
  interval_source: string;
  final_test_mae: number | null;
  final_test_p95_error: number | null;
  historical_baseline_mae: number | null;
  mae_improvement_percent: number | null;
};

export type ModelStatus = {
  model_version: string;
  model_family?: string;
  inference_method?: string;
  training_datasets: string[];
  validation_dataset: string;
  training_case_count: number;
  validation_case_count: number;
  validation_composite_improvement_percent: number | null;
  mean_mae_improvement_percent?: number | null;
  direct_win_count: number;
  target_count: number;
  high_speed_only: boolean;
  historical_neighbour_method_used_at_inference?: boolean;
  validation_metrics?: Record<
    string,
    {
      direct_mae: number | null;
      historical_baseline_mae: number | null;
      mae_improvement_percent: number | null;
    }
  >;
};

export type DashboardData = {
  counts: {
    products: number;
    batches: number;
    cards: number;
    passes: number;
    samples: number;
    dat_files: number;
    historical_candidates: number;
    pending_reviews: number;
    active_passes: number;
  };
  recent_recommendations: Array<Record<string, unknown>>;
  model: ModelStatus;
};

export type DatFile = {
  id: string;
  filename: string;
  start_time: string;
  end_time: string;
  frames: number | null;
  channel_count: number;
  quality_status: string;
  schema_hash: string;
};

export type DatDataset = {
  dataset: string;
  role: "training" | "validation";
  status: "ready" | "missing";
  file_count: number;
  parse_success_count: number;
  segment_count: number;
  learning_eligible_count: number;
  stable_seconds: number;
  tuning_seconds: number;
};
