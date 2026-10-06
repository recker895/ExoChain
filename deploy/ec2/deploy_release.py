"""Prepare immutable releases, switch services, and roll back failed health checks.

Installed once on EC2 by bootstrap.py; no application secrets live in this file.
"""

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import time
from urllib.request import Request, urlopen
import uuid

RELEASE_ID = re.compile(r"[0-9a-f]{40}-[0-9]+-[0-9]+")
SERVICES = ("exochain-backend.service", "exochain-frontend.service")


def command(args, **kwargs):
    subprocess.run(args, check=True, **kwargs)


def replace_link(link: Path, target: Path):
    temporary = link.with_name(link.name + "." + uuid.uuid4().hex)
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, link)


def extract_release(archive: Path, destination: Path):
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            path = PurePosixPath(member.name)
            if path.is_absolute() or ".." in path.parts or "\\" in member.name:
                raise ValueError("Unsafe archive path")
            if path.parts and (path.parts[0] in {"data", ".venv", ".git"}
                               or any(part.startswith(".env") for part in path.parts)):
                raise ValueError("Archive must not contain persistent state or environment files")
            if member.isdev() or member.isfifo():
                raise ValueError("Unsupported archive member")
        # Also rejects escaping symlinks/hardlinks, including links through other members.
        bundle.extractall(destination, filter="data")


def prepare(base: Path, settings: dict, release_id: str) -> Path:
    if not RELEASE_ID.fullmatch(release_id):
        raise ValueError("Invalid release ID")
    release = base / "releases" / release_id
    if release.exists():
        raise ValueError("Release already exists; use a new workflow attempt")
    archive = base / "incoming" / (release_id + ".tar.gz")
    if not archive.is_file():
        raise ValueError("Uploaded release archive is missing")
    if shutil.disk_usage(base).free < 2 * 1024**3:
        raise ValueError("Less than 2 GiB free; review old releases before deploying")
    release.mkdir()
    extract_release(archive, release)
    marker = json.loads((release / "release.json").read_text())
    commit, run_id, _attempt = release_id.split("-")
    if marker.get("commit") != commit or str(marker.get("run_id")) != run_id:
        raise ValueError("Release identity does not match the requested workflow")
    required = ("requirements.lock", "dashboard/backend/main.py",
                "node_modules/@arcnautical/maritime-routing/package.json",
                "dashboard/frontend/.next/standalone/dashboard/frontend/server.js",
                "dashboard/frontend/.next/standalone/dashboard/frontend/public/deployment.json")
    if any(not (release / path).is_file() for path in required):
        raise ValueError("Release is incomplete")
    original = Path(settings["original_root"])
    for name in ("data", ".env"):
        source = original / name
        if not source.exists():
            raise ValueError(f"Original shared {name} is missing")
        (release / name).symlink_to(source, target_is_directory=name == "data")
    environment = os.environ.copy()
    environment["PATH"] = str(Path(settings["node"]).parent) + ":" + environment.get("PATH", "")
    command([settings["uv"], "venv", "--python", "3.13", str(release / ".venv")], env=environment)
    command([settings["uv"], "pip", "install", "--python", str(release / ".venv/bin/python"),
             "--require-hashes", "-r", str(release / "requirements.lock")],
            cwd=release, env=environment)
    return release


def get(url: str):
    with urlopen(Request(url, headers={"Cache-Control": "no-cache"}), timeout=10) as response:
        return response.read()


def health(settings: dict, marker: dict | None, timeout=180):
    deadline = time.monotonic() + timeout
    error = None
    while time.monotonic() < deadline:
        try:
            if json.loads(get("http://127.0.0.1:8003/livez"))["status"] != "ALIVE":
                raise ValueError("Backend is not live")
            runs = json.loads(get("http://127.0.0.1:8003/api/v1/runs"))
            if not isinstance(runs, (list, dict)):
                raise ValueError("Invalid runs response")
            if b"exochain" not in get("http://127.0.0.1:3000/").lower():
                raise ValueError("Frontend HTML is missing")
            public = settings["public_url"].rstrip("/")
            if b"exochain" not in get(public + "/").lower():
                raise ValueError("Public HTTPS frontend failed")
            json.loads(get(public + "/api/v1/runs"))
            if marker is not None:
                deployed = json.loads(get(public + "/deployment.json?commit=" + marker["commit"]))
                if deployed != marker:
                    raise ValueError("Public HTTPS is serving a different release")
            return
        except (OSError, ValueError, KeyError) as exc:
            error = exc
            time.sleep(2)
    raise RuntimeError(f"Health checks did not pass: {error}")


def activate(base: Path, settings: dict, release: Path):
    current = base / "current"
    old = current.resolve(strict=True)
    marker = json.loads((release / "release.json").read_text())
    try:
        # Stop before switching so workers cannot mix imports from two releases.
        command(["sudo", "systemctl", "stop", *SERVICES])
        replace_link(current, release)
        command(["sudo", "systemctl", "start", *SERVICES])
        health(settings, marker)
    except Exception as deployment_error:
        print("Deployment failed; restoring the previous application release", flush=True)
        try:
            command(["sudo", "systemctl", "stop", *SERVICES])
            replace_link(current, old)
            command(["sudo", "systemctl", "start", *SERVICES])
            previous_marker = old / "release.json"
            health(settings, json.loads(previous_marker.read_text()) if previous_marker.exists() else None)
        except Exception as rollback_error:
            raise RuntimeError("Deployment AND rollback health checks failed; inspect systemd logs") from rollback_error
        raise RuntimeError("Deployment failed; previous release restored and health checked") from deployment_error
    replace_link(base / "previous", old)
    print(f"Activated {release.name}; previous release retained at {old}", flush=True)


def deploy(base: Path, release_id: str):
    import fcntl  # Linux advisory lock also protects disconnected systemd deployments.

    with (base / "deployment.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        settings = json.loads((base / "runtime.json").read_text())
        release = prepare(base, settings, release_id)
        activate(base, settings, release)
        # Keep archives/releases for manual recovery; never delete shared data.


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-id", required=True)
    args = parser.parse_args()
    deploy(Path(__file__).resolve().parent, args.release_id)
