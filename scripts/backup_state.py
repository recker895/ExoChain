"""Online SQLite backup and verified restore into a NEW directory only."""

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


def backup(sources, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    manifest = {}
    for name, source in sources.items():
        source = Path(source).resolve(strict=True)
        target = destination / (name + ".db")
        with (
            sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as src,
            sqlite3.connect(target) as dst,
        ):
            src.backup(dst)
            if dst.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise ValueError("Backup integrity check failed")
        manifest[target.name] = hashlib.sha256(target.read_bytes()).hexdigest()
    (destination / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def restore(source, destination):
    source, destination = Path(source), Path(destination)
    manifest = json.loads((source / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest.items():
        if Path(name).name != name or not name.endswith(".db"):
            raise ValueError("Invalid backup filename")
        if hashlib.sha256((source / name).read_bytes()).hexdigest() != digest:
            raise ValueError("Backup checksum mismatch")
    backup({Path(name).stem: source / name for name in manifest}, destination)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["backup", "restore"])
    parser.add_argument("destination")
    parser.add_argument("--runs", default="data/decision_runs.db")
    parser.add_argument("--history", default="data/ais_history.db")
    parser.add_argument("--source")
    args = parser.parse_args()
    if args.action == "restore":
        if not args.source:
            parser.error("restore requires --source")
        restore(args.source, args.destination)
    else:
        backup(
            {"decision_runs": args.runs, "ais_history": args.history}, args.destination
        )
