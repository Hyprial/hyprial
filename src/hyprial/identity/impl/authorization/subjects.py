"""Canonical three-segment subjects with positional kernel ID validation."""

import re

from .ports import DecodeCode, DecodeError, Subject, SubjectPattern


def _segments(raw: str) -> tuple[str, str, str]:
    if not isinstance(raw, str):
        raise DecodeError(DecodeCode.SEGMENT_COUNT)
    match = re.fullmatch(r"([^:]*):([^:]*):([^:]*)", raw)
    if match is None:
        raise DecodeError(DecodeCode.SEGMENT_COUNT)
    return match.groups()


def decode_pattern(raw: str) -> SubjectPattern:
    return SubjectPattern(*_segments(raw))


def decode_subject(raw: str) -> Subject:
    return Subject(*_segments(raw))


def matches(pattern: SubjectPattern, subject: Subject) -> bool:
    return all(
        expected == "*" or expected == actual
        for expected, actual in (
            (pattern.owner, subject.owner),
            (pattern.host, subject.host),
            (pattern.agent, subject.agent),
        )
    )
