#!/usr/bin/env python3
"""Prove the Lark stand-in works, in a process of its own.

Run directly, or through ``tests/daemon/adapters/lark/fixtures/test_fake_lark_events.py``, which executes it
as a subprocess and asserts on this output.

⚠️ A process of its own is not tidiness.  ``lark_oapi.ws.Client.start()``
never returns and the vendor exposes no stop, so a client started in-process
necessarily outlives the test that started it: its threads, its ping loop and
its reconnect schedule stay live for the rest of whatever else runs there.
The SDK also permits only one such client per process -- it drives a
module-level event loop with ``run_until_complete``, so a second ``start()``
dies with "This event loop is already running".  Both facts say the same
thing: this subject owns a process.  Process exit is the only teardown that
actually ends it.

That was learned the expensive way.  Run in-process, three consecutive full
suites went red, each blaming a *different* timing-sensitive test elsewhere --
and a probe looking for descriptor churn found none, so the mechanism was
never pinned down.  It is still not pinned down.  What is established is the
correlation and the fact that no in-process teardown can end the client; the
fix here is chosen because it removes the whole class, not because the
mechanism was identified.

What it checks:
  1. the real gateway's REST call lands on the stand-in
  2. a real websocket client connects and an event reaches our handler
  3. the client's ack distinguishes a clean handler from a raising one
  4. every URL the stand-in emitted names loopback
"""

from __future__ import annotations

import importlib.util
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    rest = _load("_fake_lark")
    events = _load("_fake_lark_ws")

    from hyprial.daemon.impl.adapters.lark.contracts.endpoint import LARK_BASE_URL_ENV
    from hyprial.daemon.impl.adapters.lark.outbound.stream import LarkEventStream
    from hyprial.daemon.impl.adapters.lark.outbound.sdk import LarkSdkGateway

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'} {label}{f': {detail}' if detail else ''}")
        if not ok:
            failures.append(label)

    with rest.FakeLark() as service, events.FakeLarkEvents() as ws:
        service.attach_events(ws.url)
        os.environ[LARK_BASE_URL_ENV] = service.base_url

        gateway = LarkSdkGateway.from_credentials(
            rest.FAKE_APP_ID, rest.FAKE_APP_SECRET
        )
        native_id = gateway.send_chat("oc_fake_chat", "hello", idempotency_key="k1")
        check(
            "rest.send-lands-on-the-stand-in",
            native_id.startswith("om_fake") and service.hits(
                "POST /open-apis/im/v1/messages"
            )
            > 0,
            f"native_id={native_id}",
        )

        received: list[object] = []
        explode = False

        def handler(event: object) -> None:
            received.append(event)
            if explode:
                raise RuntimeError("handler failure on purpose")

        stream = LarkEventStream(
            app_id=rest.FAKE_APP_ID,
            app_secret=rest.FAKE_APP_SECRET,
            on_message=handler,
        )
        threading.Thread(target=stream.start, daemon=True).start()
        ws.wait_for_connection()

        ws.deliver(events.message_receive_event(text="from the stand-in"))
        deadline = time.monotonic() + 15
        while not received and time.monotonic() < deadline:
            time.sleep(0.02)
        check("ws.event-reaches-our-handler", bool(received))

        # Delivered and handled are different facts: the reply frame carries
        # the handler's outcome.  Both outcomes are needed -- a 200 alone
        # would be equally explained by "the ack is always 200".
        clean = events.ack_status(ws.wait_for_ack())
        explode = True
        ws.deliver(events.message_receive_event())
        raised = events.ack_status(ws.wait_for_ack())
        check(
            "ws.ack-separates-handled-from-delivered",
            (clean, raised) == (200, 500),
            f"clean={clean} raising={raised}",
        )

        try:
            service.assert_every_emitted_url_is_loopback()
            service.assert_no_unserved_routes()
            check("standin.points-nowhere-outward", True)
        except AssertionError as error:
            check("standin.points-nowhere-outward", False, str(error))

    print(
        f"RESULT {'PASS' if not failures else 'FAIL'} lark-standin-selfcheck "
        f"pass={4 - len(failures)} fail={len(failures)}"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
