import base64
import json

from src.config.constants import EmailServiceType, OPENAI_API_ENDPOINTS, OPENAI_PAGE_TYPES
from src.core.http_client import OpenAIHTTPClient
from src.core.openai.oauth import OAuthStart
from src.core.register import RegistrationEngine, RegistrationResult
from src.services.base import BaseEmailService


class DummyResponse:
    def __init__(self, status_code=200, payload=None, text="", headers=None, on_return=None, url=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.headers = headers or {}
        self.on_return = on_return
        self.url = url

    def json(self):
        if self._payload is None:
            raise ValueError("no json payload")
        return self._payload


class QueueSession:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []
        self.cookies = {}

    def get(self, url, **kwargs):
        return self._request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._request("POST", url, **kwargs)

    def request(self, method, url, **kwargs):
        return self._request(method.upper(), url, **kwargs)

    def close(self):
        return None

    def _request(self, method, url, **kwargs):
        self.calls.append({
            "method": method,
            "url": url,
            "kwargs": kwargs,
        })
        if not self.steps:
            raise AssertionError(f"unexpected request: {method} {url}")
        expected_method, expected_url, response = self.steps.pop(0)
        assert method == expected_method
        assert url == expected_url
        if callable(response):
            response = response(self)
        if not getattr(response, "url", ""):
            response.url = url
        if response.on_return:
            response.on_return(self)
        return response


class FakeEmailService(BaseEmailService):
    def __init__(self, codes):
        super().__init__(EmailServiceType.TEMPMAIL)
        self.codes = list(codes)
        self.otp_requests = []

    def create_email(self, config=None):
        return {
            "email": "tester@example.com",
            "service_id": "mailbox-1",
        }

    def get_verification_code(self, email, email_id=None, timeout=120, pattern=r"(?<!\d)(\d{6})(?!\d)", otp_sent_at=None):
        self.otp_requests.append({
            "email": email,
            "email_id": email_id,
            "otp_sent_at": otp_sent_at,
        })
        if not self.codes:
            raise AssertionError("no verification code queued")
        return self.codes.pop(0)

    def list_emails(self, **kwargs):
        return []

    def delete_email(self, email_id):
        return True

    def check_health(self):
        return True


class FakeOAuthManager:
    def __init__(self):
        self.start_calls = 0
        self.callback_calls = []

    def start_oauth(self):
        self.start_calls += 1
        return OAuthStart(
            auth_url=f"https://auth.example.test/flow/{self.start_calls}",
            state=f"state-{self.start_calls}",
            code_verifier=f"verifier-{self.start_calls}",
            redirect_uri="http://localhost:1455/auth/callback",
        )

    def handle_callback(self, callback_url, expected_state, code_verifier):
        self.callback_calls.append({
            "callback_url": callback_url,
            "expected_state": expected_state,
            "code_verifier": code_verifier,
        })
        return {
            "account_id": "acct-1",
            "access_token": "access-1",
            "refresh_token": "refresh-1",
            "id_token": "id-1",
        }


class FakeOpenAIClient:
    def __init__(self, sessions, sentinel_tokens):
        self._sessions = list(sessions)
        self._session_index = 0
        self._session = self._sessions[0]
        self._sentinel_tokens = list(sentinel_tokens)

    @property
    def session(self):
        return self._session

    def check_ip_location(self):
        return True, "US"

    def check_sentinel(self, did):
        if not self._sentinel_tokens:
            raise AssertionError("no sentinel token queued")
        return self._sentinel_tokens.pop(0)

    def close(self):
        if self._session_index + 1 < len(self._sessions):
            self._session_index += 1
            self._session = self._sessions[self._session_index]


def _workspace_cookie(workspace_id):
    payload = base64.urlsafe_b64encode(
        json.dumps({"workspaces": [{"id": workspace_id}]}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{payload}.sig"


def _auth_cookie(payload):
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{encoded}.sig"


def _make_test_jwt(payload):
    header = base64.urlsafe_b64encode(
        json.dumps({"alg": "none", "typ": "JWT"}).encode("utf-8")
    ).decode("ascii").rstrip("=")
    body = base64.urlsafe_b64encode(
        json.dumps(payload).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{header}.{body}.signature"


def _response_with_did(did):
    return DummyResponse(
        status_code=200,
        text="ok",
        on_return=lambda session: session.cookies.__setitem__("oai-did", did),
    )


def _response_with_login_cookies(workspace_id="ws-1", session_token="session-1"):
    def setter(session):
        session.cookies["oai-client-auth-session"] = _workspace_cookie(workspace_id)
        session.cookies["__Secure-next-auth.session-token"] = session_token

    return DummyResponse(status_code=200, payload={}, on_return=setter)


def _response_with_chunked_login_cookies(workspace_id="ws-1", chunk_a="chunk-a", chunk_b="chunk-b"):
    def setter(session):
        session.cookies["oai-client-auth-session"] = _workspace_cookie(workspace_id)
        session.cookies["__Secure-authjs.session-token.0"] = chunk_a
        session.cookies["__Secure-authjs.session-token.1"] = chunk_b

    return DummyResponse(status_code=200, payload={}, on_return=setter)


def _response_with_auth_cookie_payload(payload, session_token="session-1"):
    def setter(session):
        session.cookies["oai-client-auth-session"] = _auth_cookie(payload)
        session.cookies["__Secure-next-auth.session-token"] = session_token

    return DummyResponse(status_code=200, payload={}, on_return=setter)


def test_check_sentinel_sends_non_empty_pow(monkeypatch):
    session = QueueSession([
        ("POST", OPENAI_API_ENDPOINTS["sentinel"], DummyResponse(payload={"token": "sentinel-token"})),
    ])
    client = OpenAIHTTPClient()
    client._session = session

    monkeypatch.setattr(
        "src.core.http_client.build_sentinel_pow_token",
        lambda user_agent: "gAAAAACpow-token",
    )

    token = client.check_sentinel("device-1")

    assert token == "sentinel-token"
    body = json.loads(session.calls[0]["kwargs"]["data"])
    assert body["id"] == "device-1"
    assert body["flow"] == "authorize_continue"
    assert body["p"] == "gAAAAACpow-token"


def test_run_registers_then_relogs_to_fetch_token():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies()),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/continue"}),
        ),
        (
            "GET",
            "https://auth.example.test/continue",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-2&state=state-2"},
            ),
        ),
    ])

    email_service = FakeEmailService(["123456", "654321"])
    engine = RegistrationEngine(email_service)
    fake_oauth = FakeOAuthManager()
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = fake_oauth

    result = engine.run()

    assert result.success is True
    assert result.source == "register"
    assert result.workspace_id == "ws-1"
    assert result.session_token == "session-1"
    assert fake_oauth.start_calls == 2
    assert len(email_service.otp_requests) == 2
    assert all(item["otp_sent_at"] is not None for item in email_service.otp_requests)
    assert sum(1 for call in session_one.calls if call["url"] == OPENAI_API_ENDPOINTS["send_otp"]) == 1
    assert sum(1 for call in session_two.calls if call["url"] == OPENAI_API_ENDPOINTS["send_otp"]) == 0
    assert sum(1 for call in session_one.calls if call["url"] == OPENAI_API_ENDPOINTS["select_workspace"]) == 0
    assert sum(1 for call in session_two.calls if call["url"] == OPENAI_API_ENDPOINTS["select_workspace"]) == 1
    relogin_start_body = json.loads(session_two.calls[1]["kwargs"]["data"])
    assert relogin_start_body["screen_hint"] == "login"
    assert relogin_start_body["username"]["value"] == "tester@example.com"
    password_verify_body = json.loads(session_two.calls[2]["kwargs"]["data"])
    assert password_verify_body == {"password": result.password}
    assert result.metadata["token_acquired_via_relogin"] is True


def test_existing_account_login_uses_auto_sent_otp_without_manual_send():
    session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies("ws-existing", "session-existing")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/continue-existing"}),
        ),
        (
            "GET",
            "https://auth.example.test/continue-existing",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-1&state=state-1"},
            ),
        ),
    ])

    email_service = FakeEmailService(["246810"])
    engine = RegistrationEngine(email_service)
    fake_oauth = FakeOAuthManager()
    engine.http_client = FakeOpenAIClient([session], ["sentinel-1"])
    engine.oauth_manager = fake_oauth

    result = engine.run()

    assert result.success is True
    assert result.source == "login"
    assert fake_oauth.start_calls == 1
    assert sum(1 for call in session.calls if call["url"] == OPENAI_API_ENDPOINTS["send_otp"]) == 0
    assert len(email_service.otp_requests) == 1
    assert email_service.otp_requests[0]["otp_sent_at"] is not None
    assert result.metadata["token_acquired_via_relogin"] is False


def test_sync_add_phone_result_sets_error_code():
    email_service = FakeEmailService(["123456"])
    engine = RegistrationEngine(email_service)
    result = engine._sync_add_phone_result(
        RegistrationResult(
            success=False,
            email="tester@example.com",
            error_message="当前账号进入 add_phone 页面，需要补充手机号后才能继续授权",
        )
    )

    assert result.error_code == "add_phone_required"
    assert "add_phone" in result.error_message


def test_register_password_uses_browser_like_headers_and_datadog_trace():
    session = QueueSession([
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
    ])

    email_service = FakeEmailService(["123456"])
    engine = RegistrationEngine(email_service)
    engine.session = session
    engine.email = "tester@example.com"

    success, password = engine._register_password("did-1", "sentinel-1")

    assert success is True
    assert password

    request = session.calls[0]
    headers = request["kwargs"]["headers"]
    payload = request["kwargs"]["json"]

    assert payload == {
        "username": "tester@example.com",
        "password": password,
    }
    assert headers["Origin"] == "https://auth.openai.com"
    assert headers["Referer"] == "https://auth.openai.com/create-account/password"
    assert headers["Content-Type"] == "application/json"
    assert headers["Sec-Fetch-Site"] == "same-origin"
    assert headers["Accept-Language"] == "en-US,en;q=0.9"
    assert "sec-ch-ua" in headers
    assert "traceparent" in headers
    assert "x-datadog-trace-id" in headers


def test_run_reassembles_chunked_session_cookie():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_chunked_login_cookies()),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/continue"}),
        ),
        (
            "GET",
            "https://auth.example.test/continue",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-2&state=state-2"},
            ),
        ),
    ])

    email_service = FakeEmailService(["123456", "654321"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = FakeOAuthManager()

    result = engine.run()

    assert result.success is True
    assert result.session_token == "chunk-achunk-b"
    assert "__Secure-authjs.session-token.0=chunk-a" in result.cookies
    assert "__Secure-authjs.session-token.1=chunk-b" in result.cookies


def test_run_falls_back_to_email_login_when_register_username_conflicts():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["register"],
            DummyResponse(
                status_code=400,
                payload={
                    "error": {
                        "message": "Failed to register username. Please try again.",
                        "code": "bad_request",
                    }
                },
                text='{"error":{"message":"Failed to register username. Please try again.","code":"bad_request"}}',
            ),
        ),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies("ws-existing", "session-existing")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/continue-existing"}),
        ),
        (
            "GET",
            "https://auth.example.test/continue-existing",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-1&state=state-1"},
            ),
        ),
    ])

    email_service = FakeEmailService(["246810"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = FakeOAuthManager()

    result = engine.run()

    assert result.success is True
    assert result.source == "login"
    assert result.password == ""
    assert result.session_token == "session-existing"
    assert len(email_service.otp_requests) == 1


def test_submit_signup_retries_when_auth_step_is_invalid():
    stale_session = QueueSession([
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(
                status_code=400,
                payload={
                    "error": {
                        "message": "Invalid authorization step.",
                        "code": "invalid_auth_step",
                    }
                },
                text='{"error":{"message":"Invalid authorization step.","code":"invalid_auth_step"}}',
            ),
        ),
    ])
    fresh_session = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
    ])

    email_service = FakeEmailService([])
    engine = RegistrationEngine(email_service)
    fake_oauth = FakeOAuthManager()
    engine.http_client = FakeOpenAIClient([stale_session, fresh_session], ["sentinel-1"])
    engine.oauth_manager = fake_oauth
    engine.email = "tester@example.com"
    engine.session = stale_session

    result = engine._submit_signup_form("stale-did", "stale-sentinel")

    assert result.success is True
    assert result.page_type == OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]
    assert fake_oauth.start_calls == 1
    retry_body = json.loads(fresh_session.calls[1]["kwargs"]["data"])
    assert retry_body["screen_hint"] == "signup"
    assert retry_body["username"]["value"] == "tester@example.com"


def test_new_registration_handles_consent_after_otp():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["CODEX_CONSENT"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            _response_with_login_cookies("ws-new", "session-new"),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/continue"}),
        ),
        (
            "GET",
            "https://auth.example.test/continue",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-2&state=state-2"},
            ),
        ),
    ])

    email_service = FakeEmailService(["111111", "222222"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = FakeOAuthManager()

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-new"
    assert result.session_token == "session-new"
    assert result.source == "register"
    consent_call = [call for call in session_two.calls if call["url"] == OPENAI_API_ENDPOINTS["signup"]][-1]
    referer = consent_call["kwargs"]["headers"].get("referer") or consent_call["kwargs"]["headers"].get("Referer") or ""
    assert consent_call["url"] == OPENAI_API_ENDPOINTS["signup"]
    assert "codex/consent" in referer


def test_new_registration_fallback_consent_when_cookie_lacks_workspace():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            _response_with_login_cookies("ws-fallback", "session-fallback"),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/continue"}),
        ),
        (
            "GET",
            "https://auth.example.test/continue",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-2&state=state-2"},
            ),
        ),
    ])

    email_service = FakeEmailService(["111111", "222222"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = FakeOAuthManager()

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-fallback"
    assert result.session_token == "session-fallback"
    assert result.source == "register"


def test_workspace_select_parser_accepts_json_continue_url():
    engine = RegistrationEngine(FakeEmailService([]))
    response = DummyResponse(
        status_code=200,
        payload={"continue_url": "https://auth.example.test/continue"},
        headers={"content-type": "application/json"},
    )

    result = engine._parse_workspace_selection_response(response)

    assert result.success is True
    assert result.continue_url == "https://auth.example.test/continue"
    assert result.error_message == ""


def test_workspace_select_parser_accepts_redirect_location():
    engine = RegistrationEngine(FakeEmailService([]))
    response = DummyResponse(
        status_code=302,
        headers={"Location": "/continue", "content-type": "text/html"},
        text="<html>redirect</html>",
        url=OPENAI_API_ENDPOINTS["select_workspace"],
    )

    result = engine._parse_workspace_selection_response(response)

    assert result.success is True
    assert result.continue_url == "https://auth.openai.com/continue"


def test_workspace_select_parser_rejects_non_json_200_response():
    engine = RegistrationEngine(FakeEmailService([]))
    response = DummyResponse(
        status_code=200,
        headers={"content-type": "text/html; charset=utf-8"},
        text="<html><body>consent</body></html>",
        url="https://auth.openai.com/api/accounts/workspace/select?code=secret",
    )

    result = engine._parse_workspace_selection_response(response)

    assert result.success is False
    assert result.error_message == "选择 Workspace 失败：响应不是 JSON/重定向"
    assert any(
        "response_url=https://auth.openai.com/api/accounts/workspace/select" in item
        and "body_kind=html" in item
        and "has_location=False" in item
        for item in engine.logs
    )


def test_workspace_select_parser_rejects_json_without_continue_url():
    engine = RegistrationEngine(FakeEmailService([]))
    response = DummyResponse(
        status_code=200,
        payload={"status": "ok"},
        headers={"content-type": "application/json"},
    )

    result = engine._parse_workspace_selection_response(response)

    assert result.success is False
    assert result.error_message == "选择 Workspace 失败：响应缺少 continue_url"


def test_workspace_select_parser_surfaces_json_error_detail_for_409():
    engine = RegistrationEngine(FakeEmailService([]))
    response = DummyResponse(
        status_code=409,
        payload={
            "error": {
                "code": "workspace_already_selected",
                "message": "Workspace already selected for this session.",
            }
        },
        headers={"content-type": "application/json"},
    )

    result = engine._parse_workspace_selection_response(response)

    assert result.success is False
    assert result.status_code == 409
    assert result.error_code == "workspace_already_selected"
    assert result.error_detail == "Workspace already selected for this session."
    assert result.error_message == "选择 Workspace 失败：Workspace already selected for this session."
    assert any("workspace/select 返回错误码: workspace_already_selected" in item for item in engine.logs)


def test_run_recovers_when_workspace_select_invalid_state_resumes_current_session():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies()),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(
                status_code=409,
                payload={
                    "error": {
                        "code": "invalid_state",
                        "message": "Invalid session. Please start over.",
                    }
                },
                headers={"content-type": "application/json"},
            ),
        ),
        ("GET", "https://auth.example.test/flow/3", _response_with_did("did-3")),
        (
            "GET",
            "https://auth.example.test/flow/3",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-3&state=state-3"},
            ),
        ),
    ])

    email_service = FakeEmailService(["123456", "654321"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2", "sentinel-3"])
    engine.oauth_manager = FakeOAuthManager()

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-1"
    assert sum(1 for call in session_two.calls if call["url"] == OPENAI_API_ENDPOINTS["select_workspace"]) == 1
    assert any(
        call["url"] == "https://auth.example.test/flow/3"
        and call["kwargs"].get("allow_redirects") is False
        for call in session_two.calls
    )
    assert any("session_resume" in item for item in result.logs)


def test_run_recovers_when_invalid_state_falls_back_to_same_account_reauth():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies()),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(
                status_code=409,
                payload={
                    "error": {
                        "code": "invalid_state",
                        "message": "Invalid session. Please start over.",
                    }
                },
                headers={"content-type": "application/json"},
            ),
        ),
        ("GET", "https://auth.example.test/flow/3", _response_with_did("did-3")),
        (
            "GET",
            "https://auth.example.test/flow/3",
            DummyResponse(status_code=200, text="<html>login challenge</html>"),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["CODEX_CONSENT"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            _response_with_login_cookies("ws-reauth", "session-1"),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/continue-recovered"}),
        ),
        (
            "GET",
            "https://auth.example.test/continue-recovered",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-3&state=state-3"},
            ),
        ),
    ])

    email_service = FakeEmailService(["123456", "654321", "789012"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2", "sentinel-3"])
    engine.oauth_manager = FakeOAuthManager()

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-reauth"
    assert len(email_service.otp_requests) == 3
    assert sum(1 for call in session_two.calls if call["url"] == OPENAI_API_ENDPOINTS["password_verify"]) == 2
    assert any("same_account_reauth" in item for item in result.logs)
    assert sum(1 for call in session_two.calls if call["url"] == "https://auth.example.test/flow/3") == 2
    recovered_select_call = [
        call for call in session_two.calls
        if call["url"] == OPENAI_API_ENDPOINTS["select_workspace"]
    ][-1]
    assert '"workspace_id":"ws-reauth"' in recovered_select_call["kwargs"]["data"]


def test_run_recovery_reuses_previous_workspace_id_when_reauth_cookie_loses_workspaces():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies("ws-original")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(
                status_code=409,
                payload={
                    "error": {
                        "code": "invalid_state",
                        "message": "Invalid session. Please start over.",
                    }
                },
                headers={"content-type": "application/json"},
            ),
        ),
        ("GET", "https://auth.example.test/flow/3", _response_with_did("did-3")),
        (
            "GET",
            "https://auth.example.test/flow/3",
            DummyResponse(status_code=200, text="<html>login challenge</html>"),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["CODEX_CONSENT"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(
                payload={},
                on_return=lambda session: session.cookies.__setitem__(
                    "oai-client-auth-session",
                    _auth_cookie(
                        {
                            "app_name_enum": "codex",
                            "session_id": "session-1",
                            "signup_source": "register",
                        }
                    ),
                ),
            ),
        ),
        (
            "GET",
            "https://auth.example.test/flow/3",
            DummyResponse(
                status_code=200,
                text="<html>login challenge</html>",
                on_return=lambda session: session.cookies.__setitem__(
                    "oai-client-auth-session",
                    _auth_cookie(
                        {
                            "app_name_enum": "codex",
                            "session_id": "session-1",
                            "signup_source": "register",
                        }
                    ),
                ),
            ),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(payload={"continue_url": "https://auth.example.test/continue-recovered"}),
        ),
        (
            "GET",
            "https://auth.example.test/continue-recovered",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-3&state=state-3"},
            ),
        ),
    ])

    email_service = FakeEmailService(["123456", "654321", "789012"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2", "sentinel-3"])
    engine.oauth_manager = FakeOAuthManager()

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-original"
    recovered_select_call = [
        call for call in session_two.calls
        if call["url"] == OPENAI_API_ENDPOINTS["select_workspace"]
    ][-1]
    assert '"workspace_id":"ws-original"' in recovered_select_call["kwargs"]["data"]
    assert any("继续沿用已确认的 Workspace ID 重试" in item for item in result.logs)


def test_run_reports_clear_error_when_invalid_state_same_account_reauth_still_fails():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies()),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(
                status_code=409,
                payload={
                    "error": {
                        "code": "invalid_state",
                        "message": "Invalid session. Please start over.",
                    }
                },
                headers={"content-type": "application/json"},
            ),
        ),
        ("GET", "https://auth.example.test/flow/3", _response_with_did("did-3")),
        (
            "GET",
            "https://auth.example.test/flow/3",
            DummyResponse(status_code=200, text="<html>login challenge</html>"),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
    ])

    email_service = FakeEmailService(["123456", "654321"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2", "sentinel-3"])
    engine.oauth_manager = FakeOAuthManager()

    result = engine.run()

    assert result.success is False
    assert "已尝试同号重新认证但仍未恢复授权流程" in result.error_message
    assert "login_password" in result.error_message
    assert any("same_account_reauth" in item for item in result.logs)


def test_run_recovers_invalid_state_via_chatgpt_session_exchange_fallback():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    access_token = _make_test_jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "acct-session",
            }
        }
    )
    session_exchange_url = (
        "https://chatgpt.com/api/auth/session"
        "?exchange_workspace_token=true&workspace_id=ws-1&reason=setCurrentAccount"
    )
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies("ws-1", "session-old")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(
                status_code=409,
                payload={
                    "error": {
                        "code": "invalid_state",
                        "message": "Invalid session. Please start over.",
                    }
                },
                headers={"content-type": "application/json"},
            ),
        ),
        ("GET", "https://auth.example.test/flow/3", _response_with_did("did-3")),
        (
            "GET",
            "https://auth.example.test/flow/3",
            DummyResponse(status_code=200, text="<html>login challenge</html>"),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["CODEX_CONSENT"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            _response_with_login_cookies("ws-1", "session-old"),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(
                status_code=409,
                payload={
                    "error": {
                        "code": "invalid_state",
                        "message": "Invalid session. Please start over.",
                    }
                },
                headers={"content-type": "application/json"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/flow/3",
            DummyResponse(status_code=200, text="<html>still no callback</html>"),
        ),
        (
            "GET",
            session_exchange_url,
            DummyResponse(
                payload={
                    "accessToken": access_token,
                    "sessionToken": "session-refreshed",
                    "account": {"id": "acct-session"},
                }
            ),
        ),
    ])

    email_service = FakeEmailService(["123456", "654321", "789012"])
    engine = RegistrationEngine(email_service)
    fake_oauth = FakeOAuthManager()
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2", "sentinel-3"])
    engine.oauth_manager = fake_oauth

    result = engine.run()

    assert result.success is False
    assert result.workspace_id == "ws-1"
    assert result.account_id == "acct-session"
    assert result.access_token == access_token
    assert result.session_token == "session-refreshed"
    assert result.error_code == "missing_refresh_token"
    assert "refresh_token" in result.error_message
    assert fake_oauth.callback_calls == []
    exchange_call = [call for call in session_two.calls if call["url"] == session_exchange_url][-1]
    assert exchange_call["kwargs"]["headers"]["Cookie"] == "__Secure-next-auth.session-token=session-old"
    assert any("ChatGPT session" in item for item in result.logs)


def test_recover_tokens_via_session_exchange_reports_missing_access_token():
    workspace_id = "ws-1"
    session_exchange_url = (
        "https://chatgpt.com/api/auth/session"
        "?exchange_workspace_token=true&workspace_id=ws-1&reason=setCurrentAccount"
    )
    session = QueueSession([
        (
            "GET",
            session_exchange_url,
            DummyResponse(
                payload={
                    "sessionToken": "session-refreshed",
                    "account": {"id": "acct-session"},
                }
            ),
        ),
    ])
    session.cookies["__Secure-next-auth.session-token"] = "session-old"

    engine = RegistrationEngine(FakeEmailService([]))
    engine.session = session

    token_info, error_message = engine._recover_tokens_via_session_exchange(
        workspace_id,
        label="workspace/select invalid_state 恢复",
    )

    assert token_info is None
    assert "accessToken" in error_message
    assert session.calls[0]["kwargs"]["headers"]["Cookie"] == "__Secure-next-auth.session-token=session-old"


def test_run_handles_workspace_select_redirect_response():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies()),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(
                status_code=302,
                headers={"Location": "https://auth.example.test/continue"},
                text="<html>redirect</html>",
            ),
        ),
        (
            "GET",
            "https://auth.example.test/continue",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-2&state=state-2"},
            ),
        ),
    ])

    email_service = FakeEmailService(["123456", "654321"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = FakeOAuthManager()

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-1"
    workspace_call = session_two.calls[4]
    assert workspace_call["kwargs"]["allow_redirects"] is False


def test_run_fails_clearly_when_otp_requires_add_phone():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["ADD_PHONE"]}}),
        ),
    ])

    email_service = FakeEmailService(["123456", "654321"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = FakeOAuthManager()

    result = engine.run()

    assert result.success is False
    assert result.error_message == "当前账号进入 add_phone 页面，需要补充手机号后才能继续授权"
    assert not any(call["url"] == OPENAI_API_ENDPOINTS["select_workspace"] for call in session_two.calls)


def test_run_recovers_when_workspace_select_conflicts_but_oauth_entry_redirects():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies()),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(
                status_code=409,
                payload={
                    "error": {
                        "code": "workspace_already_selected",
                        "message": "Workspace already selected for this session.",
                    }
                },
                headers={"content-type": "application/json"},
            ),
        ),
        (
            "GET",
            "https://auth.example.test/flow/2",
            DummyResponse(
                status_code=302,
                headers={"Location": "http://localhost:1455/auth/callback?code=code-2&state=state-2"},
            ),
        ),
    ])

    email_service = FakeEmailService(["123456", "654321"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = FakeOAuthManager()

    result = engine.run()

    assert result.success is True
    assert result.workspace_id == "ws-1"
    assert any("workspace/select 返回冲突，尝试回到原始 OAuth 入口继续流程" in item for item in result.logs)


def test_run_fails_clearly_when_workspace_cookie_has_no_workspaces():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["validate_otp"],
            _response_with_auth_cookie_payload({"name": "tester", "session_id": "session-1"}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            _response_with_auth_cookie_payload({"name": "tester", "session_id": "session-1"}),
        ),
    ])

    email_service = FakeEmailService(["123456", "654321"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = FakeOAuthManager()

    result = engine.run()

    assert result.success is False
    assert result.error_message == "服务端尚未下发 workspace 信息，无法继续授权"
    assert not any(call["url"] == OPENAI_API_ENDPOINTS["select_workspace"] for call in session_two.calls)
    assert any("授权 Cookie 里没有 workspace 信息" in item for item in result.logs)


def test_run_fails_gracefully_when_workspace_select_returns_html():
    session_one = QueueSession([
        ("GET", "https://auth.example.test/flow/1", _response_with_did("did-1")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["PASSWORD_REGISTRATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["register"], DummyResponse(payload={})),
        ("GET", OPENAI_API_ENDPOINTS["send_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], DummyResponse(payload={})),
        ("POST", OPENAI_API_ENDPOINTS["create_account"], DummyResponse(payload={})),
    ])
    session_two = QueueSession([
        ("GET", "https://auth.example.test/flow/2", _response_with_did("did-2")),
        (
            "POST",
            OPENAI_API_ENDPOINTS["signup"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["LOGIN_PASSWORD"]}}),
        ),
        (
            "POST",
            OPENAI_API_ENDPOINTS["password_verify"],
            DummyResponse(payload={"page": {"type": OPENAI_PAGE_TYPES["EMAIL_OTP_VERIFICATION"]}}),
        ),
        ("POST", OPENAI_API_ENDPOINTS["validate_otp"], _response_with_login_cookies()),
        (
            "POST",
            OPENAI_API_ENDPOINTS["select_workspace"],
            DummyResponse(
                status_code=200,
                headers={"content-type": "text/html; charset=utf-8"},
                text="<html><body>redirecting</body></html>",
            ),
        ),
    ])

    email_service = FakeEmailService(["123456", "654321"])
    engine = RegistrationEngine(email_service)
    engine.http_client = FakeOpenAIClient([session_one, session_two], ["sentinel-1", "sentinel-2"])
    engine.oauth_manager = FakeOAuthManager()

    result = engine.run()

    assert result.success is False
    assert "选择 Workspace 失败：响应不是 JSON/重定向" in result.error_message
    assert "ChatGPT SSO 兜底也失败" in result.error_message
    assert any("workspace/select 响应诊断" in item for item in result.logs)
