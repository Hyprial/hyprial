# Decision record (file header; no inline comments by repository rule):
# - `--actor-backend` selects reference-abi | native (spec 14/15); P8 removed
#   the pykka backend. Default reference-abi, the executable oracle. A backend
#   that cannot load fails every case (BackendLoadError), it is never skipped
#   (fail closed).
# - Known defects recorded in spec section 16 are the ONLY xfails, and they are
#   strict: an unexpected pass is red. Each entry names its defect. The
#   registry is empty: the P2b containment fix removed the SUP-09 / D9 entry
#   and P8 removed the pykka backend that carried it.
# - HOME, TMPDIR and XDG dirs point at a private directory for the whole run so
#   no case can read or write user or production state (AGENTS.md).
# - A faulthandler watchdog turns a hung case into a loud process failure; its
#   limit grows with WAIT (HYPRIAL_CONFORMANCE_WAIT_SCALE), never below 120 s.
# - Session verdict (support.verdict, HQ review): PASS / FAIL / BLOCKED is the
#   last terminal-summary section ("CONFORMANCE VERDICT: ..."), the exit
#   status is 0 only for PASS, 7 for BLOCKED, pytest's non-zero status for
#   FAIL; `--verdict-json PATH` writes the same data. A skip is only legal as
#   an explicit "BLOCKED:" report (FREE-03 native without a free-threaded
#   interpreter); any other skip fails the session.
"""pytest wiring for conformance/actor_engine."""

from __future__ import annotations

import faulthandler
import os
import sys
import tempfile

import pytest

import backends
from support.catalog import CASE_IDS
from support.helpers import WAIT, Harness
from support.verdict import VerdictPlugin

KNOWN_DEFECTS: dict[str, dict[str, str]] = {}
WATCHDOG_SECONDS = max(120.0, 12 * WAIT)


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--actor-backend", action="store", default="reference-abi",
                     choices=backends.BACKENDS, help="engine backend under test")
    parser.addoption("--free-threaded-python", action="store", default=os.environ.get(
        "HYPRIAL_CONFORMANCE_FT_PYTHON", ""), help="free-threaded interpreter for FREE-03")
    parser.addoption("--verdict-json", action="store", default="",
                     help="write the session verdict (PASS/FAIL/BLOCKED and counts) as JSON to this path")


def pytest_configure(config: pytest.Config) -> None:
    root = tempfile.mkdtemp(prefix="actor-conformance-")
    for key, sub in (("HOME", "home"), ("TMPDIR", "tmp"), ("XDG_CONFIG_HOME", "xdg-config"),
                     ("XDG_CACHE_HOME", "xdg-cache"), ("XDG_DATA_HOME", "xdg-data")):
        path = os.path.join(root, sub)
        os.makedirs(path, exist_ok=True)
        os.environ[key] = path
    tempfile.tempdir = None
    config.stash[_ROOT] = root
    config.pluginmanager.register(VerdictPlugin(config), "conformance-verdict")


_ROOT = pytest.StashKey[str]()


def pytest_report_header(config: pytest.Config) -> list[str]:
    return [f"actor-backend: {config.getoption('--actor-backend')}",
            f"python: {sys.version.split()[0]} ({sys.executable})",
            f"isolated HOME/TMPDIR root: {config.stash[_ROOT]}"]


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    defects = KNOWN_DEFECTS.get(config.getoption("--actor-backend"), {})
    for item in items:
        ids = [mark.args[0] for mark in item.iter_markers("case")]
        unknown = [case_id for case_id in ids if case_id not in CASE_IDS]
        if unknown:
            raise pytest.UsageError(f"{item.nodeid}: unknown case id(s) {unknown}")
        for case_id in ids:
            if case_id in defects:
                item.add_marker(pytest.mark.xfail(strict=True, reason=f"{case_id}: {defects[case_id]}"))


@pytest.fixture(autouse=True)
def _watchdog() -> object:
    faulthandler.dump_traceback_later(WATCHDOG_SECONDS, exit=True)
    yield
    faulthandler.cancel_dump_traceback_later()


@pytest.fixture(scope="session")
def backend_name(pytestconfig: pytest.Config) -> str:
    return pytestconfig.getoption("--actor-backend")


@pytest.fixture(scope="session")
def backend(backend_name: str) -> object:
    return backends.load(backend_name)


def _harness(backend: object, kind: str) -> object:
    harness = Harness(backend, kind=kind)
    yield harness
    harness.close(5.0)


@pytest.fixture(params=["python", "native"], ids=["py-handler", "native-handler"])
def h(request: pytest.FixtureRequest, backend: object) -> object:
    """Harness parameterized over both handler kinds (spec 4, D1)."""
    yield from _harness(backend, request.param)


@pytest.fixture
def hp(backend: object) -> object:
    """Harness for cases that need custom Python factories or handlers."""
    yield from _harness(backend, "python")


@pytest.fixture
def make_harness(backend: object) -> object:
    """Factory for extra engines (custom policies / seeds); all shut down after the case."""
    made: list[Harness] = []

    def make(**kwargs: object) -> Harness:
        harness = Harness(backend, **kwargs)
        made.append(harness)
        return harness

    yield make
    for harness in made:
        harness.close(5.0)
