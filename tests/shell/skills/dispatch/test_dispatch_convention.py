"""派单三条约定(design-dispatch-always-pac-2026-09-03 §三①)必须有载体且可执行。

hyprial-workflow skill 必须携带「派单三条」全文,且其安装节写明的 pi 安装命令
必须原样可执行(装出 ~/.pi/agent/skills/hyprial-workflow/SKILL.md)。
测试只认真实产物:命令从 skill 文档里解析,不另造形态。
"""

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SKILL_PATH = REPO_ROOT / ".agents" / "skills" / "hyprial-workflow" / "SKILL.md"

CONVENTION_HEADING = "## Dispatch through a PAC graph"
RULE_MARKERS = (
    "hyprial workflow run", "version: 2", "--request-id", "--reason-ref",
    "first_output_eta", "human_gates", "interactive actors may plan, dispatch or",
    "Never use await.match", "Explicit alternatives are continue",
)
INSTALL_HEADING = "## Install(pi)"


def _skill_text() -> str:
    return SKILL_PATH.read_text(encoding="utf-8")


def _install_command() -> str:
    section = _skill_text().split(INSTALL_HEADING, 1)[1]
    fence = section.split("```bash", 1)[1]
    return fence.split("```", 1)[0].strip()


def test_skill_declares_dispatch_three_rules() -> None:
    text = _skill_text()
    assert CONVENTION_HEADING in text
    for marker in RULE_MARKERS:
        assert marker in text, f"派单三条缺失关键词: {marker}"


def test_install_section_command_installs_skill(tmp_path: Path) -> None:
    """安装节的命令在沙箱 HOME 下原样执行,必须装出 skill 目录。"""
    command = _install_command()
    assert command, "安装节没有可执行命令"
    result = subprocess.run(
        command,
        shell=True,
        cwd=REPO_ROOT,
        env=dict(os.environ, HOME=str(tmp_path)),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    installed = tmp_path / ".pi" / "agent" / "skills" / "hyprial-workflow" / "SKILL.md"
    assert installed.is_file(), f"安装命令没有产出 {installed}"
