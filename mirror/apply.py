"""Apply a validated source snapshot without touching automation files."""
from __future__ import annotations
import argparse
import json
import shutil
from pathlib import Path


def apply(stage: Path, destination: Path) -> None:
    stage = stage.resolve()
    destination = destination.resolve()
    source = stage / "source"
    manifest_path = stage / "manifest.json"
    if not source.is_dir() or not manifest_path.is_file():
        raise RuntimeError("Missing validated snapshot or manifest")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("files", 0) < 1:
        raise RuntimeError("Empty snapshot; refusing update")
    if destination.name != "upstream":
        raise RuntimeError("Refusing changes outside upstream/")
    if source.is_symlink() or any(p.is_symlink() for p in source.rglob("*")):
        raise RuntimeError("Symlinks are not permitted")
    replacement = destination.parent / ".upstream-replacement"
    if replacement.exists():
        shutil.rmtree(replacement)
    shutil.copytree(source, replacement, copy_function=shutil.copy2)
    if destination.exists():
        shutil.rmtree(destination)
    replacement.rename(destination)
    print(f"Applied {manifest['files']} files to {destination}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", default=".mirror-stage", type=Path)
    parser.add_argument("--destination", default="upstream", type=Path)
    args = parser.parse_args()
    apply(args.stage, args.destination)
