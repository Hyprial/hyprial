"""R1 identity resolver over Casdoor, overrides, and legacy observations."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from hyprial.identity import UserStore, identity_slug
from hyprial.kernel import ipc_errors, short_actor_name
from hyprial.daemon.impl.configuration.identity import read_settings_identity_metadata

from .bindings import OrgBindingCache, OrgBindingUnavailable, VerifiedOrgBinding
from .models import Candidate as _Candidate
from .models import LEGACY_SOURCES as _LEGACY_SOURCES
from .models import LegacyRow as _LegacyRow
from .models import SOURCES
from .mutation.errors import IdentityResolverError
from .mutation.overrides import OverrideWrites
from .store import GuardedOverrideStore, OverrideStore


class IdentityResolver(OverrideWrites):
    """Merge R1 sources without ever inventing an open-id authority."""

    def __init__(
        self,
        *,
        state_dir: Path,
        hyprial_home: Path,
        owner: str,
        users: UserStore | Any | None = None,
        legacy_path: Path | None = None,
        org_bindings: Callable[[], tuple[VerifiedOrgBinding, ...]] | None = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        self.hyprial_home = Path(hyprial_home)
        self.owner = owner
        self._users = users
        self._legacy_path = legacy_path or self.state_dir / "adapters.sqlite3"
        self._org_bindings_is_cache = org_bindings is None
        self._org_bindings = org_bindings or OrgBindingCache(self.state_dir).bindings
        self._overrides = GuardedOverrideStore(
            OverrideStore(self.state_dir / "identity.sqlite3"),
            lambda error: IdentityResolverError(
                ipc_errors.IDENTITY_SOURCE_UNAVAILABLE,
                f"local overrides source is unavailable: {type(error).__name__}",
            ),
        )

    @staticmethod
    def _required(value: object, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise IdentityResolverError(
                ipc_errors.INVALID_ARGUMENT, f"{name} must be a non-empty string"
            )
        return value.strip()

    @classmethod
    def _adapter(cls, value: str) -> str:
        adapter = cls._required(value, "adapter")
        if ":" in adapter:
            raise IdentityResolverError(
                ipc_errors.INVALID_ARGUMENT,
                "adapter must be the bare configured Lark adapter name",
            )
        return f"lark:{adapter}"

    @staticmethod
    def _bare_adapter(value: str) -> str:
        return value.removeprefix("lark:")

    def _owner_union_id(self) -> str | None:
        path = self.hyprial_home / "settings.json"
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as error:
            raise IdentityResolverError(
                ipc_errors.IDENTITY_SOURCE_UNAVAILABLE,
                f"local login identity claim is unavailable: {type(error).__name__}",
            ) from error
        if not isinstance(record, dict):
            raise IdentityResolverError(
                ipc_errors.IDENTITY_SOURCE_UNAVAILABLE,
                "local login identity claim is unavailable: invalid settings",
            )
        claims = record.get("identityClaims")
        properties = claims.get("properties") if isinstance(claims, dict) else None
        union_id = (
            properties.get("oauth_Lark_unionId")
            if isinstance(properties, dict)
            else None
        )
        return union_id.strip() if isinstance(union_id, str) and union_id.strip() else None

    def is_owner_union(self, union_id: str | None) -> bool:
        return bool(union_id) and self._owner_union_id() == union_id

    def _open_users(self, *, create: bool = False) -> tuple[Any | None, bool]:
        if self._users is not None:
            return self._users, False
        path = self.state_dir / "users.sqlite3"
        if not create and not path.is_file():
            return None, False
        try:
            return UserStore(path, hyprial_home=self.hyprial_home), True
        except Exception as error:  # noqa: BLE001 - source boundary
            raise IdentityResolverError(
                ipc_errors.IDENTITY_SOURCE_UNAVAILABLE,
                f"local users source is unavailable: {type(error).__name__}",
            ) from error

    def _user_records(self) -> dict[str, Any]:
        users, close = self._open_users()
        if users is None:
            return {}
        try:
            return {user.user_key: user for user in users.list_users()}
        except Exception as error:  # noqa: BLE001 - source boundary
            raise IdentityResolverError(
                ipc_errors.IDENTITY_SOURCE_UNAVAILABLE,
                f"local users source is unavailable: {type(error).__name__}",
            ) from error
        finally:
            if close:
                users.close()

    def _accounts(self) -> tuple[Any, ...]:
        users, close = self._open_users()
        if users is None:
            return ()
        try:
            return tuple(
                account
                for user in users.list_users()
                for account in users.accounts(user.user_key)
            )
        except Exception as error:  # noqa: BLE001 - source boundary
            raise IdentityResolverError(
                ipc_errors.IDENTITY_SOURCE_UNAVAILABLE,
                f"local users source is unavailable: {type(error).__name__}",
            ) from error
        finally:
            if close:
                users.close()

    def _legacy_rows(
        self,
        *,
        union_id: str | None = None,
        adapter: str | None = None,
        open_id: str | None = None,
    ) -> tuple[_LegacyRow, ...]:
        if not self._legacy_path.is_file():
            return ()
        try:
            escaped_path = quote(str(self._legacy_path), safe="/")
            database = sqlite3.connect(
                f"file:{escaped_path}?mode=ro", uri=True, timeout=5.0
            )
            database.row_factory = sqlite3.Row
            query = [
                "SELECT adapter, platform_id, union_id, hyprial_owner,"
                " standing, last_seen_ms, display_name, source"
                " FROM identities WHERE kind = 'user'"
            ]
            values: list[str] = []
            if union_id is not None:
                query.append("AND union_id = ?")
                values.append(union_id)
            if adapter is not None:
                query.append("AND adapter = ?")
                values.append(self._adapter(adapter))
            if open_id is not None:
                query.append("AND platform_id = ?")
                values.append(open_id)
            query.append("ORDER BY adapter, platform_id")
            try:
                rows = database.execute(" ".join(query), values).fetchall()
            finally:
                database.close()
        except (OSError, sqlite3.Error) as error:
            raise IdentityResolverError(
                ipc_errors.IDENTITY_SOURCE_UNAVAILABLE,
                f"legacy identity source is unavailable: {type(error).__name__}",
            ) from error
        return tuple(
            _LegacyRow(
                adapter=row["adapter"],
                open_id=row["platform_id"],
                union_id=row["union_id"],
                owner=row["hyprial_owner"],
                standing=row["standing"],
                observed_at_ms=row["last_seen_ms"],
                display_name=row["display_name"],
                source=row["source"],
            )
            for row in rows
        )

    @staticmethod
    def _user_for_owner(users: dict[str, Any], owner: str) -> Any | None:
        return next((user for user in users.values() if user.owner == owner), None)

    def _owner_candidate(
        self, union_id: str, users: dict[str, Any]
    ) -> _Candidate | None:
        if self._owner_union_id() != union_id:
            return None
        user = self._user_for_owner(users, self.owner)
        return _Candidate(
            user_key=user.user_key if user is not None else identity_slug(self.owner),
            kind=user.kind if user is not None else "member",
            owner=self.owner,
            source="casdoor-login",
            confirmed_by=None,
            updated_at=user.updated_at_ms if user is not None else None,
        )

    @staticmethod
    def _candidate_for_user(
        user: Any, *, source: str, confirmed_by: str | None, updated_at: int | None
    ) -> _Candidate:
        return _Candidate(
            user_key=user.user_key,
            kind=user.kind,
            owner=user.owner if user.kind == "member" else None,
            source=source,
            confirmed_by=confirmed_by,
            updated_at=updated_at,
        )

    def _candidates(self, union_id: str) -> tuple[_Candidate, ...]:
        users = self._user_records()
        candidates: list[_Candidate] = []
        owner = self._owner_candidate(union_id, users)
        if owner is not None:
            candidates.append(owner)
        candidates.extend(
            candidate
            for candidate, bound_union in self._org_candidates()
            if bound_union == union_id
        )
        for override in self._overrides.list(platform="lark"):
            user = users.get(override.user_key)
            if override.union_id == union_id and user is not None:
                candidates.append(
                    self._candidate_for_user(
                        user,
                        source="local-override",
                        confirmed_by=override.confirmed_by,
                        updated_at=override.updated_at_ms,
                    )
                )
        for keyed in self._overrides.list_accounts():
            user = users.get(keyed.user_key)
            if keyed.union_id == union_id and user is not None:
                candidates.append(
                    self._candidate_for_user(
                        user,
                        source="local-override",
                        confirmed_by=keyed.confirmed_by,
                        updated_at=keyed.updated_at_ms,
                    )
                )
        for account in self._accounts():
            user = users.get(account.user_key)
            if (
                account.union_id == union_id
                and user is not None
            ):
                source = (
                    "local-override"
                    if account.source in {"local-override", "override"}
                    else "legacy-user-bind"
                )
                candidates.append(
                    self._candidate_for_user(
                        user,
                        source=source,
                        confirmed_by=(
                            account.confirmed_by
                            if source in {"local-override", "legacy-user-bind"}
                            else None
                        ),
                        updated_at=account.confirmed_at_ms,
                    )
                )
        for row in self._legacy_rows(union_id=union_id):
            if row.standing != "verified" or row.owner is None:
                continue
            user = self._user_for_owner(users, row.owner)
            if user is None:
                candidates.append(
                    _Candidate(
                        user_key=identity_slug(row.owner),
                        kind="member",
                        owner=row.owner,
                        source="legacy-identities",
                        confirmed_by=None,
                        updated_at=row.observed_at_ms,
                    )
                )
            else:
                candidates.append(
                    self._candidate_for_user(
                        user,
                        source="legacy-identities",
                        confirmed_by=None,
                        updated_at=row.observed_at_ms,
                    )
                )
        return tuple(candidates)

    @staticmethod
    def _row_account_source(row: _LegacyRow) -> str:
        """How we know an identities-table account (§5 ``accounts[].source``).

        Rows the adapter learned from its platform (inbound events, chat
        members, bot info) are ``observed``; a ``manual:`` row was asserted
        by a human and is ``override`` like a user-bind account.
        """

        return "override" if str(row.source).startswith("manual") else "observed"

    def accounts_for_union(self, union_id: str) -> list[dict[str, Any]]:
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for row in self._legacy_rows(union_id=union_id):
            adapter = self._bare_adapter(row.adapter)
            unique[(adapter, row.open_id)] = {
                "adapter": adapter,
                "openId": row.open_id,
                "observedAtMs": row.observed_at_ms,
                "source": self._row_account_source(row),
            }
        for account in self._accounts():
            if account.union_id != union_id:
                continue
            adapter = self._bare_adapter(account.adapter)
            known = unique.get((adapter, account.open_id))
            if known is not None and known["source"] == "observed":
                continue  # asserted and observed: the observation is stronger
            unique[(adapter, account.open_id)] = {
                "adapter": adapter,
                "openId": account.open_id,
                "observedAtMs": account.confirmed_at_ms,
                "source": "override",
            }
        return [unique[key] for key in sorted(unique)]

    def _union_keyed_accounts(self) -> set[tuple[str, str]]:
        """Accounts a later set {adapter, openId} keyed to a known unionId."""

        return {(row.adapter, row.open_id) for row in self._overrides.list_accounts()}

    def _override_accounts_for_user(self, user_key: str) -> list[dict[str, Any]]:
        accounts: list[dict[str, Any]] = []
        keyed = self._union_keyed_accounts()
        for account in self._accounts():
            # An account carrying a unionId belongs to that union's row (§5),
            # including one whose union was only learned at a later set.
            if (
                account.user_key != user_key
                or account.source != "override"
                or account.union_id is not None
                or (account.adapter, account.open_id) in keyed
            ):
                continue
            adapter = self._bare_adapter(account.adapter)
            observed = any(
                self._row_account_source(row) == "observed"
                for row in self._legacy_rows(adapter=adapter, open_id=account.open_id)
            )
            accounts.append(
                {
                    "adapter": adapter,
                    "openId": account.open_id,
                    "observedAtMs": account.confirmed_at_ms,
                    "source": "observed" if observed else "override",
                }
            )
        return accounts

    def _assert_account_owner(
        self, *, adapter: str, open_id: str, user_key: str
    ) -> None:
        namespaced = self._adapter(adapter)
        for account in self._accounts():
            if (
                account.source == "override"
                and account.adapter == namespaced
                and account.open_id == open_id
                and account.user_key != user_key
            ):
                raise IdentityResolverError(
                    ipc_errors.IDENTITY_CONFLICT,
                    f"{adapter} {open_id!r} is overridden to a different user",
                )

    def _legacy_exact_sender(
        self, *, adapter: str, open_id: str, union_id: str
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Grandfather one exact human assertion without authorising overrides."""

        users = self._user_records()
        namespaced = self._adapter(adapter)
        for account in self._accounts():
            if (
                account.adapter != namespaced
                or account.open_id != open_id
                or account.union_id is not None
                or account.source == "override"
            ):
                continue
            user = users.get(account.user_key)
            if user is None:
                continue
            return (
                {
                    "kind": "user",
                    "platformId": open_id,
                    "unionId": union_id,
                    "displayName": user.shown_name,
                    "owner": user.owner if user.kind == "member" else None,
                    "standing": "verified",
                    "source": "legacy-user-bind",
                    "userKey": user.user_key,
                    "userKind": user.kind,
                    "nickname": user.nickname,
                    "realName": user.real_name,
                },
                user.user_key,
            )
        rows = self._legacy_rows(adapter=adapter, open_id=open_id)
        verified = [row for row in rows if row.standing == "verified"]
        if verified:
            row = verified[0]
            user = self._user_for_owner(users, row.owner) if row.owner else None
            user_key = (
                user.user_key
                if user is not None
                else identity_slug(row.owner)
                if row.owner is not None
                else None
            )
            result: dict[str, Any] = {
                "kind": "user",
                "platformId": open_id,
                "unionId": union_id,
                "displayName": (
                    row.display_name
                    or (user.shown_name if user is not None else None)
                    or row.owner
                ),
                "owner": row.owner,
                "standing": "verified",
                "source": "legacy-identities",
            }
            if user_key is not None:
                result.update(
                    userKey=user_key,
                    userKind=user.kind if user is not None else "member",
                    nickname=user.nickname if user is not None else None,
                    realName=user.real_name if user is not None else None,
                )
            return result, user_key
        observed = next((row for row in rows if row.display_name), None)
        if observed is not None:
            return (
                {
                    "kind": "user",
                    "platformId": open_id,
                    "unionId": union_id,
                    "displayName": observed.display_name,
                    "owner": None,
                    "standing": "observed",
                    "source": observed.source,
                },
                None,
            )
        return None, None

    def _resolved(self, union_id: str) -> tuple[_Candidate, list[dict[str, Any]]]:
        candidates = self._candidates(union_id)
        users = sorted({candidate.user_key for candidate in candidates})
        if len(users) > 1:
            raise IdentityResolverError(
                ipc_errors.IDENTITY_CONFLICT,
                f"unionId {union_id!r} is bound to conflicting users",
            )
        if not candidates:
            raise IdentityResolverError(
                ipc_errors.IDENTITY_UNBOUND,
                f"unionId {union_id!r} is not bound",
            )
        priority = {
            "casdoor-login": 0,
            "org-directory": 1,
            "legacy-identities": 2,
            "legacy-user-bind": 2,
            "local-override": 3,
        }
        winner = min(candidates, key=lambda item: priority[item.source])
        return winner, self.accounts_for_union(union_id)

    def resolve(
        self,
        *,
        platform: str | None = None,
        union_id: str | None = None,
        adapter: str | None = None,
        open_id: str | None = None,
    ) -> dict[str, Any]:
        if platform is not None or union_id is not None:
            if platform != "lark":
                raise IdentityResolverError(
                    ipc_errors.INVALID_ARGUMENT, "platform must be lark"
                )
            union = self._required(union_id, "unionId")
        else:
            adapter_name = self._required(adapter, "adapter")
            self._adapter(adapter_name)
            account_id = self._required(open_id, "openId")
            users = self._user_records()
            rows = self._legacy_rows(adapter=adapter_name, open_id=account_id)
            unions = sorted({row.union_id for row in rows if row.union_id})
            account_only = next(
                (
                    account
                    for account in self._accounts()
                    if account.adapter == self._adapter(adapter_name)
                    and account.open_id == account_id
                    and account.union_id is None
                    and account.source == "override"
                ),
                None,
            )
            if account_only is not None and not unions:
                user = users.get(account_only.user_key)
                if user is not None:
                    result = self._binding(
                        self._candidate_for_user(
                            user,
                            source="local-override",
                            confirmed_by=account_only.confirmed_by,
                            updated_at=account_only.confirmed_at_ms,
                        ),
                        None,
                        outbound_only=True,
                    )
                    result.update(verified=False)
                    return result
            if not unions:
                raise IdentityResolverError(
                    ipc_errors.IDENTITY_UNBOUND,
                    f"{adapter_name} {account_id!r} is not bound",
                )
            if len(unions) > 1:
                raise IdentityResolverError(
                    ipc_errors.IDENTITY_CONFLICT,
                    f"{adapter_name} {account_id!r} has conflicting union ids",
                )
            union = unions[0]
        winner, accounts = self._resolved(union)
        if adapter is not None and open_id is not None:
            self._assert_account_owner(
                adapter=adapter, open_id=open_id, user_key=winner.user_key
            )
        result: dict[str, Any] = {
            "user": winner.user_key,
            "kind": winner.kind,
            "source": winner.source,
            "verified": True,
            "accounts": accounts,
        }
        if winner.confirmed_by is not None:
            result["confirmedBy"] = winner.confirmed_by
        return result

    def resolve_sender(
        self, union_id: str, *, open_id: str, adapter: str | None = None
    ) -> dict[str, Any]:
        """Project a resolved user onto the established inbound sender block."""

        union = self._required(union_id, "unionId")
        legacy, legacy_user = (
            self._legacy_exact_sender(
                adapter=adapter, open_id=open_id, union_id=union
            )
            if adapter is not None
            else (None, None)
        )
        try:
            winner, _accounts = self._resolved(union)
        except IdentityResolverError as error:
            if error.code != ipc_errors.IDENTITY_UNBOUND:
                raise
            winner = None
        if legacy is not None and legacy["standing"] == "verified":
            if (
                winner is not None
                and legacy_user is not None
                and legacy_user != winner.user_key
            ):
                raise IdentityResolverError(
                    ipc_errors.IDENTITY_CONFLICT,
                    f"{adapter} {open_id!r} conflicts with unionId {union!r}",
                )
            if winner is None or winner.source in _LEGACY_SOURCES:
                return legacy
        if winner is None:
            if legacy is not None:
                return legacy
            raise IdentityResolverError(
                ipc_errors.IDENTITY_UNBOUND, f"unionId {union!r} is not bound"
            )
        if adapter is not None:
            self._assert_account_owner(
                adapter=adapter, open_id=open_id, user_key=winner.user_key
            )
        user = self._user_records().get(winner.user_key)
        return {
            "kind": "user",
            "platformId": open_id,
            "unionId": union,
            "displayName": user.shown_name if user is not None else winner.owner,
            "owner": winner.owner,
            "standing": "verified",
            "source": winner.source,
            "userKey": winner.user_key,
            "userKind": winner.kind,
            "nickname": user.nickname if user is not None else None,
            "realName": user.real_name if user is not None else None,
        }

    def _binding(
        self,
        candidate: _Candidate,
        union_id: str | None,
        *,
        outbound_only: bool = False,
    ) -> dict[str, Any]:
        row: dict[str, Any] = {
            "user": candidate.user_key,
            "kind": candidate.kind,
            "platform": "lark",
            "unionId": union_id,
            "source": candidate.source,
            "updatedAt": candidate.updated_at,
            "accounts": (
                self.accounts_for_union(union_id)
                if union_id is not None
                else self._override_accounts_for_user(candidate.user_key)
            ),
        }
        if candidate.confirmed_by is not None:
            row["confirmedBy"] = candidate.confirmed_by
        if outbound_only:
            # An account-only override: a local-override row that only feeds
            # outbound delivery (§5); never inbound authority.
            row["outboundOnly"] = True
        return row

    def _org_candidates(self) -> list[tuple[_Candidate, str]]:
        """Verified org-directory rows from the proof-free cache (§2.2)."""
        if self._org_bindings_is_cache:
            mode = read_settings_identity_metadata(hyprial_home=self.hyprial_home)[0]
            if mode != "casdoor":
                return []
        try:
            org_bindings = self._org_bindings()
        except OrgBindingUnavailable as error:
            raise IdentityResolverError(
                ipc_errors.IDENTITY_SOURCE_UNAVAILABLE, str(error)
            ) from error
        return [
            (
                _Candidate(
                    binding.user_key, "member", binding.user, "org-directory",
                    None, binding.issued_at_ms,
                ),
                binding.union_id,
            )
            for binding in org_bindings
        ]

    def bindings(
        self, *, platform: str | None = None, source: str | None = None
    ) -> list[dict[str, Any]]:
        if platform not in (None, "lark"):
            return []
        if source is not None and source not in SOURCES:
            raise IdentityResolverError(
                ipc_errors.INVALID_ARGUMENT, "source is not a frozen identity source"
            )
        users = self._user_records()
        rows: list[dict[str, Any]] = []
        owner_union = self._owner_union_id()
        if owner_union is not None:
            candidate = self._owner_candidate(owner_union, users)
            assert candidate is not None
            rows.append(self._binding(candidate, owner_union))
        for candidate, bound_union in self._org_candidates():
            rows.append(self._binding(candidate, bound_union))
        for override in self._overrides.list(platform="lark"):
            user = users.get(override.user_key)
            if user is not None:
                rows.append(
                    self._binding(
                        self._candidate_for_user(
                            user,
                            source="local-override",
                            confirmed_by=override.confirmed_by,
                            updated_at=override.updated_at_ms,
                        ),
                        override.union_id,
                    )
                )
        keyed = self._union_keyed_accounts()
        for keyed_row in self._overrides.list_accounts():
            if (user := users.get(keyed_row.user_key)) is not None:
                rows.append(
                    self._binding(
                        self._candidate_for_user(
                            user,
                            source="local-override",
                            confirmed_by=keyed_row.confirmed_by,
                            updated_at=keyed_row.updated_at_ms,
                        ),
                        keyed_row.union_id,
                    )
                )
        for account in self._accounts():
            user = users.get(account.user_key)
            if (
                account.source == "override"
                and account.union_id is None
                and user
                and (account.adapter, account.open_id) not in keyed
            ):
                rows.append(
                    self._binding(
                        self._candidate_for_user(
                            user,
                            source="local-override",
                            confirmed_by=account.confirmed_by,
                            updated_at=account.confirmed_at_ms,
                        ),
                        None,
                        outbound_only=True,
                    )
                )
            elif account.union_id and user:
                account_source = (
                    "local-override"
                    if account.source in {"local-override", "override"}
                    else "legacy-user-bind"
                )
                rows.append(
                    self._binding(
                        self._candidate_for_user(
                            user,
                            source=account_source,
                            confirmed_by=(
                                account.confirmed_by
                                if account_source in {"local-override", "legacy-user-bind"}
                                else None
                            ),
                            updated_at=account.confirmed_at_ms,
                        ),
                        account.union_id,
                    )
                )
        for legacy in self._legacy_rows():
            if legacy.standing != "verified" or not legacy.union_id or not legacy.owner:
                continue
            user = self._user_for_owner(users, legacy.owner)
            candidate = (
                self._candidate_for_user(
                    user,
                    source="legacy-identities",
                    confirmed_by=None,
                    updated_at=legacy.observed_at_ms,
                )
                if user is not None
                else _Candidate(
                    identity_slug(legacy.owner),
                    "member",
                    legacy.owner,
                    "legacy-identities",
                    None,
                    legacy.observed_at_ms,
                )
            )
            rows.append(self._binding(candidate, legacy.union_id))
        filtered = [row for row in rows if source is None or row["source"] == source]
        unique = {
            (row["source"], row["platform"], row["unionId"], row["user"]): row
            for row in filtered
        }
        # Account-only rows have no unionId: order None before any string
        # instead of comparing None with str (TypeError).
        return [
            unique[key]
            for key in sorted(
                unique,
                key=lambda key: (key[0], key[1], key[2] is not None, key[2] or "", key[3]),
            )
        ]

    def list_users(
        self, *, kind: str | None = None, user_key: str | None = None
    ) -> list[dict[str, Any]]:
        records = self._user_records().values()
        return [
            user.to_json()
            for user in records
            if (kind is None or user.kind == kind)
            and (user_key is None or user.user_key == user_key)
        ]

    def add_user(self, params: dict[str, Any]) -> dict[str, Any]:
        users, close = self._open_users(create=True)
        assert users is not None
        try:
            user = users.add_user(
                kind=self._required(params.get("kind"), "kind"),
                owner=params.get("owner"),
                nickname=params.get("nickname"),
                real_name=params.get("realName"),
                display_name=params.get("displayName"),
                confirmed_by=self._required(params.get("confirmedBy"), "confirmedBy"),
            )
            return user.to_json()
        finally:
            if close:
                users.close()

    @staticmethod
    def _legacy_profile_open_id(profile: Any, adapter: str) -> str | None:
        binding = getattr(profile, "owner_open_id", None)
        if binding is None or binding.channel != adapter:
            return None
        return binding.open_id

    @staticmethod
    def _one_open_id(values: set[str], *, adapter: str) -> str | None:
        if len(values) > 1:
            raise IdentityResolverError(
                ipc_errors.IDENTITY_CONFLICT,
                f"more than one openId is bound for adapter {adapter!r}",
            )
        return next(iter(values), None)

    def owner_open_id(self, owner_or_user_key: str, adapter: str) -> str | None:
        """Resolve one user's outbound open-id in exactly one Lark app.

        Unlike :meth:`profile_open_id`, this sender-side lookup needs no
        receiver ``UserProfile``.  The target may live on another node; the
        local identity graph (override, union binding, then grandfathered
        observation) is enough.  Adapter matching stays app-qualified so an
        open-id observed by another bot can never be borrowed.
        """

        identity = self._required(owner_or_user_key, "owner/user key")
        users = self._user_records()
        user = users.get(identity) or self._user_for_owner(users, identity)
        user_key = user.user_key if user is not None else identity_slug(identity)
        owner = user.owner if user is not None else identity
        suffix = short_actor_name(adapter)
        adapters = {adapter, f"lark:{suffix}", suffix}

        override_ids = {
            account.open_id
            for account in self._accounts()
            if account.user_key == user_key
            and account.source == "override"
            and account.adapter in adapters
        }
        selected = self._one_open_id(override_ids, adapter=suffix)
        if selected is not None:
            return selected

        bound_ids: set[str] = set()
        for row in self.bindings(platform="lark"):
            if row["user"] != user_key or row["unionId"] is None:
                continue
            for account in row["accounts"]:
                if account["adapter"] in adapters:
                    self._resolved(row["unionId"])
                    bound_ids.add(account["openId"])
        selected = self._one_open_id(bound_ids, adapter=suffix)
        if selected is not None:
            return selected

        legacy_ids = {
            row.open_id
            for row in self._legacy_rows(adapter=suffix)
            if row.standing == "verified"
            and row.owner is not None
            and (row.owner == owner or identity_slug(row.owner) == user_key)
        }
        return self._one_open_id(legacy_ids, adapter=suffix)

    def profile_open_id(self, profile: Any, adapter: str) -> str | None:
        """Outbound observed account, with read-only users.json continuity.

        R1 keeps open-id delivery until the union-id send E2E passes. The
        resolver therefore owns the legacy profile read as a compatibility
        account source; callers no longer interpret ``owner_open_id``.
        """

        user = self._user_for_owner(self._user_records(), profile.owner)
        user_key = user.user_key if user is not None else identity_slug(profile.owner)
        suffix = short_actor_name(adapter)
        adapters = {adapter, f"lark:{suffix}", suffix}
        for account in self._accounts():
            if (
                account.user_key == user_key
                and account.source == "override"
                and account.adapter in adapters
            ):
                return account.open_id
        for row in self.bindings(platform="lark"):
            if row["user"] != user_key:
                continue
            for account in row["accounts"]:
                if account["adapter"] in adapters:
                    if row["unionId"] is not None:
                        self._resolved(row["unionId"])
                    return account["openId"]
        return self._legacy_profile_open_id(profile, adapter)
