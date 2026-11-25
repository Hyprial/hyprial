"""Durable reopen and source adapter regressions without personal orgs.

Decision (phase D, package T3, 2026-11-25): the durable work space is held by
``OrgFsRuntime`` (built directly, independent of ``HYPRIAL_ORGFS_IMPL``),
not by a journal store handed to the legacy facade.  The companion
``test_revision.py`` was deleted: the contract suite's ``content/test_revision.py``
pins the same durable content revision on every implementation.
"""
import json
from dataclasses import replace

from hyprial.daemon.impl.orgfs.composition.state import OrgFsRuntime
from tests.daemon.org_membership.migration.test_migration import OWNER, fixture, implementation, run_step


def test_foreign_context_permission_does_not_block_migration(tmp_path):
    svc, source, fs = fixture(tmp_path, with_work_context=False)
    work = fs.create_space("group-remote")
    fs.update_space_meta(work.space_id, {"org": "group-remote", "kind": "work"})
    fs.mkdir(work.space_id, "directory")
    fs.write_text(work.space_id, "directory/org.json", '{"state":"active"}')
    fs._spaces[work.space_id].info = replace(work, owner="user:foreign@example")
    source["contexts"] = [{"source": work.space_id, "org": "group-remote", "digest": "accepted",
                           "residents": [{"name": "worker", "node": "node"}]}]
    result = svc.run()
    assert not result["failed"] and not result["pending"]


def test_report_is_private_and_source_exception_is_not_empty_done(tmp_path):
    svc, _, _ = fixture(tmp_path, with_work_context=False)

    def broken():
        raise ValueError("secret-value-must-not-be-reported")

    svc.inventory = broken
    report = svc.run()
    assert report["failed"] and not report["verified"]
    assert "secret-value" not in json.dumps(report)
    path = __import__("pathlib").Path(report["reportPath"])
    assert path.stat().st_mode & 0o777 == 0o600


def test_durable_work_space_migration_survives_reopen(tmp_path):
    implementation()
    sid = "00000000-0000-0000-0000-000000000021"
    runtime = OrgFsRuntime(tmp_path / "disk", node_id="node", author="user:" + OWNER)
    fs = runtime.facade
    try:
        work = fs.create_space("group-team", space_id=sid)
        fs.update_space_meta(work.space_id, {"org": "group-team", "kind": "work"})
        fs.mkdir(work.space_id, "directory")
        fs.write_text(work.space_id, "directory/org.json", '{"state":"active"}')
        svc, source, _ = fixture(tmp_path / "home", fs=fs, with_work_context=False)
        source["contexts"] = [{"source": work.space_id, "org": "group-team", "digest": "accepted",
                               "residents": [{"name": "worker", "node": "node"}]}]
        report = svc.run()
        assert report["verified"] is True
    finally:
        runtime.close()


def _journal_from_before_s3a(home, *, user_phase):
    from hyprial.daemon.impl.org.membership.migration.journal import digest

    path = home / "state" / "org-membership-v1" / "journal.json"
    data = json.loads(path.read_text())
    data["inventory"]["users"] = [
        {**row, "receivers": {"preferred": {"machine": "node"}, "squire": None, "userProxy": None}}
        for row in data["inventory"]["users"]]
    data["sourceDigest"] = digest(data["inventory"])
    data["entities"]["user:" + OWNER]["phase"] = user_phase
    data["entities"]["user:" + OWNER]["targets"] = {
        "personal-owner": {"record": {"owner": OWNER}, "created": True, "version": "v1"}}
    path.write_text(json.dumps(data))
    return path


def test_completed_journal_from_before_s3a_stays_done_on_rerun(tmp_path):
    svc, _, _ = fixture(tmp_path)
    assert run_step(tmp_path, svc)["steps"][0]["state"] == "done"
    path = _journal_from_before_s3a(tmp_path, user_phase="REPORTED")
    frozen = json.loads(path.read_text())
    for _ in range(2):
        rerun = run_step(tmp_path, svc)["steps"][0]
        assert rerun["state"] == "done", rerun
    assert json.loads(path.read_text()) == frozen


def test_unfinished_journal_from_before_s3a_drifts_until_moved_aside(tmp_path):
    svc, _, _ = fixture(tmp_path)
    assert run_step(tmp_path, svc)["steps"][0]["state"] == "done"
    path = _journal_from_before_s3a(tmp_path, user_phase="RECEIVERS_WRITTEN")
    drifted = run_step(tmp_path, svc)["steps"][0]
    assert drifted["state"] == "failed"
    assert drifted["detail"] == "source-drift"
    path.rename(path.with_name("journal.json.before-s3a"))
    assert run_step(tmp_path, svc)["steps"][0]["state"] == "done"
