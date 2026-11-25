"""The real adapter, talking to a stand-in Lark instead of the real one.

What is faked here is the *service*, never our own code and never the SDK.
The gateway under test is the production one, built the production way; only
the host it resolves has moved.  The SDK is the real ``lark_oapi``, because an
SDK stub would drift the moment the vendor ships a release and would tell us
nothing about whether our calls are shaped right.

The three questions from ``_fake_lark`` each get their own test, including
the controls that show each check can fail.  A guard that has never been seen
to fail is indistinguishable from one that cannot.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from hyprial.daemon.impl.adapters.lark.contracts.endpoint import LARK_BASE_URL_ENV
from hyprial.daemon.impl.adapters.lark.outbound.stream import LarkEventStream
from hyprial.daemon.impl.adapters.lark.outbound.sdk_base import LarkSdkError
from hyprial.daemon.impl.adapters.lark.outbound.sdk import LarkSdkGateway

ROOT = Path(__file__).resolve().parents[5]
FAKE = ROOT / "contract" / "e2e" / "_fake_lark.py"


def _fake_module():
    spec = importlib.util.spec_from_file_location("_fake_lark_probe", FAKE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake():
    module = _fake_module()
    with module.FakeLark() as service:
        yield module, service


def test_the_real_gateway_sends_through_the_stand_in(
    fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Question 2: the traffic has to actually arrive here.

    Asserting only that ``send_chat`` returned an id would pass against the
    real service too, which is the whole failure mode -- a call site that was
    never redirected keeps working and nothing says so.  The returned id is
    one the fake minted, and the fake counted the request.
    """

    module, service = fake
    monkeypatch.setenv(LARK_BASE_URL_ENV, service.base_url)

    gateway = LarkSdkGateway.from_credentials(
        module.FAKE_APP_ID, module.FAKE_APP_SECRET
    )
    native_id = gateway.send_chat("oc_fake_chat", "hello", idempotency_key="k1")

    assert native_id.startswith("om_fake")
    service.assert_routes_were_used(
        "POST /open-apis/im/v1/messages",
        "POST /open-apis/auth/v3/tenant_access_token/internal",
    )
    service.assert_no_unserved_routes()
    service.assert_every_emitted_url_is_loopback()


def test_the_arrival_check_can_fail(fake) -> None:
    """The control for question 2.

    Without this, ``assert_routes_were_used`` passing would be equally
    explained by "the traffic arrived" and by "the check does not work".
    """

    module, service = fake

    with pytest.raises(module.FakeLarkError, match="never reached"):
        service.assert_routes_were_used("POST /open-apis/im/v1/messages")


def test_an_untaught_route_is_refused_loudly_rather_than_answered(
    fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plausible answer for a route nobody tested looks exactly like coverage.

    The 501 is the negative half of the hit counter: counting proves what was
    exercised, refusing proves nothing else was quietly satisfied.
    """

    module, service = fake
    monkeypatch.setenv(LARK_BASE_URL_ENV, service.base_url)

    gateway = LarkSdkGateway.from_credentials(
        module.FAKE_APP_ID, module.FAKE_APP_SECRET
    )
    # A real gateway call whose route this fake was deliberately not taught.
    # LarkSdkError, not bare Exception: a broad `raises` here would have been
    # satisfied by a TypeError from calling the gateway wrong, which is what
    # happened on the first draft -- no request was ever made and the test
    # still looked like it had exercised the 501 path.  "Did not run" has to
    # stay distinguishable from "ran and failed" in the test as well.
    with pytest.raises(LarkSdkError):
        gateway.download_message_resource(
            "om_fake00000001", "file_x", resource_type="file"
        )

    with pytest.raises(module.FakeLarkError, match="does not serve"):
        service.assert_no_unserved_routes()


def test_the_websocket_endpoint_hands_back_a_loopback_address(
    fake, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Question 3, on the route where it bites first.

    ``_get_conn_url`` is an HTTP request whose *response* names the host the
    SDK then connects to.  A stand-in that answered with a public ``wss://``
    would satisfy questions 1 and 2 -- unreachable by us, all traffic counted
    -- and still send the system under test straight out of the sandbox.
    """

    module, service = fake
    monkeypatch.setenv(LARK_BASE_URL_ENV, service.base_url)

    previous = _current_event_loop()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        stream = LarkEventStream(
            app_id=module.FAKE_APP_ID,
            app_secret=module.FAKE_APP_SECRET,
            on_message=lambda _event: None,
        )
        stream.probe_rest_endpoint()
    finally:
        tasks = asyncio.all_tasks(loop)
        for task in tasks:
            task.cancel()
        if tasks:
            loop.run_until_complete(asyncio.gather(*tasks, return_exceptions=True))
        loop.close()
        asyncio.set_event_loop(previous)

    service.assert_routes_were_used("POST /callback/ws/endpoint")
    service.assert_every_emitted_url_is_loopback()


def test_the_outward_pointing_check_can_fail(fake) -> None:
    """The control for question 3.

    Question 3 is the one that stays false while 1 and 2 are true, so its
    guard is the one most worth proving reachable.
    """

    module, service = fake
    service.state.emitted_urls.append("wss://open.feishu.cn/ws")

    with pytest.raises(module.FakeLarkError, match="outside loopback"):
        service.assert_every_emitted_url_is_loopback()


def test_the_fake_refuses_to_borrow_a_real_apps_identity(fake) -> None:
    """lark_oapi caches tenant tokens process-globally, keyed by app id alone.

    The domain is not part of that key, so two clients sharing an id share a
    token even when one is pointed here and the other at the vendor.  Refusing
    the id makes the crossing impossible rather than unlikely -- the same
    reason the calibration workflow is given no Lark credentials at all.
    """

    module, _service = fake

    module.assert_isolated_app_id(module.FAKE_APP_ID)
    with pytest.raises(module.FakeLarkError, match="refuses app id"):
        module.assert_isolated_app_id("cli_aaf9ea19d4b8dbe4")


def test_the_environment_closes_the_proxy_hole(fake) -> None:
    """A shell proxy intercepting loopback makes question 2 fail silently.

    The run then passes without having talked to anything -- the fake sits
    idle and the assertions pass against whatever answered instead.
    """

    _module, service = fake

    environment = service.environment(
        {"HTTP_PROXY": "http://proxy.invalid:3128", "https_proxy": "http://p:1"}
    )

    assert environment[LARK_BASE_URL_ENV] == service.base_url
    assert "HTTP_PROXY" not in environment
    assert "https_proxy" not in environment
    assert environment["NO_PROXY"] == "127.0.0.1,localhost"


def _current_event_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_event_loop_policy().get_event_loop()
    except Exception:  # noqa: BLE001 - best-effort restore only
        return None
