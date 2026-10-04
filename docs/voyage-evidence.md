# Voyage evidence: what is connected and what is still required

The six checks appear in the Transport decision panel with their own `VALID`, `PARTIAL`, `DATA_REQUIRED`, `STALE`, `UNAVAILABLE`, `PASS` or `FAIL` status. A successful ArcNautical geographic route does not mean these checks passed, or that a vessel may sail. The original OR-Tools optimizer is unchanged.

| Check | Current implementation | What must be supplied for a real answer |
| --- | --- | --- |
| Vessel fuel | Interpolates each route segment from the selected vessel's current speed/fuel-rate curve; estimate is model-derived and excludes weather/current corrections. | Actual vessel ID, valid vessel profile and measured or documented fuel curve covering the route speed. |
| Weather | Hourly Open-Meteo forecast sampled by estimated position/time, with retrieval provenance and horizon checks. | Real departure time and a licensed production forecast feed; values beyond the horizon stay unavailable. |
| Ocean currents | Existing Copernicus observations only if route/time matched; optionally up to three daily route tiles with credentials, always partial due to time resolution. | Copernicus credentials and a suitable forecast product/resolution for operational passage times. |
| Waves | Hourly Open-Meteo Marine route forecast. Vessel-specific suitability only if a current threshold exists. | Forecast coverage and the vessel's documented significant-wave threshold. |
| Draft | Checks supplied draft against documented edge depth only for a navigation-authority network with verified restrictions. | Approved nautical network, depth/bathymetry, tides/UKC policy, vessel draft and restriction evidence. ArcNautical alone cannot pass this check. |
| Port congestion | Shows PortWatch port activity by UN/LOCODE, never labels that activity as a berth queue. | Direct port/terminal queue or waiting-time observations with timestamp and provenance. |

`GET /api/v1/contracts/business` describes the canonical `business_inputs` JSON. `vessel_profiles` and `port_queues` belong there, associated with the selected shipment's `vessel_id` and origin/destination UN/LOCODEs. Each is an evidence record, not a preference slider. Do not invent a fuel curve, depth, queue or forecast to clear a blocker.

## MY INTERVENTION REQUIRED

1. **Authoritative shipment and vessel data.** Obtain permission from the vessel/operator and a source export/API covering the actual shipment ID, vessel ID, departure, draft, speed/fuel curve and wave limit. Configure the enterprise business source and its trust setting. Without this, fuel and vessel-suitability results stay unavailable and a test-only shipment cannot be timed.
2. **Marine forecast access.** Confirm commercial usage rights for Open-Meteo Weather and Marine (or provide a licensed equivalent) and production network access. The current public endpoint is for development/testing; a commercial subscription may require endpoint/API-key configuration before production use. Past the forecast horizon, no precise forecast can be promised.
3. **Copernicus access and product selection.** Register for Copernicus Marine, set `COPERNICUSMARINE_SERVICE_USERNAME` and `COPERNICUSMARINE_SERVICE_PASSWORD` in the backend process environment (never in the request body), and select a route-wide forecast product with adequate space/time resolution. Current bounded daily samples are partial, not a full voyage current forecast.
4. **Navigation authority.** Supply a licensed, navigationally suitable network plus authoritative depth, tides/under-keel-clearance rules and restrictions. ArcNautical geography must remain planning-only until independently validated. No software switch can certify it.
5. **Port queue provider.** Obtain a terminal/port feed with actual queued vessels or waiting hours, timestamps and access permission. PortWatch alone reports activity and cannot establish berth congestion.
6. **Decision metrics.** Supply defensible edge cost, fuel, risk, weather/current penalties, vessel limits and their sources in the canonical route-network contract. Without these, the existing route solver intentionally declines to choose an operational route. Approver and ERP integration remain separate subsequent gates.

No ERP order or booking is sent by this assessment.
