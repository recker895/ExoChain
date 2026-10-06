"""Actual IMF PortWatch daily activity adapter; no waiting-time estimate."""
from typing import Any
from agents.base import BaseAgent, AgentContext

class PortInfrastructureDataAgent(BaseAgent[dict[str, Any]]):
    agent_id = "data.port_infrastructure"
    agent_name = "Port and Infrastructure State Agent"
    agent_type = "DATA"
    version = "2.0.0"
    dependencies = ("IMF PortWatch",)

    BASE_URL = (
        "https://services9.arcgis.com/weJ1QsnbMYJlCHdG/"
        "arcgis/rest/services/Daily_Ports_Data/FeatureServer/0/query"
    )

    PAGE_SIZE = 1000

    def validate_input(self, context: AgentContext) -> bool:
        return context is not None

    def _query(self, params: dict[str, Any]) -> dict[str, Any]:
        import requests

        response = requests.get(
            self.BASE_URL,
            params=params,
            timeout=10,
        )
        response.raise_for_status()

        payload = response.json()

        if "error" in payload:
            raise RuntimeError(str(payload["error"]))

        return payload

    def _latest_date(self) -> str:
        payload = self._query({
            "where": "1=1",
            "outFields": "date",
            "returnGeometry": "false",
            "returnDistinctValues": "true",
            "orderByFields": "date DESC",
            "resultRecordCount": 1,
            "f": "json",
        })

        features = payload.get("features", [])

        if not features:
            raise RuntimeError("PortWatch returned no available dates")

        return features[0]["attributes"]["date"]

    def _fetch_all_ports(self, date_value: str) -> list[dict[str, Any]]:
        where = f"date = DATE '{date_value}'"

        count_payload = self._query({
            "where": where,
            "returnCountOnly": "true",
            "f": "json",
        })

        total = int(count_payload.get("count", 0))

        records: list[dict[str, Any]] = []

        for offset in range(0, total, self.PAGE_SIZE):
            payload = self._query({
                "where": where,
                "outFields": "*",
                "returnGeometry": "false",
                "resultOffset": offset,
                "resultRecordCount": self.PAGE_SIZE,
                "orderByFields": "ObjectId ASC",
                "f": "json",
            })

            for feature in payload.get("features", []):
                attributes = feature.get("attributes", {})
                if attributes:
                    records.append(attributes)

            if len(records) >= total:
                break

        return records

    def run(self, context: AgentContext) -> dict[str, Any]:
        import time

        started = time.perf_counter()

        try:
            latest_date = self._latest_date()
            ports = self._fetch_all_ports(latest_date)

            latency_ms = round(
                (time.perf_counter() - started) * 1000,
                2,
            )

            if not ports:
                return {
                    "status": "PARTIAL",
                    "data_quality": "EMPTY",
                    "source": "IMF_PORTWATCH",
                    "date": latest_date,
                    "port_count": 0,
                    "ports": [],
                    "latency_ms": latency_ms,
                }

            return {
                "status": "SUCCESS",
                "data_quality": "VALID",
                "source": "IMF_PORTWATCH",
                "dataset": "Daily_Ports_Data",
                "date": latest_date,
                "port_count": len(ports),
                "ports": ports,
                "latency_ms": latency_ms,
            }

        except Exception as exc:
            return {
                "status": "FAILED",
                "data_quality": "UNAVAILABLE",
                "source": "IMF_PORTWATCH",
                "port_count": 0,
                "ports": [],
                "error": str(exc),
                "latency_ms": round(
                    (time.perf_counter() - started) * 1000,
                    2,
                ),
            }

