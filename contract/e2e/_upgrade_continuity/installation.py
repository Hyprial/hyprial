"""Private scenario implementation; the entry owns configuration and patch seams.

The explicit live entry mapping avoids a second cache or copied runtime state.
These helpers do not start any scenario during import.
"""
from __future__ import annotations
from typing import Any
def run(_entry: dict[str, Any], /, *command, env, cwd):
    rendered = [str(item) for item in command]
    result = _entry['subprocess'].run(
        rendered, cwd=cwd, env=env, text=True,
        stdout=_entry['subprocess'].PIPE, stderr=_entry['subprocess'].STDOUT, check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"command failed ({result.returncode}): {' '.join(rendered)}\n"
            f"{result.stdout[-4000:]}"
        )
    return result.stdout


def hyprial(_entry: dict[str, Any], /, executable, args, env, *, exits):
    result = _entry['subprocess'].run(
        [str(executable), *args], env=env, text=True,
        capture_output=True, timeout=300, check=False,
    )
    if result.returncode not in exits:
        raise AssertionError(
            f"hyprial {' '.join(args)} exited {result.returncode}; "
            f"stdout={result.stdout[-2000:]!r} stderr={result.stderr[-2000:]!r}"
        )
    for line in reversed([ln for ln in result.stdout.splitlines() if ln.strip()]):
        try:
            value = _entry['json'].loads(line)
        except _entry['json'].JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise AssertionError(f"hyprial {' '.join(args)} did not emit a JSON object: {result.stdout!r}")


def upgrade_command(_entry: dict[str, Any], /, executable, env) -> list[str]:
    """Probe the old CLI: fixture version stamps do not identify its surface."""
    for command in (["daemon", "upgrade"], ["upgrade"]):
        result = _entry['subprocess'].run(
            [str(executable), *command, "--help"],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode == 0:
            return [*command, "--json"]
    raise AssertionError("old CLI supports neither daemon upgrade nor upgrade")


def init_command(_entry: dict[str, Any], /, executable, env) -> list[str]:
    """Avoid user-service registration wherever the installed CLI supports it."""
    result = _entry['subprocess'].run(
        [str(executable), "init", "--help"],
        env=env, text=True, capture_output=True, timeout=30, check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"init help failed ({result.returncode}): "
            f"{(result.stdout + result.stderr)[-2000:]}"
        )
    service = ["--no-service"] if "--no-service" in result.stdout else []
    return ["init", *service, "--json"]


def _message_send_help(_entry: dict[str, Any], /, executable, env) -> tuple[list[str], str]:
    """Only an absent message group permits probing the legacy send command."""

    result = _entry['subprocess'].run(
        [str(executable), "message", "send", "--help"],
        env=env, text=True, capture_output=True, timeout=30, check=False,
    )
    if result.returncode == 0:
        return ["message", "send"], result.stdout
    diagnostic = result.stdout + result.stderr
    if result.returncode != 2 or "No such command 'message'" not in diagnostic:
        raise AssertionError(
            f"old CLI message send help failed ({result.returncode}): {diagnostic[-2000:]}"
        )
    result = _entry['subprocess'].run(
        [str(executable), "send", "--help"],
        env=env, text=True, capture_output=True, timeout=30, check=False,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"old CLI send help failed ({result.returncode}): "
            f"{(result.stdout + result.stderr)[-2000:]}"
        )
    return ["send"], result.stdout


def message_send_command(_entry: dict[str, Any], /, executable, env) -> list[str]:
    return _message_send_help(_entry, executable, env)[0]


def message_summary_args(_entry: dict[str, Any], /, executable, env, summary: str) -> list[str]:
    """Detect the real old CLI's summary support without a fixture-version guess."""

    _, help_text = _message_send_help(_entry, executable, env)
    return ["--summary", summary] if "--summary" in help_text else []


def verify_restart_identity(
    before: dict[str, Any], after: dict[str, Any], live: dict[str, Any], *,
    old_pid: int, old_epoch: str, old_version: str, new_version: str,
) -> str:
    """Legacy CLI projections still require independent serving-generation facts."""
    if not old_epoch or before.get("pid") != old_pid or before.get("version") != old_version:
        raise AssertionError(f"upgrade reported wrong pre-restart facts: {before}")
    new_pid = after.get("pid")
    if not isinstance(new_pid, int) or isinstance(new_pid, bool) or new_pid <= 0 or new_pid == old_pid or after.get("version") != new_version:
        raise AssertionError(f"upgrade reported wrong post-restart facts: {after}")
    if (
        live.get("running") is not True
        or live.get("phase") not in {"serving", "reconciled"}
        or live.get("pid") != new_pid
        or live.get("version") != new_version
    ):
        raise AssertionError(f"independent daemon is not the serving replacement: {live}")
    new_epoch = live.get("epoch")
    if not isinstance(new_epoch, str) or not new_epoch or new_epoch == old_epoch:
        raise AssertionError(f"independent daemon epoch is not fresh: {live}")
    before_has_epoch, after_has_epoch = "epoch" in before, "epoch" in after
    if before_has_epoch != after_has_epoch:
        raise AssertionError("partial CLI epoch projection is not a legacy projection")
    if before_has_epoch and (before["epoch"] != old_epoch or after["epoch"] != new_epoch):
        raise AssertionError("reported CLI epochs do not match independent daemon facts")
    return new_epoch


def daemon_ps(_entry: dict[str, Any], /, state_dir, *, timeout: float = 30.0):
    """Wait on light readiness before a strict full-state read after restart."""
    deadline = _entry['time'].monotonic() + timeout
    while True:
        probe = _entry['ipc'](state_dir, "ping", {})
        if probe.get("restorePending") is not True:
            result = _entry['ipc'](state_dir, "ps", {})
            if not isinstance(result, dict):
                raise AssertionError(f"daemon ps IPC returned no object result: {result!r}")
            return result
        if _entry['time'].monotonic() >= deadline:
            raise AssertionError(f"daemon restore did not settle within {timeout:g}s: {probe}")
        _entry['time'].sleep(0.2)


def ipc(_entry: dict[str, Any], /, state_dir, method, params, timeout):
    frame: dict[str, object] = {"version": 1, "id": str(_entry['uuid4']()), "method": method}
    if params:
        frame["params"] = params
    client = _entry['socket'].socket(_entry['socket'].AF_UNIX, _entry['socket'].SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(str(state_dir / "daemon.sock"))
        client.sendall(_entry['json'].dumps(frame).encode("utf-8") + b"\n")
        buffer = bytearray()
        while True:
            chunk = client.recv(65536)
            if not chunk:
                raise AssertionError(f"daemon closed connection during {method}")
            buffer.extend(chunk)
            while b"\n" in buffer:
                line, _, rest = buffer.partition(b"\n")
                buffer = bytearray(rest)
                if not line.strip():
                    continue
                response = _entry['json'].loads(line)
                if response.get("id") != frame["id"]:
                    continue
                if isinstance(response.get("error"), dict):
                    raise AssertionError(f"{method} failed: {response['error']}")
                return response["result"]
    finally:
        client.close()


def git_config(_entry: dict[str, Any], /, path, remote):
    path.write_text(
        "\n".join((
            '[protocol "file"]', "\tallow = always",
            f'[url "{remote.as_uri()}"]',
            f"\tinsteadOf = {_entry['GIT_SOURCE']}",
            f"\tinsteadOf = {_entry['OFFICIAL_GIT_SOURCE']}",
            "[user]", "\tname = hyprial upgrade continuity",
            "\temail = hyprial-upgrade-continuity@example.invalid", "",
        )),
        encoding="utf-8",
    )


def materialize_revision(_entry: dict[str, Any], /, path, revision, env):
    """Replace the complete tracked tree, including additions and deletions."""

    _entry['run']("git", "read-tree", "--reset", "-u", revision, env=env, cwd=path)


def resolve_upgrade_from(_entry: dict[str, Any], /, seed, host_env):
    """Fetch the promoted OLD tree into ``seed``; return (source, commit).

    Read from the real remote, not a local tag: a checkout's tags are only as
    fresh as its last tag fetch, and CI's checkout fetches ${GITHUB_SHA}
    alone.  Runs with the HOST environment -- the isolated HOME has no
    credentials for the remote -- and before any insteadOf rewrite exists.
    """

    source = _entry['os'].environ.get(_entry['UPGRADE_FROM_ENV'], "").strip() or _entry['UPGRADE_FROM_DEFAULT']
    origin = _entry['run']("git", "remote", "get-url", "origin", env=host_env, cwd=_entry['ROOT']).strip()
    ref = source if _entry['re'].fullmatch(r"[0-9a-f]{40}", source) else f"refs/tags/{source}"
    _entry['run']("git", "fetch", "--no-tags", origin, ref, env=host_env, cwd=seed)
    commit = _entry['run'](
        "git", "rev-parse", "--verify", "FETCH_HEAD^{commit}", env=host_env, cwd=seed
    ).strip()
    return source, commit


def read_desired_state(_entry: dict[str, Any], /, state, installed_python, env):
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

    document = _entry['json'].loads(
        _entry['run'](installed_python, "-c", _entry['_READ_DESIRED_STATE'], str(state), env=env)
    )
    if not isinstance(document, dict):
        raise AssertionError(f"no committed desired-state document under {state}")
    return document


def channel_protocol_version_at(_entry: dict[str, Any], /, repository, revision, env):
    """The Channel protocol the tree at ``revision`` speaks.

    The old channel is driven by the OLD package, so it must be judged by the
    old tree's constant, not by the candidate's imported one: once a candidate
    bumps the protocol, the upgraded daemon is right to call a held-open old
    client ``legacy_or_unknown``.
    """

    candidates = (
        "src/hyprial/kernel/impl/contracts/channels/channel.py",
        "src/hyprial/contracts/channel.py",
    )
    present = _entry['run'](
        "git", "ls-tree", "--name-only", revision, "--", *candidates,
        env=env, cwd=repository,
    ).splitlines()
    path = next((path for path in candidates if path in present), None)
    if path is None:
        raise AssertionError(f"{revision[:12]} declares no CHANNEL_PROTOCOL_VERSION")
    source = _entry['run'](
        "git", "show", f"{revision}:{path}", env=env, cwd=repository,
    )
    match = _entry['re'].search(r"(?m)^CHANNEL_PROTOCOL_VERSION\s*=\s*(\d+)\s*$", source)
    if match is None:
        raise AssertionError(
            f"{revision[:12]} declares no CHANNEL_PROTOCOL_VERSION"
        )
    return int(match.group(1))


def seed_remote(_entry: dict[str, Any], /, root, env, host_env):
    remote = root / "remote.git"
    seed = root / "seed"
    _entry['run']("git", "init", "--bare", remote, env=env)
    _entry['run']("git", "clone", "--no-local", _entry['ROOT'], seed, env=env)
    candidate_revision = _entry['run'](
        "git", "rev-parse", "HEAD", env=env, cwd=seed
    ).strip()
    upgrade_from, old_revision = _entry['resolve_upgrade_from'](seed, host_env)
    if old_revision == candidate_revision:
        raise _entry['NoUpgradeDelta'](
            f"{upgrade_from} is the candidate {candidate_revision[:12]}; "
            "there is no upgrade to exercise"
        )
    _entry['run']("git", "checkout", "-B", "dev", "HEAD", env=env, cwd=seed)
    for inherited_tag in _entry['run']("git", "tag", "--list", env=env, cwd=seed).splitlines():
        _entry['run']("git", "tag", "--delete", inherited_tag, env=env, cwd=seed)
    _entry['run']("git", "config", "user.name", "hyprial upgrade continuity", env=env, cwd=seed)
    _entry['run'](
        "git", "config", "user.email",
        "hyprial-upgrade-continuity@example.invalid", env=env, cwd=seed,
    )
    # The whole promoted tree, unmodified but for its version stamp: the old
    # daemon, its Channel and its own `hyprial upgrade` are exactly what a
    # deployment on that promotion runs.
    _entry['materialize_revision'](seed, old_revision, env)
    _entry['write_project_version'](seed / "pyproject.toml", _entry['INITIAL_VERSION'])
    _entry['run']("git", "add", "-A", env=env, cwd=seed)
    _entry['run'](
        "git", "commit", "-m", "test: seed disposable old version", env=env, cwd=seed
    )
    _entry['run']("git", "tag", f"v{_entry['INITIAL_VERSION']}", env=env, cwd=seed)
    _entry['run']("git", "remote", "set-url", "origin", remote, env=env, cwd=seed)
    _entry['run']("git", "push", "origin", "--tags", env=env, cwd=seed)
    return remote, seed, candidate_revision, upgrade_from, old_revision


def push_upgrade(_entry: dict[str, Any], /, seed, candidate_revision, new, env):
    _entry['materialize_revision'](seed, candidate_revision, env)
    project = seed / "pyproject.toml"
    _entry['write_project_version'](project, new)
    _entry['run']("git", "add", "-A", env=env, cwd=seed)
    _entry['run']("git", "commit", "-m", "test: publish disposable upgrade", env=env, cwd=seed)
    commit = _entry['run']("git", "rev-parse", "HEAD", env=env, cwd=seed).strip()
    _entry['run']("git", "tag", f"v{new}", env=env, cwd=seed)
    _entry['run']("git", "push", "origin", f"v{new}", env=env, cwd=seed)
    return commit
