from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from hyprial.daemon.impl.harnesses.codex import CodexAppServerClient
from hyprial.kernel.impl.log import Logger

from ..support import spec


class _ProcessGroup:
    def register(self, _pid: int) -> None:
        return None

    def exists(self, _pid: int) -> bool:
        return False

    def release_if_gone(self, _pid: int) -> bool:
        return True

    def signal(self, _pid: int, _signum: int) -> None:
        return None


def test_unrecorded_codex_version_warns_once_and_launches(tmp_path: Path) -> None:
    code = """
import json, sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get('method')
    identifier = message.get('id')
    if method == 'initialize':
        result = {'userAgent': 'fixture/9.9.9 (Test OS; arm64) terminal (fixture; 1)'}
        print(json.dumps({'id': identifier, 'result': result}), flush=True)
    elif method == 'thread/start':
        result = {'thread': {'id': 'thread-version'}}
        print(json.dumps({'id': identifier, 'result': result}), flush=True)
"""

    async def drive() -> None:
        logger = Logger.worker(
            tmp_path / "state", runtime="codex", name="codex-version"
        )
        client = CodexAppServerClient(
            spec(),
            command=(sys.executable, "-c", code),
            process_group=_ProcessGroup(),  # type: ignore[arg-type]
            logger=logger,
        )
        async with client:
            assert client.session_ref == "thread-version"

        records = [
            json.loads(line)
            for line in logger.path.read_text(encoding="utf-8").splitlines()
        ]
        warnings = [
            record
            for record in records
            if record["event"] == "codex.version.unrecorded"
        ]
        assert len(warnings) == 1
        assert warnings[0]["level"] == "warn"
        assert warnings[0]["version"] == "9.9.9"

    asyncio.run(drive())
