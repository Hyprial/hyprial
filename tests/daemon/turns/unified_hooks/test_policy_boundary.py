"""The §2 boundary test: no embedded policy vocabulary on the hook path.

Restored per review #1228 on #1306 (it guarded the retired v1 configuration
module before).  Per design-turn-hooks-recap §2 the daemon and its unified
hook path carry mechanism only: changing categories, wording, the classifier
agent, questions or which messages are hooked must need no change in
``src/hyprial``.  This is the grep the reviewer runs, kept executable.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]

#: The mechanism-only hook path (unified-hooks H2 layout) plus the delivery
#: prompt renderer a before-delivery hook may inject into.
SCANNED_DIRECTORIES = (
    ROOT / "src/hyprial/kernel/impl/hooks",
    ROOT / "src/hyprial/daemon/impl/hooks",
    ROOT / "src/hyprial/biz/impl/hooks",
)

#: Same vocabulary as the original v1 boundary test: our organisation's
#: working habits must not appear in the code that carries the mechanism.
FORBIDDEN_VOCABULARY = ("jev", "pac", "task category", "routing table", "hint text")


def test_hook_path_contains_no_embedded_policy_vocabulary() -> None:
    from hyprial.daemon.impl import api as daemon_api

    sources = [
        path.read_text(encoding="utf-8")
        for directory in SCANNED_DIRECTORIES
        for path in sorted(directory.rglob("*.py"))
        if "__pycache__" not in path.parts
    ]
    sources.append(inspect.getsource(daemon_api.delivery_prompt))
    source = "\n".join(sources).casefold()
    offenders = [
        term
        for term in FORBIDDEN_VOCABULARY
        if re.search(rf"(?<![\w-]){re.escape(term)}(?![\w-])", source) is not None
    ]
    assert not offenders, (
        f"policy vocabulary {offenders!r} embedded in the hook mechanism path; "
        "policy lives outside the codebase (design-turn-hooks-recap §2)"
    )
