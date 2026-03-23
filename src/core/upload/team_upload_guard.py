"""
Shared guardrails for Team-context uploads.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

from ...database import crud
from ...database.models import Account

logger = logging.getLogger(__name__)

TEAM_UPLOAD_GUARD_KEY = "team_upload_guard"
TEAM_UPLOAD_RECORDS_KEY = "records"

TEAM_REFRESH_TOKEN_DUPLICATE = "team_refresh_token_duplicate"
TEAM_REFRESH_TOKEN_REUPLOAD_BLOCKED = "team_refresh_token_reupload_blocked"
TEAM_MULTIGROUP_COPY_BLOCKED = "team_multigroup_copy_blocked"
OPENAI_OAUTH_TOKEN_REFRESH_FAILED = "openai_oauth_token_refresh_failed"
OPENAI_OAUTH_INVALID_REQUEST = "openai_oauth_invalid_request"
REFRESH_TOKEN_REUSED = "refresh_token_reused"


def _normalize_team_account_id(team_context: Optional[Dict[str, Any]]) -> str:
    return str((team_context or {}).get("team_account_id") or "").strip()


def _normalize_service_id(service_id: Optional[int]) -> int:
    try:
        numeric = int(service_id or 0)
    except (TypeError, ValueError):
        return 0
    return numeric if numeric > 0 else 0


def _snapshot_key(platform: str, service_id: Optional[int], team_account_id: str) -> str:
    return f"{platform}:{_normalize_service_id(service_id)}:{team_account_id}"


def build_refresh_token_hash(refresh_token: Optional[str]) -> str:
    token = str(refresh_token or "").strip()
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def shorten_refresh_token_hash(refresh_token_hash: Optional[str]) -> str:
    value = str(refresh_token_hash or "").strip()
    return value[:12] if value else ""


def classify_team_upload_error(error_message: Optional[str]) -> Dict[str, Any]:
    message = str(error_message or "").strip()
    lower_message = message.lower()
    guard_message = "该账号需要重新登录或刷新令牌后再重新上传。"

    if "refresh_token_reused" in lower_message:
        return {
            "reason_code": REFRESH_TOKEN_REUSED,
            "guard_blocked": False,
            "guard_message": guard_message,
        }
    if "openai_oauth_token_refresh_failed" in lower_message:
        return {
            "reason_code": OPENAI_OAUTH_TOKEN_REFRESH_FAILED,
            "guard_blocked": False,
            "guard_message": guard_message,
        }
    if "status 401" in lower_message and "invalid_request_error" in lower_message:
        return {
            "reason_code": OPENAI_OAUTH_INVALID_REQUEST,
            "guard_blocked": False,
            "guard_message": guard_message,
        }
    return {}


def is_unrecoverable_team_upload_error(error_message: Optional[str]) -> bool:
    return bool(classify_team_upload_error(error_message))


def build_team_upload_guard_detail(
    account: Account,
    *,
    reason_code: str,
    error: str,
    guard_message: str,
) -> Dict[str, Any]:
    return {
        "id": account.id,
        "email": account.email,
        "success": False,
        "error": error,
        "reason_code": reason_code,
        "guard_blocked": True,
        "guard_message": guard_message,
    }


def enrich_team_upload_error_detail(detail: Dict[str, Any], error_message: Optional[str]) -> Dict[str, Any]:
    detail.update(classify_team_upload_error(error_message))
    return detail


def _load_team_upload_records(account: Account) -> Dict[str, Dict[str, Any]]:
    extra_data = account.extra_data if isinstance(account.extra_data, dict) else {}
    section = extra_data.get(TEAM_UPLOAD_GUARD_KEY)
    if not isinstance(section, dict):
        return {}
    records = section.get(TEAM_UPLOAD_RECORDS_KEY)
    return dict(records) if isinstance(records, dict) else {}


def evaluate_team_upload_guard(
    db,
    accounts: Iterable[Account],
    *,
    platform: str,
    team_context: Optional[Dict[str, Any]],
    service_id: Optional[int] = None,
    selected_group_ids: Optional[Iterable[int]] = None,
) -> Dict[str, Any]:
    candidates = list(accounts or [])
    team_account_id = _normalize_team_account_id(team_context)
    if not candidates or not team_account_id:
        return {"allowed_accounts": candidates, "blocked_details": []}

    blocked_by_account_id: Dict[int, Dict[str, Any]] = {}
    normalized_group_ids = []
    for group_id in selected_group_ids or []:
        try:
            numeric = int(group_id)
        except (TypeError, ValueError):
            continue
        if numeric > 0 and numeric not in normalized_group_ids:
            normalized_group_ids.append(numeric)

    if platform == "sub2api" and len(normalized_group_ids) > 1:
        for account in candidates:
            blocked_by_account_id[account.id] = build_team_upload_guard_detail(
                account,
                reason_code=TEAM_MULTIGROUP_COPY_BLOCKED,
                error="Team 账号上传到 Sub2API 时仅允许单分组，已阻止多分组复制上传。",
                guard_message="请将 Team 账号上传改为单分组后重试。",
            )
        logger.warning(
            "Blocked Team Sub2API multi-group upload for team %s with groups %s",
            team_account_id,
            normalized_group_ids,
        )
        return {"allowed_accounts": [], "blocked_details": list(blocked_by_account_id.values())}

    duplicated_accounts: Dict[str, List[Account]] = {}
    for account in candidates:
        refresh_token_hash = build_refresh_token_hash(account.refresh_token)
        if not refresh_token_hash:
            continue
        duplicated_accounts.setdefault(refresh_token_hash, []).append(account)

    for refresh_token_hash, entries in duplicated_accounts.items():
        if len(entries) <= 1:
            continue
        token_hint = shorten_refresh_token_hash(refresh_token_hash)
        for account in entries:
            blocked_by_account_id[account.id] = build_team_upload_guard_detail(
                account,
                reason_code=TEAM_REFRESH_TOKEN_DUPLICATE,
                error=f"检测到同一 Team 团队内重复使用 refresh token 指纹 {token_hint}，已阻止上传。",
                guard_message="同一 Team 上传批次中出现重复 refresh token，请先重新登录相关账号后再上传。",
            )
        logger.warning(
            "Blocked duplicated Team refresh token hash %s on team %s for platform %s",
            token_hint,
            team_account_id,
            platform,
        )

    for account in candidates:
        if account.id in blocked_by_account_id:
            continue
        refresh_token_hash = build_refresh_token_hash(account.refresh_token)
        if not refresh_token_hash:
            continue
        record = _load_team_upload_records(account).get(_snapshot_key(platform, service_id, team_account_id))
        if not record:
            continue
        if str(record.get("refresh_token_hash") or "") != refresh_token_hash:
            continue
        blocked_by_account_id[account.id] = build_team_upload_guard_detail(
            account,
            reason_code=TEAM_REFRESH_TOKEN_REUPLOAD_BLOCKED,
            error="该 Team 账号使用相同 refresh token 已成功上传过当前平台服务，已阻止重复上传。",
            guard_message="若需要再次上传，请先重新登录该账号以生成新的 refresh token。",
        )
        logger.warning(
            "Blocked repeated Team upload for account %s on %s service %s team %s",
            account.email,
            platform,
            _normalize_service_id(service_id),
            team_account_id,
        )

    allowed_accounts = [account for account in candidates if account.id not in blocked_by_account_id]
    return {"allowed_accounts": allowed_accounts, "blocked_details": list(blocked_by_account_id.values())}


def record_team_upload_success(
    db,
    account: Account,
    *,
    platform: str,
    team_context: Optional[Dict[str, Any]],
    service_id: Optional[int] = None,
) -> None:
    team_account_id = _normalize_team_account_id(team_context)
    refresh_token_hash = build_refresh_token_hash(account.refresh_token)
    if not team_account_id or not refresh_token_hash:
        return

    extra_data = account.extra_data.copy() if isinstance(account.extra_data, dict) else {}
    section = dict(extra_data.get(TEAM_UPLOAD_GUARD_KEY) or {})
    records = dict(section.get(TEAM_UPLOAD_RECORDS_KEY) or {})
    task_uuid = str(
        (team_context or {}).get("team_invite_task_uuid")
        or (team_context or {}).get("team_task_uuid")
        or ""
    ).strip()
    records[_snapshot_key(platform, service_id, team_account_id)] = {
        "team_account_id": team_account_id,
        "refresh_token_hash": refresh_token_hash,
        "platform": platform,
        "service_id": _normalize_service_id(service_id),
        "uploaded_at": datetime.utcnow().isoformat(),
        "task_uuid": task_uuid,
    }
    section[TEAM_UPLOAD_RECORDS_KEY] = records
    extra_data[TEAM_UPLOAD_GUARD_KEY] = section
    crud.update_account(
        db,
        account.id,
        extra_data=extra_data,
        updated_at=datetime.utcnow(),
    )
