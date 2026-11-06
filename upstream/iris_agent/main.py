#!/usr/bin/env python3
"""Run the iris project benchmark."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "bin"))
from workflow import run_project


def main():
    # The project directory is read from bin/agent.conf (iris_dir),
    # not passed on the command line.
    run_project()


if __name__ == "__main__":
    main()
