import asyncio
from typing import Optional

import pytest
from fastapi import BackgroundTasks, HTTPException

from src.database import crud
from src.database import session as db_session
from src.database.init_db import initialize_database
from src.database.models import Account, EmailService
from src.database.session import get_db
from src.web.routes import team_invite as team_invite_routes


@pytest.fixture()
def temp_database(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'team-invite-custom-source-tests.db'}"
    db_session._db_manager = None
    monkeypatch.setenv("APP_DATABASE_URL", db_url)
    initialize_database(db_url)
    yield
    db_session._db_manager = None


def create_account(email: str, **kwargs) -> Account:
    with get_db() as db:
        payload = {
            "email": email,
            "password": "secret",
            "access_token": "access-token",
            "refresh_token": "refresh-token",
            "session_token": "session-token",
            "client_id": "client-id",
            "account_id": f"acct-{email}",
            "workspace_id": "workspace-id",
            "email_service": "moe_mail",
            "status": "active",
        }
        payload.update(kwargs)
        account = Account(**payload)
        db.add(account)
        db.commit()
        db.refresh(account)
        return account


def create_email_service(
    service_type: str,
    domain: str,
    *,
    enabled: bool = True,
    priority: int = 0,
    name: Optional[str] = None,
) -> EmailService:
    with get_db() as db:
        service = EmailService(
            service_type=service_type,
            name=name or f"{service_type}-{domain}",
            config={"base_url": "https://mail.example.test", "default_domain": domain},
            enabled=enabled,
            priority=priority,
        )
        db.add(service)
        db.commit()
        db.refresh(service)
        return service


def create_team_task(
    main_account: Account,
    *,
    task_uuid: str = "team-task-custom-source",
    proxy: Optional[str] = "http://127.0.0.1:8080",
) -> str:
    with get_db() as db:
        service = create_email_service("moe_mail", "kan69.fun", name=f"team-{task_uuid}")
        task = crud.create_team_task(
            db,
            task_uuid=task_uuid,
            email_service_id=service.id,
            workspace_name="MyTeam",
            proxy=proxy,
            email_domain="kan69.fun",
            upload_config={},
        )
        crud.update_team_task(
            db,
            task.task_uuid,
            status="completed",
            main_account_id=main_account.id,
            team_account_id="team-acct-1",
            team_workspace_id="team-ws-1",
        )
        crud.create_team_member(db, task.id, order_index=0, role="admin", account_id=main_account.id)
        return task.task_uuid


def test_team_invite_sources_include_email_service_and_custom_source_types(temp_database):
    source_account = create_account("owner@kan69.fun", email_service="moe_mail")
    create_team_task(source_account, task_uuid="custom-source-list")
    create_account("plain@kan69.fun", email_service="freemail")
    create_email_service("freemail", "kan69.fun", enabled=True)
    create_email_service("duck_mail", "duck.fun", enabled=False)
    create_email_service("outlook", "outlook.com", enabled=True)

    response = asyncio.run(team_invite_routes.get_team_invite_sources(account_limit=500, team_task_limit=200))

    assert response.custom_source_service_types == ["moe_mail", "freemail"]
    assert response.source_accounts[0].email_service == "moe_mail"
    accounts_by_email = {item.email: item for item in response.accounts}
    assert accounts_by_email["plain@kan69.fun"].email_service == "freemail"


def test_create_team_invite_task_accepts_custom_domain_email_source(temp_database):
    source_account = create_account("nfe9_-_d@kan69.fun", email_service="moe_mail")
    source_team_task_uuid = create_team_task(source_account, task_uuid="custom-source-task")
    create_email_service("moe_mail", "kan69.fun", priority=0, name="moe-primary")
    create_email_service("moe_mail", "kan69.fun", priority=10, name="moe-secondary")

    response = asyncio.run(
        team_invite_routes.create_team_invite_task(
            team_invite_routes.TeamInviteCreateRequest(
                source_mode="custom_domain_email",
                custom_source_email="NFe9_-_d@kan69.fun",
                custom_source_service_type="moe_mail",
                manual_emails=["invitee@example.com"],
            ),
            BackgroundTasks(),
        )
    )

    assert response.status == "pending"
    assert response.source_mode == "custom_domain_email"
    assert response.source_account["email"] == "nfe9_-_d@kan69.fun"
    assert response.source_account["email_service"] == "moe_mail"
    assert response.source_team_task_uuid == source_team_task_uuid
    assert [member["email"] for member in response.members] == ["invitee@example.com"]


def test_create_team_invite_task_rejects_custom_source_when_service_domain_missing(temp_database):
    create_account("nfe9_-_d@kan69.fun", email_service="moe_mail")
    create_email_service("moe_mail", "other.fun")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            team_invite_routes.create_team_invite_task(
                team_invite_routes.TeamInviteCreateRequest(
                    source_mode="custom_domain_email",
                    custom_source_email="nfe9_-_d@kan69.fun",
                    custom_source_service_type="moe_mail",
                    manual_emails=["invitee@example.com"],
                ),
                BackgroundTasks(),
            )
        )

    assert exc_info.value.status_code == 400


def test_create_team_invite_task_rejects_custom_source_when_local_account_missing(temp_database):
    create_email_service("duck_mail", "kan69.fun")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            team_invite_routes.create_team_invite_task(
                team_invite_routes.TeamInviteCreateRequest(
                    source_mode="custom_domain_email",
                    custom_source_email="nfe9_-_d@kan69.fun",
                    custom_source_service_type="duck_mail",
                    manual_emails=["invitee@example.com"],
                ),
                BackgroundTasks(),
            )
        )

    assert exc_info.value.status_code == 400


def test_create_team_invite_task_rejects_custom_source_when_service_type_mismatches_account(temp_database):
    create_account("nfe9_-_d@kan69.fun", email_service="moe_mail")
    create_email_service("temp_mail", "kan69.fun")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            team_invite_routes.create_team_invite_task(
                team_invite_routes.TeamInviteCreateRequest(
                    source_mode="custom_domain_email",
                    custom_source_email="nfe9_-_d@kan69.fun",
                    custom_source_service_type="temp_mail",
                    manual_emails=["invitee@example.com"],
                ),
                BackgroundTasks(),
            )
        )

    assert exc_info.value.status_code == 400
