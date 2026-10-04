"""ERP boundary: unavailable by default, explicit simulation, strict HTTP contract."""

import hashlib
from typing import Protocol
import requests
from core.schemas.contracts import ExecutionCommand, ExecutionResult


def configured_erp(simulation=False):
    from config.settings import settings

    if simulation:
        return (
            SimulationERP() if settings.ERP_MODE == "simulation" else UnavailableERP()
        )
    if (
        settings.ERP_MODE == "http"
        and settings.ERP_URL
        and settings.ERP_TOKEN.get_secret_value()
    ):
        return HTTPERP(settings.ERP_URL, settings.ERP_TOKEN.get_secret_value())
    return UnavailableERP()


class ERPClient(Protocol):
    def submit_purchase_order(self, command: ExecutionCommand) -> ExecutionResult: ...
    def get_purchase_order(self, reference: str) -> dict: ...
    def cancel_purchase_order(self, reference: str, idempotency_key: str) -> dict: ...
    def reconcile_purchase_order(
        self, command: ExecutionCommand
    ) -> ExecutionResult: ...


class UnavailableERP:
    def reconcile_purchase_order(self, command):
        return self.submit_purchase_order(command)

    def submit_purchase_order(self, command):
        return ExecutionResult(
            status="ERP_UNAVAILABLE",
            idempotency_key=command.idempotency_key,
            message="No authoritative ERP connector configured",
        )

    def get_purchase_order(self, reference):
        return {"status": "ERP_UNAVAILABLE"}

    def cancel_purchase_order(self, reference, idempotency_key):
        return {"status": "ERP_UNAVAILABLE"}


class SimulationERP(UnavailableERP):
    def reconcile_purchase_order(self, command):
        return self.submit_purchase_order(command)

    def submit_purchase_order(self, command):
        return ExecutionResult(
            status="EXECUTION_SIMULATED",
            simulation=True,
            idempotency_key=command.idempotency_key,
            external_reference="SIM-"
            + hashlib.sha256(command.idempotency_key.encode()).hexdigest()[:20],
            message="Simulation only; no purchase order submitted to an ERP",
        )


class HTTPERP:
    def __init__(self, url, token):
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not token
        ):
            raise ValueError("ERP requires HTTPS base URL and credentials")
        self.url = url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"}

    def submit_purchase_order(self, command):
        try:
            response = requests.post(
                self.url + "/purchase-orders",
                json=command.model_dump(mode="json"),
                headers={**self.headers, "Idempotency-Key": command.idempotency_key},
                timeout=(5, 25),
                allow_redirects=False,
            )
            if response.status_code in (400, 403, 422):
                return ExecutionResult(
                    status="REJECTED",
                    idempotency_key=command.idempotency_key,
                    message="ERP rejected the purchase order",
                )
            response.raise_for_status()
            if not 200 <= response.status_code < 300:
                raise ValueError("ERP did not acknowledge submission")
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("invalid ERP response")
            result = self.validate_acknowledgement(command, payload)
            result.response_metadata = {"http_status": response.status_code}
            if (
                result.idempotency_key != command.idempotency_key
                or result.simulation
                or result.status == "EXECUTION_SIMULATED"
            ):
                raise ValueError("ERP response identity mismatch")
            if result.status == "EXECUTED" and not result.external_reference:
                raise ValueError("ERP acknowledgement missing")
            return result
        except requests.RequestException:
            return ExecutionResult(
                status="UNKNOWN",
                idempotency_key=command.idempotency_key,
                message="Submission outcome unknown; reconcile with ERP before retrying",
            )
        except (ValueError, TypeError):
            return ExecutionResult(
                status="FAILED",
                idempotency_key=command.idempotency_key,
                message="Malformed ERP acknowledgement; reconcile before retrying",
            )

    @staticmethod
    def validate_acknowledgement(command, payload):
        result = ExecutionResult.model_validate(payload)
        if (
            result.idempotency_key != command.idempotency_key
            or result.simulation
            or result.status == "EXECUTION_SIMULATED"
        ):
            raise ValueError("ERP response identity or execution mode mismatch")
        if result.status == "EXECUTED" and (
            not result.external_reference
            or result.plan_hash != command.plan_hash
            or result.acknowledged_actions != command.actions
            or (
                result.acknowledged_cost_usd is not None
                and result.acknowledged_cost_usd > command.total_cost_usd
            )
        ):
            raise ValueError(
                "ERP acknowledgement does not match approved purchase order"
            )
        # Provider messages and metadata are not trusted log/display content.
        result.message = "ERP purchase-order acknowledgement received"
        result.response_metadata = {
            key: value
            for key, value in result.response_metadata.items()
            if key == "http_status" and type(value) is int and 100 <= value <= 599
        }
        return result

    def reconcile_purchase_order(self, command):
        from urllib.parse import quote

        try:
            response = requests.get(
                self.url
                + "/purchase-orders/by-idempotency/"
                + quote(command.idempotency_key, safe=""),
                headers=self.headers,
                timeout=(5, 25),
                allow_redirects=False,
            )
            response.raise_for_status()
            if not 200 <= response.status_code < 300:
                raise ValueError("Unacknowledged reconciliation")
            result = self.validate_acknowledgement(command, response.json())
            result.response_metadata = {"http_status": response.status_code}
            return result
        except (requests.RequestException, ValueError, TypeError):
            return ExecutionResult(
                status="UNKNOWN",
                idempotency_key=command.idempotency_key,
                message="ERP lookup inconclusive; no submission retried",
            )

    def get_purchase_order(self, reference):
        from urllib.parse import quote

        response = requests.get(
            self.url + "/purchase-orders/" + quote(reference, safe=""),
            headers=self.headers,
            timeout=(5, 15),
            allow_redirects=False,
        )
        response.raise_for_status()
        return response.json()

    def cancel_purchase_order(self, reference, idempotency_key):
        from urllib.parse import quote

        response = requests.post(
            self.url + "/purchase-orders/" + quote(reference, safe="") + "/cancel",
            headers={**self.headers, "Idempotency-Key": idempotency_key},
            timeout=(5, 15),
            allow_redirects=False,
        )
        response.raise_for_status()
        return response.json()
