"""Read installed public Agent identities without changing the fixture registry."""

from __future__ import annotations

from typing import Any

from hyprial.kernel import is_agent_id_segment, parse_agent_uri


_READ_AGENT_IDENTITIES = '''
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
from hyprial.identity import AgentRegistry

database = Path(sys.argv[1]) / "agents.sqlite3"
if not database.is_file():
    raise AssertionError("the fixture agent registry does not exist")
owner, machine = sys.argv[2:4]
names = json.loads(sys.argv[4])
with tempfile.TemporaryDirectory(prefix="upgrade-registry-read-") as raw:
    snapshot = Path(raw) / "agents.sqlite3"
    with closing(sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(snapshot)) as target:
            source.backup(target)
    registry = AgentRegistry(snapshot, owner=owner, machine=machine,
                             legacy_directory=Path(raw) / "no-legacy")
    try:
        records = {}
        for name in names:
            agent = registry.get(name)
            if agent is None:
                raise AssertionError("the fixture agent identity is missing: " + name)
            records[name] = {"actor": agent.actor, "uri": agent.uri,
                             "owner": agent.owner, "machine": agent.machine,
                             "agentId": agent.agent_id}
        print(json.dumps(records))
    finally:
        registry.close()
'''


def read_agent_identities(
    entry: dict[str, Any], state, installed_python, env, *, owner: str,
    machine: str, names: tuple[str, ...],
) -> dict[str, dict[str, str]]:
    """The installed reader migrates only a disposable read-only SQLite backup."""
    records = entry['json'].loads(entry['run'](
        installed_python, "-c", _READ_AGENT_IDENTITIES, state, owner, machine,
        entry['json'].dumps(names), env=env,
    ))
    if not isinstance(records, dict) or set(records) != set(names):
        raise AssertionError("the public registry reader returned the wrong identities")
    for name, record in records.items():
        _record(name, record)
        if (record['owner'], record['machine']) != (owner, machine):
            raise AssertionError("the fixture registry returned a foreign identity")
    return records


def _record(name: str, record: object) -> dict[str, str]:
    keys = ('actor', 'uri', 'owner', 'machine', 'agentId')
    if not isinstance(record, dict) or any(
        not isinstance(record.get(key), str) or not record[key] for key in keys
    ):
        raise AssertionError("the public registry identity is incomplete")
    if record['actor'] != name or not is_agent_id_segment(record['agentId']):
        raise AssertionError("the public registry returned the wrong logical identity")
    parsed = parse_agent_uri(record['uri'])
    if parsed is None or parsed[:2] != (record['owner'], record['machine']):
        raise AssertionError("the public registry URI has the wrong namespace")
    if parsed[2] not in {name, record['agentId']}:
        raise AssertionError("the public registry URI names another incarnation")
    return record


def migrated_identities(
    before: dict[str, dict[str, str]], current: dict[str, dict[str, str]],
) -> dict[str, dict[str, dict[str, str]]]:
    """Pin current addresses to the original public incarnation ids."""
    if not before or set(before) != set(current):
        raise AssertionError("the registry identity set changed during upgrade")
    result = {}
    for name in before:
        old, new = _record(name, before[name]), _record(name, current[name])
        if any(old[key] != new[key] for key in ('actor', 'owner', 'machine', 'agentId')):
            raise AssertionError("an agent incarnation changed during upgrade")
        if parse_agent_uri(new['uri'])[2] != new['agentId']:
            raise AssertionError("the current registry did not publish its canonical entity address")
        result[name] = {'before': dict(old), 'current': dict(new)}
    return result


def current_channel_actor(original_actor: str, identity: dict[str, Any]) -> str:
    before, current = identity['before'], identity['current']
    if original_actor != before['uri']:
        raise AssertionError("the Channel actor is not its captured registry identity")
    mapped = migrated_identities({before['actor']: before}, {before['actor']: current})
    return mapped[before['actor']]['current']['uri']
