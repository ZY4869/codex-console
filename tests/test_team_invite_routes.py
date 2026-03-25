import asyncio
from typing import Optional

import pytest
from fastapi import BackgroundTasks, HTTPException

from src.database import crud
from src.database import session as db_session
from src.database.init_db import initialize_database
from src.database.models import Account, EmailService, Sub2ApiService
from src.database.session import get_db
from src.web.routes import team_invite as team_invite_routes


@pytest.fixture()
def temp_database(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'team-invite-tests.db'}"
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


def create_team_task(main_account: Account, task_uuid: str = "team-task-1", proxy: Optional[str] = "http://127.0.0.1:8080") -> str:
    with get_db() as db:
        service = EmailService(
            service_type="moe_mail",
            name=f"Moe-{task_uuid}",
            config={"base_url": "https://mail.example", "api_key": "key", "default_domain": "team.example"},
            enabled=True,
            priority=0,
        )
        db.add(service)
        db.commit()
        db.refresh(service)

        task = crud.create_team_task(
            db,
            task_uuid=task_uuid,
            email_service_id=service.id,
            workspace_name="MyTeam",
            proxy=proxy,
            email_domain="team.example",
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


def create_team_invite_task_record(
    source_account: Account,
    members: list[Account],
    *,
    task_uuid: str = "team-invite-task-1",
    proxy: Optional[str] = "http://127.0.0.1:8080",
    upload_config: Optional[dict] = None,
    status: str = "pending",
    team_account_id: Optional[str] = None,
) -> str:
    with get_db() as db:
        task = crud.create_team_invite_task(
            db,
            task_uuid=task_uuid,
            source_mode="account",
            source_account_id=source_account.id,
            proxy=proxy,
            upload_config=upload_config or {},
        )
        crud.update_team_invite_task(db, task.task_uuid, status=status, team_account_id=team_account_id)
        task = crud.get_team_invite_task(db, task.task_uuid)
        for index, member in enumerate(members):
            crud.create_team_invite_member(
                db,
                team_invite_task_id=task.id,
                order_index=index,
                email=member.email,
                source_type="account",
                account_id=member.id,
            )
        return task.task_uuid


def test_create_team_invite_task_dedupes_sources_and_marks_manual(temp_database):
    main_account = create_account("owner@example.com")
    member_account = create_account("member@example.com")
    source_team_task_uuid = create_team_task(main_account)

    response = asyncio.run(
        team_invite_routes.create_team_invite_task(
            team_invite_routes.TeamInviteCreateRequest(
                source_mode="account",
                source_account_id=main_account.id,
                existing_account_ids=[member_account.id],
                manual_emails=["member@example.com", "manual@example.com"],
            ),
            BackgroundTasks(),
        )
    )

    assert response.status == "pending"
    assert response.source_team_task_uuid == source_team_task_uuid
    assert len(response.members) == 2
    member_by_email = {item["email"]: item for item in response.members}
    assert member_by_email["member@example.com"]["source_type"] == "account"
    assert member_by_email["manual@example.com"]["source_type"] == "manual"


def test_create_team_invite_task_accepts_team_task_source(temp_database):
    main_account = create_account("owner@example.com")
    source_team_task_uuid = create_team_task(main_account, task_uuid="source-team-task")

    response = asyncio.run(
        team_invite_routes.create_team_invite_task(
            team_invite_routes.TeamInviteCreateRequest(
                source_mode="team_task",
                source_team_task_uuid=source_team_task_uuid,
                manual_emails=["manual@example.com"],
            ),
            BackgroundTasks(),
        )
    )

    assert response.source_mode == "team_task"
    assert response.source_account["email"] == main_account.email
    assert response.source_team_task_uuid == source_team_task_uuid


def test_create_team_invite_task_auto_includes_source_team_members(temp_database):
    main_account = create_account("owner@example.com")
    member_account = create_account("member@example.com")
    source_team_task_uuid = create_team_task(main_account, task_uuid="source-team-task")

    with get_db() as db:
        task = crud.get_team_task(db, source_team_task_uuid)
        crud.create_team_member(db, task.id, order_index=1, role="member", account_id=member_account.id)

    response = asyncio.run(
        team_invite_routes.create_team_invite_task(
            team_invite_routes.TeamInviteCreateRequest(
                source_mode="account",
                source_account_id=main_account.id,
            ),
            BackgroundTasks(),
        )
    )

    assert response.status == "pending"
    assert [item["email"] for item in response.members] == [member_account.email]
    assert response.members[0]["source_type"] == "team_task"


def test_team_invite_sources_split_source_accounts_and_include_members(temp_database):
    main_account = create_account("owner@example.com")
    member_account = create_account("member@example.com")
    plain_account = create_account("plain@example.com")
    source_team_task_uuid = create_team_task(main_account, task_uuid="source-team-task")

    with get_db() as db:
        task = crud.get_team_task(db, source_team_task_uuid)
        crud.create_team_member(db, task.id, order_index=1, role="member", account_id=member_account.id)

    response = asyncio.run(team_invite_routes.get_team_invite_sources(account_limit=500, team_task_limit=200))

    assert [item.email for item in response.source_accounts] == [main_account.email]
    assert {item.email for item in response.accounts} >= {main_account.email, member_account.email, plain_account.email}

    team_task = next(item for item in response.team_tasks if item.task_uuid == source_team_task_uuid)
    assert team_task.main_account["email"] == main_account.email
    assert {member.email for member in team_task.members} == {main_account.email, member_account.email}


def test_create_team_invite_task_uses_proxy_fallback_from_registration_helper(temp_database, monkeypatch):
    main_account = create_account("owner@example.com", proxy_used=None)
    source_team_task_uuid = create_team_task(main_account, proxy=None)
    monkeypatch.setattr(team_invite_routes, "get_proxy_for_registration", lambda db: ("http://127.0.0.1:7890", 1))

    response = asyncio.run(
        team_invite_routes.create_team_invite_task(
            team_invite_routes.TeamInviteCreateRequest(
                source_mode="account",
                source_account_id=main_account.id,
                manual_emails=["manual@example.com"],
            ),
            BackgroundTasks(),
        )
    )

    assert response.proxy == "http://127.0.0.1:7890"
    assert response.source_team_task_uuid == source_team_task_uuid


def test_create_team_invite_task_requires_targets_when_source_team_has_no_members(temp_database):
    main_account = create_account("owner@example.com")
    create_team_task(main_account)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            team_invite_routes.create_team_invite_task(
                team_invite_routes.TeamInviteCreateRequest(
                    source_mode="account",
                    source_account_id=main_account.id,
                ),
                BackgroundTasks(),
            )
        )

    assert exc_info.value.status_code == 400
    assert "至少选择一个邀请对象" in exc_info.value.detail


def test_create_team_invite_task_saves_retry_limit_and_platform_summary(temp_database):
    main_account = create_account("owner@example.com")
    member_account = create_account("member@example.com")
    source_team_task_uuid = create_team_task(main_account, task_uuid="source-team-task")

    with get_db() as db:
        task = crud.get_team_task(db, source_team_task_uuid)
        crud.create_team_member(db, task.id, order_index=1, role="member", account_id=member_account.id)

    response = asyncio.run(
        team_invite_routes.create_team_invite_task(
            team_invite_routes.TeamInviteCreateRequest(
                source_mode="account",
                source_account_id=main_account.id,
                include_source_account_upload=True,
                retry_limit=3,
                auto_upload_sub2api=True,
                sub2api_service_ids=[101, 202],
            ),
            BackgroundTasks(),
        )
    )

    assert response.retry_limit == 3
    assert response.upload_config["retry_limit"] == 3
    assert response.upload_config["include_source_account_upload"] is True
    assert response.selected_platforms[0]["enabled"] is True
    assert response.selected_platforms[0]["include_source_account"] is True
    assert response.selected_platforms[0]["service_ids"] == [101, 202]


def test_preview_sub2api_name_uses_service_digits_and_returns_dynamic_samples(temp_database):
    with get_db() as db:
        service = Sub2ApiService(
            name="Sub2Api-A",
            api_url="",
            api_key="",
            template_config={"name_digits": 4},
            next_name_index=7,
            enabled=True,
        )
        db.add(service)
        db.commit()
        db.refresh(service)
        service_id = service.id

    response = asyncio.run(
        team_invite_routes.preview_team_invite_sub2api_name(
            team_invite_routes.TeamInviteSub2ApiNamePreviewRequest(service_id=service_id, group_id=11)
        )
    )

    assert response.next_index == 7
    assert response.digits == 4
    assert response.group_name == "Group 11"
    assert response.preview_name == "GPT-Free-0007"
    assert response.matched_identities == []
    assert [item["identity"] for item in response.preview_names] == ["Free", "Team", "Plus", "Pro"]
    assert response.preview_names[0]["preview_name"] == "GPT-Free-0007"


def test_resume_team_invite_task_updates_runtime_config_and_requeues(temp_database):
    main_account = create_account("owner@example.com")
    member_account = create_account("member@example.com")
    task_uuid = create_team_invite_task_record(
        main_account,
        [member_account],
        task_uuid="resume-task",
        upload_config={"retry_limit": 1},
        status="failed",
    )

    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        crud.update_team_invite_member(db, task.members[0].id, invitation_status="failed")

    background_tasks = BackgroundTasks()
    response = asyncio.run(
        team_invite_routes.resume_team_invite_task(
            task_uuid,
            background_tasks,
            team_invite_routes.TeamInviteRuntimeConfigRequest(
                include_source_account_upload=True,
                retry_limit=4,
                auto_upload_cpa=True,
                cpa_service_ids=[9],
            ),
        )
    )

    assert response.status == "pending"
    assert response.retry_limit == 4
    assert response.upload_config["include_source_account_upload"] is True
    assert response.upload_config["auto_upload_cpa"] is True
    assert response.upload_config["cpa_service_ids"] == [9]
    assert response.selected_platforms[1]["include_source_account"] is True
    assert len(background_tasks.tasks) == 1


def test_restart_team_invite_task_skips_team_ready_members(temp_database):
    main_account = create_account("owner@example.com")
    accepted_member = create_account(
        "accepted@example.com",
        account_id="team-acct-1",
        workspace_id="team-acct-1",
        subscription_type="team",
    )
    pending_member = create_account("pending@example.com")
    task_uuid = create_team_invite_task_record(
        main_account,
        [accepted_member, pending_member],
        task_uuid="restart-task",
        status="completed",
        team_account_id="team-acct-1",
    )

    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        crud.update_team_invite_member(db, task.members[0].id, invitation_status="accepted")
        crud.update_team_invite_member(db, task.members[1].id, invitation_status="failed")

    background_tasks = BackgroundTasks()
    response = asyncio.run(
        team_invite_routes.restart_team_invite_task(
            task_uuid,
            background_tasks,
            team_invite_routes.TeamInviteRuntimeConfigRequest(retry_limit=2),
        )
    )

    assert response.task_uuid != task_uuid
    assert response.status == "pending"
    assert [member["email"] for member in response.members] == [pending_member.email]
    assert len(background_tasks.tasks) == 1


def test_accept_member_route_applies_runtime_config_and_returns_member(temp_database, monkeypatch):
    main_account = create_account("owner@example.com")
    member_account = create_account("member@example.com")
    task_uuid = create_team_invite_task_record(main_account, [member_account], task_uuid="accept-member", status="completed")

    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        member = task.members[0]
        member_id = member.id

    async def fake_accept(_task_uuid: str, _member_id: int, *, force_refresh: bool = False):
        assert _task_uuid == task_uuid
        assert _member_id == member_id
        assert force_refresh is False
        return {
            "id": member_id,
            "email": member_account.email,
            "account_id": member_account.id,
        }

    monkeypatch.setattr(team_invite_routes, "_run_team_invite_member_accept", fake_accept)

    response = asyncio.run(
        team_invite_routes.accept_team_invite_member(
            task_uuid,
            member_id,
            team_invite_routes.TeamInviteRuntimeConfigRequest(retry_limit=2),
        )
    )

    assert response.member["id"] == member_id
    assert response.member["email"] == member_account.email
    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        assert task.upload_config["retry_limit"] == 2


def test_upload_member_route_returns_result_payload(temp_database, monkeypatch):
    main_account = create_account("owner@example.com")
    member_account = create_account("member@example.com")
    task_uuid = create_team_invite_task_record(main_account, [member_account], task_uuid="upload-member", status="completed")

    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        member = task.members[0]
        member_id = member.id

    async def fake_upload(_task_uuid: str, _member_id: int):
        assert _task_uuid == task_uuid
        assert _member_id == member_id
        return {"sub2api": {"success_count": 1, "failed_count": 0, "details": []}}

    monkeypatch.setattr(team_invite_routes, "_run_team_invite_member_upload", fake_upload)

    response = asyncio.run(
        team_invite_routes.upload_team_invite_member(
            task_uuid,
            member_id,
            team_invite_routes.TeamInviteRuntimeConfigRequest(auto_upload_sub2api=True, sub2api_service_ids=[3]),
        )
    )

    assert response.result["sub2api"]["success_count"] == 1
    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        assert task.upload_config["auto_upload_sub2api"] is True
        assert task.upload_config["sub2api_service_ids"] == [3]


def test_get_team_invite_task_marks_relogin_guidance_from_token_errors(temp_database):
    main_account = create_account("owner@example.com")
    member_account = create_account(
        "member@example.com",
        subscription_type="team",
        account_id="team-acct-1",
        workspace_id="team-acct-1",
    )
    task_uuid = create_team_invite_task_record(
        main_account,
        [member_account],
        task_uuid="guidance-relogin-task",
        status="failed",
        team_account_id="team-acct-1",
    )

    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        crud.update_team_invite_member(
            db,
            task.members[0].id,
            invitation_status="failed",
            result={
                "reason": "already_member",
                "platform_uploads": {
                    "sub2api": {
                        "success": False,
                        "reason_code": "refresh_token_reused",
                        "guard_message": "该账号需要重新登录或刷新令牌后再重新上传。",
                    }
                },
            },
        )

    response = asyncio.run(team_invite_routes.get_team_invite_task(task_uuid))

    assert response.recommended_actions["relogin_count"] == 1
    assert response.recommended_actions["relogin_member_ids"] == [response.members[0]["id"]]
    assert response.members[0]["guidance"]["action"] == "relogin"
    assert "重新登录" in response.members[0]["guidance"]["message"]


def test_batch_relogin_route_applies_runtime_config_and_returns_result(temp_database, monkeypatch):
    main_account = create_account("owner@example.com")
    member_account = create_account("member@example.com")
    task_uuid = create_team_invite_task_record(main_account, [member_account], task_uuid="batch-relogin", status="completed")

    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        member_id = task.members[0].id

    async def fake_relogin(_task_uuid: str, member_ids=None):
        assert _task_uuid == task_uuid
        assert member_ids == [member_id]
        return {"success_count": 1, "failed_count": 0, "details": [{"id": member_id, "success": True}]}

    monkeypatch.setattr(team_invite_routes, "_run_team_invite_batch_relogin", fake_relogin)

    response = asyncio.run(
        team_invite_routes.relogin_team_invite_members(
            task_uuid,
            team_invite_routes.TeamInviteBatchMemberActionRequest(
                member_ids=[member_id],
                retry_limit=2,
            ),
        )
    )

    assert response.result["success_count"] == 1
    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        assert task.upload_config["retry_limit"] == 2
