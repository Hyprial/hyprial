"""Fail-closed scan for credential material in mockable examples.

Decision record (M-03, 2026-10-31)
- Examples and mocks are development material that is committed, served and
  often pasted into issues.  A real token or home path there is a release of a
  secret even though it never reached the daemon.
- The rule is deliberately narrow and explicable: sensitive keys may only carry
  the synthetic sentinel, a ``contentBase64`` document must decode to that
  sentinel, and every string is checked for well-known credential prefixes,
  PEM armour, long base64 blobs and absolute user-home paths.
- Findings name the JSON path and never echo the candidate value, so a failing
  gate cannot spread the secret it caught.
"""

from __future__ import annotations

import base64
import binascii
import re
from typing import Any

SENTINEL = "example-not-a-credential"
SENTINEL_B64 = base64.b64encode(SENTINEL.encode("utf-8")).decode("ascii")
_SENSITIVE_KEYS = frozenset(
    {"setuptoken", "token", "password", "secret", "apikey", "contentbase64"}
)
_SENSITIVE_SUFFIXES = ("token", "password", "apikey")
_CREDENTIAL_PREFIXES = (
    "sk-",
    "ghp_",
    "github_pat_",
    "xoxb-",
    "xoxp-",
    "AKIA",
    "-----BEGIN ",
)
_LONG_BASE64 = re.compile(r"^[A-Za-z0-9+/]{60,}={0,2}$")


class SensitiveMaterialError(ValueError):
    """A mockable example contains material that must never be published."""


def _is_sensitive(key: str) -> bool:
    lowered = key.lower()
    return lowered in _SENSITIVE_KEYS or lowered.endswith(_SENSITIVE_SUFFIXES)


def _check_string(value: str, path: str, findings: list[str]) -> None:
    if value.startswith(_CREDENTIAL_PREFIXES):
        findings.append(f"{path}: looks like a credential prefix")
    if "/Users/" in value or "HYPRIAL_HOME" in value or "~/." in value:
        findings.append(f"{path}: absolute user-home path is not allowed")
    if _LONG_BASE64.fullmatch(value) and value != SENTINEL_B64:
        findings.append(f"{path}: long base64 blob is not the synthetic sentinel")


def _check_sensitive(key: str, value: Any, path: str, findings: list[str]) -> None:
    if value is None:
        return
    if key.lower() == "contentbase64":
        if not isinstance(value, str) or not value:
            findings.append(f"{path}: contentBase64 must be a non-empty string")
            return
        try:
            decoded = base64.b64decode(value, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError):
            findings.append(f"{path}: contentBase64 is not valid UTF-8 base64")
            return
        if decoded != SENTINEL:
            findings.append(f"{path}: contentBase64 must decode to the synthetic sentinel")
        return
    if not isinstance(value, str) or value != SENTINEL:
        findings.append(f"{path}: sensitive value must be the synthetic sentinel")


def scan_value(value: Any, path: str = "$") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if _is_sensitive(key):
                _check_sensitive(key, item, child, findings)
            else:
                findings.extend(scan_value(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            findings.extend(scan_value(item, f"{path}[{index}]"))
    elif isinstance(value, str):
        _check_string(value, path, findings)
    return findings


def assert_no_sensitive_material(document: Any, path: str = "$") -> None:
    findings = scan_value(document, path)
    if findings:
        raise SensitiveMaterialError(
            "sensitive material found in mockable examples: " + "; ".join(findings)
        )
