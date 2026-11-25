#!/usr/bin/env python3
"""E2E-010: upgrade a *running* deployment and prove continuity.

Distinct from E2E-005 (install/upgrade/uninstall + version resolution, which
never boots a daemon) and from E2E-002/003 (same-version restart survival):
this scenario boots a live daemon on an *old* version with connectors, a
registered interactive session, and in-flight durable messages, upgrades the
package and lets ``hyprial upgrade`` gracefully restart the running daemon, then
asserts the deployment came back automatically on a *different* version with

  * connectors recovered online (fresh worker PIDs),
  * the interactive session re-registered on the new daemon,
  * pending messages preserved (stable messageId, fresh deliveryId, FIFO) and
    not replayed after acknowledgement.

The version boundary is genuine: the OLD version is the whole ``v0.5.1``
tree (the oldest supported channel with upgrade-safe logging), the NEW one the candidate; it reuses
E2E-005's isolated uv-tool install against a disposable bare Git remote (no
ambient uv/HOME state; real Forgejo read once for the old tag, never written). py's ``hyprial upgrade`` swaps the on-disk package
(``uv tool install --force`` at the highest version tag), gracefully stops a
running daemon, and launches the upgraded generation through the same detached
``daemon run`` path as ``hyprial init``. Every check is PASS/FAIL and the runner
exits 0 only when nothing failed.
"""

from __future__ import annotations

import json as json
import os as os
import re as re
import signal as signal
import socket as socket
import subprocess
import sys
import tempfile as tempfile
import time as time
from dataclasses import dataclass
from functools import partial as partial
from pathlib import Path
from uuid import uuid4 as uuid4

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _upgrade_continuity import channel_probe as _channel_probe_impl  # noqa: E402
from _upgrade_continuity import installation as _installation_impl  # noqa: E402
from _upgrade_continuity import inbox as _inbox_impl  # noqa: E402
from _upgrade_continuity import identity as _identity_impl  # noqa: E402
from _upgrade_continuity import scenario as _scenario_impl  # noqa: E402

from _diagnostic_text import clip, one_line
from _daemon_runner import (
    free_tcp_endpoint as free_tcp_endpoint,
    refuse_unisolatable_daemon as refuse_unisolatable_daemon,
    short_temp_root as short_temp_root,
    write_project_version as write_project_version,
)

from hyprial.kernel import CHANNEL_PROTOCOL_VERSION as CHANNEL_PROTOCOL_VERSION

ROOT = Path(__file__).resolve().parents[2]
GIT_SOURCE = "ssh://git@git.internal.hyprial.com/HyprialOS/harness-bridge.git"
OFFICIAL_GIT_SOURCE = "https://github.com/Hyprial/hyprial.git"
NODE_ID = "e2e-upgrade-continuity"
DRIVER = "upgrade-driver"
SESSION_ACTOR = "upgrade-cont-session"
CHANNEL_NAME = "upgrade-config-probe"
ADAPTER_NAMES = ("upgrade-cont-alpha", "upgrade-cont-beta")
SECRET_MARKER = "e2e-upgrade-secret-must-not-print"
INITIAL_VERSION = "0.4.0.dev0"
UPGRADED_VERSION = "0.4.0"
#: v0.5.1 is the oldest supported channel containing acc00a82d. A candidate
#: cannot retrofit that logging fix into an already-running older channel.
#: The pre-v0.5.1 limitation remains executable in the separately named runner.
UPGRADE_FROM_ENV = "HYPRIAL_E2E_UPGRADE_FROM"
UPGRADE_FROM_DEFAULT = "v0.5.1"



class NoUpgradeDelta(Exception):
    """The promoted tree IS the candidate: there is no upgrade to exercise."""


@dataclass(frozen=True, slots=True)
class Result:
    status: str
    check: str
    detail: str


class Report:
    def __init__(self) -> None:
        self.results: list[Result] = []

    def add(self, status: str, check: str, detail: str) -> None:
        clean = one_line(detail)
        clean = clip(clean)
        self.results.append(Result(status, check, clean))
        print(f"RESULT {status} E2E-010 {check} {clean}", flush=True)

    def exit_code(self) -> int:
        counts = {
            status: sum(item.status == status for item in self.results)
            for status in ("PASS", "FAIL", "BLOCKED")
        }
        overall = "FAIL" if counts["FAIL"] else (
            "BLOCKED" if counts["BLOCKED"] else "PASS"
        )
        print(
            "RESULT "
            f"{overall} E2E-010 upgrade-continuity-{os.environ.get('HYPRIAL_E2E_UPGRADE_CASE', 'same-interpreter')} "
            f"pass={counts['PASS']} fail={counts['FAIL']} "
            f"blocked={counts['BLOCKED']}",
            flush=True,
        )
        return 1 if counts["FAIL"] else (2 if counts["BLOCKED"] else 0)


def _safe_tool_error_kind(error: BaseException) -> str:
    """Classify nested transport failures without serializing their payloads."""
    return _channel_probe_impl._safe_tool_error_kind({}, error)


# --------------------------------------------------------------------------- #
# Fake Lark worker: the isolation seam.  It signals readiness on the inherited
# HYPRIAL_LARK_READY_FD exactly as the real worker does, then idles until SIGTERM.
# No production credential or Lark network is touched.
# --------------------------------------------------------------------------- #
def fake_worker(name: str) -> int:
    return _channel_probe_impl.fake_worker({'json': json, 'os': os, 'signal': signal, 'time': time}, name)


async def _run_stdio_client(
    *,
    command: str,
    args: list[str],
    child_env: dict[str, str],
    ready: Path,
    stop: Path | None,
    phase: Path,
    facts: dict[str, object] | None = None,
) -> None:
    """Hold one real installed stdio Channel open without a Claude account."""
    return await _channel_probe_impl._run_stdio_client({'_merge_probe_state': _merge_probe_state, '_read_phase': _read_phase, '_safe_tool_error_kind': _safe_tool_error_kind, '_write_probe_state': _write_probe_state, 'sys': sys}, command=command, args=args, child_env=child_env, ready=ready, stop=stop, phase=phase, facts=facts)


def _write_probe_state(path: Path, document: dict[str, object]) -> None:
    return _channel_probe_impl._write_probe_state({'json': json}, path, document)


def _merge_probe_state(path: Path, update: dict[str, object]) -> None:
    return _channel_probe_impl._merge_probe_state({'_write_probe_state': _write_probe_state, 'json': json}, path, update)


def _read_phase(path: Path) -> str:
    return _channel_probe_impl._read_phase({}, path)


def _set_phase(path: Path, phase: str) -> None:
    return _channel_probe_impl._set_phase({}, path, phase)


def installed_python(executable: Path) -> Path:
    """Locate the interpreter that owns the package installed beside ``executable``."""
    return _channel_probe_impl.installed_python({'Path': Path, 'os': os}, executable)


def channel_spawn(executable: str, inner_args: list[str]) -> tuple[str, list[str]]:
    """Resolve the installed build's Channel server entry (R11 seam)."""
    return _channel_probe_impl.channel_spawn(
        {
            'Path': Path,
            'installed_python': installed_python,
            'subprocess': subprocess,
        },
        executable,
        inner_args,
    )


def read_target_process_identity(executable: Path, pid: int) -> str:
    """Read the owner birth marker with the SAME hyprial build the child runs.

    The marker is re-read and compared by the spawned channel child, so it
    must come from the child's own package: this probe runs the workspace
    code, which may carry a newer marker format than a seeded old install.
    Comparing across marker formats is exactly the upgrade-seam defect this
    scenario must not reintroduce through its own harness.
    """
    return _channel_probe_impl.read_target_process_identity({'Path': Path, 'os': os, 'subprocess': subprocess, 'installed_python': installed_python}, executable, pid)


def channel_client_mode(argv: list[str]) -> int:
    """Process mode: use the installed package to own an MCP child across upgrade."""
    return _channel_probe_impl.channel_client_mode({'Path': Path, '_run_stdio_client': _run_stdio_client, 'channel_spawn': channel_spawn, 'os': os, 'partial': partial, 'read_target_process_identity': read_target_process_identity}, argv)


def fake_claude_mode(argv: list[str]) -> int:
    """Exercise the generated MCP config while keeping all traffic isolated."""
    return _channel_probe_impl.fake_claude_mode({'CHANNEL_NAME': CHANNEL_NAME, 'Path': Path, '_run_stdio_client': _run_stdio_client, 'json': json, 'os': os, 'partial': partial}, argv)


def run(*command: str | Path, env: dict[str, str], cwd: Path | None = None) -> str:
    return _installation_impl.run({'subprocess': subprocess}, *command, env=env, cwd=cwd)


def hyprial(
    executable: Path, args: list[str], env: dict[str, str],
    *, exits: tuple[int, ...] = (0,),
) -> dict[str, object]:
    # 300s, not 60: `hyprial upgrade` reinstalls through uv, and on a cold CI
    # cache (run 488) the same step that takes seconds locally blew past 60s.
    # The bound still catches a hang; it just stops punishing cold caches.
    return _installation_impl.hyprial({'json': json, 'subprocess': subprocess}, executable, args, env, exits=exits)


def upgrade_command(executable: Path, env: dict[str, str]) -> list[str]:
    """Select the old executable's command without relying on fixture stamps."""
    return _installation_impl.upgrade_command({'subprocess': subprocess}, executable, env)


def init_command(executable: Path, env: dict[str, str]) -> list[str]:
    return _installation_impl.init_command({'subprocess': subprocess}, executable, env)


def message_summary_args(executable: Path, env: dict[str, str], summary: str) -> list[str]:
    return _installation_impl.message_summary_args({'subprocess': subprocess}, executable, env, summary)


def message_send_command(executable: Path, env: dict[str, str]) -> list[str]:
    return _installation_impl.message_send_command({'subprocess': subprocess}, executable, env)


def verify_restart_identity(before, after, live, **expected) -> str:
    return _installation_impl.verify_restart_identity(before, after, live, **expected)


def ipc(state_dir: Path, method: str, params: dict[str, object], timeout: float = 15.0):
    return _installation_impl.ipc({'json': json, 'socket': socket, 'uuid4': uuid4}, state_dir, method, params, timeout)


def daemon_ps(env: dict[str, str]) -> dict[str, object]:
    """Read the daemon ``ps`` snapshot over IPC.

    R7 deleted the top-level ``ps`` CLI command; this is the documented IPC
    seam (``_installation_impl.ipc``) the scenario already uses for session and
    inbox reads, so the assertions keep the same fields.
    """

    return _installation_impl.daemon_ps(
        {'ipc': ipc, 'time': time}, Path(env["HARNESS_STATE_DIR"])
    )


def git_config(path: Path, remote: Path) -> None:
    return _installation_impl.git_config({'GIT_SOURCE': GIT_SOURCE, 'OFFICIAL_GIT_SOURCE': OFFICIAL_GIT_SOURCE}, path, remote)


def materialize_revision(path: Path, revision: str, env: dict[str, str]) -> None:
    """Replace the complete tracked tree, including additions and deletions."""
    return _installation_impl.materialize_revision({'run': run}, path, revision, env)


def resolve_upgrade_from(seed: Path, host_env: dict[str, str]) -> tuple[str, str]:
    """Fetch the promoted OLD tree into ``seed``; return (source, commit).

    Read from the real remote, not a local tag: a checkout's tags are only as
    fresh as its last tag fetch, and CI's checkout fetches ${GITHUB_SHA}
    alone.  Runs with the HOST environment -- the isolated HOME has no
    credentials for the remote -- and before any insteadOf rewrite exists.
    """
    return _installation_impl.resolve_upgrade_from({'ROOT': ROOT, 'UPGRADE_FROM_DEFAULT': UPGRADE_FROM_DEFAULT, 'UPGRADE_FROM_ENV': UPGRADE_FROM_ENV, 'os': os, 're': re, 'run': run}, seed, host_env)


_READ_DESIRED_STATE = """
import json, sys
from pathlib import Path
try:
    from hyprial.daemon.impl.desired_state.sqlite import DesiredStateSqliteShadow
    from hyprial.daemon.impl.state_db import StateDatabase
except ModuleNotFoundError as error:
    if error.name != "hyprial.daemon.impl":
        raise
    from hyprial.daemon.desired_state_sqlite import DesiredStateSqliteShadow
    from hyprial.daemon.state_db import StateDatabase
document = DesiredStateSqliteShadow(
    StateDatabase(Path(sys.argv[1]) / "lifecycle-operations.sqlite3")
).read_document()
print(json.dumps(document))
"""


def read_desired_state(
    state: Path, installed_python: Path, env: dict[str, str]
) -> dict[str, object]:
    """The running daemon's committed desired-state document, read-only.

    Desired state lives only in SQLite (``lifecycle-operations.sqlite3``) in
    every version this scenario installs; the ``desired-state.json.v1`` file
    earlier revisions of this runner read was written only by the 2026-08-13
    tree.  Read through the product's own reverse mapping, run by the
    INSTALLED package's interpreter: the table layout moves between versions
    (the candidate reads columns an older promotion never created), so each
    version must read its own database.  Never construct
    ``DesiredStateStore`` here -- its constructor migrates, i.e. may WRITE to
    the database of the daemon under test.
    """
    return _installation_impl.read_desired_state({'_READ_DESIRED_STATE': _READ_DESIRED_STATE, 'json': json, 'run': run}, state, installed_python, env)


def channel_protocol_version_at(
    repository: Path, revision: str, env: dict[str, str]
) -> int:
    """The Channel protocol the tree at ``revision`` speaks.

    The old channel is driven by the OLD package, so it must be judged by the
    old tree's constant, not by the candidate's imported one: once a candidate
    bumps the protocol, the upgraded daemon is right to call a held-open old
    client ``legacy_or_unknown``.
    """
    return _installation_impl.channel_protocol_version_at({'re': re, 'run': run}, repository, revision, env)


def read_agent_identities(state, installed_python, env, *, owner, machine, names):
    return _identity_impl.read_agent_identities(
        {'json': json, 'run': run}, state, installed_python, env,
        owner=owner, machine=machine, names=names,
    )


def migrated_identities(before, current):
    return _identity_impl.migrated_identities(before, current)


def seed_remote(
    root: Path, env: dict[str, str], host_env: dict[str, str]
) -> tuple[Path, Path, str, str, str]:
    return _installation_impl.seed_remote({'INITIAL_VERSION': INITIAL_VERSION, 'NoUpgradeDelta': NoUpgradeDelta, 'ROOT': ROOT, 'materialize_revision': materialize_revision, 'resolve_upgrade_from': resolve_upgrade_from, 'run': run, 'write_project_version': write_project_version}, root, env, host_env)


def push_upgrade(
    seed: Path, candidate_revision: str, new: str, env: dict[str, str]
) -> str:
    return _installation_impl.push_upgrade({'materialize_revision': materialize_revision, 'run': run, 'write_project_version': write_project_version}, seed, candidate_revision, new, env)

def write_channels(home: Path) -> None:
    return _inbox_impl.write_channels({'ADAPTER_NAMES': ADAPTER_NAMES, 'SECRET_MARKER': SECRET_MARKER, 'json': json}, home)


def adapters_online(status: dict[str, object]) -> dict[str, object]:
    return _inbox_impl.adapters_online({}, status)


def wait_for_adapters(executable: Path, env: dict[str, str], timeout: float = 20.0):
    return _inbox_impl.wait_for_adapters({'ADAPTER_NAMES': ADAPTER_NAMES, 'adapters_online': adapters_online, 'hyprial': hyprial, 'ps': daemon_ps, 'time': time}, executable, env, timeout)


def pending_ids(status: dict[str, object]) -> list[str]:
    return _inbox_impl.pending_ids({}, status)


def delivery_for(status: dict[str, object], message_id: str) -> str:
    return _inbox_impl.delivery_for({}, status, message_id)


def pending_delivery_for(pending: dict[str, object], message_id: str) -> str:
    """Extract a deliveryId from a message.pending.list result (any era)."""
    return _inbox_impl.pending_delivery_for({}, pending, message_id)


def assert_legacy_rows(
    state_dir: Path, driver_uri: str, expectations: dict[str, str],
    *, summaries: dict[str, str | None] | None = None,
):
    """C48: an old-version row's summary/bodyBytes/read_body survive upgrade."""
    return _inbox_impl.assert_legacy_rows(
        {'ipc': ipc, 'Path': Path, 'sys': sys, 'uuid4': uuid4},
        state_dir,
        driver_uri,
        expectations,
        summaries=summaries,
    )


def wait_for_path(
    path: Path,
    process: subprocess.Popen[bytes],
    error_log: Path,
    *,
    stage: str,
    timeout: float = 45.0,
):
    return _channel_probe_impl.wait_for_path({'_channel_diagnostic': _channel_diagnostic, 'time': time}, path, process, error_log, stage=stage, timeout=timeout)


def start_channel_client(
    *, executable: Path, env: dict[str, str], root: Path,
    actor: str, session_ref: str, phase: Path,
) -> tuple[subprocess.Popen[bytes], Path, Path, Path]:
    return _channel_probe_impl.start_channel_client({'Path': Path, '__file__': __file__, '_set_phase': _set_phase, 'subprocess': subprocess, 'sys': sys, 'wait_for_path': wait_for_path}, executable=executable, env=env, root=root, actor=actor, session_ref=session_ref, phase=phase)


def stop_channel_client(
    process: subprocess.Popen[bytes], stop: Path, facts_path: Path, phase_path: Path
) -> None:
    return _channel_probe_impl.stop_channel_client({'_set_phase': _set_phase, 'json': json, 'os': os, 'subprocess': subprocess, 'time': time}, process, stop, facts_path, phase_path)


def wait_for_channel_child_alive(
    facts_path: Path,
    process: subprocess.Popen[bytes],
    error_log: Path,
    *,
    child_pid: int,
    phase: str,
    timeout: float = 10.0,
) -> dict[str, object]:
    """Require one unchanged stdio child and a live stable verifier owner."""
    return _channel_probe_impl.wait_for_channel_child_alive({'_channel_diagnostic': _channel_diagnostic, 'json': json, 'time': time}, facts_path, process, error_log, child_pid=child_pid, phase=phase, timeout=timeout)


def wait_for_channel_current(
    executable: Path, env: dict[str, str], *, actor: str, session_ref: str,
    build_version: str, process: subprocess.Popen[bytes] | None = None,
    error_log: Path | None = None,
    daemon_epoch: str,
    stage: str,
    protocol_version: int,
    generation: str,
    require_operational_liveness: bool,
    timeout: float = 45.0,
    actor_identity=None,
) -> dict[str, object]:
    return _channel_probe_impl.wait_for_channel_current({'_channel_diagnostic': _channel_diagnostic, 'hyprial': hyprial, 'ps': daemon_ps, 'time': time, 'actor_identity': actor_identity}, executable, env, actor=actor, session_ref=session_ref, build_version=build_version, process=process, error_log=error_log, daemon_epoch=daemon_epoch, stage=stage, protocol_version=protocol_version, generation=generation, require_operational_liveness=require_operational_liveness, timeout=timeout)


def _channel_diagnostic(path: Path | None) -> str:
    return _channel_probe_impl._channel_diagnostic({}, path)


def wait_for_new_tool_call(
    path: Path,
    previous: int,
    *,
    daemon_epoch: str,
    process: subprocess.Popen[bytes],
    error_log: Path,
    stage: str,
    timeout: float = 30.0,
) -> int:
    """Prove the same held-open MCP client reached the current daemon."""
    return _channel_probe_impl.wait_for_new_tool_call({'_channel_diagnostic': _channel_diagnostic, 'json': json, 'time': time}, path, previous, daemon_epoch=daemon_epoch, process=process, error_log=error_log, stage=stage, timeout=timeout)


def write_fake_claude_launcher(path: Path, *, facts: Path, phase: Path) -> None:
    # The fake is spawned by the DAEMON, not by the `hyprial agent start` CLI that
    # carried these variables, and the daemon forwards only part of its
    # caller's environment -- so the paths travel inside the launcher, like
    # the interpreter and script path already do.
    return _channel_probe_impl.write_fake_claude_launcher({'Path': Path, '__file__': __file__, 'sys': sys}, path, facts=facts, phase=phase)


def init_daemon_identity(init: dict[str, object]) -> dict[str, object]:
    """init's daemon identity, across the readiness-split shape change.

    Builds since the restore gate spread the ping payload -- flat
    running/pid/epoch/nodeId/owner -- while older builds nested the ps
    daemon block under "daemon".  This scenario drives both.
    """
    return _inbox_impl.init_daemon_identity({}, init)


def local_channel_actor(daemon: dict[str, object]) -> str:
    """Mint the Channel actor from the old daemon's real local identity."""
    return _inbox_impl.local_channel_actor({}, daemon)


def main() -> int:
    return _scenario_impl.main({
        'ADAPTER_NAMES': ADAPTER_NAMES,
        'CHANNEL_NAME': CHANNEL_NAME,
        'CHANNEL_PROTOCOL_VERSION': CHANNEL_PROTOCOL_VERSION,
        'DRIVER': DRIVER,
        'GIT_SOURCE': GIT_SOURCE,
        'INITIAL_VERSION': INITIAL_VERSION,
        'NODE_ID': NODE_ID,
        'NoUpgradeDelta': NoUpgradeDelta,
        'Path': Path,
        'Report': Report,
        'SESSION_ACTOR': SESSION_ACTOR,
        'UPGRADED_VERSION': UPGRADED_VERSION,
        '__file__': __file__,
        '_set_phase': _set_phase,
        'channel_client_mode': channel_client_mode,
        'channel_protocol_version_at': channel_protocol_version_at,
        'delivery_for': delivery_for,
        'assert_legacy_rows': assert_legacy_rows,
        'fake_claude_mode': fake_claude_mode,
        'fake_worker': fake_worker,
        'free_tcp_endpoint': free_tcp_endpoint,
        'git_config': git_config,
        'hyprial': hyprial,
        'init_daemon_identity': init_daemon_identity,
        'init_command': init_command,
        'ipc': ipc,
        'ps': daemon_ps,
        'json': json,
        'local_channel_actor': local_channel_actor,
        'os': os,
        'pending_delivery_for': pending_delivery_for,
        'pending_ids': pending_ids,
        'push_upgrade': push_upgrade,
        'read_desired_state': read_desired_state,
        'read_agent_identities': read_agent_identities,
        'migrated_identities': migrated_identities,
        'refuse_unisolatable_daemon': refuse_unisolatable_daemon,
        'run': run,
        'seed_remote': seed_remote,
        'short_temp_root': short_temp_root,
        'start_channel_client': start_channel_client,
        'stop_channel_client': stop_channel_client,
        'sys': sys,
        'tempfile': tempfile,
        'time': time,
        'uuid4': uuid4,
        'upgrade_command': upgrade_command,
        'verify_restart_identity': verify_restart_identity,
        'message_summary_args': message_summary_args,
        'message_send_command': message_send_command,
        'wait_for_adapters': wait_for_adapters,
        'wait_for_channel_child_alive': wait_for_channel_child_alive,
        'wait_for_channel_current': wait_for_channel_current,
        'wait_for_new_tool_call': wait_for_new_tool_call,
        'write_channels': write_channels,
        'write_fake_claude_launcher': write_fake_claude_launcher,
    })


if __name__ == "__main__":
    try:
        exit_code = main()
    except BaseException as error:  # noqa: BLE001 - final executable boundary
        clean = one_line(error)
        clean = clip(clean)
        print(
            "RESULT FAIL E2E-010 upgrade-continuity "
            f"pass=0 fail=1 blocked=0 error={clean}",
            flush=True,
        )
        exit_code = 1
    raise SystemExit(exit_code)
