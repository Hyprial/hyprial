"""Owned SDK effect; credentials travel only through the private worker result."""

import os

from ..onboarding import resolve_lark_app_credential


def run(job, publish):
    import lark_oapi
    from lark_oapi.scene.registration.errors import AppAccessDeniedError, AppExpiredError

    def prompt(value):
        publish({"status": "awaiting_user", "verificationUrl": value.url, "expiresIn": value.expire_in})

    try:
        domain = os.environ.get("HYPRIAL_LARK_ACCOUNTS_DOMAIN")
        credential = resolve_lark_app_credential(
            name=job["adapter"], register_app=lark_oapi.register_app, on_verification_url=prompt,
            non_interactive=False, env={}, create_only=True, source="hyprial",
            **({"domain": domain, "lark_domain": domain} if domain else {}),
        )
        publish({"status": "completed", "credential": {"appId": credential.app_id, "appSecret": credential.app_secret}})
    except AppAccessDeniedError:
        publish({"status": "denied"})
    except AppExpiredError:
        publish({"status": "expired"})
    except Exception:
        publish({"status": "unknown", "errorCode": "APP_CREATION_UNCONFIRMED"})
