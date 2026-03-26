"""
Grok 注册 API 路由。
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func

from ...config.settings import get_settings, update_settings
from ...core.dynamic_proxy import get_proxy_url_for_task
from ...core.grok.register_workflow import (
    GrokRegisterOrchestrator,
    RUNNING_GROK_STATUSES,
    build_grok_response,
)
from ...core.grok.runtime import (
    get_flaresolverr_manager,
    get_local_solver_manager,
    inspect_local_solver_service,
    probe_local_solver_service,
)
from ...database import grok_crud
from ...database.grok_models import GrokRegisterTask
from ...database.session import get_db
from ...web.task_manager import task_manager

router = APIRouter()


def _secret_value(secret) -> str:
    return secret.get_secret_value() if secret else ""


def _service_id_or_none(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _default_config_payload() -> Dict[str, Any]:
    settings = get_settings()
    return {
        "target_count": settings.grok_default_target_count,
        "thread_count": settings.grok_default_thread_count,
        "proxy": settings.grok_default_proxy or settings.proxy_url or None,
        "email_domain": settings.grok_default_email_domain,
        "email_service_type": settings.grok_default_email_service_type or "auto",
        "email_service_id": _service_id_or_none(settings.grok_default_email_service_id),
        "captcha_mode": settings.grok_default_captcha_mode,
        "solver_url": settings.grok_default_solver_url,
        "solver_command": settings.grok_solver_command,
        "flaresolverr_url": settings.grok_default_flaresolverr_url,
        "managed_solver_port": settings.grok_managed_solver_port,
        "flaresolverr_command": settings.grok_flaresolverr_command,
        "has_bczy_api_key": bool(_secret_value(settings.grok_default_bczy_api_key)),
        "has_yescaptcha_key": bool(_secret_value(settings.grok_default_yescaptcha_key)),
    }


def _resolved_task_config(request: "GrokRegisterCreateRequest") -> Dict[str, Any]:
    settings = get_settings()
    bczy_api_key = request.bczy_api_key if request.bczy_api_key is not None else _secret_value(settings.grok_default_bczy_api_key)
    yescaptcha_key = request.yescaptcha_key if request.yescaptcha_key is not None else _secret_value(settings.grok_default_yescaptcha_key)
    solver_url = request.solver_url or settings.grok_default_solver_url
    solver_command = request.solver_command if request.solver_command is not None else settings.grok_solver_command
    flaresolverr_url = request.flaresolverr_url or settings.grok_default_flaresolverr_url
    email_service_type = request.email_service_type or settings.grok_default_email_service_type or "auto"
    email_service_id = request.email_service_id if request.email_service_id is not None else _service_id_or_none(settings.grok_default_email_service_id)
    return {
        "bczy_api_key": bczy_api_key,
        "yescaptcha_key": yescaptcha_key,
        "solver_url": solver_url,
        "solver_command": solver_command,
        "flaresolverr_url": flaresolverr_url,
        "email_service_type": email_service_type,
        "email_service_id": email_service_id,
        "email_code_timeout": settings.email_code_timeout,
        "email_code_poll_interval": settings.email_code_poll_interval,
    }


def _resolve_task_proxy(request_proxy: Optional[str], settings) -> Optional[str]:
    if request_proxy is not None:
        from ...core.dynamic_proxy import normalize_proxy_input
        return normalize_proxy_input(str(request_proxy))

    saved = str(settings.grok_default_proxy or "").strip()
    if saved:
        return saved

    fallback = get_proxy_url_for_task()
    return str(fallback).strip() or None if fallback else None


def _serialize_task(task: GrokRegisterTask) -> Dict[str, Any]:
    runtime = task_manager.get_status(task.task_uuid) or {}
    return build_grok_response(task, runtime)


def _local_solver_port(url: Optional[str]) -> Optional[int]:
    text = str(url or "").strip()
    if not text:
        return None
    parsed = urlparse(text)
    if parsed.scheme not in {"http", "https"}:
        return None
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        return None
    if parsed.port:
        return int(parsed.port)
    return 443 if parsed.scheme == "https" else 80


def _ensure_local_solver_ready(*, solver_url: Optional[str], solver_command: Optional[str], managed_port: int) -> str:
    target_url = str(solver_url or "").strip() or f"http://127.0.0.1:{managed_port}"
    if probe_local_solver_service(target_url):
        return target_url

    port = _local_solver_port(target_url)
    if port is None:
        raise HTTPException(status_code=400, detail="Local solver is not reachable.")

    try:
        status = get_local_solver_manager().start(port, command=solver_command)
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    resolved_url = str(status.get("url") or target_url).strip() or target_url
    if not probe_local_solver_service(resolved_url):
        raise HTTPException(status_code=400, detail="Local solver is not reachable.")
    return resolved_url


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


async def _run_grok_task(task_uuid: str):
    loop = task_manager.get_loop() or asyncio.get_event_loop()
    task_manager.set_loop(loop)
    await loop.run_in_executor(task_manager.executor, GrokRegisterOrchestrator(task_uuid).run)


class GrokRegisterCreateRequest(BaseModel):
    target_count: int = Field(default=1, ge=1, le=200)
    thread_count: int = Field(default=1, ge=1, le=10)
    proxy: Optional[str] = None
    email_domain: str = "bczy.site"
    email_service_type: Optional[str] = None
    email_service_id: Optional[int] = None
    captcha_mode: str = Field(default="yescaptcha", pattern="^(yescaptcha|local)$")
    bczy_api_key: Optional[str] = None
    yescaptcha_key: Optional[str] = None
    solver_url: Optional[str] = None
    solver_command: Optional[str] = None
    flaresolverr_url: Optional[str] = None


class GrokRegisterConfigUpdateRequest(BaseModel):
    target_count: Optional[int] = Field(default=None, ge=1, le=200)
    thread_count: Optional[int] = Field(default=None, ge=1, le=10)
    proxy: Optional[str] = None
    email_domain: Optional[str] = None
    email_service_type: Optional[str] = None
    email_service_id: Optional[int] = None
    captcha_mode: Optional[str] = Field(default=None, pattern="^(yescaptcha|local)$")
    solver_url: Optional[str] = None
    solver_command: Optional[str] = None
    flaresolverr_url: Optional[str] = None
    bczy_api_key: Optional[str] = None
    yescaptcha_key: Optional[str] = None
    managed_solver_port: Optional[int] = Field(default=None, ge=1, le=65535)
    flaresolverr_command: Optional[str] = None


class GrokRuntimeActionRequest(BaseModel):
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    url: Optional[str] = None
    command: Optional[str] = None


@router.get("/config")
async def get_grok_register_config():
    return _default_config_payload()


@router.put("/config")
async def update_grok_register_config(request: GrokRegisterConfigUpdateRequest):
    payload: Dict[str, Any] = {}
    provided_fields = request.model_fields_set
    if request.target_count is not None:
        payload["grok_default_target_count"] = request.target_count
    if request.thread_count is not None:
        payload["grok_default_thread_count"] = request.thread_count
    if request.proxy is not None:
        payload["grok_default_proxy"] = request.proxy
    if request.email_domain is not None:
        payload["grok_default_email_domain"] = request.email_domain
    if request.email_service_type is not None:
        payload["grok_default_email_service_type"] = request.email_service_type
    if "email_service_id" in provided_fields:
        payload["grok_default_email_service_id"] = str(request.email_service_id or "")
    if request.captcha_mode is not None:
        payload["grok_default_captcha_mode"] = request.captcha_mode
    if request.solver_url is not None:
        payload["grok_default_solver_url"] = request.solver_url
    if request.solver_command is not None:
        payload["grok_solver_command"] = request.solver_command
    if request.flaresolverr_url is not None:
        payload["grok_default_flaresolverr_url"] = request.flaresolverr_url
    if request.managed_solver_port is not None:
        payload["grok_managed_solver_port"] = request.managed_solver_port
    if request.flaresolverr_command is not None:
        payload["grok_flaresolverr_command"] = request.flaresolverr_command
    if request.bczy_api_key is not None:
        payload["grok_default_bczy_api_key"] = request.bczy_api_key
    if request.yescaptcha_key is not None:
        payload["grok_default_yescaptcha_key"] = request.yescaptcha_key
    if payload:
        update_settings(**payload)
    return _default_config_payload()


@router.post("/create")
async def create_grok_register_task(request: GrokRegisterCreateRequest, background_tasks: BackgroundTasks):
    settings = get_settings()
    proxy = _resolve_task_proxy(request.proxy, settings)
    captcha_mode = request.captcha_mode or settings.grok_default_captcha_mode
    task_config = _resolved_task_config(request)

    if captcha_mode == "local":
        task_config["solver_url"] = _ensure_local_solver_ready(
            solver_url=task_config.get("solver_url"),
            solver_command=task_config.get("solver_command"),
            managed_port=settings.grok_managed_solver_port,
        )

    task_uuid = str(uuid.uuid4())
    with get_db() as db:
        task = grok_crud.create_grok_task(
            db,
            task_uuid=task_uuid,
            target_count=request.target_count,
            thread_count=request.thread_count,
            proxy=proxy,
            email_domain=request.email_domain or settings.grok_default_email_domain,
            captcha_mode=captcha_mode,
            config=task_config,
        )
        task_manager.update_status(task_uuid, "pending", snapshot=_serialize_task(task))

    background_tasks.add_task(_run_grok_task, task_uuid)
    with get_db() as db:
        created = grok_crud.get_grok_task(db, task_uuid)
        return _serialize_task(created)


@router.get("/tasks")
async def list_grok_register_tasks(
    status: Optional[str] = Query(default=None),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
):
    with get_db() as db:
        query = db.query(GrokRegisterTask)
        if status:
            query = query.filter(GrokRegisterTask.status == status)
        total = query.with_entities(func.count(GrokRegisterTask.id)).scalar() or 0
        tasks = query.order_by(GrokRegisterTask.created_at.desc()).offset(skip).limit(limit).all()
        return {"total": total, "tasks": [_serialize_task(task) for task in tasks]}


@router.get("/{task_uuid}")
async def get_grok_register_task(task_uuid: str):
    with get_db() as db:
        task = grok_crud.get_grok_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Grok register task not found.")
        return _serialize_task(task)


@router.get("/{task_uuid}/logs")
async def get_grok_register_logs(task_uuid: str):
    with get_db() as db:
        task = grok_crud.get_grok_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Grok register task not found.")
        return {"task_uuid": task_uuid, "logs": _combine_logs(task_uuid, task.logs)}


@router.post("/{task_uuid}/cancel")
async def cancel_grok_register_task(task_uuid: str):
    with get_db() as db:
        task = grok_crud.get_grok_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Grok register task not found.")
        if task.status not in RUNNING_GROK_STATUSES:
            raise HTTPException(status_code=400, detail="Current task cannot be cancelled.")
        task_manager.cancel_task(task_uuid)
        if task.status == "pending":
            grok_crud.update_grok_task(
                db,
                task_uuid,
                status="cancelled",
                completed_at=datetime.utcnow(),
                error_message="Task cancelled before execution.",
            )
            task = grok_crud.get_grok_task(db, task_uuid)
            task_manager.update_status(task_uuid, "cancelled", snapshot=_serialize_task(task))
            return {"success": True, "message": "Task cancelled."}
    return {"success": True, "message": "Cancellation requested."}


@router.delete("/{task_uuid}")
async def delete_grok_register_task(task_uuid: str):
    with get_db() as db:
        task = grok_crud.get_grok_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="Grok register task not found.")
        if task.status in RUNNING_GROK_STATUSES:
            raise HTTPException(status_code=400, detail="Cannot delete a running Grok task.")
        grok_crud.delete_grok_task(db, task_uuid)
    task_manager.cleanup_task(task_uuid)
    return {"success": True, "message": "Task deleted."}


@router.post("/solver/start")
async def start_local_solver(request: GrokRuntimeActionRequest = Body(default=GrokRuntimeActionRequest())):
    settings = get_settings()
    port = request.port or settings.grok_managed_solver_port
    try:
        return get_local_solver_manager().start(port, command=request.command or settings.grok_solver_command)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/solver/stop")
async def stop_local_solver():
    return get_local_solver_manager().stop()


@router.get("/solver/status")
async def get_local_solver_status(url: Optional[str] = Query(default=None)):
    manager = get_local_solver_manager()
    if url:
        details = inspect_local_solver_service(url)
        return {
            "running": manager.is_running(),
            "managed": manager.is_running(),
            "healthy": bool(details.get("healthy")),
            "placeholder": bool(details.get("placeholder")),
            "url": url,
            "command": manager.status().get("command"),
            "last_error": None,
        }
    return manager.status()


@router.post("/flaresolverr/start")
async def start_flaresolverr(request: GrokRuntimeActionRequest = Body(default=GrokRuntimeActionRequest())):
    settings = get_settings()
    command = request.command or settings.grok_flaresolverr_command
    url = request.url or settings.grok_default_flaresolverr_url
    try:
        return get_flaresolverr_manager().start(command, url=url)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/flaresolverr/stop")
async def stop_flaresolverr(request: GrokRuntimeActionRequest = Body(default=GrokRuntimeActionRequest())):
    return get_flaresolverr_manager().stop(url=request.url)


@router.get("/flaresolverr/status")
async def get_flaresolverr_status(url: Optional[str] = Query(default=None)):
    settings = get_settings()
    return get_flaresolverr_manager().status(url=url or settings.grok_default_flaresolverr_url)
