"""
注册任务 API 路由
"""

import asyncio
import logging
import uuid
import random
import time
from datetime import datetime, timezone
from typing import Any, List, Optional, Dict, Tuple, Literal

from fastapi import APIRouter, HTTPException, Query, BackgroundTasks
from pydantic import BaseModel, ConfigDict, Field

from ...config.constants import (
    RoleTag,
    normalize_role_tag,
    role_tag_to_account_label,
)
from ...config.settings import get_settings, Settings
from ...core.auto_registration import (
    add_auto_registration_log,
    get_auto_registration_inventory,
    get_auto_registration_logs,
    get_auto_registration_state,
    update_auto_registration_state,
)
from ...core.enhanced_protocol_register import (
    AdaptiveProtocolRegistrationEngine,
    EnhancedProtocolRegistrationEngine,
)
from ...core.register import RegistrationEngine, RegistrationResult, RegistrationCancelledError
from ...core.timezone_utils import utcnow_naive
from ...database import crud
from ...database.models import RegistrationTask, ScheduledRegistrationJob, Proxy, Account
from ...database.session import get_db
from ...services import EmailServiceFactory, EmailServiceType
from ..schedule_utils import normalize_schedule_config, compute_next_run_at, describe_schedule
from ..task_manager import task_manager
from .registration_selection import (
    DOMAIN_SELECTION_TYPES,
    RegistrationSelectionExhaustedError,
    RegistrationSelectionRequest,
    build_service_options,
    normalize_email_service_config,
    resolve_email_service_for_registration,
)

logger = logging.getLogger(__name__)
router = APIRouter()

RETRY_BACKOFF_SECONDS = (2, 4, 6, 8, 10)
UPLOAD_RETRY_MODE_REUSE_SAVED_ACCOUNT = "reuse_saved_account"
_TASK_RESULT_UNSET = object()

# 任务存储（简单的内存存储，生产环境应使用 Redis）
running_tasks: dict = {}
# 批量任务存储
batch_tasks: Dict[str, dict] = {}


class RegistrationAttemptError(Exception):
    """保留结构化注册失败结果，供运行时重试策略做精细分支。"""

    def __init__(self, result: RegistrationResult, email_service: str):
        self.result = result
        self.email_service = email_service
        super().__init__(result.error_message or "注册失败")


def _normalize_registration_mode(registration_mode: Optional[str]) -> str:
    mode = str(registration_mode or "").strip().lower()
    return "browser" if mode == "browser" else "protocol"


def _normalize_protocol_variant(protocol_variant: Optional[str]) -> str:
    variant = str(protocol_variant or "").strip().lower()
    if variant == "adaptive":
        return "adaptive"
    if variant == "enhanced":
        return "enhanced"
    return "legacy"


def _get_registration_flow_key(
    registration_mode: Optional[str],
    protocol_variant: Optional[str] = None,
) -> str:
    normalized_mode = _normalize_registration_mode(registration_mode)
    if normalized_mode == "browser":
        return "browser"
    return f"protocol.{_normalize_protocol_variant(protocol_variant)}"


def _cancel_batch_tasks(batch_id: str) -> None:
    """同步取消批量任务下的所有子任务，并更新自动注册状态。"""
    batch = batch_tasks.get(batch_id)
    if not batch:
        return

    for task_uuid in batch.get("task_uuids", []):
        task_manager.cancel_task(task_uuid)

    auto_state = get_auto_registration_state()
    if auto_state.get("current_batch_id") == batch_id:
        update_auto_registration_state(
            status="cancelling",
            message=f"自动补货取消中: {batch_id}",
        )
        add_auto_registration_log(f"[自动注册] 已提交补货批量任务取消请求: {batch_id}")


# ============== Proxy Helper Functions ==============

def get_proxy_for_registration(db) -> Tuple[Optional[str], Optional[int]]:
    """
    获取用于注册的代理

    策略：
    1. 优先从代理列表中随机选择一个启用的代理
    2. 如果代理列表为空且启用了动态代理，调用动态代理 API 获取
    3. 否则使用系统设置中的静态默认代理

    Returns:
        Tuple[proxy_url, proxy_id]: 代理 URL 和代理 ID（如果来自代理列表）
    """
    # 先尝试从代理列表中获取
    proxy = crud.get_random_proxy(db)
    if proxy:
        return proxy.proxy_url, proxy.id

    # 代理列表为空，尝试动态代理或静态代理
    from ...core.dynamic_proxy import get_proxy_url_for_task
    proxy_url = get_proxy_url_for_task()
    if proxy_url:
        return proxy_url, None

    return None, None


def update_proxy_usage(db, proxy_id: Optional[int]):
    """更新代理的使用时间"""
    if proxy_id:
        crud.update_proxy_last_used(db, proxy_id)


# ============== Proxy Test ==============

class ProxyTestRequest(BaseModel):
    """代理测试请求"""
    proxy: str


@router.post("/proxy/test")
async def test_proxy_connection(request: ProxyTestRequest):
    """测试用户输入的代理字符串是否可用"""
    from ...core.dynamic_proxy import normalize_proxy_input

    proxy_url = normalize_proxy_input(request.proxy)
    if not proxy_url:
        raise HTTPException(status_code=400, detail="代理地址不能为空")

    import time
    from curl_cffi import requests as cffi_requests
    try:
        proxies = {"http": proxy_url, "https": proxy_url}
        start = time.time()
        resp = cffi_requests.get(
            "https://api.ipify.org?format=json",
            proxies=proxies,
            timeout=10,
            impersonate="chrome110",
        )
        elapsed = round((time.time() - start) * 1000)
        if resp.status_code == 200:
            ip = resp.json().get("ip", "")
            return {
                "success": True,
                "proxy_url": proxy_url,
                "ip": ip,
                "response_time": elapsed,
                "message": f"代理可用，出口 IP: {ip}，响应时间: {elapsed}ms",
            }
        return {"success": False, "proxy_url": proxy_url, "message": f"代理连接失败: HTTP {resp.status_code}"}
    except Exception as e:
        return {"success": False, "proxy_url": proxy_url, "message": f"代理连接失败: {e}"}


# ============== Pydantic Models ==============

class RegistrationTaskCreate(BaseModel):
    """创建注册任务请求"""
    email_service_type: str = "tempmail"
    protocol_variant: Literal["legacy", "enhanced", "adaptive"] = "legacy"
    registration_mode: str = "protocol"  # "protocol" 或 "browser"
    proxy: Optional[str] = None
    email_service_config: Optional[dict] = None
    email_service_id: Optional[int] = None
    random_email_service: bool = False
    random_outlook_account: bool = False
    random_domain: bool = False
    subdomain_only: bool = False
    selected_email_addresses: List[str] = Field(default_factory=list)
    selected_domains: List[str] = Field(default_factory=list)
    auto_upload_cpa: bool = False
    cpa_service_ids: List[int] = Field(default_factory=list)  # 指定 CPA 服务 ID 列表，空则取第一个启用的
    auto_upload_sub2api: bool = False
    sub2api_service_ids: List[int] = Field(default_factory=list)  # 指定 Sub2API 服务 ID 列表
    auto_upload_tm: bool = False
    tm_service_ids: List[int] = Field(default_factory=list)  # 指定 TM 服务 ID 列表
    auto_upload_new_api: bool = False
    new_api_service_ids: List[int] = Field(default_factory=list)
    registration_type: str = RoleTag.CHILD.value


class BatchRegistrationRequest(BaseModel):
    """批量注册请求"""
    count: int = 1
    email_service_type: str = "tempmail"
    protocol_variant: Literal["legacy", "enhanced", "adaptive"] = "legacy"
    registration_mode: str = "protocol"  # "protocol" 或 "browser"
    proxy: Optional[str] = None
    email_service_config: Optional[dict] = None
    email_service_id: Optional[int] = None
    random_email_service: bool = False
    random_outlook_account: bool = False
    random_domain: bool = False
    subdomain_only: bool = False
    selected_email_addresses: List[str] = Field(default_factory=list)
    selected_domains: List[str] = Field(default_factory=list)
    interval_min: int = 5
    interval_max: int = 30
    concurrency: int = 1
    mode: str = "pipeline"
    auto_upload_cpa: bool = False
    cpa_service_ids: List[int] = Field(default_factory=list)
    auto_upload_sub2api: bool = False
    sub2api_service_ids: List[int] = Field(default_factory=list)
    auto_upload_tm: bool = False
    tm_service_ids: List[int] = Field(default_factory=list)
    auto_upload_new_api: bool = False
    new_api_service_ids: List[int] = Field(default_factory=list)
    registration_type: str = RoleTag.CHILD.value


class RegistrationTaskResponse(BaseModel):
    """注册任务响应"""
    id: int
    task_uuid: str
    status: str
    email_service_id: Optional[int] = None
    proxy: Optional[str] = None
    logs: Optional[str] = None
    result: Optional[dict] = None
    error_message: Optional[str] = None
    attempt: Optional[int] = None
    max_attempts: Optional[int] = None
    retrying: Optional[bool] = None
    last_error: Optional[str] = None
    next_retry_in_seconds: Optional[int] = None
    email: Optional[str] = None
    email_service: Optional[str] = None
    created_at: Optional[str] = None
    started_at: Optional[str] = None
    completed_at: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class BatchRegistrationResponse(BaseModel):
    """批量注册响应"""
    batch_id: str
    count: int
    tasks: List[RegistrationTaskResponse]


class TaskListResponse(BaseModel):
    """任务列表响应"""
    total: int
    tasks: List[RegistrationTaskResponse]


class EmailRegistrationStatItemResponse(BaseModel):
    email: str
    email_service: Optional[str] = None
    total_attempts: int
    success_count: int
    failure_count: int
    add_phone_count: int
    last_status: Optional[str] = None
    last_error: Optional[str] = None
    last_used_at: Optional[str] = None
    last_add_phone_at: Optional[str] = None


class EmailRegistrationStatsResponse(BaseModel):
    items: List[EmailRegistrationStatItemResponse]
    total: int


# ============== Outlook 批量注册模型 ==============

class EmailDomainRegistrationStatItemResponse(BaseModel):
    email_domain: str
    total_attempts: int
    success_count: int
    failure_count: int
    add_phone_count: int
    last_used_at: Optional[str] = None


class EmailDomainRegistrationSummaryResponse(BaseModel):
    total_domains: int
    total_attempts: int
    success_count: int
    failure_count: int
    add_phone_count: int
    success_rate: float
    top_domain: Optional[str] = None
    top_domain_attempts: int


class EmailDomainRegistrationStatsResponse(BaseModel):
    items: List[EmailDomainRegistrationStatItemResponse]
    total: int
    summary: EmailDomainRegistrationSummaryResponse


class OutlookAccountForRegistration(BaseModel):
    """可用于注册的 Outlook 账户"""
    id: int                      # EmailService 表的 ID
    email: str
    name: str
    has_oauth: bool              # 是否有 OAuth 配置
    is_registered: bool          # 是否已注册
    registered_account_id: Optional[int] = None


class OutlookAccountsListResponse(BaseModel):
    """Outlook 账户列表响应"""
    total: int
    registered_count: int        # 已注册数量
    unregistered_count: int      # 未注册数量
    accounts: List[OutlookAccountForRegistration]


class OutlookBatchRegistrationRequest(BaseModel):
    """Outlook 批量注册请求"""
    service_ids: List[int]
    skip_registered: bool = True
    proxy: Optional[str] = None
    interval_min: int = 5
    interval_max: int = 30
    concurrency: int = 1
    mode: str = "pipeline"
    auto_upload_cpa: bool = False
    cpa_service_ids: List[int] = []
    auto_upload_sub2api: bool = False
    sub2api_service_ids: List[int] = []
    auto_upload_tm: bool = False
    tm_service_ids: List[int] = []
    auto_upload_new_api: bool = False
    new_api_service_ids: List[int] = []
    registration_type: str = RoleTag.CHILD.value


class OutlookBatchRegistrationResponse(BaseModel):
    """Outlook 批量注册响应"""
    batch_id: str
    total: int                   # 总数
    skipped: int                 # 跳过数（已注册）
    to_register: int             # 待注册数
    service_ids: List[int]       # 实际要注册的服务 ID


class ScheduledRegistrationRequest(BaseModel):
    """创建或更新计划注册任务请求。"""
    name: str = Field(..., min_length=1, max_length=100)
    enabled: bool = True
    schedule_type: str
    schedule_config: Dict[str, Any]
    registration_config: Dict[str, Any]
    timezone: str = "local"


class ScheduledRegistrationJobResponse(BaseModel):
    """计划注册任务响应。"""
    id: int
    job_uuid: str
    name: str
    enabled: bool
    status: str
    schedule_type: str
    schedule_config: Dict[str, Any]
    schedule_description: str
    registration_config: Dict[str, Any]
    timezone: Optional[str] = None
    next_run_at: Optional[str] = None
    last_run_at: Optional[str] = None
    last_success_at: Optional[str] = None
    last_error: Optional[str] = None
    run_count: int
    consecutive_failures: int
    is_running: bool
    last_triggered_task_uuid: Optional[str] = None
    last_triggered_batch_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class ScheduledRegistrationJobListResponse(BaseModel):
    """计划注册任务列表响应。"""
    total: int
    jobs: List[ScheduledRegistrationJobResponse]


# ============== Helper Functions ==============

def task_to_response(task: RegistrationTask) -> RegistrationTaskResponse:
    """转换任务模型为响应"""
    runtime_status = task_manager.get_status(task.task_uuid) or {}
    error_message = (
        runtime_status.get("last_error")
        or runtime_status.get("error")
        or task.error_message
    )
    return RegistrationTaskResponse(
        id=task.id,
        task_uuid=task.task_uuid,
        status=runtime_status.get("status", task.status),
        email_service_id=task.email_service_id,
        proxy=task.proxy,
        logs=task.logs,
        result=task.result,
        error_message=error_message,
        attempt=runtime_status.get("attempt"),
        max_attempts=runtime_status.get("max_attempts"),
        retrying=runtime_status.get("retrying"),
        last_error=runtime_status.get("last_error") or runtime_status.get("error"),
        next_retry_in_seconds=runtime_status.get("next_retry_in_seconds"),
        email=runtime_status.get("email"),
        email_service=runtime_status.get("email_service"),
        created_at=task.created_at.isoformat() if task.created_at else None,
        started_at=task.started_at.isoformat() if task.started_at else None,
        completed_at=task.completed_at.isoformat() if task.completed_at else None,
    )


def scheduled_job_to_response(job: ScheduledRegistrationJob) -> ScheduledRegistrationJobResponse:
    """转换计划任务模型为响应。"""
    schedule_config = job.schedule_config or {}
    return ScheduledRegistrationJobResponse(
        id=job.id,
        job_uuid=job.job_uuid,
        name=job.name,
        enabled=job.enabled,
        status=job.status,
        schedule_type=job.schedule_type,
        schedule_config=schedule_config,
        schedule_description=describe_schedule(job.schedule_type, schedule_config),
        registration_config=job.registration_config or {},
        timezone=job.timezone,
        next_run_at=job.next_run_at.isoformat() if job.next_run_at else None,
        last_run_at=job.last_run_at.isoformat() if job.last_run_at else None,
        last_success_at=job.last_success_at.isoformat() if job.last_success_at else None,
        last_error=job.last_error,
        run_count=job.run_count or 0,
        consecutive_failures=job.consecutive_failures or 0,
        is_running=bool(job.is_running),
        last_triggered_task_uuid=job.last_triggered_task_uuid,
        last_triggered_batch_id=job.last_triggered_batch_id,
        created_at=job.created_at.isoformat() if job.created_at else None,
        updated_at=job.updated_at.isoformat() if job.updated_at else None,
    )


def _normalize_email_service_config(
    service_type: EmailServiceType,
    config: Optional[dict],
    proxy_url: Optional[str] = None
) -> dict:
    """按服务类型兼容旧字段名，避免不同服务的配置键互相污染。"""
    return normalize_email_service_config(service_type, config, proxy_url)


def _get_max_registration_attempts() -> int:
    """获取单个注册任务的最大尝试次数（首次执行 + 额外重试次数）。"""
    retries = max(0, get_settings().registration_max_retries)
    return retries + 1


def _get_retry_wait_seconds(attempt: int) -> int:
    """获取下一次重试前的等待秒数。"""
    index = min(max(attempt - 1, 0), len(RETRY_BACKOFF_SECONDS) - 1)
    return RETRY_BACKOFF_SECONDS[index]


def _is_add_phone_required_message(message: Optional[str]) -> bool:
    return "add_phone" in str(message or "").strip().lower()


def _is_add_phone_required_result(result: Optional[RegistrationResult]) -> bool:
    if not result:
        return False
    error_code = str(getattr(result, "error_code", "") or "").strip().lower()
    return error_code == "add_phone_required" or _is_add_phone_required_message(
        getattr(result, "error_message", None)
    )


def _record_email_attempt_if_needed(
    db,
    result: Optional[RegistrationResult],
    email_service: Optional[str],
) -> None:
    email = str(getattr(result, "email", "") or "").strip()
    if not email:
        return

    success = bool(getattr(result, "success", False))
    if success:
        status = "success"
    elif _is_add_phone_required_result(result):
        status = "add_phone"
    else:
        status = "failed"

    crud.record_email_registration_attempt(
        db,
        email_address=email,
        email_service=email_service,
        status=status,
        error_message=getattr(result, "error_message", None),
    )


def _email_stat_to_response_item(stat) -> EmailRegistrationStatItemResponse:
    payload = stat.to_dict()
    return EmailRegistrationStatItemResponse(
        email=payload.get("email") or payload.get("email_address") or "",
        email_service=payload.get("email_service"),
        total_attempts=int(payload.get("total_attempts") or 0),
        success_count=int(payload.get("success_count") or 0),
        failure_count=int(payload.get("failure_count") or 0),
        add_phone_count=int(payload.get("add_phone_count") or 0),
        last_status=payload.get("last_status"),
        last_error=payload.get("last_error"),
        last_used_at=payload.get("last_used_at"),
        last_add_phone_at=payload.get("last_add_phone_at"),
    )


def _get_stat_value(stat: Any, key: str) -> Any:
    mapping = getattr(stat, "_mapping", None)
    if mapping is not None and key in mapping:
        return mapping.get(key)
    if isinstance(stat, dict):
        return stat.get(key)
    return getattr(stat, key, None)


def _serialize_optional_datetime(value: Any) -> Optional[str]:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _email_domain_stat_to_response_item(stat: Any) -> EmailDomainRegistrationStatItemResponse:
    return EmailDomainRegistrationStatItemResponse(
        email_domain=str(_get_stat_value(stat, "email_domain") or "").strip(),
        total_attempts=int(_get_stat_value(stat, "total_attempts") or 0),
        success_count=int(_get_stat_value(stat, "success_count") or 0),
        failure_count=int(_get_stat_value(stat, "failure_count") or 0),
        add_phone_count=int(_get_stat_value(stat, "add_phone_count") or 0),
        last_used_at=_serialize_optional_datetime(_get_stat_value(stat, "last_used_at")),
    )


def _email_domain_summary_to_response_item(summary: Dict[str, Any]) -> EmailDomainRegistrationSummaryResponse:
    payload = dict(summary or {})
    return EmailDomainRegistrationSummaryResponse(
        total_domains=int(payload.get("total_domains") or 0),
        total_attempts=int(payload.get("total_attempts") or 0),
        success_count=int(payload.get("success_count") or 0),
        failure_count=int(payload.get("failure_count") or 0),
        add_phone_count=int(payload.get("add_phone_count") or 0),
        success_rate=float(payload.get("success_rate") or 0),
        top_domain=str(payload.get("top_domain") or "").strip() or None,
        top_domain_attempts=int(payload.get("top_domain_attempts") or 0),
    )


def _combine_task_logs(task_uuid: str, persisted_logs: Optional[str]) -> List[str]:
    """合并数据库与内存中的任务日志，避免轮询时漏掉实时日志。"""
    merged_logs: List[str] = []
    seen_logs = set()

    sources = []
    if persisted_logs:
        sources.append(persisted_logs.split("\n"))
    sources.append(task_manager.get_logs(task_uuid))

    for log_list in sources:
        for log in log_list:
            if not log or log in seen_logs:
                continue
            seen_logs.add(log)
            merged_logs.append(log)

    return merged_logs


def _mark_task_cancelled(
    db,
    task_uuid: str,
    attempt: int,
    max_attempts: int,
    error_message: Optional[str] = None,
    email: Optional[str] = None,
    email_service: Optional[str] = None,
):
    """将任务标记为已取消，并同步数据库与内存状态。"""
    update_kwargs: Dict[str, Any] = {
        "status": "cancelled",
        "completed_at": datetime.utcnow(),
    }
    if error_message:
        update_kwargs["error_message"] = error_message

    crud.update_registration_task(db, task_uuid, **update_kwargs)
    task_manager.update_status(
        task_uuid,
        "cancelled",
        attempt=attempt,
        max_attempts=max_attempts,
        retrying=False,
        last_error=error_message,
        error=error_message,
        next_retry_in_seconds=None,
        email=email,
        email_service=email_service,
    )


def _mark_task_failed(
    db,
    task_uuid: str,
    *,
    attempt: Optional[int],
    max_attempts: int,
    error_message: Optional[str],
    result: Optional[Dict[str, Any]] = None,
    email: Optional[str] = None,
    email_service: Optional[str] = None,
) -> None:
    crud.update_registration_task(
        db,
        task_uuid,
        status="failed",
        completed_at=utcnow_naive(),
        result=result,
        error_message=error_message,
    )
    task_manager.update_status(
        task_uuid,
        "failed",
        attempt=attempt,
        max_attempts=max_attempts,
        retrying=False,
        last_error=error_message,
        error=error_message,
        next_retry_in_seconds=None,
        email=email,
        email_service=email_service,
    )


def _wait_for_retry_or_cancel(task_uuid: str, wait_seconds: int) -> bool:
    """等待下次重试，期间允许用户取消任务。"""
    deadline = time.monotonic() + max(0, wait_seconds)

    while time.monotonic() < deadline:
        if task_manager.is_cancelled(task_uuid):
            return False
        remaining = deadline - time.monotonic()
        time.sleep(min(0.5, max(remaining, 0)))

    return not task_manager.is_cancelled(task_uuid)


def _coerce_optional_int(value: Any) -> Optional[int]:
    """尽力把值转成整数，失败时返回 None。"""
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _update_task_result_context(
    base_result: Optional[Dict[str, Any]],
    *,
    saved_account_email: Any = _TASK_RESULT_UNSET,
    saved_account_id: Any = _TASK_RESULT_UNSET,
    upload_summary: Any = _TASK_RESULT_UNSET,
) -> Dict[str, Any]:
    """在注册结果 JSON 上叠加可复用账号与上传重试上下文。"""
    payload = dict(base_result or {})

    if saved_account_email is not _TASK_RESULT_UNSET:
        if saved_account_email:
            payload["saved_account_email"] = str(saved_account_email).strip()
        else:
            payload.pop("saved_account_email", None)

    if saved_account_id is not _TASK_RESULT_UNSET:
        normalized_id = _coerce_optional_int(saved_account_id)
        if normalized_id is not None:
            payload["saved_account_id"] = normalized_id
        else:
            payload.pop("saved_account_id", None)

    if upload_summary is not _TASK_RESULT_UNSET:
        if upload_summary is None:
            payload.pop("upload_summary", None)
        else:
            payload["upload_summary"] = upload_summary

    if payload.get("saved_account_email") or payload.get("saved_account_id") is not None:
        payload["upload_retry_mode"] = UPLOAD_RETRY_MODE_REUSE_SAVED_ACCOUNT
    else:
        payload.pop("upload_retry_mode", None)

    return payload


def _resolve_saved_retry_account(
    db,
    *,
    saved_account_id: Optional[int] = None,
    saved_account_email: Optional[str] = None,
) -> Optional[Account]:
    """根据任务结果里的上下文找回已注册账号。"""
    account = None
    if saved_account_id is not None:
        account = crud.get_account_by_id(db, saved_account_id)
    if not account and saved_account_email:
        account = crud.get_account_by_email(db, saved_account_email)
    return account


def _summarize_upload_errors(details: List[Dict[str, Any]]) -> str:
    """从上传详情中提炼一条简洁错误摘要。"""
    messages: List[str] = []
    for detail in details or []:
        if detail.get("success"):
            continue
        message = str(detail.get("error") or detail.get("message") or "").strip()
        if message and message not in messages:
            messages.append(message)
        if len(messages) >= 3:
            break
    return "；".join(messages)


def _collect_upload_service_summary(service, raw_result: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """将单个平台单个服务的上传结果压平成统一结构。"""
    result = dict(raw_result or {})
    details = list(result.get("details") or [])
    summary = {
        "service_id": getattr(service, "id", None),
        "service_name": getattr(service, "name", ""),
        "success_count": int(result.get("success_count") or 0),
        "failed_count": int(result.get("failed_count") or 0),
        "skipped_count": int(result.get("skipped_count") or 0),
        "details": details,
    }
    error_summary = _summarize_upload_errors(details)
    if error_summary:
        summary["error_summary"] = error_summary
    return summary


def _build_noop_upload_summary() -> Dict[str, Any]:
    return {
        "success": True,
        "error_summary": "",
        "failed_platforms": [],
        "platforms": {},
    }


def _normalize_upload_summary(summary: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """兼容测试替身，确保上传结果结构稳定。"""
    if not isinstance(summary, dict):
        return _build_noop_upload_summary()
    return {
        "success": bool(summary.get("success", True)),
        "error_summary": str(summary.get("error_summary") or "").strip(),
        "failed_platforms": list(summary.get("failed_platforms") or []),
        "platforms": dict(summary.get("platforms") or {}),
    }


def _upload_summary_has_failures(summary: Optional[Dict[str, Any]]) -> bool:
    normalized = _normalize_upload_summary(summary)
    return bool(normalized.get("failed_platforms")) or not bool(normalized.get("success", True))


def _build_upload_retry_error(summary: Optional[Dict[str, Any]], *, exhausted: bool) -> str:
    normalized = _normalize_upload_summary(summary)
    detail = normalized.get("error_summary") or "自动上传失败"
    prefix = "账号已注册，自动上传失败且重试已耗尽" if exhausted else "账号已注册，自动上传失败"
    return f"{prefix}: {detail}"


def _perform_registration_uploads(
    db,
    account_email: str,
    account_id: Optional[int],
    log_callback,
    auto_upload_cpa: bool = False,
    cpa_service_ids: Optional[List[int]] = None,
    auto_upload_sub2api: bool = False,
    sub2api_service_ids: Optional[List[int]] = None,
    auto_upload_tm: bool = False,
    tm_service_ids: Optional[List[int]] = None,
    auto_upload_new_api: bool = False,
    new_api_service_ids: Optional[List[int]] = None,
):
    """执行注册后的自动上传流程，返回结构化结果供重试逻辑判断。"""
    summary = _build_noop_upload_summary()
    enabled_platforms = {
        "cpa": bool(auto_upload_cpa),
        "sub2api": bool(auto_upload_sub2api),
        "tm": bool(auto_upload_tm),
        "new_api": bool(auto_upload_new_api),
    }
    if not any(enabled_platforms.values()):
        return summary

    saved_account = _resolve_saved_retry_account(
        db,
        saved_account_id=_coerce_optional_int(account_id),
        saved_account_email=str(account_email or "").strip() or None,
    )
    if not saved_account:
        message = "账号已注册，但数据库中未找到保存记录"
        log_callback(f"[系统] {message}")
        summary["success"] = False
        summary["error_summary"] = message
        summary["failed_platforms"].append("account_lookup")
        return summary

    account_ids = [saved_account.id]
    platform_failures: List[str] = []
    error_messages: List[str] = []

    if auto_upload_cpa:
        from ...core.upload.cpa_upload import batch_upload_to_cpa

        platform_summary = {
            "enabled": True,
            "success_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "services": [],
        }
        service_ids = cpa_service_ids or [s.id for s in crud.get_cpa_services(db, enabled=True)]
        if not service_ids:
            log_callback("[CPA] 无可用 CPA 服务，跳过上传")
            platform_summary["reason"] = "no_services"

        for service_id in service_ids:
            service = crud.get_cpa_service_by_id(db, service_id)
            if not service:
                continue
            log_callback(f"[CPA] 正在把账号打包发往服务站: {service.name}")
            raw_result = batch_upload_to_cpa(
                account_ids,
                api_url=service.api_url,
                api_token=service.api_token,
                service_id=service.id,
                dedupe=True,
            )
            service_summary = _collect_upload_service_summary(service, raw_result)
            platform_summary["services"].append(service_summary)
            platform_summary["success_count"] += service_summary["success_count"]
            platform_summary["failed_count"] += service_summary["failed_count"]
            platform_summary["skipped_count"] += service_summary["skipped_count"]
            log_callback(
                f"[CPA] {service.name}: 成功 {service_summary['success_count']} / 失败 {service_summary['failed_count']} / 跳过 {service_summary['skipped_count']}"
            )
            if service_summary["failed_count"] > 0:
                failure_message = service_summary.get("error_summary") or "上传失败"
                error_messages.append(f"CPA({service.name}): {failure_message}")
        summary["platforms"]["cpa"] = platform_summary
        if platform_summary["failed_count"] > 0:
            platform_failures.append("cpa")

    if auto_upload_sub2api:
        from ...core.upload.sub2api_upload import batch_upload_to_sub2api

        platform_summary = {
            "enabled": True,
            "success_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "services": [],
        }
        service_ids = sub2api_service_ids or [s.id for s in crud.get_sub2api_services(db, enabled=True)]
        if not service_ids:
            log_callback("[Sub2API] 无可用 Sub2API 服务，跳过上传")
            platform_summary["reason"] = "no_services"

        for service_id in service_ids:
            service = crud.get_sub2api_service_by_id(db, service_id)
            if not service:
                continue
            log_callback(f"[Sub2API] 正在把账号发往服务站: {service.name}")
            raw_result = batch_upload_to_sub2api(
                account_ids,
                api_url=service.api_url,
                api_key=service.api_key,
                service_id=service.id,
                dedupe=True,
            )
            service_summary = _collect_upload_service_summary(service, raw_result)
            platform_summary["services"].append(service_summary)
            platform_summary["success_count"] += service_summary["success_count"]
            platform_summary["failed_count"] += service_summary["failed_count"]
            platform_summary["skipped_count"] += service_summary["skipped_count"]
            log_callback(
                f"[Sub2API] {service.name}: 成功 {service_summary['success_count']} / 失败 {service_summary['failed_count']} / 跳过 {service_summary['skipped_count']}"
            )
            if service_summary["failed_count"] > 0:
                failure_message = service_summary.get("error_summary") or "上传失败"
                error_messages.append(f"Sub2API({service.name}): {failure_message}")
        summary["platforms"]["sub2api"] = platform_summary
        if platform_summary["failed_count"] > 0:
            platform_failures.append("sub2api")

    if auto_upload_tm:
        from ...core.upload.team_manager_upload import batch_upload_to_team_manager

        platform_summary = {
            "enabled": True,
            "success_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "services": [],
        }
        service_ids = tm_service_ids or [s.id for s in crud.get_tm_services(db, enabled=True)]
        if not service_ids:
            log_callback("[TM] 无可用 Team Manager 服务，跳过上传")
            platform_summary["reason"] = "no_services"

        for service_id in service_ids:
            service = crud.get_tm_service_by_id(db, service_id)
            if not service:
                continue
            log_callback(f"[TM] 正在把账号发往服务站: {service.name}")
            raw_result = batch_upload_to_team_manager(
                account_ids,
                api_url=service.api_url,
                api_key=service.api_key,
                service_id=service.id,
                dedupe=True,
            )
            service_summary = _collect_upload_service_summary(service, raw_result)
            platform_summary["services"].append(service_summary)
            platform_summary["success_count"] += service_summary["success_count"]
            platform_summary["failed_count"] += service_summary["failed_count"]
            platform_summary["skipped_count"] += service_summary["skipped_count"]
            log_callback(
                f"[TM] {service.name}: 成功 {service_summary['success_count']} / 失败 {service_summary['failed_count']} / 跳过 {service_summary['skipped_count']}"
            )
            if service_summary["failed_count"] > 0:
                failure_message = service_summary.get("error_summary") or "上传失败"
                error_messages.append(f"TM({service.name}): {failure_message}")
        summary["platforms"]["tm"] = platform_summary
        if platform_summary["failed_count"] > 0:
            platform_failures.append("tm")

    if auto_upload_new_api:
        from ...core.upload.new_api_upload import batch_upload_to_new_api

        platform_summary = {
            "enabled": True,
            "success_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "services": [],
        }
        service_ids = new_api_service_ids or [s.id for s in crud.get_new_api_services(db, enabled=True)]
        if not service_ids:
            log_callback("[NewAPI] 无可用 new-api 服务，跳过上传")
            platform_summary["reason"] = "no_services"

        for service_id in service_ids:
            service = crud.get_new_api_service_by_id(db, service_id)
            if not service:
                continue
            log_callback(f"[NewAPI] 正在把账号发往服务站: {service.name}")
            raw_result = batch_upload_to_new_api(
                account_ids,
                api_url=service.api_url,
                username=getattr(service, "username", None),
                password=getattr(service, "password", None),
                service_id=service.id,
                dedupe=True,
            )
            service_summary = _collect_upload_service_summary(service, raw_result)
            platform_summary["services"].append(service_summary)
            platform_summary["success_count"] += service_summary["success_count"]
            platform_summary["failed_count"] += service_summary["failed_count"]
            platform_summary["skipped_count"] += service_summary["skipped_count"]
            log_callback(
                f"[NewAPI] {service.name}: 成功 {service_summary['success_count']} / 失败 {service_summary['failed_count']} / 跳过 {service_summary['skipped_count']}"
            )
            if service_summary["failed_count"] > 0:
                failure_message = service_summary.get("error_summary") or "上传失败"
                error_messages.append(f"NewAPI({service.name}): {failure_message}")
        summary["platforms"]["new_api"] = platform_summary
        if platform_summary["failed_count"] > 0:
            platform_failures.append("new_api")

    summary["failed_platforms"] = platform_failures
    summary["success"] = not platform_failures
    summary["error_summary"] = "；".join(error_messages[:3])
    return summary


def _execute_single_registration_attempt(
    db,
    task_uuid: str,
    email_service_type: str,
    email_service_id: Optional[int],
    email_service_config: Optional[dict],
    actual_proxy_url: Optional[str],
    proxy_id: Optional[int],
    selection_index: int,
    random_email_service: bool,
    random_outlook_account: bool,
    random_domain: bool,
    subdomain_only: bool,
    selected_email_addresses: List[str],
    selected_domains: List[str],
    skipped_email_addresses: List[str],
    log_callback,
    registration_mode: str = "protocol",
    protocol_variant: str = "legacy",
    registration_type: str = RoleTag.CHILD.value,
) -> Tuple[RegistrationResult, str]:
    """执行一次完整的注册尝试，成功后保证账号已落库。"""
    selection = RegistrationSelectionRequest(
        random_email_service=random_email_service,
        random_outlook_account=random_outlook_account,
        random_domain=random_domain,
        subdomain_only=subdomain_only,
        selected_email_addresses=selected_email_addresses or [],
        selected_domains=selected_domains or [],
        skipped_email_addresses=skipped_email_addresses or [],
        selection_index=selection_index,
    )
    resolved_service = resolve_email_service_for_registration(
        db=db,
        service_type=EmailServiceType(email_service_type),
        requested_service_id=email_service_id,
        fallback_config=email_service_config,
        proxy_url=actual_proxy_url,
        selection=selection,
        log_callback=log_callback,
    )

    if resolved_service.email_service_id:
        crud.update_registration_task(
            db,
            task_uuid,
            email_service_id=resolved_service.email_service_id,
        )

    task_manager.update_status(
        task_uuid,
        "running",
        email_service=resolved_service.service_type.value,
    )

    email_service = EmailServiceFactory.create(
        resolved_service.service_type,
        resolved_service.config,
        name=resolved_service.service_name,
    )

    flow_key = _get_registration_flow_key(registration_mode, protocol_variant)
    if flow_key == "browser":
        from ...core.browser_register import BrowserRegistrationEngine
        engine = BrowserRegistrationEngine(
            email_service=email_service,
            proxy_url=actual_proxy_url,
            callback_logger=log_callback,
            task_uuid=task_uuid,
        )
    elif flow_key == "protocol.enhanced":
        engine = EnhancedProtocolRegistrationEngine(
            email_service=email_service,
            proxy_url=actual_proxy_url,
            callback_logger=log_callback,
            task_uuid=task_uuid,
            check_cancelled=task_manager.create_check_cancelled_callback(task_uuid),
        )
    elif flow_key == "protocol.adaptive":
        engine = AdaptiveProtocolRegistrationEngine(
            email_service=email_service,
            proxy_url=actual_proxy_url,
            callback_logger=log_callback,
            task_uuid=task_uuid,
            check_cancelled=task_manager.create_check_cancelled_callback(task_uuid),
        )
    else:
        engine = RegistrationEngine(
            email_service=email_service,
            proxy_url=actual_proxy_url,
            callback_logger=log_callback,
            task_uuid=task_uuid,
            check_cancelled=task_manager.create_check_cancelled_callback(task_uuid),
        )

    # 注册引擎实例，使取消能立即关闭浏览器
    task_manager.register_engine(task_uuid, engine)
    try:
        result = engine.run()
    finally:
        task_manager.unregister_engine(task_uuid)

    if not result.success:
        raise RegistrationAttemptError(result, resolved_service.service_type.value)

    update_proxy_usage(db, proxy_id)
    if not engine.save_to_database(result):
        raise RuntimeError("注册成功但保存到数据库失败")

    role_tag = normalize_role_tag(registration_type)
    account_label = role_tag_to_account_label(role_tag)
    saved_account = crud.get_account_by_email(db, result.email)
    if saved_account:
        extra_data = dict(saved_account.extra_data or {})
        extra_data["account_label"] = account_label
        extra_data["role_tag"] = role_tag
        extra_data["registration_type"] = role_tag
        crud.update_account(
            db,
            saved_account.id,
            role_tag=role_tag,
            extra_data=extra_data,
        )

    return result, resolved_service.service_type.value


def _run_sync_registration_task(
    task_uuid: str,
    email_service_type: str,
    proxy: Optional[str],
    email_service_config: Optional[dict],
    email_service_id: Optional[int] = None,
    random_email_service: bool = False,
    random_outlook_account: bool = False,
    random_domain: bool = False,
    subdomain_only: bool = False,
    selected_email_addresses: List[str] = None,
    selected_domains: List[str] = None,
    selection_index: int = 0,
    log_prefix: str = "",
    batch_id: str = "",
    registration_mode: str = "protocol",
    protocol_variant: str = "legacy",
    auto_upload_cpa: bool = False,
    cpa_service_ids: List[int] = None,
    auto_upload_sub2api: bool = False,
    sub2api_service_ids: List[int] = None,
    auto_upload_tm: bool = False,
    tm_service_ids: List[int] = None,
    auto_upload_new_api: bool = False,
    new_api_service_ids: List[int] = None,
    registration_type: str = RoleTag.CHILD.value,
):
    """
    在线程池中执行的同步注册任务

    这个函数会被 run_in_executor 调用，运行在独立线程中
    """
    with get_db() as db:
        max_attempts = _get_max_registration_attempts()
        last_error: Optional[str] = None
        last_email: Optional[str] = None
        last_email_service: Optional[str] = None
        task_result_payload: Dict[str, Any] = {}
        saved_account_email: Optional[str] = None
        saved_account_id: Optional[int] = None
        latest_upload_summary: Optional[Dict[str, Any]] = None
        skipped_email_addresses = set()

        try:
            task = crud.update_registration_task(
                db, task_uuid,
                status="running",
                started_at=utcnow_naive(),
                completed_at=None,
                error_message=None,
            )
            if not task:
                logger.error(f"任务不存在: {task_uuid}")
                return

            task_result_payload = dict(task.result or {})
            saved_account_email = str(task_result_payload.get("saved_account_email") or "").strip() or None
            saved_account_id = _coerce_optional_int(task_result_payload.get("saved_account_id"))
            if "upload_summary" in task_result_payload:
                latest_upload_summary = _normalize_upload_summary(task_result_payload.get("upload_summary"))

            if task_manager.is_cancelled(task_uuid):
                _mark_task_cancelled(db, task_uuid, attempt=0, max_attempts=max_attempts)
                logger.info(f"任务 {task_uuid} 已取消，跳过执行")
                return

            from ...core.dynamic_proxy import normalize_proxy_input
            actual_proxy_url = normalize_proxy_input(proxy) if proxy else None
            proxy_id = None
            if not actual_proxy_url:
                actual_proxy_url, proxy_id = get_proxy_for_registration(db)
                if actual_proxy_url:
                    logger.info(f"任务 {task_uuid} 使用代理: {actual_proxy_url[:50]}...")

            crud.update_registration_task(db, task_uuid, proxy=actual_proxy_url)
            log_callback = task_manager.create_log_callback(task_uuid, prefix=log_prefix, batch_id=batch_id)
            flow_key = _get_registration_flow_key(registration_mode, protocol_variant)
            log_callback(f"[系统] 当前注册流程: flow={flow_key}")
            logger.info("注册任务启动: task=%s flow=%s", task_uuid, flow_key)
            task_manager.update_status(
                task_uuid,
                "running",
                attempt=0,
                max_attempts=max_attempts,
                retrying=False,
                last_error=None,
                next_retry_in_seconds=None,
                email_service=email_service_type or None,
            )

            for attempt in range(1, max_attempts + 1):
                if task_manager.is_cancelled(task_uuid):
                    _mark_task_cancelled(
                        db,
                        task_uuid,
                        attempt=attempt - 1,
                        max_attempts=max_attempts,
                        error_message=last_error,
                        email=last_email,
                        email_service=last_email_service,
                    )
                    logger.info(f"任务 {task_uuid} 在第 {attempt} 次尝试前已取消")
                    return

                task_manager.update_status(
                    task_uuid,
                    "running",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    retrying=False,
                    last_error=last_error,
                    next_retry_in_seconds=None,
                    email=last_email,
                    email_service=last_email_service,
                )
                log_callback(f"[系统] 开始第 {attempt}/{max_attempts} 次注册尝试")

                try:
                    saved_account = _resolve_saved_retry_account(
                        db,
                        saved_account_id=saved_account_id,
                        saved_account_email=saved_account_email,
                    )
                    if (saved_account_email or saved_account_id is not None) and not saved_account:
                        log_callback("[系统] 已保存的注册账号不存在，回退到重新注册流程")
                        saved_account_email = None
                        saved_account_id = None
                        latest_upload_summary = None
                        task_result_payload = _update_task_result_context(
                            task_result_payload,
                            saved_account_email=None,
                            saved_account_id=None,
                            upload_summary=None,
                        )
                        crud.update_registration_task(
                            db,
                            task_uuid,
                            result=task_result_payload or None,
                            error_message=None,
                        )

                    account_email: Optional[str] = None
                    account_id: Optional[int] = None

                    if saved_account:
                        saved_account_email = saved_account.email
                        saved_account_id = saved_account.id
                        account_email = saved_account.email
                        account_id = saved_account.id
                        last_email = saved_account.email
                        last_email_service = saved_account.email_service
                        log_callback(f"[系统] 复用已注册账号继续自动上传: {account_email}")
                    else:
                        latest_upload_summary = None
                        try:
                            result, email_service_value = _execute_single_registration_attempt(
                                db=db,
                                task_uuid=task_uuid,
                                email_service_type=email_service_type,
                                email_service_id=email_service_id,
                                email_service_config=email_service_config,
                                actual_proxy_url=actual_proxy_url,
                                proxy_id=proxy_id,
                                selection_index=selection_index,
                                random_email_service=random_email_service,
                                random_outlook_account=random_outlook_account,
                                random_domain=random_domain,
                                subdomain_only=subdomain_only,
                                selected_email_addresses=selected_email_addresses or [],
                                selected_domains=selected_domains or [],
                                skipped_email_addresses=sorted(skipped_email_addresses),
                                log_callback=log_callback,
                                registration_mode=registration_mode,
                                protocol_variant=protocol_variant,
                                registration_type=registration_type,
                            )
                        except RegistrationSelectionExhaustedError as exc:
                            last_error = str(exc) or "当前可选邮箱地址已耗尽"
                            log_callback(f"[错误] {last_error}")
                            _mark_task_failed(
                                db,
                                task_uuid,
                                attempt=attempt,
                                max_attempts=max_attempts,
                                error_message=last_error,
                                result=task_result_payload or None,
                                email=last_email,
                                email_service=last_email_service,
                            )
                            logger.warning(f"注册任务失败: {task_uuid}, 原因: {last_error}")
                            return
                        except ValueError as exc:
                            last_error = str(exc)
                            log_callback(f"[错误] {last_error}")
                            _mark_task_failed(
                                db,
                                task_uuid,
                                attempt=attempt,
                                max_attempts=max_attempts,
                                error_message=last_error,
                                result=task_result_payload or None,
                                email=last_email,
                                email_service=last_email_service,
                            )
                            logger.warning(f"注册任务失败: {task_uuid}, 原因: {last_error}")
                            return
                        except RegistrationAttemptError as exc:
                            result = exc.result
                            email_service_value = exc.email_service
                            last_email = result.email or last_email
                            last_email_service = email_service_value or last_email_service
                            saved_account_email = None
                            saved_account_id = None
                            latest_upload_summary = None
                            task_result_payload = _update_task_result_context(
                                result.to_dict(),
                                saved_account_email=None,
                                saved_account_id=None,
                                upload_summary=None,
                            )
                            crud.update_registration_task(
                                db,
                                task_uuid,
                                result=task_result_payload or None,
                                error_message=result.error_message or str(exc),
                            )
                            _record_email_attempt_if_needed(db, result, email_service_value)
                            last_error = result.error_message or str(exc)

                            if _is_add_phone_required_result(result):
                                normalized_email = str(result.email or "").strip().lower()
                                if normalized_email:
                                    skipped_email_addresses.add(normalized_email)
                                log_callback("[系统] 命中 add_phone，已加入当前任务跳过集合并立即尝试下一地址")
                                task_manager.update_status(
                                    task_uuid,
                                    "running",
                                    attempt=attempt,
                                    max_attempts=max_attempts,
                                    retrying=False,
                                    last_error=last_error,
                                    next_retry_in_seconds=None,
                                    email=last_email,
                                    email_service=last_email_service,
                                )
                                if attempt < max_attempts:
                                    continue

                            raise RuntimeError(last_error)

                        last_email = result.email
                        last_email_service = email_service_value
                        account_email = result.email
                        _record_email_attempt_if_needed(db, result, email_service_value)
                        saved_account = _resolve_saved_retry_account(
                            db,
                            saved_account_email=result.email,
                        )
                        saved_account_email = result.email
                        saved_account_id = getattr(saved_account, "id", None)
                        account_id = saved_account_id
                        task_result_payload = _update_task_result_context(
                            result.to_dict(),
                            saved_account_email=saved_account_email,
                            saved_account_id=saved_account_id,
                            upload_summary=None,
                        )
                        crud.update_registration_task(
                            db,
                            task_uuid,
                            result=task_result_payload or None,
                            error_message=None,
                        )

                    task_manager.update_status(
                        task_uuid,
                        "running",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        retrying=False,
                        last_error=None,
                        next_retry_in_seconds=None,
                        email=last_email,
                        email_service=last_email_service,
                    )

                    try:
                        latest_upload_summary = _normalize_upload_summary(
                            _perform_registration_uploads(
                                db,
                                account_email=account_email or "",
                                account_id=account_id,
                                log_callback=log_callback,
                                auto_upload_cpa=auto_upload_cpa,
                                cpa_service_ids=cpa_service_ids or [],
                                auto_upload_sub2api=auto_upload_sub2api,
                                sub2api_service_ids=sub2api_service_ids or [],
                                auto_upload_tm=auto_upload_tm,
                                tm_service_ids=tm_service_ids or [],
                                auto_upload_new_api=auto_upload_new_api,
                                new_api_service_ids=new_api_service_ids or [],
                            )
                        )
                    except Exception as upload_exc:
                        logger.warning(f"注册任务自动上传异常: {task_uuid}, 错误: {upload_exc}")
                        log_callback(f"[系统] 自动上传异常，将按重试策略继续: {upload_exc}")
                        latest_upload_summary = _normalize_upload_summary(
                            {
                                "success": False,
                                "error_summary": str(upload_exc) or "自动上传异常",
                                "failed_platforms": ["upload_exception"],
                                "platforms": {},
                            }
                        )

                    task_result_payload = _update_task_result_context(
                        task_result_payload,
                        saved_account_email=saved_account_email or account_email,
                        saved_account_id=saved_account_id if saved_account_id is not None else account_id,
                        upload_summary=latest_upload_summary,
                    )

                    if _upload_summary_has_failures(latest_upload_summary):
                        upload_error = _build_upload_retry_error(latest_upload_summary, exhausted=False)
                        crud.update_registration_task(
                            db,
                            task_uuid,
                            result=task_result_payload or None,
                            error_message=upload_error,
                        )
                        raise RuntimeError(upload_error)

                    crud.update_registration_task(
                        db,
                        task_uuid,
                        status="completed",
                        completed_at=utcnow_naive(),
                        result=task_result_payload or None,
                        error_message=None,
                    )
                    task_manager.update_status(
                        task_uuid,
                        "completed",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        retrying=False,
                        last_error=None,
                        next_retry_in_seconds=None,
                        email=last_email,
                        email_service=last_email_service,
                    )
                    logger.info(f"注册任务完成: {task_uuid}, 邮箱: {last_email}")
                    return
                except RegistrationCancelledError as exc:
                    last_error = str(exc) or "任务已取消"
                    _mark_task_cancelled(
                        db,
                        task_uuid,
                        attempt=attempt,
                        max_attempts=max_attempts,
                        error_message=last_error,
                        email=last_email,
                        email_service=last_email_service,
                    )
                    logger.info(f"任务 {task_uuid} 在执行过程中已取消: {last_error}")
                    return
                except Exception as exc:
                    last_error = str(exc)
                    if _upload_summary_has_failures(latest_upload_summary) and (saved_account_email or saved_account_id is not None):
                        logger.warning(
                            f"注册任务自动上传失败，将复用已注册账号重试: {task_uuid}, 邮箱: {saved_account_email or last_email}"
                        )
                    if attempt >= max_attempts and _upload_summary_has_failures(latest_upload_summary):
                        last_error = _build_upload_retry_error(latest_upload_summary, exhausted=True)
                    logger.warning(f"注册任务第 {attempt}/{max_attempts} 次尝试失败: {task_uuid}, 原因: {last_error}")

                if attempt < max_attempts:
                    wait_seconds = _get_retry_wait_seconds(attempt)
                    log_callback(f"[系统] 第 {attempt}/{max_attempts} 次尝试失败: {last_error}")
                    log_callback(f"[系统] 将在 {wait_seconds} 秒后自动重试")
                    crud.update_registration_task(
                        db,
                        task_uuid,
                        status="running",
                        result=task_result_payload or None,
                        error_message=last_error,
                    )
                    task_manager.update_status(
                        task_uuid,
                        "running",
                        attempt=attempt,
                        max_attempts=max_attempts,
                        retrying=True,
                        last_error=last_error,
                        error=last_error,
                        next_retry_in_seconds=wait_seconds,
                        email=last_email,
                        email_service=last_email_service,
                    )
                    if not _wait_for_retry_or_cancel(task_uuid, wait_seconds):
                        _mark_task_cancelled(
                            db,
                            task_uuid,
                            attempt=attempt,
                            max_attempts=max_attempts,
                            error_message=last_error,
                            email=last_email,
                            email_service=last_email_service,
                        )
                        logger.info(f"任务 {task_uuid} 在重试等待期间已取消")
                        return
                    continue

                if _upload_summary_has_failures(latest_upload_summary) and (saved_account_email or saved_account_id is not None):
                    last_error = _build_upload_retry_error(latest_upload_summary, exhausted=True)
                    log_callback(f"[错误] {last_error}")
                    logger.warning(
                        f"注册任务自动上传重试已耗尽，但账号已保留: {task_uuid}, 邮箱: {saved_account_email or last_email}"
                    )
                else:
                    log_callback(f"[错误] 重试已耗尽，任务失败: {last_error}")
                crud.update_registration_task(
                    db,
                    task_uuid,
                    status="failed",
                    completed_at=utcnow_naive(),
                    result=task_result_payload or None,
                    error_message=last_error,
                )
                task_manager.update_status(
                    task_uuid,
                    "failed",
                    attempt=attempt,
                    max_attempts=max_attempts,
                    retrying=False,
                    last_error=last_error,
                    error=last_error,
                    next_retry_in_seconds=None,
                    email=last_email,
                    email_service=last_email_service,
                )
                logger.warning(f"注册任务失败: {task_uuid}, 原因: {last_error}")
                return

        except RegistrationCancelledError as e:
            logger.info(f"注册任务已取消: {task_uuid}, 原因: {e}")
            _mark_task_cancelled(
                db,
                task_uuid,
                attempt=None,
                max_attempts=max_attempts,
                error_message=str(e),
                email=last_email,
                email_service=last_email_service,
            )
        except Exception as e:
            logger.error(f"注册任务异常: {task_uuid}, 错误: {e}", exc_info=True)

            try:
                crud.update_registration_task(
                    db,
                    task_uuid,
                    status="failed",
                    completed_at=utcnow_naive(),
                    result=task_result_payload or None,
                    error_message=str(e),
                )
                task_manager.update_status(
                    task_uuid,
                    "failed",
                    attempt=None,
                    max_attempts=max_attempts,
                    retrying=False,
                    last_error=str(e),
                    error=str(e),
                    next_retry_in_seconds=None,
                    email=last_email,
                    email_service=last_email_service,
                )
            except Exception as inner_exc:
                logger.error(f"注册任务 {task_uuid} 更新失败状态时再次异常: {inner_exc}", exc_info=True)


async def run_registration_task(
    task_uuid: str,
    email_service_type: str,
    proxy: Optional[str],
    email_service_config: Optional[dict],
    email_service_id: Optional[int] = None,
    random_email_service: bool = False,
    random_outlook_account: bool = False,
    random_domain: bool = False,
    subdomain_only: bool = False,
    selected_email_addresses: List[str] = None,
    selected_domains: List[str] = None,
    selection_index: int = 0,
    log_prefix: str = "",
    batch_id: str = "",
    registration_mode: str = "protocol",
    protocol_variant: str = "legacy",
    auto_upload_cpa: bool = False,
    cpa_service_ids: List[int] = None,
    auto_upload_sub2api: bool = False,
    sub2api_service_ids: List[int] = None,
    auto_upload_tm: bool = False,
    tm_service_ids: List[int] = None,
    auto_upload_new_api: bool = False,
    new_api_service_ids: List[int] = None,
    registration_type: str = RoleTag.CHILD.value,
):
    """
    异步执行注册任务

    使用 run_in_executor 将同步任务放入线程池执行，避免阻塞主事件循环
    """
    loop = task_manager.get_loop()
    if loop is None:
        loop = asyncio.get_event_loop()
        task_manager.set_loop(loop)

    # 初始化 TaskManager 状态
    task_manager.update_status(
        task_uuid,
        "pending",
        attempt=0,
        max_attempts=_get_max_registration_attempts(),
        retrying=False,
        last_error=None,
        next_retry_in_seconds=None,
        email_service=email_service_type or None,
    )
    task_manager.add_log(task_uuid, f"{log_prefix} [系统] 任务 {task_uuid[:8]} 已加入队列" if log_prefix else f"[系统] 任务 {task_uuid[:8]} 已加入队列")

    try:
        # 在线程池中执行同步任务（传入 log_prefix 和 batch_id 供回调使用）
        await loop.run_in_executor(
            task_manager.executor,
            _run_sync_registration_task,
            task_uuid,
            email_service_type,
            proxy,
            email_service_config,
            email_service_id,
            random_email_service,
            random_outlook_account,
            random_domain,
            subdomain_only,
            selected_email_addresses or [],
            selected_domains or [],
            selection_index,
            log_prefix,
            batch_id,
            registration_mode,
            protocol_variant,
            auto_upload_cpa,
            cpa_service_ids or [],
            auto_upload_sub2api,
            sub2api_service_ids or [],
            auto_upload_tm,
            tm_service_ids or [],
            auto_upload_new_api,
            new_api_service_ids or [],
            registration_type,
        )
    except Exception as e:
        logger.error(f"线程池执行异常: {task_uuid}, 错误: {e}")
        task_manager.add_log(task_uuid, f"[错误] 线程池执行异常: {str(e)}")
        task_manager.update_status(
            task_uuid,
            "failed",
            attempt=None,
            max_attempts=_get_max_registration_attempts(),
            retrying=False,
            last_error=str(e),
            error=str(e),
            next_retry_in_seconds=None,
            email_service=email_service_type or None,
        )


def _init_batch_state(batch_id: str, task_uuids: List[str]):
    """初始化批量任务内存状态"""
    task_manager.init_batch(batch_id, len(task_uuids))
    batch_tasks[batch_id] = {
        "total": len(task_uuids),
        "completed": 0,
        "success": 0,
        "failed": 0,
        "cancelled": False,
        "task_uuids": task_uuids,
        "current_index": 0,
        "logs": [],
        "finished": False
    }


def _make_batch_helpers(batch_id: str):
    """返回 add_batch_log 和 update_batch_status 辅助函数"""
    def add_batch_log(msg: str):
        batch_tasks[batch_id]["logs"].append(msg)
        task_manager.add_batch_log(batch_id, msg)

    def update_batch_status(**kwargs):
        for key, value in kwargs.items():
            if key in batch_tasks[batch_id]:
                batch_tasks[batch_id][key] = value
        task_manager.update_batch_status(batch_id, **kwargs)

    return add_batch_log, update_batch_status


async def run_batch_parallel(
    batch_id: str,
    task_uuids: List[str],
    email_service_type: str,
    proxy: Optional[str],
    email_service_config: Optional[dict],
    email_service_id: Optional[int],
    random_email_service: bool,
    random_outlook_account: bool,
    random_domain: bool,
    subdomain_only: bool,
    selected_email_addresses: List[str],
    selected_domains: List[str],
    concurrency: int,
    auto_upload_cpa: bool = False,
    cpa_service_ids: List[int] = None,
    auto_upload_sub2api: bool = False,
    sub2api_service_ids: List[int] = None,
    auto_upload_tm: bool = False,
    tm_service_ids: List[int] = None,
    auto_upload_new_api: bool = False,
    new_api_service_ids: List[int] = None,
    registration_mode: str = "protocol",
    protocol_variant: str = "legacy",
    registration_type: str = RoleTag.CHILD.value,
):
    """
    并行模式：所有任务同时提交，Semaphore 控制最大并发数
    """
    _init_batch_state(batch_id, task_uuids)
    add_batch_log, update_batch_status = _make_batch_helpers(batch_id)
    semaphore = asyncio.Semaphore(concurrency)
    counter_lock = asyncio.Lock()
    add_batch_log(f"[系统] 并行模式启动，并发数: {concurrency}，总任务: {len(task_uuids)}")

    async def _run_one(idx: int, uuid: str):
        prefix = f"[任务{idx + 1}]"
        async with semaphore:
            if task_manager.is_batch_cancelled(batch_id) or batch_tasks[batch_id]["cancelled"]:
                with get_db() as db:
                    crud.update_registration_task(
                        db,
                        uuid,
                        status="cancelled",
                        completed_at=utcnow_naive(),
                        error_message="批量任务已取消",
                    )
                task_manager.cancel_task(uuid)
                task_manager.update_status(uuid, "cancelled", error="批量任务已取消")
                return
            await run_registration_task(
                uuid, email_service_type, proxy, email_service_config, email_service_id,
                random_email_service, random_outlook_account, random_domain, subdomain_only,
                selected_email_addresses, selected_domains, idx,
                log_prefix=prefix, batch_id=batch_id,
                auto_upload_cpa=auto_upload_cpa, cpa_service_ids=cpa_service_ids or [],
                auto_upload_sub2api=auto_upload_sub2api, sub2api_service_ids=sub2api_service_ids or [],
                auto_upload_tm=auto_upload_tm, tm_service_ids=tm_service_ids or [],
                auto_upload_new_api=auto_upload_new_api, new_api_service_ids=new_api_service_ids or [],
                registration_mode=registration_mode,
                protocol_variant=protocol_variant,
                registration_type=registration_type,
            )
        with get_db() as db:
            t = crud.get_registration_task(db, uuid)
            if t:
                async with counter_lock:
                    new_completed = batch_tasks[batch_id]["completed"] + 1
                    new_success = batch_tasks[batch_id]["success"]
                    new_failed = batch_tasks[batch_id]["failed"]
                    if t.status == "completed":
                        new_success += 1
                        add_batch_log(f"{prefix} [成功] 注册成功")
                    elif t.status == "failed":
                        new_failed += 1
                        add_batch_log(f"{prefix} [失败] 注册失败: {t.error_message}")
                    update_batch_status(completed=new_completed, success=new_success, failed=new_failed)

    try:
        await asyncio.gather(*[_run_one(i, u) for i, u in enumerate(task_uuids)], return_exceptions=True)
        if not task_manager.is_batch_cancelled(batch_id):
            add_batch_log(f"[完成] 批量任务完成！成功: {batch_tasks[batch_id]['success']}, 失败: {batch_tasks[batch_id]['failed']}")
            update_batch_status(finished=True, status="completed")
        else:
            update_batch_status(finished=True, status="cancelled")
    except Exception as e:
        logger.error(f"批量任务 {batch_id} 异常: {e}")
        add_batch_log(f"[错误] 批量任务异常: {str(e)}")
        update_batch_status(finished=True, status="failed")
    finally:
        batch_tasks[batch_id]["finished"] = True


async def run_batch_pipeline(
    batch_id: str,
    task_uuids: List[str],
    email_service_type: str,
    proxy: Optional[str],
    email_service_config: Optional[dict],
    email_service_id: Optional[int],
    random_email_service: bool,
    random_outlook_account: bool,
    random_domain: bool,
    subdomain_only: bool,
    selected_email_addresses: List[str],
    selected_domains: List[str],
    interval_min: int,
    interval_max: int,
    concurrency: int,
    auto_upload_cpa: bool = False,
    cpa_service_ids: List[int] = None,
    auto_upload_sub2api: bool = False,
    sub2api_service_ids: List[int] = None,
    auto_upload_tm: bool = False,
    tm_service_ids: List[int] = None,
    auto_upload_new_api: bool = False,
    new_api_service_ids: List[int] = None,
    registration_mode: str = "protocol",
    protocol_variant: str = "legacy",
    registration_type: str = RoleTag.CHILD.value,
):
    """
    流水线模式：每隔 interval 秒启动一个新任务，Semaphore 限制最大并发数
    """
    _init_batch_state(batch_id, task_uuids)
    add_batch_log, update_batch_status = _make_batch_helpers(batch_id)
    semaphore = asyncio.Semaphore(concurrency)
    counter_lock = asyncio.Lock()
    running_tasks_list = []
    add_batch_log(f"[系统] 流水线模式启动，并发数: {concurrency}，总任务: {len(task_uuids)}")

    async def _run_and_release(idx: int, uuid: str, pfx: str):
        try:
            if task_manager.is_batch_cancelled(batch_id) or batch_tasks[batch_id]["cancelled"]:
                with get_db() as db:
                    crud.update_registration_task(
                        db,
                        uuid,
                        status="cancelled",
                        completed_at=utcnow_naive(),
                        error_message="批量任务已取消",
                    )
                task_manager.cancel_task(uuid)
                task_manager.update_status(uuid, "cancelled", error="批量任务已取消")
                return
            await run_registration_task(
                uuid, email_service_type, proxy, email_service_config, email_service_id,
                random_email_service, random_outlook_account, random_domain, subdomain_only,
                selected_email_addresses, selected_domains, idx,
                log_prefix=pfx, batch_id=batch_id,
                auto_upload_cpa=auto_upload_cpa, cpa_service_ids=cpa_service_ids or [],
                auto_upload_sub2api=auto_upload_sub2api, sub2api_service_ids=sub2api_service_ids or [],
                auto_upload_tm=auto_upload_tm, tm_service_ids=tm_service_ids or [],
                auto_upload_new_api=auto_upload_new_api, new_api_service_ids=new_api_service_ids or [],
                registration_mode=registration_mode,
                protocol_variant=protocol_variant,
                registration_type=registration_type,
            )
            with get_db() as db:
                t = crud.get_registration_task(db, uuid)
                if t:
                    async with counter_lock:
                        new_completed = batch_tasks[batch_id]["completed"] + 1
                        new_success = batch_tasks[batch_id]["success"]
                        new_failed = batch_tasks[batch_id]["failed"]
                        if t.status == "completed":
                            new_success += 1
                            add_batch_log(f"{pfx} [成功] 注册成功")
                        elif t.status == "failed":
                            new_failed += 1
                            add_batch_log(f"{pfx} [失败] 注册失败: {t.error_message}")
                        update_batch_status(completed=new_completed, success=new_success, failed=new_failed)
        finally:
            semaphore.release()

    try:
        for i, task_uuid in enumerate(task_uuids):
            if task_manager.is_batch_cancelled(batch_id) or batch_tasks[batch_id]["cancelled"]:
                with get_db() as db:
                    for remaining_uuid in task_uuids[i:]:
                        crud.update_registration_task(db, remaining_uuid, status="cancelled")
                add_batch_log("[取消] 批量任务已取消")
                update_batch_status(finished=True, status="cancelled")
                break

            update_batch_status(current_index=i)
            await semaphore.acquire()
            prefix = f"[任务{i + 1}]"
            add_batch_log(f"{prefix} 开始注册...")
            t = asyncio.create_task(_run_and_release(i, task_uuid, prefix))
            running_tasks_list.append(t)

            if i < len(task_uuids) - 1 and not task_manager.is_batch_cancelled(batch_id):
                wait_time = random.randint(interval_min, interval_max)
                logger.info(f"批量任务 {batch_id}: 等待 {wait_time} 秒后启动下一个任务")
                await asyncio.sleep(wait_time)

        if running_tasks_list:
            await asyncio.gather(*running_tasks_list, return_exceptions=True)

        if not task_manager.is_batch_cancelled(batch_id):
            add_batch_log(f"[完成] 批量任务完成！成功: {batch_tasks[batch_id]['success']}, 失败: {batch_tasks[batch_id]['failed']}")
            update_batch_status(finished=True, status="completed")
    except Exception as e:
        logger.error(f"批量任务 {batch_id} 异常: {e}")
        add_batch_log(f"[错误] 批量任务异常: {str(e)}")
        update_batch_status(finished=True, status="failed")
    finally:
        batch_tasks[batch_id]["finished"] = True


async def run_batch_registration(
    batch_id: str,
    task_uuids: List[str],
    email_service_type: str,
    proxy: Optional[str],
    email_service_config: Optional[dict],
    email_service_id: Optional[int],
    random_email_service: bool = False,
    random_outlook_account: bool = False,
    random_domain: bool = False,
    subdomain_only: bool = False,
    selected_email_addresses: List[str] = None,
    selected_domains: List[str] = None,
    interval_min: int = 5,
    interval_max: int = 30,
    concurrency: int = 1,
    mode: str = "pipeline",
    auto_upload_cpa: bool = False,
    cpa_service_ids: List[int] = None,
    auto_upload_sub2api: bool = False,
    sub2api_service_ids: List[int] = None,
    auto_upload_tm: bool = False,
    tm_service_ids: List[int] = None,
    auto_upload_new_api: bool = False,
    new_api_service_ids: List[int] = None,
    registration_mode: str = "protocol",
    protocol_variant: str = "legacy",
    registration_type: str = RoleTag.CHILD.value,
):
    """根据 mode 分发到并行或流水线执行"""
    if mode == "parallel":
        await run_batch_parallel(
            batch_id, task_uuids, email_service_type, proxy,
            email_service_config, email_service_id,
            random_email_service, random_outlook_account, random_domain, subdomain_only,
            selected_email_addresses or [], selected_domains or [],
            concurrency,
            auto_upload_cpa=auto_upload_cpa, cpa_service_ids=cpa_service_ids,
            auto_upload_sub2api=auto_upload_sub2api, sub2api_service_ids=sub2api_service_ids,
            auto_upload_tm=auto_upload_tm, tm_service_ids=tm_service_ids,
            auto_upload_new_api=auto_upload_new_api, new_api_service_ids=new_api_service_ids,
            registration_mode=registration_mode,
            protocol_variant=protocol_variant,
            registration_type=registration_type,
        )
    else:
        await run_batch_pipeline(
            batch_id, task_uuids, email_service_type, proxy,
            email_service_config, email_service_id,
            random_email_service, random_outlook_account, random_domain, subdomain_only,
            selected_email_addresses or [], selected_domains or [],
            interval_min, interval_max, concurrency,
            auto_upload_cpa=auto_upload_cpa, cpa_service_ids=cpa_service_ids,
            auto_upload_sub2api=auto_upload_sub2api, sub2api_service_ids=sub2api_service_ids,
            auto_upload_tm=auto_upload_tm, tm_service_ids=tm_service_ids,
            auto_upload_new_api=auto_upload_new_api, new_api_service_ids=new_api_service_ids,
            registration_mode=registration_mode,
            protocol_variant=protocol_variant,
            registration_type=registration_type,
        )


async def run_auto_registration_batch(plan, settings: Settings) -> str:
    """执行自动补货批量注册。"""
    email_service_type = settings.registration_auto_email_service_type
    try:
        EmailServiceType(email_service_type)
    except ValueError as exc:
        raise ValueError(f"自动注册邮箱服务类型无效: {email_service_type}") from exc

    mode = settings.registration_auto_mode or "pipeline"
    if mode not in ("parallel", "pipeline"):
        raise ValueError(f"自动注册模式无效: {mode}")

    interval_min = max(0, int(settings.registration_auto_interval_min))
    interval_max = max(interval_min, int(settings.registration_auto_interval_max))
    concurrency = max(1, int(settings.registration_auto_concurrency))
    email_service_id = int(settings.registration_auto_email_service_id or 0) or None
    proxy = str(settings.registration_auto_proxy or "").strip() or None

    batch_id = str(uuid.uuid4())
    task_uuids = []

    with get_db() as db:
        for _ in range(plan.deficit):
            task_uuid = str(uuid.uuid4())
            crud.create_registration_task(
                db,
                task_uuid=task_uuid,
                proxy=proxy,
                email_service_id=email_service_id,
            )
            task_uuids.append(task_uuid)

    update_auto_registration_state(
        status="running",
        message=f"自动补货任务运行中: {batch_id}",
        current_batch_id=batch_id,
    )
    add_auto_registration_log(
        f"[自动注册] 已创建补货批量任务 {batch_id}，计划注册 {len(task_uuids)} 个账号"
    )

    await run_batch_registration(
        batch_id=batch_id,
        task_uuids=task_uuids,
        email_service_type=email_service_type,
        proxy=proxy,
        email_service_config=None,
        email_service_id=email_service_id,
        interval_min=interval_min,
        interval_max=interval_max,
        concurrency=concurrency,
        mode=mode,
        auto_upload_cpa=True,
        cpa_service_ids=[plan.cpa_service_id],
        auto_upload_sub2api=False,
        sub2api_service_ids=[],
        auto_upload_tm=False,
        tm_service_ids=[],
        auto_upload_new_api=False,
        new_api_service_ids=[],
        registration_type=RoleTag.CHILD.value,
    )

    batch = batch_tasks.get(batch_id)
    if batch:
        batch_cancelled = bool(batch.get("cancelled"))
        current_auto_state = get_auto_registration_state()
        refreshed_inventory = await asyncio.to_thread(
            get_auto_registration_inventory,
            settings,
        )
        refreshed_ready_count = (
            refreshed_inventory[0]
            if refreshed_inventory
            else current_auto_state.get("current_ready_count")
        )
        refreshed_target_count = (
            refreshed_inventory[1]
            if refreshed_inventory
            else max(1, int(settings.registration_auto_min_ready_auth_files or 1))
        )
        final_status = "cancelled" if batch_cancelled else "idle"
        final_message = (
            f"自动补货批量任务已取消: {batch_id}"
            if batch_cancelled
            else f"自动补货批量任务已完成: {batch_id}"
        )
        final_log_message = (
            f"[自动注册] 补货批量任务已取消：成功 {batch.get('success', 0)}，失败 {batch.get('failed', 0)}"
            if batch_cancelled
            else f"[自动注册] 补货批量任务已完成：成功 {batch.get('success', 0)}，失败 {batch.get('failed', 0)}"
        )
        update_auto_registration_state(
            status=final_status,
            message=final_message,
            current_batch_id=None,
            current_ready_count=refreshed_ready_count,
            target_ready_count=refreshed_target_count,
            last_checked_at=datetime.now(timezone.utc).isoformat(),
        )
        add_auto_registration_log(final_log_message)

    return batch_id


def _validate_registration_request(email_service_type: str):
    """校验邮箱服务类型。"""
    try:
        EmailServiceType(email_service_type)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"无效的邮箱服务类型: {email_service_type}",
        ) from exc


def _validate_subdomain_only_request(
    *,
    email_service_type: str,
    email_service_id: Optional[int],
    email_service_config: Optional[dict],
    proxy: Optional[str],
    random_email_service: bool,
    random_outlook_account: bool,
    random_domain: bool,
    subdomain_only: bool,
    selected_email_addresses: Optional[List[str]],
    selected_domains: Optional[List[str]],
) -> None:
    """在任务创建前提前校验仅子域名配置，避免静默忽略或异步晚失败。"""
    if not subdomain_only:
        return

    if EmailServiceType(email_service_type) not in DOMAIN_SELECTION_TYPES:
        raise HTTPException(status_code=400, detail="当前邮箱服务不支持仅子域名筛选")

    with get_db() as db:
        try:
            resolve_email_service_for_registration(
                db=db,
                service_type=EmailServiceType(email_service_type),
                requested_service_id=email_service_id,
                fallback_config=email_service_config,
                proxy_url=proxy,
                selection=RegistrationSelectionRequest(
                    random_email_service=random_email_service,
                    random_outlook_account=random_outlook_account,
                    random_domain=random_domain,
                    subdomain_only=True,
                    selected_email_addresses=selected_email_addresses or [],
                    selected_domains=selected_domains or [],
                    selection_index=0,
                ),
            )
        except (ValueError, RegistrationSelectionExhaustedError) as exc:
            raise HTTPException(status_code=400, detail=str(exc) or "当前邮箱服务不支持仅子域名筛选") from exc


def _schedule_async_job(background_tasks: Optional[BackgroundTasks], coroutine_func, *args):
    """统一调度后台异步任务。"""
    if background_tasks is not None:
        background_tasks.add_task(coroutine_func, *args)
        return

    loop = task_manager.get_loop()
    if loop is None:
        loop = asyncio.get_event_loop()
        task_manager.set_loop(loop)
    loop.create_task(coroutine_func(*args))


def _response_payload(response: Any) -> Dict[str, Any]:
    """兼容 Pydantic v1/v2 与测试替身对象。"""
    if hasattr(response, "model_dump"):
        return dict(response.model_dump())
    if hasattr(response, "dict"):
        return dict(response.dict())
    return dict(getattr(response, "__dict__", {}) or {})


async def _start_single_registration_internal(
    request: RegistrationTaskCreate,
    background_tasks: Optional[BackgroundTasks] = None,
) -> RegistrationTaskResponse:
    """启动单次注册任务。"""
    _validate_registration_request(request.email_service_type)
    _validate_subdomain_only_request(
        email_service_type=request.email_service_type,
        email_service_id=request.email_service_id,
        email_service_config=request.email_service_config,
        proxy=request.proxy,
        random_email_service=request.random_email_service,
        random_outlook_account=request.random_outlook_account,
        random_domain=request.random_domain,
        subdomain_only=request.subdomain_only,
        selected_email_addresses=request.selected_email_addresses,
        selected_domains=request.selected_domains,
    )

    task_uuid = str(uuid.uuid4())
    with get_db() as db:
        task = crud.create_registration_task(
            db,
            task_uuid=task_uuid,
            proxy=request.proxy,
        )

    _schedule_async_job(
        background_tasks,
        run_registration_task,
        task_uuid,
        request.email_service_type,
        request.proxy,
        request.email_service_config,
        request.email_service_id,
        request.random_email_service,
        request.random_outlook_account,
        request.random_domain,
        request.subdomain_only,
        request.selected_email_addresses,
        request.selected_domains,
        0,
        "",
        "",
        request.registration_mode,
        request.protocol_variant,
        request.auto_upload_cpa,
        request.cpa_service_ids,
        request.auto_upload_sub2api,
        request.sub2api_service_ids,
        request.auto_upload_tm,
        request.tm_service_ids,
        request.auto_upload_new_api,
        request.new_api_service_ids,
        request.registration_type,
    )
    return task_to_response(task)


async def _start_batch_registration_internal(
    request: BatchRegistrationRequest,
    background_tasks: Optional[BackgroundTasks] = None,
) -> BatchRegistrationResponse:
    """启动普通批量注册任务。"""
    if request.count < 1 or request.count > 1000:
        raise HTTPException(status_code=400, detail="注册数量必须在 1-1000 之间")

    _validate_registration_request(request.email_service_type)

    if request.interval_min < 0 or request.interval_max < request.interval_min:
        raise HTTPException(status_code=400, detail="间隔时间参数无效")

    if not 1 <= request.concurrency <= 50:
        raise HTTPException(status_code=400, detail="并发数必须在 1-50 之间")

    if request.mode not in ("parallel", "pipeline"):
        raise HTTPException(status_code=400, detail="模式必须为 parallel 或 pipeline")

    _validate_subdomain_only_request(
        email_service_type=request.email_service_type,
        email_service_id=request.email_service_id,
        email_service_config=request.email_service_config,
        proxy=request.proxy,
        random_email_service=request.random_email_service,
        random_outlook_account=request.random_outlook_account,
        random_domain=request.random_domain,
        subdomain_only=request.subdomain_only,
        selected_email_addresses=request.selected_email_addresses,
        selected_domains=request.selected_domains,
    )

    batch_id = str(uuid.uuid4())
    task_uuids = []

    with get_db() as db:
        for _ in range(request.count):
            task_uuid = str(uuid.uuid4())
            crud.create_registration_task(
                db,
                task_uuid=task_uuid,
                proxy=request.proxy,
            )
            task_uuids.append(task_uuid)

    with get_db() as db:
        tasks = [crud.get_registration_task(db, item_uuid) for item_uuid in task_uuids]

    _schedule_async_job(
        background_tasks,
        run_batch_registration,
        batch_id,
        task_uuids,
        request.email_service_type,
        request.proxy,
        request.email_service_config,
        request.email_service_id,
        request.random_email_service,
        request.random_outlook_account,
        request.random_domain,
        request.subdomain_only,
        request.selected_email_addresses,
        request.selected_domains,
        request.interval_min,
        request.interval_max,
        request.concurrency,
        request.mode,
        request.auto_upload_cpa,
        request.cpa_service_ids,
        request.auto_upload_sub2api,
        request.sub2api_service_ids,
        request.auto_upload_tm,
        request.tm_service_ids,
        request.auto_upload_new_api,
        request.new_api_service_ids,
        request.registration_mode,
        request.protocol_variant,
        request.registration_type,
    )

    return BatchRegistrationResponse(
        batch_id=batch_id,
        count=request.count,
        tasks=[task_to_response(task) for task in tasks if task],
    )


async def _start_outlook_batch_registration_internal(
    request: OutlookBatchRegistrationRequest,
    background_tasks: Optional[BackgroundTasks] = None,
) -> OutlookBatchRegistrationResponse:
    """启动 Outlook 批量注册任务。"""
    from ...database.models import EmailService as EmailServiceModel

    if not request.service_ids:
        raise HTTPException(status_code=400, detail="请选择至少一个 Outlook 账户")

    if request.interval_min < 0 or request.interval_max < request.interval_min:
        raise HTTPException(status_code=400, detail="间隔时间参数无效")

    if not 1 <= request.concurrency <= 50:
        raise HTTPException(status_code=400, detail="并发数必须在 1-50 之间")

    if request.mode not in ("parallel", "pipeline"):
        raise HTTPException(status_code=400, detail="模式必须为 parallel 或 pipeline")

    actual_service_ids = request.service_ids
    skipped_count = 0

    if request.skip_registered:
        actual_service_ids = []
        with get_db() as db:
            for service_id in request.service_ids:
                service = db.query(EmailServiceModel).filter(
                    EmailServiceModel.id == service_id
                ).first()
                if not service:
                    continue

                config = service.config or {}
                email = config.get("email") or service.name
                existing_account = db.query(Account).filter(Account.email == email).first()
                if existing_account:
                    skipped_count += 1
                else:
                    actual_service_ids.append(service_id)

    if not actual_service_ids:
        return OutlookBatchRegistrationResponse(
            batch_id="",
            total=len(request.service_ids),
            skipped=skipped_count,
            to_register=0,
            service_ids=[],
        )

    batch_id = str(uuid.uuid4())
    batch_tasks[batch_id] = {
        "total": len(actual_service_ids),
        "completed": 0,
        "success": 0,
        "failed": 0,
        "skipped": 0,
        "cancelled": False,
        "service_ids": actual_service_ids,
        "current_index": 0,
        "logs": [],
        "finished": False,
    }

    _schedule_async_job(
        background_tasks,
        run_outlook_batch_registration,
        batch_id,
        actual_service_ids,
        request.skip_registered,
        request.proxy,
        request.interval_min,
        request.interval_max,
        request.concurrency,
        request.mode,
        request.auto_upload_cpa,
        request.cpa_service_ids,
        request.auto_upload_sub2api,
        request.sub2api_service_ids,
        request.auto_upload_tm,
        request.tm_service_ids,
        request.auto_upload_new_api,
        request.new_api_service_ids,
        request.registration_type,
    )

    return OutlookBatchRegistrationResponse(
        batch_id=batch_id,
        total=len(request.service_ids),
        skipped=skipped_count,
        to_register=len(actual_service_ids),
        service_ids=actual_service_ids,
    )


async def dispatch_registration_config(
    registration_config: Dict[str, Any],
    background_tasks: Optional[BackgroundTasks] = None,
) -> Dict[str, Any]:
    """按统一注册配置分发执行注册任务。"""
    config = dict(registration_config or {})
    reg_mode = config.get("reg_mode") or "single"
    email_service_type = config.get("email_service_type")
    if not email_service_type:
        raise HTTPException(status_code=400, detail="缺少邮箱服务类型")

    if email_service_type == "outlook_batch":
        request = OutlookBatchRegistrationRequest(
            service_ids=config.get("service_ids") or [],
            skip_registered=bool(config.get("skip_registered", True)),
            proxy=config.get("proxy"),
            interval_min=int(config.get("interval_min") or 5),
            interval_max=int(config.get("interval_max") or 30),
            concurrency=int(config.get("concurrency") or 1),
            mode=config.get("mode") or "pipeline",
            auto_upload_cpa=bool(config.get("auto_upload_cpa", False)),
            cpa_service_ids=config.get("cpa_service_ids") or [],
            auto_upload_sub2api=bool(config.get("auto_upload_sub2api", False)),
            sub2api_service_ids=config.get("sub2api_service_ids") or [],
            auto_upload_tm=bool(config.get("auto_upload_tm", False)),
            tm_service_ids=config.get("tm_service_ids") or [],
            auto_upload_new_api=bool(config.get("auto_upload_new_api", False)),
            new_api_service_ids=config.get("new_api_service_ids") or [],
            registration_type=config.get("registration_type") or RoleTag.CHILD.value,
        )
        response = await _start_outlook_batch_registration_internal(request, background_tasks)
        return {
            "kind": "batch",
            "batch_id": response.batch_id,
            "payload": _response_payload(response),
        }

    _validate_registration_request(email_service_type)

    common_kwargs = {
        "registration_mode": _normalize_registration_mode(config.get("registration_mode") or "protocol"),
        "protocol_variant": _normalize_protocol_variant(config.get("protocol_variant") or "legacy"),
        "random_email_service": bool(config.get("random_email_service", False)),
        "random_outlook_account": bool(config.get("random_outlook_account", False)),
        "random_domain": bool(config.get("random_domain", False)),
        "subdomain_only": bool(config.get("subdomain_only", False)),
        "selected_email_addresses": config.get("selected_email_addresses") or [],
        "selected_domains": config.get("selected_domains") or [],
        "auto_upload_cpa": bool(config.get("auto_upload_cpa", False)),
        "cpa_service_ids": config.get("cpa_service_ids") or [],
        "auto_upload_sub2api": bool(config.get("auto_upload_sub2api", False)),
        "sub2api_service_ids": config.get("sub2api_service_ids") or [],
        "auto_upload_tm": bool(config.get("auto_upload_tm", False)),
        "tm_service_ids": config.get("tm_service_ids") or [],
        "auto_upload_new_api": bool(config.get("auto_upload_new_api", False)),
        "new_api_service_ids": config.get("new_api_service_ids") or [],
        "registration_type": config.get("registration_type") or RoleTag.CHILD.value,
    }

    if reg_mode == "batch":
        request = BatchRegistrationRequest(
            count=int(config.get("batch_count") or 1),
            email_service_type=email_service_type,
            proxy=config.get("proxy"),
            email_service_config=config.get("email_service_config"),
            email_service_id=config.get("email_service_id"),
            interval_min=int(config.get("interval_min") or 5),
            interval_max=int(config.get("interval_max") or 30),
            concurrency=int(config.get("concurrency") or 1),
            mode=config.get("mode") or "pipeline",
            **common_kwargs,
        )
        response = await _start_batch_registration_internal(request, background_tasks)
        return {
            "kind": "batch",
            "batch_id": response.batch_id,
            "payload": _response_payload(response),
        }

    request = RegistrationTaskCreate(
        email_service_type=email_service_type,
        proxy=config.get("proxy"),
        email_service_config=config.get("email_service_config"),
        email_service_id=config.get("email_service_id"),
        **common_kwargs,
    )
    response = await _start_single_registration_internal(request, background_tasks)
    return {
        "kind": "single",
        "task_uuid": response.task_uuid,
        "payload": _response_payload(response),
    }


# ============== API Endpoints ==============

@router.post("/start", response_model=RegistrationTaskResponse)
async def start_registration(
    request: RegistrationTaskCreate,
    background_tasks: BackgroundTasks
):
    return await _start_single_registration_internal(request, background_tasks)


@router.post("/batch", response_model=BatchRegistrationResponse)
async def start_batch_registration(
    request: BatchRegistrationRequest,
    background_tasks: BackgroundTasks
):
    return await _start_batch_registration_internal(request, background_tasks)


@router.get("/batch/{batch_id}")
async def get_batch_status(batch_id: str):
    """获取批量任务状态"""
    if batch_id not in batch_tasks:
        raise HTTPException(status_code=404, detail="批量任务不存在")

    batch = batch_tasks[batch_id]
    runtime_status = task_manager.get_batch_status(batch_id) or {}
    return {
        "batch_id": batch_id,
        "total": batch["total"],
        "completed": batch["completed"],
        "success": batch["success"],
        "failed": batch["failed"],
        "current_index": batch["current_index"],
        "cancelled": batch["cancelled"],
        "finished": batch.get("finished", False),
        "status": runtime_status.get("status", "running"),
        "progress": f"{batch['completed']}/{batch['total']}"
    }


@router.get("/auto-monitor")
async def get_auto_registration_monitor():
    auto_state = get_auto_registration_state()
    current_batch_id = auto_state.get("current_batch_id")
    batch = batch_tasks.get(current_batch_id) if current_batch_id else None
    logs = get_auto_registration_logs().copy()
    return {
        **auto_state,
        "batch": batch,
        "logs": logs,
    }


@router.post("/batch/{batch_id}/cancel")
async def cancel_batch(batch_id: str):
    """取消批量任务"""
    if batch_id not in batch_tasks:
        raise HTTPException(status_code=404, detail="批量任务不存在")

    batch = batch_tasks[batch_id]
    if batch.get("finished"):
        raise HTTPException(status_code=400, detail="批量任务已完成")

    batch["cancelled"] = True
    task_manager.cancel_batch(batch_id)
    _cancel_batch_tasks(batch_id)
    return {"success": True, "message": "批量任务取消请求已提交，正在让它们有序收工"}


@router.get("/tasks", response_model=TaskListResponse)
async def list_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    status: Optional[str] = Query(None),
):
    """获取任务列表"""
    with get_db() as db:
        query = db.query(RegistrationTask)

        if status:
            query = query.filter(RegistrationTask.status == status)

        total = query.count()
        offset = (page - 1) * page_size
        tasks = query.order_by(RegistrationTask.created_at.desc()).offset(offset).limit(page_size).all()

        return TaskListResponse(
            total=total,
            tasks=[task_to_response(t) for t in tasks]
        )


@router.get("/tasks/{task_uuid}", response_model=RegistrationTaskResponse)
async def get_task(task_uuid: str):
    """获取任务详情"""
    with get_db() as db:
        task = crud.get_registration_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")
        return task_to_response(task)


@router.get("/tasks/{task_uuid}/logs")
async def get_task_logs(task_uuid: str):
    """获取任务日志"""
    with get_db() as db:
        task = crud.get_registration_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")

        response = task_to_response(task)
        return {
            "task_uuid": task_uuid,
            "status": response.status,
            "logs": _combine_task_logs(task_uuid, task.logs),
            "error_message": response.error_message,
            "attempt": response.attempt,
            "max_attempts": response.max_attempts,
            "retrying": response.retrying,
            "last_error": response.last_error,
            "next_retry_in_seconds": response.next_retry_in_seconds,
            "email": response.email,
            "email_service": response.email_service,
        }


@router.post("/tasks/{task_uuid}/cancel")
async def cancel_task(task_uuid: str):
    """取消任务"""
    with get_db() as db:
        task = crud.get_registration_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")

        if task.status not in ["pending", "running"]:
            raise HTTPException(status_code=400, detail="任务已完成或已取消")

        task_manager.cancel_task(task_uuid)
        task = crud.update_registration_task(db, task_uuid, status="cancelled")
        task_manager.update_status(
            task_uuid,
            "cancelling",
            attempt=(task_manager.get_status(task_uuid) or {}).get("attempt"),
            max_attempts=(task_manager.get_status(task_uuid) or {}).get("max_attempts"),
            retrying=False,
            next_retry_in_seconds=None,
        )

        return {"success": True, "message": "任务已取消"}


@router.delete("/tasks/{task_uuid}")
async def delete_task(task_uuid: str):
    """删除任务"""
    with get_db() as db:
        task = crud.get_registration_task(db, task_uuid)
        if not task:
            raise HTTPException(status_code=404, detail="任务不存在")

        if task.status == "running":
            raise HTTPException(status_code=400, detail="无法删除运行中的任务")

        crud.delete_registration_task(db, task_uuid)

        return {"success": True, "message": "任务已删除"}


@router.get("/stats")
async def get_registration_stats():
    """获取注册统计信息"""
    with get_db() as db:
        from sqlalchemy import func

        # 按状态统计
        status_stats = db.query(
            RegistrationTask.status,
            func.count(RegistrationTask.id)
        ).group_by(RegistrationTask.status).all()

        # 今日统计
        today = utcnow_naive().date()
        today_status_stats = db.query(
            RegistrationTask.status,
            func.count(RegistrationTask.id)
        ).filter(
            func.date(RegistrationTask.created_at) == today
        ).group_by(RegistrationTask.status).all()

        today_count = db.query(func.count(RegistrationTask.id)).filter(
            func.date(RegistrationTask.created_at) == today
        ).scalar()

        today_by_status = {status: count for status, count in today_status_stats}
        today_success = int(today_by_status.get("completed", 0))
        today_failed = int(today_by_status.get("failed", 0))
        today_total = int(today_count or 0)
        today_success_rate = round((today_success / today_total) * 100, 1) if today_total > 0 else 0.0

        return {
            "by_status": {status: count for status, count in status_stats},
            "today_count": today_total,
            "today_total": today_total,
            "today_success": today_success,
            "today_failed": today_failed,
            "today_success_rate": today_success_rate,
            "today_by_status": today_by_status,
        }


@router.get("/email-stats", response_model=EmailRegistrationStatsResponse)
async def get_email_registration_stats(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    with get_db() as db:
        items = crud.get_email_registration_stats(db, skip=offset, limit=limit)
        total = crud.count_email_registration_stats(db)
        return EmailRegistrationStatsResponse(
            items=[_email_stat_to_response_item(item) for item in items],
            total=total,
        )


@router.get("/email-domain-stats", response_model=EmailDomainRegistrationStatsResponse)
async def get_email_domain_registration_stats(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    with get_db() as db:
        items = crud.get_email_domain_registration_stats(db, skip=offset, limit=limit)
        total = crud.count_email_domain_registration_stats(db)
        summary = crud.get_email_domain_registration_summary(db)
        return EmailDomainRegistrationStatsResponse(
            items=[_email_domain_stat_to_response_item(item) for item in items],
            total=total,
            summary=_email_domain_summary_to_response_item(summary),
        )


@router.get("/available-services")
async def get_available_email_services():
    """
    获取可用于注册的邮箱服务列表

    返回所有已启用的邮箱服务，包括：
    - tempmail: 临时邮箱（无需配置）
    - yyds_mail: YYDS Mail 临时邮箱（需 API Key）
    - outlook: 已导入的 Outlook 账户
    - moe_mail: 已配置的自定义域名服务
    """
    from ...database.models import EmailService as EmailServiceModel
    from ...config.settings import get_settings

    settings = get_settings()
    result = {
        "tempmail": {
            "available": bool(settings.tempmail_enabled),
            "count": 1 if settings.tempmail_enabled else 0,
            "services": ([{
                "id": None,
                "name": "Tempmail.lol",
                "type": "tempmail",
                "description": "临时邮箱，自动创建"
            }] if settings.tempmail_enabled else [])
        },
        "yyds_mail": {
            "available": False,
            "count": 0,
            "services": []
        },
        "outlook": {
            "available": False,
            "count": 0,
            "services": []
        },
        "moe_mail": {
            "available": False,
            "count": 0,
            "services": []
        },
        "yyds_mail": {
            "available": False,
            "count": 0,
            "services": []
        },
        "temp_mail": {
            "available": False,
            "count": 0,
            "services": []
        },
        "cloudmail": {
            "available": False,
            "count": 0,
            "services": []
        },
        "duck_mail": {
            "available": False,
            "count": 0,
            "services": []
        },
        "luckmail": {
            "available": False,
            "count": 0,
            "services": []
        },
        "freemail": {
            "available": False,
            "count": 0,
            "services": []
        },
        "imap_mail": {
            "available": False,
            "count": 0,
            "services": []
        },
        "luckmail": {
            "available": False,
            "count": 0,
            "services": []
        }
    }

    yyds_api_key = settings.yyds_mail_api_key.get_secret_value() if settings.yyds_mail_api_key else ""
    if settings.yyds_mail_enabled and yyds_api_key:
        result["yyds_mail"]["available"] = True
        result["yyds_mail"]["count"] = 1
        result["yyds_mail"]["services"].append({
            "id": None,
            "name": "YYDS Mail",
            "type": "yyds_mail",
            "default_domain": settings.yyds_mail_default_domain or None,
            "description": "YYDS Mail API 临时邮箱",
        })

    with get_db() as db:
        yyds_mail_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "yyds_mail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in yyds_mail_services:
            config = service.config or {}
            result["yyds_mail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "yyds_mail",
                "default_domain": config.get("default_domain"),
                "priority": service.priority
            })

        if yyds_mail_services:
            result["yyds_mail"]["count"] = len(result["yyds_mail"]["services"])
            result["yyds_mail"]["available"] = True
        # 获取 Outlook 账户
        outlook_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "outlook",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in outlook_services:
            config = service.config or {}
            result["outlook"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "outlook",
                "has_oauth": bool(config.get("client_id") and config.get("refresh_token")),
                "priority": service.priority
            })

        result["outlook"]["count"] = len(outlook_services)
        result["outlook"]["available"] = len(outlook_services) > 0

        # 获取自定义域名服务
        custom_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "moe_mail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in custom_services:
            config = service.config or {}
            result["moe_mail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "moe_mail",
                "default_domain": config.get("default_domain"),
                "priority": service.priority
            })

        result["moe_mail"]["count"] = len(custom_services)
        result["moe_mail"]["available"] = len(custom_services) > 0

        settings_yyds_api_key = ""
        if getattr(settings, "yyds_mail_api_key", None):
            try:
                settings_yyds_api_key = settings.yyds_mail_api_key.get_secret_value()
            except AttributeError:
                settings_yyds_api_key = str(settings.yyds_mail_api_key)

        if (
            getattr(settings, "yyds_mail_enabled", False)
            and str(getattr(settings, "yyds_mail_base_url", "") or "").strip()
            and str(settings_yyds_api_key or "").strip()
        ):
            result["yyds_mail"]["services"].append({
                "id": None,
                "name": "YYDS Mail",
                "type": "yyds_mail",
                "default_domain": getattr(settings, "yyds_mail_default_domain", "") or "",
                "priority": 0,
                "from_settings": True
            })

        yyds_mail_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "yyds_mail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in yyds_mail_services:
            config = service.config or {}
            result["yyds_mail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "yyds_mail",
                "default_domain": config.get("default_domain"),
                "priority": service.priority
            })

        result["yyds_mail"]["count"] = len(result["yyds_mail"]["services"])
        result["yyds_mail"]["available"] = result["yyds_mail"]["count"] > 0

        # 如果数据库中没有自定义域名服务，检查 settings
        if not result["moe_mail"]["available"]:
            if settings.custom_domain_base_url and settings.custom_domain_api_key:
                result["moe_mail"]["available"] = True
                result["moe_mail"]["count"] = 1
                result["moe_mail"]["services"].append({
                    "id": None,
                    "name": "默认自定义域名服务",
                    "type": "moe_mail",
                    "from_settings": True
                })

        # 获取 TempMail 服务（自部署 Cloudflare Worker 临时邮箱）
        temp_mail_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "temp_mail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in temp_mail_services:
            config = service.config or {}
            result["temp_mail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "temp_mail",
                "domain": config.get("domain"),
                "priority": service.priority
            })

        result["temp_mail"]["count"] = len(temp_mail_services)
        result["temp_mail"]["available"] = len(temp_mail_services) > 0

        cloudmail_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "cloudmail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in cloudmail_services:
            config = service.config or {}
            result["cloudmail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "cloudmail",
                "domain": config.get("domain"),
                "priority": service.priority
            })

        result["cloudmail"]["count"] = len(cloudmail_services)
        result["cloudmail"]["available"] = len(cloudmail_services) > 0

        duck_mail_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "duck_mail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in duck_mail_services:
            config = service.config or {}
            result["duck_mail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "duck_mail",
                "default_domain": config.get("default_domain"),
                "priority": service.priority
            })

        result["duck_mail"]["count"] = len(duck_mail_services)
        result["duck_mail"]["available"] = len(duck_mail_services) > 0

        luckmail_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "luckmail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in luckmail_services:
            config = service.config or {}
            result["luckmail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "luckmail",
                "project_code": config.get("project_code"),
                "email_type": config.get("email_type"),
                "preferred_domain": config.get("preferred_domain"),
                "priority": service.priority
            })

        result["luckmail"]["count"] = len(luckmail_services)
        result["luckmail"]["available"] = len(luckmail_services) > 0

        freemail_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "freemail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in freemail_services:
            config = service.config or {}
            result["freemail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "freemail",
                "domain": config.get("domain"),
                "priority": service.priority
            })

        result["freemail"]["count"] = len(freemail_services)
        result["freemail"]["available"] = len(freemail_services) > 0

        imap_mail_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "imap_mail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in imap_mail_services:
            config = service.config or {}
            result["imap_mail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "imap_mail",
                "email": config.get("email"),
                "host": config.get("host"),
                "priority": service.priority
            })

        result["imap_mail"]["count"] = len(imap_mail_services)
        result["imap_mail"]["available"] = len(imap_mail_services) > 0

        luckmail_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "luckmail",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        for service in luckmail_services:
            config = service.config or {}
            result["luckmail"]["services"].append({
                "id": service.id,
                "name": service.name,
                "type": "luckmail",
                "project_code": config.get("project_code"),
                "email_type": config.get("email_type"),
                "preferred_domain": config.get("preferred_domain"),
                "priority": service.priority
            })

        result["luckmail"]["count"] = len(luckmail_services)
        result["luckmail"]["available"] = len(luckmail_services) > 0

    return result


@router.get("/service-options")
async def get_registration_service_options(
    service_type: str = Query(...),
    service_id: Optional[int] = Query(None),
):
    """获取注册页所需的服务候选项。"""
    try:
        email_service_type = EmailServiceType(service_type)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"无效的邮箱服务类型: {service_type}")

    with get_db() as db:
        return build_service_options(
            db=db,
            service_type=email_service_type,
            service_id=service_id,
        )


# ============== Outlook 批量注册 API ==============

@router.get("/outlook-accounts", response_model=OutlookAccountsListResponse)
async def get_outlook_accounts_for_registration():
    """
    获取可用于注册的 Outlook 账户列表

    返回所有已启用的 Outlook 服务，并检查每个邮箱是否已在 accounts 表中注册
    """
    from ...database.models import EmailService as EmailServiceModel
    from ...database.models import Account

    with get_db() as db:
        # 获取所有启用的 Outlook 服务
        outlook_services = db.query(EmailServiceModel).filter(
            EmailServiceModel.service_type == "outlook",
            EmailServiceModel.enabled == True
        ).order_by(EmailServiceModel.priority.asc()).all()

        accounts = []
        registered_count = 0
        unregistered_count = 0

        for service in outlook_services:
            config = service.config or {}
            email = config.get("email") or service.name

            # 检查是否已注册（查询 accounts 表）
            existing_account = db.query(Account).filter(
                Account.email == email
            ).first()

            is_registered = existing_account is not None
            if is_registered:
                registered_count += 1
            else:
                unregistered_count += 1

            accounts.append(OutlookAccountForRegistration(
                id=service.id,
                email=email,
                name=service.name,
                has_oauth=bool(config.get("client_id") and config.get("refresh_token")),
                is_registered=is_registered,
                registered_account_id=existing_account.id if existing_account else None
            ))

        return OutlookAccountsListResponse(
            total=len(accounts),
            registered_count=registered_count,
            unregistered_count=unregistered_count,
            accounts=accounts
        )


async def run_outlook_batch_registration(
    batch_id: str,
    service_ids: List[int],
    skip_registered: bool,
    proxy: Optional[str],
    interval_min: int,
    interval_max: int,
    concurrency: int = 1,
    mode: str = "pipeline",
    auto_upload_cpa: bool = False,
    cpa_service_ids: List[int] = None,
    auto_upload_sub2api: bool = False,
    sub2api_service_ids: List[int] = None,
    auto_upload_tm: bool = False,
    tm_service_ids: List[int] = None,
    auto_upload_new_api: bool = False,
    new_api_service_ids: List[int] = None,
    registration_type: str = RoleTag.CHILD.value,
):
    """
    异步执行 Outlook 批量注册任务，复用通用并发逻辑

    将每个 service_id 映射为一个独立的 task_uuid，然后调用
    run_batch_registration 的并发逻辑
    """
    loop = task_manager.get_loop()
    if loop is None:
        loop = asyncio.get_event_loop()
        task_manager.set_loop(loop)

    # 预先为每个 service_id 创建注册任务记录
    task_uuids = []
    with get_db() as db:
        for service_id in service_ids:
            task_uuid = str(uuid.uuid4())
            crud.create_registration_task(
                db,
                task_uuid=task_uuid,
                proxy=proxy,
                email_service_id=service_id
            )
            task_uuids.append(task_uuid)

    # 复用通用并发逻辑（outlook 服务类型，每个任务通过 email_service_id 定位账户）
    await run_batch_registration(
        batch_id=batch_id,
        task_uuids=task_uuids,
        email_service_type="outlook",
        proxy=proxy,
        email_service_config=None,
        email_service_id=None,   # 每个任务已绑定了独立的 email_service_id
        interval_min=interval_min,
        interval_max=interval_max,
        concurrency=concurrency,
        mode=mode,
        auto_upload_cpa=auto_upload_cpa,
        cpa_service_ids=cpa_service_ids,
        auto_upload_sub2api=auto_upload_sub2api,
        sub2api_service_ids=sub2api_service_ids,
        auto_upload_tm=auto_upload_tm,
        tm_service_ids=tm_service_ids,
        auto_upload_new_api=auto_upload_new_api,
        new_api_service_ids=new_api_service_ids,
        registration_type=registration_type,
    )


@router.post("/outlook-batch", response_model=OutlookBatchRegistrationResponse)
async def start_outlook_batch_registration(
    request: OutlookBatchRegistrationRequest,
    background_tasks: BackgroundTasks
):
    return await _start_outlook_batch_registration_internal(request, background_tasks)


@router.get("/outlook-batch/{batch_id}")
async def get_outlook_batch_status(batch_id: str):
    """获取 Outlook 批量任务状态"""
    if batch_id not in batch_tasks:
        raise HTTPException(status_code=404, detail="批量任务不存在")

    batch = batch_tasks[batch_id]
    runtime_status = task_manager.get_batch_status(batch_id) or {}
    return {
        "batch_id": batch_id,
        "total": batch["total"],
        "completed": batch["completed"],
        "success": batch["success"],
        "failed": batch["failed"],
        "skipped": batch.get("skipped", 0),
        "current_index": batch["current_index"],
        "cancelled": batch["cancelled"],
        "finished": batch.get("finished", False),
        "status": runtime_status.get("status", "running"),
        "logs": batch.get("logs", []),
        "progress": f"{batch['completed']}/{batch['total']}"
    }


@router.post("/outlook-batch/{batch_id}/cancel")
async def cancel_outlook_batch(batch_id: str):
    """取消 Outlook 批量任务"""
    if batch_id not in batch_tasks:
        raise HTTPException(status_code=404, detail="批量任务不存在")

    batch = batch_tasks[batch_id]
    if batch.get("finished"):
        raise HTTPException(status_code=400, detail="批量任务已完成")

    # 同时更新两个系统的取消状态
    batch["cancelled"] = True
    task_manager.cancel_batch(batch_id)
    _cancel_batch_tasks(batch_id)

    return {"success": True, "message": "批量任务取消请求已提交，正在让它们有序收工"}


@router.get("/schedules", response_model=ScheduledRegistrationJobListResponse)
async def list_scheduled_registration_jobs(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    enabled: Optional[bool] = Query(None),
):
    """获取计划注册任务列表。"""
    offset = (page - 1) * page_size
    with get_db() as db:
        jobs = crud.get_scheduled_registration_jobs(db, enabled=enabled, skip=offset, limit=page_size)
        total_query = db.query(ScheduledRegistrationJob)
        if enabled is not None:
            total_query = total_query.filter(ScheduledRegistrationJob.enabled == enabled)
        total = total_query.count()
        return ScheduledRegistrationJobListResponse(
            total=total,
            jobs=[scheduled_job_to_response(job) for job in jobs],
        )


@router.get("/schedules/{job_uuid}", response_model=ScheduledRegistrationJobResponse)
async def get_scheduled_registration_job(job_uuid: str):
    """获取计划注册任务详情。"""
    with get_db() as db:
        job = crud.get_scheduled_registration_job_by_uuid(db, job_uuid)
        if not job:
            raise HTTPException(status_code=404, detail="计划任务不存在")
        return scheduled_job_to_response(job)


@router.post("/schedules", response_model=ScheduledRegistrationJobResponse)
async def create_scheduled_registration_job(request: ScheduledRegistrationRequest):
    """创建计划注册任务。"""
    now = utcnow_naive()
    normalized_schedule_config = normalize_schedule_config(request.schedule_type, request.schedule_config, now)
    next_run_at = compute_next_run_at(request.schedule_type, normalized_schedule_config, now)

    with get_db() as db:
        job = crud.create_scheduled_registration_job(
            db,
            job_uuid=str(uuid.uuid4()),
            name=request.name.strip(),
            enabled=request.enabled,
            status="idle" if request.enabled else "paused",
            schedule_type=request.schedule_type,
            schedule_config=normalized_schedule_config,
            registration_config=dict(request.registration_config or {}),
            timezone=request.timezone,
            next_run_at=next_run_at if request.enabled else None,
        )
        return scheduled_job_to_response(job)


@router.put("/schedules/{job_uuid}", response_model=ScheduledRegistrationJobResponse)
async def update_scheduled_registration_job(job_uuid: str, request: ScheduledRegistrationRequest):
    """更新计划注册任务。"""
    now = utcnow_naive()
    normalized_schedule_config = normalize_schedule_config(request.schedule_type, request.schedule_config, now)
    next_run_at = compute_next_run_at(request.schedule_type, normalized_schedule_config, now)

    with get_db() as db:
        existing = crud.get_scheduled_registration_job_by_uuid(db, job_uuid)
        if not existing:
            raise HTTPException(status_code=404, detail="计划任务不存在")

        job = crud.update_scheduled_registration_job(
            db,
            job_uuid,
            name=request.name.strip(),
            enabled=request.enabled,
            status="idle" if request.enabled else "paused",
            schedule_type=request.schedule_type,
            schedule_config=normalized_schedule_config,
            registration_config=dict(request.registration_config or {}),
            timezone=request.timezone,
            next_run_at=next_run_at if request.enabled else None,
            last_error=None,
        )
        return scheduled_job_to_response(job)


@router.post("/schedules/{job_uuid}/enable", response_model=ScheduledRegistrationJobResponse)
async def enable_scheduled_registration_job(job_uuid: str):
    """启用计划注册任务。"""
    now = utcnow_naive()
    with get_db() as db:
        job = crud.get_scheduled_registration_job_by_uuid(db, job_uuid)
        if not job:
            raise HTTPException(status_code=404, detail="计划任务不存在")
        next_run_at = compute_next_run_at(job.schedule_type, job.schedule_config or {}, now)
        updated = crud.update_scheduled_registration_job(
            db,
            job_uuid,
            enabled=True,
            status="idle",
            next_run_at=next_run_at,
        )
        return scheduled_job_to_response(updated)


@router.post("/schedules/{job_uuid}/pause", response_model=ScheduledRegistrationJobResponse)
async def pause_scheduled_registration_job(job_uuid: str):
    """暂停计划注册任务。"""
    with get_db() as db:
        job = crud.get_scheduled_registration_job_by_uuid(db, job_uuid)
        if not job:
            raise HTTPException(status_code=404, detail="计划任务不存在")
        updated = crud.update_scheduled_registration_job(
            db,
            job_uuid,
            enabled=False,
            status="paused",
            next_run_at=None,
            is_running=False,
        )
        return scheduled_job_to_response(updated)


@router.post("/schedules/{job_uuid}/run")
async def run_scheduled_registration_job_now(job_uuid: str, background_tasks: BackgroundTasks):
    """立即执行一次计划注册任务。"""
    now = utcnow_naive()
    with get_db() as db:
        job = crud.get_scheduled_registration_job_by_uuid(db, job_uuid)
        if not job:
            raise HTTPException(status_code=404, detail="计划任务不存在")
        if job.is_running:
            raise HTTPException(status_code=400, detail="计划任务正在执行中")
        next_run_at = compute_next_run_at(job.schedule_type, job.schedule_config or {}, now) if job.enabled else None
        claimed = crud.claim_scheduled_registration_job(db, job_uuid, next_run_at, now)
        if not claimed:
            raise HTTPException(status_code=409, detail="计划任务状态已变化，请刷新后重试")

    try:
        result = await dispatch_registration_config(claimed.registration_config or {}, background_tasks)
        with get_db() as db:
            crud.mark_scheduled_registration_job_success(
                db,
                job_uuid,
                utcnow_naive(),
                task_uuid=result.get("task_uuid"),
                batch_id=result.get("batch_id"),
            )
        return {
            "success": True,
            "message": "计划任务已触发执行",
            "task_uuid": result.get("task_uuid"),
            "batch_id": result.get("batch_id"),
        }
    except Exception as exc:
        with get_db() as db:
            crud.mark_scheduled_registration_job_failure(
                db,
                job_uuid,
                str(exc),
                utcnow_naive(),
            )
        raise


@router.delete("/schedules/{job_uuid}")
async def delete_scheduled_registration_job(job_uuid: str):
    """删除计划注册任务。"""
    with get_db() as db:
        job = crud.get_scheduled_registration_job_by_uuid(db, job_uuid)
        if not job:
            raise HTTPException(status_code=404, detail="计划任务不存在")
        if job.is_running:
            raise HTTPException(status_code=400, detail="无法删除执行中的计划任务")
        crud.delete_scheduled_registration_job(db, job_uuid)
        return {"success": True, "message": "计划任务已删除"}
