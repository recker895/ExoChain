"""One-time Ubuntu bootstrap. Run manually as ubuntu, never from pull requests."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess


def install(original: Path, base: Path, node: Path, uv: Path):
    if original != Path("/home/ubuntu/ExoChain") or base != Path("/home/ubuntu/exochain-deploy"):
        raise ValueError("This bootstrap is scoped to the existing ExoChain server paths")
    for required in (original / ".env", original / "data", node, uv):
        if not required.exists():
            raise ValueError(f"Missing existing runtime path: {required}")
    if (base / "runtime.json").exists():
        raise ValueError("Already bootstrapped; review existing setup instead of overwriting it")
    if (base / "current").exists() or (base / "current").is_symlink():
        raise ValueError("Existing current link needs review; refusing partial-setup overwrite")
    for service in ("exochain-backend", "exochain-frontend"):
        dropin = Path("/etc/systemd/system") / (service + ".service.d") / "50-exochain-deploy.conf"
        if dropin.exists():
            raise ValueError(f"Existing deployment drop-in needs review: {dropin}")
    subprocess.run(["sudo", "nginx", "-t"], check=True)
    for service in ("exochain-backend", "exochain-frontend"):
        subprocess.run(["systemctl", "is-active", "--quiet", service], check=True)
    base.mkdir(exist_ok=True)
    for name in ("incoming", "releases", "unit-backups"):
        (base / name).mkdir(exist_ok=True)
    shutil.copy2(Path(__file__).with_name("deploy_release.py"), base / "deploy_release.py")
    (base / "current").symlink_to(original, target_is_directory=True)
    current = base / "current"
    configurations = {
        "exochain-backend": (
            f"[Service]\nWorkingDirectory={current}\n"
            f"Environment=PATH={current}/.venv/bin:{node.parent}:/usr/local/bin:/usr/bin:/bin\n"
            f"ExecStart=\nExecStart={current}/.venv/bin/python -m uvicorn dashboard.backend.main:app "
            "--host 127.0.0.1 --port 8003 --workers 1\nTimeoutStopSec=180\n"
        ),
        "exochain-frontend": (
            f"[Service]\nWorkingDirectory={current}/dashboard/frontend\n"
            "Environment=NODE_ENV=production\nEnvironment=HOSTNAME=127.0.0.1\nEnvironment=PORT=3000\n"
            f"ExecStart=\nExecStart={node} {current}/dashboard/frontend/.next/standalone/dashboard/frontend/server.js\n"
        ),
    }
    for service, configuration in configurations.items():
        backup = subprocess.check_output(["systemctl", "cat", service], text=True)
        (base / "unit-backups" / (service + ".txt")).write_text(backup)
        dropin = Path("/etc/systemd/system") / (service + ".service.d") / "50-exochain-deploy.conf"
        if dropin.exists():
            raise ValueError(f"Refusing to overwrite existing drop-in: {dropin}")
        subprocess.run(["sudo", "mkdir", "-p", str(dropin.parent)], check=True)
        subprocess.run(["sudo", "tee", str(dropin)], input=configuration, text=True,
                       check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
    settings = {"original_root": str(original), "node": str(node), "uv": str(uv),
                "public_url": "https://exochain.space"}
    (base / "runtime.json").write_text(json.dumps(settings, indent=2) + "\n")
    print("Prepared deployment paths; current still points to the existing application.")
    print("No services restarted, no Nginx/TLS/configuration/data replaced.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--node", type=Path, required=True)
    parser.add_argument("--uv", type=Path, default=Path("/home/ubuntu/.local/bin/uv"))
    args = parser.parse_args()
    install(Path("/home/ubuntu/ExoChain"), Path("/home/ubuntu/exochain-deploy"),
            args.node.resolve(), args.uv.resolve())
