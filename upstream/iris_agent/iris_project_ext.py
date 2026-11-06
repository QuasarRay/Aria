import json
import subprocess


def dune_build_capture(project_dir):
    """Run dune build, capture output. Returns 'OK' on success, raw output on failure."""
    print("\n  [Running dune build to find broken proofs...]")
    try:
        result = subprocess.run(
            ["dune", "build"],
            capture_output=True, text=True, timeout=600,
            cwd=project_dir,
        )
    except subprocess.TimeoutExpired:
        return "TIMEOUT: dune build exceeded 600s"
    if result.returncode == 0:
        print("  [dune build OK — no broken proofs]")
        return "OK"
    print("  [dune build FAILED — extracting errors via LLM]")
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
    si_logic/. (proofmode/ IS scanned.)
    """
    # Search "." with cwd=project_dir so --exclude-dir only filters *nested*
    # directories, never the search root itself (project_dir may legitimately
    # be a directory named e.g. "tests").
    result = subprocess.run(
        ["grep", "-rl", "Admitted\\.", "--include=*.v",
         "--exclude-dir=_build",
         "--exclude-dir=tests",
         "--exclude-dir=iris_unstable",
         "--exclude-dir=iris_heap_lang",
         "--exclude-dir=iris_deprecated",
         "--exclude-dir=si_logic",
         "."],
        capture_output=True, text=True, cwd=project_dir
    )
    files = [f.strip() for f in result.stdout.splitlines() if f.strip()]
    # Normalize the "./foo.v" grep output to plain relative paths.
    rel_files = []
    for f in files:
        if f.startswith("./"):
            f = f[2:]
        rel_files.append(f)
    return sorted(rel_files)

def run_dune_build(project_dir):
    """Run dune build to compile the project."""
    print("\n  [Running dune build...]")
    result = subprocess.run(
        ["dune", "build"],
        capture_output=True, text=True, timeout=600,
        cwd=project_dir
    )
    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        print("  [dune build FAILED]\n" + output)
        return False
    print("  [dune build OK]")
    return True
