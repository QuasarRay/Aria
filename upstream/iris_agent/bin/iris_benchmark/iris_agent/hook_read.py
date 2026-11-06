#!/usr/bin/env python3
"""Read hook: confine the Read tool to the working tree so the agent cannot read
the original Iris proofs from opam/system installs or a pristine checkout."""

import json
import os
import sys

import os as _os
_agent_root = _os.path.dirname(_os.path.abspath(__file__))
while _agent_root != _os.path.dirname(_agent_root) and not _os.path.isfile(_os.path.join(_agent_root, 'iris_agent_ext.py')):
    _agent_root = _os.path.dirname(_agent_root)
sys.path.insert(0, _agent_root)
import iris_agent_ext


class _File:
    def __init__(self, path):
        self.path = path


Allow = "allow"


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


def _handle_decision(result):
    if result == Allow or result is None:
        return _allow()
    if isinstance(result, tuple) and len(result) == 2 and result[0] == "deny":
        return _deny(result[1])
    return _allow()


def main():
    hook_input = json.loads(sys.stdin.read())

    # The work tree is the session cwd; expose it to iris_agent_ext.
    cwd = hook_input.get("cwd")
    if cwd:
        os.environ["LOOM_WORK_ROOT"] = cwd

    tool_input = hook_input.get("tool_input", {})
    file_path = tool_input.get("file_path", "")
    if not file_path:
        json.dump(_allow(), sys.stdout)
        return

    result = iris_agent_ext.no_external_read_fn(_File(file_path))
    json.dump(_handle_decision(result), sys.stdout)


if __name__ == "__main__":
    main()
