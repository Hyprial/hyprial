"""Repository-level contract for the code and CLI rename."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path



ROOT = Path(__file__).resolve().parents[4]
OLD_LOWER = "h2" + "b"
OLD_ENV_PREFIX = "H2" + "B_"
NEW_LOWER = "hyprial"
NEW_ENV_PREFIX = "HYPRIAL_"
CONTRACT_HISTORY = {
    "e2e/e2e-014-real-env-evidence.md",
    "e2e/feishu-live-roundtrip-evidence.md",
}
ALLOWED_CONTRACT_OLD_NAMES = (
    "alicesmith-" + OLD_LOWER + "-e2e",
)

def _scrub_allowed(relative: str, line: str) -> str:
    """Remove every allowed name from ``line``, longest first.

    Longest first matters: a shorter scrub run first would leave a fragment
    of a longer name behind -- which then reads as an unallowed occurrence.
    """
    allowed = ALLOWED_CONTRACT_OLD_NAMES
    for name in sorted(allowed, key=len, reverse=True):
        line = line.replace(name, "")
    return line


def _has_unallowed_old_name(relative: str, line: str) -> bool:
    """The guard's per-line decision, as one callable.

    Extracted so the negative control below exercises the code the guard
    actually runs. A control that re-implements the decision proves only that
    two copies agree.
    """
    return OLD_LOWER in _scrub_allowed(relative, line).lower()


def _contract_texts() -> tuple[list[tuple[Path, str]], list[str]]:
    contract = ROOT / "contract"
    texts: list[tuple[Path, str]] = []
    parse_errors: list[str] = []
    for path in sorted(contract.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\0" in data:
            continue
        try:
            texts.append((path, data.decode("utf-8")))
        except UnicodeDecodeError as error:
            parse_errors.append(f"{path.relative_to(contract)}:{error}")
    return texts, parse_errors


def _live_python_files() -> list[Path]:
    return sorted((ROOT / "src").rglob("*.py")) + sorted(
        (ROOT / "tests").rglob("*.py")
    )


def test_python_package_and_imports_are_renamed() -> None:
    assert (ROOT / "src" / NEW_LOWER).is_dir()
    assert not (ROOT / "src" / OLD_LOWER).exists()

    old_import = re.compile(
        rf"^\s*(?:from\s+{OLD_LOWER}(?:\.|\s)|import\s+{OLD_LOWER}(?:\.|\s|$))",
        re.MULTILINE,
    )
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in _live_python_files()
        if old_import.search(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_distribution_and_cli_entries_are_renamed() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert project["project"]["name"] == NEW_LOWER
    assert project["project"]["scripts"] == {
        NEW_LOWER: f"{NEW_LOWER}.cli:main",
    }
    retired_paths = (
        ROOT / "src" / NEW_LOWER / "migration.py",
        ROOT / "scripts" / "migrate_from_ts.py",
        ROOT / "scripts" / "cleanup-and-migrate-ts.sh",
    )
    assert [
        path.relative_to(ROOT).as_posix()
        for path in retired_paths
        if path.exists()
    ] == []
    assert project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == [
        f"src/{NEW_LOWER}"
    ]


# No live file may name the retired prefix any more.
_ENV_PREFIX_ALLOWANCES: dict[str, str] = {}


def test_live_python_uses_only_the_new_environment_prefix() -> None:
    offenders: list[str] = []
    allowed_seen: list[str] = []
    positive_controls = 0
    for path in _live_python_files():
        relative = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if OLD_ENV_PREFIX in text:
            if relative in _ENV_PREFIX_ALLOWANCES:
                allowed_seen.append(relative)
            else:
                offenders.append(relative)
        positive_controls += text.count(NEW_ENV_PREFIX)
    assert offenders == []
    assert positive_controls > 0
    # ⭐ An allowance that no longer matches anything is a stale exemption: it
    #   would keep a future offender invisible.  ⇒ Every entry must still be
    #   earning its keep.
    assert sorted(allowed_seen) == sorted(_ENV_PREFIX_ALLOWANCES)


def test_code_paths_and_sidecar_module_are_renamed() -> None:
    code_roots = (
        ".forgejo",
        "contract",
        "docker",
        "scripts",
        "sidecar",
        "spikes",
        "src",
        "tests",
    )
    code_paths = [
        path.relative_to(ROOT).as_posix()
        for root in code_roots
        for path in (ROOT / root).rglob("*")
        if path.is_file()
    ]
    offenders = [
        path for path in code_paths if OLD_LOWER in path and not path.startswith("src/gui/")
    ]
    assert offenders == []

    sidecar = ROOT / "sidecar" / f"{NEW_LOWER}-tailcat"
    assert (sidecar / "cmd" / f"{NEW_LOWER}-tailcat").is_dir()
    module = (sidecar / "go.mod").read_text(encoding="utf-8").splitlines()[0]
    assert module.endswith(f"/sidecar/{NEW_LOWER}-tailcat")


def test_live_contract_has_no_unclassified_old_product_names() -> None:
    contract = ROOT / "contract"
    offenders: list[str] = []
    positive_controls = 0
    texts, parse_errors = _contract_texts()
    for path, text in texts:
        relative = path.relative_to(contract).as_posix()
        if relative in CONTRACT_HISTORY:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            positive_controls += line.lower().count(NEW_LOWER)
            if _has_unallowed_old_name(relative, line):
                offenders.append(f"{relative}:{number}:{line}")
    assert parse_errors == []
    assert len(texts) > 0
    assert positive_controls > 0
    assert offenders == []


def test_dead_contract_instructions_are_removed() -> None:
    texts, parse_errors = _contract_texts()
    live = "\n".join(
        text
        for path, text in texts
        if path.relative_to(ROOT / "contract").as_posix() not in CONTRACT_HISTORY
    )
    assert parse_errors == []
    assert len(texts) > 0
    assert "python-sdk/" + NEW_LOWER in live
    assert NEW_LOWER + "-e2e019-worker:local" not in live


def test_contract_alignment_report_records_all_three_exits() -> None:
    report = (ROOT / "docs" / "notes" / "contract-align-report.md").read_text(
        encoding="utf-8"
    )
    for heading in ("## 已换", "## 改名单漏项", "## 已删死指令", "## 需要人点"):
        assert heading in report
    assert "+" + OLD_LOWER + "/ack" in report
    assert "python-sdk/" + OLD_LOWER in report
    assert "属 adapter 改名迁移单,不在本单射程" in report
