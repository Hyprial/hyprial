"""Opt-in real daemon fixture: synthetic by default, explicit model authorization.

Only launch in the explicitly supplied private test HOME. No fake launcher,
supervisor, runtime, transport or app-server responses are installed.

就绪判定等到 dispatch gate 打开（ping phase 离开 restoring）。CI red
2026-11-03（#2291 runs 44123/44164 shard 1）：fixture 旧 ready() 在首个
ping 即返回，而 ping 是 restore 期间的 light method；随后首个重方法
（agent.secret.revoke / list）若仍处 restoring 会被 router 秒回
DAEMON_RESTORING error envelope，测试取 ["result"] 即 KeyError。该
契约本身是产品有意设计（router PR #332 F4②），故只改 fixture 就绪等待，
不改产品。竞态在 dev checkout 上即已探针复现，非 gui-v1 特有。
"""

from __future__ import annotations

import json
import asyncio
import itertools
import fcntl
import subprocess
import os
from pathlib import Path
import re
import sys
import threading
import time
import sqlite3
from contextlib import closing

from hyprial.identity.impl.agents import AgentSecretStore, SecretSource
from hyprial.identity.impl.agents.home.config import AgentConfig
from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.harnesses.codex import client as codex
from hyprial.daemon.impl.harnesses.codex.client import CodexAppServerClient
from hyprial.daemon.impl.harnesses.model_provider import ModelProviderError
from hyprial.daemon.impl.harnesses.smolvm_runtime import SmolvmWorkerRuntime
from hyprial.daemon.impl.transfer.execution.smolvm import _private_environment, _regular_read
from hyprial.shell.impl.daemon_wiring import daemon_dependencies as _deep_module_daemon_dependencies


def prepare_payload(output):
    source, workspace = output / "C", output / "workspace"
    (source / "native/codex").mkdir(parents=True)
    workspace.mkdir()
    (workspace / ".git").mkdir()
    (source / "native/codex/config.toml").write_text(
        'cli_auth_credentials_store = "ephemeral"\ncheck_for_update_on_startup = false\n'
        "allow_login_shell = false\n"
        f'[projects.{json.dumps(str(workspace))}]\ntrust_level = "trusted"\n'
    )
    return source, workspace


def health_snapshot(socket, output, label):
    # Reuse the existing real Unix protocol helpers, not another fake service.
    from tests.daemon.workers.test_worker_relay import request, frame

    ping = request(socket, frame("ping"))["result"]
    status = request(socket, frame("ps"))["result"]
    result = {
        "pid": ping["pid"],
        "epoch": ping["epoch"],
        "adapters": status["adapters"],
    }
    (output / (label + ".json")).write_text(json.dumps(result, indent=2))
    return result


# Adapter fields an operator watches, split by what can move WITHOUT the
# operator: `status`/`online`/`pid` follow a supervised worker reconnect, so
# they are observations rather than gates. `configured`/`desired` are the
# operator's intent for that adapter -- a flip inside a test window is a
# configuration change, not churn, so it gates the run (review 2026-09-29
# cc-sw-review; follow-up implemented 2026-09-30).
_ADAPTER_CHURN_FIELDS = ("status", "online")
_ADAPTER_INTENT_FIELDS = ("configured", "desired")


def production_identity(before, after):
    """Decide the production daemon identity from ``pid``+``epoch``+adapter set.

    A production restart changes ``pid`` and ``epoch``; nothing else in the
    snapshot does. An adapter worker is supervised separately and reconnects
    inside a test window on its own, flipping that adapter's ``status``/``online``
    (and its ``pid``) without touching the daemon. Comparing the whole snapshot
    turned that unrelated churn into a false FAIL, so the adapter list is now
    reported field by field. ``identity_unchanged`` still means only "the daemon
    process is the same one"; everything an operator did NOT do is reported
    separately as ``configuration_unchanged``, and ``production_unchanged``
    requires both -- an adapter added or removed, or its ``configured``/``desired``
    intent flipped, under a test is a production configuration change and must
    still fail the run.
    """
    adapters_before = {entry.get("id"): entry for entry in before.get("adapters", [])}
    adapters_after = {entry.get("id"): entry for entry in after.get("adapters", [])}
    shared = sorted(adapters_before.keys() & adapters_after.keys())
    changes = [
        {
            "id": adapter_id,
            "field": field,
            "before": adapters_before[adapter_id].get(field),
            "after": adapters_after[adapter_id].get(field),
        }
        for adapter_id in shared
        for field in _ADAPTER_CHURN_FIELDS
        if adapters_before[adapter_id].get(field)
        != adapters_after[adapter_id].get(field)
    ]
    intent_changes = [
        {
            "id": adapter_id,
            "field": field,
            "before": adapters_before[adapter_id].get(field),
            "after": adapters_after[adapter_id].get(field),
        }
        for adapter_id in shared
        for field in _ADAPTER_INTENT_FIELDS
        if adapters_before[adapter_id].get(field)
        != adapters_after[adapter_id].get(field)
    ]
    reconnected = sorted(
        adapter_id
        for adapter_id in shared
        if adapters_before[adapter_id].get("pid")
        != adapters_after[adapter_id].get("pid")
    )
    return {
        "identity_keys": ["pid", "epoch"],
        "identity_unchanged": all(
            before.get(key) == after.get(key) for key in ("pid", "epoch")
        ),
        "pid_before": before.get("pid"),
        "pid_after": after.get("pid"),
        "epoch_before": before.get("epoch"),
        "epoch_after": after.get("epoch"),
        "adapters_changed": bool(changes)
        or bool(intent_changes)
        or adapters_before.keys() != adapters_after.keys(),
        "adapter_changes": changes,
        "intent_changes": intent_changes,
        "adapters_reconnected": reconnected,
        "adapters_added": sorted(adapters_after.keys() - adapters_before.keys()),
        "adapters_removed": sorted(adapters_before.keys() - adapters_after.keys()),
        "configuration_unchanged": adapters_before.keys() == adapters_after.keys()
        and not intent_changes,
    }


def production_unchanged(before, after):
    """True when the daemon identity AND the adapter configuration survived."""
    identity = production_identity(before, after)
    return identity["identity_unchanged"] and identity["configuration_unchanged"]


def private_daemon_environment(root, repo):
    home = root / "home"
    home.mkdir(mode=0o700)
    (root / "tmp").mkdir()
    prepare_payload(root)
    # Restrict the synthetic provider to guest loopback via the real setting.
    (home / "settings.json").write_text(
        json.dumps(
            {
                "workerProxy": {
                    "url": "http://127.0.0.1:1",
                    "vendors": ["deepseek"],
                    "noProxy": "",
                }
            }
        )
    )
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
        "HOME": str(home),
        "TMPDIR": str(root / "tmp"),
        "HYPRIAL_HOME": str(home),
        "HARNESS_STATE_DIR": str(home / "state"),
        "HYPRIAL_NETWORK_ISOLATED": "1",
        "HYPRIAL_PEER_DISCOVERY": "0",
        # The daemon is launched as a script (not `-m`), so sys.path[0] is the
        # fixture directory, not the repo root; the fixture's own support
        # import `tests.fixtures.smolvm.smolvm_model_evidence` therefore needs
        # the repo root explicitly on PYTHONPATH. `src` keeps the daemon on
        # this checkout's source instead of any ambient editable install.
        "PYTHONPATH": os.pathsep.join((str(repo), str(repo / "src"))),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    for name in ("CONFIG", "CACHE", "DATA", "STATE"):
        path = root / ("xdg-" + name.lower())
        path.mkdir()
        env[f"XDG_{name}_HOME"] = str(path)
    return env


def start_isolated_daemon(root, repo, generation, env, processes, streams):
    from tests.daemon.workers.test_worker_relay import request, frame

    log = (root / f"daemon-{generation}.log").open("wb")
    streams.append(log)
    process = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), str(root), generation],
        cwd=repo,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=log,
        stderr=log,
        start_new_session=True,
    )
    processes.append(process)

    def ready():
        assert process.poll() is None, (
            f"Daemon exited {process.returncode}; see daemon-{generation}.log"
        )
        try:
            result = request(root / "home/state/daemon.sock", frame("ping"))["result"]
            if result["pid"] != process.pid:
                return None
            if result.get("phase") == "restoring" or result.get("restorePending"):
                return None
            return result
        except (OSError, KeyError, ValueError):
            return None

    return process, wait_for(ready, f"{generation} daemon ready")


def stop_isolated_daemons(processes, streams):
    for process in processes:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=60)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
    for stream in streams:
        stream.close()


def start_managed_worker(root, repo, env, spec_path=None, *, name="probe"):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hyprial.cli",
            "start",
            "codex",
            "--headless",
            "--name",
            name,
            "--provider",
            "deepseek",
            "--model",
            "deepseek-flash",
            *(["--smolvm-spec", str(spec_path)] if spec_path else []),
            "--json",
        ],
        cwd=repo,
        env=env,
        capture_output=True,
        timeout=90,
    )
    (root / "cli-start.json").write_bytes(result.stdout)
    (root / "cli-start.stderr").write_bytes(result.stderr)
    return result


def events(root, generation):
    path = root / f"events-{generation}.jsonl"
    return (
        [json.loads(line) for line in path.read_text().splitlines()]
        if path.exists()
        else []
    )


def wait_for(check, label, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.2)
    raise AssertionError(f"Timed out: {label}")


def wait_for_session_ref(
    state_dir, expected, *, name="probe", harness="codex", timeout=60
):
    """Observe the committed desired ref before stopping its live native thread."""
    if not expected:
        raise ValueError("A native thread ID is required")

    def committed():
        with closing(
            sqlite3.connect(
                f"file:{state_dir / 'lifecycle-operations.sqlite3'}?mode=ro", uri=True
            )
        ) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(
                "SELECT * FROM harnesses WHERE harness=? AND name=?",
                (harness, name),
            ).fetchone()
            return (
                dict(row)
                if row is not None and row["session_ref"] == expected
                else None
            )

    return wait_for(committed, f"committed session_ref for {harness}:{name}", timeout)


def live(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def emergency_cleanup(runtime, state_dir, spec, *, actor="agent:test:isolated:probe"):
    """Failure hygiene only; never counted as daemon cleanup passing."""
    marker = runtime / "owner.json"
    if not marker.exists():
        return False
    with (runtime / "runtime.lock").open("r+") as lease:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        owner = json.loads(_regular_read(marker))
        assert owner["actor"] == actor
        assert owner["stateDir"] == str(state_dir) and owner["spec"] == spec.to_json()
        cleaner = object.__new__(SmolvmWorkerRuntime)
        cleaner.spec, cleaner.output = spec, runtime
        cleaner._env = _private_environment(runtime, reuse=True)
        cleaner._env["SMOLVM_AGENT_ROOTFS"] = str(runtime / "engine/agent-rootfs")
        cleaner._clean_engine()
        relay = runtime / "relay"
        if relay.exists():
            assert not relay.is_symlink() and set(p.name for p in relay.iterdir()) <= {
                "daemon.sock"
            }
            (relay / "daemon.sock").unlink(missing_ok=True)
            relay.rmdir()
        marker.unlink()
        (state_dir / "smolvm-runtimes" / runtime.name).unlink(missing_ok=True)
    return True


def main() -> None:
    root = Path(sys.argv[1]).resolve(strict=True)
    evidence = root / ("events-" + sys.argv[2] + ".jsonl")
    home = root / "home"
    assert Path(os.environ["HOME"]).resolve() == home
    assert Path(os.environ["HYPRIAL_HOME"]).resolve() == home
    assert os.environ["HYPRIAL_NETWORK_ISOLATED"] == "1"
    lock = threading.Lock()

    def emit(event):
        with lock, evidence.open("a") as stream:
            stream.write(json.dumps(dict(time=time.time(), **event)) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    if __package__:
        from .smolvm_model_evidence import (
            install_model_observers,
            model_authorization,
            read_authorized_key,
        )
    else:
        from tests.fixtures.smolvm.smolvm_model_evidence import install_model_observers, model_authorization, read_authorized_key

    install_model_observers(emit)
    authorization_path = os.environ.get("AT08_MODEL_AUTHORIZATION")
    authorization = (
        model_authorization(Path(authorization_path)) if authorization_path else None
    )
    from hyprial.kernel import parse_agent_uri

    owner, node, name = (
        parse_agent_uri(authorization["actor"])
        if authorization
        else ("test", "isolated", "probe")
    )

    original_request = CodexAppServerClient.request
    original_provider = codex.codex_provider_configuration

    def observe_provider(spec, environment, **kwargs):
        try:
            return original_provider(spec, environment, **kwargs)
        except ModelProviderError as error:
            emit(
                {
                    "kind": "provider-rejected",
                    "errorType": type(error).__name__,
                    "agentKeyPresent": bool(environment.get("DEEPSEEK_API_KEY")),
                    "legacyFallbackAllowed": kwargs.get(
                        "allow_legacy_home_fallback", True
                    ),
                }
            )
            raise

    async def observe_request(client, method, params=None, **kwargs):
        event = {"kind": "protocol", "method": method}
        if isinstance(params, dict) and "threadId" in params:
            event["threadId"] = params["threadId"]
        if method == "turn/start":
            if authorization is None:
                emit(dict(event, prohibited=True))
                raise AssertionError("No model turn is authorized in this fixture")
            model_authorization(Path(authorization_path))
            event["input"] = params["input"]
            # One native turn attempt per daemon generation, persisted before
            # forwarding. Reconnect/restart may not silently repeat billable work.
            attempt = root / f"turn-attempt-{sys.argv[2]}.json"
            with attempt.open("x") as stream:
                json.dump({"method": method, "threadId": params["threadId"]}, stream)
                stream.flush()
                os.fsync(stream.fileno())
        try:
            result = await original_request(client, method, params, **kwargs)
        except Exception as error:
            emit(dict(event, errorType=type(error).__name__))
            raise
        if isinstance(result, dict) and isinstance(result.get("thread"), dict):
            event["returnedThreadId"] = result["thread"].get("id")
        if method == "thread/start":
            raw = await asyncio.to_thread(
                client._execution_runtime._admin,
                "machine",
                "exec",
                "--name",
                "worker",
                "--timeout",
                "10s",
                "--",
                "/bin/sh",
                "-c",
                'for f in /proc/[0-9]*/comm; do if [ "$(cat "$f" 2>/dev/null)" = codex ]; then '
                'p=${f%/comm}; printf "%s " "${p##*/}"; readlink "$p/exe"; fi; done',
                timeout=15,
            )
            event["guestCodexProcesses"] = raw.decode().splitlines()
        if method in {"thread/start", "thread/resume"}:
            binding = client._execution_runtime.binding
            event["binding"] = {
                "actor": binding.actor,
                "sessionRef": binding.session_ref,
                "daemonEpoch": binding.daemon_epoch,
            }
        emit(dict(event, ok=True))
        return result

    original_admin = SmolvmWorkerRuntime._admin
    admin_sequence = itertools.count(1)

    def observe_admin(runtime, *args, **kwargs):
        previous = root / "previous-managers.json"
        if args[:2] == ("machine", "create") and previous.exists():
            live = []
            for pid in json.loads(previous.read_text()):
                try:
                    os.kill(pid, 0)
                    live.append(pid)
                except ProcessLookupError:
                    pass
            emit(
                {
                    "kind": "before-create",
                    "previousLiveManagers": live,
                    "owner": json.loads((runtime.output / "owner.json").read_text()),
                    "pointer": json.loads(runtime._active_record.read_text()),
                }
            )
            assert not live, "Prior VM survived into replacement create"
        # Administrative operations contain no credentials; still avoid dumping
        # arbitrary guest output or environment into this compact observation log.
        event = {
            "kind": "admin",
            "operation": list(args[:2]),
            "output": str(runtime.output),
        }
        try:
            result = original_admin(runtime, *args, **kwargs)
        except Exception as error:
            emit(dict(event, errorType=type(error).__name__))
            raise
        raw_path = root / f"admin-{sys.argv[2]}-{next(admin_sequence):02}.stdout"
        raw_path.write_bytes(result)
        event["stdoutFile"] = raw_path.name
        event["managerPids"] = [
            int(p) for p in re.findall(rb"running \(PID: (\d+)\)", result)
        ]
        if args[:2] == ("machine", "ls"):
            event["machines"] = result.decode().splitlines()
        emit(dict(event, ok=True))
        return result

    CodexAppServerClient.request = observe_request
    codex.codex_provider_configuration = observe_provider
    SmolvmWorkerRuntime._admin = observe_admin
    app = DaemonApplication(
        state_dir=home / "state",
        socket_path=home / "state/daemon.sock",
        hyprial_home=home,
        node_id=node,
        owner=owner,
        zenoh_listen=("tcp/127.0.0.1:0",),
        zenoh_connect=(),
    **_deep_module_daemon_dependencies())
    app.network_isolated = True
    app.owns_process_exit()
    if sys.argv[2] == "first":
        app.agents.create(
            name,
            cwd=str(root / "workspace"),
            # dev's create port freezes config as JSON (ports.CreateAgentCommand),
            # so hand it the serialized shape the IPC path would carry.
            config=AgentConfig(str(root / "C")).to_json(),
        )
        app._agent_registry.ensure_home(name)
        store = AgentSecretStore(Path(app._agent_registry.home_receipt(name).path))
        entry = "authorized-model" if authorization else "synthetic-provider"
        if authorization:
            receipt = app._agent_registry.home_receipt(name)
            (root / "credential-location.json").write_text(
                json.dumps(
                    {
                        "path": str(Path(receipt.path) / "secrets.sqlite"),
                        "actor": authorization["actor"],
                        "grantId": entry,
                    }
                )
            )
        key = (
            read_authorized_key(authorization)
            if authorization
            else "SYNTHETIC-NOT-A-CREDENTIAL"
        )
        store.set(
            service=entry, field="DEEPSEEK_API_KEY", value=key,
            actor="user:owner", via="cli", at_ms=1,
        )
        del key
        if (
            authorization
            or os.environ.get("AT08_SYNTHETIC_GRANT", "present") == "present"
        ):
            app._agent_registry.grant_secret(
                name,
                grant_id=entry,
                source=SecretSource.AGENT_PRIVATE,
                entry_id=entry,
                field_name="DEEPSEEK_API_KEY",
                environment_names=("DEEPSEEK_API_KEY",),
                revision=1,
            )
    emit({"kind": "daemon", "pid": os.getpid(), "epoch": app.epoch})
    app.run()


if __name__ == "__main__":
    main()
