import subprocess
import time as _time
import os
from pathlib import Path
from datetime import datetime, timedelta

# Runtime helpers (mirrored from generated workflow.py)
Allow = "allow"
def Deny(reason): return ("deny", reason)
def Ask(reason): return ("ask", reason)
def Ok(value): return ("ok", value)
def Err(msg): return ("err", msg)

# Path to coq_check.py. For the lambda-rust (RustBelt) experiment this points
# at the make-based verifier in scripts_lrust/ (the Iris one is dune-based).
# Override with LOOM_SCRIPTS_DIR if needed.
SCRIPTS_DIR = os.environ.get(
    "LOOM_SCRIPTS_DIR",
    str(Path(__file__).resolve().parent.parent / "scripts_lrust"),
)

# Night-only mode: set LOOM_NIGHT_ONLY=1 to only run during 22:00-07:00
# Usage: LOOM_NIGHT_ONLY=1 python3 main.py ~/MyProjects/iris
def check_schedule():
    """If LOOM_NIGHT_ONLY=1, wait until 22:00-05:00 window."""
    if os.environ.get("LOOM_NIGHT_ONLY") != "1":
        return
    while True:
        hour = datetime.now().hour
        if hour >= 22 or hour < 5:
            return
        now = datetime.now()
        target = now.replace(hour=22, minute=0, second=0, microsecond=0)
        if target < now:
            target = target + timedelta(days=1)
        wait_secs = (target - now).total_seconds()
        print(f"  [Outside night window. Waiting until 22:00 ({int(wait_secs/60)} min)...]")
        _time.sleep(wait_secs)

# Results log file
RESULTS_FILE = str(Path(__file__).resolve().parent / "benchmark_results.log")

def record_result(prompt, project_dir, status, duration_secs, retries):
    """Extract lemma name and file from prompt, append to results log."""
    import re
    # Extract lemma name: "proof for the lemma X" or "lemma X in"
    lemma_match = re.search(r'lemma\s+(\w+)', prompt)
    lemma = lemma_match.group(1) if lemma_match else "unknown"
    # Extract file path: "in <path>.v"
    file_match = re.search(r'in\s+(\S+\.v)', prompt)
    file_path = file_match.group(1) if file_match else "unknown"

    timestamp = datetime.now().isoformat()
    with open(RESULTS_FILE, "a") as f:
        f.write(timestamp + " | " + status + " | " + lemma + " | " + file_path + " | " + str(round(duration_secs, 1)) + "s | retries: " + str(retries) + "\n")

def start_timer():
    return _time.time()

def elapsed(start):
    return _time.time() - start

# --- Pre-hook: block new Admitted ---
# Only deny if the edit introduces MORE Admitted. than before.
def no_admitted_fn(file, original):
    if not file.path.endswith(".v"):
        return Allow
    old_count = original.count("Admitted.")
    new_count = file.content.count("Admitted.")
    if new_count > old_count:
        return Deny("Cannot add new Admitted. (had " + str(old_count) + ", now " + str(new_count) + ")")
    return Allow

# --- Pre-hook: iris linter ---
# Check that Instance/Arguments/Hint etc. have explicit Global/Local locality.
def lint_fn(file, original):
    if not file.path.endswith(".v"):
        return Allow
    import re
    pattern = re.compile(
        r'^\s*((Existing\s+|Program\s+|Declare\s+)?Instance|Arguments|Remove|'
        r'Hint\s+(Extern|Constructors|Resolve|Immediate|Mode|Opaque|Transparent|Unfold|Rewrite)|'
        r'(Open|Close)\s+Scope|Opaque|Transparent|Typeclasses (Opaque|Transparent))\b'
    )
    for i, line in enumerate(file.content.splitlines(), 1):
        if pattern.match(line):
            return Deny("Line " + str(i) + ": missing Global/Local locality: " + line.strip())
    return Allow

# --- Pre-hook: safe bash commands (allowlist) ---
# Integrity: the upstream proofs are PUBLIC (gitlab.mpi-sws.org), and the local
# copy + .git were removed, so the only way the agent could still copy them is by
# fetching from the network or reaching outside the working tree. We therefore
# allow ONLY read-only inspection of the working tree and writing proofs via the
# Edit tool. Everything else — network (curl/wget), interpreters (python/sh),
# git, build tools — is DENIED (not "ask"), and compound commands / command
# substitution are refused so a denied command cannot be smuggled past the
# first-word check (e.g. `cat x; curl ...` or `echo $(git show ...)`).
# WORK_ROOT is the agent's workspace, supplied by the hooks via LOOM_WORK_ROOT
# (the session cwd), falling back to the current directory. Nothing is hardcoded,
# so the experiment is self-contained and portable.
def _work_root():
    return os.environ.get("LOOM_WORK_ROOT") or os.getcwd()

_SHELL_OPERATORS = {";", "|", "&&", "||", "&", "`"}


def _is_compound(tokens):
    """True if the command chains/backgrounds or uses command substitution."""
    for t in tokens:
        if t in _SHELL_OPERATORS:
            return True
        if "$(" in t or "`" in t or "<(" in t:
            return True
    return False


def _within_work(path):
    """True if path is inside the work tree. Relative paths are resolved against
    the workspace cwd, so they are in-tree unless they escape via '..'."""
    s = str(path)
    if ".." in s.split("/"):
        return False
    if s.startswith("/"):
        wr = os.path.realpath(_work_root())
        rp = os.path.realpath(s)
        return rp == wr or rp.startswith(wr + os.sep)
    return True


def _paths_within_work(args):
    for a in args:
        if a.startswith("-"):
            continue
        if not _within_work(a):
            return False
    return True


def command_is_compound(command):
    """Quote-aware detection of shell chaining / command substitution in the RAW
    command string. Catches ';', '|', '&', '`', '$(', '<(', '>(' even when
    attached to an adjacent token (e.g. 'cat a.v;curl ...'), while NOT flagging
    these characters inside quotes (e.g. a grep regex \"P1\\|P2\"). Plain
    redirections ('>', '<') are allowed. shlex tokenisation cannot do this
    reliably, so the bash hook calls this on the raw command first."""
    quote = None
    i, n = 0, len(command)
    while i < n:
        c = command[i]
        if quote is not None:
            if quote == '"' and c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
        elif c in (";", "|", "&", "`"):
            return True
        elif c == "$" and i + 1 < n and command[i + 1] == "(":
            return True
        elif c in ("<", ">") and i + 1 < n and command[i + 1] == "(":
            return True
        i += 1
    return False


def safe_command_fn(cmd):
    tokens = [cmd.name] + list(cmd.args)

    # No chaining / substitution: forces every command through this check.
    # (Primary detection is command_is_compound() on the raw string in the hook;
    # this token-level check is a backup for substitution that survives shlex.)
    if _is_compound(tokens):
        return Deny("Run one simple command at a time: no ';', '|', '&&', '||', "
                    "subshells '( )', or command substitution '$(...)'/backticks.")

    # Build / verification / version-control tools: the harness verifies for you.
    if cmd.name in ("dune", "make", "coqc", "coqtop", "coqchk", "rocq", "git"):
        return Deny("Do not run " + cmd.name + ". The system verifies your proof "
                    "automatically. Just write the proof with the Edit tool.")

    # Read-only inspection of the working tree only.
    read_cmds = ("ls", "cat", "head", "tail", "grep", "find", "echo", "wc", "sort")
    if cmd.name in read_cmds:
        if cmd.name == "find":
            dangerous = {"-exec", "-execdir", "-ok", "-okdir", "-delete",
                         "-fprint", "-fprintf", "-fls"}
            if any(a in dangerous for a in cmd.args):
                return Deny("find -exec/-delete and similar actions are not allowed.")
        if not _paths_within_work(cmd.args):
            return Deny("You may only read files inside the project working tree.")
        return Allow

    # Cleaning compiled artifacts is fine.
    if cmd.name == "rm":
        coq_exts = (".vo", ".vok", ".vos", ".glob")
        files = [a for a in cmd.args if not a.startswith("-")]
        if files and all(f.endswith(coq_exts) for f in files):
            return Allow
        return Deny("rm only allowed on Coq compiled files")

    # Everything else (curl, wget, python, ssh, nc, ...) is DENIED. Derive the
    # proof yourself; do not fetch or reconstruct it from external sources.
    return Deny("Command '" + cmd.name + "' is not allowed. You may only inspect "
                "the project with ls/cat/grep/find/head/tail/wc and write proofs "
                "with the Edit tool. Network access, interpreters, git, and build "
                "tools are blocked.")

# --- Post-turn hook: verify the proof ---
# Uses scripts_lrust/coq_check.py, which builds the file with `make` (inside
# the lambda-rust opam switch) and, on failure, replays with coqtop to extract
# the open goal.
def verify_proof_fn(files):
    print("\n  [verify_proof: checking " + str(len(files)) + " file(s)]")

    v_files = [f for f in files if f.path.endswith(".v")]
    if not v_files:
        print("  [verify_proof: no .v files edited — agent did not use Edit tool]")
        return Err("No .v file was modified. You must use the Edit tool to write the proof.")

    for f in v_files:
        print("  [verify_proof: running coq_check.py on " + f.path + "]")

        # coq_check.py auto-detects the project root from the file path
        try:
            result = subprocess.run(
                ["python3", SCRIPTS_DIR + "/coq_check.py", f.path],
                capture_output=True, text=True, timeout=600
            )
        except subprocess.TimeoutExpired:
            print("  [verify_proof: TIMEOUT]")
            return Err("Verification timed out. A tactic may be stuck (divergent).")

        output = result.stdout.strip()
        if result.returncode != 0 or "Error:" in output:
            print("  [verify_proof: FAILED]\n" + output)
            return Err(output)

        print("  [verify_proof: OK]")
    return Ok(())


# --- Pre-hook: confine the Read tool to the working tree ---
# The original proofs may live in the opam switch, system installs, or a pristine
# checkout; deny the Read tool from reaching anything outside the working copy so
# the model must DERIVE the proof rather than copy it. No path is hardcoded — the
# work tree is WORK_ROOT (the session cwd, supplied by the hook).
def no_external_read_fn(file):
    p = str(getattr(file, "path", file))
    if not _within_work(p):
        return Deny("Reading " + p + " is denied — it is outside the project "
                    "working tree. Derive the proof from the working copy in "
                    "your cwd; do not look up the upstream/proven version.")
    return Allow
