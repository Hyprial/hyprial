"""Acquire the short-lived, non-bearer Casdoor binding assertion."""

from __future__ import annotations

from dataclasses import replace
from typing import Callable

from hyprial.daemon import NetworkProfile
from hyprial.identity import (
    BINDING_ASSERTION_CLIENT_ID,
    BINDING_ASSERTION_OWNER,
    binding_assertion_claims_unverified,
)

from .oidc import (
    DeviceAuthorization,
    LoginError,
    _fetch_endpoints,
    _poll_device_token,
    _request_device_authorization,
)


BINDING_SCOPE = "openid profile"


def _announce(
    authorization: DeviceAuthorization,
    *,
    restarted: bool,
    emit: Callable[[str, dict], None],
    open_browser: bool,
    open_url: Callable[[str], bool],
) -> None:
    uri = authorization.verification_uri_complete or authorization.verification_uri
    emit(
        "binding-device",
        {
            "verificationUri": uri,
            "userCode": authorization.user_code,
            "interval": authorization.interval,
            "expiresIn": authorization.expires_in,
            "restarted": restarted,
        },
    )
    if open_browser:
        open_url(uri)


def obtain_binding_proof(
    profile: NetworkProfile,
    *,
    emit: Callable[[str, dict], None],
    open_browser: bool,
    open_url: Callable[[str], bool],
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> str:
    """Run at most two assertion-app device grants and return only the JWT."""

    binding_profile = replace(profile, client_id=BINDING_ASSERTION_CLIENT_ID)
    endpoints = _fetch_endpoints(binding_profile)
    for attempt in range(2):
        authorization = _request_device_authorization(
            endpoints, binding_profile, scope=BINDING_SCOPE
        )
        _announce(
            authorization,
            restarted=attempt > 0,
            emit=emit,
            open_browser=open_browser,
            open_url=open_url,
        )
        try:
            proof, _discarded_refresh = _poll_device_token(
                endpoints,
                binding_profile,
                authorization,
                sleep=sleep,
                clock=clock,
                require_refresh=False,
            )
        except LoginError as error:
            if error.code == "DEVICE_CODE_EXPIRED" and attempt == 0:
                continue
            if error.code == "DEVICE_CODE_EXPIRED":
                raise LoginError(
                    "BINDING_PROOF_EXPIRED",
                    "the binding proof authorization expired twice; start a fresh "
                    "login and complete the confirmation promptly",
                    data={"attempts": 2},
                ) from error
            raise
        return proof
    raise AssertionError("bounded binding proof loop did not return")


def validate_binding_confirmation(
    proof: str, *, expected_user: str, expected_union_id: str
) -> None:
    """Refuse a device confirmation completed under another account."""

    try:
        claims = binding_assertion_claims_unverified(proof)
    except ValueError as error:
        raise LoginError(
            "BINDING_PROOF_INVALID",
            "the binding confirmation returned an invalid assertion; will retry",
        ) from error
    if claims.get("owner") != BINDING_ASSERTION_OWNER:
        raise LoginError(
            "BINDING_ACCOUNT_MISMATCH",
            "the binding confirmation belongs to a different Casdoor organization; "
            "will retry",
        )
    if (
        claims.get("name") != expected_user
        or claims.get("oauth_Lark_unionId") != expected_union_id
    ):
        raise LoginError(
            "BINDING_ACCOUNT_MISMATCH",
            "the binding confirmation was approved by another account; will retry",
        )


__all__ = [
    "BINDING_SCOPE",
    "obtain_binding_proof",
    "validate_binding_confirmation",
]
