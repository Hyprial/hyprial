"""Isolated real dsh-tui interactive carrier acceptance runner.

This runner is intentionally gated: a PTY/tmux-capable executor and an isolated
Messages-compatible test provider are required. Missing prerequisites are
reported as ``RESULT BLOCKED``; they are never turned into a skip or a PASS.

Output contract: one ``STAGE=<name> <status>`` line per stage and one final
``RESULT PASS|FAIL|BLOCKED <scenario> <detail>`` line.
Exit codes: 0 PASS, 1 FAIL, 2 BLOCKED.

``--mock-provider`` starts ``contract/e2e/_messages_tool_mock.py`` on loopback
with a synthetic key, so the wake turn really calls ``harness_read`` and
``harness_reply`` without any provider credential. ``--evidence-dir`` (outside
the checkout) receives pane captures, the mock's request log, the carrier
worker log and the process tree, on PASS and on FAIL.

``/mcp`` must list ``harness-bridge`` (dsh-tui builds that view from the same
tool runtime the model is offered), and ``mcp-tool-called`` additionally
requires that the mock saw the model call ``harness_read`` and
``harness_reply``. ``--fail-first-wake`` (mock only) answers the first wake
turn with HTTP 400 and requires a ``wake.failed`` naming ``TURN_ERROR`` before
the retried wake succeeds: a rejected turn must never be logged as signalled.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "contract" / "e2e"))
from _daemon_runner import (  # noqa: E402
    IsolatedDaemon,
    hyprial_executable,
    scenario_tempdir,
    short_temp_root,
)
from _messages_tool_mock import (  # noqa: E402
    HARNESS_READ,
    HARNESS_REPLY,
    ScriptedMessagesMock,
)

SCENARIO = "dsh-tui-interactive"


class Blocked(RuntimeError):
    pass


class Failed(RuntimeError):
    pass


def stage(name: str, status: str, detail: str = "") -> None:
    suffix = f" {detail}" if detail else ""
    print(f"STAGE={name} {status}{suffix}", flush=True)


def run(
    args: list[str], *, env: dict[str, str] | None = None, timeout: float = 60.0
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        env=env,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )


def wait_until(probe: Any, accept: Any, *, timeout: float, label: str) -> Any:
    deadline = time.monotonic() + timeout
    last: Any = None
    while time.monotonic() < deadline:
        try:
            last = probe()
        except Exception as error:  # noqa: BLE001 - runner reports last probe
            last = f"{type(error).__name__}: {error}"
        if accept(last):
            return last
        time.sleep(0.25)
    raise Failed(f"timeout waiting for {label}; last={str(last)[:800]!r}")


def tmux(args: list[str], *, timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return run(["tmux", *args], timeout=timeout)


def tmux_alive(socket_name: str, session: str) -> bool:
    result = tmux(["-L", socket_name, "has-session", "-t", session])
    return result.returncode == 0


def pane(socket_name: str, session: str) -> str:
    result = tmux(["-L", socket_name, "capture-pane", "-p", "-S", "-500", "-t", session])
    return result.stdout if result.returncode == 0 else result.stderr


def send_keys(socket_name: str, session: str, text: str, *, enter: bool = False) -> None:
    # Literal mode prevents tmux from interpreting prompt characters as keys.
    result = tmux(["-L", socket_name, "send-keys", "-t", session, "-l", text])
    if result.returncode != 0:
        raise Failed(f"tmux send-keys failed: {result.stderr[-500:]}")
    if enter:
        result = tmux(["-L", socket_name, "send-keys", "-t", session, "Enter"])
        if result.returncode != 0:
            raise Failed(f"tmux Enter failed: {result.stderr[-500:]}")


def stop_tmux(socket_name: str, session: str) -> None:
    if tmux_alive(socket_name, session):
        tmux(["-L", socket_name, "send-keys", "-t", session, "C-c"])
        time.sleep(0.2)
    tmux(["-L", socket_name, "kill-session", "-t", session])
    tmux(["-L", socket_name, "kill-server"])


def daemon_session(daemon: IsolatedDaemon, actor: str) -> dict[str, Any] | None:
    status = daemon.command("daemon", "status", "--json", transient_timeout=5.0)
    sessions = status.get("interactiveSessions")
    if not isinstance(sessions, list):
        raise Failed(f"ps interactiveSessions is invalid: {status}")
    for item in sessions:
        if not isinstance(item, dict):
            continue
        # The launcher is given a bare name; the daemon reports the actor URI.
        value = str(item.get("actor") or "")
        if value == actor or value.endswith(f":{actor}"):
            return item
    return None


def agent_list(daemon: IsolatedDaemon) -> list[dict[str, Any]]:
    value = daemon.command("agent", "list", "--json", transient_timeout=5.0)
    agents = value.get("agents")
    if not isinstance(agents, list):
        raise Failed(f"agent list returned no agents: {value}")
    return [item for item in agents if isinstance(item, dict)]


def agent_uri(daemon: IsolatedDaemon, name: str) -> str:
    def find() -> str | None:
        for item in agent_list(daemon):
            candidate = item.get("actor") or item.get("name")
            if candidate == name and isinstance(item.get("uri"), str):
                return item["uri"]
        return None

    uri = wait_until(find, lambda value: isinstance(value, str), timeout=20, label=f"agent {name}")
    assert isinstance(uri, str)
    return uri


def _stop_scenario(
    socket_name: str, session: str, daemon: IsolatedDaemon | None
) -> None:
    """Tear the daemon and tmux down before the scenario tempdir is removed.

    TemporaryDirectory cleanup used to run while the daemon still wrote into
    the root, so it raised ``ENOTEMPTY`` and replaced the real failure with a
    cleanup error.
    """

    try:
        stop_tmux(socket_name, session)
    except Exception:
        pass
    if daemon is None:
        return
    try:
        daemon.stop()
    except Exception:
        pass
    try:
        daemon.wait_stopped()
    except Exception:
        pass


def carrier_pids(control_socket: Path) -> list[int]:
    needle = f"--control-socket {control_socket}"
    ps = run(["ps", "-axo", "pid=,command="], timeout=20)
    pids: list[int] = []
    for line in ps.stdout.splitlines():
        value = line.strip()
        if needle not in value:
            continue
        head, _, _ = value.partition(" ")
        try:
            pids.append(int(head))
        except ValueError:
            continue
    return pids


def pending_reply(daemon: IsolatedDaemon, sender: str, marker: str) -> str | None:
    try:
        value = daemon.command("message", "inbox", sender, "inbox", "--json", transient_timeout=3.0)
    except Exception:
        return None
    entries = value.get("entries")
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        message = entry.get("message")
        if isinstance(message, str) and marker in message:
            return message
    return None


def preflight(*, mock_provider: bool) -> dict[str, str]:
    try:
        hyprial = str(hyprial_executable())
    except AssertionError as error:
        raise Blocked(str(error)) from error
    checks = {
        "hyprial": hyprial,
        "tmux": shutil.which("tmux") or "",
        "dsh-tui": shutil.which("dsh-tui") or "",
        "dsh": shutil.which("dsh") or "",
        "node": shutil.which("node") or "",
    }
    missing = [name for name, value in checks.items() if not value]
    if missing:
        raise Blocked("missing executable(s): " + ", ".join(missing))

    seed = os.environ.get("HYPRIAL_DSH_TUI_E2E_DSH_HOME_SEED", "")
    if not seed:
        raise Blocked(
            "HYPRIAL_DSH_TUI_E2E_DSH_HOME_SEED must point to an isolated "
            "DSH_HOME template with profiles/dsh-tui installed (no secrets)"
        )
    seed_path = Path(seed).expanduser()
    if not (seed_path / "profiles" / "dsh-tui").is_dir():
        raise Blocked(f"DSH_HOME seed lacks profiles/dsh-tui: {seed_path}")

    base_url = os.environ.get("HYPRIAL_DSH_TUI_E2E_PROVIDER_BASE_URL", "")
    api_key = os.environ.get("HYPRIAL_DSH_TUI_E2E_PROVIDER_API_KEY", "")
    if mock_provider:
        base_url, api_key = "mock", MOCK_API_KEY
    if not base_url or not api_key:
        raise Blocked(
            "HYPRIAL_DSH_TUI_E2E_PROVIDER_BASE_URL and "
            "HYPRIAL_DSH_TUI_E2E_PROVIDER_API_KEY are required unless "
            "--mock-provider is given; use the scripted tool-call mock or an "
            "explicitly authorized test key"
        )

    # tmux may exist while the executor forbids fork/PTY allocation. Prove the
    # primitive before declaring the environment runnable.
    probe_socket = f"dsh-e2e-probe-{uuid4().hex[:8]}"
    probe = run(
        ["tmux", "-L", probe_socket, "new-session", "-d", "-s", "probe", "true"],
        timeout=15,
    )
    run(["tmux", "-L", probe_socket, "kill-server"], timeout=10)
    if probe.returncode != 0:
        raise Blocked(
            "tmux cannot create an isolated PTY session in this executor: "
            + (probe.stderr or probe.stdout).strip()[-500:]
        )
    return {**checks, "seed": str(seed_path.resolve()), "base_url": base_url, "api_key": api_key}


#: tmux session variable that carries the provider key to the wrapper.
PROVIDER_KEY_VARIABLE = "DSH_TUI_E2E_PROVIDER_KEY"

#: Synthetic, non-credential key handed to dsh when the scripted mock answers.
MOCK_API_KEY = "synthetic-not-a-credential"

#: The only host variables the launched TUI may inherit. Everything else
#: (agent tokens, XDG roots, proxies, CI markers) stays out of `run_tui.sh`:
#: the wrapper used to export the whole sanitized host environment, which put
#: real credential names and values on disk and into failure artifacts.
_TUI_ENV_ALLOWLIST = (
    "HOME",
    "PATH",
    "TMPDIR",
    "LANG",
    "LC_ALL",
    "DSH_HOME",
    "HYPRIAL_HOME",
    "HYPRIAL_OWNER",
    "HYPRIAL_NODE_ID",
    "HYPRIAL_NETWORK_ISOLATED",
    "HYPRIAL_PEER_DISCOVERY",
    "HYPRIAL_ZENOH_LISTEN",
    "HYPRIAL_ZENOH_CONNECT",
    "HARNESS_STATE_DIR",
    "HARNESS_SOCKET_PATH",
    "PYTHONPATH",
)


def write_tui_wrapper(
    path: Path,
    *,
    executable: str,
    actor: str,
    work: Path,
    env: dict[str, str],
) -> None:
    assignments = {name: env[name] for name in _TUI_ENV_ALLOWLIST if name in env}
    assignments.update(
        {
            "PWD": str(work),
            "TERM": "xterm-256color",
            "DSH_TUI_LANG": "en",
            "HARNESS_DSH_TUI_BIN": env["DSH_TUI_BIN"],
            "DEEPSEEK_BASE_URL": env["PROVIDER_BASE_URL"],
        }
    )
    # `env -i`: the tmux server was forked from the runner and carries the
    # host environment; exporting the allowlist on top of it would still let
    # every other host variable reach the launcher, the carrier and dsh.
    command = ["exec", "env", "-i"]
    for name, value in sorted(assignments.items()):
        command.append(shlex.quote(f"{name}={value}"))
    # The provider key is never written into the wrapper: tmux hands it to
    # this shell as a session variable and the shell expands it at exec time.
    command.append(f'DEEPSEEK_API_KEY="${{{PROVIDER_KEY_VARIABLE}}}"')
    command.extend(
        [
            shlex.quote(executable),
            "agent",
            "start",
            "dsh",
            "--name",
            shlex.quote(actor),
            "--cwd",
            shlex.quote(str(work)),
        ]
    )
    lines = ["set -eu", f"cd {shlex.quote(str(work))}", " ".join(command)]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o700)


def evidence_directory(value: str | None) -> Path | None:
    """Resolve ``--evidence-dir``; never inside the checkout."""

    if not value:
        return None
    path = Path(value).expanduser().resolve()
    if path == ROOT or ROOT in path.parents:
        raise Blocked(f"--evidence-dir must be outside the checkout: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


class Evidence:
    """Pane captures and run facts copied out of the disposable root."""

    def __init__(self, directory: Path | None, socket_name: str, session: str) -> None:
        self.directory = directory
        self.socket_name = socket_name
        self.session = session

    def write(self, name: str, text: str) -> None:
        if self.directory is not None:
            (self.directory / name).write_text(text, encoding="utf-8")

    def pane(self, tag: str) -> str:
        text = pane(self.socket_name, self.session)
        self.write(f"pane-{tag}.txt", text)
        return text

    def process_tree(self, tag: str, needles: tuple[str, ...]) -> None:
        listing = run(["ps", "-axo", "pid=,ppid=,pgid=,stat=,command="], timeout=20)
        rows = [
            line
            for line in listing.stdout.splitlines()
            if any(needle in line for needle in needles)
        ]
        self.write(f"ps-{tag}.txt", "\n".join(rows) + "\n")

    def copy_logs(self, node_root: Path, home: Path) -> None:
        if self.directory is None:
            return
        workers = node_root / "state" / "logs" / "workers"
        for path in sorted(workers.glob("dsh-*.jsonl")):
            shutil.copyfile(path, self.directory / f"worker-{path.name}")
        crash = home / ".dsh-tui" / "crash.log"
        if crash.is_file():
            shutil.copyfile(crash, self.directory / "dsh-tui-crash.log")


def assert_no_first_run_guide(evidence: Evidence, socket_name: str, session: str) -> str:
    """The workspace-start guard: a fresh HOME must open with no guide.

    The launcher owns the first-run screens (design §10.15), so dsh-tui's
    "Welcome to dsh-TUI" wizard at the registered stage is a product
    regression and the run is red. The runner never dismisses it: a manual
    Escape here once made a seeding regression pass as `dismissed after 1
    Esc` (review-tui rejection, 2026-10-19).
    """

    text = pane(socket_name, session)
    if "Welcome to dsh-TUI" in text:
        evidence.pane("first-run-guide")
        raise Failed(
            "dsh-tui's first-run guide is showing on a fresh HOME; the "
            "launcher's workspace-start contract regressed"
        )
    return "no guide"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--reply-timeout", type=float, default=180.0)
    parser.add_argument(
        "--crash-mode",
        choices=("kill", "socket"),
        default="kill",
        help="exercise carrier SIGKILL or injection-socket loss; run both for full acceptance",
    )
    parser.add_argument(
        "--mock-provider",
        action="store_true",
        help="answer dsh from the scripted tool-call Messages mock (no key)",
    )
    parser.add_argument(
        "--fail-first-wake",
        action="store_true",
        help="mock only: reject the first wake turn and require wake.failed + retry",
    )
    parser.add_argument(
        "--evidence-dir",
        help="directory outside the checkout that receives captures on PASS and FAIL",
    )
    args = parser.parse_args()

    try:
        if args.fail_first_wake and not args.mock_provider:
            raise Blocked("--fail-first-wake requires --mock-provider")
        preflight_result = preflight(mock_provider=args.mock_provider)
        evidence_dir = evidence_directory(args.evidence_dir)
    except Blocked as error:
        stage("preflight", "BLOCKED", str(error))
        print(f"RESULT BLOCKED {SCENARIO} {error}")
        return 2
    provider_kind = "scripted-mock" if args.mock_provider else "external"
    stage(
        "preflight",
        "OK",
        f"hyprial+tmux+dsh-tui+dsh+node+seed provider={provider_kind}",
    )

    executable = preflight_result["hyprial"]
    dsh_tui_bin = preflight_result["dsh-tui"]
    seed = Path(preflight_result["seed"])
    provider_api_key = preflight_result["api_key"]
    nonce = uuid4().hex[:10]
    actor = f"dsh-tui-{nonce}"
    sender = f"dsh-sender-{nonce}"
    socket_name = f"dsh-e2e-{nonce}"
    session = f"dsh-e2e-{nonce}"
    marker = f"DSH-E2E-{nonce}"
    evidence = Evidence(evidence_dir, socket_name, session)

    work_root = short_temp_root()
    # Failure evidence must never default into the checkout: the shared
    # default is `<cwd>/e2e-artifacts`, which can commit a run's secrets.
    if not os.environ.get("HYPRIAL_E2E_ARTIFACTS") and not os.environ.get(
        "HYPRIAL_E2E_ROOT"
    ):
        os.environ["HYPRIAL_E2E_ARTIFACTS"] = str(
            work_root / f"dsh-tui-e2e-artifacts-{nonce}"
        )
    daemon: IsolatedDaemon | None = None
    mock: ScriptedMessagesMock | None = None
    try:
        with scenario_tempdir(SCENARIO, directory=work_root) as root:
            node_root = root / "daemon"
            home = root / "home"
            try:
                if args.mock_provider:
                    mock_log = (
                        evidence_dir / "provider-requests.jsonl"
                        if evidence_dir is not None
                        else root / "provider-requests.jsonl"
                    )
                    mock = ScriptedMessagesMock(
                        log_path=mock_log,
                        marker_pattern=r"DSH-E2E-[0-9a-f]+",
                        fail_wakes=1 if args.fail_first_wake else 0,
                    ).__enter__()
                    provider_base_url = mock.base_url
                else:
                    provider_base_url = preflight_result["base_url"]
                dsh_home = root / "dsh"
                shutil.copytree(seed, dsh_home, symlinks=True)
                work = root / "work"
                home.mkdir(mode=0o700)
                work.mkdir(mode=0o700)
                # A genuinely fresh HOME: the launcher itself must mark
                # dsh-tui's first-run guide and one-shot workspace home seen
                # (the workspace-start ruling), so the runner no longer seeds
                # ~/.dsh-tui prefs or presses Escape on a guide. The pane
                # captured at the registered stage is the proof that the TUI
                # opened straight into the workspace chat.
                daemon = IsolatedDaemon(Path(executable), node_root, f"{nonce}-node")
                daemon.env["HOME"] = str(home)
                daemon.env["DSH_HOME"] = str(dsh_home)
                daemon.env["HARNESS_DSH_TUI_BIN"] = dsh_tui_bin
                daemon.start()
                stage("isolated-daemon", "OK", f"home={root}")

                daemon.command("agent", "create", "--name", sender, "--json")
                sender_uri = agent_uri(daemon, sender)
                stage("sender-agent", "OK", sender_uri)

                # Agent-to-agent sends ride the org link; without a composed space
                # every message parks in the outbox (link_hold=LINK_SELF_FULL).
                from _runner_shared import org as _org

                _org.ensure_local_org(daemon, sender_uri, prefix="dsh-tui")
                stage("org-space", "OK", "local org composed")

                wrapper = root / "run_tui.sh"
                tui_env = {
                    **daemon.env,
                    "DSH_TUI_BIN": dsh_tui_bin,
                    "PROVIDER_BASE_URL": provider_base_url,
                }
                write_tui_wrapper(
                    wrapper,
                    executable=executable,
                    actor=actor,
                    work=work,
                    env=tui_env,
                )
                started = run(
                    [
                        "tmux",
                        "-L",
                        socket_name,
                        "new-session",
                        "-d",
                        "-x",
                        "200",
                        "-y",
                        "50",
                        "-s",
                        session,
                        "-c",
                        str(work),
                        f"sh {shlex.quote(str(wrapper))}",
                    ],
                    # A new `-L` server copies this environment into its global
                    # environment, so the key reaches the wrapper without
                    # appearing in any argv (and so in any `ps` capture).
                    env={**os.environ, PROVIDER_KEY_VARIABLE: provider_api_key},
                    timeout=30,
                )
                if started.returncode != 0:
                    raise Failed(f"tmux start failed: {started.stderr[-1000:]}")
                stage("dsh-tui-start", "OK", "tmux session created")

                def registered() -> dict[str, Any] | None:
                    return daemon_session(daemon, actor) if daemon is not None else None

                session_info = wait_until(
                    registered,
                    lambda value: isinstance(value, dict),
                    timeout=args.timeout,
                    label="dsh-channel session registration",
                )
                assert isinstance(session_info, dict)
                if session_info.get("source") != "dsh-channel":
                    raise Failed(f"registered session has wrong source: {session_info}")
                dsh_actor = str(session_info.get("actor"))
                evidence.write("session-info.json", json.dumps(session_info, indent=2))
                evidence.process_tree("registered", (str(root), f"--actor {actor} "))
                stage(
                    "session-register",
                    "OK",
                    f"{dsh_actor} source=dsh-channel processPid={session_info.get('processPid')}",
                )
                stage("onboarding", "OK", assert_no_first_run_guide(evidence, socket_name, session))
                evidence.pane("registered")

                send_keys(socket_name, session, "/mcp", enter=True)
                time.sleep(3.0)
                mcp_pane = evidence.pane("mcp")
                if "harness-bridge" not in mcp_pane:
                    raise Failed("/mcp does not list harness-bridge (see pane-mcp.txt)")
                stage("mcp-tools", "OK", "harness-bridge listed by /mcp")

                draft_marker = f"DRAFT-{marker}"
                send_keys(socket_name, session, draft_marker)
                time.sleep(0.5)
                typed_pane = evidence.pane("draft")
                if draft_marker not in typed_pane:
                    raise Failed("could not observe the draft marker in the TUI input")
                stage("draft-typed", "OK", "partial draft present")

                text = (
                    f"{marker} Harness E2E request. Preserve this marker exactly in "
                    "your reply after calling harness_read."
                )
                sent = daemon.command(
                    "message",
                    "send",
                    "--from",
                    sender_uri,
                    "--to",
                    dsh_actor,
                    "--summary",
                    f"{marker} dsh-tui E2E",
                    text,
                    "--json",
                    transient_timeout=10.0,
                )
                message_id = sent.get("messageId")
                if not isinstance(message_id, str):
                    raise Failed(f"send returned no messageId: {sent}")
                stage("message-sent", "OK", message_id)

                reply = wait_until(
                    lambda: pending_reply(daemon, sender_uri, marker),
                    lambda value: isinstance(value, str),
                    timeout=args.reply_timeout,
                    label="model harness_read + harness_reply",
                )
                assert isinstance(reply, str)
                evidence.pane("after-reply")
                stage("wake-read-reply", "OK", "reply marker received")
                if mock is not None:
                    reads, replies = mock.tool_calls(HARNESS_READ), mock.tool_calls(HARNESS_REPLY)
                    if reads < 1 or replies < 1:
                        raise Failed(
                            f"mock saw harness_read={reads} harness_reply={replies}"
                        )
                    stage(
                        "mcp-tool-called",
                        "OK",
                        f"{HARNESS_READ}={reads} {HARNESS_REPLY}={replies}",
                    )
                else:
                    stage("mcp-tool-called", "OK", "implied by the reply (external provider)")

                after_pane = pane(socket_name, session)
                if draft_marker not in after_pane:
                    raise Failed(
                        "the partial draft disappeared after wake; prompt input was touched"
                    )
                stage("draft-preserved", "OK", draft_marker)

                def carrier_wake_events(event: str = "wake.signalled") -> list[dict[str, Any]]:
                    # WakeCoordinator writes into the carrier worker log
                    # (state/logs/workers/<runtime>-<name>.jsonl); `hyprial log` does not
                    # surface those entries.
                    entries: list[dict[str, Any]] = []
                    workers = node_root / "state" / "logs" / "workers"
                    for path in sorted(workers.glob("dsh-*.jsonl")):
                        try:
                            lines = path.read_text(encoding="utf-8").splitlines()
                        except OSError:
                            continue
                        for line in lines:
                            try:
                                entry = json.loads(line)
                            except ValueError:
                                continue
                            if (
                                isinstance(entry, dict)
                                and entry.get("event") == event
                                and entry.get("messageId") == message_id
                            ):
                                entries.append(entry)
                    return entries

                wake_events = carrier_wake_events()
                if len(wake_events) != 1:
                    raise Failed(
                        "expected exactly one wake.signalled edge for the request; "
                        f"got {len(wake_events)}"
                    )
                stage("wake-single-signal", "OK", "one formal wake edge")
                failed_wakes = carrier_wake_events("wake.failed")
                if args.fail_first_wake:
                    if len(failed_wakes) != 1 or "TURN_ERROR" not in str(
                        failed_wakes[0].get("detail")
                    ):
                        raise Failed(
                            "expected one wake.failed naming TURN_ERROR before the "
                            f"retry; got {failed_wakes}"
                        )
                    stage(
                        "wake-failure-reported",
                        "OK",
                        f"wake.failed detail={failed_wakes[0].get('detail')!r} then retried",
                    )
                elif failed_wakes:
                    raise Failed(f"unexpected wake.failed entries: {failed_wakes}")

                # The control socket is under the generated launch dir; discover it.
                launch_dirs = sorted((node_root / "state").glob("dsh-tui-*"))
                if len(launch_dirs) != 1:
                    raise Failed(f"expected one dsh launch dir, found {launch_dirs}")
                control_socket = launch_dirs[0] / "bridge.sock"
                evidence.process_tree("pre-crash", (str(root),))
                if args.crash_mode == "socket":
                    control_socket.unlink(missing_ok=True)
                    stage("injection-socket-removed", "OK", str(control_socket))
                    wait_until(
                        lambda: daemon_session(daemon, dsh_actor) is None,
                        bool,
                        timeout=20,
                        label="carrier unregister after injection socket loss",
                    )
                    stage("socket-loss-unregister", "OK", "session unregistered")
                else:
                    pids = carrier_pids(control_socket)
                    if not pids:
                        raise Failed(f"could not find dsh-carrier pid for {control_socket}")
                    for pid in pids:
                        os.kill(pid, 9)
                    stage("carrier-crash", "OK", f"sigkilled {pids}")

                wait_until(
                    lambda: not tmux_alive(socket_name, session),
                    bool,
                    timeout=30,
                    label=f"TUI exit after {args.crash_mode}",
                )
                wait_until(
                    lambda: not any((node_root / "state").glob("dsh-tui-*")),
                    bool,
                    timeout=30,
                    label=f"launch-dir cleanup after {args.crash_mode}",
                )
                wait_until(
                    lambda: daemon_session(daemon, dsh_actor) is None,
                    bool,
                    timeout=20,
                    label=f"session withdrawn after {args.crash_mode}",
                )
                leftovers = [
                    line
                    for line in run(["ps", "-axo", "pid=,command="], timeout=20).stdout.splitlines()
                    if (
                        str(root) in line
                        and any(
                            name in line
                            for name in ("dsh-carrier", "dsh-tui.js", "/dsh --profile")
                        )
                    )
                    or f"agent-channel --actor {actor} " in line
                ]
                if leftovers:
                    raise Failed(f"processes left behind: {leftovers}")
                stage(
                    "crash-cleanup",
                    "OK",
                    f"mode={args.crash_mode} TUI stopped, launch dir removed, session withdrawn",
                )

                print(f"RESULT PASS {SCENARIO} actor={dsh_actor} marker={marker}")
                return 0
            except BaseException:
                try:
                    evidence.pane("failure")
                    evidence.process_tree("failure", (str(root),))
                except Exception:  # noqa: BLE001 - evidence must not mask the failure
                    pass
                raise
            finally:
                try:
                    evidence.copy_logs(node_root, home)
                except Exception:  # noqa: BLE001 - evidence must not mask the result
                    pass
                _stop_scenario(socket_name, session, daemon)
                if mock is not None:
                    mock.__exit__(None, None, None)
                    mock = None

    except Blocked as error:
        stage("runner", "BLOCKED", str(error))
        print(f"RESULT BLOCKED {SCENARIO} {error}")
        return 2
    except Exception as error:  # noqa: BLE001 - final classification
        stage("runner", "FAIL", f"{type(error).__name__}: {error}")
        print(f"RESULT FAIL {SCENARIO} {type(error).__name__}: {error}")
        return 1
    finally:
        # Backstop only: the scenario itself stopped these inside the tempdir.
        try:
            _stop_scenario(socket_name, session, daemon)
        except Exception:
            pass
        if mock is not None:
            mock.__exit__(None, None, None)


if __name__ == "__main__":
    raise SystemExit(main())
