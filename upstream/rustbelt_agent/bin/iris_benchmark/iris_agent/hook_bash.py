#!/usr/bin/env python3
"""Generated Bash hook script."""

import json
import os
import shlex
import sys

import os as _os
_agent_root = _os.path.dirname(_os.path.abspath(__file__))
while _agent_root != _os.path.dirname(_agent_root) and not _os.path.isfile(_os.path.join(_agent_root, 'iris_agent_ext.py')):
    _agent_root = _os.path.dirname(_agent_root)
sys.path.insert(0, _agent_root)
import iris_agent_ext




class _Command:
    def __init__(self, name, args):
        self.name = name
        self.args = args


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

    # The work tree is the session cwd; expose it to iris_agent_ext so the
    # path-confinement check knows what "inside the project" means.
    cwd = hook_input.get("cwd")
    if cwd:
        os.environ["LOOM_WORK_ROOT"] = cwd

    tool_input = hook_input.get("tool_input", {})
    command = tool_input.get("command", "")

    # Quote-aware: refuse chaining / command substitution on the RAW command so a
    # denied command (curl, git, ...) cannot be smuggled past the first-word check
    # via 'cat x;curl ...', 'x && git ...', 'echo $(git ...)', etc.
    if iris_agent_ext.command_is_compound(command):
        json.dump(_deny(
            "Run one simple command at a time: no ';', '|', '&&', '||', "
            "backticks, or command substitution '$(...)'. Network/git/build "
            "tools are blocked; just read with cat/grep/find and Edit the proof."
        ), sys.stdout)
        return

    try:
        parts = shlex.split(command)
    except ValueError:
        json.dump(_deny("Could not parse command (unbalanced quotes?). "
                        "Use a single simple command."), sys.stdout)
        return

    if not parts:
        json.dump(_allow(), sys.stdout)
        return

    cmd = _Command(parts[0], parts[1:])

    # Hook: safe_command
    result = iris_agent_ext.safe_command_fn(cmd)
    response = _handle_decision(result)
    if response["hookSpecificOutput"]["permissionDecision"] != "allow":
        json.dump(response, sys.stdout)
        return


    json.dump(_allow(), sys.stdout)


if __name__ == "__main__":
    main()
