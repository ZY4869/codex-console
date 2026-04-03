from types import SimpleNamespace

from src.core.anyauto import register_flow as register_flow_module
from src.core.anyauto.register_flow import AnyAutoRegistrationEngine


class FakeCookie:
    def __init__(self, name, value):
        self.name = name
        self.value = value


class RecordingEmailService:
    def __init__(self, emails):
        self._emails = list(emails)
        self.create_calls = 0

    def create_email(self):
        self.create_calls += 1
        if not self._emails:
            raise AssertionError("no email queued")
        return self._emails.pop(0)


class BaseFakeChatGPTClient:
    def __init__(self, *args, **kwargs):
        self.session = SimpleNamespace(
            cookies=SimpleNamespace(
                jar=[FakeCookie("__Secure-next-auth.session-token", "cookie-session-token")]
            )
        )
        self.device_id = "device-12345678"
        self.ua = "ua"
        self.sec_ch_ua = "sec-ch-ua"
        self.impersonate = "chrome"
        self._log = lambda _msg: None


class UnusedOAuthClient:
    def __init__(self, *args, **kwargs):
        self.session = None
        self.last_error = ""
        self._log = lambda _msg: None


def _patch_common_dependencies(monkeypatch, chatgpt_client_cls, oauth_client_cls):
    monkeypatch.setattr(register_flow_module, "ChatGPTClient", chatgpt_client_cls)
    monkeypatch.setattr(register_flow_module, "OAuthClient", oauth_client_cls)
    monkeypatch.setattr(register_flow_module, "generate_random_name", lambda: ("Test", "User"))
    monkeypatch.setattr(register_flow_module, "generate_random_birthday", lambda: "1990-01-01")
    monkeypatch.setattr(
        register_flow_module,
        "get_settings",
        lambda: SimpleNamespace(
            registration_default_password_length=12,
            openai_auth_url="https://auth.openai.com/oauth/authorize",
            openai_client_id="client-id",
            openai_redirect_uri="http://localhost:1455/auth/callback",
        ),
    )


def test_http_400_retry_reuses_same_email(monkeypatch):
    class FakeChatGPTClient(BaseFakeChatGPTClient):
        register_emails = []

        def register_complete_flow(self, email, *args, **kwargs):
            self.__class__.register_emails.append(email)
            if len(self.__class__.register_emails) == 1:
                return False, "HTTP 400: Failed to create account. Please try again."
            return True, "ok"

        def reuse_session_and_get_tokens(self):
            return True, {
                "access_token": "session-access-token",
                "refresh_token": "session-refresh-token",
                "session_token": "session-token",
                "account_id": "acct-session",
                "workspace_id": "ws-session",
                "auth_provider": "openai",
                "expires": "2099-01-01T00:00:00Z",
                "user_id": "user-session",
                "user": {"id": "user-session"},
                "account": {"id": "acct-session"},
            }

    _patch_common_dependencies(monkeypatch, FakeChatGPTClient, UnusedOAuthClient)
    logs = []
    service = RecordingEmailService(
        [
            {"email": "Retry@Example.com", "service_id": "svc-1"},
            {"email": "other@example.com", "service_id": "svc-2"},
        ]
    )

    result = AnyAutoRegistrationEngine(
        email_service=service,
        max_retries=3,
        callback_logger=logs.append,
    ).run()

    assert result["success"] is True
    assert service.create_calls == 1
    assert FakeChatGPTClient.register_emails == ["retry@example.com", "retry@example.com"]
    assert any("复用同一个邮箱继续重试" in line for line in logs)


def test_retry_switches_to_new_email_after_four_failures(monkeypatch):
    class FakeChatGPTClient(BaseFakeChatGPTClient):
        register_emails = []

        def register_complete_flow(self, email, *args, **kwargs):
            self.__class__.register_emails.append(email)
            if len(self.__class__.register_emails) < 5:
                return False, "HTTP 400: Failed to create account. Please try again."
            return True, "ok"

        def reuse_session_and_get_tokens(self):
            return True, {
                "access_token": "session-access-token",
                "refresh_token": "session-refresh-token",
                "session_token": "session-token",
                "account_id": "acct-session",
                "workspace_id": "ws-session",
                "auth_provider": "openai",
                "expires": "2099-01-01T00:00:00Z",
                "user_id": "user-session",
                "user": {"id": "user-session"},
                "account": {"id": "acct-session"},
            }

    _patch_common_dependencies(monkeypatch, FakeChatGPTClient, UnusedOAuthClient)
    logs = []
    service = RecordingEmailService(
        [
            {"email": "first@example.com", "service_id": "svc-1"},
            {"email": "second@example.com", "service_id": "svc-2"},
        ]
    )

    result = AnyAutoRegistrationEngine(
        email_service=service,
        max_retries=5,
        callback_logger=logs.append,
    ).run()

    assert result["success"] is True
    assert service.create_calls == 2
    assert FakeChatGPTClient.register_emails[:4] == ["first@example.com"] * 4
    assert FakeChatGPTClient.register_emails[4] == "second@example.com"
    assert any("同一个邮箱连续失败 4/4" in line for line in logs)


def test_add_phone_in_oauth_enrichment_switches_to_new_email(monkeypatch):
    class FakeChatGPTClient(BaseFakeChatGPTClient):
        register_emails = []

        def register_complete_flow(self, email, *args, **kwargs):
            self.__class__.register_emails.append(email)
            return True, "ok"

        def reuse_session_and_get_tokens(self):
            return True, {
                "access_token": "session-access-token",
                "session_token": "session-token",
                "account_id": "acct-session",
                "workspace_id": "ws-session",
                "auth_provider": "openai",
                "expires": "2099-01-01T00:00:00Z",
                "user_id": "user-session",
                "user": {"id": "user-session"},
                "account": {"id": "acct-session"},
            }

    class FakeOAuthClient:
        login_calls = 0

        def __init__(self, *args, **kwargs):
            self.session = None
            self.last_error = ""
            self._log = lambda _msg: None

        def login_and_get_tokens(self, *args, **kwargs):
            FakeOAuthClient.login_calls += 1
            if FakeOAuthClient.login_calls == 1:
                self.last_error = "add_phone_required"
                return None
            return {
                "access_token": "oauth-access-token",
                "refresh_token": "oauth-refresh-token",
                "id_token": "oauth-id-token",
            }

        def _decode_oauth_session_cookie(self):
            return {"workspaces": [{"id": "ws-oauth"}]}

    _patch_common_dependencies(monkeypatch, FakeChatGPTClient, FakeOAuthClient)
    logs = []
    service = RecordingEmailService(
        [
            {"email": "first@example.com", "service_id": "svc-1"},
            {"email": "second@example.com", "service_id": "svc-2"},
        ]
    )

    result = AnyAutoRegistrationEngine(
        email_service=service,
        max_retries=3,
        callback_logger=logs.append,
    ).run()

    assert result["success"] is True
    assert result["refresh_token"] == "oauth-refresh-token"
    assert service.create_calls == 2
    assert FakeOAuthClient.login_calls == 2
    assert FakeChatGPTClient.register_emails == ["first@example.com", "second@example.com"]
    assert any("检测到 add_phone，按失败处理并更换邮箱重试" in line for line in logs)
