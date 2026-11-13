"""The suite's own interpreter is the first `python3` on PATH.

Tests boot release bundles, and a bundle's launcher runs `exec python3`, the
first python3 on PATH. ci.sh puts the gate's interpreter first. A run outside
ci.sh inherited the host's PATH instead, and on a host whose first python3 is
outside the supported range (3.15.1 where this was found) every bundle the
suite booted refused to start (audit 2026-11-10, found while fixing F-24).
conftest.py now puts the directory of the interpreter running the suite
first on PATH, for the session and every process it starts.
"""

import os
import subprocess
import sys
from pathlib import Path


def test_the_first_python3_on_path_is_the_interpreter_running_the_suite():
    assert os.environ["PATH"].split(os.pathsep)[0] == str(Path(sys.executable).parent)
    child = subprocess.run(
        ["python3", "-c", "import sys; print(sys.prefix); print(sys.version)"],
        capture_output=True, text=True, check=True)
    prefix, version = child.stdout.splitlines()
    assert (prefix, version) == (sys.prefix, sys.version.splitlines()[0]), (
        f"python3 on PATH is {version} at {prefix}, not the suite's {sys.version}")
