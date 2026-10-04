# Single-host deployment

This deploys the existing pipeline, not a separate demo. It is not an HA topology.
No business records, enterprise credentials or production certificates are bundled.
The $35,000 ceiling and approval protections remain unchanged.

## Provisioning

Copy `production.env.example` to `production.env`. Set `PUBLIC_HOST`, an explicit
HTTPS CORS hostname, the authoritative source identifier, and
`BUSINESS_SOURCE_TRUST=AUTHORITATIVE` only after verifying source ownership.
For JSON/CSV put the maintained source under `deploy/business` and configure its
container path `/business/...`; publish updates atomically. Alternatively configure
`BUSINESS_API_URL` with the existing BusinessInputs HTTPS response contract.
Mock/demo/test/fixture-labelled records and sources are rejected in production.
Trust attestation is an operator assertion, not automatic verification of ownership.

Create `deploy/secrets/operator_token` and `approver_token` with distinct random
credentials of at least 32 characters, plus `ais_key`, `business_token`, `erp_token`.
Unused provider token files may be empty. Restrict these files to the deployment
account; never commit them. Supply `deploy/tls/cert.pem` (full certificate chain)
and `key.pem` for PUBLIC_HOST. TLS terminates at Caddy; internal services expose
no host ports. The edge binds loopback by default. Explicitly configure the desired
bind/443 mapping and host firewall only after TLS and credentials are verified.

ERP requires `ERP_MODE=http`, HTTPS `ERP_URL`, bearer token, and a documented
`ERP_HEALTH_PATH` returning HTTP 200. The existing gateway contract is
POST `/purchase-orders` and GET `/purchase-orders/by-idempotency/{key}`;
responses must satisfy ExecutionResult and echo exact authorized actions, plan hash,
idempotency key and PO identifier. Obtain and test the actual vendor mapping before
allowing purchases. Configured does not mean connected. Simulation is forbidden here.

## Start and verify (repository root)

```powershell
docker compose --env-file deploy/production.env -f deploy/compose.yml config --quiet
docker compose --env-file deploy/production.env -f deploy/compose.yml up -d --build
docker compose --env-file deploy/production.env -f deploy/compose.yml ps
docker compose --env-file deploy/production.env -f deploy/compose.yml exec api python -m scripts.production_verify --url https://YOUR_HOST --request /business/YOUR_REAL_REQUEST.json
```

The verification command reads the operator token from configuration, checks TLS,
authenticated readiness and operations, optionally submits a supplied request, and
polls its actual result. It never approves or submits a purchase. `/livez` indicates
process liveness only; authenticated `/readyz` returns 503 until binding dependencies
are verified. `/api/v1/operations` exposes failures, unresolved executions and source
health for an authenticated external monitor; alert on NOT_READY, failed runs and
unresolved execution outcomes. Telemetry quality is visible without making unrelated
weather/freight mandatory for replenishment.

## Durability and recovery

Named volumes hold SQLite state/history, Kafka logs and Redis AOF. Do not use
`down -v`. RunStore upgrades version-0 databases idempotently to schema version 1
and rejects newer schemas. API restart fences expired workflow leases; unstarted
drafts are recoverable via authenticated `/start`. It never replays ambiguous ERP POSTs.
Use authenticated `/reconcile` and the ERP lookup contract for unknown submissions.

Online SQLite backup, with WAL handled by SQLite's backup API:

```powershell
docker compose --env-file deploy/production.env -f deploy/compose.yml exec api python -m scripts.backup_state backup /app/data/backup-YYYYMMDD --runs /app/data/decision_runs.db --history /app/data/ais_history.db
docker compose --env-file deploy/production.env -f deploy/compose.yml cp api:/app/data/backup-YYYYMMDD backups/backup-YYYYMMDD
python -m scripts.backup_state restore backups/restore-check --source backups/backup-YYYYMMDD
```

Store backups encrypted off-host with retention and monitored scheduling. Restore
checks hashes and SQLite integrity and refuses existing destinations. Before live
restore stop writers, preserve the old volume, restore into a new volume and verify
approval, command and idempotency records before switching. Redis/Kafka volume
backups require coordinated stop/quiescence and platform volume snapshots; this
repository does not claim those external backup schedules or host snapshots exist.
Single-host loss, host firewall, certificate renewal, backup scheduling and alert
delivery must be provisioned and tested by the deployment environment.

## Local verification without enterprise connectivity

```powershell
python -m ruff check .
python -m scripts.check_repository
$env:REDIS_INTEGRATION='1'; python -m pytest -q
npm --prefix dashboard/frontend test
npm --prefix dashboard/frontend run lint
docker build -t exochain-engine:production .
docker build -t exochain-dashboard:production dashboard/frontend
python -m scripts.deployment_smoke
```

Smoke checks use an isolated Compose project and ephemeral test CA, no business
records and no ERP calls. It verifies TLS, authorization, all 19 agents, blocked
missing-data behavior and restart persistence, then removes its containers/network.
Its volumes remain for inspection. Feasible PO/timeout/concurrency/reconciliation
paths use isolated fixtures in the test suite; they do not prove a live ERP connection.

To verify the existing explicitly local mock scenario without changing policy/data:

```powershell
python -m scripts.verify_replenishment_live --inventory-ids INV-001 INV-002 --budget 35000 --simulation
```

This targets the existing development API at localhost:8000, not production. Its
minimum feasible commitment is $117,227, so it must remain BLOCKED with no approval.
