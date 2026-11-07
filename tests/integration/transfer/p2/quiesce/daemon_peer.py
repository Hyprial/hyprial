"""Test-only quiesce driver and pipe crash barriers inside a real daemon.

No production IPC is added. This disposable entry injects a trusted internal
request; it does not test public admission or authenticated override.
"""

from dataclasses import replace
import os
import sys
import threading

from hyprial.daemon.impl.application import DaemonApplication
from hyprial.daemon.impl.composition import HarnessPortClient
from hyprial.daemon.impl.harnesses.runtime.transfer_ports import HoldStartsForTransfer
from hyprial.daemon.impl.transfer.p2.actor import Command, Operation
from hyprial.daemon.impl.transfer.p2.journal import PhaseJournal
from hyprial.daemon.impl.transfer.p2.model import Binding, Role, AdmissionConflict
from hyprial.daemon.impl.transfer.p2.source.effect import QuiesceRequest
from hyprial.shell import main


def run():
    phase, output = sys.argv[1], int(sys.argv[2])
    original_handle = DaemonApplication.handle
    original_begin, original_finish = PhaseJournal.begin, PhaseJournal.finish
    original_call = HarnessPortClient.call

    def checkpoint(name):
        if name == phase:
            os.write(output, (name + "\n").encode())
            # Parent waits on the pipe, then SIGKILLs this exact daemon PID.
            # No sleep-based race or production timer is involved.
            threading.Event().wait()

    def begin(self, operation_id, **kwargs):
        if operation_id == "crash-move":
            checkpoint("before-intent")
        result = original_begin(self, operation_id, **kwargs)
        if operation_id == "crash-move":
            checkpoint("after-intent")
        return result

    def finish(self, operation_id, **kwargs):
        # The lifecycle stop receipt is already durable here; the journal row
        # is still quiesce-pending. This is the window a restarted daemon must
        # certify against the owner receipt before closing the intent.
        if operation_id == "crash-move":
            checkpoint("after-stop")
        result = original_finish(self, operation_id, **kwargs)
        if operation_id == "crash-move":
            checkpoint("after-quiesce")
        return result

    def call(self, command, expected, **kwargs):
        if isinstance(command, HoldStartsForTransfer) and command.operation_id == "crash-move":
            checkpoint("before-hold")
        return original_call(self, command, expected, **kwargs)

    def handle(self, method, params, *args, **kwargs):
        if method != "test.p2.quiesce":
            return original_handle(self, method, params, *args, **kwargs)
        agent = self.agents.require(params.get("actor", "transfer-source"))
        spec = next(spec for spec in self.desired_state.load().harnesses
                    if spec.name == agent.actor)
        binding = Binding(Role.SOURCE, self.owner, self.node_id, agent.actor,
                          agent.entity_token, params.get("targetNode", "isolated-target"),
                          params.get("targetActor", "target"), "a" * 64)
        operation_id = params.get("operationId", "crash-move")
        try:
            self._transfer_p2.submit(Command(Operation.PREPARE, operation_id, binding)).result(5)
        except AdmissionConflict as error:
            return {"rejected": error.reason.value}
        if params.get("prepareOnly"):
            # Let the test observe admission directly, without queuing a
            # second long effect behind the deliberately paused first one.
            return {"prepared": True}
        request = QuiesceRequest(operation_id, agent.actor, replace(
            self._lifecycle_spec(spec), expected_entity_token=agent.entity_token))
        result = self._quiesce_p2_source(request).result(70)
        return {"state": result.state, "reason": result.reason}

    PhaseJournal.begin, PhaseJournal.finish = begin, finish
    HarnessPortClient.call = call
    DaemonApplication.handle = handle
    sys.argv = ["hyprial", "daemon", "run"]
    main()


if __name__ == "__main__":
    run()
