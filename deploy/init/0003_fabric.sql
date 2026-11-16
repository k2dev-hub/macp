-- Fabric anchoring: the fingerprint of a provenance record is submitted to a
-- consortium chain.  The hash chain stays the local source of truth; the chain
-- transaction is the external, multi-party witness of it.

alter table build_provenance
  add column if not exists fabric_tx_id    text,
  add column if not exists fabric_block    bigint,
  add column if not exists fabric_status   text,
  add column if not exists fabric_error    text,
  add column if not exists fabric_anchored timestamptz;

create index if not exists build_provenance_fabric_idx
  on build_provenance (project_id, fabric_status);
