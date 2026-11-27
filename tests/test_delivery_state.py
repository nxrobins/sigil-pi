"""Run the real shared SIGIL delivery-state kernel through the product forge.

These are component tests, not proof of authenticated dispatch or durability.
The table is an independent oracle; production decision code stays in SIGIL.
"""

import pytest

from conftest import PI_ROOT, forge_err, forge_ok


SOURCE = (PI_ROOT / "app/shared/delivery_state.sigil").read_text()
# Columns: claim, response, definitely-unsent, lost-owner, cancel, expiry.
# None is a rejected transition, not a successful terminal state.
EXPECTED = (
    (1, None, None, 0, 5, 6),
    (None, 2, 3, 4, 4, 4),
    (2, 2, 2, 2, 2, 2),
    (3, 3, 3, 3, 3, 3),
    (4, 4, 4, 4, 4, 4),
    (5, 5, 5, 5, 5, 5),
    (6, 6, 6, 6, 6, 6),
)


@pytest.mark.parametrize("phase,event,expected", [
    (phase, event, expected)
    for phase, row in enumerate(EXPECTED)
    for event, expected in enumerate(row)
])
def test_delivery_transition_matrix(mcp, phase, event, expected):
    payload = f"{phase}{event}"
    if expected is None:
        assert "409" in forge_err(mcp, SOURCE, payload)
    else:
        assert forge_ok(mcp, SOURCE, payload) == str(expected)


@pytest.mark.parametrize("payload", ["", "0", "000", "70", "06", "-1",
                                      "a0", "0a", "é", "00\n"])
def test_delivery_encoding_is_strict(mcp, payload):
    assert "400" in forge_err(mcp, SOURCE, payload)


def test_delivery_kernel_has_no_effect_authority():
    # Guard the intentionally pure boundary; actual forgings use zero grants.
    assert "AUTHORSHIP: hand-authored" in SOURCE.split("module ", 1)[0]
    code = "\n".join(line.split("//", 1)[0] for line in SOURCE.splitlines())
    assert 'extern "C"' not in code
    assert "use sigil::" not in code
    assert "#[trusted]" not in code
    assert "! { Alloc }" in code
