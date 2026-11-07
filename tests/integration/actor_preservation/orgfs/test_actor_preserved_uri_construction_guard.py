"""CI 守卫:URI 产生点唯一(Allen)。

`agent:` URI 只能由 `hyprial.uri.canonical_agent_uri` 构造。
src/ 下的 f-string 拼接(`f"agent:…"`)是一条不受类型检查、不做段校验
的暗道 —— 每一处都可能产生构造函数拒绝的形态。本测试扫描 src/ 全文,
发现一处拼接即红;变异证明:在任意 src 文件造一处拼接,本测试必须失败。
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[4] / 'src/hyprial'

def _is_uri_splice(node: ast.JoinedStr) -> bool:
    literal = "".join(
        value.value
        for value in node.values
        if isinstance(value, ast.Constant) and isinstance(value.value, str)
    )
    return literal.startswith(("agent:", "orgfs:"))


def _joined_string(source: str) -> ast.JoinedStr:
    assignment = ast.parse(source).body[0]
    assert isinstance(assignment, ast.Assign)
    assert isinstance(assignment.value, ast.JoinedStr)
    return assignment.value


def test_uri_splice_classifier_catches_fixed_identity_segments() -> None:
    assert _is_uri_splice(
        _joined_string('value = f"orgfs:{owner}:{space_id}:{node_id}"')
    )
    assert _is_uri_splice(
        _joined_string('value = f"agent:{owner}:{machine}:{actor}"')
    )
    assert _is_uri_splice(
        _joined_string('value = f"agent:fixed-owner:fixed-machine:{actor}"')
    )
    assert not _is_uri_splice(
        _joined_string('value = f"{AGENT_RECEIPT_NAMESPACE}:retire:{operation_id}"')
    )


def test_no_f_string_agent_uri_splices_in_src() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(source, filename=str(path))):
            if isinstance(node, ast.JoinedStr) and _is_uri_splice(node):
                segment = ast.get_source_segment(source, node) or "<f-string>"
                offenders.append(
                    f"{path.relative_to(SRC)}:{node.lineno}: {segment.strip()}"
                )

    assert offenders == [], (
        "agent:/orgfs: URI splices found in src/ — construct URIs only through "
        "uri.canonical_agent_uri / uri.canonical_orgfs_uri (URI 产生点唯一):\n"
        + "\n".join(offenders)
    )


#: The per-tailnet web host is derived from the node's own tailnet records,
#: never a constant (Allen 2026-09-28, design §7a): one org's MagicDNS domain
#: must not appear in src.  Kill-mutation: hard-coding this host in the
#: derivation helper turns this guard red together with the other-domain
#: surface test in tests/test_orgfs_uri.py.
_HARD_CODED_WEB_HOST = "inside" + ".hyprial.ai"


def test_no_hard_coded_orgfs_web_host_in_src() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if _HARD_CODED_WEB_HOST in line:
                offenders.append(f"{path.relative_to(SRC)}:{lineno}: {line.strip()}")

    assert offenders == [], (
        "hard-coded orgfs web host found in src/ — derive the per-tailnet "
        "host through uri.orgfs_web_host, never a constant (design §7a):\n"
        + "\n".join(offenders)
    )
