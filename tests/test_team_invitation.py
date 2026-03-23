import base64
import json
from types import SimpleNamespace

from src.core.openai import team_invitation
from src.core.openai.account_sensitive_info import SENSITIVE_SESSION_PAYLOAD_KEY
from src.database.models import Account


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


class DummySession:
    def __init__(self, response):
        self.response = response
        self.calls = []
        self.closed = False

    def request(self, method, url, **kwargs):
        if isinstance(self.response, Exception):
            raise self.response
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        return self.response

    def close(self):
        self.closed = True


def make_test_jwt(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("ascii").rstrip("=")
    return f"header.{encoded}.signature"


def test_discover_team_account_extracts_active_team(monkeypatch):
    payload = {
        "accounts": {
            "acct-free": {
                "account": {"plan_type": "free"},
                "entitlement": {},
            },
            "acct-team": {
                "account": {
                    "plan_type": "team",
                    "workspace_id": "ws-team",
                    "name": "My Team",
                    "account_user_role": "owner",
                },
                "entitlement": {
                    "subscription_plan": "chatgptteamplan",
                    "has_active_subscription": True,
                    "expires_at": "2030-01-01T00:00:00Z",
                },
            },
        }
    }

    monkeypatch.setattr(team_invitation.cffi_requests, "get", lambda *args, **kwargs: DummyResponse(payload=payload))
    admin = Account(email="admin@example.com", email_service="moe_mail", access_token="token-1")

    result = team_invitation.discover_team_account(admin)

    assert result["success"] is True
    assert result["account"]["team_account_id"] == "acct-team"
    assert result["account"]["team_workspace_id"] == "ws-team"
    assert result["account"]["has_active_subscription"] is True


def test_discover_team_account_retries_with_session_proxy_mode_on_tls_proxy_error(monkeypatch):
    payload = {
        "accounts": {
            "acct-team": {
                "account": {"plan_type": "team", "workspace_id": "ws-team"},
                "entitlement": {"has_active_subscription": True},
            },
        }
    }
    session = DummySession(DummyResponse(payload=payload))

    def fake_get(*args, **kwargs):
        raise RuntimeError(
            "Failed to perform, curl: (35) TLS connect error: "
            "error:00000000:OPENSSL_internal:invalid library"
        )

    monkeypatch.setattr(team_invitation.cffi_requests, "get", fake_get)
    monkeypatch.setattr(team_invitation.cffi_requests, "Session", lambda **kwargs: session)
    admin = Account(email="admin@example.com", email_service="moe_mail", access_token="token-1")

    result = team_invitation.discover_team_account(admin, proxy="http://127.0.0.1:7890")

    assert result["success"] is True
    assert result["account"]["team_account_id"] == "acct-team"
    assert session.calls[0]["method"] == "GET"
    assert session.calls[0]["url"].endswith("/accounts/check/v4-2023-04-27")
    assert session.calls[0]["kwargs"]["headers"]["Authorization"] == "Bearer token-1"


def test_discover_team_account_retries_with_stdlib_requests_after_session_proxy_error(monkeypatch):
    payload = {
        "accounts": {
            "acct-team": {
                "account": {"plan_type": "team", "workspace_id": "ws-team"},
                "entitlement": {"has_active_subscription": True},
            },
        }
    }
    session = DummySession(
        RuntimeError(
            "Failed to perform, curl: (35) TLS connect error: "
            "error:00000000:OPENSSL_internal:invalid library"
        )
    )
    captured = {}

    def fake_get(*args, **kwargs):
        raise RuntimeError(
            "Failed to perform, curl: (35) TLS connect error: "
            "error:00000000:OPENSSL_internal:invalid library"
        )

    class FakeStdSession:
        def __init__(self):
            self.trust_env = True
            self.proxies = {}

        def request(self, method, url, **kwargs):
            captured["method"] = method
            captured["url"] = url
            captured["kwargs"] = kwargs
            captured["trust_env"] = self.trust_env
            captured["proxies"] = dict(self.proxies)
            return DummyResponse(payload=payload)

        def close(self):
            return None

    monkeypatch.setattr(team_invitation.cffi_requests, "get", fake_get)
    monkeypatch.setattr(team_invitation.cffi_requests, "Session", lambda **kwargs: session)
    monkeypatch.setattr(team_invitation.std_requests, "Session", FakeStdSession)
    admin = Account(email="admin@example.com", email_service="moe_mail", access_token="token-1")

    result = team_invitation.discover_team_account(admin, proxy="http://127.0.0.1:7890")

    assert result["success"] is True
    assert result["account"]["team_account_id"] == "acct-team"
    assert captured["method"] == "GET"
    assert captured["url"].endswith("/accounts/check/v4-2023-04-27")
    assert captured["trust_env"] is False
    assert captured["proxies"] == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }
    assert "Mozilla/5.0" in captured["kwargs"]["headers"]["User-Agent"]


def test_discover_team_account_does_not_fall_back_to_direct_when_explicit_proxy_is_set(monkeypatch):
    session = DummySession(
        RuntimeError(
            "Failed to perform, curl: (35) TLS connect error: "
            "error:00000000:OPENSSL_internal:invalid library"
        )
    )
    calls = []

    def fake_get(*args, **kwargs):
        raise RuntimeError(
            "Failed to perform, curl: (35) TLS connect error: "
            "error:00000000:OPENSSL_internal:invalid library"
        )

    class FakeStdSession:
        def __init__(self):
            self.trust_env = True
            self.proxies = {}

        def request(self, method, url, **kwargs):
            calls.append(
                {
                    "method": method,
                    "url": url,
                    "kwargs": kwargs,
                    "trust_env": self.trust_env,
                    "proxies": dict(self.proxies),
                }
            )
            raise RuntimeError("proxy eof")

        def close(self):
            return None

    monkeypatch.setattr(team_invitation.cffi_requests, "get", fake_get)
    monkeypatch.setattr(team_invitation.cffi_requests, "Session", lambda **kwargs: session)
    monkeypatch.setattr(team_invitation.std_requests, "Session", FakeStdSession)
    admin = Account(email="admin@example.com", email_service="moe_mail", access_token="token-1")

    result = team_invitation.discover_team_account(admin, proxy="http://127.0.0.1:7890")

    assert result["success"] is False
    assert len(calls) == 1
    assert calls[0]["trust_env"] is False
    assert calls[0]["proxies"] == {
        "http": "http://127.0.0.1:7890",
        "https": "http://127.0.0.1:7890",
    }
    assert "proxy eof" in result["error"]


def test_send_team_invitation_uses_expected_request_shape(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, json=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers
        captured["json"] = json
        return DummyResponse(payload={"ok": True}, status_code=200)

    monkeypatch.setattr(team_invitation.cffi_requests, "post", fake_post)
    admin = Account(email="admin@example.com", email_service="moe_mail", access_token="token-1")

    success, message = team_invitation.send_team_invitation(admin, "team-acct", "member@example.com")

    assert success is True
    assert "成功" in message
    assert captured["url"].endswith("/backend-api/accounts/team-acct/invites")
    assert captured["headers"]["Authorization"] == "Bearer token-1"
    assert captured["headers"]["chatgpt-account-id"] == "team-acct"
    assert captured["json"] == {
        "email_addresses": ["member@example.com"],
        "role": "standard-user",
        "resend_emails": True,
    }


def test_extract_invitation_link_handles_plaintext_and_html():
    plain = "Please join: https://chatgpt.com/invite/abc123?foo=bar"
    html = '<a href="https://chatgpt.com/workspace/invite/xyz">Join now</a>'
    multi = "ignore https://example.com/x and keep https://chatgpt.com/join/team-1"

    assert team_invitation.extract_invitation_link(plain) == "https://chatgpt.com/invite/abc123?foo=bar"
    assert team_invitation.extract_invitation_link(html) == "https://chatgpt.com/workspace/invite/xyz"
    assert team_invitation.extract_invitation_link(multi) == "https://chatgpt.com/join/team-1"


def test_resolve_session_token_falls_back_to_sensitive_payload():
    account = Account(
        email="member@example.com",
        email_service="moe_mail",
        session_token="",
        extra_data={
            SENSITIVE_SESSION_PAYLOAD_KEY: {
                "sessionToken": "payload-session-token",
            }
        },
    )

    assert team_invitation.resolve_session_token(account) == "payload-session-token"


def test_resolve_session_token_falls_back_to_cookie_string():
    account = Account(
        email="member@example.com",
        email_service="moe_mail",
        session_token="",
        cookies="foo=bar; __Secure-next-auth.session-token=cookie-session-token; baz=qux",
    )

    assert team_invitation.resolve_session_token(account) == "cookie-session-token"


def test_resolve_session_token_reassembles_chunked_cookie_string():
    account = Account(
        email="member@example.com",
        email_service="moe_mail",
        session_token="",
        cookies=(
            "foo=bar; "
            "__Secure-authjs.session-token.0=chunk-a; "
            "__Secure-authjs.session-token.1=chunk-b; "
            "baz=qux"
        ),
    )

    session_cookie = team_invitation.resolve_session_cookie(account)

    assert session_cookie["name"] == "__Secure-authjs.session-token"
    assert session_cookie["value"] == "chunk-achunk-b"
    assert team_invitation.resolve_session_token(account) == "chunk-achunk-b"


def test_refresh_member_team_token_uses_sensitive_payload_session_token(monkeypatch):
    captured = {}
    access_token = make_test_jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "team-acct-1",
                "chatgpt_user_id": "user-1",
            }
        }
    )

    def fake_get(url, headers=None, **kwargs):
        captured["url"] = url
        captured["headers"] = headers
        return DummyResponse(
            payload={
                "accessToken": access_token,
                "sessionToken": "refreshed-session-token",
                "expires": "2030-01-01T00:00:00Z",
                "account": {"id": "team-acct-1"},
            }
        )

    monkeypatch.setattr(team_invitation.cffi_requests, "get", fake_get)
    member = Account(
        email="member@example.com",
        email_service="moe_mail",
        session_token="",
        extra_data={
            SENSITIVE_SESSION_PAYLOAD_KEY: {
                "sessionToken": "payload-session-token",
            }
        },
    )

    result = team_invitation.refresh_member_team_token(member, "team-acct-1")

    assert result["success"] is True
    assert captured["url"].endswith("workspace_id=team-acct-1&reason=setCurrentAccount")
    assert captured["headers"]["Cookie"] == "__Secure-next-auth.session-token=payload-session-token"
    assert result["session_token"] == "refreshed-session-token"
    assert result["account_id"] == "team-acct-1"
    assert result["user_id"] == "user-1"


def test_refresh_member_team_token_uses_chunked_authjs_cookie_name(monkeypatch):
    captured = {}
    access_token = make_test_jwt(
        {
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "team-acct-1",
                "chatgpt_user_id": "user-1",
            }
        }
    )

    def fake_get(url, headers=None, **kwargs):
        captured["headers"] = headers
        return DummyResponse(
            payload={
                "accessToken": access_token,
                "sessionToken": "refreshed-session-token",
                "expires": "2030-01-01T00:00:00Z",
                "account": {"id": "team-acct-1"},
            }
        )

    monkeypatch.setattr(team_invitation.cffi_requests, "get", fake_get)
    member = Account(
        email="member@example.com",
        email_service="moe_mail",
        session_token="",
        cookies="__Secure-authjs.session-token.0=chunk-a; __Secure-authjs.session-token.1=chunk-b",
    )

    result = team_invitation.refresh_member_team_token(member, "team-acct-1")

    assert result["success"] is True
    assert captured["headers"]["Cookie"] == "__Secure-authjs.session-token=chunk-achunk-b"
