"""The jev worker finds TYPESAFE_API_KEY where the user-side ``jev`` command does.

Allen 2026-09-23: «用户只要在自己的环境变量中存过且可以使用jev bash命令,就不需要填写jev的key».
The user-side ``jev`` reads the key after ``source ~/.config/typesafe/env``; a
hyprial worker's environment is an allowlist, so the shell export never reaches
it.  The worker therefore reads that one file itself -- parsed, never executed --
after an explicitly injected environment value, which still wins.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hyprial.daemon.impl.harnesses.python_worker import _python_worker as worker
from hyprial.daemon.impl.harnesses.python_worker._python_worker import CredentialLookup, resolve_credential

SENTINEL = "ts-sentinel-0f3c9b2e7a41d6"


def _home(tmp_path: Path, content: str | None) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    if content is not None:
        target = home / ".config" / "typesafe"
        target.mkdir(parents=True)
        (target / "env").write_text(content, encoding="utf-8")
    return home


# -- the five lookup cases --------------------------------------------------------


def test_environment_value_is_used_and_named_environment(tmp_path: Path) -> None:
    home = _home(tmp_path, f"export TYPESAFE_API_KEY='{SENTINEL}-file'\n")
    value, source, code = resolve_credential(
        {"TYPESAFE_API_KEY": SENTINEL, "HOME": str(home)}
    )
    assert (value, source, code) == (SENTINEL, "environment", None)


def test_file_is_read_when_the_environment_has_no_value(tmp_path: Path) -> None:
    home = _home(tmp_path, f"# comment\n\nexport TYPESAFE_API_KEY='{SENTINEL}'\n")
    value, source, code = resolve_credential({"HOME": str(home)})
    assert (value, source, code) == (SENTINEL, "file", None)


def test_neither_source_stops_at_file_absent(tmp_path: Path) -> None:
    home = _home(tmp_path, None)
    assert resolve_credential({"HOME": str(home)}) == (
        None,
        None,
        CredentialLookup.FILE_ABSENT,
    )


def test_file_without_the_key_is_distinct_from_no_file(tmp_path: Path) -> None:
    home = _home(tmp_path, "export OTHER_KEY='x'\n")
    assert resolve_credential({"HOME": str(home)}) == (
        None,
        None,
        CredentialLookup.KEY_ABSENT,
    )


def test_no_home_stops_at_env_absent_and_reads_no_file() -> None:
    assert resolve_credential({}) == (None, None, CredentialLookup.ENV_ABSENT)


# -- parsing: the file is data, never a script -------------------------------------


@pytest.mark.parametrize(
    "line",
    [
        f"export TYPESAFE_API_KEY='{SENTINEL}'",
        f'export TYPESAFE_API_KEY="{SENTINEL}"',
        f"TYPESAFE_API_KEY={SENTINEL}",
        f"   export   TYPESAFE_API_KEY='{SENTINEL}'   ",
    ],
)
def test_supported_assignment_shapes(tmp_path: Path, line: str) -> None:
    home = _home(tmp_path, line + "\n")
    assert resolve_credential({"HOME": str(home)})[0] == SENTINEL


def test_last_assignment_wins_like_source_would(tmp_path: Path) -> None:
    home = _home(
        tmp_path,
        f"export TYPESAFE_API_KEY='old'\nexport TYPESAFE_API_KEY='{SENTINEL}'\n",
    )
    assert resolve_credential({"HOME": str(home)})[0] == SENTINEL


def test_empty_value_counts_as_absent(tmp_path: Path) -> None:
    home = _home(tmp_path, "export TYPESAFE_API_KEY=''\n")
    assert resolve_credential({"HOME": str(home)})[2] == CredentialLookup.KEY_ABSENT


def test_other_keys_and_commented_assignments_are_ignored(tmp_path: Path) -> None:
    home = _home(
        tmp_path,
        f"# export TYPESAFE_API_KEY='{SENTINEL}'\nexport TYPESAFE_API_KEY_OLD='x'\n",
    )
    assert resolve_credential({"HOME": str(home)})[2] == CredentialLookup.KEY_ABSENT


def test_the_file_is_never_executed(tmp_path: Path) -> None:
    marker = tmp_path / "executed"
    home = _home(
        tmp_path,
        f"touch {marker}\nexport TYPESAFE_API_KEY='{SENTINEL}'\n",
    )
    assert resolve_credential({"HOME": str(home)})[0] == SENTINEL
    assert not marker.exists()


def test_the_path_is_a_constant_not_taken_from_the_environment() -> None:
    assert worker.CREDENTIAL_FILE_RELATIVE == Path(".config") / "typesafe" / "env"


# -- the closed set of lookup codes ------------------------------------------------


def test_lookup_codes_are_a_closed_set_of_four() -> None:
    assert CredentialLookup.ALL == (
        "TYPESAFE_CREDENTIAL_ENV_ABSENT",
        "TYPESAFE_CREDENTIAL_FILE_ABSENT",
        "TYPESAFE_CREDENTIAL_KEY_ABSENT",
        "PROVIDER_AUTHENTICATION_FAILED",
    )
    assert len(set(CredentialLookup.ALL)) == 4


@pytest.mark.parametrize(
    "code",
    [
        CredentialLookup.ENV_ABSENT,
        CredentialLookup.FILE_ABSENT,
        CredentialLookup.KEY_ABSENT,
    ],
)
def test_unavailable_error_keeps_the_permanent_code_and_names_where_to_configure(
    code: str,
) -> None:
    error = worker._credential_unavailable(code)
    # Permanent in daemon/api.py, so the turn is not redelivered.
    assert error.code == "PROVIDER_AUTHENTICATION_FAILED"
    assert str(error).startswith(f"{code}: ")
    assert "~/.config/typesafe/env" in str(error)
    assert "hyprial agent secret" in str(error)


# -- end to end through the worker process, with a sentinel -------------------------


def _run_worker(environment: dict[str, str]) -> tuple[list[dict[str, object]], str]:
    process = subprocess.run(
        [sys.executable, "-m", "hyprial.daemon.impl.harnesses.python_worker._python_worker", "--kind", "jev"],
        input=(
            json.dumps(
                {
                    "v": 1,
                    "op": "call",
                    "id": "cred-probe",
                    "payload": {
                        "state": {"ticket": "probe"},
                        "questions": {"urgent": {"type": "noul"}},
                    },
                }
            )
            + "\n"
            + '{"v":1,"op":"stop"}\n'
        ).encode(),
        capture_output=True,
        env=environment,
        timeout=60,
    )
    frames = [json.loads(line) for line in process.stdout.decode().splitlines() if line]
    return frames, process.stdout.decode() + process.stderr.decode()


def _base_environment(home: Path) -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name in {"PATH", "LANG", "PYTHONPATH"}
    }
    environment["HOME"] = str(home)
    environment["HYPRIAL_SOURCE_COMMIT"] = "3137ed2ad92e5bb2c4369661d660815b63ae787b"
    return environment


def test_ready_reports_file_source_and_the_value_appears_nowhere(
    tmp_path: Path,
) -> None:
    home = _home(tmp_path, f"export TYPESAFE_API_KEY='{SENTINEL}'\n")
    environment = _base_environment(home)
    # Never reach the real service with a fake key: route it to a dead local
    # proxy so the call fails fast on connection, after the key was resolved.
    environment.update(
        HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9"
    )
    frames, raw = _run_worker(environment)
    ready = next(frame for frame in frames if frame.get("type") == "ready")
    assert ready["credentialSource"] == "file"
    # The call got PAST the credential lookup: it fails on the dead proxy
    # (transient), not as "credential unavailable".  Without this the zero below
    # could mean the key was never read at all.
    error = next(frame for frame in frames if frame.get("type") == "error")
    assert error["code"] == "PROVIDER_TRANSIENT_FAILURE"
    # ... and the value is in no emitted byte, stdout or stderr.
    assert raw.count(SENTINEL) == 0


def test_no_credential_starts_and_fails_the_first_call_loudly(tmp_path: Path) -> None:
    frames, raw = _run_worker(_base_environment(_home(tmp_path, None)))
    ready = next(frame for frame in frames if frame.get("type") == "ready")
    assert ready["credentialSource"] is None
    error = next(frame for frame in frames if frame.get("type") == "error")
    assert error["code"] == "PROVIDER_AUTHENTICATION_FAILED"
    assert error["stage"] == "call"
    assert str(error["message"]).startswith("TYPESAFE_CREDENTIAL_FILE_ABSENT: ")
    assert "Traceback" not in raw
