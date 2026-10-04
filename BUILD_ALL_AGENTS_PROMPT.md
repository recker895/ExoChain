You are the principal software architect and implementation engineer for this repository.

PROJECT:
ExoChain Engine — an autonomous, production-grade, multi-agent supply-chain decision intelligence and optimization platform.

OBJECTIVE:
Implement the COMPLETE 18-agent architecture now.

Do NOT build toy agents.
Do NOT create dictionaries pretending to be agents.
Do NOT use hard-coded intelligence outputs.
Do NOT generate fake/random telemetry.
Do NOT replace working infrastructure with mocks.
Do NOT create superficial wrappers around functions and call them "agents".

You must inspect the existing repository first and build on the CURRENT implementation.

============================================================
CURRENT ARCHITECTURE THAT MUST BE PRESERVED
============================================================

Existing infrastructure already implemented and working includes:

1. Real AISStream ingestion
   wss://stream.aisstream.io/v0/stream

2. Kafka telemetry
   telemetry-ais
   telemetry-opensky
   stgnn-graph-events
   agent-decision-logs

3. Redis StateStore
   - vessel current state
   - vessel history
   - intelligence state
   - decision state
   - ocean-current tile cache

4. AIS historical recorder
   SQLite WAL database

5. ST-GNN graph construction
   - vessel graph
   - temporal vessel features
   - kNN/radius edges

6. ST-GNN model
   - temporal processing
   - graph spatial processing
   - future vessel position prediction

7. Copernicus Marine ocean current integration
   Dataset:
   cmems_mod_glo_phy-cur_anfc_0.083deg_P1D-m
   Variables:
   uo
   vo

8. OceanCurrentDataAgent

9. DataCluster

10. OR-Tools optimization

11. Monte Carlo risk / cost analysis

12. Validation and human approval gate

13. ERP execution layer

14. Pydantic schemas under:
   core/schemas/

15. State management under:
   core/state/

16. Existing LangGraph supervisor

The existing working infrastructure is valuable.
Reuse it.
Refactor only where architecturally necessary.

============================================================
TARGET ARCHITECTURE
============================================================

Implement:

                         EXOCHAIN OS
                              |
                    ┌─────────┴─────────┐
                    │                   │
               DATA CLUSTER      INTELLIGENCE CLUSTER
                    │                   │
                 6 AGENTS             6 AGENTS
                    │                   │
                    └─────────┬─────────┘
                              |
                       DECISION CLUSTER
                              |
                           6 AGENTS
                              |
                       OPTIMIZATION
                              |
                         VALIDATION
                              |
                       HUMAN APPROVAL
                              |
                           EXECUTION
                              |
                             ERP
                              |
                       FEEDBACK LOOP

Total:

18 SPECIALIZED AGENTS

============================================================
DATA CLUSTER — 6 AGENTS
============================================================

Create actual production-grade agents:

1. AIS / Maritime Telemetry Agent

Responsibilities:
- consume normalized AIS telemetry
- validate vessel positions
- detect malformed telemetry
- enrich vessel state
- maintain vessel trajectory state
- calculate basic kinematic features
- publish structured telemetry
- use Redis/Kafka
- never fabricate missing telemetry

2. Aviation Telemetry Agent

Responsibilities:
- integrate OpenSky or configured aviation provider
- retrieve relevant aircraft
- normalize aircraft telemetry
- validate position/speed/heading
- publish aviation telemetry
- cache provider responses
- handle API failure/rate limits gracefully

3. Ocean Current Agent

Use the existing OceanCurrentDataAgent.

Responsibilities:
- Copernicus Marine
- uo / vo
- spatial lookup
- cache
- land masking
- current speed/direction
- vessel-specific ocean-current enrichment

Do NOT duplicate this implementation unnecessarily.

4. Weather & Wave Data Agent

Build a real provider abstraction.

Primary:
- Open-Meteo where applicable

Architecture must support future:
- marine weather provider
- wave height
- wave direction
- wind
- pressure
- precipitation
- visibility
- storm indicators

Do not invent wave values when unavailable.

5. Port & Infrastructure State Agent

Create provider abstraction for:
- port congestion
- berth availability
- port closures
- waiting time
- infrastructure disruptions
- canal restrictions
- chokepoint status

The system must distinguish:

AVAILABLE
UNAVAILABLE
STALE
UNKNOWN

Never convert UNKNOWN into safe/normal.

6. External Market / Supply Chain Event Agent

Create extensible ingestion for:
- commodity prices
- fuel prices
- freight rates
- geopolitical events
- strikes
- sanctions
- trade restrictions
- supplier disruptions
- logistics news/events

Use adapters and structured events.

Do not scrape random websites directly inside agents.
Use provider interfaces.

============================================================
INTELLIGENCE CLUSTER — 6 AGENTS
============================================================

These agents must transform DATA into actual intelligence.

1. Risk Intelligence Agent

Inputs:
- AIS
- vessel state/history
- ST-GNN prediction
- ocean currents
- weather
- port state
- disruption events

Produce:

RiskAssessment

Risk dimensions:

- collision risk
- route deviation risk
- weather risk
- congestion risk
- operational risk
- geopolitical risk
- supply disruption risk
- ETA uncertainty

Use explainable feature contributions.

No hard-coded risk values.

Integrate ST-GNN predictions where available.

2. Demand Intelligence Agent

Responsibilities:
- demand forecasting
- demand anomalies
- seasonality
- trend
- volatility
- confidence intervals

Architecture should support:
- statistical forecasting
- ML forecasting
- future deep-learning forecasting

Do not invent demand datasets.
If no demand source exists, return an explicit DATA_UNAVAILABLE state.

3. Forecast Intelligence Agent

Forecast:

- ETA
- transit time
- fuel consumption
- congestion
- delivery delay
- route duration

Use actual telemetry/environmental features.

Architecture:

observations
→ feature engineering
→ model
→ forecast
→ uncertainty
→ ForecastPoint

4. Disruption Intelligence Agent

Detect:

- port closure
- storm
- strike
- vessel failure
- route obstruction
- geopolitical disruption
- supplier disruption
- aviation disruption
- infrastructure disruption

Use event correlation.

Every disruption must include:

event_id
source
timestamp
severity
confidence
affected_entities
affected_routes
evidence
expiry/validity

5. Anomaly Intelligence Agent

Detect abnormal behavior:

- vessel speed anomaly
- route deviation
- AIS silence
- abnormal dwell time
- port dwell anomaly
- unexpected congestion
- unusual weather exposure
- supplier performance anomaly

Use statistical / ML methods where appropriate.

Do not merely use arbitrary thresholds everywhere.

6. Scenario Intelligence Agent

Generate structured possible future states.

Example:

BASELINE
ADVERSE_WEATHER
PORT_CONGESTION
VESSEL_DELAY
SUPPLIER_DISRUPTION
MULTI_EVENT

Each scenario must contain:

scenario_id
assumptions
affected_entities
probability/confidence where statistically defensible
expected impacts
uncertainties
required decision inputs

Do not pretend scenario probabilities are real probabilities unless supported by an actual model.

============================================================
DECISION CLUSTER — 6 AGENTS
============================================================

These agents convert intelligence into actionable decision proposals.

1. Route Decision Agent

Determine candidate maritime routes.

Consider:

- vessel constraints
- current
- weather
- waves
- congestion
- ETA
- fuel
- risk
- chokepoints

Generate CANDIDATE routes.

Do not directly execute.

2. Modal Decision Agent

Evaluate:

SEA
AIR
ROAD
RAIL
MULTIMODAL

Generate candidate modal strategies.

Consider:

cost
time
capacity
risk
availability
carbon where available

The final selection must be performed by optimization, not arbitrary agent preference.

3. Inventory Decision Agent

Determine:

- reorder actions
- safety stock changes
- allocation
- emergency inventory
- shortage mitigation

Must support constraints and uncertainty.

4. Supplier Decision Agent

Evaluate supplier actions:

- continue
- increase allocation
- decrease allocation
- diversify
- emergency sourcing
- procurement

Use supplier risk/performance intelligence.

Do not automatically execute procurement.

5. Disruption Response Decision Agent

Generate mitigation strategies:

- reroute
- delay
- expedite
- switch mode
- split shipment
- alternate supplier
- alternate port
- hold inventory

Every proposal must contain:

reason
evidence
expected effect
cost
risk
constraints
confidence

6. Executive / Coordination Decision Agent

Aggregate outputs from all decision agents.

Responsibilities:

- identify conflicting decisions
- consolidate candidate strategies
- produce final DecisionProposal set
- identify unresolved uncertainty
- determine whether optimization is required
- prepare optimization request

This agent must NOT bypass optimization.

============================================================
AGENT CONTRACT
============================================================

Every agent must implement a consistent contract.

Create a robust base abstraction such as:

BaseAgent

with:

agent_id
agent_name
agent_type
version
dependencies
execute(context)
validate_input(...)
validate_output(...)
health_check()
metrics()

Agents must return structured objects.

Do NOT return arbitrary dictionaries as the primary interface.

Use Pydantic models.

Each execution should contain:

execution_id
agent_id
timestamp
status
latency_ms
input references
output
warnings
errors
data_quality
confidence

============================================================
DATA QUALITY
============================================================

Implement explicit data quality states:

VALID
PARTIAL
STALE
UNKNOWN
INVALID
UNAVAILABLE

Every intelligence result must carry data-quality metadata.

Never silently substitute missing data with fabricated values.

============================================================
AGENT OBSERVABILITY
============================================================

Every agent must emit:

- execution start
- execution completion
- latency
- success/failure
- input counts
- output counts
- confidence
- warnings
- errors

Publish important events to:

agent-decision-logs

Use structured JSON.

============================================================
CONCURRENCY
============================================================

Agents inside each cluster must execute concurrently when dependencies allow it.

Use:

ThreadPoolExecutor

or

asyncio

depending on the existing implementation.

Do NOT create a sequential chain of 18 agents.

Architecture should be:

Data Cluster
   ├── Agent 1 ─┐
   ├── Agent 2 ─┤
   ├── Agent 3 ─┤
   ├── Agent 4 ─┤
   ├── Agent 5 ─┤
   └── Agent 6 ─┘
                ↓
         Intelligence Cluster
                ↓
   ├── Agent 1 ─┐
   ├── Agent 2 ─┤
   ├── Agent 3 ─┤
   ├── Agent 4 ─┤
   ├── Agent 5 ─┤
   └── Agent 6 ─┘
                ↓
            Decision
                ↓
   ├── Agent 1 ─┐
   ├── Agent 2 ─┤
   ├── Agent 3 ─┤
   ├── Agent 4 ─┤
   ├── Agent 5 ─┤
   └── Agent 6 ─┘

Use dependency-aware execution.

============================================================
LLM USAGE
============================================================

DO NOT put an LLM inside every agent.

Use deterministic code / ML / optimization wherever possible.

LLMs may be used for:

- qualitative event interpretation
- unstructured text extraction
- scenario explanation
- decision rationale
- conflicting evidence synthesis

LLMs must NOT replace:

- numerical optimization
- constraint solving
- risk mathematics
- telemetry validation
- routing calculations
- deterministic business rules
- database operations

Keep LLM calls isolated behind interfaces.

============================================================
OPTIMIZATION LAYER
============================================================

Keep optimization OUTSIDE the agents.

Create a production optimization layer supporting:

1. Route optimization
2. Modal split optimization
3. Procurement optimization
4. Inventory optimization
5. Cost optimization
6. Time optimization
7. Risk constraints
8. Capacity constraints
9. Budget constraints
10. Service-level constraints

Use OR-Tools.

Optimization input:

OptimizationRequest

Optimization output:

OptimizationSolution

Include:

objective
constraints
candidate decisions
selected decisions
total cost
total time
risk metrics
solver status
constraint violations
explanation

Do not allow an agent to directly select a final solution while bypassing the optimizer.

============================================================
MONTE CARLO
============================================================

Build a proper simulation service.

Support uncertainty in:

- transit time
- fuel cost
- demand
- delays
- congestion
- disruption duration
- supplier lead time

Return:

expected cost
P50
P90
P95
variance
CVaR where applicable
probability of SLA violation

Make distributions configurable.

Never use arbitrary random values merely to make the demo look realistic.

============================================================
HUMAN APPROVAL
============================================================

Preserve the existing human approval safety gate.

Rules:

If decision requires approval:

PENDING_HUMAN_APPROVAL

MUST NOT execute ERP action.

Approval must be explicit.

Execution must have a hard guard.

Rejected:

REJECTED

Approved:

APPROVED

Only then:

EXECUTION

Build the architecture so LangGraph checkpointing can later resume the workflow after approval.

Do not bypass this.

============================================================
EXECUTION / ERP
============================================================

Execution layer must remain isolated from decision generation.

ExecutionCommand

must contain:

command_id
decision_id
action
target
parameters
approval_status
requested_by
timestamp
idempotency_key

ExecutionResult:

status
external_reference
timestamp
error
rollback_available

All execution must be idempotent.

============================================================
FEEDBACK LOOP
============================================================

Create architecture for:

decision
→ execution
→ actual outcome
→ telemetry
→ performance measurement
→ model feedback

Track:

predicted ETA vs actual ETA
predicted cost vs actual cost
predicted risk vs observed events
planned route vs actual route
supplier prediction vs actual supplier performance

This should eventually support model retraining.

============================================================
PROJECT STRUCTURE
============================================================

Create a clean architecture approximately like:

agents/
    base.py

    data/
        __init__.py
        ais_agent.py
        aviation_agent.py
        ocean_current_agent.py
        weather_wave_agent.py
        port_infrastructure_agent.py
        market_event_agent.py

    intelligence/
        __init__.py
        risk_agent.py
        demand_agent.py
        forecast_agent.py
        disruption_agent.py
        anomaly_agent.py
        scenario_agent.py

    decision/
        __init__.py
        route_agent.py
        modal_agent.py
        inventory_agent.py
        supplier_agent.py
        disruption_response_agent.py
        executive_agent.py

orchestrator/
    data_cluster.py
    intelligence_cluster.py
    decision_cluster.py
    optimization_engine.py
    supervisor.py

optimization/
    route_optimizer.py
    modal_optimizer.py
    inventory_optimizer.py
    procurement_optimizer.py
    monte_carlo.py

services/
    providers/
        ais/
        aviation/
        ocean/
        weather/
        ports/
        market/

    feature_engineering/
    forecasting/
    risk/
    observability/
    feedback/

core/
    schemas/
    state/
    events/

tests/
    agents/
    intelligence/
    decision/
    optimization/
    integration/

============================================================
IMPORTANT IMPLEMENTATION RULES
============================================================

1. FIRST inspect the entire repository.

2. Identify existing implementations before creating files.

3. Reuse existing:
   - StateStore
   - AIS ingestion
   - Kafka
   - Copernicus
   - ST-GNN
   - schemas
   - OR-Tools
   - supervisor
   - validation
   - approval

4. Do not duplicate functionality.

5. Do not silently break existing APIs.

6. Add compatibility layers when necessary.

7. Update imports.

8. Update __init__.py files.

9. Update Pydantic schemas where required.

10. Add type hints.

11. Add structured logging.

12. Add exception handling.

13. Add retries only where appropriate.

14. Add timeouts to external providers.

15. Add provider failure isolation.

16. Do not hide errors.

17. Do not catch Exception and silently continue.

18. Do not use global mutable state unnecessarily.

19. Do not create circular imports.

20. Use dependency injection where useful.

21. Use configuration through environment variables.

22. Never hard-code API keys.

23. Never print secrets.

24. Never commit .env.

25. Existing secrets must not appear in generated code.

26. Preserve the human approval safety boundary.

27. Never fabricate live external data.

28. Never label a placeholder implementation as production-ready.

29. If a provider is unavailable, expose:
   UNAVAILABLE
   rather than fake values.

30. Make every agent independently testable.

============================================================
TESTING REQUIREMENTS
============================================================

Create unit tests for every agent.

At minimum test:

- valid input
- missing input
- stale data
- invalid data
- provider failure
- timeout
- malformed provider response
- successful execution
- structured output
- observability
- idempotency where applicable

Integration tests:

AIS
→ Kafka
→ Redis
→ DataCluster
→ IntelligenceCluster
→ DecisionCluster
→ Optimization
→ Validation
→ Approval
→ Execution

Test that:

HUMAN_APPROVAL_REQUIRED = TRUE

actually prevents execution.

Test that:

REJECTED

prevents execution.

Test that:

APPROVED

allows execution.

============================================================
QUALITY GATES
============================================================

After implementation run:

python -m compileall .

pytest -q

and any project-specific tests.

Also run import checks.

Check for:

- circular imports
- missing dependencies
- invalid Pydantic schemas
- broken LangGraph state
- broken Redis access
- broken Kafka access
- broken Copernicus access

Do NOT stop after creating files.

Fix actual failures.

============================================================
FINAL SUPERVISOR
============================================================

The final LangGraph flow must conceptually be:

START
 ↓
DATA_CLUSTER
 ↓
INTELLIGENCE_CLUSTER
 ↓
DECISION_CLUSTER
 ↓
OPTIMIZATION
 ↓
VALIDATION
 ↓
 ┌─────────────────────────────┐
 │                             │
AUTO APPROVED          HUMAN APPROVAL
 │                             │
 ↓                         WAIT/END
EXECUTION                       │
 │                              │
 ↓                         APPROVED
FEEDBACK                         │
                                 ↓
                              EXECUTION
                                 ↓
                              FEEDBACK

Rejected decisions must terminate safely.

============================================================
NO DEMO ARCHITECTURE
============================================================

The following are explicitly forbidden:

❌ hard-coded agent outputs
❌ random telemetry
❌ fake vessel positions
❌ fake weather
❌ fake risk scores
❌ dictionaries pretending to be agents
❌ sequential 18-node spaghetti
❌ LLM everywhere
❌ optimizer bypass
❌ approval bypass
❌ silently assuming missing data is safe
❌ fake probabilities
❌ fake production claims
❌ TODO-only implementations
❌ placeholder methods returning None
❌ duplicate infrastructure

============================================================
DELIVERABLE
============================================================

Implement the architecture completely in the repository.

At the end provide:

1. Files created
2. Files modified
3. 18-agent inventory
4. Data flow
5. Agent dependencies
6. External providers used
7. Tests created
8. Tests executed
9. Any remaining genuine limitations
10. Exact commands required to run the system

Do not merely explain what should be built.

BUILD IT.

Start by inspecting the repository and existing code.
Then implement.
Then run tests.
Then fix failures.
Then report the final architecture.
