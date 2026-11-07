"""Repository-level contracts for the open-source project documentation."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


README_SECTIONS = [
    "## ✨ What is HyprialOS?",
    "## 🚀 Features",
    "## 🏗️ Architecture",
    "## 📦 Installation",
    "## ⚡ Quickstart",
    "## 📚 Documentation",
    "## 🗺️ Status and roadmap",
    "## 🤝 Contributing",
    "## 🔒 Security",
    "## 📄 License",
]
README_ZH = "docs/i18n/README.zh-CN.md"


def _section(readme: str, heading: str) -> str:
    return readme.split(heading + "\n", 1)[1].split("\n## ", 1)[0]


def test_readme_has_the_public_project_entrypoint_sections() -> None:
    readme = _text("README.md")
    headings = re.findall(r"^#{1,2} .+$", readme, flags=re.MULTILINE)
    assert headings == README_SECTIONS
    intro = _section(readme, README_SECTIONS[0])
    first_paragraph = intro.strip().split("\n\n", 1)[0]
    assert "AI agent" in first_paragraph
    assert "decentralized" in first_paragraph
    assert "durable" in first_paragraph


def test_readme_links_the_chinese_translation_and_back() -> None:
    readme = _text("README.md")
    translation = _text(README_ZH)
    assert f"[简体中文]({README_ZH})" in readme
    assert "[English](../../README.md)" in translation
    assert len(re.findall(r"^## .+$", translation, flags=re.MULTILINE)) == len(
        README_SECTIONS
    )


def test_readme_images_exist_in_the_repository() -> None:
    for path, prefix in (("README.md", ""), (README_ZH, "docs/i18n/")):
        text = _text(path)
        images = re.findall(r'(?:src|srcset)="([^"]+)"', text)
        assert images, f"{path} has no images"
        for image in images:
            resolved = (ROOT / prefix / image).resolve()
            assert resolved.is_file(), f"{path}: missing image {image}"


def test_readme_declares_the_public_repository_a_read_only_mirror() -> None:
    readme = _text("README.md")
    head = readme.split(README_SECTIONS[0], 1)[0]
    assert "read-only release mirror" in head
    assert "do not accept pull requests" in head


def test_operator_details_are_preserved_in_the_linked_runbook() -> None:
    readme = _text("README.md")
    runbook = _text("docs/guides/operator-guide.md")
    assert "[Operator guide](docs/guides/operator-guide.md)" in readme
    for heading in (
        "首次启动:放行 macOS 防火墙",
        "意外发现旧 TS 安装时：先删除旧版，再安装",
        "tag-only 升级与排序规则",
        "自动更新 loop(每天两次，按轨道或最新 tag)",
        # 3d4ffa91 (2026-10-04) aligned the guide with Tailcat: the first two
        # were renamed, and the tsnet join section ("入网(hyprial login
        # U3b:网络段)") was retired with tsnet itself -- its successor is
        # 「登录与设备准备」.
        "网络 profile 与多 home",
        "用户登录（身份与设备 key）",
        "登录与设备准备",
    ):
        assert re.search(rf"^#{{2,3}} {re.escape(heading)}$", runbook, re.MULTILINE)


PUBLIC_REPO = "https://github.com/Hyprial/hyprial"


PUBLIC_INSTALLERS = (
    "curl -fsSL https://dl.hyprial.com/install.sh | sh",
    'powershell -c "irm https://dl.hyprial.com/install.ps1 | iex"',
)


def test_readme_installs_from_the_public_release_repo() -> None:
    """安装入口必须指向公网发布面。

    2026-09-16 起分发改走 ``Hyprial/hyprial``:安装不再需要内网访问权限。
    2026-11-03 起主入口与 www.hyprial.com/quickstart 对齐为 dl.hyprial.com
    的一行安装器（Allen：「一行安装器为主，与 hyprial.com 对齐」），uv + 公网
    git 保留为备选。
    ⛔ 这条断言防的是【反向漂移】—— 安装命令悄悄改回内网 ssh 而没人发现。
    """
    readme = _text("README.md")
    install = _section(readme, "## 📦 Installation")
    for required in PUBLIC_INSTALLERS:
        assert required in install, f"公网安装命令缺失: {required}"
    assert install.index(PUBLIC_INSTALLERS[0]) < install.index(
        f"uv tool install git+{PUBLIC_REPO}.git@internal"
    )
    # Allen 2026-09-16 裁「确认走该通道 / 当前公网安装脚本应该跟 dev」；
    # 2026-09-18 该通道由 dev-track 改名为 internal（去掉与分支重名）。
    # ⛔ 不得退回字面占位符:它在镜像上无法解析,而用户复制后拿到的报错
    # 长得像他自己的网络问题。
    assert "<version-tag>" not in readme
    assert f"uv tool install --force git+{PUBLIC_REPO}.git@v0.4.0" not in readme


def test_readmes_name_no_internal_host() -> None:
    """Security review of #2330 (2026-11-03): the public README and its
    translation give only public addresses; internal remotes come from a
    maintainer, never from a published file."""
    for path in ("README.md", README_ZH):
        text = _text(path)
        assert ".internal." not in text, path
        assert "ssh://" not in text, path
        assert "git clone https://github.com/Hyprial/hyprial.git" in text, path


def test_readme_routes_contributors_through_the_maintainer() -> None:
    """公网读者要能看出:补丁需要先加入开发内网,而入口是联系 maintainer。"""
    readme = _text("README.md")
    dev_section = _section(readme, "## 🤝 Contributing")
    assert "contact a maintainer" in dev_section
    assert "development network" in dev_section
    translation = _text(README_ZH)
    assert "联系 maintainer" in translation
    assert "内网" in translation


def test_license_is_the_complete_apache_2_text() -> None:
    license_text = _text("LICENSE")
    assert license_text.splitlines()[:3] == [
        "Apache License",
        "Version 2.0, January 2004",
        "http://www.apache.org/licenses/",
    ]
    assert "TERMS AND CONDITIONS FOR USE, REPRODUCTION, AND DISTRIBUTION" in license_text
    assert "END OF TERMS AND CONDITIONS" in license_text
    # 96d3ef2d corrected the holder; keep the current notice exact rather
    # than reverting the authoritative LICENSE to satisfy a stale consumer.
    assert "Copyright 2026 Hyprial Tech." in license_text
    assert len(license_text.splitlines()) >= 200


def test_project_metadata_declares_the_apache_2_license() -> None:
    project = tomllib.loads(_text("pyproject.toml"))["project"]
    assert project["license"] == "Apache-2.0"


def test_contributing_reuses_repository_agent_guidance() -> None:
    contributing = _text("CONTRIBUTING.md")
    for reference in (
        ".agents/hooks/README.md",
        ".agents/skills/hyprial-e2e/SKILL.md",
        ".agents/skills/forgejo-ops/SKILL.md",
        ".claude/settings.json",
    ):
        assert reference in contributing
    assert "uv sync --extra test" in contributing
    # The full suite includes the one-off tool pin, as documented by
    # d96d6ef4; an unqualified invocation is no longer the full-suite recipe.
    assert "uv run --with libcst==1.9.0 pytest tests/" in contributing
    assert "npm --prefix src/gui" not in contributing
    assert "docs/design/gui-retirement-restore-ledger-2026-10-27.md" in contributing
    assert "uv run ruff check ." in contributing
