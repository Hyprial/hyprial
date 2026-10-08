"""Observe Codex versions without using them as launch admission gates."""

from __future__ import annotations

import re
from collections.abc import Mapping

from hyprial.identity import load_packaged_support_matrix
from hyprial.kernel import Logger


_CODEX_USER_AGENT_VERSION = re.compile(
    r"^[^/\s]+/(?P<version>\d+\.\d+\.\d+)(?:[-+][^\s]+)?(?:\s|$)"
)


def warn_if_unrecorded_codex_version(
    initialize: object, logger: Logger | None
) -> str | None:
    """Warn once for this initialize receipt when its Codex version is new."""

    if logger is None or not isinstance(initialize, Mapping):
        return None
    user_agent = initialize.get("userAgent")
    if not isinstance(user_agent, str):
        return None
    matched = _CODEX_USER_AGENT_VERSION.match(user_agent)
    if matched is None:
        return None
    version = matched.group("version")
    recorded = {
        row.key.version
        for row in load_packaged_support_matrix().rows
        if row.key.harness == "codex"
    }
    if version not in recorded:
        logger.warn("codex.version.unrecorded", version=version)
    return version
