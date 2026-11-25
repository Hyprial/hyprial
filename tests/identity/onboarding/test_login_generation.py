"""The public login-generation reader returns only the non-secret integer."""

from __future__ import annotations

import json
from pathlib import Path

from hyprial.identity import read_login_generation


def _write_login(home: Path, record: object) -> None:
    secrets = home / "secrets"
    secrets.mkdir(parents=True)
    (secrets / "login.json").write_text(json.dumps(record), encoding="utf-8")


def test_a_committed_generation_is_read(tmp_path):
    _write_login(
        tmp_path,
        {"version": 1, "refreshToken": "never-returned", "identityGeneration": 7},
    )
    assert read_login_generation(tmp_path) == 7


def test_a_missing_or_pre_generation_record_answers_none(tmp_path):
    assert read_login_generation(tmp_path) is None
    _write_login(tmp_path, {"version": 1, "refreshToken": "x"})
    assert read_login_generation(tmp_path) is None


def test_logout_removal_answers_none_even_with_a_high_water_mark(tmp_path):
    (tmp_path / ".identity-generation").write_text(
        json.dumps({"identityGeneration": 9}), encoding="utf-8"
    )
    assert read_login_generation(tmp_path) is None


def test_a_corrupt_record_answers_none_instead_of_raising(tmp_path):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "login.json").write_text("{not json", encoding="utf-8")
    assert read_login_generation(tmp_path) is None
