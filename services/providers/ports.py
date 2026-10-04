"""IMF PortWatch location registry, joined by identity, never geocoded guesses.

Metadata: https://www.arcgis.com/sharing/rest/content/items/acc668d199d1472abaaf2467133d4ca4/info/metadata/metadata.xml
Attribution: UN Global Platform; IMF PortWatch (portwatch.imf.org).
"""

import json
import requests
from config.settings import settings
from core.schemas.contracts import Coordinate, utcnow

URL = "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/arcgis/rest/services/PortWatch_ports_database/FeatureServer/0/query"
CACHE_KEY = "exochain:portwatch:locations:v1"


def port_locations(store):
    try:
        cached = store.redis.get(CACHE_KEY)
        if cached:
            return json.loads(cached)
    except Exception:
        pass
    records = {}
    for offset in range(0, 20000, 1000):
        response = requests.get(
            URL,
            params={
                "where": "1=1",
                "outFields": "portid,lat,lon,LOCODE",
                "returnGeometry": "false",
                "resultOffset": offset,
                "resultRecordCount": 1000,
                "orderByFields": "ObjectId ASC",
                "f": "json",
            },
            timeout=settings.PROVIDER_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if "error" in payload:
            raise ValueError("Port registry unavailable")
        features = payload.get("features", [])
        for feature in features:
            fields = feature.get("attributes", {})
            if (
                not fields.get("portid")
                or fields.get("lat") is None
                or fields.get("lon") is None
            ):
                continue
            position = Coordinate(latitude=fields["lat"], longitude=fields["lon"])
            records[fields["portid"]] = {
                **position.model_dump(),
                "locode": fields.get("LOCODE"),
            }
        if not payload.get("exceededTransferLimit") and len(features) < 1000:
            break
    else:
        raise ValueError("Port registry pagination exceeded configured bound")
    if not records:
        raise ValueError("Port locations unavailable")
    result = {
        "source": URL,
        "received_at": utcnow().isoformat(),
        "observed_at": None,
        "ports": records,
    }
    try:
        store.redis.set(CACHE_KEY, json.dumps(result), ex=86400)
    except Exception:
        pass
    return result


def join_port_locations(activity, locations):
    result = []
    for port in activity:
        position = locations.get("ports", {}).get(port.get("portid"))
        result.append(
            {
                **port,
                **(position or {}),
                "location_source": locations.get("source") if position else None,
                "location_received_at": locations.get("received_at")
                if position
                else None,
                "location_observed_at": locations.get("observed_at"),
                "location_status": "AVAILABLE"
                if position
                else "PORT_LOCATION_UNAVAILABLE",
            }
        )
    return result
