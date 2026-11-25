"""E2E scenarios must run the tree under test, and be able to prove it.

One E2E run's provenance became undeterminable after the fact: the resolver
picked a binary, resolved it, and returned only the path, so "which hyprial did
that run exercise" had no answer left anywhere.  Worse, its last candidate
was ``PATH`` -- the installed production build -- selected silently.  A
scenario could therefore pass while testing something other than the
checkout, and nothing in the result said so.

These pin the three properties that make that impossible: no silent fallback,
the choice is part of the return value, and a declared SHA is actually
checked against the tree.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[5]
RUNNER = ROOT / "contract" / "e2e" / "_daemon_runner.py"


def _runner():
    spec = importlib.util.spec_from_file_location("_daemon_runner_probe", RUNNER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _resolution():
    """The module that OWNS the resolution behaviour.

    2026-10-16: the resolver was split out of _daemon_runner.py into
    contract/e2e/_hyprial_resolution.py to get that file back under the 1000-line
    structural limit.  The scenarios below patch the resolver's ROOT and its view
    of PATH, so the patch belongs on the module that reads those names; patching
    them on _daemon_runner would now be a patch on a re-export that nothing reads
    -- which is how the first version of this split went red.
    """
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return importlib.import_module("contract.e2e._hyprial_resolution")


def test_missing_tree_venv_never_silently_falls_back_to_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The production build on PATH must not stand in for the tree under test.

    Reverting to a PATH candidate makes this return a path instead of raising:
    the scenario would then run the installed hyprial and report success.
    """

    runner = _runner()
    resolution = _resolution()
    monkeypatch.setattr(resolution, "ROOT", tmp_path)  # no .venv/bin/hyprial here
    monkeypatch.delenv("HYPRIAL_BIN", raising=False)

    fake_path_hyprial = tmp_path / "usr-bin" / "hyprial"
    fake_path_hyprial.parent.mkdir(parents=True)
    fake_path_hyprial.write_text("#!/bin/sh\nexit 0\n")
    fake_path_hyprial.chmod(0o755)
    monkeypatch.setattr(resolution.shutil, "which", lambda _name: str(fake_path_hyprial))

    with pytest.raises(AssertionError) as excinfo:
        runner.resolve_hyprial()

    # It must not merely fail -- it must say why PATH was refused, or the next
    # person re-adds the fallback to "fix" the error.
    assert "PATH is deliberately NOT a candidate" in str(excinfo.value)


def test_error_names_every_candidate_including_the_refused_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The message must not describe a narrower search than was performed.

    The previous text named HYPRIAL_BIN and .venv only, while the resolver also
    consulted PATH -- so its reader built a two-candidate mental model of a
    three-candidate resolver, and the omitted one was the harmful one.
    """

    runner = _runner()
    resolution = _resolution()
    monkeypatch.setattr(resolution, "ROOT", tmp_path)
    monkeypatch.delenv("HYPRIAL_BIN", raising=False)
    monkeypatch.setattr(resolution.shutil, "which", lambda _name: "/usr/local/bin/hyprial")

    with pytest.raises(AssertionError) as excinfo:
        runner.resolve_hyprial()

    message = str(excinfo.value)
    assert "HYPRIAL_BIN" in message
    assert ".venv" in message
    assert "/usr/local/bin/hyprial" in message


def test_resolution_reports_which_candidate_won(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Returning only the path is what made provenance unanswerable."""

    runner = _runner()
    resolution = _resolution()
    monkeypatch.setattr(resolution, "ROOT", tmp_path)
    chosen = tmp_path / "explicit" / "hyprial"
    chosen.parent.mkdir(parents=True)
    chosen.write_text("#!/bin/sh\nexit 0\n")
    chosen.chmod(0o755)
    monkeypatch.setenv("HYPRIAL_BIN", str(chosen))

    resolution = runner.resolve_hyprial()

    assert resolution.path == chosen.resolve()
    assert resolution.source == "HYPRIAL_BIN"
    # This particular binary is a bare shell script with no venv around it, so
    # no installer record exists to name a tree.  That is why src_dir is None
    # here -- NOT because the binary came from HYPRIAL_BIN.  The earlier version of
    # this test asserted the same value for the opposite reason, and that
    # reading is what shipped the defect: the resolver used the candidate's
    # name as a proxy for whether a tree existed, and refused to look.
    assert resolution.src_dir is None
    assert resolution.tree_head is None
    assert "no site-packages" in resolution.src_reason


def _plant_editable_install(venv: Path, tree: Path, *, python: str = "3.12") -> Path:
    """Write the on-disk layout ``uv sync`` produces, and return the binary.

    The record written here is byte-shaped like the real one; that it *is* what
    uv writes was checked separately against an actual ``uv sync`` rather than
    assumed, because a fixture agreeing with the code that reads it proves
    nothing about the installer.
    """

    binary = venv / "bin" / "hyprial"
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    dist_info = venv / "lib" / f"python{python}" / "site-packages" / "hyprial-0.4.2.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "direct_url.json").write_text(
        json.dumps({"url": tree.as_uri(), "dir_info": {"editable": True}}),
        encoding="utf-8",
    )
    return binary


def test_hyprial_bin_pointing_into_a_checkout_is_given_its_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The layout CI actually uses must resolve to the checkout under test.

    ``HYPRIAL_BIN`` in the workflow is ``$GITHUB_WORKSPACE/.venv/bin/hyprial`` -- a
    venv built from the very checkout whose SHA the run declares.  Deciding
    src_dir from the candidate's name refused that tree, so every scenario
    that declared a SHA failed on a guard that had the answer available and
    did not look.
    """

    runner = _runner()
    resolution = _resolution()
    monkeypatch.setattr(resolution, "ROOT", tmp_path / "unused-tree")
    tree = tmp_path / "workspace"
    tree.mkdir()
    binary = _plant_editable_install(tree / ".venv", tree)
    monkeypatch.setenv("HYPRIAL_BIN", str(binary))

    resolution = runner.resolve_hyprial()

    assert resolution.source == "HYPRIAL_BIN"
    assert resolution.src_dir == tree
    assert "editable install recorded by" in resolution.src_reason


def test_a_non_editable_install_is_not_treated_as_the_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A built copy's tree can move while the executed code stays frozen.

    Accepting any ``file://`` url would let the guard pass by comparing the
    declared SHA against a tree it is not running, which is the same silent
    green the whole resolver exists to remove.
    """

    runner = _runner()
    resolution = _resolution()
    monkeypatch.setattr(resolution, "ROOT", tmp_path / "unused-tree")
    tree = tmp_path / "workspace"
    tree.mkdir()
    venv = tmp_path / "elsewhere" / ".venv"
    binary = _plant_editable_install(venv, tree)
    record = next(venv.glob("lib/python*/site-packages/hyprial-*.dist-info/direct_url.json"))
    record.write_text(
        json.dumps({"url": tree.as_uri(), "dir_info": {}}), encoding="utf-8"
    )
    monkeypatch.setenv("HYPRIAL_BIN", str(binary))

    resolution = runner.resolve_hyprial()

    assert resolution.src_dir is None
    assert "non-editable" in resolution.src_reason


def test_the_refusal_carries_what_was_looked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """"Cannot be checked" must name evidence, not just a conclusion.

    Six scenarios failed on this message at once; a reader who cannot see
    which record was consulted has to reconstruct the resolver to know
    whether the environment or the guard is at fault.
    """

    runner = _runner()
    monkeypatch.setenv("HYPRIAL_E2E_EXPECT_SHA", "c" * 40)
    resolution = runner.HYPRIALResolution(
        path=Path("/tmp/hyprial"),
        source="HYPRIAL_BIN",
        src_dir=None,
        tree_head=None,
        src_reason="no hyprial-*.dist-info under /tmp/venv",
    )

    with pytest.raises(AssertionError) as excinfo:
        runner.assert_tree_matches_declared_sha(resolution)

    assert "no hyprial-*.dist-info under /tmp/venv" in str(excinfo.value)


def test_declared_sha_mismatch_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner()
    monkeypatch.setenv("HYPRIAL_E2E_EXPECT_SHA", "0" * 40)
    resolution = runner.HYPRIALResolution(
        path=Path("/tmp/hyprial"),
        source="tree .venv",
        src_dir=Path("/tmp/tree"),
        tree_head="a" * 40,
    )

    with pytest.raises(AssertionError, match="declared"):
        runner.assert_tree_matches_declared_sha(resolution)


def test_declared_sha_check_fails_when_its_subject_is_unobservable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-existence check must also assert it is looking somewhere real.

    Without this, a tree that moved away or a git that stopped answering
    yields tree_head=None, the equality never fires, and the gate reports
    green while guarding nothing.
    """

    runner = _runner()
    monkeypatch.setenv("HYPRIAL_E2E_EXPECT_SHA", "a" * 40)

    no_tree = runner.HYPRIALResolution(
        path=Path("/tmp/hyprial"), source="HYPRIAL_BIN", src_dir=None, tree_head=None
    )
    with pytest.raises(AssertionError, match="no source tree"):
        runner.assert_tree_matches_declared_sha(no_tree)

    unreadable = runner.HYPRIALResolution(
        path=Path("/tmp/hyprial"),
        source="tree .venv",
        src_dir=Path("/tmp/tree"),
        tree_head=None,
    )
    with pytest.raises(AssertionError, match="rev-parse"):
        runner.assert_tree_matches_declared_sha(unreadable)


def test_hyprial_executable_records_provenance_for_every_scenario(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Provenance must not depend on each scenario remembering to ask for it.

    Twenty scenarios call hyprial_executable(); routing the record through it is
    what makes "which hyprial ran" answerable without editing all twenty.
    """

    runner = _runner()
    resolution = _resolution()
    monkeypatch.setattr(resolution, "_RECORDED_RESOLUTION", None)
    chosen = tmp_path / "bin" / "hyprial"
    chosen.parent.mkdir(parents=True)
    chosen.write_text("#!/bin/sh\nexit 0\n")
    chosen.chmod(0o755)
    monkeypatch.setenv("HYPRIAL_BIN", str(chosen))
    monkeypatch.delenv("HYPRIAL_E2E_EXPECT_SHA", raising=False)
    monkeypatch.delenv("GITHUB_SHA", raising=False)

    runner.hyprial_executable()

    printed = capsys.readouterr().out
    assert "hyprial-under-test" in printed
    assert f"path={chosen.resolve()}" in printed
    assert "source=HYPRIAL_BIN" in printed


def test_hyprial_executable_enforces_a_declared_sha(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The assertion has to sit on the path scenarios actually take."""

    runner = _runner()
    chosen = tmp_path / "bin" / "hyprial"
    chosen.parent.mkdir(parents=True)
    chosen.write_text("#!/bin/sh\nexit 0\n")
    chosen.chmod(0o755)
    monkeypatch.setenv("HYPRIAL_BIN", str(chosen))
    monkeypatch.setenv("HYPRIAL_E2E_EXPECT_SHA", "b" * 40)

    with pytest.raises(AssertionError, match="cannot be checked"):
        runner.hyprial_executable()


def test_declaring_no_sha_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Local runs declare nothing; the check must not invent a requirement."""

    runner = _runner()
    monkeypatch.delenv("HYPRIAL_E2E_EXPECT_SHA", raising=False)
    monkeypatch.delenv("GITHUB_SHA", raising=False)

    runner.assert_tree_matches_declared_sha(
        runner.HYPRIALResolution(
            path=Path("/tmp/hyprial"), source="HYPRIAL_BIN", src_dir=None, tree_head=None
        )
    )
