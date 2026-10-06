"""Fail closed before CI can reintroduce the unpatched server dependencies."""

import json
from pathlib import Path
import re


def check(root: Path) -> None:
    package = json.loads((root / "dashboard/frontend/package.json").read_text())
    lock = json.loads((root / "dashboard/frontend/package-lock.json").read_text())
    for name, section in (("next", "dependencies"), ("eslint-config-next", "devDependencies")):
        pinned = package[section][name]
        if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", pinned):
            raise ValueError(f"{name} must use an exact stable version")
        version = tuple(map(int, pinned.split(".")))
        if version < (16, 3, 6):
            raise ValueError(
                f"{name} is below the existing server baseline (16.3.6). "
                "First copy the server's patched package.json and package-lock.json "
                "back into dashboard/frontend; see deploy/ec2/README.md."
            )
        if lock["packages"][f"node_modules/{name}"]["version"] != pinned:
            raise ValueError(f"{name} package.json and lockfile do not match")
        if lock["packages"][""][section][name] != pinned:
            raise ValueError(f"{name} lockfile root does not match package.json")
    if package["dependencies"]["next"] != package["devDependencies"]["eslint-config-next"]:
        raise ValueError("Next.js and its ESLint configuration must match")


if __name__ == "__main__":
    check(Path(__file__).resolve().parents[2])
