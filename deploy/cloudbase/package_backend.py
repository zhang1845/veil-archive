#!/usr/bin/env python3
"""Create a minimal local-upload build context; never include runtime secrets."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import zipfile


def package(destination: Path) -> dict:
    repo = Path(__file__).resolve().parents[2]
    backend = repo / "backend"
    files = [(repo / "deploy/cloudbase/Dockerfile", "Dockerfile")]
    for name in ("pyproject.toml", "uv.lock"):
        files.append((backend / name, name))
    if (backend / "README.md").is_file():
        files.append((backend / "README.md", "README.md"))
    for file in sorted((backend / "src").rglob("*")):
        relative = file.relative_to(backend)
        if file.is_symlink():
            raise ValueError("Build context contains a symbolic link")
        if not file.is_file():
            continue
        if any(part.startswith(".") or part in {"__pycache__", "private-data", "backups"}
               for part in relative.parts):
            continue
        if file.suffix.lower() in {".pyc", ".pyo", ".db", ".sqlite", ".log", ".dump"}:
            continue
        if file.name.endswith(("_smoke.py", "_probe.py")):
            continue
        files.append((file, relative.as_posix()))
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.exists():
        raise FileExistsError("Refusing to overwrite an existing deployment artifact")
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for source, name in files:
            if source.is_symlink() or ".env" in Path(name).parts:
                raise ValueError("Unsafe build context path")
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (0o100644 << 16)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, source.read_bytes())
    return {"archive": str(destination.resolve()), "files": len(files),
            "bytes": destination.stat().st_size,
            "sha256": sha256(destination.read_bytes()).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(json.dumps(package(args.output), ensure_ascii=False))
