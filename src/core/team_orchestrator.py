"""
Team 创建两阶段编排服务。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..config.constants import EmailServiceType
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
    list_team_invites,
    list_team_members,
    refresh_member_team_token,
    send_team_invitation,
)
from .register import RegistrationEngine
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
TERMINAL_TASK_STATUSES = {"completed", "failed", "cancelled"}
TEAM_RETRY_BACKOFF_SECONDS = (2, 4, 6, 8, 10)


class TeamCancelledError(RuntimeError):
    """Team 任务被用户取消。"""


class TeamOrchestrationError(RuntimeError):
    """Team 编排失败。"""


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


def build_account_summary(account: Optional[Account]) -> Optional[Dict[str, Any]]:
    if not account:
        return None
    return {
        "id": account.id,
        "email": account.email,
        "password": account.password,
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
            team_task = self._get_task()
            if not team_task:
                raise TeamOrchestrationError("Team 任务不存在")

            service_type, email_service = self._build_registration_email_service(team_task)
            expected_domain = team_task.email_domain
            members = sorted(team_task.members or [], key=lambda item: item.order_index)
            if len(members) != TEAM_MEMBER_TOTAL:
                raise TeamOrchestrationError(f"成员数量异常，期望 {TEAM_MEMBER_TOTAL} 个，实际 {len(members)} 个")

            self._log("开始顺序注册 5 个同域名账号")
            registered_accounts: List[Dict[str, Any]] = []

            for member in members:
                self._raise_if_cancelled()
                label = "主账号" if member.order_index == 0 else f"成员 {member.order_index}"
                self._log(f"{label} 开始注册")
                result, account = self._register_single_member(member, email_service, service_type)
                if not result.success or not account:
                    raise TeamOrchestrationError(result.error_message or f"{label} 注册失败")

                domain = account.email.split("@", 1)[1] if "@" in account.email else None
                if not expected_domain:
                    expected_domain = domain
                    self._update_task_fields(email_domain=expected_domain)
                elif domain and expected_domain and domain.lower() != expected_domain.lower():
                    raise TeamOrchestrationError(f"检测到不同域名: 期望 {expected_domain}，实际 {domain}")

                if member.order_index == 0:
                    self._update_task_fields(main_account_id=account.id)
                self._update_member(member.id, account_id=account.id, invitation_status="registered")

                registered_accounts.append({
                    "member_id": member.id,
                    "order_index": member.order_index,
                    "role": member.role,
                    "account_id": account.id,
                    "email": account.email,
                })
                self._sync_status_snapshot()
                self._log(f"{label} 注册完成: {account.email}")

            result = self._merge_result({
                "registration": {
                    "completed": True,
                    "member_count": len(registered_accounts),
                    "accounts": registered_accounts,
                }
            })
            self._set_task_status("waiting_subscription", result=result, error_message=None)
            self._log("5 个账号注册完成，等待用户手动升级 Team 订阅")
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
            if team_task.status not in {"verifying", "inviting", "accepting", "uploading"}:
                raise TeamOrchestrationError(f"当前状态不允许继续: {team_task.status}")
            if not team_task.members or not team_task.main_account:
                raise TeamOrchestrationError("主账号或成员信息缺失")

            main_account = team_task.main_account
            proxy = team_task.proxy
            self._log("开始校验主账号 Team 订阅状态")
            self._raise_if_cancelled()

            subscription_type = check_subscription_status(main_account, proxy)
            if subscription_type != "team":
                raise TeamOrchestrationError(f"主账号尚未升级 Team，当前状态: {subscription_type}")

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
            self._log(f"已确认 Team 订阅，team_account_id={team_account_id}")
            for member in sorted(team_task.members, key=lambda item: item.order_index):
                if member.order_index == 0:
                    continue
                self._raise_if_cancelled()
                self._invite_member(main_account, member, team_account_id, proxy)

            self._set_task_status("accepting")
            for member in sorted(team_task.members, key=lambda item: item.order_index):
                if member.order_index == 0:
                    continue
                self._raise_if_cancelled()
                self._accept_member_invitation(member, team_account_id, proxy)

            self._refresh_account_team_context(main_account.id, team_account_id, is_main=True)
            self._set_task_status("uploading")
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
            self._log("Team 创建流程已完成")
        except TeamCancelledError:
            self._cancel_task("订阅确认后阶段已取消")
        except Exception as exc:
            logger.exception("Team 第二阶段失败: %s", self.task_uuid)
            self._fail_task(str(exc))

    def _register_single_member(
        self,
        member: TeamMember,
        email_service,
        service_type: EmailServiceType,
    ) -> Tuple[Any, Optional[Account]]:
        task_label = "主账号" if member.order_index == 0 else f"成员 {member.order_index}"

        with get_db() as db:
            crud.update_registration_task(
                db,
                member.registration_task_uuid,
                status="running",
                started_at=datetime.utcnow(),
            )

        def callback(message: str):
            self._log(f"[{task_label}] {message}")

        engine = RegistrationEngine(
            email_service=email_service,
            proxy_url=self._get_task_proxy(),
            callback_logger=callback,
            task_uuid=member.registration_task_uuid,
        )
        result = engine.run()

        if not result.success:
            with get_db() as db:
                crud.update_registration_task(
                    db,
                    member.registration_task_uuid,
                    status="failed",
                    completed_at=datetime.utcnow(),
                    error_message=result.error_message or "注册失败",
                    result=result.to_dict(),
                )
            self._update_member(member.id, invitation_status="failed")
            return result, None

        if not engine.save_to_database(result):
            result.success = False
            result.error_message = result.error_message or "保存注册结果到数据库失败"
            with get_db() as db:
                crud.update_registration_task(
                    db,
                    member.registration_task_uuid,
                    status="failed",
                    completed_at=datetime.utcnow(),
                    error_message=result.error_message,
                    result=result.to_dict(),
                )
            self._update_member(member.id, invitation_status="failed")
            return result, None

        with get_db() as db:
            account = crud.get_account_by_email(db, result.email)
            crud.update_registration_task(
                db,
                member.registration_task_uuid,
                status="completed",
                completed_at=datetime.utcnow(),
                result=result.to_dict(),
            )
            if not account:
                raise TeamOrchestrationError(f"账号已注册但数据库中未找到: {result.email}")
            db.refresh(account)
            return result, account

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
        self._sync_status_snapshot()
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
        self._sync_status_snapshot()
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

            updates: Dict[str, Any] = {
                "workspace_id": team_account_id,
                "subscription_type": "team",
                "subscription_at": datetime.utcnow(),
                "updated_at": datetime.utcnow(),
            }
            if account.session_token:
                refresh_result = refresh_member_team_token(account, team_account_id, self._get_task_proxy())
                if not refresh_result.get("success"):
                    raise TeamOrchestrationError(refresh_result.get("error") or f"刷新 Team token 失败: {account.email}")
                updates.update({
                    "access_token": refresh_result.get("access_token"),
                    "session_token": refresh_result.get("session_token"),
                    "expires_at": refresh_result.get("expires_at"),
                    "last_refresh": datetime.utcnow(),
                })

            crud.update_account(db, account.id, **updates)
            refreshed_account_id = account.id
        if is_main:
            self._update_task_fields(main_account_id=refreshed_account_id)

    def _upload_team_payload(self, team_account_id: str) -> Dict[str, Any]:
        with get_db() as db:
            task = crud.get_team_task(db, self.task_uuid)
            if not task:
                raise TeamOrchestrationError("任务不存在，无法上传")
            members = sorted(task.members or [], key=lambda item: item.order_index)
            account_ids = [member.account_id for member in members if member.account_id]
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
            merged["details"].extend(item.get("details", []))
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

    def _sync_status_snapshot(self):
        with get_db() as db:
            task = crud.get_team_task(db, self.task_uuid)
            if not task:
                return
            snapshot = build_team_response(task)
        task_manager.update_status(self.task_uuid, snapshot["status"], snapshot=snapshot)

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
        self._sync_status_snapshot()

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
        self._sync_status_snapshot()
