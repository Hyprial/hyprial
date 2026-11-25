#!/usr/bin/env python3
"""REAL E2E: managed Codex app-server roundtrip, recovery, and interrupt."""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# Stand on its own when loaded by path (spec_from_file_location in the CI
# baseline tests), not only when run as a script: the sibling helper module
# lives next to this file.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _daemon_runner import ROOT, hyprial_executable, short_temp_root
from _runner_shared.ps import ps_from_env
from _deepseek import (
    MODEL as DEEPSEEK_MODEL,
    PROVIDER as DEEPSEEK_PROVIDER,
    DeepSeekUnavailable,
    deepseek_environment,
    selection_args,
)

sys.path.insert(0, str(ROOT / "src"))
from hyprial.daemon.impl.api import HarnessDelivery, HarnessResultStatus
from hyprial.kernel import HarnessLaunchSpec
from hyprial.daemon.impl.harnesses.codex.client import CodexAppServerProcess


CODEX = Path(
    os.environ.get("HYPRIAL_CODEX_BIN")
    or shutil.which("codex")
    or "codex"
)


# CLI probe-sender persona (Allen b: resolve-or-reject).
DRIVER = "codex-as-driver"


def result(status: str, check: str, detail: str) -> None:
    print(f"RESULT {status} CODEX-APP-SERVER {check} {' '.join(detail.split())}", flush=True)


def cli(executable: Path, env: dict[str, str], *args: str) -> dict[str, object]:
    completed = subprocess.run(
        [str(executable), *args], env=env, text=True, capture_output=True,
        timeout=45, check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(f"hyprial {' '.join(args)} failed: {completed.stderr[-1000:]}")
    value = json.loads(completed.stdout)
    if not isinstance(value, dict):
        raise AssertionError("hyprial CLI returned a non-object")
    return value


def wait_result(process: CodexAppServerProcess, timeout: float = 180) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        values = process.drain_results()
        if values:
            return values[0]
        time.sleep(0.1)
    raise AssertionError("timed out waiting for Codex result")


def delivery(identifier: str, message: str) -> HarnessDelivery:
    return HarnessDelivery(
        identifier, "codex-real-e2e", "sender", "agent:owner:node:codex-real", message
    )


def process_rows() -> list[tuple[int, int, str]]:
    completed = subprocess.run(
        ["ps", "-ww", "-axo", "pid=,ppid=,command="], text=True,
        capture_output=True, timeout=10, check=True,
    )
    rows = []
    for line in completed.stdout.splitlines():
        fields = line.strip().split(maxsplit=2)
        if len(fields) == 3:
            try:
                rows.append((int(fields[0]), int(fields[1]), fields[2]))
            except ValueError:
                pass
    return rows


def descendants(parent: int) -> list[tuple[int, str]]:
    rows = process_rows()
    owned = {parent}
    changed = True
    while changed:
        changed = False
        for pid, ppid, _ in rows:
            if ppid in owned and pid not in owned:
                owned.add(pid)
                changed = True
    return [(pid, command) for pid, _, command in rows if pid in owned and pid != parent]


def main() -> int:
    executable = hyprial_executable()
    version = subprocess.run(
        [str(CODEX), "--version"], text=True, capture_output=True, timeout=10, check=False
    ).stdout.strip()
    try:
        provider_environment = deepseek_environment()
    except DeepSeekUnavailable as error:
        result("BLOCKED", "preflight", str(error))
        return 2
    result(
        "PASS",
        "preflight",
        f"binary={CODEX.resolve()} version={version} "
        f"provider={DEEPSEEK_PROVIDER} model={DEEPSEEK_MODEL}",
    )

    # A short root keeps state/daemon.sock under the AF_UNIX limit even after
    # macOS resolves /var/folders/... to /private/var/folders/....
    with tempfile.TemporaryDirectory(
        prefix="hyprial-codex-as-", dir=short_temp_root()
    ) as temporary:
        root = Path(temporary)
        # Only `hyprial init` creates a home in the product; the runner creates
        # its disposable home explicitly, like a fixture.
        (root / "home").mkdir(mode=0o700)
        env = dict(os.environ)
        env.update(
            HYPRIAL_HOME=str(root / "home"), HARNESS_STATE_DIR=str(root / "state"),
            HYPRIAL_NODE_ID="codex-app-server-e2e",
            HYPRIAL_NETWORK_ISOLATED="1",
        )
        env.update(provider_environment)
        env.pop("HARNESS_SOCKET_PATH", None)
        daemon = subprocess.Popen(
            [str(executable), "daemon", "run", "--json"], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            socket = root / "state" / "daemon.sock"
            deadline = time.monotonic() + 15
            while not socket.exists() and time.monotonic() < deadline:
                if daemon.poll() is not None:
                    out, err = daemon.communicate(timeout=5)
                    raise AssertionError(
                        "isolated daemon exited during startup: "
                        f"stdout={out[-500:]!r} stderr={err[-500:]!r}"
                    )
                time.sleep(0.1)
            # resolve-or-reject (Allen b): the CLI probe sender must be a
            # registered identity — no more bare node id as sender.
            cli(executable, env, "agent", "create", "--name", DRIVER, "--json")
            started = cli(
                executable, env, "agent", "start", "codex", "--name", "codex-real",
                "--nickname", "codex-real", "--cwd", str(root), "--headless",
                *selection_args(), "--json", "--",
                "-a", "never", "-s", "workspace-write",
            )
            status = ps_from_env(env)
            connectors = status.get("connectors")
            if started.get("ok") is not True or not isinstance(connectors, list) or not any(
                isinstance(item, dict) and item.get("id") == "codex:codex-real"
                and item.get("running") is True for item in connectors
            ):
                raise AssertionError("hyprial agent start did not register a ready Codex actor")
            result("PASS", "a.start-register", "managed default is ready app-server")

            conversation = f"codex-e2e-{time.time_ns()}"
            sent = cli(
                executable, env, "message", "send", "--from", DRIVER,
                "--to", "codex-real", "--conversation", conversation,
                "--summary", "Reply exactly HYPRIAL_CODEX_DAEMON_OK.",
                "Reply exactly HYPRIAL_CODEX_DAEMON_OK.", "--json",
            )
            request_id = sent.get("messageId")
            deadline = time.monotonic() + 180
            reply = None
            while time.monotonic() < deadline:
                pending = ps_from_env(env).get("pendingMessages", [])
                reply = next(
                    (item for item in pending if isinstance(item, dict)
                     and item.get("conversationId") == conversation
                     and item.get("messageId") != request_id), None
                )
                if reply is not None:
                    break
                time.sleep(0.5)
            if reply is None:
                raise AssertionError("no correlated Codex daemon reply")
            cli(executable, env, "message", "ack", "--from", DRIVER, str(reply["messageId"]), "--json")
            result("PASS", "b.roundtrip", "dispatch received; correlated reply acknowledged")
        finally:
            try:
                cli(executable, env, "agent", "stop", "--all", "--json")
                cli(executable, env, "daemon", "stop", "--json")
            except Exception:
                daemon.send_signal(signal.SIGTERM)
            try:
                daemon.wait(timeout=15)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait()

        spec = HarnessLaunchSpec(
            harness="codex", name="codex-real", headless=True, cwd=str(root),
            args=("-a", "never", "-s", "workspace-write"),
            model_provider=DEEPSEEK_PROVIDER,
            model=DEEPSEEK_MODEL,
        )
        process = CodexAppServerProcess(
            spec,
            command=(str(CODEX.resolve()),),
            env=provider_environment,
        )
        try:
            if not process.wait_ready(timeout=30):
                raise AssertionError(process.last_error or "app-server not ready")
            original_thread = process.session_ref
            process.enqueue(delivery("memory-1", "Remember nonce COBALT_741. Reply exactly MEMORY_SET."))
            first = wait_result(process)
            child = process._client  # noqa: SLF001 - deliberate crash-boundary E2E
            native = getattr(child, "_process", None)
            if first.status is not HarnessResultStatus.COMPLETED or native is None:
                raise AssertionError("first real turn did not complete")
            os.killpg(native.pid, signal.SIGKILL)
            process.enqueue(delivery("memory-2", "Reply with only the nonce I asked you to remember."))
            recovered = wait_result(process)
            if recovered.status is not HarnessResultStatus.COMPLETED or "COBALT_741" not in recovered.output:
                raise AssertionError("reconnected Codex lost thread context")
            if process.session_ref != original_thread:
                raise AssertionError("child reconnect changed native thread")
            result("PASS", "c.child-resume", "SIGKILL recovered same thread with context")

            process.enqueue(delivery("interrupt", "Use the shell tool to run sleep 120, then reply SLEEP_DONE."))
            deadline = time.monotonic() + 60
            while not process.interrupt("interrupt", timeout=5):
                if time.monotonic() >= deadline:
                    raise AssertionError("long turn never became interruptible")
                time.sleep(0.1)
            interrupted = wait_result(process, 60)
            process.enqueue(delivery("after-interrupt", "Reply exactly AFTER_INTERRUPT_OK."))
            after = wait_result(process)
            if interrupted.status is not HarnessResultStatus.INTERRUPTED:
                raise AssertionError("turn did not terminate as interrupted")
            if after.status is not HarnessResultStatus.COMPLETED or "AFTER_INTERRUPT_OK" not in after.output:
                raise AssertionError("post-interrupt delivery failed")
            zombies = [command for _, command in descendants(os.getpid()) if "sleep 120" in command]
            if zombies:
                raise AssertionError("sleep child survived interrupt")
            result("PASS", "d.interrupt", "long turn stopped; no sleep child; next delivery completed")
        finally:
            process.stop()

        approval_spec = HarnessLaunchSpec(
            harness="codex", name="codex-approval", headless=True, cwd=str(root),
            args=("-a", "untrusted", "-s", "workspace-write"),
            model_provider=DEEPSEEK_PROVIDER,
            model=DEEPSEEK_MODEL,
        )
        approval = CodexAppServerProcess(
            approval_spec,
            command=(str(CODEX.resolve()),),
            env=provider_environment,
        )
        try:
            if not approval.wait_ready(timeout=30):
                raise AssertionError(approval.last_error or "approval probe not ready")
            approval.enqueue(
                delivery(
                    "approval",
                    "Use the shell tool to run `python -c \"print('APPROVAL_PROBE')\"`. If approval is denied, reply exactly APPROVAL_DECLINED.",
                )
            )
            approval_result = wait_result(approval)
            if approval_result.status is not HarnessResultStatus.COMPLETED:
                raise AssertionError("approval denial stalled or failed the turn")
            if "item/commandExecution/requestApproval" not in approval.server_request_methods:
                raise AssertionError("real Codex did not exercise command approval")
            result("PASS", "approval", "real command request declined fail-closed; turn completed")
        finally:
            approval.stop()

    result("PASS", "summary", "5/5 real Codex app-server gates passed; isolated roots cleaned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
