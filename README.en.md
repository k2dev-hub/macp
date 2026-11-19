# MACP — Multi-Agent Coordination Plane

**English** · [中文](README.md)

MACP organises multi-agent coding as an auditable pipeline: the task graph is the
single source of truth, every artifact goes through Git, every delivery carries
a signed receipt, and every event is chained by hash. Agents work in sandboxes
or disposable virtual machines, and nothing merges until its gates pass.

## What it solves

In a multi-agent system the hard part is not model capability — it is knowing
who changed what, when, and on which evidence. The mechanisms below fix that
process into place:

- **The task graph is the source of truth.** A work order declares the write
  surface, the acceptance gates and the execution profile; the repository only
  holds the output.
- **Gates bind to the execution environment.** Where a build runs is where it is
  re-verified — separate gates for Linux, Android and Windows.
- **Deliveries are verifiable.** Candidate ref → gate results → an Ed25519-signed
  receipt → merge. Anyone can recompute the receipt from the public key.
- **History is tamper-evident.** An append-only event chain, daily anchors and an
  off-host mirror; editing one record breaks the hash.
- **Consortium anchoring (optional)**: with `[fabric]` enabled, each record's
  fingerprint is also submitted to a Fabric channel; the API only knows a local
  bridge address, and the chaincode keeps anchors idempotent per fingerprint;
- **Stamp package**: every build also emits `provenance.json`, `verify-qr.svg`,
  `verify-qr.png` and a short readme, archived with the artifact;

- **Merges are single-lane.** One merger at a time, a re-verification after the
  merge, and a rollback path when it fails.

## Quick start

```bash
git clone https://github.com/k2dev-hub/macp.git && cd macp
./deploy/bootstrap.sh                          # config, keys, TLS, self-check
docker compose -f deploy/docker-compose.yml up -d --build
```

Open `https://localhost:9443/`. The admin token is written to
`deploy/secrets/write_token` on first start (mode 0600, never committed, never
printed).

No model API key is required: the stack ships with a built-in `mock-openai`
service that drives the whole path — dispatch → write → gates → receipt → merge.
Point the large-model settings in `config/macp.toml` (or drop a key into
`deploy/secrets/deepseek_api_key`) to use a real model.

## Architecture

```
       browser / CLI / MCP client
                 │
            nginx (TLS)                    read-only 8088 / full access 9443
                 │
   ┌─────────────┴──────────────┐
   │  API (FastAPI + SSE)       │  MCP gateway /mcp (21 tools)
   └─────────────┬──────────────┘
                 │
   scheduler · coordinator · merger · observer · signer · runner
                 │
   ┌─────────┬───┴────┬──────────┬────────────┐
 sandbox    Linux VM  Android VM Windows VM   PostgreSQL
 (bwrap)    (KVM)     (KVM)      (static)      events / tasks / receipts
```

The path of one delivery:

1. The scheduler offers a ready task to an idle worker, which claims it and
   receives a **task-bound, short-lived token**.
2. The worker calls tools (`shell_run`, `gate_run`, `workspace_write_file`, …).
   Each call is checked against the task's work order — write surface and
   platform — and rejected with 403 when out of bounds.
3. Gates run a real build in the same execution profile; artifact SHA-256 values
   are recorded in the chain.
4. The candidate ref is submitted, the signer issues a receipt, the observer
   records the events.
5. The merger lands it single-lane, re-verifies, and rolls back on failure.

## Execution profiles

| Platform | Carrier | Use |
|---|---|---|
| `sandbox` | bwrap | fast iteration, interpreted scripts |
| `linux` | disposable Debian VM | `make` / `cargo` / real ELF artifacts |
| `android` | disposable Android VM | `gradle assembleDebug` → signed, verified APK |
| `windows` | Windows LTSC build host | `cmd` / MSBuild → PE artifacts |

`shell_run` must name a platform; the system routes to the matching executor and
refuses anything outside the task's declared allow-list.

## Build provenance

Every artifact-producing gate leaves a verifiable record without extra work from
the agent: artifact hash and size, source commit, the exact command, the
execution environment, the dependency list read from the worktree, the builder
and the time.

```bash
curl -H "Authorization: Bearer $TOKEN" https://<host>/v1/provenance/B-20261116-56C782
curl https://<host>/verify/B-20261116-56C782          # public verification page
curl https://<host>/verify/B-20261116-56C782/qr.svg   # QR code for a sticker
```

Verification recomputes the record fingerprint, the signature and the artifact
hash. Replace the artifact or edit any field and the page turns red, naming the
check that failed.

## Observability and operations

- `GET /v1/health`, `GET /metrics` (Prometheus text format)
- Observer heartbeat and event freshness; an external watchdog checks every
  minute and restarts what fails
- Daily backups (database, repository bundles, workspace), WAL archiving, PITR
  drills, restore drills
- Release gate `./deploy/release-check.sh`: health, audit chain, replication,
  systemd units, disk, three real build chains, platform routing, build provenance, browser E2E

## Configuration

Everything deployment-specific — addresses, paths, external services — lives in
`config/macp.toml` (template: `config/macp.example.toml`). After editing:

```bash
python3 deploy/render_env.py        # render deploy/.env
python3 -m server.settings doctor   # validate, then probe enabled integrations
```

It also runs with no config at all: single host, loopback only, no external
device. See [docs/CONFIG.md](docs/CONFIG.md).

## Documentation

| Topic | Location |
|---|---|
| Configuration and split deployments | [docs/CONFIG.md](docs/CONFIG.md) |
| Code walkthrough (for newcomers) | [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md) |
| MCP gateway | [docs/MCP.md](docs/MCP.md) |
| Consortium anchoring | [deploy/fabric/README.md](deploy/fabric/README.md) |
| Consortium demo handbook | [docs/FABRIC-DEMO.md](docs/FABRIC-DEMO.md) |
| Project site | [site/README.md](site/README.md) |
| Minimal audit edition | [deploy/minimal/README.md](deploy/minimal/README.md) |

## License

MIT — see [LICENSE](LICENSE).
