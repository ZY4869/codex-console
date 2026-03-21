"""
Team 创建 API 路由。
"""

import asyncio
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func

from ...core.team_orchestrator import (
    TeamOrchestrator,
    build_team_response,
    extract_email_domain_from_config,
    get_supported_team_service_values,
    is_supported_team_service,
    normalize_email_service_config,
)
from ...database import crud
from ...database.models import EmailService, TeamTask
from ...database.session import get_db
from ...services import EmailServiceType
from ...web.task_manager import task_manager
from .registration import get_proxy_for_registration

router = APIRouter()

RUNNING_TEAM_STATUSES = {"registering", "verifying", "inviting", "accepting", "uploading"}
CANCELLABLE_TEAM_STATUSES = RUNNING_TEAM_STATUSES | {"waiting_subscription"}


class TeamCreateRequest(BaseModel):
    email_service_id: int
    workspace_name: str = "MyTeam"
    proxy: Optional[str] = None
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


class TeamTaskListResponse(BaseModel):
    total: int
    tasks: List[TeamTaskResponse]


class TeamLogsResponse(BaseModel):
    task_uuid: str
    logs: List[str] = Field(default_factory=list)


def _serialize_team_task(task: TeamTask) -> TeamTaskResponse:
    return TeamTaskResponse(**build_team_response(task))


async def _run_team_phase_one(task_uuid: str):
    loop = task_manager.get_loop() or asyncio.get_event_loop()
    task_manager.set_loop(loop)
    await loop.run_in_executor(task_manager.executor, TeamOrchestrator(task_uuid).run_registration_phase)


async def _run_team_phase_two(task_uuid: str):
    loop = task_manager.get_loop() or asyncio.get_event_loop()
    task_manager.set_loop(loop)
    await loop.run_in_executor(task_manager.executor, TeamOrchestrator(task_uuid).run_post_subscription_phase)


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
            workspace_name=request.workspace_name,
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
        task_manager.update_status(task_uuid, "pending", snapshot=build_team_response(team_task))

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
        logs = [line for line in (task.logs or "").splitlines() if line.strip()]
        return TeamLogsResponse(task_uuid=task_uuid, logs=logs)


@router.post("/{task_uuid}/confirm-subscription")
async def confirm_team_subscription(task_uuid: str, background_tasks: BackgroundTasks):
    with get_db() as db:
        task = crud.get_team_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Team 任务不存在")
        if task.status != "waiting_subscription":
            raise HTTPException(status_code=400, detail="当前状态不允许确认订阅")
        if not task.main_account_id:
            raise HTTPException(status_code=400, detail="主账号尚未注册完成")
        crud.update_team_task(db, task_uuid, status="verifying", error_message=None, completed_at=None)
        task = crud.get_team_task(db, task_uuid)
        task_manager.update_status(task_uuid, "verifying", snapshot=build_team_response(task))

    background_tasks.add_task(_run_team_phase_two, task_uuid)
    return {"success": True, "message": "已开始执行订阅校验与邀请流程"}


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
            task_manager.update_status(task_uuid, "cancelled", snapshot=build_team_response(task))
            return {"success": True, "message": "任务已取消"}

        return {"success": True, "message": "已提交取消请求，当前阶段会在安全点停止"}


@router.delete("/{task_uuid}")
async def delete_team_task(task_uuid: str):
    with get_db() as db:
        task = crud.get_team_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Team 任务不存在")
        if task.status not in {"completed", "failed", "cancelled"}:
            raise HTTPException(status_code=400, detail="当前状态不允许删除")

        registration_task_uuids = [member.registration_task_uuid for member in task.members if member.registration_task_uuid]
        for reg_task_uuid in registration_task_uuids:
            reg_task = crud.get_registration_task(db, reg_task_uuid)
            if reg_task and reg_task.status != "running":
                db.delete(reg_task)
        db.delete(task)
        db.commit()

    task_manager.cleanup_task(task_uuid)
    return {"success": True, "message": "任务已删除"}
