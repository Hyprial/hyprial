"""Intranet-marker and leak-report policy implementation.

The command module supplies its canonical rule table and exception type so
its public constants and monkeypatch seams remain authoritative.

Decision record (security review REJECT of #2330, 2026-11-03): an exemption
used to cover a whole (file, rule) pair, so a new real marker added to an
exempted file passed silently (reproduced by the reviewer).  An exemption now
covers exactly one line, identified by the SHA-256 of its bytes: editing the
line, or adding another marker line, makes it a hit again.  Line numbers are
not part of the key, so unrelated edits do not churn the allowlist.  Real
node and device names cannot be a published regex (the rule would publish
them), so they come from a private marker file kept outside the snapshot.
File paths are scanned too (a name can sit in a file name), and a path hit
can never be exempted.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Pattern

PRIVATE_MARKER_RULE = "private-marker"


@dataclass(frozen=True)
class AllowlistEntry:
    path: str
    rule: str
    fingerprint: str
    reason: str


@dataclass(frozen=True)
class IntranetScan:
    """What one scan found, including the exact exemptions it applied."""

    hits: list[str]
    exemptions: list[str]
    scanned: int
    used: set[tuple[str, str, str]]


def line_fingerprint(line: bytes) -> str:
    """The exemption key of one raw line (trailing CR ignored)."""
    return hashlib.sha256(line.rstrip(b"\r")).hexdigest()[:16]


def private_marker_rule(text: str, error_type) -> tuple[str, Pattern[bytes]]:
    """Compile the private marker list (one literal per line, ``#`` comments).

    A marker matches as a whole token (no letter or digit on either side), so
    a short handle does not fire inside a checksum or a longer word.
    """
    markers = [
        line.strip() for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not markers:
        raise error_type("private marker list is empty; refusing to scan without it")
    alternation = b"|".join(re.escape(m.encode("utf-8")) for m in sorted(markers, key=len, reverse=True))
    return PRIVATE_MARKER_RULE, re.compile(
        b"(?i)(?<![A-Za-z0-9])(?:" + alternation + b")(?![A-Za-z0-9])"
    )


def parse_allowlist(text: str, rules, error_type) -> tuple[AllowlistEntry, ...]:
    """Parse strict ``path :: rule :: line-sha256-16 :: reason`` records."""
    known = {name for name, _ in rules} | {PRIVATE_MARKER_RULE}
    entries: list[AllowlistEntry] = []
    seen: set[tuple[str, str, str]] = set()
    for lineno, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("::")]
        if len(parts) != 4 or not all(parts):
            raise error_type(
                f"allowlist line {lineno} malformed (expected "
                "'path :: rule :: line-sha256-16 :: reason'; whole-file "
                "exemptions are no longer accepted): " + repr(line)
            )
        path, rule, fingerprint, reason = parts
        if rule not in known:
            raise error_type(
                f"allowlist line {lineno}: unknown rule {rule!r}; "
                f"known rules: {', '.join(sorted(known))}"
            )
        if rule == PRIVATE_MARKER_RULE:
            raise error_type(
                f"allowlist line {lineno}: {PRIVATE_MARKER_RULE} hits cannot be exempted"
            )
        if not re.fullmatch(r"[0-9a-f]{16}", fingerprint):
            raise error_type(f"allowlist line {lineno}: fingerprint must be 16 hex digits")
        if (path, rule, fingerprint) in seen:
            raise error_type(f"allowlist line {lineno}: duplicate entry {path!r} / {rule!r}")
        seen.add((path, rule, fingerprint))
        entries.append(AllowlistEntry(path, rule, fingerprint, reason))
    return tuple(entries)


def scan_intranet_markers(
    tree: Path,
    allowlist: tuple[AllowlistEntry, ...],
    rules: tuple[tuple[str, Pattern[bytes]], ...],
) -> IntranetScan:
    """Scan raw file lines and report non-exempt hits fail-closed."""
    reasons = {(e.path, e.rule, e.fingerprint): e.reason for e in allowlist}
    hits: list[str] = []
    exemptions: list[str] = []
    used: set[tuple[str, str, str]] = set()
    scanned = 0
    for path in sorted(tree.rglob("*")):
        if not path.is_file():
            continue
        scanned += 1
        rel = path.relative_to(tree).as_posix()
        for rule_name, pattern in rules:
            if pattern.search(rel.encode("utf-8", "surrogateescape")):
                hits.append(f"{rel}:0 {rule_name} (in the path; paths cannot be exempted)")
        try:
            content = path.read_bytes()
        except OSError as exc:
            hits.append(f"{rel}:0 unreadable-file ({exc.__class__.__name__})")
            continue
        for lineno, line in enumerate(content.splitlines(), start=1):
            for rule_name, pattern in rules:
                if not pattern.search(line):
                    continue
                key = (rel, rule_name, line_fingerprint(line))
                if key in reasons:
                    used.add(key)
                    exemptions.append(f"{rel}:{lineno} {rule_name} :: {reasons[key]}")
                else:
                    hits.append(f"{rel}:{lineno} {rule_name} fp={key[2]}")
    return IntranetScan(hits, exemptions, scanned, used)


def stale_allowlist_entries(
    allowlist: tuple[AllowlistEntry, ...], scan: IntranetScan, tree_paths: set[str]
) -> list[str]:
    """Describe allowlist entries that exempted no marker in this scan."""
    reports: list[str] = []
    for entry in allowlist:
        if (entry.path, entry.rule, entry.fingerprint) in scan.used:
            continue
        if entry.path not in tree_paths:
            reports.append(f"allowlist stale entry (path not in snapshot): {entry.path}")
        else:
            reports.append(
                "allowlist stale entry (no matching marker): "
                f"{entry.path} :: {entry.rule} :: {entry.fingerprint}"
            )
    return reports


def parse_leaks_report(raw: bytes, error_type) -> list[dict[str, object]]:
    """Parse gitleaks JSON and reject anything not shaped as a list of objects."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise error_type(f"gitleaks report unreadable: {exc}") from exc
    if not isinstance(data, list):
        raise error_type("gitleaks report is not a JSON array; refusing to guess")
    findings: list[dict[str, object]] = []
    for item in data:
        if not isinstance(item, dict):
            raise error_type(
                "gitleaks report contains a non-object entry; refusing to guess"
            )
        findings.append(item)
    return findings


def describe_finding(finding: dict[str, object]) -> str:
    """Return rule and location only; never include a matched secret value."""
    rule = finding.get("RuleID", "<unknown-rule>")
    file = finding.get("File", "<unknown-file>")
    line = finding.get("StartLine", "")
    location = f"{file}:{line}" if line != "" else str(file)
    return f"{rule} {location}"
