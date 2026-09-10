"""Build-only test wrapper and independent fixture inputs, not API policy."""

import json

from api_support import BODY, credential
from conftest import PI_ROOT, SIGIL_ROOT
from request_rate_support import snapshot
from scripts.compose_request_policy import compose_request_policy as build_request_policy
from turn_support import record

ROOT = PI_ROOT


def compose_request_policy():
    return build_request_policy(PI_ROOT, SIGIL_ROOT)


def incoming(*, row=None, limit=2, now=150, fractional=0, observed=None,
             method="POST", path="/v1/operations", body=None):
    row = credential() if row is None else row
    return record("RP1\n", [row["facts"], str(limit), str(now), str(fractional),
        snapshot() if observed is None else observed, method, path,
        json.dumps(BODY) if body is None else body])
