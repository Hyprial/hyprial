"""Only pause real EffectLane retry callbacks/worker; never fabricate events."""

import pathlib
import threading
import time
import json
import sys
import traceback
from hyprial.identity.impl.provider_auth.actor._authority import ProviderAuthAuthority
from hyprial.kernel.impl.actor_runtime.effects import EffectCompleted

root = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(root))
from tests.identity.providers.authentication.episodes import test_native_recovery as m  # noqa: E402
mode = sys.argv[1]
output = pathlib.Path(sys.argv[2])
records = []
owners = []
lock = threading.Lock()
sends = {}
receives = {}
selected = None
helper_twice = threading.Event()
decision_twice = threading.Event()
stale_acked = threading.Event()
other_release = threading.Event()


def record(kind, **kw):
    with lock:
        records.append(
            dict(
                t=time.monotonic(),
                thread=threading.current_thread().name,
                kind=kind,
                **kw,
            )
        )


def snapshot(a):
    with a._core._lock:
        episodes = {
            k: dict(
                round=v.round,
                inflight=v.inflight,
                pending=v.closing_pending,
                sent=v.closing_sent,
            )
            for k, v in a._core._episodes.items()
        }
    return dict(
        status=a.status(),
        episodes=episodes,
        state_worker_alive=all(t.is_alive() for t in a._decisions._threads),
    )


original_init = ProviderAuthAuthority.__init__
original_receive = ProviderAuthAuthority._receive
original_run = ProviderAuthAuthority._run_helper
original_apply = ProviderAuthAuthority._apply_decision


def init(self, **kw):
    original_init(self, **kw)
    owners.append(self)
    tell = self._runtime.tell

    def observed_tell(handle, event):
        global selected
        if isinstance(event, EffectCompleted):
            if event.operation_id.startswith("helper:") and event.result:
                request, _ = event.result
                if request.provider == "kimi-coding" and request.round_no == 3:
                    selected = event.operation_id
            if selected and event.operation_id in (selected, "finished:" + selected):
                key = event.operation_id
                with lock:
                    sends[key] = sends.get(key, 0) + 1
                    n = sends[key]
                # Establish H2 before D2 while owner is held at D1 before ACK.
                if mode != "baseline" and key == "finished:" + selected and n == 2:
                    assert helper_twice.wait(3)
                result = tell(handle, event)
                record("native-completion-enqueued", operation=key, copy=n)
                if key == selected and n == 2:
                    helper_twice.set()
                if key == "finished:" + selected and n == 2:
                    decision_twice.set()
                return result
        return tell(handle, event)

    self._runtime.tell = observed_tell
    take = self._decisions._take_request
    taken = {}

    def observed_take():
        item = take()
        if item is None:
            return None
        if selected and item.operation_id == "finished:" + selected:
            taken[item.operation_id] = taken.get(item.operation_id, 0) + 1
            if mode != "baseline" and taken[item.operation_id] == 2:
                record("reused-request-dequeued", operation=item.operation_id)
                assert stale_acked.wait(3)
        return item

    self._decisions._take_request = observed_take


def run(self, request):
    result = original_run(self, request)
    if request.round_no == 3:
        record("third-helper-return", provider=request.provider, outcome=str(result[1]))
        if request.provider == "openai-codex" and mode != "baseline":
            assert other_release.wait(15)
    return result


def apply(self, command):
    try:
        return original_apply(self, command)
    finally:
        if hasattr(command, "helper") and command.helper.round_no == 3:
            record(
                "third-decision-receipt",
                command=type(command).__name__,
                provider=command.helper.provider,
                **snapshot(self),
            )


def receive(self, event):
    if not isinstance(event, EffectCompleted):
        return original_receive(self, event)
    key = event.operation_id
    if selected and key == "finished:" + selected:
        receives[key] = receives.get(key, 0) + 1
        n = receives[key]
        if mode != "baseline" and n == 1:
            assert helper_twice.wait(3)
            assert decision_twice.wait(3)
        original_receive(self, event)
        record("decision-ack-applied", copy=n, **snapshot(self))
        if n == 2:
            record(
                "replayed-decision-ack",
                reused_id_present=key in self._commands,
                helper_custody=self._helpers.snapshot().outstanding,
            )
            stale_acked.set()
            other_release.set()
        return
    return original_receive(self, event)


wait = m._wait


def observed_wait(predicate, *, timeout=3):

    # closing_pending lives in the generator code object, hence inspect recursively.
    def mentions(code):
        return "closing_pending" in code.co_names or any(
            mentions(x) for x in code.co_consts if hasattr(x, "co_names")
        )

    closing = mentions(predicate.__code__)
    if closing:
        record("closing-wait-entry", remaining=timeout, **snapshot(owners[0]))
    try:
        return wait(predicate, timeout=timeout)
    except BaseException:
        if closing:
            record(
                "closing-wait-failure-before-finally",
                remaining_on_entry=timeout,
                **snapshot(owners[0]),
            )
        raise


def error(args):
    record("thread-error", error=repr(args.exc_value))
    other_release.set()


threading.excepthook = error
ProviderAuthAuthority.__init__ = init
ProviderAuthAuthority._receive = receive
ProviderAuthAuthority._run_helper = run
ProviderAuthAuthority._apply_decision = apply
m._wait = observed_wait
outcome = "PASS"
try:
    m.test_h2_n4_blocked_native_rounds_retain_all_terminal_successors(
        output.parent / "case"
    )
except BaseException:
    traceback.print_exc()
    outcome = "FAIL"
finally:
    other_release.set()
    stale_acked.set()
    output.write_text(json.dumps(records, indent=2))
    if outcome == "PASS":
        assert owners[0].status()["custody"] == 0
        assert all(
            not t.is_alive()
            for lane in (
                owners[0]._helpers,
                owners[0]._decisions,
                owners[0]._notifications,
            )
            for t in lane._threads
        )
    print(mode, outcome, flush=True)
    sys.exit(0 if outcome == "PASS" else 1)
