import json
import os
import subprocess
from pathlib import Path

# reglang builds with `make` (coq_makefile), inside a dedicated opam switch.
OPAM_SWITCH = os.environ.get("LRUST_OPAM_SWITCH", "reglang")


def _make_cmd(jobs=8):
    """Whole-project build command, run inside the lambda-rust opam switch."""
    return ["opam", "exec", "--switch", OPAM_SWITCH, "--", "make", f"-j{jobs}"]


def dune_build_capture(project_dir):
    """Build the project, capture output. Returns 'OK' on success, raw output on failure.
    (Name kept for compatibility; uses `make` for lambda-rust.)"""
    print("\n  [Running make to find broken proofs...]")
    try:
        result = subprocess.run(
            _make_cmd(),
            capture_output=True, text=True, timeout=1200,
            cwd=project_dir,
        )
    except subprocess.TimeoutExpired:
        return "TIMEOUT: make exceeded 1200s"
    if result.returncode == 0:
        print("  [make OK — no broken proofs]")
        return "OK"
    print("  [make FAILED — extracting errors via LLM]")
    return (result.stdout + "\n" + result.stderr).strip()


def count_coq_errors(output):
    """Count distinct 'Error:' lines in dune build output (grep-style ground truth)."""
    return sum(1 for line in output.splitlines() if line.lstrip().startswith("Error:"))


def parse_extracted(text):
    """Parse LLM-extracted error list as JSON. Returns list of dicts on success, [] on failure.

    Accepts:
      - bare JSON array
      - JSON wrapped in ```json ... ``` fences
      - JSON with preamble/postamble (slices from first [ to last ])

    Each entry is normalized to {file, line, lemma, error} as strings.
    """
    text = text.strip()

    # Strip optional markdown code fences
    if text.startswith("```"):
        lines = text.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    data = None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to slice the JSON portion if the LLM added explanatory text
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                return []
        else:
            return []

    if not isinstance(data, list):
        return []

    entries = []
    for item in data:
        if not isinstance(item, dict):
            continue
        if not all(k in item for k in ("file", "lemma", "error")):
            continue
        entries.append({
            "file": str(item.get("file", "")).strip(),
            "line": str(item.get("line", "")).strip(),
            "lemma": str(item.get("lemma", "")).strip(),
            "error": str(item.get("error", "")).strip(),
        })
    return entries


def list_length(lst):
    return len(lst)


def has_non_proof_error(entries):
    """True if any extracted entry is flagged as outside a proof body (lemma == 'NONE')."""
    return any(e.get("lemma", "").strip().upper() == "NONE" for e in entries)


def entry_file(e):
    return e["file"]


def entry_lemma(e):
    return e["lemma"]


def entry_error(e):
    return e["error"]

def find_admitted_files(project_dir):
    """Find all .v files containing Admitted. in the project.

    Excludes: _build/, tests/, iris_unstable/, iris_heap_lang/, iris_deprecated/,
    proofmode/, si_logic/.
    """
    result = subprocess.run(
        ["grep", "-rl", "Admitted\\.", "--include=*.v",
         "--exclude-dir=_build",
         "--exclude-dir=tests",
         "--exclude-dir=iris_unstable",
         "--exclude-dir=iris_heap_lang",
         "--exclude-dir=iris_deprecated",
         "--exclude-dir=proofmode",
         "--exclude-dir=si_logic",
         project_dir],
        capture_output=True, text=True
    )
    files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
    # Make paths relative to project_dir
    rel_files = []
    for f in files:
        try:
            rel_files.append(str(Path(f).relative_to(project_dir)))
        except ValueError:
            rel_files.append(f)
    return sorted(rel_files)

def run_dune_build(project_dir):
    """Compile the project. (Name kept for compatibility; uses `make`.)"""
    print("\n  [Running make...]")
    result = subprocess.run(
        _make_cmd(),
        capture_output=True, text=True, timeout=1200,
        cwd=project_dir
    )
    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        print("  [make FAILED]\n" + output)
        return False
    print("  [make OK]")
    return True
