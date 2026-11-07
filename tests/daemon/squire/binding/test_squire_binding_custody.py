import json
import os
import subprocess
import sys
from hyprial.daemon.impl.squire.setup import SetupStateStore


_CHILD = r"""
import json, sys
from pathlib import Path
from hyprial.daemon.impl.squire.setup import SetupIdentity, SquireSetup
job = json.loads(sys.stdin.readline())
home = Path(job['home'])
setup = SquireSetup(hyprial_home=home, state_dir=home/'state', claude_available=lambda:False)
def gated(method):
    def invoke(*args):
        method(*args)
        print('authorized', flush=True)
        assert sys.stdin.readline().strip() == 'continue'
    return invoke
setup.setup_state.verify = gated(setup.setup_state.verify)
setup.setup_state.consume_verified = gated(setup.setup_state.consume_verified)
try:
    setup.run(SetupIdentity('owner','owner-key','owner','test-node','machine-key'),
        channel='channel:owner:test-node:app', owner_open_id=job['open_id'],
        binding_code=job['code'], squire_home=home/'squire', step='owner-open-id')
    print('bound', flush=True)
except ValueError:
    print('rejected', flush=True)
"""


def test_same_code_cannot_authorize_two_offline_setup_processes(tmp_path):
    home = tmp_path / "home"
    state = SetupStateStore(home / "state" / "squire-setup.json")
    pending = state.ensure_binding("owner-key", "channel:owner:test-node:app")
    children = []
    env = dict(os.environ, HYPRIAL_NODE_ID="test-node")
    try:
        for open_id in ("ou_first", "ou_second"):
            child = subprocess.Popen(
                [sys.executable, "-c", _CHILD],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            children.append(child)
            child.stdin.write(
                json.dumps(
                    {"home": str(home), "code": pending.code, "open_id": open_id}
                )
                + "\n"
            )
            child.stdin.flush()
        decisions = [child.stdout.readline().strip() for child in children]
        assert sorted(decisions) == ["authorized", "rejected"]
        winner = decisions.index("authorized")
        children[winner].stdin.write("continue\n")
        children[winner].stdin.flush()
        assert children[winner].stdout.readline().strip() == "bound"
        for child in children:
            assert child.wait(timeout=5) == 0, child.stderr.read()
        users = json.loads((home / "state" / "users.json").read_text())
        assert (
            users["users"][0]["ownerOpenId"]["openId"]
            == ("ou_first", "ou_second")[winner]
        )
        assert json.loads(state.path.read_text())["pendingBindings"] == {}
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            for stream in (child.stdin, child.stdout, child.stderr):
                stream.close()


def test_spent_authorization_is_not_revived_after_caller_failure(tmp_path):
    import pytest

    store = SetupStateStore(tmp_path / "setup.json")
    binding = store.ensure_binding("owner", "channel")
    store.consume_verified("owner", binding.channel, binding.code)
    # A caller failure after authorization must not permit a different identity
    # to reuse the old code, even if the first profile write was uncertain.
    with pytest.raises(ValueError, match="invalid or expired"):
        SetupStateStore(store.path).consume_verified(
            "owner", binding.channel, binding.code
        )
    replacement = store.ensure_binding("owner", "channel")
    assert replacement.code != binding.code
