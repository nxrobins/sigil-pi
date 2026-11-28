"""Run the COMPLETE existing AV1/AP2 admission suite against the AV2 artifact.

Only the fixed SIGIL program fixture changes. Original cases, expected values,
maximum payloads and native transactional prerequisites remain authoritative.
"""

import inspect

import pytest

import test_admission as original
from conftest import PI_ROOT, SIGIL_ROOT, needs_toolchain
from scripts.compose_bootstrap_admission import compose_bootstrap_admission


for _name, _function in inspect.getmembers(original, inspect.isfunction):
    if _name.startswith("test_"):
        globals()[_name] = _function
append_probe = original.append_probe


@pytest.fixture(scope="module")
def program():
    needs_toolchain()
    return compose_bootstrap_admission(PI_ROOT, SIGIL_ROOT).text


def test_original_case_inventory_is_retained():
    expected = {name for name, value in vars(original).items() if name.startswith("test_") and callable(value)}
    actual = {name for name in globals() if name.startswith("test_")} - {"test_original_case_inventory_is_retained"}
    assert actual == expected and len(expected) == 19
