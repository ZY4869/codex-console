"""
Grok 复用主程序邮箱服务的桥接层。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from ...config.constants import EmailServiceType
from ...config.settings import get_settings
from ...database.models import EmailService as EmailServiceModel
from ...database.session import get_db
from ...services import BaseEmailService, EmailServiceFactory
from .email_service import BczyEmailService, GrokEmailServiceError


DOMAIN_CAPABLE_TYPES = {
    EmailServiceType.MOE_MAIL,
    EmailServiceType.TEMP_MAIL,
    EmailServiceType.DUCK_MAIL,
    EmailServiceType.FREEMAIL,
}
SUPPORTED_GROK_SERVICE_TYPES = (
    EmailServiceType.TEMPMAIL,
    EmailServiceType.MOE_MAIL,
    EmailServiceType.TEMP_MAIL,
    EmailServiceType.DUCK_MAIL,
    EmailServiceType.FREEMAIL,
)
GROK_VERIFICATION_CODE_PATTERN = r"(?<![A-Z0-9])([A-Z0-9]{3}-[A-Z0-9]{3}|\d{6})(?![A-Z0-9])"


@dataclass
class GrokEmailServiceResolution:
    service_type: EmailServiceType
    service_name: str
    config: Dict[str, Any]
    email_service_id: Optional[int] = None


class GrokManagedEmailService:
    def __init__(self, service: BaseEmailService, *, service_type: EmailServiceType, domain: str):
        self._service = service
        self._service_type = service_type
        self._domain = (domain or "").strip().lstrip("@")
        self._mailboxes: Dict[str, Dict[str, Any]] = {}

    def create_address(self, order_index: int) -> Dict[str, Any]:
        request_config = self._build_create_config(order_index)
        payload = self._service.create_email(request_config or None)
        email = str((payload or {}).get("email") or (payload or {}).get("address") or "").strip()
        if not email:
            raise GrokEmailServiceError("Reusable email service did not return an address.")
        self._mailboxes[email.lower()] = payload or {}
        return {"email": email, "payload": payload}

    def poll_code(self, email: str, *, timeout_seconds: int, poll_interval: int) -> str:
        mailbox = self._mailboxes.get(str(email or "").strip().lower(), {})
        email_id = str(mailbox.get("service_id") or mailbox.get("id") or "").strip() or None
        code = self._service.get_verification_code(
            email=str(email),
            email_id=email_id,
            timeout=max(1, int(timeout_seconds)),
            pattern=GROK_VERIFICATION_CODE_PATTERN,
        )
        if code:
            return str(code)
        raise GrokEmailServiceError(
            f"Timed out while waiting for verification code: {email} "
            f"(timeout={timeout_seconds}s, poll_interval={poll_interval}s)"
        )

    def _build_create_config(self, order_index: int) -> Dict[str, Any]:
        request_config: Dict[str, Any] = {}
        if self._domain and self._service_type in DOMAIN_CAPABLE_TYPES:
            if self._service_type == EmailServiceType.MOE_MAIL:
                request_config["default_domain"] = self._domain
            else:
                request_config["domain"] = self._domain
        return request_config


def create_grok_email_service(task) -> BczyEmailService | GrokManagedEmailService:
    config = task.config or {}
    if config.get("mock_mode") or _uses_legacy_adapter(config):
        return BczyEmailService(
            api_key=config.get("bczy_api_key"),
            domain=task.email_domain or "bczy.site",
            config=config,
        )

    resolved = resolve_grok_email_service(task)
    service = EmailServiceFactory.create(
        resolved.service_type,
        resolved.config,
        name=resolved.service_name,
    )
    return GrokManagedEmailService(
        service,
        service_type=resolved.service_type,
        domain=task.email_domain or "",
    )


def resolve_grok_email_service(task) -> GrokEmailServiceResolution:
    config = dict(task.config or {})
    selected_type = str(config.get("email_service_type") or "auto").strip().lower()
    selected_id = _coerce_int(config.get("email_service_id"))
    proxy_url = task.proxy or None
    domain = str(task.email_domain or "").strip().lstrip("@")

    if selected_type and selected_type != "auto":
        service_type = _parse_supported_type(selected_type)
        return _resolve_specific_service(service_type, selected_id, proxy_url=proxy_url, domain=domain)

    return _resolve_auto_service(proxy_url=proxy_url, domain=domain)


def _uses_legacy_adapter(config: Dict[str, Any]) -> bool:
    return bool(str(config.get("email_create_url") or "").strip() or str(config.get("email_messages_url") or "").strip())


def _resolve_auto_service(*, proxy_url: Optional[str], domain: str) -> GrokEmailServiceResolution:
    ordered_types = _get_auto_service_order()
    if domain:
        matching_candidates = []
        for service_type in ordered_types:
            try:
                candidate = _resolve_specific_service(service_type, None, proxy_url=proxy_url, domain=domain)
            except GrokEmailServiceError:
                continue
            if _config_matches_domain(service_type, candidate.config, domain):
                matching_candidates.append(candidate)
        if matching_candidates:
            return matching_candidates[0]
        raise GrokEmailServiceError(
            f"No reusable email service is available for domain @{domain}. "
            "Configure a matching service on the 邮箱服务 page or choose a different domain."
        )

    for service_type in ordered_types:
        try:
            return _resolve_specific_service(service_type, None, proxy_url=proxy_url, domain=domain)
        except GrokEmailServiceError:
            continue

    raise GrokEmailServiceError(
        "No reusable email service is configured. Configure TempMail/Freemail/MoeMail on the 邮箱服务 page."
    )


def _get_auto_service_order() -> list[EmailServiceType]:
    settings = get_settings()
    raw_priority = dict(settings.email_service_priority or {})
    order = sorted(
        SUPPORTED_GROK_SERVICE_TYPES,
        key=lambda item: (int(raw_priority.get(item.value, 999)), item.value),
    )
    return list(order)


def _resolve_specific_service(
    service_type: EmailServiceType,
    service_id: Optional[int],
    *,
    proxy_url: Optional[str],
    domain: str,
) -> GrokEmailServiceResolution:
    settings = get_settings()
    if service_type == EmailServiceType.TEMPMAIL:
        config = {
            "base_url": settings.tempmail_base_url,
            "timeout": settings.tempmail_timeout,
            "max_retries": settings.tempmail_max_retries,
            "proxy_url": proxy_url,
        }
        return GrokEmailServiceResolution(
            service_type=service_type,
            service_name="Tempmail.lol",
            config=config,
            email_service_id=None,
        )

    if service_type == EmailServiceType.MOE_MAIL:
        selected = _get_database_service(service_type, service_id)
        if selected:
            config = _normalize_email_service_config(service_type, selected.config, proxy_url=proxy_url, domain=domain)
            return GrokEmailServiceResolution(
                service_type=service_type,
                service_name=selected.name,
                config=config,
                email_service_id=selected.id,
            )
        if settings.custom_domain_base_url and settings.custom_domain_api_key:
            config = _normalize_email_service_config(
                service_type,
                {
                    "base_url": settings.custom_domain_base_url,
                    "api_key": settings.custom_domain_api_key.get_secret_value() if settings.custom_domain_api_key else "",
                },
                proxy_url=proxy_url,
                domain=domain,
            )
            return GrokEmailServiceResolution(
                service_type=service_type,
                service_name="默认自定义域名服务",
                config=config,
                email_service_id=None,
            )

    selected = _get_database_service(service_type, service_id)
    if selected:
        config = _normalize_email_service_config(service_type, selected.config, proxy_url=proxy_url, domain=domain)
        return GrokEmailServiceResolution(
            service_type=service_type,
            service_name=selected.name,
            config=config,
            email_service_id=selected.id,
        )

    raise GrokEmailServiceError(f"No enabled Grok email service found for type: {service_type.value}")


def _get_database_service(service_type: EmailServiceType, service_id: Optional[int]) -> Optional[EmailServiceModel]:
    with get_db() as db:
        query = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == service_type.value,
            EmailServiceModel.enabled == True,
        )
        if service_id is not None:
            selected = query.filter(EmailServiceModel.id == service_id).first()
            if not selected:
                raise GrokEmailServiceError(f"Email service does not exist or is disabled: {service_id}")
            return selected
        return query.order_by(EmailServiceModel.priority.asc(), EmailServiceModel.id.asc()).first()


def _normalize_email_service_config(
    service_type: EmailServiceType,
    config: Optional[Dict[str, Any]],
    *,
    proxy_url: Optional[str],
    domain: str,
) -> Dict[str, Any]:
    normalized = dict(config or {})
    if "api_url" in normalized and "base_url" not in normalized:
        normalized["base_url"] = normalized.pop("api_url")
    if proxy_url and "proxy_url" not in normalized:
        normalized["proxy_url"] = proxy_url
    if domain and service_type in DOMAIN_CAPABLE_TYPES:
        if service_type == EmailServiceType.MOE_MAIL:
            normalized["default_domain"] = domain
        else:
            normalized["domain"] = domain
    return normalized


def _config_matches_domain(service_type: EmailServiceType, config: Dict[str, Any], domain: str) -> bool:
    if not domain:
        return True
    if service_type not in DOMAIN_CAPABLE_TYPES:
        return False
    configured = str(config.get("default_domain") or config.get("domain") or "").strip().lstrip("@")
    return not configured or configured.lower() == domain.lower()


def _parse_supported_type(value: str) -> EmailServiceType:
    try:
        service_type = EmailServiceType(value)
    except ValueError as exc:
        raise GrokEmailServiceError(f"Unsupported Grok email service type: {value}") from exc
    if service_type not in SUPPORTED_GROK_SERVICE_TYPES:
        raise GrokEmailServiceError(f"Unsupported Grok email service type: {value}")
    return service_type


def _coerce_int(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None
