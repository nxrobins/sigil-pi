"""Verify real pytest fixture registration without running a second native job."""

import os
from pathlib import Path
import re
import subprocess
import sys

from conftest import PI_ROOT


ROOT = Path(__file__).resolve().parent.parent


def test_browser_modules_share_all_original_native_prerequisites_once():
    # The isolated stage needs the real main conftest registered as a plugin.
    # An integrated checkout uses normal conftest discovery instead.
    plugin = [] if ROOT == PI_ROOT else ["-p", "conftest"]
    modules = [ROOT / "tests/test_request_browser.py",
               ROOT / "tests/test_request_browser_regression.py"]
    paths = dict.fromkeys([str(ROOT / "tests"), str(PI_ROOT / "tests"), str(PI_ROOT),
                          *os.environ.get("PYTHONPATH", "").split(os.pathsep)])
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": os.pathsep.join(path for path in paths if path)}
    result = subprocess.run([sys.executable, "-m", "pytest", *plugin,
                             "-p", "no:cacheprovider", "--setup-plan", "-q", *map(str, modules)],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    setups = re.findall(r"(?m)^\s*SETUP\s+S\s+(\w+)\b", result.stdout)
    for name in ("native_service_binary", "native_fixed_evaluator_binary",
                 "native_release_service_binary", "native_worker_binary",
                 "native_store_binary", "browser_runtime"):
        assert setups.count(name) == 1, (name, result.stdout)
    # An empty selection must not masquerade as a prerequisite-sharing check.
    cases = [line for line in result.stdout.splitlines() if "::test_" in line]
    assert len(cases) == 14, result.stdout
