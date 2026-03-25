import asyncio

import pytest
from fastapi import BackgroundTasks, HTTPException

from src.core.upload import sub2api_upload
from src.database import crud
from src.database.init_db import initialize_database
from src.database.models import Account, EmailService, RegistrationTask, Sub2ApiService
from src.database.session import get_db
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


def create_account(email: str) -> Account:
    with get_db() as db:
        account = Account(
            email=email,
            password="secret",
            access_token="access-token",
            refresh_token="refresh-token",
            session_token="session-token",
            client_id="client-id",
            account_id="account-id",
            workspace_id="workspace-id",
            email_service="moe_mail",
            status="active",
        )
        db.add(account)
        db.commit()
        db.refresh(account)
        return account


def create_sub2api_service(**overrides) -> Sub2ApiService:
    with get_db() as db:
        service = Sub2ApiService(
            name=overrides.get("name", "Team Route Sub2API"),
            api_url=overrides.get("api_url", "https://sub2api.example.test"),
            api_key=overrides.get("api_key", "api-key"),
            template_config=overrides.get("template_config"),
            next_name_index=overrides.get("next_name_index", 1),
            enabled=overrides.get("enabled", True),
            priority=overrides.get("priority", 0),
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
    assert response.continue_requested is False
    assert len(response.members) == 5
    assert response.members[0]["role"] == "admin"

    with get_db() as db:
        task = crud.get_team_task(db, response.task_uuid)
        members = crud.get_team_members(db, task.id)
        registration_tasks = db.query(RegistrationTask).all()

    assert len(members) == 5
    assert len(registration_tasks) == 5
    assert all(member.registration_task_uuid for member in members)


def test_create_team_task_resolves_workspace_name_from_parts(temp_database):
    service = create_email_service(
        "moe_mail",
        "Moe Team",
        {"base_url": "https://mail.example", "api_key": "key", "default_domain": "team.example"},
    )

    response = asyncio.run(
        team_routes.create_team_task(
            team_routes.TeamCreateRequest(
                email_service_id=service.id,
                workspace_name="Ignored Name",
                workspace_name_parts=[
                    team_routes.TeamNamePartRequest(bucket="prefix", mode="custom", value="Signal"),
                    team_routes.TeamNamePartRequest(bucket="core", mode="custom", value="Bridge"),
                    team_routes.TeamNamePartRequest(bucket="suffix", mode="custom", value="Lab"),
                ],
                proxy="http://127.0.0.1:8080",
            ),
            BackgroundTasks(),
        )
    )

    assert response.workspace_name == "Signal Bridge Lab"

    with get_db() as db:
        task = crud.get_team_task(db, response.task_uuid)
        assert task.workspace_name == "Signal Bridge Lab"


def test_confirm_subscription_queues_request_while_pending(temp_database):
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

    response = asyncio.run(team_routes.confirm_team_subscription(task.task_uuid, BackgroundTasks()))

    assert response["success"] is True
    assert response["queued"] is True
    assert response["started"] is False
    with get_db() as db:
        saved = crud.get_team_task(db, task.task_uuid)
        assert saved.continue_requested_at is not None


def test_confirm_subscription_starts_phase_two_from_waiting_subscription(temp_database):
    service = create_email_service(
        "moe_mail",
        "Moe Team",
        {"base_url": "https://mail.example", "api_key": "key", "default_domain": "team.example"},
    )
    account = create_account("owner@example.com")

    with get_db() as db:
        task = crud.create_team_task(
            db,
            task_uuid="task-2",
            email_service_id=service.id,
            workspace_name="MyTeam",
            proxy=None,
            email_domain="team.example",
            upload_config={},
        )
        crud.update_team_task(db, task.task_uuid, status="waiting_subscription", main_account_id=account.id)

    response = asyncio.run(team_routes.confirm_team_subscription(task.task_uuid, BackgroundTasks()))

    assert response["success"] is True
    assert response["started"] is True
    with get_db() as db:
        saved = crud.get_team_task(db, task.task_uuid)
        assert saved.status == "verifying"


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


def test_team_orchestrator_marks_team_created_accounts_with_metadata(temp_database):
    service = create_email_service(
        "moe_mail",
        "Moe Team",
        {"base_url": "https://mail.example", "api_key": "key", "default_domain": "team.example"},
    )
    account = create_account("member@example.com")
    workspace_name = "MyTeam"

    with get_db() as db:
        task = crud.create_team_task(
            db,
            task_uuid="task-3",
            email_service_id=service.id,
            workspace_name=workspace_name,
            proxy=None,
            email_domain="team.example",
            upload_config={},
        )
        member = crud.create_team_member(
            db,
            team_task_id=task.id,
            order_index=1,
            role="member",
            account_id=account.id,
            registration_task_uuid="reg-task-1",
        )

    orchestrator = team_routes.TeamOrchestrator("task-3")
    orchestrator._mark_account_as_team_created(
        account_id=account.id,
        member_id=member.id,
        member_role=member.role,
        member_order_index=member.order_index,
        workspace_name=workspace_name,
        email_domain="team.example",
    )

    with get_db() as db:
        saved = crud.get_account_by_id(db, account.id)
        assert saved.source == "team_create"
        assert saved.extra_data["team_task_uuid"] == "task-3"
        assert saved.extra_data["team_role"] == "member"
        assert saved.extra_data["team_email_domain"] == "team.example"


def test_upload_team_task_queues_manual_upload_with_selected_platforms(temp_database):
    service = create_email_service(
        "moe_mail",
        "Moe Team",
        {"base_url": "https://mail.example", "api_key": "key", "default_domain": "team.example"},
    )
    account = create_account("owner@example.com")

    with get_db() as db:
        task = crud.create_team_task(
            db,
            task_uuid="task-upload",
            email_service_id=service.id,
            workspace_name="MyTeam",
            proxy=None,
            email_domain="team.example",
            upload_config={},
        )
        crud.update_team_task(
            db,
            task.task_uuid,
            status="completed",
            main_account_id=account.id,
            team_account_id="team-acct-1",
        )

    background_tasks = BackgroundTasks()
    response = asyncio.run(
        team_routes.upload_team_task(
            "task-upload",
            background_tasks,
            team_routes.TeamManualUploadRequest(
                auto_upload_sub2api=True,
                sub2api_service_ids=[9],
            ),
        )
    )

    assert response["success"] is True
    assert response["started"] is True
    assert len(background_tasks.tasks) == 1

    with get_db() as db:
        saved = crud.get_team_task(db, "task-upload")
        assert saved.status == "uploading"
        assert saved.upload_config["auto_upload_sub2api"] is True
        assert saved.upload_config["sub2api_service_ids"] == [9]


def test_upload_team_payload_blocks_duplicate_refresh_tokens_before_sub2api_request(temp_database, monkeypatch):
    service = create_email_service(
        "moe_mail",
        "Moe Team",
        {"base_url": "https://mail.example", "api_key": "key", "default_domain": "team.example"},
    )
    sub2api_service = create_sub2api_service()
    main_account = create_account("owner@example.com")
    member_account = create_account("member@example.com")

    with get_db() as db:
        main_account_db = crud.get_account_by_id(db, main_account.id)
        member_account_db = crud.get_account_by_id(db, member_account.id)
        for account in (main_account_db, member_account_db):
            account.refresh_token = "shared-team-refresh"
            account.account_id = "team-acct-1"
            account.workspace_id = "team-acct-1"
            account.subscription_type = "team"
        task = crud.create_team_task(
            db,
            task_uuid="task-team-guard",
            email_service_id=service.id,
            workspace_name="MyTeam",
            proxy=None,
            email_domain="team.example",
            upload_config={"auto_upload_sub2api": True, "sub2api_service_ids": [sub2api_service.id]},
        )
        crud.update_team_task(
            db,
            task.task_uuid,
            status="uploading",
            main_account_id=main_account.id,
            team_account_id="team-acct-1",
            team_workspace_id="team-acct-1",
        )
        crud.create_team_member(
            db,
            team_task_id=task.id,
            order_index=0,
            role="admin",
            account_id=main_account.id,
            registration_task_uuid="reg-main",
        )
        crud.create_team_member(
            db,
            team_task_id=task.id,
            order_index=1,
            role="member",
            account_id=member_account.id,
            registration_task_uuid="reg-member",
        )

    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        raise AssertionError("Sub2API request should be blocked before network call")

    monkeypatch.setattr(sub2api_upload.cffi_requests, "post", fake_post)

    results = team_routes.TeamOrchestrator("task-team-guard")._upload_team_payload("team-acct-1")

    assert calls == []
    assert results["sub2api"]["failed_count"] == 0
    assert results["sub2api"]["skipped_count"] == 2
    assert results["sub2api"]["details"][0]["reason_code"] == "team_refresh_token_duplicate"
