"""
Team 创建 API 路由。
"""

import asyncio
import uuid
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func

from ...core.team_workflow import (
    TeamOrchestrator,
    build_team_response,
    extract_email_domain_from_config,
    get_supported_team_service_values,
    is_supported_team_service,
    normalize_email_service_config,
)
from ...core.team_name_builder import build_team_workspace_name
from ...database import crud
from ...database.models import EmailService, TeamTask
from ...database.session import get_db
from ...services import EmailServiceType
from ...web.task_manager import task_manager
from .registration import get_proxy_for_registration

router = APIRouter()

RUNNING_TEAM_STATUSES = {"registering", "verifying", "inviting", "accepting", "uploading"}
CANCELLABLE_TEAM_STATUSES = RUNNING_TEAM_STATUSES | {"waiting_subscription"}


class TeamNamePartRequest(BaseModel):
    bucket: Optional[str] = None
    mode: Literal["random", "custom"] = "random"
    value: Optional[str] = None


class TeamCreateRequest(BaseModel):
    email_service_id: int
    workspace_name: str = "MyTeam"
    workspace_name_parts: List[TeamNamePartRequest] = Field(default_factory=list)
    proxy: Optional[str] = None
    auto_upload_sub2api: bool = False
    sub2api_service_ids: List[int] = Field(default_factory=list)
    auto_upload_cpa: bool = False
    cpa_service_ids: List[int] = Field(default_factory=list)
    auto_upload_tm: bool = False
    tm_service_ids: List[int] = Field(default_factory=list)


class TeamManualUploadRequest(BaseModel):
    auto_upload_sub2api: bool = False
    sub2api_service_ids: List[int] = Field(default_factory=list)
    auto_upload_cpa: bool = False
    cpa_service_ids: List[int] = Field(default_factory=list)
    auto_upload_tm: bool = False
    tm_service_ids: List[int] = Field(default_factory=list)


class TeamTaskResponse(BaseModel):
    id: int
    task_uuid: str
    status: str
    email_service_id: Optional[int] = None
    email_domain: Optional[str] = None
    proxy: Optional[str] = None
    workspace_name: str
    team_account_id: Optional[str] = None
    team_workspace_id: Optional[str] = None
    continue_requested: bool = False
    upload_config: Dict[str, Any] = Field(default_factory=dict)
    logs: Optional[str] = None
    error_message: Optional[str] = None
    result: Dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    main_account: Optional[Dict[str, Any]] = None
    members: List[Dict[str, Any]] = Field(default_factory=list)
    stats: Dict[str, int] = Field(default_factory=dict)
    retrying: bool = False
    current_member_index: Optional[int] = None
    current_member_attempt: Optional[int] = None
    max_member_attempts: Optional[int] = None
    next_retry_in_seconds: Optional[int] = None
    runtime_message: Optional[str] = None


class TeamTaskListResponse(BaseModel):
    total: int
    tasks: List[TeamTaskResponse]


class TeamLogsResponse(BaseModel):
    task_uuid: str
    logs: List[str] = Field(default_factory=list)


def _build_runtime_snapshot(task: TeamTask) -> Dict[str, Any]:
    runtime = task_manager.get_status(task.task_uuid) or {}
    return build_team_response(task, runtime)


def _serialize_team_task(task: TeamTask) -> TeamTaskResponse:
    return TeamTaskResponse(**_build_runtime_snapshot(task))


def _combine_team_logs(task_uuid: str, persisted_logs: Optional[str]) -> List[str]:
    merged: List[str] = []
    seen = set()
    for source in ((persisted_logs or "").splitlines(), task_manager.get_logs(task_uuid)):
        for line in source:
            if not line or line in seen:
                continue
            seen.add(line)
            merged.append(line)
    return merged


async def _run_team_phase_one(task_uuid: str):
    loop = task_manager.get_loop() or asyncio.get_event_loop()
    task_manager.set_loop(loop)
    await loop.run_in_executor(task_manager.executor, TeamOrchestrator(task_uuid).run_registration_phase)


async def _run_team_phase_two(task_uuid: str):
    loop = task_manager.get_loop() or asyncio.get_event_loop()
    task_manager.set_loop(loop)
    await loop.run_in_executor(task_manager.executor, TeamOrchestrator(task_uuid).run_post_subscription_phase)


async def _run_team_manual_upload(task_uuid: str, upload_config: Dict[str, Any]):
    loop = task_manager.get_loop() or asyncio.get_event_loop()
    task_manager.set_loop(loop)
    await loop.run_in_executor(task_manager.executor, TeamOrchestrator(task_uuid).run_manual_upload, upload_config)


def _build_upload_config_payload(request: Optional[TeamManualUploadRequest]) -> Optional[Dict[str, Any]]:
    if request is None:
        return None
    return {
        "auto_upload_sub2api": request.auto_upload_sub2api,
        "sub2api_service_ids": request.sub2api_service_ids,
        "auto_upload_cpa": request.auto_upload_cpa,
        "cpa_service_ids": request.cpa_service_ids,
        "auto_upload_tm": request.auto_upload_tm,
        "tm_service_ids": request.tm_service_ids,
    }


@router.get("/available-email-services")
async def get_available_team_email_services():
    with get_db() as db:
        services = (
            db.query(EmailService)
            .filter(
                EmailService.enabled.is_(True),
                EmailService.service_type.in_(get_supported_team_service_values()),
            )
            .order_by(EmailService.priority.asc(), EmailService.id.asc())
            .all()
        )

        return {
            "supported_types": get_supported_team_service_values(),
            "services": [
                {
                    "id": service.id,
                    "name": service.name,
                    "service_type": service.service_type,
                    "domain": extract_email_domain_from_config(service.config),
                    "priority": service.priority,
                }
                for service in services
            ],
        }


@router.post("/create", response_model=TeamTaskResponse)
async def create_team_task(request: TeamCreateRequest, background_tasks: BackgroundTasks):
    with get_db() as db:
        email_service = crud.get_email_service_by_id(db, request.email_service_id)
        if not email_service or not email_service.enabled:
            raise HTTPException(status_code=400, detail="邮箱服务不存在或已禁用")
        if not is_supported_team_service(email_service.service_type):
            raise HTTPException(status_code=400, detail="Team 页面仅支持 moe_mail、freemail、temp_mail")

        actual_proxy = request.proxy
        if not actual_proxy:
            actual_proxy, _ = get_proxy_for_registration(db)

        service_type = EmailServiceType(email_service.service_type)
        config = normalize_email_service_config(service_type, email_service.config, actual_proxy)
        email_domain = extract_email_domain_from_config(config)
        workspace_name = build_team_workspace_name(
            request.workspace_name,
            [item.model_dump() for item in request.workspace_name_parts],
        )
        task_uuid = str(uuid.uuid4())
        upload_config = {
            "auto_upload_sub2api": request.auto_upload_sub2api,
            "sub2api_service_ids": request.sub2api_service_ids,
            "auto_upload_cpa": request.auto_upload_cpa,
            "cpa_service_ids": request.cpa_service_ids,
            "auto_upload_tm": request.auto_upload_tm,
            "tm_service_ids": request.tm_service_ids,
        }
        team_task = crud.create_team_task(
            db,
            task_uuid=task_uuid,
            email_service_id=request.email_service_id,
            workspace_name=workspace_name,
            proxy=actual_proxy,
            email_domain=email_domain,
            upload_config=upload_config,
        )

        for index in range(5):
            reg_task_uuid = str(uuid.uuid4())
            crud.create_registration_task(
                db,
                task_uuid=reg_task_uuid,
                email_service_id=request.email_service_id,
                proxy=actual_proxy,
            )
            crud.create_team_member(
                db,
                team_task_id=team_task.id,
                order_index=index,
                role="admin" if index == 0 else "member",
                registration_task_uuid=reg_task_uuid,
            )

        db.refresh(team_task)
        task_manager.update_status(task_uuid, "pending", snapshot=_build_runtime_snapshot(team_task))

    background_tasks.add_task(_run_team_phase_one, task_uuid)

    with get_db() as db:
        task = crud.get_team_task(db, task_uuid)
        return _serialize_team_task(task)


@router.get("/tasks", response_model=TeamTaskListResponse)
async def list_team_tasks(
    status: Optional[str] = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
):
    with get_db() as db:
        query = db.query(TeamTask)
        if status:
            query = query.filter(TeamTask.status == status)
        total = query.with_entities(func.count(TeamTask.id)).scalar() or 0
        tasks = query.order_by(TeamTask.created_at.desc()).offset(skip).limit(limit).all()
        return TeamTaskListResponse(total=total, tasks=[_serialize_team_task(task) for task in tasks])


@router.get("/{task_uuid}", response_model=TeamTaskResponse)
async def get_team_task(task_uuid: str):
    with get_db() as db:
        task = crud.get_team_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Team 任务不存在")
        return _serialize_team_task(task)


@router.get("/{task_uuid}/logs", response_model=TeamLogsResponse)
async def get_team_task_logs(task_uuid: str):
    with get_db() as db:
        task = crud.get_team_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Team 任务不存在")
        logs = _combine_team_logs(task_uuid, task.logs)
        return TeamLogsResponse(task_uuid=task_uuid, logs=logs)


@router.post("/{task_uuid}/confirm-subscription")
async def confirm_team_subscription(task_uuid: str, background_tasks: BackgroundTasks):
    with get_db() as db:
        task = crud.get_team_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Team 任务不存在")

        updates: Dict[str, Any] = {
            "continue_requested_at": datetime.utcnow(),
            "completed_at": None,
        }

        if task.status in {"completed", "failed", "cancelled"}:
            crud.update_team_task(db, task_uuid, **updates)
            task = crud.get_team_task(db, task_uuid)
            task_manager.update_status(task_uuid, task.status, snapshot=_build_runtime_snapshot(task), runtime_message="当前任务已结束，如需继续请新建任务")
            return {"success": True, "message": "当前任务已结束，如需继续请新建任务", "queued": False, "started": False, "noop": True}

        if task.status in RUNNING_TEAM_STATUSES:
            crud.update_team_task(db, task_uuid, **updates)
            task = crud.get_team_task(db, task_uuid)
            task_manager.update_status(task_uuid, task.status, snapshot=_build_runtime_snapshot(task), runtime_message="第二阶段已在执行，无需重复点击")
            return {"success": True, "message": "第二阶段已在执行，无需重复点击", "queued": False, "started": False, "noop": True}

        if task.status in {"pending", "registering"} or not task.main_account_id:
            crud.update_team_task(db, task_uuid, **updates)
            task = crud.get_team_task(db, task_uuid)
            task_manager.update_status(task_uuid, task.status, snapshot=_build_runtime_snapshot(task), runtime_message="已记录继续请求，注册完成后会自动继续")
            return {"success": True, "message": "已记录继续请求，注册完成后会自动继续", "queued": True, "started": False, "noop": False}

        crud.update_team_task(db, task_uuid, status="verifying", error_message=None, **updates)
        task = crud.get_team_task(db, task_uuid)
        task_manager.update_status(task_uuid, "verifying", snapshot=_build_runtime_snapshot(task), runtime_message="开始校验 Team 订阅状态")

    background_tasks.add_task(_run_team_phase_two, task_uuid)
    return {"success": True, "message": "已开始执行 Team 订阅校验与邀请流程", "queued": False, "started": True, "noop": False}


@router.post("/{task_uuid}/upload")
async def upload_team_task(
    task_uuid: str,
    background_tasks: BackgroundTasks,
    request: Optional[TeamManualUploadRequest] = None,
):
    upload_config = _build_upload_config_payload(request)

    with get_db() as db:
        task = crud.get_team_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Team 任务不存在")

        if task.status in RUNNING_TEAM_STATUSES:
            task_manager.update_status(
                task_uuid,
                task.status,
                snapshot=_build_runtime_snapshot(task),
                runtime_message="当前 Team 任务正在执行，请稍后再试上传",
            )
            return {
                "success": True,
                "message": "当前 Team 任务正在执行，请稍后再试上传",
                "queued": False,
                "started": False,
                "noop": True,
            }

        if not task.team_account_id:
            raise HTTPException(status_code=400, detail="Team 创建尚未完成，暂时无法上传到平台")

        effective_upload_config = upload_config or (task.upload_config or {})
        if not any(
            effective_upload_config.get(flag)
            for flag in ("auto_upload_sub2api", "auto_upload_cpa", "auto_upload_tm")
        ):
            raise HTTPException(status_code=400, detail="请至少选择一个上传平台")

        crud.update_team_task(
            db,
            task_uuid,
            status="uploading",
            error_message=None,
            completed_at=None,
            upload_config=effective_upload_config,
        )
        updated_task = crud.get_team_task(db, task_uuid)
        task_manager.update_status(
            task_uuid,
            "uploading",
            snapshot=_build_runtime_snapshot(updated_task),
            runtime_message="正在准备手动上传到平台",
        )

    background_tasks.add_task(_run_team_manual_upload, task_uuid, effective_upload_config)
    return {
        "success": True,
        "message": "已开始手动上传 Team 账号到平台",
        "queued": False,
        "started": True,
        "noop": False,
    }


@router.post("/{task_uuid}/cancel")
async def cancel_team_task(task_uuid: str):
    with get_db() as db:
        task = crud.get_team_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Team 任务不存在")
        if task.status not in CANCELLABLE_TEAM_STATUSES:
            raise HTTPException(status_code=400, detail="当前状态不允许取消")

        task_manager.cancel_task(task_uuid)
        if task.status == "waiting_subscription":
            for member in task.members:
                if member.invitation_status not in {"accepted", "uploaded", "failed"}:
                    member.invitation_status = "cancelled"
            task.status = "cancelled"
            task.completed_at = datetime.utcnow()
            task.error_message = "任务已取消"
            db.commit()
            task_manager.update_status(task_uuid, "cancelled", snapshot=_build_runtime_snapshot(task), runtime_message="任务已取消")
            return {"success": True, "message": "任务已取消"}

        return {"success": True, "message": "已提交取消请求，当前阶段会在安全点停止"}


@router.delete("/{task_uuid}")
async def delete_team_task(task_uuid: str):
    with get_db() as db:
        task = crud.get_team_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Team 任务不存在")
        crud.delete_team_task(db, task_uuid)
    return {"success": True, "message": "任务已删除"}
