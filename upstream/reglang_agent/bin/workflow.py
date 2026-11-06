#!/usr/bin/env python3
"""Generated CHL orchestration script."""

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import os as _os
_agent_root = _os.path.dirname(_os.path.abspath(__file__))
while _agent_root != _os.path.dirname(_agent_root) and not _os.path.isfile(_os.path.join(_agent_root, 'iris_agent_ext.py')):
    _agent_root = _os.path.dirname(_agent_root)
sys.path.insert(0, _agent_root)
import iris_project_ext

from iris_benchmark import workflow as _req_iris_benchmark




# --- Runtime helpers ---

class _File:
    def __init__(self, path, content):
        self.path = path
        self.content = content


class _Response:
    def __init__(self, result, session_id, stop_reason, is_error, modified_files, cost_usd):
        self.result = result
        self.session_id = session_id
        self.stop_reason = stop_reason
        self.is_error = is_error
        self.modified_files = modified_files
        self.cost_usd = cost_usd


def Ok(value):
    return ("ok", value)


def Err(msg):
    return ("err", msg)


def _invoke(workspace_cwd, prompt, settings_path, system_prompt=None, max_turns=None, session_id=None, verbose=False):
    """Invoke Claude Code CLI and return a _Response."""
    RETRYABLE_ERROR_PATTERNS = [
        "API Error",
        "Rate limit",
        "rate_limit",
        "overloaded",
        "Internal server error",
        "Service Unavailable",
        "Connection error",
        "out of extra usage",
        "out of usage",
        "hit your limit",
    ]

    retry_count = 0
    while True:
        response = _invoke_once(workspace_cwd, prompt, settings_path, system_prompt, max_turns, session_id, verbose)

        # Check if the error is retryable
        is_retryable = False
        if response.result:
            for pattern in RETRYABLE_ERROR_PATTERNS:
                if pattern in response.result:
                    is_retryable = True
                    break

        if not is_retryable:
            return response

        retry_count += 1
        print(f"\n  [Retryable error: {response.result}]")
        print(f"  [Retrying in 60s... (attempt {retry_count}, Ctrl+C to stop)]", flush=True)
        time.sleep(60)
        print(f"  [Retrying now...]", flush=True)


def _invoke_once(workspace_cwd, prompt, settings_path, system_prompt=None, max_turns=None, session_id=None, verbose=False):
    """Single attempt to invoke Claude Code CLI."""
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "stream-json",
        "--verbose",
        # Build-error extractor: only needs to read sources to look up lemma
        # names. Restrict the toolset so it cannot fetch anything externally.
        "--tools", "Read,Grep,Glob",
    ]

    if settings_path:
        cmd.extend(["--settings", str(settings_path)])

    if system_prompt:
        cmd.extend(["--system-prompt", system_prompt])

    if max_turns:
        cmd.extend(["--max-turns", str(max_turns)])

    if session_id:
        cmd.extend(["--resume", session_id])

    cwd = Path(workspace_cwd)
    if not cwd.is_dir():
        return _Response(
            result=f"Workspace directory not found: {workspace_cwd}",
            session_id=None,
            stop_reason="error",
            is_error=True,
            modified_files=[],
            cost_usd=0.0,
        )

    # Start Claude CLI as a child process.
    # stdout=PIPE captures the JSON stream so we can read it line by line.
    # stderr=PIPE captures error messages separately.
    # The process runs in the workspace directory (cwd).
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=workspace_cwd,
    )

    new_session_id = None
    result_text = ""
    stop_reason = ""
    is_error = False
    cost_usd = 0.0
    edited_files = []

    # Read the JSON stream line by line (JSONL format: one JSON object per line).
    # The loop blocks on each line until the CLI sends a new event.
    # It exits when the CLI closes stdout (process finished).
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Event types: "system" (init), "assistant" (text/tool calls), "result" (final)
        msg_type = event.get("type")

        # The first event is "system" with subtype "init" — capture the session_id
        # so we can resume this session later with --resume.
        if msg_type == "system" and event.get("subtype") == "init":
            new_session_id = event.get("session_id")

        # "assistant" events contain Claude's responses: text blocks and tool calls.
        if msg_type == "assistant":
            message = event.get("message", {})
            for block in message.get("content", []):
                if block.get("type") == "tool_use":
                    tool_name = block.get("name", "")
                    tool_input = block.get("input", {})
                    # Track Write/Edit file paths for the Turn hook.
                    # Only these tools modify files; Bash may too but we
                    # can't easily tell which files a command touched.
                    if tool_name in ("Write", "Edit"):
                        fp = tool_input.get("file_path", "")
                        if fp:
                            edited_files.append(fp)
                    _print_tool_use(tool_name, tool_input, verbose)
                if block.get("type") == "text":
                    print(block["text"], end="", flush=True)

        # "result" is the last event — contains the final outcome.
        # result_text is Claude's summary, is_error flags failures,
        # stop_reason is why it stopped ("end_turn", "max_turns", etc.).
        if msg_type == "result":
            new_session_id = event.get("session_id", new_session_id)
            result_text = event.get("result", "")
            stop_reason = event.get("stop_reason", "")
            is_error = event.get("is_error", False)
            cost_usd = event.get("cost_usd", 0.0)
            print()

    # Wait for the CLI process to fully exit.
    proc.wait()

    # If CLI crashed without sending a result event, read stderr
    if proc.returncode != 0 and not result_text:
        stderr_output = proc.stderr.read()
        result_text = stderr_output.strip() if stderr_output else f"CLI exited with code {proc.returncode}"
        is_error = True

    # Build the list of modified files for Turn hooks.
    # Claude CLI runs with cwd=workspace_cwd, so file paths may be
    # relative to that directory. Resolve them to absolute paths.
    modified_files = []
    for fp in sorted(set(edited_files)):
        p = Path(fp)
        if not p.is_absolute():
            p = Path(workspace_cwd) / p
        fp = str(p.resolve())
        if p.exists():
            modified_files.append(_File(fp, p.read_text()))
        else:
            modified_files.append(_File(fp, ""))

    return _Response(
        result=result_text,
        session_id=new_session_id,
        stop_reason=stop_reason,
        is_error=is_error,
        modified_files=modified_files,
        cost_usd=cost_usd,
    )


def _print_tool_use(tool_name, tool_input, verbose=False):
    if tool_name == "Write":
        print(f"\n  [Writing {tool_input.get('file_path', '')}]")
        if verbose:
            print(f"  Content:\n{tool_input.get('content', '')}")
    elif tool_name == "Edit":
        print(f"\n  [Editing {tool_input.get('file_path', '')}]")
        if verbose:
            print(f"  Old: {tool_input.get('old_string', '')}")
            print(f"  New: {tool_input.get('new_string', '')}")
    elif tool_name == "Read":
        print(f"\n  [Reading {tool_input.get('file_path', '')}]")
    elif tool_name == "Bash":
        print(f"\n  [Running: {tool_input.get('command', '')}]")
    elif tool_name == "Glob":
        print(f"\n  [Searching: {tool_input.get('pattern', '')}]")
    elif tool_name == "Grep":
        print(f"\n  [Grepping: {tool_input.get('pattern', '')}]")
    else:
        print(f"\n  [{tool_name}]")
    if verbose and tool_name not in ("Write", "Edit"):
        print(f"  Input: {json.dumps(tool_input, indent=2)}")


_ws_project_cwd = '.'
_ws_project_system_prompt = 'You are a Coq build-error analyzer. Given dune build output, you extract one record per Coq error and respond with ONLY a JSON array, no markdown, no preamble, no trailing prose. Each record has keys: file (relative path), line (integer), lemma (the enclosing Lemma/Theorem/Definition/Instance/Fact/Corollary name, or the literal string NONE if the error is not inside a proof body), error (a one-line summary of the Coq error). Use the Read tool to look up enclosing lemma names from the source file when the error context does not name it.'
_ws_project_max_turns = 30
_ws_project_verbose = False


SETTINGS_PATH = Path(__file__).parent / "settings.json"


# --- Turn hook functions ---

# No Turn hooks


def _run_turn_hooks(response, turn_hook_fns):
    """Run Turn hooks on modified files. Returns error message or None."""
    for fn in turn_hook_fns:
        result = fn(response.modified_files)
        if isinstance(result, tuple) and result[0] == "err":
            return result[1]
    return None


# --- Workflow ---

def run_project(project_dir):
    print('########################################')
    print('Phase 0: build & fix broken proofs (looped)')
    print('########################################')
    build_clean = False
    round = 0
    while ((not build_clean) and (round < 20)):
        round = (round + 1)
        print('')
        print(str(str('--- Phase 0 round ') + str(str(round))) + str(' ---'))
        build_out = iris_project_ext.dune_build_capture(project_dir)
        if (build_out == 'OK'):
            build_clean = True
        else:
            expected = iris_project_ext.count_coq_errors(build_out)
            print(str(str('Build reports ') + str(str(expected))) + str(' Coq error(s). Asking LLM to extract details (JSON)...'))
            attempt = 0
            got = (0 - 1)
            last_text = ''
            while ((got != expected) and (attempt < 3)):
                attempt = (attempt + 1)
                r = _invoke(
                    project_dir,
                    str(str(str(str('Here is the dune build output:\\n----------\\n') + str(build_out)) + str('\\n----------\\nFor each Coq Error, output one JSON object with keys file, line, lemma, error. Output a JSON array of exactly ')) + str(str(expected))) + str(' objects. No markdown, no preamble. Use Read to look up the enclosing lemma name in the source file when the error context does not name it. If an error is outside any proof body, set lemma to the literal string NONE.'),
                    SETTINGS_PATH,
                    system_prompt=_ws_project_system_prompt,
                    max_turns=_ws_project_max_turns,
                    session_id=None,
                    verbose=_ws_project_verbose,
                )
                if r.is_error:
                    print(str('LLM extraction call failed: ') + str(r.result))
                    return None
                last_text = r.result
                parsed_now = iris_project_ext.parse_extracted(r.result)
                got = iris_project_ext.list_length(parsed_now)
                if (got != expected):
                    print(str(str(str(str(str(str('Attempt ') + str(str(attempt))) + str(': got ')) + str(str(got))) + str(' entries, expected ')) + str(str(expected))) + str('. Retrying...'))
            if (got != expected):
                print('ERROR: could not extract a consistent error list after 3 attempts. Aborting Phase 0.')
                print('Raw build output:')
                print(build_out)
                return None
            entries = iris_project_ext.parse_extracted(last_text)
            has_non_proof = iris_project_ext.has_non_proof_error(entries)
            if has_non_proof:
                print('Non-proof error detected in build output. Please fix manually before re-running:')
                print(build_out)
                return None
            print(str(str('Fixing ') + str(str(expected))) + str(' broken proof(s) this round...'))
            fix_session = ''
            fixed = 0
            for entry in entries:
                file = iris_project_ext.entry_file(entry)
                lemma = iris_project_ext.entry_lemma(entry)
                err = iris_project_ext.entry_error(entry)
                print('========================================')
                print(str(str(str('Fixing: lemma ') + str(lemma)) + str(' in ')) + str(file))
                print(str('Error: ') + str(err))
                print('========================================')
                fix_prompt = str(str(str(str(str(str('The proof of lemma ') + str(lemma)) + str(' in ')) + str(file)) + str(' is failing to compile with this error:\\n')) + str(err)) + str('\\nUse the Edit tool to fix the proof. End with Qed.')
                s = _req_iris_benchmark.fix_one(project_dir, fix_prompt, fix_session)
                fix_session = s
                fixed = (fixed + 1)
                if ((fixed % 6) == 0):
                    fix_session = ''
            print(str(str(str(str('Round ') + str(str(round))) + str(' done — ')) + str(str(fixed))) + str(' fix(es). Rebuilding...'))
    if (not build_clean):
        print('Phase 0 did not converge after 20 rounds. Aborting before Phase 1.')
        return None
    print('Phase 0: project builds clean.')
    print('')
    print('########################################')
    print('Phase 1: find & prove Admitted lemmas')
    print('########################################')
    files = iris_project_ext.find_admitted_files(project_dir)
    print('Files with Admitted lemmas:')
    for file in files:
        print(str('  ') + str(file))
    print('========================================')
    for file in files:
        print('')
        print('########################################')
        print(str('Processing: ') + str(file))
        print('########################################')
        _req_iris_benchmark.benchmark(project_dir, file)
        build_ok = iris_project_ext.run_dune_build(project_dir)
        if (not build_ok):
            print(str('WARNING: dune build failed after processing ') + str(file))
    print('')
    print('########################################')
    print('Project benchmark complete')
    print('########################################')

