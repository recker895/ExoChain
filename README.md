# ExoChain Decision OS

Evidence-driven supply-chain planning with a Windows-compatible control tower.
Missing commercial data blocks execution. Real telemetry is not a customer order,
supplier quote, freight rate, navigational network, or approval.

## Run locally (PowerShell, repository root)

Python 3.13 and Node.js 20.9+ are required. Docker Desktop supplies the existing
Kafka 4.1 and Redis 7.4 services. Use a virtual environment for a clean install:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-dev.txt
if (!(Test-Path -LiteralPath .env)) { Copy-Item .env.example .env }
python -m scripts.configure_local_auth
docker compose up -d
python -m scripts.init_topics
```

**Do not overwrite an existing `.env`.** Fill in the existing AISStream and
Copernicus credentials locally. They are never included in this repository.
Set independent, high-entropy `OPERATOR_API_TOKEN` and `APPROVER_API_TOKEN` secrets.
`ENVIRONMENT=production` enforces different tokens of at least 32 characters.
The dashboard credential dialog holds tokens only in memory. Mutations are
unavailable until the required role is configured. All executable plans currently
require human approval, including plans below the legacy approval threshold.

Run these in separate terminals, from the repository root:

```powershell
python -m services.ais_ingestion
python -m services.maritime_consumer
python -m services.ais_history_recorder
python -m uvicorn dashboard.backend.main:app --host 127.0.0.1 --port 8000
```

```powershell
cd dashboard/frontend
npm ci
npm run dev
```

Open `http://127.0.0.1:3000`. API documentation is at
`http://127.0.0.1:8000/docs`. The frontend uses `NEXT_PUBLIC_API_URL` (default
`http://127.0.0.1:8000`). An enterprise MapLibre style can be configured with
`NEXT_PUBLIC_MAP_STYLE_URL`; the development map uses attributed OpenStreetMap
raster tiles. Map tiles are a background, never a navigation source.

Kafka topics: `telemetry-ais`, `telemetry-opensky`, `stgnn-graph-events`,
`agent-decision-logs`. AIS state and historical recording have independent
consumer groups. Never reset offsets or delete volumes to resolve a stale feed.
Docker listeners are bound to loopback. This compose topology is for host-based
development; deploying services inside containers requires internal advertised
Kafka listeners, TLS, authentication and an appropriate production topology.

## Architecture

```text
AISStream -> Kafka -> atomic Redis state/history + SQLite historical recorder
OpenSky / Open-Meteo / Copernicus / IMF PortWatch -> typed provider envelopes
JSON / CSV / authenticated HTTPS business adapters -> canonical business inputs
    -> six data domains with freshness and spatial joins
    -> seven intelligence domains
    -> six decision domains generating candidates
    -> navigation graph search + OR-Tools constrained optimization
    -> explicit-assumption Monte Carlo (optional)
    -> independent policy and cost-ledger validation
    -> durable human approval bound to immutable plan hash
    -> idempotent ERP adapter (unavailable, explicit simulation, or configured HTTPS)
    -> SQLite audit and Kafka transactional outbox -> authenticated SSE dashboard
```

`core/schemas/contracts.py` is the contract authority. `ExoChainOSState` in
`orchestrator/supervisor.py` explicitly declares every state boundary. Objects,
clients and models stay in dependency closures. Snapshots, candidates, constraints,
objective weights, solver metadata, approvals and results persist in
`data/decision_runs.db`. What-if runs reuse the original snapshot with a new run ID
and cannot execute. Agent start/completion events are actual persisted events.

## Connect business evidence

Use one of:

- Import canonical JSON in the dashboard or `POST /api/v1/runs` with
  `request` and `business_inputs`.
- Set `BUSINESS_DATA_PATH` to a JSON document or directory of CSV files.
- Set `BUSINESS_API_URL` and `BUSINESS_API_TOKEN` for a read-only HTTPS API
  returning the same canonical JSON shape.

The exact schema is exposed by `GET /api/v1/contracts/business` and checked by
`POST /api/v1/business/validate`. CSV filenames correspond to collection names:
`orders.csv`, `shipments.csv`, `skus.csv`, `demand.csv`, `inventory.csv`,
`suppliers.csv`, `supplier_quotes.csv`, `carriers.csv`, `carrier_options.csv`,
`disruptions.csv`; supply `route_network.json` separately. Nested values such as
`provenance` are JSON inside quoted CSV cells. Import adapters reject missing
required fields and unknown contracts. No sample business records are installed.

Every business entity requires identity, source, version, creation/observation and
expiry timestamps, quality, and provenance references. Inventory requires a
nonempty demand reference and an explicit demand quantity, service level,
lead time, costs, safety stock and capacity. A forecast is not automatically an
authorized purchase requirement.

`TRANSPORT` requires supplied shipment IDs and budget. Select `route` for supplied
navigation-network candidates or `modal` for actual carrier quotes. Joint route
and carrier costs are blocked until their commercial scope can be reconciled.
`REPLENISHMENT` requires inventory IDs and budget and always includes procurement.
`ASSESSMENT` gathers real evidence but cannot authorize execution.

## Solver and approval semantics

Route generation enumerates bounded loop-free paths on supplied directed edges,
using multiple Dijkstra objective profiles. Geometry comes only from those edges.
Draft, vessel permissions, speed, closure, validity and arrival limits filter paths.
There is no implied ENC coverage or global time-dependent routing optimum.

Route selection minimizes the integer-scaled sum of weighted normalized cost,
time, fuel, risk, weather and current penalties. Each metric is divided by its
maximum within the shipment candidate set. The dashboard percentages are each
weighted term divided by the total. `OPTIMAL` describes the CP-SAT model over
that generated set, not globally optimal ocean navigation.

Modal allocation satisfies exact shipment quantities with quote capacity,
duration, risk and budget limits. Inventory balances current stock, orders,
demand, shortage and ending stock; its objective includes actual supplied costs.
Procurement allocates the inventory order quantity to qualified supplier quotes.
Money constraints use integer cents, conservatively rounding commitments upward.
Inventory purchase estimates are replaced by supplier commitments in the final
ledger, never counted twice. These are sequential component optimizations, not
a claim of a globally optimal integrated supply chain.

Approval binds run, plan ID/version, complete evidence/candidate/solution hash,
cost, actions, approver and expiry. Pending stays pending until explicit approval,
rejection or expiry. Execution revalidates data freshness and reconciles prices,
quantities and costs. A changed plan invalidates approval. SQLite reserves an
idempotency key before calling ERP; uncertain submissions require reconciliation
and are never automatically duplicated.

Default `ERP_MODE=unavailable` returns `ERP_UNAVAILABLE`. Explicit simulation
requires both `ERP_MODE=simulation` and the request's simulation flag, and returns
`EXECUTION_SIMULATED`. The HTTPS adapter requires a structured acknowledgement
matching the idempotency key and an external reference for real execution.
Transport booking is unavailable; it is never substituted with a purchase order.

## Model deployment

Install optional training dependencies with `pip install -r requirements-ml.txt`.
Existing dataset building and training remain available:

```powershell
python -m services.stgnn_dataset_builder
python -m models.st_gnn.train_stgnn
```

Training a checkpoint does not approve it. `STGNN_MODEL_MANIFEST` must reference
SHA-256-verified checkpoint, training normalization and a matching independent
validation report. It must state the model version, exact feature names,
preprocessing version `ais-bin-mean-circular-v1-complete-window`, bin seconds,
input steps and max nodes. The report must explicitly approve trajectory
inference for that version/preprocessing contract. Runtime uses the training
architecture/features/adjacency and complete observed bins, never random weights
or invented missing history. The repository currently has no approved deployment
manifest; therefore operational forecasting returns `MODEL_UNAVAILABLE`.

## Verification

```powershell
python -m scripts.check_repository
python -m pytest -q
$env:REDIS_INTEGRATION='1'
python -m pytest -q
python -m scripts.verify_live --vessels 8
cd dashboard/frontend
npm run lint
npm run build
```

The Redis integration test uses unique `exochain-test:` keys and removes only its
own keys. Unit-test business fixtures are labeled `TEST_FIXTURE_ONLY` and never
loaded by the application or live verification. The live verification persists
a real assessment and a sanitized report at `data/live_verification.json`.

See [implementation report](docs/IMPLEMENTATION.md) for measured results, changed
files, limitations and acceptance assessment. Passing tests is not a production
readiness certification.

## CI/CD for the existing EC2 site

GitHub Actions tests and builds pull requests and `main`. Optional deployment to
the existing EC2 server includes health checks and code rollback. Deployment is
disabled until GitHub/AWS/server setup is completed; see
[EC2 pipeline setup](deploy/ec2/README.md) before enabling it.
