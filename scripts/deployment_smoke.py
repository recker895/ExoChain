"""Isolated production-image/TLS/Kafka/Redis smoke check; no business records or ERP calls.

Uses an ephemeral internal CA explicitly for this test, never as production TLS.
Retains isolated volumes for inspection; removes only its uniquely named services.
"""

import json
import os
from pathlib import Path
import secrets
import subprocess
import tempfile
import time

import requests


def main():
    root = Path(__file__).resolve().parents[1]
    project = "exochain-smoke-" + secrets.token_hex(4)
    with tempfile.TemporaryDirectory(prefix=project) as directory:
        temporary = Path(directory)
        env_file = temporary / "runtime.env"
        env_file.write_text(
            "PUBLIC_HOST=localhost\nHTTPS_BIND=127.0.0.1\nHTTPS_PORT=17443\nERP_MODE=unavailable\nBUSINESS_SOURCE_TRUST=UNVERIFIED\nBUSINESS_SOURCE_ID=\nBUSINESS_DATA_PATH=\nBUSINESS_API_URL=\n"
        )
        files = {}
        for name in (
            "operator_token",
            "approver_token",
            "ais_key",
            "business_token",
            "erp_token",
            "tls_cert",
            "tls_key",
        ):
            path = temporary / name
            path.write_text(secrets.token_hex(32) if name.endswith("token") else "")
            files[name] = {"file": str(path)}
        caddy = temporary / "Caddyfile"
        caddy.write_text(
            "{\n auto_https disable_redirects\n admin off\n}\nhttps://localhost:8443 {\n tls internal\n @backend path /api/* /health /livez /readyz\n reverse_proxy @backend api:8000\n reverse_proxy frontend:3000\n}\n"
        )
        override = temporary / "override.json"
        override.write_text(
            json.dumps(
                {
                    "secrets": files,
                    "services": {
                        "edge": {
                            "volumes": [
                                str(caddy).replace("\\", "/")
                                + ":/etc/caddy/Caddyfile:ro"
                            ]
                        }
                    },
                }
            )
        )
        env = os.environ.copy()
        env["RUNTIME_ENV_FILE"] = str(env_file)
        command = [
            "docker",
            "compose",
            "--env-file",
            str(env_file),
            "-p",
            project,
            "-f",
            str(root / "deploy/compose.yml"),
            "-f",
            str(override),
        ]

        def compose(*args, capture=False):
            return subprocess.run(
                command + list(args),
                env=env,
                check=True,
                text=True,
                capture_output=capture,
            )

        try:
            compose("config", "--quiet")
            compose(
                "up",
                "-d",
                "--no-build",
                "api",
                "frontend",
                "maritime",
                "history",
                "edge",
            )
            edge = compose("ps", "-q", "edge", capture=True).stdout.strip()
            ca = temporary / "root.crt"
            for _ in range(60):
                probe = subprocess.run(
                    [
                        "docker",
                        "cp",
                        edge + ":/data/caddy/pki/authorities/local/root.crt",
                        str(ca),
                    ],
                    capture_output=True,
                )
                if probe.returncode == 0:
                    break
                time.sleep(1)
            base = "https://localhost:17443"
            headers = {
                "Authorization": "Bearer " + (temporary / "operator_token").read_text()
            }
            for _ in range(60):
                try:
                    response = requests.get(base + "/livez", verify=str(ca), timeout=3)
                    if response.status_code == 200:
                        break
                except requests.RequestException:
                    pass
                time.sleep(1)
            else:
                raise AssertionError("API did not become live")
            assert requests.get(base, verify=str(ca), timeout=10).status_code == 200
            assert (
                requests.get(
                    base + "/api/v1/telemetry", verify=str(ca), timeout=10
                ).status_code
                == 401
            )
            readiness = requests.get(
                base + "/readyz", headers=headers, verify=str(ca), timeout=40
            )
            assert (
                readiness.status_code == 503
                and readiness.json()["status"] == "NOT_READY"
            )
            assert readiness.json()["dependencies"]["business"] == "UNAVAILABLE"
            response = requests.post(
                base + "/api/v1/runs",
                headers=headers,
                json={"request": {"operation": "ASSESSMENT"}},
                verify=str(ca),
                timeout=10,
            )
            assert response.status_code == 202
            rid = response.json()["run_id"]
            for _ in range(120):
                state = requests.get(
                    base + "/api/v1/runs/" + rid,
                    headers=headers,
                    verify=str(ca),
                    timeout=10,
                ).json()
                if state["status"] in {"BLOCKED", "FAILED", "PENDING_APPROVAL"}:
                    break
                time.sleep(1)
            assert (
                state["status"] == "BLOCKED"
                and not state["approval"]
                and not state["execution"]
            )
            count = sum(
                len(state[k]["executions"])
                for k in ("data", "intelligence", "decisions")
            )
            assert count == 19
            assert (
                requests.post(
                    base + "/api/v1/runs/" + rid + "/execute",
                    headers=headers,
                    verify=str(ca),
                    timeout=10,
                ).status_code
                == 409
            )
            compose("restart", "api")
            for _ in range(60):
                try:
                    saved = requests.get(
                        base + "/api/v1/runs/" + rid,
                        headers=headers,
                        verify=str(ca),
                        timeout=3,
                    )
                    if saved.status_code == 200:
                        assert saved.json()["status"] == "BLOCKED"
                        break
                except requests.RequestException:
                    pass
                time.sleep(1)
            else:
                raise AssertionError("Restart recovery failed")
            print(
                json.dumps(
                    {
                        "project": project,
                        "run_id": rid,
                        "status": state["status"],
                        "agents": count,
                        "tls_verified": True,
                        "restart_persistence": True,
                        "readiness": readiness.json(),
                    },
                    indent=2,
                )
            )
        finally:
            compose("down", "--timeout", "30")


if __name__ == "__main__":
    main()
