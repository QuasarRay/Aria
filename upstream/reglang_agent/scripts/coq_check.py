#!/usr/bin/env python3
"""Check a Coq file in a make-based (coq_makefile) project, with goal
extraction on failure.

This is the lambda-rust (RustBelt) variant of the verifier: unlike the Iris
development, lambda-rust ships no dune project, so it is built with `make`
(coq_makefile from _CoqProject) inside a dedicated opam switch.

Fast path: `make <file>.vo`.
On failure: replay the proof up to the failing line with coqtop (using the
_CoqProject flags) and extract the open goal.

Environment:
  LRUST_OPAM_SWITCH   opam switch holding Rocq + Iris dev (default: lambda-rust)
"""

import os
import re
import select
import subprocess
import sys
from pathlib import Path

# opam switch in which the reglang dependencies (Coq 8.18 + MathComp) are installed.
OPAM_SWITCH = os.environ.get("LRUST_OPAM_SWITCH", "reglang")

MAKE_TIMEOUT = 300   # seconds for a single-file `make`
GOAL_TIMEOUT = 120   # seconds for coqtop goal extraction
LINE_TIMEOUT = 30    # seconds per line in the line-by-line coqtop fallback


def _in_switch(cmd):
    """Wrap a command so it runs in the lambda-rust opam switch."""
    return ["opam", "exec", "--switch", OPAM_SWITCH, "--"] + cmd


def _find_project_dir(filepath):
    """Walk up from filepath to the make/coq_makefile project root."""
    p = Path(filepath).resolve().parent
    while p != p.parent:
        if (p / "Makefile").exists() and (
            (p / "_CoqProject").exists() or (p / "_RocqProject").exists()
        ):
            return str(p)
        p = p.parent
    return str(Path(filepath).resolve().parent)


def main():
    if len(sys.argv) < 2:
        print("Usage: coq_check.py <file.v> [project_dir]")
        sys.exit(1)

    filepath = sys.argv[1]
    project_dir = sys.argv[2] if len(sys.argv) > 2 else _find_project_dir(filepath)
    print(f"[debug] project dir: {project_dir}", file=sys.stderr)

    if not Path(filepath).exists():
        print(f"File not found: {filepath}")
        sys.exit(1)

    rel_file = filepath
    try:
        rel_file = str(Path(filepath).resolve().relative_to(Path(project_dir).resolve()))
    except ValueError:
        pass

    lines = Path(filepath).read_text().splitlines()

    # Fast path: make the .vo.
    ok, error_output = make_check(rel_file, project_dir)
    if ok:
        print("OK")
        sys.exit(0)

    # If make timed out (a stuck/divergent tactic), fall back to a
    # line-by-line coqtop replay to pinpoint the offending line.
    if error_output.startswith("Timeout:"):
        print(error_output, file=sys.stderr)
        print("Falling back to line-by-line coqtop to find the stuck tactic...",
              file=sys.stderr)
        sys.exit(coqtop_fallback(project_dir, lines))

    # On failure, pinpoint the line and recover the open goal.
    error_line = parse_error_line(error_output, rel_file)
    if error_line:
        print(f"Error at line {error_line}:")
        if error_line <= len(lines):
            print(f"  {lines[error_line - 1].strip()}")
        print(error_output)
        goal = extract_goal_at(rel_file, project_dir, lines, error_line)
        if goal:
            print(f"\nGoal at line {error_line}:")
            print(goal)
    else:
        print(error_output)
    sys.exit(1)


def make_check(rel_file, project_dir):
    """Run `make <file>.vo`. Returns (ok, error_output)."""
    vo_target = (rel_file + "o") if rel_file.endswith(".v") else (rel_file + ".vo")
    # Force a recompile so an edited/stripped proof is actually re-checked.
    try:
        (Path(project_dir) / vo_target).unlink()
    except FileNotFoundError:
        pass

    cmd = _in_switch(["make", vo_target])
    print(f"[debug] cmd: {' '.join(cmd)}", file=sys.stderr)
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=MAKE_TIMEOUT, cwd=project_dir
        )
    except subprocess.TimeoutExpired:
        return False, f"Timeout: make took longer than {MAKE_TIMEOUT}s. A tactic may be stuck."

    output = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        return True, ""
    return False, output


def parse_error_line(error_output, rel_file):
    """Extract the error line number from coqc/make output."""
    name = re.escape(Path(rel_file).name)
    patterns = [
        re.compile(r'File ".*?' + name + r'", line (\d+)'),
        re.compile(name + r':(\d+):\d+:'),
    ]
    for pattern in patterns:
        m = pattern.search(error_output)
        if m:
            return int(m.group(1))
    return None


def extract_goal_at(rel_file, project_dir, lines, error_line):
    """Replay up to error_line - 1 with coqtop, then Show the goal."""
    cmd = build_coqtop_cmd(project_dir)
    partial = "\n".join(lines[: error_line - 1]) + "\nShow.\n"
    try:
        result = subprocess.run(
            cmd, input=partial, capture_output=True, text=True,
            timeout=GOAL_TIMEOUT, cwd=project_dir,
        )
        return extract_last_goal(result.stdout + result.stderr)
    except subprocess.TimeoutExpired:
        return None


def extract_last_goal(output):
    """Extract the last goal block from coqtop output."""
    buf, found = [], False
    for line in output.split("\n"):
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


def build_coqtop_cmd(project_dir):
    """Coq REPL with the project's _CoqProject library mappings, in the switch.

    reglang is Coq 8.18, whose REPL is the legacy `coqtop` (Rocq 9.x renamed it
    to `rocq repl`).
    """
    # _CoqProject wraps coqc options as `-arg <flag>` (a coq_makefile directive).
    # coqtop wants the flag directly, so drop the `-arg` wrappers; the wrapped
    # token (e.g. `-w`, `-notation-overridden`) is kept and passed through.
    flags = [f for f in read_coq_project(project_dir) if f != "-arg"]
    return _in_switch(["coqtop"] + flags + ["-quiet"])


def _repl_available(cmd):
    """Probe whether the REPL binary actually runs (Rocq renamed coqtop).
    Returns False if the binary is missing, so callers can fall back to an
    honest message instead of a misleading 'coqtop exited' artifact."""
    try:
        r = subprocess.run(
            cmd, input="Check 0.\n", capture_output=True, text=True, timeout=60
        )
    except Exception:
        return False
    out = r.stdout + r.stderr
    if "ommand not found" in out:  # opam exec: "Command not found 'coqtop'"
        return False
    return ("Rocq <" in out) or ("Coq <" in out) or ("Welcome" in out)


def read_coq_project(project_dir):
    """Read -Q/-R/-arg flags from _CoqProject or _RocqProject."""
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


def coqtop_fallback(project_dir, lines):
    """Replay the file into the REPL one line at a time to locate the line that
    errors or diverges. Used when `make` times out (a stuck tactic)."""
    cmd = build_coqtop_cmd(project_dir)

    # If the REPL binary is unavailable, do NOT fabricate a bogus
    # "coqtop exited at line N": report the real make-level timeout instead.
    if not _repl_available(cmd):
        print(f"Timeout: make exceeded {MAKE_TIMEOUT}s — a tactic in your proof "
              f"likely diverges (non-terminating). The line-by-line localizer "
              f"could not run, so the exact line is not pinpointed.")
        return 1

    proc = subprocess.Popen(
        cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, cwd=project_dir,
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
            return "".join(output_parts) if output_parts else None
        chunk = os.read(proc.stdout.fileno(), 4096)
        if not chunk:
            return "".join(output_parts)
        output_parts.append(chunk.decode("utf-8", errors="replace"))
        timeout = 0.1


def show_goal_fresh(cmd, lines, error_line, project_dir):
    """Start a fresh coqtop and show the goal at the error line."""
    partial = "\n".join(lines[: error_line - 1]) + "\nShow.\n"
    try:
        result = subprocess.run(
            cmd, input=partial, capture_output=True, text=True,
            timeout=GOAL_TIMEOUT, cwd=project_dir,
        )
        goal = extract_last_goal(result.stdout + result.stderr)
        if goal:
            print(f"\nGoal at line {error_line}:")
            print(goal)
    except subprocess.TimeoutExpired:
        print("\nCould not get goal (timed out)")


if __name__ == "__main__":
    main()
