"""Canonical local node-id resolution."""

from __future__ import annotations

import logging
import os
import socket
from collections.abc import Callable, Mapping

_LOG = logging.getLogger(__name__)


def resolve_node_id(
    environ: Mapping[str, str] | None = None,
    *,
    hostname: Callable[[], str] | None = None,
    warn: Callable[[str], object] | None = None,
) -> str:
    """Return ``HYPRIAL_NODE_ID`` or the stripped host name.

    An explicitly configured but empty value is ignored with one warning.
    No caller may receive an empty node id.
    """

    source = os.environ if environ is None else environ
    if "HYPRIAL_NODE_ID" in source:
        configured = source["HYPRIAL_NODE_ID"].strip()
        if configured:
            return configured
        message = (
            "HYPRIAL_NODE_ID is empty after trimming; ignoring it and falling "
            "back to the host name"
        )
        (warn or _LOG.warning)(message)
    fallback = (hostname or socket.gethostname)().strip()
    if not fallback:
        raise ValueError("node id and host name must not be empty")
    return fallback
