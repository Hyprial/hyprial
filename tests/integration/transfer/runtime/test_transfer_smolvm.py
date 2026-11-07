"""Adapter/CLI contract tests use a fake executable; real VM is opt-in below."""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hyprial.daemon.impl.transfer.execution import smolvm as smolvm
from hyprial.shell.impl.cli.commands.common.root import app as _cli_root_app
import hyprial.shell.impl.cli.runtime.entry  # noqa: F401  (installs the explicit CLI composition)


@pytest.fixture
def material(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(smolvm, "_SYSTEM_E2FS_DIRS", ())
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "agent-rootfs").mkdir()
    (bundle / "agent-rootfs/seed").write_text("immutable seed")
    binary = bundle / "smolvm"
    rootfs = tmp_path / "rootfs"
    (rootfs / "bin").mkdir(parents=True)
    (rootfs / "bin/sh").write_text("fake rootfs, not a VM")
    script = r"""
import json, os, pathlib, sys
a = sys.argv[1:]
here = pathlib.Path.cwd()
mode = pathlib.Path(__file__).with_suffix('.mode').read_text()
# Apple's Python launcher adds its own text-encoding hint after exec.
assert set(os.environ) <= {'PATH', 'LANG', 'LC_ALL', 'HOME', 'XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_CACHE_HOME', 'XDG_RUNTIME_DIR', 'TMPDIR', '__CF_USER_TEXT_ENCODING', 'SMOLVM_AGENT_ROOTFS'}
assert pathlib.Path(os.environ['HOME']).is_relative_to(here / 'engine')
assert not (here / 'Smolfile').exists()
assert pathlib.Path(os.environ['SMOLVM_AGENT_ROOTFS']).is_relative_to(here / 'engine')
assert (pathlib.Path(os.environ['PATH'].split(':')[0]) / 'resize2fs').exists()
if a == ['--version']:
    print('smolvm 1.18.0' if mode == 'version' else 'smolvm 1.19.0')
elif a[:2] == ['machine', 'run']:
    (pathlib.Path(os.environ['SMOLVM_AGENT_ROOTFS']) / 'seed').write_text('private marker')
    assert a[a.index('--cpus')+1] == '2'
    assert a[a.index('--mem')+1] == '1024'
    assert a[a.index('--storage')+1] == '1'
    assert a[a.index('--overlay')+1] == '1'
    assert '--net' not in a and '--port' not in a and '--ssh-agent' not in a
    mount = a[a.index('--volume')+1]
    assert mount.endswith(':/hyprial/agent:rw')
    fixture = pathlib.Path(mount.split(':')[0])
    assert fixture.is_relative_to(here)
    assert (fixture / 'config/config.txt').read_text() == 'synthetic-config\n'
    assert (fixture / 'state/session.txt').read_text() == 'synthetic-session\n'
    assert (fixture / 'workspace/input.txt').read_text() == 'synthetic-workspace\n'
    if mode == 'exit':
        print('guest failed', file=sys.stderr)
        sys.exit(7)
    if mode == 'live-pid':
        (here / 'engine/agent.pid').write_text(str(os.getppid()))
    if mode == 'rootfs':
        (pathlib.Path(a[a.index('--image')+1]) / 'bin/sh').write_text('changed')
    w = fixture / 'workspace'
    if mode == 'symlink-output':
        (w / 'output.txt').symlink_to(here / 'unmounted-host-canary')
    elif mode != 'missing-output':
        (w / 'output.txt').write_text('mapped-output\n')
        (w / 'output.txt').chmod(0o600)
    (w / 'system.txt').write_text('Darwin\n' if mode == 'not-linux' else 'Linux\n')
    (w / 'arch.txt').write_text('aarch64\n')
    (w / 'host-link').symlink_to(a[-1])
    print('incomplete' if mode == 'missing-marker' else 'HYPRIAL_MAPPING_OK')
    if mode == 'exit-late':
        sys.exit(7)
elif a[:2] == ['machine', 'ls']:
    if mode == 'cleanup':
        sys.exit(3)
    if mode == 'leftover' and not (here / 'deleted').exists():
        print('probe-owned')
elif a[:2] == ['machine', 'delete']:
    assert a[2:] == ['--name', 'probe-owned', '--force']
    (here / 'deleted').touch()
else:
    raise AssertionError(a)
"""
    binary.write_text(f"#!{sys.executable}\n" + script)
    binary.chmod(0o700)
    binary.with_suffix(".mode").write_text("pass")
    with tempfile.TemporaryDirectory(prefix="at-", dir="/tmp") as short:
        yield binary, rootfs, Path(short).resolve() / "out"


def invoke(material):
    binary, rootfs, output = material
    return CliRunner().invoke(
        _cli_root_app,
        [
            "agent", "transfer", "runtime",
            "probe",
            "--smolvm",
            str(binary),
            "--resize2fs",
            str(binary),
            "--rootfs",
            str(rootfs),
            "--output",
            str(output),
            "--json",
        ],
    )


def test_cli_isolated_environment_writeback_and_truthful_scope(material, monkeypatch):
    for name in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "SSH_AUTH_SOCK",
        "HYPRIAL_PROBE_HOST_SECRET",
    ):
        monkeypatch.setenv(name, "synthetic-not-inherited")
    result = invoke(material)
    assert result.exit_code == 0, result.stdout
    report = json.loads(result.stdout)
    assert report["ok"]
    assert report["checks"] == {
        "mapping": "PASS",
        "source_rootfs": "PASS",
        "runtime_bundle": "PASS",
        "host_canaries": "PASS",
        "worker": "NOT_RUN",
        "sandbox": "NOT_RUN",
        "authentication": "NOT_RUN",
        "cross_machine": "NOT_RUN",
    }
    assert report["guest_symlink_read"] == "REJECTED"
    assert report["host_writeback"]["mode"] & 0o777 == 0o600
    assert report["host_writeback"]["uid"] == os.getuid()
    assert report["cleanup"]["ok"] and report["cleanup"]["residuals"] == []
    output = material[2]
    assert output.stat().st_mode & 0o777 == 0o700
    assert not (output / "engine").exists()
    assert (output / "guest.json").exists()
    for path in output.glob("*.json"):
        assert "synthetic-not-inherited" not in path.read_text()
        assert "synthetic-host-only-canary" not in path.read_text()


@pytest.mark.parametrize(
    "mode",
    [
        "version",
        "exit",
        "exit-late",
        "missing-marker",
        "missing-output",
        "not-linux",
        "symlink-output",
        "rootfs",
        "cleanup",
        "live-pid",
    ],
)
def test_cli_failures_never_report_success(material, mode):
    material[0].with_suffix(".mode").write_text(mode)
    result = invoke(material)
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["code"] == "SMOLVM_PROBE_FAILED"
    report = json.loads((material[2] / "report.json").read_text())
    assert not report["ok"] and report["errors"]
    assert "synthetic-host-only-canary" not in result.stdout
    if mode == "rootfs":
        assert report["checks"]["source_rootfs"] == "FAIL"
    if mode in ("cleanup", "live-pid"):
        assert report["cleanup"]["residuals"]
        assert (material[2] / "engine").exists()
    else:
        assert report["cleanup"]["ok"]
    if mode == "exit":
        assert report["execution"]["exit_code"] == 7
        assert (material[2] / "guest.stderr").read_text() == "guest failed\n"


def test_cleanup_deletes_only_private_listed_machine(material):
    material[0].with_suffix(".mode").write_text("leftover")
    result = invoke(material)
    assert result.exit_code == 0, result.stdout
    assert (material[2] / "deleted").exists()
    report = json.loads(result.stdout)
    assert report["cleanup"]["commands"][1]["argv"][-5:] == [
        "machine",
        "delete",
        "--name",
        "probe-owned",
        "--force",
    ]


def test_guest_absolute_shell_link_is_not_resolved_against_host(material):
    shell = material[1] / "bin/sh"
    shell.unlink()
    shell.symlink_to("/bin/guest-only-busybox")
    (material[1] / "bin/guest-only-busybox").write_text("guest shell")
    result = invoke(material)
    assert result.exit_code == 0, result.stdout


def test_long_socket_path_rejected_before_spawn(material, tmp_path, monkeypatch):
    monkeypatch.setattr(
        smolvm.subprocess,
        "Popen",
        lambda *a, **kw: pytest.fail("spawn before path check"),
    )
    parent = tmp_path / ("x" * 100)
    parent.mkdir()
    with pytest.raises(smolvm.SmolvmProbeError, match="short output path"):
        smolvm.probe_smolvm(
            binary=material[0],
            rootfs=material[1],
            output=parent / "output",
            resize2fs=material[0],
        )


def test_missing_disk_tool_rejected_before_output_creation(material):
    with pytest.raises(smolvm.SmolvmProbeError, match="resize2fs"):
        smolvm.probe_smolvm(
            binary=material[0],
            rootfs=material[1],
            output=material[2],
            resize2fs=material[2] / "absent",
        )
    assert not material[2].exists()


def test_explicit_tool_shadowing_is_rejected(material, tmp_path, monkeypatch):
    system = tmp_path / "system-tools"
    system.mkdir()
    (system / "resize2fs").write_text("different runtime tool")
    monkeypatch.setattr(smolvm, "_SYSTEM_E2FS_DIRS", (str(system),))
    with pytest.raises(smolvm.SmolvmProbeError, match="would use"):
        smolvm.probe_smolvm(
            binary=material[0],
            rootfs=material[1],
            output=material[2],
            resize2fs=material[0],
        )


def test_parent_alias_cannot_hide_mount_delimiter(material, monkeypatch):
    parent = material[2].parent
    target = parent / "bad:mount"
    target.mkdir()
    alias = parent / "alias"
    alias.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(
        smolvm.subprocess,
        "Popen",
        lambda *a, **kw: pytest.fail("spawn before validation"),
    )
    with pytest.raises(smolvm.SmolvmProbeError, match="Resolved paths"):
        smolvm.probe_smolvm(
            binary=material[0],
            rootfs=material[1],
            output=alias / "out",
            resize2fs=material[0],
        )
    assert list(target.iterdir()) == []


@pytest.mark.parametrize(
    "kind",
    [
        "existing",
        "symlink",
        "missing-binary",
        "missing-rootfs",
        "relative",
        "delimiter",
    ],
)
def test_invalid_inputs_do_not_spawn_or_touch_existing_data(
    material, monkeypatch, kind
):
    binary, rootfs, output = material
    sentinel = output.parent / "existing-data"
    sentinel.write_text("keep")
    if kind == "existing":
        output.mkdir()
        (output / "keep").write_text("keep")
    elif kind == "symlink":
        output.symlink_to(sentinel)
    elif kind == "missing-binary":
        binary = binary.with_name("absent")
    elif kind == "missing-rootfs":
        rootfs = rootfs.with_name("absent")
    elif kind == "relative":
        output = Path("relative")
    else:
        output = output.with_name("bad:mount")
    monkeypatch.setattr(
        smolvm.subprocess,
        "Popen",
        lambda *a, **kw: pytest.fail("spawn before validation"),
    )
    with pytest.raises(smolvm.SmolvmProbeError):
        smolvm.probe_smolvm(
            binary=binary, rootfs=rootfs, output=output, resize2fs=material[0]
        )
    assert sentinel.read_text() == "keep"
    if kind == "existing":
        assert (output / "keep").read_text() == "keep"


def test_nofollow_rejects_symlink_file_and_parent(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("DO_NOT_READ")
    link = tmp_path / "link"
    link.symlink_to(secret)
    parent = tmp_path / "parent"
    parent.symlink_to(tmp_path, target_is_directory=True)
    for path in (link, parent / "secret"):
        with pytest.raises(OSError):
            smolvm._regular_read(path)
    snap = smolvm._snapshot(link)
    assert snap["type"] == "symlink" and "sha256" not in snap
    assert "DO_NOT_READ" not in json.dumps(snap)


def test_runner_timeout_reaps_process_and_records_raw_output(tmp_path):
    env = smolvm._private_environment(tmp_path)
    result = smolvm._run(
        [
            sys.executable,
            "-c",
            "import time; print('began', flush=True); time.sleep(60)",
        ],
        tmp_path,
        "hang",
        env,
        0.2,
    )
    assert result["timeout"] and result["exit_code"] < 0
    assert not smolvm._succeeded(result)
    assert (tmp_path / "hang.stdout").read_text() == "began\n"
    assert json.loads((tmp_path / "hang.json").read_text()) == result


def test_runtime_writes_outside_private_home_fail_probe(material, monkeypatch):
    snapshots = iter(({"host": "before"}, {"host": "after"}))
    monkeypatch.setattr(smolvm, "_host_canaries", lambda: next(snapshots))
    result = invoke(material)
    assert result.exit_code == 1
    assert json.loads(result.stdout)["data"]["checks"]["host_canaries"] == "FAIL"


def test_interrupt_in_prelude_still_leaves_a_report(material, monkeypatch):
    """An owned SIGINT before the VM phase must leave a report, not an empty dir."""
    binary, rootfs, output = material
    real = smolvm._snapshot
    fired: list[int] = []

    def snapshot(path):
        if Path(path) == binary.parent and not fired:
            fired.append(1)
            os.kill(os.getpid(), signal.SIGINT)
        return real(path)

    monkeypatch.setattr(smolvm, "_snapshot", snapshot)
    with pytest.raises(KeyboardInterrupt):
        smolvm.probe_smolvm(binary=binary, rootfs=rootfs, output=output, resize2fs=binary)
    report = json.loads((output / "report.json").read_text())
    assert report["interrupted"] is True
    assert report["interrupt_signal"] == "SIGINT"
    assert report["phase"] == "setup"
    assert report["checks"]["mapping"] == "NOT_RUN"
    assert report["ok"] is False
    assert not (output / "engine").exists()
    # Review 2026-09-29 (cc-sw-review): the old line asserted
    # ``(output / "errors" in report) is False``, which can never fail because a
    # Path is never a key of a dict.  Check the recorded evidence instead.
    assert report["errors"] == [
        "Interrupted by SIGINT; the evidence for this run was written before exit"
    ]


def test_interrupt_during_the_guest_run_is_recorded_in_the_report(
    material, monkeypatch
):
    """A SIGINT during the guest run must set ``interrupted`` as well.

    Review 2026-09-29 (cc-sw-review): an interrupt inside the guest phase reaches
    the probe as an interrupted execution rather than as a raised
    ``KeyboardInterrupt``, so the mapping already became FAIL while
    ``report["interrupted"]`` stayed False.  The two fields must agree.
    """
    binary, rootfs, output = material
    real = smolvm._run

    def run(argv, path, label, env, timeout):
        result = real(argv, path, label, env, timeout)
        if label == "guest":
            result = {
                **result,
                "exit_code": -signal.SIGINT,
                "timeout": False,
                "interrupted": True,
            }
            (path / f"{label}.json").write_text(json.dumps(result, indent=2) + "\n")
        return result

    monkeypatch.setattr(smolvm, "_run", run)
    with pytest.raises(smolvm.SmolvmProbeError):
        smolvm.probe_smolvm(
            binary=binary, rootfs=rootfs, output=output, resize2fs=binary
        )
    report = json.loads((output / "report.json").read_text())
    assert report["interrupted"] is True
    assert report["interrupt_signal"] == "SIGINT"
    assert report["checks"]["mapping"] == "FAIL"
    assert report["ok"] is False


def test_interrupt_while_writing_post_run_evidence_keeps_the_evidence(material, monkeypatch):
    """The deferred window must finish report.json and the three after snapshots."""
    binary, rootfs, output = material
    real = smolvm._snapshot
    seen = {"bundle": 0}

    def snapshot(path):
        if Path(path) == binary.parent:
            seen["bundle"] += 1
            if seen["bundle"] == 2:  # the post-run snapshot inside the finally
                os.kill(os.getpid(), signal.SIGINT)
        return real(path)

    monkeypatch.setattr(smolvm, "_snapshot", snapshot)
    with pytest.raises(KeyboardInterrupt):
        smolvm.probe_smolvm(binary=binary, rootfs=rootfs, output=output, resize2fs=binary)
    report = json.loads((output / "report.json").read_text())
    assert report["interrupted"] is True
    assert report["phase"] == "cleanup"
    assert report["cleanup"]["ok"] is True
    assert report["ok"] is False
    for name in ("runtime-after.json", "rootfs-after.json", "host-after.json"):
        assert (output / name).exists(), name


def test_real_smolvm_mapping():
    """NOT_RUN in normal CI; invoke under with-machine-lock on an approved host."""
    names = (
        "HYPRIAL_TEST_SMOLVM",
        "HYPRIAL_TEST_SMOLVM_ROOTFS",
        "HYPRIAL_TEST_SMOLVM_OUTPUT",
        "HYPRIAL_TEST_RESIZE2FS",
    )
    if not all(os.environ.get(name) for name in names):
        pytest.skip(
            "NOT_RUN: real VM requires explicit local runtime/rootfs/output and approved machine lock"
        )
    material = tuple(Path(os.environ[name]) for name in names)
    result = smolvm.probe_smolvm(
        binary=material[0],
        rootfs=material[1],
        output=material[2],
        resize2fs=material[3],
    )
    report = result
    assert report["checks"]["mapping"] == "PASS"
    assert report["checks"]["worker"] == "NOT_RUN"
