from src.config.constants import PASSWORD_SPECIAL_CHARSET
from src.core import register as register_module
from src.core.anyauto.register_flow import AnyAutoRegistrationEngine
from src.core.anyauto.utils import FlowState
from src.core.register import RegistrationEngine
from src.core.utils import generate_password


def _assert_password_is_hardened(password: str) -> None:
    assert len(password) >= 8
    assert any(ch.islower() for ch in password)
    assert any(ch.isupper() for ch in password)
    assert any(ch.isdigit() for ch in password)
    assert any(ch in PASSWORD_SPECIAL_CHARSET for ch in password)


def test_generate_password_contains_special_characters():
    _assert_password_is_hardened(generate_password(12))


def test_registration_engine_generate_password_contains_special_characters():
    engine = RegistrationEngine.__new__(RegistrationEngine)
    _assert_password_is_hardened(RegistrationEngine._generate_password(engine, 12))


def test_anyauto_generate_password_contains_special_characters():
    _assert_password_is_hardened(AnyAutoRegistrationEngine._build_password(12))


def test_register_password_with_retry_retries_generic_400_only_when_still_on_password_page(monkeypatch):
    engine = RegistrationEngine.__new__(RegistrationEngine)
    attempts = []
    logs = []
    engine._last_register_password_context = {}
    engine._last_register_password_error = None
    engine._otp_sent_at = None
    engine._is_existing_account = False
    engine._consent_skip_otp = False
    engine._registration_conflict_detected = False
    engine._registration_conflict_message = ""

    def fake_register_password(_did=None, _sen_token=None):
        attempts.append(1)
        if len(attempts) < 3:
            engine._last_register_password_error = (
                "注册密码接口返回异常: HTTP 400, type=invalid_request_error, code=-, "
                "message=Failed to create account. Please try again."
            )
            engine._last_register_password_context = {
                "status_code": 400,
                "error_type": "invalid_request_error",
                "error_code": "",
                "error_message": "Failed to create account. Please try again.",
                "flow_state": FlowState(page_type="create_account_password"),
            }
            return False, None
        return True, "Aa1!retryPwd"

    monkeypatch.setattr(register_module.time, "sleep", lambda _seconds: None)
    engine._register_password = fake_register_password
    engine._probe_register_password_flow_state = lambda: FlowState(page_type="create_account_password")
    engine._log = lambda message, level="info": logs.append((level, message))

    success, password = RegistrationEngine._register_password_with_retry(engine, None, None)

    assert success is True
    assert password == "Aa1!retryPwd"
    assert len(attempts) == 3
    assert any("确认仍停留在密码页" in message for _level, message in logs)


def test_register_password_with_retry_switches_to_email_otp_branch(monkeypatch):
    engine = RegistrationEngine.__new__(RegistrationEngine)
    logs = []
    attempts = []
    engine._last_register_password_context = {}
    engine._last_register_password_error = None
    engine._otp_sent_at = None
    engine._is_existing_account = False
    engine._consent_skip_otp = False
    engine._registration_conflict_detected = False
    engine._registration_conflict_message = ""
    engine.password = "Aa1!flowShift"

    def fake_register_password(_did=None, _sen_token=None):
        attempts.append(1)
        engine._last_register_password_error = (
            "注册密码接口返回异常: HTTP 400, type=invalid_request_error, code=-, "
            "message=Failed to create account. Please try again."
        )
        engine._last_register_password_context = {
            "status_code": 400,
            "error_type": "invalid_request_error",
            "error_code": "",
            "error_message": "Failed to create account. Please try again.",
            "flow_state": FlowState(page_type="create_account_password"),
        }
        return False, None

    monkeypatch.setattr(register_module.time, "sleep", lambda _seconds: None)
    engine._register_password = fake_register_password
    engine._probe_register_password_flow_state = lambda: FlowState(page_type="email_otp_verification")
    engine._log = lambda message, level="info": logs.append((level, message))

    success, password = RegistrationEngine._register_password_with_retry(engine, None, None)

    assert success is True
    assert password == "Aa1!flowShift"
    assert len(attempts) == 1
    assert engine._is_existing_account is True
    assert engine._otp_sent_at is not None
    assert any("邮箱验证码页" in message for _level, message in logs)


def test_register_password_with_retry_stops_when_flow_shifts_to_login_password(monkeypatch):
    engine = RegistrationEngine.__new__(RegistrationEngine)
    logs = []
    attempts = []
    engine._last_register_password_context = {}
    engine._last_register_password_error = None
    engine._otp_sent_at = None
    engine._is_existing_account = False
    engine._consent_skip_otp = False
    engine._registration_conflict_detected = False
    engine._registration_conflict_message = ""

    def fake_register_password(_did=None, _sen_token=None):
        attempts.append(1)
        engine._last_register_password_error = (
            "注册密码接口返回异常: HTTP 400, type=invalid_request_error, code=-, "
            "message=Failed to create account. Please try again."
        )
        engine._last_register_password_context = {
            "status_code": 400,
            "error_type": "invalid_request_error",
            "error_code": "",
            "error_message": "Failed to create account. Please try again.",
            "flow_state": FlowState(page_type="create_account_password"),
        }
        return False, None

    monkeypatch.setattr(register_module.time, "sleep", lambda _seconds: None)
    engine._register_password = fake_register_password
    engine._probe_register_password_flow_state = lambda: FlowState(page_type="login_password")
    engine._log = lambda message, level="info": logs.append((level, message))

    success, password = RegistrationEngine._register_password_with_retry(engine, None, None)

    assert success is False
    assert password is None
    assert len(attempts) == 1
    assert engine._registration_conflict_detected is True
    assert "登录密码页" in engine._registration_conflict_message
    assert any("登录密码页" in message for _level, message in logs)
