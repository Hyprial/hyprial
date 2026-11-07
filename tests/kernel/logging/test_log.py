from __future__ import annotations

import json
import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from hyprial.kernel.impl.log import Logger


def _process_writer(state_dir: str, process_id: int, count: int) -> None:
    logger = Logger.worker(
        Path(state_dir), runtime="pi", name="concurrent-worker"
    )
    for index in range(count):
        logger.info("concurrent.record", process=process_id, index=index)


def _records(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_schema_routes_permissions_and_scopes(tmp_path: Path) -> None:
    daemon = Logger.daemon(tmp_path, name="node-1")
    adapter = Logger.adapter(tmp_path, name="lark-one")
    worker = Logger.worker(tmp_path, runtime="pi", name="agent/unsafe")

    daemon.bind(component="zenoh").warn("zenoh.unset", detail="isolated")
    adapter.info("adapter.ready", pid=101)
    worker.error("worker.exited", pid=202, returnCode=23)

    assert daemon.path == tmp_path / "logs" / "daemon.jsonl"
    assert adapter.path == tmp_path / "logs" / "lark-gateway.jsonl"
    assert worker.path == tmp_path / "logs" / "workers" / "pi-agent_unsafe.jsonl"
    daemon_record = _records(daemon.path)[0]
    assert set(daemon_record) == {
        "ts",
        "level",
        "component",
        "name",
        "event",
        "detail",
    }
    assert daemon_record["ts"].endswith("Z")  # type: ignore[union-attr]
    assert daemon_record["level"] == "warn"
    assert daemon_record["component"] == "zenoh"
    assert daemon_record["name"] == "node-1"
    assert daemon_record["event"] == "zenoh.unset"
    assert _records(worker.path)[0]["runtime"] == "pi"
    for path in (daemon.path, adapter.path, worker.path):
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "logs").stat().st_mode & 0o777 == 0o700


def test_redact_preserves_token_diagnostic_and_strips_bare_keys() -> None:
    from hyprial.kernel.impl.log import redact

    # "token refresh" is diagnostic wording, not a credential value: the
    # verbatim cause must survive into alerts (追加 2).
    assert redact(
        "Kimi Code token refresh unauthorized (status 400)"
    ) == "Kimi Code token refresh unauthorized (status 400)"
    # Bare API keys (sk-ant-api03- / sk-proj-) are stripped even without a
    # key=value wrapper (S5).
    assert redact("err: sk-ant-api03-abc123def456ghi789") == "err: [REDACTED]"
    assert redact("err: sk-proj-xyz789abcdefghijkl") == "err: [REDACTED]"
    # A real token value after "token" is still redacted.
    assert redact("token loose-sentinel") == "token [REDACTED]"


def test_library_redacts_nested_secrets_tokens_urls_and_stderr(tmp_path: Path) -> None:
    logger = Logger.worker(tmp_path, runtime="claude", name="safe")
    logger.error(
        "worker.exited",
        secret="secret-sentinel",
        nested={"accessToken": "token-sentinel"},
        detail="url=https://user:password@example.test/path?token=url-sentinel",
        stderrTail=(
            "Authorization: Bearer auth-sentinel\n"
            "api_key=stderr-sentinel token loose-sentinel"
        ),
        jwt="abcdefgh.ijklmnop.qrstuvwx",
    )

    raw = logger.path.read_text(encoding="utf-8")
    for sentinel in (
        "secret-sentinel",
        "token-sentinel",
        "auth-sentinel",
        "password@example.test",
        "url-sentinel",
        "stderr-sentinel",
        "loose-sentinel",
        "abcdefgh.ijklmnop.qrstuvwx",
    ):
        assert sentinel not in raw
    record = _records(logger.path)[0]
    assert record["secret"] == "[REDACTED]"
    assert record["nested"] == {"accessToken": "[REDACTED]"}
    assert "[REDACTED_URL]" in record["detail"]  # type: ignore[operator]
    assert "[REDACTED_TOKEN]" in record["jwt"]  # type: ignore[operator]


def test_reserved_schema_fields_cannot_be_overridden(tmp_path: Path) -> None:
    logger = Logger.daemon(tmp_path, name="node")
    with pytest.raises(ValueError, match="reserved log fields"):
        logger.info("safe.event", component="attacker")


def test_thread_and_process_appends_keep_every_jsonl_record(tmp_path: Path) -> None:
    logger = Logger.worker(tmp_path, runtime="pi", name="concurrent-worker")
    thread_count = 4
    records_per_writer = 20

    def write_thread(thread_id: int) -> None:
        for index in range(records_per_writer):
            logger.info("concurrent.record", thread=thread_id, index=index)

    with ThreadPoolExecutor(max_workers=thread_count) as executor:
        list(executor.map(write_thread, range(thread_count)))

    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_process_writer,
            args=(str(tmp_path), process_id, records_per_writer),
        )
        for process_id in range(3)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    records = _records(logger.path)
    assert len(records) == (thread_count + len(processes)) * records_per_writer
    assert all(record["event"] == "concurrent.record" for record in records)


def test_rotation_hook_runs_inside_the_shared_append_path(tmp_path: Path) -> None:
    calls: list[Path] = []
    logger = Logger.daemon(
        tmp_path, name="node", rotation_hook=lambda path: calls.append(path)
    )

    logger.info("daemon.ready")

    assert calls == [logger.path]
