"""The Lark stand-in's inbound half, exercised in a process of its own.

The work happens in ``contract/e2e/run-lark-standin-selfcheck.py``;
this runs it and reads its verdict.  Two reasons it cannot live in this
process, both properties of the vendor SDK rather than preferences:

  - ``lark_oapi.ws.Client.start()`` never returns and the vendor exposes no
    stop, so a client started here outlives the test that started it -- its
    threads, its ping loop and its reconnect schedule stay live for the
    remaining two-and-a-half thousand tests.
  - Only one such client can exist per process: it drives a module-level
    event loop with ``run_until_complete``, so a second ``start()`` dies with
    "This event loop is already running".

Both say the same thing -- this subject owns a process -- and process exit is
the only teardown that actually ends it.

⚠️ Learned the expensive way, and the mechanism is still unknown.  Run
in-process, three consecutive full suites went red, each blaming a *different*
timing-sensitive test elsewhere, while the websocket test itself passed.  A
probe sampling descriptors for 45s afterwards -- longer than the SDK's 30s
reconnect nonce -- found no churn either way, so "the leftover client did it"
was never demonstrated.  What is established: the correlation, and that no
in-process teardown can end the client.  Subprocess isolation is chosen
because it removes the class, not because the cause was identified.  If those
reds continue after this, that is real information and this note is wrong.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

from hyprial.daemon.impl.adapters.lark.contracts.endpoint import LARK_BASE_URL_ENV
from hyprial.daemon.impl.adapters.lark.outbound.stream import LarkEventStream

ROOT = Path(__file__).resolve().parents[5]
SCENARIOS = ROOT / "contract" / "e2e"
SELFCHECK = SCENARIOS / "run-lark-standin-selfcheck.py"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name}_probe", SCENARIOS / f"{name}.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_inbound_loop_closes_against_the_stand_in() -> None:
    """A real SDK client, a stand-in websocket, and an event that arrives.

    Before this existed nothing in this repository had ever opened a Lark
    websocket, so nothing had shown that an inbound event reaches our handler
    at all -- only that the pieces compiled.

    The subprocess is not an implementation detail to look past: its exit
    status is the assertion, and its four PASS lines name the four properties.
    A timeout is fatal rather than skipped, because "the client never
    connected" and "the suite is slow today" must not share an outcome.
    """

    completed = subprocess.run(
        [sys.executable, str(SELFCHECK)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(ROOT),
    )

    assert completed.returncode == 0, (
        "the Lark stand-in self-check failed:\n"
        f"{completed.stdout[-4000:]}\n{completed.stderr[-2000:]}"
    )
    for expected in (
        "PASS rest.send-lands-on-the-stand-in",
        "PASS ws.event-reaches-our-handler",
        "PASS ws.ack-separates-handled-from-delivered",
        "PASS standin.points-nowhere-outward",
    ):
        assert expected in completed.stdout, (
            f"{expected!r} is missing, so that property was not exercised even "
            f"though the run reported success:\n{completed.stdout[-4000:]}"
        )


def test_the_selfcheck_can_fail(tmp_path: Path) -> None:
    """The control: a green exit status has to be capable of being red.

    Without this, exit 0 would be equally explained by "the four properties
    held" and by "the script cannot report a failure".
    """

    source = SELFCHECK.read_text(encoding="utf-8")
    # The copy lives outside the scenarios directory, so it has to be told
    # where its siblings are -- and it is copied rather than run with a
    # "fail on purpose" switch, because a production script must not carry a
    # flag whose only purpose is to make it report a failure it did not have.
    mutated = source.replace(
        "HERE = Path(__file__).resolve().parent",
        f"HERE = Path({str(SCENARIOS)!r})",
    ).replace(
        'check("standin.points-nowhere-outward", True)',
        'check("standin.points-nowhere-outward", False, "forced")',
    )
    assert mutated != source, "the self-check no longer has the line this mutates"

    broken = tmp_path / "broken.py"
    broken.write_text(mutated, encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(broken)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(ROOT),
    )

    assert completed.returncode == 1
    assert "RESULT FAIL lark-standin-selfcheck" in completed.stdout


def test_without_an_attached_event_server_nothing_pretends_to_work(
    lark_ws_default_loop, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Endpoint discovery still answers, and the address accepts no socket.

    Readiness and health rest on the discovery request alone, so it must keep
    working with no event server attached.  What must NOT happen is a scenario
    that silently needed inbound events looking healthy.

    Safe in-process because no client is started: ``probe_rest_endpoint``
    re-runs the discovery POST and leaves no threads behind.
    """

    rest = _load("_fake_lark")
    with rest.FakeLark() as service:
        monkeypatch.setenv(LARK_BASE_URL_ENV, service.base_url)

        stream = LarkEventStream(
            app_id=rest.FAKE_APP_ID,
            app_secret=rest.FAKE_APP_SECRET,
            on_message=lambda _event: None,
        )
        stream.probe_rest_endpoint()

        service.assert_routes_were_used("POST /callback/ws/endpoint")
        service.assert_every_emitted_url_is_loopback()
