import type { CourseMetadata, SopStep } from "@/api/types";

export interface StepLearningReport {
  step_index: number;
  name: string;
  completed: boolean;
  skipped: boolean;
  duration_ms: number;
  expected_duration_s: number;
  error_count: number;
  unknown_count: number;
  needs_review: boolean;
}

export interface LearningReport {
  practice_index: number;
  completion_rate: number;
  completed_steps: number;
  total_steps: number;
  correction_count: number;
  call_count: number;
  weak_steps: StepLearningReport[];
  steps: StepLearningReport[];
  method_note: string;
}

export interface PracticeCheck {
  check_id: string;
  status: "satisfied" | "not_yet" | "violated" | "unknown";
  evidence_frame_ids: string[];
  reason: string;
}

export interface PracticeSession {
  id: string;
  sop_id: string;
  learner_id: string;
  status: "active" | "paused" | "finished" | "stopped";
  revision: number;
  steps: SopStep[];
  created_at: string;
  server_time_ms: number;
  state: {
    current_step_index: number | null;
    step_revision: number;
    step_state: "observing" | "confirming" | "correcting" | "completed";
    completed: number[];
    skipped: number[];
    checks: PracticeCheck[];
    feedback: string;
    service_error: string;
    pending_observation_id: string | null;
    entered_at_ms: number;
    last_sequence: number;
    call_count: number;
    error_count: number;
    unknown_streak: number;
    step_errors: Record<string, number>;
    step_unknowns: Record<string, number>;
    manual_checks: string[];
    attempts: { step_index: number; duration_ms: number; source: string }[];
  };
  learner?: { id: string; display_name: string };
  course?: { process_key: string; course_meta: CourseMetadata };
  report?: LearningReport;
}

export interface PracticeCourse {
  sop_id: string;
  process_key: string;
  version: number;
  course_meta: CourseMetadata;
  step_count: number;
  issues: string[];
}

export interface CapturedFrame {
  sequence: number;
  captured_at_ms: number;
  data_uri: string;
}

export interface PracticeEvent {
  id: number;
  type: string;
  payload: {
    session: PracticeSession;
    observation_id?: string;
    result?: { checks: PracticeCheck[]; latency_ms: number };
  };
}
