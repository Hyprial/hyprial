"""HTTP send reconciliation binds the handler's preferred actor selector."""

from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY


def test_send_lookup_http_profile_binds_actor_without_changing_ipc_aliases() -> None:
    spec = METHOD_REGISTRY.get("message.send.lookup").spec
    assert spec.http_exposed is True
    assert spec.mutates is False and spec.retry == "safe"
    assert spec.effective_http_binding.identity.mode == "local-owner-user-selector"
    assert spec.effective_http_binding.identity.param == "actor"
    assert "actor" not in spec.params_schema.get("required", ())
