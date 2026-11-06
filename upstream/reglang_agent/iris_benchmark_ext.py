import re
import sys
import json
from pathlib import Path


def grep_admitted(file_path):
    """Grep for Admitted. with line numbers. Returns the grep output string."""
    import subprocess
    result = subprocess.run(
        ["grep", "-n", "Admitted\\.", file_path],
        capture_output=True, text=True
    )
    return result.stdout.strip()


def count_lines(text):
    """Count non-empty lines in a string."""
    if not text:
        return 0
    return len(text.strip().split("\n"))


def parse_lemma_list(result_text):
    """Parse LLM response to extract lemma names."""
    for line in result_text.splitlines():
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    lemmas = []
    for line in result_text.splitlines():
        name = line.strip().strip("-").strip("*").strip("`").strip()
        if name and re.match(r"^[^\W\d][\w']*$", name):
            lemmas.append(name)
    return lemmas


def list_length(lst):
    """Return the length of a list."""
    return len(lst)

def pause_check():
    """Give user 2 seconds to press Enter to pause. Returns True if paused."""
    import select
    print("  [Press Enter within 2s to pause, or wait to continue...]", flush=True)
    ready, _, _ = select.select([sys.stdin], [], [], 2.0)
    if ready:
        sys.stdin.readline()
        input("  [Paused. Press Enter to resume...]")
        return True
    return False
