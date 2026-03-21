from types import SimpleNamespace

from src.core.openai import team_invitation
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
