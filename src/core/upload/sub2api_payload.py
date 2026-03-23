"""
Sub2API template config, name planning, and payload builders.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import logging
from typing import Any, Dict, Iterable, List, Optional

from ...database import crud
from ...database.models import Account, Sub2ApiService
from .sub2api_naming import (
    build_sub2api_dynamic_name,
    discover_sub2api_identity_occupied_name_indices,
    next_available_index,
    normalize_sub2api_identity,
    reserve_smallest_available_indices,
    resolve_sub2api_group_naming_identity,
)

logger = logging.getLogger(__name__)

DEFAULT_MODEL_MAPPING = {
    "gpt-5-codex": "gpt-5-codex",
    "gpt-5.1": "gpt-5.1",
    "gpt-5.1-codex": "gpt-5.1-codex",
    "gpt-5.1-codex-max": "gpt-5.1-codex-max",
    "gpt-5.1-codex-mini": "gpt-5.1-codex-mini",
    "gpt-5.2": "gpt-5.2",
    "gpt-5.2-codex": "gpt-5.2-codex",
    "gpt-5.3": "gpt-5.3",
    "gpt-5.3-codex": "gpt-5.3-codex",
    "gpt-5.4": "gpt-5.4",
}

DEFAULT_TEMPLATE_CONFIG = {
    "name_prefix": "GPT-",
    "name_digits": 9,
    "default_concurrency": 1,
    "default_priority": 50,
    "default_rate_multiplier": 1,
    "auto_pause_on_expired": True,
    "default_group_ids": [],
}


def normalize_sub2api_template_config(template_config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    config = deepcopy(DEFAULT_TEMPLATE_CONFIG)
    if template_config:
        for key, value in template_config.items():
            if key in config and value is not None:
                config[key] = value

    config["name_prefix"] = str(config["name_prefix"] or DEFAULT_TEMPLATE_CONFIG["name_prefix"])
    config["name_digits"] = max(1, int(config["name_digits"]))
    config["default_concurrency"] = max(1, int(config["default_concurrency"]))
    config["default_priority"] = int(config["default_priority"])
    config["default_rate_multiplier"] = float(config["default_rate_multiplier"])
    config["auto_pause_on_expired"] = bool(config["auto_pause_on_expired"])
    group_ids: List[int] = []
    for value in config.get("default_group_ids") or []:
        try:
            group_id = int(value)
        except (TypeError, ValueError):
            continue
        if group_id > 0 and group_id not in group_ids:
            group_ids.append(group_id)
    config["default_group_ids"] = group_ids
    return config


def resolve_sub2api_service(db, service_id: Optional[int] = None) -> Optional[Sub2ApiService]:
    if service_id:
        return crud.get_sub2api_service_by_id(db, service_id)
    services = crud.get_sub2api_services(db, enabled=True)
    return services[0] if services else None


def _resolve_primary_group_id(service: Optional[Sub2ApiService]) -> Optional[int]:
    if not service:
        return None
    default_group_ids = normalize_sub2api_template_config(service.template_config).get("default_group_ids") or []
    if not default_group_ids:
        return None
    try:
        return int(default_group_ids[0])
    except (TypeError, ValueError):
        return None


def reserve_sub2api_named_indices(
    db,
    service: Optional[Sub2ApiService],
    identity_counts: Dict[str, int],
    *,
    group_id: Optional[int] = None,
) -> Dict[str, List[int]]:
    normalized_counts = {
        normalize_sub2api_identity(identity): max(0, int(count or 0))
        for identity, count in (identity_counts or {}).items()
        if int(count or 0) > 0
    }
    if not normalized_counts:
        return {}

    if not service:
        return {
            identity: list(range(1, count + 1))
            for identity, count in normalized_counts.items()
        }

    template_config = normalize_sub2api_template_config(service.template_config)
    digits = int(template_config["name_digits"])
    fallback_start = max(1, int(service.next_name_index or 1))
    reserved: Dict[str, List[int]] = {}
    max_reserved_index = fallback_start - 1

    use_remote = bool(group_id and service.api_url and service.api_key)
    if use_remote:
        try:
            for identity, count in normalized_counts.items():
                occupied_indices = discover_sub2api_identity_occupied_name_indices(
                    service.api_url,
                    service.api_key,
                    int(group_id),
                    identity,
                    digits,
                )
                indices = reserve_smallest_available_indices(occupied_indices, count)
                reserved[identity] = indices
                combined = set(occupied_indices).union(indices)
                max_reserved_index = max(max_reserved_index, next_available_index(combined) - 1)
            logger.info("Reserved smart Sub2API name indices %s from group %s", reserved, group_id)
        except Exception as exc:
            logger.warning(
                "Failed to inspect Sub2API group %s for dynamic naming, falling back to local counter: %s",
                group_id,
                exc,
            )
            reserved.clear()

    if not reserved:
        for identity, count in normalized_counts.items():
            indices = list(range(fallback_start, fallback_start + count))
            reserved[identity] = indices
            if indices:
                max_reserved_index = max(max_reserved_index, indices[-1])

    service.next_name_index = max(int(service.next_name_index or 1), max_reserved_index + 1)
    service.template_config = template_config
    db.commit()
    db.refresh(service)
    return reserved


def reserve_sub2api_name_indices(db, service: Optional[Sub2ApiService], count: int) -> List[int]:
    reserved = reserve_sub2api_named_indices(
        db,
        service,
        {"Free": count},
        group_id=_resolve_primary_group_id(service),
    )
    return reserved.get("Free", [])


def current_exported_at() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def format_sub2api_name(template_config: Dict[str, Any], index: int, identity: Optional[str] = None) -> str:
    digits = int((template_config or {}).get("name_digits") or DEFAULT_TEMPLATE_CONFIG["name_digits"])
    return build_sub2api_dynamic_name(identity or "Free", index, digits)


def build_sub2api_named_accounts(
    accounts: Iterable[Account],
    *,
    template_config: Dict[str, Any],
    indices_by_identity: Dict[str, List[int]],
    group_name: Optional[str] = None,
) -> List[Dict[str, Any]]:
    cursors = {
        normalize_sub2api_identity(identity): list(values)
        for identity, values in (indices_by_identity or {}).items()
    }
    entries: List[Dict[str, Any]] = []
    for account in list(accounts):
        account_identity = normalize_sub2api_identity(account.subscription_type)
        naming_identity = resolve_sub2api_group_naming_identity(group_name, account_identity)
        bucket = cursors.get(naming_identity) or []
        if not bucket:
            raise ValueError(f"Sub2API naming bucket exhausted for identity {naming_identity}")
        name_index = bucket.pop(0)
        cursors[naming_identity] = bucket
        entries.append(
            {
                "account": account,
                "account_identity": account_identity,
                "naming_identity": naming_identity,
                "name_index": name_index,
                "generated_name": format_sub2api_name(template_config, name_index, naming_identity),
                "group_name": group_name,
            }
        )
    return entries


def build_sub2api_account_item(
    account: Account,
    *,
    template_config: Dict[str, Any],
    name_index: Optional[int] = None,
    generated_name: Optional[str] = None,
    naming_identity: Optional[str] = None,
    concurrency_override: Optional[int] = None,
    priority_override: Optional[int] = None,
    include_export_extras: bool = False,
) -> Dict[str, Any]:
    expires_at_ts = int(account.expires_at.timestamp()) if account.expires_at else 0
    credentials: Dict[str, Any] = {
        "access_token": account.access_token or "",
        "chatgpt_account_id": account.account_id or "",
        "chatgpt_user_id": "",
        "client_id": account.client_id or "",
        "expires_at": expires_at_ts,
        "expires_in": 863999,
        "model_mapping": deepcopy(DEFAULT_MODEL_MAPPING),
        "organization_id": account.workspace_id or "",
        "refresh_token": account.refresh_token or "",
    }
    if include_export_extras:
        credentials["email"] = account.email
        if account.id_token:
            credentials["id_token"] = account.id_token
        credentials["plan_type"] = account.subscription_type or "free"

    return {
        "name": generated_name or format_sub2api_name(template_config, int(name_index or 1), naming_identity),
        "notes": (account.remark or "").strip() or account.email,
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "extra": {},
        "concurrency": int(concurrency_override or template_config["default_concurrency"]),
        "priority": int(priority_override if priority_override is not None else template_config["default_priority"]),
        "rate_multiplier": template_config["default_rate_multiplier"],
        "auto_pause_on_expired": template_config["auto_pause_on_expired"],
    }


def build_sub2api_export_payload(
    accounts: Iterable[Account],
    *,
    name_indices: Optional[List[int]] = None,
    named_accounts: Optional[List[Dict[str, Any]]] = None,
    template_config: Dict[str, Any],
) -> Dict[str, Any]:
    if named_accounts is not None:
        items = [
            build_sub2api_account_item(
                entry["account"],
                template_config=template_config,
                name_index=entry.get("name_index"),
                generated_name=entry.get("generated_name"),
                naming_identity=entry.get("naming_identity"),
                include_export_extras=True,
            )
            for entry in named_accounts
        ]
    else:
        items = [
            build_sub2api_account_item(
                account,
                template_config=template_config,
                name_index=index,
                include_export_extras=True,
            )
            for account, index in zip(list(accounts), name_indices or [])
        ]
    return {
        "exported_at": current_exported_at(),
        "proxies": [],
        "accounts": items,
    }


def build_sub2api_upload_payload(
    accounts: Iterable[Account],
    *,
    name_indices: Optional[List[int]] = None,
    named_accounts: Optional[List[Dict[str, Any]]] = None,
    template_config: Dict[str, Any],
    concurrency_override: Optional[int] = None,
    priority_override: Optional[int] = None,
) -> Dict[str, Any]:
    if named_accounts is not None:
        items = [
            build_sub2api_account_item(
                entry["account"],
                template_config=template_config,
                name_index=entry.get("name_index"),
                generated_name=entry.get("generated_name"),
                naming_identity=entry.get("naming_identity"),
                concurrency_override=concurrency_override,
                priority_override=priority_override,
            )
            for entry in named_accounts
        ]
    else:
        items = [
            build_sub2api_account_item(
                account,
                template_config=template_config,
                name_index=index,
                concurrency_override=concurrency_override,
                priority_override=priority_override,
            )
            for account, index in zip(list(accounts), name_indices or [])
        ]
    return {
        "data": {
            "type": "sub2api-data",
            "version": 1,
            "exported_at": current_exported_at(),
            "proxies": [],
            "accounts": items,
        },
        "skip_default_group_bind": True,
    }
