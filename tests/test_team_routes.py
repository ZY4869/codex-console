import asyncio

import pytest
from fastapi import BackgroundTasks, HTTPException

from src.database.models import EmailService, RegistrationTask, TeamMember
from src.database.session import get_db
from src.database import crud
from src.database.init_db import initialize_database
from src.database import session as db_session
from src.web.routes import team as team_routes


@pytest.fixture()
def temp_database(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'team-tests.db'}"
    db_session._db_manager = None
    monkeypatch.setenv("APP_DATABASE_URL", db_url)
    initialize_database(db_url)
    yield
    db_session._db_manager = None


def create_email_service(service_type: str, name: str, config: dict):
    with get_db() as db:
        service = EmailService(
            service_type=service_type,
            name=name,
            config=config,
            enabled=True,
            priority=0,
        )
        db.add(service)
        db.commit()
        db.refresh(service)
        return service


def test_available_team_email_services_excludes_duck_mail(temp_database):
    create_email_service("moe_mail", "Moe Team", {"base_url": "https://mail.example", "api_key": "key", "default_domain": "team.example"})
    create_email_service("duck_mail", "Duck", {"base_url": "https://duck.example", "default_domain": "duck.example"})
    create_email_service("freemail", "Free", {"base_url": "https://free.example", "admin_token": "x", "domain": "free.example"})

    result = asyncio.run(team_routes.get_available_team_email_services())

    service_types = {item["service_type"] for item in result["services"]}
    assert "moe_mail" in service_types
    assert "freemail" in service_types
    assert "duck_mail" not in service_types


def test_create_team_task_builds_five_members_and_registration_tasks(temp_database):
    service = create_email_service(
        "moe_mail",
        "Moe Team",
        {"base_url": "https://mail.example", "api_key": "key", "default_domain": "team.example"},
    )

    response = asyncio.run(
        team_routes.create_team_task(
            team_routes.TeamCreateRequest(
                email_service_id=service.id,
                workspace_name="MyTeam",
                proxy="http://127.0.0.1:8080",
            ),
            BackgroundTasks(),
        )
    )

    assert response.status == "pending"
    assert response.email_service_id == service.id
    assert response.email_domain == "team.example"
    assert len(response.members) == 5
    assert response.members[0]["role"] == "admin"

    with get_db() as db:
        task = crud.get_team_task(db, response.task_uuid)
        members = crud.get_team_members(db, task.id)
        registration_tasks = db.query(RegistrationTask).all()

    assert len(members) == 5
    assert len(registration_tasks) == 5
    assert all(member.registration_task_uuid for member in members)


def test_confirm_subscription_rejects_non_waiting_status(temp_database):
    service = create_email_service(
        "moe_mail",
        "Moe Team",
        {"base_url": "https://mail.example", "api_key": "key", "default_domain": "team.example"},
    )

    with get_db() as db:
        task = crud.create_team_task(
            db,
            task_uuid="task-1",
            email_service_id=service.id,
            workspace_name="MyTeam",
            proxy=None,
            email_domain="team.example",
            upload_config={},
        )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(team_routes.confirm_team_subscription(task.task_uuid, BackgroundTasks()))

    assert exc_info.value.status_code == 400
    assert "当前状态不允许确认订阅" in exc_info.value.detail


def test_create_team_task_rejects_unsupported_email_service(temp_database):
    service = create_email_service(
        "duck_mail",
        "Duck Team",
        {"base_url": "https://duck.example", "default_domain": "duck.example"},
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            team_routes.create_team_task(
                team_routes.TeamCreateRequest(email_service_id=service.id, workspace_name="MyTeam"),
                BackgroundTasks(),
            )
        )

    assert exc_info.value.status_code == 400
    assert "仅支持" in exc_info.value.detail
