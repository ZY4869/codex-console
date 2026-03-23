from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from ...config.settings import get_settings
from ...database.models import Account, EmailService as EmailServiceModel
from ...services import EmailServiceFactory, EmailServiceType

LogCallback = Optional[Callable[[str], None]]

ADDRESS_SELECTION_TYPES = {
    EmailServiceType.MOE_MAIL,
    EmailServiceType.TEMP_MAIL,
    EmailServiceType.FREEMAIL,
    EmailServiceType.OUTLOOK,
}

DOMAIN_SELECTION_TYPES = {
    EmailServiceType.MOE_MAIL,
    EmailServiceType.TEMP_MAIL,
    EmailServiceType.DUCK_MAIL,
    EmailServiceType.FREEMAIL,
}

ENUMERATION_LIMIT = 200


@dataclass
class RegistrationSelectionRequest:
    random_email_service: bool = False
    random_outlook_account: bool = False
    random_domain: bool = False
    selected_email_addresses: Sequence[str] = field(default_factory=list)
    selected_domains: Sequence[str] = field(default_factory=list)
    selection_index: int = 0


@dataclass
class ResolvedEmailService:
    service_type: EmailServiceType
    config: Dict[str, Any]
    email_service_id: Optional[int]
    service_name: str


def normalize_email_service_config(
    service_type: EmailServiceType,
    config: Optional[dict],
    proxy_url: Optional[str] = None,
) -> dict:
    normalized = config.copy() if config else {}

    if "api_url" in normalized and "base_url" not in normalized:
        normalized["base_url"] = normalized.pop("api_url")

    if service_type == EmailServiceType.MOE_MAIL:
        if "domain" in normalized and "default_domain" not in normalized:
            normalized["default_domain"] = normalized.pop("domain")
    elif service_type in (EmailServiceType.TEMP_MAIL, EmailServiceType.FREEMAIL):
        if "default_domain" in normalized and "domain" not in normalized:
            normalized["domain"] = normalized.pop("default_domain")
    elif service_type == EmailServiceType.DUCK_MAIL:
        if "domain" in normalized and "default_domain" not in normalized:
            normalized["default_domain"] = normalized.pop("domain")

    if proxy_url and "proxy_url" not in normalized:
        normalized["proxy_url"] = proxy_url

    return normalized


def build_service_options(
    db,
    service_type: EmailServiceType,
    service_id: Optional[int] = None,
    proxy_url: Optional[str] = None,
) -> Dict[str, Any]:
    notes: List[str] = []

    if service_type == EmailServiceType.TEMPMAIL:
        return {
            "service_type": service_type.value,
            "service_id": None,
            "service_name": "Tempmail.lol",
            "supports_random_service": False,
            "supports_random_domain": False,
            "supports_address_selection": False,
            "supports_random_outlook_account": False,
            "domains": [],
            "email_addresses": [],
            "notes": notes,
        }

    if service_type == EmailServiceType.OUTLOOK:
        accounts = _build_outlook_account_options(db)
        domains = sorted({item["email"].split("@", 1)[1] for item in accounts if "@" in item["email"]})
        return {
            "service_type": service_type.value,
            "service_id": service_id,
            "service_name": "Outlook",
            "supports_random_service": False,
            "supports_random_domain": False,
            "supports_address_selection": True,
            "supports_random_outlook_account": True,
            "domains": domains,
            "email_addresses": accounts,
            "notes": notes,
        }

    selected_service = _find_options_service(db, service_type, service_id)
    if not selected_service:
        if service_type == EmailServiceType.MOE_MAIL:
            settings = get_settings()
            if settings.custom_domain_base_url and settings.custom_domain_api_key:
                config = normalize_email_service_config(
                    service_type,
                    {
                        "base_url": settings.custom_domain_base_url,
                        "api_key": settings.custom_domain_api_key.get_secret_value() if settings.custom_domain_api_key else "",
                    },
                    proxy_url,
                )
                try:
                    service = EmailServiceFactory.create(service_type, config, name="默认自定义域名服务")
                    domains = _dedupe_strings(service.list_domains())
                    email_addresses = _normalize_email_options(service.list_emails())
                    return {
                        "service_type": service_type.value,
                        "service_id": None,
                        "service_name": "默认自定义域名服务",
                        "supports_random_service": False,
                        "supports_random_domain": len(domains) > 1,
                        "supports_address_selection": len(email_addresses) > 0,
                        "supports_random_outlook_account": False,
                        "domains": domains,
                        "email_addresses": email_addresses,
                        "notes": notes,
                    }
                except Exception as exc:
                    notes.append(f"读取默认自定义域名服务失败: {exc}")

        return {
            "service_type": service_type.value,
            "service_id": service_id,
            "service_name": service_type.value,
            "supports_random_service": False,
            "supports_random_domain": service_type in DOMAIN_SELECTION_TYPES,
            "supports_address_selection": service_type in ADDRESS_SELECTION_TYPES,
            "supports_random_outlook_account": False,
            "domains": [],
            "email_addresses": [],
            "notes": ["未找到可用的邮箱服务。"],
        }

    actual_type = EmailServiceType(selected_service.service_type)
    config = normalize_email_service_config(actual_type, selected_service.config, proxy_url)
    domains: List[str] = []
    email_addresses: List[Dict[str, Any]] = []

    try:
        service = EmailServiceFactory.create(actual_type, config, name=selected_service.name)
        if actual_type in DOMAIN_SELECTION_TYPES:
            domains = _dedupe_strings(service.list_domains())
        if actual_type in ADDRESS_SELECTION_TYPES:
            email_addresses = _normalize_email_options(service.list_emails())
    except Exception as exc:
        notes.append(f"读取服务能力失败: {exc}")

    return {
        "service_type": actual_type.value,
        "service_id": selected_service.id,
        "service_name": selected_service.name,
        "supports_random_service": _supports_random_service(db, actual_type),
        "supports_random_domain": actual_type in DOMAIN_SELECTION_TYPES and len(domains) > 1,
        "supports_address_selection": actual_type in ADDRESS_SELECTION_TYPES and len(email_addresses) > 0,
        "supports_random_outlook_account": False,
        "domains": domains,
        "email_addresses": email_addresses,
        "notes": notes,
    }


def resolve_email_service_for_registration(
    db,
    service_type: EmailServiceType,
    requested_service_id: Optional[int],
    fallback_config: Optional[Dict[str, Any]],
    proxy_url: Optional[str],
    selection: RegistrationSelectionRequest,
    log_callback: LogCallback = None,
) -> ResolvedEmailService:
    if service_type == EmailServiceType.TEMPMAIL:
        settings = get_settings()
        config = {
            "base_url": settings.tempmail_base_url,
            "timeout": settings.tempmail_timeout,
            "max_retries": settings.tempmail_max_retries,
            "proxy_url": proxy_url,
        }
        if fallback_config:
            config.update(normalize_email_service_config(service_type, fallback_config, proxy_url))
        _log(log_callback, "使用 Tempmail.lol 默认服务")
        return ResolvedEmailService(
            service_type=service_type,
            config=config,
            email_service_id=None,
            service_name="Tempmail.lol",
        )

    if service_type == EmailServiceType.OUTLOOK:
        return _resolve_outlook_service(
            db=db,
            requested_service_id=requested_service_id,
            proxy_url=proxy_url,
            selection=selection,
            log_callback=log_callback,
        )

    db_service = _select_database_service(
        db=db,
        service_type=service_type,
        requested_service_id=requested_service_id,
        random_email_service=selection.random_email_service,
    )
    if db_service:
        actual_type = EmailServiceType(db_service.service_type)
        config = normalize_email_service_config(actual_type, db_service.config, proxy_url)
        _log(log_callback, f"使用邮箱服务: {db_service.name} (ID: {db_service.id}, 类型: {actual_type.value})")
        config = _apply_domain_and_address_selection(
            service_type=actual_type,
            service_name=db_service.name,
            config=config,
            selection=selection,
            log_callback=log_callback,
        )
        return ResolvedEmailService(
            service_type=actual_type,
            config=config,
            email_service_id=db_service.id,
            service_name=db_service.name,
        )

    if service_type == EmailServiceType.MOE_MAIL:
        settings = get_settings()
        if settings.custom_domain_base_url and settings.custom_domain_api_key:
            config = normalize_email_service_config(
                service_type,
                {
                    "base_url": settings.custom_domain_base_url,
                    "api_key": settings.custom_domain_api_key.get_secret_value() if settings.custom_domain_api_key else "",
                },
                proxy_url,
            )
            config = _apply_domain_and_address_selection(
                service_type=service_type,
                service_name="默认自定义域名服务",
                config=config,
                selection=selection,
                log_callback=log_callback,
            )
            _log(log_callback, "使用默认自定义域名服务")
            return ResolvedEmailService(
                service_type=service_type,
                config=config,
                email_service_id=None,
                service_name="默认自定义域名服务",
            )

    if fallback_config:
        config = normalize_email_service_config(service_type, fallback_config, proxy_url)
        config = _apply_domain_and_address_selection(
            service_type=service_type,
            service_name=service_type.value,
            config=config,
            selection=selection,
            log_callback=log_callback,
        )
        return ResolvedEmailService(
            service_type=service_type,
            config=config,
            email_service_id=requested_service_id,
            service_name=service_type.value,
        )

    raise ValueError(f"没有可用的 {service_type.value} 邮箱服务")


def _resolve_outlook_service(
    db,
    requested_service_id: Optional[int],
    proxy_url: Optional[str],
    selection: RegistrationSelectionRequest,
    log_callback: LogCallback = None,
) -> ResolvedEmailService:
    candidates = _get_outlook_candidates(db)
    if requested_service_id and not selection.selected_email_addresses and not (selection.random_outlook_account or selection.random_email_service):
        candidates = [item for item in candidates if item["service"].id == requested_service_id]

    if selection.selected_email_addresses:
        selected_set = {item.lower() for item in selection.selected_email_addresses}
        filtered = [item for item in candidates if item["email"].lower() in selected_set]
        if filtered:
            candidates = filtered
        else:
            raise ValueError("所选 Outlook 账号不可用")

    if not candidates:
        raise ValueError("没有可用的 Outlook 账号")

    candidate_pool = candidates
    if selection.random_outlook_account or selection.random_email_service:
        unregistered = [item for item in candidates if not item["is_registered"]]
        if unregistered:
            candidate_pool = unregistered

    chosen = _pick_entry(
        candidate_pool,
        selection_index=selection.selection_index,
        randomize=selection.random_outlook_account or selection.random_email_service,
        cycle=bool(selection.selected_email_addresses),
    )
    db_service = chosen["service"]
    config = normalize_email_service_config(EmailServiceType.OUTLOOK, db_service.config, proxy_url)
    config["existing_email"] = chosen["email"]
    _log(log_callback, f"使用 Outlook 账号: {chosen['email']}")
    return ResolvedEmailService(
        service_type=EmailServiceType.OUTLOOK,
        config=config,
        email_service_id=db_service.id,
        service_name=db_service.name,
    )


def _apply_domain_and_address_selection(
    service_type: EmailServiceType,
    service_name: str,
    config: Dict[str, Any],
    selection: RegistrationSelectionRequest,
    log_callback: LogCallback,
) -> Dict[str, Any]:
    if not (
        (service_type in DOMAIN_SELECTION_TYPES and (selection.random_domain or selection.selected_domains))
        or (service_type in ADDRESS_SELECTION_TYPES and selection.selected_email_addresses)
    ):
        return config

    service = EmailServiceFactory.create(service_type, config, name=service_name)

    if service_type in DOMAIN_SELECTION_TYPES:
        config = _apply_domain_selection(service_type, service, config, selection, log_callback)

    if service_type in ADDRESS_SELECTION_TYPES and selection.selected_email_addresses:
        config = _apply_email_address_selection(service, config, selection, log_callback)

    return config


def _apply_domain_selection(
    service_type: EmailServiceType,
    service,
    config: Dict[str, Any],
    selection: RegistrationSelectionRequest,
    log_callback: LogCallback,
) -> Dict[str, Any]:
    available_domains = _dedupe_strings(service.list_domains())
    if not available_domains:
        return config

    selected_domains = _ordered_matches(
        available_domains,
        selection.selected_domains,
        value_getter=lambda item: item,
    )
    domain_pool = selected_domains or available_domains
    if selection.selected_domains and not selected_domains:
        raise ValueError("所选域名在当前邮箱服务中不可用")

    if not selection.random_domain and not selection.selected_domains:
        return config

    chosen = _pick_entry(
        domain_pool,
        selection_index=selection.selection_index,
        randomize=selection.random_domain,
        cycle=bool(selection.selected_domains),
    )
    if service_type == EmailServiceType.MOE_MAIL:
        config["default_domain"] = chosen
        config["domain"] = chosen
    elif service_type in (EmailServiceType.TEMP_MAIL, EmailServiceType.FREEMAIL):
        config["domain"] = chosen
    elif service_type == EmailServiceType.DUCK_MAIL:
        config["default_domain"] = chosen

    _log(log_callback, f"本次注册使用域名: @{chosen}")
    return config


def _apply_email_address_selection(service, config: Dict[str, Any], selection: RegistrationSelectionRequest, log_callback: LogCallback) -> Dict[str, Any]:
    available_addresses = _normalize_email_options(service.list_emails())
    address_pool = _ordered_matches(
        available_addresses,
        selection.selected_email_addresses,
        value_getter=lambda item: item["email"],
    )
    if not address_pool:
        raise ValueError("所选邮箱地址在当前邮箱服务中不可用")

    chosen = _pick_entry(
        address_pool,
        selection_index=selection.selection_index,
        randomize=False,
        cycle=len(address_pool) > 1,
    )
    config["existing_email"] = chosen["email"]
    if chosen.get("id"):
        config["existing_email_id"] = chosen["id"]
    _log(log_callback, f"本次注册复用邮箱地址: {chosen['email']}")
    return config


def _find_options_service(db, service_type: EmailServiceType, service_id: Optional[int]) -> Optional[EmailServiceModel]:
    if service_id:
        return (
            db.query(EmailServiceModel)
            .filter(
                EmailServiceModel.id == service_id,
                EmailServiceModel.enabled == True,
            )
            .first()
        )

    service = (
        db.query(EmailServiceModel)
        .filter(
            EmailServiceModel.service_type == service_type.value,
            EmailServiceModel.enabled == True,
        )
        .order_by(EmailServiceModel.priority.asc(), EmailServiceModel.id.asc())
        .first()
    )
    return service


def _select_database_service(db, service_type: EmailServiceType, requested_service_id: Optional[int], random_email_service: bool) -> Optional[EmailServiceModel]:
    if requested_service_id and not random_email_service:
        service = (
            db.query(EmailServiceModel)
            .filter(
                EmailServiceModel.id == requested_service_id,
                EmailServiceModel.enabled == True,
            )
            .first()
        )
        if service:
            return service
        raise ValueError(f"邮箱服务不存在或已禁用: {requested_service_id}")

    services = (
        db.query(EmailServiceModel)
        .filter(
            EmailServiceModel.service_type == service_type.value,
            EmailServiceModel.enabled == True,
        )
        .order_by(EmailServiceModel.priority.asc(), EmailServiceModel.id.asc())
        .all()
    )
    if not services:
        return None
    return random.choice(services) if random_email_service else services[0]


def _get_outlook_candidates(db) -> List[Dict[str, Any]]:
    services = (
        db.query(EmailServiceModel)
        .filter(
            EmailServiceModel.service_type == EmailServiceType.OUTLOOK.value,
            EmailServiceModel.enabled == True,
        )
        .order_by(EmailServiceModel.priority.asc(), EmailServiceModel.id.asc())
        .all()
    )
    candidates: List[Dict[str, Any]] = []
    for service in services:
        config = service.config or {}
        email = str(config.get("email") or service.name or "").strip()
        if not email:
            continue
        existing_account = db.query(Account).filter(Account.email == email).first()
        candidates.append(
            {
                "service": service,
                "email": email,
                "is_registered": existing_account is not None,
            }
        )
    return candidates


def _build_outlook_account_options(db) -> List[Dict[str, Any]]:
    options: List[Dict[str, Any]] = []
    for candidate in _get_outlook_candidates(db):
        service = candidate["service"]
        config = service.config or {}
        options.append(
            {
                "id": str(service.id),
                "email": candidate["email"],
                "label": candidate["email"],
                "service_id": service.id,
                "has_oauth": bool(config.get("client_id") and config.get("refresh_token")),
                "is_registered": candidate["is_registered"],
            }
        )
    return options


def _normalize_email_options(items: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = set()
    result: List[Dict[str, Any]] = []
    for item in items:
        email = str(item.get("email") or item.get("address") or "").strip()
        if not email:
            continue
        lowered = email.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(
            {
                "id": str(item.get("id") or item.get("service_id") or email),
                "email": email,
                "label": email,
                "raw": item,
            }
        )
        if len(result) >= ENUMERATION_LIMIT:
            break
    return result


def _supports_random_service(db, service_type: EmailServiceType) -> bool:
    if service_type == EmailServiceType.OUTLOOK:
        return False
    count = (
        db.query(EmailServiceModel)
        .filter(
            EmailServiceModel.service_type == service_type.value,
            EmailServiceModel.enabled == True,
        )
        .count()
    )
    return count > 1


def _dedupe_strings(items: Sequence[Any]) -> List[str]:
    seen = set()
    result: List[str] = []
    for item in items:
        value = str(item or "").strip().lstrip("@")
        if not value:
            continue
        lowered = value.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(value)
    return result[:ENUMERATION_LIMIT]


def _pick_entry(items: Sequence[Any], selection_index: int = 0, randomize: bool = False, cycle: bool = False):
    if not items:
        raise ValueError("没有可用候选项")
    if randomize:
        return random.choice(list(items))
    if cycle and len(items) > 1:
        return list(items)[selection_index % len(items)]
    return list(items)[0]


def _ordered_matches(items: Sequence[Any], selected_values: Sequence[str], value_getter: Callable[[Any], str]):
    """按前端选中顺序筛选候选项，保证批量轮转顺序与用户勾选顺序一致。"""
    if not selected_values:
        return list(items)

    normalized_items = {value_getter(item).lower(): item for item in items}
    result = []
    seen = set()

    for selected in selected_values:
        key = selected.lower()
        if key in seen or key not in normalized_items:
            continue
        seen.add(key)
        result.append(normalized_items[key])

    return result


def _log(callback: LogCallback, message: str) -> None:
    if callback:
        callback(f"[选择] {message}")
