"""Run ALL original 21 HTTP/browser cases against the explicit v7 profile.

No assertion, driver, timeout, grant or expected outcome is rewritten. The only
substitutions are the static test deployment and binary under test. Originals
remain required on their v6 profile as part of the separate main source gate.
"""
import pytest

import test_browser_accounting as accounting
import test_browser_boundaries as boundaries
import test_browser_connections as connections
import test_browser_flow as flow
import test_browser_outage as outage
import test_public_assets_http as public
from browser_support import BrowserApi
from http_service_support import (HttpBrowserApi, configure,
                                  http_service_binary as http_service_binary)
from test_turn_execution import programs as programs

# Re-export the original function objects, including their parameterization.
from test_browser_accounting import (
    test_browser_midturn_accounting_stop_is_truthful_and_does_not_repeat_work as
    test_browser_midturn_accounting_stop_is_truthful_and_does_not_repeat_work)
from test_browser_boundaries import (
    test_browser_refusal_does_not_change_state_or_start_provider_work as
    test_browser_refusal_does_not_change_state_or_start_provider_work,
    test_browser_refresh_after_actual_send_then_cancel_preserves_uncertainty_and_restart as
    test_browser_refresh_after_actual_send_then_cancel_preserves_uncertainty_and_restart)
from test_browser_connections import (
    test_browser_discards_actual_previous_tenant_history_after_reconnection as
    test_browser_discards_actual_previous_tenant_history_after_reconnection)
from test_browser_flow import test_real_browser_shared_service_flow as test_real_browser_shared_service_flow
from test_browser_outage import (
    test_browser_real_service_outage_then_reopen_never_resubmits_committed_work as
    test_browser_real_service_outage_then_reopen_never_resubmits_committed_work)
from test_public_assets_http import (
    test_public_mounts_and_sigil_api_share_host_without_reads_starting_work as
    test_public_mounts_and_sigil_api_share_host_without_reads_starting_work,
    test_browser_origin_refusal_cannot_commit_even_with_an_actual_valid_token as
    test_browser_origin_refusal_cannot_commit_even_with_an_actual_valid_token,
    test_actual_model_tool_followup_two_tenants_and_reopen_with_browser_transport as
    test_actual_model_tool_followup_two_tenants_and_reopen_with_browser_transport,
    test_bad_public_manifest_refuses_before_application_state_creation as
    test_bad_public_manifest_refuses_before_application_state_creation,
    test_frozen_older_host_refuses_opt_in_instead_of_silently_serving_an_unchecked_ui as
    test_frozen_older_host_refuses_opt_in_instead_of_silently_serving_an_unchecked_ui)


BASE_MODULES = (accounting, boundaries, connections, flow, outage, public)


@pytest.fixture(scope="session")
def browser_service_binary(http_service_binary, browser_runtime):
    # http_service_binary retains all native/evaluator gates. Missing/mismatched
    # locked browser dependencies remain a hard failure in browser_runtime.
    assert browser_runtime["playwright"]
    return http_service_binary


@pytest.fixture(autouse=True)
def explicit_v7_test_deployment(monkeypatch):
    for module in BASE_MODULES:
        assert module.BrowserApi is BrowserApi, "unexpected test deployment substitution"
        monkeypatch.setattr(module, "BrowserApi", HttpBrowserApi)
    monkeypatch.setattr(public, "configure", configure)
