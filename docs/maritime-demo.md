# ExoChain: the five-minute maritime demo

The main workflow is now **Route Demo**. It reuses the existing LangGraph,
19 specialist tasks, ArcNautical bridge, OR-Tools CP-SAT and MapLibre dashboard.
Inventory replenishment is still available as a separate workflow.

## What was wrong

ArcNautical was installed and could generate Singapore–Rotterdam. Its result was
not consistently being supplied by the main form, which defaulted to data
assessment or enterprise inventory inputs. A route-derived demonstration was
also going through commercial plan/approval requirements. Environmental
providers used the displayed AIS fleet, while the voyage assessor deliberately
refused to treat a test-derived shipment as a verified voyage. Consequently,
real route geometry did not become a useful evidence-enriched comparison.

The repair is to pass the port pair into the actual instrumented data task,
generate geographic candidates there, sample their corridors independently of
AIS availability, preserve the evidence, and compare only usable dimensions.
No optimizer result or agent success is hard-coded.

## Start it

From `G:\Projects\ExoChain-Engine`, keep two PowerShell terminals open.
If an older backend or frontend is running, stop that instance first using its
terminal. Do not delete your data directory or history.

Backend terminal:

```powershell
cd G:\Projects\ExoChain-Engine
python -m uvicorn dashboard.backend.main:app --host 127.0.0.1 --port 8003
```

Frontend terminal:

```powershell
cd G:\Projects\ExoChain-Engine\dashboard\frontend
$env:NEXT_PUBLIC_API_URL = 'http://127.0.0.1:8003'
npm run dev
```

Open `http://127.0.0.1:3000`. These commands deliberately use matching addresses;
a frontend pointing at a stopped API produces “Failed to fetch.” Development
mode is required for this non-commercial classroom workflow.

Dependencies, **only if not already installed**:

```powershell
# Project root
python -m pip install -r requirements.txt
npm ci
# dashboard/frontend
npm ci
```

### Optional live AIS pipeline

Geographic comparison and public weather do not require Kafka/Redis to be
running. Actual ship observations do. The existing infrastructure remains:

```powershell
cd G:\Projects\ExoChain-Engine
docker compose up -d --no-recreate --wait --wait-timeout 60
python -m scripts.init_topics
```

Then keep the following existing modules running in separate terminals:

```powershell
python -m services.ais_ingestion
python -m services.maritime_consumer
python -m services.ais_history_recorder
```

Do not start duplicate ingestion/consumer instances if they are already running.
AISStream must actually observe the selected MMSI; having an API key does not
guarantee satellite coverage or a fresh observation of this particular ship.

## Demonstrate it

1. Open the dashboard. No operator or approver token is required. Authentication
   is removed: keep this demonstration bound to `127.0.0.1`, not a public server.
2. Open **Route Demo → Plan a route**.
3. Search and choose a ship, departure port and destination port from the
   dropdowns. The ship's IMO and source-reported MMSI populate automatically.
   For an example, choose MAERSK ENSHI, Singapore and Rotterdam, with a planning
   speed of `14` knots. Those ports are defaults, not fixed routing inputs.
   “Other supported port…” accepts another five-character UN/LOCODE.
4. Leave maximum duration empty. Maximum risk is a limit on the comparative
   environmental index, not an insured loss probability. Use `1` for an
   unconstrained classroom comparison, or a lower limit to demonstrate rejection.
5. Leave the available metric preferences at `1`. Optional Groq explanation
   needs your configured key; switching it off does not disable the agent tasks.
6. Click **Run agents & optimize route** once.
7. Watch task status change, expand outputs, inspect both route rows and the
   selected solid green path. Dashed routes are alternatives.
8. Explain the metrics actually listed under **Metrics used**, the dimensions
   under **Excluded**, and the source quality/timestamps. There is no booking,
   purchase order, ERP execution or approval step in this main demo.

If your duration or evidence-derived risk limit excludes every candidate, the
result is **NO_FEASIBLE_ROUTE**, not a fake successful recommendation. Change
the actual limit and run again. Each submission creates one recorded run; the
dropdown also contains previous runs, not optimizer training iterations.

## What actually happens

```text
Vessel reference + port pair + speed + optional limits/preferences
    → POST /api/v1/runs → background LangGraph worker
    → data tasks: real ArcNautical routes, AIS lookup, public corridor data
    → intelligence tasks: evidence checks, indices, time/scenario comparisons
    → decision tasks: evidence-enriched candidates and optional AI synthesis
    → OR-Tools CP-SAT: exactly one candidate per shipment, checked limits
    → independent recomputation/validation → ROUTE_READY
    → dashboard: task events, alternative table, selected map and provenance
```

Tasks run when a request is submitted; they are not all permanent background
language-model conversations. Provider requests and independent task groups
run concurrently. The existing AIS ingestion services, when started, are
continuous background processes.

### All 19 existing tasks really execute

| Task group | Actual tasks and work |
|---|---|
| Data (6) | business: port inputs and ArcNautical generation; maritime: exact MMSI/identity/freshness lookup; environment: concurrent wind, wave and current retrieval; ports: requested endpoint PortWatch activity; aviation: actual feed availability check, unused for sea-route scores; market: supplied disruption-record check |
| Intelligence (7) | risk: indices from usable corridor evidence; demand: relevance/history check; forecast: distance/speed transit estimate; disruption: supplied events and geographic chokepoints; anomaly: route input/geometry consistency checks; scenario: compare supported preference scenarios without invented probabilities; port: endpoint activity interpretation |
| Decisions (6) | route: create evidence-enriched candidates and check actual vessel/fuel data; modal: check carrier-allocation scope; inventory: check replenishment scope; supplier: check procurement scope; disruption response: cross-check attributed events against actual alternatives; executive: synthesize peer outputs, optionally call Groq |

The execution wrapper publishes a real start event before calling each function
and a completion event afterward. An executed task may return `UNAVAILABLE`,
`PARTIAL` or `NOT_APPLICABLE`; this is different from never running. Weather,
wave and ocean retrieval are separate bounded provider calls inside the
environment task, not extra fabricated records beyond the existing 19.

These are coordinated specialist/tool agents. They are **not 19 independent
LLMs**. The optional executive Groq call is an actual language-model request;
mathematical scores and final selection never come from its prose.

## Route generation versus optimization

ArcNautical uses its packaged real-world geographic sea-routing network. For
routes crossing the Red Sea/Suez corridor, the adapter also uses the package's
supported `via_waypoints` option at the Cape of Good Hope. Only distinct,
successfully computed geometry is retained. Other port pairs may legitimately
produce one route; the application does not invent an alternative.

The package generates geometry and nautical-mile distance. Estimated hours are
`distance_nm / operator_planning_speed_knots`; that speed is an assumption, not
the vessel's measured AIS speed or a predicted arrival time.

OR-Tools receives those candidates, not an invented path. It minimizes the sum
of weighted normalized available metrics. Normalization divides each metric by
the maximum observed among the candidates; each available metric's weight is
used as entered. A metric is enabled only if it is present for **all** compared
candidates and has positive weight. Missing metrics stay `null`, never zero.
If no available metric has positive weight, choose a non-zero distance/time
preference rather than expecting an arbitrary optimal result.

Risk, weather, current and wave use real captured model values when available.
For an even-handed comparison, at least three valid common fractional sample
positions must exist on every route. Sample validity, units and model-grid
distance are checked. The transparent educational transformations are:

- wind penalty: mean of `min(1, wind_m/s / 25)`;
- wave penalty: mean of `min(1, wave_height_m / 8)`;
- current penalty: mean of `min(1, adverse_along_track_current_m/s / 2)`;
- risk index: mean of the available above indices, with unknown hazards listed.

These divisors define the scoring scale; they are **not vessel-certified
operating thresholds**. Favorable currents are distinguished by projecting
model flow vectors onto route headings. Environmental penalties do not alter
the distance/speed time estimate; no unsupported time-saving claim is made.

Limits are honored when checkable: estimated duration, evidence-derived risk,
and a budget only if actual priced candidates exist. No mandatory budget or
commercial rate is manufactured. Draft/depth clearance, geopolitical closure
and vessel operating restrictions remain unknown without verified evidence.
The result is **best among compared candidates and available scoring
dimensions**, not globally optimal or certified safe for navigation. With one
candidate, the interface explicitly says no alternative was available to compare.

## Searchable catalogs: real identities, not live tracking

`config/maritime_catalog.json` is shared by the form, backend and routing bridge.
It contains 20 major port choices (including Mumbai, Nhava Sheva/JNPA, Chennai,
Mundra, Visakhapatnam and Thoothukudi) and 10 real container-ship identities, with
source URLs and a catalog check date of 2026-10-06. Port codes were checked
against UNECE and the Hamburg Port Authority. Coordinates are approximate
public routing reference points inspected from ArcNautical 1.0.1, with explicit
UNECE coordinates for corrected Shanghai, Ningbo and Shekou port records.
They are **not measured berth locations or depth clearance**.

Important corrections: Shanghai port uses CNSHG, Ningbo port CNNBG, Shenzhen's
specific Shekou port CNSHK, and Hong Kong HKHKG. The package's legacy airport
or incorrect country-prefixed codes are rejected with an explanation. New York
uses the harbour location USNYC, not an invented code for every New Jersey terminal.
Other custom ports are checked against the installed routing package; only the
14 catalog ports receive this source-checked correction layer.

Ship sources are public VesselFinder identity records. IMO is the stable key;
MMSI is a dated source-reported lookup identifier and can change. The catalog
does **not** supply live positions, measured speeds, vessel limits or fuel curves.
For example, EVER GIVEN uses the newer source-reported Liberia MMSI, not its
older Panama MMSI. Matching AIS state is still required to show a ship observation.

Changing ports changes the actual routing request, route geometry, environmental
sample coordinates, endpoint activity query and map bounds. Changing ships
changes the exact MMSI lookup and stored identity; it does not change sea geography
or fabricate ship-specific performance. Changing assumed speed changes calculated
duration. Missing AIS or PortWatch coverage is clearly unavailable.

Current flow velocities in m/s, km/h and knots are converted to m/s before
projection and scoring: km/h divided by 3.6; knots multiplied by 1852/3600.
Original values and units are retained with the conversion. Unknown units remain
unavailable; they are not guessed or assigned zero.

## Sources and credentials

| Source | What is real | Needed |
|---|---|---|
| ArcNautical | Public-network route geometry, distance and named crossed zones; static network, not a live closure feed | Installed root Node package; no key |
| Open-Meteo Weather | Current numerical-model wind/direction/temperature at corridor coordinates | Internet access; no key for this public endpoint |
| Open-Meteo Marine | Current model wave height/direction/period and flow speed/direction, using its Copernicus/MeteoFrance model data | Internet access and endpoint coverage; no key for this public endpoint |
| AISStream → Kafka → Redis | Actual observed vessel state; exact MMSI lookup and freshness/name checks | Existing `AISSTREAM_API_KEY`, running pipeline and actual selected-ship coverage |
| IMF PortWatch | Daily endpoint port activity, observation date and source quality | Internet access; no key; activity is **not** berth waiting time/congestion |
| Groq | Optional real AI explanation of actual peer evidence | `GROQ_API_KEY`, optional `GROQ_MODEL`; default `llama-3.1-8b-instant` |
| Direct Copernicus integration | Existing fleet/tile ocean integration remains intact outside this public-corridor flow | `COPERNICUSMARINE_SERVICE_USERNAME` and `COPERNICUSMARINE_SERVICE_PASSWORD` are currently missing |
| Vessel fuel / depth | Only supplied, sourced vessel-specific curves or verified clearance data | Not present; excluded rather than fabricated |

The AISStream/Groq keys were configured in this checkout during verification
(values were not printed). Tavily is absent and is not used by this demo.
The corridor demo deliberately uses the public Open-Meteo marine model endpoint
without pretending the unconfigured direct Copernicus integration succeeded.

All environmental samples in this implementation are a **current model
snapshot**, not forecasts for the ship's arrival at every position many weeks
later. Receipt time and forecast-valid time are retained separately. The IMO,
MMSI and vessel name entered by the operator are references; only matching AIS
observations can verify the supported identity fields. No position is invented.

Primary provider documentation:
[Open-Meteo Weather](https://open-meteo.com/en/docs),
[Open-Meteo Marine](https://open-meteo.com/en/docs/marine-weather-api),
[Groq models](https://console.groq.com/docs/models).

## Files changed for this route workflow

| File(s), relative to project root | Reason |
|---|---|
| `config/maritime_catalog.json`, `services/maritime_catalog.py` | Shared sourced static catalog, supported-code validation and per-run identity/source snapshot |
| `dashboard/frontend/src/app/maritimeSelection.ts` | Validate selected ports/speed, build the actual matching request scope from endpoints and vessel IMO |
| `services/providers/arcnautical_route.mjs` | Apply source-checked port corrections before routing; report unsupported ports without fake geometry |
| `core/schemas/contracts.py` | Carry route context, metric provenance, wave/port/distance preferences and optional AI flag |
| `dashboard/backend/main.py` | Validate port/speed inputs and pass routing work into the actual queued agent run |
| `services/providers/arcnautical_adapter.py` | Compute legitimate alternatives inside the workflow and preserve planning-speed provenance |
| `agents/data/weather_agent.py` | Reuse weather adapter for bounded public corridor weather/marine queries |
| `services/route_evidence.py` (new) | Capture, validate and compare actual corridor samples, vessel/fuel evidence and explicit indices |
| `orchestrator/data_cluster.py` | Make source collection route-scoped and keep every data task actually instrumented |
| `agents/data/port_infrastructure_agent.py`, `services/providers/registry.py` | Bound calls and retrieve just the requested PortWatch endpoints |
| `agents/intelligence/intelligence_agents.py` | Produce meaningful route-specific checks, evidence indices and comparisons |
| `agents/decision/decision_agents.py` | Attach intelligence to real candidates; scope unrelated domains honestly; synthesize peers |
| `services/route_explanation.py` (new), `config/settings.py`, `.env.example` | Optional actual Groq call and documented configuration; no model-generated numerical objective |
| `optimization/navigation.py`, `optimization/engine.py` | Dynamic available dimensions, entered weights, real limits and explicit exclusion reasons |
| `services/policy.py` | Independently verify enriched metrics against the captured evidence |
| `orchestrator/supervisor.py`, `services/demo.py` | Finish the main geographic run at `ROUTE_READY` without ERP/approval or invented budget; preserve replenishment |
| `dashboard/frontend/src/app/MaritimeDemo.tsx` (new) | Searchable ship/port dropdowns, all 19 task cards, actual comparison/map/recommendation and sources |
| `dashboard/frontend/src/app/page.tsx`, `types.ts`, `Workspaces.tsx`, `globals.css` | Main route-demo view, live agent events, new types/layout and honest route-ready status |
| `dashboard/frontend/src/app/OperationsMap.tsx` | Fix the browser-observed map-refresh/source race; draw candidate geometry even if segments are absent |
| `tests/test_maritime_demo.py` (new) | Actual routing plus isolated provider fixtures to prove evidence reaches CP-SAT, all functions execute, constraints and validation |
| `tests/test_maritime_catalog.py` | Different real port geometries, multiple vessel/endpoint/sample queries, unavailable feeds, unit conversion and invalid inputs |
| `dashboard/frontend/tests/maritime.test.cjs` (new) | Render the real form/task cards/comparison and preserve unavailable evidence |
| `dashboard/frontend/tests/operations-map.test.cjs` (new) | Reproduce retained map-ready state and verify actual supplied geometry is written to the route layer |
| `scripts/verify_maritime_demo.py` (new) | Actual-provider isolated run; prints routes, optimizer, sources, agent statuses and API check without clearing history |
| `docs/maritime-demo.md` (this file) | Startup, demo steps, source limitations, working file map and presentation explanation |

Existing uncommitted replenishment changes were preserved. Tests contain
explicit fixtures; no fixture weather, positions, currents, waves or risk are
loaded by the real application or the live verification script.

## Verification and observed example

```powershell
cd G:\Projects\ExoChain-Engine
python -m pytest -q
python -m scripts.verify_maritime_demo --ai
python -m scripts.verify_maritime_demo --origin HKHKG --destination LKCMB --imo 9893890
cd dashboard\frontend
npm test
npx tsc --noEmit
npm run build
```

The live verification uses a separate database under `data/maritime-check-*`.
It neither clears the application's run history nor sends an ERP command.
It calls actual providers with **no mocks**. Provider failures are part of its
report, not silently substituted successes. API integration is also covered by
the async request/event tests. On a host permitting local socket access, add
`--api-check` to read the computed live run through the local no-login ASGI API.
This optional check cannot initialize Windows' local async socket pair in the
restricted coding environment; it is not presented as a completed live check.
Redis integration is opt-in and must be run with
the isolated Redis test setup when a real Redis instance is accessible.

Observed real ArcNautical output on this checkout at 14 planning knots:

| Candidate | Distance (nm) | Estimated hours |
|---|---:|---:|
| Default geographic route (Suez/Red Sea corridor) | 7,991.1 | 570.79 |
| Via Cape of Good Hope | 10,449.3 | 746.38 |

The actual local pipeline reached `ROUTE_READY`, called all 19 tasks and returned
an `OPTIMAL` CP-SAT route component with the default route selected on distance
and time. External network/DNS and Redis access were unavailable in the coding
environment, so **live AIS/environment/PortWatch/Groq success has not been
verified here**. The comparison used real computed geography plus an explicit
speed assumption—not fabricated environmental values. Automated fixture tests
separately prove actual evidence fields reach the solver and that changing a
weather preference can change the selected route. Those fixtures are tests,
not proof that a live external provider succeeded.

Earlier verification of the fixed-pair workflow recorded 160 backend tests and
14 frontend tests. The dropdown update adds independent catalog, request-handler,
query-plumbing, geometry, map-bounds and current-conversion checks. See the current
delivery report for fresh test/build results; old provider snapshots are not
proof of present external availability.

### Fresh dropdown verification: 2026-10-06

- Backend: **175 passed, 1 opt-in Redis test skipped**. Frontend: **19 passed**.
- TypeScript checking and `npm run build`: **passed**.
- Browser check: ship/port selections populated the corresponding identifiers;
  search filtered the catalog and identical endpoints displayed a warning.
  The run button remains visible while the form body scrolls, including the
  narrow in-app browser. Submission errors are shown inside the form.
- Real installed routing-package tests: Shanghai port → Busan, Hong Kong →
  Colombo, and Los Angeles → New York. Generated endpoints match selected
  catalog coordinates; estimated duration matches the entered speed.
- Isolated no-mock run `fa2b1aa9-ef31-43ce-8343-842666fb90bd`: EVER ACE,
  Hong Kong → Colombo, 14 assumed knots; `ROUTE_READY`, all 19 tasks executed,
  one geographic candidate, 2,881.9 nm and 205.85 estimated hours. With one
  candidate there is no comparison claim. No approval or ERP execution occurred.
- In that run, AIS/Redis and external environmental/PortWatch calls failed
  due to local connection/DNS restrictions. Those fields stayed unavailable;
  the solver used only distance/time. **No fresh environmental, port-activity,
  AIS-position or Groq success is claimed.** The network attempt was real,
  and no test fixtures were supplied to it.
- The isolated run is saved under `data/maritime-check-x6pmpn78/runs.db`;
  existing application history was not changed or cleared.

Files changed specifically for this dropdown update (earlier uncommitted
workflow/replenishment changes were preserved):

- Catalog: `config/maritime_catalog.json`, `services/maritime_catalog.py`.
- Form/request/results: `dashboard/frontend/src/app/MaritimeDemo.tsx`,
  `maritimeSelection.ts`, `page.tsx`, `types.ts`, `globals.css` in that directory.
- Routing/collection: `dashboard/backend/main.py`, `orchestrator/data_cluster.py`,
  `services/providers/arcnautical_route.mjs`, `services/providers/arcnautical_adapter.py`,
  `services/route_evidence.py`.
- Verification: `tests/test_maritime_catalog.py`,
  `dashboard/frontend/tests/maritime.test.cjs`,
  `dashboard/frontend/tests/operations-map.test.cjs`, `scripts/verify_maritime_demo.py`.
- Guide: `docs/maritime-demo.md`.

## Explain it to your professor

“I enter a vessel reference and two ports. The routing tool computes real sea
route alternatives. Specialized tasks try to collect ship observations,
weather, waves, currents and port activity, while other tasks interpret and
cross-check that evidence. Missing inputs are shown honestly and excluded.
Google OR-Tools chooses the route with the lowest weighted objective that
meets the limits we can check. The dashboard shows the tasks, their outputs,
the alternatives, the chosen path and where its numbers came from. It is a
planning demonstration, not a shipping booking or navigation certificate.”
