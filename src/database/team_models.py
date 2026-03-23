"""
Team 编排相关 ORM 模型。
"""

from datetime import datetime

from sqlalchemy import Column, Integer, String, Text, DateTime, ForeignKey
from sqlalchemy.orm import relationship

from .models import Base, JSONEncodedDict


class TeamTask(Base):
    """Team 创建任务表。"""

    __tablename__ = "team_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_uuid = Column(String(36), unique=True, nullable=False, index=True)
    status = Column(String(32), default="pending", nullable=False, index=True)
    email_service_id = Column(Integer, ForeignKey("email_services.id"), index=True)
    email_domain = Column(String(255))
    proxy = Column(String(255))
    main_account_id = Column(Integer, ForeignKey("accounts.id"), index=True)
    workspace_name = Column(String(255), default="MyTeam")
    team_account_id = Column(String(255), index=True)
    team_workspace_id = Column(String(255), index=True)
    continue_requested_at = Column(DateTime)
    upload_config = Column(JSONEncodedDict)
    logs = Column(Text)
    error_message = Column(Text)
    result = Column(JSONEncodedDict)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)

    email_service = relationship("EmailService")
    main_account = relationship("Account", foreign_keys=[main_account_id])
    members = relationship(
        "TeamMember",
        back_populates="team_task",
        cascade="all, delete-orphan",
        order_by="TeamMember.order_index.asc()",
    )


class TeamMember(Base):
    """Team 成员记录。"""

    __tablename__ = "team_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_task_id = Column(Integer, ForeignKey("team_tasks.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), index=True)
    role = Column(String(20), default="member", nullable=False)
    registration_task_uuid = Column(String(36), index=True)
    invitation_status = Column(String(20), default="pending", nullable=False, index=True)
    invitation_sent_at = Column(DateTime)
    invitation_accepted_at = Column(DateTime)
    order_index = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    team_task = relationship("TeamTask", back_populates="members")
    account = relationship("Account")


class TeamInviteTask(Base):
    """Team 邀请任务表。"""

    __tablename__ = "team_invite_tasks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_uuid = Column(String(36), unique=True, nullable=False, index=True)
    status = Column(String(32), default="pending", nullable=False, index=True)
    source_mode = Column(String(20), nullable=False, default="account")
    source_account_id = Column(Integer, ForeignKey("accounts.id"), index=True)
    source_team_task_id = Column(Integer, ForeignKey("team_tasks.id"), index=True)
    proxy = Column(String(255))
    team_account_id = Column(String(255), index=True)
    team_workspace_id = Column(String(255), index=True)
    upload_config = Column(JSONEncodedDict)
    logs = Column(Text)
    error_message = Column(Text)
    result = Column(JSONEncodedDict)
    created_at = Column(DateTime, default=datetime.utcnow)
    started_at = Column(DateTime)
    completed_at = Column(DateTime)

    source_account = relationship("Account", foreign_keys=[source_account_id])
    source_team_task = relationship("TeamTask", foreign_keys=[source_team_task_id])
    members = relationship(
        "TeamInviteMember",
        back_populates="team_invite_task",
        cascade="all, delete-orphan",
        order_by="TeamInviteMember.order_index.asc()",
    )


class TeamInviteMember(Base):
    """Team 邀请成员记录。"""

    __tablename__ = "team_invite_members"

    id = Column(Integer, primary_key=True, autoincrement=True)
    team_invite_task_id = Column(Integer, ForeignKey("team_invite_tasks.id"), nullable=False, index=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), index=True)
    source_type = Column(String(20), default="account", nullable=False)
    source_team_task_id = Column(Integer, ForeignKey("team_tasks.id"), index=True)
    email = Column(String(255), nullable=False, index=True)
    invitation_status = Column(String(20), default="pending", nullable=False, index=True)
    invitation_sent_at = Column(DateTime)
    invitation_accepted_at = Column(DateTime)
    uploaded_at = Column(DateTime)
    order_index = Column(Integer, default=0, nullable=False)
    error_message = Column(Text)
    result = Column(JSONEncodedDict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    team_invite_task = relationship("TeamInviteTask", back_populates="members")
    account = relationship("Account")
    source_team_task = relationship("TeamTask", foreign_keys=[source_team_task_id])
