"""Compile repository Python and reject unsafe operational source regressions."""

import ast
import compileall
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = (
    "agents",
    "config",
    "core",
    "data_sources",
    "models",
    "optimization",
    "orchestrator",
    "services",
    "dashboard/backend",
    "tests",
    "scripts",
    "legacy",
)


def main():
    files = [p for directory in DIRECTORIES for p in (ROOT / directory).rglob("*.py")]
    errors = []
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        except SyntaxError as exc:
            errors.append(f"{path.relative_to(ROOT)}:{exc.lineno}:syntax error")
        if "legacy" not in path.parts and "tests" not in path.parts:
            source = path.read_text(encoding="utf-8-sig")
            if "PO-" + "EXO-" in source:
                errors.append(f"{path.relative_to(ROOT)}: hardcoded PO")
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "get"
                    and len(node.args) > 1
                ):
                    key, default = node.args[:2]
                    if (
                        isinstance(key, ast.Constant)
                        and isinstance(key.value, str)
                        and "demand" in key.value.lower()
                        and isinstance(default, ast.Constant)
                        and default.value == 100
                    ):
                        errors.append(
                            f"{path.relative_to(ROOT)}:{node.lineno}: fabricated demand fallback"
                        )
    compiled = all(compileall.compile_dir(ROOT / d, quiet=1) for d in DIRECTORIES)
    print({"python_files": len(files), "compiled": compiled, "errors": errors})
    if errors or not compiled:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
