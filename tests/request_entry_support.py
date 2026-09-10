"""Test convenience wrapper around the production build-only v8 recipe."""

from conftest import PI_ROOT, SIGIL_ROOT
from scripts.compose_request_entry import compose_request_entry as build_request_entry

ROOT = PI_ROOT


def compose_request_entry(limit):
    return build_request_entry(PI_ROOT, SIGIL_ROOT, request_limit=limit)
