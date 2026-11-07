"""Whole-module and branch-profile guards for the old-interpreter upgrade tail."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .guard_support import (
    LAZY_TARGET,
    SCENARIOS,
    add_called_helper,
    copy_package,
    mutate_function,
    mutate_function_statement,
    run_profile,
    run_static_scan,
    surface_data,
)


def _payload(completed) -> dict[str, object]:  # type: ignore[no-untyped-def]
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def _surface() -> tuple[list[str], list[str]]:
    data = surface_data()
    return list(data["functions"]), list(data["modules"])  # type: ignore[arg-type]


def _function_scenarios() -> dict[str, str]:
    return dict(surface_data()["functionScenarios"])  # type: ignore[arg-type]


def test_committed_surface_is_the_true_root_profile_projection(
    tmp_path: Path,
) -> None:
    """Recompute the closure from the install swap, never from a hand list."""

    functions, modules = _surface()
    data = surface_data()
    assert data["root"] == {
        "module": "hyprial.shell.impl.cli.commands.upgrade.commands",
        "function": "_perform_upgrade",
        "swapCall": "_install_locked_requirement",
        "continuationLine": 343,
    }
    assert len(functions) == len(set(functions)) == data["functionCount"]
    assert len(modules) == len(set(modules)) == data["moduleCount"]
    assert modules == sorted({item.split(":", 1)[0] for item in functions})
    assert set(_function_scenarios()) == set(functions)
    assert set(_function_scenarios().values()) <= set(SCENARIOS)
    assert set(data["scenarios"]) == set(SCENARIOS)  # type: ignore[arg-type]
    assert len(data["primedModules"]) == data["primedModuleCount"]  # type: ignore[arg-type]

    observed: set[str] = set()
    for index, scenario in enumerate(SCENARIOS):
        payload = _payload(run_profile(copy_package(tmp_path / str(index)), scenario))
        trace = set(payload["trace"])
        assert len(trace) == data["scenarios"][scenario]  # type: ignore[index]
        observed.update(trace)
    assert observed == set(functions)
    assert {item.split(":", 1)[0] for item in observed} == set(modules)


def test_static_whole_module_import_scan_has_no_findings(tmp_path: Path) -> None:
    """M1 scans every function, not a hand-maintained executed-function list."""

    _functions, modules = _surface()
    payload = _payload(run_static_scan(copy_package(tmp_path), modules))
    assert payload["findings"] == []


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_branch_forcing_profile_stays_inside_committed_surface(
    tmp_path: Path, scenario: str
) -> None:
    """M2 runs real bodies with only process/socket/service/send seams faked."""

    functions, modules = _surface()
    payload = _payload(run_profile(copy_package(tmp_path), scenario))
    observed = set(payload["trace"])
    observed_modules = {item.split(":", 1)[0] for item in observed}
    assert payload["primed"] == surface_data()["primedModules"]
    assert payload["blockedImports"] == []
    assert set(payload["stubbedProductFunctions"]).isdisjoint(functions), (
        "an M2 scenario replaced product code inside the M1 surface: "
        f"{set(payload['stubbedProductFunctions']) & set(functions)}"
    )
    assert observed <= set(functions)
    assert observed_modules <= set(modules)
    assert len(observed) == surface_data()["scenarios"][scenario]  # type: ignore[index]
    if scenario in {"child-exited", "hard-cap", "old-daemon", "wrong-version"}:
        assert payload["failed"] is True
        # One restart-failure alert and one full-command outcome alert.
        assert payload["alertCount"] == 2
    elif scenario == "not-reconciled":
        # One unconfirmed outcome and its restore follow-up.
        assert payload["failed"] is False
        assert payload["alertCount"] == 2
    else:
        assert payload["failed"] is False
        assert payload["alertCount"] == 1


@pytest.mark.parametrize(
    "statement",
    (
        "import hyprial.daemon.impl.configuration.home_guard",
        "from hyprial.daemon.impl.configuration import home_guard",
        f'importlib.import_module("{LAZY_TARGET}")',
        f'__import__("{LAZY_TARGET}")',
        "from hyprial.daemon import live_daemon_pid",
        "import hyprial.daemon as daemon_facade\ndaemon_facade.live_daemon_pid",
    ),
    ids=("import", "from-import", "importlib", "dunder-import", "lazy-from", "lazy-attribute"),
)
def test_static_scan_covers_every_import_route(tmp_path: Path, statement: str) -> None:
    function_id = (
        "hyprial.shell.impl.cli.commands.upgrade.commands:_pending_connector_count"
    )
    package_root = copy_package(tmp_path)
    if statement.startswith("importlib."):
        statement = "import importlib\n" + statement
    mutate_function_statement(package_root, function_id, statement)
    _functions, modules = _surface()

    payload = _payload(run_static_scan(package_root, modules))

    assert any(finding["target"] == LAZY_TARGET for finding in payload["findings"])


def test_static_scan_rejects_a_nonliteral_dynamic_import(tmp_path: Path) -> None:
    function_id = (
        "hyprial.shell.impl.cli.commands.upgrade.commands:_pending_connector_count"
    )
    package_root = copy_package(tmp_path)
    mutate_function_statement(
        package_root,
        function_id,
        "import importlib\ntarget = 'hyprial.daemon.impl.configuration.home_guard'\n"
        "importlib.import_module(target)",
    )
    _functions, modules = _surface()

    payload = _payload(run_static_scan(package_root, modules))

    assert any(
        finding["function"] == function_id
        and finding["reason"] == "cannot verify dynamic import"
        for finding in payload["findings"]
    )


@pytest.mark.parametrize(
    "function_id",
    (
        "hyprial.shell.impl.cli.commands.upgrade.commands:_pending_connector_count",
        "hyprial.shell.impl.cli.commands.common.daemon_stop:_observe_restart_process",
    ),
)
def test_reviewer_mutations_fail_the_static_scan(
    tmp_path: Path, function_id: str
) -> None:
    package_root = copy_package(tmp_path)
    mutate_function(package_root, function_id)
    _functions, modules = _surface()

    payload = _payload(run_static_scan(package_root, modules))

    assert any(
        finding["function"] == function_id and finding["target"] == LAZY_TARGET
        for finding in payload["findings"]
    )


@pytest.mark.parametrize(
    ("function_id", "scenario"),
    (
        (
            "hyprial.shell.impl.cli.commands.upgrade.commands:_pending_connector_count",
            "not-reconciled",
        ),
        (
            "hyprial.shell.impl.cli.commands.common.daemon_stop:_observe_restart_process",
            "child-exited",
        ),
        (
            "hyprial.shell.impl.cli.commands.upgrade.commands:_follow_up_restore_confirmation",
            "not-reconciled",
        ),
        (
            "hyprial.daemon.impl.autoupdate.alert:notify_upgrade_outcome",
            "reconciled",
        ),
    ),
)
def test_reviewer_mutations_fail_the_fenced_branch_profile(
    tmp_path: Path, function_id: str, scenario: str
) -> None:
    package_root = copy_package(tmp_path)
    mutate_function(package_root, function_id)

    completed = run_profile(package_root, scenario)

    assert completed.returncode != 0
    assert "post-install import fence blocked attempts" in completed.stderr
    assert LAZY_TARGET in completed.stderr


@pytest.mark.parametrize("function_id", _surface()[0])
def test_every_profiled_function_rejects_a_new_lazy_facade_import(
    tmp_path: Path, function_id: str
) -> None:
    """Mutation table: each committed function is RED under M1."""

    package_root = copy_package(tmp_path)
    mutate_function(package_root, function_id)
    _functions, modules = _surface()

    payload = _payload(run_static_scan(package_root, modules))

    assert any(
        finding["function"] == function_id and finding["target"] == LAZY_TARGET
        for finding in payload["findings"]
    )


@pytest.mark.parametrize(("function_id", "scenario"), _function_scenarios().items())
def test_every_profiled_function_rejects_a_new_lazy_import_under_m2(
    tmp_path: Path, function_id: str, scenario: str
) -> None:
    """Mutation table: each reached function is RED in a reaching M2 scenario."""

    package_root = copy_package(tmp_path)
    mutate_function(package_root, function_id)

    completed = run_profile(package_root, scenario)

    assert completed.returncode != 0
    if function_id == "hyprial.daemon.interface:__getattr__":
        # The exact facade import recursively re-enters the facade before it
        # can resolve its provider. It is RED under M2 and M1, but no provider
        # module reaches meta_path for the side channel to record.
        assert "cannot import name 'live_daemon_pid'" in completed.stderr
        assert "partially initialized module 'hyprial.daemon'" in completed.stderr
    else:
        assert "post-install import fence blocked attempts" in completed.stderr
        assert LAZY_TARGET in completed.stderr


@pytest.mark.parametrize(
    "function_id",
    (
        "hyprial.shell.impl.gui.apps:upgrade",
        "hyprial.shell.impl.cli.commands.upgrade.commands:_unpack_paired_gui",
        "hyprial.shell.impl.gui.apps:_unavailable",
        "hyprial.daemon.impl.upgrade.runner:run_upgrade_steps",
        "hyprial.shell.impl.cli.output.errors:json_failure",
    ),
    ids=(
        "paired-gui",
        "gui-retirement-result",
        "gui-unavailable",
        "upgrade-steps",
        "error-rendering",
    ),
)
def test_true_root_mutations_fail_the_static_scan(
    tmp_path: Path, function_id: str
) -> None:
    """The guard begins at the install swap, not inside the restart helper."""

    package_root = copy_package(tmp_path)
    mutate_function(package_root, function_id)
    _functions, modules = _surface()

    payload = _payload(run_static_scan(package_root, modules))

    assert any(
        finding["function"] == function_id and finding["target"] == LAZY_TARGET
        for finding in payload["findings"]
    )


@pytest.mark.parametrize(
    ("function_id", "scenario"),
    (
        ("hyprial.shell.impl.gui.apps:upgrade", "gui-present"),
        ("hyprial.shell.impl.cli.commands.upgrade.commands:_unpack_paired_gui", "gui-present"),
        ("hyprial.shell.impl.gui.apps:_unavailable", "gui-present"),
        ("hyprial.daemon.impl.upgrade.runner:run_upgrade_steps", "steps-pending"),
        ("hyprial.shell.impl.cli.output.errors:json_failure", "child-exited"),
    ),
    ids=(
        "paired-gui",
        "gui-retirement-result",
        "gui-unavailable",
        "upgrade-steps",
        "error-rendering",
    ),
)
def test_true_root_mutations_fail_the_fenced_profile(
    tmp_path: Path, function_id: str, scenario: str
) -> None:
    package_root = copy_package(tmp_path)
    mutate_function(package_root, function_id)

    completed = run_profile(package_root, scenario)

    assert completed.returncode != 0
    assert "post-install import fence blocked attempts" in completed.stderr
    assert LAZY_TARGET in completed.stderr


def test_profile_rejects_a_new_called_helper_until_data_is_updated(
    tmp_path: Path,
) -> None:
    package_root = copy_package(tmp_path)
    helper = add_called_helper(package_root)
    functions, _modules = _surface()

    payload = _payload(run_profile(package_root, "reconciled"))

    assert helper in set(payload["trace"]) - set(functions)


def test_stale_old_package_runs_restart_alert_and_real_renderer(
    tmp_path: Path,
) -> None:
    """B3 swaps files after OLD imports, then runs restart, alert, and render."""

    old_root = copy_package(tmp_path / "old")
    new_root = copy_package(tmp_path / "new")
    future_name = "POST_INSTALL_NEW_KERNEL_NAME"
    new_kernel = new_root / "hyprial" / "kernel" / "__init__.py"
    new_kernel.write_text(
        new_kernel.read_text(encoding="utf-8")
        + f"\n{future_name} = 'new-files-only'\n",
        encoding="utf-8",
    )
    new_sdk = (
        new_root
        / "hyprial"
        / "daemon"
        / "impl"
        / "adapters"
        / "lark"
        / "outbound"
        / "sdk.py"
    )
    new_sdk.write_text(
        new_sdk.read_text(encoding="utf-8")
        + f"\nfrom hyprial.kernel import {future_name}\n",
        encoding="utf-8",
    )

    completed = run_profile(
        old_root,
        "child-exited",
        mode="stale",
        new_package_root=new_root,
    )
    if completed.returncode != 0:
        expected = (
            "cannot import name 'POST_INSTALL_NEW_KERNEL_NAME' "
            "from 'hyprial.kernel'"
        )
        assert "ImportError" in completed.stderr and expected in completed.stderr, (
            completed.stderr
        )
        pytest.fail(f"old modules imported new files after the swap: {expected}")
    payload = _payload(completed)

    assert payload["alertCount"] >= 1
    assert payload["failed"] is True
    assert '"resolvedTag":"v0.4.0.dev2"' in payload["rendered"]
