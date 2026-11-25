"""Say which test failed, and why, at the moment it fails.

THE HAZARD. pytest names a failed test in its closing report, and until then
shows it as one F among the progress marks. A run that is stopped first never
gets there. The full gate is such a run when it reaches the job's time limit in
.github/workflows/ci.yml: the runner kills it where it stands, and its log ends
in marks. Which tests those F were can be worked out afterwards, by counting
marks against the collection order of that commit. What they said cannot.

So each failing report is written out as it arrives, on a line of its own that
starts with MARK and the test's name, and is flushed at once: the next thing
this process does may be to wait in the test during which it is killed.
pytest's own report at the end is unchanged.

tests/conftest.py loads this for the suite. test_failed_now.py runs it the way
the gate runs pytest, and stops a run to see what it had already said.
"""
import pytest

MARK = "FAILED NOW"
ANNOUNCER = "failed_now.Announcer"


def reason(report):
    """The first line of what pytest's closing summary gives after a failed
    test's name, and nothing where pytest gives nothing."""
    longrepr = report.longrepr
    whole = longrepr if isinstance(longrepr, str) else getattr(getattr(longrepr, "reprcrash", None), "message", "")
    return (whole.splitlines() or [""])[0]


def line(nodeid, phase, seconds, reason):
    """The one line a failure leaves in the log if the run never reaches its report.

    `phase` is where it failed: "setup", "call" or "teardown". `seconds` is how
    long that phase ran. `reason` is a single line and may be empty.
    """
    detail = reason if len(reason) <= 300 else reason[:297] + "..."
    return f"{MARK} {nodeid} [{phase}, {seconds:.1f}s]" + (f" {detail}" if detail else "")


class Announcer:
    def __init__(self, terminal):
        self.terminal = terminal

    @pytest.hookimpl(trylast=True)  # after pytest has written this report's own F or E
    def pytest_runtest_logreport(self, report):
        if report.failed:
            said = line(report.nodeid, report.when, report.duration, reason(report))
            self.terminal.write(f"\n{said}\n", flush=True)


@pytest.hookimpl(trylast=True)  # after pytest has registered its terminal reporter
def pytest_configure(config):
    # The reporter is always there: `-q` in pytest.ini is its option, and without it pytest does not start.
    config.pluginmanager.register(Announcer(config.pluginmanager.get_plugin("terminalreporter")), ANNOUNCER)
