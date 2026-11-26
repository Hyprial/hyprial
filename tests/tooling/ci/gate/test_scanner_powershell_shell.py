"""The acquisition scanner reads a run step in the shell the runner would use.

A workflow may declare a shell in three places and the runner resolves them in
a fixed order: the step's ``shell``, then the job's ``defaults.run.shell``, then
the workflow's ``defaults.run.shell``; with none declared it is the runner's
default, bash on Linux and macOS and pwsh on Windows.

The scanner parses POSIX shell with ``shlex``. Applied to PowerShell that
produces nonsense argv: backslashes are eaten as escapes, ``$env:USERPROFILE``
is read as a variable named ``env``, and a pipeline of ``|``-less cmdlets is
read as one long command. So a PowerShell step is not parsed as POSIX shell.
It is not skipped silently either: the scanner is a conservative inventory,
and a download it cannot read must stay visible. The step produces one
diagnostic and no site, which is the same treatment ``eval`` and an
unresolved executable already get.

The sample command is a bare ``uv sync``: it may fetch a managed Python as
well as packages, so the scanner files it under both the UV and the PY
family. These tests are about which SHELL a step is read as, so they assert
that the UV family is present rather than the full set of families.
"""

from __future__ import annotations

import pytest
import yaml

from tests.support.ci import _ci_download_scanner as scanner

WF = ".github/workflows/fixture.yml"
POWERSHELL_DIAGNOSTIC = "PowerShell run step is not parsed as POSIX shell"


def scan(tmp_path, *jobs, defaults=None):
    document = {"jobs": {name: job for name, job in jobs}}
    if defaults:
        document["defaults"] = defaults
    return scanner.Scanner(tmp_path).scan({WF: yaml.safe_dump(document, sort_keys=False)})


def scan_one(tmp_path, job, **workflow_defaults):
    """Scan one job, optionally under a workflow ``defaults.run.shell``.

    Returns (sites, the diagnostics that say a step was not parsed).
    """
    defaults = {"run": {"shell": workflow_defaults["shell"]}} if workflow_defaults else None
    inventory = scan(tmp_path, ("build", job), defaults=defaults)
    unparsed = [d for d in inventory.diagnostics if POWERSHELL_DIAGNOSTIC in d.message]
    return inventory.sites, unparsed


DOWNLOAD = "uv sync --locked\n"
DOWNLOADED = {"UV", "PY"}


def families(sites):
    return {site.family for site in sites}


def test_an_undeclared_shell_on_a_posix_runner_is_parsed_as_posix(tmp_path):
    sites, diagnostics = scan_one(tmp_path, {"runs-on": "macos-host", "steps": [{"run": DOWNLOAD}]})
    assert DOWNLOADED <= families(sites)
    assert diagnostics == []


@pytest.mark.parametrize("shell", ["pwsh", "powershell", "pwsh -NoLogo", "powershell.exe"])
def test_a_step_level_powershell_shell_is_not_split_with_shlex(tmp_path, shell):
    sites, diagnostics = scan_one(
        tmp_path,
        {"runs-on": "windows-2025", "steps": [{"shell": shell, "run": DOWNLOAD}]},
    )
    assert sites == []
    assert len(diagnostics) == 1 and diagnostics[0].job == "build"


def test_a_job_default_shell_applies_to_every_run_step_of_the_job(tmp_path):
    job = {
        "runs-on": "windows-2025",
        "defaults": {"run": {"shell": "pwsh"}},
        "steps": [{"run": DOWNLOAD}, {"run": "npm ci"}],
    }
    sites, diagnostics = scan_one(tmp_path, job)
    assert sites == []
    assert [d.step for d in diagnostics] == ["step[0]", "step[1]"]


def test_a_workflow_default_shell_reaches_a_job_that_declares_none(tmp_path):
    job = {"runs-on": "windows-11-arm", "steps": [{"run": DOWNLOAD}]}
    sites, diagnostics = scan_one(tmp_path, job, shell="powershell")
    assert sites == []
    assert len(diagnostics) == 1


def test_the_job_default_beats_the_workflow_default(tmp_path):
    job = {
        "runs-on": "windows-2025",
        "defaults": {"run": {"shell": "bash"}},
        "steps": [{"run": DOWNLOAD}],
    }
    sites, diagnostics = scan_one(tmp_path, job, shell="pwsh")
    assert DOWNLOADED <= families(sites)
    assert diagnostics == []


def test_the_step_shell_beats_the_job_and_workflow_defaults(tmp_path):
    job = {
        "runs-on": "windows-2025",
        "defaults": {"run": {"shell": "pwsh"}},
        "steps": [{"shell": "bash", "run": DOWNLOAD}, {"run": DOWNLOAD}],
    }
    sites, diagnostics = scan_one(tmp_path, job, shell="pwsh")
    assert {site.step for site in sites} == {"step[0]"}
    assert DOWNLOADED <= families(sites)
    assert [d.step for d in diagnostics] == ["step[1]"]


def test_a_posix_step_under_a_posix_workflow_default_is_unchanged(tmp_path):
    job = {"runs-on": "ubuntu-latest", "steps": [{"run": DOWNLOAD}]}
    sites, diagnostics = scan_one(tmp_path, job, shell="bash")
    assert DOWNLOADED <= families(sites)
    assert diagnostics == []


@pytest.mark.parametrize("label", ["windows-latest", "windows-2025", "windows-11-arm"])
def test_the_runner_default_on_windows_is_powershell(tmp_path, label):
    sites, diagnostics = scan_one(tmp_path, {"runs-on": label, "steps": [{"run": DOWNLOAD}]})
    assert sites == []
    assert len(diagnostics) == 1


def test_a_powershell_step_never_yields_a_posix_site(tmp_path):
    """The Windows idioms that ``shlex`` would misread, in one script."""

    script = (
        "uv sync --locked\n"
        "if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }\n"
        'Copy-Item -Path "$env:USERPROFILE\\cache\\uv" -Destination .\\cache -Recurse\n'
        "rustup toolchain install 1.93.1 | Tee-Object -FilePath install.log\n"
        'if ($LASTEXITCODE) { throw "rustup failed" }\n'
    )
    sites, diagnostics = scan_one(
        tmp_path, {"runs-on": "windows-2025", "steps": [{"shell": "pwsh", "run": script}]}
    )
    assert sites == []
    assert len(diagnostics) == 1
    assert "not parsed" in diagnostics[0].message


def test_the_same_idioms_as_posix_would_have_been_misread(tmp_path):
    """Control for the test above: the reader is sensitive to these characters.

    This is what the PowerShell case is kept away from. Read as POSIX shell the
    script is split on the backslashes and the parentheses, which is wrong for
    PowerShell and right for nothing here.
    """

    script = (
        "uv sync --locked\n"
        'cp "$HOME\\cache\\uv" ./cache\n'
        "rustup toolchain install 1.93.1 | tee install.log\n"
    )
    sites, _ = scan_one(tmp_path, {"runs-on": "ubuntu-latest", "steps": [{"run": script}]})
    assert {site.family for site in sites} >= {"UV", "RUSTUP"}


def test_a_powershell_diagnostic_names_the_job_and_the_step(tmp_path):
    _sites, diagnostics = scan_one(
        tmp_path,
        {
            "runs-on": "windows-2025",
            "defaults": {"run": {"shell": "pwsh"}},
            "steps": [{"id": "install", "run": DOWNLOAD}],
        },
    )
    assert [(d.workflow, d.job, d.step, d.file) for d in diagnostics] == [(WF, "build", "install", WF)]


def test_one_diagnostic_per_powershell_step_across_matrix_rows(tmp_path):
    job = {
        "runs-on": "windows-2025",
        "defaults": {"run": {"shell": "pwsh"}},
        "strategy": {"matrix": {"shard": [1, 2]}},
        "steps": [{"run": DOWNLOAD}],
    }
    inventory = scan(tmp_path, ("build", job))
    powershell_rows = [d for d in inventory.diagnostics if POWERSHELL_DIAGNOSTIC in d.message]
    assert sorted(d.job for d in powershell_rows) == ["build[shard=1]", "build[shard=2]"]
    assert inventory.sites == []


def test_a_step_with_no_run_block_is_never_a_powershell_step(tmp_path):
    job = {
        "runs-on": "windows-2025",
        "defaults": {"run": {"shell": "pwsh"}},
        "steps": [{"uses": "actions/checkout@" + "a" * 40}],
    }
    inventory = scan(tmp_path, ("build", job))
    assert not any(POWERSHELL_DIAGNOSTIC in d.message for d in inventory.diagnostics)


def test_the_diagnostic_makes_the_inventory_violate_policy(tmp_path):
    """A PowerShell step is a violation on its own, so it cannot be waved through."""

    from tests.support.ci.ci_download_policy import violations

    inventory = scan(
        tmp_path,
        ("build", {"runs-on": "windows-2025", "defaults": {"run": {"shell": "pwsh"}}, "steps": [{"run": DOWNLOAD}]}),
    )
    reported = violations(
        inventory, {"version": 1, "coverage": [], "exceptions": [], "edges": [], "runner_contracts": []}
    )
    assert len(reported) == 1 and POWERSHELL_DIAGNOSTIC in reported[0]
