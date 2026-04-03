from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

from src.config.constants import EmailServiceType
from src.core import enhanced_protocol_register as adapter_module
from src.core.enhanced_protocol_register import (
    AdaptiveProtocolRegistrationEngine,
    EnhancedProtocolRegistrationEngine,
)
from src.database import crud
from src.database.models import Base
from src.database.session import DatabaseSessionManager


class DummyEmailService:
    service_type = EmailServiceType.FREEMAIL
    name = "dummy-freemail"


class FakeCookies:
    def __init__(self, values):
        self._values = dict(values)

    def items(self):
        return self._values.items()


def _build_db_manager(name: str) -> DatabaseSessionManager:
    runtime_dir = Path("tests_runtime")
    runtime_dir.mkdir(exist_ok=True)
    db_path = runtime_dir / name
    if db_path.exists():
        db_path.unlink()

    manager = DatabaseSessionManager(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=manager.engine)
    return manager


def test_enhanced_protocol_engine_maps_success_and_persists_account(monkeypatch):
    manager = _build_db_manager("enhanced_protocol_success.db")

    @contextmanager
    def fake_get_db():
        session = manager.SessionLocal()
        try:
            yield session
        finally:
            session.close()

    class FakeAnyAutoRegistrationEngine:
        def __init__(self, **kwargs):
            self.email = "enhanced-success@example.com"
            self.password = "Passw0rd!"
            self.email_info = {"service_id": 321}
            self.session = None

        def run(self):
            return {
                "success": True,
                "access_token": "access-token",
                "refresh_token": "refresh-token",
                "id_token": "id-token",
                "session_token": "session-token",
                "account_id": "acct_123",
                "workspace_id": "ws_123",
                "metadata": {"auth_provider": "openai"},
            }

    monkeypatch.setattr(adapter_module, "AnyAutoRegistrationEngine", FakeAnyAutoRegistrationEngine)
    monkeypatch.setattr(adapter_module, "get_db", fake_get_db)

    engine = EnhancedProtocolRegistrationEngine(email_service=DummyEmailService())
    result = engine.run()

    assert result.success is True
    assert result.email == "enhanced-success@example.com"
    assert result.password == "Passw0rd!"
    assert result.account_id == "acct_123"
    assert result.workspace_id == "ws_123"
    assert result.metadata["registration_flow"] == "protocol.enhanced"

    assert engine.save_to_database(result) is True
    with manager.session_scope() as session:
        account = crud.get_account_by_email(session, "enhanced-success@example.com")
        assert account is not None
        assert account.account_id == "acct_123"
        assert account.workspace_id == "ws_123"
        assert account.email_service == EmailServiceType.FREEMAIL.value


def test_enhanced_protocol_engine_maps_add_phone_result(monkeypatch):
    class FakeAnyAutoRegistrationEngine:
        def __init__(self, **kwargs):
            self.email = "enhanced-phone@example.com"
            self.password = "Passw0rd!"
            self.email_info = {"service_id": 1}
            self.session = None

        def run(self):
            return {
                "success": True,
                "metadata": {
                    "phone_verification_required": True,
                    "token_pending": True,
                    "oauth_error": "add_phone required",
                },
            }

    monkeypatch.setattr(adapter_module, "AnyAutoRegistrationEngine", FakeAnyAutoRegistrationEngine)

    engine = EnhancedProtocolRegistrationEngine(email_service=DummyEmailService())
    result = engine.run()

    assert result.success is False
    assert result.email == "enhanced-phone@example.com"
    assert result.error_code == "add_phone_required"
    assert "add_phone" in result.error_message


def test_enhanced_protocol_engine_persists_cookie_fallback_session(monkeypatch):
    class FakeAnyAutoRegistrationEngine:
        def __init__(self, **kwargs):
            self.email = "enhanced-cookie@example.com"
            self.password = "Passw0rd!"
            self.email_info = {"service_id": 9}
            self.session = SimpleNamespace(
                cookies=FakeCookies(
                    {
                        "__Secure-next-auth.session-token": "cookie-session-token",
                        "foo": "bar",
                    }
                )
            )

        def run(self):
            return {
                "success": True,
                "access_token": "access-token",
                "account_id": "acct_cookie",
                "workspace_id": "ws_cookie",
                "metadata": {},
            }

    monkeypatch.setattr(adapter_module, "AnyAutoRegistrationEngine", FakeAnyAutoRegistrationEngine)

    engine = EnhancedProtocolRegistrationEngine(email_service=DummyEmailService())
    result = engine.run()

    assert result.success is True
    assert result.session_token == "cookie-session-token"
    assert result.cookies == "__Secure-next-auth.session-token=cookie-session-token; foo=bar"


def test_enhanced_protocol_engine_respects_cancel(monkeypatch):
    class FakeAnyAutoRegistrationEngine:
        def __init__(self, **kwargs):
            self.check_cancelled = kwargs["check_cancelled"]
            self.email = "cancelled@example.com"
            self.password = "Passw0rd!"
            self.email_info = {"service_id": 1}
            self.session = None

        def run(self):
            if self.check_cancelled():
                raise RuntimeError("任务已取消")
            return {"success": True}

    monkeypatch.setattr(adapter_module, "AnyAutoRegistrationEngine", FakeAnyAutoRegistrationEngine)

    engine = EnhancedProtocolRegistrationEngine(email_service=DummyEmailService())
    engine.cancel()
    result = engine.run()

    assert result.success is False
    assert "任务已取消" in result.error_message


def test_adaptive_protocol_engine_sets_flow_metadata_and_extra_config(monkeypatch):
    captured = {}

    class FakeAnyAutoRegistrationEngine:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs
            self.email = "adaptive@example.com"
            self.password = "Passw0rd!"
            self.email_info = {"service_id": 7}
            self.session = None

        def run(self):
            return {
                "success": True,
                "access_token": "adaptive-token",
                "metadata": {},
            }

    monkeypatch.setattr(adapter_module, "AnyAutoRegistrationEngine", FakeAnyAutoRegistrationEngine)

    engine = AdaptiveProtocolRegistrationEngine(email_service=DummyEmailService())
    result = engine.run()

    assert result.success is True
    assert result.metadata["registration_flow"] == "protocol.adaptive"
    assert captured["kwargs"]["extra_config"]["adaptive_register_recovery"] is True


# ---------------------------------------------------------------------------
# Adaptive recovery state machine tests
# ---------------------------------------------------------------------------

from unittest.mock import patch, MagicMock
from src.core.anyauto.chatgpt_client import ChatGPTClient, RegisterUserResult
from src.core.anyauto.utils import FlowState, describe_flow_state


def _make_client():
    """Create a ChatGPTClient with verbose=False for testing."""
    client = ChatGPTClient.__new__(ChatGPTClient)
    client.verbose = False
    client.AUTH = "https://auth.openai.com"
    client.BASE = "https://chatgpt.com"
    client.last_registration_state = None
    return client


def _generic_400_result():
    """Return a RegisterUserResult simulating the generic 400 failure."""
    return RegisterUserResult(
        success=False,
        status_code=400,
        error_message="Failed to create account. Please try again.",
        response_state=FlowState(page_type="create_account_password"),
    )


def _stub_preauth(client):
    """Patch the pre-auth phase so register_complete_flow jumps to state machine."""
    client.visit_homepage = MagicMock(return_value=True)
    client.get_csrf_token = MagicMock(return_value="fake-csrf")
    client.signin = MagicMock(return_value="https://auth.openai.com/create-account/password")
    client.authorize = MagicMock(return_value="https://auth.openai.com/create-account/password")
    client.send_email_otp = MagicMock(return_value=True)


class FakeSkymailClient:
    def wait_for_verification_code(self, email, timeout=90, exclude_codes=None):
        return "123456"


def test_adaptive_recovery_to_email_otp():
    """400 后恢复到 email_otp_verification，应继续验证码分支。"""
    client = _make_client()
    _stub_preauth(client)

    recovery_state = FlowState(page_type="email_otp_verification",
                               current_url="https://auth.openai.com/email-verification")

    client.register_user = MagicMock(return_value=_generic_400_result())
    client._probe_register_recovery_state = MagicMock(return_value=recovery_state)

    # After recovery to email_otp, the flow will call wait_for_verification_code then verify_email_otp
    # verify_email_otp succeeds and transitions to about_you, then create_account succeeds and goes to completion
    about_you_state = FlowState(page_type="about_you",
                                current_url="https://auth.openai.com/about-you")
    complete_state = FlowState(page_type="callback",
                               current_url="https://chatgpt.com/api/auth/callback/login-web")
    client.verify_email_otp = MagicMock(return_value=(True, about_you_state))
    client.create_account = MagicMock(return_value=(True, complete_state))

    skymail = FakeSkymailClient()
    success, msg = client.register_complete_flow(
        "test@example.com", "Pass123!", "John", "Doe", "2000-01-01",
        skymail, adaptive_register_recovery=True,
    )

    assert success is True
    assert msg == "注册成功"
    client._probe_register_recovery_state.assert_called_once()


def test_adaptive_recovery_to_about_you():
    """400 后恢复到 about_you，应跳过 OTP 直接进入 create_account。"""
    client = _make_client()
    _stub_preauth(client)

    recovery_state = FlowState(page_type="about_you",
                               current_url="https://auth.openai.com/about-you")

    client.register_user = MagicMock(return_value=_generic_400_result())
    client._probe_register_recovery_state = MagicMock(return_value=recovery_state)

    complete_state = FlowState(page_type="callback",
                               current_url="https://chatgpt.com/api/auth/callback/login-web")
    client.create_account = MagicMock(return_value=(True, complete_state))

    skymail = FakeSkymailClient()
    success, msg = client.register_complete_flow(
        "test@example.com", "Pass123!", "John", "Doe", "2000-01-01",
        skymail, adaptive_register_recovery=True,
    )

    assert success is True
    assert msg == "注册成功"
    # verify_email_otp should NOT be called (OTP was skipped)
    assert not hasattr(client, '_verify_email_otp_called')


def test_adaptive_recovery_to_add_phone():
    """400 后恢复到 add_phone，应返回 add_phone_required。"""
    client = _make_client()
    _stub_preauth(client)

    recovery_state = FlowState(page_type="add_phone",
                               current_url="https://auth.openai.com/add-phone")

    client.register_user = MagicMock(return_value=_generic_400_result())
    client._probe_register_recovery_state = MagicMock(return_value=recovery_state)

    skymail = FakeSkymailClient()
    success, msg = client.register_complete_flow(
        "test@example.com", "Pass123!", "John", "Doe", "2000-01-01",
        skymail, adaptive_register_recovery=True,
    )

    assert success is True
    assert msg == "add_phone_required"


def test_adaptive_recovery_to_completion():
    """400 后恢复到 external_url / chatgpt_home，应继续进入 session/token 提取。"""
    client = _make_client()
    _stub_preauth(client)

    recovery_state = FlowState(page_type="chatgpt_home",
                               current_url="https://chatgpt.com/")

    client.register_user = MagicMock(return_value=_generic_400_result())
    client._probe_register_recovery_state = MagicMock(return_value=recovery_state)

    skymail = FakeSkymailClient()
    success, msg = client.register_complete_flow(
        "test@example.com", "Pass123!", "John", "Doe", "2000-01-01",
        skymail, adaptive_register_recovery=True,
    )

    # chatgpt_home is a completion state, so it should succeed
    assert success is True
    assert msg == "注册成功"


def test_adaptive_recovery_still_password_page():
    """400 后仍停留 create_account_password，应保持失败。"""
    client = _make_client()
    _stub_preauth(client)

    # Recovery state is still on the password page
    recovery_state = FlowState(page_type="create_account_password",
                               current_url="https://auth.openai.com/create-account/password")

    client.register_user = MagicMock(return_value=_generic_400_result())
    client._probe_register_recovery_state = MagicMock(return_value=recovery_state)

    skymail = FakeSkymailClient()
    success, msg = client.register_complete_flow(
        "test@example.com", "Pass123!", "John", "Doe", "2000-01-01",
        skymail, adaptive_register_recovery=True,
    )

    assert success is False
    assert "注册失败" in msg


def test_adaptive_recovery_to_login_password():
    """400 后恢复到 login_password，应保持失败且提示进入登录流程。"""
    client = _make_client()
    _stub_preauth(client)

    recovery_state = FlowState(page_type="login_password",
                               current_url="https://auth.openai.com/log-in/password")

    client.register_user = MagicMock(return_value=_generic_400_result())
    client._probe_register_recovery_state = MagicMock(return_value=recovery_state)

    skymail = FakeSkymailClient()
    success, msg = client.register_complete_flow(
        "test@example.com", "Pass123!", "John", "Doe", "2000-01-01",
        skymail, adaptive_register_recovery=True,
    )

    assert success is False
    assert "登录流程" in msg
