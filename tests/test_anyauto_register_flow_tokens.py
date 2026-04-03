from types import SimpleNamespace

from src.core.anyauto import register_flow as register_flow_module
from src.core.anyauto.register_flow import AnyAutoRegistrationEngine


class DummyEmailService:
    def create_email(self):
        return {"email": "Tester@Example.com", "service_id": "svc-1"}


class FakeCookie:
    def __init__(self, name, value):
        self.name = name
        self.value = value


class FakeChatGPTClient:
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

    def register_complete_flow(self, *args, **kwargs):
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


def _patch_common_dependencies(monkeypatch, oauth_client_cls):
    monkeypatch.setattr(register_flow_module, "ChatGPTClient", FakeChatGPTClient)
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


def test_run_enriches_refresh_token_after_session_reuse(monkeypatch):
    class FakeOAuthClient:
        login_calls = 0

        def __init__(self, *args, **kwargs):
            self.session = None
            self.last_error = ""
            self._log = lambda _msg: None

        def login_and_get_tokens(self, *args, **kwargs):
            FakeOAuthClient.login_calls += 1
            assert self.session is not None
            return {
                "access_token": "oauth-access-token",
                "refresh_token": "oauth-refresh-token",
                "id_token": "oauth-id-token",
            }

        def _decode_oauth_session_cookie(self):
            return {"workspaces": [{"id": "ws-oauth"}]}

    _patch_common_dependencies(monkeypatch, FakeOAuthClient)

    result = AnyAutoRegistrationEngine(email_service=DummyEmailService()).run()

    assert result["success"] is True
    assert result["access_token"] == "oauth-access-token"
    assert result["refresh_token"] == "oauth-refresh-token"
    assert result["id_token"] == "oauth-id-token"
    assert result["session_token"] == "session-token"
    assert result["account_id"] == "acct-session"
    assert result["workspace_id"] == "ws-session"
    assert result["metadata"]["oauth_token_enriched"] is True
    assert FakeOAuthClient.login_calls == 1


def test_run_keeps_session_tokens_when_oauth_enrichment_fails(monkeypatch):
    class FakeOAuthClient:
        login_calls = 0

        def __init__(self, *args, **kwargs):
            self.session = None
            self.last_error = ""
            self._log = lambda _msg: None

        def login_and_get_tokens(self, *args, **kwargs):
            FakeOAuthClient.login_calls += 1
            self.last_error = "oauth enrichment failed"
            return None

        def _decode_oauth_session_cookie(self):
            return {"workspaces": [{"id": "ws-oauth"}]}

    _patch_common_dependencies(monkeypatch, FakeOAuthClient)

    result = AnyAutoRegistrationEngine(email_service=DummyEmailService()).run()

    assert result["success"] is True
    assert result["access_token"] == "session-access-token"
    assert result["refresh_token"] == ""
    assert result["id_token"] == ""
    assert result["session_token"] == "session-token"
    assert result["account_id"] == "acct-session"
    assert result["workspace_id"] == "ws-session"
    assert result["metadata"]["oauth_token_enriched"] is False
    assert FakeOAuthClient.login_calls == 1
