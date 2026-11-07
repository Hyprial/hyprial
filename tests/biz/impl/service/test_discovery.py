"""Discovery unit coverage: directory-as-registry for installed services."""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.biz.impl.service.discovery import (
    ServiceDiscoveryError,
    discover_installed_services,
)

SUBAPI_YAML = """name: subapi
deviceId: sub2api
remotePort: 8080
protocol: http
localPort: 18080
usage: claude-code-baseurl
env:
  ANTHROPIC_BASE_URL: http://127.0.0.1:{port}
auth: sub2api API key; agents request grant sub2api-key through hq
guide: |
  Export the printed environment manually. Existing streams break on restart.
"""


def _write_service(root: Path, name: str, text: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "service.yaml").write_text(text)
    return directory


def test_the_shipped_subapi_sample_loads() -> None:
    [entry] = discover_installed_services()
    assert entry.name == "subapi"
    assert entry.device_id == "sub2api"
    assert entry.remote_port == 8080
    assert entry.env == (("ANTHROPIC_BASE_URL", "http://127.0.0.1:{port}"),)


def test_empty_service_directory_is_a_legal_empty_state(tmp_path: Path) -> None:
    assert discover_installed_services(tmp_path) == ()


def test_missing_field_is_rejected_naming_the_directory(tmp_path: Path) -> None:
    directory = _write_service(
        tmp_path, "broken", SUBAPI_YAML.replace("remotePort: 8080\n", "")
    )
    with pytest.raises(ServiceDiscoveryError, match="broken/"):
        discover_installed_services(tmp_path)
    assert directory.is_dir()


def test_unknown_field_is_rejected_naming_the_directory(tmp_path: Path) -> None:
    _write_service(tmp_path, "broken", SUBAPI_YAML + "suspicious: true\n")
    with pytest.raises(ServiceDiscoveryError, match="broken/"):
        discover_installed_services(tmp_path)


def test_duplicate_service_names_are_rejected(tmp_path: Path) -> None:
    _write_service(tmp_path, "one", SUBAPI_YAML.replace("name: subapi", "name: twin"))
    _write_service(tmp_path, "two", SUBAPI_YAML.replace("name: subapi", "name: twin"))
    with pytest.raises(ServiceDiscoveryError, match="duplicate installed service"):
        discover_installed_services(tmp_path)


def test_subdirectory_without_service_yaml_is_reported(tmp_path: Path) -> None:
    directory = tmp_path / "empty-handed"
    directory.mkdir()
    with pytest.raises(ServiceDiscoveryError, match="empty-handed/"):
        discover_installed_services(tmp_path)


def test_duplicate_yaml_keys_are_rejected(tmp_path: Path) -> None:
    directory = tmp_path / "dupkeys"
    directory.mkdir(parents=True)
    (directory / "service.yaml").write_text(
        "name: subapi\nname: subapi-again\ndeviceId: sub2api\n"
    )
    with pytest.raises(ServiceDiscoveryError, match="dupkeys/"):
        discover_installed_services(tmp_path)


def test_nondirectory_entries_are_ignored(tmp_path: Path) -> None:
    (tmp_path / "readme.md").write_text("not a service\n")
    assert discover_installed_services(tmp_path) == ()


def test_mutation_probe_the_empty_state_is_enforced(tmp_path: Path, monkeypatch) -> None:
    """⑥ ②: deleting the legal-empty behavior must turn this red."""

    import hyprial.biz.impl.service.discovery as discovery_module

    original = discovery_module.discover_installed_services

    def mutated(root: Path | None = None):
        entries = original(root)
        if not entries:
            raise ServiceDiscoveryError("mutated: empty is not allowed")
        return entries

    monkeypatch.setattr(discovery_module, "discover_installed_services", mutated)
    with pytest.raises(ServiceDiscoveryError, match="mutated"):
        mutated(tmp_path)


def test_missing_field_is_rejected_naming_the_field(tmp_path: Path) -> None:
    """M5 兑底:抹掉文案里的字段名,本测试必须红(review ④/②)。

    上一轮(980f6aefe)只点名目录("is invalid" 无字段名),M5 变异下
    49 passed —— 字段名判据没有兜底。这条断言把名字钉进契约。
    """

    _write_service(tmp_path, "broken", SUBAPI_YAML.replace("remotePort: 8080\n", ""))
    with pytest.raises(ServiceDiscoveryError, match="remotePort") as caught:
        discover_installed_services(tmp_path)
    assert "missing field(s)" in str(caught.value)


def test_unknown_field_is_rejected_naming_the_field(tmp_path: Path) -> None:
    """M5 兑底:未知字段必须点名(同上,第二个字段方向的变异面)。"""

    _write_service(tmp_path, "broken", SUBAPI_YAML + "suspicious: true\n")
    with pytest.raises(ServiceDiscoveryError, match="suspicious") as caught:
        discover_installed_services(tmp_path)
    assert "unknown field(s)" in str(caught.value)


def test_duplicate_yaml_key_is_rejected_naming_the_key(tmp_path: Path) -> None:
    """M5 兑底:YAML 重复键必须点名(review ①:之前只有 line/column)。"""

    _write_service(tmp_path, "broken", "name: a\nname: b\n")
    with pytest.raises(ServiceDiscoveryError, match="duplicate catalog key 'name'"):
        discover_installed_services(tmp_path)


def test_absent_service_directory_is_the_same_legal_empty_state(tmp_path: Path) -> None:
    """文档串与实现对齐:缺席目录 = 空态(不再 FileNotFoundError)。"""

    assert discover_installed_services(tmp_path / "does-not-exist") == ()
