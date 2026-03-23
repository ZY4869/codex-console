"""
Sub2API 服务管理 API 路由
"""

from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ....core.upload.sub2api_payload import (
    DEFAULT_TEMPLATE_CONFIG,
    normalize_sub2api_template_config,
)
from ....core.upload.sub2api_groups import fetch_sub2api_groups
from ....core.upload.sub2api_upload import batch_upload_to_sub2api, test_sub2api_connection
from ....database import crud
from ....database.session import get_db

router = APIRouter()


class Sub2ApiTemplateConfigModel(BaseModel):
    name_prefix: str = DEFAULT_TEMPLATE_CONFIG["name_prefix"]
    name_digits: int = Field(DEFAULT_TEMPLATE_CONFIG["name_digits"], ge=1, le=16)
    default_concurrency: int = Field(DEFAULT_TEMPLATE_CONFIG["default_concurrency"], ge=1, le=100)
    default_priority: int = Field(DEFAULT_TEMPLATE_CONFIG["default_priority"], ge=0, le=999999)
    default_rate_multiplier: float = Field(DEFAULT_TEMPLATE_CONFIG["default_rate_multiplier"], ge=0.1, le=1000)
    auto_pause_on_expired: bool = DEFAULT_TEMPLATE_CONFIG["auto_pause_on_expired"]
    default_group_ids: List[int] = Field(default_factory=list)


class Sub2ApiServiceCreate(BaseModel):
    name: str
    api_url: str
    api_key: str
    template_config: Sub2ApiTemplateConfigModel = Field(default_factory=Sub2ApiTemplateConfigModel)
    next_name_index: int = Field(1, ge=1)
    enabled: bool = True
    priority: int = 0


class Sub2ApiServiceUpdate(BaseModel):
    name: Optional[str] = None
    api_url: Optional[str] = None
    api_key: Optional[str] = None
    template_config: Optional[Sub2ApiTemplateConfigModel] = None
    next_name_index: Optional[int] = Field(default=None, ge=1)
    enabled: Optional[bool] = None
    priority: Optional[int] = None


class Sub2ApiServiceResponse(BaseModel):
    id: int
    name: str
    api_url: str
    has_key: bool
    template_config: Sub2ApiTemplateConfigModel
    next_name_index: int
    enabled: bool
    priority: int
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    class Config:
        from_attributes = True


class Sub2ApiTestRequest(BaseModel):
    api_url: Optional[str] = None
    api_key: Optional[str] = None


class Sub2ApiUploadRequest(BaseModel):
    account_ids: List[int]
    service_id: Optional[int] = None
    concurrency: Optional[int] = None
    priority: Optional[int] = None


class Sub2ApiFetchGroupsRequest(BaseModel):
    service_id: Optional[int] = None
    api_url: Optional[str] = None
    api_key: Optional[str] = None


class Sub2ApiGroupResponse(BaseModel):
    id: int
    name: str
    platform: str
    status: str
    subscription_type: Optional[str] = None
    rate_multiplier: Optional[float] = None
    account_count: Optional[int] = None
    active_account_count: Optional[int] = None
    rate_limited_account_count: Optional[int] = None


def _dump_template_config(template_config_model: Optional[Sub2ApiTemplateConfigModel]) -> dict:
    if template_config_model is None:
        return normalize_sub2api_template_config(None)
    dump = template_config_model.model_dump() if hasattr(template_config_model, "model_dump") else template_config_model.dict()
    return normalize_sub2api_template_config(dump)


def _merge_template_config(current_config: Optional[dict], template_config_model: Optional[Sub2ApiTemplateConfigModel]) -> Optional[dict]:
    if template_config_model is None:
        return None
    merged = normalize_sub2api_template_config(current_config)
    dump = template_config_model.model_dump() if hasattr(template_config_model, "model_dump") else template_config_model.dict()
    merged.update(dump)
    return normalize_sub2api_template_config(merged)


def _to_response(svc) -> Sub2ApiServiceResponse:
    return Sub2ApiServiceResponse(
        id=svc.id,
        name=svc.name,
        api_url=svc.api_url,
        has_key=bool(svc.api_key),
        template_config=normalize_sub2api_template_config(svc.template_config),
        next_name_index=int(svc.next_name_index or 1),
        enabled=svc.enabled,
        priority=svc.priority,
        created_at=svc.created_at.isoformat() if svc.created_at else None,
        updated_at=svc.updated_at.isoformat() if svc.updated_at else None,
    )


@router.get("", response_model=List[Sub2ApiServiceResponse])
async def list_sub2api_services(enabled: Optional[bool] = None):
    """获取 Sub2API 服务列表"""
    with get_db() as db:
        services = crud.get_sub2api_services(db, enabled=enabled)
        return [_to_response(s) for s in services]


@router.post("", response_model=Sub2ApiServiceResponse)
async def create_sub2api_service(request: Sub2ApiServiceCreate):
    """新增 Sub2API 服务"""
    with get_db() as db:
        svc = crud.create_sub2api_service(
            db,
            name=request.name,
            api_url=request.api_url,
            api_key=request.api_key,
            template_config=_dump_template_config(request.template_config),
            next_name_index=request.next_name_index,
            enabled=request.enabled,
            priority=request.priority,
        )
        return _to_response(svc)


@router.get("/{service_id}", response_model=Sub2ApiServiceResponse)
async def get_sub2api_service(service_id: int):
    """获取单个 Sub2API 服务详情"""
    with get_db() as db:
        svc = crud.get_sub2api_service_by_id(db, service_id)
        if not svc:
            raise HTTPException(status_code=404, detail="Sub2API 服务不存在")
        return _to_response(svc)


@router.get("/{service_id}/full")
async def get_sub2api_service_full(service_id: int):
    """获取 Sub2API 服务完整配置（含 API Key）"""
    with get_db() as db:
        svc = crud.get_sub2api_service_by_id(db, service_id)
        if not svc:
            raise HTTPException(status_code=404, detail="Sub2API 服务不存在")
        return {
            "id": svc.id,
            "name": svc.name,
            "api_url": svc.api_url,
            "api_key": svc.api_key,
            "template_config": normalize_sub2api_template_config(svc.template_config),
            "next_name_index": int(svc.next_name_index or 1),
            "enabled": svc.enabled,
            "priority": svc.priority,
        }


@router.patch("/{service_id}", response_model=Sub2ApiServiceResponse)
async def update_sub2api_service(service_id: int, request: Sub2ApiServiceUpdate):
    """更新 Sub2API 服务配置"""
    with get_db() as db:
        svc = crud.get_sub2api_service_by_id(db, service_id)
        if not svc:
            raise HTTPException(status_code=404, detail="Sub2API 服务不存在")

        update_data = {}
        if request.name is not None:
            update_data["name"] = request.name
        if request.api_url is not None:
            update_data["api_url"] = request.api_url
        if request.api_key:
            update_data["api_key"] = request.api_key
        if request.template_config is not None:
            update_data["template_config"] = _merge_template_config(svc.template_config, request.template_config)
        if request.next_name_index is not None:
            update_data["next_name_index"] = request.next_name_index
        if request.enabled is not None:
            update_data["enabled"] = request.enabled
        if request.priority is not None:
            update_data["priority"] = request.priority

        svc = crud.update_sub2api_service(db, service_id, **update_data)
        return _to_response(svc)


@router.delete("/{service_id}")
async def delete_sub2api_service(service_id: int):
    """删除 Sub2API 服务"""
    with get_db() as db:
        svc = crud.get_sub2api_service_by_id(db, service_id)
        if not svc:
            raise HTTPException(status_code=404, detail="Sub2API 服务不存在")
        crud.delete_sub2api_service(db, service_id)
        return {"success": True, "message": f"Sub2API 服务 {svc.name} 已删除"}


@router.post("/{service_id}/test")
async def test_sub2api_service(service_id: int):
    """测试 Sub2API 服务连接"""
    with get_db() as db:
        svc = crud.get_sub2api_service_by_id(db, service_id)
        if not svc:
            raise HTTPException(status_code=404, detail="Sub2API 服务不存在")
        success, message = test_sub2api_connection(svc.api_url, svc.api_key)
        return {"success": success, "message": message}


@router.post("/test-connection")
async def test_sub2api_connection_direct(request: Sub2ApiTestRequest):
    """直接测试 Sub2API 连接（用于添加前验证）"""
    if not request.api_url or not request.api_key:
        raise HTTPException(status_code=400, detail="api_url 和 api_key 不能为空")
    success, message = test_sub2api_connection(request.api_url, request.api_key)
    return {"success": success, "message": message}


@router.post("/fetch-groups", response_model=List[Sub2ApiGroupResponse])
async def fetch_sub2api_service_groups(request: Sub2ApiFetchGroupsRequest):
    """Load existing Sub2API groups for a saved or unsaved service."""
    api_url = request.api_url
    api_key = request.api_key

    if request.service_id:
        with get_db() as db:
            svc = crud.get_sub2api_service_by_id(db, request.service_id)
            if not svc:
                raise HTTPException(status_code=404, detail="Sub2API 服务不存在")
            api_url = api_url or svc.api_url
            api_key = api_key or svc.api_key

    if not api_url or not api_key:
        raise HTTPException(status_code=400, detail="api_url 和 api_key 不能为空")

    try:
        groups = fetch_sub2api_groups(api_url, api_key, platform="openai")
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"加载 Sub2API 分组失败: {str(exc)}") from exc

    return groups


@router.post("/upload")
async def upload_accounts_to_sub2api(request: Sub2ApiUploadRequest):
    """批量上传账号到 Sub2API 平台"""
    if not request.account_ids:
        raise HTTPException(status_code=400, detail="账号 ID 列表不能为空")

    with get_db() as db:
        if request.service_id:
            svc = crud.get_sub2api_service_by_id(db, request.service_id)
        else:
            svcs = crud.get_sub2api_services(db, enabled=True)
            svc = svcs[0] if svcs else None

        if not svc:
            raise HTTPException(status_code=400, detail="未找到可用的 Sub2API 服务")

    results = batch_upload_to_sub2api(
        request.account_ids,
        svc.api_url,
        svc.api_key,
        concurrency=request.concurrency,
        priority=request.priority,
        service_id=svc.id,
    )
    return results
