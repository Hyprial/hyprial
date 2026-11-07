from __future__ import annotations

from hyprial.daemon.impl.desired_state import ServiceRegistry
from hyprial.daemon.impl.orgfs.runtime import OrgFsRuntime
from hyprial.biz.impl.service.discovery import discover_installed_services
from hyprial.daemon.impl.service_connect import (
    CatalogRegistry,
    ServiceConnectError,
    parse_catalog_document,
)

from .support import catalog_document


def _cache(*documents: str) -> ServiceRegistry:
    entries = [parse_catalog_document("api.md", text).to_json() for text in documents]
    return ServiceRegistry.from_json(
        {
            "schemaVersion": 1,
            "spaceId": "space-services",
            "owner": "user:owner",
            "cachedAtMs": 1,
            "entries": entries,
        }
    )


def test_builtin_override_addition_deletion_and_valid_empty(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path / "orgfs", node_id="node-a", author="user:owner")
    try:
        space = runtime.facade.create_space("services")
        override = catalog_document(local_port=19090, guide="org override")
        extra = catalog_document(
            name="echo", device_id="echo", remote_port=9000, local_port=None
        )
        runtime.facade.write_text(space.space_id, "api.md", override)
        runtime.facade.write_text(space.space_id, "echo.md", extra)

        loaded = CatalogRegistry(
            runtime.facade, space_id=space.space_id, owner="user:owner",
            discovered=discover_installed_services,
        ).load()
        assert loaded.source == "orgfs"
        assert loaded.entry("api").local_port == 19090
        assert loaded.entry("echo").remote_port == 9000
        assert loaded.entry("api").guide == "org override\n"
        # The shipped subapi sample rides along with the OrgFS overrides.
        assert loaded.entry("subapi").remote_port == 8080
        assert loaded.cache is not None
        assert {entry.name for entry in loaded.cache.entries} == {"api", "echo"}

        for node in runtime.facade.listdir(space.space_id, "id:root"):
            runtime.facade.remove(space.space_id, f"id:{node.node_id}")
        empty = CatalogRegistry(
            runtime.facade,
            space_id=space.space_id,
            owner="user:owner",
            cached=loaded.cache,
            discovered=discover_installed_services,
        ).load()
        assert empty.source == "orgfs"
        assert empty.cache is not None and empty.cache.entries == ()
        # No OrgFS entries ⇒ the shipped subapi sample is what remains.
        assert empty.entry("subapi").remote_port == 8080
        assert empty.find("api") is None
    finally:
        runtime.close()

def test_invalid_snapshot_falls_back_only_to_matching_last_good_cache() -> None:
    cached = _cache(catalog_document(local_port=19090))

    class BrokenOrgfs:
        def spaces(self):
            raise RuntimeError("secret raw orgfs diagnostic")

    fallback = CatalogRegistry(
        BrokenOrgfs(),
        space_id="space-services",
        owner="user:owner",
        cached=cached,
        discovered=discover_installed_services,
    ).load()
    assert fallback.source == "cache"
    assert fallback.entry("api").local_port == 19090
    assert "secret raw" not in (fallback.error or "")

    changed_pin = CatalogRegistry(
        BrokenOrgfs(),
        space_id="new-space",
        owner="user:owner",
        cached=cached,
        discovered=discover_installed_services,
    ).load()
    assert changed_pin.source == "discovery"
    assert changed_pin.entry("subapi").remote_port == 8080


def test_parser_rejects_ambiguous_types_templates_and_public_secrets() -> None:
    invalid = (
        catalog_document().replace("name: api\n", "name: api\nname: duplicate\n"),
        catalog_document().replace("remotePort: 8080", "remotePort: true"),
        catalog_document(env="{TOKEN: '{port.__class__}'}"),
        catalog_document(auth="https://user:password@example.test/key"),
        catalog_document(guide="paste tcSecretAddressMaterialHere now"),
    )
    for document in invalid:
        try:
            parse_catalog_document("api.md", document)
        except ServiceConnectError as error:
            assert error.code == "SERVICE_REGISTRY_INVALID"
            assert "password" not in str(error)
            assert "tcSecret" not in str(error)
        else:
            raise AssertionError("invalid catalog document was accepted")


def test_parser_rejects_dangerous_or_noncanonical_environment_names() -> None:
    for key in (
        "PATH",
        "LD_PRELOAD",
        "DYLD_INSERT_LIBRARIES",
        "PYTHONPATH",
        "HOME",
        "SHELL",
        "BASH_ENV",
        "ENV",
        "lowercase",
        "_PRIVATE",
    ):
        try:
            parse_catalog_document(
                "api.md", catalog_document(env=f"{{{key}: 'safe-{{port}}'}}")
            )
        except ServiceConnectError as error:
            assert error.code == "SERVICE_REGISTRY_INVALID"
        else:
            raise AssertionError(f"dangerous environment name was accepted: {key}")

    accepted = parse_catalog_document(
        "api.md", catalog_document(env="{SERVICE_API_URL: 'http://127.0.0.1:{port}'}")
    )
    assert accepted.env == (("SERVICE_API_URL", "http://127.0.0.1:{port}"),)


def test_root_layout_stem_and_trust_pin_are_enforced(tmp_path) -> None:
    runtime = OrgFsRuntime(tmp_path / "orgfs", node_id="node-a", author="user:owner")
    try:
        space = runtime.facade.create_space("services")
        runtime.facade.write_text(space.space_id, "wrong.md", catalog_document())
        result = CatalogRegistry(
            runtime.facade, space_id=space.space_id, owner="user:owner",
            discovered=discover_installed_services,
        ).load()
        assert result.source == "discovery"
        assert result.error == "service catalog is invalid"

        wrong_owner = CatalogRegistry(
            runtime.facade, space_id=space.space_id, owner="user:other",
            discovered=discover_installed_services,
        ).load()
        assert wrong_owner.source == "discovery"
        assert wrong_owner.error == "service catalog is unavailable"
    finally:
        runtime.close()


def test_same_name_conflict_is_observable_and_orgfs_wins(tmp_path) -> None:
    """M6 兜底:翻转合并优先级(让 discovered 赢)必须红;冲突必须可观测。

    review ② c/④:上一轮(980f6aefe)优先级虽对,但 M6 下 49 passed ——
    判据没有兜底;且 RegistrySnapshot 无冲突标记,有/无冲突 source 同值,
    只能靠比对值推断遮蔽发生过。
    """

    runtime = OrgFsRuntime(tmp_path / "orgfs", node_id="node-a", author="user:owner")
    try:
        space = runtime.facade.create_space("services")
        # OrgFS 显式条目:同名 subapi,端口 9999(与随包 8080 形成可辨差异)。
        shadowing = catalog_document(
            name="subapi", device_id="sub2api", remote_port=9999, local_port=19999
        )
        runtime.facade.write_text(space.space_id, "subapi.md", shadowing)

        loaded = CatalogRegistry(
            runtime.facade, space_id=space.space_id, owner="user:owner",
            discovered=discover_installed_services,
        ).load()
        # 优先级:显式 OrgFS 条目赢(翻转 dict.update 顺序 ⇒ 本断言红)。
        assert loaded.entry("subapi").remote_port == 9999
        assert loaded.entry("subapi").local_port == 19999
        # 可观测信号:冲突名单里出现被遮蔽的服务名(抹掉 conflicts 计算 ⇒ 红)。
        assert loaded.conflicts == ("subapi",)
        # 无冲突对照:冲突名单为空,不是"永远报一个"。
        plain = CatalogRegistry(
            runtime.facade, space_id=space.space_id, owner="user:owner",
            discovered=discover_installed_services,
        )
        for node in runtime.facade.listdir(space.space_id, "id:root"):
            runtime.facade.remove(space.space_id, f"id:{node.node_id}")
        no_conflict = plain.load()
        assert no_conflict.conflicts == ()
        assert no_conflict.entry("subapi").remote_port == 8080
    finally:
        runtime.close()
