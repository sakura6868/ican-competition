// 后端 API 数据类型（与 gongxu/api/routes/* 对齐）。

export interface User {
  id: string;
  username: string;
  display_name: string;
  role: "admin" | "instructor" | "learner" | "observer";
}

export interface Asset {
  id: string;
  kind: "video" | "manual" | "image";
  filename: string;
  sha256: string;
  license_note: string;
  authorized_by: string | null;
  has_encrypted_storage: boolean;
  allow_provider_export: boolean;
  indexed?: boolean;
  processing_job_id?: string | null;
  processing_status?: string | null;
  created_at: string;
}

export interface SopStep {
  id: string;
  index: number;
  name: string;
  predecessors: number[];
  optional: boolean;
  critical: boolean;
  tools: string[];
  objects: string[];
  actions: string[];
  expected_duration_s: number;
  completion_checks?: string[];
  observable_checks: string[];
  unobservable_checks: string[];
  weight: number;
  evidence_refs: EvidenceRef[];
  teaching?: {
    source_asset_id: string;
    start_ms: number;
    end_ms: number;
    instruction: string;
    reference_timestamps_ms: number[];
  } | null;
  verification?: {
    checks: {
      id: string;
      criterion: string;
      category: "completion" | "safety";
      required: boolean;
      mode: "visual" | "manual";
      correction: string;
    }[];
    confirm_windows: number;
    teacher_confirmed: boolean;
  } | null;
}

export type SourceGrade = "video_reference" | "manual_backed" | "sensor_verified";

export interface EvidenceRef {
  kind: "video" | "manual" | "sensor";
  ref: string;
  description: string;
  asset_id?: string | null;
  timestamp_ms?: number | null;
  page?: number | null;
  section?: string | null;
  chunk_id?: number | null;
  chunk_index?: number | null;
  content_hash?: string | null;
}

export interface ProcessVocabulary {
  process_key: string;
  tools: string[];
  objects: string[];
  actions: string[];
  approved: boolean;
  source: string;
}

export interface Sop {
  id: string;
  process_key: string;
  version: number;
  status: string;
  source_grade: SourceGrade;
  steps: SopStep[];
  course_meta: CourseMetadata;
  scoring_profile: Record<string, number>;
  prompt_version: string;
  provider_receipt_id: string | null;
  reference_asset_id?: string | null;
  source_asset_ids?: string[];
  compiled_by?: string | null;
  published_at?: string | null;
  created_at: string;
}

export interface CourseMetadata {
  title: string;
  description: string;
  objectives: string[];
  difficulty: "入门" | "进阶" | "挑战";
  audience: string;
  estimated_minutes: number;
}

export interface JobStatus {
  job_id: string;
  status: string;
}

export interface JobDetail {
  id: string;
  job_type: string;
  status: "queued" | "running" | "retrying" | "succeeded" | "failed" | "cancelled" | string;
  stage: string;
  progress_percent: number;
  status_text: string;
  error_summary: string;
  retry_count: number;
  max_retries: number;
  result: Record<string, unknown>;
  checkpoint: Record<string, unknown>;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface RagChunk {
  chunk_id: number;
  asset_id: string;
  chunk_index: number;
  text: string;
  score: number;
  page?: number | null;
  section?: string | null;
}

export interface AssessmentSession {
  id: string;
  mode: string;
  process_key: string;
  status: string;
  learner_id: string | null;
  input_sha256: string | null;
  input_asset_id?: string | null;
  sop_version_id?: string | null;
  job_id?: string | null;
  progress_percent?: number;
  progress_stage?: string;
  created_at: string;
}

export interface ScoreReport {
  id: string;
  session_id: string;
  total_score: number;
  result: string;
  dimension_scores: Record<string, number>;
  evidence_coverage: number;
  model_version: string;
  scoring_rules_version: string;
}

export interface StepJudgement {
  step_index: number;
  step_id: string;
  step_name?: string;
  judgement: string;
  start_ms: number;
  end_ms: number;
  rule_code: string;
  reason: string;
  confidence?: number;
  keyframe_ref?: string | null;
  evidence_timestamp_ms?: number | null;
  evidence_url?: string | null;
  detected_tools?: string[];
  detected_objects?: string[];
  detected_actions?: string[];
  observable_check_results?: Record<
    string,
    "passed" | "failed" | "insufficient_evidence"
  >;
}

export interface CoachingFeedback {
  version: string;
  source: "external_model" | "external_model_partial" | "deterministic_rules";
  explanation: string;
  practice_plan: {
    focus_steps: string[];
    repeat_count: number;
    note: string;
  };
  provider: string | null;
  model_id: string | null;
  errors: string[];
}

export interface SessionReport {
  report: ScoreReport | null;
  steps: StepJudgement[];
  rejected?: boolean;
  reject_reason?: string;
  coaching?: CoachingFeedback | null;
  analysis_duration_ms?: number | null;
  provider_metrics?: {
    provider: string;
    model_id: string;
    calls: number;
    successful_calls: number;
    failed_calls: number;
    input_tokens: number;
    output_tokens: number;
    total_tokens: number;
    latency_ms: number;
    pricing_status: string;
  } | null;
}

export interface AssessmentEvidence {
  session_id: string;
  input_asset_id: string | null;
  preview_url: string | null;
  steps: StepJudgement[];
  observations?: EvidenceObservation[];
}

export interface EvidenceObservation {
  timestamp_ms: number;
  hand: string;
  tool: string;
  object: string;
  action: string;
  relation: string;
  confidence: number;
}

export interface AssessmentProgress {
  session_id: string;
  session_status: string;
  job: JobDetail | null;
  report_ready: boolean;
  report_id: string | null;
  analyzed_frames?: number;
  estimated_remaining_seconds?: number | null;
}

export interface SubmitAssessmentResponse {
  session: AssessmentSession;
  job_id: string;
  job_status: string;
  preview_url: string | null;
  cache_hit?: boolean;
  cache_source_session_id?: string | null;
}

export type DemoScenario = "complete" | "error" | "inconclusive";

export interface DemoReplayResponse {
  scenario: DemoScenario;
  label: string;
  source: "fixture_replay";
  cached: true;
  disclaimer: string;
  sop: {
    id: string;
    process_key: string;
    version: number;
  };
  report: ScoreReport;
  steps: StepJudgement[];
  observations: EvidenceObservation[];
  quality: {
    sampled_frames: number;
    visible_ratio: number;
  };
  preview_url: string | null;
}
