"""Actual v8/browser lifecycle coverage retaining the original browser drivers.

Seven lifecycle cases reuse their original complete test functions unchanged.
The two inactive-credential cases reuse their complete unchanged assertions.
The active but scope-denied case explicitly verifies v8 request accounting while
preserving the no-domain-change/no-effect boundary; the original v6/v7 checks
still require whole-state equality on their original profiles.
"""

import pytest

import test_browser_accounting as accounting
import test_browser_boundaries as boundaries
import test_browser_connections as connections
import test_browser_flow as flow
import test_browser_outage as outage
from api_support import TOKEN_A, credential
from browser_support import BrowserApi
# Use the shared conftest registration: every native prerequisite still runs,
# but the expensive session verification is not re-registered in this module.
from http_service_support import (effect_counts, state,
                                 http_fixture_secret as http_fixture_secret)
from request_api_checks import counters, only_accounting_changed
from request_browser_support import RequestRegressionBrowserApi
from request_host_support import request_host_binary as request_host_binary
from test_turn_execution import programs as programs

from test_browser_accounting import (
    test_browser_midturn_accounting_stop_is_truthful_and_does_not_repeat_work as
    test_browser_midturn_accounting_stop_is_truthful_and_does_not_repeat_work)
from test_browser_boundaries import (
    test_browser_refresh_after_actual_send_then_cancel_preserves_uncertainty_and_restart as
    test_browser_refresh_after_actual_send_then_cancel_preserves_uncertainty_and_restart)
from test_browser_connections import (
    test_browser_discards_actual_previous_tenant_history_after_reconnection as
    test_browser_discards_actual_previous_tenant_history_after_reconnection)
from test_browser_flow import test_real_browser_shared_service_flow as test_real_browser_shared_service_flow
from test_browser_outage import (
    test_browser_real_service_outage_then_reopen_never_resubmits_committed_work as
    test_browser_real_service_outage_then_reopen_never_resubmits_committed_work)

pytestmark = pytest.mark.usefixtures("http_fixture_secret")


@pytest.fixture(scope="session")
def browser_service_binary(request_host_binary, browser_runtime):
    assert browser_runtime["playwright"]
    return request_host_binary


@pytest.fixture(autouse=True)
def explicit_v8_browser_deployment(monkeypatch):
    for module in (accounting, boundaries, connections, flow, outage):
        assert module.BrowserApi is BrowserApi, "unexpected test deployment substitution"
        monkeypatch.setattr(module, "BrowserApi", RequestRegressionBrowserApi)


@pytest.mark.parametrize("mode", ["unknown_credential", "expired_credential"])
def test_original_inactive_credential_browser_refusal_still_cannot_change_any_state(
        browser_service_binary, programs, scripted_llm, tmp_path, monkeypatch, mode):
    boundaries.test_browser_refusal_does_not_change_state_or_start_provider_work(
        browser_service_binary, programs, scripted_llm, tmp_path, monkeypatch, mode)


def test_actual_scope_denied_browser_submission_changes_only_request_accounting(
        browser_service_binary, programs, scripted_llm, tmp_path):
    root, workspace = tmp_path / "service", tmp_path / "workspace"
    workspace.mkdir()
    rows = [credential(scopes=["sessions:read"])]
    with RequestRegressionBrowserApi(browser_service_binary, root, programs,
                                    scripted_llm.url, workspace, rows=rows) as api:
        before = state(root)
        result = boundaries.browser(boundaries.client_config(api, TOKEN_A, "denied_submission", tmp_path))
        assert result["submissions"] == 1 and result["cancellations"] == 0
        assert result["pageErrors"] == result["foreignRequests"] == 0
        after = counters(root)
        assert set(after) == {"a.budget"}
        assert after["a.budget"][0] == 2 and after["a.budget"][1] == "tenant-a"
        only_accounting_changed(before, state(root), 2)
        assert effect_counts(root) == {}
        assert scripted_llm.requests == []
    assert (tmp_path / "screenshots/02-refusal.png").is_file()
