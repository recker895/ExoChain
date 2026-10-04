"""TLS-verified authenticated readiness and optional real request; no approval/ERP."""

import argparse
import json
import time
from pathlib import Path

import requests

from config.settings import settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--request")
    args = parser.parse_args()
    if not args.url.startswith("https://"):
        parser.error("Production verification requires HTTPS")
    headers = {
        "Authorization": "Bearer " + settings.OPERATOR_API_TOKEN.get_secret_value()
    }
    base = args.url.rstrip("/")
    readiness = requests.get(
        base + "/readyz", headers=headers, timeout=60, allow_redirects=False
    )
    print(
        json.dumps(
            {"readiness_http": readiness.status_code, "readiness": readiness.json()},
            indent=2,
        )
    )
    readiness.raise_for_status()
    operations = requests.get(
        base + "/api/v1/operations", headers=headers, timeout=60, allow_redirects=False
    )
    operations.raise_for_status()
    print(json.dumps(operations.json(), indent=2))
    if args.request:
        payload = json.loads(Path(args.request).read_text(encoding="utf-8"))
        if not payload.get("request", {}).get("require_human_approval", True):
            parser.error("Verification requires human approval")
        response = requests.post(
            base + "/api/v1/runs",
            headers=headers,
            json=payload,
            timeout=30,
            allow_redirects=False,
        )
        response.raise_for_status()
        rid = response.json()["run_id"]
        for _ in range(240):
            response = requests.get(
                base + "/api/v1/runs/" + rid,
                headers=headers,
                timeout=30,
                allow_redirects=False,
            )
            response.raise_for_status()
            state = response.json()
            if state["status"] in {"BLOCKED", "FAILED", "PENDING_APPROVAL"}:
                print(
                    json.dumps(
                        {
                            "run_id": rid,
                            "status": state["status"],
                            "validation": state["validation"],
                        },
                        indent=2,
                    )
                )
                if state.get("execution"):
                    raise AssertionError("Unexpected execution before human approval")
                return
            time.sleep(1)
        raise TimeoutError("Run did not terminate within verification deadline")


if __name__ == "__main__":
    main()
