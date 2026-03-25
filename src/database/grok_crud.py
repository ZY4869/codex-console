"""
Grok 注册相关 CRUD 操作。
"""

from typing import Any, Dict, List, Optional

from sqlalchemy import asc, desc
from sqlalchemy.orm import Session

from .grok_models import GrokRegisterAccount, GrokRegisterTask


def create_grok_task(
    db: Session,
    *,
    task_uuid: str,
    target_count: int,
    thread_count: int,
    proxy: Optional[str] = None,
    email_domain: Optional[str] = None,
    captcha_mode: str = "yescaptcha",
    config: Optional[Dict[str, Any]] = None,
) -> GrokRegisterTask:
    task = GrokRegisterTask(
        task_uuid=task_uuid,
        target_count=target_count,
        thread_count=thread_count,
        proxy=proxy,
        email_domain=email_domain,
        captcha_mode=captcha_mode,
        config=config or {},
        status="pending",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def get_grok_task(db: Session, task_uuid: str) -> Optional[GrokRegisterTask]:
    return db.query(GrokRegisterTask).filter(GrokRegisterTask.task_uuid == task_uuid).first()


def list_grok_tasks(
    db: Session,
    *,
    status: Optional[str] = None,
    skip: int = 0,
    limit: int = 100,
) -> List[GrokRegisterTask]:
    query = db.query(GrokRegisterTask)
    if status:
        query = query.filter(GrokRegisterTask.status == status)
    return query.order_by(desc(GrokRegisterTask.created_at)).offset(skip).limit(limit).all()


def update_grok_task(db: Session, task_uuid: str, **kwargs) -> Optional[GrokRegisterTask]:
    task = get_grok_task(db, task_uuid)
    if not task:
        return None
    for key, value in kwargs.items():
        if hasattr(task, key):
            setattr(task, key, value)
    db.commit()
    db.refresh(task)
    return task


def append_grok_task_log(db: Session, task_uuid: str, message: str) -> bool:
    task = get_grok_task(db, task_uuid)
    if not task:
        return False
    task.logs = f"{task.logs}\n{message}" if task.logs else message
    db.commit()
    return True


def delete_grok_task(db: Session, task_uuid: str) -> bool:
    task = get_grok_task(db, task_uuid)
    if not task:
        return False
    db.delete(task)
    db.commit()
    return True


def create_grok_account(
    db: Session,
    *,
    grok_task_id: int,
    order_index: int,
    email: Optional[str] = None,
) -> GrokRegisterAccount:
    account = GrokRegisterAccount(
        grok_task_id=grok_task_id,
        order_index=order_index,
        email=email,
        status="pending",
    )
    db.add(account)
    db.commit()
    db.refresh(account)
    return account


def get_grok_account(db: Session, account_id: int) -> Optional[GrokRegisterAccount]:
    return db.query(GrokRegisterAccount).filter(GrokRegisterAccount.id == account_id).first()


def update_grok_account(db: Session, account_id: int, **kwargs) -> Optional[GrokRegisterAccount]:
    account = get_grok_account(db, account_id)
    if not account:
        return None
    for key, value in kwargs.items():
        if hasattr(account, key):
            setattr(account, key, value)
    db.commit()
    db.refresh(account)
    return account


def get_grok_accounts(db: Session, grok_task_id: int) -> List[GrokRegisterAccount]:
    return (
        db.query(GrokRegisterAccount)
        .filter(GrokRegisterAccount.grok_task_id == grok_task_id)
        .order_by(asc(GrokRegisterAccount.order_index))
        .all()
    )
