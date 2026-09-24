"""Resolved agent-home P2 roots and non-secret tool environment profiles.

This module is the shared boundary consumed by provider adapters.  It resolves
one durable Agent identity into one immutable launch context; provider modules
do not derive roots from ``HOME``, cwd, or an actor URI themselves.
"""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from hyprial.updates import DEFAULT_GIT_URL

from .config import (
    NATIVE_CONFIG_MAPPING_VERSION,
    ConfigManifest,
    ConfigProjectionReceipt,
    NativeConfigProjection,
    build_native_projection,
    materialize_native_projection,
    require_agent_config,
    validate_agent_config_location,
)

__all__ = [
    "AgentRuntimeContext",
    "AgentRuntimeError",
    "AgentRuntimeRoots",
    "AgentToolProfile",
    "DEFAULT_AGENT_TOOL_PROFILE",
    "SshToolAuthorization",
    "resolve_agent_runtime_context",
]

_P2_HARNESSES = frozenset({"claude", "codex", "pi"})
_NATIVE_ROOT_ENV = {
    "claude": "CLAUDE_CONFIG_DIR",
    "codex": "CODEX_HOME",
    "pi": "PI_CODING_AGENT_DIR",
}
_PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SSH_TOKEN = re.compile(r"[A-Za-z0-9._@:-]+\Z")


class AgentRuntimeError(ValueError):
    """A P2 root/profile combination cannot be represented safely."""


def _default_git_endpoint() -> tuple[str, str]:
    parsed = urlsplit(DEFAULT_GIT_URL)
    if parsed.hostname is None:
        raise AgentRuntimeError("default Git URL must name a host")
    return parsed.hostname, urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))


_DEFAULT_GIT_HOSTNAME, _DEFAULT_GIT_REWRITE_TARGET = _default_git_endpoint()


@dataclass(frozen=True, slots=True)
class SshToolAuthorization:
    """Explicit non-secret selection metadata for one approved SSH signer."""

    auth_sock: str
    identity_file: str
    known_hosts_file: str
    host: str = "code.hyprial.com"
    hostname: str = _DEFAULT_GIT_HOSTNAME
    user: str = "git"

    def __post_init__(self) -> None:
        for label, value in (
            ("auth_sock", self.auth_sock),
            ("identity_file", self.identity_file),
            ("known_hosts_file", self.known_hosts_file),
        ):
            if not value or "\n" in value or not Path(value).is_absolute():
                raise AgentRuntimeError(f"SSH {label} must be an absolute path")
        if any(
            not value or _SSH_TOKEN.fullmatch(value) is None
            for value in (self.host, self.hostname, self.user)
        ):
            raise AgentRuntimeError("SSH host, hostname, and user must not be blank")


@dataclass(frozen=True, slots=True)
class AgentToolProfile:
    """Approved, non-secret tool identity and optional SSH capability."""

    profile_id: str
    git_author_name: str
    git_author_email: str
    git_rewrite_source: str = "https://code.hyprial.com/"
    git_rewrite_target: str = _DEFAULT_GIT_REWRITE_TARGET
    tea_login: str = "code.hyprial.com"
    ssh: SshToolAuthorization | None = None

    def __post_init__(self) -> None:
        values = (
            self.profile_id,
            self.git_author_name,
            self.git_author_email,
            self.git_rewrite_source,
            self.git_rewrite_target,
            self.tea_login,
        )
        if any(not value for value in values):
            raise AgentRuntimeError("tool profile fields must not be blank")
        if "\n" in "".join(values):
            raise AgentRuntimeError("tool profile fields must be single-line values")
        if _PROFILE_ID.fullmatch(self.profile_id) is None:
            raise AgentRuntimeError("tool profile id contains an unsafe path character")
        if any(character in self.git_rewrite_target for character in '\"]'):
            raise AgentRuntimeError("Git rewrite target contains an unsafe config character")
        if (
            self.git_rewrite_source != "https://code.hyprial.com/"
            or self.git_rewrite_target != _DEFAULT_GIT_REWRITE_TARGET
            or self.tea_login != "code.hyprial.com"
        ):
            raise AgentRuntimeError(
                "tool profile may not widen the approved Forgejo endpoints"
            )


DEFAULT_AGENT_TOOL_PROFILE = AgentToolProfile(
    profile_id="agent-home-p2-allen-v1",
    git_author_name="Allen Woods",
    git_author_email="allenwoods@users.noreply.code.hyprial.com",
)


@dataclass(frozen=True, slots=True)
class AgentRuntimeRoots:
    """All roots for one selected harness, derived from one home receipt."""

    agent_home: Path
    config_source: Path
    projection_root: Path
    native_root: Path
    session_root: Path
    tool_home: Path
    xdg_config_home: Path
    xdg_cache_home: Path
    xdg_data_home: Path
    xdg_state_home: Path
    tool_profile_root: Path

    def __post_init__(self) -> None:
        for value in (
            self.agent_home,
            self.config_source,
            self.projection_root,
            self.native_root,
            self.session_root,
            self.tool_home,
            self.xdg_config_home,
            self.xdg_cache_home,
            self.xdg_data_home,
            self.xdg_state_home,
            self.tool_profile_root,
        ):
            if not value.is_absolute():
                raise AgentRuntimeError("runtime roots must be absolute")


@dataclass(frozen=True, slots=True)
class AgentRuntimeContext:
    """One incarnation-bound P2 context carried to the final exec boundary."""

    actor: str
    entity_token: str = field(repr=False)
    harness: str
    manifest: ConfigManifest
    projection: NativeConfigProjection
    projection_receipt: ConfigProjectionReceipt
    roots: AgentRuntimeRoots
    tool_profile_id: str
    environment_items: tuple[tuple[str, str], ...] = field(repr=False)
    auth_method: str | None = None
    auth_revision: str | None = None

    def __post_init__(self) -> None:
        if self.harness not in _P2_HARNESSES:
            raise AgentRuntimeError(f"unsupported P2 harness {self.harness!r}")
        if not self.actor or not self.entity_token or not self.tool_profile_id:
            raise AgentRuntimeError("runtime context identity must not be blank")
        names = [name for name, _value in self.environment_items]
        if names != sorted(names) or len(names) != len(set(names)):
            raise AgentRuntimeError("runtime environment names must be sorted and unique")

    @property
    def config_revision(self) -> str:
        return self.manifest.revision

    def environment(self) -> dict[str, str]:
        """Return only generated roots/profile selectors; never credentials."""

        return dict(self.environment_items)

    def public_projection(self) -> dict[str, object]:
        """Non-secret CLI handoff; paths diagnose but confer no new grant."""

        return {
            "mode": "agent-home-p2",
            "actor": self.actor,
            "harness": self.harness,
            "configRevision": self.config_revision,
            "mappingVersion": NATIVE_CONFIG_MAPPING_VERSION,
            "projectionRoot": str(self.roots.projection_root),
            "nativeRoot": str(self.roots.native_root),
            "sessionRoot": str(self.roots.session_root),
            "toolProfileId": self.tool_profile_id,
            "environment": self.environment(),
            "auth": (
                {"method": self.auth_method, "revision": self.auth_revision}
                if self.auth_method is not None and self.auth_revision is not None
                else None
            ),
        }


def resolve_agent_runtime_context(
    *,
    registry: Any,
    agent_name: str,
    harness: str,
    cwd: str | None,
    tool_profile: AgentToolProfile,
    containerized: bool = False,
) -> AgentRuntimeContext | None:
    """Resolve/materialize one P2 context, or return ``None`` for legacy mode.

    An explicit Agent config opts that identity into P2 for the three supported
    harnesses.  DSH/residents without an explicit C stay untouched.  A P2
    context never silently falls back when its entry cannot carry the contract.
    """

    agent = registry.require(agent_name)
    if agent.config is None or harness not in _P2_HARNESSES:
        return None
    if containerized:
        raise AgentRuntimeError(
            "agent-home P2 is not supported for containerized launches"
        )
    config = require_agent_config(agent.config, actor=agent.actor)
    receipt = registry.home_receipt(agent.actor)
    agent_home = Path(receipt.path)
    validate_agent_config_location(config, agent_home=agent_home, cwd=cwd)
    manifest = config.freeze_manifest()
    projection = build_native_projection(manifest, harness)

    revision_root = agent_home / "state" / "config" / agent.entity_token / manifest.revision
    projection_parent = revision_root / "native"
    projection_root = projection_parent / harness
    native_root = agent_home / "secrets" / "native" / harness
    session_root = (
        agent_home / "state" / "pi"
        if harness == "pi"
        else agent_home / "state" / "sessions" / harness
    )
    tool_home = agent_home / "state" / "home"
    xdg_config_home = agent_home / "secrets" / "tools" / "xdg"
    xdg_root = agent_home / "state" / "xdg"
    tool_profile_root = revision_root / "tools" / tool_profile.profile_id

    for directory in (
        revision_root,
        projection_parent,
        native_root,
        session_root,
        tool_home,
        xdg_config_home,
        xdg_root / "cache",
        xdg_root / "data",
        xdg_root / "state",
        tool_profile_root,
    ):
        _ensure_private_directory(agent_home, directory)

    incumbent = None
    if projection_root.exists() or projection_root.is_symlink():
        incumbent = ConfigProjectionReceipt(
            actor=agent.uri,
            entity_token=agent.entity_token,
            source_digest=projection.source_digest,
            harness=projection.harness,
            mapping_version=projection.mapping_version,
            projection_digest=projection.digest,
            projection_root=str(projection_root),
            items=projection.items,
        )
    projection_receipt = materialize_native_projection(
        config,
        manifest,
        projection,
        actor=agent.uri,
        entity_token=agent.entity_token,
        projection_root=projection_root,
        incumbent=incumbent,
    )
    tool_environment = _materialize_tool_profile(tool_profile_root, tool_profile)
    roots = AgentRuntimeRoots(
        agent_home=agent_home,
        config_source=Path(config.source),
        projection_root=projection_root,
        native_root=native_root,
        session_root=session_root,
        tool_home=tool_home,
        xdg_config_home=xdg_config_home,
        xdg_cache_home=xdg_root / "cache",
        xdg_data_home=xdg_root / "data",
        xdg_state_home=xdg_root / "state",
        tool_profile_root=tool_profile_root,
    )
    environment = {
        "HOME": str(tool_home),
        "XDG_CONFIG_HOME": str(xdg_config_home),
        "XDG_CACHE_HOME": str(xdg_root / "cache"),
        "XDG_DATA_HOME": str(xdg_root / "data"),
        "XDG_STATE_HOME": str(xdg_root / "state"),
        _NATIVE_ROOT_ENV[harness]: str(native_root),
        **tool_environment,
    }
    return AgentRuntimeContext(
        actor=agent.uri,
        entity_token=agent.entity_token,
        harness=harness,
        manifest=manifest,
        projection=projection,
        projection_receipt=projection_receipt,
        roots=roots,
        tool_profile_id=tool_profile.profile_id,
        environment_items=tuple(sorted(environment.items())),
    )


def _materialize_tool_profile(
    root: Path, profile: AgentToolProfile
) -> dict[str, str]:
    gitconfig = root / "gitconfig"
    git_body = (
        "[user]\n"
        f"\tname = {profile.git_author_name}\n"
        f"\temail = {profile.git_author_email}\n"
    ).encode()
    if profile.ssh is not None:
        git_body += (
            f"[url \"{profile.git_rewrite_target}\"]\n"
            f"\tinsteadOf = {profile.git_rewrite_source}\n"
        ).encode()
    _write_or_verify_private_file(gitconfig, git_body)
    environment = {
        "GIT_CONFIG_GLOBAL": str(gitconfig),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": profile.git_author_name,
        "GIT_AUTHOR_EMAIL": profile.git_author_email,
        "GIT_COMMITTER_NAME": profile.git_author_name,
        "GIT_COMMITTER_EMAIL": profile.git_author_email,
        "HYPRIAL_TOOL_PROFILE_ID": profile.profile_id,
        "HYPRIAL_TEA_LOGIN": profile.tea_login,
    }
    if profile.ssh is None:
        return environment
    ssh = profile.ssh
    ssh_config = root / "ssh_config"
    ssh_body = (
        f"Host {ssh.host} {ssh.hostname}\n"
        f"    HostName {ssh.hostname}\n"
        f"    User {ssh.user}\n"
        "    IdentitiesOnly yes\n"
        f"    IdentityFile {ssh.identity_file}\n"
        f"    IdentityAgent {ssh.auth_sock}\n"
        f"    UserKnownHostsFile {ssh.known_hosts_file}\n"
        "    StrictHostKeyChecking yes\n"
        "    PasswordAuthentication no\n"
        "    KbdInteractiveAuthentication no\n"
    ).encode()
    _write_or_verify_private_file(ssh_config, ssh_body)
    environment.update(
        {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": (
                f"url.{profile.git_rewrite_target}.insteadOf"
            ),
            "GIT_CONFIG_VALUE_0": profile.git_rewrite_source,
            "SSH_AUTH_SOCK": ssh.auth_sock,
            "GIT_SSH_COMMAND": f"ssh -F {shlex.quote(str(ssh_config))}",
        }
    )
    return environment


def _ensure_private_directory(root: Path, target: Path) -> None:
    try:
        relative = target.relative_to(root)
    except ValueError as error:
        raise AgentRuntimeError("runtime directory escaped the agent home") from error
    current = root
    for part in relative.parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            pass
        try:
            metadata = current.lstat()
        except OSError as error:
            raise AgentRuntimeError(f"cannot inspect runtime directory {part}") from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise AgentRuntimeError(f"runtime directory {part} is not private")


def _write_or_verify_private_file(path: Path, body: bytes) -> None:
    expected = hashlib.sha256(body).digest()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        try:
            metadata = path.lstat()
            actual = path.read_bytes()
        except OSError as error:
            raise AgentRuntimeError(f"cannot verify tool profile file {path.name}") from error
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != os.getuid()
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or hashlib.sha256(actual).digest() != expected
        ):
            raise AgentRuntimeError(f"tool profile file {path.name} drifted")
        return
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(body)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
