"""Approved, non-secret tool identity value types for one agent runtime.

AgentToolProfile and SshToolAuthorization moved here unchanged from
runtime/context.py (decision agent-git-identity 2026-11-03) so that profile
construction, content-derived profile ids, and git-author resolution live in
one package.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from hyprial.kernel import DEFAULT_GIT_URL

from .errors import AgentRuntimeError

__all__ = ["AgentToolProfile", "SshToolAuthorization", "derived_profile_id"]

_PROFILE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_SSH_TOKEN = re.compile(r"[A-Za-z0-9._@:-]+\Z")

_PROFILE_ID_PREFIX = "agent-git-v1-"


def _default_git_endpoint() -> tuple[str, str]:
    parsed = urlsplit(DEFAULT_GIT_URL)
    if parsed.hostname is None:
        raise AgentRuntimeError("default Git URL must name a host")
    return parsed.hostname, urlunsplit((parsed.scheme, parsed.netloc, "/", "", ""))


_DEFAULT_GIT_HOSTNAME, _DEFAULT_GIT_REWRITE_TARGET = _default_git_endpoint()


def derived_profile_id(git_author_name: str, git_author_email: str) -> str:
    """A stable profile id for one resolved git identity.

    Content-addressed on purpose: when the owner's configured identity
    changes, the next launch materializes a fresh tool-profile root instead of
    failing the write-or-verify check against a stale gitconfig under the old
    id.  The identity itself stays readable in the profile directory it lands
    in; the id carries none of it.
    """

    digest = hashlib.sha256(
        f"{git_author_name}\x00{git_author_email}".encode()
    ).hexdigest()[:16]
    return f"{_PROFILE_ID_PREFIX}{digest}"


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
