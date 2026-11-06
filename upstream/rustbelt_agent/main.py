#!/usr/bin/env python3
"""Run the iris project benchmark."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "bin"))
from workflow import run_project


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 main.py <project_dir>")
        print("Example: python3 main.py ~/MyProjects/iris")
        sys.exit(1)

    project_dir = sys.argv[1]
    run_project(project_dir)


if __name__ == "__main__":
    main()
