from __future__ import annotations

import json
import threading
import pytest

from hyprial.kernel.impl.log import Logger


def test_async_logger_requires_a_host(tmp_path):
    with pytest.raises(ValueError, match="host"):
        Logger.daemon(tmp_path, name="unbound", asynchronous=True)
    with pytest.raises(ValueError, match="host"):
        Logger.adapter(tmp_path, name="unbound", asynchronous=True)


@pytest.mark.parametrize("factory", [Logger.daemon, Logger.adapter])
def test_disk_stall_does_not_block_log_admission_and_overflow_is_visible(tmp_path, factory, actor_host):
    entered, release = threading.Event(), threading.Event()

    def rotate(path):
        entered.set()
        assert release.wait(3)

    logger = factory(
        tmp_path, name="test", rotation_hook=rotate, asynchronous=True, capacity=1,
        actor_host=actor_host
    )
    try:
        logger.info("first")
        assert entered.wait(1)
        logger.bind(component="inbox").info("second")
        logger.error("overflow")
        status = logger.writer_status()
        assert status.accepted == 2
        assert status.rejected == 1
        assert not logger.close(timeout=0.01)
        logger.info("closed")
        assert logger.writer_status().rejected == 2
    finally:
        release.set()
        assert logger.close(timeout=3)
    records = [json.loads(row) for row in logger.path.read_text().splitlines()]
    assert [row["event"] for row in records] == ["first", "second"]
    assert records[1]["component"] == "inbox"


def test_append_failure_is_counted_without_recursive_logging(tmp_path, actor_host):
    def rotate(path):
        raise OSError("disk unavailable")

    logger = Logger.daemon(
        tmp_path, name="test", rotation_hook=rotate, asynchronous=True, actor_host=actor_host
    )
    logger.info("fails", token="synthetic-secret")
    assert logger.close(timeout=3)
    status = logger.writer_status()
    assert status.accepted == status.failed == 1
    assert status.written == 0
    assert status.last_error == "OSError"


def test_async_log_freezes_redacted_payload_before_admission(tmp_path, actor_host):
    entered, release = threading.Event(), threading.Event()

    def rotate(path):
        entered.set()
        assert release.wait(3)

    logger = Logger.daemon(
        tmp_path, name="test", rotation_hook=rotate, asynchronous=True, actor_host=actor_host
    )
    fields = {"value": "before", "secret": "synthetic-secret"}
    try:
        logger.info("record", nested=fields)
        assert entered.wait(1)
        fields["value"] = "after"
    finally:
        release.set()
        assert logger.close(timeout=3)
    record = json.loads(logger.path.read_text())
    assert record["nested"] == {"value": "before", "secret": "[REDACTED]"}
