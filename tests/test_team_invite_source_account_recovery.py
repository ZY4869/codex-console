from datetime import datetime, timedelta

import pytest

from src.database import crud
from src.database import session as db_session
from src.database.init_db import initialize_database
from src.database.models import Account
from src.database.session import get_db


@pytest.fixture()
def temp_database(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'team-invite-source-recovery-tests.db'}"
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


def create_team_invite_task_record(
    source_account: Account,
    members: list[Account],
    *,
    task_uuid: str,
    upload_config: dict | None = None,
) -> str:
    with get_db() as db:
        task = crud.create_team_invite_task(
            db,
            task_uuid=task_uuid,
            source_mode="account",
            source_account_id=source_account.id,
            proxy="http://127.0.0.1:8080",
            upload_config=upload_config or {},
        )
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


def test_team_invite_workflow_recovers_source_account_before_discovery(temp_database, monkeypatch):
    from src.core import team_invite_workflow

    source_account = create_account(
        "owner@example.com",
        access_token="",
        refresh_token="",
        session_token="stale-session-token",
        cookies="stale-cookie",
    )
    member_account = create_account(
        "member@example.com",
        account_id="team-acct-1",
        workspace_id="team-acct-1",
        subscription_type="team",
        access_token="member-team-token",
    )
    task_uuid = create_team_invite_task_record(source_account, [member_account], task_uuid="source-recovery-success")

    recover_calls = []
    discover_calls = []
    uploaded = {}

    monkeypatch.setattr(
        team_invite_workflow,
        "recover_account_session_via_login",
        lambda account, proxy_url=None, callback_logger=None, **kwargs: (
            recover_calls.append(account.email)
            or {
                "success": True,
                "access_token": "recovered-access-token",
                "refresh_token": "recovered-refresh-token",
                "id_token": "recovered-id-token",
                "session_token": "recovered-session-token",
                "account_id": "personal-acct-after-login",
                "workspace_id": "personal-ws-after-login",
                "source": "login",
            }
        ),
    )
    monkeypatch.setattr(
        team_invite_workflow,
        "discover_team_account",
        lambda account, proxy: (
            discover_calls.append(account.access_token)
            or {
                "success": True,
                "account": {
                    "team_account_id": "team-acct-1",
                    "team_workspace_id": "team-ws-1",
                    "subscription_plan": "team",
                },
            }
        ),
    )
    monkeypatch.setattr(
        team_invite_workflow,
        "refresh_member_team_token",
        lambda account, team_account_id, proxy: {
            "success": True,
            "account_id": team_account_id,
            "access_token": "team-access-token",
            "session_token": "team-session-token",
            "expires_at": datetime.utcnow() + timedelta(hours=1),
        },
    )

    def fake_upload(self, account_ids, *args, **kwargs):
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
        refreshed_source_account = crud.get_account_by_id(db, source_account.id)

    assert task.status == "completed"
    assert recover_calls == [source_account.email]
    assert discover_calls == ["recovered-access-token"]
    assert refreshed_source_account.account_id == "team-acct-1"
    assert refreshed_source_account.workspace_id == "team-acct-1"
    assert refreshed_source_account.subscription_type == "team"
    assert refreshed_source_account.access_token == "team-access-token"
    assert uploaded["account_ids"] == [member_account.id]


def test_team_invite_workflow_fails_early_when_source_account_cannot_recover(temp_database, monkeypatch):
    from src.core import team_invite_workflow

    source_account = create_account(
        "owner@example.com",
        access_token="",
        refresh_token="",
        session_token="",
        cookies="",
        extra_data={},
    )
    member_account = create_account("member@example.com")
    task_uuid = create_team_invite_task_record(source_account, [member_account], task_uuid="source-recovery-failed")

    monkeypatch.setattr(
        team_invite_workflow,
        "recover_account_session_via_login",
        lambda account, proxy_url=None, callback_logger=None, **kwargs: {
            "success": False,
            "error": f"source-account-relogin-required: {account.email}",
        },
    )
    monkeypatch.setattr(
        team_invite_workflow,
        "discover_team_account",
        lambda *args, **kwargs: pytest.fail("discover_team_account should not run when source recovery fails"),
    )

    team_invite_workflow.TeamInviteOrchestrator(task_uuid).run()

    with get_db() as db:
        task = crud.get_team_invite_task(db, task_uuid)

    assert task.status == "failed"
    assert "source-account-relogin-required" in (task.error_message or "")
