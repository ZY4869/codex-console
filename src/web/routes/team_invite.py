"""
Team 邀请 API 路由。
"""

import asyncio
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func

from ...core.team_invite_workflow import TeamInviteOrchestrator, build_team_invite_response
from ...core.upload.sub2api_groups import fetch_sub2api_groups
from ...core.upload.sub2api_naming import (
    discover_sub2api_identity_occupied_name_indices,
    extract_sub2api_group_identities,
    get_sub2api_preview_identities,
    next_available_index,
)
from ...core.upload.sub2api_payload import format_sub2api_name, normalize_sub2api_template_config
from ...database import crud
from ...database.models import Account, Sub2ApiService, TeamInviteTask, TeamTask
from ...database.session import get_db
from ...web.task_manager import task_manager
from .registration import get_proxy_for_registration

router = APIRouter()

RUNNING_INVITE_STATUSES = {"verifying", "inviting", "accepting", "uploading"}
CANCELLABLE_INVITE_STATUSES = RUNNING_INVITE_STATUSES | {"pending"}


class TeamInviteCreateRequest(BaseModel):
    source_mode: str
    source_account_id: Optional[int] = None
    source_team_task_uuid: Optional[str] = None
    existing_account_ids: List[int] = Field(default_factory=list)
    team_source_task_uuids: List[str] = Field(default_factory=list)
    manual_emails: List[str] = Field(default_factory=list)
    proxy: Optional[str] = None
    auto_upload_sub2api: bool = False
    sub2api_service_ids: List[int] = Field(default_factory=list)
    sub2api_group_ids_by_service: Dict[str, List[int]] = Field(default_factory=dict)
    auto_upload_cpa: bool = False
    cpa_service_ids: List[int] = Field(default_factory=list)
    auto_upload_tm: bool = False
    tm_service_ids: List[int] = Field(default_factory=list)
    retry_limit: int = Field(default=0, ge=0, le=10)


class TeamInviteTaskResponse(BaseModel):
    id: int
    task_uuid: str
    status: str
    source_mode: str
    proxy: Optional[str] = None
    team_account_id: Optional[str] = None
    team_workspace_id: Optional[str] = None
    upload_config: Dict[str, Any] = Field(default_factory=dict)
    logs: Optional[str] = None
    error_message: Optional[str] = None
    result: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    source_account: Optional[Dict[str, Any]] = None
    source_team_task_uuid: Optional[str] = None
    members: List[Dict[str, Any]] = Field(default_factory=list)
    stats: Dict[str, int] = Field(default_factory=dict)
    runtime_message: Optional[str] = None
    resume_available: bool = False
    restart_available: bool = False
    retry_limit: int = 0
    selected_platforms: List[Dict[str, Any]] = Field(default_factory=list)
    team_member_count: int = 0
    custom_member_count: int = 0
    recommended_actions: Dict[str, Any] = Field(default_factory=dict)


class TeamInviteRuntimeConfigRequest(BaseModel):
    proxy: Optional[str] = None
    auto_upload_sub2api: Optional[bool] = None
    sub2api_service_ids: Optional[List[int]] = None
    sub2api_group_ids_by_service: Optional[Dict[str, List[int]]] = None
    auto_upload_cpa: Optional[bool] = None
    cpa_service_ids: Optional[List[int]] = None
    auto_upload_tm: Optional[bool] = None
    tm_service_ids: Optional[List[int]] = None
    retry_limit: Optional[int] = Field(default=None, ge=0, le=10)


class TeamInviteMemberActionResponse(BaseModel):
    success: bool = True
    task: TeamInviteTaskResponse
    member: Dict[str, Any]
    result: Dict[str, Any] = Field(default_factory=dict)


class TeamInviteBatchMemberActionRequest(TeamInviteRuntimeConfigRequest):
    member_ids: List[int] = Field(default_factory=list)


class TeamInviteBatchActionResponse(BaseModel):
    success: bool = True
    task: TeamInviteTaskResponse
    result: Dict[str, Any] = Field(default_factory=dict)


class TeamInviteSub2ApiNamePreviewRequest(BaseModel):
    service_id: int
    group_id: int = Field(ge=1)


class TeamInviteSub2ApiNamePreviewResponse(BaseModel):
    service_id: int
    group_id: int
    group_name: str
    next_index: int
    preview_name: str
    digits: int
    matched_identities: List[str] = Field(default_factory=list)
    preview_names: List[Dict[str, Any]] = Field(default_factory=list)


class TeamInviteTaskListResponse(BaseModel):
    total: int
    tasks: List[TeamInviteTaskResponse]


class TeamInviteLogsResponse(BaseModel):
    task_uuid: str
    logs: List[str] = Field(default_factory=list)


class TeamInviteSourceAccountResponse(BaseModel):
    id: int
    email: str
    remark: Optional[str] = None
    status: str
    source: Optional[str] = None
    team_task_uuid: Optional[str] = None
    team_role: Optional[str] = None
    team_order_index: Optional[int] = None


class TeamInviteSourceTaskResponse(BaseModel):
    task_uuid: str
    status: str
    workspace_name: str
    email_domain: Optional[str] = None
    team_account_id: Optional[str] = None
    team_workspace_id: Optional[str] = None
    proxy: Optional[str] = None
    main_account_id: Optional[int] = None
    member_count: int = 0
    main_account: Optional[Dict[str, Any]] = None
    members: List[TeamInviteSourceAccountResponse] = Field(default_factory=list)


class TeamInviteSourcesResponse(BaseModel):
    source_accounts: List[TeamInviteSourceAccountResponse] = Field(default_factory=list)
    accounts: List[TeamInviteSourceAccountResponse] = Field(default_factory=list)
    team_tasks: List[TeamInviteSourceTaskResponse] = Field(default_factory=list)


def _load_sub2api_group_name(service: Sub2ApiService, group_id: int) -> str:
    fallback_name = f"Group {group_id}"
    if not service.api_url or not service.api_key:
        return fallback_name
    try:
        groups = fetch_sub2api_groups(service.api_url, service.api_key, platform="openai")
    except Exception:
        return fallback_name
    for group in groups or []:
        try:
            current_group_id = int(group.get("id"))
        except (AttributeError, TypeError, ValueError):
            continue
        if current_group_id == int(group_id):
            return str(group.get("name") or fallback_name)
    return fallback_name


def _build_sub2api_preview_names(
    service: Sub2ApiService,
    *,
    group_id: int,
    group_name: str,
    digits: int,
    fallback_index: int,
) -> List[Dict[str, Any]]:
    preview_names: List[Dict[str, Any]] = []
    for identity in get_sub2api_preview_identities(group_name):
        next_index = fallback_index
        if service.api_url and service.api_key:
            try:
                occupied_indices = discover_sub2api_identity_occupied_name_indices(
                    service.api_url,
                    service.api_key,
                    group_id,
                    identity,
                    digits,
                    platform="openai",
                )
                next_index = next_available_index(occupied_indices)
            except Exception:
                next_index = fallback_index
        preview_names.append(
            {
                "identity": identity,
                "next_index": next_index,
                "preview_name": format_sub2api_name({"name_digits": digits}, next_index, identity),
            }
        )
    return preview_names


def _serialize_task(task: TeamInviteTask) -> TeamInviteTaskResponse:
    runtime = task_manager.get_status(task.task_uuid) or {}
    return TeamInviteTaskResponse(**build_team_invite_response(task, runtime))


def _combine_logs(task_uuid: str, persisted_logs: Optional[str]) -> List[str]:
    merged: List[str] = []
    seen = set()
    for source in ((persisted_logs or "").splitlines(), task_manager.get_logs(task_uuid)):
        for line in source:
            if not line or line in seen:
                continue
            seen.add(line)
            merged.append(line)
    return merged


def _parse_manual_emails(values: List[str]) -> List[str]:
    emails: List[str] = []
    for value in values or []:
        for piece in re.split(r"[\s,;]+", value.strip()):
            candidate = piece.strip().lower()
            if candidate and re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", candidate):
                emails.append(candidate)
    return emails


def _build_upload_config(
    request: TeamInviteCreateRequest | TeamInviteRuntimeConfigRequest,
    current_config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    config = dict(current_config or {})
    for key in (
        "auto_upload_sub2api",
        "sub2api_service_ids",
        "sub2api_group_ids_by_service",
        "auto_upload_cpa",
        "cpa_service_ids",
        "auto_upload_tm",
        "tm_service_ids",
        "retry_limit",
    ):
        value = getattr(request, key, None)
        if value is not None:
            config[key] = value

    config.setdefault("auto_upload_sub2api", False)
    config.setdefault("sub2api_service_ids", [])
    config.setdefault("sub2api_group_ids_by_service", {})
    config.setdefault("auto_upload_cpa", False)
    config.setdefault("cpa_service_ids", [])
    config.setdefault("auto_upload_tm", False)
    config.setdefault("tm_service_ids", [])
    config.setdefault("retry_limit", 0)
    return config


def _apply_runtime_config(
    db,
    task: TeamInviteTask,
    request: Optional[TeamInviteRuntimeConfigRequest],
) -> TeamInviteTask:
    if not request:
        return task
    updates: Dict[str, Any] = {
        "upload_config": _build_upload_config(request, task.upload_config),
    }
    if request.proxy is not None:
        updates["proxy"] = request.proxy.strip() or None
    updated = crud.update_team_invite_task(db, task.task_uuid, **updates)
    return updated or task


async def _run_team_invite_task(task_uuid: str):
    loop = task_manager.get_loop() or asyncio.get_event_loop()
    task_manager.set_loop(loop)
    await loop.run_in_executor(task_manager.executor, TeamInviteOrchestrator(task_uuid).run)


async def _run_team_invite_member_accept(task_uuid: str, member_id: int, *, force_refresh: bool = False):
    loop = task_manager.get_loop() or asyncio.get_event_loop()
    task_manager.set_loop(loop)
    return await loop.run_in_executor(
        task_manager.executor,
        lambda: TeamInviteOrchestrator(task_uuid).accept_or_refresh_member(member_id, force_refresh=force_refresh),
    )


async def _run_team_invite_member_upload(task_uuid: str, member_id: int):
    loop = task_manager.get_loop() or asyncio.get_event_loop()
    task_manager.set_loop(loop)
    return await loop.run_in_executor(
        task_manager.executor,
        lambda: TeamInviteOrchestrator(task_uuid).upload_member(member_id),
    )


async def _run_team_invite_batch_relogin(task_uuid: str, member_ids: Optional[List[int]] = None):
    loop = task_manager.get_loop() or asyncio.get_event_loop()
    task_manager.set_loop(loop)
    return await loop.run_in_executor(
        task_manager.executor,
        lambda: TeamInviteOrchestrator(task_uuid).relogin_members(member_ids),
    )


def _serialize_source_account(
    account: Account,
    *,
    team_task: Optional[TeamTask] = None,
    team_role: Optional[str] = None,
    team_order_index: Optional[int] = None,
) -> TeamInviteSourceAccountResponse:
    extra_data = account.extra_data or {}
    return TeamInviteSourceAccountResponse(
        id=account.id,
        email=account.email,
        remark=account.remark,
        status=account.status,
        source=account.source,
        team_task_uuid=team_task.task_uuid if team_task else extra_data.get("team_task_uuid"),
        team_role=team_role if team_role is not None else extra_data.get("team_role"),
        team_order_index=team_order_index if team_order_index is not None else extra_data.get("team_order_index"),
    )


def _serialize_source_team_task(task: TeamTask) -> TeamInviteSourceTaskResponse:
    members = sorted(task.members or [], key=lambda item: item.order_index)
    serialized_members = [
        _serialize_source_account(
            member.account,
            team_task=task,
            team_role=member.role,
            team_order_index=member.order_index,
        )
        for member in members
        if member.account
    ]
    main_account = None
    main_account_id = None
    if task.main_account:
        main_account_id = task.main_account.id
        main_account = {
            "id": task.main_account.id,
            "email": task.main_account.email,
            "remark": task.main_account.remark,
            "account_id": task.main_account.account_id,
            "workspace_id": task.main_account.workspace_id,
        }
    return TeamInviteSourceTaskResponse(
        task_uuid=task.task_uuid,
        status=task.status,
        workspace_name=task.workspace_name,
        email_domain=task.email_domain,
        team_account_id=task.team_account_id,
        team_workspace_id=task.team_workspace_id,
        proxy=task.proxy,
        main_account_id=main_account_id,
        member_count=len(serialized_members),
        main_account=main_account,
        members=serialized_members,
    )


def _resolve_source_team_task_for_account(db, account: Optional[Account]) -> Optional[TeamTask]:
    if not account:
        return None

    team_task = (
        db.query(TeamTask)
        .filter(TeamTask.main_account_id == account.id)
        .order_by(TeamTask.created_at.desc(), TeamTask.id.desc())
        .first()
    )
    if team_task:
        return team_task

    extra_data = account.extra_data or {}
    team_task_uuid = extra_data.get("team_task_uuid")
    if team_task_uuid:
        return crud.get_team_task(db, str(team_task_uuid))
    return None


def _resolve_effective_proxy(
    db,
    request_proxy: Optional[str],
    source_team_task: Optional[TeamTask],
    source_account: Optional[Account],
) -> Optional[str]:
    direct_proxy = (request_proxy or "").strip()
    if direct_proxy:
        return direct_proxy

    team_proxy = ((source_team_task.proxy if source_team_task else None) or "").strip()
    if team_proxy:
        return team_proxy

    account_proxy = ((source_account.proxy_used if source_account else None) or "").strip()
    if account_proxy:
        return account_proxy

    fallback_proxy, _ = get_proxy_for_registration(db)
    return fallback_proxy


def _is_member_team_ready(member, team_account_id: Optional[str]) -> bool:
    account = getattr(member, "account", None)
    normalized_team_account_id = str(team_account_id or "").strip()
    if not account or not normalized_team_account_id:
        return False
    return (
        bool(account.access_token)
        and str(account.account_id or "").strip() == normalized_team_account_id
        and str(account.workspace_id or "").strip() == normalized_team_account_id
        and str(account.subscription_type or "").strip() == "team"
    )


def _should_skip_member_on_restart(member, team_account_id: Optional[str]) -> bool:
    if member.invitation_status in {"accepted", "uploaded"}:
        return True
    return _is_member_team_ready(member, team_account_id)


@router.get("/sources", response_model=TeamInviteSourcesResponse)
async def get_team_invite_sources(
    account_limit: int = Query(default=500, ge=1, le=2000),
    team_task_limit: int = Query(default=200, ge=1, le=1000),
):
    with get_db() as db:
        accounts = (
            db.query(Account)
            .order_by(Account.created_at.desc(), Account.id.desc())
            .limit(account_limit)
            .all()
        )
        team_tasks = (
            db.query(TeamTask)
            .filter(TeamTask.main_account_id.isnot(None))
            .order_by(TeamTask.created_at.desc(), TeamTask.id.desc())
            .limit(team_task_limit)
            .all()
        )

        source_accounts: List[TeamInviteSourceAccountResponse] = []
        seen_source_account_ids = set()
        for task in team_tasks:
            if not task.main_account or task.main_account.id in seen_source_account_ids:
                continue
            seen_source_account_ids.add(task.main_account.id)
            source_accounts.append(
                _serialize_source_account(
                    task.main_account,
                    team_task=task,
                    team_role="admin",
                    team_order_index=0,
                )
            )

        return TeamInviteSourcesResponse(
            source_accounts=source_accounts,
            accounts=[_serialize_source_account(account) for account in accounts],
            team_tasks=[_serialize_source_team_task(task) for task in team_tasks],
        )


@router.post("/create", response_model=TeamInviteTaskResponse)
async def create_team_invite_task(request: TeamInviteCreateRequest, background_tasks: BackgroundTasks):
    with get_db() as db:
        if request.source_mode not in {"account", "team_task"}:
            raise HTTPException(status_code=400, detail="source_mode 必须是 account 或 team_task")

        source_team_task = None
        if request.source_mode == "account":
            if not request.source_account_id:
                raise HTTPException(status_code=400, detail="source_account_id 不能为空")
            source_account = crud.get_account_by_id(db, request.source_account_id)
            if not source_account:
                raise HTTPException(status_code=404, detail="主账号不存在")
            source_team_task = _resolve_source_team_task_for_account(db, source_account)
        else:
            if not request.source_team_task_uuid:
                raise HTTPException(status_code=400, detail="source_team_task_uuid 不能为空")
            source_team_task = crud.get_team_task(db, request.source_team_task_uuid)
            if not source_team_task or not source_team_task.main_account_id:
                raise HTTPException(status_code=404, detail="源 Team 任务不存在或缺少主账号")
            source_account = crud.get_account_by_id(db, source_team_task.main_account_id)
            if not source_account:
                raise HTTPException(status_code=404, detail="源 Team 主账号不存在")

        excluded_email = (source_account.email or "").lower()
        upload_config = _build_upload_config(request)

        candidates: Dict[str, Dict[str, Any]] = {}
        order = 0

        def upsert_candidate(
            email: str,
            *,
            priority: int,
            source_type: str,
            account_id: Optional[int] = None,
            source_team_task_id: Optional[int] = None,
        ):
            nonlocal order
            if not email or email == excluded_email:
                return
            candidate = candidates.get(email)
            if candidate is None:
                candidates[email] = {
                    "email": email,
                    "priority": priority,
                    "source_type": source_type,
                    "account_id": account_id,
                    "source_team_task_id": source_team_task_id,
                    "order": order,
                }
                order += 1
                return
            if priority > candidate["priority"]:
                candidate.update(
                    {
                        "priority": priority,
                        "source_type": source_type,
                        "account_id": account_id,
                        "source_team_task_id": source_team_task_id,
                    }
                )

        if source_team_task:
            for member in source_team_task.members or []:
                if member.account and member.account.email:
                    upsert_candidate(
                        member.account.email.lower(),
                        priority=4,
                        source_type="team_task",
                        account_id=member.account.id,
                        source_team_task_id=source_team_task.id,
                    )

        for account_id in request.existing_account_ids:
            account = crud.get_account_by_id(db, account_id)
            if account and account.email:
                upsert_candidate(account.email.lower(), priority=3, source_type="account", account_id=account.id)

        for team_task_uuid in request.team_source_task_uuids:
            team_task = crud.get_team_task(db, team_task_uuid)
            if not team_task:
                continue
            for member in team_task.members or []:
                if member.account and member.account.email:
                    upsert_candidate(
                        member.account.email.lower(),
                        priority=2,
                        source_type="team_task",
                        account_id=member.account.id,
                        source_team_task_id=team_task.id,
                    )

        for email in _parse_manual_emails(request.manual_emails):
            upsert_candidate(email, priority=1, source_type="manual")

        if not candidates:
            raise HTTPException(status_code=400, detail="至少选择一个邀请对象")

        task_uuid = str(uuid.uuid4())
        task = crud.create_team_invite_task(
            db,
            task_uuid=task_uuid,
            source_mode=request.source_mode,
            source_account_id=source_account.id,
            source_team_task_id=source_team_task.id if source_team_task else None,
            proxy=_resolve_effective_proxy(db, request.proxy, source_team_task, source_account),
            upload_config=upload_config,
        )

        for index, candidate in enumerate(sorted(candidates.values(), key=lambda item: item["order"])):
            crud.create_team_invite_member(
                db,
                team_invite_task_id=task.id,
                order_index=index,
                email=candidate["email"],
                source_type=candidate["source_type"],
                account_id=candidate["account_id"],
                source_team_task_id=candidate["source_team_task_id"],
            )

        db.refresh(task)
        task_manager.update_status(
            task_uuid,
            "pending",
            snapshot=build_team_invite_response(task, task_manager.get_status(task_uuid)),
        )

    background_tasks.add_task(_run_team_invite_task, task_uuid)
    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        return _serialize_task(task)


@router.get("/tasks", response_model=TeamInviteTaskListResponse)
async def list_team_invite_tasks(
    status: Optional[str] = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
):
    with get_db() as db:
        query = db.query(TeamInviteTask)
        if status:
            query = query.filter(TeamInviteTask.status == status)
        total = query.with_entities(func.count(TeamInviteTask.id)).scalar() or 0
        tasks = query.order_by(TeamInviteTask.created_at.desc()).offset(skip).limit(limit).all()
        return TeamInviteTaskListResponse(total=total, tasks=[_serialize_task(task) for task in tasks])


@router.get("/{task_uuid}", response_model=TeamInviteTaskResponse)
async def get_team_invite_task(task_uuid: str):
    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Team 邀请任务不存在")
        return _serialize_task(task)


@router.get("/{task_uuid}/logs", response_model=TeamInviteLogsResponse)
async def get_team_invite_logs(task_uuid: str):
    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Team 邀请任务不存在")
        return TeamInviteLogsResponse(task_uuid=task_uuid, logs=_combine_logs(task_uuid, task.logs))


@router.post("/sub2api-name-preview", response_model=TeamInviteSub2ApiNamePreviewResponse)
async def preview_team_invite_sub2api_name(request: TeamInviteSub2ApiNamePreviewRequest):
    with get_db() as db:
        service = crud.get_sub2api_service_by_id(db, request.service_id)
        if not service:
            raise HTTPException(status_code=404, detail="Sub2API 服务不存在")

        template_config = normalize_sub2api_template_config(service.template_config)
        digits = int(template_config["name_digits"])
        fallback_index = max(1, int(service.next_name_index or 1))
        group_name = _load_sub2api_group_name(service, request.group_id)
        matched_identities = extract_sub2api_group_identities(group_name)
        preview_names = _build_sub2api_preview_names(
            service,
            group_id=request.group_id,
            group_name=group_name,
            digits=digits,
            fallback_index=fallback_index,
        )
        primary_preview = preview_names[0] if preview_names else {
            "identity": "Free",
            "next_index": fallback_index,
            "preview_name": format_sub2api_name(template_config, fallback_index, "Free"),
        }

        return TeamInviteSub2ApiNamePreviewResponse(
            service_id=service.id,
            group_id=request.group_id,
            group_name=group_name,
            next_index=int(primary_preview["next_index"]),
            preview_name=str(primary_preview["preview_name"]),
            digits=digits,
            matched_identities=matched_identities,
            preview_names=preview_names,
        )


@router.post("/{task_uuid}/resume", response_model=TeamInviteTaskResponse)
async def resume_team_invite_task(
    task_uuid: str,
    background_tasks: BackgroundTasks,
    request: Optional[TeamInviteRuntimeConfigRequest] = Body(default=None),
):
    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Team 邀请任务不存在")
        task = _apply_runtime_config(db, task, request)
        snapshot = build_team_invite_response(task, task_manager.get_status(task_uuid))
        if task.status in RUNNING_INVITE_STATUSES:
            return TeamInviteTaskResponse(**snapshot)
        if not snapshot.get("resume_available"):
            raise HTTPException(status_code=400, detail="当前任务没有可继续的成员")

        task_manager.cleanup_task(task_uuid)
        crud.update_team_invite_task(
            db,
            task_uuid,
            status="pending",
            completed_at=None,
            error_message=None,
        )
        task = crud.get_team_invite_task(db, task_uuid)
        task_manager.update_status(
            task_uuid,
            "pending",
            snapshot=build_team_invite_response(task, task_manager.get_status(task_uuid)),
            runtime_message="任务已重新排队，准备继续未完成成员",
        )

    background_tasks.add_task(_run_team_invite_task, task_uuid)
    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        return _serialize_task(task)


@router.post("/{task_uuid}/restart", response_model=TeamInviteTaskResponse)
async def restart_team_invite_task(
    task_uuid: str,
    background_tasks: BackgroundTasks,
    request: Optional[TeamInviteRuntimeConfigRequest] = Body(default=None),
):
    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Team 邀请任务不存在")
        if task.status in RUNNING_INVITE_STATUSES:
            raise HTTPException(status_code=400, detail="任务运行中，暂时不能重新开始")

        upload_config = _build_upload_config(request or TeamInviteRuntimeConfigRequest(), task.upload_config)
        proxy = task.proxy
        if request and request.proxy is not None:
            proxy = request.proxy.strip() or None

        restart_members = [
            member
            for member in sorted(task.members or [], key=lambda item: item.order_index)
            if not _should_skip_member_on_restart(member, task.team_account_id)
        ]
        if not restart_members:
            raise HTTPException(status_code=400, detail="当前任务没有需要重新开始的成员")

        new_task_uuid = str(uuid.uuid4())
        new_task = crud.create_team_invite_task(
            db,
            task_uuid=new_task_uuid,
            source_mode=task.source_mode,
            source_account_id=task.source_account_id,
            source_team_task_id=task.source_team_task_id,
            proxy=proxy,
            upload_config=upload_config,
        )
        for index, member in enumerate(restart_members):
            crud.create_team_invite_member(
                db,
                team_invite_task_id=new_task.id,
                order_index=index,
                email=member.email,
                source_type=member.source_type,
                account_id=member.account_id,
                source_team_task_id=member.source_team_task_id,
            )
        db.refresh(new_task)
        task_manager.update_status(
            new_task_uuid,
            "pending",
            snapshot=build_team_invite_response(new_task, task_manager.get_status(new_task_uuid)),
            runtime_message="已根据原任务配置创建新的续跑任务",
        )

    background_tasks.add_task(_run_team_invite_task, new_task_uuid)
    with get_db() as db:
        task = crud.get_team_invite_task(db, new_task_uuid)
        return _serialize_task(task)


@router.post("/{task_uuid}/members/{member_id}/accept", response_model=TeamInviteMemberActionResponse)
async def accept_team_invite_member(
    task_uuid: str,
    member_id: int,
    request: Optional[TeamInviteRuntimeConfigRequest] = Body(default=None),
):
    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Team 邀请任务不存在")
        if task.status in RUNNING_INVITE_STATUSES:
            raise HTTPException(status_code=400, detail="任务运行中，暂不支持手动成员操作")
        member = crud.get_team_invite_member_by_id(db, member_id)
        if not member or member.team_invite_task_id != task.id:
            raise HTTPException(status_code=404, detail="邀请成员不存在")
        _apply_runtime_config(db, task, request)

    member_payload = await _run_team_invite_member_accept(task_uuid, member_id, force_refresh=False)
    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        return TeamInviteMemberActionResponse(success=True, task=_serialize_task(task), member=member_payload)


@router.post("/{task_uuid}/members/{member_id}/refresh-team", response_model=TeamInviteMemberActionResponse)
async def refresh_team_invite_member(
    task_uuid: str,
    member_id: int,
    request: Optional[TeamInviteRuntimeConfigRequest] = Body(default=None),
):
    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Team 邀请任务不存在")
        if task.status in RUNNING_INVITE_STATUSES:
            raise HTTPException(status_code=400, detail="任务运行中，暂不支持手动成员操作")
        member = crud.get_team_invite_member_by_id(db, member_id)
        if not member or member.team_invite_task_id != task.id:
            raise HTTPException(status_code=404, detail="邀请成员不存在")
        _apply_runtime_config(db, task, request)

    member_payload = await _run_team_invite_member_accept(task_uuid, member_id, force_refresh=True)
    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        return TeamInviteMemberActionResponse(success=True, task=_serialize_task(task), member=member_payload)


@router.post("/{task_uuid}/members/{member_id}/upload", response_model=TeamInviteMemberActionResponse)
async def upload_team_invite_member(
    task_uuid: str,
    member_id: int,
    request: Optional[TeamInviteRuntimeConfigRequest] = Body(default=None),
):
    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Team 邀请任务不存在")
        if task.status in RUNNING_INVITE_STATUSES:
            raise HTTPException(status_code=400, detail="任务运行中，暂不支持手动成员操作")
        member = crud.get_team_invite_member_by_id(db, member_id)
        if not member or member.team_invite_task_id != task.id:
            raise HTTPException(status_code=404, detail="邀请成员不存在")
        _apply_runtime_config(db, task, request)

    result = await _run_team_invite_member_upload(task_uuid, member_id)
    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        runtime = task_manager.get_status(task_uuid) or {}
        member_payload = build_team_invite_response(task, runtime)["members"]
        selected_member = next((item for item in member_payload if item["id"] == member_id), {})
        return TeamInviteMemberActionResponse(
            success=True,
            task=_serialize_task(task),
            member=selected_member,
            result=result,
        )


@router.post("/{task_uuid}/members/relogin", response_model=TeamInviteBatchActionResponse)
async def relogin_team_invite_members(
    task_uuid: str,
    request: TeamInviteBatchMemberActionRequest,
):
    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Team 邀请任务不存在")
        if task.status in RUNNING_INVITE_STATUSES:
            raise HTTPException(status_code=400, detail="任务运行中，暂不支持批量一键重登")
        _apply_runtime_config(db, task, request)

    result = await _run_team_invite_batch_relogin(task_uuid, request.member_ids)
    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        return TeamInviteBatchActionResponse(
            success=True,
            task=_serialize_task(task),
            result=result,
        )


@router.post("/{task_uuid}/cancel")
async def cancel_team_invite_task(task_uuid: str):
    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Team 邀请任务不存在")
        if task.status not in CANCELLABLE_INVITE_STATUSES:
            raise HTTPException(status_code=400, detail="当前状态不允许取消")

        task_manager.cancel_task(task_uuid)
        if task.status == "pending":
            for member in task.members:
                if member.invitation_status not in {"uploaded", "accepted", "failed", "skipped", "invite_only"}:
                    member.invitation_status = "cancelled"
            task.status = "cancelled"
            task.completed_at = datetime.utcnow()
            task.error_message = "任务已取消"
            db.commit()
            task_manager.update_status(
                task_uuid,
                "cancelled",
                snapshot=build_team_invite_response(task, task_manager.get_status(task_uuid)),
                runtime_message="任务已取消",
            )
            return {"success": True, "message": "任务已取消"}

    return {"success": True, "message": "已提交取消请求，当前阶段会在安全点停止"}


@router.delete("/{task_uuid}")
async def delete_team_invite_task(task_uuid: str):
    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Team 邀请任务不存在")
        crud.delete_team_invite_task(db, task_uuid)
    return {"success": True, "message": "任务已删除"}
