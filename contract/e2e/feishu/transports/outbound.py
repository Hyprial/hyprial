"""E2E-004 transports / outbound; explicit callbacks keep entrypoint seams live."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from _daemon_runner import IsolatedDaemon
from feishu.evidence import ContractBlocked, ContractFailure, safe_cli_error


@dataclass(frozen=True)
class TransportsOutboundPorts:
    """Only this module's observed collaborators and configuration."""

    APP_ID: str
    APP_NAME: str
    OUTBOUND_SENDER: str
    SECRET_PATH: Path
    entrypoint: str
    injected_app_credentials: Callable[..., Any]
    outbound_daemon_environment: Callable[..., Any]
    hyprial_cli: Callable[..., Any]


def hyprial_cli(
    executable: Path,
    environment: Mapping[str, str],
    *arguments: str,
    cwd: Path | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """Run the exact checkout CLI against one disposable daemon home."""

    result = subprocess.run(
        [str(executable), *arguments],
        env=dict(environment),
        text=True,
        capture_output=True,
        check=False,
        cwd=str(cwd) if cwd is not None else None,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise ContractFailure(
            f"hyprial {' '.join(arguments[:2])} failed ({result.returncode}): "
            f"{safe_cli_error(result.stderr, result.stdout)}"
        )
    try:
        value = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ContractFailure("hyprial returned non-JSON success output") from error
    if not isinstance(value, dict):
        raise ContractFailure("hyprial JSON output must be an object")
    if value.get("ok") is not True:
        raise ContractFailure(
            f"hyprial returned a non-success result: {safe_cli_error('', result.stdout)}"
        )
    return value


def prepare_outbound_daemon(
    root: Path, *, chat_id: str, _ports: TransportsOutboundPorts
) -> tuple[Path, dict[str, str], dict[str, Any]]:
    """Create a secret-safe isolated home for the outbound public CLI seam.

    The approved test credential stays out of the repository and logs.
    In the default (developer) mode the disposable home contains only a
    symlink to the read-only source file; in env-injected mode (CI) the
    injected secret is written fresh at mode 0600 and the production file
    is never opened.
    """

    executable = Path(
        os.environ.get(
            "HYPRIAL_BIN",
            str(Path(_ports.entrypoint).resolve().parents[2] / ".venv/bin/hyprial"),
        )
    ).resolve()
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise ContractBlocked(f"exact hyprial executable is missing: {executable}")
    daemon_root = root / "outbound-daemon"
    workspace = root / "outbound-workspace"
    workspace.mkdir(mode=0o700)
    home = daemon_root / "home"
    state = daemon_root / "state"
    secrets = home / "secrets"
    secrets.mkdir(parents=True)
    state.mkdir(parents=True)
    (home / "channels.json").write_text(
        json.dumps(
            {
                "version": 1,
                "gateways": [
                    {
                        "provider": "lark",
                        "name": _ports.APP_NAME,
                        "appId": _ports.APP_ID,
                        "credentialRef": f"lark-{_ports.APP_NAME}",
                        "defaultRoute": "e2e-outbound-file",
                        "routes": [
                            {
                                "name": "e2e-outbound-file",
                                "type": "direct",
                                "nativeId": chat_id,
                            }
                        ],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    credential_path = secrets / f"lark-{_ports.APP_NAME}.json"
    injected = _ports.injected_app_credentials()
    if injected is not None:
        credential_path.write_text(
            json.dumps({"appSecret": injected[1]}), encoding="utf-8"
        )
        credential_path.chmod(0o600)
    else:
        credential_path.symlink_to(_ports.SECRET_PATH)
    environment = _ports.outbound_daemon_environment(home, state)
    if (
        Path(environment["HYPRIAL_HOME"]).resolve()
        == (Path.home() / ".hyprial").resolve()
    ):
        raise ContractFailure("outbound E2E home resolved to the real ~/.hyprial")
    version = _ports.hyprial_cli(executable, environment, "version", "--json")
    _ports.hyprial_cli(executable, environment, "init", "--json", timeout=150.0)
    # resolve-or-reject (Allen b): the outbound send's sender must be a
    # registered identity on this daemon.  Disposable home, so no teardown
    # cleanup is needed beyond stopping the daemon.
    _ports.hyprial_cli(
        executable,
        environment,
        "agent",
        "create",
        "--name",
        _ports.OUTBOUND_SENDER,
        "--json",
    )
    # A sender name alone has no file-read authority. Bind the public CLI
    # to a disposable session whose cwd contains the outbound attachment.
    session_ref = uuid4().hex
    session = IsolatedDaemon(
        executable, daemon_root, environment["HYPRIAL_NODE_ID"]
    ).request(
        "session.register",
        {
            "actor": _ports.OUTBOUND_SENDER,
            "sessionRef": session_ref,
            "cwd": str(workspace.resolve()),
            "command": ["e2e004-outbound-driver"],
            "source": "e2e",
            "runtime": "e2e_interactive",
        },
    )
    actor = session.get("actor")
    if not isinstance(actor, str) or not actor:
        raise ContractFailure("outbound session registration returned no actor")
    _bind_sender_cell(executable, environment, root, _ports=_ports)
    environment["HYPRIAL_WORKER_ACTOR"] = actor
    environment["HYPRIAL_WORKER_SESSION_REF"] = session_ref
    environment["E2E_OUTBOUND_USER_TARGET"] = f"user:{actor.split(':')[1]}"
    return executable, environment, version


def _bind_sender_cell(
    executable: Path, environment: dict[str, str], root: Path, *, _ports: TransportsOutboundPorts
) -> None:
    """Give the sender's cell the App as an adapter member (I4b: the push picks its chat)."""

    shown = _ports.hyprial_cli(
        executable, environment, "agent", "cell", "show", _ports.OUTBOUND_SENDER, "--json"
    )
    cell = shown.get("cell") if isinstance(shown, dict) else None
    if not isinstance(cell, dict) or not isinstance(cell.get("revision"), int):
        raise ContractFailure("outbound sender cell show answered no revision")
    members = cell.get("members") if isinstance(cell.get("members"), list) else []
    params = dict(cell.get("params") if isinstance(cell.get("params"), dict) else {})
    member_data = dict(params.get("cell.members") or {})
    member_data["im"] = {"adapter": _ports.APP_NAME}
    if "cell.entry" not in params and members:
        params["cell.entry"] = members[0]["ref"]
    fragment = root / "outbound-sender-cell.json"
    fragment.write_text(json.dumps({
        "members": [*members, {"ref": "im", "role": "adapter"}],
        "params": {**params, "cell.members": member_data},
    }), encoding="utf-8")
    applied = _ports.hyprial_cli(
        executable, environment, "agent", "cell", "apply", _ports.OUTBOUND_SENDER,
        "--expected-revision", str(cell["revision"]), "--file", str(fragment), "--json",
    )
    if not isinstance(applied, dict) or applied.get("ok") is not True:
        raise ContractFailure("outbound sender cell apply did not bind the App")


def stop_outbound_daemon(
    executable: Path | None,
    environment: Mapping[str, str] | None,
    *,
    _ports: TransportsOutboundPorts,
) -> None:
    if executable is None or environment is None:
        return
    socket_path = Path(environment["HARNESS_STATE_DIR"]) / "daemon.sock"
    if socket_path.exists():
        _ports.hyprial_cli(
            executable, environment, "daemon", "stop", "--json", timeout=20.0
        )
