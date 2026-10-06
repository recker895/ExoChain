"""Shared sourced catalog, not an external observation or AIS feed."""
import json
from functools import lru_cache
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


@lru_cache(maxsize=1)
def catalog():
    return json.loads((ROOT / "config/maritime_catalog.json").read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def supported_port_codes():
    completed = subprocess.run(["node", str(ROOT / "services/providers/arcnautical_route.mjs"), "--ports"],
                               cwd=ROOT, capture_output=True, text=True, check=True, timeout=10)
    return frozenset(json.loads(completed.stdout))


def validate_ports(origin, destination):
    codes = [origin.strip().upper(), destination.strip().upper()]
    if codes[0] == codes[1]:
        raise ValueError("Departure and destination ports must differ")
    for code in codes:
        if code in catalog()["blocked_legacy_codes"]:
            raise ValueError(catalog()["blocked_legacy_codes"][code])
        if code not in supported_port_codes():
            raise ValueError(f"Port {code} is not supported. Choose a listed port or a supported custom UN/LOCODE.")


def selection_snapshot(business, request):
    """Persist source/date with the run; do not retroactively relabel old runs."""
    ports = {p["locode"]: p for p in catalog()["ports"]}
    reference = request.vessel_reference
    vessel = next((v for v in catalog()["vessels"] if reference and v["imo"] == reference.imo
                   and v["mmsi"] == reference.mmsi and v["name"] == reference.vessel_name), None)
    shipment = next((s for s in business.shipments if s.id in request.shipment_ids), None)
    return {"classification": catalog()["classification"], "verified_at": catalog()["version"],
            "vessel": vessel, "departure": ports.get(shipment.origin_id) if shipment else None,
            "destination": ports.get(shipment.destination_id) if shipment else None,
            "coordinate_source": catalog()["coordinate_source"], "coordinate_note": catalog()["coordinate_note"],
            "vessel_note": catalog()["vessel_note"]}
