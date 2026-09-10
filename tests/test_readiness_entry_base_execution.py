"""Run every existing v8 entry transition fixture against the reduced base.

This tests unchanged entry behavior, not a readiness endpoint or real receipts.
Source/provenance assertions for the new recipe live in test_sigil_omit.py.
"""

import pytest

from conftest import PI_ROOT, SIGIL_ROOT, needs_toolchain
from scripts.readiness_entry_base import prepare_entry_base
from test_request_entry import (  # noqa: F401
    test_real_bootstrap_retains_profile_validation_and_never_counts_as_a_request,
    test_unmatched_or_inactive_credentials_reply_without_any_accounting_read,
    test_all_active_requests_read_the_bound_quota_before_route_or_body_handling,
    test_policy_call_receives_original_request_and_actual_observation_with_same_sample_fraction,
    test_policy_proposal_must_be_committed_before_domain_work_or_deferred_error,
    test_malformed_receipts_cannot_unlock_the_held_body,
    test_exhausted_rate_returns_retry_metadata_without_any_write_or_body_error,
)


@pytest.fixture(scope="module")
def entry():
    needs_toolchain()
    return prepare_entry_base(PI_ROOT, SIGIL_ROOT, request_limit=2).text
