"""Package tracked source and the tested Linux build, never local secrets/state."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile

SOURCE_DIRS = ("agents", "config", "core", "data_sources", "models", "optimization",
               "orchestrator", "services", "scripts", "dashboard/backend")
SOURCE_FILES = ("requirements.lock", "package.json", "package-lock.json")


def selected(name: str) -> bool:
    path = Path(name)
    if any(part in {".git", ".venv", "node_modules", "__pycache__", "data", "secrets"}
           or part.startswith(".env") for part in path.parts):
        return False
    if path.suffix in {".pem", ".key", ".db", ".sqlite", ".pyc"}:
        return False
    return name in SOURCE_FILES or any(name.startswith(directory + "/") for directory in SOURCE_DIRS)


def package(root: Path, output: Path, commit: str) -> None:
    # Export HEAD, not the dirty workspace or server-only package edits.
    with tempfile.TemporaryDirectory(prefix="exochain-package-") as temporary:
        stage = Path(temporary) / "release"
        stage.mkdir()
        archive = Path(temporary) / "source.tar"
        subprocess.run(["git", "archive", "--format=tar", f"--output={archive}", "HEAD"],
                       cwd=root, check=True)
        with tarfile.open(archive) as source:
            members = [member for member in source.getmembers() if selected(member.name)]
            source.extractall(stage, members=members, filter="data")
        frontend = root / "dashboard/frontend"
        runtime = stage / "dashboard/frontend/.next/standalone"
        shutil.copytree(frontend / ".next/standalone", runtime, symlinks=True)
        server_root = runtime / "dashboard/frontend"
        if not (server_root / "server.js").is_file():
            raise ValueError("Expected monorepo standalone server.js is missing")
        shutil.copytree(frontend / "public", server_root / "public", dirs_exist_ok=True)
        shutil.copytree(frontend / ".next/static", server_root / ".next/static", dirs_exist_ok=True)
        # Node route generation needs the root package, not just the frontend trace.
        shutil.copytree(root / "node_modules", stage / "node_modules", symlinks=True)
        for path in stage.rglob("*"):
            if path.name.startswith(".env") or path.suffix in {".pem", ".key", ".db", ".sqlite"}:
                raise ValueError(f"Refusing to bundle possible secret/state file: {path.relative_to(stage)}")
        marker = {"commit": commit, "run_id": os.environ.get("GITHUB_RUN_ID", "local")}
        (server_root / "public/deployment.json").write_text(json.dumps(marker) + "\n")
        (stage / "release.json").write_text(json.dumps(marker) + "\n")
        output.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(output, "w:gz", dereference=False) as bundled:
            for child in sorted(stage.iterdir()):
                bundled.add(child, arcname=child.name)
        print(f"Release SHA256: {hashlib.sha256(output.read_bytes()).hexdigest()}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if os.environ.get("GITHUB_SHA", commit) != commit:
        raise ValueError("Checkout does not match workflow commit")
    package(root, args.output.resolve(), commit)
