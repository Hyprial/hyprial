"""Private process effect entry; never an HTTP or user-facing CLI protocol."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import shutil
from typing import Any

from hyprial.kernel import bind_current_process_to_parent

from .inventory import configuration, scope_inventory, unknown


def run(job: dict[str, Any], publish) -> None:
    home, state_dir = Path(job["home"]), Path(job["stateDir"])
    adapter = job["adapter"]
    if job["action"] == "inventory":
        publish({"status": "completed", "authorization": scope_inventory(home, state_dir, adapter)})
        return
    app_id = job["appId"]
    current = configuration(home, state_dir, adapter)
    if current["appId"] != app_id:
        publish({"status": "completed", "authorization": unknown(adapter, "CONFIGURATION_CHANGED", app_id)})
        return
    import lark_oapi
    from lark_oapi.scene.registration.errors import AppAccessDeniedError, AppExpiredError, RegisterAppError
    from hyprial.daemon.impl.adapters.lark.credentials.onboarding import reauthorize_lark_app

    def prompt(value) -> None:
        publish({"status": "awaiting_user", "verificationUrl": value.url, "expiresIn": value.expire_in})

    try:
        domain = os.environ.get("HYPRIAL_LARK_ACCOUNTS_DOMAIN")
        reauthorize_lark_app(app_id=app_id, register_app=lark_oapi.register_app,
            on_verification_url=prompt, source="hyprial",
            **({"domain": domain, "lark_domain": domain} if domain else {}))
        publish({"status": "checking"})
        result = scope_inventory(home, state_dir, adapter)
        if result["appId"] != app_id:
            result = unknown(adapter, "CONFIGURATION_CHANGED", app_id)
        publish({"status": "completed", "authorization": result})
    except AppAccessDeniedError:
        publish({"status": "denied", "errorCode": "AUTHORIZATION_DENIED"})
    except AppExpiredError:
        publish({"status": "expired", "errorCode": "AUTHORIZATION_EXPIRED"})
    except RegisterAppError as error:
        publish({"status": "cancelled" if error.code == "abort" else "unknown",
                 "errorCode": "AUTHORIZATION_CANCELLED" if error.code == "abort" else "AUTHORIZATION_FAILED"})
    except Exception:  # noqa: BLE001 - raw SDK errors can contain secret material
        publish({"status": "unknown", "errorCode": "AUTHORIZATION_FAILED"})


def main() -> None:
    path = Path(sys.argv[1])

    def publish(value: dict[str, Any]) -> None:
        temporary = path.parent / "result.tmp"
        temporary.write_text(json.dumps(value), encoding="utf-8")
        temporary.replace(path.parent / "result.json")

    release_binding = None
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
        release_binding = bind_current_process_to_parent(
            job["ownerPid"], job["ownerIdentity"],
            before_exit=lambda: shutil.rmtree(path.parent, ignore_errors=True),
        )
        run(job, publish)
    except Exception:  # noqa: BLE001 - only sanitized process status crosses the boundary
        publish({"status": "unknown", "errorCode": "AUTHORIZATION_WORKER_FAILED"})
    finally:
        if release_binding is not None:
            release_binding()


if __name__ == "__main__":
    main()
