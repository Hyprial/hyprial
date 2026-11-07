"""T16/T17 (P1b B3): Git/SSH capability and HOME staging fidelity, end to end.

T17 (design §8, spec-agent-home-p1b-b3-production §2): against a REAL isolated
daemon (IsolatedHarness: own HYPRIAL_HOME/HARNESS_STATE_DIR, peer discovery
off), a REAL headless pi worker — spawned through the production composition
path (lifecycle.start → HarnessLauncher → compose_worker_child_launch →
PiRpcProcess) — really runs git:

* the Git one-shot insteadOf triple, carried by the daemon's own environment,
  passes the whitelist as a complete group and REWRITES the push URL: the
  worker pushes to an unresolvable fake https base that only a
  ``url.<file bare>.insteadOf <fake base>`` rewrite can reach (control: the
  same URL from this test process, without the triple, fails);
* SSH_AUTH_SOCK arrives in the worker EXACTLY equal to the deployment-
  authorized path the daemon was given, and is actually usable (a REAL
  ``ssh-agent`` started by the test; ``ssh-add -l`` exit 0/1 = reachable,
  2 = refused — no stub agent, per "桩不许自造形态");
* remote principal verification: the commit that lands on the disposable
  bare remote is authored AND committed by the agent's own expected
  principal (the sentinel identity in the worker's HOME gitconfig), not the
  host's and not another agent's;
* a granted secret (DEEPSEEK_API_KEY = unique per-run sentinel) reaches the
  worker through the resolver, and its VALUE appears in no artifact the
  commit/push path produces: daemon log, git transcript, env dump, results,
  or the remote.

T16 (HOME staging fidelity): on the same real path, the worker's HOME is the
daemon's original value verbatim (sentinel tea/gitconfig/ssh roots left
untouched), approved XDG names pass as-is and stay ABSENT when absent, and
no ``<agent home>/state/home`` is created (P1 keeps the real HOME; the
redirect is P2).

Negative leg: a partial Git triple (COUNT+KEY, no VALUE) is DROPPED by the
composition, not honored — the worker env carries none of the three names
and the push to the fake base fails named.  The same leg pins the
operation-level refusal for an unusable SSH_AUTH_SOCK (``ssh-add`` exit 2,
a named agent refusal) with no fallback identity available (sentinel .ssh
is empty).  Value-level authorization of the socket path itself ("authorized
path" equality, design §3.2 line 114's start-refusal) is NOT implemented in
the product and is out of this slice: its required input is the operator-
approved deployment value that only Allen holds (B3 spec §1: approve values,
list the missing ones — recorded in the PR body and
docs/notes/p1b-b3-deployment-values-2026-09-20.md).
"""

from __future__ import annotations

from tests.support.runtime._tmp import hyprial_test_tmp_root

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.daemon.channels.mcp.lifecycle.test_mcp_channel_owner_migration_e2e import IsolatedHarness, _wait_until

pytestmark = pytest.mark.anyio

OWNER = "owner-a"
NODE = "node-e2e"
WORKER = "git-worker"

SENTINEL_NAME = "P1B E2E Worker"
SENTINEL_EMAIL = "p1b-worker@e2e.invalid"
FAKE_PUSH_BASE = "https://p1b-e2e.invalid/"
REMOTE_NAME = "remote.git"
#: Unique per run: a value that must survive ONLY inside the worker's
#: environment and its legitimate store, never in any artifact of the
#: commit/push path.
SECRET_SENTINEL = f"p1b-e2e-secret-{uuid.uuid4().hex}"

_WORKER_WRAPPER = '''
import json, os, subprocess, sys
from pathlib import Path

ROOT = Path(__ROOT__)
FAKE_PI = __FAKE_PI__
REMOTE_URL = __REMOTE_URL__
TRANSCRIPT = ROOT / "git-transcript.txt"

NAMES = [
    "HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "SSH_AUTH_SOCK",
    "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0",
    "HYPRIAL_AGENT_DIR", "HYPRIAL_WORKER_ACTOR",
    "HYPRIAL_WORKER_SESSION_REF", "HYPRIAL_NODE_ID", "HYPRIAL_OWNER",
]
envdump = {name: os.environ.get(name) for name in NAMES}
# Presence only: the granted value itself is a secret and must not land in
# this artifact either.
envdump["DEEPSEEK_API_KEY_PRESENT"] = "DEEPSEEK_API_KEY" in os.environ
(ROOT / "worker-env.json").write_text(json.dumps(envdump, indent=2, sort_keys=True))

git_env = dict(os.environ)
git_env["GIT_TERMINAL_PROMPT"] = "0"
git_env["GIT_CONFIG_NOSYSTEM"] = "1"

lines = []


def run(argv, cwd=None):
    proc = subprocess.run(
        argv, cwd=cwd, capture_output=True, text=True, env=git_env, timeout=60
    )
    lines.append("$ " + " ".join(str(part) for part in argv))
    if proc.stdout:
        lines.append(proc.stdout)
    if proc.stderr:
        lines.append(proc.stderr)
    return proc


work = ROOT / "work"
run(["git", "init", "-q", str(work)])
run(["git", "remote", "add", "origin", REMOTE_URL], cwd=work)
(work / "artifact.txt").write_text("p1b b3 e2e artifact\\n")
run(["git", "add", "artifact.txt"], cwd=work)
commit = run(["git", "commit", "-q", "-m", "p1b b3 e2e: worker commit"], cwd=work)
head = run(["git", "rev-parse", "HEAD"], cwd=work).stdout.strip()
identity = run(
    ["git", "log", "-1", "--format=%an%x00%ae%x00%cn%x00%ce"], cwd=work
).stdout
push = run(["git", "push", "origin", "HEAD:refs/heads/main"], cwd=work)
agent = run(["ssh-add", "-l"])
TRANSCRIPT.write_text("\\n".join(lines))
(ROOT / "results.json").write_text(json.dumps({
    "commit_rc": commit.returncode,
    "head": head,
    "identity": identity,
    "push_rc": push.returncode,
    "ssh_add_rc": agent.returncode,
}))
os.execv(sys.executable, [sys.executable, FAKE_PI, *sys.argv[1:]])
'''


def _start_ssh_agent(root: Path) -> tuple[Path, int]:
    """A REAL ssh-agent bound to a test-local socket (no stub forms)."""

    sock = root / "agent.sock"
    proc = subprocess.run(
        ["ssh-agent", "-a", str(sock), "-s"],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    pid = None
    for line in proc.stdout.splitlines():
        if line.startswith("SSH_AGENT_PID="):
            pid = int(line.split("=", 1)[1].split(";")[0])
    assert pid is not None and sock.exists(), proc.stdout
    return sock, pid


def _sentinel_home(root: Path) -> Path:
    home = root / "sentinel-home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".config" / "tea").mkdir(parents=True)
    (home / ".config" / "tea" / "config.yml").write_text(
        "# non-secret tea sentinel\n", encoding="utf-8"
    )
    (home / ".gitconfig").write_text(
        "[user]\n"
        f"\tname = {SENTINEL_NAME}\n"
        f"\temail = {SENTINEL_EMAIL}\n"
        "[commit]\n"
        "\tgpgsign = false\n",
        encoding="utf-8",
    )
    return home


def _seed_grant(harness: IsolatedHarness) -> None:
    """Pre-seed one agent, its home, and one granted secret BEFORE the daemon
    opens the same registry (sequential access; the daemon resolves the grant
    at worker composition time)."""

    from hyprial.identity.impl.agents import AgentRegistry, SecretResolver, SecretSource

    # configured_hyprial_home() resolves the daemon's HYPRIAL_HOME (macOS
    # /tmp -> /private/tmp): the receipt the daemon validates is stored with
    # the RESOLVED spelling, so the seed must use it too.
    home = harness.home.resolve()
    registry = AgentRegistry(
        harness.state / "agents.sqlite3",
        owner=OWNER,
        machine=NODE,
        hyprial_home=home,
    )
    try:
        registry.create(WORKER)
        registry.ensure_home(WORKER)
        resolver = SecretResolver(home, registry)
        resolver.write_user_provider("deepseek", {"apiKey": SECRET_SENTINEL})
        registry.grant_secret(
            WORKER,
            grant_id="deepseek",
            source=SecretSource.USER_PROVIDER,
            entry_id="deepseek",
            field_name="apiKey",
            environment_names=("DEEPSEEK_API_KEY",),
            revision=1,
        )
    finally:
        registry.close()


def _write_wrapper(root: Path) -> Path:
    wrapper = root / "worker_git_wrapper.py"
    fake_pi = Path(__file__).resolve().parents[4] / 'tests/fixtures/harnesses/fake_pi_rpc.py'
    text = (
        _WORKER_WRAPPER.replace("__ROOT__", repr(str(root)))
        .replace("__FAKE_PI__", repr(str(fake_pi)))
        .replace("__REMOTE_URL__", repr(FAKE_PUSH_BASE + REMOTE_NAME))
    )
    wrapper.write_text(text, encoding="utf-8")
    return wrapper


class _GitCapabilityHarness:
    """IsolatedHarness plus the git/ssh staging for one leg of T16/T17."""

    def __init__(
        self,
        root: Path,
        *,
        advertised_sock: Path,
        real_agent: bool,
        git_triple: bool,
    ) -> None:
        self.root = root
        self.home_dir = _sentinel_home(root)
        self.agent_sock: Path | None = None
        self.agent_pid: int | None = None
        if real_agent:
            self.agent_sock, self.agent_pid = _start_ssh_agent(root)
            assert self.agent_sock == advertised_sock
        self.remote = root / REMOTE_NAME
        subprocess.run(
            ["git", "init", "--bare", "-q", str(self.remote)],
            check=True,
            capture_output=True,
            timeout=30,
        )
        harness = IsolatedHarness(root)
        harness.environment.update(
            {
                "HOME": str(self.home_dir),
                "XDG_CONFIG_HOME": str(self.home_dir / ".config"),
                "FAKE_PI_MODE": "settled",
                "FAKE_PI_LOG": str(root / "pi-cmds.jsonl"),
                "FAKE_PI_ARGV_LOG": str(root / "pi-argv.jsonl"),
                "SSH_AUTH_SOCK": str(advertised_sock),
            }
        )
        harness.environment.pop("XDG_CACHE_HOME", None)
        if git_triple:
            harness.environment.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": f"url.file://{root}/.insteadOf",
                    "GIT_CONFIG_VALUE_0": FAKE_PUSH_BASE,
                }
            )
        else:
            # The partial-group leg: COUNT and KEY without VALUE.  The
            # composition must DROP the group, not honor it.
            harness.environment.update(
                {
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": f"url.file://{root}/.insteadOf",
                }
            )
            harness.environment.pop("GIT_CONFIG_VALUE_0", None)
        harness.write_owner(OWNER)
        _seed_grant(harness)
        harness.start_daemon()
        self.harness = harness

    def launch_worker(self) -> None:
        wrapper = _write_wrapper(self.root)
        started = self.harness.ipc(
            "lifecycle.start",
            {
                "provider": "pi",
                "name": WORKER,
                "headless": True,
                "command": [sys.executable, str(wrapper)],
                "cwd": str(self.root),
                "args": [],
            },
        )
        assert started["ok"] is True

    def results(self) -> dict[str, object] | None:
        path = self.root / "results.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def envdump(self) -> dict[str, object]:
        return json.loads((self.root / "worker-env.json").read_text(encoding="utf-8"))

    def transcript(self) -> str:
        return (self.root / "git-transcript.txt").read_text(encoding="utf-8")

    def close(self) -> None:
        try:
            self.harness.ipc("down", {"name": WORKER, "provider": "pi"})
        except Exception:  # noqa: BLE001 - teardown must not mask the failure
            pass
        self.harness.stop_daemon()
        if self.agent_pid is not None:
            try:
                os.kill(self.agent_pid, 15)
            except OSError:
                pass
        shutil.rmtree(self.root, ignore_errors=True)


@pytest.fixture()
def positive() -> Iterator[_GitCapabilityHarness]:
    root = Path(tempfile.mkdtemp(dir=hyprial_test_tmp_root(), prefix="hyprial-gitcap-"))
    value = _GitCapabilityHarness(
        root,
        advertised_sock=root / "agent.sock",
        real_agent=True,
        git_triple=True,
    )
    try:
        yield value
    finally:
        value.close()


@pytest.fixture()
def negative() -> Iterator[_GitCapabilityHarness]:
    root = Path(tempfile.mkdtemp(dir=hyprial_test_tmp_root(), prefix="hyprial-gitcap-"))
    # Bogus socket path (never created): the operation-level refusal leg.
    value = _GitCapabilityHarness(
        root,
        advertised_sock=root / "bogus-agent.sock",
        real_agent=False,
        git_triple=False,
    )
    try:
        yield value
    finally:
        value.close()


def _read_artifacts(harness: _GitCapabilityHarness) -> dict[str, str]:
    artifacts = {}
    for name in ("daemon.out", "git-transcript.txt", "worker-env.json", "results.json"):
        path = harness.root / name
        artifacts[name] = path.read_text(encoding="utf-8") if path.exists() else ""
    log = subprocess.run(
        ["git", f"--git-dir={harness.remote}", "log", "-p", "--all"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    artifacts["remote-history"] = log.stdout + log.stderr
    return artifacts


async def test_t16_home_staging_fidelity_e2e(
    positive: _GitCapabilityHarness,
) -> None:
    """HOME reaches the final child verbatim; XDG passes as-is or stays
    absent; no ``A/state/home`` is created; the sentinel roots stay intact."""

    positive.launch_worker()
    await _wait_until(positive.results, timeout=60.0)
    dump = positive.envdump()
    assert dump["HOME"] == str(positive.home_dir)
    assert dump["XDG_CONFIG_HOME"] == str(positive.home_dir / ".config")
    assert dump["XDG_CACHE_HOME"] is None
    agent_dir = Path(str(dump["HYPRIAL_AGENT_DIR"]))
    assert agent_dir == positive.harness.home.resolve() / "agents" / WORKER
    assert not (agent_dir / "state" / "home").exists()
    # The original roots keep their prior values, untouched by the run.
    assert (positive.home_dir / ".gitconfig").read_text(encoding="utf-8").count(
        SENTINEL_EMAIL
    ) == 1
    assert (positive.home_dir / ".config" / "tea" / "config.yml").exists()
    # The ambient HOME of this test process was NOT the one delivered.
    assert dump["HOME"] != os.environ.get("HOME")


async def test_t17_git_ssh_capability_commit_push_e2e(
    positive: _GitCapabilityHarness,
) -> None:
    """The T17 contract: real rewrite, real agent socket, remote principal,
    and zero secret leakage through the commit/push path."""

    # Control: the fake base is genuinely unreachable WITHOUT the rewrite —
    # a successful push through it can only be the insteadOf triple working.
    control = subprocess.run(
        ["git", "ls-remote", FAKE_PUSH_BASE + REMOTE_NAME],
        capture_output=True,
        text=True,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", "GIT_TERMINAL_PROMPT": "0"},
    )
    assert control.returncode != 0

    positive.launch_worker()
    await _wait_until(positive.results, timeout=60.0)
    results = positive.results()
    assert results is not None
    dump = positive.envdump()

    # SSH_AUTH_SOCK == the deployment-authorized path, actually usable.
    assert positive.agent_sock is not None
    assert dump["SSH_AUTH_SOCK"] == str(positive.agent_sock)
    # 0 = identities listed, 1 = agent reachable with none; 2 = refused.
    assert results["ssh_add_rc"] in (0, 1)

    # The complete triple passed the whitelist as a group.
    assert dump["GIT_CONFIG_COUNT"] == "1"
    assert dump["GIT_CONFIG_KEY_0"] == f"url.file://{positive.root}/.insteadOf"
    assert dump["GIT_CONFIG_VALUE_0"] == FAKE_PUSH_BASE

    # The granted secret flowed through the resolver into the worker env
    # (presence only — its value is asserted absent from every artifact).
    assert dump["DEEPSEEK_API_KEY_PRESENT"] is True
    assert dump["HYPRIAL_WORKER_ACTOR"] == f"agent:{OWNER}:{NODE}:{WORKER}"

    # Real commit + real push through the rewritten URL.
    assert results["commit_rc"] == 0
    assert results["push_rc"] == 0
    expected_identity = (
        f"{SENTINEL_NAME}\x00{SENTINEL_EMAIL}\x00"
        f"{SENTINEL_NAME}\x00{SENTINEL_EMAIL}\n"
    )
    assert results["identity"] == expected_identity

    # Remote principal verification: the disposable remote carries exactly
    # one commit, the worker's, authored AND committed by the agent's own
    # expected principal — not the host's, not another agent's.
    remote = subprocess.run(
        [
            "git",
            f"--git-dir={positive.remote}",
            "log",
            "-1",
            "--format=%H%x00%an%x00%ae%x00%cn%x00%ce",
            "refs/heads/main",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    fields = remote.stdout.strip().split("\x00")
    assert fields[0] == results["head"]
    assert fields[1:] == [SENTINEL_NAME, SENTINEL_EMAIL, SENTINEL_NAME, SENTINEL_EMAIL]
    count = subprocess.run(
        ["git", f"--git-dir={positive.remote}", "rev-list", "--count", "--all"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert count.stdout.strip() == "1"
    host_name = subprocess.run(
        ["git", "config", "--global", "user.name"],
        capture_output=True,
        text=True,
        timeout=15,
    ).stdout.strip()
    if host_name:
        assert fields[1] != host_name

    # No secret value in ANY artifact of the commit/push path.
    artifacts = _read_artifacts(positive)
    assert SECRET_SENTINEL not in json.dumps(artifacts)
    # And the transcript names the fake base: the push demonstrably went at
    # the URL only the rewrite can reach (paired with the control above).
    assert FAKE_PUSH_BASE + REMOTE_NAME in positive.transcript()


async def test_t17_partial_git_triple_dropped_and_unusable_sock_refused_e2e(
    negative: _GitCapabilityHarness,
) -> None:
    """Negative leg: a partial Git triple is dropped, not honored (the push
    to the fake base fails named), and an unusable SSH_AUTH_SOCK is refused
    at the operation level with no fallback identity available."""

    negative.launch_worker()
    await _wait_until(negative.results, timeout=60.0)
    results = negative.results()
    assert results is not None
    dump = negative.envdump()

    # The incomplete group did not leak into the worker in any part.
    assert dump["GIT_CONFIG_COUNT"] is None
    assert dump["GIT_CONFIG_KEY_0"] is None
    assert dump["GIT_CONFIG_VALUE_0"] is None

    # Without the rewrite the fake base fails — named, visible, not silent.
    assert results["push_rc"] != 0
    transcript = negative.transcript()
    assert "p1b-e2e.invalid" in transcript

    # The unusable agent socket is refused by the real tool (exit 2 =
    # "error connecting to agent"); the sentinel HOME carries no key
    # material, so there is no fallback identity to slide into.
    assert results["ssh_add_rc"] == 2
    assert dump["SSH_AUTH_SOCK"] == str(negative.root / "bogus-agent.sock")
    assert list((negative.home_dir / ".ssh").iterdir()) == []

    # The granted secret still reached the worker; nothing leaked.
    assert dump["DEEPSEEK_API_KEY_PRESENT"] is True
    artifacts = _read_artifacts(negative)
    assert SECRET_SENTINEL not in json.dumps(artifacts)
