# Replenishment operations

## College demonstration

The new-run form defaults to **College demo**. Select Inventory replenishment,
use `INV-001, INV-002` and a budget of `125000`, and leave the JSON input empty.
The backend supplies a labeled classroom dataset with current demo timestamps.
An uploaded JSON dataset can also be replayed on the demo clock; quantities and
prices are retained, together with the original provenance.

All 19 agent functions are invoked. Missing live AIS/weather/ERP feeds are optional.
The CP-SAT model still calculates quantities and supplier allocations using the
entered budget, capacity, risk and lead-time limits. Approval and order submission
are simulated locally, resulting in `EXECUTION_SIMULATED`; no ERP connection is
needed. Repeating the demonstration does not reserve inventory or supplier capacity.

With the built-in sample and a budget of 125000, the full plan orders 730 bearings
and 710 pumps for a total commitment of 120275 USD, including fixed ordering costs.
An impossible request completes as `DEMO_COMPLETE` with constraint warnings and
no simulated order. A data assessment completes without requiring a purchase plan.
Old recorded runs retain their previous status; create a new run after updating.

Disable College demo to use the original business-source/approval workflow below.
Demo requests are unavailable when `ENVIRONMENT=production`.

The existing six-stage graph now carries a versioned business snapshot through the
19 agent records, inventory/procurement CP-SAT models, validation and bound approval.
Execution and reconciliation remain separately authorized operator API operations.

## Business source

Set `BUSINESS_DATA_PATH` to an authoritative JSON file or a CSV directory, or set
`BUSINESS_API_URL` and `BUSINESS_API_TOKEN` for an HTTPS endpoint returning the same
`BusinessInputs` JSON object. API configuration takes precedence. Set
`BUSINESS_SOURCE_ID` to a stable system identifier; otherwise a digest of the configured
source location identifies it. Neither credentials nor the URL enter run snapshots.

Use `GET /api/v1/contracts/business` or `docs/business-input.schema.json` for the
contract. CSV filenames match the business domains (for example `inventory.csv`,
`supplier_quotes.csv`, `suppliers.csv`, `skus.csv`); nested provenance is JSON within
the CSV cell. Publish file exports atomically. Directory changes during capture are
rejected. Records require source, identity, version, timestamps, validity, quality
and provenance. Every selected inventory SKU must exist in `skus`; its demand
reference must identify a demand record or a provenance reference. Demand quantities
remain source supplied; forecasts never silently replace them.

Entity versions must change when commercial facts change. In particular, inventory
must reflect outstanding orders and quotes must reflect remaining supplier capacity
before their next version is published. SQLite reservations prevent reusing an
inventory version and prevent oversubscribing a quote version across runs.

`GET /api/v1/business/source` verifies the configured source and reports quality,
source identity, content hash, and record versions. It requires operator or approver
credentials. Missing configuration reports `BUSINESS_SOURCE_NOT_CONFIGURED`.
Missing or invalid evidence does not create an executable plan.

Caller imports remain available for review and explicit simulations. Production HTTP
execution re-reads the configured source and requires exact content equality with the
approved snapshot. Configured snapshots are also rechecked before requesting and
granting approval. A changed snapshot requires a new run and approval; it is never
patched into an approved plan. The ERP receives snapshot hash and source versions
to support its own concurrency checks at the final system-of-record boundary.

Typed disruptions can declare `supply_effect: "UNAVAILABLE"` with explicit
`supplier_ids`, `location_ids`, or `quote_ids`. Disruption intelligence emits typed
restrictions; the supplier decision agent filters quotes; independent validation
rechecks the source restrictions. Narrative text and severity alone do not invent
capacity reductions or probabilities.

## Mathematical semantics (policy version 2)

Inventory and procurement are two result components of one coupled CP-SAT model.
For inventory position i and eligible quote j, integer allocation x[i,j] sums to
order q[i]. Integer shortage s[i] and ending inventory e[i] satisfy:

```
current[i] + q[i] - demand[i] + s[i] = e[i]
safety[i] <= e[i] <= max_stock[i]
0 <= s[i] <= floor(demand[i] * (1 - service_level[i]))
0 <= q[i] <= max(0, max_stock[i] - current[i])
q[i] > 0 iff ordering_binary[i] = 1
sum_i x[i,j] <= quote_capacity[j] - reserved_units[j]
sum_i (current[i] + q[i]) <= supplied warehouse capacity
quoted_purchase_cost + fixed_ordering_cost <= request budget <= policy ceiling
```

Safety stock is a **hard ending-stock constraint**, not an optional objective.
Service level is a deterministic maximum shortage fraction, not a measured
probability. Procurement below full demand plus safety stock is permitted only
within that shortage allowance. Warehouse capacity retains peak receipt semantics.
Only approved, valid, matching SKU/location quotes within risk and lead-time limits
are eligible. Previously committed inventory versions block new executable plans.
Reservations are captured in the immutable snapshot and rechecked atomically at ERP
reservation time, closing races between concurrent plans.

The primary objective minimizes quoted purchasing + fixed ordering + ending-stock
holding + shortage penalties. Monetary coefficients are conservatively rounded up
to cents for CP-SAT. Inventory reference prices are informational, not budget
constraints. Once economic optimality is proven, the existing cost/time/risk weights
and supplier quality/reliability break ties without worsening that economic value.
There is no LLM arithmetic. Holding/shortage costs are economic costs, not PO charges.

On infeasibility, a diagnostic solve removes **only** the request budget and
minimizes cash commitment. An OPTIMAL diagnostic reports a proven minimum feasible
budget and an allocation witness, never selected actions or approval. An infeasible
diagnostic means non-budget constraints also conflict; a timed-out diagnostic must
not claim a proven minimum. Policy version participates in the approval hash.

Worker ownership is persisted and heartbeated. After a 120-second expired lease,
an interrupted pre-approval workflow is marked FAILED with an audit event and fenced
against late writes. Its partial snapshot is not replayed. Unstarted DRAFT runs
remain recoverable through /start. Approved/ERP runs are never replayed by recovery.

## Approval

Human approval is the default. `ALLOW_AUTOMATIC_APPROVAL=false` disables policy
approval. Only replenishment requests with `require_human_approval=false`, valid
plans, and total cost at or below `APPROVAL_THRESHOLD_USD` may receive automatic
approval when that setting is explicitly true. Policy approval records its own
identity, mode, reason, timestamp and expiry. Changing policy before execution
invalidates eligibility. Automatic approval does not submit to ERP automatically.

Operator and approver tokens must differ. All ERP submissions require a valid bound
approval; hash, actions, costs, source evidence and expiry are rechecked.

## ERP protocol

Configure `ERP_MODE=http`, `ERP_URL` as an HTTPS base URL without embedded credentials,
query or fragment, and `ERP_TOKEN`. The default `unavailable` mode performs no external
submission. Simulation requires both request `simulation=true` and
`ERP_MODE=simulation`; simulation never creates production feedback.

`POST {ERP_URL}/purchase-orders` receives a serialized `ExecutionCommand` with bearer
authentication and `Idempotency-Key`. It contains the approved actions, request/run/plan
identities, hash, source versions, purchase amount, fixed ordering amount and total.
Each action includes exact quantity, supplier/SKU/location/quote identities, quote
version, unit and line cost, and planned lead time. This is a command submission
contract; an ERP gateway must process all its lines atomically under the command's
idempotency key and return one durable submission/PO reference. Partial acceptance
must not be reported as `EXECUTED`.

Return the `ExecutionResult` schema. An `EXECUTED` acknowledgement must echo the
idempotency key, plan hash and exact `acknowledged_actions`, provide a nonempty
`external_reference`, and must not be simulated. Optional `acknowledged_cost_usd`
cannot exceed the authorized total. Optional `actual_lead_time_days` must describe
an actual observed value; omit it when unknown. Other terminal/status responses are
`REJECTED`, `FAILED`, `UNKNOWN`, or `IN_PROGRESS`. HTTP 409 is ambiguous and requires
lookup, because the original idempotent request may already exist. Redirects are
rejected; TLS verification remains enabled.

`GET {ERP_URL}/purchase-orders/by-idempotency/{key}` must return the same acknowledgement
contract and never create an order. This endpoint is necessary to recover a timeout
without an ERP PO identifier. A 404, timeout, invalid response or unknown outcome
leaves reconciliation required. No retry of the purchase-order POST is performed.

Commands, reservations, acknowledgements and lifecycle history are durable in the
existing RunStore SQLite database. `SUBMITTED` means dispatch was reserved/attempted,
not that ERP acknowledged receipt. After a crash, an in-progress submission becomes
eligible for lookup after 60 seconds. Reconciliation does not require an unexpired
approval because it only observes the already authorized command; it cannot change it.

## API workflow and dashboard

1. Operator: `POST /api/v1/runs` with a replenishment `request` containing authoritative
   inventory IDs and an explicit budget. Omit `business_inputs` to use the configured source.
2. Reader: `GET /api/v1/runs/{run_id}` until validation and approval policy complete.
   Creation automatically schedules the graph; HTTP 202/DRAFT is an acceptance
   response, not the final result. The worker atomically records RUNNING before
   fetching data. An unstarted draft left by an interrupted API can be recovered
   with operator `POST /api/v1/runs/{run_id}/start`; running/completed runs cannot
   be restarted. `/execute` is exclusively for an approved ERP plan.
3. Approver, when pending: `POST /api/v1/runs/{run_id}/approval` with the returned
   `plan_hash`, `decision: "APPROVED"` and a reason.
4. Operator: `POST /api/v1/runs/{run_id}/execute`. Repeated calls return the durable
   existing result. No second purchase order is sent.
5. Operator: `POST /api/v1/runs/{run_id}/reconcile` to query the ERP by the stored key.
6. Reader: `GET /api/v1/feedback` for typed acknowledged/reconciled outcomes.

The Decisions workspace shows inventory/demand, candidates, selected quantities/costs,
source hash/versions/provenance, validation, approval mode, ERP reference and reconciliation.
Feedback records preserve planned values and optional ERP observations. Order-level
acknowledged cost/lead time are labeled as order-level, not falsely allocated to lines.
Future DataSnapshots include the latest feedback; demand intelligence exposes it as
evidence. It never adds inventory, invents goods receipt or performs synthetic learning.

## Verification and operation

From the repository root:

```powershell
python -m scripts.check_repository
$env:REDIS_INTEGRATION='1'
python -m pytest -q
cd dashboard/frontend
npm run build
cd ../..
./scripts/start_demo.ps1
```

If Windows reserves port 3000, use `./scripts/start_demo.ps1 -FrontendPort 4000`.
The launcher adds that origin for a newly started API. An already running API must
be restarted with `CORS_ORIGINS` including `http://127.0.0.1:4000` (and the localhost
equivalent) before the browser can use the alternate port.

Tests use clearly labeled fixtures and HTTP transport doubles. They do not create
enterprise records or place live orders. The Redis test uses uniquely scoped keys
and removes only those keys. Actual business/ERP connectivity must be verified against
the configured enterprise systems before enabling production execution.
