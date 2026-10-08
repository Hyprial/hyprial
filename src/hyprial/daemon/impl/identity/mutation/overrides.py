"""Local-override writes: each clear removes exactly what its own set created.

§5 symmetry (design head cc2c7184): set {platform, unionId} writes a
union-keyed override and set {adapter, openId} an account-keyed one;
neither clear touches the other's rows.  Mixed into IdentityResolver.
"""

from __future__ import annotations

from typing import Any

from hyprial.kernel import ipc_errors

from ..store import OverrideConflictError
from .errors import IdentityResolverError


class OverrideWrites:
    """override.set / override.clear for IdentityResolver (§5)."""

    def observed_union(self, *, adapter: str, open_id: str) -> str | None:
        self._adapter(adapter)
        rows = self._legacy_rows(adapter=adapter, open_id=open_id)
        unions = sorted({row.union_id for row in rows if row.union_id})
        if len(unions) != 1:
            return None
        return unions[0]

    def set_account_override(
        self, *, adapter: str, open_id: str, user_key: str, confirmed_by: str
    ) -> dict[str, Any]:
        namespaced = self._adapter(adapter)
        users, close = self._open_users()
        if users is None:
            raise IdentityResolverError(
                ipc_errors.INVALID_ARGUMENT, f"no local user {user_key!r}"
            )
        try:
            user = users.get_user(user_key)
            if user is None:
                raise IdentityResolverError(
                    ipc_errors.INVALID_ARGUMENT, f"no local user {user_key!r}"
                )
            existing_account = None
            for existing_user in users.list_users():
                for account in users.accounts(existing_user.user_key):
                    if account.adapter != namespaced or account.open_id != open_id:
                        continue
                    if account.user_key != user_key:
                        raise IdentityResolverError(
                            ipc_errors.IDENTITY_CONFLICT,
                            f"{adapter} {open_id!r} belongs to a different user",
                        )
                    existing_account = account
            union_id: str | None = (
                existing_account.union_id
                if existing_account is not None and existing_account.union_id
                else self.observed_union(adapter=adapter, open_id=open_id)
            )
            if union_id is not None and {
                candidate.user_key for candidate in self._candidates(union_id)
            } - {user_key}:
                raise IdentityResolverError(
                    ipc_errors.IDENTITY_CONFLICT,
                    f"lark unionId {union_id!r} is bound to another user",
                )
            account = existing_account
            if account is None:
                account, _changed = users.bind_account(
                    user_key,
                    adapter=namespaced,
                    open_id=open_id,
                    union_id=union_id,
                    confirmed_by=confirmed_by,
                    source="override",
                )
        finally:
            if close:
                users.close()
        if union_id is not None:
            # Keyed by the account, so clear {adapter, openId} undoes exactly
            # this and never a union override set on its own (§5 symmetry).
            updated_at = self._overrides.set_account(
                adapter=namespaced,
                open_id=open_id,
                union_id=union_id,
                user_key=user_key,
                confirmed_by=confirmed_by,
            ).updated_at_ms
        else:
            updated_at = account.confirmed_at_ms
        return self._binding(
            self._candidate_for_user(
                user,
                source="local-override",
                confirmed_by=confirmed_by,
                updated_at=updated_at,
            ),
            union_id,
            outbound_only=union_id is None,
        )

    def clear_account_override(self, *, adapter: str, open_id: str) -> dict[str, Any]:
        """Undo exactly what set {adapter, openId} created (§5 symmetry)."""

        namespaced = self._adapter(adapter)
        union_id = next(
            (
                row.union_id
                for row in self._overrides.list_accounts()
                if (row.adapter, row.open_id) == (namespaced, open_id)
            ),
            None,
        )
        cleared = self._overrides.clear_account(adapter=namespaced, open_id=open_id)
        users, close = self._open_users()
        try:
            for account in self._accounts() if users is not None else ():
                if (account.adapter, account.open_id) != (namespaced, open_id):
                    continue
                union_id = union_id or account.union_id
                if account.source == "override":
                    assert users is not None
                    users.unbind_account(
                        account.user_key,
                        adapter=namespaced,
                        open_id=open_id,
                        confirmed_by=account.confirmed_by,
                    )
                    cleared = True
        finally:
            if close and users is not None:
                users.close()
        result: dict[str, Any] = {"cleared": cleared}
        remaining = self._candidates(union_id) if union_id is not None else ()
        if remaining:
            # Truthful revocation: the unionId still resolves elsewhere.
            try:
                source = self._resolved(union_id)[0].source  # type: ignore[arg-type]
            except IdentityResolverError:
                source = "conflict"  # bound to more than one user; never pick one
            result["unionBindingRemains"] = {"unionId": union_id, "source": source}
        return result

    def set_override(
        self, *, platform: str, union_id: str, user_key: str, confirmed_by: str
    ) -> dict[str, Any]:
        users = self._user_records()
        user = users.get(user_key)
        if user is None:
            raise IdentityResolverError(
                ipc_errors.INVALID_ARGUMENT, f"no local user {user_key!r}"
            )
        try:
            row = self._overrides.set(
                platform=platform,
                union_id=union_id,
                user_key=user_key,
                confirmed_by=confirmed_by,
            )
        except OverrideConflictError as error:
            raise IdentityResolverError(
                ipc_errors.IDENTITY_CONFLICT, str(error)
            ) from error
        return self._binding(
            self._candidate_for_user(
                user,
                source="local-override",
                confirmed_by=row.confirmed_by,
                updated_at=row.updated_at_ms,
            ),
            union_id,
        )

    def clear_override(self, *, platform: str, union_id: str) -> bool:
        # Never an account-keyed set: that is cleared by {adapter, openId}.
        return self._overrides.clear(platform=platform, union_id=union_id)
