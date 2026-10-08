from __future__ import annotations



import json





from typing import Any







from hyprial.daemon.impl.orgfs.api  import OrgFsError


from hyprial.daemon.impl.orgfs.projection.checkout_authority  import CheckoutAuthority








from hyprial.daemon.impl.orgfs.composition.vocabulary import _CheckoutSpace

class RuntimeCheckout:
    """Responsibility methods on the sole OrgFsRuntime state host.

    This class never constructs, copies, or persists an independent host.
    """

    def purge_plan(self, space_id: str, targets: list[dict[str, Any]]) -> Any:
        return self.facade.purge_plan(space_id, targets)


    def purge(self, space_id: str, plan_id: str) -> Any:
        result = self.facade.purge(space_id, plan_id)
        self._reconcile_retirements(space_id)
        return result


    def purge_status(self, space_id: str, plan_id: str) -> Any:
        return self.facade.purge_status(space_id, plan_id)


    def unban(self, space_id: str, sha: str) -> None:
        self.facade.unban(space_id, sha)


    def _write_checkout_config(self) -> None:
        with self._checkout_config_lock:
            with self._checkout_lock:
                spaces = sorted(self._checkouts)
            self._checkout_config.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._checkout_config.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {"schemaVersion": 1, "spaces": spaces},
                    separators=(",", ":"),
                    sort_keys=True,
                ),
                encoding="utf-8",
            )
            temporary.replace(self._checkout_config)


    def _restore_checkouts(self) -> None:
        try:
            value = json.loads(self._checkout_config.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            if self.logger is not None:
                self.logger("warn", "orgfs.checkout.restore-failed", detail=str(exc))
            return
        spaces = value.get("spaces") if isinstance(value, dict) else None
        if not isinstance(spaces, list):
            return
        for space_id in spaces:
            if not isinstance(space_id, str) or space_id not in self.stores:
                continue
            try:
                self._set_checkout(space_id, True, persist=False)
            except Exception as exc:
                if self.logger is not None:
                    self.logger(
                        "warn",
                        "orgfs.checkout.restore-failed",
                        spaceId=space_id,
                        detail=str(exc),
                    )


    def _set_checkout(
        self, space_id: str, enabled: bool, *, persist: bool
    ) -> dict[str, object]:
        root = self.state_dir / "orgfs" / "spaces" / space_id / "checkout"
        with self._checkout_lock:
            if self._checkout_closing:
                raise OrgFsError("unavailable", {"message": "checkouts are closing"})
            authority = self._checkouts.get(space_id)
            if authority is None:
                authority = CheckoutAuthority(self.facade, self.blobs, space_id, root)
                if enabled:
                    self._checkouts[space_id] = authority
        if enabled:
            try:
                authority.enable()
            except Exception:
                with self._checkout_lock:
                    if self._checkouts.get(space_id) is authority:
                        self._checkouts.pop(space_id, None)
                authority.close(timeout=5.0)
                raise
        else:
            try:
                authority.disable()
            finally:
                with self._checkout_lock:
                    if self._checkouts.get(space_id) is authority:
                        self._checkouts.pop(space_id, None)
                authority.close(timeout=5.0)
        if persist:
            self._write_checkout_config()
        return {"spaceId": space_id, "enabled": enabled, "path": str(root)}


    def checkout(self, space_id: str, enabled: bool) -> dict[str, object]:
        return self._ask_lifecycle(_CheckoutSpace(space_id, bool(enabled)))  # type: ignore[return-value]


    def _checkout_sync(self, space_id: str, enabled: bool) -> dict[str, object]:
        return self._set_checkout(space_id, bool(enabled), persist=True)
