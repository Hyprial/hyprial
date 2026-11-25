"""Canonical local node-id resolution.

The node id lives in one file in the HYPRIAL home, minted once when the home
is created (owner decision, 2026-11-20).  The host name is never read for
identity; an existing install writes its old value into the file once (see
the migration guide named in :class:`NodeIdNotConfigured`).
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from hyprial.kernel.impl.configuration.home import configured_hyprial_home

_LOG = logging.getLogger(__name__)

NODE_ID_FILE = "node-id"
MIGRATION_GUIDE = "docs/guides/migration-im-push-by-agent-binding.md"


class NodeIdNotConfigured(ValueError):
    """The home has no usable node-id file and ``HYPRIAL_NODE_ID`` is unset."""

    code = "NODE_ID_NOT_CONFIGURED"

    def __init__(self, path: Path, reason: str) -> None:
        self.path = path
        self.data: dict[str, Any] = {"path": str(path), "guide": MIGRATION_GUIDE}
        super().__init__(
            f"node id file {reason}: {path}; see {MIGRATION_GUIDE} "
            "(section: node id file)"
        )


def node_id_path(home: Path) -> Path:
    """Where *home* keeps its node id."""

    return Path(home) / NODE_ID_FILE


def mint_node_id(home: Path) -> str:
    """Write a fresh ``d.<32 hex>`` node id into *home* and return it.

    Exclusive create: an existing file is never replaced.
    """

    value = f"d.{secrets.token_hex(16)}"
    with node_id_path(home).open("x", encoding="utf-8") as stream:
        stream.write(value + "\n")
    return value


def read_node_id(home: Path) -> str:
    """The stripped content of *home*'s node-id file; refuse missing or empty."""

    path = node_id_path(home)
    try:
        value = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        raise NodeIdNotConfigured(path, "is missing") from None
    if not value:
        raise NodeIdNotConfigured(path, "is empty")
    return value


def resolve_node_id(
    environ: Mapping[str, str] | None = None,
    *,
    warn: Callable[[str], object] | None = None,
) -> str:
    """Return ``HYPRIAL_NODE_ID`` or the value in the home's node-id file.

    An explicitly configured but empty value is ignored with one warning.
    No caller may receive an empty node id.
    """

    source = os.environ if environ is None else environ
    if "HYPRIAL_NODE_ID" in source:
        configured = source["HYPRIAL_NODE_ID"].strip()
        if configured:
            return configured
        message = (
            "HYPRIAL_NODE_ID is empty after trimming; ignoring it and reading "
            "the home's node-id file"
        )
        (warn or _LOG.warning)(message)
    return read_node_id(configured_hyprial_home(source)[0])
