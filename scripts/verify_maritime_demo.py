"""Live, isolated maritime demonstration: actual providers and routing, no mocks.

Run python -m scripts.verify_maritime_demo [--ai]. The temporary run database
does not replace or clear the operator's dashboard history. Provider failures
remain unavailable and are printed, rather than replaced with test values.
"""
import argparse
import asyncio
import json
import tempfile
from pathlib import Path

from core.schemas.contracts import RunRequest
from core.state.run_store import RunStore
from orchestrator.data_cluster import DataCluster
from orchestrator.supervisor import build_exochain_graph, initial_state
from services.maritime_catalog import catalog, validate_ports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ai", action="store_true", help="Call the configured Groq model for synthesis")
    parser.add_argument("--api-check", action="store_true", help="Also check the ASGI API (requires local async socket access)")
    parser.add_argument("--origin", default="SGSIN")
    parser.add_argument("--destination", default="NLRTM")
    parser.add_argument("--speed", type=float, default=14)
    parser.add_argument("--imo", default=catalog()["vessels"][0]["imo"], choices=[v["imo"] for v in catalog()["vessels"]],
                        help="Stable IMO identity from the sourced vessel catalog")
    args = parser.parse_args()
    args.origin, args.destination = args.origin.strip().upper(), args.destination.strip().upper()
    validate_ports(args.origin, args.destination)
    vessel = next(v for v in catalog()["vessels"] if v["imo"] == args.imo)
    shipment = f"MARITIME-{args.origin}-{args.destination}-{vessel['imo']}"
    request = RunRequest(
        operation="TRANSPORT", demo_mode=True, shipment_ids=[shipment],
        required_components=["route"], require_human_approval=False,
        max_risk=1, use_ai_explanation=args.ai,
        weights={"distance": 1, "time": 1, "risk": 1, "weather": 1,
                 "current": 1, "wave": 1, "port": 1, "fuel": 1},
        vessel_reference={"shipment_id": shipment, "mmsi": vessel["mmsi"],
                          "imo": vessel["imo"], "vessel_name": vessel["name"]},
    )
    verification_root = Path(__file__).resolve().parents[1] / "data"
    verification_root.mkdir(exist_ok=True)
    database = Path(tempfile.mkdtemp(prefix="maritime-check-", dir=verification_root)) / "runs.db"
    store = RunStore(database)
    cluster = DataCluster()
    state = initial_state(request, route_input={
        "origin_locode": args.origin, "destination_locode": args.destination,
        "shipment_id": shipment, "planning_speed_knots": args.speed,
    })
    print("Running actual routing, data providers and all existing agent tasks...", flush=True)
    state = build_exochain_graph(store=store, data_cluster=cluster).invoke(state)
    print(f"Actual graph completed: {state['status']}.", flush=True)
    records = [a for stage in ("data", "intelligence", "decisions") for a in state[stage]["executions"]]
    component = state["optimization"]["components"].get("route", {})
    sources = {key: {"source": item["source"], "quality": item["quality"],
                     "observed_at": item.get("observed_at"), "errors": item.get("errors", []),
                     "usable_points": (item.get("payload") or {}).get("usable_points")}
               for key, item in state["data"]["sources"].items()
               if key in {"selected_vessel", "business", "weather", "ocean", "waves", "ports"}}
    api_status = "NOT_REQUESTED: API integration is tested by pytest; use --api-check for a live-state read"
    if args.api_check:
        import httpx
        from dashboard.backend.main import create_app
        async def read_actual_run():
            transport = httpx.ASGITransport(app=create_app(store, cluster.registry, cluster))
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await asyncio.wait_for(client.get(f"/api/v1/runs/{state['run_id']}"), timeout=15)
        response = asyncio.run(read_actual_run())
        assert response.status_code == 200, response.status_code
        assert response.json()["optimization"] == state["optimization"]
        api_status = "PASS: local no-login API returns the same actual computed solution"
    summary = {
        "verification_mode": "LIVE_PROVIDERS_NO_MOCKS", "run_id": state["run_id"],
        "status": state["status"], "agents_executed": len(records),
        "selection": state["data"]["route_context"].get("selection"),
        "agents": {a["agent_id"]: a["status"] for a in records},
        "candidates": [{"label": r["planning_label"], "distance_nm": round(r["distance_km"] / 1.852, 2),
                        "estimated_hours": round(r["duration_hours"], 2),
                        "geometry_points": len(r["geometry"]), "risk_index": r.get("risk_score")}
                       for r in state["decisions"]["routes"]],
        "solver": component.get("solver"), "solver_status": component.get("status"),
        "selected": [{"label": r["planning_label"], "objective": r["objective_value"]}
                     for r in component.get("selected", [])],
        "metrics": component.get("explanation", {}), "sources": sources,
        "ai_synthesis": state["decisions"].get("explanation", {}).get("ai_synthesis"),
        "api_check": api_status, "isolated_run_database": str(database),
        "no_booking_or_erp": state["execution"] is None,
    }
    print(json.dumps(summary, indent=2), flush=True)
    assert len(records) == 19 and len({a["agent_id"] for a in records}) == 19
    assert all(a["started_at"] and a["completed_at"] for a in records)
    assert state["status"] == "ROUTE_READY", state["validation"]
    assert component.get("selected") and component["solver"] == "OR-Tools CP-SAT"
    assert state["execution"] is None and state["approval"] is None


if __name__ == "__main__":
    main()
