"""CI 守卫:`channel:lark:` 不再生(PAC b8d2b52b6358 / NAMB efeeaf0a01ba /
NAMT3 664a7d314947 —— Tier 3 收口,这是终态规则,不再是过渡期规则)。

2026-08-24 迁移裁定后,`channel:lark:` 只应作为**历史数据的兼容读法**存在于
`hyprial.adapters.lark.reply_bridge`(该形态的唯一官方解析入口,和
`hyprial.uri` 的 `channel:<owner>:<machine>:<name>` 四段形态同理)。
任何新代码都不该再写出这个字面量 —— 出站一律 `adapter:lark:`
(`hyprial.uri.ADAPTER_URI_PREFIX` / reply_bridge 的
`_LARK_ADAPTER_PREFIX`)。

2026-08-25(NAMT3)裁定:入站双收**保留**,不设版本周期终止——生产存量的
`channel:lark:*`/`channel:<owner>:<machine>:<name>` 地址(desired-state/
inbox/日志)零迁移零破坏,旧形态靠"没有新写入"自然消亡,不靠主动摘除
读取兼容。`proto/mapping.py` 已整体删除(IRC codec 剔除,同一 PAC),
它原本裸用的 `"channel:"` 七字符前缀(不含 `"channel:lark:"` 这个更具体
的字面量)随文件一起消失,不再需要白名单条目。

变异证明:在任意 src 文件(白名单外)造一处 `"channel:lark:"` 字面量,
本测试必须失败。
"""

from __future__ import annotations

from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[4] / 'src/hyprial'

_TOKENS = ('"channel:lark:', "'channel:lark:")

# (相对路径, 行片段) —— channel:lark: 字面量的唯一官方定义点。
# 白名单收缩方向:只出不进。
_WHITELIST: tuple[tuple[str, str], ...] = (
    (
        "daemon/impl/adapters/lark/contracts/reply_bridge.py",
        '_LARK_CHANNEL_PREFIX = "channel:lark:"',
    ),
)


def _is_whitelisted(relpath: str, line: str) -> bool:
    return any(
        relpath == path and fragment in line for path, fragment in _WHITELIST
    )


def test_no_new_channel_lark_literals_outside_the_reply_bridge_definition() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        relpath = str(path.relative_to(SRC))
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if any(token in line for token in _TOKENS) and not _is_whitelisted(
                relpath, line
            ):
                offenders.append(f"{relpath}:{lineno}: {line.strip()}")

    assert not offenders, (
        "new channel:lark: literal(s) found outside "
        "adapters/lark/reply_bridge.py's legacy-spelling definition — every "
        "new mint must use adapter:lark: (ADAPTER_URI_PREFIX / "
        "_LARK_ADAPTER_PREFIX); channel:lark: is permanent dual-read "
        "compatibility only, never a new write (PAC NAMT3 664a7d314947):\n"
        + "\n".join(offenders)
    )


def test_prefix_guard_rejects_a_new_unwhitelisted_source_file(tmp_path, monkeypatch) -> None:
    import sys

    probe = tmp_path / "new_owner.py"
    probe.write_text('_LARK_CHANNEL_PREFIX = "channel:lark:"\n', encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "SRC", tmp_path)
    with pytest.raises(AssertionError, match="new_owner.py"):
        test_no_new_channel_lark_literals_outside_the_reply_bridge_definition()
