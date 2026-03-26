import asyncio
from types import SimpleNamespace
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


def test_create_team_invite_task_auto_bootstraps_missing_custom_source_account(temp_database, monkeypatch):
    create_email_service("moe_mail", "kan69.fun", priority=0, name="moe-primary")

    def fake_bootstrap(db, **kwargs):
        return crud.create_account(
            db,
            email=kwargs["normalized_email"],
            password=None,
            client_id="client-id",
            session_token="session-token",
            cookies="cookie-a=1",
            email_service=kwargs["normalized_service_type"],
            email_service_id="mailbox-1",
            account_id="acct-auto-bootstrap",
            workspace_id="ws-auto-bootstrap",
            access_token="access-token",
            refresh_token="refresh-token",
            id_token="id-token",
            proxy_used=kwargs["proxy_url"],
            extra_data={"path": "auto-bootstrap"},
            source="login",
            status="active",
        )

    monkeypatch.setattr(team_invite_routes, "_bootstrap_custom_source_account", fake_bootstrap)

    response = asyncio.run(
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

    assert response.status == "pending"
    assert response.source_account["email"] == "nfe9_-_d@kan69.fun"
    assert response.source_account["email_service"] == "moe_mail"
    with get_db() as db:
        account = crud.get_account_by_email(db, "nfe9_-_d@kan69.fun")
        assert account is not None
        assert account.account_id == "acct-auto-bootstrap"
        assert account.source == "login"


def test_bootstrap_custom_source_prefers_local_password_login_refresh(temp_database, monkeypatch):
    existing_account = create_account(
        "nfe9_-_d@kan69.fun",
        email_service="moe_mail",
        password="local-secret",
        access_token="",
        refresh_token="",
        session_token="stale-session",
        cookies="stale-cookie",
    )
    matched_service = create_email_service("moe_mail", "kan69.fun", priority=0, name="moe-primary")

    monkeypatch.setattr(
        team_invite_routes,
        "resolve_email_service_for_registration",
        lambda **kwargs: SimpleNamespace(
            service_type=team_invite_routes.EmailServiceType.MOE_MAIL,
            config={},
            service_name="moe-primary",
        ),
    )
    monkeypatch.setattr(
        team_invite_routes,
        "recover_account_session_via_login",
        lambda account, proxy_url=None, **kwargs: {
            "success": True,
            "email": account.email,
            "account_id": "acct-recovered",
            "workspace_id": "ws-recovered",
            "access_token": "access-recovered",
            "refresh_token": "refresh-recovered",
            "id_token": "id-recovered",
            "session_token": "session-recovered",
            "cookies": "cookie-recovered=1",
            "email_service_id": "mailbox-recovered",
            "source": "login",
        },
    )

    class FailRegistrationEngine:
        def __init__(self, *args, **kwargs):
            pytest.fail("RegistrationEngine should not run when local password refresh succeeds")

    monkeypatch.setattr(team_invite_routes, "RegistrationEngine", FailRegistrationEngine)

    with get_db() as db:
        account = team_invite_routes._bootstrap_custom_source_account(
            db,
            normalized_email="nfe9_-_d@kan69.fun",
            normalized_service_type="moe_mail",
            matched_service=matched_service,
            proxy_url="http://127.0.0.1:8080",
        )

    assert account.id == existing_account.id
    assert account.password == "local-secret"
    assert account.account_id == "acct-recovered"
    assert account.workspace_id == "ws-recovered"
    assert account.access_token == "access-recovered"
    assert account.email_service_id == "mailbox-recovered"


def test_bootstrap_custom_source_uses_login_probe_before_registration(temp_database, monkeypatch):
    matched_service = create_email_service("moe_mail", "kan69.fun", priority=0, name="moe-primary")

    monkeypatch.setattr(
        team_invite_routes,
        "resolve_email_service_for_registration",
        lambda **kwargs: SimpleNamespace(
            service_type=team_invite_routes.EmailServiceType.MOE_MAIL,
            config={},
            service_name="moe-primary",
        ),
    )
    monkeypatch.setattr(team_invite_routes.EmailServiceFactory, "create", lambda *args, **kwargs: object())

    class LoginProbeEngine:
        def __init__(self, email_service=None, proxy_url=None, **kwargs):
            self.email_service = email_service
            self.proxy_url = proxy_url
            self.logs = []
            self.email = None
            self.email_info = {"service_id": "mailbox-otp"}
            self._is_existing_account = False

        def _create_email(self):
            self.email = "nfe9_-_d@kan69.fun"
            return True

        def _prepare_authorize_flow(self, label):
            return "did-1", "sentinel-1"

        def _submit_login_start(self, did, sen_token):
            return SimpleNamespace(success=True, page_type=team_invite_routes.OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"])

        def _complete_token_exchange(self, result):
            result.email = self.email
            result.account_id = "acct-probed"
            result.workspace_id = "ws-probed"
            result.access_token = "access-probed"
            result.refresh_token = "refresh-probed"
            result.id_token = "id-probed"
            result.session_token = "session-probed"
            result.cookies = "cookie-probed=1"
            result.source = "login"
            result.metadata = {"path": "login-probe"}
            return True

        def run(self):
            pytest.fail("run() should not execute when login probe already succeeded")

    monkeypatch.setattr(team_invite_routes, "RegistrationEngine", LoginProbeEngine)

    with get_db() as db:
        account = team_invite_routes._bootstrap_custom_source_account(
            db,
            normalized_email="nfe9_-_d@kan69.fun",
            normalized_service_type="moe_mail",
            matched_service=matched_service,
            proxy_url="http://127.0.0.1:8080",
        )

    assert account.email == "nfe9_-_d@kan69.fun"
    assert account.account_id == "acct-probed"
    assert account.workspace_id == "ws-probed"
    assert account.access_token == "access-probed"
    assert account.source == "login"
    assert account.email_service_id == "mailbox-otp"


def test_bootstrap_custom_source_fails_early_when_login_requires_password_but_no_local_account(temp_database, monkeypatch):
    matched_service = create_email_service("moe_mail", "kan69.fun", priority=0, name="moe-primary")

    monkeypatch.setattr(
        team_invite_routes,
        "resolve_email_service_for_registration",
        lambda **kwargs: SimpleNamespace(
            service_type=team_invite_routes.EmailServiceType.MOE_MAIL,
            config={},
            service_name="moe-primary",
        ),
    )
    monkeypatch.setattr(team_invite_routes.EmailServiceFactory, "create", lambda *args, **kwargs: object())

    class PasswordPageProbeEngine:
        def __init__(self, email_service=None, proxy_url=None, **kwargs):
            self.email_service = email_service
            self.proxy_url = proxy_url
            self.logs = []
            self.email = None
            self.email_info = {"service_id": "mailbox-otp"}

        def _create_email(self):
            self.email = "nfe9_-_d@kan69.fun"
            return True

        def _prepare_authorize_flow(self, label):
            return "did-1", "sentinel-1"

        def _submit_login_start(self, did, sen_token):
            return SimpleNamespace(success=True, page_type=team_invite_routes.OPENAI_PAGE_TYPES["LOGIN_PASSWORD"])

        def run(self):
            pytest.fail("run() should not execute when login probe already identified password login")

    monkeypatch.setattr(team_invite_routes, "RegistrationEngine", PasswordPageProbeEngine)

    with get_db() as db:
        with pytest.raises(HTTPException) as exc_info:
            team_invite_routes._bootstrap_custom_source_account(
                db,
                normalized_email="nfe9_-_d@kan69.fun",
                normalized_service_type="moe_mail",
                matched_service=matched_service,
                proxy_url="http://127.0.0.1:8080",
            )

    assert exc_info.value.status_code == 400
    assert "没有保存这个 OpenAI 密码" in exc_info.value.detail


def test_create_team_invite_task_falls_back_when_service_domain_missing(temp_database, monkeypatch):
    create_email_service("moe_mail", "other.fun", priority=0, name="moe-fallback")
    create_email_service("moe_mail", "backup.fun", priority=10, name="moe-backup")
    captured = {}

    def fake_bootstrap(db, **kwargs):
        captured["service_id"] = kwargs["matched_service"].id
        captured["service_name"] = kwargs["matched_service"].name
        return crud.create_account(
            db,
            email=kwargs["normalized_email"],
            password=None,
            client_id="client-id",
            session_token="session-token",
            cookies="cookie-a=1",
            email_service=kwargs["normalized_service_type"],
            email_service_id="mailbox-1",
            account_id="acct-fallback",
            workspace_id="ws-fallback",
            access_token="access-token",
            refresh_token="refresh-token",
            id_token="id-token",
            proxy_used=kwargs["proxy_url"],
            extra_data={"path": "fallback-match"},
            source="login",
            status="active",
        )

    monkeypatch.setattr(team_invite_routes, "_bootstrap_custom_source_account", fake_bootstrap)

    response = asyncio.run(
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

    assert response.status == "pending"
    assert captured["service_name"] == "moe-fallback"


def test_create_team_invite_task_prefers_exact_domain_service_before_fallback(temp_database, monkeypatch):
    create_email_service("moe_mail", "other.fun", priority=0, name="moe-fallback")
    create_email_service("moe_mail", "kan69.fun", priority=10, name="moe-exact")
    captured = {}

    def fake_bootstrap(db, **kwargs):
        captured["service_id"] = kwargs["matched_service"].id
        captured["service_name"] = kwargs["matched_service"].name
        return crud.create_account(
            db,
            email=kwargs["normalized_email"],
            password=None,
            client_id="client-id",
            session_token="session-token",
            cookies="cookie-a=1",
            email_service=kwargs["normalized_service_type"],
            email_service_id="mailbox-1",
            account_id="acct-exact",
            workspace_id="ws-exact",
            access_token="access-token",
            refresh_token="refresh-token",
            id_token="id-token",
            proxy_used=kwargs["proxy_url"],
            extra_data={"path": "exact-match"},
            source="login",
            status="active",
        )

    monkeypatch.setattr(team_invite_routes, "_bootstrap_custom_source_account", fake_bootstrap)

    response = asyncio.run(
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

    assert response.status == "pending"
    assert captured["service_name"] == "moe-exact"


def test_create_team_invite_task_rejects_custom_source_when_service_type_has_no_enabled_service(temp_database):
    create_email_service("duck_mail", "kan69.fun", enabled=False)

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
    assert "没有可用服务" in exc_info.value.detail


def test_create_team_invite_task_rejects_custom_source_when_auto_bootstrap_fails(temp_database, monkeypatch):
    create_email_service("duck_mail", "kan69.fun")

    def fake_bootstrap(*args, **kwargs):
        raise HTTPException(status_code=400, detail="自动登录/注册失败: nfe9_-_d@kan69.fun")

    monkeypatch.setattr(team_invite_routes, "_bootstrap_custom_source_account", fake_bootstrap)

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
    assert "自动登录/注册失败" in exc_info.value.detail


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
