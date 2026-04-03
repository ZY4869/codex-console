"""
Sub2API account upload and export helpers.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from curl_cffi import requests as cffi_requests

from ...database.models import Account
from ...database.session import get_db
from .platform_upload_dedupe import (
    build_platform_duplicate_detail,
    load_platform_upload_record,
    save_platform_upload_record,
)
from .sub2api_groups import (
    bind_sub2api_accounts_to_groups,
    fetch_sub2api_groups,
    find_sub2api_account_ids_by_names,
    search_sub2api_accounts,
)
from .sub2api_naming import normalize_sub2api_identity, resolve_sub2api_group_naming_identity
from .sub2api_payload import (
    build_sub2api_export_payload,
    build_sub2api_named_accounts,
    build_sub2api_upload_payload,
    normalize_sub2api_template_config,
    reserve_sub2api_named_indices,
    resolve_sub2api_service,
)
from .team_upload_guard import (
    enrich_team_upload_error_detail,
    evaluate_team_upload_guard,
    record_team_upload_success,
)

logger = logging.getLogger(__name__)


def _normalize_group_ids(values: Optional[List[int]]) -> List[int]:
    normalized: List[int] = []
    for group_id in values or []:
        try:
            numeric = int(group_id)
        except (TypeError, ValueError):
            continue
        if numeric > 0 and numeric not in normalized:
            normalized.append(numeric)
    return normalized


def _load_group_name_map(api_url: Optional[str], api_key: Optional[str], group_ids: List[int]) -> Dict[int, str]:
    mapping = {group_id: f"Group {group_id}" for group_id in group_ids}
    if not group_ids or not api_url or not api_key:
        return mapping
    try:
        groups = fetch_sub2api_groups(api_url, api_key, platform="openai")
    except Exception as exc:
        logger.warning("Failed to fetch Sub2API groups for dynamic naming: %s", exc)
        return mapping

    for group in groups or []:
        try:
            group_id = int(group.get("id"))
        except (TypeError, ValueError, AttributeError):
            continue
        if group_id in mapping:
            mapping[group_id] = str(group.get("name") or mapping[group_id])
    return mapping


def _extract_sub2api_credentials(item: Dict[str, Any]) -> Dict[str, Any]:
    credentials = item.get("credentials")
    return credentials if isinstance(credentials, dict) else {}


def _resolve_sub2api_duplicate_detail(
    account: Account,
    *,
    api_url: str,
    api_key: str,
    service_id: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    record = load_platform_upload_record(
        account,
        "sub2api",
        service_id=service_id,
        api_url=api_url,
    )
    generated_names = {
        str(name).strip()
        for name in (record or {}).get("generated_names") or []
        if str(name).strip()
    }

    search_terms: List[str] = []
    for raw_value in (account.account_id, account.workspace_id, account.email):
        value = str(raw_value or "").strip()
        if value and value not in search_terms:
            search_terms.append(value)

    remote_unstable = False
    for term in search_terms:
        try:
            items = search_sub2api_accounts(api_url, api_key, term, platform="openai")
        except Exception as exc:
            logger.warning("Sub2API duplicate precheck failed for %s with term %s: %s", account.email, term, exc)
            remote_unstable = True
            continue

        saw_matchable_fields = False
        for item in items:
            credentials = _extract_sub2api_credentials(item)
            remote_account_id = str(
                credentials.get("chatgpt_account_id")
                or item.get("chatgpt_account_id")
                or ""
            ).strip()
            remote_org_id = str(
                credentials.get("organization_id")
                or item.get("organization_id")
                or ""
            ).strip()
            remote_notes = str(item.get("notes") or item.get("note") or "").strip()
            remote_name = str(item.get("name") or "").strip()
            if remote_account_id or remote_org_id or remote_notes or remote_name:
                saw_matchable_fields = True

            if account.account_id and remote_account_id == str(account.account_id).strip():
                return build_platform_duplicate_detail(
                    account,
                    source="remote",
                    message="Sub2API 已存在该账号，已跳过",
                )
            if account.workspace_id and remote_org_id == str(account.workspace_id).strip():
                return build_platform_duplicate_detail(
                    account,
                    source="remote",
                    message="Sub2API 已存在该账号，已跳过",
                )
            if remote_notes.lower() == str(account.email or "").strip().lower():
                return build_platform_duplicate_detail(
                    account,
                    source="remote",
                    message="Sub2API 已存在该账号，已跳过",
                )
            if generated_names and remote_name in generated_names:
                return build_platform_duplicate_detail(
                    account,
                    source="remote",
                    message="Sub2API 已存在该账号，已跳过",
                )

        if items and not saw_matchable_fields:
            remote_unstable = True

    if not remote_unstable or not record:
        return None

    return build_platform_duplicate_detail(
        account,
        source="local_record",
        message="Sub2API 已存在本地上传记录，已跳过",
        extra={
            "group_id": None,
            "group_name": None,
            "naming_identity": normalize_sub2api_identity(account.subscription_type),
            "generated_name": None,
            "copy_index": 0,
        },
    )


def _count_naming_identities(accounts: List[Account], group_name: Optional[str]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for account in accounts:
        account_identity = normalize_sub2api_identity(account.subscription_type)
        naming_identity = resolve_sub2api_group_naming_identity(group_name, account_identity)
        counts[naming_identity] = counts.get(naming_identity, 0) + 1
    return counts


def _apply_team_context(payload: Dict[str, Any], team_context: Optional[dict]) -> None:
    team_account_id = str((team_context or {}).get("team_account_id") or "").strip()
    if not team_account_id:
        return
    for item in payload["data"]["accounts"]:
        credentials = item.setdefault("credentials", {})
        credentials["organization_id"] = team_account_id
        credentials["chatgpt_account_id"] = team_account_id


def _build_detail(
    entry: Dict[str, Any],
    *,
    success: bool,
    copy_index: int,
    group_id: Optional[int],
    group_name: Optional[str],
    message: Optional[str] = None,
    error: Optional[str] = None,
) -> Dict[str, Any]:
    detail = {
        "id": entry["account"].id,
        "email": entry["account"].email,
        "success": success,
        "group_id": group_id,
        "group_name": group_name,
        "naming_identity": entry["naming_identity"],
        "generated_name": entry["generated_name"],
        "copy_index": copy_index,
    }
    if success:
        detail["message"] = message or "上传成功"
    else:
        detail["error"] = error or "上传失败"
        enrich_team_upload_error_detail(detail, detail["error"])
    return detail


def _parse_error_message(response) -> str:
    error_message = f"上传失败: HTTP {response.status_code}"
    try:
        payload = response.json()
        if isinstance(payload, dict):
            return payload.get("message", error_message)
    except Exception:
        pass
    text = getattr(response, "text", "") or ""
    return f"{error_message} - {text[:200]}" if text else error_message


def _create_import_headers(
    *,
    api_key: str,
    exported_at: str,
    service_id: Optional[int],
    group_id: Optional[int],
    copy_index: int,
    identity_signature: str,
) -> Dict[str, str]:
    return {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "Idempotency-Key": (
            f"import-{service_id or 0}-{group_id or 'ungrouped'}-"
            f"{identity_signature.lower()}-{copy_index}-{exported_at}"
        ),
    }


def _upload_copy(
    *,
    entries: List[Dict[str, Any]],
    api_url: str,
    api_key: str,
    template_config: Dict[str, Any],
    concurrency: Optional[int],
    priority: Optional[int],
    team_context: Optional[dict],
    service_id: Optional[int],
    copy_index: int,
    group_id: Optional[int],
    group_name: Optional[str],
) -> Dict[str, Any]:
    payload = build_sub2api_upload_payload(
        [entry["account"] for entry in entries],
        named_accounts=entries,
        template_config=template_config,
        concurrency_override=concurrency,
        priority_override=priority,
    )
    _apply_team_context(payload, team_context)

    generated_names = [entry["generated_name"] for entry in entries]
    identity_signature = "-".join(sorted({entry["naming_identity"] for entry in entries})) or "mixed"
    headers = _create_import_headers(
        api_key=api_key,
        exported_at=payload["data"]["exported_at"],
        service_id=service_id,
        group_id=group_id,
        copy_index=copy_index,
        identity_signature=identity_signature,
    )

    response = cffi_requests.post(
        api_url.rstrip("/") + "/api/v1/admin/accounts/data",
        json=payload,
        headers=headers,
        proxies=None,
        timeout=30,
        impersonate="chrome110",
    )
    if response.status_code not in (200, 201):
        error_text = _parse_error_message(response)
        return {
            "success_count": 0,
            "failed_count": len(entries),
            "details": [
                _build_detail(
                    entry,
                    success=False,
                    copy_index=copy_index,
                    group_id=group_id,
                    group_name=group_name,
                    error=error_text,
                )
                for entry in entries
            ],
        }

    if not group_id:
        return {
            "success_count": len(entries),
            "failed_count": 0,
            "details": [
                _build_detail(
                    entry,
                    success=True,
                    copy_index=copy_index,
                    group_id=None,
                    group_name=None,
                    message="成功上传",
                )
                for entry in entries
            ],
        }

    logger.info("Sub2API import succeeded, binding %s accounts to group %s", len(entries), group_id)
    try:
        account_ids_by_name = find_sub2api_account_ids_by_names(api_url, api_key, generated_names)
        bindable_ids = [account_ids_by_name[name] for name in generated_names if name in account_ids_by_name]
        missing_names = [name for name in generated_names if name not in account_ids_by_name]
        if bindable_ids:
            bind_sub2api_accounts_to_groups(api_url, api_key, bindable_ids, [group_id])
        details = []
        success_count = 0
        failed_count = 0
        for entry in entries:
            generated_name = entry["generated_name"]
            if generated_name in missing_names:
                failed_count += 1
                details.append(
                    _build_detail(
                        entry,
                        success=False,
                        copy_index=copy_index,
                        group_id=group_id,
                        group_name=group_name,
                        error=f"账号已上传，但未找到导入后的记录: {generated_name}",
                    )
                )
                continue
            success_count += 1
            details.append(
                _build_detail(
                    entry,
                    success=True,
                    copy_index=copy_index,
                    group_id=group_id,
                    group_name=group_name,
                    message=f"成功上传并绑定到 {group_name or f'Group {group_id}'}",
                )
            )
        return {
            "success_count": success_count,
            "failed_count": failed_count,
            "details": details,
        }
    except Exception as exc:
        logger.error("Sub2API group bind failed after import: %s", exc)
        error_text = f"账号已上传，但自动绑定分组失败: {str(exc)}"
        return {
            "success_count": 0,
            "failed_count": len(entries),
            "details": [
                _build_detail(
                    entry,
                    success=False,
                    copy_index=copy_index,
                    group_id=group_id,
                    group_name=group_name,
                    error=error_text,
                )
                for entry in entries
            ],
        }


def _perform_sub2api_upload(
    accounts: List[Account],
    api_url: Optional[str],
    api_key: Optional[str],
    concurrency: Optional[int] = None,
    priority: Optional[int] = None,
    team_context: Optional[dict] = None,
    service_id: Optional[int] = None,
    group_ids_override: Optional[List[int]] = None,
    dedupe: bool = False,
) -> Dict[str, Any]:
    results = {
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "details": [],
        "team_context": team_context,
        "account_total": len(accounts),
        "copy_total": 0,
    }
    if not accounts:
        return results

    uploadable_accounts = [acc for acc in accounts if acc.access_token]
    skipped_accounts = [acc for acc in accounts if not acc.access_token]
    for account in skipped_accounts:
        results["skipped_count"] += 1
        results["details"].append(
            {
                "id": account.id,
                "email": account.email,
                "success": False,
                "error": "缺少 access_token",
                "group_id": None,
                "group_name": None,
                "naming_identity": normalize_sub2api_identity(account.subscription_type),
                "generated_name": None,
                "copy_index": 0,
            }
        )
    if not uploadable_accounts:
        return results

    service = None
    template_config = normalize_sub2api_template_config(None)
    with get_db() as db:
        if service_id or not api_url or not api_key:
            service = resolve_sub2api_service(db, service_id)
            if service_id and not service:
                return {
                    **results,
                    "failed_count": len(uploadable_accounts),
                    "details": results["details"] + [
                        {
                            "id": acc.id,
                            "email": acc.email,
                            "success": False,
                            "error": "指定的 Sub2API 服务不存在",
                            "group_id": None,
                            "group_name": None,
                            "naming_identity": normalize_sub2api_identity(acc.subscription_type),
                            "generated_name": None,
                            "copy_index": 0,
                        }
                        for acc in uploadable_accounts
                    ],
                }
            if service:
                api_url = api_url or service.api_url
                api_key = api_key or service.api_key
                template_config = normalize_sub2api_template_config(service.template_config)

        if not api_url:
            error_text = "Sub2API URL 未配置"
            results["failed_count"] += len(uploadable_accounts)
            results["details"].extend(
                {
                    "id": acc.id,
                    "email": acc.email,
                    "success": False,
                    "error": error_text,
                    "group_id": None,
                    "group_name": None,
                    "naming_identity": normalize_sub2api_identity(acc.subscription_type),
                    "generated_name": None,
                    "copy_index": 0,
                }
                for acc in uploadable_accounts
            )
            return results
        if not api_key:
            error_text = "Sub2API API Key 未配置"
            results["failed_count"] += len(uploadable_accounts)
            results["details"].extend(
                {
                    "id": acc.id,
                    "email": acc.email,
                    "success": False,
                    "error": error_text,
                    "group_id": None,
                    "group_name": None,
                    "naming_identity": normalize_sub2api_identity(acc.subscription_type),
                    "generated_name": None,
                    "copy_index": 0,
                }
                for acc in uploadable_accounts
            )
            return results

        group_ids = _normalize_group_ids(group_ids_override or template_config.get("default_group_ids") or [])
        group_name_map = _load_group_name_map(api_url, api_key, group_ids)
        guard_result = evaluate_team_upload_guard(
            db,
            uploadable_accounts,
            platform="sub2api",
            team_context=team_context,
            service_id=service.id if service else service_id,
            selected_group_ids=group_ids,
        )
        blocked_details = list(guard_result.get("blocked_details") or [])
        if blocked_details:
            results["failed_count"] += len(blocked_details)
            results["details"].extend(blocked_details)
        uploadable_accounts = list(guard_result.get("allowed_accounts") or [])
        if dedupe and not team_context:
            deduped_accounts: List[Account] = []
            for account in uploadable_accounts:
                duplicate_detail = _resolve_sub2api_duplicate_detail(
                    account,
                    api_url=api_url,
                    api_key=api_key,
                    service_id=service.id if service else service_id,
                )
                if duplicate_detail:
                    duplicate_detail.update(
                        {
                            "group_id": None,
                            "group_name": None,
                            "naming_identity": normalize_sub2api_identity(account.subscription_type),
                            "generated_name": None,
                            "copy_index": 0,
                        }
                    )
                    results["skipped_count"] += 1
                    results["details"].append(duplicate_detail)
                    continue
                deduped_accounts.append(account)
            uploadable_accounts = deduped_accounts
        if not uploadable_accounts:
            return results
        upload_targets = [
            {
                "copy_index": index,
                "group_id": group_id,
                "group_name": group_name_map.get(group_id) or f"Group {group_id}",
            }
            for index, group_id in enumerate(group_ids, start=1)
        ] or [{"copy_index": 1, "group_id": None, "group_name": None}]

        if team_context:
            logger.info("Sub2API 上传携带 Team 上下文: %s", team_context.get("team_task_uuid"))

        for target in upload_targets:
            identity_counts = _count_naming_identities(uploadable_accounts, target["group_name"])
            indices_by_identity = reserve_sub2api_named_indices(
                db,
                service,
                identity_counts,
                group_id=target["group_id"],
            )
            entries = build_sub2api_named_accounts(
                uploadable_accounts,
                template_config=template_config,
                indices_by_identity=indices_by_identity,
                group_name=target["group_name"],
            )
            copy_result = _upload_copy(
                entries=entries,
                api_url=api_url,
                api_key=api_key,
                template_config=template_config,
                concurrency=concurrency,
                priority=priority,
                team_context=team_context,
                service_id=service.id if service else service_id,
                copy_index=target["copy_index"],
                group_id=target["group_id"],
                group_name=target["group_name"],
            )
            results["success_count"] += int(copy_result.get("success_count") or 0)
            results["failed_count"] += int(copy_result.get("failed_count") or 0)
            results["details"].extend(copy_result.get("details") or [])
            if team_context:
                account_lookup = {account.id: account for account in uploadable_accounts}
                for detail in copy_result.get("details") or []:
                    if not detail.get("success"):
                        continue
                    account_id = detail.get("id")
                    account = account_lookup.get(account_id)
                    if not account:
                        continue
                    record_team_upload_success(
                        db,
                        account,
                        platform="sub2api",
                        team_context=team_context,
                        service_id=service.id if service else service_id,
                    )

        if dedupe and not team_context:
            names_by_account_id: Dict[int, List[str]] = {}
            for detail in results["details"]:
                if not detail.get("success"):
                    continue
                try:
                    account_id = int(detail.get("id"))
                except (TypeError, ValueError):
                    continue
                generated_name = str(detail.get("generated_name") or "").strip()
                bucket = names_by_account_id.setdefault(account_id, [])
                if generated_name and generated_name not in bucket:
                    bucket.append(generated_name)

            account_lookup = {account.id: account for account in uploadable_accounts}
            for account_id, generated_names in names_by_account_id.items():
                account = account_lookup.get(account_id)
                if not account:
                    continue
                save_platform_upload_record(
                    db,
                    account,
                    "sub2api",
                    service_id=service.id if service else service_id,
                    api_url=api_url,
                    metadata={"generated_names": generated_names},
                )

    results["copy_total"] = sum(1 for detail in results["details"] if detail.get("generated_name"))
    return results


def prepare_sub2api_export_payload(db, accounts: List[Account], service_id: Optional[int] = None):
    service = resolve_sub2api_service(db, service_id)
    if not service:
        raise ValueError("未找到可用的 Sub2API 服务，请先在设置中配置")

    template_config = normalize_sub2api_template_config(service.template_config)
    default_group_ids = _normalize_group_ids(template_config.get("default_group_ids") or [])
    primary_group_id = default_group_ids[0] if default_group_ids else None
    group_name = _load_group_name_map(service.api_url, service.api_key, [primary_group_id]).get(primary_group_id) if primary_group_id else None
    identity_counts = _count_naming_identities(accounts, group_name)
    indices_by_identity = reserve_sub2api_named_indices(db, service, identity_counts, group_id=primary_group_id)
    named_accounts = build_sub2api_named_accounts(
        accounts,
        template_config=template_config,
        indices_by_identity=indices_by_identity,
        group_name=group_name,
    )
    payload = build_sub2api_export_payload(
        accounts,
        named_accounts=named_accounts,
        template_config=template_config,
    )
    return service, payload


def upload_to_sub2api(
    accounts: List[Account],
    api_url: Optional[str],
    api_key: Optional[str],
    concurrency: Optional[int] = None,
    priority: Optional[int] = None,
    team_context: Optional[dict] = None,
    service_id: Optional[int] = None,
    group_ids_override: Optional[List[int]] = None,
    target_type: str = "sub2api",
) -> Tuple[bool, str]:
    if not accounts:
        return False, "无可上传的账号"

    results = _perform_sub2api_upload(
        accounts,
        api_url,
        api_key,
        concurrency=concurrency,
        priority=priority,
        team_context=team_context,
        service_id=service_id,
        group_ids_override=group_ids_override,
    )
    success = int(results.get("failed_count") or 0) <= 0 and int(results.get("success_count") or 0) > 0
    copy_total = int(results.get("copy_total") or 0)
    account_total = len([acc for acc in accounts if acc.access_token])
    if success:
        if copy_total > account_total:
            return True, f"成功上传 {account_total} 个账号，生成 {copy_total} 份分组副本"
        return True, f"成功上传 {account_total} 个账号"

    errors = [
        str(detail.get("error") or "").strip()
        for detail in results.get("details", [])
        if not detail.get("success") and detail.get("error")
    ]
    error_text = "；".join(item for item in errors[:3] if item) or "上传失败"
    return False, error_text


def batch_upload_to_sub2api(
    account_ids: List[int],
    api_url: Optional[str],
    api_key: Optional[str],
    concurrency: Optional[int] = None,
    priority: Optional[int] = None,
    team_context: Optional[dict] = None,
    service_id: Optional[int] = None,
    group_ids_override: Optional[List[int]] = None,
    dedupe: bool = False,
) -> dict:
    results = {
        "success_count": 0,
        "failed_count": 0,
        "skipped_count": 0,
        "details": [],
        "team_context": team_context,
        "account_total": 0,
        "copy_total": 0,
    }

    with get_db() as db:
        accounts = []
        for account_id in account_ids:
            account = db.query(Account).filter(Account.id == account_id).first()
            if not account:
                results["failed_count"] += 1
                results["details"].append(
                    {
                        "id": account_id,
                        "email": None,
                        "success": False,
                        "error": "账号不存在",
                        "group_id": None,
                        "group_name": None,
                        "naming_identity": "Free",
                        "generated_name": None,
                        "copy_index": 0,
                    }
                )
                continue
            accounts.append(account)

    if not accounts:
        return results

    upload_results = _perform_sub2api_upload(
        accounts,
        api_url,
        api_key,
        concurrency=concurrency,
        priority=priority,
        team_context=team_context,
        service_id=service_id,
        group_ids_override=group_ids_override,
        dedupe=dedupe,
    )
    results.update(upload_results)
    return results


def test_sub2api_connection(api_url: str, api_key: str) -> Tuple[bool, str]:
    if not api_url:
        return False, "API URL 不能为空"
    if not api_key:
        return False, "API Key 不能为空"

    url = api_url.rstrip("/") + "/api/v1/admin/accounts/data"
    headers = {"x-api-key": api_key}
    try:
        response = cffi_requests.get(
            url,
            headers=headers,
            proxies=None,
            timeout=10,
            impersonate="chrome110",
        )
        if response.status_code in (200, 201, 204, 405):
            return True, "Sub2API 连接测试成功"
        if response.status_code == 401:
            return False, "连接成功，但 API Key 无效"
        if response.status_code == 403:
            return False, "连接成功，但权限不足"
        return False, f"服务器返回异常状态码: {response.status_code}"
    except cffi_requests.exceptions.ConnectionError as exc:
        return False, f"无法连接到服务器: {str(exc)}"
    except cffi_requests.exceptions.Timeout:
        return False, "连接超时，请检查网络配置"
    except Exception as exc:
        return False, f"连接测试失败: {str(exc)}"
