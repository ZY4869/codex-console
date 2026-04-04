"""
Team Manager 上传功能
参照 CPA 上传模式，直连不走代理
"""

import logging
from typing import List, Optional, Tuple

from curl_cffi import requests as cffi_requests

from ...database.models import Account
from ...database.session import get_db
from ..register import build_missing_refresh_token_error, has_required_refresh_token
from .platform_upload_dedupe import (
    build_platform_duplicate_detail,
    load_platform_upload_record,
    save_platform_upload_record,
)
from .team_upload_guard import (
    enrich_team_upload_error_detail,
    evaluate_team_upload_guard,
    record_team_upload_success,
)

logger = logging.getLogger(__name__)


def _build_missing_upload_token_message(account: Account) -> str:
    if not str(account.access_token or "").strip():
        return "账号缺少 access_token"
    if not has_required_refresh_token(account.refresh_token):
        return build_missing_refresh_token_error("当前账号上传前")
    return ""


def _resolve_team_manager_duplicate_detail(
    account: Account,
    *,
    api_url: Optional[str],
    service_id: Optional[int] = None,
):
    record = load_platform_upload_record(
        account,
        "tm",
        service_id=service_id,
        api_url=api_url,
    )
    if not record:
        return None
    return build_platform_duplicate_detail(
        account,
        source="local_record",
        message="Team Manager 已存在本地上传记录，已跳过",
    )


def upload_to_team_manager(
    account: Account,
    api_url: str,
    api_key: str,
    team_context: dict = None,
) -> Tuple[bool, str]:
    """
    上传单账号到 Team Manager（直连，不走代理）

    Returns:
        (成功标志, 消息)
    """
    if not api_url:
        return False, "Team Manager API URL 未配置"
    if not api_key:
        return False, "Team Manager API Key 未配置"
    token_error = _build_missing_upload_token_message(account)
    if token_error:
        return False, token_error

    url = api_url.rstrip("/") + "/admin/teams/import"
    headers = {
        "X-API-Key": api_key,
        "Content-Type": "application/json",
    }
    team_account_id = str((team_context or {}).get("team_account_id") or "").strip()
    payload = {
        "import_type": "single",
        "email": account.email,
        "access_token": account.access_token or "",
        "session_token": account.session_token or "",
        "refresh_token": account.refresh_token or "",
        "client_id": account.client_id or "",
        "account_id": team_account_id or account.account_id or "",
    }
    if team_context:
        logger.info("Team Manager 单账号上传携带 Team 上下文: %s", team_context.get("team_task_uuid"))

    try:
        resp = cffi_requests.post(
            url,
            headers=headers,
            json=payload,
            proxies=None,
            timeout=30
        )
        if resp.status_code in (200, 201):
            return True, "上传成功"
        error_msg = f"上传失败: HTTP {resp.status_code}"
        try:
            detail = resp.json()
            if isinstance(detail, dict):
                error_msg = detail.get("message", error_msg)
        except Exception:
            error_msg = f"{error_msg} - {resp.text[:200]}"
        return False, error_msg
    except Exception as e:
        logger.error(f"Team Manager 上传异常: {e}")
        return False, f"上传异常: {str(e)}"


def batch_upload_to_team_manager(
    account_ids: List[int],
    api_url: str,
    api_key: str,
    team_context: dict = None,
    service_id: Optional[int] = None,
    dedupe: bool = False,
) -> dict:
    """
    批量上传账号到 Team Manager（使用 batch 模式，一次请求提交所有账号）

    Returns:
        包含成功/失败统计和详情的字典
    """
    results = {
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "details": [],
        "team_context": team_context,
    }

    with get_db() as db:
        valid_accounts = []
        for account_id in account_ids:
            account = db.query(Account).filter(Account.id == account_id).first()
            if not account:
                results["failed_count"] += 1
                results["details"].append(
                    {"id": account_id, "email": None, "success": False, "error": "账号不存在"}
                )
                continue
            token_error = _build_missing_upload_token_message(account)
            if token_error:
                results["failed_count"] += 1
                results["details"].append(
                    {"id": account_id, "email": account.email, "success": False, "error": token_error}
                )
                continue
            valid_accounts.append(account)

        guard_result = evaluate_team_upload_guard(
            db,
            valid_accounts,
            platform="tm",
            team_context=team_context,
            service_id=service_id,
        )
        blocked_details = list(guard_result.get("blocked_details") or [])
        if blocked_details:
            results["skipped_count"] += len(blocked_details)
            results["details"].extend(blocked_details)

        valid_accounts = list(guard_result.get("allowed_accounts") or [])
        if dedupe and not team_context:
            deduped_accounts = []
            for account in valid_accounts:
                duplicate_detail = _resolve_team_manager_duplicate_detail(
                    account,
                    api_url=api_url,
                    service_id=service_id,
                )
                if duplicate_detail:
                    results["skipped_count"] += 1
                    results["details"].append(duplicate_detail)
                    continue
                deduped_accounts.append(account)
            valid_accounts = deduped_accounts
        if not valid_accounts:
            return results

        lines = [
            ",".join([
                account.email or "",
                account.access_token or "",
                account.refresh_token or "",
                account.session_token or "",
                account.client_id or "",
            ])
            for account in valid_accounts
        ]

        url = api_url.rstrip("/") + "/admin/teams/import"
        headers = {
            "X-API-Key": api_key,
            "Content-Type": "application/json",
        }
        payload = {
            "import_type": "batch",
            "content": "\n".join(lines),
        }
        if team_context:
            logger.info("Team Manager 批量上传携带 Team 上下文: %s", team_context.get("team_task_uuid"))

        try:
            resp = cffi_requests.post(
                url,
                headers=headers,
                json=payload,
                proxies=None,
                timeout=60,
                impersonate="chrome110",
            )
            if resp.status_code in (200, 201):
                for account in valid_accounts:
                    if dedupe and not team_context:
                        save_platform_upload_record(
                            db,
                            account,
                            "tm",
                            service_id=service_id,
                            api_url=api_url,
                        )
                    record_team_upload_success(
                        db,
                        account,
                        platform="tm",
                        team_context=team_context,
                        service_id=service_id,
                    )
                    results["success_count"] += 1
                    results["details"].append(
                        {"id": account.id, "email": account.email, "success": True, "message": "批量上传成功"}
                    )
            else:
                error_msg = f"批量上传失败: HTTP {resp.status_code}"
                try:
                    detail = resp.json()
                    if isinstance(detail, dict):
                        error_msg = detail.get("message", error_msg)
                except Exception:
                    error_msg = f"{error_msg} - {resp.text[:200]}"
                for account in valid_accounts:
                    results["failed_count"] += 1
                    detail = {"id": account.id, "email": account.email, "success": False, "error": error_msg}
                    enrich_team_upload_error_detail(detail, error_msg)
                    results["details"].append(detail)
        except Exception as e:
            logger.error(f"Team Manager 批量上传异常: {e}")
            error_msg = f"上传异常: {str(e)}"
            for account in valid_accounts:
                results["failed_count"] += 1
                detail = {"id": account.id, "email": account.email, "success": False, "error": error_msg}
                enrich_team_upload_error_detail(detail, error_msg)
                results["details"].append(detail)

    return results


def test_team_manager_connection(api_url: str, api_key: str) -> Tuple[bool, str]:
    """
    测试 Team Manager 连接（直连）

    Returns:
        (成功标志, 消息)
    """
    if not api_url:
        return False, "API URL 不能为空"
    if not api_key:
        return False, "API Key 不能为空"

    url = api_url.rstrip("/") + "/admin/teams/import"
    headers = {"X-API-Key": api_key}

    try:
        resp = cffi_requests.options(
            url,
            headers=headers,
            proxies=None,
            timeout=10,
            impersonate="chrome110",
        )
        if resp.status_code in (200, 204, 401, 403, 405):
            if resp.status_code == 401:
                return False, "连接成功，但 API Key 无效"
            return True, "Team Manager 连接测试成功"
        return False, f"服务器返回异常状态码: {resp.status_code}"
    except cffi_requests.exceptions.ConnectionError as e:
        return False, f"无法连接到服务器: {str(e)}"
    except cffi_requests.exceptions.Timeout:
        return False, "连接超时，请检查网络配置"
    except Exception as e:
        return False, f"连接测试失败: {str(e)}"
