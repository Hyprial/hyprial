"""Atomic decoding of strict versioned rule payloads from original JSON bytes."""

import json

from .ports import DecodeCode, DecodeError, Rule
from .subjects import decode_pattern


def valid_token(value: object, *, limit: int) -> bool:
    return (
        type(value) is str and 1 <= len(value) <= limit
        and all(33 <= ord(char) <= 126 and char not in "*\\" for char in value)
    )


def _object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result = {}
    for key, value in pairs:
        if key in result:
            raise DecodeError(DecodeCode.DUPLICATE_KEY)
        result[key] = value
    return result


def _invalid_constant(value: str) -> None:
    raise DecodeError(DecodeCode.PAYLOAD_INVALID)


def decode_rules(raw: bytes) -> tuple[Rule, ...]:
    if type(raw) is not bytes:
        raise DecodeError(DecodeCode.PAYLOAD_INVALID)
    try:
        payload = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object,
            parse_constant=_invalid_constant,
        )
        if type(payload) is not dict:
            raise DecodeError(DecodeCode.PAYLOAD_INVALID)
        if type(payload.get("schemaVersion")) is not int or payload["schemaVersion"] != 1:
            raise DecodeError(DecodeCode.SCHEMA_VERSION_INVALID)
        if set(payload) != {"schemaVersion", "subjectProfile", "rules"}:
            raise DecodeError(DecodeCode.PAYLOAD_INVALID)
        if payload["subjectProfile"] != "stable-id-v1" or type(payload["rules"]) is not list:
            raise DecodeError(DecodeCode.PAYLOAD_INVALID)
        rules = []
        for entry in payload["rules"]:
            if type(entry) is not dict or set(entry) != {"resource", "action", "allow"}:
                raise DecodeError(DecodeCode.PAYLOAD_INVALID)
            if type(entry["allow"]) is not list:
                raise DecodeError(DecodeCode.PAYLOAD_INVALID)
            if not valid_token(entry["resource"], limit=2048) or not valid_token(entry["action"], limit=128):
                raise DecodeError(DecodeCode.PAYLOAD_INVALID)
            rules.append(Rule(
                resource=entry["resource"], action=entry["action"],
                allow=tuple(decode_pattern(value) for value in entry["allow"]),
            ))
        return tuple(rules)
    except DecodeError:
        raise
    except (KeyError, TypeError, ValueError, RecursionError):
        raise DecodeError(DecodeCode.PAYLOAD_INVALID) from None
