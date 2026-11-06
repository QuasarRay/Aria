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
import iris_benchmark_ext

from iris_agent import workflow as _req_iris_agent




# --- Runtime helpers ---

class _File:
    def __init__(self, path, content):
        self.path = path
        self.content = content


class _Workspace:
    """A runtime working directory for invoke, produced by `open at <dir>`.

    Carries only cwd. system_prompt/max_turns/verbose default to None/None/
    False and are supplied per call as invoke overrides (typically reading
    config["..."]).
    """
    def __init__(self, cwd, system_prompt=None, max_turns=None, verbose=False):
        self.cwd = cwd
        self.system_prompt = system_prompt
        self.max_turns = max_turns
        self.verbose = verbose


class _Response:
    def __init__(self, result, session_id, stop_reason, is_error, modified_files, cost_usd):
        self.result = result
        self.session_id = session_id
        self.stop_reason = stop_reason
        self.is_error = is_error
        self.modified_files = modified_files
        self.cost_usd = cost_usd


_CONFIG_FILE = "agent.conf"
_config_cache = None


def _config_path():
    return Path(__file__).resolve().parent / _CONFIG_FILE


def _load_config():
    """Parse the agent's `agent.conf` (key = value per line), cached.

    Quoted values stay strings. Unquoted values are type-inferred:
    true/false -> bool, all-digits -> int, otherwise string.
    """
    global _config_cache
    if _config_cache is not None:
        return _config_cache
    cfg = {}
    path = _config_path()
    if path.is_file():
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k = k.strip()
            v = v.strip()
            if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                v = v[1:-1]
            elif v.lower() == "true":
                v = True
            elif v.lower() == "false":
                v = False
            else:
                try:
                    v = int(v)
                except ValueError:
                    pass
            cfg[k] = v
    _config_cache = cfg
    return cfg


def _config_get(key):
    cfg = _load_config()
    if key not in cfg:
        raise RuntimeError(f"config key {key!r} not found in {_config_path()}")
    return cfg[key]


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

def _resolve_iris_dir(d):
    """Resolve a relative iris_dir against the case_study root (dir with main.py)
    so the config can stay relative and resolve consistently from any subfolder."""
    if Path(d).is_absolute():
        return str(d)
    root = Path(__file__).resolve().parent
    while root != root.parent and not (root / "main.py").is_file():
        root = root.parent
    return str((root / d).resolve())


def benchmark(file_path):
    project_dir = _resolve_iris_dir(_config_get('iris_dir'))
    print(str(str('Extracting Admitted lemmas from ') + str(file_path)) + str('...'))
    grep_result = iris_benchmark_ext.grep_admitted(str(str(project_dir) + str('/')) + str(file_path))
    expected = iris_benchmark_ext.count_lines(grep_result)
    session = ''
    rws = _Workspace(project_dir)
    r = _invoke(
        rws.cwd,
        str(str(str(str('Read ') + str(file_path)) + str('. Here are the line numbers containing Admitted.:\\n')) + str(grep_result)) + str('\\nFor each line, find the enclosing lemma/theorem/definition name. Output ONLY the names, one per line.'),
        SETTINGS_PATH,
        system_prompt=_config_get('iris_benchmark_system_prompt'),
        max_turns=_config_get('iris_benchmark_max_turns'),
        session_id=None,
        verbose=rws.verbose,
    )
    if r.is_error:
        print(str('Failed to extract lemmas: ') + str(r.result))
    else:
        lemmas = iris_benchmark_ext.parse_lemma_list(r.result)
        got = iris_benchmark_ext.list_length(lemmas)
        if (got != expected):
            print(str(str(str(str('Count mismatch: expected ') + str(str(expected))) + str(' but got ')) + str(str(got))) + str('. Retrying...'))
            r2 = _invoke(
                rws.cwd,
                str(str(str(str(str(str(str(str('You returned ') + str(str(got))) + str(' lemma names but there are ')) + str(str(expected))) + str(' Admitted. lines. Please re-read ')) + str(file_path)) + str(' and output ALL ')) + str(str(expected))) + str(' names, one per line.'),
                SETTINGS_PATH,
                system_prompt=_config_get('iris_benchmark_system_prompt'),
                max_turns=_config_get('iris_benchmark_max_turns'),
                session_id=r.session_id,
                verbose=rws.verbose,
            )
            if (not r2.is_error):
                lemmas = iris_benchmark_ext.parse_lemma_list(r2.result)
        print('Found lemmas:')
        for lemma in lemmas:
            print(str('  - ') + str(lemma))
        print('========================================')
        count = 0
        for lemma in lemmas:
            print('========================================')
            print(str('Proving: ') + str(lemma))
            print('========================================')
            prompt = str(str(str(str('Use the Edit tool to replace the Admitted. after lemma ') + str(lemma)) + str(' in ')) + str(file_path)) + str(' with your proof ending in Qed. Do NOT just print the proof as text — you MUST use the Edit tool to modify the file. I will verify the file after you edit it.')
            new_session = _req_iris_agent.prove(prompt, session)
            session = new_session
            count = (count + 1)
            if ((count % 6) == 0):
                print('  [Resetting session after 6 lemmas]')
                session = ''
            iris_benchmark_ext.pause_check()
        print('========================================')
        print(str('File complete: ') + str(file_path))


def fix_one(prompt, session_id):
    s = _req_iris_agent.prove(prompt, session_id)
    return s

