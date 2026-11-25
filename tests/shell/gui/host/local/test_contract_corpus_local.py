"""The contract/gui-session corpus holds over the in-process daemon transport.

Decision: host-serves-daemon-in-process-20261119 (2822, B3).  The corpus tests in
``tests/shell/gui/host/test_contract_corpus.py`` run against ``FakeUpstream``;
this one scenario runs the same validators over the ``LocalDaemon`` ASGI
transport: open a session, relay one method, change the principal, get
``GUI_SESSION_CHANGED`` for the in-flight call and refresh.  No value of the
corpus is copied here.
"""

from __future__ import annotations

import asyncio
from typing import Any

from tests.shell.gui.host.local.support import FAILURE_BOUND_SECONDS, open_local, world
from tests.shell.gui.host.local.test_local_daemon_driver import HoldsTheAnswer
from tests.shell.gui.host.support import run_gateway
from tests.shell.gui.host.test_contract_corpus import CORPUS, _corpus_pairs, _validator
from tests.shell.request_http.rebind.support import OWNER, write_home


BOOTSTRAP_STATUS = {
    "ok": True,
    "phase": "bootstrap",
    "authenticated": False,
    "owner": None,
    "deviceReady": True,
    "initialized": False,
    "operations": [],
    "nextActions": ["login"],
}


def test_the_session_flow_over_the_local_daemon_transport_validates_against_the_corpus(
    tmp_path, monkeypatch
) -> None:
    daemon = world(tmp_path, monkeypatch, phase="bootstrap")
    daemon.handlers["onboarding.status"] = lambda params: BOOTSTRAP_STATUS
    local = open_local(tmp_path)
    first = local.config
    held = HoldsTheAnswer(local.transport)
    try:

        async def scenario(gateway: Any) -> None:
            opened = await gateway.open(headers={"x-hyprial-session-contract": "1"})
            assert list(_validator("sessionResponse").iter_errors(opened.session)) == []
            assert opened.session["sessionContract"] == {"version": 1, "min": 1, "max": 1}
            headers = opened.headers()
            relayed = await gateway.call(
                "/daemon/request/onboarding.status", method="POST", body={}, headers=headers
            )
            assert relayed.status == 200, relayed.raw
            held.entered, held.release = asyncio.Event(), asyncio.Event()
            pending = asyncio.create_task(
                gateway.call("/daemon/request/onboarding.status", method="POST", body={}, headers=headers)
            )
            await asyncio.wait_for(held.entered.wait(), FAILURE_BOUND_SECONDS)
            write_home(tmp_path, owner=OWNER)
            daemon.become_serving()
            assert await asyncio.to_thread(local.wait_for_change, first, FAILURE_BOUND_SECONDS)
            held.release.set()
            changed = await pending
            assert (changed.status, changed.json["code"]) == (409, "GUI_SESSION_CHANGED")
            assert (changed.status, changed.json["code"]) in _corpus_pairs()
            assert list(_validator("hostError").iter_errors(changed.json)) == []
            assert changed.json["dispatch"] in CORPUS["envelope"]["dispatchValues"]
            refreshed = await gateway.call("/daemon/session", method="POST", body={}, headers=headers)
            assert refreshed.status == 200, refreshed.raw
            assert list(_validator("sessionResponse").iter_errors(refreshed.json)) == []
            assert refreshed.json["principal"]["kind"] == "serving"

        run_gateway(scenario, upstream=held, config=first, authority=local.authority)
        assert local.failure is None
    finally:
        local.close()
