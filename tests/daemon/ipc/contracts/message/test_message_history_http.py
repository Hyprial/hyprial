"""The HTTP history read keeps the daemon's participant-scoped authority."""

from hyprial.daemon.impl.request_catalog.registry import METHOD_REGISTRY


def test_history_has_bound_read_profile_and_original_handler() -> None:
    bound = METHOD_REGISTRY.get("message.history")
    assert bound is not None
    assert bound.spec.http_exposed is True
    assert bound.spec.mutates is False and bound.spec.retry == "safe"
    assert bound.spec.effective_http_binding.identity.mode == "local-owner-user-selector"
    assert bound.spec.effective_http_binding.identity.param == "actor"
    assert bound.spec.effective_http_binding.readiness == "inbox-service"

    class App:
        def _ipc_message_history(self, method, params, _trusted_message_origin=None):
            assert method == "message.history"
            assert params == {"actor": "user:owner", "conversationId": "conv"}
            assert _trusted_message_origin == "ipc"
            return {"ok": True, "conversationId": "conv", "entries": [], "nextCursor": None}

    assert METHOD_REGISTRY.invoke(App(), "message.history", {"actor": "user:owner", "conversationId": "conv"},
                                  trusted_message_origin="ipc")["entries"] == []


def test_history_preserves_legacy_nullable_optional_fields() -> None:
    params = {"actor": "user:owner", "conversationId": "conv", "from": None,
              "onBehalfOf": None, "limit": None, "mode": None}

    class App:
        def _ipc_message_history(self, method, received, _trusted_message_origin=None):
            assert method == "message.history" and received == params
            return {"ok": True, "conversationId": "conv", "entries": [], "nextCursor": None}

    assert METHOD_REGISTRY.invoke(App(), "message.history", params)["ok"] is True
