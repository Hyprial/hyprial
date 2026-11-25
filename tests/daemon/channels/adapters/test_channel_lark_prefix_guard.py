"""CI guard: no source file mints a ``channel:`` address literal.

F1c (Allen 2026-11-23, Q0-b) retired the ``channel:`` adapter-address
spelling: every adapter writes ``adapter:``, and the readers no longer
dual-read.  S3c deleted the last surviving literal (the kernel's user-profile
reader), so the whitelist is empty.

Mutation proof: a ``"channel:x:"``/``f"channel:{...}"`` literal, or the bare
``"channel:"`` prefix, in any non-whitelisted source file must turn this test
red.  Comment lines (file-header decision blocks) are not scanned.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

SRC = Path(__file__).resolve().parents[4] / "src/hyprial"

_LITERAL = re.compile(r"""(?:\bf)?["']channel:["'A-Za-z{<]""")

_WHITELIST: tuple[tuple[str, str], ...] = ()


def _is_whitelisted(relpath: str, line: str) -> bool:
    return any(
        relpath == path and fragment in line for path, fragment in _WHITELIST
    )


def test_no_channel_address_literals_in_source() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        relpath = str(path.relative_to(SRC))
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if line.lstrip().startswith("#"):
                continue
            if _LITERAL.search(line) and not _is_whitelisted(relpath, line):
                offenders.append(f"{relpath}:{lineno}: {line.strip()}")

    assert not offenders, (
        "channel: address literal(s) found; the spelling is retired (F1c), "
        "write adapter: (ADAPTER_URI_PREFIX) instead:\n" + "\n".join(offenders)
    )


@pytest.mark.parametrize(
    "source",
    [
        'PREFIX = "channel:lark:"\n',
        'value = f"channel:telegram:{instance}:{chat}"\n',
        'if value.startswith("channel:"):\n    pass\n',
        "PREFIX = 'channel:'\n",
    ],
)
def test_guard_rejects_a_new_channel_literal(
    tmp_path, monkeypatch, source: str
) -> None:
    import sys

    probe = tmp_path / "new_owner.py"
    probe.write_text(source, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "SRC", tmp_path)
    with pytest.raises(AssertionError, match="new_owner.py"):
        test_no_channel_address_literals_in_source()
