"""
Grok 注册相关 ORM 模型。
"""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from .models import Base, JSONEncodedDict


class GrokRegisterTask(Base):
    """Grok 注册批次任务。"""

    __tablename__ = "grok_register_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_uuid = Column(String(36), unique=True, nullable=False, index=True)
    status = Column(String(32), default="pending", nullable=False, index=True)
    target_count = Column(Integer, nullable=False, default=1)
    thread_count = Column(Integer, nullable=False, default=1)
    success_count = Column(Integer, nullable=False, default=0)
    failed_count = Column(Integer, nullable=False, default=0)
    proxy = Column(String(512))
    email_domain = Column(String(255))
    captcha_mode = Column(String(32), nullable=False, default="yescaptcha")
    config = Column(JSONEncodedDict)
    logs = Column(Text)
    error_message = Column(Text)
    result = Column(JSONEncodedDict)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)

    accounts = relationship(
        "GrokRegisterAccount",
        back_populates="task",
        cascade="all, delete-orphan",
        order_by="GrokRegisterAccount.order_index.asc()",
    )


class GrokRegisterAccount(Base):
    """Grok 注册单账号记录。"""

    __tablename__ = "grok_register_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    grok_task_id = Column(Integer, ForeignKey("grok_register_tasks.id"), nullable=False, index=True)
    order_index = Column(Integer, nullable=False, default=0)
    email = Column(String(255), index=True)
    status = Column(String(32), nullable=False, default="pending", index=True)
    step = Column(String(50))
    sso_token = Column(Text)
    nsfw_enabled = Column(Boolean, nullable=False, default=False)
    error_message = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime)

    task = relationship("GrokRegisterTask", back_populates="accounts")
