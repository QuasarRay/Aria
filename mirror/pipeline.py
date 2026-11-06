"""Dagger Python pipeline for isolated tests and upstream snapshot acquisition."""
from __future__ import annotations
import argparse
import asyncio
import sys
from pathlib import Path
import dagger
from dagger import dag
from fetch import SOURCE_URL

PROJECT_ROOT = Path(__file__).resolve().parent.parent


async def run(mode: str, export_path: Path | None) -> None:
    async with dagger.connection(dagger.Config(log_output=sys.stderr)):
        sources = dag.host().directory(str(PROJECT_ROOT), include=["mirror/**"])
        container = (
            dag.container()
            .from_("python:3.12-slim")
            .with_directory("/workspace", sources)
            .with_workdir("/workspace")
        )
        if mode == "test":
            result = await container.with_exec(
                ["python", "-m", "unittest", "discover", "-s", "mirror/tests", "-v"]
            ).stdout()
            print(result)
            return
        if export_path is None:
            raise ValueError("--export is required for fetch")
        container = container.with_exec([
            "python", "mirror/fetch.py", "--url", SOURCE_URL, "--output", "/out"
        ])
        await container.directory("/out").export(str(export_path.resolve()))
        print(f"Validated source snapshot exported to {export_path}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["test", "fetch"])
    parser.add_argument("--export", type=Path)
    args = parser.parse_args()
    asyncio.run(run(args.mode, args.export))


if __name__ == "__main__":
    main()
