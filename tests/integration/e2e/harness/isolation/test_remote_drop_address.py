"""Remote slot recovery must exercise the registry's official recipient identity."""

from pathlib import Path
import runpy

import pytest


SCENARIO = runpy.run_path(
    str(Path(__file__).resolve().parents[5] / "contract/e2e/run-remote-drop-no-rebuild.py")
)
ADDRESS = "agent:remote-drop-bob:remote-drop-b:a.0123456789abcdef0123456789abcdef"


@pytest.mark.parametrize("uri", [ADDRESS, "agent:owner:node:remote-drop-driver", None])
def test_remote_delivery_uses_only_the_published_id_address(uri):
    sent_targets = []

    class Daemon:
        node_id = "remote-drop-b"

        def ps(self):
            return {"daemon": {"owner": "remote-drop-bob"}, "pendingMessages": [{"messageId": "m1"}]}

        def command(self, *args, **kwargs):
            if args[:2] == ("agent", "list"):
                return {"agents": [{"actor": SCENARIO["DRIVER"], "uri": uri}]}
            if args[:2] == ("message", "ack"):
                return {"acknowledged": True}
            sent_targets.append(args[args.index("--to") + 1])
            return {"messageId": "m1"}

    daemon = Daemon()
    if uri == ADDRESS:
        assert SCENARIO["_send_and_ack"](daemon, daemon, "probe") == "m1"
        assert sent_targets == [ADDRESS]
    else:
        with pytest.raises(AssertionError, match="id address"):
            SCENARIO["_send_and_ack"](daemon, daemon, "probe")
        assert sent_targets == []
