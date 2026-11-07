"""AT10 SPEC：能力枚举 -> 真实入口映射，不得与代码漂移。

卡 359 item 0 要求「把已冻结 capability 枚举对到真实使用入口」，并由本线维护者
先交 SPEC 改版给 cc-sw-review 评审。本守卫把 ``docs/design/design-at10-capability-entry-map.md``
§1 的表钉在代码单一真相源上：枚举项、可授予集合、以及「哪几项真的接了执行入口」
三者对不上就红。
"""

from __future__ import annotations

from pathlib import Path

from hyprial.identity.impl.agents.runtime.enforcement import CAPABILITY_ENTRY_POINTS, GRANTABLE_CAPABILITIES

ROOT = Path(__file__).resolve().parents[3]
DOC = ROOT / "docs" / "design" / "design-at10-capability-entry-map.md"

#: 只有这两项在 daemon 真实入口上有执行点（#1074）。第三项出现时必须先改 SPEC。
WIRED = {"channel", "org-context"}


def _table_rows() -> dict[str, list[str]]:
    rows: dict[str, list[str]] = {}
    for line in DOC.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip().strip("`") for cell in line.strip("|").split("|")]
        if len(cells) < 5:
            continue
        name = cells[0].strip("`")
        if name not in CAPABILITY_ENTRY_POINTS:
            continue
        rows[name] = cells
    return rows


def test_the_mapping_table_covers_every_frozen_capability_exactly_once() -> None:
    rows = _table_rows()
    assert set(rows) == set(CAPABILITY_ENTRY_POINTS), (
        "docs/design/design-at10-capability-entry-map.md §1 必须逐项列出冻结枚举；"
        f"表里是 {sorted(rows)}，代码是 {sorted(CAPABILITY_ENTRY_POINTS)}"
    )


def test_the_grantable_column_matches_the_frozen_grantable_set() -> None:
    rows = _table_rows()
    documented = {name for name, cells in rows.items() if cells[1] == "是"}
    assert documented == set(GRANTABLE_CAPABILITIES)


def test_only_the_wired_capabilities_claim_a_real_entry_point() -> None:
    rows = _table_rows()
    wired = {name for name, cells in rows.items() if cells[4].startswith("**已接线**")}
    assert wired == WIRED, (
        "接了第三个执行入口就必须先改 SPEC（docs/design/design-at10-capability-entry-map.md）"
    )
    for name in WIRED:
        assert "无" not in rows[name][3], f"{name} 必须有真实入口，不能写「无」"


def test_the_withdrawal_limit_declaration_is_present() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert "撤回授权只改变后续判定的答案" in text
    assert "不会" in text and "消失" in text
