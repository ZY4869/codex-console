from datetime import datetime

import pytest

from src.config import settings as settings_module
from src.core.grok import managed_email_service as managed_email_module
from src.core.grok import register_workflow as workflow_module
from src.core.grok.register_workflow import GrokRegisterOrchestrator
from src.core.grok.signup_client import (
    GrokSignupBootstrap,
    GrokSignupResult,
    normalize_email_validation_code,
)
from src.services.base import looks_like_verification_email
from src.database import grok_crud
from src.database import session as db_session
from src.database.init_db import initialize_database
from src.database.models import EmailService
from src.database.session import get_db
from src.web.task_manager import task_manager


@pytest.fixture()
def temp_database(tmp_path, monkeypatch):
    db_url = f"sqlite:///{tmp_path / 'grok-workflow.db'}"
    monkeypatch.setenv("APP_DATABASE_URL", db_url)
    monkeypatch.setenv("APP_DATA_DIR", str(tmp_path))
    db_session._db_manager = None
    settings_module._settings = None
    initialize_database(db_url)
    yield
    db_session._db_manager = None
    settings_module._settings = None


def create_task(task_uuid: str, *, target_count: int = 2, config=None):
    with get_db() as db:
        return grok_crud.create_grok_task(
            db,
            task_uuid=task_uuid,
            target_count=target_count,
            thread_count=1,
            email_domain="mock.local",
            captcha_mode="local",
            config=config or {},
        )


def test_grok_workflow_completes_in_mock_mode(temp_database):
    create_task(
        "grok-mock-success",
        config={
            "mock_mode": True,
            "turnstile_site_key": "site-key",
            "signup_page_url": "https://example.test/signup",
        },
    )

    GrokRegisterOrchestrator("grok-mock-success").run()

    with get_db() as db:
        task = grok_crud.get_grok_task(db, "grok-mock-success")
        task.accounts
        accounts = list(task.accounts)

    assert task.status == "completed"
    assert task.success_count == 2
    assert task.failed_count == 0
    assert all(account.status == "completed" for account in accounts)
    assert all(account.sso_token for account in accounts)
    assert all(account.nsfw_enabled for account in accounts)


def test_grok_workflow_cancels_at_safe_point(temp_database, monkeypatch):
    create_task(
        "grok-cancelled",
        target_count=1,
        config={
            "mock_mode": True,
            "turnstile_site_key": "site-key",
            "signup_page_url": "https://example.test/signup",
        },
    )

    original_send = GrokRegisterOrchestrator._send_verification_code

    def cancel_then_send(self, email, config):
        task_manager.cancel_task(self.task_uuid)
        return original_send(self, email, config)

    monkeypatch.setattr(GrokRegisterOrchestrator, "_send_verification_code", cancel_then_send)

    GrokRegisterOrchestrator("grok-cancelled").run()

    with get_db() as db:
        task = grok_crud.get_grok_task(db, "grok-cancelled")
        account = task.accounts[0]

    assert task.status == "cancelled"
    assert account.status == "cancelled"
    assert account.completed_at is not None


def test_grok_workflow_persists_logs_and_stats(temp_database):
    create_task(
        "grok-log-task",
        target_count=1,
        config={
            "mock_mode": True,
            "turnstile_site_key": "site-key",
            "signup_page_url": "https://example.test/signup",
        },
    )

    GrokRegisterOrchestrator("grok-log-task").run()

    with get_db() as db:
        task = grok_crud.get_grok_task(db, "grok-log-task")

    assert task.result["success_count"] == 1
    assert "completed" in (task.logs or "")
    assert task.completed_at and isinstance(task.completed_at, datetime)


def test_grok_workflow_reuses_configured_email_service_in_auto_mode(temp_database, monkeypatch):
    create_task(
        "grok-managed-email",
        target_count=1,
        config={
            "yescaptcha_key": "dummy-key",
        },
    )

    with get_db() as db:
        db.add(
            EmailService(
                service_type="freemail",
                name="Freemail Managed",
                config={
                    "base_url": "https://freemail.example.test",
                    "admin_token": "secret",
                    "domain": "mock.local",
                },
                enabled=True,
                priority=0,
            )
        )
        db.commit()

    calls = {"service_type": None, "name": None, "create_email": 0, "get_code": 0}

    class FakeReusableService:
        def create_email(self, config=None):
            calls["create_email"] += 1
            assert (config or {}).get("domain") == "mock.local"
            return {"email": "managed@mock.local", "service_id": "managed-box"}

        def get_verification_code(self, email, email_id=None, timeout=120, pattern=None, otp_sent_at=None):
            calls["get_code"] += 1
            assert email == "managed@mock.local"
            assert email_id == "managed-box"
            assert pattern == managed_email_module.GROK_VERIFICATION_CODE_PATTERN
            return "123456"

    def fake_factory(service_type, config, name=None):
        calls["service_type"] = service_type.value
        calls["name"] = name
        assert config["domain"] == "mock.local"
        return FakeReusableService()

    monkeypatch.setattr(managed_email_module.EmailServiceFactory, "create", fake_factory)
    monkeypatch.setattr(GrokRegisterOrchestrator, "_send_verification_code", lambda self, email, config: {"challenge_id": "ok"})
    monkeypatch.setattr(GrokRegisterOrchestrator, "_verify_code", lambda self, email, code, challenge, config: {"verified": True})
    monkeypatch.setattr(GrokRegisterOrchestrator, "_sign_up", lambda self, email, code, captcha_token, verification, config: "sso-managed")
    monkeypatch.setattr(GrokRegisterOrchestrator, "_handle_nsfw_step", lambda self, account_id, config, proxy_url, signup_result: None)
    monkeypatch.setattr(GrokRegisterOrchestrator, "_follow_set_cookie_chain", lambda response: "sso-managed")
    monkeypatch.setattr(managed_email_module.BczyEmailService, "create_address", lambda self, order_index: (_ for _ in ()).throw(AssertionError("legacy adapter should not be used")))

    orchestrator = GrokRegisterOrchestrator("grok-managed-email")
    monkeypatch.setattr(orchestrator.turnstile_service, "solve", lambda **kwargs: "turnstile-token")
    monkeypatch.setattr(orchestrator.user_agreement_service, "accept", lambda **kwargs: None)

    orchestrator.run()

    with get_db() as db:
        task = grok_crud.get_grok_task(db, "grok-managed-email")
        account = task.accounts[0]

    assert calls["service_type"] == "freemail"
    assert calls["name"] == "Freemail Managed"
    assert calls["create_email"] == 1
    assert calls["get_code"] == 1
    assert task.status == "completed"
    assert account.email == "managed@mock.local"


def test_grok_workflow_uses_discovered_signup_bootstrap(temp_database, monkeypatch):
    create_task(
        "grok-bootstrap-task",
        target_count=1,
        config={
            "yescaptcha_key": "dummy-key",
        },
    )

    bootstrap = GrokSignupBootstrap(
        site_url="https://accounts.x.ai",
        signup_url="https://accounts.x.ai/sign-up",
        site_key="site-key-from-bootstrap",
        state_tree="state-tree-from-bootstrap",
        action_id="7f8c18544add07ec70d3b96137e2df4586def41ecd",
    )
    calls = {
        "discover": 0,
        "turnstile": None,
        "agreement": None,
        "nsfw": None,
        "closed": False,
    }

    class FakeSignupClient:
        def __init__(self, *, proxy_url, bootstrap):
            assert proxy_url is None
            assert bootstrap == expected_bootstrap
            self.impersonate = "chrome120"
            self.user_agent = "ua/test"

        def close(self):
            calls["closed"] = True

        def send_verification_code(self, email):
            return {"status": "sent", "email": email}

        def verify_code(self, email, code):
            return {"status": "verified", "email": email, "code": code}

        def sign_up(self, email, code, captcha_token):
            return GrokSignupResult(
                sso_token="sso-main",
                sso_rw_token="sso-main-rw",
                grok_sso_token="sso-grok",
                grok_sso_rw_token="sso-grok-rw",
                grok_cookies={"cf_clearance": "cookie-value"},
                impersonate="chrome120",
                user_agent="ua/test",
            )

    class FakeEmailService:
        def create_address(self, order_index):
            return {"email": "bootstrap@mock.local"}

        def poll_code(self, email, timeout_seconds, poll_interval):
            assert email == "bootstrap@mock.local"
            return "654321"

    expected_bootstrap = bootstrap
    monkeypatch.setattr(
        workflow_module,
        "discover_signup_bootstrap",
        lambda **kwargs: calls.__setitem__("discover", calls["discover"] + 1) or expected_bootstrap,
    )
    monkeypatch.setattr(workflow_module, "GrokSignupClient", FakeSignupClient)
    monkeypatch.setattr(workflow_module, "create_grok_email_service", lambda task: FakeEmailService())

    orchestrator = GrokRegisterOrchestrator("grok-bootstrap-task")
    monkeypatch.setattr(
        orchestrator.turnstile_service,
        "solve",
        lambda **kwargs: calls.__setitem__("turnstile", kwargs) or "turnstile-token",
    )
    monkeypatch.setattr(
        orchestrator.user_agreement_service,
        "accept",
        lambda **kwargs: calls.__setitem__("agreement", kwargs) or {"success": True},
    )
    monkeypatch.setattr(
        orchestrator.nsfw_service,
        "enable",
        lambda **kwargs: calls.__setitem__("nsfw", kwargs) or {"success": True},
    )

    orchestrator.run()

    with get_db() as db:
        task = grok_crud.get_grok_task(db, "grok-bootstrap-task")
        account = task.accounts[0]

    assert calls["discover"] == 1
    assert calls["turnstile"]["sitekey"] == "site-key-from-bootstrap"
    assert calls["turnstile"]["page_url"] == "https://accounts.x.ai/sign-up"
    assert calls["agreement"]["sso_token"] == "sso-main"
    assert calls["agreement"]["sso_rw_token"] == "sso-main-rw"
    assert calls["nsfw"]["sso_token"] == "sso-grok"
    assert calls["nsfw"]["extra_cookies"] == {"cf_clearance": "cookie-value"}
    assert calls["closed"] is True
    assert task.status == "completed"
    assert account.sso_token == "sso-main"


def test_grok_email_keyword_matcher_accepts_xai_and_grok():
    assert looks_like_verification_email("The xAI Team", "Validate your email")
    assert looks_like_verification_email("support@grok.com", "Your code")
    assert looks_like_verification_email("OpenAI", "Verification code")
    assert looks_like_verification_email("plain sender", "nothing interesting") is False


def test_grok_verification_pattern_matches_xai_code_not_css_digits():
    html = """
    ZHO-A5O xAI confirmation code
    <style>
      h1 { color: #333333; }
    </style>
    <p>Validate your email</p>
    <td>ZHO-A5O</td>
    """
    import re

    match = re.search(managed_email_module.GROK_VERIFICATION_CODE_PATTERN, html)
    assert match is not None
    assert match.group(1) == "ZHO-A5O"


def test_normalize_grok_email_validation_code_removes_hyphen():
    assert normalize_email_validation_code("YLB-40T") == "YLB40T"
    assert normalize_email_validation_code(" 9vk-wg7 ") == "9VKWG7"
