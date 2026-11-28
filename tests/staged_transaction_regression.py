"""Run unchanged legacy transaction suites against the staged actual binaries.

The staged readiness fixture retains all original native/evaluator gates. No
expectation, timeout, source fixture or prerequisite is replaced here.
"""
import pytest

from audited_transaction_support import audited_native as audited_native
from readiness_host_support import readiness_host_binary as readiness_host_binary


@pytest.fixture(scope="session")
def native_transaction_binary(audited_native):
    return audited_native[0]


@pytest.fixture(scope="session")
def native_store_binary(audited_native):
    return audited_native[1]


@pytest.fixture(scope="session")
def native_service_binary(readiness_host_binary):
    return readiness_host_binary


@pytest.fixture(scope="session")
def native_claimed_worker_binary(readiness_host_binary):
    # The same full native/evaluator gates and optimized build include this bin.
    return readiness_host_binary.with_name("sigil-claimed-worker")
