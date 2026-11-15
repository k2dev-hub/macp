-- ============================================================
-- MACP v0.2 reference schema (PostgreSQL 16)
-- ============================================================

create extension if not exists pgcrypto;

-- ---------- 项目与仓库 ----------

create table projects (
  id            uuid primary key default gen_random_uuid(),
  code          text not null unique check (code ~ '^[a-z][a-z0-9-]{1,31}$'),
  name          text not null check (length(name) between 1 and 200),
  policy        jsonb not null default '{}'::jsonb,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now(),
  version       integer not null default 1 check (version > 0)
);

create table repos (
  id            uuid primary key default gen_random_uuid(),
  project_id    uuid not null references projects(id) on delete restrict,
  code          text not null check (code ~ '^[a-z][a-z0-9-]{1,31}$'),
  mirror_path   text not null,
  main_ref      text not null default 'refs/heads/main',
  gate_profile  jsonb not null default '{}'::jsonb,
  active        boolean not null default true,
  created_at    timestamptz not null default now(),
  unique (project_id, code)
);

-- ---------- 执行者与会话 ----------

create table agents (
  id              uuid primary key default gen_random_uuid(),
  project_id      uuid not null references projects(id) on delete restrict,
  code            text not null check (code ~ '^[a-z][a-z0-9-]{1,31}$'),
  role            text not null check (role in ('owner','coordinator','worker','observer','runner','service')),
  model_policy    jsonb not null default '{}'::jsonb,
  capabilities    text[] not null default '{}',
  allowed_paths   text[] not null default '{}',
  budget          jsonb not null default '{}'::jsonb,
  status          text not null default 'offline'
                  check (status in ('offline','idle','working','waiting_user','blocked','stalled','dead')),
  current_task_id uuid,
  heartbeat_at    timestamptz,
  token_version   integer not null default 1 check (token_version > 0),
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  unique (project_id, code)
);

create table sessions (
  id                  uuid primary key default gen_random_uuid(),
  agent_id            uuid not null references agents(id) on delete restrict,
  authority           text not null,
  external_session_id text not null,
  model               text not null,
  prompt_version      text,
  context_used        integer not null default 0 check (context_used >= 0),
  context_limit       integer not null check (context_limit > 0),
  status              text not null default 'active'
                      check (status in ('active','waiting_user','blocked','stalled','completed','archived','dead')),
  current_task_id     uuid,
  last_turn_id        text,
  started_at          timestamptz not null default now(),
  ended_at            timestamptz,
  unique (agent_id, external_session_id)
);

-- ---------- 工作分区 ----------

create table workstreams (
  id                  uuid primary key default gen_random_uuid(),
  project_id          uuid not null references projects(id) on delete restrict,
  code                text not null check (code ~ '^[a-z][a-z0-9-]{1,31}$'),
  name                text not null,
  responsibility      text not null default '',
  write_surface       jsonb not null default '[]'::jsonb,
  acceptance_profile  jsonb not null default '{}'::jsonb,
  wip_limit           integer not null default 1 check (wip_limit between 1 and 10),
  owner_agent_id      uuid references agents(id) on delete restrict,
  active              boolean not null default true,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  unique (project_id, code)
);

-- ---------- 任务与说明书 ----------

create table tasks (
  id                  uuid primary key default gen_random_uuid(),
  project_id          uuid not null references projects(id) on delete restrict,
  code                text not null check (code ~ '^T-[0-9]{4,}$'),
  workstream_id       uuid not null references workstreams(id) on delete restrict,
  title               text not null check (length(title) between 1 and 200),
  kind                text not null check (kind in ('slice','fix','infra','migration','spike','docs')),
  priority            text not null default 'P2' check (priority in ('P0','P1','P2','P3')),
  state               text not null default 'draft'
                      check (state in ('draft','ready','assigned','in_progress','blocked','review',
                                       'accepted','merge_queued','merging','merged','done',
                                       'reopened','cancelled','failed')),
  current_attempt     integer not null default 0 check (current_attempt >= 0),
  active_work_order_id uuid,
  current_session_id  uuid references sessions(id) on delete restrict,
  budget              jsonb not null default '{}'::jsonb,
  acceptance_profile  jsonb not null default '{}'::jsonb,
  completion_contract jsonb not null default '{}'::jsonb,
  created_by          uuid not null references agents(id) on delete restrict,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  ready_at            timestamptz,
  assigned_at         timestamptz,
  accepted_at         timestamptz,
  merged_at           timestamptz,
  done_at             timestamptz,
  cancelled_at        timestamptz,
  reopen_count        integer not null default 0 check (reopen_count >= 0),
  version             integer not null default 1 check (version > 0),
  unique (project_id, code)
);

create table task_dependencies (
  task_id             uuid not null references tasks(id) on delete restrict,
  depends_on_task_id  uuid not null references tasks(id) on delete restrict,
  kind                text not null default 'hard' check (kind in ('hard','soft')),
  created_at          timestamptz not null default now(),
  primary key (task_id, depends_on_task_id),
  check (task_id <> depends_on_task_id)
);

create table work_orders (
  id                uuid primary key default gen_random_uuid(),
  task_id           uuid not null references tasks(id) on delete restrict,
  version           integer not null check (version >= 1),
  body              jsonb not null,
  body_hash         text not null check (body_hash ~ '^sha256:[a-f0-9]{64}$'),
  status            text not null default 'draft'
                    check (status in ('draft','validated','active','superseded','expired')),
  validation_errors jsonb not null default '[]'::jsonb,
  validated_at      timestamptz,
  created_by        uuid not null references agents(id) on delete restrict,
  created_at        timestamptz not null default now(),
  unique (task_id, version)
);

create table attempts (
  id                uuid primary key default gen_random_uuid(),
  task_id           uuid not null references tasks(id) on delete restrict,
  number            integer not null check (number >= 1),
  state             text not null default 'created'
                    check (state in ('created','active','submitted','closed','abandoned')),
  parent_attempt_id uuid references attempts(id) on delete restrict,
  base_commit       text,
  branch            text,
  candidate_ref     text,
  patch_digest      text check (patch_digest is null or patch_digest ~ '^sha256:[a-f0-9]{64}$'),
  created_at        timestamptz not null default now(),
  submitted_at      timestamptz,
  closed_at         timestamptz,
  unique (task_id, number)
);

create table assignments (
  id                uuid primary key default gen_random_uuid(),
  task_id           uuid not null references tasks(id) on delete restrict,
  attempt_id        uuid references attempts(id) on delete restrict,
  agent_id          uuid not null references agents(id) on delete restrict,
  state             text not null default 'offered'
                    check (state in ('offered','claimed','active','submitted','closed','expired','revoked')),
  lease_expires_at  timestamptz,
  heartbeat_at      timestamptz,
  fencing_token     bigint not null check (fencing_token > 0),
  created_at        timestamptz not null default now(),
  claimed_at        timestamptz,
  closed_at         timestamptz
);

-- 一个任务同时最多一个有效 assignment
create unique index uq_assignments_one_active
  on assignments(task_id)
  where state in ('offered','claimed','active','submitted');

-- ---------- 工作副本 ----------

create table workspaces (
  id            uuid primary key default gen_random_uuid(),
  task_id       uuid not null references tasks(id) on delete restrict,
  attempt_id    uuid not null references attempts(id) on delete restrict,
  repo_id       uuid not null references repos(id) on delete restrict,
  path          text not null,
  branch        text not null,
  base_commit   text not null,
  state         text not null default 'creating'
                check (state in ('creating','ready','submitted','cleaned','failed')),
  created_at    timestamptz not null default now(),
  cleaned_at    timestamptz,
  unique (attempt_id)
);

-- ---------- 路径租约 ----------

create table leases (
  id              uuid primary key default gen_random_uuid(),
  project_id      uuid not null references projects(id) on delete restrict,
  path_glob       text not null,
  lease_key       text not null,
  mode            text not null check (mode in ('read','write','append')),
  owner_task_id   uuid not null references tasks(id) on delete restrict,
  owner_attempt_id uuid references attempts(id) on delete restrict,
  owner_agent_id  uuid not null references agents(id) on delete restrict,
  fencing_token   bigint not null check (fencing_token > 0),
  reason          text not null default '',
  state           text not null default 'active'
                  check (state in ('active','expired','released','revoked')),
  expires_at      timestamptz not null,
  renewed_at      timestamptz,
  released_at     timestamptz,
  created_at      timestamptz not null default now()
);

-- ---------- 门与证据 ----------

create table gate_defs (
  id            uuid primary key default gen_random_uuid(),
  project_id    uuid not null references projects(id) on delete restrict,
  code          text not null check (code ~ '^[a-z][a-z0-9-]{1,63}$'),
  version       integer not null check (version >= 1),
  level         text not null check (level in ('L1','L2','L3','security')),
  body          jsonb not null,
  body_hash     text not null check (body_hash ~ '^sha256:[a-f0-9]{64}$'),
  status        text not null default 'active'
                check (status in ('active','deprecated','disabled')),
  approved_by   uuid references agents(id) on delete restrict,
  approved_at   timestamptz,
  created_at    timestamptz not null default now(),
  unique (project_id, code, version)
);

create table evidence (
  id              uuid primary key default gen_random_uuid(),
  project_id      uuid not null references projects(id) on delete restrict,
  sha256          text not null check (sha256 ~ '^[a-f0-9]{64}$'),
  uri             text not null,
  media_type      text not null,
  size_bytes      bigint not null check (size_bytes >= 0),
  kind            text not null check (kind in ('log','artifact','report','screenshot','manifest','trace')),
  signature       text,
  retention_until timestamptz,
  created_at      timestamptz not null default now(),
  unique (project_id, sha256)
);

create table gate_runs (
  id              uuid primary key default gen_random_uuid(),
  project_id      uuid not null references projects(id) on delete restrict,
  gate_def_id     uuid not null references gate_defs(id) on delete restrict,
  task_id         uuid not null references tasks(id) on delete restrict,
  attempt_id      uuid references attempts(id) on delete restrict,
  input_digest    text not null check (input_digest ~ '^sha256:[a-f0-9]{64}$'),
  state           text not null default 'queued'
                  check (state in ('queued','running','passed','failed','error','skipped','quarantined')),
  exit_code       integer,
  runner_id       text,
  evidence_id     uuid references evidence(id) on delete restrict,
  baseline_diff   jsonb not null default '[]'::jsonb,
  logs_uri        text,
  signature       text,
  started_at      timestamptz,
  finished_at     timestamptz,
  created_at      timestamptz not null default now()
);

-- ---------- 回执与评审 ----------

create table receipts (
  id                uuid primary key default gen_random_uuid(),
  project_id        uuid not null references projects(id) on delete restrict,
  code              text not null check (code ~ '^R-[0-9]{6,}$'),
  task_id           uuid not null references tasks(id) on delete restrict,
  attempt_id        uuid not null references attempts(id) on delete restrict,
  agent_id          uuid not null references agents(id) on delete restrict,
  session_id        uuid references sessions(id) on delete restrict,
  work_order_hash   text not null check (work_order_hash ~ '^sha256:[a-f0-9]{64}$'),
  base_commit       text not null,
  candidate_ref     text not null,
  body              jsonb not null,
  body_hash         text not null check (body_hash ~ '^sha256:[a-f0-9]{64}$'),
  status            text not null default 'submitted'
                    check (status in ('submitted','accepted','rejected','superseded')),
  stale_base        boolean not null default false,
  signature         text,
  created_at        timestamptz not null default now(),
  unique (project_id, code),
  unique (task_id, attempt_id)
);

create table reviews (
  id                  uuid primary key default gen_random_uuid(),
  receipt_id          uuid not null unique references receipts(id) on delete restrict,
  reviewer_agent_id   uuid not null references agents(id) on delete restrict,
  decision            text not null check (decision in ('accept','reject')),
  findings            jsonb not null default '[]'::jsonb,
  evidence_set_hash   text not null check (evidence_set_hash ~ '^sha256:[a-f0-9]{64}$'),
  signature           text,
  created_at          timestamptz not null default now(),
  check (decision <> 'reject' or jsonb_array_length(findings) > 0)
);

-- ---------- 合并 ----------

create table merge_requests (
  id                  uuid primary key default gen_random_uuid(),
  project_id          uuid not null references projects(id) on delete restrict,
  task_id             uuid not null references tasks(id) on delete restrict,
  attempt_id          uuid not null references attempts(id) on delete restrict,
  candidate_ref       text not null,
  target_ref          text not null default 'refs/heads/main',
  base_commit         text not null,
  state               text not null default 'queued'
                      check (state in ('queued','verifying','merging','merged','pushed',
                                       'conflict','needs_resolution','failed','reverted','cancelled')),
  wave                integer not null default 0,
  verification_run_ids jsonb not null default '[]'::jsonb,
  merge_commit        text,
  pushed_at           timestamptz,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now()
);

create unique index uq_merge_active
  on merge_requests(task_id)
  where state in ('queued','verifying','merging','conflict','needs_resolution');

create table merge_steps (
  id                uuid primary key default gen_random_uuid(),
  merge_request_id  uuid not null references merge_requests(id) on delete restrict,
  step              text not null check (step in ('rebase','verify','lease_check','id_check',
                                                 'approval_check','fast_forward','push','post_verify')),
  state             text not null default 'pending'
                    check (state in ('pending','running','passed','failed','skipped')),
  detail            jsonb not null default '{}'::jsonb,
  started_at        timestamptz,
  finished_at       timestamptz
);

-- ---------- 审批与决策 ----------

create table approvals (
  id              uuid primary key default gen_random_uuid(),
  project_id      uuid not null references projects(id) on delete restrict,
  task_id         uuid references tasks(id) on delete restrict,
  action          text not null,
  resource        jsonb not null default '{}'::jsonb,
  risk            text not null check (risk in ('low','medium','high','critical')),
  options         jsonb not null default '[]'::jsonb,
  default_option  text not null,
  requested_by    uuid not null references agents(id) on delete restrict,
  requested_at    timestamptz not null default now(),
  expires_at      timestamptz,
  decision        text check (decision in ('approved','denied','expired')),
  decided_by      uuid references agents(id) on delete restrict,
  decided_at      timestamptz,
  rationale       text,
  check (jsonb_typeof(options) = 'array')
);

create table decisions (
  id                      uuid primary key default gen_random_uuid(),
  project_id              uuid not null references projects(id) on delete restrict,
  question                text not null,
  options                 jsonb not null default '[]'::jsonb,
  choice                  text not null,
  rationale               text not null,
  decider_agent_id        uuid not null references agents(id) on delete restrict,
  related_task_id         uuid references tasks(id) on delete restrict,
  supersedes_decision_id  uuid references decisions(id) on delete restrict,
  created_at              timestamptz not null default now(),
  check (jsonb_typeof(options) = 'array')
);

-- ---------- 事件与投递 ----------

create table events (
  seq                 bigserial primary key,
  id                  uuid not null unique default gen_random_uuid(),
  project_id          uuid not null references projects(id) on delete restrict,
  type                text not null check (type ~ '^[a-z]+\.[a-z_]+$'),
  version             integer not null default 1 check (version >= 1),
  aggregate_type      text not null,
  aggregate_id        uuid not null,
  aggregate_version   integer not null check (aggregate_version >= 1),
  actor_type          text not null check (actor_type in ('human','agent','service','system')),
  actor_id            uuid,
  session_id          uuid,
  correlation_id      text,
  causation_id        uuid,
  idempotency_key     text not null unique,
  payload             jsonb not null default '{}'::jsonb,
  occurred_at         timestamptz not null default now(),
  prev_hash           text,
  hash                text not null check (hash ~ '^sha256:[a-f0-9]{64}$')
  -- 状态类事件的 aggregate_version 由应用层保证递增；
  -- 非状态事件（心跳/告警）可复用版本，因此不做唯一约束。
);

create table outbox (
  id            bigserial primary key,
  event_id      uuid not null unique references events(id) on delete restrict,
  topic         text not null default 'macp.events',
  available_at  timestamptz not null default now(),
  dispatched_at timestamptz,
  attempts      integer not null default 0 check (attempts >= 0)
);

-- ---------- 全局 ID ----------

create table id_namespaces (
  id          uuid primary key default gen_random_uuid(),
  project_id  uuid not null references projects(id) on delete restrict,
  code        text not null check (code ~ '^[a-z][a-z0-9_.-]{1,63}$'),
  next_value  bigint not null default 1 check (next_value >= 1),
  block_size  integer not null default 10 check (block_size between 1 and 1000),
  updated_at  timestamptz not null default now(),
  unique (project_id, code)
);

create table id_reservations (
  id            uuid primary key default gen_random_uuid(),
  namespace_id  uuid not null references id_namespaces(id) on delete restrict,
  task_id       uuid not null references tasks(id) on delete restrict,
  attempt_id    uuid references attempts(id) on delete restrict,
  start_value   bigint not null,
  end_value     bigint not null,
  used_values   bigint[] not null default '{}',
  state         text not null default 'reserved'
                check (state in ('reserved','committed','released')),
  created_at    timestamptz not null default now(),
  check (start_value >= 1 and start_value <= end_value)
);

create table id_registry (
  namespace_id  uuid not null references id_namespaces(id) on delete restrict,
  value         bigint not null check (value >= 1),
  task_id       uuid not null references tasks(id) on delete restrict,
  created_at    timestamptz not null default now(),
  primary key (namespace_id, value)
);

-- ---------- 热文件 / 已知问题 / 待验证 ----------

create table hotfile_policies (
  id                  uuid primary key default gen_random_uuid(),
  project_id          uuid not null references projects(id) on delete restrict,
  path_glob           text not null,
  lease_key           text not null,
  owner_workstream_id uuid references workstreams(id) on delete restrict,
  strategy            text not null check (strategy in ('single_writer','append_segment','generated','id_allocator','read_only')),
  append_marker       text,
  max_growth_bytes    bigint check (max_growth_bytes is null or max_growth_bytes >= 0),
  active              boolean not null default true,
  created_at          timestamptz not null default now()
);

create table known_issues (
  id              uuid primary key default gen_random_uuid(),
  project_id      uuid not null references projects(id) on delete restrict,
  signature       text not null,
  owner_agent_id  uuid references agents(id) on delete restrict,
  trigger         text not null,
  acceptance      text not null,
  state           text not null default 'open' check (state in ('open','fixed','accepted')),
  created_at      timestamptz not null default now(),
  unique (project_id, signature)
);

create table pending_validations (
  id            uuid primary key default gen_random_uuid(),
  project_id    uuid not null references projects(id) on delete restrict,
  task_id       uuid not null references tasks(id) on delete restrict,
  receipt_id    uuid not null references receipts(id) on delete restrict,
  item          text not null,
  owner_agent_id uuid not null references agents(id) on delete restrict,
  acceptance    text not null,
  due_at        timestamptz,
  state         text not null default 'open' check (state in ('open','closed','waived')),
  evidence_id   uuid references evidence(id) on delete restrict,
  closed_at     timestamptz,
  created_at    timestamptz not null default now()
);

-- ---------- 任务状态历史 ----------

create table task_state_transitions (
  id          bigserial primary key,
  task_id     uuid not null references tasks(id) on delete restrict,
  from_state  text,
  to_state    text not null,
  at          timestamptz not null default now(),
  actor_type  text not null check (actor_type in ('human','agent','service','system')),
  actor_id    uuid,
  reason      text
);

-- ---------- 观察者 ----------

create table observer_cursors (
  consumer    text primary key,
  since_seq   bigint not null default 0,
  updated_at  timestamptz not null default now()
);

create table observer_dedupe (
  consumer    text not null,
  dedupe_key  text not null,
  event_id    uuid not null references events(id) on delete restrict,
  sent_at     timestamptz not null default now(),
  primary key (consumer, dedupe_key)
);

create table observer_notifications (
  id          uuid primary key default gen_random_uuid(),
  consumer    text not null,
  dedupe_key  text not null,
  event_id    uuid not null references events(id) on delete restrict,
  channel     text not null,
  status      text not null check (status in ('sent','failed','skipped')),
  sent_at     timestamptz not null default now()
);

create table alerts (
  id          uuid primary key default gen_random_uuid(),
  project_id  uuid not null references projects(id) on delete restrict,
  severity    text not null check (severity in ('info','warning','critical')),
  code        text not null,
  resource    jsonb not null default '{}'::jsonb,
  state       text not null default 'open' check (state in ('open','acked','cleared')),
  first_seen  timestamptz not null default now(),
  last_seen   timestamptz not null default now(),
  count       integer not null default 1 check (count >= 1),
  details     jsonb not null default '{}'::jsonb
);

-- ---------- 指标与审计 ----------

create table metrics_daily (
  day         date not null,
  project_id  uuid not null references projects(id) on delete restrict,
  metric      text not null,
  value       numeric not null,
  meta        jsonb not null default '{}'::jsonb,
  primary key (day, project_id, metric)
);

create table audit_anchors (
  id            uuid primary key default gen_random_uuid(),
  day           date not null unique,
  last_seq      bigint not null,
  hash          text not null check (hash ~ '^sha256:[a-f0-9]{64}$'),
  external_ref  text,
  created_at    timestamptz not null default now()
);

-- ---------- 交接与派单授权 ----------

create table handoffs (
  id                uuid primary key default gen_random_uuid(),
  task_id           uuid not null references tasks(id) on delete restrict,
  attempt_id        uuid references attempts(id) on delete restrict,
  from_session_id   uuid references sessions(id) on delete restrict,
  to_session_id     uuid references sessions(id) on delete restrict,
  packet            jsonb not null,
  hash              text not null check (hash ~ '^sha256:[a-f0-9]{64}$'),
  created_at        timestamptz not null default now()
);

create table dispatch_grants (
  id                  uuid primary key default gen_random_uuid(),
  project_id          uuid not null references projects(id) on delete restrict,
  issuer_agent_id     uuid not null references agents(id) on delete restrict,
  subject_agent_id    uuid not null references agents(id) on delete restrict,
  can_dispatch        boolean not null default false,
  max_depth           integer not null default 2 check (max_depth between 0 and 5),
  allowed_workstreams jsonb not null default '[]'::jsonb,
  expires_at          timestamptz,
  created_at          timestamptz not null default now()
);

create table agent_tokens (
  id          uuid primary key default gen_random_uuid(),
  agent_id    uuid not null references agents(id) on delete restrict,
  token_hash  text not null unique,
  scopes      text[] not null default '{}',
  task_id     uuid references tasks(id) on delete restrict,
  attempt_id  uuid references attempts(id) on delete restrict,
  expires_at  timestamptz not null,
  revoked_at  timestamptz,
  created_at  timestamptz not null default now()
);

-- ---------- 循环外键（建表后补） ----------

alter table tasks
  add constraint fk_tasks_active_work_order
  foreign key (active_work_order_id) references work_orders(id) on delete restrict;

alter table agents
  add constraint fk_agents_current_task
  foreign key (current_task_id) references tasks(id) on delete restrict;

alter table sessions
  add constraint fk_sessions_current_task
  foreign key (current_task_id) references tasks(id) on delete restrict;

-- ---------- indexes ----------
-- 任务查询
create index ix_tasks_project_state_priority
  on tasks(project_id, state, priority, created_at);
create index ix_tasks_workstream_state
  on tasks(workstream_id, state);
create index ix_tasks_ready
  on tasks(project_id, priority, created_at)
  where state = 'ready';

-- 依赖
create index ix_task_deps_depends_on on task_dependencies(depends_on_task_id);

-- 会话/执行者
create index ix_agents_project_status on agents(project_id, status);
create index ix_agents_heartbeat on agents(heartbeat_at);
create index ix_sessions_agent_status on sessions(agent_id, status);

-- 分配/工作副本
create index ix_assignments_state_heartbeat on assignments(state, heartbeat_at);
create index ix_workspaces_state on workspaces(state);

-- 租约
create index ix_leases_active_key
  on leases(project_id, lease_key)
  where state = 'active';
create index ix_leases_expiry
  on leases(expires_at)
  where state = 'active';
create index ix_leases_owner on leases(owner_task_id);

-- 门
create index ix_gate_runs_task_state on gate_runs(task_id, state);
create index ix_gate_runs_input on gate_runs(input_digest);
create index ix_gate_defs_project_level on gate_defs(project_id, level, status);

-- 回执/评审
create index ix_receipts_task_status on receipts(task_id, status);
create index ix_reviews_reviewer on reviews(reviewer_agent_id);

-- 合并
create index ix_merge_state on merge_requests(project_id, state, created_at);
create index ix_merge_steps_mr on merge_steps(merge_request_id, started_at);

-- 事件
create index ix_events_project_seq on events(project_id, seq);
create index ix_events_type_time on events(type, occurred_at);
create index ix_events_correlation on events(correlation_id);
create index ix_events_aggregate on events(aggregate_type, aggregate_id, seq);
create index ix_outbox_pending on outbox(available_at)
  where dispatched_at is null;

-- 审批/决策
create index ix_approvals_pending on approvals(project_id, expires_at)
  where decision is null;
create index ix_decisions_task on decisions(related_task_id);

-- 待验证
create index ix_pending_validations_open
  on pending_validations(project_id, due_at)
  where state = 'open';

-- 观察者/告警
create index ix_observer_notifications_time on observer_notifications(sent_at);
create index ix_alerts_open on alerts(project_id, severity, last_seen)
  where state = 'open';

-- 状态历史
create index ix_task_state_transitions_task
  on task_state_transitions(task_id, at);

-- ---------- triggers ----------
create or replace function touch_updated_at() returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

create trigger trg_projects_touch before update on projects
  for each row execute function touch_updated_at();
create trigger trg_agents_touch before update on agents
  for each row execute function touch_updated_at();
create trigger trg_workstreams_touch before update on workstreams
  for each row execute function touch_updated_at();
create trigger trg_tasks_touch before update on tasks
  for each row execute function touch_updated_at();
create trigger trg_merge_requests_touch before update on merge_requests
  for each row execute function touch_updated_at();

create or replace function enforce_review_separation() returns trigger as $$
declare author uuid;
begin
  select agent_id into author from receipts where id = new.receipt_id;
  if author is null then
    raise exception 'receipt not found';
  end if;
  if author = new.reviewer_agent_id then
    raise exception 'reviewer must differ from author';
  end if;
  return new;
end;
$$ language plpgsql;

create trigger trg_review_separation before insert on reviews
  for each row execute function enforce_review_separation();
