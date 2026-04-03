"""
Team 邀请工作流。
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..config.constants import EmailServiceType
from ..database import crud
from ..database.models import Account, TeamInviteMember, TeamInviteTask
from ..database.session import get_db
from ..services import EmailServiceFactory
from ..web.task_manager import task_manager
from .openai.team_invitation import (
    accept_team_invitation_by_link,
    discover_team_account,
    extract_invitation_link,
    list_team_invites,
    list_team_members,
    refresh_member_team_token,
    send_team_invitation,
)
from .team_workflow import (
    build_account_summary,
    build_inbox_config,
    collect_email_candidates,
    extract_full_email_content,
    has_recoverable_account_session,
    recover_account_session_via_login,
)
from .upload.cpa_upload import batch_upload_to_cpa
from .upload.sub2api_upload import batch_upload_to_sub2api
from .upload.team_manager_upload import batch_upload_to_team_manager
from .upload.team_upload_guard import (
    OPENAI_OAUTH_INVALID_REQUEST,
    OPENAI_OAUTH_TOKEN_REFRESH_FAILED,
    REFRESH_TOKEN_REUSED,
    TEAM_REFRESH_TOKEN_DUPLICATE,
    TEAM_REFRESH_TOKEN_REUPLOAD_BLOCKED,
    is_unrecoverable_team_upload_error,
)

logger = logging.getLogger(__name__)

RUNNING_INVITE_STATUSES = {"verifying", "inviting", "accepting", "uploading"}
RELOGIN_REASON_CODES = {
    TEAM_REFRESH_TOKEN_DUPLICATE,
    TEAM_REFRESH_TOKEN_REUPLOAD_BLOCKED,
    REFRESH_TOKEN_REUSED,
    OPENAI_OAUTH_TOKEN_REFRESH_FAILED,
    OPENAI_OAUTH_INVALID_REQUEST,
}
RELOGIN_ERROR_KEYWORDS = (
    "重新登录",
    "重新登錄",
    "session_token",
    "refresh token",
    "refresh_token_reused",
    "openai_oauth_token_refresh_failed",
    "invalid_request_error",
    "token refresh failed",
    "token refresh retry exhausted",
)
RECOVERABLE_ERROR_KEYWORDS = (
    "timeout",
    "timed out",
    "connection",
    "network",
    "proxy",
    "连接失败",
    "连接异常",
    "读取 Team 邀请邮件",
    "未能在邮箱中读取到",
    "刷新 Team token 失败",
    "上传失败",
    "上传异常",
)


class TeamInviteCancelledError(RuntimeError):
    """Invite task cancelled."""


class TeamInviteError(RuntimeError):
    """Invite task failed."""


def _extract_retry_limit(upload_config: Optional[Dict[str, Any]]) -> int:
    try:
        return max(0, int((upload_config or {}).get("retry_limit") or 0))
    except (TypeError, ValueError):
        return 0


def _is_account_team_ready(account: Optional[Account], team_account_id: Optional[str]) -> bool:
    normalized_team_account_id = str(team_account_id or "").strip()
    if not account or not normalized_team_account_id:
        return False
    return (
        bool(account.access_token)
        and str(account.account_id or "").strip() == normalized_team_account_id
        and str(account.workspace_id or "").strip() == normalized_team_account_id
        and str(account.subscription_type or "").strip() == "team"
    )


def _build_selected_platforms(upload_config: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    config = upload_config or {}
    return [
        {
            "key": "sub2api",
            "label": "Sub2API",
            "enabled": bool(config.get("auto_upload_sub2api")),
            "service_ids": list(config.get("sub2api_service_ids") or []),
            "group_ids_by_service": dict(config.get("sub2api_group_ids_by_service") or {}),
            "include_source_account": bool(config.get("include_source_account_upload")),
        },
        {
            "key": "cpa",
            "label": "CPA",
            "enabled": bool(config.get("auto_upload_cpa")),
            "service_ids": list(config.get("cpa_service_ids") or []),
            "include_source_account": bool(config.get("include_source_account_upload")),
        },
        {
            "key": "tm",
            "label": "Team Manager",
            "enabled": bool(config.get("auto_upload_tm")),
            "service_ids": list(config.get("tm_service_ids") or []),
            "include_source_account": bool(config.get("include_source_account_upload")),
        },
    ]


def _build_member_platform_uploads(member: TeamInviteMember) -> Dict[str, Any]:
    uploads = (member.result or {}).get("platform_uploads") or {}
    return dict(uploads) if isinstance(uploads, dict) else {}


def _build_sub2api_copy_result(detail: Dict[str, Any]) -> Dict[str, Any]:
    copy_result = {
        "success": bool(detail.get("success")),
        "group_id": detail.get("group_id"),
        "group_name": detail.get("group_name"),
        "naming_identity": detail.get("naming_identity"),
        "generated_name": detail.get("generated_name"),
        "copy_index": detail.get("copy_index"),
    }
    if detail.get("message"):
        copy_result["message"] = str(detail.get("message"))
    if detail.get("error"):
        copy_result["error"] = str(detail.get("error"))
    if detail.get("reason_code"):
        copy_result["reason_code"] = detail.get("reason_code")
    if detail.get("guard_message"):
        copy_result["guard_message"] = detail.get("guard_message")
    if detail.get("guard_blocked") is not None:
        copy_result["guard_blocked"] = bool(detail.get("guard_blocked"))
    return copy_result


def _build_account_platform_uploads(
    account_id: Optional[int],
    upload_results: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    if not account_id or not isinstance(upload_results, dict):
        return {}

    platform_uploads: Dict[str, Any] = {}
    normalized_account_id = str(account_id)
    for platform_key, platform_result in upload_results.items():
        if platform_key in {"team_context", "noop"} or not isinstance(platform_result, dict):
            continue

        details = [
            detail
            for detail in (platform_result.get("details") or [])
            if str(detail.get("id") or "").strip() == normalized_account_id
        ]
        if not details:
            continue

        success = all(bool(detail.get("success")) for detail in details)
        reason_codes: List[str] = []
        guard_messages: List[str] = []
        guard_blocked = False
        for detail in details:
            reason_code = str(detail.get("reason_code") or "").strip()
            if reason_code and reason_code not in reason_codes:
                reason_codes.append(reason_code)
            guard_message = str(detail.get("guard_message") or "").strip()
            if guard_message and guard_message not in guard_messages:
                guard_messages.append(guard_message)
            guard_blocked = guard_blocked or bool(detail.get("guard_blocked"))

        if platform_key == "sub2api":
            copies = [
                _build_sub2api_copy_result(detail)
                for detail in details
                if any(
                    detail.get(field) is not None
                    for field in ("generated_name", "group_id", "group_name", "copy_index")
                )
            ]
            success_count = sum(1 for item in copies if item.get("success"))
            failed_count = sum(1 for item in copies if not item.get("success"))
            entry: Dict[str, Any] = {
                "success": success,
                "copies": copies,
                "copy_total": len(copies),
                "success_count": success_count,
                "failed_count": failed_count,
            }
            if success:
                entry["message"] = (
                    f"已完成 {len(copies)} 份副本上传，成功 {success_count} 份"
                    if copies
                    else "上传成功"
                )
            else:
                entry["message"] = (
                    f"已处理 {len(copies)} 份副本，成功 {success_count} 份，失败 {failed_count} 份"
                    if copies
                    else None
                )
                entry["error"] = "；".join(
                    str(detail.get("error") or detail.get("message") or "").strip()
                    for detail in details
                    if not detail.get("success")
                ) or "上传失败"
        else:
            messages = [
                str(detail.get("message") or "").strip()
                for detail in details
                if detail.get("success") and detail.get("message")
            ]
            errors = [
                str(detail.get("error") or detail.get("message") or "").strip()
                for detail in details
                if not detail.get("success")
            ]
            entry = {"success": success}
            if success:
                entry["message"] = "；".join(item for item in messages if item) or "上传成功"
            else:
                entry["error"] = "；".join(item for item in errors if item) or "上传失败"

        if reason_codes:
            entry["reason_codes"] = reason_codes
            entry["reason_code"] = reason_codes[0]
        if guard_messages:
            entry["guard_message"] = "；".join(guard_messages)
        if guard_blocked:
            entry["guard_blocked"] = True
        platform_uploads[platform_key] = entry

    return platform_uploads


def _build_source_account_summary(task: TeamInviteTask, team_account_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not task.source_account:
        return None

    summary = build_account_summary(task.source_account) or {}
    included_in_upload = bool((task.upload_config or {}).get("include_source_account_upload"))
    platform_uploads = _build_account_platform_uploads(
        task.source_account_id,
        (task.result or {}).get("upload"),
    )

    upload_status = "accepted"
    if platform_uploads:
        upload_status = "uploaded" if all(item.get("success") for item in platform_uploads.values()) else "failed"
    elif included_in_upload and task.status == "uploading":
        upload_status = "uploading"

    summary.update(
        {
            "team_ready": _is_account_team_ready(task.source_account, team_account_id),
            "included_in_upload": included_in_upload,
            "platform_uploads": platform_uploads,
            "upload_status": upload_status,
        }
    )
    return summary


def _member_can_resume(member: TeamInviteMember, team_account_id: Optional[str]) -> bool:
    if member.invitation_status == "uploaded":
        return False
    if member.account_id and _is_account_team_ready(member.account, team_account_id):
        return True
    if member.invitation_status in {"pending", "invited", "accepted", "failed", "cancelled"}:
        return True
    return bool(member.account_id and (member.result or {}).get("reason") in {"pending_invite", "already_member"})


def _member_should_skip_on_restart(member: TeamInviteMember, team_account_id: Optional[str]) -> bool:
    if member.invitation_status in {"accepted", "uploaded"}:
        return True
    return bool(member.account_id and _is_account_team_ready(member.account, team_account_id))


def _build_member_action_flags(
    member: TeamInviteMember,
    *,
    task_status: str,
    team_account_id: Optional[str],
) -> Dict[str, bool]:
    managed = bool(member.account_id)
    running = task_status in RUNNING_INVITE_STATUSES
    team_ready = managed and _is_account_team_ready(member.account, team_account_id)
    reason = str(((member.result or {}).get("reason")) or "").strip()
    can_accept_or_refresh = (
        managed
        and not running
        and (
            not team_ready
            or reason in {"pending_invite", "already_member"}
            or member.invitation_status in {"invited", "failed", "cancelled", "skipped"}
        )
    )
    return {
        "accept_or_refresh": can_accept_or_refresh,
        "refresh": managed and not running and bool(team_account_id),
        "upload": managed and team_ready and not running,
    }


def _collect_member_platform_reason_codes(member: TeamInviteMember) -> List[str]:
    codes: List[str] = []
    uploads = _build_member_platform_uploads(member)
    for upload in uploads.values():
        if not isinstance(upload, dict):
            continue
        for value in upload.get("reason_codes") or []:
            code = str(value or "").strip()
            if code and code not in codes:
                codes.append(code)
        primary_code = str(upload.get("reason_code") or "").strip()
        if primary_code and primary_code not in codes:
            codes.append(primary_code)
    return codes


def _collect_member_guard_messages(member: TeamInviteMember) -> List[str]:
    messages: List[str] = []
    uploads = _build_member_platform_uploads(member)
    for upload in uploads.values():
        if not isinstance(upload, dict):
            continue
        guard_message = str(upload.get("guard_message") or "").strip()
        if guard_message and guard_message not in messages:
            messages.append(guard_message)
    return messages


def _member_matches_relogin_error(member: TeamInviteMember) -> bool:
    error_candidates = [str(member.error_message or "").strip()]
    uploads = _build_member_platform_uploads(member)
    for upload in uploads.values():
        if not isinstance(upload, dict):
            continue
        error_candidates.append(str(upload.get("error") or "").strip())
        error_candidates.append(str(upload.get("message") or "").strip())

    normalized_messages = "；".join(item for item in error_candidates if item).lower()
    return any(keyword.lower() in normalized_messages for keyword in RELOGIN_ERROR_KEYWORDS)


def _build_member_guidance(
    member: TeamInviteMember,
    *,
    task_status: str,
    team_account_id: Optional[str],
) -> Dict[str, Any]:
    managed = bool(member.account_id and member.account)
    running = task_status in RUNNING_INVITE_STATUSES
    team_ready = managed and _is_account_team_ready(member.account, team_account_id)
    reason = str(((member.result or {}).get("reason")) or "").strip()
    reason_codes = _collect_member_platform_reason_codes(member)
    guard_messages = _collect_member_guard_messages(member)

    if not managed:
        return {
            "action": "none",
            "label": "仅邀请",
            "tone": "pending",
            "message": "手填邮箱只参与邀请，不参与自动登录或成员级上传。",
            "batchable": False,
            "reason_codes": reason_codes,
        }

    if any(code in RELOGIN_REASON_CODES for code in reason_codes) or _member_matches_relogin_error(member):
        return {
            "action": "relogin",
            "label": "建议重登",
            "tone": "failed",
            "message": guard_messages[0] if guard_messages else "该账号需要重新登录并刷新 Team 身份后再继续上传。",
            "batchable": not running,
            "reason_codes": reason_codes,
        }

    if not team_ready and not has_recoverable_account_session(member.account):
        return {
            "action": "relogin",
            "label": "建议重登",
            "tone": "failed",
            "message": "该账号缺少可用会话，建议先重新登录再刷新 Team 身份。",
            "batchable": not running,
            "reason_codes": reason_codes,
        }

    if reason == "pending_invite" or member.invitation_status == "invited":
        return {
            "action": "reinvite",
            "label": "建议重邀",
            "tone": "warning",
            "message": "该账号仍处于待接受邀请状态，建议重新执行邀请/接受邀请。",
            "batchable": False,
            "reason_codes": reason_codes,
        }

    if member.invitation_status in {"failed", "cancelled"} and not team_ready:
        return {
            "action": "reinvite",
            "label": "建议重邀",
            "tone": "warning",
            "message": "该账号尚未完成 Team 加入，建议重新执行邀请链路。",
            "batchable": False,
            "reason_codes": reason_codes,
        }

    if team_ready and member.invitation_status != "uploaded":
        return {
            "action": "upload",
            "label": "可上传",
            "tone": "completed",
            "message": "该账号已具备 Team 身份，可直接执行成员上传。",
            "batchable": False,
            "reason_codes": reason_codes,
        }

    if not team_ready and member.account_id:
        return {
            "action": "refresh",
            "label": "建议刷新",
            "tone": "pending",
            "message": "该账号需要刷新 Team 身份后才能继续后续上传。",
            "batchable": False,
            "reason_codes": reason_codes,
        }

    return {
        "action": "none",
        "label": "无需处理",
        "tone": "completed" if member.invitation_status == "uploaded" else "pending",
        "message": "当前没有额外处理建议。",
        "batchable": False,
        "reason_codes": reason_codes,
    }


def _is_recoverable_error_message(error_message: Optional[str]) -> bool:
    if not error_message:
        return False
    if is_unrecoverable_team_upload_error(error_message):
        return False
    normalized_message = str(error_message).lower()
    return any(keyword.lower() in normalized_message for keyword in RECOVERABLE_ERROR_KEYWORDS)


def build_team_invite_member_summary(
    member: TeamInviteMember,
    *,
    task_status: str,
    team_account_id: Optional[str],
) -> Dict[str, Any]:
    account = member.account
    member_result = member.result or {}
    guidance = _build_member_guidance(
        member,
        task_status=task_status,
        team_account_id=team_account_id,
    )
    return {
        "id": member.id,
        "team_invite_task_id": member.team_invite_task_id,
        "account_id": member.account_id,
        "email": member.email,
        "source_type": member.source_type,
        "source_team_task_id": member.source_team_task_id,
        "invitation_status": member.invitation_status,
        "invitation_sent_at": member.invitation_sent_at.isoformat() if member.invitation_sent_at else None,
        "invitation_accepted_at": member.invitation_accepted_at.isoformat() if member.invitation_accepted_at else None,
        "uploaded_at": member.uploaded_at.isoformat() if member.uploaded_at else None,
        "order_index": member.order_index,
        "error_message": member.error_message,
        "result": member_result,
        "team_ready": _is_account_team_ready(account, team_account_id),
        "action_flags": _build_member_action_flags(
            member,
            task_status=task_status,
            team_account_id=team_account_id,
        ),
        "guidance": guidance,
        "platform_uploads": _build_member_platform_uploads(member),
        "last_action": member_result.get("last_action"),
        "created_at": member.created_at.isoformat() if member.created_at else None,
        "updated_at": member.updated_at.isoformat() if member.updated_at else None,
        "account": build_account_summary(account) if account else None,
    }


def _build_task_recommended_actions(member_summaries: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    action_map: Dict[str, Dict[str, Any]] = {
        "relogin": {"count": 0, "member_ids": [], "emails": []},
        "reinvite": {"count": 0, "member_ids": [], "emails": []},
        "refresh": {"count": 0, "member_ids": [], "emails": []},
        "upload": {"count": 0, "member_ids": [], "emails": []},
    }

    for member in member_summaries:
        guidance = member.get("guidance") or {}
        action = str(guidance.get("action") or "").strip()
        if action not in action_map:
            continue
        action_map[action]["count"] += 1
        member_id = member.get("id")
        email = member.get("email")
        if member_id:
            action_map[action]["member_ids"].append(member_id)
        if email:
            action_map[action]["emails"].append(email)

    return {
        "relogin_count": action_map["relogin"]["count"],
        "relogin_member_ids": action_map["relogin"]["member_ids"],
        "relogin_emails": action_map["relogin"]["emails"],
        "reinvite_count": action_map["reinvite"]["count"],
        "reinvite_member_ids": action_map["reinvite"]["member_ids"],
        "reinvite_emails": action_map["reinvite"]["emails"],
        "refresh_count": action_map["refresh"]["count"],
        "refresh_member_ids": action_map["refresh"]["member_ids"],
        "upload_count": action_map["upload"]["count"],
        "upload_member_ids": action_map["upload"]["member_ids"],
    }


def build_team_invite_response(task: TeamInviteTask, runtime_status: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    members = sorted(task.members or [], key=lambda item: item.order_index)
    runtime_status = runtime_status or {}
    team_account_id = task.team_account_id
    upload_config = task.upload_config or {}
    member_summaries = [
        build_team_invite_member_summary(
            member,
            task_status=task.status,
            team_account_id=team_account_id,
        )
        for member in members
    ]
    recommended_actions = _build_task_recommended_actions(member_summaries)
    source_account_summary = _build_source_account_summary(task, team_account_id)
    stats = {
        "total_members": len(members),
        "managed_members": sum(1 for item in members if item.account_id),
        "manual_members": sum(1 for item in members if not item.account_id),
        "invited_members": sum(
            1 for item in members if item.invitation_status in {"invited", "accepted", "uploaded", "invite_only"}
        ),
        "accepted_members": sum(1 for item in members if item.invitation_status in {"accepted", "uploaded"}),
        "uploaded_members": sum(1 for item in members if item.invitation_status == "uploaded"),
        "failed_members": sum(1 for item in members if item.invitation_status == "failed"),
        "skipped_members": sum(1 for item in members if item.invitation_status == "skipped"),
        "team_ready_members": sum(1 for item in members if _is_account_team_ready(item.account, team_account_id)),
    }
    return {
        "id": task.id,
        "task_uuid": task.task_uuid,
        "status": task.status,
        "source_mode": task.source_mode,
        "proxy": task.proxy,
        "team_account_id": task.team_account_id,
        "team_workspace_id": task.team_workspace_id,
        "upload_config": upload_config,
        "logs": task.logs,
        "error_message": task.error_message,
        "result": task.result or {},
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "started_at": task.started_at.isoformat() if task.started_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        "source_account": source_account_summary,
        "source_team_task_uuid": task.source_team_task.task_uuid if task.source_team_task else None,
        "members": member_summaries,
        "stats": stats,
        "runtime_message": runtime_status.get("runtime_message"),
        "resume_available": task.status in {"failed", "cancelled", "completed"} and any(
            _member_can_resume(member, team_account_id) for member in members
        ),
        "restart_available": task.status not in RUNNING_INVITE_STATUSES and any(
            not _member_should_skip_on_restart(member, team_account_id) for member in members
        ),
        "retry_limit": _extract_retry_limit(upload_config),
        "selected_platforms": _build_selected_platforms(upload_config),
        "team_member_count": sum(1 for member in members if member.source_type == "team_task"),
        "custom_member_count": sum(1 for member in members if member.source_type != "team_task"),
        "recommended_actions": recommended_actions,
    }


class TeamInviteOrchestrator:
    def __init__(self, task_uuid: str):
        self.task_uuid = task_uuid

    def run(self):
        try:
            self._set_task_status("verifying", started_at=datetime.utcnow(), error_message=None, completed_at=None)
            task, source_account, team_account_id, proxy = self._load_task_context(force_discovery=True)

            managed_upload_ids: List[int] = []
            self._set_task_status("inviting")
            members = sorted(task.members or [], key=lambda item: item.order_index)
            for member in members:
                self._raise_if_cancelled()
                member = self._reload_member(member.id)
                if not member or member.invitation_status == "uploaded":
                    continue
                if member.account_id and _is_account_team_ready(member.account, team_account_id):
                    self._mark_member_team_ready(
                        member.id,
                        reason=str(((member.result or {}).get("reason")) or "").strip() or None,
                        last_action="resume_team_context",
                    )
                    self._append_unique_account_id(managed_upload_ids, member.account_id)
                    continue

                self._push_runtime(f"正在处理 {member.email}")
                self._process_member_for_workflow(
                    source_account,
                    member,
                    team_account_id,
                    proxy,
                    managed_upload_ids,
                )

            self._push_runtime("正在校验成员 Team 空间并准备上传")
            if self._should_upload_source_account(task) and source_account and source_account.id:
                self._append_unique_account_id(managed_upload_ids, source_account.id)
                self._log(f"已将主账号加入上传队列: {source_account.email}")

            self._validate_accounts_ready_for_team_upload(managed_upload_ids, team_account_id)
            self._set_task_status("uploading")
            upload_results = self._upload_selected_accounts(managed_upload_ids)
            if self._has_upload_failures(upload_results):
                raise TeamInviteError("上传阶段存在失败结果，请查看详情")

            result = self._merge_result({"upload": upload_results})
            self._set_task_status("completed", result=result, completed_at=datetime.utcnow(), error_message=None)
            self._push_runtime("Team 邀请流程已完成")
            self._log("Team 邀请流程已完成")
        except TeamInviteCancelledError:
            self._cancel_task("邀请任务已取消")
        except Exception as exc:
            logger.exception("Team 邀请流程失败: %s", self.task_uuid)
            self._fail_task(str(exc))

    def resume(self):
        self.run()

    def accept_or_refresh_member(self, member_id: int, *, force_refresh: bool = False) -> Dict[str, Any]:
        task, source_account, team_account_id, proxy = self._load_task_context(force_discovery=force_refresh)
        member = self._reload_member(member_id)
        if not member:
            raise TeamInviteError("邀请成员不存在")
        if not member.account_id:
            raise TeamInviteError("手填邮箱仅支持发送邀请，不支持接受邀请或刷新 Team 身份")

        if _is_account_team_ready(member.account, team_account_id) and not force_refresh:
            self._mark_member_team_ready(member.id, last_action="manual_refresh_team")
            self._push_runtime(f"{member.email} 已处于 Team 身份")
            refreshed_member = self._reload_member(member.id)
            if not refreshed_member:
                raise TeamInviteError("邀请成员不存在")
            return build_team_invite_member_summary(
                refreshed_member,
                task_status=task.status,
                team_account_id=team_account_id,
            )

        reason = str(((member.result or {}).get("reason")) or "").strip()
        if force_refresh or member.invitation_status == "accepted" or reason == "already_member":
            self._log(f"手动刷新 Team 身份: {member.email}")
            self._run_with_retries(
                f"刷新 {member.email} Team 身份",
                lambda: self._refresh_member_team_context(
                    member,
                    team_account_id,
                    proxy,
                    reason=reason or "manual_refresh",
                    last_action="manual_refresh_team",
                ),
            )
        else:
            before_invites = self._read_team_email_snapshot(source_account, team_account_id, proxy, source="invites")
            before_members = self._read_team_email_snapshot(source_account, team_account_id, proxy, source="members")
            email_lower = member.email.lower()
            if email_lower in before_members:
                self._run_with_retries(
                    f"刷新 {member.email} Team 身份",
                    lambda: self._refresh_member_team_context(
                        member,
                        team_account_id,
                        proxy,
                        reason="already_member",
                        last_action="manual_refresh_team",
                    ),
                )
            else:
                if email_lower not in before_invites and member.invitation_status not in {
                    "invited",
                    "failed",
                    "cancelled",
                    "skipped",
                }:
                    self._run_with_retries(
                        f"发送 {member.email} 邀请",
                        lambda: self._invite_member(source_account, member, team_account_id, proxy),
                    )
                self._log(f"手动接受邀请: {member.email}")
                self._run_with_retries(
                    f"接受 {member.email} 邀请",
                    lambda: self._accept_member_invitation(
                        member,
                        team_account_id,
                        proxy,
                        last_action="manual_accept_invite",
                    ),
                )

        refreshed_member = self._reload_member(member.id)
        self._sync_status_snapshot(runtime_message=f"{member.email} 操作已完成")
        if not refreshed_member:
            raise TeamInviteError("邀请成员不存在")
        return build_team_invite_member_summary(
            refreshed_member,
            task_status=task.status,
            team_account_id=team_account_id,
        )

    def upload_member(self, member_id: int, *, require_platform_selection: bool = True) -> Dict[str, Any]:
        _task, _source_account, team_account_id, proxy = self._load_task_context(force_discovery=False)
        member = self._reload_member(member_id)
        if not member:
            raise TeamInviteError("邀请成员不存在")
        if not member.account_id:
            raise TeamInviteError("手填邮箱不支持上传")

        if not _is_account_team_ready(member.account, team_account_id):
            self._log(f"{member.email} 尚未完成 Team 身份刷新，先尝试刷新")
            self._run_with_retries(
                f"刷新 {member.email} Team 身份",
                lambda: self._refresh_member_team_context(
                    member,
                    team_account_id,
                    proxy,
                    reason=str(((member.result or {}).get("reason")) or "").strip() or "manual_upload",
                    last_action="manual_refresh_before_upload",
                ),
            )

        self._validate_accounts_ready_for_team_upload([member.account_id], team_account_id)
        results = self._upload_selected_accounts(
            [member.account_id],
            require_platform_selection=require_platform_selection,
        )
        if self._has_upload_failures(results):
            raise TeamInviteError("成员上传存在失败结果，请查看上传结果与日志")
        self._sync_status_snapshot(runtime_message=f"{member.email} 上传完成")
        return results

    def relogin_members(self, member_ids: Optional[Sequence[int]] = None) -> Dict[str, Any]:
        task, _source_account, team_account_id, proxy = self._load_task_context(force_discovery=False)
        requested_ids = {
            int(member_id)
            for member_id in (member_ids or [])
            if isinstance(member_id, int) or str(member_id).isdigit()
        }
        member_refs = sorted(task.members or [], key=lambda item: item.order_index)
        candidates: List[TeamInviteMember] = []
        for member_ref in member_refs:
            member = self._reload_member(member_ref.id)
            if not member:
                continue
            if requested_ids and member.id not in requested_ids:
                continue
            guidance = _build_member_guidance(member, task_status=task.status, team_account_id=team_account_id)
            if guidance.get("action") == "relogin":
                candidates.append(member)

        if not candidates:
            raise TeamInviteError("当前没有建议重登的成员")

        results: Dict[str, Any] = {
            "success_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "details": [],
        }

        for member in candidates:
            member = self._reload_member(member.id)
            if not member or not member.account:
                results["failed_count"] += 1
                results["details"].append(
                    {"id": member.id if member else None, "email": getattr(member, "email", None), "success": False, "error": "成员账号不存在"}
                )
                continue

            guidance = _build_member_guidance(member, task_status=task.status, team_account_id=team_account_id)
            reason = str(((member.result or {}).get("reason")) or "").strip() or "manual_relogin"
            try:
                self._run_with_retries(
                    f"重登 {member.email}",
                    lambda member=member, reason=reason: self._refresh_member_team_context(
                        member,
                        team_account_id,
                        proxy,
                        reason=reason,
                        last_action="batch_relogin",
                        force_relogin=True,
                    ),
                )
                refreshed_member = self._reload_member(member.id)
                results["success_count"] += 1
                results["details"].append(
                    {
                        "id": member.id,
                        "email": member.email,
                        "success": True,
                        "message": "已重新登录并刷新 Team 身份",
                        "team_ready": bool(refreshed_member and _is_account_team_ready(refreshed_member.account, team_account_id)),
                    }
                )
            except Exception as exc:  # noqa: PERF203
                error_text = str(exc)
                result = dict(member.result or {})
                result["last_action"] = "batch_relogin_failed"
                self._update_member(member.id, invitation_status="failed", error_message=error_text, result=result)
                results["failed_count"] += 1
                results["details"].append(
                    {
                        "id": member.id,
                        "email": member.email,
                        "success": False,
                        "error": error_text,
                        "guidance_message": guidance.get("message"),
                    }
                )

        merged = self._merge_result({"relogin": results})
        self._update_task_fields(result=merged)
        success_count = int(results.get("success_count") or 0)
        failed_count = int(results.get("failed_count") or 0)
        runtime_message = f"批量重登完成：成功 {success_count}，失败 {failed_count}"
        self._log(runtime_message)
        self._sync_status_snapshot(runtime_message=runtime_message)
        return results

    def _load_task_context(
        self,
        *,
        force_discovery: bool,
    ) -> tuple[TeamInviteTask, Account, str, Optional[str]]:
        task = self._get_task()
        if not task or not task.source_account:
            raise TeamInviteError("邀请任务缺少主账号信息")

        source_account = task.source_account
        proxy = task.proxy
        team_account_id = str(task.team_account_id or "").strip()
        if team_account_id and not force_discovery:
            refreshed_source_account = self._refresh_source_account_team_context(
                source_account.id,
                team_account_id,
                proxy,
            )
            refreshed_task = self._get_task() or task
            return refreshed_task, refreshed_task.source_account or refreshed_source_account, team_account_id, proxy

        self._push_runtime("正在发现目标 Team")
        self._log("开始发现目标 Team")
        self._log(f"当前使用代理: {proxy or '直连'}")
        if not source_account.access_token:
            self._push_runtime("涓昏处鍙风己灏?access_token锛屽皾璇曞厛鑷姩琛ョ櫥")
            source_account = self._ensure_source_account_access_before_discovery(source_account.id, proxy)
            refreshed_task = self._get_task() or task
            source_account = refreshed_task.source_account or source_account
        discovery = discover_team_account(source_account, proxy)
        if not discovery.get("success"):
            raise TeamInviteError(discovery.get("error") or "无法发现 team_account_id")

        team_account = discovery["account"]
        team_account_id = str(team_account.get("team_account_id") or "").strip()
        if not team_account_id:
            raise TeamInviteError("discover_team_account 未返回 team_account_id")

        self._update_task_fields(
            team_account_id=team_account_id,
            team_workspace_id=team_account.get("team_workspace_id"),
        )
        self._merge_result(
            {
                "team": {
                    "team_account_id": team_account_id,
                    "team_workspace_id": team_account.get("team_workspace_id"),
                    "subscription_plan": team_account.get("subscription_plan"),
                }
            }
        )
        refreshed_source_account = self._refresh_source_account_team_context(
            source_account.id,
            team_account_id,
            proxy,
        )
        refreshed_task = self._get_task() or task
        return refreshed_task, refreshed_task.source_account or refreshed_source_account, team_account_id, proxy

    @staticmethod
    def _should_upload_source_account(task: Optional[TeamInviteTask]) -> bool:
        if not task or not task.source_account_id:
            return False
        return bool((task.upload_config or {}).get("include_source_account_upload"))

    def _ensure_source_account_access_before_discovery(
        self,
        account_id: int,
        proxy: Optional[str],
    ) -> Account:
        with get_db() as db:
            account = crud.get_account_by_id(db, account_id)
            if not account:
                raise TeamInviteError(f"主账号不存在: {account_id}")

        recovery = recover_account_session_via_login(
            account,
            proxy_url=proxy,
            callback_logger=self._log,
        )
        if not recovery.get("success"):
            message = recovery.get("error") or f"主账号缺少 access_token，且无法自动补登: {account.email}"
            raise TeamInviteError(message)

        self._persist_recovered_account_session(account.id, recovery)
        with get_db() as db:
            refreshed_account = crud.get_account_by_id(db, account.id)
            if not refreshed_account:
                raise TeamInviteError(f"主账号不存在: {account_id}")
        if not refreshed_account.access_token:
            raise TeamInviteError(f"主账号自动补登后仍缺少 access_token: {refreshed_account.email}")

        self._log(f"主账号自动补登成功，继续发现 Team: {refreshed_account.email}")
        return refreshed_account

    def _persist_recovered_account_session(self, account_id: int, recovery: Dict[str, Any]):
        with get_db() as db:
            fresh_account = crud.get_account_by_id(db, account_id)
            if not fresh_account:
                raise TeamInviteError(f"璐﹀彿涓嶅瓨鍦? {account_id}")
            recovered_account_id = recovery.get("account_id")
            if recovered_account_id:
                fresh_account.account_id = recovered_account_id
            crud.update_account(
                db,
                fresh_account.id,
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

    def _refresh_source_account_team_context(
        self,
        account_id: int,
        team_account_id: str,
        proxy: Optional[str],
    ) -> Account:
        with get_db() as db:
            account = crud.get_account_by_id(db, account_id)
            if not account:
                raise TeamInviteError(f"涓昏处鍙蜂笉瀛樺湪: {account_id}")

        if _is_account_team_ready(account, team_account_id):
            return account

        recovery = None
        if not has_recoverable_account_session(account):
            self._log(f"{account.email} 缂哄皯鍙敤浼氳瘽锛屾鍦ㄨ嚜鍔ㄨˉ鐧绘仮澶嶄富鍙?Team 浼氳瘽")
            recovery = recover_account_session_via_login(
                account,
                proxy_url=proxy,
                callback_logger=self._log,
                target_workspace_id=team_account_id,
            )
            if not recovery.get("success"):
                message = recovery.get("error") or f"涓昏处鍙疯嚜鍔ㄨˉ鐧诲け璐? {account.email}"
                raise TeamInviteError(message)
            self._persist_recovered_account_session(account.id, recovery)
            with get_db() as db:
                account = crud.get_account_by_id(db, account.id)
                if not account:
                    raise TeamInviteError(f"涓昏处鍙蜂笉瀛樺湪: {account_id}")

        updates: Dict[str, Any] = {
            "workspace_id": team_account_id,
            "subscription_type": "team",
            "subscription_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        consent_has_team_token = recovery is not None and recovery.get("access_token") and not recovery.get("session_token")
        if consent_has_team_token:
            updates.update(
                {
                    "account_id": recovery.get("account_id") or team_account_id,
                    "access_token": recovery.get("access_token"),
                    "session_token": "",
                    "last_refresh": datetime.utcnow(),
                }
            )
        elif account.session_token:
            refresh_result = refresh_member_team_token(account, team_account_id, proxy)
            if not refresh_result.get("success"):
                raise TeamInviteError(refresh_result.get("error") or f"鍒锋柊涓昏处鍙?Team token 澶辫触: {account.email}")
            updates.update(
                {
                    "account_id": refresh_result.get("account_id") or team_account_id,
                    "access_token": refresh_result.get("access_token"),
                    "session_token": refresh_result.get("session_token"),
                    "expires_at": refresh_result.get("expires_at"),
                    "last_refresh": datetime.utcnow(),
                }
            )
        else:
            updates["account_id"] = team_account_id

        with get_db() as db:
            fresh_account = crud.get_account_by_id(db, account.id)
            if not fresh_account:
                raise TeamInviteError(f"涓昏处鍙蜂笉瀛樺湪: {account_id}")
            refreshed_account_id = updates.pop("account_id", None)
            if refreshed_account_id:
                fresh_account.account_id = refreshed_account_id
            crud.update_account(db, fresh_account.id, **updates)
            db.refresh(fresh_account)
            refreshed_account = fresh_account

        self._log(f"宸插悓姝ヤ富鍙?Team 韬唤: {refreshed_account.email}")
        return refreshed_account

    def _process_member_for_workflow(
        self,
        source_account: Account,
        member: TeamInviteMember,
        team_account_id: str,
        proxy: Optional[str],
        managed_upload_ids: List[int],
    ):
        if member.invitation_status == "invite_only":
            return
        if member.invitation_status == "accepted" and member.account_id:
            self._set_task_status("accepting")
            self._run_with_retries(
                f"刷新 {member.email} Team 身份",
                lambda: self._refresh_member_team_context(
                    member,
                    team_account_id,
                    proxy,
                    reason=str(((member.result or {}).get("reason")) or "").strip() or None,
                    last_action="auto_refresh_team",
                ),
            )
            self._append_unique_account_id(managed_upload_ids, member.account_id)
            return

        invite_result = self._run_with_retries(
            f"处理 {member.email} 邀请",
            lambda: self._invite_member(source_account, member, team_account_id, proxy),
        )
        if invite_result == "already_member" and member.account_id:
            self._run_with_retries(
                f"刷新 {member.email} Team 身份",
                lambda: self._refresh_member_team_context(
                    member,
                    team_account_id,
                    proxy,
                    reason="already_member",
                    last_action="auto_refresh_team",
                ),
            )
            self._append_unique_account_id(managed_upload_ids, member.account_id)
        elif invite_result in {"pending_invite", "accepted_ready"} and member.account_id:
            self._set_task_status("accepting")
            if invite_result == "pending_invite":
                self._log(f"{member.email} 存在待处理邀请，尝试自动接受")
            self._run_with_retries(
                f"接受 {member.email} 邀请",
                lambda: self._accept_member_invitation(
                    member,
                    team_account_id,
                    proxy,
                    last_action="auto_accept_invite",
                ),
            )
            self._append_unique_account_id(managed_upload_ids, member.account_id)

    def _invite_member(
        self,
        admin_account: Account,
        member: TeamInviteMember,
        team_account_id: str,
        proxy: Optional[str],
    ) -> str:
        member = self._reload_member(member.id)
        if not member:
            raise TeamInviteError("邀请成员不存在")

        email = member.email
        self._log(f"发送邀请给 {email}")
        before_invites = self._read_team_email_snapshot(admin_account, team_account_id, proxy, source="invites")
        before_members = self._read_team_email_snapshot(admin_account, team_account_id, proxy, source="members")
        email_lower = email.lower()

        if email_lower in before_members:
            self._merge_member_result(member.id, {"reason": "already_member", "last_action": "detect_already_member"})
            self._update_member(member.id, invitation_status="skipped", error_message=None)
            self._log(f"{email} 已经在 Team 中，跳过邀请")
            return "already_member"
        if email_lower in before_invites:
            self._merge_member_result(member.id, {"reason": "pending_invite", "last_action": "detect_pending_invite"})
            self._update_member(member.id, invitation_status="skipped", error_message=None)
            self._log(f"{email} 已存在待处理邀请，暂不重复邀请")
            return "pending_invite"

        success, message = send_team_invitation(admin_account, team_account_id, email, proxy)
        if not success:
            self._update_member(member.id, invitation_status="failed", error_message=message)
            raise TeamInviteError(message)

        time.sleep(2)
        after_invites = self._read_team_email_snapshot(admin_account, team_account_id, proxy, source="invites")
        after_members = self._read_team_email_snapshot(admin_account, team_account_id, proxy, source="members")
        in_members = email_lower in after_members
        in_invites = email_lower in after_invites
        if not in_members and not in_invites:
            error_message = f"邀请接口返回成功，但未在成员/邀请列表中看到 {email}"
            self._update_member(member.id, invitation_status="failed", error_message=error_message)
            raise TeamInviteError(error_message)

        member_status = "invite_only" if not member.account_id else "invited"
        self._update_member(
            member.id,
            invitation_status=member_status,
            invitation_sent_at=datetime.utcnow(),
            error_message=None,
            result={
                **(member.result or {}),
                "before_invites": sorted(before_invites),
                "before_members": sorted(before_members),
                "after_invites": sorted(after_invites),
                "after_members": sorted(after_members),
                "last_action": "send_invite",
            },
        )
        self._log(f"邀请已确认写入列表: {email}")
        return "accepted_ready" if member.account_id else "invite_only"

    def _accept_member_invitation(
        self,
        member: TeamInviteMember,
        team_account_id: str,
        proxy: Optional[str],
        *,
        last_action: Optional[str] = None,
    ):
        member = self._reload_member(member.id)
        if not member or not member.account:
            raise TeamInviteError("成员账号不存在，无法接受邀请")
        account = member.account
        invitation_link = self._wait_for_invitation_link(account)
        if not invitation_link:
            self._update_member(member.id, invitation_status="failed", error_message="未读取到 Team 邀请邮件")
            raise TeamInviteError(f"未能在邮箱中读取到 {account.email} 的 Team 邀请链接")

        success, message = accept_team_invitation_by_link(account, invitation_link, proxy)
        if not success:
            self._update_member(member.id, invitation_status="failed", error_message=message)
            raise TeamInviteError(message)

        self._refresh_member_team_context(
            member,
            team_account_id,
            proxy,
            last_action=last_action or "accept_invite",
        )
        self._log(f"{account.email} 已接受 Team 邀请")

    def _refresh_member_team_context(
        self,
        member: TeamInviteMember,
        team_account_id: str,
        proxy: Optional[str],
        *,
        reason: Optional[str] = None,
        last_action: Optional[str] = None,
        force_relogin: bool = False,
    ):
        member = self._reload_member(member.id)
        if not member or not member.account:
            raise TeamInviteError("成员账号不存在，无法刷新 Team 上下文")

        account = member.account
        recovery = None
        if force_relogin or not has_recoverable_account_session(account):
            if force_relogin:
                self._log(f"{account.email} 正在执行一键重登，并同步刷新 Team 身份")
            else:
                self._log(f"{account.email} 缺少可用会话，正在尝试自动补登恢复 Team 会话")
            recovery = recover_account_session_via_login(
                account,
                proxy_url=proxy,
                callback_logger=self._log,
                target_workspace_id=team_account_id,
            )
            if not recovery.get("success"):
                message = recovery.get("error") or (
                    f"账号缺少 session_token，无法切换到 Team 空间，请重新登录后重试: {account.email}"
                )
                self._update_member(member.id, invitation_status="failed", error_message=message)
                raise TeamInviteError(message)

            with get_db() as db:
                fresh_account = crud.get_account_by_id(db, account.id)
                if not fresh_account:
                    raise TeamInviteError(f"账号不存在: {account.id}")
                recovered_account_id = recovery.get("account_id")
                if recovered_account_id:
                    fresh_account.account_id = recovered_account_id
                crud.update_account(
                    db,
                    fresh_account.id,
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

            member = self._reload_member(member.id)
            if not member or not member.account:
                raise TeamInviteError(f"账号不存在: {account.id}")
            account = member.account

        updates: Dict[str, Any] = {
            "workspace_id": team_account_id,
            "subscription_type": "team",
            "subscription_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }

        consent_has_team_token = recovery is not None and recovery.get("access_token") and not recovery.get("session_token")
        if consent_has_team_token:
            self._log(f"{account.email} consent 流程已直接拿到 Team token，跳过 session_token 刷新")
            updates.update(
                {
                    "account_id": recovery.get("account_id") or team_account_id,
                    "access_token": recovery.get("access_token"),
                    "session_token": "",
                    "last_refresh": datetime.utcnow(),
                }
            )
        else:
            refresh_result = refresh_member_team_token(account, team_account_id, proxy)
            if not refresh_result.get("success"):
                self._update_member(member.id, invitation_status="failed", error_message=refresh_result.get("error"))
                raise TeamInviteError(refresh_result.get("error") or f"刷新 Team token 失败: {account.email}")
            updates.update(
                {
                    "account_id": refresh_result.get("account_id") or team_account_id,
                    "access_token": refresh_result.get("access_token"),
                    "session_token": refresh_result.get("session_token"),
                    "expires_at": refresh_result.get("expires_at"),
                    "last_refresh": datetime.utcnow(),
                }
            )

        with get_db() as db:
            fresh_account = crud.get_account_by_id(db, account.id)
            if not fresh_account:
                raise TeamInviteError(f"账号不存在: {account.id}")
            refreshed_account_id = updates.pop("account_id", None)
            if refreshed_account_id:
                fresh_account.account_id = refreshed_account_id
            crud.update_account(db, fresh_account.id, **updates)

        result = dict(member.result or {})
        if reason:
            result["reason"] = reason
        if last_action:
            result["last_action"] = last_action
        self._update_member(
            member.id,
            invitation_status="accepted",
            invitation_accepted_at=datetime.utcnow(),
            error_message=None,
            result=result,
        )
        if reason == "already_member":
            self._log(f"{account.email} 已在 Team 中，已刷新本地 Team 信息")

    def _validate_accounts_ready_for_team_upload(self, account_ids: Sequence[int], team_account_id: str):
        if not account_ids:
            return

        invalid_accounts: List[str] = []
        with get_db() as db:
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
            raise TeamInviteError(f"以下账号尚未切换到目标 Team 空间，无法上传: {joined}")

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
                raise TeamInviteError(f"无法构建收件配置: {account.email}")

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

    def _upload_selected_accounts(
        self,
        account_ids: List[int],
        *,
        require_platform_selection: bool = False,
    ) -> Dict[str, Any]:
        with get_db() as db:
            task = crud.get_team_invite_task(db, self.task_uuid)
            if not task:
                raise TeamInviteError("邀请任务不存在，无法上传")
            upload_config = task.upload_config or {}
            enabled_platforms = [item for item in _build_selected_platforms(upload_config) if item.get("enabled")]
            if require_platform_selection and not enabled_platforms:
                raise TeamInviteError("请先选择至少一个上传平台")

            team_context = {
                "team_task_uuid": task.task_uuid,
                "team_invite_task_uuid": task.task_uuid,
                "team_account_id": task.team_account_id,
                "team_workspace_id": task.team_workspace_id,
                "source_account_id": task.source_account_id,
                "include_source_account_upload": bool(upload_config.get("include_source_account_upload")),
                "members": [
                    {
                        "team_invite_member_id": member.id,
                        "account_id": member.account_id,
                        "email": member.email,
                        "source_type": member.source_type,
                        "invitation_status": member.invitation_status,
                    }
                    for member in sorted(task.members or [], key=lambda item: item.order_index)
                ],
            }
            results: Dict[str, Any] = {"team_context": team_context}

            if not account_ids:
                results["noop"] = {
                    "success_count": 0,
                    "failed_count": 0,
                    "skipped_count": len(task.members or []),
                    "details": [],
                }
                self._record_member_upload_results(account_ids, results)
                merged = self._merge_result({"team_context": team_context, "upload": results})
                self._update_task_fields(result=merged)
                return results

            sub2api_group_map = upload_config.get("sub2api_group_ids_by_service") or {}
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
                    group_ids_override = sub2api_group_map.get(str(service.id)) or sub2api_group_map.get(service.id) or []
                    self._log(f"上传到 Sub2API: {service.name}")
                    platform_results.append(
                        self._execute_platform_upload(
                            f"Sub2API 上传失败: {service.name}",
                            lambda service=service, group_ids_override=group_ids_override: batch_upload_to_sub2api(
                                account_ids,
                                api_url=service.api_url,
                                api_key=service.api_key,
                                team_context=team_context,
                                service_id=service.id,
                                group_ids_override=group_ids_override,
                            ),
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
                        self._execute_platform_upload(
                            f"CPA 上传失败: {service.name}",
                            lambda service=service: batch_upload_to_cpa(
                                account_ids,
                                api_url=service.api_url,
                                api_token=service.api_token,
                                team_context=team_context,
                                service_id=service.id,
                            ),
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
                        self._execute_platform_upload(
                            f"Team Manager 上传失败: {service.name}",
                            lambda service=service: batch_upload_to_team_manager(
                                account_ids,
                                api_url=service.api_url,
                                api_key=service.api_key,
                                team_context=team_context,
                                service_id=service.id,
                            ),
                        )
                    )
                results["tm"] = self._collapse_platform_results(platform_results)

            if len(results) == 1:
                results["noop"] = {
                    "success_count": len(account_ids),
                    "failed_count": 0,
                    "skipped_count": 0,
                    "details": [],
                }

            self._record_member_upload_results(account_ids, results)
            merged = self._merge_result({"team_context": team_context, "upload": results})
            self._update_task_fields(result=merged)
            return results

    def _execute_platform_upload(
        self,
        label: str,
        callback: Callable[[], Dict[str, Any]],
    ) -> Dict[str, Any]:
        def _wrapped() -> Dict[str, Any]:
            result = callback()
            failed_count = int((result or {}).get("failed_count") or 0)
            if failed_count <= 0:
                return result

            errors = [
                str(detail.get("error") or detail.get("message") or "").strip()
                for detail in (result or {}).get("details", [])
                if not detail.get("success")
            ]
            recoverable_errors = [item for item in errors if _is_recoverable_error_message(item)]
            if recoverable_errors:
                error_text = "；".join(item for item in recoverable_errors[:3] if item) or label
                raise TeamInviteError(error_text)
            return result

        return self._run_with_retries(label, _wrapped)

    def _record_member_upload_results(self, account_ids: Sequence[int], upload_results: Dict[str, Any]):
        member_ids_by_account: Dict[int, List[int]] = {}
        with get_db() as db:
            task = crud.get_team_invite_task(db, self.task_uuid)
            if not task:
                return
            for member in task.members:
                if member.account_id:
                    member_ids_by_account.setdefault(member.account_id, []).append(member.id)

        enabled_platform_keys = [
            key for key, value in upload_results.items() if key not in {"team_context", "noop"} and isinstance(value, dict)
        ]
        if not enabled_platform_keys:
            for account_id in account_ids:
                for member_id in member_ids_by_account.get(account_id, []):
                    member = self._reload_member(member_id)
                    if not member:
                        continue
                    result = dict(member.result or {})
                    platform_uploads = dict(result.get("platform_uploads") or {})
                    platform_uploads["noop"] = {"success": True, "message": "未配置外部上传平台，已跳过平台上传"}
                    result["platform_uploads"] = platform_uploads
                    result["last_action"] = "noop_upload"
                    self._update_member(
                        member_id,
                        invitation_status="uploaded",
                        uploaded_at=datetime.utcnow(),
                        error_message=None,
                        result=result,
                    )
            return

        per_member_platform: Dict[int, Dict[str, Dict[str, Any]]] = {}
        for platform_key in enabled_platform_keys:
            platform_result = upload_results.get(platform_key) or {}
            for detail in platform_result.get("details", []):
                account_id = detail.get("id")
                if not account_id:
                    continue
                success = bool(detail.get("success"))
                message = detail.get("message") or detail.get("error") or ""
                for member_id in member_ids_by_account.get(account_id, []):
                    member_entry = per_member_platform.setdefault(member_id, {})
                    platform_entry = member_entry.setdefault(
                        platform_key,
                        {
                            "success": True,
                            "messages": [],
                            "errors": [],
                            "copies": [],
                            "reason_codes": [],
                            "guard_messages": [],
                            "guard_blocked": False,
                        },
                    )
                    platform_entry["success"] = platform_entry["success"] and success
                    if success and message:
                        platform_entry["messages"].append(str(message))
                    if not success and message:
                        platform_entry["errors"].append(str(message))
                    reason_code = str(detail.get("reason_code") or "").strip()
                    if reason_code and reason_code not in platform_entry["reason_codes"]:
                        platform_entry["reason_codes"].append(reason_code)
                    guard_message = str(detail.get("guard_message") or "").strip()
                    if guard_message and guard_message not in platform_entry["guard_messages"]:
                        platform_entry["guard_messages"].append(guard_message)
                    platform_entry["guard_blocked"] = platform_entry["guard_blocked"] or bool(detail.get("guard_blocked"))
                    if platform_key == "sub2api" and any(
                        detail.get(field) is not None for field in ("generated_name", "group_id", "group_name", "copy_index")
                    ):
                        platform_entry["copies"].append(_build_sub2api_copy_result(detail))

        for account_id in account_ids:
            for member_id in member_ids_by_account.get(account_id, []):
                member = self._reload_member(member_id)
                if not member:
                    continue
                result = dict(member.result or {})
                platform_uploads = dict(result.get("platform_uploads") or {})
                platform_statuses = per_member_platform.get(member_id, {})
                errors: List[str] = []
                all_success = True
                for platform_key in enabled_platform_keys:
                    status = platform_statuses.get(platform_key)
                    if not status:
                        all_success = False
                        error_text = f"{platform_key} 缺少上传结果"
                        errors.append(error_text)
                        platform_uploads[platform_key] = {"success": False, "error": error_text}
                        if platform_key == "sub2api":
                            platform_uploads[platform_key]["copies"] = []
                            platform_uploads[platform_key]["copy_total"] = 0
                            platform_uploads[platform_key]["success_count"] = 0
                            platform_uploads[platform_key]["failed_count"] = 0
                        continue
                    copies = list(status.get("copies") or [])
                    copy_success_count = sum(1 for item in copies if item.get("success"))
                    copy_failed_count = sum(1 for item in copies if not item.get("success"))
                    guard_message = "；".join(status.get("guard_messages") or [])
                    reason_codes = list(status.get("reason_codes") or [])
                    if status["success"]:
                        if platform_key == "sub2api":
                            platform_uploads[platform_key] = {
                                "success": True,
                                "message": (
                                    f"已完成 {len(copies)} 份副本上传，成功 {copy_success_count} 份"
                                    if copies
                                    else "上传成功"
                                ),
                                "copies": copies,
                                "copy_total": len(copies),
                                "success_count": copy_success_count,
                                "failed_count": copy_failed_count,
                            }
                        else:
                            platform_uploads[platform_key] = {
                                "success": True,
                                "message": "；".join(status["messages"]) or "上传成功",
                            }
                    else:
                        all_success = False
                        error_text = "；".join(status["errors"]) or "上传失败"
                        errors.append(error_text)
                        if platform_key == "sub2api":
                            platform_uploads[platform_key] = {
                                "success": False,
                                "message": (
                                    f"已处理 {len(copies)} 份副本，成功 {copy_success_count} 份，失败 {copy_failed_count} 份"
                                    if copies
                                    else None
                                ),
                                "error": error_text,
                                "copies": copies,
                                "copy_total": len(copies),
                                "success_count": copy_success_count,
                                "failed_count": copy_failed_count,
                            }
                        else:
                            platform_uploads[platform_key] = {"success": False, "error": error_text}
                    if reason_codes:
                        platform_uploads[platform_key]["reason_codes"] = reason_codes
                        platform_uploads[platform_key]["reason_code"] = reason_codes[0]
                    if guard_message:
                        platform_uploads[platform_key]["guard_message"] = guard_message
                    if status.get("guard_blocked"):
                        platform_uploads[platform_key]["guard_blocked"] = True

                result["platform_uploads"] = platform_uploads
                result["last_action"] = "manual_upload" if len(account_ids) == 1 else "auto_upload"
                if all_success:
                    self._update_member(
                        member_id,
                        invitation_status="uploaded",
                        uploaded_at=datetime.utcnow(),
                        error_message=None,
                        result=result,
                    )
                else:
                    self._update_member(
                        member_id,
                        invitation_status="failed",
                        error_message="；".join(errors[:3]) if errors else "上传失败",
                        result=result,
                    )

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
            raise TeamInviteError(f"未找到可用的 {platform_name} 服务")
        services = list_getter(db)
        if not services:
            raise TeamInviteError(f"未配置可用的 {platform_name} 服务")
        return [services[0]]

    @staticmethod
    def _collapse_platform_results(results: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        merged = {
            "success_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "details": [],
            "services": list(results),
            "account_total": 0,
            "copy_total": 0,
        }
        account_ids = set()
        for item in results:
            merged["success_count"] += item.get("success_count", 0)
            merged["failed_count"] += item.get("failed_count", 0)
            merged["skipped_count"] += item.get("skipped_count", 0)
            details = list(item.get("details", []))
            guard_blocked = sum(1 for detail in details if detail.get("guard_blocked") and not detail.get("success"))
            if guard_blocked:
                merged["failed_count"] = max(0, merged["failed_count"] - guard_blocked)
                merged["skipped_count"] += guard_blocked
            for detail in details:
                merged["details"].append(detail)
                account_id = detail.get("id")
                if account_id:
                    account_ids.add(account_id)
                if detail.get("generated_name"):
                    merged["copy_total"] += 1
        merged["account_total"] = len(account_ids)
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

    def _get_task(self) -> Optional[TeamInviteTask]:
        with get_db() as db:
            task = crud.get_team_invite_task(db, self.task_uuid)
            if not task:
                return None
            task.members
            if task.source_account:
                task.source_account.email
            if task.source_team_task:
                task.source_team_task.task_uuid
            return task

    def _reload_member(self, member_id: int) -> Optional[TeamInviteMember]:
        with get_db() as db:
            member = crud.get_team_invite_member_by_id(db, member_id)
            if member and member.account:
                member.account.email
            return member

    def _set_task_status(self, status: str, **kwargs):
        self._update_task_fields(status=status, **kwargs)
        self._sync_status_snapshot()

    def _update_task_fields(self, **kwargs):
        with get_db() as db:
            crud.update_team_invite_task(db, self.task_uuid, **kwargs)

    def _update_member(self, member_id: int, **kwargs):
        with get_db() as db:
            crud.update_team_invite_member(db, member_id, **kwargs)

    def _merge_result(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        with get_db() as db:
            task = crud.get_team_invite_task(db, self.task_uuid)
            if not task:
                raise TeamInviteError("邀请任务不存在，无法更新结果")
            result = task.result.copy() if task.result else {}
            for key, value in updates.items():
                if isinstance(value, dict) and isinstance(result.get(key), dict):
                    result[key].update(value)
                else:
                    result[key] = value
            crud.update_team_invite_task(db, self.task_uuid, result=result)
            return result

    def _merge_member_result(self, member_id: int, updates: Dict[str, Any]) -> Dict[str, Any]:
        member = self._reload_member(member_id)
        if not member:
            raise TeamInviteError("邀请成员不存在")
        result = dict(member.result or {})
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key].update(value)
            else:
                result[key] = value
        self._update_member(member_id, result=result)
        return result

    def _log(self, message: str):
        if not re.match(r"^\[\d{2}:\d{2}:\d{2}\]", message):
            timestamp = datetime.now().strftime("%H:%M:%S")
            message = f"[{timestamp}] {message}"
        task_manager.add_log(self.task_uuid, message)
        with get_db() as db:
            crud.append_team_invite_task_log(db, self.task_uuid, message)

    def _sync_status_snapshot(self, runtime_message: Optional[str] = None):
        with get_db() as db:
            task = crud.get_team_invite_task(db, self.task_uuid)
            if not task:
                return
            runtime_status = task_manager.get_status(self.task_uuid) or {}
            if runtime_message is not None:
                runtime_status["runtime_message"] = runtime_message
            snapshot = build_team_invite_response(task, runtime_status)
        task_manager.update_status(
            self.task_uuid,
            snapshot["status"],
            snapshot=snapshot,
            runtime_message=snapshot.get("runtime_message"),
        )

    def _push_runtime(self, message: str):
        self._sync_status_snapshot(runtime_message=message)

    def _raise_if_cancelled(self):
        if task_manager.is_cancelled(self.task_uuid):
            raise TeamInviteCancelledError(self.task_uuid)

    def _run_with_retries(self, label: str, callback: Callable[[], Any]) -> Any:
        max_attempts = self._get_retry_limit() + 1
        last_error: Optional[Exception] = None
        for attempt in range(1, max_attempts + 1):
            self._raise_if_cancelled()
            try:
                return callback()
            except TeamInviteCancelledError:
                raise
            except Exception as exc:  # noqa: PERF203
                last_error = exc
                error_text = str(exc)
                if attempt >= max_attempts or not _is_recoverable_error_message(error_text):
                    raise
                self._log(f"{label} 失败，准备重试 {attempt}/{max_attempts - 1}: {error_text}")
                time.sleep(min(5, attempt))
        if last_error:
            raise last_error
        return None

    def _get_retry_limit(self) -> int:
        task = self._get_task()
        return _extract_retry_limit(task.upload_config if task else None)

    @staticmethod
    def _append_unique_account_id(account_ids: List[int], account_id: Optional[int]):
        if account_id and account_id not in account_ids:
            account_ids.append(account_id)

    def _mark_member_team_ready(
        self,
        member_id: int,
        *,
        reason: Optional[str] = None,
        last_action: Optional[str] = None,
    ):
        member = self._reload_member(member_id)
        if not member:
            return
        result = dict(member.result or {})
        if reason:
            result["reason"] = reason
        if last_action:
            result["last_action"] = last_action
        self._update_member(
            member_id,
            invitation_status="accepted",
            invitation_accepted_at=member.invitation_accepted_at or datetime.utcnow(),
            error_message=None,
            result=result,
        )

    @staticmethod
    def _has_upload_failures(upload_results: Dict[str, Any]) -> bool:
        for platform_key, platform_result in upload_results.items():
            if platform_key == "team_context" or not isinstance(platform_result, dict):
                continue
            if int(platform_result.get("failed_count") or 0) > 0:
                return True
        return False

    def _cancel_task(self, message: str):
        with get_db() as db:
            task = crud.get_team_invite_task(db, self.task_uuid)
            if not task:
                return
            for member in task.members:
                if member.invitation_status not in {"uploaded", "accepted", "failed", "skipped", "invite_only"}:
                    member.invitation_status = "cancelled"
            task.status = "cancelled"
            task.completed_at = datetime.utcnow()
            task.error_message = message
            db.commit()
        self._log(message)
        self._push_runtime(message)

    def _fail_task(self, error_message: str):
        with get_db() as db:
            task = crud.get_team_invite_task(db, self.task_uuid)
            if not task:
                return
            task.status = "failed"
            task.error_message = error_message
            task.completed_at = datetime.utcnow()
            db.commit()
        self._log(f"任务失败: {error_message}")
        self._push_runtime(error_message)
