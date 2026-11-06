#!/usr/bin/env python3
"""Generated Write/Edit hook script."""

import json
import sys
from pathlib import Path

import os as _os
_agent_root = _os.path.dirname(_os.path.abspath(__file__))
while _agent_root != _os.path.dirname(_agent_root) and not _os.path.isfile(_os.path.join(_agent_root, 'iris_agent_ext.py')):
    _agent_root = _os.path.dirname(_agent_root)
sys.path.insert(0, _agent_root)
import iris_agent_ext




class _File:
    def __init__(self, path, content):
        self.path = path
        self.content = content


Allow = "allow"


def Deny(reason):
    return ("deny", reason)


def Ask(question):
    return ("ask", question)


def _allow():
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
        }
    }


def _deny(reason):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def _ask(question):
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": question,
        }
    }


def _handle_decision(result):
    """Convert a hook function return value to a hook response."""
    if result == Allow or result is None:
        return _allow()
    if isinstance(result, tuple) and len(result) == 2:
        kind, reason = result
        if kind == "deny":
            return _deny(reason)
        if kind == "ask":
            return _ask(reason)
    return _allow()


def main():
    hook_input = json.loads(sys.stdin.read())
    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})
    file_path = tool_input.get("file_path", "")

    # Compute file content and original
    path = Path(file_path)
    original = path.read_text() if path.exists() else ""

    if tool_name == "Write":
        content = tool_input.get("content", "")
    elif tool_name == "Edit":
        old_string = tool_input.get("old_string", "")
        new_string = tool_input.get("new_string", "")
        if old_string in original:
            content = original.replace(old_string, new_string, 1)
        else:
            content = original
    else:
        json.dump(_allow(), sys.stdout)
        return

    file = _File(file_path, content)

    # Hook: no_admitted
    result = iris_agent_ext.no_admitted_fn(file, original)
    response = _handle_decision(result)
    if response["hookSpecificOutput"]["permissionDecision"] != "allow":
        json.dump(response, sys.stdout)
        return

    # Hook: lint
    result = iris_agent_ext.lint_fn(file, original)
    response = _handle_decision(result)
    if response["hookSpecificOutput"]["permissionDecision"] != "allow":
        json.dump(response, sys.stdout)
        return


    json.dump(_allow(), sys.stdout)


if __name__ == "__main__":
    main()
