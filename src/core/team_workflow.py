"""
Team 创建与运行时状态工作流。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..config.constants import EmailServiceType, OPENAI_PAGE_TYPES
from ..config.settings import get_settings
from ..database import crud
from ..database.models import Account, EmailService, TeamMember, TeamTask
from ..database.session import get_db
from ..services import EmailServiceFactory
from ..web.task_manager import task_manager
from .openai.payment import check_subscription_status
from .openai.team_invitation import (
    accept_team_invitation_by_link,
    discover_team_account,
    extract_invitation_link,
    list_cookie_names,
    list_team_invites,
    list_team_members,
    refresh_member_team_token,
    resolve_session_token,
    serialize_cookie_store,
    send_team_invitation,
)
from .register import RegistrationEngine, RegistrationResult
from .upload.cpa_upload import batch_upload_to_cpa
from .upload.sub2api_upload import batch_upload_to_sub2api
from .upload.team_manager_upload import batch_upload_to_team_manager

logger = logging.getLogger(__name__)

TEAM_MEMBER_TOTAL = 5
TEAM_SUPPORTED_SERVICE_TYPES = (
    EmailServiceType.MOE_MAIL,
    EmailServiceType.FREEMAIL,
    EmailServiceType.TEMP_MAIL,
)
TEAM_RETRY_BACKOFF_SECONDS = (2, 4, 6, 8, 10)


class TeamCancelledError(RuntimeError):
    """Team task cancelled by user."""


class TeamOrchestrationError(RuntimeError):
    """Team workflow failed."""


class TeamSubscriptionPendingError(RuntimeError):
    """Team subscription is not ready yet."""


def get_supported_team_service_values() -> List[str]:
    return [item.value for item in TEAM_SUPPORTED_SERVICE_TYPES]


def is_supported_team_service(service_type: str) -> bool:
    return service_type in get_supported_team_service_values()


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

    if proxy_url and "proxy_url" not in normalized:
        normalized["proxy_url"] = proxy_url

    return normalized


def extract_email_domain_from_config(config: Optional[dict]) -> Optional[str]:
    config = config or {}
    domain = config.get("default_domain") or config.get("domain")
    if domain:
        return str(domain).lstrip("@")
    return None


def build_inbox_config(db, service_type: EmailServiceType, email: str) -> Optional[dict]:
    settings = get_settings()

    if service_type == EmailServiceType.TEMPMAIL:
        return {
            "base_url": settings.tempmail_base_url,
            "timeout": settings.tempmail_timeout,
            "max_retries": settings.tempmail_max_retries,
        }

    if service_type == EmailServiceType.MOE_MAIL:
        domain = email.split("@", 1)[1] if "@" in email else ""
        services = (
            db.query(EmailService)
            .filter(
                EmailService.service_type == EmailServiceType.MOE_MAIL.value,
                EmailService.enabled.is_(True),
            )
            .order_by(EmailService.priority.asc())
            .all()
        )
        selected = None
        for service in services:
            config = service.config or {}
            if config.get("default_domain") == domain or config.get("domain") == domain:
                selected = service
                break
        if not selected and services:
            selected = services[0]
        if not selected:
            return None
        return normalize_email_service_config(service_type, selected.config)

    selected = (
        db.query(EmailService)
        .filter(
            EmailService.service_type == service_type.value,
            EmailService.enabled.is_(True),
        )
        .order_by(EmailService.priority.asc())
        .first()
    )
    if not selected:
        return None
    return normalize_email_service_config(service_type, selected.config)


def has_recoverable_account_session(account: Optional[Account]) -> bool:
    if not account:
        return False
    return bool(resolve_session_token(account))


def _extract_service_email_address(email_info: Any) -> str:
    if isinstance(email_info, dict):
        raw_email = email_info.get("email")
        if isinstance(raw_email, dict):
            for key in ("address", "email"):
                value = str(raw_email.get(key) or "").strip()
                if value:
                    return value
        elif raw_email is not None:
            value = str(raw_email).strip()
            if value:
                return value

        for key in ("address", "email_address", "mailbox"):
            value = str(email_info.get(key) or "").strip()
            if value:
                return value
    elif email_info is not None:
        return str(email_info).strip()
    return ""


def _extract_service_email_id(email_info: Any, fallback: str = "") -> str:
    if isinstance(email_info, dict):
        for key in ("service_id", "id"):
            value = str(email_info.get(key) or "").strip()
            if value:
                return value
    return str(fallback or "").strip()


def _serialize_session_cookies(session: Any) -> str:
    return serialize_cookie_store(getattr(session, "cookies", None))


def ensure_account_email_mailbox(
    email_service,
    account: Account,
    callback_logger: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    target_email = str(account.email or "").strip()
    if not target_email:
        return {"success": False, "error": f"账号缺少邮箱地址，无法自动补登: {account.id}"}

    def _log(message: str):
        if callback_logger:
            callback_logger(message)

    target_email_lower = target_email.lower()
    email_id_hint = str(account.email_service_id or "").strip()
    try:
        known_emails = email_service.list_emails(limit=200)
    except Exception as exc:
        known_emails = []
        _log(f"查询邮箱服务现有地址失败，准备直接尝试重建 {target_email}: {exc}")

    for item in known_emails or []:
        candidate_email = _extract_service_email_address(item)
        if candidate_email.lower() != target_email_lower:
            continue
        email_info = {
            "email": candidate_email,
            "service_id": _extract_service_email_id(item, fallback=email_id_hint or candidate_email),
            "id": _extract_service_email_id(item, fallback=email_id_hint or candidate_email),
        }
        _log(f"已确认邮箱服务仍存在 {candidate_email}，继续用于自动补登")
        return {"success": True, "email_info": email_info, "created": False}

    local_part, _, domain = target_email.partition("@")
    create_config: Dict[str, Any] = {}
    if local_part:
        create_config["name"] = local_part
    if domain:
        create_config["domain"] = domain

    _log(f"邮箱服务中未找到 {target_email}，正在尝试按原地址重建")
    try:
        created = email_service.create_email(create_config)
    except Exception as exc:
        return {"success": False, "error": f"自动补登前重建邮箱失败: {target_email} - {exc}"}

    created_email = _extract_service_email_address(created)
    created_email_id = _extract_service_email_id(created, fallback=email_id_hint or created_email or target_email)
    if created_email.lower() != target_email_lower:
        return {
            "success": False,
            "error": f"邮箱服务未按原地址重建，期望 {target_email}，实际 {created_email or 'unknown'}",
        }

    return {
        "success": True,
        "email_info": {
            "email": created_email,
            "service_id": created_email_id,
            "id": created_email_id,
        },
        "created": True,
    }


def recover_account_session_via_login(
    account: Account,
    proxy_url: Optional[str] = None,
    callback_logger: Optional[Callable[[str], None]] = None,
    target_workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not account.email or not account.password:
        return {"success": False, "error": f"账号缺少邮箱或密码，无法自动补登: {account.email or account.id}"}

    try:
        service_type = EmailServiceType(account.email_service)
    except Exception:
        return {"success": False, "error": f"账号邮箱服务类型无效，无法自动补登: {account.email}"}

    with get_db() as db:
        inbox_config = build_inbox_config(db, service_type, account.email)
    if not inbox_config:
        return {"success": False, "error": f"无法构建邮箱收件配置，无法自动补登: {account.email}"}

    email_service = EmailServiceFactory.create(service_type, inbox_config)
    mailbox_state = ensure_account_email_mailbox(email_service, account, callback_logger=callback_logger)
    if not mailbox_state.get("success"):
        return {
            "success": False,
            "error": mailbox_state.get("error") or f"自动补登前检查邮箱失败: {account.email}",
        }

    engine = RegistrationEngine(
        email_service,
        proxy_url=proxy_url,
        callback_logger=callback_logger,
    )
    engine.email = account.email
    engine.password = account.password
    engine.email_info = mailbox_state["email_info"]
    engine._is_existing_account = True
    if target_workspace_id:
        engine._target_workspace_id = target_workspace_id

    did, sen_token = engine._prepare_authorize_flow("Team 自动补登")
    if not did:
        return {"success": False, "error": f"自动补登获取 Device ID 失败: {account.email}", "logs": engine.logs}
    if not sen_token:
        return {"success": False, "error": f"自动补登 Sentinel 校验失败: {account.email}", "logs": engine.logs}

    login_start_result = engine._submit_login_start(did, sen_token)
    if not login_start_result.success:
        return {
            "success": False,
            "error": login_start_result.error_message or f"自动补登提交邮箱失败: {account.email}",
            "logs": engine.logs,
        }
    if login_start_result.page_type != OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]:
        return {
            "success": False,
            "error": f"自动补登未进入密码页: {login_start_result.page_type or 'unknown'}",
            "logs": engine.logs,
        }

    password_result = engine._submit_login_password()
    if not password_result.success:
        return {
            "success": False,
            "error": password_result.error_message or f"自动补登提交密码失败: {account.email}",
            "logs": engine.logs,
        }
    if not password_result.is_existing_account:
        return {
            "success": False,
            "error": f"自动补登未进入验证码页: {password_result.page_type or 'unknown'}",
            "logs": engine.logs,
        }

    result = RegistrationResult(success=False, email=account.email, logs=engine.logs)
    if not engine._complete_token_exchange(result):
        return {
            "success": False,
            "error": result.error_message or f"自动补登 token 获取失败: {account.email}",
            "logs": engine.logs,
        }

    cookies = _serialize_session_cookies(getattr(engine, "session", None))
    session_token = result.session_token or resolve_session_token(Account(
        email=result.email or account.email,
        email_service=account.email_service,
        session_token=result.session_token or "",
        cookies=cookies,
    ))
    if not session_token and not result.access_token:
        cookie_names = list_cookie_names(getattr(getattr(engine, "session", None), "cookies", None))
        cookie_names_text = ", ".join(cookie_names) if cookie_names else "none"
        return {
            "success": False,
            "error": f"自动补登录成功，但未提取到可用 session_token 或 access_token: {account.email}；当前捕获到的 cookie 名称: {cookie_names_text}",
            "logs": engine.logs,
        }

    result.success = True
    return {
        "success": True,
        "email": result.email,
        "account_id": result.account_id,
        "workspace_id": result.workspace_id,
        "access_token": result.access_token,
        "refresh_token": result.refresh_token,
        "id_token": result.id_token,
        "session_token": session_token or "",
        "cookies": cookies,
        "email_service_id": engine.email_info.get("service_id"),
        "source": result.source,
        "logs": engine.logs,
    }


def build_account_summary(account: Optional[Account]) -> Optional[Dict[str, Any]]:
    if not account:
        return None
    return {
        "id": account.id,
        "email": account.email,
        "email_service": account.email_service,
        "password": account.password,
        "remark": account.remark,
        "status": account.status,
        "source": account.source,
        "account_id": account.account_id,
        "workspace_id": account.workspace_id,
        "subscription_type": account.subscription_type,
        "access_token": account.access_token,
        "session_token": account.session_token,
        "expires_at": account.expires_at.isoformat() if account.expires_at else None,
    }


def build_member_summary(member: TeamMember) -> Dict[str, Any]:
    account = member.account
    return {
        "id": member.id,
        "team_task_id": member.team_task_id,
        "account_id": member.account_id,
        "role": member.role,
        "order_index": member.order_index,
        "registration_task_uuid": member.registration_task_uuid,
        "invitation_status": member.invitation_status,
        "invitation_sent_at": member.invitation_sent_at.isoformat() if member.invitation_sent_at else None,
        "invitation_accepted_at": member.invitation_accepted_at.isoformat() if member.invitation_accepted_at else None,
        "created_at": member.created_at.isoformat() if member.created_at else None,
        "updated_at": member.updated_at.isoformat() if member.updated_at else None,
        "account": build_account_summary(account) if account else None,
    }


def build_team_response(task: TeamTask, runtime_status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    members = sorted(task.members or [], key=lambda item: item.order_index)
    stats = {
        "total_members": len(members),
        "registered_members": sum(1 for item in members if item.invitation_status in {"registered", "invited", "accepted", "uploaded"}),
        "invited_members": sum(1 for item in members if item.invitation_status in {"invited", "accepted", "uploaded"}),
        "accepted_members": sum(1 for item in members if item.invitation_status in {"accepted", "uploaded"}),
        "uploaded_members": sum(1 for item in members if item.invitation_status == "uploaded"),
        "failed_members": sum(1 for item in members if item.invitation_status == "failed"),
        "cancelled_members": sum(1 for item in members if item.invitation_status == "cancelled"),
    }
    runtime_status = runtime_status or {}
    return {
        "id": task.id,
        "task_uuid": task.task_uuid,
        "status": task.status,
        "email_service_id": task.email_service_id,
        "email_domain": task.email_domain,
        "proxy": task.proxy,
        "workspace_name": task.workspace_name,
        "team_account_id": task.team_account_id,
        "team_workspace_id": task.team_workspace_id,
        "continue_requested": bool(task.continue_requested_at),
        "upload_config": task.upload_config or {},
        "logs": task.logs,
        "error_message": task.error_message,
        "result": task.result or {},
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "main_account": build_account_summary(task.main_account) if task.main_account else None,
        "members": [build_member_summary(member) for member in members],
        "stats": stats,
        "retrying": bool(runtime_status.get("retrying")),
        "current_member_index": runtime_status.get("current_member_index"),
        "current_member_attempt": runtime_status.get("current_member_attempt"),
        "max_member_attempts": runtime_status.get("max_member_attempts"),
        "next_retry_in_seconds": runtime_status.get("next_retry_in_seconds"),
        "runtime_message": runtime_status.get("runtime_message"),
    }


def collect_email_candidates(payload: Any) -> List[str]:
    emails: List[str] = []
    pattern = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    def _visit(value: Any):
        if isinstance(value, dict):
            for item in value.values():
                _visit(item)
            return
        if isinstance(value, (list, tuple, set)):
            for item in value:
                _visit(item)
            return
        if isinstance(value, str):
            candidate = value.strip().lower()
            if pattern.match(candidate):
                emails.append(candidate)

    _visit(payload)
    return emails


def extract_full_email_content(message: Dict[str, Any], detail: Optional[Dict[str, Any]]) -> str:
    parts = [
        str(message.get("subject", "")),
        str(message.get("from", "")),
        str(message.get("content", "")),
    ]
    if detail:
        parts.extend([
            str(detail.get("subject", "")),
            str(detail.get("from", "")),
            str(detail.get("content", "")),
            str(detail.get("html", "")),
        ])
    return "\n".join(part for part in parts if part)


class TeamOrchestrator:
    def __init__(self, task_uuid: str):
        self.task_uuid = task_uuid

    def run_registration_phase(self):
        try:
            self._set_task_status("registering", started_at=datetime.utcnow(), error_message=None, completed_at=None)
            self._push_runtime_state(runtime_message="正在准备 Team 注册任务")
            team_task = self._get_task()
            if not team_task:
                raise TeamOrchestrationError("Team 任务不存在")

            _, email_service = self._build_registration_email_service(team_task)
            expected_domain = team_task.email_domain
            members = sorted(team_task.members or [], key=lambda item: item.order_index)
            if len(members) != TEAM_MEMBER_TOTAL:
                raise TeamOrchestrationError(f"成员数量异常，期望 {TEAM_MEMBER_TOTAL} 个，实际 {len(members)} 个")

            self._log("开始顺序注册 5 个同域名账号")
            registered_accounts: List[Dict[str, Any]] = []

            for member in members:
                self._raise_if_cancelled()
                label = self._member_label(member.order_index)
                self._log(f"{label} 开始注册")
                result, account = self._register_single_member(member, email_service)

                domain = account.email.split("@", 1)[1] if "@" in account.email else None
                if not expected_domain:
                    expected_domain = domain
                    self._update_task_fields(email_domain=expected_domain)
                elif domain and expected_domain and domain.lower() != expected_domain.lower():
                    raise TeamOrchestrationError(f"检测到不同域名: 期望 {expected_domain}，实际 {domain}")

                if member.order_index == 0:
                    self._update_task_fields(main_account_id=account.id)
                self._mark_account_as_team_created(
                    account_id=account.id,
                    member_id=member.id,
                    member_role=member.role,
                    member_order_index=member.order_index,
                    workspace_name=team_task.workspace_name,
                    email_domain=expected_domain,
                )
                self._update_member(member.id, account_id=account.id, invitation_status="registered")
                registered_accounts.append({
                    "member_id": member.id,
                    "order_index": member.order_index,
                    "role": member.role,
                    "account_id": account.id,
                    "email": account.email,
                })
                self._push_runtime_state(
                    current_member_index=member.order_index,
                    current_member_attempt=None,
                    retrying=False,
                    next_retry_in_seconds=None,
                    runtime_message=f"{label} 注册完成: {account.email}",
                )
                self._log(f"{label} 注册完成: {account.email}")

            result = self._merge_result({
                "registration": {
                    "completed": True,
                    "member_count": len(registered_accounts),
                    "accounts": registered_accounts,
                }
            })
            self._set_task_status("waiting_subscription", result=result, error_message=None)
            self._push_runtime_state(
                current_member_index=None,
                current_member_attempt=None,
                retrying=False,
                next_retry_in_seconds=None,
                runtime_message="5 个账号注册完成，等待 Team 订阅",
            )
            self._log("5 个账号注册完成，等待用户手动升级 Team 订阅")
            self._maybe_continue_after_registration()
        except TeamCancelledError:
            self._cancel_task("注册阶段已取消")
        except Exception as exc:
            logger.exception("Team 注册阶段失败: %s", self.task_uuid)
            self._fail_task(str(exc))

    def run_post_subscription_phase(self):
        try:
            team_task = self._get_task()
            if not team_task:
                raise TeamOrchestrationError("Team 任务不存在")
            if team_task.status == "waiting_subscription":
                self._set_task_status("verifying", error_message=None, completed_at=None)
                team_task = self._get_task()
            elif team_task.status not in {"verifying", "inviting", "accepting", "uploading"}:
                raise TeamOrchestrationError(f"当前状态不允许继续: {team_task.status}")
            if not team_task.members or not team_task.main_account:
                raise TeamOrchestrationError("主账号或成员信息缺失")

            main_account = team_task.main_account
            proxy = team_task.proxy
            self._push_runtime_state(runtime_message="正在校验主账号 Team 订阅状态")
            self._log("开始校验主账号 Team 订阅状态")
            self._raise_if_cancelled()

            subscription_type = check_subscription_status(main_account, proxy)
            if subscription_type != "team":
                raise TeamSubscriptionPendingError(f"主账号尚未升级 Team，当前状态: {subscription_type}")

            discovery = discover_team_account(main_account, proxy)
            if not discovery.get("success"):
                raise TeamOrchestrationError(discovery.get("error") or "无法发现 team_account_id")

            team_account = discovery["account"]
            team_account_id = str(team_account.get("team_account_id") or "").strip()
            if not team_account_id:
                raise TeamOrchestrationError("discover_team_account 未返回 team_account_id")

            self._update_task_fields(
                team_account_id=team_account_id,
                team_workspace_id=team_account.get("team_workspace_id"),
            )
            self._refresh_account_team_context(main_account.id, team_account_id, is_main=True)
            self._merge_result({
                "team": {
                    "team_account_id": team_account_id,
                    "team_workspace_id": team_account.get("team_workspace_id"),
                    "subscription_plan": team_account.get("subscription_plan"),
                    "has_active_subscription": team_account.get("has_active_subscription"),
                }
            })

            self._set_task_status("inviting")
            self._push_runtime_state(runtime_message=f"Team 已确认，开始发送邀请: {team_account_id}")
            self._log(f"已确认 Team 订阅，team_account_id={team_account_id}")
            for member in sorted(team_task.members, key=lambda item: item.order_index):
                if member.order_index == 0:
                    continue
                self._raise_if_cancelled()
                self._invite_member(main_account, member, team_account_id, proxy)

            self._set_task_status("accepting")
            self._push_runtime_state(runtime_message="邀请发送完成，开始自动接受")
            for member in sorted(team_task.members, key=lambda item: item.order_index):
                if member.order_index == 0:
                    continue
                self._raise_if_cancelled()
                self._accept_member_invitation(member, team_account_id, proxy)

            self._refresh_account_team_context(main_account.id, team_account_id, is_main=True)
            self._set_task_status("uploading")
            self._push_runtime_state(runtime_message="成员已加入 Team，开始上传到平台")
            upload_results = self._upload_team_payload(team_account_id)
            if any(item.get("failed_count", 0) > 0 for item in upload_results.values() if isinstance(item, dict)):
                raise TeamOrchestrationError("上传阶段存在失败结果，请查看详情")

            with get_db() as db:
                task = crud.get_team_task(db, self.task_uuid)
                if task:
                    for member in task.members:
                        if member.invitation_status in {"accepted", "registered", "invited"}:
                            member.invitation_status = "uploaded"
                    db.commit()

            result = self._merge_result({"upload": upload_results})
            self._set_task_status("completed", result=result, completed_at=datetime.utcnow(), error_message=None)
            self._push_runtime_state(
                current_member_index=None,
                current_member_attempt=None,
                retrying=False,
                next_retry_in_seconds=None,
                runtime_message="Team 创建流程已完成",
            )
            self._log("Team 创建流程已完成")
        except TeamSubscriptionPendingError as exc:
            self._set_task_status("waiting_subscription", error_message=None, completed_at=None)
            self._push_runtime_state(
                current_member_index=None,
                current_member_attempt=None,
                retrying=False,
                next_retry_in_seconds=None,
                runtime_message=str(exc),
            )
            self._log(str(exc))
        except TeamCancelledError:
            self._cancel_task("订阅确认后阶段已取消")
        except Exception as exc:
            logger.exception("Team 第二阶段失败: %s", self.task_uuid)
            self._fail_task(str(exc))

    def run_manual_upload(self, upload_config_override: Optional[Dict[str, Any]] = None):
        try:
            team_task = self._get_task()
            if not team_task:
                raise TeamOrchestrationError("Team 任务不存在")
            if team_task.status in {"registering", "verifying", "inviting", "accepting"}:
                raise TeamOrchestrationError(f"当前状态 {team_task.status}，暂时无法单独上传")
            if not team_task.team_account_id:
                raise TeamOrchestrationError("Team 创建尚未完成，暂时无法上传到平台")

            upload_config = upload_config_override or team_task.upload_config or {}
            if not any(
                upload_config.get(flag)
                for flag in ("auto_upload_sub2api", "auto_upload_cpa", "auto_upload_tm")
            ):
                raise TeamOrchestrationError("请至少选择一个上传平台")

            self._set_task_status(
                "uploading",
                upload_config=upload_config,
                error_message=None,
                completed_at=None,
            )
            self._push_runtime_state(
                current_member_index=None,
                current_member_attempt=None,
                retrying=False,
                next_retry_in_seconds=None,
                runtime_message="正在手动上传 Team 账号到平台",
            )
            self._log("开始手动上传 Team 账号到平台")

            upload_results = self._upload_team_payload(team_task.team_account_id)
            if any(item.get("failed_count", 0) > 0 for item in upload_results.values() if isinstance(item, dict)):
                raise TeamOrchestrationError("手动上传阶段存在失败结果，请查看详情")

            with get_db() as db:
                task = crud.get_team_task(db, self.task_uuid)
                if task:
                    for member in task.members:
                        if member.invitation_status in {"accepted", "registered", "invited"}:
                            member.invitation_status = "uploaded"
                    db.commit()

            result = self._merge_result({"upload": upload_results})
            self._set_task_status("completed", result=result, completed_at=datetime.utcnow(), error_message=None)
            self._push_runtime_state(
                current_member_index=None,
                current_member_attempt=None,
                retrying=False,
                next_retry_in_seconds=None,
                runtime_message="Team 账号已完成手动上传",
            )
            self._log("Team 账号已完成手动上传")
        except TeamCancelledError:
            self._cancel_task("手动上传阶段已取消")
        except Exception as exc:
            logger.exception("Team 手动上传失败: %s", self.task_uuid)
            self._fail_task(str(exc))

    def _register_single_member(
        self,
        member: TeamMember,
        email_service,
    ) -> Tuple[Any, Account]:
        task_label = self._member_label(member.order_index)
        max_attempts = self._get_max_member_attempts()

        with get_db() as db:
            crud.update_registration_task(
                db,
                member.registration_task_uuid,
                status="running",
                started_at=datetime.utcnow(),
                completed_at=None,
                error_message=None,
            )

        def callback(message: str):
            self._log(f"[{task_label}] {message}")

        for attempt in range(1, max_attempts + 1):
            self._raise_if_cancelled()
            self._push_runtime_state(
                current_member_index=member.order_index,
                current_member_attempt=attempt,
                retrying=False,
                next_retry_in_seconds=None,
                runtime_message=f"{task_label} 第 {attempt}/{max_attempts} 次注册尝试",
            )
            self._log(f"{task_label} 第 {attempt}/{max_attempts} 次注册尝试")

            result = None
            try:
                engine = RegistrationEngine(
                    email_service=email_service,
                    proxy_url=self._get_task_proxy(),
                    callback_logger=callback,
                    task_uuid=member.registration_task_uuid,
                )
                result = engine.run()
                if not result.success:
                    raise TeamOrchestrationError(result.error_message or f"{task_label} 注册失败")
                if not engine.save_to_database(result):
                    raise TeamOrchestrationError("保存注册结果到数据库失败")

                with get_db() as db:
                    account = crud.get_account_by_email(db, result.email)
                    crud.update_registration_task(
                        db,
                        member.registration_task_uuid,
                        status="completed",
                        completed_at=datetime.utcnow(),
                        error_message=None,
                        result=result.to_dict(),
                    )
                    if not account:
                        raise TeamOrchestrationError(f"账号已注册但数据库中未找到: {result.email}")
                    db.refresh(account)
                return result, account
            except TeamCancelledError:
                raise
            except Exception as exc:
                last_error = str(exc)
                result_payload = result.to_dict() if result and hasattr(result, "to_dict") else None
                if attempt < max_attempts:
                    wait_seconds = self._get_retry_wait_seconds(attempt)
                    with get_db() as db:
                        crud.update_registration_task(
                            db,
                            member.registration_task_uuid,
                            status="running",
                            error_message=last_error,
                            result=result_payload,
                        )
                    self._log(f"{task_label} 第 {attempt}/{max_attempts} 次失败: {last_error}")
                    self._log(f"{task_label} 将在 {wait_seconds} 秒后自动重试")
                    self._push_runtime_state(
                        current_member_index=member.order_index,
                        current_member_attempt=attempt,
                        retrying=True,
                        next_retry_in_seconds=wait_seconds,
                        runtime_message=f"{task_label} 自动重试中: {last_error}",
                    )
                    if not self._wait_for_retry_or_cancel(wait_seconds):
                        raise TeamCancelledError(self.task_uuid)
                    continue

                with get_db() as db:
                    crud.update_registration_task(
                        db,
                        member.registration_task_uuid,
                        status="failed",
                        completed_at=datetime.utcnow(),
                        error_message=last_error,
                        result=result_payload,
                    )
                self._update_member(member.id, invitation_status="failed")
                self._push_runtime_state(
                    current_member_index=member.order_index,
                    current_member_attempt=attempt,
                    retrying=False,
                    next_retry_in_seconds=None,
                    runtime_message=f"{task_label} 注册失败: {last_error}",
                )
                raise TeamOrchestrationError(last_error) from exc

        raise TeamOrchestrationError(f"{task_label} 注册失败")

    def _invite_member(
        self,
        admin_account: Account,
        member: TeamMember,
        team_account_id: str,
        proxy: Optional[str],
    ):
        member = self._reload_member(member.id)
        if not member or not member.account:
            raise TeamOrchestrationError("成员账号不存在，无法发送邀请")

        email = member.account.email
        self._log(f"发送邀请给 {email}")
        before_invites = self._read_team_email_snapshot(admin_account, team_account_id, proxy, source="invites")
        before_members = self._read_team_email_snapshot(admin_account, team_account_id, proxy, source="members")
        success, message = send_team_invitation(admin_account, team_account_id, email, proxy)
        if not success:
            self._update_member(member.id, invitation_status="failed")
            raise TeamOrchestrationError(message)

        time.sleep(2)
        after_invites = self._read_team_email_snapshot(admin_account, team_account_id, proxy, source="invites")
        after_members = self._read_team_email_snapshot(admin_account, team_account_id, proxy, source="members")
        email_lower = email.lower()
        in_members = email_lower in after_members
        in_invites = email_lower in after_invites

        if not in_members and not in_invites:
            self._update_member(member.id, invitation_status="failed")
            raise TeamOrchestrationError(f"邀请接口返回成功，但未在成员/邀请列表中看到 {email}，判定为假成功")

        self._update_member(member.id, invitation_status="invited", invitation_sent_at=datetime.utcnow())
        self._merge_result({
            "invites": {
                email: {
                    "before_invites": sorted(before_invites),
                    "before_members": sorted(before_members),
                    "after_invites": sorted(after_invites),
                    "after_members": sorted(after_members),
                    "status": "member_exists" if in_members else "invited",
                }
            }
        })
        self._push_runtime_state(runtime_message=f"邀请已确认写入列表: {email}")
        self._log(f"邀请已确认写入列表: {email}")

    def _accept_member_invitation(
        self,
        member: TeamMember,
        team_account_id: str,
        proxy: Optional[str],
    ):
        member = self._reload_member(member.id)
        if not member or not member.account:
            raise TeamOrchestrationError("成员账号不存在，无法接受邀请")

        account = member.account
        self._log(f"等待 {account.email} 的邀请邮件")
        invitation_link = self._wait_for_invitation_link(account)
        if not invitation_link:
            self._update_member(member.id, invitation_status="failed")
            raise TeamOrchestrationError(f"未能在邮箱中读取到 {account.email} 的 Team 邀请链接")

        success, message = accept_team_invitation_by_link(account, invitation_link, proxy)
        if not success:
            self._update_member(member.id, invitation_status="failed")
            raise TeamOrchestrationError(message)

        self._refresh_account_team_context(account.id, team_account_id, is_main=False)
        self._update_member(member.id, invitation_status="accepted", invitation_accepted_at=datetime.utcnow())
        self._push_runtime_state(runtime_message=f"{account.email} 已接受 Team 邀请")
        self._log(f"{account.email} 已接受 Team 邀请")

    def _wait_for_invitation_link(self, account: Account, timeout: int = 180, interval: int = 5) -> Optional[str]:
        mailbox_id = account.email_service_id or account.email
        if not mailbox_id:
            return None

        deadline = time.time() + timeout
        while time.time() < deadline:
            self._raise_if_cancelled()
            with get_db() as db:
                service_type = EmailServiceType(account.email_service)
                inbox_config = build_inbox_config(db, service_type, account.email)
            if not inbox_config:
                raise TeamOrchestrationError(f"无法构建收件配置: {account.email}")

            service = EmailServiceFactory.create(EmailServiceType(account.email_service), inbox_config)
            messages = service.get_email_messages(mailbox_id, limit=20)
            for message in messages:
                detail = None
                message_id = message.get("id")
                if message_id:
                    try:
                        detail = service.get_message_content(mailbox_id, message_id)
                    except Exception:
                        detail = None
                content = extract_full_email_content(message, detail)
                candidate = extract_invitation_link(content)
                if candidate:
                    return candidate
            time.sleep(interval)

        return None

    def _refresh_account_team_context(self, account_id: int, team_account_id: str, is_main: bool):
        with get_db() as db:
            account = crud.get_account_by_id(db, account_id)
            if not account:
                raise TeamOrchestrationError(f"账号不存在: {account_id}")

            if not has_recoverable_account_session(account):
                self._log(f"{account.email} 缺少可用会话，正在尝试自动补登恢复 Team 会话")
                recovery = recover_account_session_via_login(account, proxy_url=self._get_task_proxy(), callback_logger=self._log)
                if not recovery.get("success"):
                    raise TeamOrchestrationError(
                        recovery.get("error") or f"账号缺少 session_token，无法切换到 Team 空间，请重新登录后重试: {account.email}"
                    )
                recovered_account_id = recovery.get("account_id")
                if recovered_account_id:
                    account.account_id = recovered_account_id
                crud.update_account(
                    db,
                    account.id,
                    access_token=recovery.get("access_token"),
                    refresh_token=recovery.get("refresh_token"),
                    id_token=recovery.get("id_token"),
                    session_token=recovery.get("session_token"),
                    cookies=recovery.get("cookies"),
                    email_service_id=recovery.get("email_service_id"),
                    workspace_id=recovery.get("workspace_id"),
                    source=recovery.get("source") or "login",
                    status="active",
                    last_refresh=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
                account = crud.get_account_by_id(db, account_id)
                if not account:
                    raise TeamOrchestrationError(f"账号不存在: {account_id}")

            updates: Dict[str, Any] = {
                "workspace_id": team_account_id,
                "subscription_type": "team",
                "subscription_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
            refresh_result = refresh_member_team_token(account, team_account_id, self._get_task_proxy())
            if not refresh_result.get("success"):
                raise TeamOrchestrationError(refresh_result.get("error") or f"刷新 Team token 失败: {account.email}")
            updates.update({
                "account_id": refresh_result.get("account_id") or team_account_id,
                "access_token": refresh_result.get("access_token"),
                "session_token": refresh_result.get("session_token"),
                "expires_at": refresh_result.get("expires_at"),
                "last_refresh": datetime.utcnow(),
            })

            refreshed_team_account_id = updates.pop("account_id", None)
            if refreshed_team_account_id:
                account.account_id = refreshed_team_account_id
            crud.update_account(db, account.id, **updates)
            refreshed_account_id = account.id
        if is_main:
            self._update_task_fields(main_account_id=refreshed_account_id)

    def _validate_accounts_ready_for_team_upload(self, db, account_ids: Sequence[int], team_account_id: str):
        invalid_accounts: List[str] = []
        for account_id in account_ids:
            account = crud.get_account_by_id(db, account_id)
            if not account:
                invalid_accounts.append(f"#{account_id}: missing")
                continue
            if account.workspace_id != team_account_id or account.subscription_type != "team":
                invalid_accounts.append(f"{account.email}: workspace={account.workspace_id}, plan={account.subscription_type}")
                continue
            if not account.access_token or account.account_id != team_account_id:
                invalid_accounts.append(f"{account.email}: missing team token/account_id")

        if invalid_accounts:
            joined = "; ".join(invalid_accounts[:5])
            raise TeamOrchestrationError(f"以下账号尚未切换到目标 Team 空间，无法上传: {joined}")

    def _upload_team_payload(self, team_account_id: str) -> Dict[str, Any]:
        with get_db() as db:
            task = crud.get_team_task(db, self.task_uuid)
            if not task:
                raise TeamOrchestrationError("任务不存在，无法上传")
            members = sorted(task.members or [], key=lambda item: item.order_index)
            account_ids = [member.account_id for member in members if member.account_id]
            self._validate_accounts_ready_for_team_upload(db, account_ids, team_account_id)
            team_context = {
                "team_task_uuid": task.task_uuid,
                "team_account_id": team_account_id,
                "team_workspace_id": task.team_workspace_id,
                "workspace_name": task.workspace_name,
                "email_domain": task.email_domain,
                "main_account_id": task.main_account_id,
                "members": [
                    {
                        "team_member_id": member.id,
                        "account_id": member.account_id,
                        "role": member.role,
                        "order_index": member.order_index,
                    }
                    for member in members
                ],
            }
            upload_config = task.upload_config or {}
            results: Dict[str, Any] = {"team_context": team_context}

            if upload_config.get("auto_upload_sub2api"):
                services = self._select_services(
                    db,
                    upload_config.get("sub2api_service_ids", []),
                    crud.get_sub2api_service_by_id,
                    lambda session: crud.get_sub2api_services(session, enabled=True),
                    "Sub2API",
                )
                platform_results = []
                for service in services:
                    self._raise_if_cancelled()
                    self._log(f"上传到 Sub2API: {service.name}")
                    platform_results.append(
                        batch_upload_to_sub2api(
                            account_ids,
                            api_url=service.api_url,
                            api_key=service.api_key,
                            team_context=team_context,
                            service_id=service.id,
                        )
                    )
                results["sub2api"] = self._collapse_platform_results(platform_results)

            if upload_config.get("auto_upload_cpa"):
                services = self._select_services(
                    db,
                    upload_config.get("cpa_service_ids", []),
                    crud.get_cpa_service_by_id,
                    lambda session: crud.get_cpa_services(session, enabled=True),
                    "CPA",
                )
                platform_results = []
                for service in services:
                    self._raise_if_cancelled()
                    self._log(f"上传到 CPA: {service.name}")
                    platform_results.append(
                        batch_upload_to_cpa(
                            account_ids,
                            api_url=service.api_url,
                            api_token=service.api_token,
                            team_context=team_context,
                            service_id=service.id,
                        )
                    )
                results["cpa"] = self._collapse_platform_results(platform_results)

            if upload_config.get("auto_upload_tm"):
                services = self._select_services(
                    db,
                    upload_config.get("tm_service_ids", []),
                    crud.get_tm_service_by_id,
                    lambda session: crud.get_tm_services(session, enabled=True),
                    "Team Manager",
                )
                platform_results = []
                for service in services:
                    self._raise_if_cancelled()
                    self._log(f"上传到 Team Manager: {service.name}")
                    platform_results.append(
                        batch_upload_to_team_manager(
                            account_ids,
                            api_url=service.api_url,
                            api_key=service.api_key,
                            team_context=team_context,
                            service_id=service.id,
                        )
                    )
                results["tm"] = self._collapse_platform_results(platform_results)

            if len(results) == 1:
                self._log("未启用任何自动上传平台，上传阶段按空操作完成")
                results["noop"] = {
                    "success_count": len(account_ids),
                    "failed_count": 0,
                    "skipped_count": 0,
                    "details": [],
                }

            merge_result = self._merge_result({
                "team_context": team_context,
                "upload": results,
            })
            self._update_task_fields(result=merge_result)
            return results

    def _select_services(
        self,
        db,
        selected_ids: Sequence[int],
        getter,
        list_getter,
        platform_name: str,
    ) -> List[Any]:
        if selected_ids:
            services = []
            for service_id in selected_ids:
                service = getter(db, service_id)
                if service and getattr(service, "enabled", True):
                    services.append(service)
            if services:
                return services
            raise TeamOrchestrationError(f"未找到可用的 {platform_name} 服务")

        services = list_getter(db)
        if not services:
            raise TeamOrchestrationError(f"未配置可用的 {platform_name} 服务")
        return [services[0]]

    @staticmethod
    def _collapse_platform_results(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        merged = {
            "success_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "details": [],
            "services": list(results),
        }
        for item in results:
            merged["success_count"] += item.get("success_count", 0)
            merged["failed_count"] += item.get("failed_count", 0)
            merged["skipped_count"] += item.get("skipped_count", 0)
            details = list(item.get("details", []))
            merged["details"].extend(details)
            guard_blocked = sum(1 for detail in details if detail.get("guard_blocked") and not detail.get("success"))
            if guard_blocked:
                merged["failed_count"] = max(0, merged["failed_count"] - guard_blocked)
                merged["skipped_count"] += guard_blocked
        return merged

    def _read_team_email_snapshot(
        self,
        admin_account: Account,
        team_account_id: str,
        proxy: Optional[str],
        *,
        source: str,
    ) -> set[str]:
        if source == "invites":
            response = list_team_invites(admin_account, team_account_id, proxy)
            items = response.get("items", [])
        else:
            response = list_team_members(admin_account, team_account_id, proxy)
            items = response.get("members", [])
        return set(collect_email_candidates(items))

    def _build_registration_email_service(self, team_task: TeamTask) -> Tuple[EmailServiceType, Any]:
        with get_db() as db:
            email_service = crud.get_email_service_by_id(db, team_task.email_service_id)
            if not email_service or not email_service.enabled:
                raise TeamOrchestrationError("邮箱服务不存在或已禁用")
            service_type = EmailServiceType(email_service.service_type)
            if service_type not in TEAM_SUPPORTED_SERVICE_TYPES:
                raise TeamOrchestrationError(f"不支持的 Team 邮箱服务类型: {service_type.value}")
            config = normalize_email_service_config(service_type, email_service.config, team_task.proxy)
        return service_type, EmailServiceFactory.create(service_type, config)

    def _get_task(self) -> Optional[TeamTask]:
        with get_db() as db:
            task = crud.get_team_task(db, self.task_uuid)
            if not task:
                return None
            task.members
            if task.main_account:
                task.main_account.email
            return task

    def _get_task_proxy(self) -> Optional[str]:
        with get_db() as db:
            task = crud.get_team_task(db, self.task_uuid)
            return task.proxy if task else None

    def _reload_member(self, member_id: int) -> Optional[TeamMember]:
        with get_db() as db:
            member = crud.get_team_member_by_id(db, member_id)
            if member and member.account:
                member.account.email
            return member

    def _set_task_status(self, status: str, **kwargs):
        self._update_task_fields(status=status, **kwargs)
        self._sync_status_snapshot()

    def _update_task_fields(self, **kwargs):
        with get_db() as db:
            crud.update_team_task(db, self.task_uuid, **kwargs)

    def _update_member(self, member_id: int, **kwargs):
        with get_db() as db:
            crud.update_team_member(db, member_id, **kwargs)

    def _mark_account_as_team_created(
        self,
        *,
        account_id: int,
        member_id: int,
        member_role: str,
        member_order_index: int,
        workspace_name: str,
        email_domain: Optional[str],
    ):
        with get_db() as db:
            account = crud.get_account_by_id(db, account_id)
            if not account:
                return

            extra_data = account.extra_data.copy() if account.extra_data else {}
            extra_data.update({
                "team_task_uuid": self.task_uuid,
                "team_member_id": member_id,
                "team_role": member_role,
                "team_order_index": member_order_index,
                "team_email_domain": email_domain,
                "team_workspace_name": workspace_name,
                "is_team_created_account": True,
            })
            crud.update_account(
                db,
                account.id,
                source="team_create",
                extra_data=extra_data,
            )

    def _merge_result(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        with get_db() as db:
            task = crud.get_team_task(db, self.task_uuid)
            if not task:
                raise TeamOrchestrationError("任务不存在，无法更新结果")
            result = task.result.copy() if task.result else {}
            for key, value in updates.items():
                if isinstance(value, dict) and isinstance(result.get(key), dict):
                    result[key].update(value)
                else:
                    result[key] = value
            crud.update_team_task(db, self.task_uuid, result=result)
            return result

    def _log(self, message: str):
        if not re.match(r"^\[\d{2}:\d{2}:\d{2}\]", message):
            timestamp = datetime.now().strftime("%H:%M:%S")
            message = f"[{timestamp}] {message}"
        task_manager.add_log(self.task_uuid, message)
        with get_db() as db:
            crud.append_team_task_log(db, self.task_uuid, message)

    def _sync_status_snapshot(self, **runtime_overrides):
        with get_db() as db:
            task = crud.get_team_task(db, self.task_uuid)
            if not task:
                return
            runtime_status = task_manager.get_status(self.task_uuid) or {}
            runtime_payload = self._build_runtime_payload(runtime_status, runtime_overrides)
            snapshot = build_team_response(task, runtime_payload)
        task_manager.update_status(self.task_uuid, snapshot["status"], snapshot=snapshot, **runtime_payload)

    def _push_runtime_state(self, **runtime_overrides):
        self._sync_status_snapshot(**runtime_overrides)

    def _build_runtime_payload(self, runtime_status: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "retrying": overrides.get("retrying", runtime_status.get("retrying", False)),
            "current_member_index": overrides.get("current_member_index", runtime_status.get("current_member_index")),
            "current_member_attempt": overrides.get("current_member_attempt", runtime_status.get("current_member_attempt")),
            "max_member_attempts": overrides.get("max_member_attempts", runtime_status.get("max_member_attempts", self._get_max_member_attempts())),
            "next_retry_in_seconds": overrides.get("next_retry_in_seconds", runtime_status.get("next_retry_in_seconds")),
            "runtime_message": overrides.get("runtime_message", runtime_status.get("runtime_message")),
        }

    def _raise_if_cancelled(self):
        if task_manager.is_cancelled(self.task_uuid):
            raise TeamCancelledError(self.task_uuid)

    def _cancel_task(self, message: str):
        with get_db() as db:
            task = crud.get_team_task(db, self.task_uuid)
            if not task:
                return
            for member in task.members:
                if member.invitation_status not in {"accepted", "uploaded", "failed"}:
                    member.invitation_status = "cancelled"
            task.status = "cancelled"
            task.completed_at = datetime.utcnow()
            task.error_message = message
            db.commit()
        self._log(message)
        self._sync_status_snapshot(
            retrying=False,
            current_member_index=None,
            current_member_attempt=None,
            next_retry_in_seconds=None,
            runtime_message=message,
        )

    def _fail_task(self, error_message: str):
        with get_db() as db:
            task = crud.get_team_task(db, self.task_uuid)
            if not task:
                return
            task.status = "failed"
            task.error_message = error_message
            task.completed_at = datetime.utcnow()
            db.commit()
        self._log(f"任务失败: {error_message}")
        self._sync_status_snapshot(
            retrying=False,
            current_member_index=None,
            current_member_attempt=None,
            next_retry_in_seconds=None,
            runtime_message=error_message,
        )

    def _maybe_continue_after_registration(self):
        task = self._get_task()
        if not task or not task.continue_requested_at:
            return
        self._log("检测到已点击继续，注册完成后自动进入订阅校验阶段")
        self.run_post_subscription_phase()

    @staticmethod
    def _member_label(order_index: int) -> str:
        return "主账号" if order_index == 0 else f"成员 {order_index}"

    def _wait_for_retry_or_cancel(self, wait_seconds: int) -> bool:
        deadline = time.monotonic() + max(0, wait_seconds)
        while time.monotonic() < deadline:
            if task_manager.is_cancelled(self.task_uuid):
                return False
            remaining = deadline - time.monotonic()
            time.sleep(min(0.5, max(remaining, 0)))
        return not task_manager.is_cancelled(self.task_uuid)

    @staticmethod
    def _get_max_member_attempts() -> int:
        retries = max(0, get_settings().registration_max_retries)
        return retries + 1

    @staticmethod
    def _get_retry_wait_seconds(attempt: int) -> int:
        index = min(max(attempt - 1, 0), len(TEAM_RETRY_BACKOFF_SECONDS) - 1)
        return TEAM_RETRY_BACKOFF_SECONDS[index]
