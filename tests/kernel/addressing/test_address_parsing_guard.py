"""CI 守卫:地址解析唯一(Allen:URI 产生点唯一 —— 一个写者,也一个读者).

`agent:`/`channel:` 身份文法的解构只能发生在
`hyprial.uri`(`parse_agent_uri` / `parse_channel_uri` /
`agent_uri_actor` / `short_actor_name`)。src/ 其它任何文件里的裸
`split(":")`/`rsplit(":", 1)`/`partition(":")`/`startswith("agent:")`/
`removeprefix("agent:")`/`startswith("channel:")` 都是暗道解析。
白名单只含**与身份无关的文法**(每条注明其所有者);新增身份解析必须
进 uri 模块。变异证明:在任意 src 文件造一处裸解析,本测试必须红。

白名单的另一半纪律是**只能收缩**:每条豁免必须**恰好命中一行现行代码**
(见 `test_whitelist_entries_match_exactly_one_live_line`)。空条目不是
无害的历史残留 —— 它等于给"未来某个作者在那条路径上写的下一行"预留了
一张免费豁免;2026-10-13 实测有两条已经这样腐坏。
"""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[3] / 'src/hyprial'


_TOKENS = (
    '.split(":")',
    '.rsplit(":", 1)',
    '.partition(":")',
    '.startswith("agent:")',
    '.removeprefix("agent:")',
    '.startswith("channel:")',
)

# (相对路径, 行片段) —— 与身份无关的既有文法,每条注明所有者。
# 白名单收缩方向:只出不进。新增条目必须在 PR 里说明文法所有者是谁;
# 并且必须恰好命中一行现行代码(空条目/宽条目由下面的守卫测试钉住)。
_WHITELIST: tuple[tuple[str, str], ...] = (
    # route:<adapter>:<route> 文法的所有者(RouteTarget.parse)。
    ("daemon/impl/network/route_delivery.py", 'parts = value.split(":")'),
    # harness:provider:model CLI 组合参数(_parse_combo)。
    # M0.3 叶子切分之后,这份文法的唯一持有者是 agent/runtime/tiers.py:
    # squire 那条命令的入口已经不再持有该文法,因此不再占白名单条目。
    ("shell/impl/cli/commands/agent/runtime/tiers.py", 'parts = raw.split(":")'),
    # active:<harnesses>:<files>:<bytes> 落地回执文法,所有者是
    # p2/orchestration/target.py 的 _parse_active_receipt —— 回执自身是耐久
    # 见证,与 agent:/channel: 身份 URI 无关,不进 uri 模块。
    (
        "daemon/impl/transfer/p2/orchestration/target.py",
        'parts = receipt.split(":")',
    ),
    # channel 进程身份标记的 scheme:value 分解(mcp/channel 自有文法)。
    ("daemon/impl/mcp/channel/ownership.py", 'scheme, separator, value = piece.partition(":")'),
    # 目标类型分类文法的所有者(classify_target_identity)。
    ("daemon/impl/configuration/identity.py", 'scheme, _, remainder = value.partition(":")'),
    ("daemon/impl/configuration/identity.py", 'parts = value.split(":")'),
    # PAC activation marker pac:<graph>:<node>:<incarnation>:<effect>, written
    # into desired-state nicknames; its single owner is pac/actors/marker.py.
    ("daemon/impl/pac/actors/marker.py", 'parts = nickname.split(":")'),
)


def _is_whitelisted(relpath: str, line: str) -> bool:
    return any(
        relpath == path and fragment in line for path, fragment in _WHITELIST
    )


def _matching_lines() -> dict[tuple[str, str], list[str]]:
    """Whitelist entry -> the现行 lines it exempts (`relpath:lineno`)."""

    hits: dict[tuple[str, str], list[str]] = {entry: [] for entry in _WHITELIST}
    for path in sorted(SRC.rglob("*.py")):
        relpath = str(path.relative_to(SRC))
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            for entry in _WHITELIST:
                if relpath == entry[0] and entry[1] in line:
                    hits[entry].append(f"{relpath}:{lineno}")
    return hits


def test_no_bare_address_parsing_outside_the_uri_module() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        relpath = str(path.relative_to(SRC))
        if relpath == "kernel/impl/primitives/uri.py":
            continue  # the one agent/channel URI reader
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if any(token in line for token in _TOKENS) and not _is_whitelisted(
                relpath, line
            ):
                offenders.append(f"{relpath}:{lineno}: {line.strip()}")

    assert not offenders, (
        "bare address parsing outside hyprial/uri.py — decompose "
        "URIs only through parse_agent_uri/parse_channel_uri "
        "(URI 产生点唯一,读写同源):\n" + "\n".join(offenders)
    )


def test_whitelist_entries_match_exactly_one_live_line() -> None:
    """每条豁免必须恰好命中一行现行代码 —— 白名单只能收缩.

    空条目是白名单腐坏:文件改名、行被改写、或整个文法搬到别处之后,键还
    留在表里 ⇒ 它不再是"对某段历史文法的豁免",而是"对那条路径上未来某一行
    的免费放行"。宽条目同理:一条注释声称的文法比实际放行的面窄。

    2026-10-13 实测到的两条空条目(本次已删):
      · `squire/squire.py :: parts = raw.split(":")` —— M0.3 叶子切分把
        harness:provider:model 文法搬去了 `agent/runtime/tiers.py`;
      · `transport/zenoh/config.py :: rsplit(":", 1)` —— 该行被改写为
        `address.rpartition(":")`,而 `rpartition(":")` 不在 `_TOKENS` 里,
        所以既不再命中、也没被主守卫接住。
    """

    rotten = {
        f"{path} :: {fragment}": found
        for (path, fragment), found in _matching_lines().items()
        if len(found) != 1
    }
    assert not rotten, (
        "whitelist entries must match exactly one live line — 白名单只能收缩 "
        "(空条目 = 给未来某一行预留的免费豁免;宽条目 = 比注释声称的更宽):\n"
        + "\n".join(f"  {key} -> {found}" for key, found in sorted(rotten.items()))
    )
