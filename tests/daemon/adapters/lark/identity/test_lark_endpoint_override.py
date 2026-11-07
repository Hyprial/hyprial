"""Every Lark exit this process has must point at the same host.

Three constructors independently decide which host the adapter talks to: the
SDK REST client, the SDK websocket client (which owns both the endpoint-
discovery request and the long connection), and the v6 scope client.  A
redirect that reached two of the three would be worse than none: the run would
report itself as pointed somewhere local while one path still reached a
production App, and nothing in a green result says which paths were used.

``LarkScopeClient`` already accepted an ``origin`` before this seam existed and
no production caller ever passed one, so only its default was ever in effect.
"Injectable" and "injected" are different facts; the tests below assert the
second.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hyprial.shell.impl.cli.commands.adapter import admin as adapter_admin
from hyprial.shell.impl.cli.commands.adapter import authorize as adapter_authorize

from hyprial.daemon.impl.adapters.lark.contracts.endpoint import (
    FEISHU_BASE_URL,
    LARK_BASE_URL_ENV,
    is_production_lark,
    lark_base_url,
)
from hyprial.daemon.impl.adapters.lark.credentials.scopes import LarkScopeClient
from hyprial.daemon.impl.adapters.lark.outbound.sdk import LarkSdkGateway
from hyprial.daemon.impl.adapters.lark.outbound.stream import LarkEventStream

SOURCE_DIR = Path(__file__).resolve().parents[5] / 'src/hyprial'
LOCAL = "http://127.0.0.1:8931"

# Occurrences of the production host that are NOT network exits, each with the
# reason it is allowed to stay.  A new occurrence anywhere else fails the
# exhaustiveness test below, which is the point: a fourth exit added later must
# not be able to appear silently.
#
# Keyed by path relative to src/hyprial.  The scan used to cover only the adapter
# package, under a name that promised the whole thing -- so an OpenAPI exit
# opened in cli.py or daemon/ would have passed it while the name went on
# claiming otherwise.  That is worse than having no such test, because "there
# is already an exhaustiveness test" is what stops the next person counting.
# I nearly relied on it myself.
NON_EXIT_OCCURRENCES = {
    # Three, and the count is deliberate rather than approximate -- writing 2
    # here from memory is what this test caught on its first run:
    #   1. The shared Feishu console origin used by the developer-console and
    #      preselected scope-apply links.  It must keep naming the real console
    #      even when this process's traffic is redirected; a person cannot
    #      administer an App on a stand-in.
    #   2-3. _OFFICIAL_LINK_HOSTS: the validation allowlist for authorization
    #      links returned by the platform, which names both vendor domains.
    #      Not an exit -- but note it would reject a stand-in origin's
    #      authorization URL, which is a real interaction to handle when a
    #      scenario first reaches it.
    "daemon/impl/adapters/lark/credentials/scopes.py": 3,
    # Two allowed browser verification hosts in _HOSTS. The authorization
    # service validates an SDK-returned URL but never requests either host;
    # its worker's OpenAPI inventory still uses configured_scope_client and
    # lark_base_url(), and device registration uses ACCOUNTS_DOMAIN_ENV.
    "daemon/impl/adapters/lark/credentials/authorization/service.py": 2,
    # One documentation reference in a comment: the page whose error table
    # names the codes in _UNREADABLE_CHAT_CODES.  Not an exit -- this module
    # never requests that host -- and the host is spelled out on purpose,
    # because "the platform documents these codes" is a memory while a
    # citation the reviewer can open is a reading, and those three codes are
    # exactly the part of that change a reviewer has to check for themselves.
    "daemon/impl/adapters/lark/inbound/reconcile.py": 1,
    # This module states the default it hands out.
    "daemon/impl/adapters/lark/contracts/endpoint.py": 2,
    # Three documentation links in comments, naming the Lark pages that
    # specify the error codes and upload endpoints this module handles.
    "daemon/impl/network/route_delivery.py": 3,
    # Two documentation links in a comment, naming the official "Get chat
    # history" error-code table (feishu.cn and its larksuite.com mirror)
    # that LARK_HISTORY_CODES_PERMANENT_CHAT_GONE is sourced from.
    "daemon/impl/adapters/lark/outbound/sdk_base.py": 2,
}

# Lark's other service.  The accounts / device-authorization endpoint is a
# separate host with its own long-standing knob, exercised by E2E-011 against
# a loopback stand-in.  Recorded here because "I checked and it is unrelated"
# and "I never looked" are indistinguishable afterwards.
ACCOUNTS_DOMAIN_ENV = "HYPRIAL_LARK_ACCOUNTS_DOMAIN"


def test_the_default_is_the_vendor_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unconfigured process must behave exactly as it did before the seam."""

    monkeypatch.delenv(LARK_BASE_URL_ENV, raising=False)

    assert lark_base_url() == FEISHU_BASE_URL
    assert is_production_lark(lark_base_url())


def test_a_trailing_slash_is_stripped() -> None:
    """Both consumers concatenate a path onto this value directly.

    ``lark_oapi``'s ws client builds ``domain + "/callback/ws/endpoint"``, and
    ``//callback`` is a different route on some servers.
    """

    assert lark_base_url({LARK_BASE_URL_ENV: f"{LOCAL}/"}) == LOCAL


def test_a_typoed_override_does_not_read_as_safe() -> None:
    """The question is "is this the real one", not "does it look like my fake".

    Asked the other way round, an override that resolved to neither -- a typo,
    an empty string that fell back, a stale variable -- would pass a check
    written as "is not production".
    """

    assert is_production_lark(FEISHU_BASE_URL)
    assert is_production_lark("https://open.larksuite.com/")
    assert not is_production_lark(LOCAL)
    assert not is_production_lark("http://opeen.feishu.cn")


def test_the_rest_client_follows_the_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(LARK_BASE_URL_ENV, LOCAL)

    gateway = LarkSdkGateway.from_credentials("cli_fake", "secret")

    assert gateway._client._config.domain == LOCAL


def test_the_websocket_client_follows_the_override(
    lark_ws_default_loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ws client owns two things, and both move with ``domain``.

    Its endpoint-discovery POST is the request ``probe_rest_endpoint`` re-runs
    for health, and the URL that POST returns is what the socket connects to.
    Leaving it on the vendor host would keep the inbound half on production
    while the outbound half had moved -- the exact split this seam exists to
    make impossible.
    """

    monkeypatch.setenv(LARK_BASE_URL_ENV, LOCAL)

    # ``lark_ws_default_loop`` is why this does not leak descriptors: the SDK
    # reads the thread's default loop when it builds its expiring cache, and
    # an earlier test that ran asyncio.run() leaves that loop closed, so the
    # constructor builds a fresh one nobody closes.  See the fixture.
    stream = LarkEventStream(
        app_id="cli_fake",
        app_secret="secret",
        on_message=lambda _event: None,
    )

    assert stream._client._domain == LOCAL


def test_the_scope_client_follows_the_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Its production callers pass no origin, so the default is the only path."""

    monkeypatch.setenv(LARK_BASE_URL_ENV, LOCAL)

    assert LarkScopeClient("cli_fake", "secret")._origin == LOCAL
    # An explicit origin still wins; the offline scope tests rely on it.
    assert (
        LarkScopeClient("cli_fake", "secret", origin="https://elsewhere.test")._origin
        == "https://elsewhere.test"
    )


def test_no_fourth_exit_can_appear_unnoticed() -> None:
    """Enumerate the production host in source rather than trusting a memory.

    Wiring three of four exits and wiring four of four look identical from
    outside, and the unwired one would read as protected.  This counts them
    instead: any new occurrence of the vendor host in the adapter has to be
    justified here, which forces whoever adds one to decide whether it is a
    network exit.
    """

    found: dict[str, int] = {}
    for path in sorted(SOURCE_DIR.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        count = text.count("open.feishu.cn") + text.count("open.larksuite.com")
        if count:
            found[str(path.relative_to(SOURCE_DIR))] = count

    assert found == NON_EXIT_OCCURRENCES, (
        "the production Lark host appears somewhere new in src/hyprial; if it is a "
        "network exit it must go through lark_base_url(), and if it is not, "
        "record it in NON_EXIT_OCCURRENCES with the reason"
    )


def test_the_accounts_endpoint_is_a_different_service_with_its_own_knob() -> None:
    """Two Lark services, two knobs -- and only one of them is this seam's.

    Redirecting OpenAPI does not redirect the accounts/device-authorization
    flow, and a reader who assumes one knob moves everything would build a
    stand-in that half the traffic ignores.  Pinned rather than merely
    commented so the split survives someone deleting the comment.
    """

    cli = "\n".join(
        Path(module.__file__).read_text(encoding="utf-8")
        for module in (adapter_admin, adapter_authorize)
    )

    assert cli.count(ACCOUNTS_DOMAIN_ENV) >= 2
    assert LARK_BASE_URL_ENV != ACCOUNTS_DOMAIN_ENV
