"""Defects found by the real macOS (launchd) drill, 2026-11-21.

Decisions pinned here:
- ``HYPRIAL_NODE_ID`` in the caller's environment is dropped from every live
  CLI command: the product's units never set it, so a supervised daemon reads
  ``<home>/node-id``, and the CLI must resolve the node the same way.  The
  summary names every dropped variable that was set (never its value);
- a ``uv-receipt.toml`` ``python`` that is a request string (``"3.12"``), not an
  existing absolute path, falls back to the tool environment's own interpreter,
  and ``paths.resolve`` names what is missing;
- ``post.routines`` reports a failed ``routine list`` (non-zero exit or
  ``ok: false``) as that failure, with its code, not as an empty list;
- ``service.attributed-after-install`` and ``rollback.attributed`` leave a PASS
  record when they pass.
"""

from __future__ import annotations

import json
import os
import sys

from ..fakes import FakeHost, build_apply, check, result, smoke, summary_of

NODE = "node-from-the-shell-0123"


def test_an_ambient_node_id_is_dropped_and_named_in_the_summary(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("HYPRIAL_NODE_ID", NODE)
    monkeypatch.setenv("HYPRIAL_OWNER", "someone")
    instance, host = build_apply(tmp_path)

    rc = instance.finish(instance.run_apply())

    assert rc == smoke.EXIT_OK
    hyprial_envs = [
        env for command, env in zip(host.calls, host.envs) if command[0].endswith("hyprial")
    ]
    assert hyprial_envs
    assert all(env is not None and "HYPRIAL_NODE_ID" not in env for env in hyprial_envs)
    summary = summary_of(instance)
    assert summary["droppedEnvironment"] == ["HYPRIAL_NODE_ID", "HYPRIAL_OWNER"]
    text = (instance.artifacts_dir / "summary.json").read_text()
    text += (instance.artifacts_dir / "commands.json").read_text()
    assert NODE not in text


def test_no_dropped_variables_means_an_empty_list(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("HYPRIAL_NODE_ID", raising=False)
    monkeypatch.delenv("HYPRIAL_OWNER", raising=False)
    monkeypatch.delenv("HARNESS_SOCKET_PATH", raising=False)
    instance, host = build_apply(tmp_path)

    instance.finish(instance.run_apply())

    assert summary_of(instance)["droppedEnvironment"] == []


def tool_with_receipt(tmp_path, python_value: str, *, venv_python: bool):
    instance, host = build_apply(tmp_path, mode="preflight")
    instance.options.python = None
    tool = tmp_path / "tool"
    (tool / "bin").mkdir(parents=True, exist_ok=True)
    (tool / "uv-receipt.toml").write_text(
        f'[tool]\nrequirements = []\npython = "{python_value}"\n'
    )
    if venv_python:
        (tool / "bin" / "python").write_text("#!/bin/sh\n")
    return instance, host, tool


def test_a_receipt_python_request_string_falls_back_to_the_tool_interpreter(tmp_path) -> None:
    instance, host, tool = tool_with_receipt(tmp_path, "3.12", venv_python=True)

    assert instance.resolve_paths() is True
    assert instance.python == tool / "bin" / "python"


def test_paths_resolve_names_what_is_missing(tmp_path) -> None:
    instance, host, tool = tool_with_receipt(tmp_path, "3.12", venv_python=False)

    assert instance.prepare() is False

    detail = check(instance, "paths.resolve").detail
    assert "python" in detail
    assert "3.12" in detail
    assert str(tool / "bin" / "python") in detail


def test_a_failed_routine_list_is_reported_as_a_failure(tmp_path) -> None:
    instance, host = build_apply(tmp_path, extra=["--expect-routine", "nightly"])
    original = host.hyprial

    def hyprial(command):
        if command[1:3] == ["routine", "list"]:
            return result(
                command, 1,
                {"ok": False, "code": "PAC_OWNER_UNKNOWN", "error": "no owner is logged in"},
            )
        return original(command)

    host.hyprial = hyprial

    instance.finish(instance.run_apply())

    routines = check(instance, "post.routines")
    assert routines.status == smoke.FAIL
    assert "PAC_OWNER_UNKNOWN" in routines.detail
    assert "rc=1" in routines.detail
    assert "listed=[]" not in routines.detail


def test_a_listed_routine_still_passes(tmp_path) -> None:
    instance, host = build_apply(tmp_path, extra=["--expect-routine", "nightly"])
    original = host.hyprial

    def hyprial(command):
        if command[1:3] == ["routine", "list"]:
            return result(command, 0, json.dumps(
                {"ok": True, "routines": [{"name": "nightly", "enabled": True}]}
            ))
        return original(command)

    host.hyprial = hyprial

    rc = instance.finish(instance.run_apply())

    assert rc == smoke.EXIT_OK
    assert check(instance, "post.routines").status == smoke.PASS


def test_attribution_checks_leave_a_pass_record(tmp_path) -> None:
    instance, host = build_apply(tmp_path)
    original = host.daemon_payload

    def payload():
        answer = original()
        if host.installed_version == "0.2.0" and answer["daemon"].get("running"):
            answer["daemon"]["lifecycle"] = {"crashed": True}
        return answer

    host.daemon_payload = payload

    instance.finish(instance.run_apply())

    assert check(instance, "service.attributed-after-install").status == smoke.PASS
    assert check(instance, "rollback.attributed").status == smoke.PASS
    assert instance.result == "ROLLED_BACK"


def test_fallback_cli_directory_is_first_on_live_path_while_uv_tool_bin_dir_stays_bin_dir(
    tmp_path,
) -> None:
    bin_dir = tmp_path / "empty-bin"
    bin_dir.mkdir()
    tool_dir = tmp_path / "tool"
    (tool_dir / "bin").mkdir(parents=True)
    fallback_cli = tool_dir / "bin" / "hyprial"
    fallback_cli.write_text("#!/bin/sh\nexit 0\n")
    fallback_cli.chmod(0o755)
    (tool_dir / "bin" / "python").write_text("#!/bin/sh\nexit 0\n")

    args = smoke.build_parser().parse_args(
        [
            "preflight",
            "--new-wheel",
            str(tmp_path / "hyprial-0.2.0-py3-none-any.whl"),
            "--wheelhouse",
            str(tmp_path),
            "--artifacts-dir",
            str(tmp_path / "artifacts"),
            "--tool-dir",
            str(tool_dir),
            "--bin-dir",
            str(bin_dir),
            "--python",
            sys.executable,
        ]
    )
    instance = smoke.Smoke(args, runner=FakeHost(tmp_path, tmp_path / "home"))

    assert instance.resolve_paths() is True
    assert instance.live_cli == fallback_cli
    assert instance.live_environment()["PATH"].split(os.pathsep)[0] == str(tool_dir / "bin")
    assert instance.live_install_environment()["UV_TOOL_BIN_DIR"] == str(bin_dir)
