"""Production identity reads use the ordinary registry, not test registration."""


def test_identity_reads_and_refusals_on_ordinary_product_entry(http_server):
    def post(method, params, **kwargs):
        return http_server.request(
            "POST", "/daemon/request/" + method, body=params, **kwargs
        )

    status, _, payload = post("identity.users.list", {})
    assert (status, payload.get("dispatch")) == (200, "dispatched"), payload
    assert isinstance(payload["result"]["users"], list)
    assert payload["requestId"]

    status, _, payload = post("identity.users.list", {"owner": "forged"})
    assert (status, payload["code"], payload["dispatch"]) == (
        400,
        "INVALID_ARGUMENT",
        "not-dispatched",
    )
    status, _, payload = post(
        "identity.users.list", {}, token="wrong-test-token"
    )
    assert (status, payload["code"], payload["dispatch"]) == (
        401,
        "HTTP_UNAUTHORIZED",
        "not-dispatched",
    )

    status, _, payload = post("identity.users.add", {})
    assert (status, payload["code"]) == (400, "INVALID_ARGUMENT"), payload
    assert payload["dispatch"] == "not-dispatched"
