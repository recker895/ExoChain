# ArcNautical geographic route planning and voyage evidence

`POST /api/v1/runs` accepts `arcnautical_route`. The adapter converts the locally installed `@arcnautical/maritime-routing` output into the existing `BusinessInputs.route_network`; the normal transport decision agent, route candidate generator, OR-Tools optimizer and validator remain in place. It preserves ArcNautical geometry, distance, duration, crossed hazard zones, version and output fingerprint.

The network is always `GEOGRAPHIC_PLANNING_ONLY`: it is **not** a navigationally approved route, booking or vessel trace. In ordinary transport mode, validation still blocks operational approval. In explicit transport `demo_mode`, ArcNautical edges become backend route candidates and OR-Tools compares the available distance and duration. Missing cost, fuel, risk, weather, current and draft data stay null and are excluded, not invented. A valid demo result may reach `PENDING_APPROVAL`, but demo execution is disabled even after human approval.

## Local smoke test

From the repository root in PowerShell (with the backend already running and an operator token configured):

```powershell
$body = @{ request = @{ operation = 'TRANSPORT'; demo_mode = $true; shipment_ids = @('ARC-SGSIN-NLRTM-DEMO'); required_components = @('route'); budget_usd = 2000; max_risk = 0.5; require_human_approval = $true; vessel_reference = @{ shipment_id = 'ARC-SGSIN-NLRTM-DEMO'; mmsi = '563275100'; imo = '9502946'; vessel_name = 'MAERSK ENSHI' } }; arcnautical_route = @{ origin_locode = 'SGSIN'; destination_locode = 'NLRTM'; shipment_id = 'ARC-SGSIN-NLRTM-DEMO' } } | ConvertTo-Json -Depth 8
Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:8000/api/v1/runs' -Headers @{ Authorization = 'Bearer <OPERATOR_TOKEN>' } -ContentType 'application/json' -Body $body
```

Replace the token placeholder locally; never put a credential in a shared file. The adapter creates a clearly marked **test-only shipment stub** when no `business_inputs` are supplied. Its departure time and quantity are not observations. Expect an optimized geographic demo with unavailable fuel and cost, not an approved sailing or ERP action.

The demo request includes this vessel reference (it does **not** make the synthetic shipment a real booking):

```json
"vessel_reference": {
  "shipment_id": "ARC-SGSIN-NLRTM-DEMO",
  "mmsi": "563275100",
  "imo": "9502946",
  "vessel_name": "MAERSK ENSHI"
}
```

ExoChain reads that exact MMSI from the existing Redis AIS state, independently of the 200-vessel displayed sample, and captures it in the immutable run. A missing, stale or name-mismatched observation is labeled accordingly but does not block demo optimization. AIS does not independently verify the supplied IMO number, actual voyage, fuel curve or safe draft. The selected vessel never makes ArcNautical geography navigationally approved.

To assess an actual shipment, provide `business_inputs` in the same request with exactly one matching shipment, and no `route_network` (the adapter supplies that). Use `GET /api/v1/contracts/business` for the current canonical schema. A current `vessel_profiles` record can supply a vessel-specific fuel curve, draft and wave threshold. Optional `port_queues` records can supply observed berth queues and waiting times. All require original source, observation and validity times, and provenance. In production the business source must be configured and trusted; pasted JSON alone does not establish enterprise authority.

The route assessment samples the geographic geometry at nine points and estimates passage times from the shipment's departure and ArcNautical duration. It requests hourly Open-Meteo weather and Marine wave forecasts at those points. Forecasts are model-derived and omitted outside their horizon. Existing Copernicus current evidence is used only when space and time match; configured Copernicus credentials also permit up to three direct daily-grid samples, labeled partial. Fuel is interpolated only from a current supplied vessel speed/fuel curve, without invented weather/current savings. Draft clearance remains `DATA_REQUIRED` without authoritative depths, restrictions and an approved navigation network. PortWatch is shown as activity, not physical berth congestion; exact queue/wait data needs a separate source.

## Restart and checks

From the repository root:

```powershell
npm install
python -m pytest -q -p no:cacheprovider tests/test_arcnautical_adapter.py tests/test_voyage_assessment.py
cd dashboard/frontend
npm run lint
npm run build
```

Start/restart the API and dashboard in separate terminals from the repository root:

```powershell
python -m uvicorn dashboard.backend.main:app --host 127.0.0.1 --port 8000
```

```powershell
cd dashboard/frontend
npm run dev
```

If your local dashboard uses port 8001 instead, keep its `NEXT_PUBLIC_API_URL` and the test URL aligned. Do not submit an authorized ERP execution as part of this test.

See [voyage-evidence.md](voyage-evidence.md) for the missing production inputs and provider setup.
