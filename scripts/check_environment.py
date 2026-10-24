#!/usr/bin/env python
"""Verify a reproducible ABGES Python and engine environment."""
from __future__ import annotations
import argparse, hashlib, importlib, json, sys
from pathlib import Path

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
for relative in ("tools/Python", "tools/HIBLUP", "tools/PLINK"):
    location = str(ROOT / relative)
    if location not in sys.path:
        sys.path.insert(0, location)


def sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-engines", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    checks = []
    checks.append({"name": "python", "ok": sys.version_info[:2] == (3, 14),
                   "actual": ".".join(map(str, sys.version_info[:3])), "required": "3.14.x"})
    for name in ("yaml", "pandas", "numpy", "scipy", "docx", "matplotlib", "openpyxl", "chardet", "pypinyin", "mcp"):
        try:
            module = importlib.import_module(name)
            checks.append({"name": name, "ok": True, "actual": getattr(module, "__version__", "installed")})
        except Exception as exc:
            checks.append({"name": name, "ok": False, "actual": repr(exc)})
    manifest = json.loads((ROOT / "tools" / "ENGINE_MANIFEST.json").read_text(encoding="utf-8"))
    for engine in manifest["engines"]:
        binary = ROOT / engine["path"]
        exists = binary.is_file()
        sha256_valid = exists and sha256(binary).lower() == engine["sha256"].lower()
        engine_dir = binary.parent
        missing_required_files = [
            name for name in engine.get("required_files", [])
            if not (engine_dir / name).is_file()
        ]
        valid = sha256_valid and not missing_required_files
        checks.append({"name": engine["name"], "ok": valid if args.require_engines else True,
                       "available": exists, "sha256_valid": sha256_valid,
                       "missing_required_files": missing_required_files,
                       "expected_sha256": engine["sha256"], "path": str(binary)})
    result = {"status": "ok" if all(item["ok"] for item in checks) else "failed", "checks": checks}
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else result)
    return 0 if result["status"] == "ok" else 1

if __name__ == "__main__":
    raise SystemExit(main())
