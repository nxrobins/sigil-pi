"""A run that is stopped must already have said which tests failed, and why.

These need no toolchain. pytest is started the way ci.sh starts it, with this
repository's pytest.ini and quiet twice over, on a scratch test file. Its
output goes to a file, as the gate's goes to a pipe: neither is a terminal, so
nothing arrives that was not flushed.
"""
import os
import re
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from failed_now import MARK, line, reason

TESTS = Path(__file__).resolve().parent
PI_ROOT = TESTS.parent
PLUGIN = ("-p", "failed_now")

# One of each outcome, ordered so that every failure's mark ends a row of marks. The two
# expected failures are declared from inside their tests: the README's count of honest
# xfails (tests/test_guards.py) is a count of xfail decorator lines in test files, and
# would take decorators in this text for two more.
MIXED = """
    import pytest


    def test_passes():
        pass


    @pytest.mark.skip(reason="not here")
    def test_is_skipped():
        raise AssertionError("never runs")


    def test_fails_as_expected():
        pytest.xfail("known to be wrong")


    def test_reaches_a_wrong_answer():
        assert 2 + 2 == 5, "arithmetic is unwell\\nwith a second line of detail"


    @pytest.fixture
    def broken_before():
        raise RuntimeError("the fixture could not start")


    def test_never_starts(broken_before):
        pass


    @pytest.fixture
    def broken_after():
        yield
        raise RuntimeError("the fixture could not stop")


    def test_passes_then_cannot_clean_up(broken_after):
        pass


    def test_passes_against_expectation(request):
        request.applymarker(pytest.mark.xfail(reason="believed broken", strict=True))


    def test_passes_last():
        pass
"""

STOPPED = """
    import time


    def test_reaches_a_wrong_answer():
        assert 2 + 2 == 5, "arithmetic is unwell"


    def test_is_still_running_when_the_job_is_stopped():
        time.sleep(600)
"""


def start(directory, source, *options):
    """pytest as the gate starts it, on a scratch file, writing to a file."""
    directory.mkdir(exist_ok=True)
    scratch = directory / "test_scratch.py"
    scratch.write_text(textwrap.dedent(source))
    log = directory / "log"
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": os.pathsep.join(
        [str(TESTS), *filter(None, [os.environ.get("PYTHONPATH")])])}
    with log.open("w") as output:
        process = subprocess.Popen(
            [sys.executable, "-m", "pytest", "-c", str(PI_ROOT / "pytest.ini"), "--rootdir", str(directory),
             "-q", "-p", "no:cacheprovider", *options, str(scratch)],
            cwd=directory, env=environment, stdout=output, stderr=subprocess.STDOUT)
    return process, log


def said(text):
    """The lines of this kind among the complete lines of `text`."""
    return [each for each in text[:text.rfind("\n") + 1].splitlines() if each.startswith(MARK)]


def while_running(text):
    """What a run printed before pytest's closing report, which opens with a rule of `=`."""
    lines = text.splitlines()
    return lines[:next((at for at, each in enumerate(lines) if each.startswith("=")), len(lines))]


def closing_report(text):
    return text.splitlines()[len(while_running(text)):]


@pytest.fixture(scope="module")
def finished(tmp_path_factory):
    """The same scratch suite run to its end twice: with the plugin and without it."""
    logs = {}
    for name, options in (("with_plugin", PLUGIN), ("without_plugin", ())):
        process, log = start(tmp_path_factory.mktemp(name), MIXED, *options)
        assert process.wait(300) == 1, log.read_text()
        logs[name] = log.read_text()
    return SimpleNamespace(**logs)


def test_a_run_that_is_stopped_has_already_said_which_test_failed(tmp_path):
    """The gate's case. A job that reaches its time limit is killed where it
    stands, and pytest's report of what failed was still to come."""
    process, log = start(tmp_path, STOPPED, *PLUGIN)
    try:
        deadline = time.monotonic() + 60
        while not said(log.read_text()) and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        heard, still_running = said(log.read_text()), process.poll() is None
    finally:
        process.kill()
        process.wait(30)
    everything = log.read_text()
    assert heard and still_running, (
        f"the run had not named its failure while it was still going; all it wrote was {everything!r}")
    assert len(heard) == 1 and heard[0].startswith(f"{MARK} test_scratch.py::test_reaches_a_wrong_answer")
    assert said(everything) == heard and not closing_report(everything), (
        "this run was meant to be killed before pytest reported anything itself")


def test_each_failure_is_said_once_directly_after_the_mark_pytest_gave_it(finished):
    """Nothing for a test that passed, was skipped or failed as expected, and
    nothing run together with the marks on either side."""
    wrong, never_starts, cannot_clean_up, against = said(finished.with_plugin)
    *rows, last = while_running(finished.with_plugin)
    assert rows == [".sxF", wrong, "E", never_starts, ".E", cannot_clean_up, "F", against]
    assert re.fullmatch(r"\. +\[100%\]", last)
    for heard, name in ((wrong, "test_reaches_a_wrong_answer"), (never_starts, "test_never_starts"),
                        (cannot_clean_up, "test_passes_then_cannot_clean_up"),
                        (against, "test_passes_against_expectation")):
        assert heard.startswith(f"{MARK} test_scratch.py::{name}")


def test_pytests_own_report_at_the_end_is_unchanged(finished):
    [marks] = while_running(finished.without_plugin)
    assert re.fullmatch(r"\.sxFE\.EF\. +\[100%\]", marks)
    assert closing_report(finished.with_plugin) == closing_report(finished.without_plugin)
    assert any(each.startswith("FAILED test_scratch.py::test_reaches_a_wrong_answer")
               for each in closing_report(finished.with_plugin))


def test_each_line_gives_the_phase_and_the_first_line_of_the_reason(finished):
    wrong, never_starts, cannot_clean_up, against = said(finished.with_plugin)
    assert "call" in wrong and "AssertionError: arithmetic is unwell" in wrong
    assert "second line of detail" not in wrong
    assert "setup" in never_starts and "RuntimeError: the fixture could not start" in never_starts
    assert "teardown" in cannot_clean_up and "RuntimeError: the fixture could not stop" in cannot_clean_up
    assert "call" in against and "[XPASS(strict)] believed broken" in against


def test_the_reason_is_the_first_line_of_what_pytests_own_summary_gives():
    """pytest takes it from the crash entry of a failure, or from the text itself
    when a failure is only text, as a strict xfail that passed is."""
    crash = SimpleNamespace(reprcrash=SimpleNamespace(message="ValueError: the first line\nand the rest"))
    assert reason(SimpleNamespace(longrepr=crash)) == "ValueError: the first line"
    assert reason(SimpleNamespace(longrepr="[XPASS(strict)] believed broken")) == "[XPASS(strict)] believed broken"
    # Where pytest's summary has nothing to add after the name, neither does this.
    assert reason(SimpleNamespace(longrepr=SimpleNamespace(reprcrash=None))) == ""
    assert reason(SimpleNamespace(longrepr=("tests/test_x.py", 3, "a shape with no crash entry"))) == ""
    assert reason(SimpleNamespace(longrepr="")) == ""


def test_the_line_names_the_test_first_then_says_the_phase_and_the_reason():
    heard = line("tests/test_x.py::test_y[a-b]", "call", 30.21, "assert (503 == 202)")
    assert heard.startswith(f"{MARK} tests/test_x.py::test_y[a-b] ")
    assert "call" in heard and "assert (503 == 202)" in heard
    assert "[call, 30.2s]" in heard
    assert "\n" not in heard
    prefix = f"{MARK} tests/test_x.py::test_y [setup, 0.0s]"
    assert line("tests/test_x.py::test_y", "setup", 0, "") == prefix
    detail = line("tests/test_x.py::test_y", "setup", 0, "x" * 301).removeprefix(prefix + " ")
    assert len(detail) == 300 and detail.endswith("...")
