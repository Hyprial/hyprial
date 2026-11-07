"""CI 守卫:URI 产生点唯一(Allen)。

`agent:` URI 只能由 `hyprial.uri.canonical_agent_uri` 构造。
src/ 下的 f-string 拼接(`f"agent:…"`)是一条不受类型检查、不做段校验
的暗道 —— 每一处都可能产生构造函数拒绝的形态。本测试扫描 src/ 全文,
发现一处拼接即红;变异证明:在任意 src 文件造一处拼接,本测试必须失败。
"""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / 'src/hyprial'

# f-string 拼接的两种引号形态。canonical_agent_uri 自身用
# f"{AGENT_URI_PREFIX}…"（前缀是常量，不是字面量拼接），不在此列。
# 2026-09-28 起同样禁止 `f"orgfs:` 拼接：orgfs 节点 URI 只能由
# `canonical_orgfs_uri` 构造（Q4=A；provenance 串也是规范 URI + @version）。
_SPLICE_TOKENS = ('f"agent:', "f'agent:", 'f"orgfs:', "f'orgfs:")


def test_no_f_string_agent_uri_splices_in_src() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if any(token in line for token in _SPLICE_TOKENS):
                offenders.append(f"{path.relative_to(SRC)}:{lineno}: {line.strip()}")

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
