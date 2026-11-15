export type TaskState =
  | "draft"
  | "ready"
  | "assigned"
  | "in_progress"
  | "blocked"
  | "review"
  | "accepted"
  | "merge_queued"
  | "merging"
  | "merged"
  | "done"
  | "reopened"
  | "cancelled"
  | "failed";

export interface Overview {
  demo: boolean;
  generated_at: string;
  execution_wip: number;
  review_debt: number;
  blocked: number;
  accepted: number;
  agents_online: number;
  events_5m: number;
  last_event_at: string | null;
  model_cost_today?: number;
  open_alerts?: number;
  event_lag_seconds?: number;
  tool_error_rate?: number;
  states: Record<string, number>;
  workstreams: Array<{ code: string; name: string; wip_limit: number; wip: number }>;
}

export interface Task {
  code: string;
  title: string;
  state: TaskState;
  priority: string;
  kind: string;
  current_attempt: number;
  created_at: string;
  updated_at: string;
  workstream: string;
  assignee: string | null;
  assignment_state: string | null;
}

export interface Agent {
  code: string;
  role: string;
  status: string;
  model_policy: Record<string, unknown>;
  heartbeat_at: string | null;
  current_task: string | null;
}

export interface EventItem {
  seq: number;
  type: string;
  actor_type: string;
  actor_id: string | null;
  payload: Record<string, unknown>;
  occurred_at: string;
  task: string | null;
}

export interface Metrics {
  event_rate: Array<{ minute: string; count: number }>;
  states: Array<{ state: string; count: number }>;
  workstreams: Array<{ code: string; name: string; wip_limit: number; wip: number }>;
  event_types: Array<{ type: string; count: number }>;
}

export interface TaskDetail extends Task {
  accepted_at: string | null;
  work_order: (Record<string, unknown> & { version?: number; hash?: string }) | null;
  receipt: Receipt | null;
  leases: Lease[];
  documents: DocumentMeta[];
  events: Array<{
    seq: number;
    type: string;
    actor_type: string;
    actor_id: string | null;
    payload: Record<string, unknown>;
    occurred_at: string;
  }>;
}

export interface Receipt {
  code: string;
  status: string;
  body: Record<string, unknown>;
  base_commit: string;
  candidate_ref: string;
  stale_base: boolean;
  created_at: string;
}

export interface AlertRow {
  id: string;
  severity: "info" | "warning" | "critical";
  code: string;
  resource: Record<string, unknown>;
  state: string;
  first_seen: string;
  last_seen: string;
  count: number;
  details: Record<string, unknown>;
}

export interface Lease {
  id: string;
  path_glob: string;
  lease_key: string;
  mode: string;
  state: string;
  fencing_token: number;
  expires_at: string;
  task: string;
  agent: string;
}

export interface Approval {
  id: string;
  action: string;
  resource: Record<string, unknown>;
  risk: string;
  options: string[];
  default_option: string;
  requested_at: string;
  expires_at: string | null;
  decision: string | null;
  task: string | null;
  requested_by: string | null;
  decided_by: string | null;
  rationale: string | null;
}

export interface LlmProvider {
  id: string;
  agent_code: string;
  name: string;
  base_url: string;
  model: string;
  price_input_per_1m: number;
  price_output_per_1m: number;
  price_cache_hit_per_1m: number;
  enabled: boolean;
  updated_at: string;
  api_key_set: boolean;
  api_key_hint: string;
}

export interface DocumentMeta {
  id: string;
  filename: string;
  media_type: string;
  size_bytes: number;
  sha256: string;
  char_count: number;
  warnings: string[];
  created_at: string;
}
