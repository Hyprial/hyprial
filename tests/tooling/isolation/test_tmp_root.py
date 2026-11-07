from __future__ import annotations

import pytest

from tests.support.runtime._tmp import hyprial_test_tmp_root


def test_hyprial_test_tmp_root_uses_configured_root_or_local_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYPRIAL_TEST_TMP_ROOT", "/ci-tmpfs")
    assert hyprial_test_tmp_root() == "/ci-tmpfs"

    monkeypatch.delenv("HYPRIAL_TEST_TMP_ROOT")
    assert hyprial_test_tmp_root() == "/tmp"


def test_hyprial_test_tmp_root_rejects_a_root_that_cannot_fit_a_typical_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HYPRIAL_TEST_TMP_ROOT", "/" + "x" * 80)
    with pytest.raises(AssertionError, match="too long"):
        hyprial_test_tmp_root()


def test_real_daemon_short_roots_follow_the_ci_ram_disk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # These helpers start real daemons outside the autouse fixture. On CI
    # they must land on /ci-tmpfs like everything else, or the daemon's
    # startup stalls behind btrfs commits (test_profile_two_homes t11, task
    # 40404). Locally (variable unset) they keep their own short-root lookup.
    import importlib.util
    import sys
    from pathlib import Path

    from tests.daemon.home.guard import support as test_home_guard
    from tests.daemon.network import test_network_isolated
    from tests.integration.profiles.homes import test_profile_two_homes

    runner_path = (
        Path(__file__).resolve().parents[3] / 'contract/e2e/_daemon_runner.py'
    )
    sys.path.insert(0, str(runner_path.parent))
    try:
        spec = importlib.util.spec_from_file_location("_daemon_runner_probe", runner_path)
        assert spec is not None and spec.loader is not None
        runner = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(runner)
    finally:
        sys.path.remove(str(runner_path.parent))

    monkeypatch.setenv("HYPRIAL_TEST_TMP_ROOT", "/ci-tmpfs")
    assert test_home_guard._short_temp_root() == "/ci-tmpfs"
    assert test_network_isolated._short_root() == "/ci-tmpfs"
    assert test_profile_two_homes._short_temp_root() == "/ci-tmpfs"
    assert runner.short_temp_root() == Path("/ci-tmpfs")

    monkeypatch.delenv("HYPRIAL_TEST_TMP_ROOT")
    assert test_home_guard._short_temp_root() != "/ci-tmpfs"
    assert runner.short_temp_root() != Path("/ci-tmpfs")
