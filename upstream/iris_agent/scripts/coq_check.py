#!/usr/bin/env python3
"""Check a Coq file using dune build, with goal extraction on failure.

Fast path: runs "dune build" to check the whole file.
On failure: uses coqtop to replay up to the error line and extract the goal.

Prerequisites: project dependencies must already be built.
"""

import os
import re
import select
import subprocess
import sys
from pathlib import Path

LINE_TIMEOUT = 30


def _find_project_dir(filepath):
    """Walk up from filepath to find the project root."""
    p = Path(filepath).resolve().parent
    # First pass: look for dune-project
    q = p
    while q != q.parent:
        if (q / "dune-project").exists():
            return str(q)
        q = q.parent
    # Second pass: look for other markers
    while p != p.parent:
        if any((p / m).exists() for m in ["_CoqProject", "_RocqProject", "Makefile"]):
            return str(p)
        p = p.parent
    return str(Path(filepath).resolve().parent)


def main():
    if len(sys.argv) < 2:
        print("Usage: coq_check.py <file.v> [project_dir]")
        sys.exit(1)

    filepath = sys.argv[1]
    if len(sys.argv) > 2:
        project_dir = sys.argv[2]
    else:
        project_dir = _find_project_dir(filepath)
        print(f"[debug] auto-detected project dir: {project_dir}", file=sys.stderr)

    if not Path(filepath).exists():
        print(f"File not found: {filepath}")
        sys.exit(1)

    rel_file = filepath
    try:
        rel_file = str(Path(filepath).relative_to(project_dir))
    except ValueError:
        pass

    lines = Path(filepath).read_text().splitlines()

    # Fast path: dune build
    ok, error_output = dune_check(rel_file, project_dir)
    if ok:
        print("OK")
        sys.exit(0)

    # If dune timed out, fall back to coqtop to find the stuck line
    if error_output.startswith("Timeout:"):
        print(error_output, file=sys.stderr)
        print("Falling back to coqtop to find stuck tactic...", file=sys.stderr)
        sys.exit(coqtop_fallback(rel_file, project_dir, lines))

    # Parse error line from dune output
    error_line = parse_error_line(error_output, rel_file)
    if error_line:
        print(f"Error at line {error_line}:")
        if error_line <= len(lines):
            print(f"  {lines[error_line - 1].strip()}")
        print(error_output)

        # Extract goal at error line using coqtop
        goal = extract_goal_at(rel_file, project_dir, lines, error_line)
        if goal:
            print(f"\nGoal at line {error_line}:")
            print(goal)
    else:
        print(error_output)

    sys.exit(1)


def coqtop_fallback(rel_file, project_dir, lines):
    """Fall back to old line-by-line coqtop check."""
    cmd = build_coqtop_cmd(rel_file, project_dir)
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=project_dir,
    )

    for i, line in enumerate(lines, 1):
        try:
            proc.stdin.write(line + "\n")
            proc.stdin.flush()
        except BrokenPipeError:
            output = proc.stdout.read()
            print(f"Error at line {i}: coqtop exited")
            print(output)
            show_goal_fresh(cmd, lines, i, project_dir)
            return 1

        output = drain_output(proc, LINE_TIMEOUT)

        if output is None:
            print(f"Timeout at line {i} (>{LINE_TIMEOUT}s):")
            print(f"  {line.strip()}")
            proc.kill()
            show_goal_fresh(cmd, lines, i, project_dir)
            return 1

        if output and "Error:" in output:
            print(f"Error at line {i}:")
            print(f"  {line.strip()}")
            print(output)
            proc.kill()
            show_goal_fresh(cmd, lines, i, project_dir)
            return 1

    proc.stdin.close()
    proc.wait()
    print("OK (coqtop fallback)")
    return 0


def drain_output(proc, timeout):
    """Read available output from proc.stdout with a timeout."""
    output_parts = []
    while True:
        ready, _, _ = select.select([proc.stdout], [], [], timeout)
        if not ready:
            if not output_parts:
                return None
            return "".join(output_parts)
        chunk = os.read(proc.stdout.fileno(), 4096)
        if not chunk:
            return "".join(output_parts)
        output_parts.append(chunk.decode("utf-8", errors="replace"))
        timeout = 0.1


def show_goal_fresh(cmd, lines, error_line, project_dir):
    """Start a fresh coqtop and show the goal at the error line."""
    partial = "\n".join(lines[:error_line - 1]) + "\nShow.\n"
    try:
        result = subprocess.run(
            cmd,
            input=partial,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=project_dir,
        )
        output = result.stdout + result.stderr
        goal = extract_last_goal(output)
        if goal:
            print(f"\nGoal at line {error_line}:")
            print(goal)
    except subprocess.TimeoutExpired:
        print(f"\nCould not get goal (timed out)")


def dune_check(rel_file, project_dir):
    """Run dune build to check the file. Returns (ok, error_output)."""
    project = Path(project_dir)

    # Build the .vo target from the .v file
    vo_target = rel_file.replace(".v", ".vo")
    cmd = ["dune", "build", f"_build/default/{vo_target}"]

    # Wrap in nix if needed
    if (project / "flake.nix").exists():
        cmd = ["nix", "develop", "-c"] + cmd

    print(f"[debug] cmd: {' '.join(cmd)}", file=sys.stderr)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=project_dir,
        )
    except subprocess.TimeoutExpired:
        return False, "Timeout: dune build took longer than 30s. A tactic may be stuck."

    output = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        return True, ""
    return False, output


def parse_error_line(error_output, rel_file):
    """Extract the error line number from dune/coqc output."""
    # Patterns like: File "./iris/algebra/agree.v", line 136, characters 0-27:
    # Or: iris/algebra/agree.v:136:0: Error:
    patterns = [
        re.compile(r'File ".*?' + re.escape(Path(rel_file).name) + r'", line (\d+)'),
        re.compile(re.escape(Path(rel_file).name) + r':(\d+):\d+:'),
    ]
    for pattern in patterns:
        m = pattern.search(error_output)
        if m:
            return int(m.group(1))
    return None


def extract_goal_at(rel_file, project_dir, lines, error_line):
    """Start coqtop, replay up to error_line - 1, then run Show to get the goal."""
    cmd = build_coqtop_cmd(rel_file, project_dir)
    partial = "\n".join(lines[:error_line - 1]) + "\nShow.\n"

    try:
        result = subprocess.run(
            cmd,
            input=partial,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=project_dir,
        )
        output = result.stdout + result.stderr
        goal = extract_last_goal(output)
        return goal
    except subprocess.TimeoutExpired:
        return None


def extract_last_goal(output):
    """Extract the last goal block from coqtop output."""
    lines = output.split("\n")
    buf = []
    found = False
    for line in lines:
        stripped = line.strip()
        if re.match(r'^(\w+ < ?|Rocq < ?)$', stripped):
            continue
        if re.match(r'\d+ goals?', stripped):
            found = True
            buf = []
        if found:
            if stripped.startswith("Rocq <") or stripped.endswith(" <"):
                continue
            buf.append(line)
    return "\n".join(buf).strip() if buf else None


def build_coqtop_cmd(rel_file, project_dir):
    """Build the coqtop command for goal extraction."""
    project = Path(project_dir)
    is_dune = (project / "dune-project").exists()
    has_nix = (project / "flake.nix").exists()

    if is_dune:
        dune_top = "coq"
        for dune_file in project.rglob("dune"):
            try:
                if "rocq.theory" in dune_file.read_text():
                    dune_top = "rocq"
                    break
            except (PermissionError, IsADirectoryError):
                pass
        cmd = ["dune", dune_top, "top", rel_file]
    else:
        flags = read_coq_project(project_dir)
        cmd = ["coqtop"] + flags + ["-quiet"]

    if has_nix:
        cmd = ["nix", "develop", "-c"] + cmd

    return cmd


def read_coq_project(project_dir):
    """Read _CoqProject or _RocqProject flags."""
    for name in ("_CoqProject", "_RocqProject"):
        path = Path(project_dir) / name
        if path.exists():
            flags = []
            for line in path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not line.endswith(".v"):
                    flags.extend(line.split())
            return flags
    return []


if __name__ == "__main__":
    main()
