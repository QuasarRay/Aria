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
import iris_agent_ext






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
        # Restrict the agent to the minimal built-in toolset. This removes
        # WebFetch, WebSearch, Task (subagent spawning), and any other tool the
        # agent could use to fetch the PUBLIC upstream proofs. Network/git/build
        # access via Bash is separately blocked by the safe_command hook.
        "--tools", "Read,Grep,Glob,Bash,Write,Edit",
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


_ws_iris_cwd = '/path/to/iris'
_ws_iris_system_prompt = "You are a Coq proof assistant working on the Iris separation logic library. Do not use Admitted. Do not verify the proof yourself, the system will verify it automatically. When working on a specific lemma, modify ONLY that lemma's proof body. Do not edit other lemmas, definitions, imports, notations, or anything outside the target proof — even if you think they need fixing."
_ws_iris_max_turns = 100
_ws_iris_verbose = True


def _materialize_settings():
    _src = Path(__file__).resolve().parent / "settings.json"
    _txt = _src.read_text()
    if "__HOOK_DIR__" not in _txt:
        return _src
    _txt = _txt.replace("__HOOK_DIR__", str(Path(__file__).resolve().parent))
    _out = _src.parent / ".settings.runtime.json"
    _out.write_text(_txt)
    return _out


SETTINGS_PATH = _materialize_settings()


# --- Turn hook functions ---

# Turn hook: verify_proof -> iris_agent_ext.verify_proof_fn


def _run_turn_hooks(response, turn_hook_fns):
    """Run Turn hooks on modified files. Returns error message or None."""
    for fn in turn_hook_fns:
        result = fn(response.modified_files)
        if isinstance(result, tuple) and result[0] == "err":
            return result[1]
    return None


# --- Workflow ---

def prove(project_dir, prompt, session_id):
    timer = iris_agent_ext.start_timer()
    last_error = ''
    last_session = session_id
    success = False
    iris_agent_ext.check_schedule()
    r = _invoke(
        project_dir,
        prompt,
        SETTINGS_PATH,
        system_prompt=_ws_iris_system_prompt,
        max_turns=_ws_iris_max_turns,
        session_id=session_id,
        verbose=_ws_iris_verbose,
    )
    _turn_err = _run_turn_hooks(r, [iris_agent_ext.verify_proof_fn])
    if _turn_err is not None:
        r.is_error = True
        r.result = _turn_err
    if r.is_error:
        last_error = r.result
        last_session = r.session_id
    else:
        success = True
    attempt = 0
    while ((not success) and (attempt < 30)):
        attempt = (attempt + 1)
        print(str(str(str('Retry ') + str(str(attempt))) + str(': ')) + str(last_error))
        r2 = _invoke(
            project_dir,
            str(str('The proof failed. Here is the error and the goal at the failing point:\\n') + str(last_error)) + str('\\nPlease fix the proof.'),
            SETTINGS_PATH,
            system_prompt=_ws_iris_system_prompt,
            max_turns=_ws_iris_max_turns,
            session_id=last_session,
            verbose=_ws_iris_verbose,
        )
        _turn_err = _run_turn_hooks(r2, [iris_agent_ext.verify_proof_fn])
        if _turn_err is not None:
            r2.is_error = True
            r2.result = _turn_err
        if r2.is_error:
            last_error = r2.result
            last_session = r2.session_id
        else:
            success = True
    duration = iris_agent_ext.elapsed(timer)
    if success:
        print('Done')
        iris_agent_ext.record_result(prompt, project_dir, 'proved', duration, attempt)
    else:
        print('Failed after 30 retries. Adding Admitted. back...')
        r3 = _invoke(
            project_dir,
            str(str('I could not prove this lemma after many attempts. Please add a comment explaining the last error:\\n') + str(last_error)) + str('\\nThen put Admitted. back so the project still compiles.'),
            SETTINGS_PATH,
            system_prompt=_ws_iris_system_prompt,
            max_turns=_ws_iris_max_turns,
            session_id=last_session,
            verbose=_ws_iris_verbose,
        )
        iris_agent_ext.record_result(prompt, project_dir, 'failed', duration, attempt)
    return last_session

