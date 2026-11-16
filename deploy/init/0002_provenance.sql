-- Build provenance: one immutable record per artifact-producing gate run.
--
-- The record answers the questions a provenance page has to answer: what was
-- built, from which commit, with which command and dependencies, in which
-- environment, by whom, and when.  Only hashes are stored here; artifacts stay
-- in the workspace and are re-hashed on verification.

create table if not exists build_provenance (
  id                uuid primary key default gen_random_uuid(),
  project_id        uuid not null references projects(id) on delete restrict,
  build_id          text not null,
  task_id           uuid references tasks(id) on delete restrict,
  task_code         text not null,
  attempt           integer not null default 1,
  gate              text not null,
  platform          text,
  profile           text,
  vm                text,
  artifact_name     text,
  artifact_sha256   text check (artifact_sha256 ~ '^[a-f0-9]{64}$'),
  artifact_size     bigint,
  artifact_path     text,
  source_commit     text,
  source_ref        text,
  build_command     jsonb not null default '[]'::jsonb,
  build_env         jsonb not null default '{}'::jsonb,
  dependencies      jsonb not null default '[]'::jsonb,
  dependency_count  integer not null default 0,
  builder           text,
  fingerprint       text not null check (fingerprint ~ '^[a-f0-9]{64}$'),
  signature         text,
  evidence_id       uuid references evidence(id) on delete restrict,
  event_id          uuid,
  created_at        timestamptz not null default now(),
  unique (project_id, build_id)
);

create index if not exists build_provenance_artifact_idx
  on build_provenance (project_id, artifact_sha256);
create index if not exists build_provenance_task_idx
  on build_provenance (project_id, task_code);
create index if not exists build_provenance_created_idx
  on build_provenance (project_id, created_at desc);
