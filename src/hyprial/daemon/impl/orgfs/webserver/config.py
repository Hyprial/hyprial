from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import ipaddress
import os
import stat
from hyprial.daemon.impl.orgfs.webserver.identity import (
    CLIENT_ADDRESS_MODES,
    CLIENT_ADDRESS_PROXY_V2,
    CLIENT_ADDRESS_SOCKET,
    DEFAULT_RETRY_AFTER_SECONDS,
)



# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ListenEndpoint:
    kind: str  # "tcp" | "unix"
    host: str | None
    port: int | None
    path: Path | None


@dataclass(frozen=True, slots=True)
class WebServiceConfig:
    host: str
    listen: str
    cert: Path
    key: Path
    client_address: str = CLIENT_ADDRESS_PROXY_V2
    retry_after_seconds: int = DEFAULT_RETRY_AFTER_SECONDS
    daemon_socket: Path | None = None


def parse_listen(value: str) -> ListenEndpoint:
    """Parse and validate the ``listen`` setting.

    ``host:port`` (loopback or an explicit non-wildcard address — wildcard
    binds are refused) or ``unix:/absolute/path``.
    """

    if value.startswith("unix:"):
        raw_path = value[len("unix:"):]
        path = Path(raw_path)
        if not raw_path or not path.is_absolute():
            raise ValueError(f"unix listen path must be absolute: {value!r}")
        return ListenEndpoint(kind="unix", host=None, port=None, path=path)
    host, separator, port_text = value.rpartition(":")
    if not separator:
        raise ValueError(f"listen must be host:port or unix:/path: {value!r}")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError(f"listen port is not a number: {value!r}") from None
    if not 0 <= port <= 65535:
        raise ValueError(f"listen port out of range: {value!r}")
    if not host or host == "*":
        raise ValueError(f"wildcard listen addresses are refused: {value!r}")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is None:
        # A host name could resolve to a wildcard (or change later); only a
        # literal address is checkable here.
        raise ValueError(f"listen host must be an IP literal: {value!r}")
    if address.is_unspecified:
        raise ValueError(f"wildcard listen addresses are refused: {value!r}")
    return ListenEndpoint(kind="tcp", host=host, port=port, path=None)


def _validate_config(config: WebServiceConfig) -> ListenEndpoint:
    endpoint = parse_listen(config.listen)
    if config.client_address not in CLIENT_ADDRESS_MODES:
        raise ValueError(
            f"client_address must be one of {sorted(CLIENT_ADDRESS_MODES)}: "
            f"{config.client_address!r}"
        )
    if endpoint.kind == "unix" and config.client_address == CLIENT_ADDRESS_SOCKET:
        raise ValueError(
            "client_address=socket has no peer address on a unix listener; "
            "use client_address=proxy-v2"
        )
    if endpoint.kind != "unix" and config.client_address == CLIENT_ADDRESS_PROXY_V2:
        # Any local process can reach a TCP listener and forge the PROXY v2
        # identity header; the sidecar likewise refuses tcp targets for v2.
        raise ValueError(
            "client_address=proxy-v2 requires a unix: listener in a 0700 "
            "directory; a TCP listener would accept forged identity headers"
        )
    if not config.host.strip():
        raise ValueError("host must be a non-empty host name")
    return endpoint


def _prepare_unix_socket(path: Path) -> None:
    """Create/verify the unix listener's parent (update 3).

    A missing parent is created with mode 0700.  An existing parent that is
    a symlink, not a directory, or world-/group-writable is refused — a
    writable or redirected parent would let another principal steal the
    socket the PROXY-v2 trust depends on.
    """

    parent = path.parent
    try:
        parent_stat = parent.lstat()
    except FileNotFoundError:
        parent.mkdir(mode=0o700)
        os.chmod(parent, 0o700)
    else:
        if stat.S_ISLNK(parent_stat.st_mode):
            raise ValueError(f"unix socket parent is a symlink: {parent}")
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise ValueError(f"unix socket parent is not a directory: {parent}")
        if parent_stat.st_mode & 0o022:
            raise ValueError(
                f"unix socket parent is group- or world-writable: {parent}"
            )
    try:
        existing = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(existing.st_mode):
        raise ValueError(f"unix socket path exists and is not a socket: {path}")
    path.unlink()


def check_config(config: WebServiceConfig) -> list[str]:
    """The ``--check`` surface: every problem found, or an empty list.

    Verifies the listen-address rules and cert/key readability plus
    certificate expiry.  Read-only: it binds nothing and creates nothing.
    """

    problems: list[str] = []
    try:
        _validate_config(config)
    except ValueError as error:
        problems.append(f"listen: {error}")
    cert_bytes: bytes | None = None
    try:
        cert_bytes = Path(config.cert).read_bytes()
    except OSError as error:
        problems.append(f"cert: cannot read {config.cert}: {error}")
    try:
        Path(config.key).read_bytes()
    except OSError as error:
        problems.append(f"key: cannot read {config.key}: {error}")
    if cert_bytes is not None:
        try:
            from cryptography import x509

            certificate = x509.load_pem_x509_certificate(cert_bytes)
            not_after = getattr(certificate, "not_valid_after_utc", None)
            if not_after is None:
                not_after = certificate.not_valid_after
            import datetime

            now = datetime.datetime.now(datetime.timezone.utc)
            if not_after.tzinfo is None:
                not_after = not_after.replace(tzinfo=datetime.timezone.utc)
            if not_after <= now:
                problems.append(f"cert: expired at {not_after.isoformat()}")
        except ValueError as error:
            problems.append(f"cert: cannot parse {config.cert}: {error}")
    return problems
