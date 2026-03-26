from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from ...database import crud
from ...database.models import Account

PLATFORM_UPLOAD_RECORDS_KEY = "platform_upload_records"
PLATFORM_DUPLICATE_REASON = "platform_duplicate"

UrlNormalizer = Callable[[str], str]


def _normalize_service_id(service_id: Optional[int]) -> Optional[int]:
    try:
        numeric = int(service_id or 0)
    except (TypeError, ValueError):
        return None
    return numeric if numeric > 0 else None


def _normalize_api_url(api_url: Optional[str], url_normalizer: Optional[UrlNormalizer]) -> str:
    raw_url = str(api_url or "").strip()
    if not raw_url:
        return ""
    normalized = url_normalizer(raw_url) if url_normalizer else raw_url.rstrip("/")
    return str(normalized or "").strip().rstrip("/").lower()


def build_upload_target_key(
    *,
    service_id: Optional[int] = None,
    api_url: Optional[str] = None,
    url_normalizer: Optional[UrlNormalizer] = None,
) -> Optional[str]:
    normalized_service_id = _normalize_service_id(service_id)
    if normalized_service_id is not None:
        return f"service:{normalized_service_id}"

    normalized_url = _normalize_api_url(api_url, url_normalizer)
    if not normalized_url:
        return None

    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:16]
    return f"url:{digest}"


def _record_key(platform: str, target_key: str) -> str:
    return f"{platform}:{target_key}"


def _load_records(account: Account) -> Dict[str, Dict[str, Any]]:
    extra_data = account.extra_data if isinstance(account.extra_data, dict) else {}
    records = extra_data.get(PLATFORM_UPLOAD_RECORDS_KEY)
    return dict(records) if isinstance(records, dict) else {}


def load_platform_upload_record(
    account: Account,
    platform: str,
    *,
    service_id: Optional[int] = None,
    api_url: Optional[str] = None,
    url_normalizer: Optional[UrlNormalizer] = None,
) -> Optional[Dict[str, Any]]:
    target_key = build_upload_target_key(
        service_id=service_id,
        api_url=api_url,
        url_normalizer=url_normalizer,
    )
    if not target_key:
        return None
    return _load_records(account).get(_record_key(platform, target_key))


def save_platform_upload_record(
    db,
    account: Account,
    platform: str,
    *,
    service_id: Optional[int] = None,
    api_url: Optional[str] = None,
    url_normalizer: Optional[UrlNormalizer] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    target_key = build_upload_target_key(
        service_id=service_id,
        api_url=api_url,
        url_normalizer=url_normalizer,
    )
    if not target_key:
        return None

    extra_data = account.extra_data.copy() if isinstance(account.extra_data, dict) else {}
    records = dict(extra_data.get(PLATFORM_UPLOAD_RECORDS_KEY) or {})
    normalized_service_id = _normalize_service_id(service_id)
    record = {
        "platform": platform,
        "service_id": normalized_service_id,
        "target_key": target_key,
        "uploaded_at": datetime.utcnow().isoformat(),
    }
    if metadata:
        record.update(metadata)
    records[_record_key(platform, target_key)] = record
    extra_data[PLATFORM_UPLOAD_RECORDS_KEY] = records
    crud.update_account(
        db,
        account.id,
        extra_data=extra_data,
        updated_at=datetime.utcnow(),
    )
    return record


def build_platform_duplicate_detail(
    account: Account,
    *,
    source: str,
    message: str,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    detail = {
        "id": account.id,
        "email": account.email,
        "success": False,
        "skipped": True,
        "reason_code": PLATFORM_DUPLICATE_REASON,
        "duplicate_source": source,
        "message": message,
    }
    if extra:
        for key, value in extra.items():
            if value is not None:
                detail[key] = value
    return detail
