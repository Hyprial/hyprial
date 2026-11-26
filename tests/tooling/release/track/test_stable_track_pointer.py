"""Track writer policy after Allen's 2026-10-26 ruling.

Stable follows formal releases; behavioural moves use real git fixtures in
test_release_tracks.py and test_track_workflows.py rather than an
always-successful git stub.  The workflow-side half of the policy (only the
formal publisher may move the stable tag, the internal mover and mirror
require a green dev push, gate relaxation stays instrumented only) was read
from the gate-python / github-publish workflow files and retired with them.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def test_track_updates_never_delete_refs_or_write_internal():
    text = (ROOT / "scripts/ci/release_tracks.py").read_text()
    assert 'self.move("stable", target, previous)' in text
    assert 'self.move("nightly", target, previous)' in text
    assert 'self.move("internal"' not in text
    assert '"--delete"' not in text and '"tag", "-d"' not in text
    retry = (ROOT / "scripts/ci/github_publish_policy/push.py").read_text()
    assert '--force-with-lease={ref}:{previous}' in retry
    assert 'f"{intended}:{ref}"' in retry
