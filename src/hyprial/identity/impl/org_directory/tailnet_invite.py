"""Closed InviteV2 payload validation; no transport or OrgFS dependencies."""
from __future__ import annotations

import re
from collections.abc import Mapping

KEY = re.compile(r'nodekey:[0-9a-f]{64}\Z')
AUTHORITY_FIELDS = {'owner', 'deviceId', 'masterPublic', 'serverPublic', 'address', 'controlPort'}
INVITE_FIELDS = {'version', 'tailnetId', 'authority', 'inviteId', 'token', 'owner', 'expiresAtMs'}


def validate_tailnet_invite(value: object, *, space_id: str) -> dict:
    if not isinstance(value, Mapping) or set(value) != INVITE_FIELDS:
        raise ValueError('tailnet invitation fields are invalid')
    if type(value['version']) is not int or value['version'] != 2:
        raise ValueError('tailnet invitation version must be 2')
    for field in ('tailnetId', 'inviteId', 'owner'):
        text = value[field]
        if not isinstance(text, str) or not text or len(text.encode()) > 1024:
            raise ValueError('tailnet invitation identity is invalid')
    if value['tailnetId'] != space_id:
        raise ValueError('tailnet invitation space mismatch')
    token = value['token']
    if not isinstance(token, str) or re.fullmatch(r'[A-Za-z0-9_-]{43}', token) is None:
        raise ValueError('tailnet invitation token is invalid')
    expires = value['expiresAtMs']
    if type(expires) is not int or not 0 <= expires <= 2**53 - 1:
        raise ValueError('tailnet invitation expiry is invalid')
    authority = value['authority']
    if not isinstance(authority, Mapping) or set(authority) != AUTHORITY_FIELDS:
        raise ValueError('tailnet authority fields are invalid')
    for field in ('owner', 'deviceId', 'address'):
        if not isinstance(authority[field], str) or not authority[field]:
            raise ValueError('tailnet authority identity is invalid')
    for field in ('masterPublic', 'serverPublic'):
        if not isinstance(authority[field], str) or KEY.fullmatch(authority[field]) is None:
            raise ValueError('tailnet authority key is invalid')
    if type(authority['controlPort']) is not int or not 1 <= authority['controlPort'] <= 65535:
        raise ValueError('tailnet control port is invalid')
    return dict(value) | {'authority': dict(authority)}
