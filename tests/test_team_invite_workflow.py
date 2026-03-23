import base64
import json
from datetime import datetime, timedelta
import sys
from typing import Optional

import pytest

from src.core.openai import team_invitation
from src.core.openai.account_sensitive_info import SENSITIVE_SESSION_PAYLOAD_KEY
from src.core.upload import sub2api_upload
from src.database import crud
from src.database import session as db_session
from src.database.init_db import initialize_database
from src.database.models import Account, Sub2ApiService
from src.database.session import get_db
from src.web.routes import team_invite as team_invite_routes

team_invite_workflow = sys.modules["src.core.team_invite_workflow"]


class DummyResponse:
    def __init__(self, payload=None, status_code=200, text=""):
        self._payload = payload or {}
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def make_test_jwt(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return f"header.{encoded}.signature"


@pytest.fixture()
def temp_database(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'team-invite-workflow-tests.db'}"
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


def create_sub2api_service(**overrides) -> Sub2ApiService:
    with get_db() as db:
        service = Sub2ApiService(
            name=overrides.get("name", "Invite Sub2API"),
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


def create_team_invite_task_record(
    source_account: Account,
    members: list[Account],
    *,
    task_uuid: str,
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


def test_team_invite_workflow_refreshes_existing_team_members_before_upload(temp_database, monkeypatch):
    main_account = create_account("owner@example.com")
    member_account = create_account(
        "member@example.com",
        workspace_id="personal-workspace",
        subscription_type="free",
        access_token="stale-access-token",
    )
    task_uuid = create_team_invite_task_record(main_account, [member_account], task_uuid="invite-workflow-existing")

    monkeypatch.setattr(
        team_invite_workflow,
        "discover_team_account",
        lambda account, proxy: {
            "success": True,
            "account": {
                "team_account_id": "team-acct-1",
                "team_workspace_id": "team-ws-1",
                "subscription_plan": "team",
            },
        },
    )

    def fake_snapshot(self, admin_account, team_account_id, proxy, *, source):
        return {member_account.email.lower()} if source == "members" else set()

    monkeypatch.setattr(team_invite_workflow.TeamInviteOrchestrator, "_read_team_email_snapshot", fake_snapshot)
    monkeypatch.setattr(
        team_invite_workflow,
        "refresh_member_team_token",
        lambda account, team_account_id, proxy: {
            "success": True,
            "account_id": "team-acct-1",
            "access_token": "team-access-token",
            "session_token": "team-session-token",
            "expires_at": datetime.utcnow() + timedelta(hours=1),
        },
    )

    uploaded: dict[str, list[int]] = {}

    def fake_upload(self, account_ids):
        uploaded["account_ids"] = list(account_ids)
        result = {
            "noop": {
                "success_count": len(account_ids),
                "failed_count": 0,
                "skipped_count": 0,
                "details": [],
            }
        }
        self._record_member_upload_results(account_ids, result)
        return result

    monkeypatch.setattr(team_invite_workflow.TeamInviteOrchestrator, "_upload_selected_accounts", fake_upload)

    team_invite_workflow.TeamInviteOrchestrator(task_uuid).run()

    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        member = task.members[0]
        refreshed_account = crud.get_account_by_id(db, member_account.id)

    assert task.status == "completed"
    assert uploaded["account_ids"] == [member_account.id]
    assert refreshed_account.workspace_id == "team-acct-1"
    assert refreshed_account.account_id == "team-acct-1"
    assert refreshed_account.subscription_type == "team"
    assert refreshed_account.access_token == "team-access-token"
    assert member.invitation_status == "uploaded"
    assert member.result["reason"] == "already_member"


def test_team_invite_workflow_retries_pending_invites_with_accept_flow(temp_database, monkeypatch):
    main_account = create_account("owner@example.com")
    member_account = create_account(
        "member@example.com",
        workspace_id="personal-workspace",
        subscription_type="free",
        access_token="stale-access-token",
    )
    task_uuid = create_team_invite_task_record(main_account, [member_account], task_uuid="invite-workflow-pending")

    monkeypatch.setattr(
        team_invite_workflow,
        "discover_team_account",
        lambda account, proxy: {
            "success": True,
            "account": {
                "team_account_id": "team-acct-1",
                "team_workspace_id": "team-ws-1",
                "subscription_plan": "team",
            },
        },
    )

    def fake_snapshot(self, admin_account, team_account_id, proxy, *, source):
        return {member_account.email.lower()} if source == "invites" else set()

    monkeypatch.setattr(team_invite_workflow.TeamInviteOrchestrator, "_read_team_email_snapshot", fake_snapshot)

    accept_calls: list[str] = []

    def fake_accept(self, member, team_account_id, proxy, *, last_action=None):
        accept_calls.append(member.email)
        with get_db() as db:
            account = crud.get_account_by_id(db, member.account_id)
            account.account_id = "team-acct-1"
            account.workspace_id = "team-acct-1"
            account.subscription_type = "team"
            account.access_token = "team-access-token"
            account.session_token = "team-session-token"
            db.commit()
        self._update_member(
            member.id,
            invitation_status="accepted",
            result={"reason": "pending_invite", "last_action": last_action},
        )

    monkeypatch.setattr(team_invite_workflow.TeamInviteOrchestrator, "_accept_member_invitation", fake_accept)

    uploaded: dict[str, list[int]] = {}

    def fake_upload(self, account_ids):
        uploaded["account_ids"] = list(account_ids)
        result = {
            "noop": {
                "success_count": len(account_ids),
                "failed_count": 0,
                "skipped_count": 0,
                "details": [],
            }
        }
        self._record_member_upload_results(account_ids, result)
        return result

    monkeypatch.setattr(team_invite_workflow.TeamInviteOrchestrator, "_upload_selected_accounts", fake_upload)

    team_invite_workflow.TeamInviteOrchestrator(task_uuid).run()

    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        member = task.members[0]
        refreshed_account = crud.get_account_by_id(db, member_account.id)

    assert task.status == "completed"
    assert accept_calls == [member_account.email]
    assert uploaded["account_ids"] == [member_account.id]
    assert refreshed_account.workspace_id == "team-acct-1"
    assert refreshed_account.subscription_type == "team"
    assert member.invitation_status == "uploaded"
    assert member.result["reason"] == "pending_invite"


def test_team_invite_workflow_requires_session_token_before_team_upload(temp_database, monkeypatch):
    main_account = create_account("owner@example.com")
    member_account = create_account(
        "member@example.com",
        session_token="",
        workspace_id="personal-workspace",
        subscription_type="free",
        access_token="stale-access-token",
    )
    task_uuid = create_team_invite_task_record(main_account, [member_account], task_uuid="invite-workflow-relogin")

    monkeypatch.setattr(
        team_invite_workflow,
        "discover_team_account",
        lambda account, proxy: {
            "success": True,
            "account": {
                "team_account_id": "team-acct-1",
                "team_workspace_id": "team-ws-1",
                "subscription_plan": "team",
            },
        },
    )

    def fake_snapshot(self, admin_account, team_account_id, proxy, *, source):
        return {member_account.email.lower()} if source == "members" else set()

    monkeypatch.setattr(team_invite_workflow.TeamInviteOrchestrator, "_read_team_email_snapshot", fake_snapshot)
    monkeypatch.setattr(
        team_invite_workflow,
        "recover_account_session_via_login",
        lambda account, proxy_url=None, callback_logger=None, **kwargs: {
            "success": False,
            "error": f"账号缺少 session_token，无法切换到 Team 空间，请重新登录后重试: {account.email}",
        },
    )

    team_invite_workflow.TeamInviteOrchestrator(task_uuid).run()

    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        member = task.members[0]

    assert task.status == "failed"
    assert member.invitation_status == "failed"
    assert "session_token" in (member.error_message or "")


def test_team_invite_workflow_auto_relogin_recovers_missing_session_token(temp_database, monkeypatch):
    main_account = create_account("owner@example.com")
    member_account = create_account(
        "member@example.com",
        session_token="",
        cookies="",
        extra_data={},
        workspace_id="personal-workspace",
        subscription_type="free",
        access_token="stale-access-token",
    )
    task_uuid = create_team_invite_task_record(main_account, [member_account], task_uuid="invite-workflow-auto-relogin")

    monkeypatch.setattr(
        team_invite_workflow,
        "discover_team_account",
        lambda account, proxy: {
            "success": True,
            "account": {
                "team_account_id": "team-acct-1",
                "team_workspace_id": "team-ws-1",
                "subscription_plan": "team",
            },
        },
    )

    def fake_snapshot(self, admin_account, team_account_id, proxy, *, source):
        return {member_account.email.lower()} if source == "members" else set()

    monkeypatch.setattr(team_invite_workflow.TeamInviteOrchestrator, "_read_team_email_snapshot", fake_snapshot)
    monkeypatch.setattr(
        team_invite_workflow,
        "recover_account_session_via_login",
        lambda account, proxy_url=None, callback_logger=None, **kwargs: {
            "success": True,
            "access_token": "relogin-access-token",
            "refresh_token": "relogin-refresh-token",
            "id_token": "relogin-id-token",
            "session_token": "relogin-session-token",
            "account_id": "personal-acct-after-login",
            "workspace_id": "personal-ws-after-login",
            "source": "login",
        },
    )
    monkeypatch.setattr(
        team_invite_workflow,
        "refresh_member_team_token",
        lambda account, team_account_id, proxy: {
            "success": True,
            "account_id": "team-acct-1",
            "access_token": "team-access-token",
            "session_token": "team-session-token",
            "expires_at": datetime.utcnow() + timedelta(hours=1),
        },
    )

    uploaded: dict[str, list[int]] = {}

    def fake_upload(self, account_ids):
        uploaded["account_ids"] = list(account_ids)
        result = {
            "noop": {
                "success_count": len(account_ids),
                "failed_count": 0,
                "skipped_count": 0,
                "details": [],
            }
        }
        self._record_member_upload_results(account_ids, result)
        return result

    monkeypatch.setattr(team_invite_workflow.TeamInviteOrchestrator, "_upload_selected_accounts", fake_upload)

    team_invite_workflow.TeamInviteOrchestrator(task_uuid).run()

    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        member = task.members[0]
        refreshed_account = crud.get_account_by_id(db, member_account.id)

    assert task.status == "completed"
    assert uploaded["account_ids"] == [member_account.id]
    assert refreshed_account.workspace_id == "team-acct-1"
    assert refreshed_account.account_id == "team-acct-1"
    assert refreshed_account.subscription_type == "team"
    assert refreshed_account.session_token == "team-session-token"
    assert member.invitation_status == "uploaded"


def test_team_invite_workflow_recovers_session_token_from_sensitive_payload(temp_database, monkeypatch):
    main_account = create_account("owner@example.com")
    member_account = create_account(
        "member@example.com",
        session_token="",
        workspace_id="personal-workspace",
        subscription_type="free",
        access_token="stale-access-token",
        extra_data={
            SENSITIVE_SESSION_PAYLOAD_KEY: {
                "sessionToken": "payload-session-token",
            }
        },
    )
    task_uuid = create_team_invite_task_record(main_account, [member_account], task_uuid="invite-workflow-sensitive")

    monkeypatch.setattr(
        team_invite_workflow,
        "discover_team_account",
        lambda account, proxy: {
            "success": True,
            "account": {
                "team_account_id": "team-acct-1",
                "team_workspace_id": "team-ws-1",
                "subscription_plan": "team",
            },
        },
    )

    def fake_snapshot(self, admin_account, team_account_id, proxy, *, source):
        return {member_account.email.lower()} if source == "members" else set()

    monkeypatch.setattr(team_invite_workflow.TeamInviteOrchestrator, "_read_team_email_snapshot", fake_snapshot)

    captured = {}
    access_token = make_test_jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "team-acct-1",
                "chatgpt_user_id": "user-1",
            }
        }
    )

    def fake_refresh_request(url, headers=None, **kwargs):
        captured["headers"] = headers
        return DummyResponse(
            payload={
                "accessToken": access_token,
                "sessionToken": "refreshed-session-token",
                "expires": "2030-01-01T00:00:00Z",
                "account": {"id": "team-acct-1"},
            }
        )

    monkeypatch.setattr(team_invitation.cffi_requests, "get", fake_refresh_request)

    uploaded: dict[str, list[int]] = {}

    def fake_upload(self, account_ids):
        uploaded["account_ids"] = list(account_ids)
        result = {
            "noop": {
                "success_count": len(account_ids),
                "failed_count": 0,
                "skipped_count": 0,
                "details": [],
            }
        }
        self._record_member_upload_results(account_ids, result)
        return result

    monkeypatch.setattr(team_invite_workflow.TeamInviteOrchestrator, "_upload_selected_accounts", fake_upload)

    team_invite_workflow.TeamInviteOrchestrator(task_uuid).run()

    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        member = task.members[0]
        refreshed_account = crud.get_account_by_id(db, member_account.id)

    assert task.status == "completed"
    assert uploaded["account_ids"] == [member_account.id]
    assert captured["headers"]["Cookie"] == "__Secure-next-auth.session-token=payload-session-token"
    assert refreshed_account.workspace_id == "team-acct-1"
    assert refreshed_account.account_id == "team-acct-1"
    assert refreshed_account.subscription_type == "team"
    assert refreshed_account.session_token == "refreshed-session-token"
    assert member.invitation_status == "uploaded"


def test_execute_platform_upload_retries_recoverable_failures(temp_database):
    main_account = create_account("owner@example.com")
    member_account = create_account("member@example.com")
    task_uuid = create_team_invite_task_record(
        main_account,
        [member_account],
        task_uuid="invite-workflow-upload-retry",
        upload_config={"retry_limit": 2},
    )

    orchestrator = team_invite_workflow.TeamInviteOrchestrator(task_uuid)
    attempts = {"count": 0}

    def flaky_upload():
        attempts["count"] += 1
        if attempts["count"] < 3:
            return {
                "success_count": 0,
                "failed_count": 1,
                "skipped_count": 0,
                "details": [{"id": member_account.id, "success": False, "error": "上传失败: network timeout"}],
            }
        return {
            "success_count": 1,
            "failed_count": 0,
            "skipped_count": 0,
            "details": [{"id": member_account.id, "success": True, "message": "ok"}],
        }

    result = orchestrator._execute_platform_upload("Sub2API 上传失败: test", flaky_upload)

    assert attempts["count"] == 3
    assert result["success_count"] == 1
    assert result["failed_count"] == 0


def test_execute_platform_upload_does_not_retry_unrecoverable_refresh_failures(temp_database):
    main_account = create_account("owner@example.com")
    member_account = create_account("member@example.com")
    task_uuid = create_team_invite_task_record(
        main_account,
        [member_account],
        task_uuid="invite-workflow-upload-no-retry",
        upload_config={"retry_limit": 3},
    )

    orchestrator = team_invite_workflow.TeamInviteOrchestrator(task_uuid)
    attempts = {"count": 0}

    def unrecoverable_upload():
        attempts["count"] += 1
        return {
            "success_count": 0,
            "failed_count": 1,
            "skipped_count": 0,
            "details": [
                {
                    "id": member_account.id,
                    "success": False,
                    "error": (
                        'token refresh retry exhausted: error: code=502 '
                        'reason="OPENAI_OAUTH_TOKEN_REFRESH_FAILED" '
                        'message="token refresh failed: status 401, body: '
                        '{"error":{"type":"invalid_request_error","code":"refresh_token_reused"}}"'
                    ),
                }
            ],
        }

    result = orchestrator._execute_platform_upload("Sub2API 上传失败: test", unrecoverable_upload)

    assert attempts["count"] == 1
    assert result["failed_count"] == 1


def test_upload_selected_accounts_blocks_duplicate_refresh_tokens_before_sub2api_request(temp_database, monkeypatch):
    main_account = create_account("owner@example.com")
    member_a = create_account(
        "member-a@example.com",
        refresh_token="same-refresh-token",
        subscription_type="team",
        account_id="team-acct-1",
        workspace_id="team-acct-1",
    )
    member_b = create_account(
        "member-b@example.com",
        refresh_token="same-refresh-token",
        subscription_type="team",
        account_id="team-acct-1",
        workspace_id="team-acct-1",
    )
    service = create_sub2api_service()
    task_uuid = create_team_invite_task_record(
        main_account,
        [member_a, member_b],
        task_uuid="invite-workflow-token-guard",
        upload_config={"auto_upload_sub2api": True, "sub2api_service_ids": [service.id]},
    )

    with get_db() as db:
        crud.update_team_invite_task(
            db,
            task_uuid,
            status="uploading",
            team_account_id="team-acct-1",
            team_workspace_id="team-acct-1",
        )

    calls = []

    def fake_post(url, **kwargs):
        calls.append({"url": url, "kwargs": kwargs})
        return DummyResponse(payload={"code": 0, "message": "success", "data": {}})

    monkeypatch.setattr(sub2api_upload.cffi_requests, "post", fake_post)

    results = team_invite_workflow.TeamInviteOrchestrator(task_uuid)._upload_selected_accounts(
        [member_a.id, member_b.id]
    )

    assert calls == []
    assert results["sub2api"]["failed_count"] == 2
    assert results["sub2api"]["details"][0]["reason_code"] == "team_refresh_token_duplicate"

    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        members_by_email = {member.email: member for member in task.members}

    for email in (member_a.email, member_b.email):
        member = members_by_email[email]
        assert member.invitation_status == "failed"
        assert member.result["platform_uploads"]["sub2api"]["guard_blocked"] is True
        assert member.result["platform_uploads"]["sub2api"]["reason_code"] == "team_refresh_token_duplicate"


def test_relogin_members_forces_login_for_relogin_guidance_members(temp_database, monkeypatch):
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
        task_uuid="invite-workflow-batch-relogin",
        status="completed",
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
        member_id = task.members[0].id

    calls = []

    def fake_refresh(self, member, team_account_id, proxy, *, reason=None, last_action=None, force_relogin=False):
        calls.append(
            {
                "member_id": member.id,
                "team_account_id": team_account_id,
                "reason": reason,
                "last_action": last_action,
                "force_relogin": force_relogin,
            }
        )

    monkeypatch.setattr(team_invite_workflow.TeamInviteOrchestrator, "_refresh_member_team_context", fake_refresh)

    result = team_invite_workflow.TeamInviteOrchestrator(task_uuid).relogin_members([member_id])

    assert result["success_count"] == 1
    assert calls[0]["member_id"] == member_id
    assert calls[0]["force_relogin"] is True
    assert calls[0]["last_action"] == "batch_relogin"


def test_accept_or_refresh_member_accepts_pending_invite_members(temp_database, monkeypatch):
    main_account = create_account("owner@example.com")
    member_account = create_account(
        "member@example.com",
        workspace_id="personal-workspace",
        subscription_type="free",
    )
    task_uuid = create_team_invite_task_record(main_account, [member_account], task_uuid="invite-workflow-manual-accept")

    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)
        member = task.members[0]
        crud.update_team_invite_task(db, task_uuid, status="completed", team_account_id="team-acct-1")
        crud.update_team_invite_member(
            db,
            member.id,
            invitation_status="skipped",
            result={"reason": "pending_invite"},
        )
        member_id = member.id

    monkeypatch.setattr(
        team_invite_workflow.TeamInviteOrchestrator,
        "_load_task_context",
        lambda self, force_discovery: (self._get_task(), main_account, "team-acct-1", None),
    )

    def fake_snapshot(self, admin_account, team_account_id, proxy, *, source):
        return {member_account.email.lower()} if source == "invites" else set()

    accepted = {"count": 0}

    def fake_accept(self, member, team_account_id, proxy, *, last_action=None):
        accepted["count"] += 1
        self._update_member(
            member.id,
            invitation_status="accepted",
            result={"reason": "pending_invite", "last_action": last_action},
        )

    monkeypatch.setattr(team_invite_workflow.TeamInviteOrchestrator, "_read_team_email_snapshot", fake_snapshot)
    monkeypatch.setattr(team_invite_workflow.TeamInviteOrchestrator, "_accept_member_invitation", fake_accept)

    payload = team_invite_workflow.TeamInviteOrchestrator(task_uuid).accept_or_refresh_member(member_id)

    assert accepted["count"] == 1
    assert payload["id"] == member_id
    assert payload["invitation_status"] == "accepted"
