"""All ten original v8 browser scenarios on the actual audited service-info entry.

Original browser assertions, deadlines and native prerequisites are retained;
the accounting/cancellation drivers add explicit unknown-total assertions.
The original v6/v7/v8 tests remain in the full suite on their own deployments.
"""

import pytest

import test_browser_accounting as accounting
import test_browser_boundaries as boundaries
import test_browser_connections as connections
import test_browser_flow as flow
import test_browser_outage as outage
from api_support import TOKEN_A, credential
from browser_support import BrowserApi
from http_service_support import effect_counts, state
from readiness_host_support import readiness_host_binary as readiness_host_binary
from request_api_checks import counters, only_accounting_changed
from service_info_support import ServiceInfoRegressionBrowserApi
from test_automatic_audit import audit_secrets as audit_secrets
from test_automatic_effect_audit import effect_secrets as effect_secrets
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

pytestmark = pytest.mark.usefixtures("audit_secrets", "effect_secrets")


@pytest.fixture(scope="session")
def browser_service_binary(readiness_host_binary, browser_runtime):
    assert browser_runtime["playwright"]
    return readiness_host_binary


@pytest.fixture(autouse=True)
def explicit_service_info_browser_deployment(monkeypatch):
    for module in (accounting, boundaries, connections, flow, outage):
        assert module.BrowserApi is BrowserApi, "unexpected test deployment substitution"
        monkeypatch.setattr(module, "BrowserApi", ServiceInfoRegressionBrowserApi)


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
    with ServiceInfoRegressionBrowserApi(browser_service_binary, root, programs,
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
